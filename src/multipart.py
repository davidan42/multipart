# -*- coding: utf-8 -*-

'''
This module provides a parser for the multipart/form-data format. It can read
from a file, a socket or an WSGI environment. The parser can be used to replace
cgi.FieldStorage (without the bugs) and works with Python 2.5+ and 3.x (2to3).

Licence (MIT)
-------------

    Copyright (c) 2010, Marcel Hellkamp.

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
    THE SOFTWARE.

Features
--------

[x] works in Python 2.5+ and 3.x (2to3).
 [x] consumes bytes regardless of Python version.
[x] parses multipart and url-encoded data.
 [ ] support for base46 and quoted-printable transfer encoding
[x] produces useful error messages.
 [x] supports uploads of unknown size (missing content-length header).
[x] supports memory and disk resource limits to prevent DOS attacks.
 [x] uses fast memory mapped files (io.BytesIO) for small uploads.
 [x] uses temporary files for big uploads.
[x] has no dependencies.
[x] has 100% test coverage.

'''

from tempfile import TemporaryFile
from wsgiref.headers import Headers
import re, sys, io
import urlparse
from bottle import MultiDict
##############################################################################
################################ Header Parser ################################
##############################################################################

_special = re.escape('()<>@,;:\\"/[]?={} \t\n\r')
_re_special = re.compile('[%s]' % _special)
_qstr = '"(?:\\\\.|[^"])*"' # Quoted string
_value = '(?:[^%s]+|%s)' % (_special, _qstr) # Save or quoted string
_option = '(?:;|^)\s*([^%s]+)\s*=\s*(%s)' % (_special, _value)
_re_option = re.compile(_option) # key=value part of an Content-Type like header

def header_quote(val):
    if not _re_special.match(val):
        return val
    return '"' + val.replace('\\','\\\\').replace('"','\\"') + '"'

def header_unquote(val, filename=False):
    if val[0] == val[-1] == '"':
        val = val[1:-1]
        if val[1:3] == ':\\' or val[:2] == '\\\\': 
            val = val.split('\\')[-1] # fix ie6 bug: full path --> filename
        return val.replace('\\\\','\\').replace('\\"','"')
    return val

def parse_options_header(header, options=None):
    if ';' not in header:
        return header.lower().strip(), {}
    ctype, tail = header.split(';', 1)
    options = options or {}
    for match in _re_option.finditer(tail):
        key = header_unquote(match.group(1)).lower().strip()
        value = header_unquote(match.group(2), key=='filename')
        options[key] = value
    return ctype, options

##############################################################################
################################## Multipart ##################################
##############################################################################

def tob(data, enc='utf8'): # Convert strings to bytes (py2 and py3)
    return data.encode(enc) if isinstance(data, unicode) else data

def copy_file(stream, target, maxread=-1, buffer_size=2*16):
    ''' Read from :stream and write to :target until :maxread or EOF. '''
    size, read = 0, stream.read
    while 1:
        to_read = buffer_size if maxread < 0 else min(buffer_size, maxread-size)
        part = read(to_read)
        if not part: return size
        target.write(part)
        size += len(part)


class MultipartError(ValueError): pass


class MultipartParser(object):
    
    def __init__(self, stream, boundary, content_length=-1,
                 disk_limit=2**30, mem_limit=2**20, memfile_limit=2**18,
                 buffer_size=2**16, charset='latin9'):
        ''' Parse a multipart/form-data byte stream. This object is an iterator
            over the parts of the message.
            
            :param stream: A file-like stream. Must implement ``.read(size)``.
            :param boundary: The multipart boundary as a byte string.
            :param content_length: The maximum number of bytes to read.
        '''
        self.stream, self.boundary = stream, boundary
        self.content_length = content_length
        self.disk_limit = disk_limit
        self.memfile_limit = memfile_limit
        self.mem_limit = min(mem_limit, self.disk_limit)
        self.buffer_size = min(buffer_size, self.mem_limit)
        self.charset = charset
        if self.buffer_size - 5 < len(boundary): # "--boundary--\n"
            raise MultipartError('Boundary does not fit into buffer_size.')
        self._done = []
        self._part_iter = None
    
    def __iter__(self):
        ''' Iterate over the parts of the multipart message. '''
        if not self._part_iter:
            self._part_iter = self._iterparse()
        for part in self._done:
            yield part
        for part in self._part_iter:
            self._done.append(part)
            yield part
    
    def parts(self):
        ''' Returns a list with all parts of the multipart message. '''
        return list(iter(self))
    
    def get(self, name, default=None):
        ''' Return the first part with that name or a default value (None). '''
        for part in self:
            if name == part.name:
                return part
        return default

    def get_all(self, name):
        ''' Return a list of parts with that name. '''
        return [p for p in self if p.name == name]

    def _lineiter(self):
        ''' Iterate over a binary file-like object line by line. Each line is
            returned as a (line, line_ending) tuple. If the line does not fit
            into self.buffer_size, line_ending is empty and the rest of the line
            is returned with the next iteration.
        '''
        read = self.stream.read
        maxread, maxbuf = self.content_length, self.buffer_size
        _bnl = tob('\r\n')
        while 1:
            lines = read(maxbuf if maxread < 0 else min(maxbuf, maxread))
            maxread -= len(lines)
            if not lines: break
            for line in lines.splitlines(True):
                if line[-2:] == _bnl: yield line[:-2], _bnl
                elif line[-1:] == _bnl[-1:]: yield line[:-1], _bnl[-1:]
                # elif line[-1:] == '\r': yield line[:-1], '\r'
                # Not supported. maxbuf could cut between \r and \n
                else:                   yield line, _bnl[:0] # b'rn'[:0] -> b''
    
    def _iterparse(self):
        lines, line = self._lineiter(), ''
        separator = tob('--') + tob(self.boundary)
        terminator = tob('--') + tob(self.boundary) + tob('--')
        # Consume first boundary. Ignore leading blank lines
        for line, nl in lines:
            if line: break
        if line != separator:
            raise MultipartError("Stream does not start with boundary")
        # For each part in stream...
        mem_used, disk_used = 0, 0 # Track used resources to prevent DoS
        is_tail = False # True if the last line was incomplete (cutted)
        opts = {'buffer_size': self.buffer_size,
                'memfile_limit': self.memfile_limit,
                'charset': self.charset}
        part = MultipartPart(**opts)
        for line, nl in lines:
            if line == terminator and not is_tail:
                part.file.seek(0)
                yield part
                break
            elif line == separator and not is_tail:
                if part.is_buffered(): mem_used  += part.size
                else:                  disk_used += part.size
                part.file.seek(0)
                yield part
                part = MultipartPart(**opts)
            else:
                is_tail = not nl # The next line continues this one
                part.feed(line, nl)
                if part.is_buffered():
                    if part.size + mem_used > self.mem_limit:
                        raise MultipartError("Memory limit reached.")
                elif part.size + disk_used > self.disk_limit:
                    raise MultipartError("Disk limit reached.")
        if line != terminator:
            raise MultipartError("Unexpected end of multipart stream.")
            

class MultipartPart(object):
    
    def __init__(self, buffer_size=2**16, memfile_limit=2**18, charset='latin9'):
        self.headerlist = []
        self.headers = None
        self.file = False
        self.size = 0
        self._buf = tob('')
        self.disposition, self.name, self.filename = None, None, None
        self.content_type, self.charset = None, charset
        self.memfile_limit = memfile_limit
        self.buffer_size = buffer_size

    def feed(self, line, nl=''):
        if self.file:
            return self.write_body(line, nl)
        return self.write_header(line, nl)

    def write_header(self, line, nl):
        line = line.decode(self.charset or 'latin9')
        if not nl: raise MultipartError('Unexpected end of line in header.')
        if not line.strip(): # blank line -> end of header segment
            self.finish_header()
        elif line[0] in ' \t' and self.headerlist:
            name, value = self.headerlist.pop()
            self.headerlist.append((name, value+line.strip()))
        else:
            if ':' not in line:
                raise MultipartError("Syntax error in header: No colon.")
            name, value = line.split(':', 1)
            self.headerlist.append((name.strip(), value.strip()))

    def write_body(self, line, nl):
        if not line and not nl: return # This does not even flush the buffer
        self.size += len(line) + len(self._buf)
        self.file.write(self._buf + line)
        self._buf = nl
        if self.content_length > 0 and self.size > self.content_length:
            raise MultipartError('Size of body exceeds Content-Length header.')
        if self.size > self.memfile_limit and isinstance(self.file, io.BytesIO):
            # TODO: What about non-file uploads that exceed the memfile_limit?
            self.file, old = TemporaryFile(mode='w+b'), self.file
            old.seek(0)
            copy_file(old, self.file, self.size, self.buffer_size)

    def finish_header(self):
        self.file = io.BytesIO()
        self.headers = Headers(self.headerlist)
        cdis = self.headers.get('Content-Disposition','')
        ctype = self.headers.get('Content-Type','')
        clen = self.headers.get('Content-Length','-1')
        if not cdis:
            raise MultipartError('Content-Disposition header is missing.')
        self.disposition, self.options = parse_options_header(cdis)
        self.name = self.options.get('name')
        self.filename = self.options.get('filename')
        self.content_type, options = parse_options_header(ctype)
        self.charset = options.get('charset') or self.charset
        self.content_length = int(self.headers.get('Content-Length','-1'))

    def is_buffered(self):
        ''' Return true if the data is fully buffered in memory.'''
        return isinstance(self.file, io.BytesIO)

    @property
    def value(self):
        ''' Data decoded with the specified charset '''
        pos = self.file.tell()
        self.file.seek(0)
        val = self.file.read()
        self.file.seek(pos)
        return val.decode(self.charset)
    
    def save_as(self, path):
        pos = self.file.tell()
        self.file.seek(0)
        with open(path, 'wb') as fp:
            size = copy_file(self.file, fp)
        self.file.seek(pos)
        return size

##############################################################################
#################################### WSGI ####################################
##############################################################################

def parse_form_data(environ, charset='utf8', strict=False, **kw):
    ''' Parse form data from an environ dict and return two :class:`MultiDict`
        instances. The first contains form fields with unicode keys and values.
        The second contains file uploads with unicode keys and
        :class:`MultipartPart` instances as values. Catch
        :exc:`ValueError` and :exc:`IndexError` to be sure.
        
        :param environ: An WSGI environment dict.
        :param charset: The charset to use if unsure. (default: utf8)
        :param strict: If True, raise :exc:`MultipartError` on parsing errors.
                       These are silently ignored by default.
    '''
        
    forms, files = MultiDict(), MultiDict()
    try:
        if environ.get('REQUEST_METHOD','GET').upper() not in ('POST', 'PUT'):
            raise MultipartError("Request method other than POST or PUT.")
        content_length = int(environ.get('CONTENT_LENGTH', '-1'))
        content_type = environ.get('CONTENT_TYPE', '')
        if not content_type:
            raise MultipartError("Missing Content-Type header.")
        content_type, options = parse_options_header(content_type)
        stream = environ.get('wsgi.input') or io.BytesIO()
        kw['charset'] = charset = options.get('charset', charset)
        if content_type == 'multipart/form-data':
            boundary = options.get('boundary','')
            if not boundary:
                raise MultipartError("No boundary for multipart/form-data.")
            for part in MultipartParser(stream, boundary, content_length, **kw):
                if part.filename:
                    files[part.name] = part
                elif part.is_buffered(): # TODO: What about big forms?
                    forms[part.name] = part.value
        elif content_type in ('application/x-www-form-urlencoded',
                              'application/x-url-encoded'):
            mem_limit = kw.get('mem_limit', 2**20)
            if content_length > mem_limit:
                raise MultipartError("Request to big. Increase MAXMEM.")
            data = stream.read(mem_limit).decode(charset)
            if stream.read(1): # These is more that does not fit mem_limit
                raise MultipartError("Request to big. Increase MAXMEM.")
            data = urlparse.parse_qs(data, keep_blank_values=True)
            for key, values in data.iteritems():
                for value in values:
                    forms[key] = value
        else:
            raise MultipartError("Unsupported content type.")
    except MultipartError:
        if strict: raise
    return forms, files

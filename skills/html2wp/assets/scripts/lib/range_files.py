"""Static files with HTTP Range, for every server the skill captures pages from.

Python's SimpleHTTPRequestHandler answers a Range request with the whole file
(200, no Accept-Ranges). Chromium then cannot seek a media file it has not
downloaded, and a page script that seeks a `preload="none"` video to its last
frame (`currentTime = duration - 0.05`, a design's reduced-motion branch) left
it on frame 0 about one capture in six: the source's capture showed another
picture than WordPress's, and gverify's capture cache kept it.

RangeFilesMixin goes before SimpleHTTPRequestHandler (or a subclass of it) in
a handler's bases. For a single `bytes=` range (`a-b`, `a-`, `-n`) it answers
206 with Content-Range; a range past the end is 416 with `bytes */size`; any
other request (no Range, several ranges, a directory, a missing file) is the
base handler's own answer. Every file answer says Accept-Ranges: bytes. HEAD
sends the same headers and no body. What the base resolves (translate_path, a
subclass's SPA fallback or guard) decides which file is served.

  python3 lib/range_files.py DIR [PORT]   serves DIR on 127.0.0.1 (PORT 0: any)
"""
import functools
import os
import re
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

SINGLE_RANGE = re.compile(r'^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$', re.I)


class RangeFilesMixin:
    def send_head(self):
        self._range_left = None
        header = self.headers.get('Range')
        match = SINGLE_RANGE.match(header) if header else None
        path = self.translate_path(self.path)
        if not match or (not match.group(1) and not match.group(2)) or not os.path.isfile(path):
            return super().send_head()
        try:
            f = open(path, 'rb')
        except OSError:
            return super().send_head()
        size = os.fstat(f.fileno()).st_size
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:  # the last n bytes
            start, end = max(0, size - int(match.group(2))), size - 1
        if start >= size or start > end:
            f.close()
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header('Content-Range', f'bytes */{size}')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        f.seek(start)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Last-Modified', self.date_time_string(int(os.fstat(f.fileno()).st_mtime)))
        self.end_headers()
        self._range_left = end - start + 1
        return f

    def end_headers(self):
        # A whole-file answer also says ranges are accepted, so a browser asks for one.
        if getattr(self, '_range_left', None) is None and self._headers_buffer and b'Accept-Ranges' not in b''.join(self._headers_buffer):
            self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def copyfile(self, source, outputfile):
        left = getattr(self, '_range_left', None)
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            chunk = source.read(min(64 * 1024, left))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)


class RangeFilesHandler(RangeFilesMixin, SimpleHTTPRequestHandler):
    """A quiet static handler with Range."""
    def log_message(self, *args):
        pass


def serve(directory, port=0, handler=RangeFilesHandler):
    """(server, origin) for DIRECTORY on 127.0.0.1; the caller runs serve_forever."""
    httpd = ThreadingHTTPServer(('127.0.0.1', int(port)), functools.partial(handler, directory=str(directory)))
    return httpd, f'http://127.0.0.1:{httpd.server_port}'


if __name__ == '__main__':
    if len(sys.argv) not in (2, 3) or not os.path.isdir(sys.argv[1]):
        sys.exit('usage: range_files.py DIR [PORT]')
    httpd, origin = serve(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else 0)
    print(origin, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

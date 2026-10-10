"""Loopback, GET-only source preview; never serves private files or payments."""
import argparse,http.server,mimetypes,urllib.request,urllib.error
from pathlib import Path
from urllib.parse import urlsplit,unquote
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--api',required=True);p.add_argument('--port',type=int,default=8080);args=p.parse_args()
u=urlsplit(args.api)
if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in {'','/'}:p.error('Use a plain HTTPS API origin')
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path.startswith('/api/public/'):
            try:
                req=urllib.request.Request(args.api.rstrip('/')+self.path,headers={'Accept':self.headers.get('Accept','application/json')})
                with urllib.request.urlopen(req,timeout=30) as r:
                    self.send_response(r.status);self.send_header('Content-Type',r.headers.get('Content-Type','application/json'));self.send_header('Cache-Control','no-store');self.end_headers()
                    while chunk:=r.read1(65536):self.wfile.write(chunk);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            except urllib.error.HTTPError as e:self.send_error(e.code)
            except Exception:self.send_error(502)
            return
        if path=='/':path='/terminal/index.html'
        file=(ROOT/unquote(path).lstrip('/')).resolve()
        if not file.is_relative_to(ROOT/'terminal') or not file.is_file() or file.suffix not in {'.html','.css','.js','.ts'}:self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'text/plain');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(file.read_bytes())
    def log_message(self,*args):pass
http.server.ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()

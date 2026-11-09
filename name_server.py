"""Local HTTP API for the RuneScape name shortlist and browser runner."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from name_service import NameService

service=NameService()

class Handler(BaseHTTPRequestHandler):
    server_version='NameScout/1.0'
    def log_message(self,fmt,*args): print('%s - %s'%(self.address_string(),fmt%args))
    def _send(self,status,payload):
        raw=json.dumps(payload,ensure_ascii=False).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self): self._send(403,{'error':'Cross-origin browser requests are not allowed'})
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        u=urlparse(self.path)
        try:
            if u.path in ('/','/dashboard.html'):
                file=Path(__file__).resolve().parent/'dashboard.html'
                if not file.exists(): return self._send(404,{'error':'dashboard.html has not been created'})
                raw=file.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            if u.path=='/api/state': return self._send(200,service.state())
            if u.path=='/api/runner/next': return self._send(200,service.runner_next(parse_qs(u.query).get('runner_id',[''])[0]))
            return self._send(404,{'error':'Not found'})
        except Exception as e: return self._send(400,{'error':str(e)})
    def do_POST(self):
        origin=self.headers.get('Origin')
        if origin and origin != 'http://127.0.0.1:8765' and self.headers.get('X-Name-Runner') != '1':
            return self._send(403,{'error':'Cross-origin request rejected'})
        u=urlparse(self.path)
        try:
            data=self.body()
            if u.path=='/api/batch': result={'prefixes':service.batch(data.get('prefixes'))}
            elif u.path=='/api/scan/start': result=service.start_scan()
            elif u.path=='/api/lookup': result=service.lookup(data.get('name',''))
            elif u.path=='/api/runner/start': result=service.runner_start(data.get('runner_id'))
            elif u.path=='/api/runner/pause': result=service.runner_action('pause')
            elif u.path=='/api/runner/resume': result=service.runner_action('resume')
            elif u.path=='/api/runner/stop': result=service.runner_action('stop')
            elif u.path=='/api/runner/oneoff': result=service.runner_oneoff(data.get('runner_id'),data.get('name'),data.get('candidate_id'),data.get('defer_pending',False))
            elif u.path=='/api/runner/verdict': result=service.runner_verdict(data)
            elif u.path=='/api/runner/progress': result=service.runner_progress(data)
            elif u.path.startswith('/api/candidate/'):
                parts=u.path.strip('/').split('/'); cid=service.canonical(unquote(parts[2]))
                if len(parts)!=4: return self._send(404,{'error':'Not found'})
                action=parts[3]
                if action=='pin': result={'id':service.set_candidate(cid,'pinned',data.get('pinned',False))}
                elif action=='skip': result={'id':service.set_candidate(cid,'skipped',data.get('skipped',False))}
                elif action=='priority': result={'id':service.set_candidate(cid,'priority',data.get('priority',0))}
                elif action=='front': result={'id':service.set_candidate(cid,'front',True)}
                else:return self._send(404,{'error':'Not found'})
            elif u.path.startswith('/api/history/') and u.path.endswith('/reset'):
                cid=unquote(u.path[len('/api/history/'): -len('/reset')].strip('/')); service.reset_history(cid); result={'id':service.canonical(cid),'status':'reset'}
            else:return self._send(404,{'error':'Not found'})
            return self._send(200,result)
        except Exception as e: return self._send(400,{'error':str(e)})

def main():
    print('Name service listening on http://127.0.0.1:8765')
    ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()

if __name__=='__main__': main()

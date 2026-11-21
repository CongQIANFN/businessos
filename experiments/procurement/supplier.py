"""Independent localhost-only fake supplier with durable idempotent receipts."""
import argparse,json,sqlite3
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--port',type=int,required=True);a=p.parse_args()
with sqlite3.connect(a.db) as c:c.execute('CREATE TABLE IF NOT EXISTS orders(opkey TEXT PRIMARY KEY,receipt TEXT,payload TEXT)')
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def respond(self,data):
        raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        with sqlite3.connect(a.db) as c:rows=c.execute('SELECT opkey,receipt,payload FROM orders ORDER BY opkey').fetchall()
        self.respond({'orders':[{'opkey':r[0],'receipt':r[1],'payload':json.loads(r[2])} for r in rows]})
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));key=data['opkey'];raw=json.dumps(data,sort_keys=True)
        with sqlite3.connect(a.db,timeout=10) as c:
            c.execute('INSERT OR IGNORE INTO orders VALUES(?,?,?)',(key,'supplier-'+key,raw));row=c.execute('SELECT receipt,payload FROM orders WHERE opkey=?',(key,)).fetchone()
        if row[1]!=raw:self.send_error(409);return
        self.respond({'receipt':row[0],'status':'confirmed'})
ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever()

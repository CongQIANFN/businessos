import shlex
import socket
import subprocess
import tempfile
from pathlib import Path

from psycopg.conninfo import make_conninfo


class IsolatedPostgres:
    def __init__(self,root=None):self.root=Path(root or tempfile.mkdtemp(prefix='businessos-pg-'));self.dsn=None
    def start(self):
        self.root.mkdir(parents=True,exist_ok=True);data=self.root/'data';sock=self.root/'socket';sock.mkdir(exist_ok=True)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        subprocess.run(['initdb','-D',str(data),'-U','experiment_admin','-A','trust','--no-locale','-E','UTF8'],check=True,capture_output=True)
        options = f'-F -p {port} -k {shlex.quote(str(sock))} -h ""'
        subprocess.run(['pg_ctl','-D',str(data),'-l',str(self.root/'postgres.log'),'-o',options,'-w','start'],check=True,capture_output=True)
        self.dsn = make_conninfo(host=str(sock), port=port, dbname='postgres', user='experiment_admin')
        return self
    def stop(self):subprocess.run(['pg_ctl','-D',str(self.root/'data'),'-m','fast','-w','stop'],capture_output=True,check=True)
    def __enter__(self):return self.start()
    def __exit__(self,*args):self.stop()

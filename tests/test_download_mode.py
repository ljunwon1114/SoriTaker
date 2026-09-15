"""Exercise inherited offline flags with the pinned Hub client and a local server."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import atomic_json, read_json
from models import clear_legacy_offline_flags


class DownloadModeTests(unittest.TestCase):
    def test_legacy_cleanup_keeps_parent_and_telemetry_settings(self):
        parent=dict(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='TRUE')
        child=clear_legacy_offline_flags(parent.copy())
        self.assertNotIn('HF_HUB_OFFLINE',child)
        self.assertNotIn('TRANSFORMERS_OFFLINE',child)
        self.assertEqual(child['HF_HUB_DISABLE_TELEMETRY'],'1')
        self.assertEqual(parent,dict(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='TRUE'))

    def test_normal_environment_has_no_forced_mode(self):
        env=clear_legacy_offline_flags(dict(SORITAKER_TEST_MARKER='keep'))
        self.assertNotIn('HF_HUB_OFFLINE',env)
        self.assertNotIn('TRANSFORMERS_OFFLINE',env)
        self.assertEqual(env['SORITAKER_TEST_MARKER'],'keep')

    def test_download_worker_clears_inherited_flags_before_real_hub_import(self):
        self.run_local_download('worker')

    def test_prepare_cli_clears_inherited_flags_before_real_hub_import(self):
        self.run_local_download('prepare')

    def run_local_download(self, entry):
        # Small stand-ins test network/download mechanics, not model inference.
        files={'config.json':b'{"test_fixture":true}', 'weights.safetensors':b'test weights'}
        revision='a'*40
        repo='mlx-community/whisper-large-v3-mlx'
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_HEAD(self):
                self.respond(False)
            def do_GET(self):
                self.respond(True)
            def respond(self,body):
                requests.append((self.command,self.path))
                if self.path.startswith('/api/models/'+repo):
                    payload=json.dumps(dict(id=repo,sha=revision,private=False,gated=False,
                        siblings=[dict(rfilename=name) for name in files])).encode()
                    content_type='application/json'
                elif '/resolve/'+revision+'/' in self.path and self.path.rsplit('/',1)[-1] in files:
                    payload=files[self.path.rsplit('/',1)[-1]]
                    content_type='application/octet-stream'
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(payload)))
                self.send_header('X-Repo-Commit',revision)
                self.send_header('ETag','"'+hashlib.sha256(payload).hexdigest()+'"')
                self.end_headers()
                if body:
                    self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                env=os.environ.copy()
                env.update(SORITAKER_DATA_DIR=str(root/'data'),HF_HOME=str(root/'hub-cache'),
                    HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                    HF_ENDPOINT=f'http://127.0.0.1:{server.server_port}',HF_HUB_DISABLE_XET='1',
                    HF_HUB_DISABLE_PROGRESS_BARS='1',HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                    NO_PROXY='127.0.0.1,localhost')
                main=Path(__file__).resolve().parents[1]/'src'/'main.py'
                if entry=='worker':
                    spec=root/'job'/'job.json'
                    atomic_json(spec,dict(kind='download',model='large'))
                    args=['--worker',str(spec)]
                else:
                    args=['--prepare','large']
                result=subprocess.run([sys.executable,str(main)]+args,env=env,capture_output=True,
                                      text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr+
                    str(read_json(root/'job'/'status.json',{})))
                manifest=read_json(root/'data'/'models'/'large'/'installed.json')
                self.assertEqual(manifest['origin']['revision'],revision)
                for name,payload in files.items():
                    self.assertEqual((root/'data'/'models'/'large'/name).read_bytes(),payload)
                self.assertTrue(any(method=='GET' and path.startswith('/api/models/') for method,path in requests))
                self.assertTrue(any(method=='GET' and '/resolve/' in path for method,path in requests))
                if entry=='worker':
                    self.assertEqual(read_json(spec.parent/'status.json')['state'],'complete')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__=='__main__':
    unittest.main()

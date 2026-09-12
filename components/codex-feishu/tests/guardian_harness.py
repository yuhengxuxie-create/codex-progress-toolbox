"""Isolated real-process transport harness; never loads a real app or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from progress_wx.guardian import Guardian,control_root
from progress_wx.guardian_channel import GuardianChannel
from progress_wx.process_control import acquire_instance,release_instance,stop_requested_for


def configuration(root):
    return SimpleNamespace(path=root/'synthetic.yaml',service=SimpleNamespace(database=root/'business.sqlite',pid_file=root/'worker.pid'),feishu=SimpleNamespace(target_open_id='synthetic-owner'))


class Channel:
    def __init__(self,root):self.root=root
    def start(self,callback):self.callback=callback
    def stop(self):pass
    def is_online(self):return not (self.root/'offline').exists()
    def send_text(self,text,*,idempotency_key):
        if (self.root/'block-send').exists():
            time.sleep(130)
        with (self.root/'sent.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps({'key':idempotency_key,'text':text})+'\n')
        return 'fake:'+idempotency_key


if __name__=='__main__':
    root=Path(sys.argv[2]).resolve()
    c=configuration(root)
    if sys.argv[1]=='guardian':
        def spawn(token):
            return subprocess.Popen([sys.executable,__file__,'worker',str(root),token],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        g=Guardian(c,Channel(root),spawn=spawn)
        original_tick=g.tick
        def tick():
            if (root/'block-control').exists():
                time.sleep(130)
            original_tick()
        g.tick=tick
        raise SystemExit(g.run())
    else:
        identity=acquire_instance(c.service.pid_file,c.path)
        p=GuardianChannel(c,sys.argv[3])
        def receive(message):
            # Durable key claim models worker-side application idempotency.
            if p.store.command('business:'+message.message_id,'accepted'):
                with (root/'received.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps({'key':message.message_id})+'\n')
        p.start(receive)
        try:
            while not stop_requested_for(c.service.pid_file,identity):
                if (root/'block-worker').exists():
                    time.sleep(130)
                if not p.heartbeat():break
                time.sleep(.2)
        finally:
            p.stop();p.store.close();release_instance(c.service.pid_file,identity)

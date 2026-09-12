"""Ten-minute isolated guardian-core resource/pressure sampling, no real SDK connection."""
import argparse
import ctypes
from ctypes import wintypes
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from progress_wx.guardian import guardian_status,control_root
from progress_wx.guardian_store import GuardianStore
spec=importlib.util.spec_from_file_location('guardian_harness',Path(__file__).with_name('guardian_harness.py'))
harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)

class Memory(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]

def memory(pid):
    k=ctypes.WinDLL('kernel32');p=ctypes.WinDLL('psapi')
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    p.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD]
    handle=k.OpenProcess(0x410,False,pid)
    try:
        value=Memory();value.cb=ctypes.sizeof(value)
        if not handle or not p.GetProcessMemoryInfo(handle,ctypes.byref(value),value.cb):raise OSError('memory query failed')
        return {'rss':value.WorkingSetSize,'private_bytes':value.PrivateUsage}
    finally:
        if handle:k.CloseHandle(handle)

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--seconds',type=int,default=600);a.add_argument('--output',required=True);args=a.parse_args()
    output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    started=time.time();samples=[];errors=[]
    with tempfile.TemporaryDirectory(prefix='guardian-soak-') as temporary:
        root=Path(temporary);c=harness.configuration(root);s=GuardianStore(control_root(c));s.intent('running')
        child=subprocess.Popen([sys.executable,str(Path(harness.__file__)),'guardian',str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline and not guardian_status(c)['worker']['ready']:time.sleep(.2)
            if not guardian_status(c)['worker']['ready']:raise RuntimeError('harness not ready')
            begin=time.monotonic();index=0
            while time.monotonic()-begin<args.seconds:
                if child.poll() is not None:raise RuntimeError('guardian unexpectedly exited')
                # Every minute includes 10 held messages and a reconnect; otherwise idle.
                if index%6==0:
                    (root/'offline').touch()
                    time.sleep(1.2)
                    for j in range(10):s.enqueue(f'soak:{index}:{j}','send_text',{'text':'synthetic resource probe'})
                    (root/'offline').unlink()
                status=guardian_status(c)
                sample={'elapsed':round(time.monotonic()-begin,2),**memory(child.pid),
                        'healthy':status['guardian']['healthy'],'worker_ready':status['worker']['ready']}
                with s.lock:
                    sample['queue']=dict(s.db.execute('SELECT state,count(*) FROM outgoing GROUP BY state').fetchall())
                # Process thread inventory via native OS query; output never contains command lines.
                probe=subprocess.run(['powershell.exe','-NoProfile','-Command',f'(Get-Process -Id {child.pid}).Threads.Count'],capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                sample['threads']=int(probe.stdout.strip()) if probe.returncode==0 else None
                samples.append(sample)
                output.write_text(json.dumps({'state':'running','started_at':started,'samples':samples,'errors':errors},indent=2),encoding='utf-8')
                index+=1;time.sleep(10)
        except Exception as exc:
            errors.append(type(exc).__name__+':'+str(exc));raise
        finally:
            s.intent('exited')
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
            stderr=child.stderr.read().decode('utf-8',errors='replace')
            if child.returncode:errors.append('guardian_exit:'+str(child.returncode))
            s.close()
            output.write_text(json.dumps({'state':'complete' if not errors else 'failed','started_at':started,'ended_at':time.time(),
                'scope':'real guardian core + fake channel + synthetic worker; excludes real SDK/network memory',
                'samples':samples,'errors':errors,'stderr':stderr[-2000:]},indent=2),encoding='utf-8')
    print('soak complete; samples='+str(len(samples)))

"""Read-only Windows logon identity, distinct from reusable session/PID values."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import subprocess

def current_session_marker():
    if os.name!='nt':return None
    try:
        class Luid(ctypes.Structure):
            _fields_=[('low',wintypes.DWORD),('high',wintypes.LONG)]
        class Statistics(ctypes.Structure):
            _fields_=[('token',Luid),('authentication',Luid),('expiration',ctypes.c_longlong),('type',wintypes.DWORD),('impersonation',wintypes.DWORD),('charged',wintypes.DWORD),('available',wintypes.DWORD),('groups',wintypes.DWORD),('privileges',wintypes.DWORD),('modified',Luid)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);adv=ctypes.WinDLL('advapi32',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        adv.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
        adv.GetTokenInformation.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
        token=wintypes.HANDLE();value=Statistics();length=wintypes.DWORD()
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):return None
        try:
            if not adv.GetTokenInformation(token,10,ctypes.byref(value),ctypes.sizeof(value),ctypes.byref(length)):return None
        finally:kernel.CloseHandle(token)
        system=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0'
        environment=dict(os.environ);environment['PSModulePath']=str(system/'Modules')
        boot=subprocess.run([str(system/'powershell.exe'),'-NoProfile','-NonInteractive','-Command',"(Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime().Ticks"],env=environment,capture_output=True,text=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        stamp=boot.stdout.strip()
        if boot.returncode or not stamp.isdigit() or len(stamp)<16:return None
        return hashlib.sha256(f'{stamp}:{value.authentication.high}:{value.authentication.low}'.encode()).hexdigest()
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        return None

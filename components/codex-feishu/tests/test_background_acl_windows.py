import ctypes
import os
import subprocess
import pytest
from progress_wx import guardian_store, secrets


@pytest.mark.skipif(os.name != 'nt', reason='Windows background process contract')
@pytest.mark.parametrize('operation', ['guardian', 'secrets'])
def test_acl_helper_requires_hidden_creation_before_any_process_launch(tmp_path, monkeypatch, operation):
    def check(command, **kwargs):
        assert kwargs.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW
        raise RuntimeError('checked-before-launch')
    monkeypatch.setattr(subprocess, 'run', check)
    with pytest.raises(RuntimeError, match='checked-before-launch'):
        if operation == 'guardian':
            guardian_store.private_directory(tmp_path/'guardian')
        else:
            secrets._tighten_acl_with_icacls(tmp_path/'secret-test')


def _visible_console_windows():
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.IsWindowVisible.argtypes=[wintypes.HWND]
    user.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
    windows=set()
    @callback_type
    def visit(hwnd, _):
        name=ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd,name,len(name))
        if user.IsWindowVisible(hwnd) and name.value in ('ConsoleWindowClass','CASCADIA_HOSTING_WINDOW_CLASS'):
            windows.add(int(hwnd))
        return True
    assert user.EnumWindows(visit,0)
    return windows


@pytest.mark.skipif(os.name != 'nt', reason='Windows actual ACL helper integration')
def test_actual_private_directory_and_secret_acl_helpers_are_hidden(tmp_path, monkeypatch):
    original=subprocess.run
    calls=[]
    def checked(command, **kwargs):
        assert kwargs.get('creationflags',0) & subprocess.CREATE_NO_WINDOW
        result=original(command,**kwargs)
        calls.append((os.path.basename(command[0]).casefold(),result.returncode))
        return result
    monkeypatch.setattr(subprocess,'run',checked)
    before=_visible_console_windows()
    guardian_store.private_directory(tmp_path/'guardian')
    secret=tmp_path/'synthetic-secret';secret.write_text('synthetic test data',encoding='utf-8')
    secrets._tighten_acl_with_icacls(secret)
    assert calls == [('whoami',0),('icacls',0)]
    assert not (_visible_console_windows()-before)

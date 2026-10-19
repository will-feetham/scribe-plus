"""
Windows shell integration: the app's identity, and its Start menu entry.

Windows labels a desktop app's notifications with the name of the program
that sent them. Scribe Plus runs inside Python, so without this they say
"Python". Here the process gets its own AppUserModelID, registered with the
name "Scribe Plus" and its icon, and the Start menu shortcut carries the
same ID, so notifications (and Settings > Notifications) say Scribe Plus.

The app calls ensure_installed() every time it starts, so the Start menu
entry comes back if it was deleted and follows the app if its folder moves.
The installer runs the same code:

    venv\\Scripts\\python.exe -m scribe_plus.win.shell             Start menu entry
    venv\\Scripts\\python.exe -m scribe_plus.win.shell --startup   ...and start at sign-in
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

AUMID = "Feetham.ScribePlus"
APP_NAME = "Scribe Plus"
DESCRIPTION = "Scribe Plus dictation"
SHORTCUT_NAME = APP_NAME + ".lnk"
SW_SHOWMINNOACTIVE = 7  # the launcher .bat flashes no console window


def app_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def programs_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_dir() -> Path:
    return programs_dir() / "Startup"


def _layout(base: Path | None = None) -> dict[str, Path]:
    base = base or app_dir()
    return {
        "target": base / "Scribe Plus.bat",
        "workdir": base,
        "icon": base / "assets" / "scribe-plus.ico",
        "toast_icon": base / "assets" / "icon.png",
    }


# -- process identity ------------------------------------------------------------


def set_process_identity() -> bool:
    """Call before any window is created."""
    try:
        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_long
        return fn(AUMID) == 0
    except Exception as exc:
        log.info("could not set the app identity: %s", exc)
        return False


def process_identity() -> str | None:
    fn = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID
    fn.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    fn.restype = ctypes.c_long
    ptr = ctypes.c_void_p()
    if fn(ctypes.byref(ptr)) != 0 or not ptr.value:
        return None
    try:
        return ctypes.wstring_at(ptr.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ptr)


def registry_key(aumid: str = AUMID) -> str:
    return "Software\\Classes\\AppUserModelId\\" + aumid


def register_identity(base: Path | None = None, aumid: str = AUMID) -> None:
    """Name and icon Windows shows for notifications from this AppUserModelID."""
    import winreg
    paths = _layout(base)
    icon = paths["toast_icon"] if paths["toast_icon"].exists() else paths["icon"]
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry_key(aumid), 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
        winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)


# -- shortcuts --------------------------------------------------------------------


def write_shortcut(path: Path, base: Path | None = None, aumid: str = AUMID) -> None:
    paths = _layout(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_shortcut_com(path, paths, aumid)
    except ImportError:
        # pywin32 not installed yet: a plain shortcut, without the app ID.
        _write_shortcut_powershell(path, paths)


def _write_shortcut_com(path: Path, paths: dict[str, Path], aumid: str) -> None:
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    pythoncom.CoInitialize()
    try:
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                          pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.SetPath(str(paths["target"]))
        link.SetWorkingDirectory(str(paths["workdir"]))
        link.SetIconLocation(str(paths["icon"]), 0)
        link.SetDescription(DESCRIPTION)
        link.SetShowCmd(SW_SHOWMINNOACTIVE)
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID,
                       propsys.PROPVARIANTType(aumid, pythoncom.VT_LPWSTR))
        store.Commit()
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
    finally:
        pythoncom.CoUninitialize()


def _ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _write_shortcut_powershell(path: Path, paths: dict[str, Path]) -> None:
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(path)});"
        f"$s.TargetPath={_ps_quote(paths['target'])};"
        f"$s.WorkingDirectory={_ps_quote(paths['workdir'])};"
        f"$s.WindowStyle={SW_SHOWMINNOACTIVE};"
        f"$s.IconLocation={_ps_quote(paths['icon'])};"
        f"$s.Description={_ps_quote(DESCRIPTION)};$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                   check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def read_shortcut(path: Path) -> dict[str, str | None]:
    """Target, working folder and app ID of a shortcut (needs pywin32)."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    pythoncom.CoInitialize()
    try:
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                          pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path), 0)
        target = link.GetPath(shell.SLGP_RAWPATH, 1024)[0]
        workdir = link.GetWorkingDirectory(1024)
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        aumid = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        return {"target": target, "workdir": workdir, "aumid": aumid or None}
    finally:
        pythoncom.CoUninitialize()


def _up_to_date(path: Path, base: Path | None, aumid: str) -> bool:
    if not path.exists():
        return False
    try:
        info = read_shortcut(path)
    except ImportError:
        return True  # can't inspect without pywin32; leave the existing one alone
    except Exception:
        return False
    want = _layout(base)
    same = lambda a, b: os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))
    return (same(info["target"], want["target"]) and same(info["workdir"], want["workdir"])
            and info["aumid"] == aumid)


def ensure_installed(startup: str = "keep", base: Path | None = None,
                     aumid: str = AUMID) -> list[str]:
    """Register the app identity and make sure the Start menu entry exists and
    points here. startup: "keep" refreshes a sign-in shortcut only if you
    already have one, "add" creates it, "remove" deletes it.
    Returns what changed."""
    changed: list[str] = []
    register_identity(base, aumid)
    menu = programs_dir() / SHORTCUT_NAME
    if not _up_to_date(menu, base, aumid):
        write_shortcut(menu, base, aumid)
        changed.append("start menu")
    boot = startup_dir() / SHORTCUT_NAME
    if startup == "remove":
        if boot.exists():
            boot.unlink()
            changed.append("startup removed")
    elif startup == "add" or (startup == "keep" and boot.exists()):
        if not _up_to_date(boot, base, aumid):
            write_shortcut(boot, base, aumid)
            changed.append("startup")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scribe Plus Start menu entry")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--startup", action="store_true", help="also start at sign-in")
    group.add_argument("--no-startup", action="store_true", help="don't start at sign-in")
    args = parser.parse_args(argv)
    mode = "add" if args.startup else "remove" if args.no_startup else "keep"
    changed = ensure_installed(mode)
    print("Start menu: Scribe Plus" + (f" (updated: {', '.join(changed)})" if changed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

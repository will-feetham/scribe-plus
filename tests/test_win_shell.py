"""Start menu entry and app identity (Windows only: real shortcuts, real registry)."""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows shell integration")

if sys.platform == "win32":
    import winreg

    from scribe_plus.win import shell

ROOT = Path(__file__).resolve().parents[1]


def same(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


@pytest.fixture
def programs(tmp_path, monkeypatch):
    folder = tmp_path / "Programs"
    monkeypatch.setattr(shell, "programs_dir", lambda: folder)
    monkeypatch.setattr(shell, "startup_dir", lambda: folder / "Startup")
    return folder


@pytest.fixture
def aumid():
    value = "Feetham.ScribePlusTest" + uuid.uuid4().hex[:8]
    yield value
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, shell.registry_key(value))
    except OSError:
        pass


def test_identity_registered_with_name_and_icon(aumid):
    shell.register_identity(aumid=aumid)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, shell.registry_key(aumid)) as key:
        assert winreg.QueryValueEx(key, "DisplayName")[0] == "Scribe Plus"
        icon = winreg.QueryValueEx(key, "IconUri")[0]
        assert winreg.QueryValueEx(key, "ShowInSettings")[0] == 1
    assert icon.endswith("icon.png") and Path(icon).exists()


def test_start_menu_entry_carries_the_app_id(programs, aumid):
    assert shell.ensure_installed(aumid=aumid) == ["start menu"]
    info = shell.read_shortcut(programs / "Scribe Plus.lnk")
    assert same(info["target"], ROOT / "Scribe Plus.bat")
    assert same(info["workdir"], ROOT)
    assert info["aumid"] == aumid
    assert not (programs / "Startup" / "Scribe Plus.lnk").exists()  # never added unasked
    assert shell.ensure_installed(aumid=aumid) == []  # already right: left alone


def test_entry_comes_back_and_follows_the_app(programs, aumid, tmp_path):
    shell.ensure_installed(aumid=aumid)
    (programs / "Scribe Plus.lnk").unlink()
    assert shell.ensure_installed(aumid=aumid) == ["start menu"]
    moved = tmp_path / "Moved Scribe Plus"
    assert shell.ensure_installed(base=moved, aumid=aumid) == ["start menu"]
    assert same(shell.read_shortcut(programs / "Scribe Plus.lnk")["target"], moved / "Scribe Plus.bat")


def test_startup_shortcut_only_when_chosen(programs, aumid):
    boot = programs / "Startup" / "Scribe Plus.lnk"
    shell.ensure_installed("keep", aumid=aumid)
    assert not boot.exists()
    assert "startup" in shell.ensure_installed("add", aumid=aumid)
    assert shell.read_shortcut(boot)["aumid"] == aumid
    assert shell.ensure_installed("keep", aumid=aumid) == []
    shell.ensure_installed("remove", aumid=aumid)
    assert not boot.exists()


def test_old_shortcut_without_app_id_is_upgraded(programs, aumid, monkeypatch):
    def no_pywin32(*_args):
        raise ImportError
    with monkeypatch.context() as m:
        m.setattr(shell, "_write_shortcut_com", no_pywin32)
        shell.write_shortcut(programs / "Scribe Plus.lnk", aumid=aumid)  # PowerShell fallback
    old = shell.read_shortcut(programs / "Scribe Plus.lnk")
    assert same(old["target"], ROOT / "Scribe Plus.bat") and old["aumid"] is None
    assert shell.ensure_installed(aumid=aumid) == ["start menu"]
    assert shell.read_shortcut(programs / "Scribe Plus.lnk")["aumid"] == aumid


def test_process_identity():
    # In a child process so the test runner keeps its own identity.
    out = subprocess.run(
        [sys.executable, "-c",
         "from scribe_plus.win import shell; print(shell.set_process_identity(), shell.process_identity())"],
        cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["True", "Feetham.ScribePlus"]


def test_installer_command(tmp_path):
    # Exactly what "Install Scribe Plus.bat" runs, with the Start menu redirected.
    env = dict(os.environ, APPDATA=str(tmp_path))
    out = subprocess.run([sys.executable, "-m", "scribe_plus.win.shell", "--startup"],
                         cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    assert "Start menu: Scribe Plus" in out.stdout
    programs = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    assert shell.read_shortcut(programs / "Scribe Plus.lnk")["aumid"] == "Feetham.ScribePlus"
    assert (programs / "Startup" / "Scribe Plus.lnk").exists()

"""Stash Bridge for Unreal Engine (editor-only, Python).

Entry points (also wired into the editor menus by menus.register()):
  stash_unreal.push_selected()        Content Browser selection -> Stash (FBX/glTF + textures)
  stash_unreal.receive()              import everything "Sent to Unreal" from the Stash web UI
  stash_unreal.import_asset(id_or_uid)
  stash_unreal.import_file_dialog()   import a downloaded Stash package (.zip)
  stash_unreal.open_settings()
"""

import json
import os

import unreal

from .client import StashClient, StashError

_CONFIG_NAME = "StashBridge.json"
DEFAULTS = {"server_url": "http://localhost:8420", "token": "", "import_root": "/Game/Stash", "auto_receive": False, "poll_seconds": 5}


def config_path() -> str:
    return os.path.join(unreal.Paths.project_config_dir(), _CONFIG_NAME)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        save_config(cfg)
    except Exception as e:
        unreal.log_warning(f"[Stash] cannot read {config_path()}: {e}")
    if os.environ.get("STASH_URL"):
        cfg["server_url"] = os.environ["STASH_URL"]
    if os.environ.get("STASH_TOKEN"):
        cfg["token"] = os.environ["STASH_TOKEN"]
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(config_path()), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump({k: cfg.get(k, v) for k, v in DEFAULTS.items()}, f, indent=2)


def client() -> StashClient:
    cfg = load_config()
    return StashClient(cfg["server_url"], cfg.get("token", ""))


def cache_dir() -> str:
    d = os.path.join(unreal.Paths.project_saved_dir(), "StashCache")
    os.makedirs(d, exist_ok=True)
    return d


def notify(msg: str, error: bool = False) -> None:
    (unreal.log_error if error else unreal.log)(f"[Stash] {msg}")


def open_settings() -> None:
    load_config()  # make sure the file exists
    path = config_path()
    notify(f"Settings file: {path}")
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import subprocess
            import sys

            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    except Exception:
        unreal.EditorDialog.show_message("Stash settings", f"Edit {path}\n(server_url, token, import_root)", unreal.AppMsgType.OK)


def open_web() -> None:
    import webbrowser

    webbrowser.open(load_config()["server_url"])


def test_connection() -> None:
    try:
        st = client().status()
        unreal.EditorDialog.show_message("Stash", f"Connected to Stash {st['version']} - {st['assets']} assets", unreal.AppMsgType.OK)
    except StashError as e:
        unreal.EditorDialog.show_message("Stash", f"Connection failed:\n{e}\n\nEdit {config_path()}", unreal.AppMsgType.OK)


def push_selected():
    from .exporter import push_selected as _push

    return _push()


def receive():
    from .importer import receive as _receive

    return _receive()


def import_asset(id_or_uid):
    from .importer import import_asset as _imp

    return _imp(id_or_uid)


def import_file_dialog():
    from .importer import import_file_dialog as _dlg

    return _dlg()

"""Editor menus: a 'Stash' main-menu entry and a Content Browser context-menu action,
plus optional auto-receive polling driven by the Slate tick."""

from __future__ import annotations

import threading
import time

import unreal

from . import load_config, notify

OWNER = "StashBridge"
_state = {"tick": None, "last": 0.0, "pending": None, "busy": False}


def _entry(name: str, label: str, tooltip: str, command: str):
    e = unreal.ToolMenuEntry(name=name, type=unreal.MultiBlockType.MENU_ENTRY)
    e.set_label(label)
    e.set_tool_tip(tooltip)
    e.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", string=command)
    return e


def register() -> None:
    menus = unreal.ToolMenus.get()
    main = menus.find_menu("LevelEditor.MainMenu")
    if main is not None:
        stash = main.add_sub_menu(OWNER, "", "StashMenu", "Stash", "Stash asset library")
        stash.add_section("Pull", "Pull from Stash")
        stash.add_menu_entry("Pull", _entry("StashReceive", "Receive sent assets", "Import everything sent to Unreal from the Stash web UI", "import stash_unreal; stash_unreal.receive()"))
        stash.add_menu_entry("Pull", _entry("StashImportFile", "Import package from file…", "Import a Stash package (.zip) you downloaded", "import stash_unreal; stash_unreal.import_file_dialog()"))
        stash.add_menu_entry("Pull", _entry("StashAuto", "Toggle auto-receive", "Poll Stash every few seconds for sent assets", "import stash_unreal.menus as m; m.toggle_auto_receive()"))
        stash.add_section("Push", "Push to Stash")
        stash.add_menu_entry("Push", _entry("StashPush", "Push selected assets", "Export the Content Browser selection to Stash", "import stash_unreal; stash_unreal.push_selected()"))
        stash.add_section("Setup", "Setup")
        stash.add_menu_entry("Setup", _entry("StashOpenWeb", "Open Stash in browser", "", "import stash_unreal; stash_unreal.open_web()"))
        stash.add_menu_entry("Setup", _entry("StashTest", "Test connection", "", "import stash_unreal; stash_unreal.test_connection()"))
        stash.add_menu_entry("Setup", _entry("StashSettings", "Edit settings…", "Opens Config/StashBridge.json", "import stash_unreal; stash_unreal.open_settings()"))

    ctx = menus.extend_menu("ContentBrowser.AssetContextMenu")
    if ctx is not None:
        ctx.add_section("StashSection", "Stash")
        ctx.add_menu_entry("StashSection", _entry("StashPushCtx", "Push to Stash", "Export these assets to your Stash library", "import stash_unreal; stash_unreal.push_selected()"))
    menus.refresh_all_widgets()
    if load_config().get("auto_receive"):
        start_auto_receive()
    notify("Stash Bridge ready (menu: Stash)")


# ----------------------------------------------------------------------------- auto receive

def _poll_thread():
    from . import client

    try:
        _state["pending"] = client().get("/api/deliveries", app="unreal")
    except Exception:
        _state["pending"] = []
    finally:
        _state["busy"] = False


def _tick(_delta):
    now = time.time()
    if _state["pending"]:
        _state["pending"] = None
        from .importer import receive

        receive()  # imports must happen on the game thread
        return
    if not _state["busy"] and now - _state["last"] > float(load_config().get("poll_seconds", 5)):
        _state["last"] = now
        _state["busy"] = True
        threading.Thread(target=_poll_thread, daemon=True).start()


def start_auto_receive():
    if _state["tick"] is None:
        _state["tick"] = unreal.register_slate_post_tick_callback(_tick)
        notify("auto-receive on")


def stop_auto_receive():
    if _state["tick"] is not None:
        unreal.unregister_slate_post_tick_callback(_state["tick"])
        _state["tick"] = None
        notify("auto-receive off")


def toggle_auto_receive():
    (stop_auto_receive if _state["tick"] is not None else start_auto_receive)()

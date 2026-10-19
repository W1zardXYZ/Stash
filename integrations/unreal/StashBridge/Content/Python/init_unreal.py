# Unreal runs every init_unreal.py found in a plugin's Content/Python folder at editor startup.
import unreal

try:
    import stash_unreal.menus

    stash_unreal.menus.register()
except Exception as e:  # never break editor startup
    unreal.log_error(f"[Stash] failed to initialise: {e}")

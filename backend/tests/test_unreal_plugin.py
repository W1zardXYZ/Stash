"""Exercise the StashBridge Unreal plugin against a stub `unreal` module.

This can't prove the real editor API matches, but it runs every code path of the
package importer / target choice / exporter helpers so Python-level bugs surface here.
"""

import importlib
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "integrations" / "unreal" / "StashBridge" / "Content" / "Python"


class _Obj:
    def __init__(self, name="obj", path="/Game/X"):
        self._name, self._path, self.props = name, path, {}

    def get_name(self):
        return self._name

    def get_path_name(self):
        return self._path

    def set_editor_property(self, k, v):
        self.props[k] = v

    def get_editor_property(self, k):
        return self.props.get(k)


def make_stub(tmp_path):
    u = types.ModuleType("unreal")
    calls = {"import": [], "mi_params": [], "created": [], "saved": []}
    for cls in ("Texture", "StaticMesh", "SkeletalMesh", "Material", "MaterialInstance", "MaterialInstanceConstant", "Texture2D"):
        setattr(u, cls, type(cls, (_Obj,), {}))
    u.Texture2D = type("Texture2D", (u.Texture,), {})
    u.MaterialInstanceConstant = type("MaterialInstanceConstant", (u.MaterialInstance,), {})
    assets = {}

    class SM(u.StaticMesh):
        def set_material(self, i, mi):
            self.props.setdefault("assigned", {})[i] = mi.get_name()

    class AssetImportTask(_Obj):
        pass

    class AssetTools:
        def import_asset_tasks(self, tasks):
            for t in tasks:
                f = Path(t.props["filename"])
                dest = t.props["destination_path"]
                path = f"{dest}/{f.stem}.{f.stem}"
                calls["import"].append((f.name, dest, t.props.get("options")))
                if f.suffix == ".fbx":
                    mesh = SM(f.stem, path)
                    slot = _Obj()
                    slot.props["material_slot_name"] = "Crate"
                    mesh.props["static_materials"] = [slot]
                    assets[path.split(".")[0]] = mesh
                else:
                    assets[path.split(".")[0]] = u.Texture2D(f.stem, path)
                t.props["imported_object_paths"] = [path]

        def create_asset(self, name, folder, cls, factory):
            o = cls(name, f"{folder}/{name}")
            assets[f"{folder}/{name}"] = o
            calls["created"].append(f"{folder}/{name}")
            return o

    u.AssetImportTask = AssetImportTask
    u.AssetToolsHelpers = types.SimpleNamespace(get_asset_tools=lambda: AssetTools())
    u.EditorAssetLibrary = types.SimpleNamespace(
        load_asset=lambda p: assets.get(p.split(".")[0], _Obj(p)),
        does_asset_exist=lambda p: p in assets,
        save_loaded_asset=lambda a: calls["saved"].append(a.get_name()),
    )

    class MEL:
        @staticmethod
        def create_material_expression(mat, cls, x, y):
            return _Obj(cls.__name__ if isinstance(cls, type) else "expr")

        connect_material_expressions = staticmethod(lambda *a: True)
        connect_material_property = staticmethod(lambda *a: True)
        recompile_material = staticmethod(lambda m: None)
        set_material_instance_parent = staticmethod(lambda mi, p: mi.set_editor_property("parent", p))

        @staticmethod
        def set_material_instance_texture_parameter_value(mi, name, tex):
            calls["mi_params"].append((mi.get_name(), name, tex.get_name()))

        set_material_instance_static_switch_parameter_value = staticmethod(lambda mi, n, v: calls["mi_params"].append((mi.get_name(), n, v)))
        set_material_instance_scalar_parameter_value = staticmethod(lambda mi, n, v: calls["mi_params"].append((mi.get_name(), n, v)))

    u.MaterialEditingLibrary = MEL
    for name in ("MaterialFactoryNew", "MaterialInstanceConstantFactoryNew", "FbxImportUI", "MaterialExpressionTextureSampleParameter2D",
                 "MaterialExpressionScalarParameter", "MaterialExpressionVectorParameter", "MaterialExpressionMultiply",
                 "MaterialExpressionStaticSwitchParameter"):
        setattr(u, name, type(name, (_Obj,), {}))
    u.FbxImportUI = type("FbxImportUI", (_Obj,), {"static_mesh_import_data": _Obj(), "skeletal_mesh_import_data": _Obj()})
    u.LinearColor = lambda *a: a
    u.MaterialProperty = types.SimpleNamespace(**{k: k for k in ("MP_BASE_COLOR", "MP_NORMAL", "MP_ROUGHNESS", "MP_METALLIC", "MP_AMBIENT_OCCLUSION", "MP_EMISSIVE_COLOR")})
    u.MaterialSamplerType = types.SimpleNamespace(SAMPLERTYPE_NORMAL="n", SAMPLERTYPE_MASKS="m")
    u.TextureCompressionSettings = types.SimpleNamespace(TC_NORMALMAP="nm", TC_MASKS="mask")
    u.FBXImportType = types.SimpleNamespace(FBXIT_SKELETAL_MESH="sk", FBXIT_STATIC_MESH="sm")
    u.SystemLibrary = types.SimpleNamespace(get_engine_version=lambda: "5.4.4-123+++UE5", get_game_name=lambda: "MyGame")
    u.Paths = types.SimpleNamespace(
        project_config_dir=lambda: str(tmp_path / "Config"),
        project_saved_dir=lambda: str(tmp_path / "Saved"),
        project_content_dir=lambda: str(tmp_path / "Content"),
        project_plugins_dir=lambda: str(tmp_path / "Plugins"),
        convert_relative_path_to_full=lambda p: p,
    )
    u.AssetRegistryHelpers = types.SimpleNamespace(get_asset_registry=lambda: types.SimpleNamespace(scan_paths_synchronous=lambda paths, force: calls.setdefault("scanned", paths)))
    u.log = u.log_warning = u.log_error = lambda *a: None
    return u, calls, assets


@pytest.fixture()
def stash_unreal(tmp_path, monkeypatch):
    u, calls, assets = make_stub(tmp_path)
    monkeypatch.setitem(sys.modules, "unreal", u)
    monkeypatch.syspath_prepend(str(PLUGIN))
    for m in [m for m in sys.modules if m.startswith("stash_unreal")]:
        del sys.modules[m]
    pkg = importlib.import_module("stash_unreal")
    importer = importlib.import_module("stash_unreal.importer")
    exporter = importlib.import_module("stash_unreal.exporter")
    return pkg, importer, exporter, calls, assets


def test_choose_target(stash_unreal):
    _, importer, _, _, _ = stash_unreal
    native = {"targets": ["original", "unreal_native", "unreal"], "meta": {"ue_engine_version": "5.3"}}
    newer = {"targets": ["original", "unreal_native", "unreal"], "meta": {"ue_engine_version": "5.6"}}
    only_native = {"targets": ["original", "unreal_native"], "meta": {}}
    blender_asset = {"targets": ["blend", "glb", "unreal", "fbx"], "meta": {}}
    assert importer._choose_target(native) == "unreal_native"
    assert importer._choose_target(newer) == "unreal"  # can't open newer packages
    assert importer._choose_target(only_native) == "unreal_native"
    assert importer._choose_target(blender_asset) == "unreal"


def test_import_package(stash_unreal, tmp_path):
    pkg, importer, _, calls, assets = stash_unreal
    root = tmp_path / "pkgsrc" / "Crate"
    (root / "Textures").mkdir(parents=True)
    (root / "Crate.fbx").write_bytes(b"fbx")
    for ch in ("base_color", "normal", "orm"):
        (root / "Textures" / f"T_Crate_{ch}.png").write_bytes(b"png")
    (root / "stash_manifest.json").write_text(json.dumps({
        "name": "Crate", "kind": "model", "skeletal": False, "files": {"mesh": "Crate.fbx"},
        "materials": [{"name": "Crate", "maps": {ch: f"Textures/T_Crate_{ch}.png" for ch in ("base_color", "normal", "orm")}}],
    }))
    z = tmp_path / "Crate_unreal.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in root.rglob("*"):
            zf.write(f, f"Crate/{f.relative_to(root).as_posix()}")
    result = importer.import_package(str(z))
    assert any(p.startswith("/Game/Stash/Crate/Crate") for p in result)
    imported = [c[0] for c in calls["import"]]
    assert "Crate.fbx" in imported and "T_Crate_normal.png" in imported
    assert "/Game/Stash/_Shared/M_StashMaster" in calls["created"]
    assert ("MI_Crate", "UseORM", True) in calls["mi_params"]
    assert ("MI_Crate", "Normal", "T_Crate_normal") in calls["mi_params"]
    normal = assets["/Game/Stash/Crate/Textures/T_Crate_normal"]
    assert normal.props["compression_settings"] == "nm" and normal.props["srgb"] is False
    mesh = assets["/Game/Stash/Crate/Crate"]
    assert mesh.props["assigned"] == {0: "MI_Crate"}


def test_import_native_never_overwrites(stash_unreal, tmp_path):
    _, importer, _, calls, _ = stash_unreal
    (tmp_path / "Content" / "Env").mkdir(parents=True)
    (tmp_path / "Content" / "Env" / "SM_Old.uasset").write_bytes(b"mine")
    z = tmp_path / "native.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Content/Env/SM_Old.uasset", b"theirs")
        zf.writestr("Content/Env/SM_New.uasset", b"new")
        zf.writestr("../evil.txt", b"x")
        zf.writestr("README_STASH.txt", b"hi")
    written = importer.import_native(str(z))
    assert written == ["Content/Env/SM_New.uasset"]
    assert (tmp_path / "Content" / "Env" / "SM_Old.uasset").read_bytes() == b"mine"
    assert not (tmp_path.parent / "evil.txt").exists()
    assert calls["scanned"] == ["/Game"]


def test_exporter_channel_guess(stash_unreal):
    _, _, exporter, _, _ = stash_unreal
    assert exporter.guess_channel("T_Rock_N") == "normal"
    assert exporter.guess_channel("NormalMap") == "normal"
    assert exporter.guess_channel("T_Rock_ORM") == "orm"
    assert exporter.guess_channel("BaseColor") == "base_color"
    assert exporter.guess_channel("T_Rock_D") == "base_color"


def test_config_roundtrip(stash_unreal, tmp_path, monkeypatch):
    pkg, *_ = stash_unreal
    monkeypatch.delenv("STASH_URL", raising=False)
    cfg = pkg.load_config()
    assert cfg["server_url"] == "http://localhost:8420"
    assert (tmp_path / "Config" / "StashBridge.json").exists()
    monkeypatch.setenv("STASH_URL", "http://nas:8420")
    assert pkg.load_config()["server_url"] == "http://nas:8420"


def test_plugin_clients_in_sync():
    """The Blender add-on and Unreal plugin ship the same stdlib client - keep the copies identical."""
    blender = PLUGIN.parents[3] / "blender_addon" / "stash_bridge" / "client.py"
    assert (PLUGIN / "stash_unreal" / "client.py").read_text() == blender.read_text()

from pathlib import Path

import pytest

from stash.ingest.formats import is_probably_hdri, kind_for_path, split_texture_name
from stash.search import fts_query


@pytest.mark.parametrize(
    "stem,base,channel",
    [
        ("Rock_01_4K_BaseColor", "Rock_01", "base_color"),
        ("T_Rock_D", "Rock", "base_color"),
        ("Planks_4K_Normal", "Planks", "normal"),
        ("wood_floor_diff_2k", "wood_floor", "base_color"),
        ("wood_floor_nor_gl_2k", "wood_floor", "normal"),
        ("Brick_ORM", "Brick", "orm"),
        ("Metal007_2K-PNG_Roughness", "Metal007", "roughness"),
        ("Ground054_1K_Color", "Ground054", "base_color"),
        ("Fabric_AmbientOcclusion", "Fabric", "ao"),
        ("chair", "chair", None),
    ],
)
def test_split_texture_name(stem, base, channel):
    assert split_texture_name(stem) == (base, channel)


def test_kind_for_path():
    assert kind_for_path(Path("a/b.FBX")) == "model"
    assert kind_for_path(Path("a.blend")) == "blend"
    assert kind_for_path(Path("x.uasset")) == "unreal"
    assert kind_for_path(Path("x.exr")) == "image"
    assert kind_for_path(Path("readme.txt")) is None


def test_hdri_detection():
    assert is_probably_hdri(Path("sky.hdr"), 4096, 2048)
    assert not is_probably_hdri(Path("mask.exr"), 1024, 1024)
    assert not is_probably_hdri(Path("photo.jpg"), 4096, 2048)


def test_fts_query():
    assert fts_query("") is None
    assert fts_query("red chair") == '"red"* AND "chair"*'
    assert fts_query('"old barn" -modern') == '"old barn" NOT "modern"*'
    assert fts_query("SM_Chair") == '"SM_Chair"*'


def test_uasset_class_skips_helper_imports(tmp_path, monkeypatch):
    """Packages of classes Stash doesn't know still get their own class, not the MetaData helper."""
    from stash.ingest import uasset

    p = tmp_path / "Thing.uasset"
    p.write_bytes(b"\0" * 64)
    for imports, expected in (
        (["MetaData", "LandscapeLayerInfoObject"], "LandscapeLayerInfoObject"),
        (["CableComponent", "MetaData", "UserDefinedStruct", "UserDefinedStructEditorData"], "UserDefinedStruct"),
        (["MetaData", "SomeFutureClass"], "SomeFutureClass"),
    ):
        monkeypatch.setattr(uasset, "_parse_header", lambda data, info, imports=imports: info.class_imports.extend(imports))
        assert uasset.parse_uasset(p).asset_class == expected

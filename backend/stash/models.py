"""ORM models.

The core entity is the Asset. Assets come from Sources (a folder, an Unreal project,
a .blend file, uploads, or bundles pushed by the Unreal/Blender integrations). Assets
can be nested (a .blend file contains objects, materials, actions; an object uses
materials; a material uses textures) - containment is `parent_id`, every other
relationship is an AssetLink.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uid() -> str:
    return uuid.uuid4().hex


# Asset kinds. Kept as plain strings so new kinds don't need a migration.
KINDS = [
    "model",  # static mesh / prop
    "skeletal_model",  # rigged mesh (armature / skeleton)
    "animation",  # action / anim sequence
    "material",  # material (node based, procedural or PBR texture set)
    "texture",  # single image
    "hdri",  # environment map
    "node_group",  # Blender geometry/shader node group
    "scene",  # .blend file, .umap level, or a multi-object scene file
    "blueprint",  # Unreal blueprint
    "unreal_asset",  # other Unreal asset types (Niagara, data tables, ...)
    "addon",  # Blender add-on / extension
    "audio",
    "other",
]

asset_tags = Table(
    "asset_tags",
    Base.metadata,
    Column("asset_id", ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # folder | unreal_project | blender_file | uploads | bundles
    kind: Mapped[str] = mapped_column(String(32), default="folder")
    path: Mapped[str] = mapped_column(Text, default="")
    # linked: index files where they are. import: copy them into the Stash store.
    mode: Mapped[str] = mapped_column(String(16), default="linked")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    assets: Mapped[list["Asset"]] = relationship(back_populates="source", passive_deletes=True)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("source_id", "source_path", "sub_key", name="uq_asset_origin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), default=new_uid, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True, default="other")
    description: Mapped[str] = mapped_column(Text, default="")
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=True, index=True)
    # Path of the originating file relative to the source root (or absolute for single files).
    source_path: Mapped[str] = mapped_column(Text, default="")
    # Identifies a sub-asset inside a container file, e.g. "object:Chair" inside a .blend.
    sub_key: Mapped[str] = mapped_column(String(300), default="")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=True, index=True)
    format: Mapped[str] = mapped_column(String(16), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | missing | error | pending
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Free-form technical metadata: poly counts, bones, animation ranges, material graph, ue class...
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    # Feature flags kept as columns so they can be filtered fast.
    is_rigged: Mapped[bool] = mapped_column(Boolean, default=False)
    is_animated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_procedural: Mapped[bool] = mapped_column(Boolean, default=False)
    has_textures: Mapped[bool] = mapped_column(Boolean, default=False)
    poly_count: Mapped[int] = mapped_column(Integer, default=0)
    thumbnail_file_id: Mapped[int | None] = mapped_column(ForeignKey("asset_files.id", ondelete="SET NULL", use_alter=True), nullable=True)
    preview_file_id: Mapped[int | None] = mapped_column(ForeignKey("asset_files.id", ondelete="SET NULL", use_alter=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped[Source | None] = relationship(back_populates="assets")
    parent: Mapped["Asset | None"] = relationship(remote_side="Asset.id", back_populates="children")
    children: Mapped[list["Asset"]] = relationship(back_populates="parent", passive_deletes=True)
    files: Mapped[list["AssetFile"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", foreign_keys="AssetFile.asset_id"
    )
    tags: Mapped[list["Tag"]] = relationship(secondary=asset_tags, back_populates="assets")
    thumbnail: Mapped["AssetFile | None"] = relationship(foreign_keys=[thumbnail_file_id], post_update=True)
    preview: Mapped["AssetFile | None"] = relationship(foreign_keys=[preview_file_id], post_update=True)


class AssetFile(Base):
    """A file belonging to an asset: original source, preview GLB, thumbnail, texture, export..."""

    __tablename__ = "asset_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    # source | preview | thumbnail | texture | blend | export | sidecar | ue_package | gallery
    role: Mapped[str] = mapped_column(String(32), index=True)
    # "store": path relative to data/store. "source": absolute path on disk (linked source).
    storage: Mapped[str] = mapped_column(String(16), default="store")
    path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(300), default="")
    format: Mapped[str] = mapped_column(String(16), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    # For textures: channel (base_color, normal, roughness, ...). For exports: target/preset key.
    label: Mapped[str] = mapped_column(String(100), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    asset: Mapped[Asset] = relationship(back_populates="files", foreign_keys=[asset_id])


class AssetLink(Base):
    __tablename__ = "asset_links"
    __table_args__ = (UniqueConstraint("from_id", "to_id", "relation", name="uq_link"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    from_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    to_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    # uses_material | uses_texture | uses_skeleton | animates | depends_on | variant_of
    relation: Mapped[str] = mapped_column(String(32))


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    color: Mapped[str] = mapped_column(String(16), default="")
    assets: Mapped[list[Asset]] = relationship(secondary=asset_tags, back_populates="tags")


class Project(Base):
    """A user-defined collection of assets, e.g. one game or film production."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    color: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    items: Mapped[list["ProjectAsset"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectAsset(Base):
    __tablename__ = "project_assets"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    note: Mapped[str] = mapped_column(Text, default="")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    project: Mapped[Project] = relationship(back_populates="items")
    asset: Mapped[Asset] = relationship()


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|done|error|cancelled
    priority: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[str] = mapped_column(Text, default="")
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(300), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Delivery(Base):
    """An asset the user sent from the web UI to an open Blender/Unreal session (the plugins poll this)."""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    app: Mapped[str] = mapped_column(String(16), index=True)  # blender | unreal
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    target: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | delivered | failed
    client: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

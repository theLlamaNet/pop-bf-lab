"""Shared data models for archives, OVA variables and renderable assets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import jade_mesh


@dataclass
class OvaVariable:
    name: str
    offset: int
    source: str
    var_offset: int | None = None
    var_type: int | None = None
    flags: int | None = None
    structure_base: int | None = None
    value_absolute: int | None = None
    value_size: int | None = None
    num_elem: int | None = None


@dataclass
class OvaStructure:
    base: int
    count: int
    names_base: int
    names_size: int
    complete_names: int
    truncated: bool
    init_base: int | None = None
    init_size: int | None = None
    source_format: str = "Jade"
    records_base: int | None = None
    names_available: bool = True
    names_encrypted: bool = False
    container_end: int | None = None
    name_slots: int | None = None


@dataclass
class TextureInfo:
    index: int
    offset: int
    data_offset: int
    data_end: int
    width: int
    height: int
    texture_type: int
    key: int
    format: str
    storage_width: int
    storage_height: int


@dataclass
class PopFileEntry:
    index: int
    offset: int
    size: int
    magic: int
    key: int
    data_offset: int
    data_type: int | None


@dataclass
class MeshInfo:
    index: int
    key: int
    entry_index: int
    version: int
    vertices: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]
    uvs: list[tuple[float, float]]
    uv_indices: list[tuple[int, int, int]]
    material_ids: list[tuple[int, int]]
    material_pack_key: int | None = None
    object_name: str = ""
    second_vertices: list[tuple[float, float, float]] | None = None
    second_faces: list[tuple[int, int, int]] | None = None
    second_uvs: list[tuple[float, float]] | None = None
    second_uv_indices: list[tuple[int, int, int]] | None = None
    second_material_ids: list[tuple[int, int]] | None = None
    normals: list[tuple[float, float, float]] | None = None
    skin_bones: list[jade_mesh.SkinBone] | None = None
    skin_flags: int = 0
    source_joint_names: tuple[str, ...] = ()
    layout_name: str = ""
    vertex_colors: list[tuple[float, float, float, float]] | None = None


@dataclass
class MeshMaterial:
    material_id: int
    material_key: int | None
    texture_key: int | None
    face_start: int
    face_count: int


@dataclass
class MaterialInfo:
    index: int
    material_id: int
    material_key: int | None
    texture_key: int | None
    normal_key: int | None = None
    # A second Jade material layer is not inherently a normal map.  It is
    # exposed separately and promoted to normal only for the WW archives.
    secondary_key: int | None = None
    metallic: float = 0.0
    alpha: float = 1.0
    source_meshes: list[int] | None = None
    texture_offset: int | None = None
    specular_offset: int | None = None
    diffuse_offset: int | None = None
    ambient: int = 0xFFFFFFFF
    diffuse_color: int = 0xFFFFFFFF
    specular_color: int = 0x00000000
    opacity: float = 1.0
    specular_exponent: float = 0.0
    ambient_offset: int | None = None
    diffuse_color_offset: int | None = None
    specular_color_offset: int | None = None
    opacity_offset: int | None = None
    specular_exponent_offset: int | None = None
    material_kind: int | None = None


@dataclass
class BigFileEntry:
    index: int
    position: int
    key: int
    size: int
    name: str
    parent: int
    fat_index: int
    first_index: int
    compressed: bool = False
    data_header_size: int = 0
    compression: str = "none"


@dataclass
class BigFileInfo:
    path: Path
    version: int
    max_file: int
    max_dir: int
    size_fat: int
    num_fat: int
    universe_key: int
    encrypted_fat: bool
    entries: list[BigFileEntry]


@dataclass
class LegacyFolderEntry:
    index: int
    file: int
    child: int
    next: int
    prev: int
    parent: int
    name: str


@dataclass
class Asset:
    name: str
    index: int
    key: int
    position: int
    size: int
    compressed: bool
    fat_index: int = 0

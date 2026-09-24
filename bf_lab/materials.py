"""Jade material records and mesh-to-material associations."""
from __future__ import annotations

import math
import struct
from .models import (
    MaterialInfo,
    MeshInfo,
    PopFileEntry,
)
from .resources import (
    _PopReader,
    _parse_pop_file_entries,
)


def _jade_color_rgba(value: int) -> tuple[float, float, float, float]:
    """Convert the D3DCOLOR-style values stored by Jade to OpenGL RGBA."""
    if value in (0, 0x00000000):
        return (1.0, 1.0, 1.0, 1.0)
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
        ((value >> 24) & 0xFF) / 255.0,
    )


def _scan_pop_materials(data: bytes) -> tuple[dict[int, list[int]], dict[int, int]]:
    """Return material-pack -> material keys and material key -> texture key."""
    packs: dict[int, list[int]] = {}
    materials: dict[int, int] = {}
    for entry in _parse_pop_file_entries(data):
        blob = data[entry.data_offset:entry.data_offset + entry.size]
        if entry.data_type == 4 and len(blob) >= 12:
            try:
                r = _PopReader(blob[4:])
                if r.u32() != 0:
                    continue
                count = r.u32()
                if count > 1000:
                    continue
                packs[entry.key] = [r.u32() for _ in range(count)]
            except (ValueError, struct.error):
                continue
        elif entry.data_type == 5 and len(blob) >= 8:
            try:
                r = _PopReader(blob[4:])
                version = r.u32()
                if not 3 <= version <= 9:
                    continue
                r.u32()
                if version >= 8:
                    r.u32(); r.u32()
                r.u32(); r.u32(); r.u32()
                if r.pos + 4 > len(blob):
                    continue
                r.u32()
                if version >= 8:
                    r.u16()
                r.u32(); r.f32(); r.f32(); r.u32()
                if version == 9:
                    r.bytes(9)
                    if r.pos + 4 > len(blob):
                        continue
                    r.u32()
                if r.pos + 4 <= len(blob):
                    materials[entry.key] = r.u32()
            except (ValueError, struct.error):
                continue
    return packs, materials


def _classic_jade_material(entry: PopFileEntry, data: bytes) -> MaterialInfo | None:
    """Decode the 32-byte Jade single-material form (GRO type 3)."""
    if entry.data_type != 3 or entry.size < 36:
        return None
    base = entry.data_offset + 4
    payload = data[base:entry.data_offset + entry.size]
    ambient, diffuse, specular, spec_exp, opacity, _flags, texture_key, _mask = struct.unpack_from("<IIIffIII", payload, 0)
    if texture_key in (0, 0xFFFFFFFF):
        texture_key = None
    if not math.isfinite(opacity) or not -0.01 <= opacity <= 1.01:
        opacity = 1.0
    if not math.isfinite(spec_exp):
        spec_exp = 0.0
    return MaterialInfo(index=0, material_id=0, material_key=entry.key, texture_key=texture_key,
                        metallic=max(0.0, min(128.0, spec_exp)), alpha=max(0.0, min(1.0, opacity)),
                        ambient=ambient, diffuse_color=diffuse, specular_color=specular,
                        opacity=opacity, specular_exponent=spec_exp,
                        ambient_offset=base + 0, diffuse_color_offset=base + 4,
                        specular_color_offset=base + 8, specular_exponent_offset=base + 12,
                        opacity_offset=base + 16, texture_offset=base + 24, source_meshes=[])


def _modern_jade_material(entry: PopFileEntry, data: bytes,
                          valid_texture_keys: set[int] | None = None) -> MaterialInfo | None:
    """Decode Jade leaf materials (kinds 4..9), including multitexture layers."""
    payload_offset = entry.data_offset + 4
    payload = data[payload_offset:entry.data_offset + entry.size]
    if len(payload) < 28:
        return None
    kind = struct.unpack_from("<I", payload, 0)[0]
    base_by_kind = {4: 40, 5: 40, 6: 40, 7: 42, 8: 50, 9: 63}
    if kind not in base_by_kind:
        return None
    base = base_by_kind[kind]
    if base + 4 > len(payload):
        return None
    layer_stride = {8: 26, 9: 39}.get(kind, 0)
    if ((not layer_stride and len(payload) != base + 4)
            or (layer_stride and (len(payload) - (base + 4)) % layer_stride)):
        return None
    offsets = [base]
    if layer_stride:
        offsets = list(range(base, len(payload) - 3, layer_stride))
    keys = [struct.unpack_from("<I", payload, off)[0] for off in offsets]
    keys = [key for key in keys if key not in (0, 0xFFFFFFFF)]
    if valid_texture_keys is not None and keys and not any(key in valid_texture_keys for key in keys):
        return None
    texture_key = keys[0] if keys else None
    secondary_key = keys[1] if len(keys) > 1 else None
    ambient, diffuse, specular = struct.unpack_from("<III", payload, 4)
    spec_exp, opacity = struct.unpack_from("<ff", payload, 16)
    if not math.isfinite(opacity) or not -0.01 <= opacity <= 1.01:
        opacity = 1.0
    if not math.isfinite(spec_exp):
        spec_exp = 0.0
    return MaterialInfo(
        index=0, material_id=0, material_key=entry.key, texture_key=texture_key,
        secondary_key=secondary_key, metallic=max(0.0, min(128.0, spec_exp)), alpha=max(0.0, min(1.0, opacity)),
        ambient=ambient, diffuse_color=diffuse, specular_color=specular,
        opacity=opacity, specular_exponent=spec_exp,
        ambient_offset=payload_offset + 4, diffuse_color_offset=payload_offset + 8,
        specular_color_offset=payload_offset + 12, specular_exponent_offset=payload_offset + 16,
        opacity_offset=payload_offset + 20, texture_offset=payload_offset + base,
        material_kind=kind, source_meshes=[],
    )


def _scan_pop_material_records(data: bytes, valid_texture_keys: set[int] | None = None) -> dict[int, MaterialInfo]:
    """Read the Jade type-5 material records used by the editor preview."""
    records: dict[int, MaterialInfo] = {}
    for entry in _parse_pop_file_entries(data):
        if entry.data_type != 5 or entry.size < 16:
            continue
        try:
            # io_scene_pop identifies POP material records by FileEntry type 5.
            # Type 3 has unrelated uses in these assets and must not be
            # promoted to a material merely because its first 32 bytes fit.
            blob = data[entry.data_offset:entry.data_offset + entry.size]
            r = _PopReader(blob[4:])
            version = r.u32()
            # Keep this retail interpretation for display.  Treating every
            # 4..9 word as a Toolkit leaf kind exposes engine colour words as
            # OpenGL modulation colours (solid blue/red over valid textures).
            if not 3 <= version <= 9:
                modern = _modern_jade_material(entry, data, valid_texture_keys)
                if modern is not None:
                    modern.index = len(records)
                    modern.material_id = len(records)
                    records[entry.key] = modern
                continue
            r.u32()
            if version >= 8:
                r.u32(); r.u32()
            r.u32(); r.u32(); r.u32()
            flags_offset = entry.data_offset + 4 + r.pos
            r.u32()
            if version >= 8:
                r.u16()
            r.u32()
            specular_offset = entry.data_offset + 4 + r.pos
            specular = r.f32()
            diffuse_offset = entry.data_offset + 4 + r.pos
            diffuse = r.f32()
            r.u32()
            if version == 9:
                r.bytes(9)
                if r.pos + 4 > len(r.data):
                    continue
                r.u32()
            if r.pos + 4 > len(r.data):
                texture_key = None
                texture_offset = None
            else:
                texture_offset = entry.data_offset + 4 + r.pos
                texture_key = r.u32()
                if version == 6 and valid_texture_keys and texture_key not in valid_texture_keys:
                    # Sands of Time demo materials can place extra fields before
                    # the diffuse texture key. Match Blender's aligned search,
                    # and retain the actual offset for material replacement.
                    for offset in range(r.pos, len(r.data) - 3, 4):
                        candidate = struct.unpack_from("<I", r.data, offset)[0]
                        if candidate in valid_texture_keys:
                            texture_key = candidate
                            texture_offset = entry.data_offset + 4 + offset
                            r.pos = offset + 4
                            break
            secondary_key = None
            if valid_texture_keys and texture_offset is not None:
                # The Blender POP reader intentionally stopped after the base
                # texture. Remaining aligned key fields are extra stages;
                # accept only keys that actually resolve to a texture in this
                # BF, which prevents flags/colours being misidentified.
                for offset in range(r.pos, len(r.data) - 3, 4):
                    candidate = struct.unpack_from("<I", r.data, offset)[0]
                    if candidate != texture_key and candidate in valid_texture_keys:
                        secondary_key = candidate
                        break
            records[entry.key] = MaterialInfo(
                index=len(records), material_id=len(records), material_key=entry.key,
                texture_key=texture_key, secondary_key=secondary_key,
                metallic=max(0.0, min(1.0, float(specular))),
                alpha=max(0.0, min(1.0, float(diffuse))), source_meshes=[],
                texture_offset=texture_offset, specular_offset=specular_offset,
                diffuse_offset=diffuse_offset,
            )
        except (ValueError, struct.error):
            continue
    return records


def _scan_pop_material_import_records(
        data: bytes, valid_texture_keys: set[int] | None = None) -> dict[int, MaterialInfo]:
    """Return write targets using Toolkit's exact kind-dependent key offsets.

    Preview colour semantics deliberately remain in _scan_pop_material_records;
    this stricter view is only for cloning/patching material dependencies.
    """
    records = _scan_pop_material_records(data, valid_texture_keys)
    for entry in _parse_pop_file_entries(data):
        if entry.data_type != 5:
            continue
        modern = _modern_jade_material(entry, data, valid_texture_keys)
        if modern is None:
            continue
        previous = records.get(entry.key)
        modern.index = previous.index if previous is not None else len(records)
        modern.material_id = previous.material_id if previous is not None else len(records)
        records[entry.key] = modern
    return records


def _associate_mesh_material_packs(data: bytes, meshes: list[MeshInfo]) -> None:
    """Use .gao records to associate mesh hashes with their material packs."""
    by_mesh = {m.key: m for m in meshes}
    entries = _parse_pop_file_entries(data)
    by_key = {entry.key: entry for entry in entries}

    def assign_visual(gro_key: int, grm_key: int, name: str) -> None:
        direct = by_mesh.get(gro_key)
        if direct is not None:
            direct.material_pack_key = grm_key
            direct.object_name = name
            return
        # GAO can point to a geometry group instead of a type-1 mesh.
        # Resolve every mesh key stored by the group, as MeshSwap.cpp does.
        group = by_key.get(gro_key)
        if group is None or group.data_type == 1:
            return
        payload = data[group.data_offset + 4:group.data_offset + group.size]
        seen: set[int] = set()
        for offset in range(0, len(payload) - 3):
            candidate = struct.unpack_from("<I", payload, offset)[0]
            mesh = by_mesh.get(candidate)
            if mesh is not None and candidate not in seen:
                mesh.material_pack_key = grm_key
                mesh.object_name = name
                seen.add(candidate)

    for entry in entries:
        if entry.data_type != struct.unpack("<I", b".gao")[0] or entry.size < 24:
            continue
        try:
            # The FileEntry's first dword is '.gao'; Gao.cpp consumes it as
            # the type before deserialising this payload.
            payload = data[entry.data_offset + 4:entry.data_offset + entry.size]
            _version, _editor_flags, identity, name_len = struct.unpack_from("<4I", payload, 0)
            if name_len > 4096 or 16 + name_len > len(payload):
                continue
            name = payload[16:16 + name_len].rstrip(b"\0").decode("latin-1", errors="replace")
            matrix_offset = 16 + name_len + 10
            bounds_size = 48 if identity & 0x00080000 else 24
            visual_offset = matrix_offset + 68 + bounds_size
            if identity & 0x00004000 and visual_offset + 8 <= len(payload):
                gro_key, grm_key = struct.unpack_from("<II", payload, visual_offset)
                assign_visual(gro_key, grm_key, name)
        except (ValueError, struct.error):
            continue

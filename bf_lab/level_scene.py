"""Small, bounds-checked Jade level graph reader and transform writer."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from .resources import _parse_pop_file_entries

GAO = struct.unpack("<I", b".gao")[0]


@dataclass
class LevelObject:
    key: int
    entry_index: int
    name: str
    mesh_key: int | None
    material_key: int | None
    matrix_offset: int
    basis: tuple[tuple[float, float, float], ...]
    position: tuple[float, float, float]
    scale: tuple[float, float, float]
    bounds: tuple[float, ...]
    kind: str
    source_asset_index: int = 0
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    dirty: bool = False
    raw_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)


def scan_level_objects(data: bytes, source_asset_index: int = 0) -> list[LevelObject]:
    objects = []
    for entry in _parse_pop_file_entries(data):
        if entry.data_type != GAO or entry.size < 80:
            continue
        payload = data[entry.data_offset + 4:entry.data_offset + entry.size]
        try:
            _version, _flags, identity, name_len = struct.unpack_from("<4I", payload)
            if not 1 <= name_len <= 4096 or 16 + name_len + 78 > len(payload):
                continue
            name = payload[16:16 + name_len].rstrip(b"\0").decode("latin-1", "replace")
            matrix = 16 + name_len + 10
            values = struct.unpack_from("<16f", payload, matrix)
            if not all(math.isfinite(v) for v in values):
                continue
            basis = tuple(tuple(values[row * 4 + col] for col in range(3)) for row in range(3))
            raw_scale = tuple(values[row * 4 + 3] for row in range(3))
            scale = tuple(value or 1.0 for value in raw_scale)
            position = tuple(values[12:15])
            bounds_at = matrix + 68 + (24 if identity & 0x80000 else 0)
            bounds = struct.unpack_from("<6f", payload, bounds_at)
            visual_at = matrix + 68 + (48 if identity & 0x80000 else 24)
            mesh_key = material_key = None
            if identity & 0x4000 and visual_at + 8 <= len(payload):
                mesh_key, material_key = struct.unpack_from("<II", payload, visual_at)
            lower = name.casefold()
            kind = ("trigger" if any(s in lower for s in ("trigger", "trig", "volume", "portal"))
                    else "light" if any(s in lower for s in ("light", "lamp", "lum_"))
                    else "mesh" if mesh_key is not None else "object")
            objects.append(LevelObject(entry.key, entry.index, name or f"Object 0x{entry.key:08X}",
                                       mesh_key, material_key, entry.data_offset + 4 + matrix,
                                       basis, position, scale, bounds, kind, source_asset_index,
                                       raw_scale=raw_scale))
        except (struct.error, ValueError):
            continue
    return objects


def write_object_transform(data: bytes, obj: LevelObject) -> bytes:
    """Patch only the 64-byte GAO matrix/position region, retaining other bytes."""
    if not obj.dirty:
        return data
    entries = _parse_pop_file_entries(data)
    entry = entries[obj.entry_index]
    if entry.key != obj.key or entry.data_type != GAO:
        raise ValueError("The level object no longer matches its source resource.")
    if not entry.data_offset <= obj.matrix_offset or obj.matrix_offset + 60 > entry.data_offset + entry.size:
        raise ValueError("Invalid level object transform offset.")
    rotated = object_basis(obj)
    scales = (raw if raw == 0.0 and scale == 1.0 else scale
              for raw, scale in zip(obj.raw_scale, obj.scale))
    values = [v for row, scale in zip(rotated, scales) for v in (*row, scale)]
    values.extend(obj.position)
    output = bytearray(data)
    struct.pack_into("<15f", output, obj.matrix_offset, *values)
    return bytes(output)


def object_basis(obj: LevelObject) -> tuple[tuple[float, float, float], ...]:
    rx, ry, rz = (math.radians(v) for v in obj.rotation)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    # Intrinsic XYZ, stored as Jade's three basis vectors and three scale values.
    rows = ((cy * cz, cy * sz, -sy),
            (sx * sy * cz - cx * sz, sx * sy * sz + cx * cz, sx * cy),
            (cx * sy * cz + sx * sz, cx * sy * sz - sx * cz, cx * cy))
    # Preserve the imported orientation. Rotation fields are relative to it.
    rotated = tuple(tuple(sum(obj.basis[k][col] * row[k] for k in range(3))
                          for col in range(3)) for row in rows)
    return rotated


def level_asset_kind(name: str) -> str | None:
    lower = name.casefold()
    for kind in ("wow", "wol"):
        if f".{kind}" in lower or f"_{kind}_" in lower or lower.endswith(f"_{kind}"):
            return kind
    return None


def wol_wow_keys(data: bytes) -> set[int]:
    """WOL dependency records store a WOW key immediately before '.wow'."""
    keys = set()
    needle = b".wow"
    start = 0
    while True:
        at = data.find(needle, start)
        if at < 0:
            return keys
        if at >= 4:
            keys.add(struct.unpack_from("<I", data, at - 4)[0])
        start = at + 4

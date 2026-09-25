"""Opened project state and staged asset changes."""
from __future__ import annotations

from pathlib import Path
from typing import Optional
import struct
from .archives import (
    _repack_legacy_bigfile,
    read_bigfile,
    read_bigfile_entry,
)
from .lzo import (
    _looks_like_pop_lzo,
    compress_pop_lzo,
    decompress_pop_lzo,
)
from .models import Asset
from .resources import _parse_pop_file_entries


class JadeProject:
    def __init__(self) -> None:
        self.path: Optional[Path] = None
        self.kind: Optional[str] = None
        self.info = None
        self.assets: list[Asset] = []
        self.raw_bin: Optional[bytes] = None
        self.decoded_bin: Optional[bytes] = None
        self.direct_compressed = False
        self.modified = False
        self.mesh_patches: dict[int, dict[int, tuple[int, bytes, bytes]]] = {}
        self.resource_additions: dict[int, dict[int, tuple[int, int, bytes]]] = {}
        self.collision_additions: dict[int, list[tuple[int, int, bytes, int]]] = {}

    @property
    def title(self) -> str:
        return self.path.name if self.path else "No file open"

    def open_bf(self, path: Path) -> None:
        info = read_bigfile(path)
        self.mesh_patches.clear()
        self.resource_additions.clear()
        self.collision_additions.clear()
        self.path = path
        self.kind = "bf"
        self.info = info
        self.raw_bin = None
        self.decoded_bin = None
        self.direct_compressed = False
        self.assets = [
            Asset(e.name, e.index, e.key, e.position, e.size & 0x7FFFFFFF,
                  bool(e.compressed), e.fat_index)
            for e in info.entries
        ]
        self.modified = False

    def open_bin(self, path: Path) -> None:
        raw = path.read_bytes()
        decoded = raw
        compressed = False
        if _looks_like_pop_lzo(raw):
            decoded = decompress_pop_lzo(raw)
            compressed = True
        self.mesh_patches.clear()
        self.resource_additions.clear()
        self.collision_additions.clear()
        self.path = path
        self.kind = "bin"
        self.info = None
        self.raw_bin = raw
        self.decoded_bin = decoded
        self.direct_compressed = compressed
        self.assets = [Asset(path.name, 0, 0, 0, len(decoded), compressed)]
        self.modified = False

    def open_dec(self, path: Path) -> None:
        data = path.read_bytes()
        self.mesh_patches.clear()
        self.resource_additions.clear()
        self.collision_additions.clear()
        self.path = path
        self.kind = "dec"
        self.info = None
        self.raw_bin = data
        self.decoded_bin = data
        self.direct_compressed = False
        self.assets = [Asset(path.name, 0, 0, 0, len(data), False)]
        self.modified = False

    def read_asset(self, asset: Asset) -> bytes:
        if self.kind in ("bin", "dec"):
            data = self.decoded_bin or b""
        else:
            entry = next(e for e in self.info.entries if e.index == asset.index)
            data = read_bigfile_entry(self.path, entry)
        return self.apply_mesh_patches(asset.index, data)

    def apply_mesh_patches(self, asset_index: int, data: bytes) -> bytes:
        patches = self.mesh_patches.get(asset_index, {})
        additions = self.resource_additions.get(asset_index, {})
        collisions = self.collision_additions.get(asset_index, [])
        if not patches and not additions and not collisions:
            return data
        from .material_stream import insert_material_resources, validate_material_resources
        from .mesh_collision import insert_collision_resources
        data = insert_collision_resources(data, collisions)
        def apply_patches(payload, selected):
            for key, original, replacement in selected:
                entries = _parse_pop_file_entries(payload)
                candidates = [entry for entry in entries if entry.key == key and
                              payload[entry.data_offset:entry.data_offset+entry.size] in (original, replacement)]
                if not candidates:
                    raise ValueError(f"Conflicting editor changes on resource 0x{key:08X}; rescan the asset.")
                for entry in reversed(candidates):
                    payload = (payload[:entry.offset] +
                               struct.pack("<III", len(replacement), entry.magic, key) +
                               replacement + payload[entry.data_offset+entry.size:])
            return payload

        added_keys = {key for key, _, _ in additions.values()}
        data = apply_patches(data, (patch for patch in patches.values()
                                    if patch[0] not in added_keys))
        data = insert_material_resources(data, list(additions.values()))
        data = apply_patches(data, (patch for patch in patches.values()
                                    if patch[0] in added_keys))
        validate_material_resources(data, {key for key, _, _ in additions.values()})
        return data

    def save_bin_as(self, target: Path, data: bytes) -> None:
        encoded = compress_pop_lzo(data) if self.direct_compressed else data
        target.write_bytes(encoded)

    def replace_bf_entry(self, asset: Asset, data: bytes, target: Path) -> None:
        entry = next(e for e in self.info.entries if e.index == asset.index)
        _repack_legacy_bigfile(self.path, entry, data, target)

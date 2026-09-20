"""Jade BF indexing, extraction and archive rebuilding."""
from __future__ import annotations

from pathlib import Path
import struct
from .config import (
    LEGACY_BF_FILE_ENTRY_SIZE,
    LEGACY_BF_FILE_TABLE_ENTRY_SIZE,
    LEGACY_BF_HEADER_SIZE,
    LZO_BLOCK_SIZE,
)
from .lzo import (
    _looks_like_pop_lzo,
    compress_pop_lzo,
    decompress_pop_lzo,
)
from .models import (
    BigFileEntry,
    BigFileInfo,
    LegacyFolderEntry,
)
from .textures import _scan_pop_textures


def _legacy_folder_entries(path: Path) -> list[LegacyFolderEntry]:
    """Read the FolderEntry table used by bf_repacker_2018_05_23_1419."""
    with path.open("rb") as stream:
        header = stream.read(LEGACY_BF_HEADER_SIZE)
        if len(header) != LEGACY_BF_HEADER_SIZE:
            raise ValueError("The .bf file is too short for the legacy header.")
        magic, version, fcount, dcount, _unk2, _unk3, capacity, _unk4, _universe_key, _fcount2, _dcount2, _file_id_offset, _unk5, _unk6, _last = struct.unpack(
            "<4sIIIQQIIIIIIiII", header
        )
        if magic != b"BIG\0" or version not in (37, 38):
            raise ValueError("The selected BF does not use the legacy v37/v38 layout.")
        folder_base = LEGACY_BF_HEADER_SIZE + capacity * LEGACY_BF_FILE_TABLE_ENTRY_SIZE + capacity * LEGACY_BF_FILE_ENTRY_SIZE
        stream.seek(folder_base)
        folders: list[LegacyFolderEntry] = []
        for index in range(min(capacity, 2_000_000)):
            raw = stream.read(84)
            if len(raw) != 84 or raw == b"\0" * 84:
                break
            file_id, child, next_id, prev, parent = struct.unpack_from("<Iiiii", raw, 0)
            name = raw[20:84].split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
            folders.append(LegacyFolderEntry(index, file_id, child, next_id, prev, parent, name))
        if not folders:
            raise ValueError("Legacy FolderEntry table not found.")
        return folders


def _legacy_folder_path(folders: list[LegacyFolderEntry], folder_index: int) -> Path:
    if folder_index < 0 or folder_index >= len(folders):
        raise ValueError(f"Invalid FolderEntry index: {folder_index}")
    parts: list[str] = []
    seen: set[int] = set()
    current = folder_index
    while current != -1:
        if current in seen:
            raise ValueError("Cycle in the legacy FolderEntry hierarchy.")
        seen.add(current)
        folder = folders[current]
        parts.append(folder.name or f"Folder_{current}")
        current = folder.parent
    return Path(*reversed(parts))


def _legacy_asset_path(root_dir: Path, folders: list[LegacyFolderEntry], entry: BigFileEntry) -> Path:
    return root_dir / _legacy_folder_path(folders, entry.parent) / entry.name


def extract_legacy_bf_as_root(source: Path, target_dir: Path) -> int:
    """Extract a legacy BF into the exact ROOT/... hierarchy of the old repacker."""
    info = read_bigfile(source)
    if info.version not in (37, 38):
        raise ValueError("Export all assets with ROOT is available for legacy v37/v38 BF archives.")
    folders = _legacy_folder_entries(source)
    if (folders[0].name or "ROOT").casefold() != "root":
        raise ValueError(f"BF FolderEntry 0 is '{folders[0].name}', expected ROOT.")
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_bf = source.read_bytes()
    for entry in info.entries:
        output = _legacy_asset_path(target_dir, folders, entry)
        output.parent.mkdir(parents=True, exist_ok=True)
        start = entry.position + entry.data_header_size
        length = entry.size & 0x7FFFFFFF
        output.write_bytes(raw_bf[start:start + length])
    return len(info.entries)


def _legacy_file_data_length(data: bytes, folder_index: int, name: str) -> int:
    """Match the old repacker's ParseFileData calculation for size.grs."""
    magic = 0xEEFFC099
    if len(data) < 16:
        return len(data)
    dec_size, enc_size = struct.unpack_from("<2I", data, 0)
    file_magic = struct.unpack_from("<I", data, 13)[0]
    file_magic2 = struct.unpack_from("<I", data, 14)[0]
    if dec_size != enc_size and (file_magic == magic or file_magic2 == magic):
        pos = 0
        while pos + 8 <= len(data):
            size_dec, size_enc = struct.unpack_from("<2I", data, pos)
            pos += 8 + size_enc
            if size_dec != LZO_BLOCK_SIZE:
                break
        return min(len(data), pos + 4)
    if dec_size == enc_size and struct.unpack_from("<I", data, 12)[0] == magic:
        return min(len(data), struct.unpack_from("<I", data, 4)[0] + 12)
    last = len(data) - 1
    while last > 0 and data[last] == 0:
        last -= 1
    if folder_index == 1:
        return last + 7
    if folder_index == 3:
        return last + 4
    if folder_index in (0, 2, 4):
        return last + 1
    return len(data)


def _update_legacy_size_grs_payload(info: BigFileInfo, payloads: dict[int, bytes], changed_indices: set[int]) -> None:
    """Refresh the logical LZO stream lengths stored by POP in size.grs."""
    if not changed_indices:
        return
    size_entry = next((entry for entry in info.entries if entry.name.casefold() == "size.grs"), None)
    if size_entry is None or size_entry.index not in payloads:
        return
    by_key = {entry.key: entry for entry in info.entries}
    data = bytearray(payloads[size_entry.index])
    changed = False
    for offset in range(0, len(data) - 7, 8):
        key, old_length = struct.unpack_from("<2I", data, offset)
        if key == 0:
            break
        entry = by_key.get(key)
        if entry is None or entry.index not in changed_indices or entry.index == size_entry.index:
            continue
        new_length = _legacy_file_data_length(payloads[entry.index], entry.parent, entry.name)
        if new_length != old_length:
            struct.pack_into("<I", data, offset + 4, new_length)
            changed = True
    if changed:
        payloads[size_entry.index] = bytes(data)


def build_legacy_bf_from_folder(template: Path, root_dir: Path, output: Path) -> int:
    """Rebuild a legacy BF from ROOT using the opened BF as its metadata template."""
    info = read_bigfile(template)
    if info.version not in (37, 38):
        raise ValueError("Build .BF from folder is available for legacy v37/v38 BF archives.")
    folders = _legacy_folder_entries(template)
    if not root_dir.is_dir() or root_dir.name.casefold() != "root":
        raise ValueError("Select the ROOT folder of the BF extraction itself.")
    original = template.read_bytes()
    payloads: dict[int, bytes] = {}
    missing: list[str] = []
    for entry in info.entries:
        path = _legacy_asset_path(root_dir.parent, folders, entry)
        if not path.is_file():
            missing.append(str(path.relative_to(root_dir.parent)))
            continue
        payloads[entry.index] = path.read_bytes()
    if missing:
        preview = ", ".join(missing[:8])
        suffix = "..." if len(missing) > 8 else ""
        raise ValueError(f"The ROOT folder is incomplete: {len(missing)} files are missing ({preview}{suffix}).")

    _update_legacy_size_grs_payload(info, payloads, set(payloads))

    ordered = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered[0].position
    rebuilt = bytearray(original[:prefix_end])
    original_cursor = prefix_end
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.size_fat * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    new_positions: dict[int, int] = {}
    for entry in ordered:
        payload = payloads[entry.index]
        rebuilt.extend(original[original_cursor:entry.position])
        new_positions[entry.index] = len(rebuilt)
        struct.pack_into("<I", rebuilt, file_id_base + entry.index * LEGACY_BF_FILE_TABLE_ENTRY_SIZE, new_positions[entry.index])
        if len(payload) != (entry.size & 0x7FFFFFFF):
            struct.pack_into("<I", rebuilt, file_entry_base + entry.index * LEGACY_BF_FILE_ENTRY_SIZE, len(payload))
        rebuilt.extend(struct.pack("<I", len(payload)))
        rebuilt.extend(payload)
        original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
    rebuilt.extend(original[original_cursor:])

    size_grs_index = next((e.index for e in info.entries if e.name == "size.grs"), None)
    if size_grs_index is not None:
        size_payload = bytearray(payloads[size_grs_index])
        by_key = {entry.key: entry for entry in info.entries}
        for offset in range(0, len(size_payload) - 7, 8):
            file_id, _old_length = struct.unpack_from("<2I", size_payload, offset)
            if file_id == 0:
                break
            entry = by_key.get(file_id)
            if entry is not None and entry.index != size_grs_index:
                struct.pack_into("<I", size_payload, offset + 4, _legacy_file_data_length(payloads[entry.index], entry.parent, entry.name))
        if len(size_payload) != len(payloads[size_grs_index]):
            raise ValueError("size.grs has changed size; use a size.grs with the same size as the original.")
        start = new_positions[size_grs_index] + 4
        rebuilt[start:start + len(size_payload)] = size_payload

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rebuilt)
    return len(payloads)


def _read_big_header(stream) -> BigFileInfo:
    header = stream.read(44)
    if len(header) != 44:
        raise ValueError("The .bf file is too short to contain the Jade header.")
    magic, version, max_file, max_dir, _max_key, _root, _free_file, _free_dir, size_fat, num_fat, universe_key = struct.unpack("<4s10I", header)
    if magic not in (b"BIG\0", b"BUG\0"):
        raise ValueError(f"Unrecognized BIG header: {magic!r}")
    file_size = Path(stream.name).stat().st_size
    entries: list[BigFileEntry] = []
    descriptor_pos = 44
    for fat_index in range(num_fat):
        stream.seek(descriptor_pos)
        raw = stream.read(24)
        if len(raw) != 24:
            raise ValueError(f"Truncated FAT descriptor #{fat_index}.")
        fat_max_file, _fat_max_dir, pos_fat, next_pos_fat, first_index, _last_index = struct.unpack("<6I", raw)
        stream.seek(pos_fat)
        file_table = stream.read(fat_max_file * 8)
        ext_base = pos_fat + size_fat * 8
        stream.seek(ext_base)
        ext_table = stream.read(fat_max_file * LEGACY_BF_FILE_ENTRY_SIZE)
        if len(file_table) != fat_max_file * 8 or len(ext_table) != fat_max_file * LEGACY_BF_FILE_ENTRY_SIZE:
            raise ValueError(f"Truncated FAT #{fat_index}.")
        for i in range(fat_max_file):
            position, key = struct.unpack_from("<2I", file_table, i * 8)
            if key == 0xFFFFFFFF:
                continue
            ext = ext_table[i * LEGACY_BF_FILE_ENTRY_SIZE:(i + 1) * LEGACY_BF_FILE_ENTRY_SIZE]
            physical_size = struct.unpack_from("<I", ext, 0)[0] & 0x7FFFFFFF
            parent = struct.unpack_from("<I", ext, 12)[0]
            raw_name = ext[20:84].split(b"\0", 1)[0]
            name = raw_name.decode("ascii", errors="replace").strip() or f"<file_{first_index + i:06d}>"
            data_header_size = 0
            compressed = False
            compression = "none"
            if version in (37, 38) and position + 4 <= file_size:
                stream.seek(position)
                if struct.unpack("<I", stream.read(4))[0] == physical_size:
                    data_header_size = 4
                    stream.seek(position + 4)
                    compressed = _looks_like_pop_lzo(stream.read(min(32, physical_size)))
                    compression = "POP-LZO" if compressed else "none"
            entries.append(BigFileEntry(first_index + i, position, key, physical_size, name, parent,
                                        fat_index, first_index, compressed, data_header_size, compression))
        descriptor_pos = next_pos_fat - 24 if next_pos_fat != 0xFFFFFFFF else descriptor_pos + 24
    entries.sort(key=lambda e: (e.fat_index, e.index))
    return BigFileInfo(Path(stream.name), version, max_file, max_dir, size_fat, num_fat, universe_key, magic == b"BUG\0", entries)


def _read_legacy_bigfile(path: Path) -> BigFileInfo:
    with path.open("rb") as stream:
        header = stream.read(LEGACY_BF_HEADER_SIZE)
        if len(header) != LEGACY_BF_HEADER_SIZE:
            raise ValueError("The .bf file is too short for the legacy POP/Jade header.")
        magic, version, fcount, dcount, _unk2, _unk3, capacity, _unk4, universe_key, _fcount2, _dcount2, _file_id_offset, _unk5, _unk6, _last = struct.unpack("<4sIIIQQIIIIIIiII", header)
        if magic != b"BIG\0" or version not in (37, 38):
            raise ValueError(f"Unrecognized legacy POP layout (magic={magic!r}, v={version}).")
        if not (1 <= fcount <= capacity <= 2_000_000):
            raise ValueError("Implausible legacy .bf header.")
        file_id_base = LEGACY_BF_HEADER_SIZE
        file_entry_base = file_id_base + capacity * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
        file_size = path.stat().st_size
        if file_entry_base + fcount * LEGACY_BF_FILE_ENTRY_SIZE > file_size:
            raise ValueError("Truncated legacy FileEntry table.")
        entries = []
        for i in range(fcount):
            stream.seek(file_id_base + i * 8)
            position, key = struct.unpack("<2I", stream.read(8))
            stream.seek(file_entry_base + i * LEGACY_BF_FILE_ENTRY_SIZE)
            ext = stream.read(LEGACY_BF_FILE_ENTRY_SIZE)
            size_on_disk, _next, _prev, parent, _timestamp = struct.unpack_from("<5I", ext, 0)
            name = ext[20:84].split(b"\0", 1)[0].decode("ascii", errors="replace").strip() or f"<file_{i:06d}>"
            if position + 4 > file_size:
                raise ValueError(f"Legacy entry #{i} points beyond the file.")
            stream.seek(position + 4)
            prefix = stream.read(min(32, size_on_disk))
            compressed = _looks_like_pop_lzo(prefix)
            entries.append(BigFileEntry(i, position, key, size_on_disk, name, parent, 0, 0, compressed, 4,
                                        "POP-LZO" if compressed else "none"))
        return BigFileInfo(path, version, capacity, dcount, capacity, 1, universe_key, False, entries)


def read_bigfile(path: Path) -> BigFileInfo:
    # POP v37/v38 uses the regular FAT header; size_of_fat can exceed file_count.
    with path.open("rb") as stream:
        return _read_big_header(stream)


def read_bigfile_entry(path: Path, entry: BigFileEntry) -> bytes:
    with path.open("rb") as stream:
        stream.seek(entry.position + entry.data_header_size)
        data = stream.read(entry.size & 0x7FFFFFFF)
    if len(data) != (entry.size & 0x7FFFFFFF):
        raise ValueError(f"Entry {entry.name} is truncated in the .bf.")
    return decompress_pop_lzo(data) if entry.compressed and entry.compression == "POP-LZO" else data


def _repack_legacy_bigfile(path: Path, selected: BigFileEntry, decoded_data: bytes, output: Path) -> None:
    original = path.read_bytes()
    info = read_bigfile(path)
    if info.version not in (37, 38):
        raise ValueError("Automatic BF rebuilding is implemented for v37/v38.")
    selected_payload = compress_pop_lzo(decoded_data) if selected.compressed else decoded_data
    payloads: dict[int, bytes] = {}
    for entry in info.entries:
        if entry.index == selected.index:
            payloads[entry.index] = selected_payload
        else:
            start = entry.position + entry.data_header_size
            length = entry.size & 0x7FFFFFFF
            payloads[entry.index] = original[start:start + length]
    _update_legacy_size_grs_payload(info, payloads, {selected.index})
    # File-table order and physical payload order are not guaranteed to match.
    # Preserve the original physical ordering while updating each indexed
    # FileIdOffset to its new position.
    ordered_entries = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered_entries[0].position
    prefix = bytearray(original[:prefix_end])
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.size_fat * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    cursor = prefix_end
    original_cursor = prefix_end
    for entry in ordered_entries:
        payload = payloads[entry.index]
        # Keep every unindexed byte between legacy entries. These gaps are
        # part of the container layout and must survive an unchanged rebuild.
        original_gap = original[original_cursor:entry.position]
        cursor += len(original_gap)
        struct.pack_into("<I", prefix, file_id_base + entry.index * 8, cursor)
        size_value = len(payload)
        struct.pack_into("<I", prefix, file_entry_base + entry.index * LEGACY_BF_FILE_ENTRY_SIZE, size_value)
        cursor += 4 + len(payload)
        original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
    with output.open("wb") as stream:
        stream.write(prefix)
        original_cursor = prefix_end
        for entry in ordered_entries:
            payload = payloads[entry.index]
            stream.write(original[original_cursor:entry.position])
            # The legacy data-block header stores the physical payload size;
            # the compression bit lives in FileEntry.size in the FAT table.
            stream.write(struct.pack("<I", len(payload)))
            stream.write(payload)
            original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
        stream.write(original[original_cursor:])


def _repack_legacy_bigfile_changes(path: Path, replacements: dict[int, bytes], output: Path) -> None:
    """Rebuild a legacy BF while applying decoded payload changes to multiple entries."""
    original = path.read_bytes()
    info = read_bigfile(path)
    if info.version not in (37, 38):
        raise ValueError("Automatic BF rebuilding is implemented for v37/v38.")

    entries_by_index = {entry.index: entry for entry in info.entries}
    payloads: dict[int, bytes] = {}
    for entry in info.entries:
        start = entry.position + entry.data_header_size
        length = entry.size & 0x7FFFFFFF
        if entry.index in replacements:
            payload = compress_pop_lzo(replacements[entry.index]) if entry.compressed else replacements[entry.index]
        else:
            payload = original[start:start + length]
        payloads[entry.index] = payload

    _update_legacy_size_grs_payload(info, payloads, set(replacements))

    ordered_entries = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered_entries[0].position
    prefix = bytearray(original[:prefix_end])
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.size_fat * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    cursor = prefix_end
    original_cursor = prefix_end
    for entry in ordered_entries:
        payload = payloads[entry.index]
        original_gap = original[original_cursor:entry.position]
        cursor += len(original_gap)
        struct.pack_into("<I", prefix, file_id_base + entry.index * 8, cursor)
        if len(payload) != (entry.size & 0x7FFFFFFF):
            struct.pack_into("<I", prefix, file_entry_base + entry.index * LEGACY_BF_FILE_ENTRY_SIZE, len(payload))
        cursor += 4 + len(payload)
        original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)

    with output.open("wb") as stream:
        stream.write(prefix)
        original_cursor = prefix_end
        for entry in ordered_entries:
            payload = payloads[entry.index]
            stream.write(original[original_cursor:entry.position])
            stream.write(struct.pack("<I", len(payload)))
            stream.write(payload)
            original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
        stream.write(original[original_cursor:])


def _patch_texture_key_in_asset(asset_data: bytes, texture_key: int, replacement_payload: bytes,
                                source_type: int, target_type: int,
                                width: int, height: int,
                                replacement_dimensions: tuple[int, int] | None = None) -> tuple[bytes, int]:
    """Patch matching POP texture entries, rebuilding their header if needed."""
    from .resources import _parse_pop_file_entries
    from .textures import _infer_mip_count
    data = bytearray(asset_data)
    target_width, target_height = replacement_dimensions or (width, height)
    if not (1 <= target_width <= 8192 and 1 <= target_height <= 8192):
        raise ValueError("POP texture dimensions must be between 1 and 8192 pixels.")
    if target_type in (5, 6, 7) and (target_width % 4 or target_height % 4):
        raise ValueError("Jade DXT textures require dimensions divisible by 4.")
    try:
        entries = _parse_pop_file_entries(data)
    except (ValueError, struct.error):
        return asset_data, 0
    matches = 0
    for resource in reversed(entries):
        if resource.key != texture_key or resource.size < 56:
            continue
        raw = bytes(data[resource.data_offset:resource.data_offset + resource.size])
        if (struct.unpack_from("<I", raw, 4)[0] != 0xFFFFFFFF
                or struct.unpack_from("<I", raw, 24)[0] != 0xCAD01234
                or struct.unpack_from("<I", raw, 32)[0] != 0xC0DEC0DE):
            continue
        old_type = struct.unpack_from("<I", raw, 40)[0]
        if old_type not in (source_type, target_type):
            continue
        old_w, old_h = struct.unpack_from("<II", raw, 44)
        pixel_start = 60 if old_type in (1, 5) else 56
        if old_type == 11 and struct.unpack_from("<I", raw, 36)[0] >= 4:
            pixel_start = 64
        blocks = max(1, (old_w + 3)//4) * max(1, (old_h + 3)//4)
        base_size = {0: old_w*old_h*3, 1: old_w*old_h,
                     5: blocks*8, 6: blocks*16, 7: blocks*16,
                     11: (old_w*old_h+1)//2}.get(old_type, 0)
        stub = len(raw) - pixel_start < base_size
        header = bytearray(raw[:56])
        # The first dimensions belong to TEX_tdst_File_Params. Preserve them
        # as TextureUpscale.cpp does; the cooked surface has its own dimensions.
        struct.pack_into("<III", header, 40, target_type, target_width, target_height)
        mip_count = 0
        if target_type in (5, 6, 7):
            mip_count = _infer_mip_count(target_width, target_height, len(replacement_payload),
                                         8 if target_type == 5 else 16) - 1
        struct.pack_into("<I", header, 52, mip_count)
        if stub:
            replacement_entry = bytes(header) + bytes(max(0, len(raw)-56))
        else:
            prefix = bytes(4) if target_type in (1, 5) else b""
            replacement_entry = bytes(header) + prefix + replacement_payload
        struct.pack_into("<I", data, resource.offset, len(replacement_entry))
        data[resource.data_offset:resource.data_offset + resource.size] = replacement_entry
        matches += 1
    return bytes(data), matches


def _collect_texture_key_replacements(path: Path, selected: BigFileEntry,
                                      texture_key: int, replacement_payload: bytes,
                                      source_type: int, target_type: int,
                                      width: int, height: int,
                                      selected_decoded: bytes,
                                      source_dimensions: tuple[int, int] | None = None) -> tuple[dict[int, bytes], list[str]]:
    """Find the same logical texture in other BF assets and patch all valid copies.

    Jade/POP assets can carry the same texture key in more than one container.
    The editor/repacker can therefore show the edited copy while the game later
    resolves another copy of the same key. Updating all matching copies keeps
    the key-to-payload invariant intact without changing material references.
    """
    info = read_bigfile(path)
    replacements: dict[int, bytes] = {}
    touched: list[str] = []

    for entry in info.entries:
        if entry.index == selected.index:
            decoded = selected_decoded
        else:
            try:
                decoded = read_bigfile_entry(path, entry)
            except Exception:
                continue
        patched, count = _patch_texture_key_in_asset(
            decoded, texture_key, replacement_payload, source_type, target_type,
            *(source_dimensions or (width, height)), replacement_dimensions=(width, height),
        )
        if count:
            replacements[entry.index] = patched
            touched.append(f"{entry.index}:{entry.name} ({count}x)")

    if selected.index not in replacements:
        raise ValueError(
            f"Texture 0x{texture_key:08X} was not found in the selected asset "
            "with compatible dimensions/format."
        )
    return replacements, touched

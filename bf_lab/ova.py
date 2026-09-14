"""OVA descriptors, variable discovery and diagnostic reports."""
from __future__ import annotations

import re
import struct
from .config import (
    AI_MAX_LEN_VAR,
    OVA_INFO_SIZE,
)
from .models import (
    OvaStructure,
    OvaVariable,
)


def _valid_identifier(name: str) -> bool:
    return (
        3 <= len(name) < AI_MAX_LEN_VAR
        and any(c.isalpha() or c == "_" for c in name)
        and all(c.isalnum() or c in "_()" for c in name)
    )


def _find_init_buffer_after_names(data: bytes, pos: int) -> tuple[int | None, int | None]:
    if pos + 4 > len(data):
        return None, None
    var2_size = struct.unpack_from("<I", data, pos)[0]
    if pos + 8 <= len(data):
        strings_size = struct.unpack_from("<I", data, pos + 4)[0]
        p = pos + 8 + var2_size + strings_size
        if var2_size <= 1024 * 1024 and strings_size <= 1024 * 1024 and p + 4 <= len(data):
            init_size = struct.unpack_from("<I", data, p)[0]
            if init_size <= len(data) - p - 4:
                return p + 4, init_size
    # POP37/38 may serialize the initial-value size directly after the names.
    if 0 < var2_size <= len(data) - pos - 4:
        return pos + 4, var2_size
    return None, None


def _pop_type_storage_size(var_type: int) -> int:
    """Serialized size of one POP/Jade variable element, from AI_gast_Types."""
    return {
        32: 4, 33: 4, 34: 4, 37: 12, 38: 4, 39: 4, 40: 4, 41: 4,
        42: 4, 43: 4, 44: 8, 45: 4, 46: 4, 48: 4, 49: 4, 50: 8,
        51: 96,
    }.get(var_type, 4)


def _recover_pop_init_buffer(data: bytes, structure: OvaStructure) -> tuple[int | None, int | None]:
    """Recover POP initial values when the editor-name table is absent/stripped."""
    if structure.init_base is not None and structure.init_size is not None:
        return structure.init_base, structure.init_size
    if structure.records_base is None or structure.container_end is None:
        return None, None

    records_end = structure.records_base + structure.count * OVA_INFO_SIZE
    if records_end + 4 > structure.container_end:
        return None, None

    required_size = 0
    for i in range(structure.count):
        info = structure.records_base + i * OVA_INFO_SIZE
        if info + OVA_INFO_SIZE > len(data):
            return None, None
        num_elem, packed_type_flags, var_offset = struct.unpack_from("<III", data, info)
        var_type = packed_type_flags & 0xFFFF
        elem_count = max(1, num_elem & 0x3FFFFFFF)
        dimensions = (num_elem >> 30) & 0x3
        end = var_offset + dimensions * 4 + elem_count * _pop_type_storage_size(var_type)
        if end < 0 or end > 1024 * 1024:
            return None, None
        required_size = max(required_size, end)
    if required_size <= 0:
        return None, None

    candidates: list[tuple[int, int]] = []
    names_end = structure.names_base + structure.names_size
    search_end = structure.container_end - required_size - 20
    for size_pos in range(records_end, max(records_end, search_end + 1), 4):
        if size_pos + 4 > len(data):
            break
        size = struct.unpack_from("<I", data, size_pos)[0]
        if size != required_size:
            continue
        # A VarInfo2 byte-size is also a multiple of 20. In stripped prototype
        # entries it can equal i_SizeInit, so do not treat that field as init size.
        if structure.names_size and size_pos == names_end and size % 20 == 0:
            continue
        init_base = size_pos + 4
        init_end = init_base + size
        if init_end + 20 > structure.container_end:
            continue
        candidates.append((init_base, size))

    if not candidates:
        return None, None
    if structure.names_available:
        outside = [c for c in candidates if c[0] >= names_end]
        if outside:
            return outside[0]
    return candidates[0]


def _detect_pop_name_span(data: bytes, names_base: int, count: int, container_end: int, full_span: int) -> int:
    full_end = names_base + full_span
    if full_end <= container_end:
        valid = sum(bool(_valid_identifier(data[names_base + i * AI_MAX_LEN_VAR:min(names_base + (i + 1) * AI_MAX_LEN_VAR, len(data))].split(b"\0", 1)[0].decode("ascii", errors="ignore"))) for i in range(count))
        if valid >= max(2, min(count, 8)):
            return full_span
    for end in range(names_base + 32, min(full_end, container_end) + 1):
        if end + 4 > container_end:
            break
        var2_size = struct.unpack_from("<I", data, end)[0]
        if not var2_size or var2_size > 1024 * 1024 or var2_size % 20 or end + 4 + var2_size > container_end:
            continue
        return end - names_base
    return 0


def _find_pop_ova_structures(data: bytes) -> list[OvaStructure]:
    candidates = []
    marker = b"\x99\xC0\xFF\xEE"
    start = 0
    while True:
        base = data.find(marker, start)
        if base < 0:
            break
        start = base + 4
        if base + 20 > len(data):
            continue
        kind, _unused, var_bytes = struct.unpack_from("<3I", data, base + 4)
        if kind != 0x0A000109 or not var_bytes or var_bytes % OVA_INFO_SIZE:
            continue
        count = var_bytes // OVA_INFO_SIZE
        if count > 4096:
            continue
        records_base = base + 20
        names_base = records_base + var_bytes
        names_span = count * AI_MAX_LEN_VAR
        container_end = len(data)
        if base >= 4:
            entry_size = struct.unpack_from("<I", data, base - 4)[0]
            if base + 8 + entry_size <= len(data):
                container_end = base + 8 + entry_size
        detected = _detect_pop_name_span(data, names_base, count, container_end, names_span)
        valid = 0
        if detected == names_span:
            for i in range(count):
                raw = data[names_base + i * AI_MAX_LEN_VAR:names_base + (i + 1) * AI_MAX_LEN_VAR]
                name = raw.split(b"\0", 1)[0].decode("ascii", errors="ignore")
                valid += int(bool(_valid_identifier(name)))
        names_available = detected == names_span
        names_end = names_base + detected
        init_base = None
        init_size = None
        candidates.append(OvaStructure(base, count, names_base, detected, valid,
                                       not names_available, init_base, init_size,
                                       source_format="POP37/38",
                                       records_base=records_base,
                                       names_available=names_available,
                                       names_encrypted=detected > 0 and valid == 0,
                                       container_end=container_end,
                                       name_slots=(detected + 29) // 30 if detected else 0))
    return candidates


def find_ova_structures(data: bytes) -> list[OvaStructure]:
    candidates = _find_pop_ova_structures(data)
    for base in range(0, max(0, len(data) - 8)):
        size_r = struct.unpack_from("<I", data, base)[0]
        if not size_r or size_r % OVA_INFO_SIZE:
            continue
        count = size_r // OVA_INFO_SIZE
        if count < 1 or count > 4096 or base + 4 + size_r + 4 > len(data):
            continue
        names_size = struct.unpack_from("<I", data, base + 4 + size_r)[0]
        if names_size != count * AI_MAX_LEN_VAR:
            continue
        names_base = base + 8 + size_r
        available = len(data) - names_base
        valid = 0
        for i in range(min(count, max(0, (available + 29) // 30))):
            raw = data[names_base + i * 30:min(names_base + (i + 1) * 30, len(data))]
            valid += int(bool(_valid_identifier(raw.split(b"\0", 1)[0].decode("ascii", errors="ignore"))))
        if valid < max(1, min(count, 2)):
            continue
        names_available = available >= names_size and valid == count
        init_base, init_size = _find_init_buffer_after_names(data, names_base + names_size)
        candidates.append(OvaStructure(base, count, names_base, names_size, valid,
                                       not names_available, init_base, init_size,
                                       names_available=names_available,
                                       names_encrypted=(available >= names_size and valid == 0)))
    return candidates


def find_variables(data: bytes) -> list[OvaVariable]:
    structures = find_ova_structures(data)
    if not structures:
        return []
    structure = max(structures, key=lambda s: (s.source_format == "POP37/38", s.complete_names, -s.base))
    if structure.source_format == "POP37/38" and structure.init_base is None:
        structure.init_base, structure.init_size = _recover_pop_init_buffer(data, structure)
    result = []
    for i in range(structure.count):
        if structure.source_format == "POP37/38":
            info = structure.records_base + i * OVA_INFO_SIZE
            _num_elem, packed, var_offset = struct.unpack_from("<III", data, info)
            var_type, flags = packed & 0xFFFF, (packed >> 16) & 0xFFFF
            if structure.names_available:
                slot = structure.names_base + i * 30
                raw = data[slot:min(slot + 30, len(data))]
                name = raw.split(b"\0", 1)[0].decode("ascii", errors="ignore")
            else:
                slot = info
                name = f"OVA_{i + 1:03d}"
        else:
            slot = structure.names_base + i * 30
            raw = data[slot:min(slot + 30, len(data))]
            name = raw.split(b"\0", 1)[0].decode("ascii", errors="ignore")
            info = structure.base + 4 + i * OVA_INFO_SIZE
            var_offset, _num_elem, var_type, flags = struct.unpack_from("<iihh", data, info)
            if not name or not _valid_identifier(name):
                name = f"OVA_{i + 1:03d}"
        if name:
            value_absolute = None
            value_size = None
            if structure.init_base is not None and structure.init_size is not None:
                # POP37/38 stores VarInfo offsets relative to the initial
                # value size field.  The reference OVA editor therefore
                # effectively has a -4 serialization bias: the byte at the
                # first value offset is four bytes after the stored anchor.
                # Resolve it here so the UI always points at the actual value
                # byte (e.g. mb_CheatsEnabled at 0x580).
                if structure.source_format == "POP37/38":
                    candidate = structure.init_base - 4 + var_offset
                else:
                    candidate = structure.init_base + var_offset
                relative = candidate - structure.init_base
                if 0 <= relative < structure.init_size:
                    value_absolute = candidate
                    elem_count = max(1, _num_elem & 0x3FFFFFFF)
                    dimensions = (_num_elem >> 30) & 0x3
                    value_size = min(
                        _pop_type_storage_size(var_type) * elem_count + dimensions * 4,
                        structure.init_size - relative,
                    )
            suffix = ""
            if not structure.names_available:
                suffix = " (editor names unavailable)" if not structure.names_encrypted else " (editor names encrypted/unavailable)"
            result.append(OvaVariable(name, slot, f"{structure.source_format} OVA @ 0x{structure.base:08X}{suffix}",
                                      var_offset, var_type, flags, structure.base, value_absolute,
                                      value_size, _num_elem))
    return result


def _find_ascii_fallback(data: bytes) -> list[OvaVariable]:
    found = []
    for match in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]{2,}", data):
        name = match.group().decode("ascii", errors="ignore")
        if _valid_identifier(name) and name.lower() not in {"ova", "ofc"}:
            found.append(OvaVariable(name, match.start(), "ASCII fallback"))
    return found


def ova_diagnostic_report(data: bytes, label: str = "buffer") -> list[str]:
    structures = find_ova_structures(data)
    markers = [m.start() for m in re.finditer(b"ova", data)]
    lines = [f"[ANALYSIS] {label}: {len(data):,} B | ASCII 'ova' markers: {len(markers)} | OVA descriptors: {len(structures)}"]
    if markers:
        lines.append("  'ova' marker at " + ", ".join(f"0x{x:08X}" for x in markers[:8]))
    if not structures:
        lines.append(f"  NO STRUCTURAL DESCRIPTOR: ASCII fallback found {len(_find_ascii_fallback(data))} strings (not used as OVA).")
        return lines
    for s in structures:
        state = "plaintext names" if s.names_available else ("encrypted/transformed names" if s.names_encrypted else "name table not included")
        records = f" records @ 0x{s.records_base:08X}" if s.records_base is not None else ""
        lines.append(f"  OK {s.source_format} @ 0x{s.base:08X}:{records} {s.count} records of 12 B | names @ 0x{s.names_base:08X} ({s.names_size} B; {state}; valid {s.complete_names}/{s.count})")
    variables = find_variables(data)
    lines.append(f"  RESULT: {len(variables)} variables displayed.")
    return lines

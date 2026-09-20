"""Bounds-checked readers for the POP FileEntry resource stream."""
from __future__ import annotations

import struct
from .models import PopFileEntry


class _PopReader:
    """Small bounds-checked reader used by the standalone mesh parser."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _need(self, size: int) -> None:
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError(f"Truncated mesh data at 0x{self.pos:X} ({size} B requested).")

    def u32(self) -> int:
        self._need(4)
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i32(self) -> int:
        self._need(4)
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def u16(self) -> int:
        self._need(2)
        value = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def i16(self) -> int:
        self._need(2)
        value = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return value

    def f32(self) -> float:
        self._need(4)
        value = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return value

    def bytes(self, size: int) -> bytes:
        self._need(size)
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def u32s(self, count: int) -> list[int]:
        if count < 0 or count > 2_000_000:
            raise ValueError(f"Implausible uint32 count: {count}")
        return [self.u32() for _ in range(count)]

    def i16s(self, count: int) -> list[int]:
        if count < 0 or count > 6_000_000:
            raise ValueError(f"Implausible int16 count: {count}")
        return [self.i16() for _ in range(count)]

    def f32s(self, count: int) -> list[float]:
        if count < 0 or count > 6_000_000:
            raise ValueError(f"Implausible float count: {count}")
        return [self.f32() for _ in range(count)]


def _pop_hex(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise ValueError("POP hash extends beyond the end of the entry.")
    return struct.unpack_from("<I", data, offset)[0]


def _parse_pop_file_entries(data: bytes) -> list[PopFileEntry]:
    """Parse the FileEntry stream used by bin_repacker_2018_05_29_0806."""
    entries: list[PopFileEntry] = []
    pos = 0
    while pos + 12 <= len(data):
        size, magic, key = struct.unpack_from("<III", data, pos)
        data_offset = pos + 12
        end = data_offset + size
        if end > len(data):
            raise ValueError(
                f"FileEntry #{len(entries)} extends beyond the end of the BIN: "
                f"offset=0x{pos:X}, size={size:,}, file={len(data):,}."
            )
        data_type = struct.unpack_from("<I", data, data_offset)[0] if size >= 4 else None
        entries.append(PopFileEntry(len(entries), pos, size, magic, key, data_offset, data_type))
        pos = end
    if pos != len(data):
        raise ValueError(f"Misaligned FileEntry table: parsing stopped at 0x{pos:X} of 0x{len(data):X}.")
    return entries


def _split_pop_footer(data: bytes) -> tuple[bytes, bytes]:
    """Dependencies precede the runtime end marker (ObjectPlacer.cpp)."""
    entries = _parse_pop_file_entries(data)
    if entries and entries[-1].key == 0x0FF7C0DE:
        return data[:entries[-1].offset], data[entries[-1].offset:]
    return data, b""

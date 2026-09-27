"""Read Jade EVE/TRL animation tracks from POP FileEntry streams.

The layout follows EVE_ul_CallbackLoadListTracks, EVE_LoadListEvents and
EVE_Event_InterpolationKey_Load in the Jade engine sources. PoP's 0x1010
rotation keys store four signed 16-bit quaternion components.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from .resources import _parse_pop_file_entries


@dataclass(frozen=True)
class AnimationKey:
    frame: int
    translation: tuple[float, float, float] | None = None
    rotation: tuple[float, float, float, float] | None = None
    no_interpolation: bool = False


@dataclass(frozen=True)
class AnimationTrack:
    gizmo: int
    keys: tuple[AnimationKey, ...]


@dataclass(frozen=True)
class AnimationClip:
    key: int
    tracks: tuple[AnimationTrack, ...]
    frames: int
    entry_index: int


def _read(fmt: str, data: bytes, pos: int):
    size = struct.calcsize(fmt)
    if pos + size > len(data):
        raise ValueError("Truncated TRL track")
    values = struct.unpack_from(fmt, data, pos)
    return (values[0] if len(values) == 1 else values), pos + size


def _packed_quaternion(word: int) -> tuple[float, float, float, float]:
    """Retail Jade 0x4010: three 10-bit components and the omitted axis."""
    omitted = word >> 30
    scale = 1.0 / (511.0 * math.sqrt(2.0))
    components = [(((word >> (10 * i)) & 0x3FF) - 512) * scale for i in range(3)]
    missing = math.sqrt(max(0.0, 1.0 - sum(value * value for value in components)))
    components.insert(omitted, missing)
    length = math.sqrt(sum(value * value for value in components))
    return tuple(value / length for value in components)


def _parse_trl(data: bytes, key: int, entry_index: int) -> AnimationClip:
    count, pos = _read("<H", data, 0)
    _flags, pos = _read("<H", data, pos)
    if not 0 < count <= 256:
        raise ValueError("Invalid TRL track count")
    tracks = []
    total_frames = 0
    for _ in range(count):
        flags, pos = _read("<H", data, pos)
        gizmo, pos = _read("<H", data, pos)
        data_length, pos = _read("<I", data, pos)
        if not flags & 0x8000 or data_length > len(data):
            raise ValueError("Unsupported TRL track")
        if not flags & 0x3F00:
            _track_type, pos = _read("<I", data, pos)
        events, pos = _read("<I", data, pos)
        if events > 100_000:
            raise ValueError("Invalid TRL event count")
        keys = []
        frame = 0
        first_flags = first_size = first_type = None
        for event_index in range(events):
            span, pos = _read("<B" if flags & 0x0200 else "<H", data, pos)
            duration = span & 0x7FFF
            event_flags, pos = _read("<H", data, pos) if event_index == 0 or not flags & 0x0800 else (first_flags & ~0x0100, pos)
            if first_flags is None:
                first_flags = event_flags
            if event_flags & 0x07C0 != 0x0080 or event_flags & 0x0020:
                raise ValueError("Unsupported TRL event type")
            size, pos = _read("<H", data, pos) if event_index == 0 or not flags & 0x2000 else (first_size, pos)
            kind, pos = _read("<H", data, pos) if event_index == 0 or not flags & 0x1000 else (first_type, pos)
            if first_size is None:
                first_size, first_type = size, kind
            if size < 4 or size > 4096:
                raise ValueError("Invalid interpolation key size")
            start = pos
            translation = rotation = None
            available = size - 4
            if kind & 3 and available >= 12:
                translation, pos = _read("<3f", data, pos)
                for _ in range((kind & 3) - 1):
                    _, pos = _read("<3f", data, pos)
            elif kind == 0x4010 and available == 4:
                word, pos = _read("<I", data, pos)
                rotation = _packed_quaternion(word)
            elif kind == 0x1010 and available == 8:
                # Retail PoP stores x/y/z/w as signed normalized 16-bit values.
                values, pos = _read("<4h", data, pos)
                rotation = tuple(value / 32767.0 for value in values)
                length = math.sqrt(sum(value * value for value in rotation))
                rotation = tuple(value / length for value in rotation) if length else (0, 0, 0, 1)
            elif kind & 0x10 and available >= (6 if kind & 0x80 else 16):
                if kind & 0x80:
                    xyz, pos = _read("<3h", data, pos)
                    x, y, z = (v / 32767.0 for v in xyz)
                    rotation = (x, y, z, math.sqrt(max(0.0, 1.0 - x*x - y*y - z*z)))
                else:
                    rotation, pos = _read("<4f", data, pos)
            elif kind & 0x04:
                pass  # Matrix keys are retained as timing only.
            if kind & 0x40:
                # Next-value interpolation is encoded after the current value.
                next_bytes = (12 if kind & 3 else 0) + (16 if kind & 0x10 else 0)
                pos += next_bytes
            if kind & 0x08:
                pos += 4
            expected = size - 4
            if pos - start > expected:
                raise ValueError("Invalid TRL key payload")
            pos = start + expected
            if pos > len(data):
                raise ValueError("Truncated TRL key")
            if translation is not None or rotation is not None:
                if not all(math.isfinite(v) for v in (translation or ()) + (rotation or ())):
                    raise ValueError("Nonfinite TRL key")
                keys.append(AnimationKey(frame, translation, rotation,
                                         bool(event_flags & 0x0800)))
            frame += duration
        if keys:
            tracks.append(AnimationTrack(gizmo, tuple(keys)))
            total_frames = max(total_frames, frame)
    if not tracks:
        raise ValueError("TRL contains no playable transform tracks")
    return AnimationClip(key, tuple(tracks), max(1, total_frames), entry_index)


def scan_animations(data: bytes) -> list[AnimationClip]:
    clips = []
    for entry in _parse_pop_file_entries(data):
        kind = entry.data_type or 0
        if kind >> 16 != 2 or kind & 0xff in (0x24, 0x30):
            continue
        # The GRO type is the first dword of the TRL header: low word is
        # track count, high word is the list flags.
        payload = data[entry.data_offset:entry.data_offset + entry.size]
        try:
            clips.append(_parse_trl(payload, entry.key, entry.index))
        except (ValueError, struct.error):
            continue
    return clips

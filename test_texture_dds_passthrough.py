"""Regression for importing native DXT1 skyboxes without recompression."""
from pathlib import Path
import struct
import tempfile
import unittest

from bf_lab.archives import _patch_texture_key_in_asset
from bf_lab.resources import _parse_pop_file_entries
from bf_lab.textures import (
    _build_dds_header,
    _dds_blob_for_dump,
    _scan_pop_textures,
    _texture_replacement_from_file,
)


KEY = 0x1F01B53B


def _texture_record(width: int, height: int, pixels: bytes | None) -> bytes:
    header = bytearray(56)
    struct.pack_into("<I", header, 4, 0xFFFFFFFF)
    struct.pack_into("<HH", header, 12, width, height)
    struct.pack_into("<I", header, 24, 0xCAD01234)
    struct.pack_into("<I", header, 32, 0xC0DEC0DE)
    struct.pack_into("<I", header, 36, 3)
    struct.pack_into("<III", header, 40, 5, width, height)
    raw = bytes(header) + bytes(4)
    if pixels is not None:
        raw += pixels + bytes(4)  # Native Jade BC1 trailer.
    return struct.pack("<III", len(raw), 0, KEY) + raw


class Dxt1SkyboxImportTest(unittest.TestCase):
    def test_resized_dds_keeps_blocks_and_native_trailer(self):
        original = _texture_record(4, 4, None) + _texture_record(4, 4, bytes(8))
        texture, = _scan_pop_textures(original)
        blocks = bytes(range(32))
        dds = _build_dds_header(8, 8, 0, 5) + blocks
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "skybox.dds"
            source.write_bytes(dds)
            pixels, texture_type, width, height = _texture_replacement_from_file(
                source, texture, original, keep_dimensions=True
            )
        self.assertEqual(pixels, blocks)
        patched, count = _patch_texture_key_in_asset(
            original, KEY, pixels, texture.texture_type, texture_type,
            texture.width, texture.height, replacement_dimensions=(width, height),
        )
        self.assertEqual(count, 2)
        self.assertEqual([entry.size for entry in _parse_pop_file_entries(patched)], [60, 96])
        replacement, = _scan_pop_textures(patched)
        self.assertEqual(_dds_blob_for_dump(patched, replacement), dds)
        self.assertEqual((replacement.width, replacement.height), (8, 8))
        self.assertEqual(patched[replacement.data_end - 4:replacement.data_end], bytes(4))


if __name__ == "__main__":
    unittest.main()

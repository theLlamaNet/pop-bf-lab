import struct
import unittest

from bf_lab.textures import _decode_pop_texture_image, _scan_pop_textures


class DemoTextureDecodeTests(unittest.TestCase):
    def test_demo_type_zero_header_includes_first_two_pixel_bytes(self):
        key = 0x2E004F71
        payload = bytearray(54)
        struct.pack_into("<I", payload, 0, key)
        struct.pack_into("<I", payload, 4, 0xFFFFFFFF)
        struct.pack_into("<hh", payload, 12, 32, 32)
        struct.pack_into("<I", payload, 24, 0xCAD01234)
        struct.pack_into("<I", payload, 32, 0xC0DEC0DE)
        struct.pack_into("<I", payload, 36, 0x20000)
        struct.pack_into("<I", payload, 40, 0)
        struct.pack_into("<II", payload, 44, 32, 32)
        payload.extend(bytes((0, 0, 255, 255)) * (32 * 32))
        data = struct.pack("<III", len(payload), 0, key) + payload

        texture, = _scan_pop_textures(data)
        self.assertEqual(texture.data_offset, 12 + 54)
        self.assertEqual(texture.format, "TGA/BGRA8")
        self.assertEqual(_decode_pop_texture_image(data, texture).getpixel((0, 0)), (255, 0, 0, 255))

    def test_dxt1_payload_starts_immediately_after_header(self):
        key = 0x1300583F
        payload = bytearray(56)
        struct.pack_into("<I", payload, 0, key)
        struct.pack_into("<I", payload, 4, 0xFFFFFFFF)
        struct.pack_into("<hh", payload, 12, 4, 4)
        struct.pack_into("<I", payload, 24, 0xCAD01234)
        struct.pack_into("<I", payload, 32, 0xC0DEC0DE)
        struct.pack_into("<I", payload, 40, 5)
        struct.pack_into("<II", payload, 44, 4, 4)
        # One BC1 block: red and black endpoints, every pixel selects red.
        payload.extend(struct.pack("<HHI", 0xF800, 0, 0))
        data = struct.pack("<III", len(payload), 0, key) + payload

        texture, = _scan_pop_textures(data)
        self.assertEqual(texture.data_offset, 12 + 56)
        self.assertEqual(texture.format, "DXT1")
        self.assertEqual(_decode_pop_texture_image(data, texture).getpixel((0, 0))[:3], (255, 0, 0))

    def test_retail_dxt1_skips_four_byte_field(self):
        key = 0x13005840
        payload = bytearray(56)
        struct.pack_into("<I", payload, 0, key)
        struct.pack_into("<I", payload, 4, 0xFFFFFFFF)
        struct.pack_into("<hh", payload, 12, 4, 4)
        struct.pack_into("<I", payload, 24, 0xCAD01234)
        struct.pack_into("<I", payload, 32, 0xC0DEC0DE)
        struct.pack_into("<I", payload, 36, 3)
        struct.pack_into("<I", payload, 40, 5)
        struct.pack_into("<II", payload, 44, 4, 4)
        payload.extend(b"\x00\x00\x00\xc0")
        payload.extend(struct.pack("<HHI", 0xF800, 0, 0))
        data = struct.pack("<III", len(payload), 0, key) + payload

        texture, = _scan_pop_textures(data)
        self.assertEqual(texture.data_offset, 12 + 60)
        self.assertEqual(_decode_pop_texture_image(data, texture).getpixel((0, 0))[:3], (255, 0, 0))


if __name__ == "__main__":
    unittest.main()

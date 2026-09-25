import struct
import unittest

from bf_lab.mesh_material_import import _set_material_alpha_mode
from bf_lab.textures import _encode_dxt5, _build_dds_header


class MeshMaterialAlphaTests(unittest.TestCase):
    def test_cutout_and_blend_use_distinct_jade_render_modes(self):
        for mode, expected_blend, expected_test in (
            ("opaque", 0, False), ("cutout", 0, True), ("blend", 1, False),
        ):
            material = bytearray(64)
            struct.pack_into("<I", material, 4, 4)
            struct.pack_into("<I", material, 28, (5 << 16) | (1 << 4) | (1 << 9) | (1 << 10))
            _set_material_alpha_mode(material, mode)
            flags = struct.unpack_from("<I", material, 28)[0]
            self.assertEqual((flags >> 16) & 0xF, expected_blend)
            self.assertEqual(bool(flags & (1 << 4)), expected_test)
            self.assertFalse(flags & ((1 << 9) | (1 << 10)))

    def test_dxt5_keeps_png_alpha_endpoints(self):
        from PIL import Image

        image = Image.new("RGBA", (4, 4), (200, 100, 50, 255))
        for y in range(4):
            for x in range(2):
                image.putpixel((x, y), (200, 100, 50, 0))
        payload = _encode_dxt5(image, 1)
        from io import BytesIO
        decoded = Image.open(BytesIO(_build_dds_header(4, 4, 0, 7) + payload)).convert("RGBA")
        self.assertEqual(decoded.getpixel((0, 0))[3], 0)
        self.assertEqual(decoded.getpixel((3, 0))[3], 255)


if __name__ == "__main__":
    unittest.main()

import struct
import unittest

from bf_lab.mesh_material_import import _set_material_alpha_mode
from bf_lab.mesh_material_import import material_source_aliases
from bf_lab.material_stream import insert_material_resources
from bf_lab.resources import _parse_pop_file_entries
from bf_lab.models import MeshInfo
from bf_lab.project import JadeProject
from bf_lab.textures import _encode_dxt5, _build_dds_header


class MeshMaterialAlphaTests(unittest.TestCase):
    def test_duplicate_glb_appearances_share_a_native_slot(self):
        mesh = MeshInfo(0, 0, 0, 0, [], [], [], [],
                        [(0, 1), (1, 1), (2, 1), (3, 1)])
        aliases = material_source_aliases(
            mesh, {0: 100, 1: 101, 2: 100, 3: 100},
            {0: (0.8, 0.8, 0.8, 1.), 1: (1., 1., 1., 1.),
             2: (0.8, 0.8, 0.8, 1.), 3: (1., 1., 1., 1.)})
        self.assertEqual(aliases, {0: 0, 1: 1, 2: 0, 3: 3})

    def test_new_leaf_is_inserted_beside_its_pack(self):
        def record(key, payload):
            return struct.pack("<III", len(payload), 0xEEFFC099, key) + payload

        pack_key, old_key, new_key, texture_key = 0x10, 0x11, 0x12, 0x13
        pack = struct.pack("<IIIII", 4, 0, 2, old_key, new_key)
        leaf = struct.pack("<II", 5, 10) + bytes(40)
        header = bytearray(56)
        struct.pack_into("<I", header, 0, texture_key)
        struct.pack_into("<I", header, 4, 0xFFFFFFFF)
        struct.pack_into("<I", header, 24, 0xCAD01234)
        struct.pack_into("<I", header, 32, 0xC0DEC0DE)
        struct.pack_into("<I", header, 40, 7)
        body = (record(pack_key, pack) + record(old_key, leaf) +
                record(texture_key, header) + record(texture_key, header + bytes(16)))
        result = insert_material_resources(body, [(new_key, 0xEEFFC099, leaf)])
        self.assertEqual([entry.key for entry in _parse_pop_file_entries(result)],
                         [pack_key, old_key, new_key, texture_key, texture_key])

        original_pack = struct.pack("<IIII", 4, 0, 1, old_key)
        original_body = (record(pack_key, original_pack) + record(old_key, leaf) +
                         record(texture_key, header) + record(texture_key, header + bytes(16)))
        project = JadeProject()
        changed_leaf = leaf + b"\x7f"
        project.mesh_patches[0] = {0: (pack_key, original_pack, pack),
                                   1: (new_key, leaf, changed_leaf)}
        project.resource_additions[0] = {0: (new_key, 0xEEFFC099, leaf)}
        staged = project.apply_mesh_patches(0, original_body)
        self.assertEqual([entry.key for entry in _parse_pop_file_entries(staged)],
                         [pack_key, old_key, new_key, texture_key, texture_key])
        new_entry = next(entry for entry in _parse_pop_file_entries(staged)
                         if entry.key == new_key)
        self.assertEqual(staged[new_entry.data_offset:new_entry.data_offset + new_entry.size],
                         changed_leaf)

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

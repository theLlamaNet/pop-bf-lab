import struct
import unittest

from bf_lab.materials import _scan_pop_material_records


def material_entry(version, texture_words):
    key = 0x12345678
    payload = struct.pack("<I", 5)
    payload += struct.pack("<7I", version, 0, 0, 0xFFFFFFFF, 0, 0, 0)
    payload += struct.pack("<2fI", 0.0, 1.0, 0)
    payload += struct.pack("<%dI" % len(texture_words), *texture_words)
    entry = struct.pack("<III", len(payload), 0, key) + payload
    return entry, key


class DemoMaterialTests(unittest.TestCase):
    def test_version_six_finds_demo_texture_and_write_offset(self):
        texture_key = 0xAABBCCDD
        data, key = material_entry(6, [0x11111111, 0x22222222, texture_key])
        record = _scan_pop_material_records(data, {texture_key})[key]
        self.assertEqual(record.texture_key, texture_key)
        self.assertEqual(struct.unpack_from("<I", data, record.texture_offset)[0], texture_key)
        self.assertIsNone(record.secondary_key)

    def test_version_six_keeps_normal_first_texture(self):
        texture_key = 0xAABBCCDD
        data, key = material_entry(6, [texture_key, 0x11111111])
        record = _scan_pop_material_records(data, {texture_key})[key]
        self.assertEqual(record.texture_key, texture_key)
        self.assertEqual(struct.unpack_from("<I", data, record.texture_offset)[0], texture_key)


if __name__ == "__main__":
    unittest.main()

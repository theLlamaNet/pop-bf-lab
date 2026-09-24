"""Level object parsing and transform safety checks."""
import struct
import unittest

from bf_lab.level_scene import scan_level_objects, write_object_transform, wol_wow_keys


def example_gao():
    name = b"Test_Trigger.gao\0"
    payload = bytearray(b".gao" + struct.pack("<4I", 10, 8, 0x4000, len(name)) + name)
    payload.extend(b"\0" * 10)
    payload.extend(struct.pack("<16f", 1, 0, 0, 1, 0, 1, 0, 1,
                               0, 0, 1, 1, 10, 20, 30, 1))
    payload.extend(b"\xCA\xFE\xBA\xBE")
    payload.extend(struct.pack("<6f", -1, -2, -3, 1, 2, 3))
    payload.extend(struct.pack("<II", 0x1234, 0x5678))
    return struct.pack("<III", len(payload), 0xEEFFC099, 0x4242) + payload


class LevelSceneTests(unittest.TestCase):
    def test_gao_transform_preserves_other_resource_bytes(self):
        original = example_gao()
        obj, = scan_level_objects(original)
        self.assertEqual((obj.kind, obj.mesh_key, obj.position),
                         ("trigger", 0x1234, (10.0, 20.0, 30.0)))
        obj.position = (11, 20, 30)
        obj.dirty = True
        changed = write_object_transform(original, obj)
        self.assertEqual(scan_level_objects(changed)[0].position, (11.0, 20.0, 30.0))
        self.assertEqual(changed[:obj.matrix_offset + 48], original[:obj.matrix_offset + 48])
        self.assertEqual(changed[obj.matrix_offset + 60:], original[obj.matrix_offset + 60:])

    def test_wol_dependency_keys(self):
        data = b"\0" * 12 + struct.pack("<I", 0x0100ABCD) + b".wow"
        self.assertEqual(wol_wow_keys(data), {0x0100ABCD})


if __name__ == "__main__":
    unittest.main()

import struct
import unittest
from dataclasses import replace

import jade_mesh
from bf_lab.mesh_import import _mesh_rli_replacements, _rli_cooked_color
from bf_lab.mesh_parser import _scan_pop_meshes
from bf_lab.resources import _parse_pop_file_entries


def _resource(key, payload):
    return struct.pack("<III", len(payload), 0x12345678, key) + payload


def _static_mesh_with_rli():
    vertices = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    geo = struct.pack("<11I", 1, 7, 0, 0, 3, 0, 0, 3, 1, 0, 1)
    geo += b"".join(struct.pack("<3f", *v) for v in vertices + [(0.0, 0.0, 1.0)] * 3)
    geo += struct.pack("<6f", 0, 0, 1, 0, 0, 1)
    geo += struct.pack("<Ii", 1, 0)
    geo += struct.pack("<6HI", 0, 1, 2, 0, 1, 2, 1)

    gao = bytearray(4 + 16 + 10 + 68 + 24 + 8)
    gao[:4] = b".gao"
    struct.pack_into("<I", gao, 12, 0x4000)
    struct.pack_into("<II", gao, len(gao) - 8, 100, 0)
    blue_bgra = bytes((220, 30, 20, 0xFD))
    gao += b"\xff\xff" + struct.pack("<I", 3) + blue_bgra * 3 + struct.pack("<I", 1)
    gao += struct.pack("<4I", 0xABCD, 8 + 3 * 12, 3, 12)
    gao += b"".join(blue_bgra + struct.pack("<2f", -1.0, -1.0) for _ in range(3))

    data = _resource(100, geo) + _resource(400, gao)
    return data, _scan_pop_meshes(data)[0]


class MeshVertexColorTests(unittest.TestCase):
    def test_mesh_scan_attaches_primary_instance_colors_for_preview(self):
        _data, target = _static_mesh_with_rli()
        expected = (220 / 255, 30 / 255, 20 / 255, 0xFD / 255)
        self.assertEqual(target.vertex_colors, [expected] * 3)

    def test_round_tripped_point_keeps_first_duplicate_luminance(self):
        colors = [bytes((10, 20, 30, 0xFD)), bytes((200, 210, 220, 0xFF))]
        got = jade_mesh.transfer_colors(
            [(1.23456789, 2.0, 3.0), (1.23456789, 2.0, 3.0)],
            [(1.2345679, 2.0, 3.0)], colors)
        # Jade RGBA (10,20,30) -> round(.30*10 + .59*20 + .11*30) = 18.
        self.assertEqual(got, [bytes((18, 18, 18, 0xFD))])

    def test_new_point_interpolates_light_level_without_color_cast(self):
        colors = [bytes((220, 30, 20, 0xFD)), bytes((180, 20, 10, 0xFE))]
        got = jade_mesh.transfer_colors(
            [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)], colors)
        self.assertEqual(got, [bytes((76, 76, 76, 0xFD))])

    def test_manual_tint_and_intensity_preserve_transferred_light_level(self):
        colors = [bytes((100, 100, 100, 0xFD))]
        got = jade_mesh.transfer_colors(
            [(0.0, 0.0, 0.0)], [(0.0, 0.0, 0.0)], colors,
            tint=(1.0, 0.5, 0.25), intensity=1.2)
        self.assertEqual(got, [bytes((120, 60, 30, 0xFD))])

    def test_cooked_rli_swaps_jade_red_and_blue_for_direct3d(self):
        self.assertEqual(_rli_cooked_color(bytes((10, 20, 30, 0xFE))),
                         bytes((30, 20, 10, 0xFE)))

    def test_static_rli_primary_and_expanded_buffers_are_achromatic(self):
        data, target = _static_mesh_with_rli()
        moved = replace(target, vertices=[(x + 0.01, y, z) for x, y, z in target.vertices])
        updates = _mesh_rli_replacements(data, target, moved)
        self.assertEqual(len(updates), 1)
        rebuilt = next(iter(updates.values()))
        primary = rebuilt.index(b"\xff\xff" + struct.pack("<I", 3)) + 6
        # Source Jade RGBA (220,30,20) has luminance 86, not unsafe white 255.
        self.assertEqual(rebuilt[primary:primary + 12], bytes((86, 86, 86, 0xFE)) * 3)
        extra_header = rebuilt.index(struct.pack("<4I", 0xABCD, 44, 3, 12))
        for index in range(3):
            offset = extra_header + 16 + index * 12
            self.assertEqual(rebuilt[offset:offset + 4], bytes((86, 86, 86, 0xFE)))

        # The rebuilt resource remains a valid GAO entry in its BIN stream.
        entries = _parse_pop_file_entries(_resource(400, rebuilt))
        self.assertEqual(entries[0].size, len(rebuilt))

    def test_static_rli_writes_manual_tint_in_both_jade_color_orders(self):
        data, target = _static_mesh_with_rli()
        updates = _mesh_rli_replacements(
            data, target, target, (1.0, 0.5, 0.25), 1.5)
        rebuilt = next(iter(updates.values()))
        primary = rebuilt.index(b"\xff\xff" + struct.pack("<I", 3)) + 6
        jade_rgba = bytes((129, 64, 32, 0xFE))
        self.assertEqual(rebuilt[primary:primary + 12], jade_rgba * 3)
        extra_header = rebuilt.index(struct.pack("<4I", 0xABCD, 44, 3, 12))
        for index in range(3):
            offset = extra_header + 16 + index * 12
            self.assertEqual(rebuilt[offset:offset + 4], bytes((32, 64, 129, 0xFE)))

        # A fresh BF/BIN scan must expose the saved primary RLI to OpenGL.
        entries = _parse_pop_file_entries(data)
        resource = entries[1]
        rescanned_data = (
            data[:resource.offset]
            + struct.pack("<III", len(rebuilt), resource.magic, resource.key)
            + rebuilt
            + data[resource.data_offset + resource.size:]
        )
        rescanned = _scan_pop_meshes(rescanned_data)[0]
        expected = (129 / 255, 64 / 255, 32 / 255, 0xFE / 255)
        self.assertEqual(rescanned.vertex_colors, [expected] * 3)

    def test_geo_point_colors_use_the_same_achromatic_transfer(self):
        data, target = _static_mesh_with_rli()
        entries = _parse_pop_file_entries(data)
        raw = data[entries[0].data_offset:entries[0].data_offset + entries[0].size]
        layout = jade_mesh.read_layout(raw)
        colored = bytearray(raw)
        # Enable three GEO dul_PointColors and insert them before the UV table.
        struct.pack_into("<III", colored, 20, 3, 1, 3)
        source = bytes((220, 30, 20, 0xFD)) * 3
        colored[layout.color_offset:layout.color_offset] = source
        colored = bytes(colored)
        colored_target = _scan_pop_meshes(_resource(100, colored))[0]
        expected_source = (220 / 255, 30 / 255, 20 / 255, 0xFD / 255)
        self.assertEqual(colored_target.vertex_colors, [expected_source] * 3)
        moved = replace(colored_target,
                        vertices=[(x + 0.01, y, z) for x, y, z in colored_target.vertices])
        rebuilt = jade_mesh.build_replacement(colored, colored_target, moved)
        rebuilt_layout = jade_mesh.read_layout(rebuilt)
        start = rebuilt_layout.color_offset
        self.assertEqual(rebuilt[start:start + 12], bytes((86, 86, 86, 0xFD)) * 3)


if __name__ == "__main__":
    unittest.main()

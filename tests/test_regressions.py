"""Regression checks: bundled Python -B -m unittest discover -s tests -v."""
from pathlib import Path
import io
import struct
import tempfile
import unittest
from unittest.mock import patch

import bf_lab
from PIL import Image
from bf_lab.archives import (
    _repack_legacy_bigfile, _repack_legacy_bigfile_changes,
    _patch_texture_key_in_asset, _collect_texture_key_replacements,
    read_bigfile, read_bigfile_entry,
)
from bf_lab.textures import (
    _scan_pop_textures, _texture_replacement_from_file, _decode_pop_texture_image,
    _encode_dxt1, _encode_dxt5,
)


def texture_record(kind=7, width=8, height=8, key=123, bgr24=False):
    image = Image.new("RGBA", (width, height), (32, 96, 160, 255))
    if kind == 7:
        payload = _encode_dxt5(image, 1)
    elif kind == 5:
        payload = _encode_dxt1(image)
    elif kind == 1:
        payload = bytes(width * height)
    else:
        payload = image.convert("RGB").tobytes("raw", "BGR") if bgr24 else image.tobytes("raw", "BGRA")
    header = bytearray(56)
    struct.pack_into("<I", header, 4, 0xFFFFFFFF)
    struct.pack_into("<hh", header, 12, width, height)
    struct.pack_into("<I", header, 24, 0xCAD01234)
    struct.pack_into("<I", header, 32, 0xC0DEC0DE)
    struct.pack_into("<I", header, 40, kind)
    struct.pack_into("<II", header, 44, width, height)
    prefix = b"ABCD" if kind in (1, 5) else b""
    body = bytes(header) + prefix + payload
    return struct.pack("<III", len(body), 0x99C0FFEE, key) + body


def write_bf(path, payloads, names=None, order=None, version=37):
    count = len(payloads)
    names = names or [f"asset{i}.bin" for i in range(count)]
    order = order or list(reversed(range(count)))
    data = bytearray(68 + count * 92)
    struct.pack_into("<4s10I", data, 0, b"BIG\0", version, count, 0, 0, 0, 0, 0, count, 1, 0)
    struct.pack_into("<6I", data, 44, count, 0, 68, 0xFFFFFFFF, 0, count - 1)
    for index in order:
        data.extend(b"GAP!")
        struct.pack_into("<II", data, 68 + index * 8, len(data), index + 100)
        meta = 68 + count * 8 + index * 84
        struct.pack_into("<I", data, meta, len(payloads[index]))
        struct.pack_into("<I", data, meta + 12, 5)
        name = names[index].encode("ascii")
        data[meta + 20:meta + 20 + len(name)] = name
        data.extend(struct.pack("<I", len(payloads[index])))
        data.extend(payloads[index])
    data.extend(b"TRAILER!")
    path.write_bytes(data)


class ArchiveTests(unittest.TestCase):
    def test_single_rebuild_growing_shrinking_and_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for version in (37, 38):
                source, target = root / "source.bf", root / "target.bf"
                original = b"original-payload" * 4
                write_bf(source, [original, b"untouched" * 8], version=version)
                selected = read_bigfile(source).entries[0]
                for payload in (original, b"bigger" * 100, b"tiny"):
                    with self.subTest(version=version, size=len(payload)):
                        _repack_legacy_bigfile(source, selected, payload, target)
                        entries = read_bigfile(target).entries
                        self.assertEqual(read_bigfile_entry(target, entries[0]), payload)
                        self.assertEqual(read_bigfile_entry(target, entries[1]), b"untouched" * 8)
                        self.assertEqual(target.read_bytes().count(b"GAP!"), 2)
                        self.assertTrue(target.read_bytes().endswith(b"TRAILER!"))
                        if payload == original:
                            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_single_rebuild_updates_size_grs(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "source.bf", Path(folder) / "target.bf"
            write_bf(source, [b"a" * 32, struct.pack("<4I", 100, 32, 0, 0)], ["asset.bin", "size.grs"])
            _repack_legacy_bigfile(source, read_bigfile(source).entries[0], b"b" * 100, target)
            size_data = read_bigfile_entry(target, read_bigfile(target).entries[1])
            self.assertEqual(struct.unpack_from("<I", size_data, 4)[0], 100)

    def test_resized_texture_propagates_to_bf_copies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target, image_path = root / "source.bf", root / "target.bf", root / "large.png"
            Image.new("RGBA", (20, 12), "red").save(image_path)
            for kind in (0, 1, 5, 7):
                with self.subTest(kind=kind):
                    data = texture_record(kind)
                    write_bf(source, [data, data, b"unrelated"])
                    selected = read_bigfile(source).entries[0]
                    tex = _scan_pop_textures(data)[0]
                    payload, target_type, w, h = _texture_replacement_from_file(image_path, tex, data, True)
                    modified, count = _patch_texture_key_in_asset(data, tex.key, payload, kind, target_type, 8, 8, (w, h))
                    self.assertEqual(count, 1)
                    replacements, touched = _collect_texture_key_replacements(
                        source, selected, tex.key, payload, kind, target_type, w, h, modified, (8, 8),
                    )
                    self.assertEqual(set(replacements), {0, 1})
                    _repack_legacy_bigfile_changes(source, replacements, target)
                    entries = read_bigfile(target).entries
                    for entry in entries[:2]:
                        decoded = read_bigfile_entry(target, entry)
                        result = _scan_pop_textures(decoded)[0]
                        self.assertEqual((result.width, result.height), (20, 12))
                        self.assertEqual(_decode_pop_texture_image(decoded, result).size, (20, 12))
                    self.assertEqual(read_bigfile_entry(target, entries[2]), b"unrelated")


class TextureTests(unittest.TestCase):
    def test_dimensions_formats_headers_and_neighbor_integrity(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.png"
            neighbor = texture_record(0, key=456)
            for kind, bgr24 in ((0, False), (0, True), (1, False), (5, False), (7, False)):
                for size in ((20, 12), (5, 7), (8, 8)):
                    Image.new("RGBA", size, "orange").save(source)
                    for keep in (False, True):
                        with self.subTest(kind=kind, bgr24=bgr24, size=size, keep=keep):
                            data = texture_record(kind, bgr24=bgr24) + neighbor
                            tex = _scan_pop_textures(data)[0]
                            payload, target_type, w, h = _texture_replacement_from_file(source, tex, data, keep)
                            expected = size if keep else (8, 8)
                            self.assertEqual((w, h), expected)
                            modified, count = _patch_texture_key_in_asset(data, tex.key, payload, kind, target_type, 8, 8, (w, h))
                            self.assertEqual(count, 1)
                            textures = _scan_pop_textures(modified)
                            result = textures[0]
                            self.assertEqual((result.width, result.height), expected)
                            self.assertEqual(struct.unpack_from("<hh", modified, result.offset + 12), expected)
                            self.assertEqual(struct.unpack_from("<II", modified, result.offset + 44), expected)
                            self.assertEqual(_decode_pop_texture_image(modified, result).size, expected)
                            self.assertTrue(modified.endswith(neighbor))
                            if kind == 5:
                                self.assertEqual(modified[result.data_offset - 4:result.data_offset], b"ABCD")

    def test_dimensions_exceeding_supported_header_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "wide.png"
            Image.new("RGBA", (8193, 1), "red").save(source)
            data = texture_record()
            with self.assertRaisesRegex(ValueError, "8192"):
                _texture_replacement_from_file(source, _scan_pop_textures(data)[0], data, True)

    def test_resizing_does_not_keep_old_native_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "new.png"
            Image.new("RGBA", (20, 12), "red").save(source)
            data = bytearray(texture_record(0))
            data.extend(b"NATIVE-TAIL")
            struct.pack_into("<I", data, 0, len(data) - 12)
            tex = _scan_pop_textures(data)[0]
            old, *_ = _texture_replacement_from_file(source, tex, data, False)
            new, *_ = _texture_replacement_from_file(source, tex, data, True)
            self.assertTrue(old.endswith(b"NATIVE-TAIL"))
            self.assertEqual(len(new), 20 * 12 * 4)


class InterfaceTests(unittest.TestCase):
    def test_import_toggle_apply_transform_and_repeat(self):
        from bf_lab.ui.app import JadeToolkit
        app = JadeToolkit()
        app.withdraw()
        try:
            with tempfile.TemporaryDirectory() as folder, patch("bf_lab.ui.texture.messagebox.showerror") as errors:
                source = Path(folder) / "larger.png"
                Image.new("RGBA", (20, 12), "red").save(source)
                data = texture_record(7)
                app._texture_data = bytearray(data)
                app._texture_original = data
                app._texture_infos = _scan_pop_textures(data)
                app.texture_tree.insert("", "end", iid="tex_0")
                app.texture_tree.selection_set("tex_0")
                app.update()
                with patch("bf_lab.ui.texture.filedialog.askopenfilename", return_value=str(source)):
                    app.import_texture_replacement()
                self.assertEqual(app._texture_replacement[3:], (8, 8))
                app.keep_imported_texture_dimensions.set(True)
                app._texture_dimensions_changed()
                self.assertEqual(app._texture_replacement[3:], (20, 12))
                app.rotate_texture()
                app.flip_texture("x")
                app.apply_texture_replacement()
                self.assertTrue(app._texture_dirty)
                self.assertEqual(app.texture_tree.item("tex_0", "values")[-1], "20 x 12")
                self.assertEqual((app._texture_selected().width, app._texture_selected().height), (20, 12))
                Image.new("RGBA", (12, 16), "blue").save(source)
                with patch("bf_lab.ui.texture.filedialog.askopenfilename", return_value=str(source)):
                    app.import_texture_replacement()
                app.apply_texture_replacement()
                self.assertEqual(app._texture_patch_source_dimensions, (8, 8))
                self.assertEqual((app._texture_selected().width, app._texture_selected().height), (12, 16))
                self.assertEqual(errors.call_count, 0, errors.call_args_list)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()

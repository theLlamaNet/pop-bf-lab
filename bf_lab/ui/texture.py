"""Texture Editor controls, image preview and replacement actions."""
from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import io
import struct
import tkinter as tk
from ..archives import (
    _collect_texture_key_replacements,
    _patch_texture_key_in_asset,
    _repack_legacy_bigfile_changes,
)
from ..models import TextureInfo
from ..resources import _parse_pop_file_entries
from ..textures import (
    _build_4bit_tga,
    _build_dds,
    _build_dds_header,
    _build_palette_tga,
    _build_tga_header,
    _build_type7_dds,
    _compressed_dds_for_preview,
    _dds_blob_for_dump,
    _dds_payload_from_file,
    _dxt1_payload_from_file,
    _dxt5_payload_for_converted_texture,
    _encode_dxt1,
    _encode_dxt5,
    _infer_dxt5_mip_count,
    _palette_payload_from_file,
    _palette_payload_from_image,
    _scan_pop_textures,
    _tga_payload_from_file,
    _transform_texture_image,
)


class TextureEditorMixin:
    def _build_texture_tab(self) -> None:
        top = ttk.Frame(self.texture_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Texture Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        # Widgets packed on the right are displayed in reverse packing order.
        # Pack from right to left: Scan, Dump, Import, then Apply on the far left.
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_textures).pack(side="right")
        ttk.Button(top, text="Dump texture", command=self.dump_texture).pack(side="right", padx=6)
        ttk.Button(top, text="Import replacement image...", command=self.import_texture_replacement).pack(side="right", padx=6)
        self.texture_apply_btn = ttk.Button(top, text="Apply texture changes", command=self.apply_texture_replacement,
                                            style="Apply.TButton", state="disabled")
        self.texture_apply_btn.pack(side="right", padx=6)
        self.texture_source_label = ttk.Label(self.texture_tab, text="No textures scanned")
        self.texture_source_label.pack(anchor="w", pady=(4, 8))
        split = ttk.Panedwindow(self.texture_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=2)
        split.add(right, weight=3)
        ttk.Label(left, text="Textures in the selected file").pack(anchor="w")
        texture_tree_frame = ttk.Frame(left)
        texture_tree_frame.pack(fill="both", expand=True, pady=(5, 0))
        self.texture_tree = ttk.Treeview(texture_tree_frame, columns=("name", "size", "format", "dims"), show="headings")
        for c, t, w in (("name", "Texture", 220), ("size", "Size", 90), ("format", "Format", 150), ("dims", "Dimensions", 100)):
            self.texture_tree.heading(c, text=t)
            self.texture_tree.column(c, width=w, anchor="w")
        self.texture_tree.pack(side="left", fill="both", expand=True)
        texture_scrollbar = ttk.Scrollbar(texture_tree_frame, orient="vertical", command=self.texture_tree.yview)
        texture_scrollbar.pack(side="right", fill="y")
        self.texture_tree.configure(yscrollcommand=texture_scrollbar.set)
        self.texture_tree.bind("<<TreeviewSelect>>", self.on_texture_selected)
        original_box = ttk.LabelFrame(right, text="Selected texture", padding=8)
        original_box.pack(fill="both", expand=True)
        self.texture_preview = tk.Label(original_box, text="Select a texture", anchor="center", justify="center", bg=self._dark["field"], fg=self._dark["muted"])
        self.texture_preview.pack(fill="both", expand=True)
        replacement_box = ttk.LabelFrame(right, text="Texture to import", padding=8)
        replacement_box.pack(fill="both", expand=True, pady=(8, 0))
        self.texture_replacement_preview = tk.Label(replacement_box, text="Import a replacement texture", anchor="center", justify="center", bg=self._dark["field"], fg=self._dark["muted"])
        self.texture_replacement_preview.pack(fill="both", expand=True)
        self.texture_info = ttk.Label(right, text="", justify="left")
        self.texture_info.pack(fill="x", pady=(8, 0))
        transform_box = ttk.LabelFrame(right, text="Texture orientation", padding=6)
        transform_box.pack(fill="x", pady=(8, 0))
        ttk.Button(transform_box, text="Rotate 90°", command=lambda: self.rotate_texture(90)).pack(side="left")
        ttk.Button(transform_box, text="Flip X axis", command=lambda: self.flip_texture("x")).pack(side="left", padx=(6, 0))
        ttk.Button(transform_box, text="Flip Y axis", command=lambda: self.flip_texture("y")).pack(side="left", padx=(6, 0))
        self.texture_transform_label = ttk.Label(transform_box, text="Rotation: 0° • Flip X: no • Flip Y: no")
        self.texture_transform_label.pack(side="left", padx=(10, 0))

    def scan_textures(self) -> None:
        if self.project.kind == "bin":
            asset = self.project.assets[0] if self.project.assets else None
        else:
            asset = self._selected_asset()
        if not asset:
            messagebox.showinfo("Texture Swap", "Select a .wow/.bin/.gao asset in the browser first.")
            return
        try:
            data = self.project.read_asset(asset)
            entries = _parse_pop_file_entries(data)
            infos = _scan_pop_textures(data)
            self._texture_data = bytearray(data)
            self._texture_original = bytes(data)
            self._texture_file_entries = entries
            self._texture_infos = infos
            self._texture_source_asset = asset
            self._texture_dirty = False
            self._clear_tree(self.texture_tree)
            for i, tex in enumerate(infos):
                self.texture_tree.insert("", "end", iid=f"tex_{i}", values=(f"Texture #{i + 1} 0x{tex.key:08X}", f"{tex.data_end - tex.data_offset:,} B", tex.format, f"{tex.width} x {tex.height}"))
            unsupported = sum(
                1 for entry in entries
                if entry.size >= 56
                and struct.unpack_from("<I", data, entry.data_offset + 32)[0] == 0xC0DEC0DE
                and struct.unpack_from("<I", data, entry.data_offset + 40)[0] not in (0, 1, 7)
            )
            self.texture_source_label.config(
                text=f"{asset.name} — {len(entries)} objects, {len(infos)} viewable textures"
                + (f", {unsupported} unsupported textures/formats" if unsupported else "")
            )
            self.texture_info.config(text="Select a texture to preview it.")
            self.tabs.select(self.texture_tab)
            self._log(f"OK    FileEntry scan: {asset.name} -> {len(entries)} objects")
            self._log(f"OK    Texture scan: {asset.name} -> {len(infos)} viewable textures")
        except Exception as exc:
            self._log(f"ERROR Texture scan: {exc}")
            messagebox.showerror("Texture Swap", str(exc))

    def _texture_selected(self) -> TextureInfo | None:
        selection = self.texture_tree.selection()
        if not selection:
            return None
        idx = int(selection[0].split("_")[-1])
        return self._texture_infos[idx] if 0 <= idx < len(self._texture_infos) else None

    def _dds_blob_for_texture(self, tex: TextureInfo) -> bytes:
        if tex.texture_type != 7:
            raise ValueError("This entry is not a DDS texture.")
        payload = bytes(self._texture_data[tex.data_offset:tex.data_end])
        if tex.format.startswith("Raw BGRA8"):
            return _build_tga_header(tex.storage_width, tex.storage_height, 32) + payload
        return _build_type7_dds(payload, tex.storage_width, tex.storage_height)

    def _tga_blob_for_texture(self, tex: TextureInfo) -> bytes:
        payload = bytes(self._texture_data[tex.data_offset:tex.data_end])
        if tex.texture_type == 0:
            return _build_tga_header(tex.storage_width, tex.storage_height, 32) + payload[:tex.storage_width * tex.storage_height * 4]
        return _build_tga_header(tex.storage_width, tex.storage_height, 32) + payload

    def _palette_tga_blob_for_texture(self, tex: TextureInfo) -> bytes:
        if tex.texture_type != 1:
            return self._tga_blob_for_texture(tex)
        if tex.data_offset < 4:
            raise ValueError("Palette texture: invalid offset.")
        palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
        palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
        if palette_entry is None or palette_entry.size < 4:
            raise ValueError(f"Palette 0x{palette_id:08X} not found in the BIN.")
        palette = self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size]
        indices = self._texture_data[tex.data_offset:tex.data_end]
        pixel_count = tex.width * tex.height
        return _build_palette_tga(tex.width, tex.height, bytes(palette), bytes(indices))

    def _set_preview_from_dds(self, widget, dds: bytes) -> None:
        try:
            from PIL import Image, ImageTk
            image = Image.open(io.BytesIO(dds)).convert("RGBA")
            image.thumbnail((480, 220), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            widget.configure(image=photo, text="")
            widget.image = photo
        except Exception as exc:
            widget.configure(image="", text=f"DDS preview unavailable\n{exc}")

    def _set_preview_from_tga(self, widget, tga: bytes) -> None:
        try:
            from PIL import Image, ImageTk
            image = Image.open(io.BytesIO(tga)).convert("RGBA")
            image.thumbnail((480, 220), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            widget.configure(image=photo, text="")
            widget.image = photo
        except Exception as exc:
            widget.configure(image="", text=f"TGA preview unavailable\n{exc}")

    def on_texture_selected(self, _event=None) -> None:
        tex = self._texture_selected()
        if not tex:
            return
        self._texture_replacement = None
        self.texture_replacement_preview.configure(image="", text="Import a replacement texture")
        self.texture_replacement_preview.image = None
        self._texture_rotation = 0
        self._texture_flip_x = False
        self._texture_flip_y = False
        self._update_texture_transform_label()
        self.texture_info.config(text=f"Offset 0x{tex.offset:08X} • payload 0x{tex.data_offset:08X}-0x{tex.data_end:08X} • {tex.width}x{tex.height} • {tex.format}")
        if tex.texture_type == 7:
            self._set_preview_from_tga(self.texture_preview, self._tga_blob_for_texture(tex)) if tex.format.startswith("Raw BGRA8") else self._set_preview_from_dds(self.texture_preview, self._dds_blob_for_texture(tex))
        elif tex.texture_type in (5, 6):
            self._set_preview_from_dds(self.texture_preview, _compressed_dds_for_preview(bytes(self._texture_data[tex.data_offset:tex.data_end]), tex))
        elif tex.texture_type == 0:
            self._set_preview_from_tga(self.texture_preview, self._tga_blob_for_texture(tex))
        elif tex.texture_type == 1:
            self._set_preview_from_tga(self.texture_preview, self._palette_tga_blob_for_texture(tex))
        elif tex.texture_type == 11:
            self._set_preview_from_tga(self.texture_preview, _build_4bit_tga(tex.width, tex.height, bytes(self._texture_data[tex.data_offset:tex.data_end])))
        else:
            self.texture_preview.configure(image="", text=f"{tex.format}\nPreview for this format is planned for a future update")
        self.texture_apply_btn.configure(state="disabled")

    def _update_texture_transform_label(self) -> None:
        if hasattr(self, "texture_transform_label"):
            self.texture_transform_label.config(
                text=(f"Rotation: {self._texture_rotation}° • "
                      f"Flip X: {'yes' if self._texture_flip_x else 'no'} • "
                      f"Flip Y: {'yes' if self._texture_flip_y else 'no'}")
            )

    def rotate_texture(self, degrees: int = 90) -> None:
        if not getattr(self, "_texture_replacement", None):
            messagebox.showinfo("Texture Swap", "Import a texture before rotating or flipping it.")
            return
        self._texture_rotation = (self._texture_rotation + degrees) % 360
        self._update_texture_transform_label()
        self._preview_texture_transform()
        self.texture_apply_btn.configure(state="normal")

    def flip_texture(self, axis: str) -> None:
        if not getattr(self, "_texture_replacement", None):
            messagebox.showinfo("Texture Swap", "Import a texture before rotating or flipping it.")
            return
        if axis.lower() == "x":
            self._texture_flip_x = not self._texture_flip_x
        elif axis.lower() == "y":
            self._texture_flip_y = not self._texture_flip_y
        else:
            raise ValueError(f"Unsupported flip axis: {axis}")
        self._update_texture_transform_label()
        self._preview_texture_transform()
        self.texture_apply_btn.configure(state="normal")

    def _preview_texture_transform(self) -> None:
        tex = self._texture_selected()
        replacement = getattr(self, "_texture_replacement", None)
        if not tex or not replacement:
            return
        try:
            from PIL import Image, ImageTk
            _source, payload, target_type = replacement
            if target_type == 7:
                blob = _build_dds(
                    payload, tex.width, tex.height,
                    _build_dds_header(tex.width, tex.height,
                                      _infer_dxt5_mip_count(tex.width, tex.height, len(payload)) - 1, 7)
                )
            elif target_type in (5, 6):
                blob = _build_dds(
                    payload, tex.width, tex.height,
                    _build_dds_header(tex.width, tex.height, 0, target_type),
                )
            elif target_type == 0:
                blob = _build_tga_header(tex.width, tex.height) + payload
            elif tex.texture_type == 1:
                palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                if palette_entry is None:
                    raise ValueError("Texture palette not found.")
                palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                blob = _build_palette_tga(tex.width, tex.height, palette, payload)
            else:
                return
            image = Image.open(io.BytesIO(blob)).convert("RGBA")
            image = _transform_texture_image(image, self._texture_rotation, self._texture_flip_x, self._texture_flip_y)
            image.thumbnail((480, 220), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            self.texture_replacement_preview.configure(image=photo, text="")
            self.texture_replacement_preview.image = photo
        except Exception as exc:
            self.texture_replacement_preview.configure(image="", text=f"Orientation preview unavailable\n{exc}")

    def import_texture_replacement(self) -> None:
        tex = self._texture_selected()
        if not tex:
            messagebox.showinfo("Texture Swap", "Select the texture to replace first.")
            return
        filetypes = [("Images", "*.png *.jpg *.jpeg *.tga *.bmp *.dds *.webp"), ("All files", "*.*")]
        source = filedialog.askopenfilename(title="Import replacement texture", filetypes=filetypes)
        if not source:
            return
        try:
            target_type = 7 if tex.texture_type == 1 else tex.texture_type
            if tex.texture_type == 7:
                payload = _dds_payload_from_file(Path(source), tex, bytes(self._texture_data))
            elif tex.texture_type == 5:
                # Jade Toolkit keeps existing DXT1 textures as type 5. Their
                # four-byte native prefix is retained while applying the patch.
                payload = _dxt1_payload_from_file(Path(source), tex)
            elif tex.texture_type == 1:
                # Palette textures intentionally become DXT5, matching the
                # existing Auto conversion path.
                payload = _dxt5_payload_for_converted_texture(Path(source), tex)
            elif tex.texture_type == 0:
                payload = _tga_payload_from_file(Path(source), tex, bytes(self._texture_data))
            elif tex.texture_type == 1:
                if tex.data_offset < 4:
                    raise ValueError("Palette texture: invalid offset.")
                palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                if palette_entry is None:
                    raise ValueError(f"Palette 0x{palette_id:08X} not found in the BIN.")
                palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                payload = _palette_payload_from_file(
                    Path(source), tex, palette,
                    bytes(self._texture_data[tex.data_offset:tex.data_end]),
                )
            else:
                raise ValueError(f"Unsupported POP texture format: type {tex.texture_type}.")
            self._texture_replacement = (Path(source), payload, target_type)
            self._texture_patch_source_type = tex.texture_type
            self._texture_patch_key = tex.key
            # Jade scanlines are vertically opposite to conventional image files.
            # Apply this correction by default; the user can still toggle it off.
            self._texture_flip_y = True
            self._update_texture_transform_label()
            if target_type == 7:
                preview_dds = _build_dds(payload, tex.width, tex.height,
                                         _build_dds_header(tex.width, tex.height,
                                                           _infer_dxt5_mip_count(tex.width, tex.height, len(payload)) - 1, 7))
                self._set_preview_from_dds(self.texture_replacement_preview, preview_dds)
            elif target_type in (5, 6):
                preview_dds = _build_dds(
                    payload, tex.width, tex.height,
                    _build_dds_header(tex.width, tex.height, 0, target_type),
                )
                self._set_preview_from_dds(self.texture_replacement_preview, preview_dds)
            else:
                if tex.texture_type == 0:
                    preview = _build_tga_header(tex.width, tex.height) + payload
                else:
                    preview = self._palette_tga_blob_for_texture(tex)
                    # Preview the imported indices rather than the old texture.
                    preview = _build_palette_tga(tex.width, tex.height,
                                                 bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size]),
                                                 payload)
                self._set_preview_from_tga(self.texture_replacement_preview, preview)
            self._preview_texture_transform()
            target_label = (
                "DXT5 (automatic conversion)" if target_type == 7 and tex.texture_type != 7
                else ("DXT1" if target_type == 5 else tex.format)
            )
            self.texture_info.config(text=f"Imported: {Path(source).name} • {len(payload):,} B • {tex.width}x{tex.height} {target_label}")
            self.texture_apply_btn.configure(state="normal")
            self._log(f"OK    Texture converted: {source} -> {len(payload):,} B {target_label} {tex.width}x{tex.height}")
        except Exception as exc:
            self._texture_replacement = None
            self.texture_apply_btn.configure(state="disabled")
            self._log(f"ERROR Import DDS: {exc}")
            messagebox.showerror("Import DDS", str(exc))

    def dump_texture(self) -> None:
        """Write the selected texture in its original embedded form beside the source .BF."""
        tex = self._texture_selected()
        asset = self._texture_source_asset
        if not tex or not asset:
            messagebox.showinfo("Texture Swap", "Select the texture to extract first.")
            return
        try:
            source_path = self.project.path
            if source_path is None:
                raise ValueError("No source .BF file available.")
            # Keep a native dump, then always create a PNG alongside it. DXT1
            # and DXT3 must be wrapped as DDS; writing their compressed blocks
            # under a .tga extension produces an unreadable/truncated image.
            suffix = ".dds" if tex.texture_type in (5, 6, 7) and not tex.format.startswith("Raw BGRA8") else ".tga"
            target = source_path.parent / f"{source_path.stem}_texture_{tex.index + 1:03d}{suffix}"
            png_target = source_path.parent / f"{source_path.stem}_texture_{tex.index + 1:03d}.png"
            if tex.texture_type in (5, 6, 7):
                blob = _dds_blob_for_dump(self._texture_data, tex)
            elif tex.texture_type == 0:
                blob = _build_tga_header(tex.width, tex.height) + bytes(
                    self._texture_data[tex.data_offset:tex.data_offset + tex.width * tex.height * 4]
                )
            elif tex.texture_type == 11:
                blob = _build_4bit_tga(
                    tex.width, tex.height, bytes(self._texture_data[tex.data_offset:tex.data_end])
                )
            else:
                blob = self._palette_tga_blob_for_texture(tex)
            try:
                from PIL import Image
                image = Image.open(io.BytesIO(blob)).convert("RGBA")
                image.save(png_target, "PNG")
            except Exception as exc:
                raise ValueError(f"Cannot convert the dump to PNG: {exc}") from exc
            target.write_bytes(blob)
            self._log(f"OK    Texture dump: {target} • PNG: {png_target}")
            messagebox.showinfo("Dump texture", f"Created:\n{target}\n{png_target}")
        except Exception as exc:
            self._log(f"ERROR Dump texture: {exc}")
            messagebox.showerror("Dump texture", str(exc))

    def apply_texture_replacement(self) -> None:
        tex = self._texture_selected()
        replacement = getattr(self, "_texture_replacement", None)
        if not tex:
            messagebox.showinfo("Texture Swap", "Select the texture to replace first.")
            return
        if not replacement:
            messagebox.showinfo("Texture Swap", "Import a texture before applying changes.")
            return
        _source, payload, target_type = replacement
        source_type = (getattr(self, "_texture_patch_source_type", tex.texture_type)
                       if getattr(self, "_texture_patch_key", tex.key) == tex.key
                       else tex.texture_type)
        if (target_type == tex.texture_type and target_type != 5
                and len(payload) != tex.data_end - tex.data_offset):
            messagebox.showerror("Texture Swap", "The imported texture does not have the same compressed size as the original.")
            return
        if self._texture_rotation or self._texture_flip_x or self._texture_flip_y:
            try:
                from PIL import Image
                if target_type == 7:
                    source_blob = _build_dds(
                        payload, tex.width, tex.height,
                        _build_dds_header(tex.width, tex.height,
                                          _infer_dxt5_mip_count(tex.width, tex.height, len(payload)) - 1, 7)
                    )
                elif target_type in (5, 6):
                    source_blob = _build_dds(
                        payload, tex.width, tex.height,
                        _build_dds_header(tex.width, tex.height, 0, target_type),
                    )
                elif target_type == 0:
                    source_blob = _build_tga_header(tex.width, tex.height) + payload
                else:
                    source_blob = self._palette_tga_blob_for_texture(tex)
                    palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                    palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                    if palette_entry is None:
                        raise ValueError("Texture palette not found.")
                    palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                    source_blob = _build_palette_tga(tex.width, tex.height, palette, payload)
                source_image = Image.open(io.BytesIO(source_blob)).convert("RGBA")
                source_image = _transform_texture_image(
                    source_image, self._texture_rotation, self._texture_flip_x, self._texture_flip_y
                )
                if target_type == 7:
                    mip_count = _infer_dxt5_mip_count(tex.width, tex.height, len(payload))
                    payload = _encode_dxt5(source_image, mip_count)
                elif target_type == 5:
                    payload = _encode_dxt1(source_image)
                elif target_type == 0:
                    transformed = source_image.tobytes()
                    # Preserve bytes after the visible level (mips/padding).
                    payload = bytearray(payload)
                    for i in range(0, len(transformed), 4):
                        r, g, b, a = transformed[i:i + 4]
                        payload[i:i + 4] = bytes((b, g, r, a))
                    payload = bytes(payload)
                elif target_type == 1:
                    palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                    palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                    if palette_entry is None:
                        raise ValueError("Texture palette not found.")
                    palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                    payload = _palette_payload_from_image(source_image, tex, palette, payload)
                if (target_type == tex.texture_type and target_type != 5
                        and len(payload) != tex.data_end - tex.data_offset):
                    raise ValueError("The transform did not produce a payload with the same size as the original.")
            except Exception as exc:
                messagebox.showerror("Texture Swap", f"Cannot apply rotation/flip: {exc}")
                return
        patched, count = _patch_texture_key_in_asset(
            bytes(self._texture_data), tex.key, payload, source_type, target_type,
            tex.width, tex.height,
        )
        if not count:
            messagebox.showerror("Texture Swap", "Selected texture not found in the current buffer.")
            return
        self._texture_data = bytearray(patched)
        self._texture_file_entries = _parse_pop_file_entries(self._texture_data)
        self._texture_infos = _scan_pop_textures(self._texture_data)
        self._texture_dirty = self._texture_data != bytearray(self._texture_original)
        self._texture_replacement = None
        self._texture_rotation = 0
        self._texture_flip_x = False
        self._texture_flip_y = False
        self._update_texture_transform_label()
        self.texture_apply_btn.configure(state="disabled")
        new_tex = next((item for item in self._texture_infos
                        if item.key == tex.key and item.texture_type == target_type
                        and item.width == tex.width and item.height == tex.height), None)
        if new_tex is not None:
            self.texture_tree.selection_set(f"tex_{new_tex.index}")
            self.texture_tree.focus(f"tex_{new_tex.index}")
            self.on_texture_selected()
        self.texture_info.config(text=f"Replacement applied • {tex.width}x{tex.height} • type {target_type} / {len(payload):,} B. Use Save/Extract/Rebuild to write the file.")
        self._log(f"PATCH TEXTURE  key=0x{tex.key:08X} • type={target_type} • payload={len(payload):,} B")

    def save_texture_asset(self) -> None:
        if not self._texture_dirty or self._texture_source_asset is None:
            messagebox.showinfo("Texture Swap", "No texture changes to save.")
            return
        asset = self._texture_source_asset
        target = filedialog.asksaveasfilename(title="Save edited texture asset", initialfile=f"{Path(asset.name).stem}_edited.bin", defaultextension=".bin")
        if not target:
            return
        try:
            if self.project.kind in ("bin", "dec"):
                self.project.decoded_bin = bytes(self._texture_data)
                self.project.save_bin_as(Path(target), bytes(self._texture_data))
            else:
                # A POP texture is identified by its Jade key.  Some BF assets
                # contain duplicate copies of that key; editing only the asset
                # currently shown in the viewer can therefore produce a BF that
                # previews correctly while the game resolves an untouched copy.
                selected_entry = next((e for e in self.project.info.entries if e.index == asset.index), None)
                texture = self._texture_selected()
                if selected_entry is None or texture is None:
                    raise ValueError("Cannot locate the BF entry for the selected texture.")
                replacements, touched = _collect_texture_key_replacements(
                    self.project.path,
                    selected_entry,
                    texture.key,
                    bytes(self._texture_data[texture.data_offset:texture.data_end]),
                    (getattr(self, "_texture_patch_source_type", texture.texture_type)
                     if getattr(self, "_texture_patch_key", texture.key) == texture.key
                     else texture.texture_type),
                    texture.texture_type,
                    texture.width,
                    texture.height,
                    bytes(self._texture_data),
                )
                _repack_legacy_bigfile_changes(self.project.path, replacements, Path(target))
                self._log(
                    f"OK    Texture key 0x{texture.key:08X}: updated {len(replacements)} assets "
                    f"({', '.join(touched[:6])}{'...' if len(touched) > 6 else ''})"
                )
            self._texture_dirty = False
            self._log(f"OK    Texture asset saved: {target}")
            messagebox.showinfo("Texture Swap", f"Created:\n{target}")
        except Exception as exc:
            self._log(f"ERROR Save texture asset: {exc}")
            messagebox.showerror("Texture Swap", str(exc))

"""Material Editor controls, properties and texture lookup."""
from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import queue
import struct
import threading
import tkinter as tk
from ..materials import (
    _associate_mesh_material_packs,
    _scan_pop_material_records,
    _scan_pop_materials,
)
from ..mesh_parser import _scan_pop_meshes
from ..models import (
    Asset,
    MaterialInfo,
    TextureInfo,
)
from ..project import JadeProject
from ..textures import (
    _decode_pop_texture_image,
    _scan_pop_textures,
)
from ..viewports import (
    MaterialViewport,
    OpenGLFrame,
)


class MaterialEditorMixin:
    def _build_material_tab(self) -> None:
        top = ttk.Frame(self.material_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Material Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_materials).pack(side="right")
        ttk.Button(top, text="Apply material changes", command=self.apply_material_changes, style="Apply.TButton").pack(side="right", padx=6)
        self.material_source_label = ttk.Label(self.material_tab, text="No materials scanned")
        self.material_source_label.pack(anchor="w", pady=(4, 8))

        split = ttk.Panedwindow(self.material_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=2)
        split.add(right, weight=5)

        ttk.Label(left, text="Detected materials").pack(anchor="w")
        material_tree_frame = ttk.Frame(left)
        material_tree_frame.pack(fill="both", expand=True, pady=(5, 0))
        self.material_tree = ttk.Treeview(material_tree_frame, columns=("name", "diffuse", "secondary", "opacity", "specular"), show="headings")
        for c, t, w in (("name", "Material", 210), ("diffuse", "Diffuse", 110), ("secondary", "Secondary", 110),
                        ("opacity", "Opacity", 65), ("specular", "Spec exp.", 72)):
            self.material_tree.heading(c, text=t)
            self.material_tree.column(c, width=w, anchor="w")
        self.material_tree.pack(side="left", fill="both", expand=True)
        material_scrollbar = ttk.Scrollbar(material_tree_frame, orient="vertical", command=self.material_tree.yview)
        material_scrollbar.pack(side="right", fill="y")
        self.material_tree.configure(yscrollcommand=material_scrollbar.set)
        self.material_tree.bind("<<TreeviewSelect>>", self.on_material_selected)

        preview_box = ttk.LabelFrame(right, text="Material preview", padding=6)
        preview_box.pack(fill="both", expand=True)
        if OpenGLFrame is not None:
            self.material_canvas = MaterialViewport(preview_box, self, highlightthickness=0, bd=0)
            self.material_canvas.pack(fill="both", expand=True)
        else:
            self.material_canvas = tk.Label(preview_box, text="OpenGL unavailable", anchor="center")
            self.material_canvas.pack(fill="both", expand=True)

        editor = ttk.LabelFrame(right, text="Material properties", padding=8)
        editor.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Diffuse texture", width=18).pack(side="left")
        self.material_diffuse_combo = ttk.Combobox(row, state="readonly", width=54)
        self.material_diffuse_combo.pack(side="left", fill="x", expand=True, padx=6)
        self.material_diffuse_combo.bind("<<ComboboxSelected>>", lambda _e: self._material_preview_changed())
        ttk.Button(row, text="Open texture", command=lambda: self._open_material_texture("diffuse")).pack(side="right", padx=(0, 4))
        ttk.Button(row, text="Refresh", command=self._material_preview_changed).pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Secondary texture", width=18).pack(side="left")
        ttk.Entry(row, textvariable=self._material_secondary_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Open texture", command=lambda: self._open_material_texture("secondary")).pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Normal map (WW)", width=18).pack(side="left")
        ttk.Entry(row, textvariable=self._material_normal_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Open texture", command=lambda: self._open_material_texture("normal")).pack(side="right", padx=(0, 4))
        ttk.Button(row, text="Add normal...", command=self.choose_material_normal).pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=(8, 2))
        ttk.Label(row, text="Specular exponent", width=18).pack(side="left")
        self.material_metal_scale = ttk.Scale(row, from_=0.0, to=128.0, variable=self._material_metallic,
                                              command=lambda _v: self._material_preview_changed())
        self.material_metal_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.material_metal_value = ttk.Label(row, text="0.00", width=6)
        self.material_metal_value.pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Opacity (Jade)", width=18).pack(side="left")
        self.material_alpha_scale = ttk.Scale(row, from_=0.0, to=1.0, variable=self._material_alpha,
                                              command=lambda _v: self._material_preview_changed())
        self.material_alpha_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.material_alpha_value = ttk.Label(row, text="1.00", width=6)
        self.material_alpha_value.pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=(8, 2))
        ttk.Label(row, text="Preview shape", width=18).pack(side="left")
        shape_combo = ttk.Combobox(row, state="readonly", width=16, textvariable=self._material_shape,
                                   values=("sphere", "cube"))
        shape_combo.pack(side="left", padx=6)
        shape_combo.bind("<<ComboboxSelected>>", lambda _e: self._material_preview_changed())
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Material projection", width=18).pack(side="left")
        self.material_projection_scale = ttk.Scale(row, from_=0.1, to=4.0, variable=self._material_projection,
                                                   command=lambda _v: self._material_preview_changed())
        self.material_projection_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.material_projection_value = ttk.Label(row, text="1.00×", width=7)
        self.material_projection_value.pack(side="right")
        self.material_info = ttk.Label(editor, text="Select a material to edit it.", justify="left")
        self.material_info.pack(anchor="w", pady=(7, 0))

    def _collect_material_texture_inventory(self, preferred_asset: Asset, data: bytes | None = None) -> dict[int, tuple[Asset, bytes, TextureInfo]]:
        """Index only the selected asset; BF-wide indexing runs in background."""
        candidate_data = data if data is not None else self.project.read_asset(preferred_asset)
        inventory: dict[int, tuple[Asset, bytes, TextureInfo]] = {}
        for texture in _scan_pop_textures(candidate_data):
            current = inventory.get(texture.key)
            if current is None or texture.data_end - texture.data_offset > current[2].data_end - current[2].data_offset:
                inventory[texture.key] = (preferred_asset, candidate_data, texture)
        return inventory

    def _is_ww_normal_map_archive(self) -> bool:
        if self.project.kind != "bf" or self.project.path is None:
            return False
        return self.project.path.name.upper() in {"WW_FULL_GAME_WINDOWS.BF", "WW_DEMO_XBOX.BF"}

    def scan_materials(self) -> None:
        asset = self._selected_asset()
        if asset is None:
            messagebox.showinfo("Material Editor", "Select a .wow/.bin/.gao asset in the browser first.")
            return
        try:
            self._material_index_generation += 1
            generation = self._material_index_generation
            self.status.set("Material Editor: reading materials and local textures...")
            self.update_idletasks()
            data = self.project.read_asset(asset)

            project_path = self.project.path
            if self._material_cache_project != project_path:
                self._material_inventory_sources.clear()
                self._material_external_texture_cache.clear()
                self._material_texture_decode_misses.clear()
                self._material_inventory_complete = False
                self._material_cache_project = project_path

            inventory = self._collect_material_texture_inventory(asset, data)
            for key in inventory:
                self._material_inventory_sources[key] = asset
            known_texture_keys = set(inventory) | set(self._material_inventory_sources)
            records = _scan_pop_material_records(data, known_texture_keys)
            ww_normal_maps = self._is_ww_normal_map_archive()
            for info in records.values():
                info.normal_key = info.secondary_key if ww_normal_maps else None

            packs, _materials = _scan_pop_materials(data)
            meshes = _scan_pop_meshes(data)
            _associate_mesh_material_packs(data, meshes)
            for mesh in meshes:
                pack = packs.get(mesh.material_pack_key, []) if mesh.material_pack_key is not None else []
                if not pack and mesh.material_pack_key in records:
                    pack = [mesh.material_pack_key]
                for material_id, _count in mesh.material_ids:
                    if not pack:
                        continue
                    pack_index = 0 if material_id < 0 else min(material_id, len(pack) - 1)
                    info = records.get(pack[pack_index])
                    if info is not None and mesh.key not in (info.source_meshes or []):
                        info.source_meshes = list(info.source_meshes or []) + [mesh.key]

            wanted_keys = {
                key for info in records.values()
                for key in (info.texture_key, info.secondary_key, info.normal_key)
                if key not in (None, 0, 0xFFFFFFFF)
            }
            images: dict[int, object] = {}
            for key in wanted_keys:
                found = inventory.get(key)
                if found is not None:
                    _texture_asset, texture_data, texture = found
                    try:
                        images[key] = _decode_pop_texture_image(texture_data, texture)
                    except Exception as exc:
                        self._log(f"WARN  Texture preview 0x{key:08X}: {exc}")
                elif key in self._material_external_texture_cache:
                    images[key] = self._material_external_texture_cache[key]

            self._material_data = bytearray(data)
            self._material_original = bytes(data)
            self._material_dirty = False
            self._material_infos = list(records.values())
            self._material_source_asset = asset
            self._material_textures = images
            self._material_texture_sources = dict(self._material_inventory_sources)
            self._material_diffuse_combo_values = [
                f"0x{key:08X}" for key in sorted(known_texture_keys)
            ]
            self._clear_tree(self.material_tree)
            for i, info in enumerate(self._material_infos):
                diffuse = f"0x{info.texture_key:08X}" if info.texture_key is not None else "<none>"
                secondary = f"0x{info.secondary_key:08X}" if info.secondary_key is not None else "<none>"
                self.material_tree.insert(
                    "", "end", iid=f"mat_{i}",
                    values=(f"0x{info.material_key:08X}", diffuse, secondary,
                            f"{info.alpha:.2f}", f"{info.metallic:.2f}")
                )
            self.material_diffuse_combo["values"] = self._material_diffuse_combo_values
            source_note = " - WW normal maps enabled" if ww_normal_maps else " - secondary layers are not normal maps"
            unresolved = len(wanted_keys - set(images))
            missing_note = f" - {unresolved} keys not found" if unresolved else ""
            indexing_note = " - BF indexing in background" if self.project.kind == "bf" and not self._material_inventory_complete else ""
            self.material_source_label.config(text=(
                f"{asset.name} - {len(self._material_infos)} materials, "
                f"{len(images)}/{len(wanted_keys)} texture previews from {len(known_texture_keys)} known textures"
                f"{source_note}{missing_note}{indexing_note}"
            ))
            self.tabs.select(self.material_tab)
            if self._material_infos:
                self.material_tree.selection_set("mat_0")
                self.material_tree.focus("mat_0")
                self.on_material_selected()
            self.status.set("Ready")
            self._log(
                f"OK    Material scan: {asset.name} -> {len(self._material_infos)} materials, "
                f"{len(images)} textures resolved immediately"
            )

            missing_known = (wanted_keys - set(images)) - self._material_texture_decode_misses
            full_index = self.project.kind == "bf" and not self._material_inventory_complete
            if full_index or missing_known:
                self._start_material_texture_search(
                    asset, missing_known, generation, full_index
                )
        except Exception as exc:
            self.status.set("Ready")
            self._log(f"ERROR Material scan: {exc}")
            messagebox.showerror("Material Editor", str(exc))

    def _start_material_texture_search(
        self, preferred_asset: Asset, texture_keys: set[int],
        generation: int, full_index: bool
    ) -> None:
        """Build the BF texture index and decode requested previews off the UI thread."""
        if self.project.kind != "bf" or self.project.path is None:
            return
        project_path = self.project.path
        preferred_index = preferred_asset.index
        wanted = set(texture_keys)
        source_indices = {
            key: asset.index for key, asset in self._material_inventory_sources.items()
            if key in wanted
        }
        action = "indexing BF textures" if full_index else f"loading {len(wanted)} textures"
        self.status.set(f"Ready - Material Editor: {action} in background...")

        def worker() -> None:
            found_images: dict[int, object] = {}
            found_sources: dict[int, Asset] = {}
            remaining = set(wanted)
            error = ""
            try:
                background_project = JadeProject()
                background_project.open_bf(project_path)
                if full_index:
                    candidates = [
                        candidate for candidate in background_project.assets
                        if candidate.index != preferred_index
                    ]
                else:
                    target_indices = set(source_indices.values())
                    candidates = [
                        candidate for candidate in background_project.assets
                        if candidate.index in target_indices
                    ]
                source_sizes: dict[int, int] = {}
                image_sizes: dict[int, int] = {}
                for candidate in candidates:
                    if generation != self._material_index_generation or project_path != self.project.path:
                        return
                    try:
                        candidate_data = background_project.read_asset(candidate)
                        candidate_textures = _scan_pop_textures(candidate_data)
                    except Exception:
                        continue
                    for texture in candidate_textures:
                        payload_size = texture.data_end - texture.data_offset
                        if full_index and payload_size > source_sizes.get(texture.key, -1):
                            source_sizes[texture.key] = payload_size
                            found_sources[texture.key] = candidate
                        if texture.key not in wanted or payload_size <= 4:
                            continue
                        if payload_size <= image_sizes.get(texture.key, -1):
                            continue
                        try:
                            found_images[texture.key] = _decode_pop_texture_image(candidate_data, texture)
                        except Exception:
                            continue
                        image_sizes[texture.key] = payload_size
                        remaining.discard(texture.key)
            except Exception as exc:
                error = str(exc)
            self._material_index_queue.put((
                generation, project_path, found_images, found_sources,
                remaining, full_index, error
            ))

        threading.Thread(
            target=worker, name="material-texture-index", daemon=True
        ).start()
        self.after(100, lambda: self._poll_material_texture_search(generation))

    def _poll_material_texture_search(self, generation: int) -> None:
        if generation != self._material_index_generation:
            return
        result = None
        while result is None:
            try:
                candidate = self._material_index_queue.get_nowait()
            except queue.Empty:
                self.after(100, lambda: self._poll_material_texture_search(generation))
                return
            if candidate[0] == generation:
                result = candidate

        (_result_generation, project_path, found_images, found_sources,
         remaining, full_index, error) = result
        if project_path != self.project.path:
            return
        self._material_external_texture_cache.update(found_images)
        self._material_texture_decode_misses.update(remaining)
        self._material_inventory_sources.update(found_sources)
        if full_index:
            self._material_inventory_complete = True
        self._material_textures.update(found_images)
        self._material_texture_sources = dict(self._material_inventory_sources)

        known_keys = set(self._material_inventory_sources)
        refreshed = _scan_pop_material_records(bytes(self._material_data), known_keys)
        ww_normal_maps = self._is_ww_normal_map_archive()
        for index, info in enumerate(self._material_infos):
            updated = refreshed.get(info.material_key)
            if updated is not None:
                info.secondary_key = updated.secondary_key
                info.normal_key = updated.secondary_key if ww_normal_maps else None
            diffuse = f"0x{info.texture_key:08X}" if info.texture_key is not None else "<none>"
            secondary = f"0x{info.secondary_key:08X}" if info.secondary_key is not None else "<none>"
            iid = f"mat_{index}"
            if self.material_tree.exists(iid):
                self.material_tree.item(
                    iid, values=(f"0x{info.material_key:08X}", diffuse, secondary,
                                 f"{info.alpha:.2f}", f"{info.metallic:.2f}")
                )

        self._material_diffuse_combo_values = [
            f"0x{key:08X}" for key in sorted(known_keys)
        ]
        self.material_diffuse_combo["values"] = self._material_diffuse_combo_values
        selected_info = self._selected_material()
        if selected_info is not None and not self.material_diffuse_combo.get():
            current = f"0x{selected_info.texture_key:08X}" if selected_info.texture_key is not None else ""
            if current in self._material_diffuse_combo_values:
                self.material_diffuse_combo.set(current)
        self._material_preview_changed()

        wanted_keys = {
            key for info in self._material_infos
            for key in (info.texture_key, info.secondary_key, info.normal_key)
            if key not in (None, 0, 0xFFFFFFFF)
        }
        unresolved_keys = wanted_keys - set(self._material_textures)
        source_note = " - WW normal maps enabled" if ww_normal_maps else " - secondary layers are not normal maps"
        missing_note = f" - {len(unresolved_keys)} keys not found" if unresolved_keys else ""
        if self._material_source_asset is not None:
            self.material_source_label.config(text=(
                f"{self._material_source_asset.name} - {len(self._material_infos)} materials, "
                f"{len(self._material_textures)}/{len(wanted_keys)} texture previews from "
                f"{len(known_keys)} BF textures{source_note}{missing_note}"
            ))
        if error:
            self._log(f"WARN  Material Editor texture index: {error}")

        secondary_missing = (
            unresolved_keys & known_keys
        ) - self._material_texture_decode_misses
        if secondary_missing and self._material_source_asset is not None:
            self._start_material_texture_search(
                self._material_source_asset, secondary_missing, generation, False
            )
            return
        self.status.set("Ready")
        self._log(
            f"OK    Material Editor texture index: {len(known_keys)} textures, "
            f"{len(found_images)} previews added"
        )

    def _selected_material(self) -> MaterialInfo | None:
        selection = self.material_tree.selection()
        if not selection: return None
        try: index = int(selection[0].split("_", 1)[1])
        except (ValueError, IndexError): return None
        return self._material_infos[index] if 0 <= index < len(self._material_infos) else None

    def on_material_selected(self, _event=None) -> None:
        info = self._selected_material()
        if info is None:
            return
        self._material_metallic.set(info.metallic)
        self._material_alpha.set(info.alpha)
        current = f"0x{info.texture_key:08X}" if info.texture_key is not None else "<none>"
        values = list(self.material_diffuse_combo["values"])
        self.material_diffuse_combo.set(current if current in values else "")
        secondary = f"0x{info.secondary_key:08X}" if info.secondary_key is not None else ""
        normal = f"0x{info.normal_key:08X}" if info.normal_key is not None else ""
        self._material_secondary_path.set(secondary)
        self._material_normal_path.set(normal)
        layout = f"Jade kind {info.material_kind}" if info.material_kind is not None else "POP type-5"
        normal_note = normal or ("<not used outside WW_FULL_GAME_windows / WW_DEMO_xbox>" if secondary else "<none>")
        self.material_info.config(text=(f"Material 0x{info.material_key:08X} • {layout}\n"
                                        f"Diffuse: {current} • Secondary: {secondary or '<none>'} • Normal: {normal_note}\n"
                                        f"Source meshes: {len(info.source_meshes or [])}"))
        self._material_preview_changed()

    def _material_preview_changed(self, *_args) -> None:
        self.material_metal_value.config(text=f"{float(self._material_metallic.get()):.2f}")
        self.material_alpha_value.config(text=f"{float(self._material_alpha.get()):.2f}")
        self.material_projection_value.config(text=f"{float(self._material_projection.get()):.2f}×")
        if not isinstance(self.material_canvas, MaterialViewport): return
        info = self._selected_material()
        if info is None: return
        selected = self.material_diffuse_combo.get()
        image = None
        if selected.startswith("0x"):
            try: image = self._material_textures.get(int(selected, 16))
            except ValueError: pass
            self.material_canvas.set_image(image)
            self.material_info.config(text=(f"Material 0x{info.material_key:08X} • Jade material record\n"
                                             f"Diffuse: {selected or '<none>'} • source meshes: {len(info.source_meshes or [])}\n"
                                             "Opacity and specular exponent are written back to the selected Jade material."))

    def _open_material_texture(self, slot: str) -> None:
        info = self._selected_material()
        if info is None or self._material_source_asset is None:
            messagebox.showinfo("Texture Editor", "Select a material first.")
            return
        key_by_slot = {"diffuse": info.texture_key, "secondary": info.secondary_key, "normal": info.normal_key}
        labels = {"diffuse": "diffuse texture", "secondary": "secondary texture", "normal": "normal map"}
        key = key_by_slot.get(slot)
        if key is None:
            messagebox.showinfo("Texture Editor", f"This material has no {labels.get(slot, 'texture')}.")
            return
        texture_asset = self._material_texture_sources.get(key)
        if texture_asset is None:
            messagebox.showwarning("Texture Editor", f"Texture 0x{key:08X} not found in the BF archive.")
            return
        previous = self.asset_tree.selection()
        try:
            target_iid = next((iid for iid, candidate in self._asset_map.items() if candidate.index == texture_asset.index), None)
            if target_iid is None:
                raise RuntimeError("Texture asset is unavailable in the filtered list.")
            self.asset_tree.selection_set(target_iid)
            self.scan_textures()
        finally:
            if previous:
                self.asset_tree.selection_set(previous)
        match = next((i for i, tex in enumerate(self._texture_infos) if tex.key == key), None)
        if match is None:
            messagebox.showwarning("Texture Editor", f"Texture 0x{key:08X} not found in the selected entry.")
            return
        iid = f"tex_{match}"
        self.texture_tree.selection_set(iid)
        self.texture_tree.focus(iid)
        self.texture_tree.see(iid)
        self.on_texture_selected()

    def choose_material_normal(self) -> None:
        path = filedialog.askopenfilename(title="Choose normal map", filetypes=[
            ("Images", "*.dds *.tga *.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not path: return
        self._material_normal_path.set(path)
        self._material_preview_changed()

    def apply_material_changes(self) -> bool:
        info = self._selected_material()
        if info is None or self._material_source_asset is None:
            messagebox.showinfo("Material Swap", "Scan and select a material first.")
            return False
        try:
            if info.texture_offset is not None:
                selected = self.material_diffuse_combo.get()
                if selected.startswith("0x"):
                    struct.pack_into("<I", self._material_data, info.texture_offset, int(selected, 16))
                    info.texture_key = int(selected, 16)
            specular_offset = info.specular_exponent_offset or info.specular_offset
            if specular_offset is not None:
                value = float(self._material_metallic.get())
                struct.pack_into("<f", self._material_data, specular_offset, value)
                info.specular_exponent = info.metallic = value
            if info.opacity_offset is not None:
                opacity = float(self._material_alpha.get())
                struct.pack_into("<f", self._material_data, info.opacity_offset, opacity)
                info.opacity = info.alpha = opacity
            self._material_dirty = self._material_data != bytearray(self._material_original)
            if not self._material_dirty:
                messagebox.showinfo("Material Swap", "No material changes to save.")
                return True
            self._log(f"APPLY Material changes: 0x{info.material_key:08X}")
            return True
        except Exception as exc:
            self._log(f"ERROR Material apply: {exc}")
            messagebox.showerror("Material Swap", str(exc))
            return False

    def save_material_changes_as_bin(self) -> None:
        if not self.apply_material_changes():
            return
        if not self._material_dirty or self._material_source_asset is None:
            return
        try:
            target = filedialog.asksaveasfilename(title="Save material changes as BIN", defaultextension=".bin",
                                                  filetypes=[("BIN", "*.bin"), ("All files", "*.*")])
            if not target: return
            if self.project.kind == "bin":
                self.project.save_bin_as(Path(target), bytes(self._material_data))
            else:
                self.project.replace_bf_entry(self._material_source_asset, bytes(self._material_data), Path(target))
            self._material_original = bytes(self._material_data)
            self._material_dirty = False
            self._log(f"OK    Material changes saved as BIN: {target}")
            messagebox.showinfo("Material Swap", f"Created:\n{target}")
        except Exception as exc:
            self._log(f"ERROR Material BIN save: {exc}")
            messagebox.showerror("Material Swap", str(exc))

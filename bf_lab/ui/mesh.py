"""Mesh Editor controls, preview and import/export actions."""
from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import jade_mesh
import math
import queue
import re
import struct
import threading
import tkinter as tk
from ..materials import (
    _associate_mesh_material_packs,
    _jade_color_rgba,
    _scan_pop_material_records,
    _scan_pop_materials,
)
from ..mesh_import import (
    _build_static_mesh_replacement,
    _load_mesh_for_swap,
    _mesh_rli_replacements,
)
from ..mesh_uv import _jade_uv_to_standard
from ..mesh_export import _mesh_obj_lines
from ..mesh_parser import _scan_pop_meshes
from ..models import (
    Asset,
    MeshInfo,
    TextureInfo,
)
from ..project import JadeProject
from ..resources import _parse_pop_file_entries
from ..textures import (
    _decode_pop_texture_image,
    _scan_pop_textures,
)
from ..viewports import (
    MeshViewport,
    OpenGLFrame,
)


class MeshEditorMixin:
    def _build_mesh_tab(self) -> None:
        top = ttk.Frame(self.mesh_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Mesh Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_meshes).pack(side="right")
        ttk.Button(top, text="Export mesh", command=self.export_mesh).pack(side="right", padx=6)
        ttk.Button(top, text="Import GLB / OBJ...", command=self.import_swap_mesh).pack(side="right", padx=6)
        ttk.Button(top, text="Apply mesh changes", command=self.apply_mesh_changes, style="Apply.TButton").pack(side="right", padx=6)
        self.mesh_source_label = ttk.Label(self.mesh_tab, text="No meshes scanned")
        self.mesh_source_label.pack(anchor="w", pady=(4, 8))

        options = ttk.Frame(self.mesh_tab)
        options.pack(fill="x", pady=(0, 6))
        self._mesh_fit_target = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Fit size and center to the original mesh", variable=self._mesh_fit_target).pack(side="left")
        ttk.Label(options, text="OBJ axes (before importing):").pack(side="left", padx=(16, 4))
        self._mesh_obj_axes = tk.StringVar(value="Jade Z-up")
        ttk.Combobox(options, textvariable=self._mesh_obj_axes, values=("Jade Z-up", "glTF Y-up"), state="readonly", width=13).pack(side="left")
        split = ttk.Panedwindow(self.mesh_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=1)
        split.add(right, weight=4)

        ttk.Label(left, text="Meshes in the selected file").pack(anchor="w")
        mesh_tree_frame = ttk.Frame(left)
        mesh_tree_frame.pack(fill="both", expand=True, pady=(5, 0))
        self.mesh_tree = ttk.Treeview(mesh_tree_frame, columns=("name", "verts", "faces", "key", "kind"), show="headings")
        for c, t, w in (("name", "Mesh", 190), ("verts", "Vertices", 80), ("faces", "Faces", 80), ("key", "Mesh ID", 105), ("kind", "Type", 120)):
            self.mesh_tree.heading(c, text=t)
            self.mesh_tree.column(c, width=w, anchor="w")
        self.mesh_tree.pack(side="left", fill="both", expand=True)
        mesh_scrollbar = ttk.Scrollbar(mesh_tree_frame, orient="vertical", command=self.mesh_tree.yview)
        mesh_scrollbar.pack(side="right", fill="y")
        self.mesh_tree.configure(yscrollcommand=mesh_scrollbar.set)
        self.mesh_tree.bind("<<TreeviewSelect>>", self.on_mesh_selected)

        preview_split = ttk.Panedwindow(right, orient="vertical")
        preview_split.pack(fill="both", expand=True)

        selected_row = ttk.Panedwindow(preview_split, orient="horizontal")
        replacement_row = ttk.Panedwindow(preview_split, orient="horizontal")
        preview_split.add(selected_row, weight=1)
        preview_split.add(replacement_row, weight=1)

        preview_box = ttk.LabelFrame(selected_row, text="Selected 3D mesh", padding=6)
        materials_box = ttk.LabelFrame(selected_row, text="Materials of selected mesh", padding=5)
        selected_row.add(preview_box, weight=4)
        selected_row.add(materials_box, weight=1)

        materials_box.columnconfigure(0, weight=1)
        materials_box.rowconfigure(0, weight=1)
        self.mesh_material_canvas = tk.Canvas(
            materials_box, height=132, highlightthickness=0,
            bg=self._dark["field"]
        )
        mesh_material_scrollbar = ttk.Scrollbar(
            materials_box, orient="vertical", command=self.mesh_material_canvas.yview
        )
        self.mesh_material_canvas.grid(row=0, column=0, sticky="nsew")
        mesh_material_scrollbar.grid(row=0, column=1, sticky="ns")
        self.mesh_material_canvas.configure(yscrollcommand=mesh_material_scrollbar.set)
        self.mesh_material_rows = ttk.Frame(self.mesh_material_canvas)
        self._mesh_material_rows_window = self.mesh_material_canvas.create_window(
            (0, 0), window=self.mesh_material_rows, anchor="nw"
        )
        self.mesh_material_rows.bind(
            "<Configure>",
            lambda _event: self.mesh_material_canvas.configure(
                scrollregion=self.mesh_material_canvas.bbox("all")
            )
        )
        self.mesh_material_canvas.bind(
            "<Configure>",
            lambda event: self.mesh_material_canvas.itemconfigure(
                self._mesh_material_rows_window, width=event.width
            )
        )
        ttk.Label(self.mesh_material_rows, text="Select a mesh to list its materials.").pack(
            anchor="w", padx=4, pady=4
        )

        replacement_box = ttk.LabelFrame(replacement_row, text="Imported replacement mesh", padding=6)
        imported_materials_box = ttk.LabelFrame(
            replacement_row, text="Materials of imported mesh", padding=5
        )
        replacement_row.add(replacement_box, weight=4)
        replacement_row.add(imported_materials_box, weight=1)

        imported_materials_box.columnconfigure(0, weight=1)
        imported_materials_box.rowconfigure(0, weight=1)
        self.swap_mesh_material_tree = ttk.Treeview(
            imported_materials_box,
            columns=("slot", "faces", "texture"),
            show="headings",
            height=6,
        )
        for column, title, width in (
            ("slot", "Slot", 55),
            ("faces", "Faces", 65),
            ("texture", "Texture", 105),
        ):
            self.swap_mesh_material_tree.heading(column, text=title)
            self.swap_mesh_material_tree.column(column, width=width, minwidth=45, anchor="w")
        swap_material_scrollbar = ttk.Scrollbar(
            imported_materials_box,
            orient="vertical",
            command=self.swap_mesh_material_tree.yview,
        )
        self.swap_mesh_material_tree.configure(yscrollcommand=swap_material_scrollbar.set)
        self.swap_mesh_material_tree.grid(row=0, column=0, sticky="nsew")
        swap_material_scrollbar.grid(row=0, column=1, sticky="ns")
        self.swap_mesh_material_tree.insert("", "end", values=("-", "-", "Import a mesh"))

        if OpenGLFrame is not None:
            self.mesh_canvas = MeshViewport(preview_box, self, highlightthickness=0, bd=0)
            self.mesh_canvas.pack(fill="both", expand=True)
            self.swap_mesh_canvas = MeshViewport(replacement_box, self, highlightthickness=0, bd=0)
            self.swap_mesh_canvas.pack(fill="both", expand=True)
        else:
            self.mesh_canvas = tk.Canvas(preview_box, bg=self._dark["field"], highlightthickness=0, cursor="hand2")
            self.mesh_canvas.pack(fill="both", expand=True)
            self.mesh_canvas.bind("<ButtonPress-1>", self._mesh_mouse_down)
            self.mesh_canvas.bind("<B1-Motion>", self._mesh_mouse_drag)
            self.mesh_canvas.bind("<ButtonRelease-1>", self._mesh_mouse_up)
            self.mesh_canvas.bind("<MouseWheel>", self._mesh_mouse_wheel)
            self.mesh_canvas.bind("<Button-4>", lambda e: self._mesh_zoom_by(1.1))
            self.mesh_canvas.bind("<Button-5>", lambda e: self._mesh_zoom_by(1 / 1.1))
            self.mesh_canvas.bind("<Configure>", lambda _e: self._schedule_mesh_render())
            self.swap_mesh_canvas = tk.Label(replacement_box, text="OpenGL unavailable", anchor="center")
            self.swap_mesh_canvas.pack(fill="both", expand=True)
        self.swap_mesh_info = ttk.Label(replacement_box, text="Import a GLB or OBJ. BF rig and weights are transferred automatically for characters.", justify="left", wraplength=750)
        self.swap_mesh_info.pack(anchor="w", pady=(6, 0))
        replacement_box.bind("<Configure>", lambda e: self.swap_mesh_info.configure(wraplength=max(250, e.width - 24)))

        info_box = ttk.LabelFrame(right, text="Mesh info", padding=8)
        info_box.pack(fill="x", pady=(8, 0))
        self.mesh_info = ttk.Label(info_box, text="Select a mesh to view it.", justify="left")
        self.mesh_info.pack(anchor="w")

    def _collect_mesh_render_resources(self, preferred_asset: Asset, data: bytes, meshes: list[MeshInfo]) -> tuple[dict[int, object], dict[int, dict[int, int]], dict[int, dict[int, tuple[float, float, float, float]]], dict[int, list[tuple[int, int | None]]], int]:
        """Resolve local mesh resources first, then search only unresolved keys."""
        local_inventory: dict[int, TextureInfo] = {}
        for texture in _scan_pop_textures(data):
            current = local_inventory.get(texture.key)
            if current is None or texture.data_end - texture.data_offset > current.data_end - current.data_offset:
                local_inventory[texture.key] = texture

        valid_texture_keys = set(local_inventory)
        packs, legacy_materials = _scan_pop_materials(data)
        records = _scan_pop_material_records(data, valid_texture_keys)

        material_textures: dict[int, dict[int, int]] = {}
        material_colors: dict[int, dict[int, tuple[float, float, float, float]]] = {}
        material_keys: dict[int, list[tuple[int, int | None]]] = {}
        wanted_keys: set[int] = set()
        for mesh in meshes:
            texture_map: dict[int, int] = {}
            color_map: dict[int, tuple[float, float, float, float]] = {}
            mesh_material_keys: list[tuple[int, int | None]] = []
            pack: list[int] = []
            if mesh.material_pack_key is not None:
                pack = packs.get(mesh.material_pack_key, [])
                if not pack and (mesh.material_pack_key in records or mesh.material_pack_key in legacy_materials):
                    pack = [mesh.material_pack_key]
            for material_id, _count in mesh.material_ids:
                material_key = None
                if pack:
                    pack_index = 0 if material_id < 0 else min(material_id, len(pack) - 1)
                    material_key = pack[pack_index]

                record = records.get(material_key) if material_key is not None else None
                texture_key = record.texture_key if record is not None else legacy_materials.get(material_key)
                if texture_key is None:
                    direct_record = records.get(material_id)
                    if direct_record is not None:
                        record = direct_record
                        material_key = material_id
                        texture_key = direct_record.texture_key
                    else:
                        texture_key = legacy_materials.get(material_id)

                mesh_material_keys.append((material_id, material_key))

                if texture_key not in (None, 0, 0xFFFFFFFF):
                    texture_map[material_id] = texture_key
                    wanted_keys.add(texture_key)
                if record is not None:
                    red, green, blue, alpha = _jade_color_rgba(record.diffuse_color)
                    color_map[material_id] = (
                        red, green, blue, max(0.0, min(1.0, alpha * record.alpha))
                    )
                else:
                    color_map[material_id] = (1.0, 1.0, 1.0, 1.0)
            material_textures[mesh.key] = texture_map
            material_colors[mesh.key] = color_map
            material_keys[mesh.key] = mesh_material_keys

        images: dict[int, object] = {}
        for texture_key in wanted_keys:
            texture = local_inventory.get(texture_key)
            if texture is None:
                continue
            try:
                images[texture_key] = _decode_pop_texture_image(data, texture)
            except Exception as exc:
                self._log(f"WARN  Mesh texture 0x{texture_key:08X}: {exc}")

        project_path = self.project.path
        if self._mesh_cache_project != project_path:
            self._mesh_external_texture_cache.clear()
            self._mesh_external_texture_misses.clear()
            self._mesh_cache_project = project_path
        missing = wanted_keys - set(images)
        for texture_key in tuple(missing):
            cached = self._mesh_external_texture_cache.get(texture_key)
            if cached is not None:
                images[texture_key] = cached
                missing.discard(texture_key)

        self._mesh_pending_texture_keys = missing - self._mesh_external_texture_misses
        return images, material_textures, material_colors, material_keys, len(wanted_keys - set(images))

    def _start_mesh_texture_search(self, preferred_asset: Asset, texture_keys: set[int], generation: int) -> None:
        """Resolve cross-asset texture references without blocking Tk's UI thread."""
        if not texture_keys or self.project.kind != "bf" or self.project.path is None:
            return
        project_path = self.project.path
        preferred_index = preferred_asset.index
        wanted = set(texture_keys)
        self.status.set(f"Ready - searching for {len(wanted)} external textures in background...")

        def worker() -> None:
            found: dict[int, object] = {}
            remaining = set(wanted)
            error = ""
            try:
                background_project = JadeProject()
                background_project.open_bf(project_path)
                for candidate in background_project.assets:
                    if candidate.index == preferred_index:
                        continue
                    try:
                        candidate_data = background_project.read_asset(candidate)
                        candidate_textures = _scan_pop_textures(candidate_data)
                    except Exception:
                        continue
                    for texture in candidate_textures:
                        if texture.key not in remaining or texture.data_end - texture.data_offset <= 4:
                            continue
                        try:
                            found[texture.key] = _decode_pop_texture_image(candidate_data, texture)
                        except Exception:
                            continue
                        remaining.discard(texture.key)
                    if not remaining:
                        break
            except Exception as exc:
                error = str(exc)
            self._mesh_texture_search_queue.put(
                (generation, project_path, found, remaining, error)
            )

        threading.Thread(target=worker, name="mesh-texture-search", daemon=True).start()
        self.after(100, lambda: self._poll_mesh_texture_search(generation))

    def _poll_mesh_texture_search(self, generation: int) -> None:
        if generation != self._mesh_texture_search_generation:
            return
        result = None
        while result is None:
            try:
                candidate = self._mesh_texture_search_queue.get_nowait()
            except queue.Empty:
                self.after(100, lambda: self._poll_mesh_texture_search(generation))
                return
            if candidate[0] == generation:
                result = candidate
        result_generation, project_path, found, remaining, error = result
        if result_generation != generation or project_path != self.project.path:
            return
        self._mesh_external_texture_cache.update(found)
        self._mesh_external_texture_misses.update(remaining)
        self._mesh_textures.update(found)
        if found:
            self.on_mesh_selected()
        if self._mesh_source_asset is not None:
            missing_note = f" - {len(remaining)} unresolved textures" if remaining else ""
            self.mesh_source_label.config(text=(
                f"{self._mesh_source_asset.name} - {len(self._mesh_infos)} viewable meshes, "
                f"{len(self._mesh_textures)} material textures resolved{missing_note}"
            ))
        if error:
            self._log(f"WARN  Background mesh texture search: {error}")
        self.status.set("Ready")
        self._log(
            f"OK    Mesh texture search: {len(found)} found, {len(remaining)} unresolved"
        )

    def scan_meshes(self) -> None:
        asset = self._selected_asset()
        if asset is None:
            messagebox.showinfo("Mesh Swap", "Select a .wow/.bin/.gao asset in the browser first.")
            return
        try:
            self._mesh_texture_search_generation += 1
            search_generation = self._mesh_texture_search_generation
            self.status.set("Mesh Editor: reading meshes, materials and local textures...")
            self.update_idletasks()
            data = self.project.read_asset(asset)
            meshes = _scan_pop_meshes(data)
            _associate_mesh_material_packs(data, meshes)
            images, texture_maps, color_maps, material_keys, unresolved = self._collect_mesh_render_resources(asset, data, meshes)
            self._mesh_data = bytearray(data)
            self._mesh_infos = meshes
            self._mesh_source_asset = asset
            self._mesh_textures = images
            self._mesh_material_textures_by_mesh = texture_maps
            self._mesh_material_colors_by_mesh = color_maps
            self._mesh_material_keys_by_mesh = material_keys
            self._mesh_material_textures = texture_maps.get(meshes[0].key, {}) if meshes else {}

            self._clear_tree(self.mesh_tree)
            for i, mesh in enumerate(meshes):
                name = mesh.object_name or f"Mesh #{i + 1}"
                self.mesh_tree.insert("", "end", iid=f"mesh_{i}",
                                      values=(name, f"{len(mesh.vertices):,}", f"{len(mesh.faces):,}", f"0x{mesh.key:08X}", mesh.layout_name or "Prototype"))
            missing_note = f" - {unresolved} unresolved textures" if unresolved else ""
            self.mesh_source_label.config(text=(f"{asset.name} - {len(meshes)} viewable meshes, "
                                                f"{len(images)} material textures resolved{missing_note}"))
            self.mesh_info.config(text="Select a mesh. Drag to rotate; use the mouse wheel to zoom.")
            self.tabs.select(self.mesh_tab)
            self._mesh_yaw = -0.45
            self._mesh_pitch = 0.18
            self._mesh_zoom = 1.0
            if meshes:
                self.mesh_tree.selection_set("mesh_0")
                self.mesh_tree.focus("mesh_0")
                self.on_mesh_selected()
            self.status.set("Ready")
            self._log(f"OK    Mesh scan: {asset.name} -> {len(meshes)} meshes, {len(images)} material textures resolved")
            self._start_mesh_texture_search(asset, self._mesh_pending_texture_keys, search_generation)
        except Exception as exc:
            self.status.set("Ready")
            self._log(f"ERROR Mesh scan: {exc}")
            messagebox.showerror("Mesh Swap", str(exc))

    def import_swap_mesh(self) -> None:
        source = filedialog.askopenfilename(
            title="Import replacement mesh",
            filetypes=[("3D mesh", "*.glb *.obj"), ("glTF Binary", "*.glb"), ("Wavefront OBJ", "*.obj")]
        )
        if not source:
            return
        try:
            path = Path(source)
            mesh, textures, material_textures, material_colors = _load_mesh_for_swap(path, self._mesh_obj_axes.get())
            self._swap_mesh = mesh
            self._swap_mesh_path = path
            self._swap_mesh_textures = textures
            self._swap_mesh_material_textures = material_textures
            self._swap_mesh_material_colors = material_colors
            if OpenGLFrame is not None and isinstance(self.swap_mesh_canvas, MeshViewport):
                self.swap_mesh_canvas.set_scene(mesh, textures, material_textures, material_colors)
            self._refresh_imported_mesh_material_list(mesh, material_textures)
            self.swap_mesh_info.config(text=(
                f"{path.name} - {len(mesh.vertices):,} vertices - {len(mesh.faces):,} faces - "
                f"{len(material_colors)} materials - {len(textures)} textures\n"
                f"{mesh.layout_name}; {len(mesh.source_joint_names)} source joints. "
                "Imported materials/textures are previewed only; Apply retains those from the BF. "
                "BF character: original rig with weights recalculated by proximity. Static target: geometry only. Export in the rest pose."
            ))
            self._log(f"OK    Mesh replacement import: {path.name} -> {len(mesh.vertices)} vertices, {len(mesh.faces)} faces")
        except Exception as exc:
            self._swap_mesh = None
            self._swap_mesh_path = None
            self._swap_mesh_textures = {}
            self._swap_mesh_material_textures = {}
            self._swap_mesh_material_colors = {}
            self._refresh_imported_mesh_material_list(None, {})
            self.swap_mesh_info.config(text=f"Mesh import failed: {exc}")
            self._log(f"ERROR Mesh replacement import: {exc}")
            messagebox.showerror("Import replacement mesh", str(exc))

    def apply_mesh_changes(self) -> bool:
        target = self._selected_mesh()
        asset = self._mesh_source_asset
        if target is None or asset is None or self._swap_mesh is None:
            messagebox.showinfo("Mesh Swap", "Scan and select a mesh, then import a GLB or OBJ.")
            return False
        if not any(a is asset for a in self.project.assets):
            messagebox.showerror("Mesh Swap", "The project has changed: rescan the asset.")
            return False
        try:
            data = self.project.read_asset(asset)
            entries = _parse_pop_file_entries(data)
            entry = entries[target.entry_index]
            if entry.key != target.key:
                raise ValueError("The selection no longer matches the asset: rescan it.")
            candidate = jade_mesh.fitted_mesh(self._swap_mesh, target) if self._mesh_fit_target.get() else self._swap_mesh
            replacement = _build_static_mesh_replacement(data, target, candidate)
            updates = _mesh_rli_replacements(data, target, candidate)
            updates[entry.index] = replacement
            patched = data
            for index, payload in sorted(updates.items(), reverse=True):
                resource = entries[index]
                patched = (patched[:resource.offset] + struct.pack("<III", len(payload), resource.magic, resource.key)
                           + payload + patched[resource.data_offset + resource.size:])
            meshes = _scan_pop_meshes(patched)
            updated = next((m for m in meshes if m.entry_index == target.entry_index), None)
            if updated is None or len(updated.faces) != len(self._swap_mesh.faces):
                raise ValueError("Rebuilt mesh verification failed.")
            _associate_mesh_material_packs(patched, meshes)
            patches = self.project.mesh_patches.setdefault(asset.index, {})
            for index, payload in updates.items():
                resource = entries[index]
                baseline = (patches[index][1] if index in patches else
                            data[resource.data_offset:resource.data_offset + resource.size])
                patches[index] = (resource.key, baseline, payload)
            self.project.modified = True
            self._mesh_data = bytearray(patched)
            self._mesh_infos = meshes
            for i, mesh in enumerate(meshes):
                self.mesh_tree.item(f"mesh_{i}", values=(mesh.object_name or f"Mesh #{i + 1}",
                    f"{len(mesh.vertices):,}", f"{len(mesh.faces):,}", f"0x{mesh.key:08X}", mesh.layout_name or "Prototype"))
            self.on_mesh_selected()
            self.status.set("Mesh applied in memory. Save the BF/BIN/DEC to write it to disk.")
            self.mesh_source_label.config(text=f"{asset.name} - meshes applied in memory, ready to save")
            self._log(f"APPLY Mesh 0x{target.key:08X}: {len(updated.vertices)} vertices, {len(updated.faces)} faces; "
                      f"{len(updated.skin_bones or [])} BF bones retained, weights transferred automatically")
            return True
        except Exception as exc:
            self._log(f"ERROR Mesh apply: {exc}")
            messagebox.showerror("Mesh Swap", str(exc))
            return False

    def _selected_mesh(self) -> MeshInfo | None:
        selection = self.mesh_tree.selection()
        if not selection:
            return None
        try:
            index = int(selection[0].split("_", 1)[1])
        except (ValueError, IndexError):
            return None
        return self._mesh_infos[index] if 0 <= index < len(self._mesh_infos) else None

    def on_mesh_selected(self, _event=None) -> None:
        mesh = self._selected_mesh()
        if mesh is None:
            return
        self._refresh_mesh_material_list(mesh)
        self.mesh_info.config(text=(
            f"Mesh ID 0x{mesh.key:08X} • {len(mesh.vertices):,} vertices • {len(mesh.faces):,} faces • "
            f"version {mesh.version} / {mesh.layout_name or 'prototype layout'} / {len(mesh.skin_bones or [])} bones" + (f" • object: {mesh.object_name}" if mesh.object_name else "")
        ))
        if OpenGLFrame is not None and isinstance(self.mesh_canvas, MeshViewport):
            texture_map = self._mesh_material_textures_by_mesh.get(mesh.key, {})
            color_map = self._mesh_material_colors_by_mesh.get(mesh.key, {})
            self.mesh_canvas.set_scene(mesh, self._mesh_textures, texture_map, color_map)
        else:
            self._render_selected_mesh()

    def _refresh_mesh_material_list(self, mesh: MeshInfo) -> None:
        for child in self.mesh_material_rows.winfo_children():
            child.destroy()

        assignments = self._mesh_material_keys_by_mesh.get(mesh.key, [])
        seen: set[tuple[int, int | None]] = set()
        visible = []
        for material_id, material_key in assignments:
            identity = (material_id, material_key)
            if identity not in seen:
                seen.add(identity)
                visible.append(identity)

        if not visible:
            ttk.Label(self.mesh_material_rows, text="No materials detected for this mesh.").pack(
                anchor="w", padx=4, pady=4
            )
            return

        for material_id, material_key in visible:
            row = ttk.Frame(self.mesh_material_rows, padding=(4, 2))
            row.pack(fill="x")
            key_text = f"0x{material_key:08X}" if material_key is not None else "unresolved"
            ttk.Label(row, text=f"Slot {material_id}: {key_text}").pack(
                side="left", fill="x", expand=True
            )
            ttk.Button(
                row, text="Open in Material editor",
                command=lambda key=material_key: self.open_mesh_material_in_editor(key)
            ).pack(side="right")

        self.mesh_material_canvas.yview_moveto(0.0)

    def _refresh_imported_mesh_material_list(
        self, mesh: MeshInfo | None, material_textures: dict[int, int]
    ) -> None:
        self._clear_tree(self.swap_mesh_material_tree)
        if mesh is None:
            self.swap_mesh_material_tree.insert("", "end", values=("-", "-", "Import a mesh"))
            return

        face_counts: dict[int, int] = {}
        for material_id, count in mesh.material_ids:
            face_counts[material_id] = face_counts.get(material_id, 0) + count
        if not face_counts:
            self.swap_mesh_material_tree.insert("", "end", values=("-", "-", "No materials"))
            return

        for material_id, count in face_counts.items():
            texture_key = material_textures.get(material_id)
            texture_text = f"0x{texture_key:08X}" if texture_key is not None else "none"
            self.swap_mesh_material_tree.insert(
                "", "end", values=(material_id, f"{count:,}", texture_text)
            )

    def open_mesh_material_in_editor(self, material_key: int | None) -> None:
        if material_key is None:
            messagebox.showwarning(
                "Material Editor",
                "This mesh material reference could not be resolved to a material record."
            )
            return
        asset = self._mesh_source_asset
        if asset is None:
            messagebox.showinfo("Material Editor", "Scan a mesh asset first.")
            return
        self.open_material_editor(asset, material_key)

    def _mesh_mouse_down(self, event) -> None:
        self._mesh_drag = (event.x, event.y, self._mesh_yaw, self._mesh_pitch)

    def _mesh_mouse_drag(self, event) -> None:
        if self._mesh_drag is None:
            return
        x0, y0, yaw0, pitch0 = self._mesh_drag
        self._mesh_yaw = yaw0 + (event.x - x0) * 0.012
        self._mesh_pitch = max(-1.35, min(1.35, pitch0 + (event.y - y0) * 0.012))
        self._schedule_mesh_render()

    def _mesh_mouse_up(self, _event) -> None:
        self._mesh_drag = None

    def _mesh_mouse_wheel(self, event) -> None:
        self._mesh_zoom_by(1.1 if event.delta > 0 else 1 / 1.1)

    def _mesh_zoom_by(self, factor: float) -> None:
        self._mesh_zoom = max(0.25, min(4.0, self._mesh_zoom * factor))
        self._schedule_mesh_render()

    def _schedule_mesh_render(self) -> None:
        if self._mesh_render_after is not None:
            try:
                self.after_cancel(self._mesh_render_after)
            except tk.TclError:
                pass
        self._mesh_render_after = self.after(30, self._render_selected_mesh)

    @staticmethod
    def _mesh_affine(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> tuple[float, float, float, float, float, float]:
        # Solve source = A * destination + b for three non-collinear points.
        (x1, y1), (x2, y2), (x3, y3) = dst
        (u1, v1), (u2, v2), (u3, v3) = src
        det = x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)
        if abs(det) < 1e-8:
            raise ValueError("Degenerate texture triangle")
        a = (u1 * (y2 - y3) + u2 * (y3 - y1) + u3 * (y1 - y2)) / det
        b = (u1 * (x3 - x2) + u2 * (x1 - x3) + u3 * (x2 - x1)) / det
        c = (u1 * (x2 * y3 - x3 * y2) + u2 * (x3 * y1 - x1 * y3) + u3 * (x1 * y2 - x2 * y1)) / det
        d = (v1 * (y2 - y3) + v2 * (y3 - y1) + v3 * (y1 - y2)) / det
        e = (v1 * (x3 - x2) + v2 * (x1 - x3) + v3 * (x2 - x1)) / det
        f = (v1 * (x2 * y3 - x3 * y2) + v2 * (x3 * y1 - x1 * y3) + v3 * (x1 * y2 - x2 * y1)) / det
        return a, b, c, d, e, f

    def _render_selected_mesh(self) -> None:
        self._mesh_render_after = None
        mesh = self._selected_mesh()
        texture_map = self._mesh_material_textures_by_mesh.get(mesh.key, {}) if mesh is not None else {}
        if mesh is None or not hasattr(self, "mesh_canvas"):
            return
        try:
            from PIL import Image, ImageDraw, ImageTk
            width = max(320, self.mesh_canvas.winfo_width())
            height = max(260, self.mesh_canvas.winfo_height())
            image = Image.new("RGBA", (width, height), self._dark["field"])
            draw = ImageDraw.Draw(image, "RGBA")
            verts = mesh.vertices
            if not verts or not mesh.faces:
                draw.text((20, 20), "Empty or unviewable mesh", fill=(220, 220, 220, 255))
            else:
                cx = sum(v[0] for v in verts) / len(verts)
                cy = sum(v[1] for v in verts) / len(verts)
                cz = sum(v[2] for v in verts) / len(verts)
                centered = [(x - cx, y - cy, z - cz) for x, y, z in verts]
                radius = max(math.sqrt(x*x + y*y + z*z) for x, y, z in centered) or 1.0
                sy, cyaw = math.sin(self._mesh_yaw), math.cos(self._mesh_yaw)
                sp, cp = math.sin(self._mesh_pitch), math.cos(self._mesh_pitch)
                projected = []
                depth = []
                for x, y, z in centered:
                    x1 = x * cyaw - z * sy
                    z1 = x * sy + z * cyaw
                    y1 = y * cp - z1 * sp
                    z2 = y * sp + z1 * cp
                    camera = 3.0 * radius
                    scale = min(width, height) * 0.42 * self._mesh_zoom
                    perspective = camera / max(0.25, camera + z2)
                    projected.append((width * 0.5 + x1 * scale / radius * perspective,
                                      height * 0.5 - y1 * scale / radius * perspective))
                    depth.append(z2)

                face_records = []
                material_index = 0
                face_end = 0
                for mat_id, count in mesh.material_ids:
                    face_end += count
                    for face_index in range(material_index, min(face_end, len(mesh.faces))):
                        face_records.append(((depth[mesh.faces[face_index][0]] + depth[mesh.faces[face_index][1]] + depth[mesh.faces[face_index][2]]) / 3.0,
                                             face_index, mat_id))
                    material_index = face_end
                if not face_records:
                    face_records = [(sum(depth[i] for i in face) / 3.0, fi, 0) for fi, face in enumerate(mesh.faces)]
                face_records.sort(reverse=True)
                for _z, face_index, mat_id in face_records:
                    a, b, c = mesh.faces[face_index]
                    dst = [projected[a], projected[b], projected[c]]
                    tex_key = texture_map.get(mat_id)
                    tex = self._mesh_textures.get(tex_key)
                    uv = None
                    if mesh.uv_indices and mesh.uvs:
                        ui = mesh.uv_indices[face_index]
                        if all(0 <= i < len(mesh.uvs) for i in ui):
                            uv = [mesh.uvs[i] for i in ui]
                    if tex is not None and uv is not None:
                        tw, th = tex.size
                        # Pillow addresses images from the top-left. Convert
                        # Jade UVs as pop3_importer does, then translate the
                        # standard bottom-left UVs into Pillow pixel space.
                        standard_uv = [_jade_uv_to_standard(value) for value in uv]
                        src = [(u * tw, (1.0 - v) * th) for u, v in standard_uv]
                        minx = max(0, int(min(p[0] for p in dst)) - 1)
                        miny = max(0, int(min(p[1] for p in dst)) - 1)
                        maxx = min(width, int(max(p[0] for p in dst)) + 2)
                        maxy = min(height, int(max(p[1] for p in dst)) + 2)
                        if maxx > minx and maxy > miny:
                            local_dst = [(x - minx, y - miny) for x, y in dst]
                            try:
                                affine = self._mesh_affine(src, local_dst)
                                patch = tex.transform((maxx - minx, maxy - miny), Image.Transform.AFFINE, affine, resample=Image.Resampling.BILINEAR)
                                mask = Image.new("L", patch.size, 0)
                                ImageDraw.Draw(mask).polygon(local_dst, fill=255)
                                image.alpha_composite(Image.composite(patch, Image.new("RGBA", patch.size), mask), (minx, miny))
                            except ValueError:
                                draw.polygon(dst, fill=(150, 150, 150, 255))
                    else:
                        draw.polygon(dst, fill=(145, 150, 160, 255))
                    # A subtle edge makes the geometry readable while keeping
                    # the actual texture visible underneath.
                    draw.line(dst + [dst[0]], fill=(20, 20, 24, 150), width=1, joint="curve")
            self._mesh_texture_photo = ImageTk.PhotoImage(image)
            self.mesh_canvas.delete("all")
            self.mesh_canvas.create_image(width // 2, height // 2, image=self._mesh_texture_photo)
            self.mesh_canvas.create_text(12, height - 14, anchor="w", text="Drag: rotate  •  Wheel: zoom", fill=self._dark["muted"])
        except Exception as exc:
            self.mesh_canvas.delete("all")
            self.mesh_canvas.create_text(12, 12, anchor="nw", text=f"Preview error: {exc}", fill="#ff7777")

    def export_mesh(self) -> None:
        mesh = self._selected_mesh()
        asset = self._mesh_source_asset
        if mesh is None or asset is None:
            messagebox.showinfo("Mesh Swap", "Scan and select a mesh first.")
            return
        try:
            source_path = self.project.path
            if source_path is None:
                raise ValueError("No source .BF/.BIN open.")
            out_dir = source_path.parent
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", mesh.object_name or f"mesh_{mesh.index + 1:03d}").strip("._") or f"mesh_{mesh.index + 1:03d}"
            base = out_dir / f"{source_path.stem}_{safe_name}_{mesh.key:08X}"
            obj_path = base.with_suffix(".obj")
            mtl_path = base.with_suffix(".mtl")
            textures_out: dict[int, Path] = {}
            for texture_key, texture in self._mesh_textures.items():
                tex_path = out_dir / f"{base.name}_tex_{texture_key:08X}.png"
                texture.save(tex_path, "PNG")
                textures_out[texture_key] = tex_path

            mtl_lines = [f"# Exported by PoP BF Lab", f"# Mesh 0x{mesh.key:08X}"]
            texture_map = self._mesh_material_textures_by_mesh.get(mesh.key, {})
            for mat_id, _count in mesh.material_ids:
                tex_key = texture_map.get(mat_id)
                mtl_lines.append(f"newmtl mat_{mat_id}")
                mtl_lines.append("Ka 0.2 0.2 0.2")
                mtl_lines.append("Kd 1.0 1.0 1.0")
                mtl_lines.append("d 1.0")
                if tex_key in textures_out:
                    mtl_lines.append(f"map_Kd {textures_out[tex_key].name}")
                mtl_lines.append("")
            mtl_path.write_text("\n".join(mtl_lines), encoding="utf-8")

            lines = _mesh_obj_lines(mesh, mtl_path.name)
            obj_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._log(f"OK    Mesh export: {obj_path}")
            messagebox.showinfo("Export mesh", f"Created in the source folder:\n{obj_path}\n{mtl_path}\n{len(textures_out)} PNG textures")
        except Exception as exc:
            self._log(f"ERROR Mesh export: {exc}")
            messagebox.showerror("Export mesh", str(exc))

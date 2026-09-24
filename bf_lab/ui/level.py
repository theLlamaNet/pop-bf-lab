"""Level list, scene opening, and in-memory GAO transform application."""
from __future__ import annotations

import struct
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..level_scene import level_asset_kind, scan_level_objects, wol_wow_keys, write_object_transform
from ..level_viewport import LevelWorkspace
from ..materials import _associate_mesh_material_packs
from ..mesh_parser import _scan_pop_meshes
from ..project import JadeProject
from ..resources import _parse_pop_file_entries
from ..textures import _decode_pop_texture_image, _scan_pop_textures


class LevelEditorMixin:
    def _build_level_tab(self):
        top = ttk.Frame(self.level_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Level Editor", font=("Segoe UI Semibold", 16)).pack(side="left")
        self.level_apply_button = ttk.Button(top, text="Apply level changes", style="Apply.TButton",
                                             command=self.apply_level_changes)
        self.level_apply_button.pack(side="right")
        self.level_apply_button.state(["disabled"])
        split = ttk.Panedwindow(self.level_tab, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(12, 0))
        left = ttk.Frame(split)
        right = ttk.Frame(split, padding=14)
        split.add(left, weight=2)
        split.add(right, weight=3)
        ttk.Label(left, text="WOW / WOL levels", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 7))
        search_row = ttk.Frame(left)
        search_row.pack(fill="x", pady=(0, 7))
        ttk.Label(search_row, text="Search:").pack(side="left")
        self.level_search_var = tk.StringVar()
        self.level_search_var.trace_add("write", lambda *_: self._filter_levels())
        ttk.Entry(search_row, textvariable=self.level_search_var).pack(side="left", fill="x", expand=True, padx=(7, 0))
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.level_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.level_tree.yview)
        self.level_tree.configure(yscrollcommand=scroll.set)
        self.level_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.level_tree.bind("<<TreeviewSelect>>", self._level_selected)
        self.level_tree.bind("<Double-1>", lambda _e: self.open_level_workspace())
        ttk.Label(right, text="Workspace", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        self.level_selection_label = ttk.Label(right, text="Select a WOW or WOL level from the list.")
        self.level_selection_label.pack(anchor="w", pady=(7, 14))
        self.level_open_button = ttk.Button(right, text="Open in Level editor workspace",
                                            command=self.open_level_workspace)
        self.level_open_button.pack(anchor="w")
        self.level_open_button.state(["disabled"])
        self.level_status_label = ttk.Label(right, text="", wraplength=550)
        self.level_status_label.pack(anchor="w", pady=(14, 0))
        self._level_assets = {}
        self._level_all_assets = []
        self._level_workspaces = []
        self._level_sessions = []
        self._level_project_token = (id(self.project), self.project.path, self.project.kind)
        self._level_texture_generation = 0
        self._level_texture_queue = queue.Queue()

    def refresh_levels(self):
        if not hasattr(self, "level_tree"):
            return
        project_token = (id(self.project), self.project.path, self.project.kind)
        if self._level_project_token != project_token:
            for workspace in self._level_workspaces[:]:
                if workspace.winfo_exists():
                    workspace.destroy()
            self._level_sessions.clear()
            self._level_texture_generation += 1
            self._level_project_token = project_token
        self._level_all_assets = []
        for asset in self.project.assets:
            kind = level_asset_kind(asset.name)
            if kind is None and self.project.kind in ("bin", "dec"):
                # A standalone BIN/DEC may carry a level header despite a generic file name.
                data = self.project.decoded_bin or b""
                if b".wow" in data[:4096]:
                    kind = "wow"
                elif b".wol" in data[:4096]:
                    kind = "wol"
            if kind is None:
                continue
            self._level_all_assets.append((kind, asset))
        self._filter_levels()
        self.level_apply_button.state(["disabled"])
        self._level_workspace_changed()

    def _filter_levels(self):
        if not hasattr(self, "level_tree"):
            return
        selection = self.level_tree.selection()
        previous = selection[0] if selection else None
        self._clear_tree(self.level_tree)
        self._level_assets = {}
        needle = self.level_search_var.get().strip().casefold()
        by_kind = {kind: self.level_tree.insert("", "end", text=kind.upper(), open=True)
                   for kind in ("wow", "wol")}
        for kind, asset in self._level_all_assets:
            if needle and needle not in asset.name.casefold():
                continue
            iid = f"level_{asset.index}"
            self._level_assets[iid] = asset
            self.level_tree.insert(by_kind[kind], "end", iid=iid, text=asset.name)
        if previous in self._level_assets:
            self.level_tree.selection_set(previous)
            self.level_tree.see(previous)
        self._level_selected()
        self.level_status_label.config(text=f"{len(self._level_assets)} of {len(self._level_all_assets)} levels shown")

    def _selected_level_asset(self):
        selection = self.level_tree.selection()
        return self._level_assets.get(selection[0]) if selection else None

    def _level_selected(self, _event=None):
        asset = self._selected_level_asset()
        self.level_open_button.state(["!disabled"] if asset else ["disabled"])
        self.level_selection_label.config(text=asset.name if asset else "Select a WOW or WOL level from the list.")

    def open_level_workspace(self):
        asset = self._selected_level_asset()
        if asset is None:
            return
        try:
            sources = [asset]
            data = self.project.read_asset(asset)
            if level_asset_kind(asset.name) == "wol":
                wanted = wol_wow_keys(data)
                matches = [a for a in self.project.assets
                           if (((a.key & 0x00FF0000) << 8) | (a.key & 0xFFFF)) in wanted
                           and level_asset_kind(a.name) == "wow"]
                sources.extend(matches)
            objects = []
            meshes = []
            images = {}
            texture_maps = {}
            color_maps = {}
            mesh_links = {}
            missing_textures = set()
            for source in sources:
                payload = self.project.read_asset(source)
                local_objects = scan_level_objects(payload, source.index)
                objects.extend(local_objects)
                found = _scan_pop_meshes(payload)
                _associate_mesh_material_packs(payload, found)
                mesh_keys = {mesh.key for mesh in found}
                entries_by_key = {entry.key: entry for entry in _parse_pop_file_entries(payload)}
                for obj in local_objects:
                    links = [obj.mesh_key] if obj.mesh_key in mesh_keys else []
                    if not links and obj.mesh_key in entries_by_key:
                        group = entries_by_key[obj.mesh_key]
                        group_data = payload[group.data_offset:group.data_offset + group.size]
                        links = [key for key in mesh_keys if struct.pack("<I", key) in group_data]
                    mesh_links[id(obj)] = links
                if found:
                    local_images, local_maps, local_colors, _keys, _missing = self._collect_mesh_render_resources(source, payload, found)
                    images.update(local_images)
                    texture_maps.update(local_maps)
                    color_maps.update(local_colors)
                    meshes.extend(found)
                    missing_textures.update(self._mesh_pending_texture_keys)
            if not objects:
                raise ValueError("No readable game objects were found in this level.")
            workspace = LevelWorkspace(self, asset.name, objects, meshes, images, texture_maps,
                                       color_maps, mesh_links, self._level_workspace_changed,
                                       self.open_level_mesh_in_editor)
            self._level_sessions.append(objects)
            self._level_workspaces.append(workspace)
            missing_textures.difference_update(images)
            self._search_level_textures(workspace, missing_textures)
            workspace.bind("<Destroy>", lambda e, w=workspace: self._level_workspaces.remove(w)
                           if e.widget is w and w in self._level_workspaces else None)
            self.level_status_label.config(text=f"Opened {len(objects)} objects and {len(meshes)} meshes from {len(sources)} asset(s).")
            self._log(f"OK    Level workspace: {asset.name} — {len(objects)} objects, {len(meshes)} meshes")
        except Exception as exc:
            self._log(f"ERROR Level workspace: {exc}")
            messagebox.showerror("Level Editor", str(exc))

    def open_level_mesh_in_editor(self, obj, mesh_key):
        asset = next((a for a in self.project.assets if a.index == obj.source_asset_index), None)
        if asset is None:
            messagebox.showerror("Level Editor", "The source asset for this mesh is no longer open.")
            return
        self.scan_meshes(asset=asset, mesh_key=mesh_key)
        selected = self._selected_mesh()
        if self._mesh_source_asset == asset and selected is not None and selected.key == mesh_key:
            self.deiconify()
            self.lift()
            self.focus_force()

    def _search_level_textures(self, workspace, wanted):
        if not wanted or self.project.kind != "bf" or self.project.path is None:
            return
        self._level_texture_generation += 1
        generation = self._level_texture_generation
        project_path = self.project.path
        wanted = set(wanted)

        def worker():
            found = {}
            try:
                project = JadeProject()
                project.open_bf(project_path)
                for candidate in project.assets:
                    if not wanted:
                        break
                    try:
                        data = project.read_asset(candidate)
                        for texture in _scan_pop_textures(data):
                            if texture.key not in wanted:
                                continue
                            try:
                                found[texture.key] = _decode_pop_texture_image(data, texture)
                                wanted.remove(texture.key)
                            except Exception:
                                pass
                    except Exception:
                        continue
            finally:
                self._level_texture_queue.put((generation, project_path, workspace, found, wanted))

        threading.Thread(target=worker, name="level-texture-search", daemon=True).start()
        self.after(150, lambda: self._poll_level_textures(generation))

    def _poll_level_textures(self, generation):
        if generation != self._level_texture_generation:
            return
        while True:
            try:
                result = self._level_texture_queue.get_nowait()
            except queue.Empty:
                self.after(150, lambda: self._poll_level_textures(generation))
                return
            if result[0] == generation:
                break
        result_generation, project_path, workspace, found, remaining = result
        if result_generation != generation or project_path != self.project.path:
            return
        self._mesh_external_texture_cache.update(found)
        if workspace.winfo_exists() and workspace.viewport is not None:
            workspace.viewport.textures.update(found)
            if workspace.viewport.winfo_ismapped():
                workspace.viewport._display()
        self._log(f"OK    Level textures: {len(found)} external textures found, {len(remaining)} unresolved")

    def _level_workspace_changed(self):
        dirty = any(obj.dirty for objects in self._level_sessions for obj in objects)
        self.level_apply_button.state(["!disabled"] if dirty else ["disabled"])

    def apply_level_changes(self):
        changes = {}
        for objects in self._level_sessions:
            for obj in objects:
                if obj.dirty:
                    changes.setdefault(obj.source_asset_index, []).append(obj)
        if not changes:
            return
        try:
            staged_by_asset = {}
            for asset_index, objects in changes.items():
                asset = next(a for a in self.project.assets if a.index == asset_index)
                before = self.project.read_asset(asset)
                after = before
                for obj in objects:
                    after = write_object_transform(after, obj)
                old_entries = _parse_pop_file_entries(before)
                new_entries = _parse_pop_file_entries(after)
                patches = self.project.mesh_patches.get(asset_index, {})
                additions = {}
                for obj in objects:
                    old = old_entries[obj.entry_index]
                    new = new_entries[obj.entry_index]
                    old_payload = before[old.data_offset:old.data_offset + old.size]
                    new_payload = after[new.data_offset:new.data_offset + new.size]
                    if old_payload == new_payload:
                        continue
                    previous = next((i for i, p in patches.items() if p[0] == old.key and p[2] == old_payload), None)
                    baseline = patches[previous][1] if previous is not None else old_payload
                    patch_index = previous if previous is not None else max((*patches.keys(), *additions.keys()), default=-1) + 1
                    additions[patch_index] = (old.key, baseline, new_payload)
                staged_by_asset[asset_index] = additions
            for asset_index, additions in staged_by_asset.items():
                self.project.mesh_patches.setdefault(asset_index, {}).update(additions)
            self.project.modified = True
            for objects in changes.values():
                for obj in objects:
                    obj.dirty = False
            self._level_workspace_changed()
            self.level_status_label.config(text="Level changes applied in memory. Save the BF/BIN/DEC to write them to disk.")
            self._log(f"APPLY Level: {sum(map(len, changes.values()))} object transforms staged")
        except Exception as exc:
            self._log(f"ERROR Level apply: {exc}")
            messagebox.showerror("Level Editor", str(exc))

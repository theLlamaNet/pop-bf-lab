"""Animation browser and looping OpenGL preview."""
from __future__ import annotations

import bisect
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import jade_mesh
from ..animations import scan_animations
from ..mesh_parser import _scan_pop_meshes
from ..materials import _associate_mesh_material_packs
from ..mesh_export import _inverse_matrix, _character_bone_metadata
from ..project import JadeProject
from ..textures import _scan_pop_textures, _decode_pop_texture_image
from ..viewports import MeshViewport, OpenGLFrame


def _sample(keys, frame, field):
    usable = [key for key in keys if getattr(key, field) is not None]
    if not usable:
        return None
    frames = [key.frame for key in usable]
    index = bisect.bisect_right(frames, frame)
    if index == 0:
        return getattr(usable[0], field)
    if index == len(usable):
        return getattr(usable[-1], field)
    left, right = usable[index - 1], usable[index]
    if left.no_interpolation:
        return getattr(left, field)
    t = (frame - left.frame) / max(1, right.frame - left.frame)
    a, b = getattr(left, field), getattr(right, field)
    if field == "rotation":
        if sum(x*y for x, y in zip(a, b)) < 0:
            b = tuple(-x for x in b)
    result = tuple(x + (y-x)*t for x, y in zip(a, b))
    if field == "rotation":
        length = sum(x*x for x in result) ** 0.5
        return tuple(x / length for x in result) if length else (0, 0, 0, 1)
    return result


def _mul_matrix(a, b):
    return tuple(sum(a[k*4 + row] * b[column*4 + k] for k in range(4))
                 for column in range(4) for row in range(4))


def _transform_point(matrix, point):
    return tuple(sum(matrix[column*4 + row] * point[column]
                     for column in range(3)) + matrix[12 + row]
                 for row in range(3))


def _local_pose(rest_local, translation, rotation, root=False):
    result = list(rest_local)
    if rotation is not None:
        x, y, z, w = rotation
        rotated = (1-2*(y*y+z*z), 2*(x*y+z*w), 2*(x*z-y*w), 0,
                   2*(x*y-z*w), 1-2*(x*x+z*z), 2*(y*z+x*w), 0,
                   2*(x*z+y*w), 2*(y*z-x*w), 1-2*(x*x+y*y), 0,
                   0, 0, 0, 1)
        if root:
            rotated = _mul_matrix(rest_local, rotated)
        result[:12] = rotated[:12]
    if translation is not None:
        # Root bind rotation maps Jade animation space into mesh space.
        # Child translations remain local until the hierarchy is composed.
        result[12:15] = ((translation[2], -translation[1], translation[0])
                         if root else translation)
    return tuple(result)


def _skin_rest(mesh):
    rest = {}
    for bone in mesh.skin_bones or []:
        try:
            world = _inverse_matrix(bone.matrix)
        except ValueError:
            continue
        prepared = []
        ibm = bone.matrix
        for vertex_index, word in bone.weights:
            if not 0 <= vertex_index < len(mesh.vertices):
                continue
            weight = jade_mesh.decode_weight(word)
            if not weight:
                continue
            vertex = mesh.vertices[vertex_index]
            bind_local = tuple(sum(ibm[column*4 + row] * vertex[column]
                                   for column in range(3)) + ibm[12 + row]
                               for row in range(3))
            prepared.append((vertex_index, weight, bind_local))
        rest[bone.index] = (world, tuple(world[12:15]), prepared, ibm)
    return rest


def _bone_poses(mesh, clip, frame, rest, parents=None):
    parents = parents or {}
    transforms = {}
    for track in clip.tracks:
        if track.gizmo == 0xFFFF:
            continue
        translation = _sample(track.keys, frame, "translation")
        rotation = _sample(track.keys, frame, "rotation")
        old_translation, old_rotation = transforms.get(track.gizmo, (None, None))
        transforms[track.gizmo] = (translation if translation is not None else old_translation,
                                    rotation if rotation is not None else old_rotation)
    poses = {}
    visiting = set()
    def world_pose(index):
        if index in poses:
            return poses[index]
        if index in visiting:
            raise ValueError("Cyclic bone hierarchy")
        visiting.add(index)
        bind_world = rest[index][0]
        parent = parents.get(index)
        if parent in rest and parent != index:
            parent_bind = rest[parent][0]
            local_rest = _mul_matrix(rest[parent][3], bind_world)
            parent_world = world_pose(parent)[0]
        else:
            local_rest = bind_world
            parent_world = None
        translation, rotation = transforms.get(index, (None, None))
        local = _local_pose(local_rest, translation, rotation, parent_world is None)
        world = _mul_matrix(parent_world, local) if parent_world is not None else local
        poses[index] = (world, tuple(world[12:15]), rotation)
        visiting.remove(index)
        return poses[index]
    for bone in mesh.skin_bones or []:
        if bone.index in rest:
            world_pose(bone.index)
    return poses


def pose_vertices(mesh, clip, frame, rest=None, parents=None):
    bones = mesh.skin_bones or []
    if not bones:
        return mesh.vertices
    rest = rest if rest is not None else _skin_rest(mesh)
    poses = _bone_poses(mesh, clip, frame, rest, parents)
    sums = [[0.0, 0.0, 0.0, 0.0] for _ in mesh.vertices]
    for bone in bones:
        pose = poses.get(bone.index)
        if pose is None:
            continue
        world, _translation, _rotation = pose
        for vertex_index, weight, bind_local in rest[bone.index][2]:
            target = _transform_point(world, bind_local)
            acc = sums[vertex_index]
            for i in range(3):
                acc[i] += weight * target[i]
            acc[3] += weight
    return [tuple((acc[i] + original[i] * max(0.0, 1.0-acc[3])) / max(1.0, acc[3])
                  for i in range(3)) if acc[3] else original
            for original, acc in zip(mesh.vertices, sums)]


class AnimationEditorMixin:
    def _build_animation_tab(self):
        top = ttk.Frame(self.animation_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Animations Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_animations).pack(side="right")
        self.animation_source_label = ttk.Label(self.animation_tab, text="No animations scanned")
        self.animation_source_label.pack(anchor="w", pady=(4, 8))
        split = ttk.Panedwindow(self.animation_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left, right = ttk.Frame(split), ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=1)
        split.add(right, weight=4)
        ttk.Label(left, text="Animations in the selected file").pack(anchor="w")
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True, pady=(5, 8))
        self.animation_tree = ttk.Treeview(tree_frame, columns=("name", "tracks", "frames", "key"), show="headings")
        for col, title, width in (("name", "Animation", 180), ("tracks", "Tracks", 70), ("frames", "Frames", 70), ("key", "ID", 110)):
            self.animation_tree.heading(col, text=title)
            self.animation_tree.column(col, width=width, anchor="w")
        self.animation_tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.animation_tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.animation_tree.configure(yscrollcommand=scrollbar.set)
        self.animation_tree.bind("<<TreeviewSelect>>", self._animation_selected)
        mesh_header = ttk.Frame(left)
        mesh_header.pack(fill="x")
        ttk.Label(mesh_header, text="Mesh for preview").pack(side="left")
        ttk.Button(mesh_header, text="Choose BIN...", command=self._choose_animation_mesh_bin).pack(side="right")
        self.animation_mesh_source_label = ttk.Label(left, text="No mesh BIN selected")
        self.animation_mesh_source_label.pack(anchor="w", pady=(3, 0))
        mesh_frame = ttk.Frame(left)
        mesh_frame.pack(fill="both", expand=True, pady=(5, 0))
        self.animation_mesh_tree = ttk.Treeview(mesh_frame, columns=("name", "verts", "bones", "key"), show="headings", height=7)
        for col, title, width in (("name", "Mesh", 170), ("verts", "Vertices", 75), ("bones", "Bones", 65), ("key", "ID", 110)):
            self.animation_mesh_tree.heading(col, text=title)
            self.animation_mesh_tree.column(col, width=width, anchor="w")
        self.animation_mesh_tree.pack(side="left", fill="both", expand=True)
        mesh_scroll = ttk.Scrollbar(mesh_frame, orient="vertical", command=self.animation_mesh_tree.yview)
        mesh_scroll.pack(side="right", fill="y")
        self.animation_mesh_tree.configure(yscrollcommand=mesh_scroll.set)
        self.animation_mesh_tree.bind("<<TreeviewSelect>>", self._animation_mesh_selected)
        preview = ttk.LabelFrame(right, text="Selected animation", padding=6)
        preview.pack(fill="both", expand=True)
        if OpenGLFrame is not None:
            self.animation_canvas = MeshViewport(preview, self, highlightthickness=0, bd=0)
        else:
            self.animation_canvas = tk.Label(preview, text="OpenGL unavailable", anchor="center")
        self.animation_canvas.pack(fill="both", expand=True)
        scrub = ttk.Frame(right)
        scrub.pack(fill="x", pady=(8, 0))
        self.animation_frame_slider = tk.Canvas(scrub, height=26, bg="#55585e",
                                                highlightthickness=0, cursor="hand2")
        self.animation_frame_slider.pack(side="left", fill="x", expand=True)
        self.animation_speed_slider = tk.Canvas(scrub, width=130, height=26, bg="#394b61",
                                                highlightthickness=0, cursor="hand2")
        self.animation_speed_slider.pack(side="right", padx=(8, 0))
        self.animation_frame_slider.bind("<Configure>", self._draw_animation_sliders)
        self.animation_speed_slider.bind("<Configure>", self._draw_animation_sliders)
        for sequence in ("<Button-1>", "<B1-Motion>"):
            self.animation_frame_slider.bind(sequence, self._seek_animation)
            self.animation_speed_slider.bind(sequence, self._set_animation_speed)
        controls = ttk.Frame(right)
        controls.pack(fill="x", pady=(8, 0))
        self.animation_playing = False
        self.animation_play_button = ttk.Button(controls, text="Play", command=self._toggle_animation_play)
        self.animation_play_button.pack(side="left")
        self.animation_loop = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Loop", variable=self.animation_loop).pack(side="left", padx=(8, 0))
        self.animation_view_mode = tk.StringVar(value="mesh")
        self.animation_view_button = ttk.Button(controls, text="Show rig", command=self._toggle_animation_view)
        self.animation_view_button.pack(side="right")
        self.animation_info = ttk.Label(right, text="Select an animation to play it in a loop.")
        self.animation_info.pack(anchor="w", pady=(6, 0))
        self._animation_clips = []
        self._animation_meshes = []
        self._animation_mesh_asset = None
        self._animation_textures = {}
        self._animation_texture_maps = {}
        self._animation_color_maps = {}
        self._animation_bone_parents = {}
        self._animation_rest = {}
        self._animation_clip = None
        self._animation_mesh = None
        self._animation_after = None
        self._animation_started = 0.0
        self._animation_frame = 0.0
        self._animation_speed = 100
        self._animation_texture_generation = 0
        self._animation_texture_queue = queue.Queue()
        self._animation_texture_search_active = False

    def _stop_animation(self):
        if self._animation_after is not None:
            self.after_cancel(self._animation_after)
            self._animation_after = None
        self._animation_clip = None
        self.animation_playing = False
        self.animation_play_button.configure(text="Play")
        self._draw_animation_sliders()

    def _draw_animation_sliders(self, _event=None):
        frame_bar = self.animation_frame_slider
        speed_bar = self.animation_speed_slider
        frame_bar.delete("all")
        speed_bar.delete("all")
        width = max(1, frame_bar.winfo_width())
        height = max(1, frame_bar.winfo_height())
        end = self._animation_clip.frames if self._animation_clip else 1
        progress = max(0.0, min(1.0, self._animation_frame / end)) if self._animation_clip else 0.0
        x = round(progress * (width - 1))
        frame_bar.create_rectangle(0, 0, width, height, fill="#55585e", outline="")
        frame_bar.create_rectangle(0, 0, x, height, fill="#bd3f43", outline="")
        frame_bar.create_line(x, 1, x, height - 1, fill="#f8e7e7", width=3)
        speed_width = max(1, speed_bar.winfo_width())
        speed_height = max(1, speed_bar.winfo_height())
        speed_x = round((self._animation_speed - 25) / 175 * speed_width)
        speed_bar.create_rectangle(0, 0, speed_width, speed_height,
                                   fill="#394b61", outline="")
        speed_bar.create_rectangle(0, 0, speed_x, speed_height,
                                   fill="#2678c8", outline="")
        speed_bar.create_text(speed_width / 2, speed_height / 2,
                              text=f"{self._animation_speed}%", fill="white",
                              font=("TkDefaultFont", 9, "bold"))

    def _seek_animation(self, event):
        clip = self._animation_clip
        if clip is None:
            return
        width = max(1, self.animation_frame_slider.winfo_width() - 1)
        fraction = max(0.0, min(1.0, event.x / width))
        self._animation_frame = round(fraction * clip.frames)
        self._animation_started = time.monotonic() - self._animation_frame / (60 * self._animation_speed / 100)
        self._animation_tick()

    def _set_animation_speed(self, event):
        width = max(1, self.animation_speed_slider.winfo_width() - 1)
        fraction = max(0.0, min(1.0, event.x / width))
        now = time.monotonic()
        clip = self._animation_clip
        if self.animation_playing and clip is not None:
            elapsed = (now - self._animation_started) * 60 * self._animation_speed / 100
            self._animation_frame = (elapsed % clip.frames if self.animation_loop.get()
                                     else min(elapsed, clip.frames))
        self._animation_speed = max(25, min(200, round((25 + fraction * 175) / 5) * 5))
        self._animation_started = now - self._animation_frame / (60 * self._animation_speed / 100)
        if clip is not None:
            self._animation_tick()
        else:
            self._draw_animation_sliders()

    def _clear_animation_editor(self):
        self._stop_animation()
        self._animation_clips = []
        self._animation_meshes = []
        self._animation_mesh = None
        self._animation_mesh_asset = None
        self._animation_rest = {}
        self._animation_textures = {}
        self._animation_texture_maps = {}
        self._animation_color_maps = {}
        self._animation_texture_generation += 1
        self._animation_texture_search_active = False
        self._clear_tree(self.animation_tree)
        self._clear_tree(self.animation_mesh_tree)
        self.animation_source_label.configure(text="No animations scanned")
        self.animation_mesh_source_label.configure(text="No mesh BIN selected")
        self.animation_info.configure(text="Select an animation to play it in a loop.")
        if hasattr(self.animation_canvas, "set_scene"):
            self.animation_canvas.set_scene(None, {})

    def scan_animations(self, asset=None):
        asset = asset or self._selected_asset()
        if asset is None:
            messagebox.showinfo("Animations Editor", "Select a .wow/.bin asset in the browser first.")
            return
        try:
            self._stop_animation()
            data = self.project.read_asset(asset)
            self._animation_clips = scan_animations(data)
            self._load_animation_meshes(asset, data)
            self._clear_tree(self.animation_tree)
            for index, clip in enumerate(self._animation_clips):
                self.animation_tree.insert("", "end", iid=f"animation_{index}",
                                           values=(f"Animation #{index + 1}", len(clip.tracks),
                                                   clip.frames, f"0x{clip.key:08X}"))
            self.animation_source_label.configure(text=f"{asset.name} - {len(self._animation_clips)} animations")
            self.tabs.select(self.animation_tab)
            if self._animation_clips:
                self.animation_tree.selection_set("animation_0")
                self._animation_selected()
            else:
                self.animation_info.configure(text="No playable TRL animation tracks found in this asset.")
            self._log(f"OK    Animation scan: {asset.name} -> {len(self._animation_clips)} clips")
        except Exception as exc:
            self._log(f"ERROR Animation scan: {exc}")
            messagebox.showerror("Animations Editor", str(exc))

    def _load_animation_meshes(self, asset, data=None):
        self._animation_texture_generation += 1
        self._animation_texture_search_active = False
        data = data if data is not None else self.project.read_asset(asset)
        meshes = [mesh for mesh in _scan_pop_meshes(data) if mesh.skin_bones]
        _associate_mesh_material_packs(data, meshes)
        images, texture_maps, color_maps, _keys, unresolved = self._collect_mesh_render_resources(asset, data, meshes)
        self._animation_mesh_asset = asset
        self._animation_meshes = meshes
        self.animation_mesh_source_label.configure(text=f"{asset.name} - {len(meshes)} skinned meshes")
        self._animation_textures = images
        self._animation_texture_maps = texture_maps
        self._animation_color_maps = color_maps
        self._animation_mesh = None
        self._clear_tree(self.animation_mesh_tree)
        for index, mesh in enumerate(meshes):
            self.animation_mesh_tree.insert("", "end", iid=f"preview_{index}",
                                            values=(mesh.object_name or f"Mesh #{index + 1}",
                                                    len(mesh.vertices), len(mesh.skin_bones or []),
                                                    f"0x{mesh.key:08X}"))
        if not meshes and hasattr(self.animation_canvas, "set_scene"):
            self.animation_canvas.set_scene(None, {})
        if unresolved:
            self._log(f"WARN  Preview mesh textures: {unresolved} unresolved in {asset.name}")
            wanted = {key for mapping in texture_maps.values() for key in mapping.values()}
            self._search_animation_textures(asset, wanted - set(images))

    def _search_animation_textures(self, asset, wanted):
        if not wanted or self.project.kind != "bf" or self.project.path is None:
            return
        self._animation_texture_generation += 1
        generation = self._animation_texture_generation
        self._animation_texture_search_active = True
        project_path = self.project.path
        source_index = asset.index

        def worker():
            found = {}
            remaining = set(wanted)
            try:
                project = JadeProject()
                project.open_bf(project_path)
                for candidate in project.assets:
                    if not remaining or generation != self._animation_texture_generation:
                        break
                    if candidate.index == source_index:
                        continue
                    try:
                        data = project.read_asset(candidate)
                        for texture in _scan_pop_textures(data):
                            if texture.key in remaining:
                                found[texture.key] = _decode_pop_texture_image(data, texture)
                                remaining.remove(texture.key)
                    except Exception:
                        continue
                self._animation_texture_queue.put((generation, found, remaining))
            except Exception as exc:
                self._animation_texture_queue.put((generation, found, remaining, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(200, self._poll_animation_textures)

    def _poll_animation_textures(self):
        while True:
            try:
                result = self._animation_texture_queue.get_nowait()
            except queue.Empty:
                if self._animation_texture_search_active and self.winfo_exists():
                    self.after(200, self._poll_animation_textures)
                return
            generation, found, remaining, *error = result
            if generation == self._animation_texture_generation:
                break
        self._animation_texture_search_active = False
        self._animation_textures.update(found)
        self._mesh_external_texture_cache.update(found)
        if self._animation_mesh and isinstance(self.animation_canvas, MeshViewport):
            self.animation_canvas.textures.update(found)
            if self.animation_canvas.winfo_ismapped():
                self.animation_canvas._display()
        self._log(f"OK    Preview texture search: {len(found)} found, {len(remaining)} unresolved")
        if error:
            self._log(f"WARN  Preview texture search: {error[0]}")

    def _choose_animation_mesh_bin(self):
        if not self.project.assets:
            messagebox.showinfo("Choose BIN", "Open a BF first.")
            return
        dialog = tk.Toplevel(self)
        dialog.title("Choose a BIN containing the preview mesh")
        dialog.geometry("730x530")
        dialog.transient(self)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        filter_var = tk.StringVar()
        ttk.Label(body, text="Filter BINs:").pack(anchor="w")
        filter_entry = ttk.Entry(body, textvariable=filter_var)
        filter_entry.pack(fill="x", pady=(4, 8))
        rows = ttk.Frame(body)
        rows.pack(fill="both", expand=True)
        tree = ttk.Treeview(rows, columns=("name", "size"), show="headings", selectmode="browse")
        tree.heading("name", text="BIN")
        tree.heading("size", text="Size")
        tree.column("name", width=570)
        tree.column("size", width=110)
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(rows, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        candidates = [asset for asset in self.project.assets if asset.name.lower().endswith(".bin")]

        def refresh(*_args):
            tree.delete(*tree.get_children())
            needle = filter_var.get().casefold()
            for index, candidate in enumerate(candidates):
                if needle and needle not in candidate.name.casefold():
                    continue
                tree.insert("", "end", iid=str(index), values=(candidate.name, f"{candidate.size:,}"))

        def choose(_event=None):
            selection = tree.selection()
            if not selection:
                return
            candidate = candidates[int(selection[0])]
            try:
                self._load_animation_meshes(candidate)
                dialog.destroy()
                self._select_best_animation_mesh()
                self._log(f"OK    Animation preview meshes: {candidate.name} -> {len(self._animation_meshes)} skinned meshes")
            except Exception as exc:
                messagebox.showerror("Choose preview BIN", str(exc), parent=dialog)

        filter_var.trace_add("write", refresh)
        refresh()
        filter_entry.focus_set()
        tree.bind("<Double-1>", choose)
        ttk.Button(body, text="Use selected BIN", command=choose).pack(anchor="e", pady=(8, 0))

    def _select_best_animation_mesh(self):
        clip = self._animation_clip
        if clip is None or not self._animation_meshes:
            return
        source = (self.project.read_asset(self._animation_mesh_asset)
                  if self._animation_mesh_asset else b"")
        def score(mesh):
            rest = _skin_rest(mesh)
            try:
                metadata = _character_bone_metadata(source, mesh, require_names=False)
            except (ValueError, IndexError, KeyError, TypeError):
                metadata = {}
            parents = {bone: record[1] for bone, record in metadata.items()}
            matched = [track for track in clip.tracks if track.gizmo in rest]
            distances = []
            for track in matched:
                translation = next((key.translation for key in track.keys
                                    if key.translation is not None), None)
                if translation is None:
                    continue
                parent = parents.get(track.gizmo)
                local = (_mul_matrix(rest[parent][3], rest[track.gizmo][0])
                         if parent in rest else rest[track.gizmo][0])
                expected = ((translation[2], -translation[1], translation[0])
                            if parent not in rest else translation)
                distances.append(math.dist(expected, local[12:15]))
            return (len(matched), -sum(distances) / len(distances) if distances else -1e6)
        index = max(range(len(self._animation_meshes)), key=lambda i: score(self._animation_meshes[i]))
        iid = f"preview_{index}"
        self.animation_mesh_tree.selection_set(iid)
        self.animation_mesh_tree.focus(iid)
        self.animation_mesh_tree.see(iid)
        self._animation_mesh_selected()

    def _animation_selected(self, _event=None):
        selection = self.animation_tree.selection()
        if not selection:
            return
        index = int(selection[0].split("_", 1)[1])
        self._stop_animation()
        self._animation_clip = self._animation_clips[index]
        self._animation_frame = 0.0
        self._animation_started = time.monotonic()
        self._select_best_animation_mesh()
        self._animation_tick()

    def _animation_mesh_selected(self, _event=None):
        selection = self.animation_mesh_tree.selection()
        index = int(selection[0].split("_", 1)[1]) if selection else -1
        self._animation_mesh = self._animation_meshes[index] if 0 <= index < len(self._animation_meshes) else None
        self._animation_rest = _skin_rest(self._animation_mesh) if self._animation_mesh else {}
        self._animation_bone_parents = {}
        if self._animation_mesh and self._animation_mesh_asset:
            try:
                data = self.project.read_asset(self._animation_mesh_asset)
                metadata = _character_bone_metadata(data, self._animation_mesh, require_names=False)
                self._animation_bone_parents = {bone: record[1] for bone, record in metadata.items()}
            except Exception as exc:
                self._log(f"WARN  Animation rig hierarchy: {exc}")
        if self._animation_mesh and isinstance(self.animation_canvas, MeshViewport):
            key = self._animation_mesh.key
            self.animation_canvas.set_scene(self._animation_mesh, self._animation_textures,
                                            self._animation_texture_maps.get(key, {}),
                                            self._animation_color_maps.get(key, {}))
            self.animation_canvas.set_rig_visible(self.animation_view_mode.get() == "rig")
        clip = self._animation_clip
        if clip:
            mesh_name = ((self._animation_mesh.object_name or "Skinned mesh")
                         if self._animation_mesh else "Choose a BIN containing a skinned mesh")
            self.animation_info.configure(text=f"0x{clip.key:08X} • {len(clip.tracks)} tracks • {clip.frames} frames at 60 fps • {mesh_name}")

    def _toggle_animation_play(self):
        if self._animation_clip is None:
            return
        self.animation_playing = not self.animation_playing
        if self.animation_playing:
            if self._animation_frame >= self._animation_clip.frames:
                self._animation_frame = 0.0
            self._animation_started = time.monotonic() - self._animation_frame / (60 * self._animation_speed / 100)
        self.animation_play_button.configure(text="Pause" if self.animation_playing else "Play")

    def _toggle_animation_view(self):
        show_rig = self.animation_view_mode.get() != "rig"
        self.animation_view_mode.set("rig" if show_rig else "mesh")
        self.animation_view_button.configure(text="Show textured mesh" if show_rig else "Show rig")
        if isinstance(self.animation_canvas, MeshViewport):
            self.animation_canvas.set_rig_visible(show_rig)
        self._animation_tick()

    def _animation_tick(self):
        if self._animation_after is not None:
            self.after_cancel(self._animation_after)
        self._animation_after = None
        clip, mesh = self._animation_clip, self._animation_mesh
        if clip is None:
            return
        if self.animation_playing:
            elapsed = (time.monotonic() - self._animation_started) * 60.0 * self._animation_speed / 100
            if self.animation_loop.get():
                self._animation_frame = elapsed % clip.frames
            else:
                self._animation_frame = min(elapsed, clip.frames)
                if elapsed >= clip.frames:
                    self.animation_playing = False
                    self.animation_play_button.configure(text="Play")
        self._draw_animation_sliders()
        if mesh and isinstance(self.animation_canvas, MeshViewport):
            vertices = pose_vertices(mesh, clip, self._animation_frame, self._animation_rest,
                                     self._animation_bone_parents)
            self.animation_canvas.update_vertices(vertices)
            poses = _bone_poses(mesh, clip, self._animation_frame, self._animation_rest,
                                self._animation_bone_parents)
            segments = [(poses[parent][1], pose[1]) for bone, pose in poses.items()
                        if (parent := self._animation_bone_parents.get(bone)) in poses]
            self.animation_canvas.set_rig_segments(segments, [pose[1] for pose in poses.values()])
        self._animation_after = self.after(33, self._animation_tick)

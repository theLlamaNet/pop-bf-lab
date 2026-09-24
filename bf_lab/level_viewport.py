"""Separate OpenGL window for a Jade level scene."""
from __future__ import annotations

import math
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .level_scene import LevelObject, object_basis
from .viewports import GL, GLU, MeshViewport, OpenGLFrame


def transformed_vertices(source_vertices, obj: LevelObject):
    bx, by, bz = object_basis(obj)
    sx, sy, sz = obj.scale
    px, py, pz = obj.position
    for x, y, z in source_vertices:
        x, y, z = x * sx, y * sy, z * sz
        yield (px + x * bx[0] + y * by[0] + z * bz[0],
               py + x * bx[1] + y * by[1] + z * bz[1],
               pz + x * bx[2] + y * by[2] + z * bz[2])


def vertex_normals(vertices, faces):
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for face in faces:
        if len(face) != 3 or any(i < 0 or i >= len(vertices) for i in face):
            continue
        a, b, c = (vertices[i] for i in face)
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        normal = (ab[1] * ac[2] - ab[2] * ac[1],
                  ab[2] * ac[0] - ab[0] * ac[2],
                  ab[0] * ac[1] - ab[1] * ac[0])
        for index in face:
            for component in range(3):
                normals[index][component] += normal[component]
    result = []
    for x, y, z in normals:
        length = math.sqrt(x * x + y * y + z * z) or 1.0
        result.append((x / length, y / length, z / length))
    return result


def ray_triangle(origin, direction, a, b, c):
    """Distance to a triangle along a ray, including back-facing triangles."""
    edge1 = tuple(b[i] - a[i] for i in range(3))
    edge2 = tuple(c[i] - a[i] for i in range(3))
    cross = (direction[1] * edge2[2] - direction[2] * edge2[1],
             direction[2] * edge2[0] - direction[0] * edge2[2],
             direction[0] * edge2[1] - direction[1] * edge2[0])
    determinant = sum(edge1[i] * cross[i] for i in range(3))
    if abs(determinant) < 1e-10:
        return None
    inverse = 1.0 / determinant
    offset = tuple(origin[i] - a[i] for i in range(3))
    u = inverse * sum(offset[i] * cross[i] for i in range(3))
    if u < 0 or u > 1:
        return None
    q = (offset[1] * edge1[2] - offset[2] * edge1[1],
         offset[2] * edge1[0] - offset[0] * edge1[2],
         offset[0] * edge1[1] - offset[1] * edge1[0])
    v = inverse * sum(direction[i] * q[i] for i in range(3))
    if v < 0 or u + v > 1:
        return None
    distance = inverse * sum(edge2[i] * q[i] for i in range(3))
    return distance if distance > 0.01 else None


if OpenGLFrame is not None:
    class LevelViewport(MeshViewport):
        def __init__(self, master, owner, **kwargs):
            super().__init__(master, owner, **kwargs)
            self.objects = []
            self.meshes = {}
            self.selected = None
            self.mode = "move"
            self.yaw = -0.55
            self.pitch = 0.35
            self.focused_object = None
            self._focus_position = None
            self.flashlight = False
            self.show_markers = True
            self.wireframe = False
            self._gizmo_drag = None
            self._press = None
            self._display_lists = {}
            self.bind("<ButtonPress-1>", self._start_gizmo_or_orbit)
            self.bind("<B1-Motion>", self._drag_gizmo_or_orbit)
            self.bind("<ButtonRelease-1>", self._release_left)

        def set_level(self, objects, meshes, textures, texture_maps, color_maps, mesh_links):
            if self._display_lists and self.winfo_ismapped():
                self.tkMakeCurrent()
                for _signature, list_id in self._display_lists.values():
                    GL.glDeleteLists(list_id, 1)
            self._display_lists.clear()
            self.objects = objects
            self.meshes = {mesh.key: mesh for mesh in meshes}
            self.textures = textures
            self.texture_maps = texture_maps
            self.color_maps = color_maps
            self.mesh_links = mesh_links
            self._release_textures()
            points = [o.position for o in objects if self.mesh_links.get(id(o))]
            if points:
                self.target = [sum(p[i] for p in points) / len(points) for i in range(3)]
                radius = max(math.dist(p, self.target) for p in points)
                self.distance = max(3.0, radius * 2.5)
            if self.winfo_ismapped():
                self._display()

        def select_object(self, obj):
            self.selected = obj
            self._display()

        def focus_object(self, obj):
            self.selected = obj
            self.focused_object = obj
            self._focus_position = tuple(obj.position)
            self.target = list(obj.position)
            linked = [self.meshes[k] for k in self.mesh_links.get(id(obj), ()) if k in self.meshes]
            radius = max((math.dist(vertex, obj.position) for mesh in linked
                          for vertex in transformed_vertices(mesh.vertices, obj)), default=0.0)
            if radius == 0.0:
                radius = max(1.0, min(self.distance * 0.02, 10.0))
            self.distance = max(2.0, radius * 2.7)
            self._display()

        def _start_gizmo_or_orbit(self, event):
            self.focus_set()
            self._press = (event.x, event.y)
            axis = self._hit_gizmo(event.x, event.y)
            self._gizmo_drag = ((event.x, event.y, axis, self.selected.position,
                                 self.selected.rotation, self.selected.scale) if axis else None)
            if self._gizmo_drag is None:
                self._down(event)

        def _release_left(self, event):
            if self._gizmo_drag is None and self._press is not None:
                if math.hypot(event.x - self._press[0], event.y - self._press[1]) < 4:
                    self.owner.select_from_viewport(self.pick_object(event.x, event.y))
            self._press = None
            self._gizmo_drag = None

        def _drag_gizmo_or_orbit(self, event):
            if self._gizmo_drag is None:
                self._orbit(event)
                return
            x, y, axis, position, rotation, scale = self._gizmo_drag
            direction = self._gizmo_screen_axis(axis)
            amount = (event.x - x) * direction[0] + (event.y - y) * direction[1]
            index = "XYZ".index(axis)
            if self.mode == "move":
                values = list(position)
                values[index] += amount * self.distance * 0.002
                self.selected.position = tuple(values)
            elif self.mode == "rotate":
                values = list(rotation)
                values[index] += amount * 0.7
                self.selected.rotation = tuple(values)
            else:
                values = list(scale)
                values[index] = max(0.001, scale[index] * max(0.01, 1 + amount * 0.01))
                self.selected.scale = tuple(values)
            self.selected.dirty = True
            self.owner.refresh_transform_fields()
            self._display()

        def _camera_frame(self):
            cp, sp = math.cos(self.pitch), math.sin(self.pitch)
            cy, sy = math.cos(self.yaw), math.sin(self.yaw)
            outward = (cp * cy, cp * sy, sp)
            right = (-sy, cy, 0.0)
            up = (-sp * cy, -sp * sy, cp)
            eye = tuple(self.target[i] + self.distance * outward[i] for i in range(3))
            return eye, outward, right, up

        def _screen_point(self, point):
            eye, outward, right, up = self._camera_frame()
            relative = tuple(point[i] - eye[i] for i in range(3))
            depth = -sum(relative[i] * outward[i] for i in range(3))
            if depth <= 0.01:
                return None
            height = max(1, self.winfo_height())
            scale = height / (2 * math.tan(math.radians(24)) * depth)
            return (self.winfo_width() / 2 + sum(relative[i] * right[i] for i in range(3)) * scale,
                    height / 2 - sum(relative[i] * up[i] for i in range(3)) * scale)

        def _gizmo_points(self):
            if self.selected is None:
                return {}
            center = self.selected.position
            length = max(0.3, self.distance * 0.12)
            start = self._screen_point(center)
            if start is None:
                return {}
            return {axis: (start, self._screen_point(tuple(center[i] + (length if i == index else 0)
                                                       for i in range(3))))
                    for index, axis in enumerate("XYZ")}

        def _hit_gizmo(self, x, y):
            best = (12.0, None)
            for axis, (start, end) in self._gizmo_points().items():
                if end is None:
                    continue
                vx, vy = end[0] - start[0], end[1] - start[1]
                length2 = vx * vx + vy * vy
                if length2 < 16:
                    continue
                t = max(0.20, min(1.2, ((x - start[0]) * vx + (y - start[1]) * vy) / length2))
                distance = math.hypot(x - start[0] - t * vx, y - start[1] - t * vy)
                if distance < best[0]:
                    best = (distance, axis)
            return best[1]

        def _gizmo_screen_axis(self, axis):
            start, end = self._gizmo_points().get(axis, ((0, 0), (1, 0)))
            if end is None:
                return (1.0, 0.0)
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy) or 1.0
            return dx / length, dy / length

        def pick_object(self, x, y):
            eye, outward, right, up = self._camera_frame()
            height = max(1, self.winfo_height())
            factor = 2 * math.tan(math.radians(24)) / height
            sx = (x - self.winfo_width() / 2) * factor
            sy = (height / 2 - y) * factor
            direction = tuple(-outward[i] + sx * right[i] + sy * up[i] for i in range(3))
            length = math.sqrt(sum(v * v for v in direction))
            direction = tuple(v / length for v in direction)
            closest = (float("inf"), None)
            for obj in self.objects:
                for key in self.mesh_links.get(id(obj), ()):
                    mesh = self.meshes.get(key)
                    if mesh is None:
                        continue
                    for vertices, faces in ((mesh.vertices, mesh.faces),
                                            (mesh.second_vertices, mesh.second_faces)):
                        if not vertices or not faces:
                            continue
                        world = list(transformed_vertices(vertices, obj))
                        for face in faces:
                            if len(face) != 3 or any(i < 0 or i >= len(world) for i in face):
                                continue
                            distance = ray_triangle(eye, direction, *(world[i] for i in face))
                            if distance is not None and distance < closest[0]:
                                closest = (distance, obj)
            return closest[1]

        def _draw_marker(self, obj):
            x, y, z = obj.position
            size = max(0.3, self.distance * 0.008)
            GL.glDisable(GL.GL_TEXTURE_2D)
            GL.glDisable(GL.GL_LIGHTING)
            color = ((1.0, 0.65, 0.15) if obj.kind == "light" else
                     (0.95, 0.2, 0.75) if obj.kind == "trigger" else (0.5, 0.8, 1.0))
            GL.glColor3f(*color)
            GL.glBegin(GL.GL_LINES)
            for axis in range(3):
                a = [x, y, z]; b = [x, y, z]
                a[axis] -= size; b[axis] += size
                GL.glVertex3f(*a); GL.glVertex3f(*b)
            if obj.kind == "trigger" and all(math.isfinite(v) for v in obj.bounds):
                xmin, ymin, zmin, xmax, ymax, zmax = obj.bounds
                if all(abs(v) < 100000 for v in obj.bounds):
                    bx, by, bz = object_basis(obj)
                    corners = []
                    for lx in (xmin, xmax):
                        for ly in (ymin, ymax):
                            for lz in (zmin, zmax):
                                lx1, ly1, lz1 = lx * obj.scale[0], ly * obj.scale[1], lz * obj.scale[2]
                                corners.append((x + lx1 * bx[0] + ly1 * by[0] + lz1 * bz[0],
                                                y + lx1 * bx[1] + ly1 * by[1] + lz1 * bz[1],
                                                z + lx1 * bx[2] + ly1 * by[2] + lz1 * bz[2]))
                    for i in range(8):
                        for bit in (1, 2, 4):
                            if not i & bit:
                                GL.glVertex3f(*corners[i]); GL.glVertex3f(*corners[i | bit])
            GL.glEnd()
            GL.glEnable(GL.GL_LIGHTING)

        def _draw_selection(self, obj, linked):
            GL.glDisable(GL.GL_TEXTURE_2D)
            GL.glDisable(GL.GL_LIGHTING)
            GL.glDisable(GL.GL_CULL_FACE)
            GL.glDepthMask(GL.GL_FALSE)
            GL.glEnable(GL.GL_POLYGON_OFFSET_LINE)
            GL.glPolygonOffset(-1.0, -1.0)
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
            GL.glColor3f(1.0, 0.48, 0.06)
            GL.glLineWidth(2.0)
            for mesh in linked:
                for source, faces in ((mesh.vertices, mesh.faces),
                                      (mesh.second_vertices, mesh.second_faces)):
                    if not source or not faces:
                        continue
                    vertices = list(transformed_vertices(source, obj))
                    GL.glBegin(GL.GL_TRIANGLES)
                    for face in faces:
                        if len(face) != 3 or any(i < 0 or i >= len(vertices) for i in face):
                            continue
                        for index in face:
                            GL.glVertex3f(*vertices[index])
                    GL.glEnd()
            GL.glLineWidth(1.0)
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE if self.wireframe else GL.GL_FILL)
            GL.glDisable(GL.GL_POLYGON_OFFSET_LINE)
            GL.glDepthMask(GL.GL_TRUE)
            GL.glEnable(GL.GL_CULL_FACE)
            GL.glEnable(GL.GL_LIGHTING)

        def _draw_gizmo(self):
            if self.selected is None:
                return
            center = self.selected.position
            length = max(0.3, self.distance * 0.12)
            GL.glDisable(GL.GL_TEXTURE_2D)
            GL.glDisable(GL.GL_LIGHTING)
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glLineWidth(3.0)
            for index, color in enumerate(((1.0, 0.25, 0.23), (0.32, 0.9, 0.36), (0.3, 0.6, 1.0))):
                end = tuple(center[i] + (length if i == index else 0.0) for i in range(3))
                GL.glColor3f(*color)
                GL.glBegin(GL.GL_LINES)
                GL.glVertex3f(*center)
                GL.glVertex3f(*end)
                GL.glEnd()
                GL.glPointSize(9.0)
                GL.glBegin(GL.GL_POINTS)
                GL.glVertex3f(*end)
                GL.glEnd()
                if self.mode == "rotate":
                    GL.glBegin(GL.GL_LINE_LOOP)
                    for step in range(48):
                        angle = step * math.tau / 48
                        point = list(center)
                        point[(index + 1) % 3] += length * 0.72 * math.cos(angle)
                        point[(index + 2) % 3] += length * 0.72 * math.sin(angle)
                        GL.glVertex3f(*point)
                    GL.glEnd()
            GL.glLineWidth(1.0)
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glEnable(GL.GL_LIGHTING)

        def camera_axes(self):
            cy, sy = math.cos(self.yaw), math.sin(self.yaw)
            cp, sp = math.cos(self.pitch), math.sin(self.pitch)
            forward = (-cp * cy, -cp * sy, -sp)
            right = (-sy, cy, 0.0)
            return forward, right

        def move_camera(self, forward=0.0, right=0.0, vertical=0.0, dt=1 / 60):
            look, side = self.camera_axes()
            direction = [forward * look[i] + right * side[i] + (vertical if i == 2 else 0.0)
                         for i in range(3)]
            length = math.sqrt(sum(v * v for v in direction))
            if length == 0:
                return
            self.focused_object = None
            speed = max(9.0, min(2100.0, self.distance * 0.36)) * dt
            self.target = [self.target[i] + speed * direction[i] / length for i in range(3)]
            self._display()

        def _pan(self, event):
            if self.drag is None:
                return
            x, y, target = self.drag
            _look, side = self.camera_axes()
            up = (-math.sin(self.pitch) * math.cos(self.yaw),
                  -math.sin(self.pitch) * math.sin(self.yaw), math.cos(self.pitch))
            scale = max(0.001, self.distance * 0.0018)
            self.focused_object = None
            self.target = [target[i] - (event.x - x) * scale * side[i]
                           + (event.y - y) * scale * up[i] for i in range(3)]
            self._display()

        def redraw(self):
            if self.focused_object is not None:
                position = tuple(self.focused_object.position)
                if position != self._focus_position:
                    self.target = [self.target[i] + position[i] - self._focus_position[i]
                                   for i in range(3)]
                    self._focus_position = position
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            self._apply_projection()
            GL.glMatrixMode(GL.GL_MODELVIEW); GL.glLoadIdentity()
            if self.flashlight:
                GL.glEnable(GL.GL_LIGHT2)
                GL.glLightfv(GL.GL_LIGHT2, GL.GL_POSITION, (0.0, 0.0, 0.0, 1.0))
                GL.glLightfv(GL.GL_LIGHT2, GL.GL_SPOT_DIRECTION, (0.0, 0.0, -1.0))
                GL.glLightfv(GL.GL_LIGHT2, GL.GL_DIFFUSE, (1.0, 0.97, 0.88, 1.0))
                GL.glLightfv(GL.GL_LIGHT2, GL.GL_SPECULAR, (1.0, 0.97, 0.88, 1.0))
                GL.glLightf(GL.GL_LIGHT2, GL.GL_SPOT_CUTOFF, 36.0)
                GL.glLightf(GL.GL_LIGHT2, GL.GL_SPOT_EXPONENT, 4.0)
                GL.glLightf(GL.GL_LIGHT2, GL.GL_LINEAR_ATTENUATION, 0.0008)
            else:
                GL.glDisable(GL.GL_LIGHT2)
            cp, sp = math.cos(self.pitch), math.sin(self.pitch)
            eye = (self.target[0] + self.distance * cp * math.cos(self.yaw),
                   self.target[1] + self.distance * cp * math.sin(self.yaw),
                   self.target[2] + self.distance * sp)
            GLU.gluLookAt(*eye, *self.target, 0, 0, 1)
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE if self.wireframe else GL.GL_FILL)
            for texture_key, image in self.textures.items():
                if texture_key not in self.texture_ids:
                    self._upload_texture(texture_key, image)
            for obj in self.objects:
                linked = [self.meshes[k] for k in self.mesh_links.get(id(obj), ()) if k in self.meshes]
                if not linked:
                    if self.show_markers:
                        self._draw_marker(obj)
                    continue
                signature = (obj.position, obj.rotation, obj.scale, len(self.texture_ids))
                cached = self._display_lists.get(id(obj))
                if cached is None or cached[0] != signature:
                    if cached is not None:
                        GL.glDeleteLists(cached[1], 1)
                    list_id = GL.glGenLists(1)
                    GL.glNewList(list_id, GL.GL_COMPILE)
                    for mesh in linked:
                        # MeshViewport owns per-face material drawing.
                        self.material_textures = self.texture_maps.get(mesh.key, {})
                        self.material_colors = self.color_maps.get(mesh.key, {})
                        vertices = list(transformed_vertices(mesh.vertices, obj))
                        normals = vertex_normals(vertices, mesh.faces)
                        self._draw_mesh(vertices, mesh.faces, mesh.uvs, mesh.uv_indices,
                                        mesh.material_ids, normals, mesh.vertex_colors)
                        if mesh.second_vertices and mesh.second_faces:
                            second = list(transformed_vertices(mesh.second_vertices, obj))
                            second_normals = vertex_normals(second, mesh.second_faces)
                            self._draw_mesh(second, mesh.second_faces, mesh.second_uvs or [],
                                            mesh.second_uv_indices or [],
                                            mesh.second_material_ids or mesh.material_ids,
                                            second_normals)
                    GL.glEndList()
                    self._display_lists[id(obj)] = (signature, list_id)
                GL.glCallList(self._display_lists[id(obj)][1])
                if obj is self.selected:
                    self._draw_selection(obj, linked)
                if obj is self.selected and self.show_markers:
                    self._draw_marker(obj)
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
            GL.glDisable(GL.GL_TEXTURE_2D)
            GL.glDisable(GL.GL_LIGHTING)
            GL.glColor3f(0.25, 0.27, 0.32)
            GL.glBegin(GL.GL_LINES)
            for i in range(-10, 11):
                GL.glVertex3f(i, -10, 0); GL.glVertex3f(i, 10, 0)
                GL.glVertex3f(-10, i, 0); GL.glVertex3f(10, i, 0)
            GL.glEnd()
            GL.glEnable(GL.GL_LIGHTING)
            self._draw_gizmo()


class LevelWorkspace(tk.Toplevel):
    def __init__(self, parent, title, objects, meshes, textures, texture_maps, color_maps,
                 mesh_links, on_change, on_open_mesh):
        super().__init__(parent)
        self.title(f"Level editor workspace — {title}")
        self.iconbitmap(default=str(Path(__file__).resolve().parent / "icons" / "wol.ico"))
        self.geometry("1220x800")
        self.minsize(850, 550)
        self.configure(bg=parent._dark["bg"])
        self.objects = objects
        self.on_change = on_change
        self.on_open_mesh = on_open_mesh
        self.selected = None
        self._held_keys = set()
        self._movement_timer = None
        self._last_camera_tick = None
        self.bind("<KeyPress>", self._key_down)
        self.bind("<KeyRelease>", self._key_up)
        self.bind("<FocusOut>", lambda _e: self._held_keys.clear())
        self.bind("<Destroy>", self._on_destroy, add="+")
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text=title, font=("Segoe UI Semibold", 12)).pack(side="left")
        ttk.Label(toolbar, text=f"{len(objects)} objects", foreground=parent._dark["muted"]).pack(side="right")
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)
        scene = ttk.Frame(body)
        inspector = ttk.Frame(body, padding=8)
        body.add(scene, weight=5)
        body.add(inspector, weight=1)
        if OpenGLFrame is None:
            ttk.Label(scene, text="OpenGL is unavailable. Install PyOpenGL and pyopengltk.").pack(expand=True)
            self.viewport = None
        else:
            self.viewport = LevelViewport(scene, self, width=900, height=650)
            self.viewport.pack(fill="both", expand=True)
            self.viewport.set_level(objects, meshes, textures, texture_maps, color_maps, mesh_links)
            self.after_idle(self.viewport.focus_set)
        modes = ttk.Frame(scene, padding=8)
        modes.place(relx=0, rely=1, anchor="sw")
        mode_row = ttk.Frame(modes)
        mode_row.pack(anchor="w")
        for label, icon, mode in (("Move", "✥", "move"), ("Rotate", "⟳", "rotate"), ("Scale", "⤢", "scale")):
            ttk.Button(mode_row, text=f"{icon}  {label}", command=lambda m=mode: self.set_mode(m)).pack(side="left", padx=2)
        self.flashlight_button = ttk.Button(mode_row, text="Flashlight: Off", command=self.toggle_flashlight)
        self.flashlight_button.pack(side="left", padx=(8, 2))
        display_row = ttk.Frame(modes)
        display_row.pack(anchor="w", pady=(5, 0))
        self.markers_button = ttk.Button(display_row, text="Reticle stars: On", command=self.toggle_markers)
        self.markers_button.pack(side="left", padx=2)
        self.wireframe_button = ttk.Button(display_row, text="Wireframe: Off", command=self.toggle_wireframe)
        self.wireframe_button.pack(side="left", padx=2)
        ttk.Label(inspector, text="Scene objects", font=("Segoe UI Semibold", 11)).pack(anchor="w")
        tree_frame = ttk.Frame(inspector)
        tree_frame.pack(fill="both", expand=True, pady=(8, 8))
        self._tree_frame = tree_frame
        self.tree = ttk.Treeview(tree_frame, columns=("mesh_editor",), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="Object")
        self.tree.heading("mesh_editor", text="Action")
        self.tree.column("#0", width=170, minwidth=110, stretch=True)
        self.tree.column("mesh_editor", width=155, minwidth=155, stretch=False, anchor="center")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree_scroll)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._tree_objects = {}
        self._tree_mesh_keys = {}
        self._last_mesh_action = (None, 0.0)
        for i, obj in enumerate(objects):
            iid = f"object_{i}"
            keys = list(dict.fromkeys(mesh_links.get(id(obj), ())))
            action = "Open in Mesh editor" if len(keys) == 1 else ""
            self.tree.insert("", "end", iid=iid, text=f"{obj.name}  [{obj.kind}]",
                             values=(action,), open=True)
            self._tree_objects[iid] = obj
            if len(keys) == 1:
                self._tree_mesh_keys[iid] = keys[0]
            elif len(keys) > 1:
                for key in keys:
                    child = f"{iid}_mesh_{key:08x}"
                    self.tree.insert(iid, "end", iid=child, text=f"Mesh 0x{key:08X}",
                                     values=("Open in Mesh editor",))
                    self._tree_objects[child] = obj
                    self._tree_mesh_keys[child] = key
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.tree.bind("<Double-1>", self._focus_tree_object)
        self.tree.bind("<ButtonRelease-1>", self._mesh_action_clicked)
        self.tree.bind("<Configure>", lambda _e: self.after_idle(self._refresh_mesh_buttons))
        self.tree.bind("<MouseWheel>", lambda _e: self.after_idle(self._refresh_mesh_buttons), add="+")
        self.tree.bind("<Button-4>", lambda _e: self.after_idle(self._refresh_mesh_buttons), add="+")
        self.tree.bind("<Button-5>", lambda _e: self.after_idle(self._refresh_mesh_buttons), add="+")
        self.tree.bind("<<TreeviewOpen>>", lambda _e: self.after_idle(self._refresh_mesh_buttons))
        self.tree.bind("<<TreeviewClose>>", lambda _e: self.after_idle(self._refresh_mesh_buttons))
        self._mesh_buttons = {}
        self.after_idle(self._refresh_mesh_buttons)
        ttk.Label(inspector, text="Transform", font=("Segoe UI Semibold", 11)).pack(anchor="w")
        self.fields = {}
        for group, labels in (("position", "XYZ"), ("rotation", "XYZ"), ("scale", "XYZ")):
            ttk.Label(inspector, text=group.title()).pack(anchor="w", pady=(6, 1))
            row = ttk.Frame(inspector)
            row.pack(fill="x")
            for axis in labels:
                var = tk.StringVar()
                field = ttk.Entry(row, textvariable=var, width=8)
                field.pack(side="left", fill="x", expand=True, padx=1)
                field.bind("<Return>", self._commit_fields)
                field.bind("<FocusOut>", self._commit_fields)
                self.fields[(group, axis)] = var
        ttk.Label(inspector, text="Click a mesh to select it; drag a colored gizmo handle to transform it.\nDouble-click an object to focus and orbit it.\nW/S: forward/back  •  A/D: strafe\nArrows: move up/down/left/right\nLeft drag: orbit  •  Right drag: pan  •  Wheel: zoom", wraplength=230).pack(anchor="w", pady=12)

    def toggle_flashlight(self):
        if self.viewport is None:
            return
        self.viewport.flashlight = not self.viewport.flashlight
        self.flashlight_button.config(text=f"Flashlight: {'On' if self.viewport.flashlight else 'Off'}")
        self.viewport._display()

    def toggle_markers(self):
        if self.viewport is None:
            return
        self.viewport.show_markers = not self.viewport.show_markers
        self.markers_button.config(text=f"Reticle stars: {'On' if self.viewport.show_markers else 'Off'}")
        self.viewport._display()

    def toggle_wireframe(self):
        if self.viewport is None:
            return
        self.viewport.wireframe = not self.viewport.wireframe
        self.wireframe_button.config(text=f"Wireframe: {'On' if self.viewport.wireframe else 'Off'}")
        self.viewport._display()

    def _key_down(self, event):
        if self.viewport is None or isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Treeview)):
            return
        key = event.keysym.lower()
        if key not in ("w", "a", "s", "d", "left", "right", "up", "down"):
            return
        self._held_keys.add(key)
        if self._movement_timer is None:
            self._last_camera_tick = time.perf_counter()
            self._movement_timer = self.after(16, self._camera_tick)

    def _key_up(self, event):
        self._held_keys.discard(event.keysym.lower())

    def _camera_tick(self):
        self._movement_timer = None
        if not self._held_keys or self.viewport is None:
            self._last_camera_tick = None
            return
        now = time.perf_counter()
        dt = min(0.08, max(0.001, now - (self._last_camera_tick or now)))
        self._last_camera_tick = now
        keys = self._held_keys
        forward = int("w" in keys) - int("s" in keys)
        side = int("d" in keys) - int("a" in keys) + int("right" in keys) - int("left" in keys)
        vertical = int("up" in keys) - int("down" in keys)
        if forward or side or vertical:
            self.viewport.move_camera(forward, side, vertical, dt)
        self._movement_timer = self.after(16, self._camera_tick)

    def _on_destroy(self, event):
        if event.widget is self and self._movement_timer is not None:
            self.after_cancel(self._movement_timer)
            self._movement_timer = None

    def set_mode(self, mode):
        if self.viewport is not None:
            self.viewport.mode = mode
            self.viewport._display()

    def select_from_viewport(self, obj):
        iid = next((row for row, value in self._tree_objects.items()
                    if value is obj and "_mesh_" not in row), None)
        if iid is None:
            self.tree.selection_remove(self.tree.selection())
            self.selected = None
            self.viewport.select_object(None)
        else:
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self._select()

    def _select(self, _event=None):
        selection = self.tree.selection()
        self.selected = self._tree_objects.get(selection[0]) if selection else None
        if self.viewport is not None:
            self.viewport.select_object(self.selected)
        self.refresh_transform_fields()
        self.after_idle(self._refresh_mesh_buttons)

    def _tree_scroll(self, *args):
        self.tree.yview(*args)
        self.after_idle(self._refresh_mesh_buttons)

    def _refresh_mesh_buttons(self):
        if not self.tree.winfo_exists():
            return
        visible = set()
        for iid in self._tree_mesh_keys:
            box = self.tree.bbox(iid, column="mesh_editor")
            if not box:
                continue
            x, y, width, height = box
            if y < 0 or y + height > self.tree.winfo_height():
                continue
            button = self._mesh_buttons.get(iid)
            if button is None:
                button = ttk.Button(self._tree_frame, text="Open in Mesh editor",
                                    command=lambda row=iid: self._open_mesh_row(row))
                self._mesh_buttons[iid] = button
            button.place(x=self.tree.winfo_x() + x + 2, y=self.tree.winfo_y() + y + 2,
                         width=max(1, width - 4), height=max(1, height - 4))
            button.lift()
            visible.add(iid)
        for iid, button in self._mesh_buttons.items():
            if iid not in visible:
                button.place_forget()

    def _focus_tree_object(self, event):
        if self.tree.identify_column(event.x) == "#1":
            return
        iid = self.tree.identify_row(event.y)
        obj = self._tree_objects.get(iid)
        if obj is None or self.viewport is None:
            return
        self.tree.selection_set(iid)
        self.selected = obj
        self.viewport.focus_object(obj)
        self.viewport.focus_set()
        self.refresh_transform_fields()

    def _mesh_action_clicked(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        self._open_mesh_row(iid)

    def _open_mesh_row(self, iid):
        key = self._tree_mesh_keys.get(iid)
        obj = self._tree_objects.get(iid)
        if key is None or obj is None:
            return
        now = time.monotonic()
        if self._last_mesh_action[0] == iid and now - self._last_mesh_action[1] < 0.45:
            return
        self._last_mesh_action = (iid, now)
        self.on_open_mesh(obj, key)

    def refresh_transform_fields(self):
        if self.selected is None:
            return
        for group in ("position", "rotation", "scale"):
            for axis, value in zip("XYZ", getattr(self.selected, group)):
                self.fields[(group, axis)].set(f"{value:.4f}")
        self.on_change()

    def _commit_fields(self, _event=None):
        if self.selected is None:
            return
        try:
            values = {group: tuple(float(self.fields[(group, a)].get()) for a in "XYZ")
                      for group in ("position", "rotation", "scale")}
            if not all(math.isfinite(v) for row in values.values() for v in row):
                raise ValueError("Transform values must be finite.")
            if any(abs(v) < 1e-6 for v in values["scale"]):
                raise ValueError("Scale cannot be zero.")
        except ValueError:
            self.refresh_transform_fields()
            return
        for group, value in values.items():
            setattr(self.selected, group, value)
        self.selected.dirty = True
        self.on_change()
        if self.viewport is not None:
            self.viewport._display()

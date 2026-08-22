"""PoP BF Lab - standalone Prince of Persia Trilogy asset explorer/editor.

The toolkit contains its own Jade BF/OVA/POP-LZO core.  It deliberately has no
runtime dependency on the user's OVA Variable Editor or PopTools folders.
"""

from __future__ import annotations

import os
import re
import struct
import math
import subprocess
import tempfile
import ctypes
import io
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "vendor"
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

try:
    from pyopengltk import OpenGLFrame
    from OpenGL import GL, GLU
except Exception:
    OpenGLFrame = None
    GL = GLU = None

MARKER = b"ova"
AI_MAX_LEN_VAR = 30
OVA_INFO_SIZE = 12
LEGACY_BF_HEADER_SIZE = 68
LEGACY_BF_FILE_ENTRY_SIZE = 84
LEGACY_BF_FILE_TABLE_ENTRY_SIZE = 8
LZO_BLOCK_SIZE = 131072


@dataclass
class OvaVariable:
    name: str
    offset: int
    source: str
    var_offset: int | None = None
    var_type: int | None = None
    flags: int | None = None
    structure_base: int | None = None
    value_absolute: int | None = None
    value_size: int | None = None
    num_elem: int | None = None


@dataclass
class OvaStructure:
    base: int
    count: int
    names_base: int
    names_size: int
    complete_names: int
    truncated: bool
    init_base: int | None = None
    init_size: int | None = None
    source_format: str = "Jade"
    records_base: int | None = None
    names_available: bool = True
    names_encrypted: bool = False
    container_end: int | None = None
    name_slots: int | None = None


@dataclass
class TextureInfo:
    index: int
    offset: int
    data_offset: int
    data_end: int
    width: int
    height: int
    texture_type: int
    key: int
    format: str
    storage_width: int
    storage_height: int


@dataclass
class PopFileEntry:
    index: int
    offset: int
    size: int
    magic: int
    key: int
    data_offset: int
    data_type: int | None


@dataclass
class MeshInfo:
    index: int
    key: int
    entry_index: int
    version: int
    vertices: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]
    uvs: list[tuple[float, float]]
    uv_indices: list[tuple[int, int, int]]
    material_ids: list[tuple[int, int]]
    material_pack_key: int | None = None
    object_name: str = ""
    second_vertices: list[tuple[float, float, float]] | None = None
    second_faces: list[tuple[int, int, int]] | None = None
    second_uvs: list[tuple[float, float]] | None = None
    second_uv_indices: list[tuple[int, int, int]] | None = None
    second_material_ids: list[tuple[int, int]] | None = None
    normals: list[tuple[float, float, float]] | None = None


@dataclass
class MeshMaterial:
    material_id: int
    material_key: int | None
    texture_key: int | None
    face_start: int
    face_count: int


@dataclass
class MaterialInfo:
    index: int
    material_id: int
    material_key: int | None
    texture_key: int | None
    normal_key: int | None = None
    metallic: float = 0.0
    alpha: float = 1.0
    source_meshes: list[int] | None = None
    texture_offset: int | None = None
    specular_offset: int | None = None
    diffuse_offset: int | None = None


if OpenGLFrame is not None:
    class MeshViewport(OpenGLFrame):
        """GPU mesh viewport embedded directly in Tkinter."""

        def __init__(self, master, owner, **kwargs):
            super().__init__(master, **kwargs)
            self.owner = owner
            self.mesh = None
            self.textures = {}
            self.texture_ids = {}
            self.yaw = -0.55
            self.pitch = 0.20
            self.distance = 3.2
            self.target = [0.0, 0.0, 0.0]
            self.drag = None
            self.bind("<ButtonPress-1>", self._down)
            self.bind("<B1-Motion>", self._orbit)
            self.bind("<ButtonPress-3>", self._pan_down)
            self.bind("<B3-Motion>", self._pan)
            self.bind("<MouseWheel>", self._wheel)
            self.bind("<Button-4>", lambda _e: self._zoom(0.86))
            self.bind("<Button-5>", lambda _e: self._zoom(1.16))

        def initgl(self):
            self.tkMakeCurrent()
            GL.glClearColor(0.055, 0.06, 0.075, 1.0)
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glDepthFunc(GL.GL_LEQUAL)
            GL.glEnable(GL.GL_CULL_FACE)
            GL.glCullFace(GL.GL_BACK)
            GL.glEnable(GL.GL_TEXTURE_2D)
            GL.glEnable(GL.GL_LIGHTING)
            GL.glEnable(GL.GL_LIGHT0)
            GL.glEnable(GL.GL_LIGHT1)
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_POSITION, (3.0, 5.0, 4.0, 1.0))
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_DIFFUSE, (0.95, 0.95, 0.95, 1.0))
            GL.glLightfv(GL.GL_LIGHT1, GL.GL_POSITION, (-4.0, 2.0, -3.0, 1.0))
            GL.glLightfv(GL.GL_LIGHT1, GL.GL_DIFFUSE, (0.35, 0.40, 0.55, 1.0))
            GL.glLightModelfv(GL.GL_LIGHT_MODEL_AMBIENT, (0.18, 0.18, 0.20, 1.0))
            GL.glColorMaterial(GL.GL_FRONT_AND_BACK, GL.GL_AMBIENT_AND_DIFFUSE)
            GL.glEnable(GL.GL_COLOR_MATERIAL)
            self._apply_projection()

        def _apply_projection(self):
            if not self.winfo_ismapped():
                return
            w, h = max(1, self.winfo_width()), max(1, self.winfo_height())
            GL.glViewport(0, 0, w, h)
            GL.glMatrixMode(GL.GL_PROJECTION)
            GL.glLoadIdentity()
            GLU.gluPerspective(48.0, float(w) / float(h), 0.01, 10000.0)
            GL.glMatrixMode(GL.GL_MODELVIEW)

        def tkResize(self, event):
            super().tkResize(event)
            if self.winfo_ismapped():
                self.tkMakeCurrent()
                self._apply_projection()

        def set_scene(self, mesh, textures):
            self.mesh = mesh
            self.textures = textures or {}
            self._release_textures()
            if mesh and mesh.vertices:
                xs = [v[0] for v in mesh.vertices]; ys = [v[1] for v in mesh.vertices]; zs = [v[2] for v in mesh.vertices]
                self.target = [(min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5, (min(zs) + max(zs)) * 0.5]
                radius = max(math.sqrt((x-self.target[0])**2 + (y-self.target[1])**2 + (z-self.target[2])**2) for x, y, z in mesh.vertices)
                self.distance = max(0.25, radius * 3.0)
            if self.winfo_ismapped():
                self._display()

        def _release_textures(self):
            if not self.texture_ids or GL is None or not self.winfo_ismapped():
                self.texture_ids.clear()
                return
            try:
                GL.glDeleteTextures(list(self.texture_ids.values()))
            except Exception:
                pass
            self.texture_ids.clear()

        def _upload_texture(self, key, image):
            if key in self.texture_ids:
                return self.texture_ids[key]
            if image is None:
                return None
            from PIL import Image
            image = image.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            tex_id = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, image.width, image.height, 0,
                            GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, image.tobytes())
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            self.texture_ids[key] = tex_id
            return tex_id

        def _draw_mesh(self, vertices, faces, uvs, uv_indices, material_ids):
            if not vertices or not faces:
                return
            face_materials = [0] * len(faces)
            cursor = 0
            for mat_id, count in material_ids or []:
                for i in range(cursor, min(cursor + count, len(faces))):
                    face_materials[i] = mat_id
                cursor += count
            for face_index, face in enumerate(faces):
                if len(face) != 3 or any(i < 0 or i >= len(vertices) for i in face):
                    continue
                mat_id = face_materials[face_index] if face_index < len(face_materials) else 0
                tex_key = self.owner._mesh_material_textures.get(mat_id)
                tex_id = self._upload_texture(tex_key, self.textures.get(tex_key)) if tex_key is not None else None
                if tex_id:
                    GL.glEnable(GL.GL_TEXTURE_2D); GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
                    GL.glColor4f(1, 1, 1, 1)
                else:
                    GL.glDisable(GL.GL_TEXTURE_2D); GL.glColor4f(0.68, 0.72, 0.80, 1)
                GL.glBegin(GL.GL_TRIANGLES)
                for corner, vi in enumerate(face):
                    if uv_indices and face_index < len(uv_indices) and uvs:
                        ui = uv_indices[face_index][corner]
                        if 0 <= ui < len(uvs):
                            GL.glTexCoord2f(float(uvs[ui][0]), 1.0 - float(uvs[ui][1]))
                    x, y, z = vertices[vi]
                    GL.glVertex3f(float(x), float(y), float(z))
                GL.glEnd()
            GL.glEnable(GL.GL_TEXTURE_2D)

        def redraw(self):
            if GL is None:
                return
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            self._apply_projection()
            GL.glMatrixMode(GL.GL_MODELVIEW)
            GL.glLoadIdentity()
            GLU.gluLookAt(0.0, 0.0, self.distance, self.target[0], self.target[1], self.target[2], 0.0, 1.0, 0.0)
            GL.glRotatef(math.degrees(self.pitch), 1, 0, 0)
            GL.glRotatef(math.degrees(self.yaw), 0, 1, 0)
            if self.mesh:
                GL.glPushMatrix()
                GL.glTranslatef(-self.target[0], -self.target[1], -self.target[2])
                self._draw_mesh(self.mesh.vertices, self.mesh.faces, self.mesh.uvs, self.mesh.uv_indices, self.mesh.material_ids)
                if self.mesh.second_vertices and self.mesh.second_faces:
                    self._draw_mesh(self.mesh.second_vertices, self.mesh.second_faces,
                                    self.mesh.second_uvs or [], self.mesh.second_uv_indices or [],
                                    self.mesh.second_material_ids or self.mesh.material_ids)
                GL.glPopMatrix()
            GL.glDisable(GL.GL_TEXTURE_2D)
            GL.glColor3f(0.22, 0.25, 0.30)
            GL.glBegin(GL.GL_LINES)
            for i in range(-5, 6):
                GL.glVertex3f(i, 0, -5); GL.glVertex3f(i, 0, 5)
                GL.glVertex3f(-5, 0, i); GL.glVertex3f(5, 0, i)
            GL.glEnd()
            GL.glEnable(GL.GL_TEXTURE_2D)

        def _down(self, event): self.drag = (event.x, event.y, self.yaw, self.pitch)
        def _orbit(self, event):
            if self.drag is None: return
            x, y, yaw, pitch = self.drag
            self.yaw = yaw + (event.x - x) * 0.012
            self.pitch = max(-1.45, min(1.45, pitch + (event.y - y) * 0.012))
            self._display()
        def _pan_down(self, event): self.drag = (event.x, event.y, self.target[:])
        def _pan(self, event):
            if self.drag is None: return
            x, y, target = self.drag
            scale = self.distance * 0.0018
            self.target[0] = target[0] - (event.x - x) * scale
            self.target[1] = target[1] + (event.y - y) * scale
            self._display()
        def _wheel(self, event): self._zoom(0.86 if event.delta > 0 else 1.16)
        def _zoom(self, factor):
            self.distance = max(0.05, min(10000.0, self.distance * factor)); self._display()


    class MaterialViewport(OpenGLFrame):
        """OpenGL preview for the Material Swap editor."""

        def __init__(self, master, owner, **kwargs):
            super().__init__(master, **kwargs)
            self.owner = owner
            self.image = None
            self.texture_id = None
            self.yaw = -0.35
            self.pitch = 0.12
            self.distance = 3.0
            self.drag = None
            self.bind("<ButtonPress-1>", lambda e: self._start(e))
            self.bind("<B1-Motion>", lambda e: self._drag(e))
            self.bind("<MouseWheel>", lambda e: self._wheel(e))
            self.bind("<Button-4>", lambda _e: self._zoom(0.86))
            self.bind("<Button-5>", lambda _e: self._zoom(1.16))

        def initgl(self):
            self.tkMakeCurrent()
            GL.glClearColor(0.055, 0.06, 0.075, 1.0)
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glEnable(GL.GL_CULL_FACE)
            GL.glCullFace(GL.GL_BACK)
            GL.glEnable(GL.GL_LIGHTING)
            GL.glEnable(GL.GL_LIGHT0)
            GL.glEnable(GL.GL_LIGHT1)
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_POSITION, (3.0, 4.0, 4.0, 1.0))
            GL.glLightfv(GL.GL_LIGHT0, GL.GL_DIFFUSE, (1.0, 1.0, 1.0, 1.0))
            GL.glLightfv(GL.GL_LIGHT1, GL.GL_POSITION, (-3.0, 1.0, -2.0, 1.0))
            GL.glLightfv(GL.GL_LIGHT1, GL.GL_DIFFUSE, (0.28, 0.32, 0.45, 1.0))
            GL.glEnable(GL.GL_COLOR_MATERIAL)
            GL.glColorMaterial(GL.GL_FRONT_AND_BACK, GL.GL_AMBIENT_AND_DIFFUSE)
            self._apply_projection()

        def _apply_projection(self):
            if not self.winfo_ismapped(): return
            w, h = max(1, self.winfo_width()), max(1, self.winfo_height())
            GL.glViewport(0, 0, w, h)
            GL.glMatrixMode(GL.GL_PROJECTION); GL.glLoadIdentity()
            GLU.gluPerspective(45.0, float(w) / float(h), 0.05, 100.0)
            GL.glMatrixMode(GL.GL_MODELVIEW)

        def set_image(self, image):
            if not self.winfo_ismapped():
                self.image = image
                return
            self.tkMakeCurrent()
            self.image = image
            if self.texture_id:
                try: GL.glDeleteTextures([self.texture_id])
                except Exception: pass
                self.texture_id = None
            if image is not None:
                image = image.convert("RGBA")
                self.texture_id = GL.glGenTextures(1)
                GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture_id)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, image.width, image.height, 0,
                                GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, image.tobytes())
                GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            self._render()

        def redraw(self):
            # pyopengltk calls redraw() from BaseOpenGLFrame._display().
            self._render(swap=False)

        def _render(self, swap=True):
            if GL is None or not self.winfo_ismapped(): return
            self.tkMakeCurrent(); self._apply_projection()
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            GL.glMatrixMode(GL.GL_MODELVIEW); GL.glLoadIdentity()
            GLU.gluLookAt(0.0, 0.0, self.distance, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
            GL.glRotatef(math.degrees(self.pitch), 1, 0, 0)
            GL.glRotatef(math.degrees(self.yaw), 0, 1, 0)
            metallic = max(0.0, min(1.0, float(self.owner._material_metallic.get())))
            alpha = max(0.05, min(1.0, float(self.owner._material_alpha.get())))
            projection = max(0.05, min(8.0, float(self.owner._material_projection.get())))
            GL.glColor4f(0.55 + metallic * 0.35, 0.58 + metallic * 0.28, 0.65 + metallic * 0.20, alpha)
            if alpha < 0.999:
                GL.glEnable(GL.GL_BLEND); GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA); GL.glDepthMask(False)
            if self.texture_id:
                GL.glEnable(GL.GL_TEXTURE_2D); GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture_id)
            if self.owner._material_shape.get() == "cube":
                s = 1.18
                faces = (
                    ((0, 0, 1), ((-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s))),
                    ((0, 0, -1), ((s, -s, -s), (-s, -s, -s), (-s, s, -s), (s, s, -s))),
                    ((1, 0, 0), ((s, -s, s), (s, -s, -s), (s, s, -s), (s, s, s))),
                    ((-1, 0, 0), ((-s, -s, -s), (-s, -s, s), (-s, s, s), (-s, s, -s))),
                    ((0, 1, 0), ((-s, s, s), (s, s, s), (s, s, -s), (-s, s, -s))),
                    ((0, -1, 0), ((-s, -s, -s), (s, -s, -s), (s, -s, s), (-s, -s, s))),
                )
                GL.glBegin(GL.GL_QUADS)
                for normal, vertices in faces:
                    GL.glNormal3f(*normal)
                    for u, v, (x, y, z) in ((0, 0, vertices[0]), (projection, 0, vertices[1]),
                                             (projection, projection, vertices[2]), (0, projection, vertices[3])):
                        if self.texture_id:
                            GL.glTexCoord2f(u, v)
                        GL.glVertex3f(x, y, z)
                GL.glEnd()
            else:
                quad = GLU.gluNewQuadric(); GLU.gluQuadricTexture(quad, bool(self.texture_id))
                if self.texture_id:
                    GL.glMatrixMode(GL.GL_TEXTURE); GL.glPushMatrix(); GL.glLoadIdentity(); GL.glScalef(projection, projection, 1.0)
                    GL.glMatrixMode(GL.GL_MODELVIEW)
                GLU.gluSphere(quad, 1.18, 48, 32); GLU.gluDeleteQuadric(quad)
                if self.texture_id:
                    GL.glMatrixMode(GL.GL_TEXTURE); GL.glPopMatrix(); GL.glMatrixMode(GL.GL_MODELVIEW)
            if self.texture_id: GL.glDisable(GL.GL_TEXTURE_2D)
            if alpha < 0.999:
                GL.glDepthMask(True); GL.glDisable(GL.GL_BLEND)
            if swap:
                self.tkSwapBuffers()

        def _start(self, event): self.drag = (event.x, event.y, self.yaw, self.pitch)
        def _drag(self, event):
            if self.drag is None: return
            x, y, yaw, pitch = self.drag
            self.yaw = yaw + (event.x - x) * 0.012
            self.pitch = max(-1.3, min(1.3, pitch + (event.y - y) * 0.012)); self._render()
        def _wheel(self, event): self._zoom(0.86 if event.delta > 0 else 1.16)
        def _zoom(self, factor):
            self.distance = max(1.6, min(8.0, self.distance * factor)); self._render()


class _PopReader:
    """Small bounds-checked reader used by the standalone mesh parser."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _need(self, size: int) -> None:
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError(f"Dati mesh troncati a 0x{self.pos:X} (richiesti {size} B).")

    def u32(self) -> int:
        self._need(4)
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def i32(self) -> int:
        self._need(4)
        value = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def u16(self) -> int:
        self._need(2)
        value = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def i16(self) -> int:
        self._need(2)
        value = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return value

    def f32(self) -> float:
        self._need(4)
        value = struct.unpack_from("<f", self.data, self.pos)[0]
        self.pos += 4
        return value

    def bytes(self, size: int) -> bytes:
        self._need(size)
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def u32s(self, count: int) -> list[int]:
        if count < 0 or count > 2_000_000:
            raise ValueError(f"Conteggio uint32 non plausibile: {count}")
        return [self.u32() for _ in range(count)]

    def i16s(self, count: int) -> list[int]:
        if count < 0 or count > 6_000_000:
            raise ValueError(f"Conteggio int16 non plausibile: {count}")
        return [self.i16() for _ in range(count)]

    def f32s(self, count: int) -> list[float]:
        if count < 0 or count > 6_000_000:
            raise ValueError(f"Conteggio float non plausibile: {count}")
        return [self.f32() for _ in range(count)]


def _pop_hex(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        raise ValueError("Hash POP oltre la fine dell'entry.")
    return struct.unpack_from("<I", data, offset)[0]


def _scan_pop_meshes(data: bytes) -> list[MeshInfo]:
    """Parse primary POP mesh blocks (type 0x00000001) without Blender/PopTools."""
    meshes: list[MeshInfo] = []
    for entry in _parse_pop_file_entries(data):
        if entry.data_type != 1 or entry.size < 32:
            continue
        try:
            r = _PopReader(data[entry.data_offset + 4:entry.data_offset + entry.size])
            version = r.u32()
            if version not in (7, 8):
                continue
            flags = r.u32()
            kb_marker = struct.unpack_from("<I", r.data, r.pos + 4)[0] if r.pos + 8 <= len(r.data) else 0
            if version == 7 and (flags & 0x8) and kb_marker == 0xC0DE2002:
                kb = _scan_kindred_blades_packet(r, entry)
                if kb is not None:
                    kb.index = len(meshes)
                    meshes.append(kb)
                continue
            has_second_mesh = r.u32() != 0
            num_vertices = r.u32()
            num_unknown = r.u32()
            has_unknown = r.u32() != 0
            num_uvs = r.u32()
            num_materials = r.u32()
            if num_vertices == 0 or num_vertices > 500_000 or num_uvs > 1_000_000 or num_materials > 4096:
                continue

            if flags & 0x8 or flags & 0x10:
                r.f32()
                has_normals = r.i16() != 0
                num4 = r.i16()
                for _ in range(max(0, num4)):
                    r.i16()
                    num_floats = r.i16()
                    r.f32s(16)
                    r.u32()
                    r.f32s(num_floats)
                if num4 > 0:
                    has_normals = r.u32() != 0 or has_normals
            else:
                r.u32()
                has_normals = r.u32() != 0

            vertex_data = r.f32s(3 * num_vertices)
            vertices = list(zip(vertex_data[0::3], vertex_data[1::3], vertex_data[2::3]))
            if has_unknown:
                r.f32s(num_unknown)
            if has_normals:
                r.f32s(3 * num_vertices)
            uv_data = r.f32s(2 * num_uvs)
            uvs = list(zip(uv_data[0::2], uv_data[1::2]))

            material_ids: list[tuple[int, int]] = []
            num_faces = 0
            for _ in range(num_materials):
                face_count = r.u32()
                material_id = r.i32()
                if face_count > 1_000_000:
                    raise ValueError("Numero di facce non plausibile.")
                material_ids.append((material_id, face_count))
                num_faces += face_count
            if num_faces == 0 or num_faces > 2_000_000:
                continue

            face_data = r.i16s(num_faces * 8)
            faces: list[tuple[int, int, int]] = []
            uv_indices: list[tuple[int, int, int]] = []
            for i in range(num_faces):
                base = i * 8
                face = tuple(face_data[base:base + 3])
                uv_indices.append(tuple(face_data[base + 3:base + 6]))
                faces.append(face)

            # Remaining mesh metadata is deliberately ignored. The primary
            # geometry above is all Mesh Swap needs for a faithful preview/export.
            if has_second_mesh:
                pass

            if any(i < 0 or i >= num_vertices for f in faces for i in f):
                continue
            if any(i < 0 or i >= max(1, num_uvs) for f in uv_indices for i in f):
                # Some old meshes omit UVs. Keep the geometry and synthesize
                # vertex-index UVs below when possible.
                if num_uvs:
                    continue
                uvs = []
                uv_indices = []
            second_vertices = second_faces = second_uvs = second_uv_indices = second_material_ids = None
            if has_second_mesh and r.pos + 16 <= len(r.data):
                try:
                    second_material_ids = []
                    second_face_count = 0
                    for _ in range(num_materials):
                        count = r.u32(); material_id = r.i32()
                        second_material_ids.append((material_id, count))
                        second_face_count += count
                    _size = r.u32(); _unknown = r.u32(); second_vertex_count = r.u32(); block_length = r.u32()
                    if second_vertex_count <= 500_000 and block_length in (20, 32, 44, 52, 64):
                        second_vertices = []
                        second_uvs = []
                        for _ in range(second_vertex_count):
                            second_vertices.append(tuple(r.f32s(3)))
                            if block_length in (32, 44, 52, 64):
                                r.f32s(3)
                            if block_length == 20:
                                second_uvs.append(tuple(r.f32s(2)))
                            elif block_length == 32:
                                second_uvs.append(tuple(r.f32s(2)))
                            elif block_length == 44:
                                second_uvs.append(tuple(r.f32s(2)))
                                r.f32s(3)
                            elif block_length == 52:
                                r.i16s(2); r.u32(); r.f32s(3)
                                second_uvs.append(tuple(r.f32s(2)))
                            else:
                                r.i16s(2); r.u32(); r.f32s(3)
                                second_uvs.append(tuple(r.f32s(2)))
                                r.u32(); r.f32s(2)
                        raw_faces = r.i16s(second_face_count * 3)
                        second_faces = [tuple(raw_faces[i:i + 3]) for i in range(0, len(raw_faces), 3)]
                        second_uv_indices = list(second_faces)
                except (ValueError, struct.error):
                    second_vertices = second_faces = second_uvs = second_uv_indices = second_material_ids = None

            meshes.append(MeshInfo(len(meshes), entry.key, entry.index, version,
                                   vertices, faces, uvs, uv_indices, material_ids,
                                   second_vertices=second_vertices, second_faces=second_faces,
                                   second_uvs=second_uvs, second_uv_indices=second_uv_indices,
                                   second_material_ids=second_material_ids))
        except (ValueError, IndexError, struct.error):
            continue
    return meshes


def _scan_kindred_blades_packet(r: _PopReader, entry: PopFileEntry) -> MeshInfo | None:
    """Recover KB v7 character geometry from its internal vertex packet."""
    valid_strides = (20, 32, 44, 52, 64)
    start = r.pos
    for packet_pos in range(start, len(r.data) - 16, 4):
        try:
            blob_size, _unknown, vertex_count, stride = struct.unpack_from("<4I", r.data, packet_pos)
            if stride not in valid_strides or not (1 <= vertex_count <= 100_000):
                continue
            vertex_offset = packet_pos + 16
            vertex_end = vertex_offset + vertex_count * stride
            if blob_size != vertex_count * stride + 8 or vertex_end + 4 > len(r.data):
                continue
            face_size = struct.unpack_from("<I", r.data, vertex_end)[0]
            face_end = vertex_end + 4 + face_size
            if face_size < 6 or face_size % 6 or face_end > len(r.data):
                continue
            indices = struct.unpack_from("<" + "h" * (face_size // 2), r.data, vertex_end + 4)
            if not indices or any(i < 0 or i >= vertex_count for i in indices):
                continue
            vertices: list[tuple[float, float, float]] = []
            uvs: list[tuple[float, float]] = []
            p = vertex_offset
            for _ in range(vertex_count):
                x, y, z = struct.unpack_from("<3f", r.data, p)
                vertices.append((x, y, z))
                if stride == 20:
                    uv = struct.unpack_from("<2f", r.data, p + 12)
                else:
                    uv = struct.unpack_from("<2f", r.data, p + 24)
                uvs.append(uv)
                p += stride
            faces = [tuple(indices[i:i + 3]) for i in range(0, len(indices), 3)]
            return MeshInfo(-1, entry.key, entry.index, 7, vertices, faces, uvs, [], [])
        except (ValueError, struct.error):
            continue
    return None


def _scan_pop_materials(data: bytes) -> tuple[dict[int, list[int]], dict[int, int]]:
    """Return material-pack -> material keys and material key -> texture key."""
    packs: dict[int, list[int]] = {}
    materials: dict[int, int] = {}
    for entry in _parse_pop_file_entries(data):
        blob = data[entry.data_offset:entry.data_offset + entry.size]
        if entry.data_type == 4 and len(blob) >= 12:
            try:
                r = _PopReader(blob[4:])
                if r.u32() != 0:
                    continue
                count = r.u32()
                if count > 1000:
                    continue
                packs[entry.key] = [r.u32() for _ in range(count)]
            except (ValueError, struct.error):
                continue
        elif entry.data_type == 5 and len(blob) >= 8:
            try:
                r = _PopReader(blob[4:])
                version = r.u32()
                if not 3 <= version <= 9:
                    continue
                r.u32()
                if version >= 8:
                    r.u32(); r.u32()
                r.u32(); r.u32(); r.u32()
                if r.pos + 4 > len(blob):
                    continue
                r.u32()
                if version >= 8:
                    r.u16()
                r.u32(); r.f32(); r.f32(); r.u32()
                if version == 9:
                    r.bytes(9)
                    if r.pos + 4 > len(blob):
                        continue
                    r.u32()
                if r.pos + 4 <= len(blob):
                    materials[entry.key] = r.u32()
            except (ValueError, struct.error):
                continue
    return packs, materials


def _scan_pop_material_records(data: bytes) -> dict[int, MaterialInfo]:
    """Read the Jade type-5 material records used by the Blender Addon."""
    records: dict[int, MaterialInfo] = {}
    for entry in _parse_pop_file_entries(data):
        if entry.data_type != 5 or entry.size < 16:
            continue
        try:
            blob = data[entry.data_offset:entry.data_offset + entry.size]
            r = _PopReader(blob[4:])
            version = r.u32()
            if not 3 <= version <= 9:
                continue
            r.u32()
            if version >= 8:
                r.u32(); r.u32()
            r.u32(); r.u32(); r.u32()
            flags_offset = entry.data_offset + 4 + r.pos
            r.u32()
            if version >= 8:
                r.u16()
            r.u32()
            specular_offset = entry.data_offset + 4 + r.pos
            specular = r.f32()
            diffuse_offset = entry.data_offset + 4 + r.pos
            diffuse = r.f32()
            r.u32()
            if version == 9:
                r.bytes(9)
                if r.pos + 4 > len(r.data):
                    continue
                r.u32()
            if r.pos + 4 > len(r.data):
                texture_key = None
                texture_offset = None
            else:
                texture_offset = entry.data_offset + 4 + r.pos
                texture_key = r.u32()
            records[entry.key] = MaterialInfo(
                index=len(records), material_id=len(records), material_key=entry.key,
                texture_key=texture_key, metallic=max(0.0, min(1.0, float(specular))),
                alpha=max(0.0, min(1.0, float(diffuse))), source_meshes=[],
                texture_offset=texture_offset, specular_offset=specular_offset,
                diffuse_offset=diffuse_offset,
            )
        except (ValueError, struct.error):
            continue
    return records


def _associate_mesh_material_packs(data: bytes, meshes: list[MeshInfo]) -> None:
    """Use .gao records to associate mesh hashes with their material packs."""
    by_mesh = {m.key: m for m in meshes}
    for entry in _parse_pop_file_entries(data):
        blob = data[entry.data_offset:entry.data_offset + entry.size]
        if len(blob) < 20 or struct.unpack_from("<I", blob, 0)[0] != 0x6F616F2E:
            continue
        try:
            r = _PopReader(blob)
            r.u32(); r.u32(); flags = r.u32(); r.u32()
            name_len = r.u32()
            if name_len <= 0 or name_len > 4096 or r.pos + name_len > len(blob):
                continue
            name = blob[r.pos:r.pos + name_len].rstrip(b"\0").decode("latin-1", errors="replace")
            r.pos += name_len
            r.u32(); r.u16(); r.f32(); r.f32s(3); r.f32(); r.f32s(3); r.f32(); r.f32s(3); r.f32(); r.f32s(3)
            if flags & 0x10000:
                r.u32()
            else:
                r.f32()
            r.u32()
            if flags & 0x80000:
                r.f32s(6)
            r.f32s(6)
            if flags & 0x4000:
                mesh_key = r.u32(); pack_key = r.u32()
                if mesh_key in by_mesh:
                    by_mesh[mesh_key].material_pack_key = pack_key
                    by_mesh[mesh_key].object_name = name
        except (ValueError, struct.error):
            continue


def _parse_pop_file_entries(data: bytes) -> list[PopFileEntry]:
    """Parse the FileEntry stream used by bin_repacker_2018_05_29_0806."""
    entries: list[PopFileEntry] = []
    pos = 0
    while pos + 12 <= len(data):
        size, magic, key = struct.unpack_from("<III", data, pos)
        data_offset = pos + 12
        end = data_offset + size
        if end > len(data):
            raise ValueError(
                f"FileEntry #{len(entries)} oltre la fine del BIN: "
                f"offset=0x{pos:X}, size={size:,}, file={len(data):,}."
            )
        data_type = struct.unpack_from("<I", data, data_offset)[0] if size >= 4 else None
        entries.append(PopFileEntry(len(entries), pos, size, magic, key, data_offset, data_type))
        pos = end
    if pos != len(data):
        raise ValueError(f"FileEntry table non allineata: parsing fermato a 0x{pos:X} di 0x{len(data):X}.")
    return entries


def _scan_pop_textures(data: bytes) -> list[TextureInfo]:
    """Find textures by walking the real POP FileEntry table, not raw marker hits."""
    textures: list[TextureInfo] = []
    for entry in _parse_pop_file_entries(data):
        if entry.size < 56 or entry.data_type is None:
            continue
        marker = struct.unpack_from("<I", data, entry.data_offset + 32)[0]
        if marker != 0xC0DEC0DE:
            continue
        width, height = struct.unpack_from("<hh", data, entry.data_offset + 12)
        texture_type = struct.unpack_from("<I", data, entry.data_offset + 40)[0]
        if texture_type not in (0, 1, 7) or not (1 <= width <= 8192 and 1 <= height <= 8192):
            continue
        data_offset = entry.data_offset + (60 if texture_type == 1 else 56)
        data_end = entry.data_offset + entry.size
        if data_end <= data_offset:
            continue
        header_w, header_h = struct.unpack_from("<II", data, entry.data_offset + 44)
        stored_w, stored_h = width, height
        if texture_type in (0, 7):
            candidate_w, candidate_h = header_w // 2, header_h // 2
            candidate_size = candidate_w * candidate_h * 4
            if (candidate_w >= width and candidate_h >= height and candidate_size > 0
                    and (data_end - data_offset) >= candidate_size
                    and (data_end - data_offset) % candidate_size == 0):
                stored_w, stored_h = candidate_w, candidate_h
        if not (1 <= stored_w <= 16384 and 1 <= stored_h <= 16384):
            continue
        payload_size = data_end - data_offset
        if texture_type == 7:
            if payload_size == 4:
                fmt = "Raw BGRA8 (reference)"
            elif payload_size == stored_w * stored_h * 4:
                fmt = "Raw BGRA8"
            else:
                fmt = "Type 7 (unknown payload)"
        elif texture_type == 0:
            base_size = stored_w * stored_h * 4
            if payload_size == stored_w * stored_h * 3:
                fmt = "TGA/BGR24"
            elif payload_size >= base_size:
                fmt = "TGA/BGRA8"
            else:
                fmt = "Type 0 (truncated)"
        else:
            fmt = "8-bit palette"
        textures.append(TextureInfo(len(textures), entry.data_offset, data_offset, data_end,
                                    width, height, texture_type, entry.key, fmt,
                                    stored_w, stored_h))
    return textures


def _dds_payload_and_info(path: Path) -> tuple[bytes, int, int, str, bytes]:
    """Read a standard DDS and return compressed payload plus dimensions/format."""
    raw = path.read_bytes()
    if len(raw) < 128 or raw[:4] != b"DDS ":
        raise ValueError("Il file importato non è un DDS valido.")
    height, width = struct.unpack_from("<II", raw, 12)
    fourcc = raw[84:88].decode("ascii", errors="replace").strip("\0")
    if not width or not height or not fourcc:
        raise ValueError("DDS privo di dimensioni o FourCC di compressione.")
    return raw[128:], width, height, fourcc, raw[:128]


def _build_dds(raw_payload: bytes, width: int, height: int, header_template: bytes) -> bytes:
    header = bytearray(header_template)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    return bytes(header) + raw_payload


def _dds_blob_for_dump(texture_data: bytes | bytearray, tex: TextureInfo) -> bytes:
    """Build a standalone DDS while preserving the embedded compressed payload exactly."""
    if tex.texture_type != 7:
        raise ValueError("Solo le texture POP type 7 possono essere scaricate come DDS.")
    payload = bytes(texture_data[tex.data_offset:tex.data_end])
    if len(payload) == tex.storage_width * tex.storage_height * 4:
        return _build_tga_header(tex.storage_width, tex.storage_height, 32) + payload
    mip_count = _infer_dxt5_mip_count(tex.storage_width, tex.storage_height, len(payload))
    return _build_type7_dds(payload, tex.storage_width, tex.storage_height)


def _build_dds_header(width: int, height: int, num_mipmaps: int, compression: int) -> bytes:
    """Create a standard 128-byte DDS header without relying on PopTools."""
    if compression not in (0, 1, 2, 5, 6, 7, 11):
        raise ValueError(f"Compressione DDS POP non supportata: {compression}.")
    mip_count = max(1, num_mipmaps + 1)
    flags = 0x0002100F if mip_count == 1 else 0x000A1007
    caps = 0x00001000 if mip_count == 1 else 0x00401008
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, flags)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 28, mip_count)
    struct.pack_into("<I", header, 76, 32)
    struct.pack_into("<I", header, 80, 0x00000004)
    fourcc = {2: b"DXT1", 5: b"DXT1", 6: b"DXT3", 7: b"DXT5", 11: b"DXT5"}.get(compression)
    if fourcc is not None:
        header[84:88] = fourcc
    else:
        # 32-bit BGRA fallback for the uncompressed POP formats.
        struct.pack_into("<I", header, 80, 0x00000041)
        struct.pack_into("<I", header, 88, 32)
        struct.pack_into("<I", header, 92, 0x00FF0000)
        struct.pack_into("<I", header, 96, 0x0000FF00)
        struct.pack_into("<I", header, 100, 0x000000FF)
        struct.pack_into("<I", header, 104, 0xFF000000)
    struct.pack_into("<I", header, 108, caps)
    return bytes(header)


def _infer_dxt5_mip_count(width: int, height: int, payload_size: int) -> int:
    """Infer the number of DXT5 mip levels from the embedded payload size."""
    total = 0
    mip_count = 0
    w, h = width, height
    while mip_count < 16:
        total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16
        mip_count += 1
        if total == payload_size:
            return mip_count
        if total > payload_size or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    raise ValueError(
        f"Payload DXT5 originale {payload_size:,} B non corrisponde a un numero intero di mipmap per {width}x{height}."
    )


def _build_type7_dds(payload: bytes, width: int, height: int) -> bytes:
    """Build a DDS for POP type 7, accepting DXT5 or raw BGRA payloads."""
    try:
        mip_count = _infer_dxt5_mip_count(width, height, len(payload))
        return _build_dds(payload, width, height, _build_dds_header(width, height, mip_count - 1, 7))
    except ValueError as dxt5_error:
        try:
            mip_count = _infer_raw_bgra_mip_count(width, height, len(payload))
        except ValueError:
            raise dxt5_error
        return _build_dds(payload, width, height, _build_dds_header(width, height, mip_count - 1, 0))


def _infer_raw_bgra_mip_count(width: int, height: int, payload_size: int) -> int:
    """Infer mip levels for POP texture payloads stored as raw 32-bit BGRA."""
    total = 0
    mip_count = 0
    w, h = width, height
    while mip_count < 16:
        total += max(1, w) * max(1, h) * 4
        mip_count += 1
        if total == payload_size:
            return mip_count
        if total > payload_size or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    raise ValueError(
        f"Payload raw BGRA {payload_size:,} B non corrisponde a un numero intero di mipmap per {width}x{height}."
    )


def _dxt5_mip_sizes(width: int, height: int, mip_count: int) -> list[tuple[int, int]]:
    sizes: list[tuple[int, int]] = []
    w, h = width, height
    for _ in range(mip_count):
        sizes.append((w, h))
        w, h = max(1, w // 2), max(1, h // 2)
    return sizes


def _rgb565(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return ((r * 31 + 127) // 255 << 11) | ((g * 63 + 127) // 255 << 5) | ((b * 31 + 127) // 255)


def _unpack565(value: int) -> tuple[int, int, int]:
    r = ((value >> 11) & 31) * 255 // 31
    g = ((value >> 5) & 63) * 255 // 63
    b = (value & 31) * 255 // 31
    return r, g, b


def _encode_dxt5_block(pixels: list[tuple[int, int, int, int]]) -> bytes:
    alphas = [p[3] for p in pixels]
    a0, a1 = max(alphas), min(alphas)
    if a0 == a1:
        a0 = min(255, a0 + 1)
    alpha_palette = [a0, a1]
    if a0 > a1:
        alpha_palette.extend((
            (6 * a0 + a1) // 7,
            (5 * a0 + 2 * a1) // 7,
            (4 * a0 + 3 * a1) // 7,
            (3 * a0 + 4 * a1) // 7,
            (2 * a0 + 5 * a1) // 7,
            (a0 + 6 * a1) // 7,
        ))
    else:
        alpha_palette.extend(((4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
                              (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5, 0, 255))
    alpha_indices = 0
    for i, alpha in enumerate(alphas):
        index = min(range(8), key=lambda n: abs(alpha_palette[n] - alpha))
        alpha_indices |= index << (3 * i)

    colors = [(p[0], p[1], p[2]) for p in pixels]
    min_rgb = tuple(min(c[i] for c in colors) for i in range(3))
    max_rgb = tuple(max(c[i] for c in colors) for i in range(3))
    c0, c1 = _rgb565(max_rgb), _rgb565(min_rgb)
    if c0 == c1:
        c0 = min(0xFFFF, c0 + 1)
    if c0 < c1:
        c0, c1 = c1, c0
    rgb0, rgb1 = _unpack565(c0), _unpack565(c1)
    color_palette = [
        rgb0,
        rgb1,
        tuple((2 * rgb0[i] + rgb1[i]) // 3 for i in range(3)),
        tuple((rgb0[i] + 2 * rgb1[i]) // 3 for i in range(3)),
    ]
    color_indices = 0
    for i, color in enumerate(colors):
        index = min(range(4), key=lambda n: sum((color[j] - color_palette[n][j]) ** 2 for j in range(3)))
        color_indices |= index << (2 * i)
    return bytes((a0, a1)) + alpha_indices.to_bytes(6, "little") + c0.to_bytes(2, "little") + c1.to_bytes(2, "little") + color_indices.to_bytes(4, "little")


def _encode_dxt5(image, mip_count: int) -> bytes:
    """Pure-Python DXT5 encoder used so conversion has no external binary dependency."""
    from PIL import Image

    out = bytearray()
    for level, (width, height) in enumerate(_dxt5_mip_sizes(*image.size, mip_count)):
        level_image = image if level == 0 else image.resize((width, height), Image.Resampling.LANCZOS)
        rgba = level_image.load()
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                pixels: list[tuple[int, int, int, int]] = []
                for y in range(by, by + 4):
                    for x in range(bx, bx + 4):
                        pixels.append(rgba[min(x, width - 1), min(y, height - 1)])
                out.extend(_encode_dxt5_block(pixels))
    return bytes(out)


def _build_tga_header(width: int, height: int, pixel_depth: int = 32) -> bytes:
    """Build the small TGA header needed for POP's raw BGRA texture payloads."""
    if not (1 <= width <= 65535 and 1 <= height <= 65535):
        raise ValueError(f"Dimensioni TGA non valide: {width}x{height}.")
    if pixel_depth not in (24, 32):
        raise ValueError(f"Profondità TGA non supportata: {pixel_depth} bit.")
    # Uncompressed true-color, bottom-left origin. The embedded POP payload is
    # already pixel data, so no external tga_header.bin is required.
    header = bytearray(18)
    header[2] = 2
    struct.pack_into("<H", header, 12, width)
    struct.pack_into("<H", header, 14, height)
    header[16] = pixel_depth
    return bytes(header)


def _build_palette_tga(width: int, height: int, palette: bytes, indices: bytes) -> bytes:
    """Expand an 8-bit POP palette texture to a standalone 32-bit TGA."""
    pixel_count = width * height
    if len(palette) < 1024 or len(indices) < pixel_count:
        raise ValueError("Dati palette/indici insufficienti per questa texture.")
    decoded = bytearray(pixel_count * 4)
    for i, index in enumerate(indices[:pixel_count]):
        palette_pos = index * 4
        decoded[i * 4:i * 4 + 4] = palette[palette_pos:palette_pos + 4]
    return _build_tga_header(width, height, 32) + bytes(decoded)


def _valid_identifier(name: str) -> bool:
    return (
        3 <= len(name) < AI_MAX_LEN_VAR
        and any(c.isalpha() or c == "_" for c in name)
        and all(c.isalnum() or c in "_()" for c in name)
    )


def _looks_like_pop_lzo(data: bytes) -> bool:
    if len(data) < 18:
        return False
    dec_size, enc_size = struct.unpack_from("<2I", data, 0)
    if dec_size <= enc_size or enc_size <= 0 or dec_size > 16 * 1024 * 1024:
        return False
    # Retail POP uses 0x99C0FFEE. Some later tools use 0x99C0FFFE.
    # Both are the same block-LZO wrapper; rejecting FE makes compressed WOW
    # payloads look raw and the FileEntry parser then reads compressed bytes as sizes.
    markers = (b"\x99\xC0\xFF\xEE", b"\x99\xC0\xFF\xFE")
    return any(data[13:17] == marker or data[14:18] == marker for marker in markers)


def _decompress_lzo_block(block: bytes, expected_size: int) -> bytes:
    """Pure-Python LZO1X decoder for POP blocks (including match streams)."""
    ip = 0
    op = bytearray()
    last_match = False

    def need(n: int) -> None:
        if ip + n > len(block):
            raise ValueError("Blocco LZO troncato.")

    def copy_literals(n: int) -> None:
        nonlocal ip
        need(n)
        op.extend(block[ip:ip + n])
        ip += n

    if not block:
        raise ValueError("Blocco LZO vuoto.")
    t = block[ip]
    ip += 1
    if t > 17:
        copy_literals(t - 17)
        last_match = True
    else:
        t = 0

    while ip < len(block):
        if t <= 15:
            if t == 0:
                zeros = 0
                while ip < len(block) and block[ip] == 0:
                    zeros += 1
                    ip += 1
                if ip >= len(block):
                    raise ValueError("LZO literal length troncata.")
                t = 18 + zeros
            else:
                t += 3
            copy_literals(t)
            if ip >= len(block):
                break
            t = block[ip]
            ip += 1
        elif t <= 31:
            need(1)
            m_off = (t & 8) << 11
            m_off += block[ip] << 3
            m_off += (t >> 2) & 7
            m_off += 1
            match_len = (t & 3) + 2
            ip += 1
            need(1)
            m_off += block[ip] >> 5
            ip += 1
            if m_off > len(op):
                raise ValueError("LZO match offset non valido.")
            for _ in range(match_len):
                op.append(op[-m_off])
            last_match = True
            t = block[ip] if ip < len(block) else 17
            if ip < len(block):
                ip += 1
        else:
            if t >= 64:
                need(1)
                m_off = ((t >> 2) & 7) | (block[ip] << 3)
                m_off += 1
                match_len = (t >> 5) + 1
                ip += 1
            elif t >= 32:
                match_len = (t & 31) + 2
                need(2)
                m_off = (block[ip] >> 2) | ((t & 8) << 11)
                m_off += 1
                ip += 2
            else:
                if t == 17 and ip + 2 <= len(block):
                    break
                match_len = t & 7
                need(2)
                m_off = (block[ip] >> 2) | ((t & 8) << 11)
                m_off += 1
                ip += 2
                if match_len == 0:
                    while ip < len(block) and block[ip] == 0:
                        match_len += 255
                        ip += 1
                    need(1)
                    match_len += 31 + block[ip] + 2
                    ip += 1
                else:
                    match_len += 2
            if m_off > len(op):
                raise ValueError("LZO match offset non valido.")
            for _ in range(match_len):
                op.append(op[-m_off])
            last_match = True
            if ip >= len(block):
                break
            t = block[ip]
            ip += 1

        # LZO1X's state transition after a literal run can introduce the
        # short match form. The decoder above handles the control byte on the
        # next loop; keep the marker only for readability/debugging.
        _ = last_match

    if len(op) != expected_size:
        raise ValueError(f"Decompressione LZO: ottenuti {len(op)} B, attesi {expected_size} B.")
    return bytes(op)


def decompress_pop_lzo(data: bytes) -> bytes:
    """Decode POP v37/v38's sequence of little-endian LZO blocks."""
    compat = ROOT / "lzo_compat.dll"
    if compat.is_file():
        try:
            lib = ctypes.CDLL(str(compat))
            native = lib.lzo_bridge_decompress
            native.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint,
                ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_uint),
            ]
            native.restype = ctypes.c_int
        except (OSError, AttributeError):
            native = None
    else:
        native = None

    pos = 0
    output = bytearray()
    while pos + 8 <= len(data):
        dec_size, enc_size = struct.unpack_from("<2I", data, pos)
        pos += 8
        if dec_size == 0 and enc_size == 0:
            break
        if enc_size > len(data) - pos:
            raise ValueError("Blocco LZO POP troncato.")
        block = data[pos:pos + enc_size]
        pos += enc_size
        if dec_size == enc_size:
            output.extend(block)
        elif native is not None:
            src = (ctypes.c_ubyte * len(block)).from_buffer_copy(block)
            dst = (ctypes.c_ubyte * dec_size)()
            out_size = ctypes.c_uint(dec_size)
            rc = native(src, enc_size, dst, ctypes.byref(out_size))
            if rc != 0 or out_size.value != dec_size:
                raise ValueError(f"Decompressione LZO POP fallita (rc={rc}, output={out_size.value}, atteso={dec_size}).")
            output.extend(bytes(dst[:out_size.value]))
        else:
            output.extend(_decompress_lzo_block(block, dec_size))
        if dec_size < LZO_BLOCK_SIZE:
            break
    return bytes(output)


def _encode_literal_lzo(block: bytes) -> bytes:
    """Encode a small literal-only LZO1X stream; larger blocks stay raw."""
    if not block:
        return b"\x11\x00\x00\x00"
    if len(block) <= 238:
        return bytes((17 + len(block),)) + block + b"\x11\x00\x00"
    return block


def compress_pop_lzo(data: bytes) -> bytes:
    """Compress POP blocks using the bundled LZO 1.08 binary."""
    if len(data) < 8 or data[4:8] != b"\x99\xC0\xFF\xEE":
        raise ValueError("Il BIN non contiene il magic POP 0x99C0FFEE a offset 4.")
    helper = ROOT / "pop_lzo_native.ps1"
    dll = ROOT / "lzo.dll"
    if not helper.is_file() or not dll.is_file():
        raise RuntimeError("Supporto LZO standalone incompleto: mancano pop_lzo_native.ps1 o lzo.dll.")
    temp_dir = Path(tempfile.mkdtemp(prefix="mini_jade_lzo_"))
    source = temp_dir / "input.dec"
    target = temp_dir / "output.enc"
    try:
        source.write_bytes(data)
        powershell = Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe")
        if not powershell.is_file():
            raise RuntimeError("Windows PowerShell 32-bit non disponibile per il runtime LZO standalone.")
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper),
             "-InputFile", str(source), "-OutputFile", str(target)],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Compressione LZO POP fallita: {details or 'errore sconosciuto'}")
        if not target.is_file():
            raise RuntimeError("Il runtime LZO standalone non ha prodotto l'output.")
        return target.read_bytes()
    finally:
        for child in temp_dir.glob("*"):
            child.unlink(missing_ok=True)
        temp_dir.rmdir()


@dataclass
class BigFileEntry:
    index: int
    position: int
    key: int
    size: int
    name: str
    parent: int
    fat_index: int
    first_index: int
    compressed: bool = False
    data_header_size: int = 0
    compression: str = "none"


@dataclass
class BigFileInfo:
    path: Path
    version: int
    max_file: int
    max_dir: int
    size_fat: int
    num_fat: int
    universe_key: int
    encrypted_fat: bool
    entries: list[BigFileEntry]


def _read_big_header(stream) -> BigFileInfo:
    header = stream.read(44)
    if len(header) != 44:
        raise ValueError("File .bf troppo corto per contenere l'header Jade.")
    magic, version, max_file, max_dir, _max_key, _root, _free_file, _free_dir, size_fat, num_fat, universe_key = struct.unpack("<4s10I", header)
    if magic not in (b"BIG\0", b"BUG\0"):
        raise ValueError(f"Header BIG non riconosciuto: {magic!r}")
    entries: list[BigFileEntry] = []
    descriptor_pos = 44
    for fat_index in range(num_fat):
        stream.seek(descriptor_pos)
        raw = stream.read(24)
        if len(raw) != 24:
            raise ValueError(f"FAT descriptor #{fat_index} troncato.")
        fat_max_file, _fat_max_dir, pos_fat, next_pos_fat, first_index, _last_index = struct.unpack("<6I", raw)
        stream.seek(pos_fat)
        file_table = stream.read(fat_max_file * 8)
        ext_base = pos_fat + size_fat * 8
        stream.seek(ext_base)
        ext_table = stream.read(fat_max_file * 88)
        if len(file_table) != fat_max_file * 8 or len(ext_table) != fat_max_file * 88:
            raise ValueError(f"FAT #{fat_index} troncata.")
        for i in range(fat_max_file):
            position, key = struct.unpack_from("<2I", file_table, i * 8)
            if key == 0xFFFFFFFF:
                continue
            ext = ext_table[i * 88:(i + 1) * 88]
            size_on_disk = struct.unpack_from("<I", ext, 0)[0]
            parent = struct.unpack_from("<I", ext, 12)[0]
            raw_name = ext[20:84].split(b"\0", 1)[0]
            name = raw_name.decode("ascii", errors="replace").strip() or f"<file_{first_index + i:06d}>"
            entries.append(BigFileEntry(first_index + i, position, key, size_on_disk & 0x7FFFFFFF,
                                        name, parent, fat_index, first_index,
                                        bool(size_on_disk & 0x80000000)))
        descriptor_pos = next_pos_fat - 24 if next_pos_fat != 0xFFFFFFFF else descriptor_pos + 24
    entries.sort(key=lambda e: (e.fat_index, e.index))
    return BigFileInfo(Path(stream.name), version, max_file, max_dir, size_fat, num_fat, universe_key, magic == b"BUG\0", entries)


def _read_legacy_bigfile(path: Path) -> BigFileInfo:
    with path.open("rb") as stream:
        header = stream.read(LEGACY_BF_HEADER_SIZE)
        if len(header) != LEGACY_BF_HEADER_SIZE:
            raise ValueError("File .bf troppo corto per l'header POP/Jade legacy.")
        magic, version, fcount, dcount, _unk2, _unk3, capacity, _unk4, universe_key, _fcount2, _dcount2, _file_id_offset, _unk5, _unk6, _last = struct.unpack("<4sIIIQQIIIIIIiII", header)
        if magic != b"BIG\0" or version not in (37, 38):
            raise ValueError(f"Layout legacy POP non riconosciuto (magic={magic!r}, v={version}).")
        if not (1 <= fcount <= capacity <= 2_000_000):
            raise ValueError("Header .bf legacy non plausibile.")
        file_id_base = LEGACY_BF_HEADER_SIZE
        file_entry_base = file_id_base + capacity * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
        file_size = path.stat().st_size
        if file_entry_base + fcount * LEGACY_BF_FILE_ENTRY_SIZE > file_size:
            raise ValueError("Tabella FileEntry legacy troncata.")
        entries = []
        for i in range(fcount):
            stream.seek(file_id_base + i * 8)
            position, key = struct.unpack("<2I", stream.read(8))
            stream.seek(file_entry_base + i * LEGACY_BF_FILE_ENTRY_SIZE)
            ext = stream.read(LEGACY_BF_FILE_ENTRY_SIZE)
            size_on_disk, _next, _prev, parent, _timestamp = struct.unpack_from("<5I", ext, 0)
            name = ext[20:84].split(b"\0", 1)[0].decode("ascii", errors="replace").strip() or f"<file_{i:06d}>"
            if position + 4 > file_size:
                raise ValueError(f"Entry legacy #{i} punta oltre il file.")
            stream.seek(position + 4)
            prefix = stream.read(min(32, size_on_disk))
            compressed = _looks_like_pop_lzo(prefix)
            entries.append(BigFileEntry(i, position, key, size_on_disk, name, parent, 0, 0, compressed, 4,
                                        "POP-LZO" if compressed else "none"))
        return BigFileInfo(path, version, capacity, dcount, capacity, 1, universe_key, False, entries)


def read_bigfile(path: Path) -> BigFileInfo:
    with path.open("rb") as stream:
        header = stream.read(8)
    if len(header) == 8 and header[:4] == b"BIG\0" and struct.unpack_from("<I", header, 4)[0] in (37, 38):
        return _read_legacy_bigfile(path)
    with path.open("rb") as stream:
        return _read_big_header(stream)


def read_bigfile_entry(path: Path, entry: BigFileEntry) -> bytes:
    with path.open("rb") as stream:
        stream.seek(entry.position + entry.data_header_size)
        data = stream.read(entry.size & 0x7FFFFFFF)
    if len(data) != (entry.size & 0x7FFFFFFF):
        raise ValueError(f"Entry {entry.name} troncata nel .bf.")
    return decompress_pop_lzo(data) if entry.compressed and entry.compression == "POP-LZO" else data


def _repack_legacy_bigfile(path: Path, selected: BigFileEntry, decoded_data: bytes, output: Path) -> None:
    original = path.read_bytes()
    info = read_bigfile(path)
    if info.version not in (37, 38):
        raise ValueError("La ricostruzione BF automatica è implementata per v37/v38.")
    selected_payload = compress_pop_lzo(decoded_data) if selected.compressed else decoded_data
    original_payload_size = selected.size & 0x7FFFFFFF
    # Most boolean edits keep the encoded size unchanged. In that case a
    # surgical replacement is the strongest fidelity guarantee: every byte
    # of the original BF container, including private padding/unknown areas,
    # is retained verbatim.
    if len(selected_payload) == original_payload_size:
        rebuilt = bytearray(original)
        start = selected.position + selected.data_header_size
        rebuilt[start:start + len(selected_payload)] = selected_payload
        output.write_bytes(rebuilt)
        return
    payloads: dict[int, bytes] = {}
    for entry in info.entries:
        if entry.index == selected.index:
            payloads[entry.index] = selected_payload
        else:
            start = entry.position + entry.data_header_size
            length = entry.size & 0x7FFFFFFF
            payloads[entry.index] = original[start:start + length]
    # File-table order and physical payload order are not guaranteed to match.
    # Preserve the original physical ordering while updating each indexed
    # FileIdOffset to its new position.
    ordered_entries = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered_entries[0].position
    prefix = bytearray(original[:prefix_end])
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.max_file * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    cursor = prefix_end
    original_cursor = prefix_end
    for entry in ordered_entries:
        payload = payloads[entry.index]
        # Keep every unindexed byte between legacy entries. These gaps are
        # part of the container layout and must survive an unchanged rebuild.
        original_gap = original[original_cursor:entry.position]
        cursor += len(original_gap)
        struct.pack_into("<I", prefix, file_id_base + entry.index * 8, cursor)
        size_value = len(payload) | (0x80000000 if entry.compressed else 0)
        struct.pack_into("<I", prefix, file_entry_base + entry.index * LEGACY_BF_FILE_ENTRY_SIZE, size_value)
        cursor += 4 + len(payload)
        original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
    with output.open("wb") as stream:
        stream.write(prefix)
        original_cursor = prefix_end
        for entry in ordered_entries:
            payload = payloads[entry.index]
            stream.write(original[original_cursor:entry.position])
            # The legacy data-block header stores the physical payload size;
            # the compression bit lives in FileEntry.size in the FAT table.
            stream.write(struct.pack("<I", len(payload)))
            stream.write(payload)
            original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
        stream.write(original[original_cursor:])


def _repack_legacy_bigfile_changes(path: Path, replacements: dict[int, bytes], output: Path) -> None:
    """Rebuild a legacy BF while applying decoded payload changes to multiple entries."""
    original = path.read_bytes()
    info = read_bigfile(path)
    if info.version not in (37, 38):
        raise ValueError("La ricostruzione BF automatica è implementata per v37/v38.")

    entries_by_index = {entry.index: entry for entry in info.entries}
    payloads: dict[int, bytes] = {}
    for entry in info.entries:
        start = entry.position + entry.data_header_size
        length = entry.size & 0x7FFFFFFF
        if entry.index in replacements:
            payload = compress_pop_lzo(replacements[entry.index]) if entry.compressed else replacements[entry.index]
        else:
            payload = original[start:start + length]
        payloads[entry.index] = payload

    if all(len(payloads[index]) == (entries_by_index[index].size & 0x7FFFFFFF)
           for index in payloads):
        rebuilt = bytearray(original)
        for index in replacements:
            entry = entries_by_index[index]
            start = entry.position + entry.data_header_size
            rebuilt[start:start + len(payloads[index])] = payloads[index]
        output.write_bytes(rebuilt)
        return

    ordered_entries = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered_entries[0].position
    prefix = bytearray(original[:prefix_end])
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.max_file * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    cursor = prefix_end
    original_cursor = prefix_end
    for entry in ordered_entries:
        payload = payloads[entry.index]
        original_gap = original[original_cursor:entry.position]
        cursor += len(original_gap)
        struct.pack_into("<I", prefix, file_id_base + entry.index * 8, cursor)
        size_value = len(payload) | (0x80000000 if entry.compressed else 0)
        struct.pack_into("<I", prefix, file_entry_base + entry.index * LEGACY_BF_FILE_TABLE_ENTRY_SIZE, size_value)
        cursor += 4 + len(payload)
        original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)

    with output.open("wb") as stream:
        stream.write(prefix)
        original_cursor = prefix_end
        for entry in ordered_entries:
            payload = payloads[entry.index]
            stream.write(original[original_cursor:entry.position])
            stream.write(struct.pack("<I", len(payload)))
            stream.write(payload)
            original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
        stream.write(original[original_cursor:])


def _patch_texture_key_in_asset(asset_data: bytes, texture_key: int, replacement_payload: bytes,
                                texture_type: int, width: int, height: int) -> tuple[bytes, int]:
    """Patch every matching copy of a POP texture key inside one decoded asset."""
    data = bytearray(asset_data)
    matches = 0
    for tex in _scan_pop_textures(data):
        if tex.key != texture_key:
            continue
        if tex.texture_type != texture_type or tex.width != width or tex.height != height:
            continue
        if tex.data_end - tex.data_offset != len(replacement_payload):
            continue
        data[tex.data_offset:tex.data_end] = replacement_payload
        matches += 1
    return bytes(data), matches


def _collect_texture_key_replacements(path: Path, selected: BigFileEntry,
                                      texture_key: int, replacement_payload: bytes,
                                      texture_type: int, width: int, height: int,
                                      selected_decoded: bytes) -> tuple[dict[int, bytes], list[str]]:
    """Find the same logical texture in other BF assets and patch all valid copies.

    Jade/POP assets can carry the same texture key in more than one container.
    The editor/repacker can therefore show the edited copy while the game later
    resolves another copy of the same key. Updating all matching copies keeps
    the key-to-payload invariant intact without changing material references.
    """
    info = read_bigfile(path)
    replacements: dict[int, bytes] = {}
    touched: list[str] = []

    for entry in info.entries:
        if entry.index == selected.index:
            decoded = selected_decoded
        else:
            try:
                decoded = read_bigfile_entry(path, entry)
            except Exception:
                continue
        patched, count = _patch_texture_key_in_asset(
            decoded, texture_key, replacement_payload, texture_type, width, height
        )
        if count:
            replacements[entry.index] = patched
            touched.append(f"{entry.index}:{entry.name} ({count}x)")

    if selected.index not in replacements:
        raise ValueError(
            f"La texture 0x{texture_key:08X} non è stata trovata nell'asset selezionato "
            "con dimensioni/formato compatibili."
        )
    return replacements, touched


def _find_init_buffer_after_names(data: bytes, pos: int) -> tuple[int | None, int | None]:
    if pos + 4 > len(data):
        return None, None
    var2_size = struct.unpack_from("<I", data, pos)[0]
    if pos + 8 <= len(data):
        strings_size = struct.unpack_from("<I", data, pos + 4)[0]
        p = pos + 8 + var2_size + strings_size
        if var2_size <= 1024 * 1024 and strings_size <= 1024 * 1024 and p + 4 <= len(data):
            init_size = struct.unpack_from("<I", data, p)[0]
            if init_size <= len(data) - p - 4:
                return p + 4, init_size
    # POP37/38 may serialize the initial-value size directly after the names.
    if 0 < var2_size <= len(data) - pos - 4:
        return pos + 4, var2_size
    return None, None


def _pop_type_storage_size(var_type: int) -> int:
    """Serialized size of one POP/Jade variable element, from AI_gast_Types."""
    return {
        32: 4, 33: 4, 34: 4, 37: 12, 38: 4, 39: 4, 40: 4, 41: 4,
        42: 4, 43: 4, 44: 8, 45: 4, 46: 4, 48: 4, 49: 4, 50: 8,
        51: 96,
    }.get(var_type, 4)


def _recover_pop_init_buffer(data: bytes, structure: OvaStructure) -> tuple[int | None, int | None]:
    """Recover POP initial values when the editor-name table is absent/stripped."""
    if structure.init_base is not None and structure.init_size is not None:
        return structure.init_base, structure.init_size
    if structure.records_base is None or structure.container_end is None:
        return None, None

    records_end = structure.records_base + structure.count * OVA_INFO_SIZE
    if records_end + 4 > structure.container_end:
        return None, None

    required_size = 0
    for i in range(structure.count):
        info = structure.records_base + i * OVA_INFO_SIZE
        if info + OVA_INFO_SIZE > len(data):
            return None, None
        num_elem, packed_type_flags, var_offset = struct.unpack_from("<III", data, info)
        var_type = packed_type_flags & 0xFFFF
        elem_count = max(1, num_elem & 0x3FFFFFFF)
        dimensions = (num_elem >> 30) & 0x3
        end = var_offset + dimensions * 4 + elem_count * _pop_type_storage_size(var_type)
        if end < 0 or end > 1024 * 1024:
            return None, None
        required_size = max(required_size, end)
    if required_size <= 0:
        return None, None

    candidates: list[tuple[int, int]] = []
    names_end = structure.names_base + structure.names_size
    search_end = structure.container_end - required_size - 20
    for size_pos in range(records_end, max(records_end, search_end + 1), 4):
        if size_pos + 4 > len(data):
            break
        size = struct.unpack_from("<I", data, size_pos)[0]
        if size != required_size:
            continue
        # A VarInfo2 byte-size is also a multiple of 20. In stripped prototype
        # entries it can equal i_SizeInit, so do not treat that field as init size.
        if structure.names_size and size_pos == names_end and size % 20 == 0:
            continue
        init_base = size_pos + 4
        init_end = init_base + size
        if init_end + 20 > structure.container_end:
            continue
        candidates.append((init_base, size))

    if not candidates:
        return None, None
    if structure.names_available:
        outside = [c for c in candidates if c[0] >= names_end]
        if outside:
            return outside[0]
    return candidates[0]


def _detect_pop_name_span(data: bytes, names_base: int, count: int, container_end: int, full_span: int) -> int:
    full_end = names_base + full_span
    if full_end <= container_end:
        valid = sum(bool(_valid_identifier(data[names_base + i * AI_MAX_LEN_VAR:min(names_base + (i + 1) * AI_MAX_LEN_VAR, len(data))].split(b"\0", 1)[0].decode("ascii", errors="ignore"))) for i in range(count))
        if valid >= max(2, min(count, 8)):
            return full_span
    for end in range(names_base + 32, min(full_end, container_end) + 1):
        if end + 4 > container_end:
            break
        var2_size = struct.unpack_from("<I", data, end)[0]
        if not var2_size or var2_size > 1024 * 1024 or var2_size % 20 or end + 4 + var2_size > container_end:
            continue
        return end - names_base
    return 0


def _find_pop_ova_structures(data: bytes) -> list[OvaStructure]:
    candidates = []
    marker = b"\x99\xC0\xFF\xEE"
    start = 0
    while True:
        base = data.find(marker, start)
        if base < 0:
            break
        start = base + 4
        if base + 20 > len(data):
            continue
        kind, _unused, var_bytes = struct.unpack_from("<3I", data, base + 4)
        if kind != 0x0A000109 or not var_bytes or var_bytes % OVA_INFO_SIZE:
            continue
        count = var_bytes // OVA_INFO_SIZE
        if count > 4096:
            continue
        records_base = base + 20
        names_base = records_base + var_bytes
        names_span = count * AI_MAX_LEN_VAR
        container_end = len(data)
        if base >= 4:
            entry_size = struct.unpack_from("<I", data, base - 4)[0]
            if base + 8 + entry_size <= len(data):
                container_end = base + 8 + entry_size
        detected = _detect_pop_name_span(data, names_base, count, container_end, names_span)
        valid = 0
        if detected == names_span:
            for i in range(count):
                raw = data[names_base + i * AI_MAX_LEN_VAR:names_base + (i + 1) * AI_MAX_LEN_VAR]
                name = raw.split(b"\0", 1)[0].decode("ascii", errors="ignore")
                valid += int(bool(_valid_identifier(name)))
        names_available = detected == names_span
        names_end = names_base + detected
        init_base = None
        init_size = None
        candidates.append(OvaStructure(base, count, names_base, detected, valid,
                                       not names_available, init_base, init_size,
                                       source_format="POP37/38",
                                       records_base=records_base,
                                       names_available=names_available,
                                       names_encrypted=detected > 0 and valid == 0,
                                       container_end=container_end,
                                       name_slots=(detected + 29) // 30 if detected else 0))
    return candidates


def find_ova_structures(data: bytes) -> list[OvaStructure]:
    candidates = _find_pop_ova_structures(data)
    for base in range(0, max(0, len(data) - 8)):
        size_r = struct.unpack_from("<I", data, base)[0]
        if not size_r or size_r % OVA_INFO_SIZE:
            continue
        count = size_r // OVA_INFO_SIZE
        if count < 1 or count > 4096 or base + 4 + size_r + 4 > len(data):
            continue
        names_size = struct.unpack_from("<I", data, base + 4 + size_r)[0]
        if names_size != count * AI_MAX_LEN_VAR:
            continue
        names_base = base + 8 + size_r
        available = len(data) - names_base
        valid = 0
        for i in range(min(count, max(0, (available + 29) // 30))):
            raw = data[names_base + i * 30:min(names_base + (i + 1) * 30, len(data))]
            valid += int(bool(_valid_identifier(raw.split(b"\0", 1)[0].decode("ascii", errors="ignore"))))
        if valid < max(1, min(count, 2)):
            continue
        names_available = available >= names_size and valid == count
        init_base, init_size = _find_init_buffer_after_names(data, names_base + names_size)
        candidates.append(OvaStructure(base, count, names_base, names_size, valid,
                                       not names_available, init_base, init_size,
                                       names_available=names_available,
                                       names_encrypted=(available >= names_size and valid == 0)))
    return candidates


def find_variables(data: bytes) -> list[OvaVariable]:
    structures = find_ova_structures(data)
    if not structures:
        return []
    structure = max(structures, key=lambda s: (s.source_format == "POP37/38", s.complete_names, -s.base))
    if structure.source_format == "POP37/38" and structure.init_base is None:
        structure.init_base, structure.init_size = _recover_pop_init_buffer(data, structure)
    result = []
    for i in range(structure.count):
        if structure.source_format == "POP37/38":
            info = structure.records_base + i * OVA_INFO_SIZE
            _num_elem, packed, var_offset = struct.unpack_from("<III", data, info)
            var_type, flags = packed & 0xFFFF, (packed >> 16) & 0xFFFF
            if structure.names_available:
                slot = structure.names_base + i * 30
                raw = data[slot:min(slot + 30, len(data))]
                name = raw.split(b"\0", 1)[0].decode("ascii", errors="ignore")
            else:
                slot = info
                name = f"OVA_{i + 1:03d}"
        else:
            slot = structure.names_base + i * 30
            raw = data[slot:min(slot + 30, len(data))]
            name = raw.split(b"\0", 1)[0].decode("ascii", errors="ignore")
            info = structure.base + 4 + i * OVA_INFO_SIZE
            var_offset, _num_elem, var_type, flags = struct.unpack_from("<iihh", data, info)
            if not name or not _valid_identifier(name):
                name = f"OVA_{i + 1:03d}"
        if name:
            value_absolute = None
            value_size = None
            if structure.init_base is not None and structure.init_size is not None:
                # POP37/38 stores VarInfo offsets relative to the initial
                # value size field.  The reference OVA editor therefore
                # effectively has a -4 serialization bias: the byte at the
                # first value offset is four bytes after the stored anchor.
                # Resolve it here so the UI always points at the actual value
                # byte (e.g. mb_CheatsEnabled at 0x580).
                if structure.source_format == "POP37/38":
                    candidate = structure.init_base - 4 + var_offset
                else:
                    candidate = structure.init_base + var_offset
                relative = candidate - structure.init_base
                if 0 <= relative < structure.init_size:
                    value_absolute = candidate
                    elem_count = max(1, _num_elem & 0x3FFFFFFF)
                    dimensions = (_num_elem >> 30) & 0x3
                    value_size = min(
                        _pop_type_storage_size(var_type) * elem_count + dimensions * 4,
                        structure.init_size - relative,
                    )
            suffix = ""
            if not structure.names_available:
                suffix = " (editor names unavailable)" if not structure.names_encrypted else " (editor names encrypted/unavailable)"
            result.append(OvaVariable(name, slot, f"{structure.source_format} OVA @ 0x{structure.base:08X}{suffix}",
                                      var_offset, var_type, flags, structure.base, value_absolute,
                                      value_size, _num_elem))
    return result


def _find_ascii_fallback(data: bytes) -> list[OvaVariable]:
    found = []
    for match in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]{2,}", data):
        name = match.group().decode("ascii", errors="ignore")
        if _valid_identifier(name) and name.lower() not in {"ova", "ofc"}:
            found.append(OvaVariable(name, match.start(), "ASCII fallback"))
    return found


def _image_rgba(path: Path, width: int, height: int):
    """Load any Pillow-supported image and normalize it to the target dimensions."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow non è disponibile. Installalo con: python -m pip install Pillow") from exc
    try:
        image = Image.open(path).convert("RGBA")
    except Exception as exc:
        raise ValueError(f"Immagine non leggibile: {exc}") from exc
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    return image


def _transform_texture_image(image, rotation: int, flip_x: bool, flip_y: bool):
    """Apply the non-destructive Texture Swap orientation controls to a Pillow image."""
    from PIL import Image
    if flip_x:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip_y:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if rotation:
        image = image.rotate(-rotation, expand=False, resample=Image.Resampling.BICUBIC)
    return image


def _dds_payload_from_file(path: Path, texture: TextureInfo, original: bytes) -> bytes:
    """Convert any Pillow-readable image to the exact POP DXT5 payload size."""
    expected = texture.data_end - texture.data_offset

    # Preserve an already-compatible DDS bitstream byte-for-byte. This also
    # supports unusual DDS headers while enforcing the game's payload contract.
    raw = path.read_bytes()
    if raw[:4] == b"DDS ":
        if len(raw) < 128:
            raise ValueError("DDS troppo corto: header mancante.")
        width, height = struct.unpack_from("<II", raw, 12)
        fourcc = raw[84:88]
        if width == texture.width and height == texture.height and fourcc == b"DXT5":
            payload = raw[128:]
            if len(payload) == expected:
                return payload
        # A DDS with another size/format is treated like every other input
        # image and decoded/re-encoded to the original POP contract below.

    try:
        from PIL import Image  # noqa: F401 - validates the bundled runtime early
    except ImportError as exc:
        raise ValueError("Pillow non è disponibile. Il runtime bundled dovrebbe contenere vendor/PIL.") from exc

    # Encode DXT5 ourselves. This preserves the exact block/mipmap count of the
    # original payload and does not depend on ImageMagick or another executable.
    image = _image_rgba(path, texture.width, texture.height)
    mip_count = _infer_dxt5_mip_count(texture.width, texture.height, expected)
    payload = _encode_dxt5(image, mip_count)
    if len(payload) != expected:
        raise ValueError(f"Conversione DDS non compatibile: originale {expected:,} B, convertito {len(payload):,} B.")
    return payload


def _tga_payload_from_file(path: Path, texture: TextureInfo, original: bytes) -> bytes:
    """Convert an image to POP type-0 pixels while preserving any extra native data.

    POP's type-0 entries are not guaranteed to contain only width*height*4 bytes.
    The legacy tools read the first image-sized region and keep the remainder of
    the FileEntry untouched. Some game assets therefore have a larger payload
    than the visible RGBA surface (for example 43,776 B for a 128x64 image,
    whose base surface is 32,768 B). Replacing only the base surface preserves
    that extra data and keeps the FileEntry size byte-for-byte compatible.
    """
    image = _image_rgba(path, texture.width, texture.height)
    rgba = image.tobytes()
    base_size = texture.width * texture.height * 4
    expected = texture.data_end - texture.data_offset

    # A few legacy type-0 assets are 24-bit. Match that representation when the
    # original payload size proves it unambiguously; otherwise use the normal
    # 32-bit BGRA representation used by the existing TGA export path.
    if expected == texture.width * texture.height * 3:
        payload = bytearray(texture.width * texture.height * 3)
        for src, dst in zip(range(0, len(rgba), 4), range(0, len(payload), 3)):
            r, g, b, _a = rgba[src:src + 4]
            payload[dst:dst + 3] = bytes((b, g, r))
        return bytes(payload)

    payload = bytearray(base_size)
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i:i + 4]
        payload[i:i + 4] = bytes((b, g, r, a))

    if expected < base_size:
        raise ValueError(
            f"Conversione TGA non compatibile: originale {expected:,} B, "
            f"servono almeno {base_size:,} B per {texture.width}x{texture.height} RGBA."
        )

    # Preserve the bytes after the visible base surface. The original POP
    # tools do the same when decoding type-0 entries, and those bytes may carry
    # legacy mip/detail data or padding that must remain in the FileEntry.
    if expected > base_size:
        tail_start = texture.data_offset + base_size
        original_tail = original[tail_start:texture.data_end]
        if len(original_tail) != expected - base_size:
            raise ValueError(
                f"Conversione TGA: coda originale non leggibile ({len(original_tail):,} B, "
                f"attesi {expected - base_size:,} B)."
            )
        payload.extend(original_tail)
    return bytes(payload)


def _palette_payload_from_file(path: Path, texture: TextureInfo, palette: bytes) -> bytes:
    """Convert any image to indices into the original POP 256-color palette."""
    if len(palette) < 1024:
        raise ValueError("Palette POP incompleta: servono 256 colori RGBA.")
    image = _image_rgba(path, texture.width, texture.height)
    palette_rgba = [tuple(palette[i:i + 4]) for i in range(0, 1024, 4)]
    pixels = image.getdata()
    indices = bytearray(texture.width * texture.height)
    # A small cache avoids repeating the nearest-color search for flat/limited
    # color images while keeping the conversion fully self-contained.
    cache: dict[tuple[int, int, int, int], int] = {}
    for i, pixel in enumerate(pixels):
        if pixel not in cache:
            cache[pixel] = min(range(256), key=lambda n: sum((pixel[c] - palette_rgba[n][c]) ** 2 for c in range(4)))
        indices[i] = cache[pixel]
    expected = texture.data_end - texture.data_offset
    if len(indices) != expected:
        raise ValueError(f"Conversione palette non compatibile: originale {expected:,} B, convertito {len(indices):,} B.")
    return bytes(indices)


def ova_diagnostic_report(data: bytes, label: str = "buffer") -> list[str]:
    structures = find_ova_structures(data)
    markers = [m.start() for m in re.finditer(b"ova", data)]
    lines = [f"[ANALISI] {label}: {len(data):,} B | marker ASCII 'ova': {len(markers)} | descrittori OVA: {len(structures)}"]
    if markers:
        lines.append("  marker 'ova' a " + ", ".join(f"0x{x:08X}" for x in markers[:8]))
    if not structures:
        lines.append(f"  NO STRUCTURAL DESCRIPTOR: fallback ASCII {len(_find_ascii_fallback(data))} stringhe (non usato come OVA).")
        return lines
    for s in structures:
        state = "nomi in chiaro" if s.names_available else ("nomi cifrati/trasformati" if s.names_encrypted else "tabella nomi non inclusa")
        records = f" records @ 0x{s.records_base:08X}" if s.records_base is not None else ""
        lines.append(f"  OK {s.source_format} @ 0x{s.base:08X}:{records} {s.count} record da 12 B | nomi @ 0x{s.names_base:08X} ({s.names_size} B; {state}; validi {s.complete_names}/{s.count})")
    variables = find_variables(data)
    lines.append(f"  RISULTATO: {len(variables)} variabili mostrate.")
    return lines


@dataclass
class Asset:
    name: str
    index: int
    key: int
    position: int
    size: int
    compressed: bool
    fat_index: int = 0


class JadeProject:
    def __init__(self) -> None:
        self.path: Optional[Path] = None
        self.kind: Optional[str] = None
        self.info = None
        self.assets: list[Asset] = []
        self.raw_bin: Optional[bytes] = None
        self.decoded_bin: Optional[bytes] = None
        self.direct_compressed = False
        self.modified = False

    @property
    def title(self) -> str:
        return self.path.name if self.path else "Nessun file aperto"

    def open_bf(self, path: Path) -> None:
        info = read_bigfile(path)
        self.path = path
        self.kind = "bf"
        self.info = info
        self.raw_bin = None
        self.decoded_bin = None
        self.direct_compressed = False
        self.assets = [
            Asset(e.name, e.index, e.key, e.position, e.size & 0x7FFFFFFF,
                  bool(e.compressed), e.fat_index)
            for e in info.entries
        ]
        self.modified = False

    def open_bin(self, path: Path) -> None:
        raw = path.read_bytes()
        decoded = raw
        compressed = False
        if _looks_like_pop_lzo(raw):
            decoded = decompress_pop_lzo(raw)
            compressed = True
        self.path = path
        self.kind = "bin"
        self.info = None
        self.raw_bin = raw
        self.decoded_bin = decoded
        self.direct_compressed = compressed
        self.assets = [Asset(path.name, 0, 0, 0, len(decoded), compressed)]
        self.modified = False

    def read_asset(self, asset: Asset) -> bytes:
        if self.kind == "bin":
            return self.decoded_bin or b""
        entry = next(e for e in self.info.entries if e.index == asset.index)
        return read_bigfile_entry(self.path, entry)

    def save_bin_as(self, target: Path, data: bytes) -> None:
        encoded = compress_pop_lzo(data) if self.direct_compressed else data
        target.write_bytes(encoded)

    def replace_bf_entry(self, asset: Asset, data: bytes, target: Path) -> None:
        entry = next(e for e in self.info.entries if e.index == asset.index)
        _repack_legacy_bigfile(self.path, entry, data, target)


class JadeToolkit(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PoP BF Lab")
        self.geometry("1380x860")
        self.minsize(1120, 720)
        self._dark_mode = True
        self._setup_theme()
        self.project = JadeProject()
        self._asset_map: dict[str, Asset] = {}
        self._ova_data = bytearray()
        self._ova_original = b""
        self._ova_variables: list[OvaVariable] = []
        self._ova_source_asset: Asset | None = None
        self._ova_dirty = False
        self._ova_candidate_offsets: list[int] = []
        self.ova_variable_mode = "jade"
        self._ova_jade_variables: list[OvaVariable] = []
        self._ova_ascii_variables: list[OvaVariable] = []
        self._texture_data = bytearray()
        self._texture_infos: list[TextureInfo] = []
        self._texture_file_entries: list[PopFileEntry] = []
        self._texture_source_asset: Asset | None = None
        self._texture_original = b""
        self._texture_dirty = False
        self._texture_image = None
        self._texture_photo = None
        self._texture_rotation = 0
        self._texture_flip_x = False
        self._texture_flip_y = False
        self._mesh_data = bytearray()
        self._mesh_infos: list[MeshInfo] = []
        self._mesh_source_asset: Asset | None = None
        self._mesh_textures: dict[int, object] = {}
        self._mesh_material_textures: dict[int, int] = {}
        self._mesh_texture_photos: list[object] = []
        self._material_data = bytearray()
        self._material_infos: list[MaterialInfo] = []
        self._material_source_asset: Asset | None = None
        self._material_textures: dict[int, object] = {}
        self._material_texture_photos: list[object] = []
        self._material_diffuse_path = tk.StringVar()
        self._material_normal_path = tk.StringVar()
        self._material_metallic = tk.DoubleVar(value=0.0)
        self._material_alpha = tk.DoubleVar(value=1.0)
        self._material_projection = tk.DoubleVar(value=1.0)
        self._material_shape = tk.StringVar(value="sphere")
        self._material_dirty = False
        self._material_preview_photo = None
        self._mesh_drag = None
        self._mesh_yaw = -0.45
        self._mesh_pitch = 0.18
        self._mesh_zoom = 1.0
        self._mesh_render_after = None
        self._build_menu()
        self._build_ui()
        self._log("INFO  PoP BF Lab pronto — core BF + POP-LZO + OVA integrato.")
        self._refresh_title()

    def _setup_theme(self) -> None:
        """Configure the permanent dark palette."""
        self._dark = {
            "bg": "#1e1f22", "surface": "#2b2d31", "surface_alt": "#2b2d31",
            "surface_hover": "#36383d", "field": "#17181a", "fg": "#f2f3f5", "muted": "#b5bac1",
            "accent": "#4752c4", "select": "#4752c4", "border": "#3f4147",
            "border_soft": "#3f4147", "accent_border": "#4752c4",
        }
        self._apply_theme()

    def _apply_theme(self) -> None:
        palette = self._dark
        self.configure(bg=palette["bg"])
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=palette["bg"], foreground=palette["fg"], font=("Segoe UI", 10))
        style.configure("TFrame", background=palette["bg"])
        style.configure("TLabel", background=palette["bg"], foreground=palette["fg"], padding=1)
        style.configure("TLabelframe", background=palette["bg"], foreground=palette["fg"])
        style.configure("TLabelframe.Label", background=palette["bg"], foreground=palette["fg"])
        style.configure("TEntry", fieldbackground=palette["field"], foreground=palette["fg"],
                        insertcolor=palette["fg"], bordercolor=palette["border"], padding=(9, 6))
        style.configure("TCombobox", fieldbackground=palette["field"], background=palette["surface"],
                        foreground=palette["fg"], arrowcolor=palette["fg"], bordercolor=palette["border"])
        style.map("TCombobox", fieldbackground=[("readonly", palette["field"])],
                  foreground=[("readonly", palette["fg"])])
        style.configure("TButton", background=palette["surface"], foreground=palette["fg"],
                        bordercolor=palette["border"], padding=(10, 7))
        style.map("TButton", background=[("active", palette["select"]), ("disabled", palette["field"])],
                  foreground=[("disabled", palette["muted"])])
        style.configure("TNotebook", background=palette["bg"], bordercolor=palette["border"], tabmargins=(2, 2, 2, 0))
        style.configure("TNotebook.Tab", background=palette["surface"], foreground=palette["fg"], padding=(15, 9), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", palette["select"])], foreground=[("selected", palette["fg"])])
        style.configure("Treeview", background=palette["field"], fieldbackground=palette["field"],
                        foreground=palette["fg"], bordercolor=palette["border_soft"], rowheight=30, relief="flat")
        style.map("Treeview", background=[("selected", palette["select"])], foreground=[("selected", palette["fg"])])
        style.configure("Treeview.Heading", background=palette["surface_alt"], foreground=palette["muted"],
                        bordercolor=palette["border_soft"], relief="flat", padding=(9, 8))
        style.configure("TPanedwindow", background=palette["bg"])
        style.configure("TScrollbar", background=palette["surface_alt"], troughcolor=palette["field"],
                        bordercolor=palette["border_soft"], arrowcolor=palette["muted"], relief="flat", width=12)
        style.configure("TCheckbutton", background=palette["bg"], foreground=palette["fg"])
        style.map("TCheckbutton", foreground=[("disabled", palette["muted"])])
        style.configure("TSeparator", background=palette["border"])
        self.option_add("*TCombobox*Listbox.background", palette["field"])
        self.option_add("*TCombobox*Listbox.foreground", palette["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", palette["select"])
        self.option_add("*TCombobox*Listbox.selectForeground", palette["fg"])
        self.option_add("*Listbox.background", palette["field"])
        self.option_add("*Listbox.foreground", palette["fg"])
        self.option_add("*Listbox.selectBackground", palette["select"])
        self.option_add("*Listbox.selectForeground", palette["fg"])
        for name in ("asset_details", "ova_text", "log_text", "ova_hex_text"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(bg=palette["field"], fg=palette["fg"], insertbackground=palette["fg"],
                                 selectbackground=palette["select"], selectforeground=palette["fg"],
                                 highlightbackground=palette["border_soft"], highlightcolor=palette["border"])
        if hasattr(self, "status"):
            self.status.configure(background=palette["surface"], foreground=palette["fg"])

    def _build_menu(self) -> None:
        palette = self._dark
        menu = tk.Menu(self, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                       activebackground=palette["select"], activeforeground=palette["fg"],
                       borderwidth=0)
        file_menu = tk.Menu(menu, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                            activebackground=palette["select"], activeforeground=palette["fg"], borderwidth=0)
        file_menu.add_command(label="Import .BF...", command=self.import_bf)
        file_menu.add_command(label="Save modified .BF as...", command=self.save_edited_bf)
        file_menu.add_command(label="Import .BIN...", command=self.import_bin)
        file_menu.add_separator()
        file_menu.add_command(label="Extract selected asset...", command=self.extract_selected)
        file_menu.add_command(label="Extract all assets", command=self.extract_all_assets)
        file_menu.add_command(label="Save edited .BIN as...", command=self.save_bin)
        file_menu.add_command(label="Rebuild .BF as...", command=self.rebuild_bf)
        file_menu.add_separator()
        file_menu.add_command(label="Close", command=self.close_project)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        menu.add_cascade(label="File", menu=file_menu)

        tools = tk.Menu(menu, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                        activebackground=palette["select"], activeforeground=palette["fg"], borderwidth=0)
        tools.add_command(label="Refresh Asset Browser", command=self.refresh_assets)
        tools.add_command(label="Diagnose OVA", command=self.diagnose_ova)
        tools.add_command(label="Open program folder", command=self.open_tools_folder)
        menu.add_cascade(label="Tools", menu=tools)

        help_menu = tk.Menu(menu, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                            activebackground=palette["select"], activeforeground=palette["fg"], borderwidth=0)
        help_menu.add_command(label="About Jade Toolkit", command=self.about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menu)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root, padding=(2, 0, 2, 12))
        header.pack(fill="x")
        title_group = ttk.Frame(header)
        title_group.pack(side="left")
        ttk.Label(title_group, text="PoP BF Lab", font=("Segoe UI Semibold", 20)).pack(anchor="w")
        ttk.Label(title_group, text="Jade assets  •  OVA variables  •  Big File editing").pack(anchor="w", pady=(3, 0))

        toolbar = ttk.Frame(root)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Import .BF", command=self.import_bf).pack(side="left")
        ttk.Button(toolbar, text="Salva .BF modificato", command=self.save_edited_bf).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Import .BIN", command=self.import_bin).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Salva .BIN modificato", command=self.save_bin).pack(side="left", padx=6)
        self.file_label = ttk.Label(toolbar, text="Nessun file aperto")
        self.file_label.pack(side="right")

        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True)
        self.asset_tab = ttk.Frame(self.tabs, padding=8)
        self.ova_tab = ttk.Frame(self.tabs, padding=8)
        self.level_tab = ttk.Frame(self.tabs, padding=8)
        self.mesh_tab = ttk.Frame(self.tabs, padding=8)
        self.texture_tab = ttk.Frame(self.tabs, padding=8)
        self.material_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.asset_tab, text="Asset Browser")
        self.tabs.add(self.ova_tab, text="OVA Variables")
        self.tabs.add(self.level_tab, text="Level Editor")
        self.tabs.add(self.mesh_tab, text="Mesh Editor")
        self.tabs.add(self.material_tab, text="Material Editor")
        self.tabs.add(self.texture_tab, text="Texture Editor")
        self._build_asset_tab()
        self._build_ova_tab()
        self._build_level_tab()
        self._build_mesh_tab()
        self._build_material_tab()
        self._build_texture_tab()

        self.status = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", pady=(8, 0))

    def _build_asset_tab(self) -> None:
        top = ttk.Frame(self.asset_tab)
        top.pack(fill="x")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh_assets())
        ttk.Label(top, text="Filter:").pack(side="left")
        ttk.Entry(top, textvariable=self.filter_var, width=40).pack(side="left", padx=6)
        self.asset_count = ttk.Label(top, text="0 assets")
        self.asset_count.pack(side="right")

        split = ttk.Panedwindow(self.asset_tab, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.Frame(split)
        right = ttk.Frame(split, padding=10)
        split.add(left, weight=3)
        split.add(right, weight=2)

        columns = ("index", "name", "size", "compression", "key")
        self.asset_tree = ttk.Treeview(left, columns=columns, show="headings")
        for col, text, width in [("index", "Index", 80), ("name", "Name", 360), ("size", "Size", 110),
                                 ("compression", "Compression", 110), ("key", "Key", 120)]:
            self.asset_tree.heading(col, text=text)
            self.asset_tree.column(col, width=width, anchor="w")
        y = ttk.Scrollbar(left, orient="vertical", command=self.asset_tree.yview)
        self.asset_tree.configure(yscrollcommand=y.set)
        self.asset_tree.pack(side="left", fill="both", expand=True)
        y.pack(side="right", fill="y")
        self.asset_tree.bind("<<TreeviewSelect>>", self.on_asset_selected)

        # Tk's native scrollbar cannot expose a corner radius, so use a slim,
        # quiet Win11-like track/thumb rather than the old bright default.

        ttk.Label(right, text="Asset details", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        self.asset_details = tk.Text(right, wrap="word", height=16, state="disabled",
                                     bg=self._dark["field"], fg=self._dark["fg"],
                                     insertbackground=self._dark["fg"], selectbackground=self._dark["select"],
                                     selectforeground=self._dark["fg"], relief="flat", borderwidth=1,
                                     highlightthickness=1, highlightbackground=self._dark["border_soft"],
                                     highlightcolor=self._dark["border"])
        self.asset_details.pack(fill="both", expand=True, pady=8)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(2, 8))
        log_header = ttk.Frame(right)
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Log", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(log_header, text="Clear", command=self.clear_log).pack(side="right")
        log_frame = ttk.Frame(right)
        log_frame.pack(fill="both", expand=False, pady=(5, 0))
        self.log_text = tk.Text(log_frame, wrap="none", height=9, state="disabled",
                                bg="#101214", fg=self._dark["fg"],
                                insertbackground=self._dark["fg"], selectbackground=self._dark["select"],
                                selectforeground=self._dark["fg"], relief="flat", borderwidth=1,
                                highlightthickness=1, highlightbackground=self._dark["border_soft"],
                                font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def _build_ova_tab(self) -> None:
        bar = ttk.Frame(self.ova_tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="Analyze current asset", command=self.analyze_current_ova).pack(side="left")
        ttk.Button(bar, text="Diagnose", command=self.diagnose_ova).pack(side="left", padx=6)
        self.ova_mode_btn = ttk.Button(bar, text="OVA: Jade reale", command=self.toggle_ova_mode)
        self.ova_mode_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Save modified .BIN", command=self.save_bin).pack(side="left", padx=6)
        ttk.Button(bar, text="Rebuild .BF", command=self.rebuild_edited_bf).pack(side="left", padx=6)
        self.ova_source = ttk.Label(bar, text="Nessuna sorgente OVA")
        self.ova_source.pack(side="right")

        split = ttk.Panedwindow(self.ova_tab, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(8, 0))
        left, right = ttk.Frame(split), ttk.Frame(split, padding=8)
        split.add(left, weight=2)
        split.add(right, weight=3)
        self.var_tree = ttk.Treeview(left, columns=("name", "value", "offset", "type", "flags"), show="headings")
        for c, t, w in [("name", "Variable", 230), ("value", "Value", 85), ("offset", "Value offset", 125), ("type", "Type", 70), ("flags", "Flags", 70)]:
            self.var_tree.heading(c, text=t)
            self.var_tree.column(c, width=w)
        self.var_tree.pack(fill="both", expand=True)
        self.var_tree.bind("<<TreeviewSelect>>", self.on_variable_selected)
        editor = ttk.Frame(right)
        editor.pack(fill="x", pady=(0, 8))
        self.ova_selected_label = ttk.Label(editor, text="Seleziona una OVA variable")
        self.ova_selected_label.pack(anchor="w")
        candidate_bar = ttk.Frame(editor)
        candidate_bar.pack(fill="x", pady=7)
        ttk.Label(candidate_bar, text="Candidati booleani 00/01/FF:").pack(side="left")
        self.ova_candidates = ttk.Combobox(candidate_bar, state="readonly", width=52)
        self.ova_candidates.pack(side="left", fill="x", expand=True, padx=7)
        self.ova_candidates.bind("<<ComboboxSelected>>", self.use_ova_candidate)
        ttk.Button(candidate_bar, text="Cerca candidati", command=self.find_ova_candidates).pack(side="left")
        actions = ttk.Frame(editor)
        actions.pack(fill="x", pady=(0, 7))
        self.ova_false_btn = ttk.Button(actions, text="Set FALSE  00", command=lambda: self.set_ova_bool(0))
        self.ova_true_btn = ttk.Button(actions, text="Set TRUE  01", command=lambda: self.set_ova_bool(1))
        self.ova_false_btn.pack(side="left")
        self.ova_true_btn.pack(side="left", padx=7)
        self.ova_offset = tk.StringVar()
        ttk.Label(actions, text="Offset:").pack(side="left", padx=(18, 5))
        ttk.Entry(actions, textvariable=self.ova_offset, width=12).pack(side="left")
        value_bar = ttk.Frame(editor)
        value_bar.pack(fill="x", pady=(0, 7))
        ttk.Label(value_bar, text="Raw value (hex):").pack(side="left")
        self.ova_value_hex = tk.StringVar()
        ttk.Entry(value_bar, textvariable=self.ova_value_hex, width=42).pack(side="left", fill="x", expand=True, padx=7)
        self.ova_apply_value_btn = ttk.Button(value_bar, text="Apply value", command=self.apply_ova_raw_value)
        self.ova_apply_value_btn.pack(side="left")
        self.ova_hex_text = tk.Text(right, wrap="none", height=9, state="disabled", font=("Consolas", 9))
        self.ova_hex_text.pack(fill="both", expand=True)
        ttk.Label(right, text="Diagnostica / OVA descriptor").pack(anchor="w", pady=(8, 3))
        self.ova_text = tk.Text(right, wrap="word", height=10, state="disabled")
        self.ova_text.pack(fill="both", expand=True)
        self._set_ova_buttons(False)

    def _build_level_tab(self) -> None:
        self._placeholder(self.level_tab, "Level Editor", "Preparato per .wow/.gao level data", [
            "• BF indexing e targeted extraction sono già disponibili nell'Asset Browser.",
            "• La struttura dei GameObject/material/mesh viene presa dal Blender Addon locale.",
            "• Questa superficie sarà il punto di ingresso per transform, groups, portals e triggers.",
        ])

    def _build_mesh_tab(self) -> None:
        top = ttk.Frame(self.mesh_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Mesh Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_meshes).pack(side="right")
        ttk.Button(top, text="Export mesh", command=self.export_mesh).pack(side="right", padx=6)
        self.mesh_source_label = ttk.Label(self.mesh_tab, text="Nessun mesh analizzato")
        self.mesh_source_label.pack(anchor="w", pady=(4, 8))

        split = ttk.Panedwindow(self.mesh_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=1)
        split.add(right, weight=4)

        ttk.Label(left, text="Mesh nel file selezionato").pack(anchor="w")
        self.mesh_tree = ttk.Treeview(left, columns=("name", "verts", "faces", "key"), show="headings")
        for c, t, w in (("name", "Mesh", 190), ("verts", "Vertices", 80), ("faces", "Faces", 80), ("key", "Mesh ID", 105)):
            self.mesh_tree.heading(c, text=t)
            self.mesh_tree.column(c, width=w, anchor="w")
        self.mesh_tree.pack(fill="both", expand=True, pady=(5, 0))
        self.mesh_tree.bind("<<TreeviewSelect>>", self.on_mesh_selected)

        preview_box = ttk.LabelFrame(right, text="Mesh 3D selezionato", padding=6)
        preview_box.pack(fill="both", expand=True)
        if OpenGLFrame is not None:
            self.mesh_canvas = MeshViewport(preview_box, self, highlightthickness=0, bd=0)
            self.mesh_canvas.pack(fill="both", expand=True)
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

        info_box = ttk.LabelFrame(right, text="Mesh info", padding=8)
        info_box.pack(fill="x", pady=(8, 0))
        self.mesh_info = ttk.Label(info_box, text="Seleziona un mesh per visualizzarlo.", justify="left")
        self.mesh_info.pack(anchor="w")

    def _build_material_tab(self) -> None:
        top = ttk.Frame(self.material_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Material Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_materials).pack(side="right")
        ttk.Button(top, text="Save changes as .BIN", command=self.save_material_changes_as_bin).pack(side="right", padx=6)
        ttk.Button(top, text="Apply material changes", command=self.apply_material_changes).pack(side="right", padx=6)
        self.material_source_label = ttk.Label(self.material_tab, text="Nessun materiale analizzato")
        self.material_source_label.pack(anchor="w", pady=(4, 8))

        split = ttk.Panedwindow(self.material_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=2)
        split.add(right, weight=5)

        ttk.Label(left, text="Materiali rilevati").pack(anchor="w")
        self.material_tree = ttk.Treeview(left, columns=("name", "diffuse", "normal", "alpha", "metallic"), show="headings")
        for c, t, w in (("name", "Material", 210), ("diffuse", "Diffuse", 110), ("normal", "Normal", 100),
                        ("alpha", "Alpha", 65), ("metallic", "Metal", 65)):
            self.material_tree.heading(c, text=t)
            self.material_tree.column(c, width=w, anchor="w")
        self.material_tree.pack(fill="both", expand=True, pady=(5, 0))
        self.material_tree.bind("<<TreeviewSelect>>", self.on_material_selected)

        preview_box = ttk.LabelFrame(right, text="Material preview", padding=6)
        preview_box.pack(fill="both", expand=True)
        if OpenGLFrame is not None:
            self.material_canvas = MaterialViewport(preview_box, self, highlightthickness=0, bd=0)
            self.material_canvas.pack(fill="both", expand=True)
        else:
            self.material_canvas = tk.Label(preview_box, text="OpenGL non disponibile", anchor="center")
            self.material_canvas.pack(fill="both", expand=True)

        editor = ttk.LabelFrame(right, text="Material properties", padding=8)
        editor.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Diffuse texture", width=18).pack(side="left")
        self.material_diffuse_combo = ttk.Combobox(row, state="readonly", width=54)
        self.material_diffuse_combo.pack(side="left", fill="x", expand=True, padx=6)
        self.material_diffuse_combo.bind("<<ComboboxSelected>>", lambda _e: self._material_preview_changed())
        ttk.Button(row, text="Refresh", command=self._material_preview_changed).pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Normal map", width=18).pack(side="left")
        ttk.Entry(row, textvariable=self._material_normal_path).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Add normal...", command=self.choose_material_normal).pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=(8, 2))
        ttk.Label(row, text="Metallicity", width=18).pack(side="left")
        self.material_metal_scale = ttk.Scale(row, from_=0.0, to=1.0, variable=self._material_metallic,
                                              command=lambda _v: self._material_preview_changed())
        self.material_metal_scale.pack(side="left", fill="x", expand=True, padx=6)
        self.material_metal_value = ttk.Label(row, text="0.00", width=6)
        self.material_metal_value.pack(side="right")
        row = ttk.Frame(editor); row.pack(fill="x", pady=2)
        ttk.Label(row, text="Alpha (preview)", width=18).pack(side="left")
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
        self.material_info = ttk.Label(editor, text="Seleziona un materiale per modificarlo.", justify="left")
        self.material_info.pack(anchor="w", pady=(7, 0))

    def scan_materials(self) -> None:
        asset = self._selected_asset()
        if asset is None:
            messagebox.showinfo("Material Swap", "Seleziona prima un asset .wow/.bin/.gao nel browser.")
            return
        try:
            data = self.project.read_asset(asset)
            records = _scan_pop_material_records(data)
            packs, _materials = _scan_pop_materials(data)
            textures = _scan_pop_textures(data)
            meshes = _scan_pop_meshes(data)
            _associate_mesh_material_packs(data, meshes)
            by_key = {tex.key: tex for tex in textures}
            from PIL import Image
            images = {}
            for tex in textures:
                try:
                    if tex.texture_type == 7:
                        blob = _dds_blob_for_dump(data, tex)
                    elif tex.texture_type == 0:
                        blob = _build_tga_header(tex.storage_width, tex.storage_height, 32) + data[tex.data_offset:tex.data_offset + tex.storage_width * tex.storage_height * 4]
                    elif tex.texture_type == 1:
                        continue
                    else:
                        continue
                    images[tex.key] = Image.open(io.BytesIO(blob)).convert("RGBA")
                except Exception:
                    continue
            for mesh in meshes:
                pack = packs.get(mesh.material_pack_key, []) if mesh.material_pack_key is not None else []
                for material_id, _count in mesh.material_ids:
                    if 0 <= material_id < len(pack):
                        key = pack[material_id]
                        info = records.get(key)
                        if info is not None and mesh.key not in (info.source_meshes or []):
                            info.source_meshes = list(info.source_meshes or []) + [mesh.key]
            self._material_data = bytearray(data)
            self._material_original = bytes(data)
            self._material_dirty = False
            self._material_infos = list(records.values())
            self._material_source_asset = asset
            self._material_textures = images
            self._material_diffuse_combo_values = []
            self._clear_tree(self.material_tree)
            for i, info in enumerate(self._material_infos):
                diff = f"0x{info.texture_key:08X}" if info.texture_key is not None else "<none>"
                self.material_tree.insert("", "end", iid=f"mat_{i}",
                                          values=(f"0x{info.material_key:08X}", diff, "<slot>",
                                                  f"{info.alpha:.2f}", f"{info.metallic:.2f}"))
                self._material_diffuse_combo_values.append(diff)
            self.material_diffuse_combo["values"] = self._material_diffuse_combo_values
            self.material_source_label.config(text=f"{asset.name} — {len(self._material_infos)} materiali, {len(images)} texture preview")
            self.tabs.select(self.material_tab)
            if self._material_infos:
                self.material_tree.selection_set("mat_0")
                self.material_tree.focus("mat_0")
                self.on_material_selected()
            self._log(f"OK    Material scan: {asset.name} -> {len(self._material_infos)} materiali")
        except Exception as exc:
            self._log(f"ERROR Material scan: {exc}")
            messagebox.showerror("Material Swap", str(exc))

    def _selected_material(self) -> MaterialInfo | None:
        selection = self.material_tree.selection()
        if not selection: return None
        try: index = int(selection[0].split("_", 1)[1])
        except (ValueError, IndexError): return None
        return self._material_infos[index] if 0 <= index < len(self._material_infos) else None

    def on_material_selected(self, _event=None) -> None:
        info = self._selected_material()
        if info is None: return
        self._material_metallic.set(info.metallic)
        self._material_alpha.set(info.alpha)
        current = f"0x{info.texture_key:08X}" if info.texture_key is not None else "<none>"
        values = list(self.material_diffuse_combo["values"])
        self.material_diffuse_combo.set(current if current in values else "")
        self._material_normal_path.set("")
        self.material_info.config(text=(f"Material 0x{info.material_key:08X} • versioned Jade type-5 record\n"
                                        f"Diffuse: {current} • source meshes: {len(info.source_meshes or [])}"))
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
            self.material_info.config(text=(f"Material 0x{info.material_key:08X} • versioned Jade type-5 record\n"
                                             f"Diffuse: {selected or '<none>'} • source meshes: {len(info.source_meshes or [])}\n"
                                             "Alpha is preview-only; Jade type-5 stores diffuse/specular intensities, not a standalone alpha scalar."))

    def choose_material_normal(self) -> None:
        path = filedialog.askopenfilename(title="Choose normal map", filetypes=[
            ("Images", "*.dds *.tga *.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not path: return
        self._material_normal_path.set(path)
        self._material_preview_changed()

    def apply_material_changes(self) -> bool:
        info = self._selected_material()
        if info is None or self._material_source_asset is None:
            messagebox.showinfo("Material Swap", "Scansiona e seleziona prima un materiale.")
            return False
        try:
            if info.texture_offset is not None:
                selected = self.material_diffuse_combo.get()
                if selected.startswith("0x"):
                    struct.pack_into("<I", self._material_data, info.texture_offset, int(selected, 16))
                    info.texture_key = int(selected, 16)
            if info.specular_offset is not None:
                struct.pack_into("<f", self._material_data, info.specular_offset, float(self._material_metallic.get()))
                info.metallic = float(self._material_metallic.get())
            self._material_dirty = self._material_data != bytearray(self._material_original)
            if not self._material_dirty:
                messagebox.showinfo("Material Swap", "Nessuna modifica del materiale da salvare.")
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
            messagebox.showinfo("Material Swap", f"Creato:\n{target}")
        except Exception as exc:
            self._log(f"ERROR Material BIN save: {exc}")
            messagebox.showerror("Material Swap", str(exc))
    def _build_texture_tab(self) -> None:
        top = ttk.Frame(self.texture_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Texture Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_textures).pack(side="right")
        ttk.Button(top, text="Dump texture", command=self.dump_texture).pack(side="right", padx=6)
        ttk.Button(top, text="Import replacement image...", command=self.import_texture_replacement).pack(side="right", padx=6)
        ttk.Button(top, text="Save changes as .BIN", command=self.save_texture_asset).pack(side="right", padx=6)
        self.texture_source_label = ttk.Label(self.texture_tab, text="Nessuna texture analizzata")
        self.texture_source_label.pack(anchor="w", pady=(4, 8))
        split = ttk.Panedwindow(self.texture_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=2)
        split.add(right, weight=3)
        left_split = ttk.Panedwindow(left, orient="vertical")
        left_split.pack(fill="both", expand=True)
        objects_box = ttk.Frame(left_split, padding=(0, 0, 0, 5))
        textures_box = ttk.Frame(left_split, padding=(0, 5, 0, 0))
        left_split.add(objects_box, weight=1)
        left_split.add(textures_box, weight=2)
        ttk.Label(objects_box, text="Oggetti/FileEntry nel BIN").pack(anchor="w")
        self.texture_object_tree = ttk.Treeview(objects_box, columns=("index", "key", "size", "type"), show="headings")
        for c, t, w in (("index", "#", 55), ("key", "File ID", 105), ("size", "Size", 90), ("type", "Data type", 105)):
            self.texture_object_tree.heading(c, text=t)
            self.texture_object_tree.column(c, width=w, anchor="w")
        self.texture_object_tree.pack(fill="both", expand=True, pady=(5, 0))
        ttk.Label(textures_box, text="Texture nel file selezionato").pack(anchor="w")
        self.texture_tree = ttk.Treeview(textures_box, columns=("name", "size", "format", "dims"), show="headings")
        for c, t, w in (("name", "Texture", 220), ("size", "Size", 90), ("format", "Format", 150), ("dims", "Dimensions", 100)):
            self.texture_tree.heading(c, text=t)
            self.texture_tree.column(c, width=w, anchor="w")
        self.texture_tree.pack(fill="both", expand=True, pady=(5, 0))
        self.texture_tree.bind("<<TreeviewSelect>>", self.on_texture_selected)
        original_box = ttk.LabelFrame(right, text="Texture selezionata", padding=8)
        original_box.pack(fill="both", expand=True)
        self.texture_preview = tk.Label(original_box, text="Seleziona una texture", anchor="center", justify="center", bg=self._dark["field"], fg=self._dark["muted"])
        self.texture_preview.pack(fill="both", expand=True)
        replacement_box = ttk.LabelFrame(right, text="Texture da importare", padding=8)
        replacement_box.pack(fill="both", expand=True, pady=(8, 0))
        self.texture_replacement_preview = tk.Label(replacement_box, text="Importa un .dds compatibile", anchor="center", justify="center", bg=self._dark["field"], fg=self._dark["muted"])
        self.texture_replacement_preview.pack(fill="both", expand=True)
        self.texture_info = ttk.Label(right, text="", justify="left")
        self.texture_info.pack(fill="x", pady=(8, 0))
        transform_box = ttk.LabelFrame(right, text="Orientamento texture", padding=6)
        transform_box.pack(fill="x", pady=(8, 0))
        ttk.Button(transform_box, text="Rotazione 90°", command=lambda: self.rotate_texture(90)).pack(side="left")
        ttk.Button(transform_box, text="Flip asse X", command=lambda: self.flip_texture("x")).pack(side="left", padx=(6, 0))
        ttk.Button(transform_box, text="Flip asse Y", command=lambda: self.flip_texture("y")).pack(side="left", padx=(6, 0))
        self.texture_transform_label = ttk.Label(transform_box, text="Rotazione: 0° • Flip X: no • Flip Y: no")
        self.texture_transform_label.pack(side="left", padx=(10, 0))
        self.texture_apply_btn = ttk.Button(right, text="Applica modifiche texture", command=self.apply_texture_replacement, state="disabled")
        self.texture_apply_btn.pack(anchor="e", pady=(6, 0))

    def scan_meshes(self) -> None:
        asset = self._selected_asset()
        if asset is None:
            messagebox.showinfo("Mesh Swap", "Seleziona prima un asset .wow/.bin/.gao nel browser.")
            return
        try:
            data = self.project.read_asset(asset)
            meshes = _scan_pop_meshes(data)
            _associate_mesh_material_packs(data, meshes)
            packs, materials = _scan_pop_materials(data)
            textures = _scan_pop_textures(data)
            self._mesh_data = bytearray(data)
            self._mesh_infos = meshes
            self._mesh_source_asset = asset
            self._mesh_textures = {}
            from PIL import Image
            for tex in textures:
                try:
                    if tex.texture_type == 7:
                        blob = _dds_blob_for_dump(data, tex)
                    elif tex.texture_type == 0:
                        blob = _build_tga_header(tex.storage_width, tex.storage_height, 32) + data[tex.data_offset:tex.data_offset + tex.storage_width * tex.storage_height * 4]
                    elif tex.texture_type == 1:
                        # Palette textures are uncommon in geometry materials;
                        # use the existing standalone TGA converter when possible.
                        self._texture_data = bytearray(data)
                        self._texture_file_entries = _parse_pop_file_entries(data)
                        blob = self._palette_tga_blob_for_texture(tex)
                    else:
                        continue
                    self._mesh_textures[tex.key] = Image.open(io.BytesIO(blob)).convert("RGBA")
                except Exception:
                    continue

            # Attach the material->texture lookup to the selected mesh without
            # changing the raw mesh parser's standalone data model.
            self._mesh_material_textures: dict[int, int] = {}
            for mesh in meshes:
                if mesh.material_pack_key is None:
                    continue
                pack = packs.get(mesh.material_pack_key, [])
                for material_id, _count in mesh.material_ids:
                    if 0 <= material_id < len(pack):
                        texture_key = materials.get(pack[material_id])
                        if texture_key is not None:
                            self._mesh_material_textures[material_id] = texture_key

            self._clear_tree(self.mesh_tree)
            for i, mesh in enumerate(meshes):
                name = mesh.object_name or f"Mesh #{i + 1}"
                self.mesh_tree.insert("", "end", iid=f"mesh_{i}",
                                      values=(name, f"{len(mesh.vertices):,}", f"{len(mesh.faces):,}", f"0x{mesh.key:08X}"))
            self.mesh_source_label.config(text=f"{asset.name} — {len(meshes)} mesh visualizzabili, {len(self._mesh_textures)} texture disponibili")
            self.mesh_info.config(text="Seleziona un mesh. Trascina con il mouse per ruotare; rotella per zoom.")
            self.tabs.select(self.mesh_tab)
            self._mesh_yaw = -0.45
            self._mesh_pitch = 0.18
            self._mesh_zoom = 1.0
            if meshes:
                self.mesh_tree.selection_set("mesh_0")
                self.mesh_tree.focus("mesh_0")
                if isinstance(self.mesh_canvas, MeshViewport):
                    self.mesh_canvas.set_scene(meshes[0], self._mesh_textures)
                else:
                    self._render_selected_mesh()
            self._log(f"OK    Mesh scan: {asset.name} -> {len(meshes)} mesh, {len(self._mesh_textures)} texture")
        except Exception as exc:
            self._log(f"ERROR Mesh scan: {exc}")
            messagebox.showerror("Mesh Swap", str(exc))

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
        self.mesh_info.config(text=(
            f"Mesh ID 0x{mesh.key:08X} • {len(mesh.vertices):,} vertices • {len(mesh.faces):,} faces • "
            f"version {mesh.version}" + (f" • object: {mesh.object_name}" if mesh.object_name else "")
        ))
        if isinstance(self.mesh_canvas, MeshViewport):
            self.mesh_canvas.set_scene(mesh, self._mesh_textures)
        else:
            self._render_selected_mesh()

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
                draw.text((20, 20), "Mesh vuoto o non visualizzabile", fill=(220, 220, 220, 255))
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
                    tex_key = getattr(self, "_mesh_material_textures", {}).get(mat_id)
                    tex = self._mesh_textures.get(tex_key)
                    uv = None
                    if mesh.uv_indices and mesh.uvs:
                        ui = mesh.uv_indices[face_index]
                        if all(0 <= i < len(mesh.uvs) for i in ui):
                            uv = [mesh.uvs[i] for i in ui]
                    if tex is not None and uv is not None:
                        tw, th = tex.size
                        src = [(u * tw, (1.0 - v) * th) for u, v in uv]
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
            self.mesh_canvas.create_text(12, height - 14, anchor="w", text="Drag: ruota  •  Wheel: zoom", fill=self._dark["muted"])
        except Exception as exc:
            self.mesh_canvas.delete("all")
            self.mesh_canvas.create_text(12, 12, anchor="nw", text=f"Preview error: {exc}", fill="#ff7777")

    def export_mesh(self) -> None:
        mesh = self._selected_mesh()
        asset = self._mesh_source_asset
        if mesh is None or asset is None:
            messagebox.showinfo("Mesh Swap", "Scansiona e seleziona prima un mesh.")
            return
        try:
            source_path = self.project.path
            if source_path is None:
                raise ValueError("Nessun .BF/.BIN sorgente aperto.")
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
            for mat_id, _count in mesh.material_ids:
                tex_key = getattr(self, "_mesh_material_textures", {}).get(mat_id)
                mtl_lines.append(f"newmtl mat_{mat_id}")
                mtl_lines.append("Ka 0.2 0.2 0.2")
                mtl_lines.append("Kd 1.0 1.0 1.0")
                mtl_lines.append("d 1.0")
                if tex_key in textures_out:
                    mtl_lines.append(f"map_Kd {textures_out[tex_key].name}")
                mtl_lines.append("")
            mtl_path.write_text("\n".join(mtl_lines), encoding="utf-8")

            lines = [f"# Exported by PoP BF Lab", f"mtllib {mtl_path.name}"]
            for x, y, z in mesh.vertices:
                lines.append(f"v {x:.8g} {y:.8g} {z:.8g}")
            for u, v in mesh.uvs:
                lines.append(f"vt {u:.8g} {1.0 - v:.8g}")
            current_face = 0
            for mat_id, count in mesh.material_ids:
                lines.append(f"usemtl mat_{mat_id}")
                for face_index in range(current_face, min(current_face + count, len(mesh.faces))):
                    face = mesh.faces[face_index]
                    if mesh.uv_indices and face_index < len(mesh.uv_indices):
                        uvf = mesh.uv_indices[face_index]
                        refs = [f"{vi + 1}/{ui + 1}" for vi, ui in zip(face, uvf)]
                    else:
                        refs = [str(vi + 1) for vi in face]
                    lines.append("f " + " ".join(refs))
                current_face += count
            obj_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._log(f"OK    Mesh export: {obj_path}")
            messagebox.showinfo("Export mesh", f"Creati nella cartella sorgente:\n{obj_path}\n{mtl_path}\n{len(textures_out)} texture PNG")
        except Exception as exc:
            self._log(f"ERROR Mesh export: {exc}")
            messagebox.showerror("Export mesh", str(exc))

    def _placeholder(self, parent, title: str, subtitle: str, lines: list[str]) -> None:
        ttk.Label(parent, text=title, font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(parent, text=subtitle).pack(anchor="w", pady=(4, 14))
        for line in lines:
            ttk.Label(parent, text=line).pack(anchor="w", pady=2)

    def _log(self, text: str) -> None:
        print(text, flush=True)
        if hasattr(self, "log_text"):
            self.log_text.config(state="normal")
            self.log_text.insert("end", text.rstrip() + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.status.set(text)

    def clear_log(self) -> None:
        if hasattr(self, "log_text"):
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.config(state="disabled")
        self._log("INFO  Log cancellato.")

    def _refresh_title(self) -> None:
        self.title(f"PoP BF Lab — {self.project.title}")
        self.file_label.config(text=self.project.title)

    def import_bf(self) -> None:
        self._log("INFO  Apertura dialogo import BF...")
        path = filedialog.askopenfilename(title="Import Jade Big File", filetypes=[("Jade Big Files", "*.bf"), ("All files", "*.*")])
        if not path:
            return
        try:
            self._log(f"INFO  Parsing BF: {path}")
            self.project.open_bf(Path(path))
            self.refresh_assets()
            self._refresh_title()
            self._log(f"INFO  BF aperto: v{self.project.info.version}, {len(self.project.assets):,} entry indicizzate, FAT={self.project.info.num_fat}")
        except Exception as exc:
            self._log(f"ERROR Import BF: {exc}")
            messagebox.showerror("Import BF", str(exc))

    def import_bin(self) -> None:
        self._log("INFO  Apertura dialogo import BIN...")
        path = filedialog.askopenfilename(title="Import BIN", filetypes=[("BIN files", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        try:
            self._log(f"INFO  Lettura BIN: {path}")
            bin_path = Path(path)
            raw = bin_path.read_bytes()
            self.project.open_bin(bin_path)
            self.refresh_assets()
            self._refresh_title()
            state = "POP-LZO decompresso" if self.project.direct_compressed else "non compresso"
            self._log(f"INFO  BIN aperto: raw={len(raw):,} B, decoded={len(self.project.decoded_bin or b''):,} B, {state}")
            self.analyze_current_ova()
        except Exception as exc:
            self._log(f"ERROR Import BIN: {exc}")
            messagebox.showerror("Import BIN", str(exc))

    def close_project(self) -> None:
        self.project = JadeProject()
        self.refresh_assets()
        self._refresh_title()
        self._clear_tree(self.var_tree)
        self._set_text(self.ova_text, "")
        self._log("Progetto chiuso")

    def refresh_assets(self) -> None:
        if not hasattr(self, "asset_tree"):
            return
        self._clear_tree(self.asset_tree)
        self._asset_map.clear()
        needle = self.filter_var.get().lower() if hasattr(self, "filter_var") else ""
        count = 0
        for asset in self.project.assets:
            if needle and needle not in asset.name.lower():
                continue
            iid = f"asset_{asset.index}_{count}"
            self._asset_map[iid] = asset
            self.asset_tree.insert("", "end", iid=iid, values=(asset.index, asset.name, f"{asset.size:,}",
                                                                  "POP-LZO" if asset.compressed else "none", f"0x{asset.key:08X}"))
            count += 1
        self.asset_count.config(text=f"{count:,} assets")

    def _clear_tree(self, tree) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _set_text(self, widget, text: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _selected_asset(self) -> Optional[Asset]:
        selection = self.asset_tree.selection()
        return self._asset_map.get(selection[0]) if selection else None

    def on_asset_selected(self, _event=None) -> None:
        asset = self._selected_asset()
        if not asset:
            return
        lines = [f"Name: {asset.name}", f"Index: {asset.index}", f"Key: 0x{asset.key:08X}",
                 f"Position: 0x{asset.position:08X}", f"Decoded size: {asset.size:,} bytes",
                 f"Compression: {'POP-LZO' if asset.compressed else 'none'}"]
        if self.project.kind == "bf":
            entry = next(e for e in self.project.info.entries if e.index == asset.index)
            lines += [f"FAT: {entry.fat_index}", f"Parent index: {entry.parent}"]
        self._set_text(self.asset_details, "\n".join(lines))

    def extract_selected(self) -> None:
        asset = self._selected_asset()
        if not asset:
            messagebox.showinfo("Extract", "Seleziona prima un asset.")
            return
        target = filedialog.asksaveasfilename(title="Extract asset", initialfile=asset.name)
        if not target:
            return
        try:
            self._log(f"INFO  Estrazione asset #{asset.index}: {asset.name}")
            data = self.project.read_asset(asset)
            Path(target).write_bytes(data)
            self._log(f"OK    Estratto {asset.name}: {len(data):,} B -> {target}")
        except Exception as exc:
            self._log(f"ERROR Extract: {exc}")
            messagebox.showerror("Extract", str(exc))

    def extract_all_assets(self) -> None:
        if self.project.kind != "bf" or self.project.path is None:
            messagebox.showinfo("Extract all assets", "Apri prima un file .BF.")
            return

        source = self.project.path
        target_dir = source.parent / f"{source.stem}_extracted"

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            used_names: set[str] = set()
            extracted = 0

            self._log(f"INFO  Estrazione completa BF: {source.name} -> {target_dir}")
            for asset in self.project.assets:
                name = Path(asset.name).name.strip()
                if not name or name in (".", ".."):
                    name = f"file_{asset.index:06d}"

                for char in '<>:"/\\|?*':
                    name = name.replace(char, "_")
                name = name.rstrip(" .") or f"file_{asset.index:06d}"

                candidate = name
                if candidate.casefold() in used_names:
                    stem = Path(name).stem
                    suffix = Path(name).suffix
                    candidate = f"{stem}_{asset.index:06d}{suffix}"
                used_names.add(candidate.casefold())

                data = self.project.read_asset(asset)
                (target_dir / candidate).write_bytes(data)
                extracted += 1

            self._log(f"OK    Estratti {extracted:,} asset -> {target_dir}")
            messagebox.showinfo(
                "Extract all assets",
                f"Estratti {extracted:,} asset in:\n{target_dir}",
            )
        except Exception as exc:
            self._log(f"ERROR Extract all: {exc}")
            messagebox.showerror("Extract all assets", str(exc))
    def rebuild_bf(self) -> None:
        if self.project.kind != "bf":
            messagebox.showinfo("Rebuild BF", "Apri un .bf e seleziona un asset da modificare.")
            return
        asset = self._selected_asset()
        if not asset:
            messagebox.showinfo("Rebuild BF", "Seleziona l'entry BF da sostituire.")
            return
        source = filedialog.askopenfilename(title="Replacement payload", initialfile=asset.name)
        if not source:
            return
        target = filedialog.asksaveasfilename(title="Save rebuilt BF", initialfile=self.project.path.stem + "_edited.bf", defaultextension=".bf", filetypes=[("Jade Big Files", "*.bf")])
        if not target:
            return
        try:
            self._log(f"INFO  Ricostruzione BF: entry #{asset.index}, payload={source}")
            self.project.replace_bf_entry(asset, Path(source).read_bytes(), Path(target))
            self._log(f"OK    BF ricostruito: {target}")
            messagebox.showinfo("Rebuild BF", f"Creato:\n{target}")
        except Exception as exc:
            self._log(f"ERROR Rebuild BF: {exc}")
            messagebox.showerror("Rebuild BF", str(exc))

    def save_bin(self) -> None:
        if self.project.kind != "bin" or self.project.decoded_bin is None:
            messagebox.showinfo("Save BIN", "Apri un .bin prima.")
            return
        target = filedialog.asksaveasfilename(title="Save BIN", initialfile=self.project.path.stem + "_edited.bin", defaultextension=".bin")
        if not target:
            return
        try:
            data = bytes(self._ova_data) if self._ova_data else self.project.decoded_bin
            self.project.decoded_bin = data
            self._log(f"INFO  Salvataggio BIN: decoded={len(data):,} B -> {target}")
            self.project.save_bin_as(Path(target), data)
            self._log(f"OK    BIN salvato: {target}")
            self._ova_dirty = False
        except Exception as exc:
            self._log(f"ERROR Save BIN: {exc}")
            messagebox.showerror("Save BIN", str(exc))

    def analyze_current_ova(self) -> None:
        if self.project.kind == "bin":
            data = self.project.decoded_bin or b""
            label = self.project.path.name
        else:
            asset = self._selected_asset()
            if not asset:
                return
            try:
                data = self.project.read_asset(asset)
            except Exception as exc:
                messagebox.showerror("OVA", str(exc))
                return
            label = asset.name
        try:
            self._log(f"INFO  Analisi OVA: {label}, {len(data):,} B")
            variables = find_variables(data)
            self._ova_data = bytearray(data)
            self._ova_original = bytes(data)
            self._ova_variables = variables
            self._ova_jade_variables = variables
            self._ova_ascii_variables = _find_ascii_fallback(data)
            self._ova_source_asset = None if self.project.kind == "bin" else self._selected_asset()
            self._ova_dirty = False
            self._clear_tree(self.var_tree)
            for i, variable in enumerate(variables):
                value = "—"
                if variable.value_absolute is not None and variable.value_absolute < len(data):
                    size = variable.value_size or 1
                    value = bytes(data[variable.value_absolute:min(len(data), variable.value_absolute + size)]).hex(" ").upper()
                self.var_tree.insert("", "end", iid=f"var_{i}", values=(variable.name, value,
                    (f"0x{variable.value_absolute:08X}" if variable.value_absolute is not None else "—"), variable.var_type, variable.flags))
            self.ova_source.config(text=f"{label} — {len(variables)} variables")
            self._set_text(self.ova_text, "\n".join(ova_diagnostic_report(data, label)))
            self._set_text(self.ova_hex_text, "")
            self.ova_candidates["values"] = ()
            self.ova_offset.set("")
            self.ova_value_hex.set("")
            self.ova_selected_label.config(text="Seleziona una OVA variable")
            self._set_ova_buttons(False)
            self.ova_apply_value_btn.configure(state="disabled")
            self.tabs.select(self.ova_tab)
            self._log(f"OK    OVA: {len(variables)} variabili strutturali trovate")
        except Exception as exc:
            self._log(f"ERROR OVA: {exc}")
            messagebox.showerror("OVA", str(exc))

    def toggle_ova_mode(self) -> None:
        self.ova_variable_mode = "ascii" if self.ova_variable_mode == "jade" else "jade"
        self._ova_variables = self._ova_ascii_variables if self.ova_variable_mode == "ascii" else self._ova_jade_variables
        self.ova_mode_btn.config(text="OVA: ASCII fallback" if self.ova_variable_mode == "ascii" else "OVA: Jade reale")
        self._clear_tree(self.var_tree)
        for i, variable in enumerate(self._ova_variables):
            value = "—"
            if variable.value_absolute is not None and variable.value_absolute < len(self._ova_data):
                size = variable.value_size or 1
                value = bytes(self._ova_data[variable.value_absolute:min(len(self._ova_data), variable.value_absolute + size)]).hex(" ").upper()
            self.var_tree.insert("", "end", iid=f"var_{i}", values=(variable.name, value,
                (f"0x{variable.value_absolute:08X}" if variable.value_absolute is not None else f"0x{variable.offset:08X}"), variable.var_type or "—", variable.flags or "—"))
        self.ova_source.config(text=f"{self.project.title} — {len(self._ova_variables)} {'ASCII candidates' if self.ova_variable_mode == 'ascii' else 'OVA variables'}")
        self._log(f"INFO  Vista OVA: {'ASCII fallback' if self.ova_variable_mode == 'ascii' else 'Jade reale'} ({len(self._ova_variables)})")

    def on_variable_selected(self, _event=None) -> None:
        selection = self.var_tree.selection()
        if not selection:
            return
        idx = int(selection[0].split("_")[-1])
        if idx < 0 or idx >= len(self._ova_variables):
            return
        variable = self._ova_variables[idx]
        details = [f"{variable.name} • record 0x{variable.offset:08X}"]
        if variable.value_absolute is not None:
            size = variable.value_size or 1
            end = min(len(self._ova_data), variable.value_absolute + size)
            raw_value = bytes(self._ova_data[variable.value_absolute:end])
            details.append(f"value @ 0x{variable.value_absolute:08X} ({len(raw_value)} B)")
            self.ova_value_hex.set(raw_value.hex(" ").upper())
            self.ova_offset.set("+0")
            self._set_ova_buttons(True)
            self.ova_apply_value_btn.configure(state="normal")
        else:
            details.append("value buffer non localizzato")
            self.ova_offset.set("")
            self.ova_value_hex.set("")
            self._set_ova_buttons(False)
            self.ova_apply_value_btn.configure(state="disabled")
        self.ova_selected_label.config(text=" • ".join(details))
        self.ova_candidates["values"] = ()
        self._ova_candidate_offsets = []
        self.show_ova_context(variable)
        self._log(f"OVA variable: {variable.name} @ 0x{variable.offset:08X}")

    def _set_ova_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.ova_false_btn.configure(state=state)
        self.ova_true_btn.configure(state=state)

    def _selected_ova_variable(self) -> OvaVariable | None:
        selection = self.var_tree.selection()
        if not selection:
            return None
        idx = int(selection[0].split("_")[-1])
        return self._ova_variables[idx] if 0 <= idx < len(self._ova_variables) else None

    def _ova_candidate_window(self, variable: OvaVariable) -> tuple[int, int]:
        if variable.value_absolute is not None:
            return max(0, variable.value_absolute - 64), min(len(self._ova_data), variable.value_absolute + 65)
        # Name-table-less / truncated entries still expose the VarInfo record.
        # Use a tight window around the record rather than scanning the whole
        # binary, making every displayed synthetic variable actionable.
        return max(0, variable.offset - 48), min(len(self._ova_data), variable.offset + OVA_INFO_SIZE + 49)

    def find_ova_candidates(self) -> None:
        variable = self._selected_ova_variable()
        if variable is None:
            messagebox.showinfo("OVA", "Seleziona prima una variabile.")
            return
        lo, hi = self._ova_candidate_window(variable)
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        candidates: list[str] = []
        offsets: list[int] = []
        for absolute in range(lo, hi):
            if self._ova_data[absolute] in (0, 1, 0xFF):
                relative = absolute - anchor
                # The four bytes immediately before a POP initial-value
                # buffer are its serialized size field. It is commonly 00/01
                # and used to appear as a tempting "-4" candidate. It is not
                # a variable value and must never be offered as the selected
                # variable's boolean target.
                if variable.value_absolute is not None and relative == -4:
                    continue
                offsets.append(absolute)
                candidates.append(f"{relative:+d}  @ 0x{absolute:08X}  = {self._ova_data[absolute]:02X}")
        self._ova_candidate_offsets = offsets
        self.ova_candidates["values"] = candidates
        if candidates:
            preferred = 0
            if variable.value_absolute is not None:
                for i, absolute in enumerate(offsets):
                    if absolute == variable.value_absolute:
                        preferred = i
                        break
            self.ova_candidates.current(preferred)
            self.use_ova_candidate()
            self._log(f"OK    Candidati booleani per {variable.name}: {len(candidates)}")
        else:
            self._log(f"INFO  Nessun 00/01 vicino a {variable.name}")
            messagebox.showinfo("OVA candidates", "Nessun byte 00/01/FF nel contesto della variabile. Puoi inserire manualmente l'offset relativo e applicare la modifica.")

    def use_ova_candidate(self, _event=None) -> None:
        idx = self.ova_candidates.current()
        if idx < 0 or idx >= len(self._ova_candidate_offsets):
            return
        variable = self._selected_ova_variable()
        if variable is None:
            return
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        self.ova_offset.set(f"{self._ova_candidate_offsets[idx] - anchor:+d}")

    def _parse_ova_offset(self) -> int | None:
        raw = self.ova_offset.get().strip()
        if not raw:
            messagebox.showwarning("Offset", "Inserisci un offset relativo oppure scegli un candidato.")
            return None
        try:
            return int(raw, 0)
        except ValueError:
            messagebox.showerror("Offset", "Usa valori come +0, -1, +4 oppure 0x10.")
            return None

    def set_ova_bool(self, value: int) -> None:
        variable = self._selected_ova_variable()
        if variable is None:
            messagebox.showinfo("OVA", "Seleziona prima una variabile.")
            return
        rel = self._parse_ova_offset()
        if rel is None:
            return
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        absolute = anchor + rel
        if absolute < 0 or absolute >= len(self._ova_data):
            messagebox.showerror("OVA", "L'offset calcolato è fuori dai limiti del BIN.")
            return
        old = self._ova_data[absolute]
        if old not in (0, 1):
            if not messagebox.askyesno("Conferma byte", f"0x{absolute:08X} contiene {old:02X}, non 00/01.\n\nImpostarlo comunque a {value:02X}?"):
                return
        self._ova_data[absolute] = value
        self._ova_dirty = self._ova_data != bytearray(self._ova_original)
        if self.project.kind == "bin":
            self.project.decoded_bin = bytes(self._ova_data)
        self.refresh_ova_values()
        self.show_ova_context(variable)
        self._log(f"PATCH OVA  {variable.name}: 0x{absolute:08X}  {old:02X} -> {value:02X}")

    def apply_ova_raw_value(self) -> None:
        variable = self._selected_ova_variable()
        if variable is None or variable.value_absolute is None:
            messagebox.showinfo("OVA", "Seleziona una variabile con valore serializzato.")
            return
        try:
            values = bytes.fromhex(self.ova_value_hex.get().strip())
        except ValueError:
            messagebox.showerror("OVA", "Inserisci byte esadecimali, ad esempio: 00 01 FF 7F")
            return
        if not values:
            messagebox.showwarning("OVA", "Inserisci almeno un byte.")
            return
        max_size = variable.value_size or len(values)
        if len(values) > max_size:
            messagebox.showerror("OVA", f"La variabile dispone di {max_size} byte serializzati.")
            return
        start = variable.value_absolute
        end = start + len(values)
        if end > len(self._ova_data):
            messagebox.showerror("OVA", "Il valore supera la fine dell'entry caricata.")
            return
        old = bytes(self._ova_data[start:end])
        self._ova_data[start:end] = values
        self._ova_dirty = self._ova_data != bytearray(self._ova_original)
        if self.project.kind == "bin":
            self.project.decoded_bin = bytes(self._ova_data)
        self.refresh_ova_values()
        self.show_ova_context(variable)
        self._log(f"PATCH OVA  {variable.name}: 0x{start:08X}  {old.hex(' ').upper()} -> {values.hex(' ').upper()}")

    def refresh_ova_values(self) -> None:
        for i, variable in enumerate(self._ova_variables):
            iid = f"var_{i}"
            if not self.var_tree.exists(iid):
                continue
            value = "—"
            if variable.value_absolute is not None and variable.value_absolute < len(self._ova_data):
                size = variable.value_size or 1
                value = bytes(self._ova_data[variable.value_absolute:min(len(self._ova_data), variable.value_absolute + size)]).hex(" ").upper()
            current = list(self.var_tree.item(iid, "values"))
            if current:
                current[1] = value
                self.var_tree.item(iid, values=current)

    def show_ova_context(self, variable: OvaVariable) -> None:
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        lo = max(0, anchor - 64)
        hi = min(len(self._ova_data), anchor + 65)
        block = bytes(self._ova_data[lo:hi])
        lines = []
        for p in range(0, len(block), 16):
            lines.append(f"0x{lo + p:08X}: " + " ".join(f"{b:02X}" for b in block[p:p + 16]))
        self._set_text(self.ova_hex_text, "\n".join(lines))

    def rebuild_edited_bf(self) -> None:
        if self.project.kind != "bf" or self.project.info is None or self._ova_source_asset is None:
            messagebox.showinfo("Rebuild BF", "Apri un .BF, analizza una entry OVA/BIN e modifica almeno una variabile.")
            return
        if not self._ova_dirty:
            messagebox.showinfo("Rebuild BF", "Non ci sono modifiche OVA da ricostruire.")
            return
        target_name = self.project.path.stem + "_edited.bf"
        target = filedialog.asksaveasfilename(title="Ricostruisci .BF modificato", initialfile=target_name,
                                              defaultextension=".bf", filetypes=[("Jade Big Files", "*.bf"), ("All files", "*.*")])
        if not target:
            return
        try:
            entry = next(e for e in self.project.info.entries if e.index == self._ova_source_asset.index)
            rebuilt = Path(target).with_suffix(Path(target).suffix + ".tmp")
            _repack_legacy_bigfile(self.project.path, entry, bytes(self._ova_data), rebuilt)
            os.replace(rebuilt, Path(target))
            self._log(f"OK    .BF ricostruito con OVA: {target}")
            self._log(f"INFO  Entry modificata: #{entry.index} {entry.name} • compressione={entry.compression}")
            self._ova_dirty = False
            messagebox.showinfo("Rebuild BF", f"Creato:\n{target}\n\nLa entry OVA è stata reinserita e, se compressa, ricompressa in POP-LZO.")
        except Exception as exc:
            self._log(f"ERROR Rebuild BF: {exc}")
            messagebox.showerror("Rebuild BF", str(exc))

    def save_edited_bf(self) -> None:
        """Save all currently modified BF-backed assets into one edited .bf."""
        if self.project.kind != "bf" or self.project.info is None or self.project.path is None:
            messagebox.showinfo("Save BF", "Apri un .bf prima.")
            return

        replacements: dict[int, bytes] = {}
        if self._ova_dirty and self._ova_source_asset is not None:
            replacements[self._ova_source_asset.index] = bytes(self._ova_data)
        if self._texture_dirty and self._texture_source_asset is not None:
            replacements[self._texture_source_asset.index] = bytes(self._texture_data)
        if self._material_dirty and self._material_source_asset is not None:
            replacements[self._material_source_asset.index] = bytes(self._material_data)

        if not replacements:
            messagebox.showinfo("Save BF", "Non ci sono modifiche .BF da salvare.")
            return

        target_name = self.project.path.stem + "_edited.bf"
        target = filedialog.asksaveasfilename(
            title="Salva .BF modificato",
            initialfile=target_name,
            defaultextension=".bf",
            filetypes=[("Jade Big Files", "*.bf"), ("All files", "*.*")],
        )
        if not target:
            return

        rebuilt = Path(target).with_suffix(Path(target).suffix + ".tmp")
        try:
            _repack_legacy_bigfile_changes(self.project.path, replacements, rebuilt)
            os.replace(rebuilt, Path(target))
            changed = ", ".join(f"#{index}" for index in sorted(replacements))
            self._ova_dirty = False
            self._texture_dirty = False
            self._material_original = bytes(self._material_data) if self._material_source_asset is not None else getattr(self, "_material_original", b"")
            self._material_dirty = False
            self._log(f"OK    .BF salvato: {target} • entry modificate: {changed}")
            messagebox.showinfo("Save BF", f"Creato:\n{target}\n\nEntry modificate: {changed}")
        except Exception as exc:
            try:
                rebuilt.unlink(missing_ok=True)
            except Exception:
                pass
            self._log(f"ERROR Save BF: {exc}")
            messagebox.showerror("Save BF", str(exc))

    def diagnose_ova(self) -> None:
        if self.project.kind == "bin":
            self.analyze_current_ova()
            return
        asset = self._selected_asset()
        if asset:
            self.analyze_current_ova()
        else:
            messagebox.showinfo("Diagnose OVA", "Apri un BIN oppure seleziona un asset BF.")

    def scan_textures(self) -> None:
        if self.project.kind == "bin":
            asset = self.project.assets[0] if self.project.assets else None
        else:
            asset = self._selected_asset()
        if not asset:
            messagebox.showinfo("Texture Swap", "Seleziona prima un asset .wow/.bin/.gao nel browser.")
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
            self._clear_tree(self.texture_object_tree)
            for entry in entries:
                data_type = f"0x{entry.data_type:08X}" if entry.data_type is not None else "—"
                self.texture_object_tree.insert(
                    "", "end", iid=f"obj_{entry.index}",
                    values=(entry.index, f"0x{entry.key:08X}", f"{entry.size:,}", data_type),
                )
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
                text=f"{asset.name} — {len(entries)} oggetti, {len(infos)} texture visualizzabili"
                + (f", {unsupported} texture/formati non supportati" if unsupported else "")
            )
            self.texture_info.config(text="Seleziona una texture per vedere l'anteprima.")
            self.tabs.select(self.texture_tab)
            self._log(f"OK    FileEntry scan: {asset.name} -> {len(entries)} oggetti")
            self._log(f"OK    Texture scan: {asset.name} -> {len(infos)} texture visualizzabili")
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
            raise ValueError("Questa entry non è una texture DDS.")
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
            raise ValueError("Texture palette: offset non valido.")
        palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
        palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
        if palette_entry is None or palette_entry.size < 4:
            raise ValueError(f"Palette 0x{palette_id:08X} non trovata nel BIN.")
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
            widget.configure(image="", text=f"Preview DDS non disponibile\n{exc}")

    def _set_preview_from_tga(self, widget, tga: bytes) -> None:
        try:
            from PIL import Image, ImageTk
            image = Image.open(io.BytesIO(tga)).convert("RGBA")
            image.thumbnail((480, 220), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            widget.configure(image=photo, text="")
            widget.image = photo
        except Exception as exc:
            widget.configure(image="", text=f"Preview TGA non disponibile\n{exc}")

    def on_texture_selected(self, _event=None) -> None:
        tex = self._texture_selected()
        if not tex:
            return
        self._texture_replacement = None
        self.texture_replacement_preview.configure(image="", text="Importa un .dds compatibile")
        self.texture_replacement_preview.image = None
        self._texture_rotation = 0
        self._texture_flip_x = False
        self._texture_flip_y = False
        self._update_texture_transform_label()
        self.texture_info.config(text=f"Offset 0x{tex.offset:08X} • payload 0x{tex.data_offset:08X}-0x{tex.data_end:08X} • {tex.width}x{tex.height} • {tex.format}")
        if tex.texture_type == 7:
            self._set_preview_from_tga(self.texture_preview, self._tga_blob_for_texture(tex)) if tex.format.startswith("Raw BGRA8") else self._set_preview_from_dds(self.texture_preview, self._dds_blob_for_texture(tex))
        elif tex.texture_type == 0:
            self._set_preview_from_tga(self.texture_preview, self._tga_blob_for_texture(tex))
        elif tex.texture_type == 1:
            self._set_preview_from_tga(self.texture_preview, self._palette_tga_blob_for_texture(tex))
        else:
            self.texture_preview.configure(image="", text=f"{tex.format}\nPreview per questo formato nel prossimo step")
        self.texture_apply_btn.configure(state="disabled")

    def _update_texture_transform_label(self) -> None:
        if hasattr(self, "texture_transform_label"):
            self.texture_transform_label.config(
                text=(f"Rotazione: {self._texture_rotation}° • "
                      f"Flip X: {'sì' if self._texture_flip_x else 'no'} • "
                      f"Flip Y: {'sì' if self._texture_flip_y else 'no'}")
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
            raise ValueError(f"Asse flip non supportato: {axis}")
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
            _source, payload = replacement
            if tex.texture_type == 7:
                blob = _build_dds(
                    payload, tex.width, tex.height,
                    _build_dds_header(tex.width, tex.height,
                                      _infer_dxt5_mip_count(tex.width, tex.height, len(payload)) - 1, 7)
                )
            elif tex.texture_type == 0:
                blob = _build_tga_header(tex.width, tex.height) + payload
            elif tex.texture_type == 1:
                palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                if palette_entry is None:
                    raise ValueError("Palette della texture non trovata.")
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
            self.texture_replacement_preview.configure(image="", text=f"Preview orientamento non disponibile\n{exc}")

    def import_texture_replacement(self) -> None:
        tex = self._texture_selected()
        if not tex:
            messagebox.showinfo("Texture Swap", "Seleziona prima la texture da sostituire.")
            return
        filetypes = [("Images", "*.png *.jpg *.jpeg *.tga *.bmp *.dds *.webp"), ("All files", "*.*")]
        source = filedialog.askopenfilename(title="Import replacement texture", filetypes=filetypes)
        if not source:
            return
        try:
            if tex.texture_type == 7:
                payload = _dds_payload_from_file(Path(source), tex, bytes(self._texture_data))
            elif tex.texture_type == 0:
                payload = _tga_payload_from_file(Path(source), tex, bytes(self._texture_data))
            elif tex.texture_type == 1:
                if tex.data_offset < 4:
                    raise ValueError("Texture palette: offset non valido.")
                palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                if palette_entry is None:
                    raise ValueError(f"Palette 0x{palette_id:08X} non trovata nel BIN.")
                palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                payload = _palette_payload_from_file(Path(source), tex, palette)
            else:
                raise ValueError(f"Formato texture POP non supportato: type {tex.texture_type}.")
            self._texture_replacement = (Path(source), payload)
            if tex.texture_type == 7:
                preview_dds = _build_dds(payload, tex.width, tex.height,
                                         _build_dds_header(tex.width, tex.height,
                                                           _infer_dxt5_mip_count(tex.width, tex.height, len(payload)) - 1, 7))
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
            self.texture_info.config(text=f"Importata: {Path(source).name} • {len(payload):,} B • compatibile con {tex.width}x{tex.height} {tex.format}")
            self.texture_apply_btn.configure(state="normal")
            self._log(f"OK    Texture convertita: {source} -> {len(payload):,} B DXT5 {tex.width}x{tex.height}")
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
            messagebox.showinfo("Texture Swap", "Seleziona prima la texture da estrarre.")
            return
        try:
            source_path = self.project.path
            if source_path is None:
                raise ValueError("Nessun file .BF sorgente disponibile.")
            # Keep the actual embedded bytes untouched. For type 7 this is the
            # compressed DXT5 payload plus the original texture header.
            suffix = ".dds" if tex.texture_type == 7 else ".tga"
            target = source_path.parent / f"{source_path.stem}_texture_{tex.index + 1:03d}{suffix}"
            if tex.texture_type == 7:
                blob = _dds_blob_for_dump(self._texture_data, tex)
            elif tex.texture_type == 0:
                blob = _build_tga_header(tex.width, tex.height) + bytes(self._texture_data[tex.data_offset:tex.data_end])
            else:
                blob = self._palette_tga_blob_for_texture(tex)
            target.write_bytes(blob)
            self._log(f"OK    Texture dump: {target}")
            messagebox.showinfo("Dump texture", f"Creato:\n{target}")
        except Exception as exc:
            self._log(f"ERROR Dump texture: {exc}")
            messagebox.showerror("Dump texture", str(exc))

    def apply_texture_replacement(self) -> None:
        tex = self._texture_selected()
        replacement = getattr(self, "_texture_replacement", None)
        if not tex:
            messagebox.showinfo("Texture Swap", "Seleziona prima la texture da sostituire.")
            return
        if not replacement:
            messagebox.showinfo("Texture Swap", "Import a texture before applying changes.")
            return
        _source, payload = replacement
        if len(payload) != tex.data_end - tex.data_offset:
            messagebox.showerror("Texture Swap", "La texture importata non ha la stessa dimensione compressa dell'originale.")
            return
        if self._texture_rotation or self._texture_flip_x or self._texture_flip_y:
            try:
                from PIL import Image
                if tex.texture_type == 7:
                    source_blob = _build_dds(
                        payload, tex.width, tex.height,
                        _build_dds_header(tex.width, tex.height,
                                          _infer_dxt5_mip_count(tex.width, tex.height, len(payload)) - 1, 7)
                    )
                elif tex.texture_type == 0:
                    source_blob = _build_tga_header(tex.width, tex.height) + payload
                else:
                    source_blob = self._palette_tga_blob_for_texture(tex)
                    palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                    palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                    if palette_entry is None:
                        raise ValueError("Palette della texture non trovata.")
                    palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                    source_blob = _build_palette_tga(tex.width, tex.height, palette, payload)
                source_image = Image.open(io.BytesIO(source_blob)).convert("RGBA")
                source_image = _transform_texture_image(
                    source_image, self._texture_rotation, self._texture_flip_x, self._texture_flip_y
                )
                if tex.texture_type == 7:
                    mip_count = _infer_dxt5_mip_count(tex.width, tex.height, len(payload))
                    payload = _encode_dxt5(source_image, mip_count)
                elif tex.texture_type == 0:
                    transformed = source_image.tobytes()
                    payload = bytearray(len(payload))
                    for i in range(0, len(transformed), 4):
                        r, g, b, a = transformed[i:i + 4]
                        payload[i:i + 4] = bytes((b, g, r, a))
                    payload = bytes(payload)
                elif tex.texture_type == 1:
                    palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                    palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                    if palette_entry is None:
                        raise ValueError("Palette della texture non trovata.")
                    palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                    temp_path = None
                    pixels = source_image.getdata()
                    palette_rgba = [tuple(palette[i:i + 4]) for i in range(0, 1024, 4)]
                    cache = {}
                    indexed = bytearray(tex.width * tex.height)
                    for i, pixel in enumerate(pixels):
                        if pixel not in cache:
                            cache[pixel] = min(range(256), key=lambda n: sum((pixel[c] - palette_rgba[n][c]) ** 2 for c in range(4)))
                        indexed[i] = cache[pixel]
                    payload = bytes(indexed)
                if len(payload) != tex.data_end - tex.data_offset:
                    raise ValueError("La trasformazione non ha prodotto un payload della stessa dimensione dell'originale.")
            except Exception as exc:
                messagebox.showerror("Texture Swap", f"Impossibile applicare rotazione/flip: {exc}")
                return
        self._texture_data[tex.data_offset:tex.data_end] = payload
        self._texture_dirty = self._texture_data != bytearray(self._texture_original)
        if tex.texture_type == 7:
            self._set_preview_from_tga(self.texture_preview, self._tga_blob_for_texture(tex)) if tex.format.startswith("Raw BGRA8") else self._set_preview_from_dds(self.texture_preview, self._dds_blob_for_texture(tex))
        elif tex.texture_type == 0:
            self._set_preview_from_tga(self.texture_preview, self._tga_blob_for_texture(tex))
        elif tex.texture_type == 1:
            self._set_preview_from_tga(self.texture_preview, self._palette_tga_blob_for_texture(tex))
        if tex.texture_type == 7:
            self._set_preview_from_dds(self.texture_replacement_preview, self._dds_blob_for_texture(tex))
        elif tex.texture_type == 0:
            self._set_preview_from_tga(self.texture_replacement_preview, self._tga_blob_for_texture(tex))
        elif tex.texture_type == 1:
            self._set_preview_from_tga(self.texture_replacement_preview, self._palette_tga_blob_for_texture(tex))
        self._texture_replacement = None
        self._texture_rotation = 0
        self._texture_flip_x = False
        self._texture_flip_y = False
        self._update_texture_transform_label()
        self.texture_apply_btn.configure(state="disabled")
        self.texture_info.config(text=f"Sostituzione applicata • {tex.width}x{tex.height} • payload invariato {len(payload):,} B. Usa Save/Extract/Rebuild per scrivere il file.")
        self._log(f"PATCH TEXTURE  key=0x{tex.key:08X} • payload={len(payload):,} B")

    def save_texture_asset(self) -> None:
        if not self._texture_dirty or self._texture_source_asset is None:
            messagebox.showinfo("Texture Swap", "Non ci sono modifiche texture da salvare.")
            return
        asset = self._texture_source_asset
        target = filedialog.asksaveasfilename(title="Save edited texture asset", initialfile=f"{Path(asset.name).stem}_edited.bin", defaultextension=".bin")
        if not target:
            return
        try:
            if self.project.kind == "bin":
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
                    raise ValueError("Impossibile risalire all'entry BF della texture selezionata.")
                replacements, touched = _collect_texture_key_replacements(
                    self.project.path,
                    selected_entry,
                    texture.key,
                    bytes(self._texture_data[texture.data_offset:texture.data_end]),
                    texture.texture_type,
                    texture.width,
                    texture.height,
                    bytes(self._texture_data),
                )
                _repack_legacy_bigfile_changes(self.project.path, replacements, Path(target))
                self._log(
                    f"OK    Texture key 0x{texture.key:08X}: aggiornate {len(replacements)} asset "
                    f"({', '.join(touched[:6])}{'...' if len(touched) > 6 else ''})"
                )
            self._texture_dirty = False
            self._log(f"OK    Texture asset salvato: {target}")
            messagebox.showinfo("Texture Swap", f"Creato:\n{target}")
        except Exception as exc:
            self._log(f"ERROR Save texture asset: {exc}")
            messagebox.showerror("Texture Swap", str(exc))

    def open_tools_folder(self) -> None:
        try:
            os.startfile(ROOT)
        except Exception as exc:
            self._log(f"ERROR Apertura cartella programma: {exc}")
            messagebox.showerror("Tools", str(exc))

    def about(self) -> None:
        messagebox.showinfo(
            "About PoP BF Lab",
            "PoP BF Lab\n\n"
            "Made by FulGer\n\n"
            "Prince of Persia Trilogy / Jade Engine v37-v38 toolkit.\n"
            "BF indexing, POP-LZO, OVA Variables e asset browser.\n\n"
            "Thanks & credits\n"
            "• bf_repacker_2018_05_23_1419 — by BlackDaemon\n"
            "• bin_repacker_2018_05_29_0806 — by BlackDaemon\n"
            "• io_scene_pop (Blender addon) — by kugelrund\n\n"
            "Jade Engine and Prince of Persia are properties of Ubisoft and their respective owners.\n"
            "This project is an independent community tool and is not affiliated with or endorsed by Ubisoft."
        )


if __name__ == "__main__":
    app = JadeToolkit()
    app.mainloop()

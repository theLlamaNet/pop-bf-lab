"""PoP BF Lab - standalone Prince of Persia Trilogy asset explorer/editor.

The toolkit contains its own Jade BF/OVA/POP-LZO core.  It deliberately has no
runtime dependency on the user's OVA Variable Editor or PopTools folders.
"""

from __future__ import annotations

import os
import re
import struct
import math
import base64
import json
import subprocess
import tempfile
import ctypes
import threading
import queue
import io
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import jade_mesh
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
    skin_bones: list[jade_mesh.SkinBone] | None = None
    skin_flags: int = 0
    source_joint_names: tuple[str, ...] = ()
    layout_name: str = ""


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
    # A second Jade material layer is not inherently a normal map.  It is
    # exposed separately and promoted to normal only for the WW archives.
    secondary_key: int | None = None
    metallic: float = 0.0
    alpha: float = 1.0
    source_meshes: list[int] | None = None
    texture_offset: int | None = None
    specular_offset: int | None = None
    diffuse_offset: int | None = None
    ambient: int = 0xFFFFFFFF
    diffuse_color: int = 0xFFFFFFFF
    specular_color: int = 0x00000000
    opacity: float = 1.0
    specular_exponent: float = 0.0
    ambient_offset: int | None = None
    diffuse_color_offset: int | None = None
    specular_color_offset: int | None = None
    opacity_offset: int | None = None
    specular_exponent_offset: int | None = None
    material_kind: int | None = None


def _jade_color_rgba(value: int) -> tuple[float, float, float, float]:
    """Convert the D3DCOLOR-style values stored by Jade to OpenGL RGBA."""
    if value in (0, 0x00000000):
        return (1.0, 1.0, 1.0, 1.0)
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
        ((value >> 24) & 0xFF) / 255.0,
    )


if OpenGLFrame is not None:
    class MeshViewport(OpenGLFrame):
        """GPU mesh viewport embedded directly in Tkinter."""

        def __init__(self, master, owner, **kwargs):
            super().__init__(master, **kwargs)
            self.owner = owner
            self.mesh = None
            self.textures = {}
            self.texture_ids = {}
            self.material_textures = {}
            self.material_colors = {}
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

        def set_scene(self, mesh, textures, material_textures=None, material_colors=None):
            self.mesh = mesh
            self.textures = textures or {}
            # Material IDs are local to a mesh/material-pack. Keeping this
            # lookup in the viewport prevents an ID in another mesh's pack
            # from selecting the wrong texture.
            self.material_textures = material_textures or {}
            self.material_colors = material_colors or {}
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

        def _draw_mesh(self, vertices, faces, uvs, uv_indices, material_ids, normals=None):
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
                tex_key = self.material_textures.get(mat_id)
                tex_id = self._upload_texture(tex_key, self.textures.get(tex_key)) if tex_key is not None else None
                red, green, blue, alpha = self.material_colors.get(mat_id, (0.68, 0.72, 0.80, 1.0))
                if tex_id:
                    GL.glEnable(GL.GL_TEXTURE_2D); GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
                    GL.glColor4f(red, green, blue, alpha)
                else:
                    GL.glDisable(GL.GL_TEXTURE_2D); GL.glColor4f(red, green, blue, alpha)
                GL.glBegin(GL.GL_TRIANGLES)
                for corner, vi in enumerate(face):
                    if uv_indices and face_index < len(uv_indices) and uvs:
                        ui = uv_indices[face_index][corner]
                        if 0 <= ui < len(uvs):
                            GL.glTexCoord2f(float(uvs[ui][0]), 1.0 - float(uvs[ui][1]))
                    if normals and 0 <= vi < len(normals):
                        nx, ny, nz = normals[vi]
                        GL.glNormal3f(float(nx), float(ny), float(nz))
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
                self._draw_mesh(self.mesh.vertices, self.mesh.faces, self.mesh.uvs, self.mesh.uv_indices, self.mesh.material_ids, self.mesh.normals)
                if self.mesh.second_vertices and self.mesh.second_faces:
                    self._draw_mesh(self.mesh.second_vertices, self.mesh.second_faces,
                                    self.mesh.second_uvs or [], self.mesh.second_uv_indices or [],
                                    self.mesh.second_material_ids or self.mesh.material_ids, None)
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
            # The PoP3 prototype stores some meshes as an interleaved direct
            # packet immediately after the version. Its header can be
            # validated exactly, so try it before the retail split-array
            # layout without relying on a game/build-specific marker.
            direct = _scan_pop3_direct_mesh(r, entry, version)
            if direct is not None:
                direct.index = len(meshes)
                meshes.append(direct)
                continue
            flags = r.u32()
            _flags2 = r.u32()
            num_vertices = r.u32()
            num_unknown = r.u32()
            has_unknown = r.u32() != 0
            num_uvs = r.u32()
            num_materials = r.u32()
            if num_vertices == 0 or num_vertices > 500_000 or num_uvs > 1_000_000 or num_materials > 4096:
                continue

            raw = data[entry.data_offset:entry.data_offset + entry.size]
            layout = jade_mesh.read_layout(raw)
            has_normals = layout.normals
            r.pos = layout.vertex_offset - 4

            vertex_data = r.f32s(3 * num_vertices)
            vertices = list(zip(vertex_data[0::3], vertex_data[1::3], vertex_data[2::3]))
            normals = None
            if has_normals:
                normal_data = r.f32s(3 * num_vertices)
                normals = list(zip(normal_data[0::3], normal_data[1::3], normal_data[2::3]))
            if has_unknown:
                r.bytes(min(num_unknown, num_vertices) * 4)
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
            # The cooked VB is a rendering representation of this same mesh,
            # not a second object; drawing both would cause z-fighting.
            meshes.append(MeshInfo(len(meshes), entry.key, entry.index, version,
                                   vertices, faces, uvs, uv_indices, material_ids,
                                   second_vertices=second_vertices, second_faces=second_faces,
                                   second_uvs=second_uvs, second_uv_indices=second_uv_indices,
                                   second_material_ids=second_material_ids, normals=normals,
                                   skin_bones=layout.bones, skin_flags=layout.skin_flags,
                                   layout_name="Character / skin" if layout.bones else "Mesh statica"))
        except (ValueError, IndexError, struct.error):
            continue
    return meshes


def _scan_pop3_direct_mesh(r: _PopReader, entry: PopFileEntry, version: int) -> MeshInfo | None:
    """Parse the exact interleaved mesh packet used by the PoP3 prototype."""
    start = r.pos
    valid_strides = (20, 32, 44, 52, 64)
    try:
        if start + 16 > len(r.data):
            return None
        _flags, _unknown1, _unknown2, material_count = struct.unpack_from("<4I", r.data, start)
        if not (1 <= material_count <= 256):
            return None

        pos = start + 16
        material_ids: list[tuple[int, int]] = []
        face_count = 0
        for _ in range(material_count):
            if pos + 8 > len(r.data):
                return None
            material_id, count = struct.unpack_from("<iI", r.data, pos)
            pos += 8
            if count > 1_000_000:
                return None
            material_ids.append((material_id, count))
            face_count += count
        if face_count == 0 or face_count > 2_000_000 or pos + 16 > len(r.data):
            return None

        blob_size, _unknown3, vertex_count, stride = struct.unpack_from("<4I", r.data, pos)
        if stride not in valid_strides or not (1 <= vertex_count <= 500_000):
            return None
        vertex_offset = pos + 16
        vertex_end = vertex_offset + vertex_count * stride
        if blob_size != vertex_count * stride + 8 or vertex_end + 4 > len(r.data):
            return None

        face_size = struct.unpack_from("<I", r.data, vertex_end)[0]
        if face_size == 0 or face_size % 6 or face_size // 6 != face_count:
            return None
        face_end = vertex_end + 4 + face_size
        if face_end > len(r.data):
            return None
        indices = struct.unpack_from("<" + "H" * (face_size // 2), r.data, vertex_end + 4)
        if any(index >= vertex_count for index in indices):
            return None

        vertices: list[tuple[float, float, float]] = []
        normals: list[tuple[float, float, float]] | None = [] if stride != 20 else None
        uvs: list[tuple[float, float]] = []
        uv_offset = 12 if stride == 20 else (44 if stride in (52, 64) else 24)
        for vertex_index in range(vertex_count):
            vertex_pos = vertex_offset + vertex_index * stride
            vertex = struct.unpack_from("<3f", r.data, vertex_pos)
            uv = struct.unpack_from("<2f", r.data, vertex_pos + uv_offset)
            if not all(math.isfinite(value) for value in (*vertex, *uv)):
                return None
            vertices.append(vertex)
            uvs.append(uv)
            if normals is not None:
                normal = struct.unpack_from("<3f", r.data, vertex_pos + 12)
                if not all(math.isfinite(value) for value in normal):
                    return None
                normals.append(normal)

        faces = [tuple(indices[i:i + 3]) for i in range(0, len(indices), 3)]
        return MeshInfo(-1, entry.key, entry.index, version, vertices, faces, uvs,
                        list(faces), material_ids, normals=normals)
    except (ValueError, struct.error):
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


def _classic_jade_material(entry: PopFileEntry, data: bytes) -> MaterialInfo | None:
    """Decode the 32-byte Jade single-material form (GRO type 3)."""
    if entry.data_type != 3 or entry.size < 36:
        return None
    base = entry.data_offset + 4
    payload = data[base:entry.data_offset + entry.size]
    ambient, diffuse, specular, spec_exp, opacity, _flags, texture_key, _mask = struct.unpack_from("<IIIffIII", payload, 0)
    if texture_key in (0, 0xFFFFFFFF):
        texture_key = None
    if not math.isfinite(opacity) or not -0.01 <= opacity <= 1.01:
        opacity = 1.0
    if not math.isfinite(spec_exp):
        spec_exp = 0.0
    return MaterialInfo(index=0, material_id=0, material_key=entry.key, texture_key=texture_key,
                        metallic=max(0.0, min(128.0, spec_exp)), alpha=max(0.0, min(1.0, opacity)),
                        ambient=ambient, diffuse_color=diffuse, specular_color=specular,
                        opacity=opacity, specular_exponent=spec_exp,
                        ambient_offset=base + 0, diffuse_color_offset=base + 4,
                        specular_color_offset=base + 8, specular_exponent_offset=base + 12,
                        opacity_offset=base + 16, texture_offset=base + 24, source_meshes=[])


def _modern_jade_material(entry: PopFileEntry, data: bytes) -> MaterialInfo | None:
    """Decode Jade leaf materials (kinds 4..9), including multitexture layers."""
    payload_offset = entry.data_offset + 4
    payload = data[payload_offset:entry.data_offset + entry.size]
    if len(payload) < 28:
        return None
    kind = struct.unpack_from("<I", payload, 0)[0]
    base_by_kind = {4: 40, 5: 40, 6: 40, 7: 42, 8: 50, 9: 63}
    if kind not in base_by_kind:
        return None
    base = base_by_kind[kind]
    if base + 4 > len(payload):
        return None
    layer_stride = {8: 26, 9: 39}.get(kind, 0)
    offsets = [base]
    if layer_stride and (len(payload) - (base + 4)) % layer_stride == 0:
        offsets = list(range(base, len(payload) - 3, layer_stride))
    keys = [struct.unpack_from("<I", payload, off)[0] for off in offsets]
    keys = [key for key in keys if key not in (0, 0xFFFFFFFF)]
    texture_key = keys[0] if keys else None
    secondary_key = keys[1] if len(keys) > 1 else None
    ambient, diffuse, specular = struct.unpack_from("<III", payload, 4)
    spec_exp, opacity = struct.unpack_from("<ff", payload, 16)
    if not math.isfinite(opacity) or not -0.01 <= opacity <= 1.01:
        opacity = 1.0
    if not math.isfinite(spec_exp):
        spec_exp = 0.0
    return MaterialInfo(
        index=0, material_id=0, material_key=entry.key, texture_key=texture_key,
        secondary_key=secondary_key, metallic=max(0.0, min(128.0, spec_exp)), alpha=max(0.0, min(1.0, opacity)),
        ambient=ambient, diffuse_color=diffuse, specular_color=specular,
        opacity=opacity, specular_exponent=spec_exp,
        ambient_offset=payload_offset + 4, diffuse_color_offset=payload_offset + 8,
        specular_color_offset=payload_offset + 12, specular_exponent_offset=payload_offset + 16,
        opacity_offset=payload_offset + 20, texture_offset=payload_offset + base,
        material_kind=kind, source_meshes=[],
    )


def _scan_pop_material_records(data: bytes, valid_texture_keys: set[int] | None = None) -> dict[int, MaterialInfo]:
    """Read the Jade type-5 material records used by the Blender Addon."""
    records: dict[int, MaterialInfo] = {}
    for entry in _parse_pop_file_entries(data):
        if entry.data_type != 5 or entry.size < 16:
            continue
        try:
            # io_scene_pop identifies POP material records by FileEntry type 5.
            # Type 3 has unrelated uses in these assets and must not be
            # promoted to a material merely because its first 32 bytes fit.
            blob = data[entry.data_offset:entry.data_offset + entry.size]
            r = _PopReader(blob[4:])
            version = r.u32()
            # POP's type-5 payload starts with a serialization version, not a
            # Jade leaf-material kind. Versions 4..9 used to be mistaken for
            # kind 4..9 and produced random/white texture assignments.
            if not 3 <= version <= 9:
                modern = _modern_jade_material(entry, data)
                if modern is not None:
                    modern.index = len(records)
                    modern.material_id = len(records)
                    records[entry.key] = modern
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
            secondary_key = None
            if valid_texture_keys and texture_offset is not None:
                # The Blender POP reader intentionally stopped after the base
                # texture. Remaining aligned key fields are extra stages;
                # accept only keys that actually resolve to a texture in this
                # BF, which prevents flags/colours being misidentified.
                for offset in range(r.pos, len(r.data) - 3, 4):
                    candidate = struct.unpack_from("<I", r.data, offset)[0]
                    if candidate != texture_key and candidate in valid_texture_keys:
                        secondary_key = candidate
                        break
            records[entry.key] = MaterialInfo(
                index=len(records), material_id=len(records), material_key=entry.key,
                texture_key=texture_key, secondary_key=secondary_key,
                metallic=max(0.0, min(1.0, float(specular))),
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
    entries = _parse_pop_file_entries(data)
    by_key = {entry.key: entry for entry in entries}

    def assign_visual(gro_key: int, grm_key: int, name: str) -> None:
        direct = by_mesh.get(gro_key)
        if direct is not None:
            direct.material_pack_key = grm_key
            direct.object_name = name
            return
        # GAO can point to a geometry group instead of a type-1 mesh.
        # Resolve every mesh key stored by the group, as MeshSwap.cpp does.
        group = by_key.get(gro_key)
        if group is None or group.data_type == 1:
            return
        payload = data[group.data_offset + 4:group.data_offset + group.size]
        seen: set[int] = set()
        for offset in range(0, len(payload) - 3):
            candidate = struct.unpack_from("<I", payload, offset)[0]
            mesh = by_mesh.get(candidate)
            if mesh is not None and candidate not in seen:
                mesh.material_pack_key = grm_key
                mesh.object_name = name
                seen.add(candidate)

    for entry in entries:
        if entry.data_type != struct.unpack("<I", b".gao")[0] or entry.size < 24:
            continue
        try:
            # The FileEntry's first dword is '.gao'; Gao.cpp consumes it as
            # the type before deserialising this payload.
            payload = data[entry.data_offset + 4:entry.data_offset + entry.size]
            _version, _editor_flags, identity, name_len = struct.unpack_from("<4I", payload, 0)
            if name_len > 4096 or 16 + name_len > len(payload):
                continue
            name = payload[16:16 + name_len].rstrip(b"\0").decode("latin-1", errors="replace")
            matrix_offset = 16 + name_len + 10
            bounds_size = 48 if identity & 0x00080000 else 24
            visual_offset = matrix_offset + 68 + bounds_size
            if identity & 0x00004000 and visual_offset + 8 <= len(payload):
                gro_key, grm_key = struct.unpack_from("<II", payload, visual_offset)
                assign_visual(gro_key, grm_key, name)
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
    """Find Jade texture entries, including PAL8, DXT1, DXT3 and 4-bit formats."""
    textures: list[TextureInfo] = []
    for entry in _parse_pop_file_entries(data):
        if entry.size < 56 or entry.data_type is None:
            continue
        # FileEntry data begins with four bytes before the Jade texture header.
        if (struct.unpack_from("<I", data, entry.data_offset + 4)[0] != 0xFFFFFFFF
                or struct.unpack_from("<I", data, entry.data_offset + 24)[0] != 0xCAD01234
                or struct.unpack_from("<I", data, entry.data_offset + 32)[0] != 0xC0DEC0DE):
            continue
        width, height = struct.unpack_from("<hh", data, entry.data_offset + 12)
        texture_type = struct.unpack_from("<I", data, entry.data_offset + 40)[0]
        version = struct.unpack_from("<I", data, entry.data_offset + 36)[0]
        if texture_type not in (0, 1, 5, 6, 7, 11) or not (1 <= width <= 8192 and 1 <= height <= 8192):
            continue
        data_offset = entry.data_offset + 56
        if texture_type in (1, 5):
            data_offset += 4
        elif texture_type == 11 and version >= 4:
            data_offset += 8
        data_end = entry.data_offset + entry.size
        if data_end <= data_offset:
            continue
        actual_w, actual_h = struct.unpack_from("<II", data, entry.data_offset + 44)
        # Jade Toolkit uses these actual dimensions directly; the old /2 rule
        # made modded textures appear missing or with a wrong data contract.
        stored_w, stored_h = (actual_w, actual_h) if 1 <= actual_w <= 8192 and 1 <= actual_h <= 8192 else (width, height)
        payload_size = data_end - data_offset
        blocks = max(1, (stored_w + 3) // 4) * max(1, (stored_h + 3) // 4)
        if texture_type == 7:
            fmt = "Raw BGRA8 (reference)" if payload_size == 4 else ("Raw BGRA8" if payload_size == stored_w * stored_h * 4 else "DXT5")
        elif texture_type == 6:
            fmt = "DXT3" if payload_size >= blocks * 16 else "DXT3 (truncated)"
        elif texture_type == 5:
            fmt = "DXT1" if payload_size >= blocks * 8 else "DXT1 (truncated)"
        elif texture_type == 0:
            fmt = "TGA/BGR24" if payload_size == stored_w * stored_h * 3 else ("TGA/BGRA8" if payload_size >= stored_w * stored_h * 4 else "Type 0 (truncated)")
        elif texture_type == 1:
            fmt = "8-bit palette"
        else:
            fmt = "4-bit grayscale"
        textures.append(TextureInfo(len(textures), entry.data_offset, data_offset, data_end,
                                    stored_w, stored_h, texture_type, entry.key, fmt,
                                    stored_w, stored_h))
    return textures


def _decode_pop_texture_image(data: bytes, tex: TextureInfo):
    """Return a PIL RGBA image for every previewable POP texture type."""
    from PIL import Image
    if tex.texture_type == 7:
        blob = _dds_blob_for_dump(data, tex)
    elif tex.texture_type in (5, 6):
        blob = _compressed_dds_for_preview(bytes(data[tex.data_offset:tex.data_end]), tex)
    elif tex.texture_type == 0:
        pixel_bytes = tex.storage_width * tex.storage_height
        payload = data[tex.data_offset:tex.data_end]
        depth = 32 if len(payload) >= pixel_bytes * 4 else 24
        blob = _build_tga_header(tex.storage_width, tex.storage_height, depth) + payload[:pixel_bytes * (depth // 8)]
    elif tex.texture_type == 1:
        entries = _parse_pop_file_entries(data)
        if tex.data_offset < 4:
            raise ValueError("Texture palette con offset non valido.")
        palette_id = struct.unpack_from("<I", data, tex.data_offset - 4)[0]
        palette_entry = next((entry for entry in entries if entry.key == palette_id), None)
        if palette_entry is None:
            raise ValueError(f"Palette 0x{palette_id:08X} non trovata.")
        palette = data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size]
        blob = _build_palette_tga(tex.width, tex.height, palette, data[tex.data_offset:tex.data_end])
    elif tex.texture_type == 11:
        blob = _build_4bit_tga(tex.width, tex.height, data[tex.data_offset:tex.data_end])
    else:
        raise ValueError(f"Formato texture POP {tex.texture_type} non supportato.")
    return Image.open(io.BytesIO(blob)).convert("RGBA")



def _read_glb_for_mesh_swap(path: Path) -> tuple[dict, list[bytes]]:
    """Read the self-contained GLB contract used by Jade Toolkit's mesh swap."""
    raw = path.read_bytes()
    if len(raw) < 20 or raw[:4] != b"glTF":
        raise ValueError("Mesh Swap richiede un GLB binario glTF 2.0.")
    _magic, version, declared_size = struct.unpack_from("<4sII", raw, 0)
    if version != 2 or declared_size > len(raw):
        raise ValueError("GLB non valido o non completamente disponibile.")
    pos = 12
    document = None
    buffers: list[bytes] = []
    while pos + 8 <= declared_size:
        length, kind = struct.unpack_from("<I4s", raw, pos)
        pos += 8
        if pos + length > declared_size:
            raise ValueError("Chunk GLB oltre la fine del file.")
        chunk = raw[pos:pos + length]
        pos += length
        if kind == b"JSON":
            document = json.loads(chunk.decode("utf-8").rstrip())
        elif kind == b"BIN\0":
            buffers.append(chunk)
    if not isinstance(document, dict) or not buffers:
        raise ValueError("Il GLB deve contenere JSON e buffer binario.")
    return document, buffers


def _glb_accessor_values(document: dict, buffers: list[bytes], accessor_index: int):
    """Bounds-checked glTF accessors, including normalized and sparse data."""
    accessors, views = document.get("accessors", []), document.get("bufferViews", [])
    if not isinstance(accessor_index, int) or not 0 <= accessor_index < len(accessors):
        raise ValueError("Accessor GLB non trovato.")
    accessor = accessors[accessor_index]
    formats = {5120:("b",1),5121:("B",1),5122:("h",2),5123:("H",2),5125:("I",4),5126:("f",4)}
    component_type = accessor.get("componentType")
    components = {"SCALAR":1,"VEC2":2,"VEC3":3,"VEC4":4,"MAT4":16}.get(accessor.get("type"))
    count = accessor.get("count")
    if component_type not in formats or components is None or not isinstance(count,int) or not 0 <= count <= 2000000:
        raise ValueError("Tipo o conteggio accessor GLB non valido.")
    fmt, component_size = formats[component_type]
    def read_view(view_index, offset, n, fmt, size, components, allow_stride=True):
        if not isinstance(view_index,int) or not 0 <= view_index < len(views):
            raise ValueError("bufferView GLB non valida.")
        view = views[view_index]
        bi = view.get("buffer",0)
        if not isinstance(bi,int) or not 0 <= bi < len(buffers):
            raise ValueError("Buffer GLB non disponibile.")
        start, length = view.get("byteOffset",0), view.get("byteLength")
        stride = view.get("byteStride",size*components) if allow_stride else size*components
        if any(not isinstance(x,int) or x<0 for x in (start,length,offset,stride)) or stride < size*components or stride%size:
            raise ValueError("Offset, lunghezza o stride GLB non validi.")
        end = offset + ((n-1)*stride+size*components if n else 0)
        if start+length > len(buffers[bi]) or end > length:
            raise ValueError("Accessor GLB oltre la propria bufferView.")
        return [struct.unpack_from("<"+fmt*components,buffers[bi],start+offset+i*stride) for i in range(n)]
    if "bufferView" in accessor:
        values = read_view(accessor["bufferView"],accessor.get("byteOffset",0),count,fmt,component_size,components)
    else:
        if accessor.get("byteOffset",0): raise ValueError("Accessor senza bufferView con offset non nullo.")
        values = [(0,)*components for _ in range(count)]
    if "sparse" in accessor:
        sparse = accessor["sparse"]
        sn = sparse.get("count")
        if not isinstance(sn,int) or not 0 < sn <= count: raise ValueError("Conteggio sparse GLB non valido.")
        si, sv = sparse.get("indices",{}), sparse.get("values",{})
        st = si.get("componentType")
        if st not in (5121,5123,5125): raise ValueError("Tipo indici sparse non valido.")
        sf, ss = formats[st]
        indices = read_view(si.get("bufferView"),si.get("byteOffset",0),sn,sf,ss,1,False)
        replacements = read_view(sv.get("bufferView"),sv.get("byteOffset",0),sn,fmt,component_size,components,False)
        previous = -1
        for (i,),value in zip(indices,replacements):
            if not previous < i < count: raise ValueError("Indici sparse non ordinati o fuori intervallo.")
            values[i] = value; previous = i
    limits = {5120:127.,5121:255.,5122:32767.,5123:65535.,5125:4294967295.}
    if accessor.get("normalized") and component_type != 5126:
        values = [tuple(max(-1.,x/limits[component_type]) for x in value) for value in values]
    if any(not math.isfinite(x) for value in values for x in value):
        raise ValueError("Accessor GLB con valori non finiti.")
    return [tuple(float(x) for x in value) for value in values]


def _glb_image(document: dict, buffers: list[bytes], image_index: int, source_path: Path):
    """Load an embedded, data-URI or sidecar glTF image for the preview."""
    from PIL import Image
    images = document.get("images", [])
    if not (0 <= image_index < len(images)):
        return None
    image = images[image_index]
    payload = None
    if isinstance(image.get("uri"), str):
        uri = image["uri"]
        if uri.startswith("data:"):
            try:
                payload = base64.b64decode(uri.split(",", 1)[1])
            except (IndexError, ValueError) as exc:
                raise ValueError("Data URI texture GLB non valida.") from exc
        else:
            candidate = (source_path.parent / uri).resolve()
            if candidate.is_file():
                payload = candidate.read_bytes()
    elif isinstance(image.get("bufferView"), int):
        views = document.get("bufferViews", [])
        view_index = image["bufferView"]
        if 0 <= view_index < len(views):
            view = views[view_index]
            buffer_index = view.get("buffer", 0)
            if 0 <= buffer_index < len(buffers):
                begin = int(view.get("byteOffset", 0))
                end = begin + int(view.get("byteLength", 0))
                payload = buffers[buffer_index][begin:end]
    return Image.open(io.BytesIO(payload)).convert("RGBA") if payload else None


def _glb_mat_mul(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    """Multiply glTF column-major 4x4 matrices."""
    return tuple(sum(left[row + 4 * k] * right[k + 4 * column] for k in range(4))
                 for column in range(4) for row in range(4))


def _glb_node_matrix(node: dict) -> tuple[float, ...]:
    """Return a validated local glTF transform, baking TRS when needed."""
    matrix = node.get("matrix")
    if matrix is not None:
        if not isinstance(matrix, list) or len(matrix) != 16:
            raise ValueError("Nodo GLB con matrix non valida.")
        values = tuple(float(value) for value in matrix)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Nodo GLB con matrix non finita.")
        return values
    translation = node.get("translation", [0.0, 0.0, 0.0])
    rotation = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    scale = node.get("scale", [1.0, 1.0, 1.0])
    if (not isinstance(translation, list) or len(translation) != 3 or
            not isinstance(rotation, list) or len(rotation) != 4 or
            not isinstance(scale, list) or len(scale) != 3):
        raise ValueError("Nodo GLB con translation, rotation o scale non validi.")
    tx, ty, tz = (float(value) for value in translation)
    x, y, z, w = (float(value) for value in rotation)
    sx, sy, sz = (float(value) for value in scale)
    if not all(math.isfinite(value) for value in (tx, ty, tz, x, y, z, w, sx, sy, sz)):
        raise ValueError("Trasformazione GLB non finita.")
    length = math.sqrt(x*x + y*y + z*z + w*w)
    if length < 1e-20:
        raise ValueError("Rotazione GLB nulla.")
    x, y, z, w = x / length, y / length, z / length, w / length
    return (
        (1 - 2*y*y - 2*z*z) * sx, (2*x*y + 2*z*w) * sx, (2*x*z - 2*y*w) * sx, 0.0,
        (2*x*y - 2*z*w) * sy, (1 - 2*x*x - 2*z*z) * sy, (2*y*z + 2*x*w) * sy, 0.0,
        (2*x*z + 2*y*w) * sz, (2*y*z - 2*x*w) * sz, (1 - 2*x*x - 2*y*y) * sz, 0.0,
        tx, ty, tz, 1.0,
    )


def _glb_transform_point(matrix: tuple[float, ...], point: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = point
    return (matrix[0]*x + matrix[4]*y + matrix[8]*z + matrix[12],
            matrix[1]*x + matrix[5]*y + matrix[9]*z + matrix[13],
            matrix[2]*x + matrix[6]*y + matrix[10]*z + matrix[14])


def _glb_transform_normal(matrix: tuple[float, ...], normal: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply inverse-transpose of the baked node transform."""
    a, b, c, d, e, f, g, h, i = matrix[0], matrix[4], matrix[8], matrix[1], matrix[5], matrix[9], matrix[2], matrix[6], matrix[10]
    determinant = a*(e*i-f*h) - b*(d*i-f*g) + c*(d*h-e*g)
    if abs(determinant) < 1e-20:
        raise ValueError("Nodo GLB con scala nulla: impossibile importare la mesh.")
    x, y, z = normal
    result = ((e*i-f*h)*x + (f*g-d*i)*y + (d*h-e*g)*z,
              (c*h-b*i)*x + (a*i-c*g)*y + (b*g-a*h)*z,
              (b*f-c*e)*x + (c*d-a*f)*y + (a*e-b*d)*z)
    length = math.hypot(*result)
    return tuple(value / length for value in result) if length > 1e-20 else (0.0, 0.0, 1.0)


def _load_glb_mesh_for_swap(path: Path) -> tuple[MeshInfo, dict[int, object], dict[int, int], dict[int, tuple[float, float, float, float]]]:
    """Import rest geometry to Jade axes; detect rigs for rebinding to the BF skin."""
    document, buffers = _read_glb_for_mesh_swap(path)
    source_joint_names = []
    meshes = document.get("meshes", [])
    if not isinstance(meshes, list) or not meshes:
        raise ValueError("Il GLB non contiene mesh.")
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("La tabella nodi GLB non è valida.")
    identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    instances = []
    child_nodes: set[int] = set()
    for node in nodes:
        children = node.get("children", []) if isinstance(node, dict) else []
        if not isinstance(children, list) or any(not isinstance(index, int) or not 0 <= index < len(nodes) for index in children):
            raise ValueError("Gerarchia nodi GLB non valida.")
        child_nodes.update(children)

    def visit(index: int, parent: tuple[float, ...], ancestry: set[int]) -> None:
        if index in ancestry:
            raise ValueError("Gerarchia GLB ciclica.")
        node = nodes[index]
        if not isinstance(node, dict):
            raise ValueError("Nodo GLB non valido.")
        joint_names = ()
        if "skin" in node:
            skins = document.get("skins", [])
            skin_index = node["skin"]
            if not isinstance(skin_index, int) or not 0 <= skin_index < len(skins):
                raise ValueError("Riferimento skin GLB non valido.")
            joints = skins[skin_index].get("joints", [])
            if not joints or len(set(joints)) != len(joints) or any(not isinstance(j, int) or not 0 <= j < len(nodes) for j in joints):
                raise ValueError("Tabella joints GLB non valida.")
            joint_names = tuple(str(nodes[j].get("name") or f"joint_{j}") for j in joints)
            source_joint_names.extend(joint_names)
        world = _glb_mat_mul(parent, _glb_node_matrix(node))
        mesh_index = node.get("mesh")
        if mesh_index is not None:
            if not isinstance(mesh_index, int) or not 0 <= mesh_index < len(meshes):
                raise ValueError("Nodo GLB con riferimento mesh non valido.")
            if "weights" in node:
                raise ValueError("Morph target GLB non supportati: esportare la posa di riposo.")
            instances.append((mesh_index, world, str(node.get("name") or meshes[mesh_index].get("name") or f"mesh_{mesh_index}"), joint_names))
        for child in node.get("children", []):
            visit(child, world, ancestry | {index})

    if nodes:
        scenes = document.get("scenes", [])
        scene_index = document.get("scene", 0)
        roots = []
        if isinstance(scene_index, int) and isinstance(scenes, list) and 0 <= scene_index < len(scenes):
            roots = scenes[scene_index].get("nodes", [])
        if not isinstance(roots, list) or not roots:
            roots = [index for index in range(len(nodes)) if index not in child_nodes]
        if not roots:
            raise ValueError("Il GLB non ha nodi radice importabili.")
        for root in roots:
            if not isinstance(root, int) or not 0 <= root < len(nodes):
                raise ValueError("Scena GLB con nodo radice non valido.")
            visit(root, identity, set())
    else:
        instances = [(index, identity, str(mesh.get("name") or f"mesh_{index}"), ())
                     for index, mesh in enumerate(meshes) if isinstance(mesh, dict)]
    if not instances:
        raise ValueError("Il GLB non contiene istanze mesh nella scena attiva.")

    materials = document.get("materials", [])
    textures = document.get("textures", [])
    vertices: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []
    uv_indices: list[tuple[int, int, int]] = []
    material_ids: list[tuple[int, int]] = []
    preview_images: dict[int, object] = {}
    material_textures: dict[int, int] = {}
    material_colors: dict[int, tuple[float, float, float, float]] = {}

    for mesh_index, transform, instance_name, joint_names in instances:
        mesh_document = meshes[mesh_index]
        if not isinstance(mesh_document, dict) or not isinstance(mesh_document.get("primitives"), list):
            raise ValueError("Mesh GLB senza primitive valide.")
        determinant = (transform[0]*(transform[5]*transform[10]-transform[9]*transform[6])
                       - transform[4]*(transform[1]*transform[10]-transform[9]*transform[2])
                       + transform[8]*(transform[1]*transform[6]-transform[5]*transform[2]))
        for primitive_index, primitive in enumerate(mesh_document["primitives"]):
            if not isinstance(primitive, dict):
                raise ValueError("Primitiva GLB non valida.")
            mode = primitive.get("mode", 4)
            if mode not in (4, 5, 6):
                continue  # Skip points/lines automatically; they cannot form a Jade mesh.
            attrs = primitive.get("attributes", {})
            if not isinstance(attrs, dict) or primitive.get("targets"):
                raise ValueError("Morph target non supportati: esportare la geometria in posa di riposo senza morph.")
            position_accessor = attrs.get("POSITION")
            if not isinstance(position_accessor, int):
                raise ValueError("Primitiva priva di POSITION.")
            positions = _glb_accessor_values(document, buffers, position_accessor)
            if not positions or any(len(position) != 3 for position in positions):
                raise ValueError("POSITION deve contenere una VEC3 per vertice.")
            texcoords = _glb_accessor_values(document, buffers, attrs["TEXCOORD_0"]) if isinstance(attrs.get("TEXCOORD_0"), int) else []
            imported_normals = _glb_accessor_values(document, buffers, attrs["NORMAL"]) if isinstance(attrs.get("NORMAL"), int) else []
            if imported_normals and (len(imported_normals) != len(positions) or any(len(normal) != 3 for normal in imported_normals)):
                raise ValueError("NORMAL deve contenere una VEC3 per vertice.")
            if texcoords and (len(texcoords) != len(positions) or any(len(uv) != 2 for uv in texcoords)):
                raise ValueError("TEXCOORD_0 deve contenere una VEC2 per vertice.")
            indices = (_glb_accessor_values(document, buffers, primitive["indices"])
                       if isinstance(primitive.get("indices"), int)
                       else [(float(index),) for index in range(len(positions))])
            raw_indices = [int(value[0]) for value in indices]
            if any(len(value) != 1 or value[0] != int(value[0]) or not 0 <= int(value[0]) < len(positions) for value in indices):
                raise ValueError("Indici GLB fuori intervallo o non interi.")
            triangles: list[tuple[int, int, int]] = []
            if mode == 4:
                if len(raw_indices) % 3:
                    raise ValueError(f"Primitiva GLB {primitive_index} non triangolare.")
                triangles = [tuple(raw_indices[offset:offset + 3]) for offset in range(0, len(raw_indices), 3)]
            elif mode == 5:
                for offset in range(2, len(raw_indices)):
                    a, b, c = raw_indices[offset-2], raw_indices[offset-1], raw_indices[offset]
                    triangles.append((b, a, c) if offset % 2 else (a, b, c))
            else:
                triangles = [(raw_indices[0], raw_indices[offset-1], raw_indices[offset]) for offset in range(2, len(raw_indices))]
            triangles = [triangle for triangle in triangles if len(set(triangle)) == 3]
            if not triangles:
                continue
            if joint_names:
                if "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
                    raise ValueError("Mesh skinned senza JOINTS_0/WEIGHTS_0.")
                for joint_attr in (key for key in attrs if key.startswith("JOINTS_")):
                    weight_attr = "WEIGHTS_" + joint_attr[7:]
                    if weight_attr not in attrs:
                        raise ValueError("Attributo WEIGHTS corrispondente mancante.")
                    jvalues = _glb_accessor_values(document, buffers, attrs[joint_attr])
                    wvalues = _glb_accessor_values(document, buffers, attrs[weight_attr])
                    if len(jvalues) != len(positions) or len(wvalues) != len(positions):
                        raise ValueError("Conteggio JOINTS/WEIGHTS diverso da POSITION.")
                    if any(len(js) != 4 or any(j != int(j) or not 0 <= j < len(joint_names) for j in js) for js in jvalues):
                        raise ValueError("Indici joints GLB fuori intervallo.")
                    if any(len(ws) != 4 or any(not math.isfinite(w) or w < 0 for w in ws) for ws in wvalues):
                        raise ValueError("Pesi GLB non validi.")
            vertex_start, uv_start = len(vertices), len(uvs)
            converted_positions = []
            for position in positions:
                x, y, z = _glb_transform_point(transform, position)
                converted_positions.append((x, -z, y))
            vertices.extend(converted_positions)
            if determinant < 0:
                triangles = [(a, c, b) for a, b, c in triangles]
            if imported_normals:
                normal_sign = -1.0 if determinant < 0 else 1.0
                for normal in imported_normals:
                    x, y, z = _glb_transform_normal(transform, normal)
                    normals.append((normal_sign * x, -normal_sign * z, normal_sign * y))
            else:
                normals.extend(_mesh_vertex_normals(converted_positions, triangles))
            uvs.extend((uv[0], uv[1]) for uv in texcoords) if texcoords else uvs.extend([(0.0, 0.0)] * len(positions))
            for triangle in triangles:
                faces.append(tuple(vertex_start + index for index in triangle))
                uv_indices.append(tuple(uv_start + index for index in triangle))
            material_id = int(primitive.get("material", primitive_index))
            material_ids.append((material_id, len(triangles)))
            material = materials[material_id] if isinstance(materials, list) and 0 <= material_id < len(materials) else {}
            pbr = material.get("pbrMetallicRoughness", {}) if isinstance(material, dict) else {}
            factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0]) if isinstance(pbr, dict) else [1.0, 1.0, 1.0, 1.0]
            factor = (factor if isinstance(factor, list) else [1.0] * 4) + [1.0] * 4
            material_colors[material_id] = tuple(max(0.0, min(1.0, float(value))) for value in factor[:4])
            texture_info = pbr.get("baseColorTexture", {}) if isinstance(pbr, dict) else {}
            texture_index = texture_info.get("index") if isinstance(texture_info, dict) else None
            if isinstance(texture_index, int) and isinstance(textures, list) and 0 <= texture_index < len(textures):
                image_index = textures[texture_index].get("source")
                if isinstance(image_index, int):
                    image = _glb_image(document, buffers, image_index, path)
                    if image is not None:
                        texture_key = 0xF0000000 | (image_index & 0x0FFFFFFF)
                        preview_images[texture_key] = image
                        material_textures[material_id] = texture_key
    if not faces:
        raise ValueError("Il GLB non contiene triangoli importabili.")
    label = ", ".join(dict.fromkeys(name for _, _, name, _ in instances))
    mesh = MeshInfo(0, 0, -1, 0, vertices, faces, uvs, uv_indices, material_ids,
                    object_name=label or path.stem, normals=normals,
                    source_joint_names=tuple(dict.fromkeys(source_joint_names)),
                    layout_name="Character GLB" if source_joint_names else "Mesh statica GLB")
    return mesh, preview_images, material_textures, material_colors

def _load_obj_mesh_for_swap(path: Path, axes: str = "Auto"):
    """OBJ corners retain independent position/UV/normal indices and materials."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    jade_axes = axes == "Jade Z-up" or (axes == "Auto" and "# Exported by PoP BF Lab" in text[:200])
    def convert(p): return tuple(p) if jade_axes else (p[0], -p[2], p[1])
    positions, texcoords, source_normals = [], [], []
    vertices, normals, faces, uv_faces, materials = [], [], [], [], []
    lookup, material_lookup, libraries = {}, {}, []
    material = 0
    smoothing = "off"
    groups = []
    def index(value, count):
        n = int(value)
        result = n - 1 if n > 0 else count + n
        if n == 0 or not 0 <= result < count: raise ValueError("Indice OBJ fuori intervallo.")
        return result
    # OBJ line continuation is common in hand-authored polygons.
    text = text.replace("\\\n", " ")
    for line_number, line in enumerate(text.splitlines(), 1):
        parts = line.split("#", 1)[0].split()
        if not parts: continue
        op, values = parts[0], parts[1:]
        try:
            if op in ("v", "vt", "vn"):
                size = 2 if op == "vt" else 3
                v = [float(x) for x in values[:size]]
                if op == "vt" and len(v) == 1: v.append(0.0)
                if len(v) != size or not all(math.isfinite(x) and abs(x) <= 3.4e38 for x in v):
                    raise ValueError("Coordinate OBJ non valide.")
                if op == "v":
                    if len(values) == 4:
                        w = float(values[3])
                        if not math.isfinite(w) or abs(w) < 1e-20: raise ValueError("Coordinata omogenea OBJ nulla.")
                        v = [x/w for x in v]
                    positions.append(convert(v))
                elif op == "vn": source_normals.append(convert(v))
                else: texcoords.append((v[0], 1.0-v[1]))
            elif op == "usemtl":
                name = " ".join(values)
                if name not in material_lookup: material_lookup[name] = len(material_lookup)
                material = material_lookup[name]
            elif op == "s": smoothing = values[0] if values else "off"
            elif op in ("o", "g"): groups.append(" ".join(values))
            elif op == "mtllib": libraries.append(" ".join(values))
            elif op == "f":
                corners = []
                for value in values:
                    ref = value.split("/")
                    if len(ref) > 3: raise ValueError("Riferimento faccia OBJ non valido.")
                    vi = index(ref[0], len(positions))
                    ti = index(ref[1], len(texcoords)) if len(ref) > 1 and ref[1] else None
                    ni = index(ref[2], len(source_normals)) if len(ref) > 2 and ref[2] else None
                    corners.append((vi, ti, ni))
                for triangle in jade_mesh.triangulate_polygon([positions[v] for v,_,_ in corners]):
                    face, uv = [], []
                    for c in triangle:
                        vi, ti, ni = corners[c]
                        pair = (vi, ni, None if ni is not None else (line_number if smoothing in ("off", "0") else smoothing))
                        if pair not in lookup:
                            lookup[pair] = len(vertices)
                            vertices.append(positions[vi])
                            normals.append(source_normals[ni] if ni is not None else None)
                        face.append(lookup[pair]); uv.append(ti)
                    faces.append(tuple(face)); uv_faces.append(tuple(uv))
                    if materials and materials[-1][0] == material:
                        materials[-1] = (material, materials[-1][1] + 1)
                    else: materials.append((material, 1))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"OBJ riga {line_number}: {exc}") from exc
    if not faces: raise ValueError("L'OBJ non contiene facce importabili.")
    if any(i is None for f in uv_faces for i in f):
        fallback_uv = len(texcoords)
        texcoords.append((0.0, 0.0))
        uv_faces = [tuple(fallback_uv if i is None else i for i in f) for f in uv_faces]
    computed = _mesh_vertex_normals(vertices, faces)
    normals = [n if n is not None else computed[i] for i,n in enumerate(normals)]
    images, textures, colors = {}, {}, {}
    for library in libraries:
        mtl_path = path.parent / library
        if not mtl_path.is_file(): continue
        current = None
        for line in mtl_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            parts = line.split("#", 1)[0].split()
            if not parts: continue
            op, values = parts[0], parts[1:]
            if op == "newmtl": current = material_lookup.get(" ".join(values))
            elif current is not None:
                if op == "Kd" and len(values) >= 3:
                    colors[current] = tuple(max(0., min(1., float(x))) for x in values[:3]) + (colors.get(current, (1.,)*4)[3],)
                elif op in ("d", "Tr") and values:
                    alpha = float(values[0]); alpha = 1-alpha if op == "Tr" else alpha
                    colors[current] = colors.get(current, (1.,)*4)[:3] + (max(0., min(1., alpha)),)
                elif op == "map_Kd" and values and not values[0].startswith("-"):
                    image_path = mtl_path.parent / " ".join(values)
                    if image_path.is_file():
                        from PIL import Image
                        with Image.open(image_path) as image:
                            key = 0xF0000000 + current
                            images[key] = image.convert("RGBA")
                            textures[current] = key
    mesh = MeshInfo(0,0,-1,0,vertices,faces,texcoords,uv_faces,materials,
                    object_name=", ".join(dict.fromkeys(groups)) or path.stem,
                    normals=normals, layout_name="Mesh statica OBJ")
    return mesh, images, textures, colors


def _load_mesh_for_swap(path: Path, obj_axes: str = "Auto"):
    if path.suffix.lower() == ".obj": return _load_obj_mesh_for_swap(path, obj_axes)
    if path.suffix.lower() == ".glb": return _load_glb_mesh_for_swap(path)
    raise ValueError("Formato mesh non supportato: scegliere .glb o .obj.")


def _mesh_vertex_normals(vertices, faces):
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for a, b, c in faces:
        u = [vertices[b][i] - vertices[a][i] for i in range(3)]
        v = [vertices[c][i] - vertices[a][i] for i in range(3)]
        n = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
        for index in (a, b, c):
            for axis in range(3):
                normals[index][axis] += n[axis]
    return [tuple(x / length for x in n) if (length := math.hypot(*n)) > 1e-20
            else (0.0, 0.0, 1.0) for n in normals]


def _mesh_rli_replacements(data: bytes, target: MeshInfo, mesh: MeshInfo) -> dict[int, bytes]:
    """Rebuild primary and cooked instance lighting tables (Toolkit Rli.cpp)."""
    entries = _parse_pop_file_entries(data)
    expanded = list(dict.fromkeys(pair for f, uv in zip(mesh.faces, mesh.uv_indices) for pair in zip(f, uv)))
    updates = {}
    for entry in entries:
        if entry.data_type != struct.unpack("<I", b".gao")[0]:
            continue
        raw = data[entry.data_offset:entry.data_offset + entry.size]
        payload = raw[4:]
        if len(payload) < 16:
            raise ValueError("GAO troncato.")
        _, _, identity, name_len = struct.unpack_from("<4I", payload)
        if not identity & 0x4000:
            continue
        visual = 16 + name_len + 10 + 68 + (48 if identity & 0x80000 else 24)
        if visual + 8 > len(payload):
            raise ValueError("Blocco visual GAO troncato.")
        gro_key = struct.unpack_from("<I", payload, visual)[0]
        if gro_key != target.key:
            group = next((e for e in entries if e.key == gro_key and e.data_type != 1), None)
            if not group:
                continue
            group_raw = data[group.data_offset:group.data_offset + group.size]
            if struct.pack("<I", target.key) not in group_raw:
                continue
            # Retail StaticLOD: GRO header (8 B), count (1 B), six distance
            # bytes, then keys. Editor streams may retain one dummy byte.
            if group.data_type != 8 or len(group_raw) < 15:
                raise ValueError("Gruppo geometrico non riconosciuto: sostituzione annullata.")
            count = group_raw[8]
            key_offset = len(group_raw) - count * 4
            if not 1 <= count <= 6 or key_offset not in (15, 16):
                raise ValueError("Tabella StaticLOD non valida.")
            keys = struct.unpack_from("<" + "I" * count, group_raw, key_offset)
            if target.key not in keys:
                continue
            # LOD resources and skeleton links remain keyed to the same GEO.
            # A shared nonempty RLI table cannot be assigned to one LOD by
            # vertex count alone (two LODs can have the same count).
            for rli_marker in range(visual + 8, min(visual + 64, len(payload)-9)):
                if payload[rli_marker:rli_marker+2] != b"\xff\xff":
                    continue
                rli_count = struct.unpack_from("<I", payload, rli_marker+2)[0]
                rli_end = rli_marker + 6 + rli_count * 4
                if rli_end+4 <= len(payload) and struct.unpack_from("<I", payload, rli_end)[0] == 1:
                    if rli_count:
                        raise ValueError("StaticLOD con RLI condivisa non vuota: associazione dei colori ambigua.")
                    break
            continue
        marker = b"\xff\xff" + struct.pack("<I", len(target.vertices))
        pos = payload.find(marker, visual, min(visual + 69, len(payload)))
        if pos < 0:
            continue
        start = pos + 6
        end = start + len(target.vertices) * 4
        if end + 4 > len(payload) or struct.unpack_from("<I", payload, end)[0] != 1:
            raise ValueError("Tabella RLI primaria non riconosciuta.")
        colors = {tuple(round(x, 5) for x in v): payload[start+i*4:start+i*4+3] + b"\xfe"
                  for i, v in enumerate(target.vertices)}
        new_colors = [colors.get(tuple(round(x, 5) for x in v), b"\xff\xff\xff\xfe") for v in mesh.vertices]
        tail = payload[end:]
        for offset in range(0, min(80, len(tail)-15), 4):
            tag, size, count, stride = struct.unpack_from("<4I", tail, offset)
            if stride != 12 or not 0 < count <= 300000 or size != 8 + count * 12:
                continue
            old_end = offset + 16 + count * 12
            if old_end > len(tail):
                raise ValueError("Buffer RLI espanso troncato.")
            block = struct.pack("<4I", tag, 8 + len(expanded)*12, len(expanded), 12)
            block += b"".join(new_colors[vi] + struct.pack("<2f", -1, -1) for vi, _ in expanded)
            tail = tail[:offset] + block + tail[old_end:]
            break
        updates[entry.index] = (raw[:4] + payload[:pos] + b"\xff\xff" + struct.pack("<I", len(new_colors))
                                + b"".join(new_colors) + tail)
    return updates


def _build_static_mesh_replacement(data: bytes, target: MeshInfo, mesh: MeshInfo) -> bytes:
    """Compatibility entry point; now handles static and skinned retail GEO."""
    entry = _parse_pop_file_entries(data)[target.entry_index]
    if entry.key != target.key:
        raise ValueError("Mesh sorgente cambiata: riscansiona l'asset.")
    raw = data[entry.data_offset:entry.data_offset + entry.size]
    candidate = replace(mesh, normals=mesh.normals or _mesh_vertex_normals(mesh.vertices, mesh.faces))
    return jade_mesh.build_replacement(raw, target, candidate)


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
    """Build a standalone DDS from the compressed bytes in a POP texture entry."""
    if tex.texture_type not in (5, 6, 7):
        raise ValueError("Solo le texture POP DXT1, DXT3 o DXT5 possono essere scaricate come DDS.")
    payload = bytes(texture_data[tex.data_offset:tex.data_end])
    if tex.texture_type in (5, 6):
        bytes_per_block = 8 if tex.texture_type == 5 else 16
        try:
            mip_count = _infer_mip_count(tex.storage_width, tex.storage_height, len(payload), bytes_per_block)
        except ValueError:
            # Native POP type-5 entries can end with a four-byte container
            # trailer after the BC1 levels. It is not DDS image data, and its
            # value is not guaranteed to be zero. Accept it only when removing
            # exactly one DWORD produces a complete mip chain.
            if tex.texture_type != 5 or len(payload) <= 4:
                raise
            dds_payload = payload[:-4]
            mip_count = _infer_mip_count(
                tex.storage_width, tex.storage_height, len(dds_payload), bytes_per_block
            )
        else:
            dds_payload = payload
        return _build_dds(
            dds_payload, tex.storage_width, tex.storage_height,
            _build_dds_header(tex.storage_width, tex.storage_height, mip_count - 1, tex.texture_type),
        )
    if len(payload) == tex.storage_width * tex.storage_height * 4:
        return _build_tga_header(tex.storage_width, tex.storage_height, 32) + payload
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


def _infer_mip_count(width: int, height: int, payload_size: int, bytes_per_block: int) -> int:
    """Infer mip levels for a block-compressed POP payload."""
    total = 0
    w, h = width, height
    for mip_count in range(1, 17):
        total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bytes_per_block
        if total == payload_size:
            return mip_count
        if total > payload_size or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    raise ValueError("Il payload non corrisponde a una catena mipmap completa.")


def _full_mip_count(width: int, height: int) -> int:
    count = 1
    while width > 1 or height > 1:
        width, height = max(1, width // 2), max(1, height // 2)
        count += 1
    return count


def _dxt5_payload_for_converted_texture(path: Path, texture: TextureInfo) -> bytes:
    """Encode DXT5 for PAL8 textures, retaining their mip count when it is known."""
    payload_size = texture.data_end - texture.data_offset
    try:
        # PAL8 stores one byte per pixel at every mip level.
        total, w, h, mip_count = 0, texture.width, texture.height, 0
        while mip_count < 16:
            total += w * h
            mip_count += 1
            if total == payload_size:
                break
            if total > payload_size or (w == 1 and h == 1):
                raise ValueError
            w, h = max(1, w // 2), max(1, h // 2)
        else:
            raise ValueError
    except ValueError:
        mip_count = _full_mip_count(texture.width, texture.height)
    return _encode_dxt5(_image_rgba(path, texture.width, texture.height), mip_count)


def _compressed_dds_for_preview(payload: bytes, tex: TextureInfo) -> bytes:
    """Wrap a DXT1/DXT3 payload for Pillow without altering embedded bytes."""
    if tex.texture_type not in (5, 6):
        raise ValueError(f"Formato compresso non supportato: type {tex.texture_type}.")
    return _build_dds(payload, tex.width, tex.height, _build_dds_header(tex.width, tex.height, 0, tex.texture_type))

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


def _encode_dxt1_block(pixels: list[tuple[int, int, int, int]]) -> bytes:
    """Encode one BC1/DXT1 block, including DXT1 punch-through alpha."""
    transparent = [pixel[3] < 128 for pixel in pixels]
    opaque = [pixel for pixel, is_transparent in zip(pixels, transparent) if not is_transparent]
    if not opaque:
        return b"\x00\x00\x00\x00\xff\xff\xff\xff"

    colors = [(pixel[0], pixel[1], pixel[2]) for pixel in opaque]
    min_rgb = tuple(min(color[i] for color in colors) for i in range(3))
    max_rgb = tuple(max(color[i] for color in colors) for i in range(3))
    c0, c1 = _rgb565(max_rgb), _rgb565(min_rgb)
    has_transparency = any(transparent)
    if has_transparency:
        # BC1's three-colour mode (c0 <= c1) reserves index 3 for alpha 0.
        if c0 > c1:
            c0, c1 = c1, c0
    else:
        # Four-colour mode needs c0 > c1. Avoid accidentally selecting the
        # transparent BC1 mode for a flat, fully opaque block.
        if c0 <= c1:
            c0 = min(0xFFFF, c1 + 1)
            if c0 <= c1:
                c1 = max(0, c0 - 1)

    rgb0, rgb1 = _unpack565(c0), _unpack565(c1)
    if has_transparency:
        palette = [
            rgb0,
            rgb1,
            tuple((rgb0[i] + rgb1[i]) // 2 for i in range(3)),
        ]
    else:
        palette = [
            rgb0,
            rgb1,
            tuple((2 * rgb0[i] + rgb1[i]) // 3 for i in range(3)),
            tuple((rgb0[i] + 2 * rgb1[i]) // 3 for i in range(3)),
        ]

    indices = 0
    for i, pixel in enumerate(pixels):
        if transparent[i]:
            index = 3
        else:
            color = pixel[:3]
            index = min(
                range(len(palette)),
                key=lambda n: sum((color[channel] - palette[n][channel]) ** 2 for channel in range(3)),
            )
        indices |= index << (2 * i)
    return c0.to_bytes(2, "little") + c1.to_bytes(2, "little") + indices.to_bytes(4, "little")


def _encode_dxt1(image, mip_count: int = 1) -> bytes:
    """Pure-Python DXT1 encoder for POP type-5 textures."""
    from PIL import Image

    out = bytearray()
    for level, (width, height) in enumerate(_dxt5_mip_sizes(*image.size, mip_count)):
        level_image = image if level == 0 else image.resize((width, height), Image.Resampling.LANCZOS)
        rgba = level_image.load()
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                pixels = [
                    rgba[min(x, width - 1), min(y, height - 1)]
                    for y in range(by, by + 4)
                    for x in range(bx, bx + 4)
                ]
                out.extend(_encode_dxt1_block(pixels))
    return bytes(out)


def _dxt1_payload_from_file(path: Path, texture: TextureInfo) -> bytes:
    """Encode a replacement as POP DXT1 (type 5), as Jade Toolkit does.

    Jade's writer emits one DXT1 base level and clears the texture mip field.
    Keeping format 5 is essential: changing a game DXT1 texture to type 7
    changes the entry layout and can make the renderer read wrong bytes.
    """
    return _encode_dxt1(_image_rgba(path, texture.width, texture.height))


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


def _build_4bit_tga(width: int, height: int, packed: bytes) -> bytes:
    """Expand Jade's low-nibble-first 4-bit grayscale texture format."""
    pixel_count = width * height
    if len(packed) < (pixel_count + 1) // 2:
        raise ValueError("Dati 4-bit insufficienti per questa texture.")
    decoded = bytearray(pixel_count * 4)
    for pixel in range(pixel_count):
        nibble = (packed[pixel // 2] & 0x0F) if pixel % 2 == 0 else (packed[pixel // 2] >> 4)
        value = nibble * 17
        decoded[pixel * 4:pixel * 4 + 4] = bytes((value, value, value, 255))
    return _build_tga_header(width, height, 32) + bytes(decoded)

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
    """Decode POP's consecutive little-endian LZO blocks (up to 0x20000 each)."""
    compat = ROOT / "lzo_compat.dll"
    native = None
    if compat.is_file():
        try:
            lib = ctypes.CDLL(str(compat))
            native = lib.lzo_bridge_decompress
            native.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
                               ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_size_t)]
            native.restype = ctypes.c_int
        except (OSError, AttributeError):
            native = None
    pos = 0
    output = bytearray()
    while pos + 8 <= len(data):
        dec_size, enc_size = struct.unpack_from("<2I", data, pos)
        if dec_size == 0 or enc_size == 0:
            break  # physical padding or optional terminator
        pos += 8
        if enc_size > len(data) - pos:
            raise ValueError("Blocco LZO POP troncato.")
        block = data[pos:pos + enc_size]
        pos += enc_size
        if dec_size == enc_size:
            output.extend(block)
        elif native is not None:
            src = (ctypes.c_ubyte * len(block)).from_buffer_copy(block)
            dst = (ctypes.c_ubyte * dec_size)()
            out_size = ctypes.c_size_t(dec_size)
            rc = native(src, len(block), dst, ctypes.byref(out_size))
            if rc != 0 or out_size.value != dec_size:
                raise ValueError(f"Decompressione LZO POP fallita (rc={rc}, output={out_size.value}, atteso={dec_size}).")
            output.extend(bytes(dst[:out_size.value]))
        else:
            output.extend(_decompress_lzo_block(block, dec_size))
        # A short final block ends the stream; a full block may be followed by another.
        if dec_size < LZO_BLOCK_SIZE:
            break
    if not output:
        raise ValueError("Wrapper LZO POP vuoto o non valido.")
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
    # POP retail assets use FFEE, while later/prototype WOWs use FFFE.
    # The marker identifies the FileEntry stream, not the LZO codec; retain
    # the original bytes and accept either wrapper when recompressing.
    pop_markers = (b"\x99\xC0\xFF\xEE", b"\x99\xC0\xFF\xFE")
    if len(data) < 8 or data[4:8] not in pop_markers:
        raise ValueError("Il BIN non contiene un magic POP valido (0x99C0FFEE/0x99C0FFFE) a offset 4.")
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
        encoded = target.read_bytes()
        if len(encoded) < 8:
            raise RuntimeError("Il runtime LZO ha prodotto un wrapper troppo corto.")
        if decompress_pop_lzo(encoded) != data:
            raise RuntimeError("Verifica LZO fallita: il BIN ricompresso non restituisce i dati .DEC originali.")
        return encoded
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


@dataclass
class LegacyFolderEntry:
    index: int
    file: int
    child: int
    next: int
    prev: int
    parent: int
    name: str


def _legacy_folder_entries(path: Path) -> list[LegacyFolderEntry]:
    """Read the FolderEntry table used by bf_repacker_2018_05_23_1419."""
    with path.open("rb") as stream:
        header = stream.read(LEGACY_BF_HEADER_SIZE)
        if len(header) != LEGACY_BF_HEADER_SIZE:
            raise ValueError("File .bf troppo corto per l'header legacy.")
        magic, version, fcount, dcount, _unk2, _unk3, capacity, _unk4, _universe_key, _fcount2, _dcount2, _file_id_offset, _unk5, _unk6, _last = struct.unpack(
            "<4sIIIQQIIIIIIiII", header
        )
        if magic != b"BIG\0" or version not in (37, 38):
            raise ValueError("Il BF selezionato non usa il layout legacy v37/v38.")
        folder_base = LEGACY_BF_HEADER_SIZE + capacity * LEGACY_BF_FILE_TABLE_ENTRY_SIZE + capacity * LEGACY_BF_FILE_ENTRY_SIZE
        stream.seek(folder_base)
        folders: list[LegacyFolderEntry] = []
        for index in range(min(capacity, 2_000_000)):
            raw = stream.read(84)
            if len(raw) != 84 or raw == b"\0" * 84:
                break
            file_id, child, next_id, prev, parent = struct.unpack_from("<Iiiii", raw, 0)
            name = raw[20:84].split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
            folders.append(LegacyFolderEntry(index, file_id, child, next_id, prev, parent, name))
        if not folders:
            raise ValueError("FolderEntry table legacy non trovata.")
        return folders


def _legacy_folder_path(folders: list[LegacyFolderEntry], folder_index: int) -> Path:
    if folder_index < 0 or folder_index >= len(folders):
        raise ValueError(f"FolderEntry index non valido: {folder_index}")
    parts: list[str] = []
    seen: set[int] = set()
    current = folder_index
    while current != -1:
        if current in seen:
            raise ValueError("Ciclo nella gerarchia FolderEntry legacy.")
        seen.add(current)
        folder = folders[current]
        parts.append(folder.name or f"Folder_{current}")
        current = folder.parent
    return Path(*reversed(parts))


def _legacy_asset_path(root_dir: Path, folders: list[LegacyFolderEntry], entry: BigFileEntry) -> Path:
    return root_dir / _legacy_folder_path(folders, entry.parent) / entry.name


def extract_legacy_bf_as_root(source: Path, target_dir: Path) -> int:
    """Extract a legacy BF into the exact ROOT/... hierarchy of the old repacker."""
    info = read_bigfile(source)
    if info.version not in (37, 38):
        raise ValueError("Export all assets con ROOT è disponibile per BF legacy v37/v38.")
    folders = _legacy_folder_entries(source)
    if (folders[0].name or "ROOT").casefold() != "root":
        raise ValueError(f"La FolderEntry 0 del BF è '{folders[0].name}', atteso ROOT.")
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_bf = source.read_bytes()
    for entry in info.entries:
        output = _legacy_asset_path(target_dir, folders, entry)
        output.parent.mkdir(parents=True, exist_ok=True)
        start = entry.position + entry.data_header_size
        length = entry.size & 0x7FFFFFFF
        output.write_bytes(raw_bf[start:start + length])
    return len(info.entries)


def _legacy_file_data_length(data: bytes, folder_index: int, name: str) -> int:
    """Match the old repacker's ParseFileData calculation for size.grs."""
    magic = 0xEEFFC099
    if len(data) < 16:
        return len(data)
    dec_size, enc_size = struct.unpack_from("<2I", data, 0)
    file_magic = struct.unpack_from("<I", data, 13)[0]
    file_magic2 = struct.unpack_from("<I", data, 14)[0]
    if dec_size != enc_size and (file_magic == magic or file_magic2 == magic):
        pos = 0
        while pos + 8 <= len(data):
            size_dec, size_enc = struct.unpack_from("<2I", data, pos)
            pos += 8 + size_enc
            if size_dec != LZO_BLOCK_SIZE:
                break
        return min(len(data), pos + 4)
    if dec_size == enc_size and struct.unpack_from("<I", data, 12)[0] == magic:
        return min(len(data), struct.unpack_from("<I", data, 4)[0] + 12)
    last = len(data) - 1
    while last > 0 and data[last] == 0:
        last -= 1
    if folder_index == 1:
        return last + 7
    if folder_index == 3:
        return last + 4
    if folder_index in (0, 2, 4):
        return last + 1
    return len(data)


def _update_legacy_size_grs_payload(info: BigFileInfo, payloads: dict[int, bytes], changed_indices: set[int]) -> None:
    """Refresh the logical LZO stream lengths stored by POP in size.grs."""
    if not changed_indices:
        return
    size_entry = next((entry for entry in info.entries if entry.name.casefold() == "size.grs"), None)
    if size_entry is None or size_entry.index not in payloads:
        return
    by_key = {entry.key: entry for entry in info.entries}
    data = bytearray(payloads[size_entry.index])
    changed = False
    for offset in range(0, len(data) - 7, 8):
        key, old_length = struct.unpack_from("<2I", data, offset)
        if key == 0:
            break
        entry = by_key.get(key)
        if entry is None or entry.index not in changed_indices or entry.index == size_entry.index:
            continue
        new_length = _legacy_file_data_length(payloads[entry.index], entry.parent, entry.name)
        if new_length != old_length:
            struct.pack_into("<I", data, offset + 4, new_length)
            changed = True
    if changed:
        payloads[size_entry.index] = bytes(data)

def build_legacy_bf_from_folder(template: Path, root_dir: Path, output: Path) -> int:
    """Rebuild a legacy BF from ROOT using the opened BF as its metadata template."""
    info = read_bigfile(template)
    if info.version not in (37, 38):
        raise ValueError("Build .BF from folder è disponibile per BF legacy v37/v38.")
    folders = _legacy_folder_entries(template)
    if not root_dir.is_dir() or root_dir.name.casefold() != "root":
        raise ValueError("Seleziona esattamente la cartella ROOT dell'estrazione BF.")
    original = template.read_bytes()
    payloads: dict[int, bytes] = {}
    missing: list[str] = []
    for entry in info.entries:
        path = _legacy_asset_path(root_dir.parent, folders, entry)
        if not path.is_file():
            missing.append(str(path.relative_to(root_dir.parent)))
            continue
        payloads[entry.index] = path.read_bytes()
    if missing:
        preview = ", ".join(missing[:8])
        suffix = "..." if len(missing) > 8 else ""
        raise ValueError(f"La cartella ROOT è incompleta: mancano {len(missing)} file ({preview}{suffix}).")

    _update_legacy_size_grs_payload(info, payloads, set(payloads))

    ordered = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered[0].position
    rebuilt = bytearray(original[:prefix_end])
    original_cursor = prefix_end
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.size_fat * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    new_positions: dict[int, int] = {}
    for entry in ordered:
        payload = payloads[entry.index]
        rebuilt.extend(original[original_cursor:entry.position])
        new_positions[entry.index] = len(rebuilt)
        struct.pack_into("<I", rebuilt, file_id_base + entry.index * LEGACY_BF_FILE_TABLE_ENTRY_SIZE, new_positions[entry.index])
        if len(payload) != (entry.size & 0x7FFFFFFF):
            struct.pack_into("<I", rebuilt, file_entry_base + entry.index * LEGACY_BF_FILE_ENTRY_SIZE, len(payload))
        rebuilt.extend(struct.pack("<I", len(payload)))
        rebuilt.extend(payload)
        original_cursor = entry.position + 4 + (entry.size & 0x7FFFFFFF)
    rebuilt.extend(original[original_cursor:])

    size_grs_index = next((e.index for e in info.entries if e.name == "size.grs"), None)
    if size_grs_index is not None:
        size_payload = bytearray(payloads[size_grs_index])
        by_key = {entry.key: entry for entry in info.entries}
        for offset in range(0, len(size_payload) - 7, 8):
            file_id, _old_length = struct.unpack_from("<2I", size_payload, offset)
            if file_id == 0:
                break
            entry = by_key.get(file_id)
            if entry is not None and entry.index != size_grs_index:
                struct.pack_into("<I", size_payload, offset + 4, _legacy_file_data_length(payloads[entry.index], entry.parent, entry.name))
        if len(size_payload) != len(payloads[size_grs_index]):
            raise ValueError("size.grs ha cambiato dimensione; usa una size.grs della stessa dimensione dell'originale.")
        start = new_positions[size_grs_index] + 4
        rebuilt[start:start + len(size_payload)] = size_payload

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rebuilt)
    return len(payloads)



def _read_big_header(stream) -> BigFileInfo:
    header = stream.read(44)
    if len(header) != 44:
        raise ValueError("File .bf troppo corto per contenere l'header Jade.")
    magic, version, max_file, max_dir, _max_key, _root, _free_file, _free_dir, size_fat, num_fat, universe_key = struct.unpack("<4s10I", header)
    if magic not in (b"BIG\0", b"BUG\0"):
        raise ValueError(f"Header BIG non riconosciuto: {magic!r}")
    file_size = Path(stream.name).stat().st_size
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
        ext_table = stream.read(fat_max_file * LEGACY_BF_FILE_ENTRY_SIZE)
        if len(file_table) != fat_max_file * 8 or len(ext_table) != fat_max_file * LEGACY_BF_FILE_ENTRY_SIZE:
            raise ValueError(f"FAT #{fat_index} troncata.")
        for i in range(fat_max_file):
            position, key = struct.unpack_from("<2I", file_table, i * 8)
            if key == 0xFFFFFFFF:
                continue
            ext = ext_table[i * LEGACY_BF_FILE_ENTRY_SIZE:(i + 1) * LEGACY_BF_FILE_ENTRY_SIZE]
            physical_size = struct.unpack_from("<I", ext, 0)[0] & 0x7FFFFFFF
            parent = struct.unpack_from("<I", ext, 12)[0]
            raw_name = ext[20:84].split(b"\0", 1)[0]
            name = raw_name.decode("ascii", errors="replace").strip() or f"<file_{first_index + i:06d}>"
            data_header_size = 0
            compressed = False
            compression = "none"
            if version in (37, 38) and position + 4 <= file_size:
                stream.seek(position)
                if struct.unpack("<I", stream.read(4))[0] == physical_size:
                    data_header_size = 4
                    stream.seek(position + 4)
                    compressed = _looks_like_pop_lzo(stream.read(min(32, physical_size)))
                    compression = "POP-LZO" if compressed else "none"
            entries.append(BigFileEntry(first_index + i, position, key, physical_size, name, parent,
                                        fat_index, first_index, compressed, data_header_size, compression))
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
    # POP v37/v38 uses the regular FAT header; size_of_fat can exceed file_count.
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
    for entry in info.entries:
        if entry.index == selected.index:
            payloads[entry.index] = selected_payload
        else:
            start = entry.position + entry.data_header_size
            length = entry.size & 0x7FFFFFFF
            payloads[entry.index] = original[start:start + length]
    _update_legacy_size_grs_payload(info, payloads, {selected.index})
    # File-table order and physical payload order are not guaranteed to match.
    # Preserve the original physical ordering while updating each indexed
    # FileIdOffset to its new position.
    ordered_entries = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered_entries[0].position
    prefix = bytearray(original[:prefix_end])
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.size_fat * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    cursor = prefix_end
    original_cursor = prefix_end
    for entry in ordered_entries:
        payload = payloads[entry.index]
        # Keep every unindexed byte between legacy entries. These gaps are
        # part of the container layout and must survive an unchanged rebuild.
        original_gap = original[original_cursor:entry.position]
        cursor += len(original_gap)
        struct.pack_into("<I", prefix, file_id_base + entry.index * 8, cursor)
        size_value = len(payload)
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

    _update_legacy_size_grs_payload(info, payloads, set(replacements))

    ordered_entries = sorted(info.entries, key=lambda entry: entry.position)
    prefix_end = ordered_entries[0].position
    prefix = bytearray(original[:prefix_end])
    file_id_base = LEGACY_BF_HEADER_SIZE
    file_entry_base = file_id_base + info.size_fat * LEGACY_BF_FILE_TABLE_ENTRY_SIZE
    cursor = prefix_end
    original_cursor = prefix_end
    for entry in ordered_entries:
        payload = payloads[entry.index]
        original_gap = original[original_cursor:entry.position]
        cursor += len(original_gap)
        struct.pack_into("<I", prefix, file_id_base + entry.index * 8, cursor)
        if len(payload) != (entry.size & 0x7FFFFFFF):
            struct.pack_into("<I", prefix, file_entry_base + entry.index * LEGACY_BF_FILE_ENTRY_SIZE, len(payload))
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
                                source_type: int, target_type: int,
                                width: int, height: int) -> tuple[bytes, int]:
    """Patch matching POP texture entries, rebuilding their header if needed."""
    data = bytearray(asset_data)
    matches = 0
    try:
        textures = _scan_pop_textures(data)
    except (ValueError, struct.error):
        # A BF also contains non-POP resources. They cannot hold a POP
        # texture record, so skip them while looking for duplicate texture keys.
        return asset_data, 0
    # Work backwards: a texture replacement can change its FileEntry size.
    for tex in reversed(textures):
        if tex.key != texture_key:
            continue
        if tex.width != width or tex.height != height:
            continue
        # The selected asset is already converted when saving a BF. Count it
        # without touching it, so it remains in the replacement set.
        if (tex.texture_type == target_type
                and bytes(data[tex.data_offset:tex.data_end]) == replacement_payload):
            matches += 1
            continue
        if tex.texture_type != source_type:
            continue
        # tex.offset is the FileEntry data start. The Jade texture header is
        # 56 B long here; types 1 and 5 have an additional four-byte prefix.
        header_end = tex.offset + 56
        if header_end > tex.data_end:
            continue
        entry = bytearray(data[tex.offset:header_end])
        struct.pack_into("<I", entry, 40, target_type)
        # Match Jade Toolkit: replacements in DXT/BGRA formats carry just the
        # base level and reset Jade's mip-count field.
        if target_type in (0, 5, 6, 7):
            struct.pack_into("<I", entry, 52, 0)
        if target_type in (1, 5):
            # A DXT1 replacement must retain the native prefix (unlike a
            # PAL8 -> DXT5 conversion, which intentionally removes it).
            prefix = (bytes(data[header_end:tex.data_offset])
                      if tex.texture_type == target_type else b"\x00\x00\x00\x00")
            if len(prefix) != 4:
                continue
        else:
            prefix = b""
        replacement_entry = bytes(entry) + prefix + replacement_payload
        struct.pack_into("<I", data, tex.offset - 12, len(replacement_entry))
        data[tex.offset:tex.data_end] = replacement_entry
        matches += 1
    return bytes(data), matches


def _collect_texture_key_replacements(path: Path, selected: BigFileEntry,
                                      texture_key: int, replacement_payload: bytes,
                                      source_type: int, target_type: int,
                                      width: int, height: int,
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
            decoded, texture_key, replacement_payload, source_type, target_type, width, height
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


def _palette_payload_from_image(image, texture: TextureInfo, palette: bytes,
                                original_payload: bytes) -> bytes:
    """Encode the base PAL8 level and preserve native mipmap/padding bytes."""
    if len(palette) < 1024:
        raise ValueError("Palette POP incompleta: servono 256 colori RGBA.")
    palette_rgba = [tuple(palette[i:i + 4]) for i in range(0, 1024, 4)]
    pixels = image.get_flattened_data()
    indices = bytearray(texture.width * texture.height)
    cache: dict[tuple[int, int, int, int], int] = {}
    for i, pixel in enumerate(pixels):
        if pixel not in cache:
            cache[pixel] = min(range(256), key=lambda n: sum((pixel[c] - palette_rgba[n][c]) ** 2 for c in range(4)))
        indices[i] = cache[pixel]
    expected = texture.data_end - texture.data_offset
    if expected < len(indices) or len(original_payload) != expected:
        raise ValueError(f"Payload palette non compatibile: originale {expected:,} B, base {len(indices):,} B.")
    return bytes(indices) + original_payload[len(indices):]


def _palette_payload_from_file(path: Path, texture: TextureInfo, palette: bytes,
                               original_payload: bytes) -> bytes:
    return _palette_payload_from_image(
        _image_rgba(path, texture.width, texture.height), texture, palette, original_payload
    )


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
        self.mesh_patches: dict[int, dict[int, tuple[int, bytes, bytes]]] = {}

    @property
    def title(self) -> str:
        return self.path.name if self.path else "Nessun file aperto"

    def open_bf(self, path: Path) -> None:
        info = read_bigfile(path)
        self.mesh_patches.clear()
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
        self.mesh_patches.clear()
        self.path = path
        self.kind = "bin"
        self.info = None
        self.raw_bin = raw
        self.decoded_bin = decoded
        self.direct_compressed = compressed
        self.assets = [Asset(path.name, 0, 0, 0, len(decoded), compressed)]
        self.modified = False

    def open_dec(self, path: Path) -> None:
        data = path.read_bytes()
        self.mesh_patches.clear()
        self.path = path
        self.kind = "dec"
        self.info = None
        self.raw_bin = data
        self.decoded_bin = data
        self.direct_compressed = False
        self.assets = [Asset(path.name, 0, 0, 0, len(data), False)]
        self.modified = False

    def read_asset(self, asset: Asset) -> bytes:
        if self.kind in ("bin", "dec"):
            data = self.decoded_bin or b""
        else:
            entry = next(e for e in self.info.entries if e.index == asset.index)
            data = read_bigfile_entry(self.path, entry)
        return self.apply_mesh_patches(asset.index, data)

    def apply_mesh_patches(self, asset_index: int, data: bytes) -> bytes:
        patches = self.mesh_patches.get(asset_index, {})
        if not patches:
            return data
        entries = _parse_pop_file_entries(data)
        for index, (key, original, replacement) in sorted(patches.items(), reverse=True):
            if index >= len(entries) or entries[index].key != key:
                raise ValueError("La struttura dell'asset è cambiata: impossibile applicare le mesh.")
            entry = entries[index]
            current = data[entry.data_offset:entry.data_offset + entry.size]
            if current not in (original, replacement):
                raise ValueError(f"Conflitto tra editor sulla mesh 0x{key:08X}; riscansiona l'asset.")
            data = (data[:entry.offset] + struct.pack("<III", len(replacement), entry.magic, key)
                    + replacement + data[entry.data_offset + entry.size:])
        _parse_pop_file_entries(data)
        return data

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
        self._mesh_material_textures_by_mesh: dict[int, dict[int, int]] = {}
        self._mesh_material_colors_by_mesh: dict[int, dict[int, tuple[float, float, float, float]]] = {}
        self._swap_mesh: MeshInfo | None = None
        self._swap_mesh_textures: dict[int, object] = {}
        self._swap_mesh_material_textures: dict[int, int] = {}
        self._swap_mesh_material_colors: dict[int, tuple[float, float, float, float]] = {}
        self._swap_mesh_path: Path | None = None
        self._mesh_texture_photos: list[object] = []
        self._mesh_external_texture_cache: dict[int, object] = {}
        self._mesh_external_texture_misses: set[int] = set()
        self._mesh_cache_project: Path | None = None
        self._mesh_pending_texture_keys: set[int] = set()
        self._mesh_texture_search_queue: queue.Queue = queue.Queue()
        self._mesh_texture_search_generation = 0

        self._material_data = bytearray()
        self._material_infos: list[MaterialInfo] = []
        self._material_source_asset: Asset | None = None
        self._material_textures: dict[int, object] = {}
        self._material_texture_sources: dict[int, Asset] = {}
        self._material_inventory_sources: dict[int, Asset] = {}
        self._material_external_texture_cache: dict[int, object] = {}
        self._material_texture_decode_misses: set[int] = set()
        self._material_inventory_complete = False
        self._material_cache_project: Path | None = None
        self._material_index_queue: queue.Queue = queue.Queue()
        self._material_index_generation = 0

        self._material_texture_photos: list[object] = []
        self._material_diffuse_path = tk.StringVar()
        self._material_secondary_path = tk.StringVar()
        self._material_normal_path = tk.StringVar()
        self._material_metallic = tk.DoubleVar(value=0.0)
        self._material_alpha = tk.DoubleVar(value=1.0)
        self._material_projection = tk.DoubleVar(value=1.0)
        self._material_shape = tk.StringVar(value="sphere")
        self._material_dirty = False
        self._material_preview_photo = None
        self._bf_repack_info = None
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
        style.configure("Apply.TButton", background="#7a3a3a", foreground=palette["fg"],
                        bordercolor="#a05a5a", padding=(10, 7))
        style.map("Apply.TButton", background=[("active", "#994848"), ("disabled", "#7a3a3a")],
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
        file_menu.add_command(label="Import .DEC...", command=self.import_dec)
        file_menu.add_separator()
        file_menu.add_command(label="Extract selected asset...", command=self.extract_selected)
        file_menu.add_command(label="Export all assets", command=self.extract_all_assets)
        file_menu.add_command(label="Save edited .BIN as...", command=self.save_bin)
        file_menu.add_command(label="Save edited .DEC as...", command=self.save_dec)
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
        help_menu.add_command(label="About PoP BF Lab", command=self.about)
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
        ttk.Button(toolbar, text="Import .DEC", command=self.import_dec).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Salva .DEC modificato", command=self.save_dec).pack(side="left", padx=6)
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
        self.bf_repack_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.asset_tab, text="Asset Browser")
        self.tabs.add(self.bf_repack_tab, text="BF Repack")
        self.tabs.add(self.ova_tab, text="OVA Variables")
        self.tabs.add(self.level_tab, text="Level Editor")
        self.tabs.add(self.mesh_tab, text="Mesh Editor")
        self.tabs.add(self.material_tab, text="Material Editor")
        self.tabs.add(self.texture_tab, text="Texture Editor")
        self._build_asset_tab()
        self._build_bf_repack_tab()
        self._build_ova_tab()
        self._build_level_tab()
        self._build_mesh_tab()
        self._build_material_tab()
        self._build_texture_tab()

        self.status = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", pady=(8, 0))

    def _build_bf_repack_tab(self) -> None:
        ttk.Label(self.bf_repack_tab, text="BF Repack", font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        actions = ttk.Frame(self.bf_repack_tab)
        actions.pack(fill="x")
        ttk.Button(actions, text="Export all assets → ROOT", command=self.extract_all_assets).pack(side="left")
        ttk.Button(actions, text="Build .BF from a folder", command=self.build_bf_from_folder).pack(side="left", padx=8)
        ttk.Button(actions, text="Decompress all .BIN", command=self.decompress_all_bins).pack(side="left", padx=8)
        ttk.Button(actions, text="Compress all .DEC", command=self.compress_all_dec).pack(side="left", padx=8)
        box = ttk.LabelFrame(self.bf_repack_tab, text="Cartella estratta", padding=10)
        box.pack(fill="x", pady=(18, 0))
        ttk.Label(box, text="Build richiede la cartella ROOT prodotta da Export all assets e il BF originale aperto come template.").pack(anchor="w")
        ttk.Label(box, text="Esempio: ROOT\Engine Datas\...  /  ROOT\Bin\size.grs").pack(anchor="w", pady=(5, 0))

        info_box = ttk.LabelFrame(self.bf_repack_tab, text="BF Information", padding=8)
        info_box.pack(fill="both", expand=True, pady=(12, 0))
        self.bf_info_text = tk.Text(info_box, height=22, wrap="none", state="disabled", font=("Consolas", 9), relief="flat", borderwidth=0)
        self.bf_info_text.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(info_box, orient="vertical", command=self.bf_info_text.yview).pack(side="right", fill="y")
        self._set_bf_repack_info("Nessun BF aperto.")

    def _set_bf_repack_info(self, text: str) -> None:
        if hasattr(self, "bf_info_text"):
            self.bf_info_text.configure(state="normal")
            self.bf_info_text.delete("1.0", "end")
            self.bf_info_text.insert("1.0", text)
            self.bf_info_text.configure(state="disabled")

    def _refresh_bf_repack_info(self) -> None:
        path = self.project.path if getattr(self.project, "kind", None) == "bf" else None
        if path is None:
            self._set_bf_repack_info("Nessun BF aperto.")
            return
        try:
            raw = path.read_bytes()
            if len(raw) < 68:
                raise ValueError("File .bf troppo corto per l'header legacy.")
            vals = struct.unpack_from("<4sIIIQQIIIIIIiII", raw, 0)
            magic, version, fcount, dcount, unk2, unk3, capacity, unk4, main_id, fcount2, dcount2, file_id_offset, unk5, unk6, last = vals
            if magic != b"BIG\0" or version not in (37, 38):
                raise ValueError("Disponibile per BF legacy v37/v38.")
            fp_entry=68; fp_data=fcount*8; fp_total=capacity*8
            fe_entry=fp_entry+fp_total; fe_data=fcount*84; fe_total=capacity*84
            fo_entry=fe_entry+fe_total; fo_total=capacity*84
            folder_count=0; pos=fo_entry
            while pos+84<=len(raw) and struct.unpack_from("<i",raw,pos+4)[0]!=0:
                folder_count+=1; pos+=84
            fo_data=folder_count*84; unk_off=fo_entry+fo_total-136
            unk=struct.unpack_from("<i",raw,unk_off)[0] if 0<=unk_off<=len(raw)-4 else 0
            hpos=fo_entry+fo_total-4
            while hpos+4<=len(raw) and struct.unpack_from("<i",raw,hpos)[0]==0: hpos+=4
            htotal=struct.unpack_from("<i",raw,hpos)[0] if hpos+4<=len(raw) else 0
            size_i=next((i for i in range(fcount) if raw[fe_entry+i*84+20:fe_entry+i*84+84].split(b"\0",1)[0].decode("ascii",errors="replace").strip()=="size.grs"),None)
            se=sd=st=-1; sc=0
            if size_i is not None:
                se=struct.unpack_from("<I",raw,fp_entry+size_i*8)[0]+4; st=struct.unpack_from("<I",raw,fe_entry+size_i*84)[0]
                q=se
                while q+8<=len(raw) and struct.unpack_from("<I",raw,q)[0]!=0: sc+=1; q+=8
                sd=sc*8
            hx=lambda v:f"0x{v:X}"
            lines=["BF Header Info",f"[00] magic: {magic.decode('ascii',errors='replace').rstrip(chr(0))}",f"[04] unk1: {version}",f"[08] fcount: {fcount}",f"[12] dcount: {dcount}",f"[16] unk2: {unk2}",f"[24] unk3: {unk3}",f"[32] capacity: {capacity}",f"[36] unk4: {unk4}",f"[40] main_id: {hx(main_id)}",f"[44] fcount_2: {fcount2}",f"[48] dcount_2: {dcount2}",f"[52] fileIdTableOffset: {file_id_offset}",f"[56] unk5: {unk5}",f"[60] unk6: {unk6}",f"[64] last: {last}","header length: 68",f"BF header total length: {htotal}","","Field Pos Table Info",f"table entry: {fp_entry}",f"data length: {fp_data}",f"total length: {fp_total}","","File Entry Table Info",f"table entry: {fe_entry}",f"data length: {fe_data}",f"total length: {fe_total}","","Folder Entry Table Info",f"[{unk_off}] unk: {unk}",f"table entry: {fo_entry}",f"data length: {fo_data}",f"total length: {fo_total}","","Size.grs Table Info",f"table entry: {se}",f"data length: {sd}",f"total length: {st}",f"files count: {sc}"]
            self._set_bf_repack_info("\n".join(lines))
        except Exception as exc:
            self._set_bf_repack_info(f"Informazioni BF non disponibili.\n\n{exc}")
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
        ttk.Button(top, text="Import GLB / OBJ...", command=self.import_swap_mesh).pack(side="right", padx=6)
        ttk.Button(top, text="Apply mesh changes", command=self.apply_mesh_changes, style="Apply.TButton").pack(side="right", padx=6)
        self.mesh_source_label = ttk.Label(self.mesh_tab, text="Nessun mesh analizzato")
        self.mesh_source_label.pack(anchor="w", pady=(4, 8))

        options = ttk.Frame(self.mesh_tab)
        options.pack(fill="x", pady=(0, 6))
        self._mesh_fit_target = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Adatta dimensione e centro alla mesh originale", variable=self._mesh_fit_target).pack(side="left")
        ttk.Label(options, text="Assi OBJ (prima di importare):").pack(side="left", padx=(16, 4))
        self._mesh_obj_axes = tk.StringVar(value="Auto")
        ttk.Combobox(options, textvariable=self._mesh_obj_axes, values=("Auto", "Jade Z-up", "glTF Y-up"), state="readonly", width=13).pack(side="left")
        split = ttk.Panedwindow(self.mesh_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=1)
        split.add(right, weight=4)

        ttk.Label(left, text="Mesh nel file selezionato").pack(anchor="w")
        mesh_tree_frame = ttk.Frame(left)
        mesh_tree_frame.pack(fill="both", expand=True, pady=(5, 0))
        self.mesh_tree = ttk.Treeview(mesh_tree_frame, columns=("name", "verts", "faces", "key", "kind"), show="headings")
        for c, t, w in (("name", "Mesh", 190), ("verts", "Vertices", 80), ("faces", "Faces", 80), ("key", "Mesh ID", 105), ("kind", "Tipo", 120)):
            self.mesh_tree.heading(c, text=t)
            self.mesh_tree.column(c, width=w, anchor="w")
        self.mesh_tree.pack(side="left", fill="both", expand=True)
        mesh_scrollbar = ttk.Scrollbar(mesh_tree_frame, orient="vertical", command=self.mesh_tree.yview)
        mesh_scrollbar.pack(side="right", fill="y")
        self.mesh_tree.configure(yscrollcommand=mesh_scrollbar.set)
        self.mesh_tree.bind("<<TreeviewSelect>>", self.on_mesh_selected)

        preview_split = ttk.Panedwindow(right, orient="vertical")
        preview_split.pack(fill="both", expand=True)
        preview_box = ttk.LabelFrame(preview_split, text="Mesh 3D selezionato", padding=6)
        replacement_box = ttk.LabelFrame(preview_split, text="Mesh importato per la sostituzione", padding=6)
        preview_split.add(preview_box, weight=1)
        preview_split.add(replacement_box, weight=1)
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
            self.swap_mesh_canvas = tk.Label(replacement_box, text="OpenGL non disponibile", anchor="center")
            self.swap_mesh_canvas.pack(fill="both", expand=True)
        self.swap_mesh_info = ttk.Label(replacement_box, text="Importa un GLB o OBJ. Per i character vengono trasferiti automaticamente rig e pesi BF.", justify="left", wraplength=750)
        self.swap_mesh_info.pack(anchor="w", pady=(6, 0))
        replacement_box.bind("<Configure>", lambda e: self.swap_mesh_info.configure(wraplength=max(250, e.width - 24)))

        info_box = ttk.LabelFrame(right, text="Mesh info", padding=8)
        info_box.pack(fill="x", pady=(8, 0))
        self.mesh_info = ttk.Label(info_box, text="Seleziona un mesh per visualizzarlo.", justify="left")
        self.mesh_info.pack(anchor="w")

    def _build_material_tab(self) -> None:
        top = ttk.Frame(self.material_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Material Editor", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_materials).pack(side="right")
        ttk.Button(top, text="Apply material changes", command=self.apply_material_changes, style="Apply.TButton").pack(side="right", padx=6)
        self.material_source_label = ttk.Label(self.material_tab, text="Nessun materiale analizzato")
        self.material_source_label.pack(anchor="w", pady=(4, 8))

        split = ttk.Panedwindow(self.material_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=2)
        split.add(right, weight=5)

        ttk.Label(left, text="Materiali rilevati").pack(anchor="w")
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
            self.material_canvas = tk.Label(preview_box, text="OpenGL non disponibile", anchor="center")
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
        self.material_info = ttk.Label(editor, text="Seleziona un materiale per modificarlo.", justify="left")
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
            messagebox.showinfo("Material Editor", "Seleziona prima un asset .wow/.bin/.gao nel browser.")
            return
        try:
            self._material_index_generation += 1
            generation = self._material_index_generation
            self.status.set("Material Editor: lettura materiali e texture locali...")
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
            missing_note = f" - {unresolved} key non trovate" if unresolved else ""
            indexing_note = " - indice BF in background" if self.project.kind == "bf" and not self._material_inventory_complete else ""
            self.material_source_label.config(text=(
                f"{asset.name} - {len(self._material_infos)} materiali, "
                f"{len(images)}/{len(wanted_keys)} texture preview da {len(known_texture_keys)} texture note"
                f"{source_note}{missing_note}{indexing_note}"
            ))
            self.tabs.select(self.material_tab)
            if self._material_infos:
                self.material_tree.selection_set("mat_0")
                self.material_tree.focus("mat_0")
                self.on_material_selected()
            self.status.set("Ready")
            self._log(
                f"OK    Material scan: {asset.name} -> {len(self._material_infos)} materiali, "
                f"{len(images)} texture risolte subito"
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
        action = "indicizzazione texture BF" if full_index else f"caricamento di {len(wanted)} texture"
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
        missing_note = f" - {len(unresolved_keys)} key non trovate" if unresolved_keys else ""
        if self._material_source_asset is not None:
            self.material_source_label.config(text=(
                f"{self._material_source_asset.name} - {len(self._material_infos)} materiali, "
                f"{len(self._material_textures)}/{len(wanted_keys)} texture preview da "
                f"{len(known_keys)} texture BF{source_note}{missing_note}"
            ))
        if error:
            self._log(f"WARN  Indice texture Material Editor: {error}")

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
            f"OK    Indice texture Material Editor: {len(known_keys)} texture, "
            f"{len(found_images)} preview aggiunte"
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
            messagebox.showinfo("Texture Editor", "Seleziona prima un materiale.")
            return
        key_by_slot = {"diffuse": info.texture_key, "secondary": info.secondary_key, "normal": info.normal_key}
        labels = {"diffuse": "texture diffuse", "secondary": "texture secondaria", "normal": "normal map"}
        key = key_by_slot.get(slot)
        if key is None:
            messagebox.showinfo("Texture Editor", f"Questo materiale non ha una {labels.get(slot, 'texture')}.")
            return
        texture_asset = self._material_texture_sources.get(key)
        if texture_asset is None:
            messagebox.showwarning("Texture Editor", f"Texture 0x{key:08X} non trovata nell’archivio BF.")
            return
        previous = self.asset_tree.selection()
        try:
            target_iid = next((iid for iid, candidate in self._asset_map.items() if candidate.index == texture_asset.index), None)
            if target_iid is None:
                raise RuntimeError("Asset texture non disponibile nell’elenco filtrato.")
            self.asset_tree.selection_set(target_iid)
            self.scan_textures()
        finally:
            if previous:
                self.asset_tree.selection_set(previous)
        match = next((i for i, tex in enumerate(self._texture_infos) if tex.key == key), None)
        if match is None:
            messagebox.showwarning("Texture Editor", f"Texture 0x{key:08X} non trovata nell’entry selezionata.")
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
            messagebox.showinfo("Material Swap", "Scansiona e seleziona prima un materiale.")
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
        # Widgets packed on the right are displayed in reverse packing order.
        # Pack from right to left: Scan, Dump, Import, then Apply on the far left.
        ttk.Button(top, text="Scan selected .wow / asset", command=self.scan_textures).pack(side="right")
        ttk.Button(top, text="Dump texture", command=self.dump_texture).pack(side="right", padx=6)
        ttk.Button(top, text="Import replacement image...", command=self.import_texture_replacement).pack(side="right", padx=6)
        self.texture_apply_btn = ttk.Button(top, text="Apply texture changes", command=self.apply_texture_replacement,
                                            style="Apply.TButton", state="disabled")
        self.texture_apply_btn.pack(side="right", padx=6)
        self.texture_source_label = ttk.Label(self.texture_tab, text="Nessuna texture analizzata")
        self.texture_source_label.pack(anchor="w", pady=(4, 8))
        split = ttk.Panedwindow(self.texture_tab, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=2)
        split.add(right, weight=3)
        ttk.Label(left, text="Texture nel file selezionato").pack(anchor="w")
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
        original_box = ttk.LabelFrame(right, text="Texture selezionata", padding=8)
        original_box.pack(fill="both", expand=True)
        self.texture_preview = tk.Label(original_box, text="Seleziona una texture", anchor="center", justify="center", bg=self._dark["field"], fg=self._dark["muted"])
        self.texture_preview.pack(fill="both", expand=True)
        replacement_box = ttk.LabelFrame(right, text="Texture da importare", padding=8)
        replacement_box.pack(fill="both", expand=True, pady=(8, 0))
        self.texture_replacement_preview = tk.Label(replacement_box, text="Import a replacement texture", anchor="center", justify="center", bg=self._dark["field"], fg=self._dark["muted"])
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

    def _collect_mesh_render_resources(self, preferred_asset: Asset, data: bytes, meshes: list[MeshInfo]) -> tuple[dict[int, object], dict[int, dict[int, int]], dict[int, dict[int, tuple[float, float, float, float]]], int]:
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
        wanted_keys: set[int] = set()
        for mesh in meshes:
            texture_map: dict[int, int] = {}
            color_map: dict[int, tuple[float, float, float, float]] = {}
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
                        texture_key = direct_record.texture_key
                    else:
                        texture_key = legacy_materials.get(material_id)

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
        return images, material_textures, material_colors, len(wanted_keys - set(images))

    def _start_mesh_texture_search(self, preferred_asset: Asset, texture_keys: set[int], generation: int) -> None:
        """Resolve cross-asset texture references without blocking Tk's UI thread."""
        if not texture_keys or self.project.kind != "bf" or self.project.path is None:
            return
        project_path = self.project.path
        preferred_index = preferred_asset.index
        wanted = set(texture_keys)
        self.status.set(f"Ready - ricerca di {len(wanted)} texture esterne in background...")

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
            missing_note = f" - {len(remaining)} texture non risolte" if remaining else ""
            self.mesh_source_label.config(text=(
                f"{self._mesh_source_asset.name} - {len(self._mesh_infos)} mesh visualizzabili, "
                f"{len(self._mesh_textures)} texture materiali risolte{missing_note}"
            ))
        if error:
            self._log(f"WARN  Ricerca texture mesh in background: {error}")
        self.status.set("Ready")
        self._log(
            f"OK    Ricerca texture mesh: {len(found)} trovate, {len(remaining)} non risolte"
        )

    def scan_meshes(self) -> None:
        asset = self._selected_asset()
        if asset is None:
            messagebox.showinfo("Mesh Swap", "Seleziona prima un asset .wow/.bin/.gao nel browser.")
            return
        try:
            self._mesh_texture_search_generation += 1
            search_generation = self._mesh_texture_search_generation
            self.status.set("Mesh Editor: lettura mesh, materiali e texture locali...")
            self.update_idletasks()
            data = self.project.read_asset(asset)
            meshes = _scan_pop_meshes(data)
            _associate_mesh_material_packs(data, meshes)
            images, texture_maps, color_maps, unresolved = self._collect_mesh_render_resources(asset, data, meshes)
            self._mesh_data = bytearray(data)
            self._mesh_infos = meshes
            self._mesh_source_asset = asset
            self._mesh_textures = images
            self._mesh_material_textures_by_mesh = texture_maps
            self._mesh_material_colors_by_mesh = color_maps
            self._mesh_material_textures = texture_maps.get(meshes[0].key, {}) if meshes else {}

            self._clear_tree(self.mesh_tree)
            for i, mesh in enumerate(meshes):
                name = mesh.object_name or f"Mesh #{i + 1}"
                self.mesh_tree.insert("", "end", iid=f"mesh_{i}",
                                      values=(name, f"{len(mesh.vertices):,}", f"{len(mesh.faces):,}", f"0x{mesh.key:08X}", mesh.layout_name or "Prototipo"))
            missing_note = f" - {unresolved} texture non risolte" if unresolved else ""
            self.mesh_source_label.config(text=(f"{asset.name} - {len(meshes)} mesh visualizzabili, "
                                                f"{len(images)} texture materiali risolte{missing_note}"))
            self.mesh_info.config(text="Seleziona un mesh. Trascina con il mouse per ruotare; rotella per zoom.")
            self.tabs.select(self.mesh_tab)
            self._mesh_yaw = -0.45
            self._mesh_pitch = 0.18
            self._mesh_zoom = 1.0
            if meshes:
                self.mesh_tree.selection_set("mesh_0")
                self.mesh_tree.focus("mesh_0")
                self.on_mesh_selected()
            self.status.set("Ready")
            self._log(f"OK    Mesh scan: {asset.name} -> {len(meshes)} mesh, {len(images)} texture materiali risolte")
            self._start_mesh_texture_search(asset, self._mesh_pending_texture_keys, search_generation)
        except Exception as exc:
            self.status.set("Ready")
            self._log(f"ERROR Mesh scan: {exc}")
            messagebox.showerror("Mesh Swap", str(exc))

    def import_swap_mesh(self) -> None:
        source = filedialog.askopenfilename(
            title="Import replacement mesh",
            filetypes=[("Mesh 3D", "*.glb *.obj"), ("glTF Binary", "*.glb"), ("Wavefront OBJ", "*.obj")]
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
            self.swap_mesh_info.config(text=(
                f"{path.name} - {len(mesh.vertices):,} vertices - {len(mesh.faces):,} faces - "
                f"{len(material_colors)} materiali - {len(textures)} texture\n"
                f"{mesh.layout_name}; {len(mesh.source_joint_names)} joint sorgente. "
                "Materiali/texture importati solo in anteprima; Apply mantiene quelli BF. "
                "Character BF: rig originale e pesi ricalcolati per prossimita. Destinazione statica: sola geometria. Esportare in posa di riposo."
            ))
            self._log(f"OK    Mesh replacement import: {path.name} -> {len(mesh.vertices)} vertici, {len(mesh.faces)} facce")
        except Exception as exc:
            self._swap_mesh = None
            self._swap_mesh_path = None
            self._swap_mesh_textures = {}
            self._swap_mesh_material_textures = {}
            self._swap_mesh_material_colors = {}
            self.swap_mesh_info.config(text=f"Import mesh non riuscito: {exc}")
            self._log(f"ERROR Mesh replacement import: {exc}")
            messagebox.showerror("Import replacement mesh", str(exc))

    def apply_mesh_changes(self) -> bool:
        target = self._selected_mesh()
        asset = self._mesh_source_asset
        if target is None or asset is None or self._swap_mesh is None:
            messagebox.showinfo("Mesh Swap", "Scansiona e seleziona una mesh, poi importa un GLB o OBJ.")
            return False
        if not any(a is asset for a in self.project.assets):
            messagebox.showerror("Mesh Swap", "Il progetto è cambiato: riscansiona l'asset.")
            return False
        try:
            data = self.project.read_asset(asset)
            entries = _parse_pop_file_entries(data)
            entry = entries[target.entry_index]
            if entry.key != target.key:
                raise ValueError("La selezione non corrisponde più all'asset: riscansiona.")
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
                raise ValueError("Verifica della mesh ricostruita non riuscita.")
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
                    f"{len(mesh.vertices):,}", f"{len(mesh.faces):,}", f"0x{mesh.key:08X}", mesh.layout_name or "Prototipo"))
            self.on_mesh_selected()
            self.status.set("Mesh applicata in memoria. Salva il BF/BIN/DEC per scriverla su disco.")
            self.mesh_source_label.config(text=f"{asset.name} - mesh applicate in memoria, pronte al salvataggio")
            self._log(f"APPLY Mesh 0x{target.key:08X}: {len(updated.vertices)} vertici, {len(updated.faces)} facce; "
                      f"{len(updated.skin_bones or [])} ossa BF conservate, pesi trasferiti automaticamente")
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
        self.mesh_info.config(text=(
            f"Mesh ID 0x{mesh.key:08X} • {len(mesh.vertices):,} vertices • {len(mesh.faces):,} faces • "
            f"version {mesh.version} / {mesh.layout_name or 'layout prototipo'} / {len(mesh.skin_bones or [])} ossa" + (f" • object: {mesh.object_name}" if mesh.object_name else "")
        ))
        if OpenGLFrame is not None and isinstance(self.mesh_canvas, MeshViewport):
            texture_map = self._mesh_material_textures_by_mesh.get(mesh.key, {})
            color_map = self._mesh_material_colors_by_mesh.get(mesh.key, {})
            self.mesh_canvas.set_scene(mesh, self._mesh_textures, texture_map, color_map)
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
                    tex_key = texture_map.get(mat_id)
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
            self._refresh_bf_repack_info()
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
            if bin_path.stem.casefold() == "univers_oin_ff0c008e":
                self.analyze_current_ova()
            else:
                self.tabs.select(self.asset_tab)
        except Exception as exc:
            self._log(f"ERROR Import BIN: {exc}")
            messagebox.showerror("Import BIN", str(exc))

    def import_dec(self) -> None:
        self._log("INFO  Apertura dialogo import DEC...")
        path = filedialog.askopenfilename(title="Import DEC", filetypes=[("DEC files", "*.dec"), ("All files", "*.*")])
        if not path:
            return
        try:
            dec_path = Path(path)
            self.project.open_dec(dec_path)
            self._ova_data = bytearray(dec_path.read_bytes())
            self._ova_original = bytes(self._ova_data)
            self._ova_dirty = False
            self.refresh_assets()
            self._refresh_title()
            self._log(f"INFO  DEC aperto: {dec_path} ({len(self._ova_data):,} B)")
            if dec_path.stem.casefold() == "univers_oin_ff0c008e":
                self.analyze_current_ova()
            else:
                self.tabs.select(self.asset_tab)
        except Exception as exc:
            self._log(f"ERROR Import DEC: {exc}")
            messagebox.showerror("Import DEC", str(exc))

    def close_project(self) -> None:
        self.project = JadeProject()
        self.refresh_assets()
        self._refresh_title()
        self._refresh_bf_repack_info()
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
            messagebox.showinfo("Export all assets", "Apri prima un file .BF.")
            return
        source = self.project.path
        target_dir = Path(filedialog.askdirectory(title="Select output folder for ROOT", mustexist=True))
        if not target_dir:
            return
        try:
            self._log(f"INFO  Export BF completo: {source.name} -> {target_dir}\ROOT")
            extracted = extract_legacy_bf_as_root(source, target_dir)
            root_dir = target_dir / "ROOT"
            self._log(f"OK    Esportati {extracted:,} asset nella struttura {root_dir}")
            messagebox.showinfo("Export all assets", f"Esportati {extracted:,} asset in:\n{root_dir}")
        except Exception as exc:
            self._log(f"ERROR Export all: {exc}")
            messagebox.showerror("Export all assets", str(exc))

    def build_bf_from_folder(self) -> None:
        if self.project.kind != "bf" or self.project.path is None:
            messagebox.showinfo("Build BF", "Apri prima il BF originale da usare come template.")
            return
        root_path = filedialog.askdirectory(title="Select ROOT folder", mustexist=True)
        if not root_path:
            return
        root_dir = Path(root_path)
        if root_dir.name.casefold() != "root":
            messagebox.showerror("Build BF", "Seleziona la cartella ROOT, non la cartella superiore.")
            return
        target = filedialog.asksaveasfilename(title="Save rebuilt BF", initialfile=self.project.path.stem + "_rebuilt.bf", defaultextension=".bf", filetypes=[("Jade Big Files", "*.bf")])
        if not target:
            return
        try:
            self._log(f"INFO  Build BF: template={self.project.path.name}, ROOT={root_dir}")
            count = build_legacy_bf_from_folder(self.project.path, root_dir, Path(target))
            self._log(f"OK    BF ricostruito da ROOT: {target} ({count:,} asset)")
            messagebox.showinfo("Build BF", f"Creato:\n{target}\n\nAsset: {count:,}")
        except Exception as exc:
            self._log(f"ERROR Build BF: {exc}")
            messagebox.showerror("Build BF", str(exc))

    def _select_root_for_lzo(self, title: str) -> Path | None:
        root = filedialog.askdirectory(title=title, mustexist=True)
        if not root:
            return None
        path = Path(root)
        if path.name.casefold() != "root":
            messagebox.showerror("BF Repack", "Seleziona la cartella ROOT dell'estrazione.")
            return None
        return path

    def decompress_all_bins(self) -> None:
        root = self._select_root_for_lzo("Select ROOT folder")
        if root is None:
            return
        try:
            folders = _legacy_folder_entries(self.project.path) if self.project.kind == "bf" and self.project.path else None
            if folders is None:
                raise ValueError("Apri prima il BF originale per mantenere la stessa gerarchia delle cartelle.")
            info = read_bigfile(self.project.path)
            done = 0
            skipped = 0
            for entry in info.entries:
                if entry.parent != 1 or "wolinfo" in entry.name.casefold() or entry.name.casefold() == "size.grs":
                    skipped += 1
                    continue
                enc = _legacy_asset_path(root.parent, folders, entry)
                dec = enc.with_suffix(".dec")
                if not enc.is_file():
                    continue
                raw = enc.read_bytes()
                try:
                    data = decompress_pop_lzo(raw)
                except Exception:
                    continue
                dec.write_bytes(data)
                done += 1
            self._log(f"OK    Decompress all .BIN: {done:,} file decodificati")
            messagebox.showinfo("Decompress all .BIN", f"Creati {done:,} file .DEC nella struttura ROOT.")
        except Exception as exc:
            self._log(f"ERROR Decompress all: {exc}")
            messagebox.showerror("Decompress all .BIN", str(exc))

    def compress_all_dec(self) -> None:
        root = self._select_root_for_lzo("Select ROOT folder")
        if root is None:
            return
        try:
            folders = _legacy_folder_entries(self.project.path) if self.project.kind == "bf" and self.project.path else None
            if folders is None:
                raise ValueError("Apri prima il BF originale per mantenere la stessa gerarchia delle cartelle.")
            info = read_bigfile(self.project.path)
            done = 0
            failed: list[str] = []
            for entry in info.entries:
                if entry.parent != 1 or "wolinfo" in entry.name.casefold() or entry.name.casefold() == "size.grs":
                    continue
                dec = _legacy_asset_path(root.parent, folders, entry).with_suffix(".dec")
                if not dec.is_file():
                    continue
                bin_path = dec.with_suffix(".bin")
                try:
                    bin_path.write_bytes(compress_pop_lzo(dec.read_bytes()))
                    done += 1
                except Exception as exc:
                    failed.append(f"{dec.name}: {exc}")
                    self._log(f"WARN  Compress all .DEC saltato {dec}: {exc}")
            self._log(f"OK    Compress all .DEC: {done:,} file compressi (.bin)")
            if failed:
                messagebox.showwarning(
                    "Compress all .DEC",
                    f"Creati {done:,} file .BIN nella struttura ROOT.\n\n"
                    f"{len(failed):,} file non compressi; vedi il log per i dettagli."
                )
            else:
                messagebox.showinfo("Compress all .DEC", f"Creati {done:,} file .BIN nella struttura ROOT.")
        except Exception as exc:
            self._log(f"ERROR Compress all: {exc}")
            messagebox.showerror("Compress all .DEC", str(exc))

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
            data = bytes(self._texture_data) if self._texture_dirty else (bytes(self._ova_data) if self._ova_data else self.project.decoded_bin)
            data = self.project.apply_mesh_patches(0, data)
            self.project.decoded_bin = data
            self._log(f"INFO  Salvataggio BIN: decoded={len(data):,} B -> {target}")
            self.project.save_bin_as(Path(target), data)
            self._log(f"OK    BIN salvato: {target}")
            if self._texture_dirty:
                self._texture_original = data
                self._texture_dirty = False
            self._ova_dirty = False
        except Exception as exc:
            self._log(f"ERROR Save BIN: {exc}")
            messagebox.showerror("Save BIN", str(exc))

    def save_dec(self) -> None:
        if self.project.kind != "dec" or self.project.decoded_bin is None:
            messagebox.showinfo("Save DEC", "Apri un .dec prima.")
            return
        target = filedialog.asksaveasfilename(title="Save DEC", initialfile=self.project.path.stem + "_edited.dec", defaultextension=".dec", filetypes=[("DEC files", "*.dec")])
        if not target:
            return
        try:
            data = bytes(self._texture_data) if self._texture_dirty else (bytes(self._ova_data) if self._ova_data else self.project.decoded_bin)
            data = self.project.apply_mesh_patches(0, data)
            self.project.decoded_bin = data
            Path(target).write_bytes(data)
            self._log(f"OK    DEC salvato: {target} ({len(data):,} B)")
            if self._texture_dirty:
                self._texture_original = data
                self._texture_dirty = False
            self._ova_dirty = False
        except Exception as exc:
            self._log(f"ERROR Save DEC: {exc}")
            messagebox.showerror("Save DEC", str(exc))

    def analyze_current_ova(self) -> None:
        if self.project.kind in ("bin", "dec"):
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
            self._ova_source_asset = None if self.project.kind in ("bin", "dec") else self._selected_asset()
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
            try:
                selected_entry = next((entry for entry in self.project.info.entries
                                       if entry.index == self._texture_source_asset.index), None)
                texture = self._texture_selected()
                if selected_entry is None or texture is None:
                    raise ValueError("Impossibile risalire all'entry BF o alla texture modificata.")
                texture_replacements, touched = _collect_texture_key_replacements(
                    self.project.path, selected_entry, texture.key,
                    bytes(self._texture_data[texture.data_offset:texture.data_end]),
                    (getattr(self, "_texture_patch_source_type", texture.texture_type)
                     if getattr(self, "_texture_patch_key", texture.key) == texture.key
                     else texture.texture_type),
                    texture.texture_type, texture.width, texture.height, bytes(self._texture_data),
                )
            except Exception as exc:
                self._log(f"ERROR Save BF: {exc}")
                messagebox.showerror("Save BF", str(exc))
                return
            replacements.update(texture_replacements)
            self._log(f"INFO  Texture key 0x{texture.key:08X}: {len(touched)} asset BF da aggiornare")
        if self._material_dirty and self._material_source_asset is not None:
            replacements[self._material_source_asset.index] = bytes(self._material_data)

        try:
            for index in self.project.mesh_patches:
                asset = next(a for a in self.project.assets if a.index == index)
                data = replacements.get(index)
                replacements[index] = (self.project.read_asset(asset) if data is None
                                       else self.project.apply_mesh_patches(index, data))
        except Exception as exc:
            messagebox.showerror("Save BF", str(exc))
            return

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
            if Path(target).resolve() == self.project.path.resolve():
                self.project.info = read_bigfile(self.project.path)
                for patches in self.project.mesh_patches.values():
                    for index, (key, _baseline, payload) in list(patches.items()):
                        patches[index] = (key, payload, payload)
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
                    raise ValueError("Texture palette: offset non valido.")
                palette_id = struct.unpack_from("<I", self._texture_data, tex.data_offset - 4)[0]
                palette_entry = next((e for e in self._texture_file_entries if e.key == palette_id), None)
                if palette_entry is None:
                    raise ValueError(f"Palette 0x{palette_id:08X} non trovata nel BIN.")
                palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                payload = _palette_payload_from_file(
                    Path(source), tex, palette,
                    bytes(self._texture_data[tex.data_offset:tex.data_end]),
                )
            else:
                raise ValueError(f"Formato texture POP non supportato: type {tex.texture_type}.")
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
                "DXT5 (conversione automatica)" if target_type == 7 and tex.texture_type != 7
                else ("DXT1" if target_type == 5 else tex.format)
            )
            self.texture_info.config(text=f"Importata: {Path(source).name} • {len(payload):,} B • {tex.width}x{tex.height} {target_label}")
            self.texture_apply_btn.configure(state="normal")
            self._log(f"OK    Texture convertita: {source} -> {len(payload):,} B {target_label} {tex.width}x{tex.height}")
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
                raise ValueError(f"Impossibile convertire il dump in PNG: {exc}") from exc
            target.write_bytes(blob)
            self._log(f"OK    Texture dump: {target} • PNG: {png_target}")
            messagebox.showinfo("Dump texture", f"Creati:\n{target}\n{png_target}")
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
        _source, payload, target_type = replacement
        source_type = (getattr(self, "_texture_patch_source_type", tex.texture_type)
                       if getattr(self, "_texture_patch_key", tex.key) == tex.key
                       else tex.texture_type)
        if (target_type == tex.texture_type and target_type != 5
                and len(payload) != tex.data_end - tex.data_offset):
            messagebox.showerror("Texture Swap", "La texture importata non ha la stessa dimensione compressa dell'originale.")
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
                        raise ValueError("Palette della texture non trovata.")
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
                        raise ValueError("Palette della texture non trovata.")
                    palette = bytes(self._texture_data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size])
                    payload = _palette_payload_from_image(source_image, tex, palette, payload)
                if (target_type == tex.texture_type and target_type != 5
                        and len(payload) != tex.data_end - tex.data_offset):
                    raise ValueError("La trasformazione non ha prodotto un payload della stessa dimensione dell'originale.")
            except Exception as exc:
                messagebox.showerror("Texture Swap", f"Impossibile applicare rotazione/flip: {exc}")
                return
        patched, count = _patch_texture_key_in_asset(
            bytes(self._texture_data), tex.key, payload, source_type, target_type,
            tex.width, tex.height,
        )
        if not count:
            messagebox.showerror("Texture Swap", "Texture selezionata non trovata nel buffer corrente.")
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
        self.texture_info.config(text=f"Sostituzione applicata • {tex.width}x{tex.height} • type {target_type} / {len(payload):,} B. Usa Save/Extract/Rebuild per scrivere il file.")
        self._log(f"PATCH TEXTURE  key=0x{tex.key:08X} • type={target_type} • payload={len(payload):,} B")

    def save_texture_asset(self) -> None:
        if not self._texture_dirty or self._texture_source_asset is None:
            messagebox.showinfo("Texture Swap", "Non ci sono modifiche texture da salvare.")
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
                    raise ValueError("Impossibile risalire all'entry BF della texture selezionata.")
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

"""Optional OpenGL mesh and material viewports for Tkinter."""
from __future__ import annotations

import math

from .mesh_uv import _jade_uv_to_standard


try:
    from pyopengltk import OpenGLFrame
    from OpenGL import GL, GLU
except Exception:
    OpenGLFrame = None
    GL = GLU = None

class _UnavailableViewport:
    """Type sentinel for isinstance checks when optional OpenGL is unavailable."""


MeshViewport = MaterialViewport = _UnavailableViewport

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
            self.vertex_tint = (1.0, 1.0, 1.0)
            self.vertex_color_intensity = 1.0
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

        def set_vertex_tint(self, tint, intensity=1.0):
            self.vertex_tint = tuple(max(0.0, min(1.0, float(value))) for value in tint)
            self.vertex_color_intensity = max(0.0, min(2.0, float(intensity)))
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

        def _draw_mesh(self, vertices, faces, uvs, uv_indices, material_ids,
                       normals=None, vertex_colors=None):
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
                red = min(1.0, red * self.vertex_tint[0] * self.vertex_color_intensity)
                green = min(1.0, green * self.vertex_tint[1] * self.vertex_color_intensity)
                blue = min(1.0, blue * self.vertex_tint[2] * self.vertex_color_intensity)
                if tex_id:
                    GL.glEnable(GL.GL_TEXTURE_2D); GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
                else:
                    GL.glDisable(GL.GL_TEXTURE_2D)
                GL.glBegin(GL.GL_TRIANGLES)
                for corner, vi in enumerate(face):
                    if vertex_colors and 0 <= vi < len(vertex_colors):
                        vr, vg, vb, va = vertex_colors[vi]
                        GL.glColor4f(red * vr, green * vg, blue * vb, alpha * va)
                    else:
                        GL.glColor4f(red, green, blue, alpha)
                    if uv_indices and face_index < len(uv_indices) and uvs:
                        ui = uv_indices[face_index][corner]
                        if 0 <= ui < len(uvs):
                            u, v = _jade_uv_to_standard(uvs[ui])
                            GL.glTexCoord2f(float(u), float(v))
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
                self._draw_mesh(
                    self.mesh.vertices, self.mesh.faces, self.mesh.uvs,
                    self.mesh.uv_indices, self.mesh.material_ids,
                    self.mesh.normals, self.mesh.vertex_colors)
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

"""Interactive preview and explicit joint assignment for BF character swaps."""
from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import jade_mesh
from .mesh_export import _inverse_matrix
from .viewports import MeshViewport, OpenGLFrame, GL, GLU


def _joint_position(bone):
    matrix = _inverse_matrix(bone.matrix)
    return tuple(matrix[12:15])


def _segment_hit(px, py, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    if length2 < 1:
        return float('inf'), 0.0
    t = max(0.0, min(1.0, ((px - start[0]) * dx + (py - start[1]) * dy) / length2))
    return math.hypot(px - start[0] - t * dx, py - start[1] - t * dy), t


def _attach_cloth_to_mapped_ancestors(mapping, parents, parts):
    """Suggest existing BF slots from the imported joint hierarchy."""
    result = dict(mapping)
    for slot, part in parts.items():
        if part != 'Cloth part' or result.get(slot) is not None:
            continue
        parent = parents.get(slot)
        seen = {slot}
        while parent is not None and parent not in seen:
            seen.add(parent)
            if result.get(parent) is not None:
                result[slot] = result[parent]
                break
            parent = parents.get(parent)
    return result


if OpenGLFrame is not None:
    class RigViewport(MeshViewport):
        def __init__(self, master, owner, **kwargs):
            super().__init__(master, owner, **kwargs)
            self.bones = []
            self.positions = {}
            self.parents = {}
            self.selected = None
            self.parts = {}
            self._rig_drag = None
            self.bind('<ButtonPress-1>', self._rig_mouse_down)
            self.bind('<B1-Motion>', self._rig_mouse_move)
            self.bind('<ButtonRelease-1>', self._rig_mouse_up)

        def _screen(self, point):
            self.tkMakeCurrent()
            self._apply_projection()
            GL.glMatrixMode(GL.GL_MODELVIEW)
            GL.glLoadIdentity()
            GLU.gluLookAt(0.0, 0.0, self.distance, self.target[0], self.target[1],
                          self.target[2], 0.0, 1.0, 0.0)
            GL.glRotatef(math.degrees(self.pitch), 1, 0, 0)
            GL.glRotatef(math.degrees(self.yaw), 0, 1, 0)
            GL.glTranslatef(-self.target[0], -self.target[1], -self.target[2])
            viewport = GL.glGetIntegerv(GL.GL_VIEWPORT)
            projected = GLU.gluProject(*point, GL.glGetDoublev(GL.GL_MODELVIEW_MATRIX),
                                       GL.glGetDoublev(GL.GL_PROJECTION_MATRIX), viewport)
            if projected is None or not 0 <= projected[2] <= 1:
                return None
            return (projected[0], viewport[3] - projected[1], projected[2])

        def _gizmo_length(self):
            return max(0.01, self.distance * 0.13)

        def focus_bone(self, slot):
            """Orbit around a joint, using its connected bones for framing."""
            position = self.positions[slot]
            linked = [other for index, other in self.positions.items()
                      if self.parents.get(index) == slot or self.parents.get(slot) == index]
            radius = max((math.dist(position, other) for other in linked), default=0.0)
            if radius <= 0:
                radius = max(0.1, self.distance * 0.1)
            self.target = list(position)
            self.distance = max(0.25, radius * 2.7)
            self._display()

        def _rig_mouse_down(self, event):
            self._rig_drag = None
            if self.selected in self.positions:
                origin = self.positions[self.selected]
                start = self._screen(origin)
                if start is not None:
                    for axis in range(3):
                        endpoint = tuple(origin[i] + (self._gizmo_length() if i == axis else 0)
                                         for i in range(3))
                        end = self._screen(endpoint)
                        if end is None:
                            continue
                        distance, fraction = _segment_hit(event.x, event.y, start, end)
                        if distance <= 9 and fraction >= 0.18:
                            self._rig_drag = (axis, event.x, event.y, origin, start, end)
                            return
            hits = []
            for bone in self.bones:
                screen = self._screen(self.positions[bone.index])
                if screen is not None:
                    distance = math.hypot(event.x - screen[0], event.y - screen[1])
                    if distance <= 13:
                        hits.append((distance, screen[2], bone.index))
            if hits:
                slot = min(hits)[2]
                self.owner.tree.selection_set(str(slot))
                self.owner.tree.focus(str(slot))
                self.owner.tree.see(str(slot))
                self.owner._select()
                return
            self._down(event)
            self._rig_drag = ('orbit',)

        def _rig_mouse_move(self, event):
            if self._rig_drag is None:
                return
            if self._rig_drag[0] == 'orbit':
                self._orbit(event)
                return
            axis, mouse_x, mouse_y, origin, start, end = self._rig_drag
            dx, dy = end[0] - start[0], end[1] - start[1]
            length2 = dx * dx + dy * dy
            if length2 < 64:
                return
            pixels = ((event.x - mouse_x) * dx + (event.y - mouse_y) * dy) / length2
            moved = list(origin)
            moved[axis] += pixels * self._gizmo_length()
            self.positions[self.selected] = tuple(moved)
            for variable, value in zip(self.owner.xyz, moved):
                variable.set(f'{value:.5f}')
            self._display()

        def _rig_mouse_up(self, _event):
            self._rig_drag = None
            self.drag = None

        def redraw(self):
            super().redraw()
            if not self.bones:
                return
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glDisable(GL.GL_LIGHTING)
            GL.glDisable(GL.GL_TEXTURE_2D)
            GL.glPointSize(9)
            GL.glLineWidth(3)
            GL.glPushMatrix()
            GL.glTranslatef(-self.target[0], -self.target[1], -self.target[2])
            GL.glBegin(GL.GL_LINES)
            for bone in self.bones:
                parent = self.parents.get(bone.index)
                if parent not in self.positions:
                    continue
                GL.glColor3f(*(0.95, 0.55, 0.18) if self.parts.get(bone.index) == 'Cloth part'
                             else (0.15, 0.85, 0.95))
                GL.glVertex3f(*self.positions[parent])
                GL.glVertex3f(*self.positions[bone.index])
            GL.glEnd()
            GL.glBegin(GL.GL_POINTS)
            for bone in self.bones:
                GL.glColor3f(*(1.0, 1.0, 0.2) if bone.index == self.selected
                             else (0.95, 0.55, 0.18) if self.parts.get(bone.index) == 'Cloth part'
                             else (0.2, 0.9, 1.0))
                GL.glVertex3f(*self.positions[bone.index])
            GL.glEnd()
            if self.selected in self.positions:
                origin = self.positions[self.selected]
                GL.glLineWidth(5)
                GL.glBegin(GL.GL_LINES)
                for axis, color in enumerate(((1.0, 0.22, 0.22),
                                              (0.2, 1.0, 0.28),
                                              (0.3, 0.55, 1.0))):
                    GL.glColor3f(*color)
                    GL.glVertex3f(*origin)
                    endpoint = list(origin)
                    endpoint[axis] += self._gizmo_length()
                    GL.glVertex3f(*endpoint)
                GL.glEnd()
            GL.glPopMatrix()
            GL.glEnable(GL.GL_LIGHTING)
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glEnable(GL.GL_TEXTURE_2D)


class RigEditor(tk.Toplevel):
    """Edit a rest pose and map imported joints onto existing GAO slots."""
    def __init__(self, parent, source, target, metadata, mode):
        super().__init__(parent)
        self.title(f"Rig Editor — {mode}")
        try:
            self.iconbitmap(str(Path(__file__).parent / 'icons' / 'rig.ico'))
        except tk.TclError:
            pass
        self.geometry('1180x760')
        self.transient(parent)
        self.source, self.target, self.metadata, self.mode = source, target, metadata, mode
        from .mesh_import import _discard_remote_skin_triangles
        self.scene_source = _discard_remote_skin_triangles(source, target)
        self.result = None
        self._previewing_result = False
        self._preview_camera = None
        self.bones = target.skin_bones if mode == 'Keep original rig' else source.skin_bones
        if not self.bones:
            raise ValueError('Rig Editor requires a skinned GLB and a skinned BF target.')
        self.positions = {bone.index: _joint_position(bone) for bone in self.bones}
        self.parents = ({index: info[1] for index, info in metadata.items()}
                        if mode == 'Keep original rig' else source.source_bone_parents or {})
        self.parts = {bone.index: 'Body part' for bone in self.bones}
        self.mapping = {}
        targets = {bone.index: metadata.get(bone.index, (f'bone_{bone.index}',))[0]
                   for bone in target.skin_bones or []}
        self.target_labels = {f'{slot}: {name}': slot for slot, name in targets.items()}
        self.target_labels['Spatial weight recovery'] = -1
        source_names = source.source_bone_names or {}
        for bone in self.bones:
            name = source_names.get(bone.index, '').casefold()
            matches = [slot for slot, target_name in targets.items() if name and name == target_name.casefold()]
            self.mapping[bone.index] = (matches[0] if len(matches) == 1 else
                                        -1 if name in ('neutral_bone', 'neutral', 'helper') else None)
            if any(token in (source_names.get(bone.index, '') or targets.get(bone.index, '')).casefold()
                   for token in ('cape', 'cloth', 'scarf', 'skirt', 'hair', 'belt', 'mantle')):
                self.parts[bone.index] = 'Cloth part'
        self._build()
        self.grab_set()
        self.wait_window()

    def _build(self):
        ttk.Label(self, text=('Rest pose: move joints to fit the imported character. '
                              'Orange marks cloth or hair joints; these must map to an existing animated BF bone.'),
                  wraplength=1100).pack(fill='x', padx=10, pady=8)
        split = ttk.Panedwindow(self, orient='horizontal')
        split.pack(fill='both', expand=True, padx=10)
        left, right = ttk.Frame(split), ttk.Frame(split)
        split.add(left, weight=4); split.add(right, weight=2)
        if OpenGLFrame is not None:
            self.viewport = RigViewport(left, self, highlightthickness=0, bd=0)
            self.viewport.pack(fill='both', expand=True)
            self.viewport.set_scene(self.scene_source, {})
            self.viewport.bones = self.bones
            self.viewport.positions = self.positions
            self.viewport.parents = self.parents
            self.viewport.parts = self.parts
            self.viewport._display()
        else:
            ttk.Label(left, text='OpenGL unavailable; use the numeric joint controls.').pack()
            self.viewport = None
        self.tree = ttk.Treeview(right, columns=('part', 'target'), show='tree headings')
        self.tree.heading('#0', text='Joint'); self.tree.heading('part', text='Type')
        self.tree.heading('target', text='BF target')
        self.tree.column('#0', width=160); self.tree.column('part', width=90); self.tree.column('target', width=125)
        self.tree.pack(fill='both', expand=True)
        for bone in self.bones:
            name = (self.metadata.get(bone.index, (f'bone_{bone.index}',))[0]
                    if self.mode == 'Keep original rig' else
                    (self.source.source_bone_names or {}).get(bone.index, f'joint_{bone.index}'))
            self.tree.insert('', 'end', iid=str(bone.index), text=f'{bone.index}: {name}',
                             values=(self.parts[bone.index], self.mapping.get(bone.index)))
        self.tree.bind('<<TreeviewSelect>>', self._select)
        self.tree.bind('<Double-1>', self._focus_tree_bone)
        edit = ttk.LabelFrame(right, text='Selected joint', padding=8)
        edit.pack(fill='x', pady=8)
        self.xyz = [tk.StringVar() for _ in range(3)]
        for axis, var in zip('XYZ', self.xyz):
            row = ttk.Frame(edit); row.pack(fill='x')
            ttk.Label(row, text=axis, width=4).pack(side='left')
            entry = ttk.Entry(row, textvariable=var); entry.pack(side='left', fill='x', expand=True)
            entry.bind('<Return>', self._set_position)
            entry.bind('<FocusOut>', self._set_position)
        self.part = tk.StringVar()
        ttk.Combobox(edit, textvariable=self.part, state='readonly',
                     values=('Body part', 'Cloth part')).pack(fill='x', pady=3)
        self.part.trace_add('write', self._set_part)
        if self.mode == 'Adapt new rig':
            self.destination = tk.StringVar()
            ttk.Combobox(edit, textvariable=self.destination, state='readonly',
                         values=['Unassigned', *self.target_labels]).pack(fill='x', pady=3)
            self.destination.trace_add('write', self._set_destination)
            ttk.Button(edit, text='Attach unassigned cloth to parent',
                       command=self._attach_cloth).pack(fill='x', pady=(8, 0))
            ttk.Button(edit, text='Match cloth to BF cloth bones',
                       command=self._match_cloth_bones).pack(fill='x', pady=(3, 0))
            self._preview_button = ttk.Button(edit, text='Preview mapped result',
                                              command=self._toggle_result_preview)
            self._preview_button.pack(fill='x', pady=(3, 0))
        ttk.Button(self, text='Apply rig', command=self._accept).pack(side='right', padx=10, pady=8)
        ttk.Button(self, text='Cancel', command=self.destroy).pack(side='right', pady=8)
        self.tree.selection_set(str(self.bones[0].index))
        self._select()

    def _selected(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _focus_tree_bone(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or self.viewport is None:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._select()
        self.viewport.focus_bone(int(iid))
        self.viewport.focus_set()

    def _select(self, _event=None):
        slot = self._selected()
        if slot is None: return
        for var, value in zip(self.xyz, self.positions[slot]): var.set(f'{value:.5f}')
        self.part.set(self.parts[slot])
        if self.mode == 'Adapt new rig':
            self.destination.set(next((label for label, target in self.target_labels.items()
                                       if target == self.mapping.get(slot)), 'Unassigned'))
        if self.viewport:
            self.viewport.selected = slot; self.viewport._display()

    def _set_position(self, _event=None):
        slot = self._selected()
        if slot is None: return
        try:
            value = tuple(float(var.get()) for var in self.xyz)
            if not all(math.isfinite(x) and abs(x) < 1e5 for x in value): raise ValueError
        except ValueError:
            self._select(); return
        self.positions[slot] = value
        if self.viewport: self.viewport._display()

    def _set_part(self, *_):
        slot = self._selected()
        if slot is not None and self.part.get() in ('Body part', 'Cloth part'):
            self.parts[slot] = self.part.get()
            self.tree.set(str(slot), 'part', self.part.get())
            if self.viewport: self.viewport._display()

    def _set_destination(self, *_):
        slot = self._selected()
        if slot is not None:
            self.mapping[slot] = self.target_labels.get(self.destination.get())
            self.tree.set(str(slot), 'target',
                          'Spatial weight recovery' if self.mapping[slot] == -1 else
                          self.mapping[slot] if self.mapping[slot] is not None else 'Unassigned')

    def _attach_cloth(self):
        proposed = _attach_cloth_to_mapped_ancestors(self.mapping, self.parents, self.parts)
        changed = [slot for slot in proposed if proposed[slot] != self.mapping.get(slot)]
        self.mapping = proposed
        for slot in changed:
            self.tree.set(str(slot), 'target', proposed[slot])
        self._select()
        remaining = sum(self.parts[slot] == 'Cloth part' and self.mapping.get(slot) is None
                        for slot in self.parts)
        messagebox.showinfo('Rig Editor',
                            f'{len(changed)} cloth joint(s) attached to an assigned ancestor. '
                            f'{remaining} still need a manual BF target. '
                            'These joints will follow the parent; independent cloth motion '
                            'requires a compatible animated BF bone.', parent=self)

    def _match_cloth_bones(self):
        targets = {bone.index: bone for bone in self.target.skin_bones or []
                   if any(token in self.metadata.get(bone.index, ('',))[0].casefold()
                          for token in ('skirt', 'hair', 'cloth', 'cape', 'scarf', 'belt'))}
        used = {slot for slot in self.mapping.values() if slot is not None and slot >= 0}
        changed = 0
        for source in self.bones:
            if self.parts[source.index] != 'Cloth part' or self.mapping[source.index] is not None:
                continue
            options = [bone for bone in targets.values() if bone.index not in used]
            if not options:
                break
            origin = self.positions[source.index]
            nearest = min(options, key=lambda bone: sum(
                (a - b) ** 2 for a, b in zip(origin, _joint_position(bone))))
            self.mapping[source.index] = nearest.index
            self.tree.set(str(source.index), 'target', nearest.index)
            used.add(nearest.index)
            changed += 1
        self._select()
        messagebox.showinfo('Rig Editor',
                            f'{changed} cloth joint(s) mapped to existing BF cloth slots. '
                            'Inspect the skinned preview before applying.', parent=self)

    def _toggle_result_preview(self):
        if self.viewport is None:
            return
        if self._previewing_result:
            self.viewport.set_scene(self.scene_source, {})
            self.viewport.bones = self.bones
            self.viewport.positions = self.positions
            self.viewport.parents = self.parents
            self.viewport.selected = self._selected()
            self.viewport.target, self.viewport.distance, self.viewport.yaw, self.viewport.pitch = self._preview_camera
            self._previewing_result = False
            self._preview_button.configure(text='Preview mapped result')
            self.viewport._display()
            return
        if any(value is None for value in self.mapping.values()):
            messagebox.showerror('Rig Editor', 'Assign every imported joint before previewing.', parent=self)
            return
        try:
            from .mesh_import import _adapt_mesh_skin_to_target
            candidate = position_imported_skin(self.source, self.positions)
            adapted = _adapt_mesh_skin_to_target(candidate, self.target, self.metadata,
                                                 self.mapping, self.parts)
        except Exception as exc:
            messagebox.showerror('Rig Editor', str(exc), parent=self)
            return
        self._preview_camera = (list(self.viewport.target), self.viewport.distance,
                                self.viewport.yaw, self.viewport.pitch)
        self.viewport.set_scene(adapted, {})
        self.viewport.bones = []
        self.viewport.selected = None
        self._previewing_result = True
        self._preview_button.configure(text='Back to rig editing')
        self.viewport._display()

    def _accept(self):
        self._set_position()
        if self.mode == 'Adapt new rig' and any(value is None for value in self.mapping.values()):
            messagebox.showerror('Rig Editor', 'Assign each imported joint to a BF bone or spatial weight recovery.', parent=self)
            return
        self.result = (dict(self.positions), dict(self.mapping), dict(self.parts))
        self.destroy()


def transfer_positioned_skin(source, target, positions):
    """Transfer original weights after moving their reference joint positions."""
    bones = target.skin_bones or []
    original = {bone.index: _joint_position(bone) for bone in bones}
    influences = [[] for _ in target.vertices]
    for bone in bones:
        for vertex, word in bone.weights:
            if 0 <= vertex < len(influences):
                influences[vertex].append((bone.index, jade_mesh.decode_weight(word)))
    moved = []
    for vertex, row in zip(target.vertices, influences):
        total = sum(weight for _, weight in row)
        shift = [sum(weight * (positions[slot][axis] - original[slot][axis]) for slot, weight in row) / total
                 if total else 0.0 for axis in range(3)]
        moved.append(tuple(vertex[axis] + shift[axis] for axis in range(3)))
    return replace(source, skin_bones=jade_mesh.transfer_skin(bones, moved, source.vertices, 3),
                   skin_flags=target.skin_flags, skin_adaptation_note='Original BF rig; positioned weight transfer')


def position_imported_skin(source, positions):
    """Move rest geometry with edited source joints before BF bind-pose conversion."""
    bones = source.skin_bones or []
    original = {bone.index: _joint_position(bone) for bone in bones}
    influences = [[] for _ in source.vertices]
    for bone in bones:
        for vertex, word in bone.weights:
            if 0 <= vertex < len(influences):
                influences[vertex].append((bone.index, jade_mesh.decode_weight(word)))
    vertices = []
    for vertex, row in zip(source.vertices, influences):
        total = sum(weight for _, weight in row)
        vertices.append(tuple(vertex[axis] +
                              (sum(weight * (positions[slot][axis] - original[slot][axis])
                                   for slot, weight in row) / total if total else 0.0)
                              for axis in range(3)))
    return replace(source, vertices=vertices, normals=None)

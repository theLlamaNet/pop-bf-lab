"""Native POP mesh discovery and geometry decoding."""
from __future__ import annotations

import jade_mesh
import math
import struct
from .models import (
    MeshInfo,
    PopFileEntry,
)
from .resources import (
    _PopReader,
    _parse_pop_file_entries,
)


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
                color_count = min(num_unknown, num_vertices)
                color_bytes = r.bytes(color_count * 4)
                vertex_colors = [tuple(channel / 255.0 for channel in color_bytes[i:i + 4])
                                 for i in range(0, len(color_bytes), 4)]
                if color_count < num_vertices:
                    vertex_colors.extend([(1.0, 1.0, 1.0, 1.0)]
                                         * (num_vertices - color_count))
            else:
                vertex_colors = None
            uv_data = r.f32s(2 * num_uvs)
            uvs = list(zip(uv_data[0::2], uv_data[1::2]))

            material_ids: list[tuple[int, int]] = []
            num_faces = 0
            for _ in range(num_materials):
                face_count = r.u32()
                material_id = r.i32()
                if face_count > 1_000_000:
                    raise ValueError("Implausible face count.")
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
                                   layout_name="Character / skin" if layout.bones else "Static mesh",
                                   vertex_colors=vertex_colors))
        except (ValueError, IndexError, struct.error):
            continue
    _attach_instance_vertex_colors(data, meshes)
    return meshes


def _attach_instance_vertex_colors(data: bytes, meshes: list[MeshInfo]) -> None:
    """Attach primary GAO RLI colours used by the game to preview meshes."""
    if not meshes:
        return
    entries = _parse_pop_file_entries(data)
    by_key: dict[int, list[MeshInfo]] = {}
    for mesh in meshes:
        by_key.setdefault(mesh.key, []).append(mesh)
    gao_type = struct.unpack("<I", b".gao")[0]
    assigned: set[int] = set()
    for entry in entries:
        if entry.data_type != gao_type:
            continue
        raw = data[entry.data_offset:entry.data_offset + entry.size]
        payload = raw[4:]
        try:
            if len(payload) < 16:
                continue
            _version, _flags, identity, name_len = struct.unpack_from("<4I", payload)
            if not identity & 0x4000:
                continue
            visual = 16 + name_len + 10 + 68 + (48 if identity & 0x80000 else 24)
            if visual + 8 > len(payload):
                continue
            gro_key = struct.unpack_from("<I", payload, visual)[0]
            candidates = by_key.get(gro_key, ())
            for mesh in candidates:
                identity_key = id(mesh)
                if identity_key in assigned:
                    continue
                marker = b"\xff\xff" + struct.pack("<I", len(mesh.vertices))
                search_at, search_end = visual, min(visual + 69, len(payload))
                while search_at < search_end:
                    pos = payload.find(marker, search_at, search_end)
                    if pos < 0:
                        break
                    start = pos + 6
                    end = start + len(mesh.vertices) * 4
                    sample = min(64, len(mesh.vertices))
                    valid_alphas = (sum(payload[start + i * 4 + 3] in (0xFD, 0xFE, 0xFF)
                                        for i in range(sample))
                                    if end <= len(payload) else 0)
                    if (end + 4 <= len(payload)
                            and struct.unpack_from("<I", payload, end)[0] == 1
                            and (not sample or valid_alphas * 10 >= sample * 9)):
                        mesh.vertex_colors = [
                            tuple(channel / 255.0 for channel in payload[offset:offset + 4])
                            for offset in range(start, end, 4)
                        ]
                        assigned.add(identity_key)
                        break
                    search_at = pos + 1
        except (IndexError, struct.error):
            continue


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

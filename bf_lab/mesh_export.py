"""OBJ and skinned GLB serialization for Jade geometry."""
import json
import math
import struct

import jade_mesh
from .mesh_import import _mesh_vertex_normals
from .mesh_uv import _jade_uv_to_standard
from .resources import _parse_pop_file_entries


def _character_bone_metadata(data, mesh, require_names=False):
    """Resolve GEO matrix slots through the owning GAO's gizmo table."""
    entries = _parse_pop_file_entries(data)
    gao_type = struct.unpack('<I', b'.gao')[0]
    objects = {}
    hosts = []
    for entry in entries:
        if entry.data_type != gao_type:
            continue
        raw = data[entry.data_offset:entry.data_offset + entry.size]
        if raw[:4] != b'.gao' or len(raw) < 24:
            continue
        payload = raw[4:]
        identity, name_size = struct.unpack_from('<II', payload, 8)
        if name_size > 4096:
            continue
        name = payload[16:16 + name_size].split(b'\0', 1)[0].decode('ascii', 'replace')
        offset = 16 + name_size + 10 + 68 + (48 if identity & 0x80000 else 24)
        gro_key = None
        if identity & 0x4000:
            if offset + 42 > len(payload):
                continue
            gro_key = struct.unpack_from('<I', payload, offset)[0]
            visual_size = 42
            if struct.unpack_from('<H', payload, offset + 38)[0] and offset + 46 <= len(payload):
                remaining = struct.unpack_from('<I', payload, offset + 42)[0]
                if remaining < len(payload):
                    visual_size = 46 + remaining
            offset += visual_size
        father = None
        if identity & 0x400000:
            if offset + 72 <= len(payload):
                father = struct.unpack_from('<I', payload, offset)[0]
            offset += 72
        gizmos = []
        if identity & 0x200000 and identity & 0x1000000 and offset + 4 <= len(payload):
            count = struct.unpack_from('<I', payload, offset)[0]
            if 0 < count < 1000 and offset + 4 + count * 8 <= len(payload):
                gizmos = [struct.unpack_from('<I', payload, offset + 4 + i * 8)[0]
                          for i in range(count)]
        objects[entry.key] = (name, father)
        if gro_key == mesh.key or (gro_key is not None and gro_key != mesh.key and
                                   any(e.key == gro_key and e.data_type == 8 and
                                       struct.pack('<I', mesh.key) in data[e.data_offset:e.data_offset + e.size]
                                       for e in entries)):
            hosts.append(gizmos)
    slots = max(hosts, key=len, default=[])
    key_to_index = {slots[bone.index]: bone.index for bone in mesh.skin_bones or []
                    if bone.index < len(slots)}
    result = {}
    for bone in mesh.skin_bones or []:
        if bone.index < len(slots):
            key = slots[bone.index]
            name, father = objects.get(key, ('', None))
            if require_names and not name.strip('\0').strip():
                continue
            seen = {key}
            while father not in key_to_index and father in objects and father not in seen:
                seen.add(father)
                father = objects[father][1]
            result[bone.index] = (name.removesuffix('.gao') or f'bone_{bone.index}',
                                  key_to_index.get(father), key)
    return result


def _mesh_obj_lines(mesh, mtl_name):
    lines = ["# Exported by PoP BF Lab", f"mtllib {mtl_name}"]
    for x, y, z in mesh.vertices:
        lines.append(f"v {x:.9g} {y:.9g} {z:.9g}")
    for uv in mesh.uvs:
        u, v = _jade_uv_to_standard(uv)
        lines.append(f"vt {u:.9g} {v:.9g}")
    normals = mesh.normals or _mesh_vertex_normals(mesh.vertices, mesh.faces)
    if len(normals) != len(mesh.vertices):
        raise ValueError("One normal per vertex is required for OBJ export.")
    for x, y, z in normals:
        lines.append(f"vn {x:.9g} {y:.9g} {z:.9g}")
    lines.append("s 1")
    cursor = 0
    for material, count in mesh.material_ids:
        lines.append(f"usemtl mat_{material}")
        for fi in range(cursor, min(cursor + count, len(mesh.faces))):
            face = mesh.faces[fi]
            if mesh.uv_indices and fi < len(mesh.uv_indices):
                refs = [f"{vi+1}/{ui+1}/{vi+1}" for vi, ui in zip(face, mesh.uv_indices[fi])]
            else:
                refs = [f"{vi+1}//{vi+1}" for vi in face]
            lines.append("f " + " ".join(refs))
        cursor += count
    return lines


def _inverse_matrix(matrix):
    rows = [[float(matrix[column * 4 + row]) for column in range(4)] +
            [float(row == column) for column in range(4)] for row in range(4)]
    for column in range(4):
        pivot = max(range(column, 4), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            raise ValueError("A character bind matrix is singular.")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        factor = rows[column][column]
        rows[column] = [value / factor for value in rows[column]]
        for row in range(4):
            if row != column:
                factor = rows[row][column]
                rows[row] = [a - factor * b for a, b in zip(rows[row], rows[column])]
    return [rows[row][column + 4] for column in range(4) for row in range(4)]


def _write_skinned_glb(mesh, path, texture_paths=None, material_textures=None,
                       bone_metadata=None):
    """Write Jade rest geometry, bind matrices and weights as glTF 2.0 GLB."""
    from .mesh_import import _glb_mat_mul, _JADE_TO_GLTF, _GLTF_TO_JADE

    bones = mesh.skin_bones or []
    if not bones:
        raise ValueError("The selected mesh has no skin.")
    if len(bones) > 65535:
        raise ValueError("Too many joints for GLB export.")
    normals = mesh.normals or _mesh_vertex_normals(mesh.vertices, mesh.faces)
    influences = [[] for _ in mesh.vertices]
    for ordinal, bone in enumerate(bones):
        for vertex, word in bone.weights:
            if not 0 <= vertex < len(influences):
                raise ValueError("Skin weight refers to an invalid vertex.")
            weight = jade_mesh.decode_weight(word)
            if weight:
                influences[vertex].append((ordinal, weight))
    if any(not row for row in influences):
        raise ValueError("A character vertex has no skin weights.")

    blob = bytearray()
    doc = {"asset": {"version": "2.0", "generator": "PoP BF Lab"},
           "scene": 0, "scenes": [], "nodes": [], "meshes": [], "skins": [],
           "buffers": [], "bufferViews": [], "accessors": [], "materials": []}

    def accessor(payload, count, component, kind, target=None):
        while len(blob) % 4:
            blob.append(0)
        offset = len(blob)
        blob.extend(payload)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(payload)}
        if target is not None:
            view["target"] = target
        vi = len(doc["bufferViews"])
        doc["bufferViews"].append(view)
        ai = len(doc["accessors"])
        doc["accessors"].append({"bufferView": vi, "componentType": component,
                                 "count": count, "type": kind})
        return ai

    def floats(rows):
        return b"".join(struct.pack("<" + "f" * len(row), *row) for row in rows)

    expanded = {}
    vertex_refs = []
    grouped = []
    cursor = 0
    for material, count in mesh.material_ids:
        indices = []
        for face_index in range(cursor, min(cursor + count, len(mesh.faces))):
            face = mesh.faces[face_index]
            uv_face = mesh.uv_indices[face_index] if mesh.uv_indices else face
            for vertex, uv in zip(face, uv_face):
                key = (vertex, uv)
                if key not in expanded:
                    expanded[key] = len(vertex_refs)
                    vertex_refs.append(key)
                indices.append(expanded[key])
        grouped.append((material, indices))
        cursor += count
    if cursor != len(mesh.faces):
        raise ValueError("Material face counts do not cover the complete mesh.")
    positions = [(v[0], v[2], -v[1]) for vi, _ in vertex_refs for v in [mesh.vertices[vi]]]
    gltf_normals = [(n[0], n[2], -n[1]) for vi, _ in vertex_refs for n in [normals[vi]]]
    # glTF and Jade use the same top-left V origin. OBJ needs the conversion
    # above, but applying it here mirrored character textures vertically.
    uvs = [mesh.uvs[ui] for _, ui in vertex_refs]
    joint_sets = [[] for _ in range((max(map(len, influences)) + 3) // 4)]
    weight_sets = [[] for _ in joint_sets]
    for vi, _ in vertex_refs:
        row = sorted(influences[vi], key=lambda pair: -pair[1])
        total = sum(weight for _, weight in row)
        for set_index, (joint_rows, weight_rows) in enumerate(zip(joint_sets, weight_sets)):
            section = row[set_index * 4:set_index * 4 + 4]
            joint_rows.append(tuple(index for index, _ in section) + (0,) * (4 - len(section)))
            weight_rows.append(tuple(weight / total for _, weight in section) + (0.,) * (4 - len(section)))
    attrs = {"POSITION": accessor(floats(positions), len(vertex_refs), 5126, "VEC3", 34962),
             "NORMAL": accessor(floats(gltf_normals), len(vertex_refs), 5126, "VEC3", 34962),
             "TEXCOORD_0": accessor(floats(uvs), len(vertex_refs), 5126, "VEC2", 34962)}
    for index, (joints, weights) in enumerate(zip(joint_sets, weight_sets)):
        attrs[f"JOINTS_{index}"] = accessor(
            b"".join(struct.pack("<4H", *row) for row in joints),
            len(vertex_refs), 5123, "VEC4", 34962)
        attrs[f"WEIGHTS_{index}"] = accessor(
            floats(weights), len(vertex_refs), 5126, "VEC4", 34962)
    texture_paths = texture_paths or {}
    material_textures = material_textures or {}
    texture_indices = {}
    for material, _ in grouped:
        entry = {"name": f"mat_{material}", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0, "roughnessFactor": 1}}
        texture_key = material_textures.get(material)
        if texture_key in texture_paths:
            if texture_key not in texture_indices:
                doc.setdefault("images", []).append({"uri": texture_paths[texture_key].name})
                doc.setdefault("textures", []).append({"source": len(doc["images"]) - 1})
                texture_indices[texture_key] = len(doc["textures"]) - 1
            entry["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_indices[texture_key]}
        doc["materials"].append(entry)
    primitives = []
    for material_index, (_, indices) in enumerate(grouped):
        if indices:
            ia = accessor(b"".join(struct.pack("<I", index) for index in indices), len(indices), 5125, "SCALAR", 34963)
            primitives.append({"attributes": attrs, "indices": ia, "material": material_index, "mode": 4})
    doc["meshes"].append({"name": mesh.object_name or "Character", "primitives": primitives})
    bind_matrices = []
    bone_metadata = bone_metadata or {}
    ordinal_by_index = {bone.index: i for i, bone in enumerate(bones)}
    for bone in bones:
        native = tuple(float(x) for x in bone.matrix)
        if len(native) != 16 or not all(math.isfinite(x) for x in native):
            raise ValueError("Invalid Jade bind matrix.")
        gltf_bind = _glb_mat_mul(_glb_mat_mul(_JADE_TO_GLTF, native), _GLTF_TO_JADE)
        bind_matrices.append(gltf_bind)
        name, parent, key = bone_metadata.get(bone.index, (f"bone_{bone.index}", None, None))
        extras = {"bone_idx": bone.index, "matrix_type": bone.matrix_type,
                  "bind_matrix": list(native)}
        if key is not None:
            extras["jade_key"] = f"0x{key:08X}"
        try:
            node_matrix = _inverse_matrix(gltf_bind)
        except ValueError:
            node_matrix = list((1., 0., 0., 0., 0., 1., 0., 0.,
                                0., 0., 1., 0., 0., 0., 0., 1.))
        doc["nodes"].append({"name": name,
                             "matrix": node_matrix,
                             "extras": extras})
    roots = []
    for ordinal, bone in enumerate(bones):
        parent = bone_metadata.get(bone.index, (None, None, None))[1]
        if parent in ordinal_by_index and parent != bone.index:
            parent_ordinal = ordinal_by_index[parent]
            doc["nodes"][parent_ordinal].setdefault("children", []).append(ordinal)
            doc["nodes"][ordinal]["matrix"] = list(_glb_mat_mul(
                bind_matrices[parent_ordinal], doc["nodes"][ordinal]["matrix"]))
        else:
            roots.append(ordinal)
    ibm = accessor(floats(bind_matrices), len(bones), 5126, "MAT4")
    doc["skins"].append({"joints": list(range(len(bones))), "inverseBindMatrices": ibm,
                         "extras": {"jade_skin_flags": mesh.skin_flags}})
    if len(roots) == 1:
        doc["skins"][0]["skeleton"] = roots[0]
    mesh_node = len(doc["nodes"])
    doc["nodes"].append({"name": mesh.object_name or "Character", "mesh": 0, "skin": 0})
    doc["scenes"].append({"nodes": roots + [mesh_node]})
    doc["buffers"].append({"byteLength": len(blob)})
    encoded = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    blob.extend(b"\0" * (-len(blob) % 4))
    length = 12 + 8 + len(encoded) + 8 + len(blob)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, length) +
                     struct.pack("<I4s", len(encoded), b"JSON") + encoded +
                     struct.pack("<I4s", len(blob), b"BIN\0") + blob)

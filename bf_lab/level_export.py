"""Export the visible level geometry and its textures."""
import json
import re
import struct
from pathlib import Path
from types import SimpleNamespace

from .mesh_uv import _jade_uv_to_standard
from .level_scene import object_basis


def transformed_vertices(source_vertices, obj):
    bx, by, bz = object_basis(obj)
    sx, sy, sz = obj.scale
    px, py, pz = obj.position
    for x, y, z in source_vertices:
        x, y, z = x * sx, y * sy, z * sz
        yield (px + x * bx[0] + y * by[0] + z * bz[0],
               py + x * bx[1] + y * by[1] + z * bz[1],
               pz + x * bx[2] + y * by[2] + z * bz[2])


def vertex_normals(vertices, faces):
    import math
    normals = [[0., 0., 0.] for _ in vertices]
    for face in faces:
        if len(face) != 3 or any(i < 0 or i >= len(vertices) for i in face):
            continue
        a, b, c = (vertices[i] for i in face)
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        normal = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
                  ab[0] * ac[1] - ab[1] * ac[0])
        for index in face:
            for component in range(3):
                normals[index][component] += normal[component]
    return [tuple(v / (math.sqrt(sum(x * x for x in row)) or 1.) for v in row)
            for row in normals]


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "scene"


def _instances(objects, meshes, links):
    by_key = {mesh.key: mesh for mesh in meshes}
    for obj in objects:
        for key in dict.fromkeys(links.get(id(obj), ())):
            mesh = by_key.get(key)
            if mesh and mesh.vertices and mesh.faces:
                yield obj, mesh


def _axis(point, axes):
    x, y, z = point
    return (x, y, z) if axes == "jade" else (x, z, -y)


def _marker(obj):
    bounds = obj.bounds
    if (obj.kind == "trigger" and len(bounds) == 6 and
            all(abs(v) < 100000 for v in bounds) and
            all(bounds[i] < bounds[i + 3] for i in range(3))):
        lo, hi = bounds[:3], bounds[3:]
    else:
        size = 0.3 if obj.kind == "light" else 0.2
        lo, hi = (-size,) * 3, (size,) * 3
    vertices = [(x, y, z) for x in (lo[0], hi[0])
                for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5),
             (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6),
             (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return SimpleNamespace(key=obj.key, vertices=vertices, faces=faces, uvs=[],
                           uv_indices=[], material_ids=[(0, len(faces))],
                           object_name=f"{obj.kind}_{obj.name}")


def export_level_scene(folder, name, fmt, objects, meshes, links, images, texture_maps,
                       export_markers=False, axes="gltf"):
    folder = Path(folder)
    instances = list(_instances(objects, meshes, links))
    if export_markers:
        linked = {id(obj) for obj, _mesh in instances}
        instances.extend((obj, _marker(obj)) for obj in objects if id(obj) not in linked)
    if not instances:
        raise ValueError("The scene has no exportable mesh geometry.")
    stem = _safe_name(Path(name).stem)
    used_keys = {texture_maps.get(mesh.key, {}).get(mat) for _, mesh in instances
                 for mat, _ in mesh.material_ids}
    textures = {}
    for key in sorted(used_keys - {None}):
        image = images.get(key)
        if image is not None:
            path = folder / f"{stem}_tex_{key:08X}.png"
            image.save(path, "PNG")
            textures[key] = path
    if fmt == "obj":
        result = _write_obj(folder, stem, instances, textures, texture_maps, axes)
    elif fmt == "glb":
        result = _write_glb(folder, stem, instances, textures, texture_maps, axes)
    else:
        raise ValueError(f"Unsupported scene format: {fmt}")
    return result, len(instances), len(textures)


def _write_obj(folder, stem, instances, textures, texture_maps, axes):
    obj_path = folder / f"{stem}.obj"
    mtl_path = folder / f"{stem}.mtl"
    lines = ["# Exported by PoP BF Lab", f"mtllib {mtl_path.name}"]
    materials = {}
    vertex_offset = uv_offset = normal_offset = 0
    for ordinal, (obj, mesh) in enumerate(instances):
        vertices = [_axis(v, axes) for v in transformed_vertices(mesh.vertices, obj)]
        normals = vertex_normals(vertices, mesh.faces)
        lines.append(f"o {_safe_name(obj.name)}_{ordinal:04d}_{mesh.key:08X}")
        lines.extend(f"v {x:.9g} {y:.9g} {z:.9g}" for x, y, z in vertices)
        lines.extend(f"vt {u:.9g} {v:.9g}" for u, v in map(_jade_uv_to_standard, mesh.uvs))
        lines.extend(f"vn {x:.9g} {y:.9g} {z:.9g}" for x, y, z in normals)
        cursor = 0
        for mat, count in mesh.material_ids:
            material = f"mat_{mesh.key:08X}_{mat}"
            materials[material] = textures.get(texture_maps.get(mesh.key, {}).get(mat))
            lines.append(f"usemtl {material}")
            for face_index in range(cursor, min(cursor + count, len(mesh.faces))):
                face = mesh.faces[face_index]
                uv_face = mesh.uv_indices[face_index] if face_index < len(mesh.uv_indices) else None
                refs = []
                for corner, vertex in enumerate(face):
                    vi = vertex_offset + vertex + 1
                    ni = normal_offset + vertex + 1
                    if uv_face and uv_face[corner] < len(mesh.uvs):
                        refs.append(f"{vi}/{uv_offset + uv_face[corner] + 1}/{ni}")
                    else:
                        refs.append(f"{vi}//{ni}")
                lines.append("f " + " ".join(refs))
            cursor += count
        vertex_offset += len(vertices)
        normal_offset += len(normals)
        uv_offset += len(mesh.uvs)
    mtl = ["# Exported by PoP BF Lab"]
    for material, texture in materials.items():
        mtl.extend((f"newmtl {material}", "Ka 0.2 0.2 0.2", "Kd 1 1 1", "d 1"))
        if texture:
            mtl.append(f"map_Kd {texture.name}")
        mtl.append("")
    mtl_path.write_text("\n".join(mtl), encoding="utf-8")
    obj_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return obj_path


def _write_glb(folder, stem, instances, textures, texture_maps, axes):
    path = folder / f"{stem}.glb"
    blob = bytearray()
    doc = {"asset": {"version": "2.0", "generator": "PoP BF Lab"},
           "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [],
           "buffers": [], "bufferViews": [], "accessors": [], "materials": []}
    texture_indices = {}
    material_indices = {}

    def accessor(data, count, kind, component, target):
        blob.extend(b"\0" * (-len(blob) % 4))
        offset = len(blob)
        blob.extend(data)
        view = len(doc["bufferViews"])
        doc["bufferViews"].append({"buffer": 0, "byteOffset": offset,
                                    "byteLength": len(data), "target": target})
        index = len(doc["accessors"])
        doc["accessors"].append({"bufferView": view, "componentType": component,
                                 "count": count, "type": kind})
        return index

    for ordinal, (obj, mesh) in enumerate(instances):
        world = [_axis(v, axes) for v in transformed_vertices(mesh.vertices, obj)]
        normals = vertex_normals(world, mesh.faces)
        expanded = {}
        positions, directions, uvs = [], [], []
        primitives = []
        cursor = 0
        for mat, count in mesh.material_ids:
            indices = []
            for face_index in range(cursor, min(cursor + count, len(mesh.faces))):
                face = mesh.faces[face_index]
                uv_face = mesh.uv_indices[face_index] if face_index < len(mesh.uv_indices) else face
                for vertex, uv in zip(face, uv_face):
                    key = (vertex, uv)
                    if key not in expanded:
                        expanded[key] = len(positions)
                        x, y, z = world[vertex]
                        nx, ny, nz = normals[vertex]
                        positions.append((x, y, z))
                        directions.append((nx, ny, nz))
                        uvs.append(mesh.uvs[uv] if uv < len(mesh.uvs) else (0., 0.))
                    indices.append(expanded[key])
            cursor += count
            if not indices:
                continue
            material_key = (mesh.key, mat)
            if material_key not in material_indices:
                entry = {"name": f"mat_{mesh.key:08X}_{mat}",
                         "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1],
                                                  "metallicFactor": 0, "roughnessFactor": 1}}
                texture_key = texture_maps.get(mesh.key, {}).get(mat)
                if texture_key in textures:
                    if texture_key not in texture_indices:
                        doc.setdefault("images", []).append({"uri": textures[texture_key].name})
                        doc.setdefault("textures", []).append({"source": len(doc["images"]) - 1})
                        texture_indices[texture_key] = len(doc["textures"]) - 1
                    entry["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_indices[texture_key]}
                material_indices[material_key] = len(doc["materials"])
                doc["materials"].append(entry)
            primitives.append((indices, material_indices[material_key]))
        if not positions:
            continue
        def floats(rows):
            return b"".join(struct.pack("<" + "f" * len(row), *row) for row in rows)
        attributes = {"POSITION": accessor(floats(positions), len(positions), "VEC3", 5126, 34962),
                      "NORMAL": accessor(floats(directions), len(directions), "VEC3", 5126, 34962),
                      "TEXCOORD_0": accessor(floats(uvs), len(uvs), "VEC2", 5126, 34962)}
        gltf_primitives = []
        for indices, material in primitives:
            index_accessor = accessor(b"".join(struct.pack("<I", i) for i in indices),
                                      len(indices), "SCALAR", 5125, 34963)
            gltf_primitives.append({"attributes": attributes, "indices": index_accessor,
                                    "material": material, "mode": 4})
        mesh_index = len(doc["meshes"])
        doc["meshes"].append({"name": mesh.object_name or f"Mesh {mesh.key:08X}",
                              "primitives": gltf_primitives})
        node_index = len(doc["nodes"])
        doc["nodes"].append({"name": f"{obj.name} {ordinal:04d}", "mesh": mesh_index})
        doc["scenes"][0]["nodes"].append(node_index)
    doc["buffers"].append({"byteLength": len(blob)})
    encoded = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    blob.extend(b"\0" * (-len(blob) % 4))
    length = 12 + 8 + len(encoded) + 8 + len(blob)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, length) +
                     struct.pack("<I4s", len(encoded), b"JSON") + encoded +
                     struct.pack("<I4s", len(blob), b"BIN\0") + blob)
    return path

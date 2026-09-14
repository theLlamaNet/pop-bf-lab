"""GLB/OBJ import, transforms and native mesh replacement payloads."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import base64
import io
import jade_mesh
import json
import math
import struct
from .models import MeshInfo
from .resources import _parse_pop_file_entries


def _read_glb_for_mesh_swap(path: Path) -> tuple[dict, list[bytes]]:
    """Read the self-contained GLB contract used by Jade Toolkit's mesh swap."""
    raw = path.read_bytes()
    if len(raw) < 20 or raw[:4] != b"glTF":
        raise ValueError("Mesh Swap requires a glTF 2.0 binary GLB.")
    _magic, version, declared_size = struct.unpack_from("<4sII", raw, 0)
    if version != 2 or declared_size > len(raw):
        raise ValueError("Invalid or incomplete GLB.")
    pos = 12
    document = None
    buffers: list[bytes] = []
    while pos + 8 <= declared_size:
        length, kind = struct.unpack_from("<I4s", raw, pos)
        pos += 8
        if pos + length > declared_size:
            raise ValueError("GLB chunk extends beyond the end of the file.")
        chunk = raw[pos:pos + length]
        pos += length
        if kind == b"JSON":
            document = json.loads(chunk.decode("utf-8").rstrip())
        elif kind == b"BIN\0":
            buffers.append(chunk)
    if not isinstance(document, dict) or not buffers:
        raise ValueError("The GLB must contain JSON and a binary buffer.")
    return document, buffers


def _glb_accessor_values(document: dict, buffers: list[bytes], accessor_index: int):
    """Bounds-checked glTF accessors, including normalized and sparse data."""
    accessors, views = document.get("accessors", []), document.get("bufferViews", [])
    if not isinstance(accessor_index, int) or not 0 <= accessor_index < len(accessors):
        raise ValueError("GLB accessor not found.")
    accessor = accessors[accessor_index]
    formats = {5120:("b",1),5121:("B",1),5122:("h",2),5123:("H",2),5125:("I",4),5126:("f",4)}
    component_type = accessor.get("componentType")
    components = {"SCALAR":1,"VEC2":2,"VEC3":3,"VEC4":4,"MAT4":16}.get(accessor.get("type"))
    count = accessor.get("count")
    if component_type not in formats or components is None or not isinstance(count,int) or not 0 <= count <= 2000000:
        raise ValueError("Invalid GLB accessor type or count.")
    fmt, component_size = formats[component_type]
    def read_view(view_index, offset, n, fmt, size, components, allow_stride=True):
        if not isinstance(view_index,int) or not 0 <= view_index < len(views):
            raise ValueError("Invalid GLB bufferView.")
        view = views[view_index]
        bi = view.get("buffer",0)
        if not isinstance(bi,int) or not 0 <= bi < len(buffers):
            raise ValueError("GLB buffer unavailable.")
        start, length = view.get("byteOffset",0), view.get("byteLength")
        stride = view.get("byteStride",size*components) if allow_stride else size*components
        if any(not isinstance(x,int) or x<0 for x in (start,length,offset,stride)) or stride < size*components or stride%size:
            raise ValueError("Invalid GLB offset, length or stride.")
        end = offset + ((n-1)*stride+size*components if n else 0)
        if start+length > len(buffers[bi]) or end > length:
            raise ValueError("GLB accessor extends beyond its bufferView.")
        return [struct.unpack_from("<"+fmt*components,buffers[bi],start+offset+i*stride) for i in range(n)]
    if "bufferView" in accessor:
        values = read_view(accessor["bufferView"],accessor.get("byteOffset",0),count,fmt,component_size,components)
    else:
        if accessor.get("byteOffset",0): raise ValueError("Accessor without a bufferView has a nonzero offset.")
        values = [(0,)*components for _ in range(count)]
    if "sparse" in accessor:
        sparse = accessor["sparse"]
        sn = sparse.get("count")
        if not isinstance(sn,int) or not 0 < sn <= count: raise ValueError("Invalid GLB sparse count.")
        si, sv = sparse.get("indices",{}), sparse.get("values",{})
        st = si.get("componentType")
        if st not in (5121,5123,5125): raise ValueError("Invalid sparse index type.")
        sf, ss = formats[st]
        indices = read_view(si.get("bufferView"),si.get("byteOffset",0),sn,sf,ss,1,False)
        replacements = read_view(sv.get("bufferView"),sv.get("byteOffset",0),sn,fmt,component_size,components,False)
        previous = -1
        for (i,),value in zip(indices,replacements):
            if not previous < i < count: raise ValueError("Sparse indices are unsorted or out of range.")
            values[i] = value; previous = i
    limits = {5120:127.,5121:255.,5122:32767.,5123:65535.,5125:4294967295.}
    if accessor.get("normalized") and component_type != 5126:
        values = [tuple(max(-1.,x/limits[component_type]) for x in value) for value in values]
    if any(not math.isfinite(x) for value in values for x in value):
        raise ValueError("GLB accessor contains non-finite values.")
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
                raise ValueError("Invalid GLB texture data URI.") from exc
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
            raise ValueError("GLB node has an invalid matrix.")
        values = tuple(float(value) for value in matrix)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("GLB node has a non-finite matrix.")
        return values
    translation = node.get("translation", [0.0, 0.0, 0.0])
    rotation = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    scale = node.get("scale", [1.0, 1.0, 1.0])
    if (not isinstance(translation, list) or len(translation) != 3 or
            not isinstance(rotation, list) or len(rotation) != 4 or
            not isinstance(scale, list) or len(scale) != 3):
        raise ValueError("GLB node has invalid translation, rotation or scale.")
    tx, ty, tz = (float(value) for value in translation)
    x, y, z, w = (float(value) for value in rotation)
    sx, sy, sz = (float(value) for value in scale)
    if not all(math.isfinite(value) for value in (tx, ty, tz, x, y, z, w, sx, sy, sz)):
        raise ValueError("Non-finite GLB transform.")
    length = math.sqrt(x*x + y*y + z*z + w*w)
    if length < 1e-20:
        raise ValueError("Zero-length GLB rotation.")
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
        raise ValueError("GLB node has zero scale: cannot import the mesh.")
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
        raise ValueError("The GLB contains no meshes.")
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("The GLB node table is invalid.")
    identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    instances = []
    child_nodes: set[int] = set()
    for node in nodes:
        children = node.get("children", []) if isinstance(node, dict) else []
        if not isinstance(children, list) or any(not isinstance(index, int) or not 0 <= index < len(nodes) for index in children):
            raise ValueError("Invalid GLB node hierarchy.")
        child_nodes.update(children)

    def visit(index: int, parent: tuple[float, ...], ancestry: set[int]) -> None:
        if index in ancestry:
            raise ValueError("Cyclic GLB hierarchy.")
        node = nodes[index]
        if not isinstance(node, dict):
            raise ValueError("Invalid GLB node.")
        joint_names = ()
        if "skin" in node:
            skins = document.get("skins", [])
            skin_index = node["skin"]
            if not isinstance(skin_index, int) or not 0 <= skin_index < len(skins):
                raise ValueError("Invalid GLB skin reference.")
            joints = skins[skin_index].get("joints", [])
            if not joints or len(set(joints)) != len(joints) or any(not isinstance(j, int) or not 0 <= j < len(nodes) for j in joints):
                raise ValueError("Invalid GLB joint table.")
            joint_names = tuple(str(nodes[j].get("name") or f"joint_{j}") for j in joints)
            source_joint_names.extend(joint_names)
        world = _glb_mat_mul(parent, _glb_node_matrix(node))
        mesh_index = node.get("mesh")
        if mesh_index is not None:
            if not isinstance(mesh_index, int) or not 0 <= mesh_index < len(meshes):
                raise ValueError("GLB node has an invalid mesh reference.")
            if "weights" in node:
                raise ValueError("GLB morph targets are not supported: export the rest pose.")
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
            raise ValueError("The GLB has no importable root nodes.")
        for root in roots:
            if not isinstance(root, int) or not 0 <= root < len(nodes):
                raise ValueError("GLB scene has an invalid root node.")
            visit(root, identity, set())
    else:
        instances = [(index, identity, str(mesh.get("name") or f"mesh_{index}"), ())
                     for index, mesh in enumerate(meshes) if isinstance(mesh, dict)]
    if not instances:
        raise ValueError("The GLB contains no mesh instances in the active scene.")

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
            raise ValueError("GLB mesh has no valid primitives.")
        determinant = (transform[0]*(transform[5]*transform[10]-transform[9]*transform[6])
                       - transform[4]*(transform[1]*transform[10]-transform[9]*transform[2])
                       + transform[8]*(transform[1]*transform[6]-transform[5]*transform[2]))
        for primitive_index, primitive in enumerate(mesh_document["primitives"]):
            if not isinstance(primitive, dict):
                raise ValueError("Invalid GLB primitive.")
            mode = primitive.get("mode", 4)
            if mode not in (4, 5, 6):
                continue  # Skip points/lines automatically; they cannot form a Jade mesh.
            attrs = primitive.get("attributes", {})
            if not isinstance(attrs, dict) or primitive.get("targets"):
                raise ValueError("Morph targets are not supported: export rest-pose geometry without morphs.")
            position_accessor = attrs.get("POSITION")
            if not isinstance(position_accessor, int):
                raise ValueError("Primitive has no POSITION attribute.")
            positions = _glb_accessor_values(document, buffers, position_accessor)
            if not positions or any(len(position) != 3 for position in positions):
                raise ValueError("POSITION must contain one VEC3 per vertex.")
            texcoords = _glb_accessor_values(document, buffers, attrs["TEXCOORD_0"]) if isinstance(attrs.get("TEXCOORD_0"), int) else []
            imported_normals = _glb_accessor_values(document, buffers, attrs["NORMAL"]) if isinstance(attrs.get("NORMAL"), int) else []
            if imported_normals and (len(imported_normals) != len(positions) or any(len(normal) != 3 for normal in imported_normals)):
                raise ValueError("NORMAL must contain one VEC3 per vertex.")
            if texcoords and (len(texcoords) != len(positions) or any(len(uv) != 2 for uv in texcoords)):
                raise ValueError("TEXCOORD_0 must contain one VEC2 per vertex.")
            indices = (_glb_accessor_values(document, buffers, primitive["indices"])
                       if isinstance(primitive.get("indices"), int)
                       else [(float(index),) for index in range(len(positions))])
            raw_indices = [int(value[0]) for value in indices]
            if any(len(value) != 1 or value[0] != int(value[0]) or not 0 <= int(value[0]) < len(positions) for value in indices):
                raise ValueError("GLB indices are out of range or are not integers.")
            triangles: list[tuple[int, int, int]] = []
            if mode == 4:
                if len(raw_indices) % 3:
                    raise ValueError(f"GLB primitive {primitive_index} is not triangular.")
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
                    raise ValueError("Skinned mesh has no JOINTS_0/WEIGHTS_0 attributes.")
                for joint_attr in (key for key in attrs if key.startswith("JOINTS_")):
                    weight_attr = "WEIGHTS_" + joint_attr[7:]
                    if weight_attr not in attrs:
                        raise ValueError("Matching WEIGHTS attribute is missing.")
                    jvalues = _glb_accessor_values(document, buffers, attrs[joint_attr])
                    wvalues = _glb_accessor_values(document, buffers, attrs[weight_attr])
                    if len(jvalues) != len(positions) or len(wvalues) != len(positions):
                        raise ValueError("JOINTS/WEIGHTS count differs from POSITION.")
                    if any(len(js) != 4 or any(j != int(j) or not 0 <= j < len(joint_names) for j in js) for js in jvalues):
                        raise ValueError("GLB joint indices are out of range.")
                    if any(len(ws) != 4 or any(not math.isfinite(w) or w < 0 for w in ws) for ws in wvalues):
                        raise ValueError("Invalid GLB weights.")
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
        raise ValueError("The GLB contains no importable triangles.")
    label = ", ".join(dict.fromkeys(name for _, _, name, _ in instances))
    mesh = MeshInfo(0, 0, -1, 0, vertices, faces, uvs, uv_indices, material_ids,
                    object_name=label or path.stem, normals=normals,
                    source_joint_names=tuple(dict.fromkeys(source_joint_names)),
                    layout_name="Character GLB" if source_joint_names else "Static mesh GLB")
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
        if n == 0 or not 0 <= result < count: raise ValueError("OBJ index is out of range.")
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
                    raise ValueError("Invalid OBJ coordinates.")
                if op == "v":
                    if len(values) == 4:
                        w = float(values[3])
                        if not math.isfinite(w) or abs(w) < 1e-20: raise ValueError("Zero OBJ homogeneous coordinate.")
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
                    if len(ref) > 3: raise ValueError("Invalid OBJ face reference.")
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
            raise ValueError(f"OBJ line {line_number}: {exc}") from exc
    if not faces: raise ValueError("The OBJ contains no importable faces.")
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
                    normals=normals, layout_name="Static mesh OBJ")
    return mesh, images, textures, colors


def _load_mesh_for_swap(path: Path, obj_axes: str = "Auto"):
    if path.suffix.lower() == ".obj": return _load_obj_mesh_for_swap(path, obj_axes)
    if path.suffix.lower() == ".glb": return _load_glb_mesh_for_swap(path)
    raise ValueError("Unsupported mesh format: choose .glb or .obj.")


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
            raise ValueError("Truncated GAO.")
        _, _, identity, name_len = struct.unpack_from("<4I", payload)
        if not identity & 0x4000:
            continue
        visual = 16 + name_len + 10 + 68 + (48 if identity & 0x80000 else 24)
        if visual + 8 > len(payload):
            raise ValueError("Truncated GAO visual block.")
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
                raise ValueError("Unrecognized geometry group: replacement cancelled.")
            count = group_raw[8]
            key_offset = len(group_raw) - count * 4
            if not 1 <= count <= 6 or key_offset not in (15, 16):
                raise ValueError("Invalid StaticLOD table.")
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
                        raise ValueError("StaticLOD has a nonempty shared RLI: ambiguous color assignment.")
                    break
            continue
        marker = b"\xff\xff" + struct.pack("<I", len(target.vertices))
        pos = payload.find(marker, visual, min(visual + 69, len(payload)))
        if pos < 0:
            continue
        start = pos + 6
        end = start + len(target.vertices) * 4
        if end + 4 > len(payload) or struct.unpack_from("<I", payload, end)[0] != 1:
            raise ValueError("Unrecognized primary RLI table.")
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
                raise ValueError("Truncated expanded RLI buffer.")
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
        raise ValueError("Source mesh has changed: rescan the asset.")
    raw = data[entry.data_offset:entry.data_offset + entry.size]
    candidate = replace(mesh, normals=mesh.normals or _mesh_vertex_normals(mesh.vertices, mesh.faces))
    return jade_mesh.build_replacement(raw, target, candidate)

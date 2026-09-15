"""OBJ serialization preserving native vertex normals and independent UVs."""
from .mesh_import import _mesh_vertex_normals
from .mesh_uv import _jade_uv_to_standard


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

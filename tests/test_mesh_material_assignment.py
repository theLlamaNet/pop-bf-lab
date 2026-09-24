import tempfile
import unittest
from pathlib import Path

from bf_lab.mesh_import import (
    _align_mesh_materials_to_target,
    _load_obj_mesh_for_swap,
    _obj_material_id,
)
from bf_lab.models import MeshInfo


def _mesh(faces, uv_indices, material_ids):
    return MeshInfo(
        0, 0, -1, 0,
        [(float(index), 0.0, 0.0) for index in range(12)],
        faces,
        [(float(index), 0.0) for index in range(12)],
        uv_indices,
        material_ids,
    )


class MeshMaterialAssignmentTests(unittest.TestCase):
    def test_lab_material_names_retain_native_slot_identity(self):
        lookup = {}
        self.assertEqual(_obj_material_id("mat_24", lookup), 24)
        # Blender can suffix a duplicated material datablock with .001.
        self.assertEqual(_obj_material_id("mat_24.001", lookup), 24)
        self.assertGreaterEqual(_obj_material_id("Wall", lookup), 0x40000000)

    def test_loose_parts_are_grouped_by_native_material_not_object_order(self):
        faces = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11)]
        uv_faces = [(9, 10, 11), (6, 7, 8), (3, 4, 5), (0, 1, 2)]
        imported = _mesh(faces, uv_faces, [(9, 1), (4, 1), (9, 1), (4, 1)])
        target = _mesh(faces[:2], uv_faces[:2], [(4, 1), (9, 1)])

        aligned = _align_mesh_materials_to_target(
            imported, target, prefer_native_slots=True)

        self.assertEqual(aligned.material_ids, [(4, 2), (9, 2)])
        self.assertEqual(aligned.faces, [faces[1], faces[3], faces[0], faces[2]])
        self.assertEqual(aligned.uv_indices,
                         [uv_faces[1], uv_faces[3], uv_faces[0], uv_faces[2]])

    def test_non_obj_material_numbers_remain_positional(self):
        faces = [(0, 1, 2), (3, 4, 5)]
        uv_faces = [(0, 1, 2), (3, 4, 5)]
        imported = _mesh(faces, uv_faces, [(9, 1), (4, 1)])
        target = _mesh(faces, uv_faces, [(4, 1), (9, 1)])

        aligned = _align_mesh_materials_to_target(
            imported, target, prefer_native_slots=False)

        self.assertEqual(aligned.material_ids, [(4, 1), (9, 1)])
        self.assertEqual(aligned.faces, faces)

    def test_obj_parser_preserves_repeated_mat_runs_after_object_reordering(self):
        obj = """# Blender export
v 0 0 0
v 1 0 0
v 0 1 0
vt 0 0
vt 1 0
vt 0 1
usemtl mat_9
f 1/1 2/2 3/3
usemtl mat_4
f 1/1 3/3 2/2
usemtl mat_9.001
f 2/2 1/1 3/3
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loose-parts.obj"
            path.write_text(obj, encoding="utf-8")
            mesh, _images, _textures, _colors = _load_obj_mesh_for_swap(path)

        self.assertEqual(mesh.material_ids, [(9, 1), (4, 1), (9, 1)])


if __name__ == "__main__":
    unittest.main()

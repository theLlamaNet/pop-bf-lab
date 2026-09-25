import struct
import json
import tempfile
import unittest
from pathlib import Path

import jade_mesh
from bf_lab.mesh_export import _write_skinned_glb, _character_bone_metadata, _mesh_obj_lines
from bf_lab.mesh_import import _load_glb_mesh_for_swap, _adapt_mesh_skin_to_target, _read_glb_for_mesh_swap, _glb_accessor_values
from bf_lab.models import MeshInfo


IDENTITY = (1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.)


def sample_mesh(bone_index, flags=0):
    return MeshInfo(
        0, 1, 0, 8, [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.)],
        [(0, 1, 2)], [(0., 0.), (1., 0.), (0., 1.)], [(0, 1, 2)],
        [(0, 1)], normals=[(0., 0., 1.)] * 3,
        skin_bones=[jade_mesh.SkinBone(
            bone_index, IDENTITY, 0,
            [(i, jade_mesh.encode_weight(1.)) for i in range(3)])],
        skin_flags=flags)


def raw_geo(mesh, cooked=False):
    payload = bytearray(struct.pack('<10I', 1, 8, 0, 0, 3, 0, 0, 3, 1, 0xC0DE2002))
    payload.extend(jade_mesh.pack_skin(mesh.skin_flags, mesh.skin_bones))
    payload.extend(struct.pack('<I', 1))
    for row in mesh.vertices + mesh.normals:
        payload.extend(struct.pack('<3f', *row))
    for row in mesh.uvs:
        payload.extend(struct.pack('<2f', *row))
    payload.extend(struct.pack('<Ii', 1, 0))
    payload.extend(struct.pack('<6HI', 0, 1, 2, 0, 1, 2, 1))
    if cooked:
        payload.extend(bytes(8))
        payload.extend(struct.pack('<IiI', 1, 0, 1))
        payload.extend(struct.pack('<4I', 8 + 3 * 52, 3, 3, 52))
        for point, normal in zip(mesh.vertices, mesh.normals):
            payload.extend(struct.pack('<6f4H5f', *point, *normal,
                                       mesh.skin_bones[0].index * 3, 0, 0, 0,
                                       1., 0., 0., 0., 0.))
        payload.extend(struct.pack('<I3H', 6, 0, 1, 2))
    return bytes(payload)


def gao_entry(key, name, identity, gro_key=None, father=None, gizmos=()):
    name_bytes = name.encode('ascii') + b'\0'
    payload = bytearray(struct.pack('<4I', 1, 0, identity, len(name_bytes)))
    payload.extend(name_bytes)
    payload.extend(bytes(10 + 68 + 24))
    if identity & 0x4000:
        visual = bytearray(42)
        struct.pack_into('<I', visual, 0, gro_key)
        payload.extend(visual)
    if identity & 0x400000:
        payload.extend(struct.pack('<I', 0xFFFFFFFF if father is None else father))
        payload.extend(bytes(68))
    if identity & 0x200000:
        payload.extend(struct.pack('<I', len(gizmos)))
        for bone_key in gizmos:
            payload.extend(struct.pack('<II', bone_key, 0))
    raw = b'.gao' + payload
    return struct.pack('<III', len(raw), 0, key) + raw


class MeshSkinRoundtripTests(unittest.TestCase):
    def test_character_glb_keeps_original_bone_and_weights(self):
        original = sample_mesh(7, 3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'character.glb'
            _write_skinned_glb(original, path)
            loaded, *_ = _load_glb_mesh_for_swap(path)
        self.assertEqual(loaded.skin_flags, 3)
        self.assertEqual(loaded.skin_bones[0].index, 7)
        self.assertEqual(loaded.skin_bones[0].matrix, IDENTITY)
        self.assertEqual(len(loaded.skin_bones[0].weights), 3)

    def test_character_glb_keeps_resolved_bone_hierarchy(self):
        original = sample_mesh(1)
        original.skin_bones.append(jade_mesh.SkinBone(2, IDENTITY, 0, []))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'character.glb'
            _write_skinned_glb(original, path, bone_metadata={
                1: ('Root', None, 0x1234), 2: ('Child', 1, 0x5678)})
            data = path.read_bytes()
            length, kind = struct.unpack_from('<I4s', data, 12)
            self.assertEqual(kind, b'JSON')
            document = json.loads(data[20:20 + length])
        self.assertEqual(document['nodes'][0]['children'], [1])
        self.assertEqual(document['nodes'][1]['name'], 'Child')
        self.assertEqual(document['nodes'][1]['extras']['jade_key'], '0x00005678')

    def test_nontrivial_bind_matrix_survives_glb_roundtrip(self):
        original = sample_mesh(5)
        matrix = list(IDENTITY)
        matrix[12:15] = [2., 3., 4.]
        original.skin_bones[0].matrix = tuple(matrix)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'character.glb'
            _write_skinned_glb(original, path)
            loaded, *_ = _load_glb_mesh_for_swap(path)
        self.assertEqual(loaded.skin_bones[0].matrix, tuple(matrix))

    def test_replacement_selects_imported_or_target_skin(self):
        target = sample_mesh(1, 2)
        imported = sample_mesh(1, 3)
        matrix = list(IDENTITY)
        matrix[12] = 2.
        imported.skin_bones[0].matrix = tuple(matrix)
        raw = raw_geo(target)
        native = jade_mesh.build_replacement(raw, target, imported,
                                              maintain_original_skin=False)
        retained = jade_mesh.build_replacement(raw, target, imported,
                                                maintain_original_skin=True)
        self.assertEqual(jade_mesh.read_layout(native).bones[0].matrix, tuple(matrix))
        self.assertEqual(jade_mesh.read_layout(native).skin_flags, 3)
        self.assertEqual(jade_mesh.read_layout(retained).bones[0].matrix, IDENTITY)
        self.assertEqual(jade_mesh.read_layout(retained).skin_flags, 2)

    def test_cooked_character_buffer_uses_selected_skin(self):
        target = sample_mesh(1, 2)
        imported = sample_mesh(1, 3)
        # Add a second slot to both skins so the cooked matrix IDs can differ.
        target.skin_bones.append(jade_mesh.SkinBone(7, IDENTITY, 0, []))
        imported.skin_bones[0].weights = []
        imported.skin_bones.append(jade_mesh.SkinBone(7, IDENTITY, 0,
            [(i, jade_mesh.encode_weight(1.)) for i in range(3)]))
        raw = raw_geo(target, cooked=True)
        for keep, expected in ((False, 21), (True, 3)):
            rebuilt = jade_mesh.build_replacement(
                raw, target, imported, maintain_original_skin=keep)
            layout = jade_mesh.read_layout(rebuilt)
            cooked = jade_mesh.read_cooked(rebuilt, layout, imported.material_ids, 1)
            self.assertEqual(struct.unpack_from('<H', cooked['vertices'], 24)[0], expected)

    def test_incompatible_gao_bone_slots_are_rejected(self):
        target = sample_mesh(1)
        imported = sample_mesh(7)
        with self.assertRaisesRegex(ValueError, 'GAO bone slots'):
            jade_mesh.build_replacement(raw_geo(target), target, imported,
                                        maintain_original_skin=False)

    def test_adapt_maps_imported_weights_to_named_target_slots(self):
        target = sample_mesh(1, 2)
        target.skin_bones.append(jade_mesh.SkinBone(3, IDENTITY, 0, []))
        source = sample_mesh(7, 9)
        displaced = list(IDENTITY)
        displaced[12] = 5.
        source.skin_bones[0].matrix = tuple(displaced)
        source.skin_bones[0].weights = [(0, jade_mesh.encode_weight(1.))]
        source.skin_bones.append(jade_mesh.SkinBone(
            8, IDENTITY, 0, [(1, jade_mesh.encode_weight(1.)),
                             (2, jade_mesh.encode_weight(1.))]))
        source.source_bone_names = {7: 'Prince_Arm', 8: 'Prince_Leg'}
        adapted = _adapt_mesh_skin_to_target(source, target, {
            1: ('Prince_Arm', None, 0x10), 3: ('Prince_Leg', 1, 0x20)})
        self.assertEqual([bone.index for bone in adapted.skin_bones], [1, 3])
        self.assertEqual(adapted.source_joint_names, ('Prince_Arm', 'Prince_Leg'))
        self.assertEqual(adapted.skin_bones[0].weights, source.skin_bones[0].weights)
        self.assertEqual(adapted.skin_bones[1].weights, source.skin_bones[1].weights)
        self.assertEqual(adapted.skin_bones[0].matrix, IDENTITY)
        self.assertEqual(adapted.skin_flags, target.skin_flags)
        rebuilt = jade_mesh.build_replacement(raw_geo(target), target, adapted,
                                               maintain_original_skin=False)
        self.assertEqual([bone.index for bone in jade_mesh.read_layout(rebuilt).bones], [1, 3])

    def test_adapt_keeps_unmatched_target_slots_without_weights(self):
        target = sample_mesh(1)
        target.skin_bones.append(jade_mesh.SkinBone(3, IDENTITY, 0, []))
        source = sample_mesh(7)
        source.source_bone_names = {7: 'Arm'}
        adapted = _adapt_mesh_skin_to_target(source, target, {
            1: ('Arm', None, 0x10)})
        self.assertEqual([bone.index for bone in adapted.skin_bones], [1, 3])
        self.assertEqual(len(adapted.skin_bones[0].weights), 3)
        self.assertEqual(adapted.skin_bones[1].weights, [])

    def test_adapt_discards_surplus_bones_and_fills_unweighted_vertices(self):
        target = sample_mesh(1)
        target.skin_bones.append(jade_mesh.SkinBone(3, IDENTITY, 0, []))
        source = sample_mesh(7)
        source.skin_bones[0].weights = [(0, jade_mesh.encode_weight(1.))]
        source.skin_bones.extend([
            jade_mesh.SkinBone(8, IDENTITY, 0, [(1, jade_mesh.encode_weight(1.))]),
            jade_mesh.SkinBone(9, IDENTITY, 0, [(2, jade_mesh.encode_weight(1.))])])
        source.source_bone_names = {7: 'Arm', 8: 'Leg', 9: 'Unused'}
        adapted = _adapt_mesh_skin_to_target(source, target, {
            1: ('Arm', None, 0x10), 3: ('Leg', None, 0x20)})
        self.assertEqual([bone.index for bone in adapted.skin_bones], [1, 3])
        self.assertEqual([vertex for vertex, _ in adapted.skin_bones[0].weights], [0, 2])
        self.assertEqual([vertex for vertex, _ in adapted.skin_bones[1].weights], [1])
        jade_mesh.build_replacement(raw_geo(target, cooked=True), target, adapted,
                                    maintain_original_skin=False)

    def test_character_glb_uv_v_matches_jade_while_obj_is_flipped(self):
        mesh = sample_mesh(1)
        mesh.uvs = [(0.1, 0.2), (0.3, 0.4), (0.5, 0.6)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'character.glb'
            _write_skinned_glb(mesh, path)
            document, buffers = _read_glb_for_mesh_swap(path)
        accessor_index = document['meshes'][0]['primitives'][0]['attributes']['TEXCOORD_0']
        exported = _glb_accessor_values(document, buffers, accessor_index)
        self.assertAlmostEqual(exported[0][1], 0.2)
        self.assertAlmostEqual(exported[2][1], 0.6)
        self.assertIn('vt 0.1 0.8', _mesh_obj_lines(mesh, 'test.mtl'))

    def test_target_bone_names_resolve_from_gao_gizmos(self):
        mesh = sample_mesh(0)
        mesh.key = 0x100
        mesh.skin_bones.append(jade_mesh.SkinBone(1, IDENTITY, 0, []))
        data = (gao_entry(0x10, 'Prince_Arm.gao', 0x400000) +
                gao_entry(0x20, 'Prince_Leg.gao', 0x400000, father=0x10) +
                gao_entry(0x30, 'Prince.gao', 0x4000 | 0x200000 | 0x1000000,
                          gro_key=0x100, gizmos=(0x10, 0x20)))
        metadata = _character_bone_metadata(data, mesh, require_names=True)
        self.assertEqual(metadata[0], ('Prince_Arm', None, 0x10))
        self.assertEqual(metadata[1], ('Prince_Leg', 0, 0x20))

    def test_adapt_matches_existing_slot_ids_before_order(self):
        target = sample_mesh(1)
        target.skin_bones.append(jade_mesh.SkinBone(3, IDENTITY, 0, []))
        source = sample_mesh(3)
        source.skin_bones[0].weights = [(0, jade_mesh.encode_weight(1.))]
        source.skin_bones.append(jade_mesh.SkinBone(
            1, IDENTITY, 0, [(1, jade_mesh.encode_weight(1.))]))
        adapted = _adapt_mesh_skin_to_target(source, target, {
            1: ('Arm', None, 0x10), 3: ('Leg', None, 0x20)})
        self.assertEqual(adapted.skin_bones[0].weights[0], source.skin_bones[1].weights[0])
        self.assertEqual(adapted.skin_bones[1].weights, source.skin_bones[0].weights)

    def test_adapt_rejects_unmatched_joint_order(self):
        target = sample_mesh(1)
        target.skin_bones.append(jade_mesh.SkinBone(3, IDENTITY, 0, []))
        source = sample_mesh(7)
        source.source_bone_names = {7: 'Other'}
        with self.assertRaisesRegex(ValueError, 'No imported joints match'):
            _adapt_mesh_skin_to_target(source, target, {
                1: ('Arm', None, 0x10), 3: ('Leg', None, 0x20)})

    def test_export_fills_unpainted_retail_vertex(self):
        mesh = sample_mesh(1)
        mesh.skin_bones[0].weights = [(0, jade_mesh.encode_weight(1.)),
                                      (1, jade_mesh.encode_weight(1.))]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'partial.glb'
            _write_skinned_glb(mesh, path)
            imported, *_ = _load_glb_mesh_for_swap(path)
        self.assertEqual({vertex for bone in imported.skin_bones for vertex, _ in bone.weights},
                         {0, 1, 2})


if __name__ == '__main__':
    unittest.main()

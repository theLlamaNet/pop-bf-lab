"""Regression tests for real Jade conventions, independent of local BF archives."""
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pop_bf_lab as lab
import jade_mesh as jm

IDENTITY=(1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.)


def triangle():
    return lab.MeshInfo(0,123,0,7,[(0.,0.,0.),(1.,0.,0.),(0.,1.,0.)],[(0,1,2)],
                        [(0.,0.),(1.,0.),(0.,1.)],[(0,1,2)],[(7,1)],normals=[(0.,0.,1.)]*3)


def resource(raw,key=123):
    return struct.pack('<III',len(raw),0xEEFFC099,key)+raw


def original(skinned=False,stride=32,prefix=8,with_cooked=True):
    m=triangle()
    raw=bytearray(struct.pack('<10I',1,7,8,4,3,0,0,3,1,0xC0DE2002 if skinned else 0))
    if skinned:
        raw.extend(struct.pack('<HH',0,2))
        for bi,weights in ((2,[(0,0x3f80),(1,0x3f00)]),(5,[(1,0x3f00),(2,0x3f80)])):
            raw.extend(struct.pack('<HH16fi',bi,len(weights),*IDENTITY,0))
            for vi,word in weights: raw.extend(struct.pack('<HH',vi,word))
    raw.extend(struct.pack('<I',1))
    for v in m.vertices+m.normals: raw.extend(struct.pack('<3f',*v))
    for uv in m.uvs: raw.extend(struct.pack('<2f',*uv))
    raw.extend(struct.pack('<Ii6HI',1,7,0,1,2,0,1,2,1))
    if not with_cooked: return bytes(raw)+bytes(8)
    magic=3 if skinned else 2
    raw.extend(bytes(prefix)+struct.pack('<IiI4I',1,7,1,8+3*stride,magic,3,stride))
    # Independent fixture buffers use actual shipped fields.
    for i in range(3):
        if skinned:
            ids,weights=([6,0,0,0],[1.,0.,0.]) if i==0 else (([6,15,0,0],[.5,.5,0.]) if i==1 else ([15,0,0,0],[1.,0.,0.]))
            raw.extend(struct.pack('<6f4H5f',*m.vertices[i],*m.normals[i],*ids,*weights,*m.uvs[i]))
            if stride==64: raw.extend(struct.pack('<3f',1,0,0))
        else: raw.extend(struct.pack('<8f',*m.vertices[i],*m.normals[i],*m.uvs[i]))
    raw.extend(struct.pack('<I3H',3,0,1,2))
    if prefix==16: raw.extend(bytes(2))
    return bytes(raw)


def write_glb(path,skin=False,transform=None):
    positions=struct.pack('<9f',0,0,0,1,0,0,0,1,0)
    blob=positions
    doc={'asset':{'version':'2.0'},'buffers':[{'byteLength':36}],
         'bufferViews':[{'buffer':0,'byteLength':36}],
         'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3'}],
         'meshes':[{'primitives':[{'attributes':{'POSITION':0}}]}],
         'nodes':[{'mesh':0}], 'scenes':[{'nodes':[0]}], 'scene':0}
    if transform: doc['nodes'][0].update(transform)
    if skin:
        doc['nodes'].append({'name':'Bone'})
        doc['nodes'][0]['skin']=0
        doc['skins']=[{'joints':[1]}]
        for semantic,ctype,fmt,values in [('JOINTS_0',5123,'12H',[0]*12),('WEIGHTS_0',5126,'12f',[1.,0.,0.,0.]*3)]:
            packet=struct.pack('<'+fmt,*values)
            doc['bufferViews'].append({'buffer':0,'byteOffset':len(blob),'byteLength':len(packet)})
            blob+=packet
            ai=len(doc['accessors'])
            doc['accessors'].append({'bufferView':ai,'componentType':ctype,'count':3,'type':'VEC4'})
            doc['meshes'][0]['primitives'][0]['attributes'][semantic]=ai
        doc['buffers'][0]['byteLength']=len(blob)
    encoded=json.dumps(doc).encode(); encoded+=b' '*((-len(encoded))%4)
    blob+=bytes((-len(blob))%4)
    chunks=struct.pack('<I4s',len(encoded),b'JSON')+encoded+struct.pack('<I4s',len(blob),b'BIN\x00')+blob
    path.write_bytes(struct.pack('<4sII',b'glTF',2,len(chunks)+12)+chunks)


class MeshImportTests(unittest.TestCase):
    def test_native_weight_encoding(self):
        self.assertEqual(jm.decode_weight(0x3f80),1.)
        self.assertEqual(jm.decode_weight(0x3f00),.5)
        self.assertEqual(jm.encode_weight(.5),0x3f00)
        with self.assertRaises(ValueError): jm.decode_weight(0xffff)

    def test_static_flags_eight_and_padding(self):
        for prefix in (8,16):
            data=resource(original(prefix=prefix))
            target=lab._scan_pop_meshes(data)[0]
            result=lab._build_static_mesh_replacement(data,target,triangle())
            self.assertEqual(lab._scan_pop_meshes(resource(result))[0].faces,[(0,1,2)])
            self.assertFalse(target.skin_bones)
            self.assertIsNone(target.second_vertices)

    def test_skin_new_topology_and_cooked_weights(self):
        for stride in (52,64):
            data=resource(original(True,stride))
            target=lab._scan_pop_meshes(data)[0]
            mesh=triangle()
            mesh.vertices=[(0.,0.,0.),(.5,0.,0.),(0.,1.,0.),(1.,0.,0.)]
            mesh.normals=[(0.,0.,1.)]*4
            mesh.faces=[(0,1,2),(1,3,2)]; mesh.uv_indices=[(0,1,2)]*2; mesh.material_ids=[(0,2)]
            result=lab._build_static_mesh_replacement(data,target,mesh)
            layout=jm.read_layout(result)
            self.assertEqual([b.index for b in layout.bones],[2,5])
            self.assertEqual([b.matrix for b in layout.bones],[IDENTITY]*2)
            sums=[sum(jm.decode_weight(w) for b in layout.bones for vi,w in b.weights if vi==i) for i in range(4)]
            for total in sums: self.assertAlmostEqual(total,1.,delta=.01)
            # Existing vertex 1 has exactly 50/50, not a guessed nearest index.
            self.assertEqual([w for b in layout.bones for vi,w in b.weights if vi==3],[0x3f00,0x3f00])
            cooked=jm.read_cooked(result,layout,[(7,2)],2)
            self.assertEqual(cooked['stride'],stride)
            for i in range(cooked['count']):
                offset=i*stride
                weights=struct.unpack_from('<3f',cooked['vertices'],offset+32)
                self.assertAlmostEqual(sum(weights),1.,places=6)
                if stride==64:
                    tangent=struct.unpack_from('<3f',cooked['vertices'],offset+52)
                    self.assertAlmostEqual(math.hypot(*tangent),1.,places=5)

    def test_unrecognized_tail_is_not_dropped(self):
        data=resource(original()+b'unknown')
        with self.assertRaisesRegex(ValueError,'Coda GEO'):
            lab._build_static_mesh_replacement(data,triangle(),triangle())

    def test_missing_cooked_buffer_stays_absent(self):
        data=resource(original(with_cooked=False))
        new=lab._build_static_mesh_replacement(data,triangle(),triangle())
        self.assertIsNone(jm.read_cooked(new,jm.read_layout(new),[(7,1)],1))

    def test_float_and_index_validation(self):
        for field,value in [('vertices',[(float('nan'),0.,0.)]*3),('faces',[(0,1,3)])]:
            mesh=triangle(); setattr(mesh,field,value)
            with self.assertRaises(ValueError): lab._build_static_mesh_replacement(resource(original()),triangle(),mesh)

    def test_obj_concave_negative_indices_and_uvs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'polygon.obj'
            path.write_text('# Exported by PoP BF Lab\nv 0 0 0\nv 2 0 0\nv 2 2 0\nv 1 1 0\nv 0 2 0\nvt .2 .3\nf -5/1 -4/1 -3/1 -2/1 -1/1\n')
            mesh,*_=lab._load_mesh_for_swap(path)
            self.assertEqual(len(mesh.faces),3)
            self.assertEqual(mesh.uvs,[(.2,.7)])
            area=sum(abs((mesh.vertices[b][0]-mesh.vertices[a][0])*(mesh.vertices[c][1]-mesh.vertices[a][1])-(mesh.vertices[b][1]-mesh.vertices[a][1])*(mesh.vertices[c][0]-mesh.vertices[a][0]))/2 for a,b,c in mesh.faces)
            self.assertAlmostEqual(area,3.)
            path.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 0 2 3\n')
            with self.assertRaisesRegex(ValueError,'riga 4'): lab._load_mesh_for_swap(path)

    def test_glb_rig_and_transforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rig.glb'
            write_glb(path,True,{'translation':[2,3,4]})
            mesh,*_=lab._load_mesh_for_swap(path)
            self.assertEqual(mesh.source_joint_names,('Bone',))
            self.assertEqual(mesh.vertices[0],(2.,-4.,3.))
            data=resource(original(True,52))
            result=lab._build_static_mesh_replacement(data,lab._scan_pop_meshes(data)[0],mesh)
            self.assertEqual([b.index for b in jm.read_layout(result).bones],[2,5])

    def test_sparse_and_bufferview_bounds(self):
        doc={'bufferViews':[{'byteLength':1},{'byteOffset':4,'byteLength':12}],
             'accessors':[{'componentType':5126,'count':2,'type':'VEC3','sparse':{'count':1,'indices':{'bufferView':0,'componentType':5121},'values':{'bufferView':1}}}]}
        blob=bytes([1,0,0,0])+struct.pack('<3f',2,3,4)
        self.assertEqual(lab._glb_accessor_values(doc,[blob],0),[(0.,0.,0.),(2.,3.,4.)])
        doc['bufferViews'][1]['byteLength']=8
        with self.assertRaisesRegex(ValueError,'bufferView'): lab._glb_accessor_values(doc,[blob],0)

    def test_rli_resize_preserves_unrelated_data(self):
        target=triangle(); candidate=triangle()
        candidate.vertices.append((2.,2.,0.))
        candidate.faces=[(0,1,2),(1,3,2)]
        candidate.uv_indices=[(0,1,2),(1,0,2)]
        candidate.material_ids=[(0,2)]
        payload=struct.pack('<4I',10,0,0x4000,0)+bytes(10+68+24)
        visual=len(payload)
        payload+=struct.pack('<II',123,777)
        marker=len(payload)
        payload+=b'\xff\xff'+struct.pack('<I',3)+b'\x10\x20\x30\xfe'*3+struct.pack('<I',1)
        payload+=struct.pack('<4I',0,8+3*12,3,12)+bytes(3*12)+b'KEEP'
        gao=b'.gao'+payload
        data=resource(original())+resource(gao,456)
        updates=lab._mesh_rli_replacements(data,target,candidate)
        updated=updates[1][4:]
        self.assertEqual(updated[:marker],payload[:marker])
        self.assertEqual(struct.unpack_from('<I',updated,marker+2)[0],4)
        self.assertEqual(updated[-4:],b'KEEP')
        end=marker+6+4*4
        self.assertEqual(struct.unpack_from('<I',updated,end)[0],1)
        self.assertEqual(struct.unpack_from('<I',updated,end+4+8)[0],4)
        self.assertEqual(struct.unpack_from('<II',updated,visual),(123,777))

    def test_static_lod_links_are_preserved(self):
        group=struct.pack('<II',8,0)+bytes([2])+bytes(6)+struct.pack('<II',123,124)
        payload=struct.pack('<4I',10,0,0x4000,0)+bytes(10+68+24)+struct.pack('<II',999,777)
        payload+=b'\xff\xff'+struct.pack('<II',0,1)
        data=resource(original())+resource(group,999)+resource(b'.gao'+payload,456)
        self.assertEqual(lab._mesh_rli_replacements(data,triangle(),triangle()),{})
        # A nonempty shared table is ambiguous and must be rejected.
        payload=payload[:-8]+struct.pack('<I',3)+b'\xff'*12+struct.pack('<I',1)
        data=resource(original())+resource(group,999)+resource(b'.gao'+payload,456)
        with self.assertRaisesRegex(ValueError,'RLI condivisa'):
            lab._mesh_rli_replacements(data,triangle(),triangle())

    def test_fit_does_not_mutate_source(self):
        source=triangle(); target=triangle(); target.vertices=[(10.,10.,10.),(20.,10.,10.),(10.,20.,10.)]
        fitted=jm.fitted_mesh(source,target)
        self.assertEqual(fitted.vertices,target.vertices)
        self.assertEqual(source.vertices[0],(0.,0.,0.))


if __name__=='__main__': unittest.main()

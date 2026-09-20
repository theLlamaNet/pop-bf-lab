import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))
import struct
import unittest
import tempfile
from dataclasses import replace
from PIL import Image
import jade_mesh
from bf_lab.models import MeshInfo
from bf_lab.mesh_import import _mesh_rli_replacements, _build_static_mesh_replacement
from bf_lab.mesh_material_import import import_mesh_materials
from bf_lab.materials import _scan_pop_materials, _associate_mesh_material_packs
from bf_lab.mesh_parser import _scan_pop_meshes
from bf_lab.resources import _parse_pop_file_entries
from bf_lab.archives import _patch_texture_key_in_asset
from bf_lab.textures import _scan_pop_textures, _decode_pop_texture_image, _encode_dxt5
from bf_lab.project import JadeProject


def resource(key, payload):
    return struct.pack('<III', len(payload), 0x12345678, key) + payload


def texture(w=4,h=4,stub=False):
    header=bytearray(56)
    for offset,value in [(0,300),(4,0xFFFFFFFF),(24,0xCAD01234),(32,0xC0DEC0DE),(36,4),(40,7),(44,w),(48,h)]:
        struct.pack_into('<I',header,offset,value)
    struct.pack_into('<hh',header,12,w,h)
    return bytes(header) + (b'\0'*4 if stub else _encode_dxt5(Image.new('RGBA',(w,h),(70,80,90,255)),1))


def fixture():
    vertices=[(0.,0.,0.),(1.,0.,0.),(0.,1.,0.)]
    raw=struct.pack('<11I',1,7,0,0,3,0,0,3,1,0,1)
    raw+=b''.join(struct.pack('<3f',*v) for v in vertices+[(0.,0.,1.)]*3)
    raw+=struct.pack('<6f',0,0,1,0,0,1)+struct.pack('<Ii',1,0)+struct.pack('<6HI',0,1,2,0,1,2,1)
    gao=bytearray(4+16+10+68+24+8)
    gao[:4]=b'.gao'
    struct.pack_into('<I',gao,12,0x4000)
    struct.pack_into('<II',gao,len(gao)-8,100,200)
    gao+=b'\xff\xff'+struct.pack('<I',3)+bytes([30,40,50,254])*3+struct.pack('<I',1)
    material=bytearray(48)
    struct.pack_into('<II',material,0,5,4)
    struct.pack_into('<I',material,44,300)
    data=resource(100,raw)+resource(200,struct.pack('<4I',4,0,1,201))+resource(201,material)+resource(300,texture())+resource(400,gao)
    mesh=_scan_pop_meshes(data)[0]
    _associate_mesh_material_packs(data,[mesh])
    return data,mesh


class RegressionTests(unittest.TestCase):
    def test_materials_that_fit_reuse_keys_without_new_resources(self):
        data,target=fixture()
        mesh=replace(target,material_ids=[(19,1)])
        candidate,updates,additions=import_mesh_materials(
            data,target,mesh,{77:Image.new('RGBA',(8,8),'red')},{19:77},{},{})
        self.assertEqual(additions,[])
        self.assertEqual(candidate.material_ids,[(0,1)])
        self.assertEqual(struct.unpack_from('<I',updates[2],44)[0],300)
        self.assertEqual(struct.unpack_from('<I',updates[3],0)[0],300)

    def test_unused_original_slots_are_filled_before_extending_pack(self):
        data,target=fixture()
        es=_parse_pop_file_entries(data)
        leaf=data[es[2].data_offset:es[2].data_offset+es[2].size]
        pack=resource(200,struct.pack('<5I',4,0,2,201,202))
        data=data[:es[1].offset]+pack+resource(202,leaf)+data[es[1].data_offset+es[1].size:]
        mesh=replace(target,material_ids=[(9,1),(12,1)])
        candidate,updates,additions=import_mesh_materials(
            data,target,mesh,{77:Image.new('RGBA',(8,8),'red')},{9:77,12:77},{},{})
        self.assertEqual(candidate.material_ids,[(0,1),(1,1)])
        self.assertEqual(additions,[])
        self.assertEqual(struct.unpack('<5I',updates[1]),(4,0,2,201,202))

    def test_collider_welds_seams_and_rebuilds_adjacency(self):
        from bf_lab.mesh_collision import build_mesh_collider
        _,mesh=fixture()
        mesh=replace(mesh,vertices=[(0,0,0),(1,0,0),(0,1,0),(1,0,0),(1,1,0),(0,1,0)],
                     faces=[(0,1,2),(3,4,5),(0,0,1)])
        raw=build_mesh_collider(mesh)
        self.assertEqual(struct.unpack_from('<IBBI',raw), (0xFFFFFFFF,5,0,4))
        cursor=10+4*12
        self.assertEqual(struct.unpack_from('<I',raw,cursor)[0],2)
        cursor+=4
        self.assertEqual(struct.unpack_from('<6f',raw,cursor),(0,0,1,0,0,1))
        cursor+=24
        self.assertEqual(struct.unpack_from('<IHBBi',raw,cursor),(1,2,1,0,-1))
        cursor+=12
        self.assertEqual(struct.unpack_from('<6H',raw,cursor),(0,1,2,1,3,2))
        cursor+=12
        self.assertEqual(struct.unpack_from('<6H',raw,cursor),(65535,65535,1,65535,0,65535))
        self.assertEqual(len(raw),cursor+12+4)

    def test_collision_attachment_save_and_replacement(self):
        from bf_lab.mesh_collision import recalculate_mesh_collision, colmap_keys
        data,target=fixture()
        entries=_parse_pop_file_entries(data)
        e=entries[-1]
        raw=bytearray(data[e.data_offset:e.data_offset+e.size])
        struct.pack_into('<I',raw,4,10)
        struct.pack_into('<16f',raw,30,1,0,0,2,0,1,0,3,0,0,1,4,10,20,30,1)
        data=data[:e.offset]+resource(e.key,raw)
        mesh=replace(target,vertices=[(0,0,0),(2,0,0),(0,3,0)])
        changes,additions=recalculate_mesh_collision(data,target,mesh,{})
        self.assertEqual(len(additions),2)
        project=JadeProject()
        project.mesh_patches[0]={i:(entries[i].key,data[entries[i].data_offset:entries[i].data_offset+entries[i].size],v) for i,v in changes.items()}
        project.collision_additions[0]=additions
        result=project.apply_mesh_patches(0,data)
        self.assertEqual(project.apply_mesh_patches(0,result),result)
        es=_parse_pop_file_entries(result)
        gao=next(e for e in es if e.key==400)
        gaoraw=result[gao.data_offset:gao.data_offset+gao.size]
        self.assertEqual(struct.unpack_from('<I',gaoraw,12)[0]&0x2100,0x2100)
        self.assertEqual(struct.unpack_from('<6f',gaoraw,98),(10,20,30,14,29,30))
        map_key=struct.unpack_from('<I',gaoraw,len(gaoraw)-4)[0]
        colmap=next(e for e in es if e.key==map_key)
        keys=colmap_keys(result[colmap.data_offset:colmap.data_offset+colmap.size],{e.key:e for e in es},result)
        self.assertEqual(keys,[additions[1][0]])
        self.assertLess(gao.offset,colmap.offset)
        self.assertLess(colmap.offset,next(e.offset for e in es if e.key==keys[0]))
        second,extra=recalculate_mesh_collision(result,target,mesh,{})
        self.assertEqual(len(second[gao.index]),len(gaoraw))
        self.assertNotEqual(struct.unpack_from('<I',second[gao.index],len(gaoraw)-4)[0],map_key)
        self.assertEqual(len(extra),2)

    def test_legacy_tail_repair_restores_two_texture_passes(self):
        from bf_lab.material_stream import repair_legacy_material_tail, validate_material_resources
        data,target=fixture()
        entries=_parse_pop_file_entries(data)
        # Retail layout: GAO/GEO/materials, texture descriptors, then pixels.
        raw_records=[data[e.offset:e.data_offset+e.size] for e in entries]
        native=b''.join([raw_records[4],raw_records[0],raw_records[1],raw_records[2],
                         resource(300,texture()[:56]),raw_records[3]])
        leaf=bytearray(data[entries[2].data_offset:entries[2].data_offset+entries[2].size])
        struct.pack_into('<I',leaf,44,900)
        tex=struct.pack('<I',300)+texture()[4:]
        broken=(native+resource(900,tex)+resource(901,leaf)+
                resource(902,struct.pack('<4I',4,0,1,901))+
                resource(0x0FF7C0DE,bytes(4)))
        with self.assertRaises(ValueError):
            validate_material_resources(broken,{900,901,902})
        fixed,keys=repair_legacy_material_tail(broken)
        validate_material_resources(fixed,keys)
        es=_parse_pop_file_entries(fixed)
        by_key={key:[e for e in es if e.key==key] for key in keys}
        self.assertEqual(len(by_key[900]),2)
        self.assertLess(by_key[902][0].offset,by_key[901][0].offset)
        self.assertLess(by_key[901][0].offset,by_key[900][0].offset)
        self.assertEqual(by_key[900][0].size,56)
        for e in by_key[900]:
            self.assertEqual(struct.unpack_from('<I',fixed,e.data_offset)[0],900)
        retained=b''.join(fixed[e.offset:e.data_offset+e.size] for e in es if e.key not in keys and e.key!=0x0FF7C0DE)
        self.assertEqual(retained,native)
        with self.assertRaises(ValueError):
            repair_legacy_material_tail(fixed)

    def test_repeated_save_resolves_patch_by_key_after_insertions(self):
        from bf_lab.material_stream import insert_material_resources
        data,target=fixture()
        entries=_parse_pop_file_entries(data)
        gao=entries[-1]
        original=data[gao.data_offset:gao.data_offset+gao.size]
        changed=original[:-4]+struct.pack('<I',2)
        project=JadeProject()
        project.resource_additions[0]={0:(999,entries[0].magic,struct.pack('<3I',4,0,0))}
        project.mesh_patches[0]={gao.index:(gao.key,original,changed)}
        result=project.apply_mesh_patches(0,data)
        self.assertEqual(project.apply_mesh_patches(0,result),result)
        self.assertNotEqual(next(e.index for e in _parse_pop_file_entries(result) if e.key==gao.key),gao.index)

    def test_new_resources_precede_runtime_end_marker(self):
        data,target=fixture()
        footer=resource(0x0FF7C0DE,bytes(4))
        project=JadeProject()
        index=len(_parse_pop_file_entries(data))
        project.resource_additions[0]={index:(999,0x12345678,texture())}
        result=project.apply_mesh_patches(0,data+footer)
        self.assertEqual([e.key for e in _parse_pop_file_entries(result)[-2:]], [999,0x0FF7C0DE])
        self.assertEqual(project.apply_mesh_patches(0,result),result)

    def test_lighting_exact_and_interpolation(self):
        colors=[bytes([20,40,60,253]),bytes([60,80,100,254])]
        got=jade_mesh.transfer_colors([(0,0,0),(2,0,0)],[(0,0,0),(1,0,0)],colors)
        self.assertEqual(got,[colors[0],bytes([40,60,80,253])])

    def test_rli_new_vertices_are_not_white(self):
        data,target=fixture()
        mesh=replace(target,vertices=[(x+.1,y,z) for x,y,z in target.vertices])
        updates=_mesh_rli_replacements(data,target,mesh)
        self.assertEqual(len(updates),1)
        self.assertIn(bytes([30,40,50,254])*3,next(iter(updates.values())))

    def test_resize_surface_preserves_file_params(self):
        data=resource(300,texture())
        pixels=_encode_dxt5(Image.new('RGBA',(8,8),(200,10,20,255)),1)
        result,count=_patch_texture_key_in_asset(data,300,pixels,7,7,4,4,(8,8))
        self.assertEqual(count,1)
        tex=_scan_pop_textures(result)[0]
        self.assertEqual((tex.width,tex.height),(8,8))
        self.assertEqual(struct.unpack_from('<hh',result,tex.offset+12),(4,4))
        self.assertEqual(_decode_pop_texture_image(result,tex).size,(8,8))
        self.assertEqual(struct.unpack_from('<I',result,tex.offset+52)[0],0)

    def test_header_only_and_placeholder_are_synchronized(self):
        pixels=bytes(64)
        for raw in (texture(stub=True),texture()[:56]):
            data=resource(300,raw)
            result,count=_patch_texture_key_in_asset(data,300,pixels,7,7,4,4,(8,8))
            self.assertEqual(count,1)
            self.assertEqual(len(result),len(data))
            self.assertEqual(struct.unpack_from('<II',result,12+44),(8,8))

    def test_invalid_dxt_dimensions(self):
        with self.assertRaises(ValueError):
            _patch_texture_key_in_asset(resource(300,texture()),300,bytes(64),7,7,4,4,(7,8))

    def test_mip_count_matches_payload(self):
        image=Image.new('RGBA',(8,8),(200,10,20,255))
        pixels=_encode_dxt5(image,3)
        result,_=_patch_texture_key_in_asset(resource(300,texture()),300,pixels,7,7,4,4,(8,8))
        self.assertEqual(struct.unpack_from('<I',result,12+52)[0],2)

    def test_untextured_import_keeps_original_materials(self):
        data,mesh=fixture()
        candidate,updates,additions=import_mesh_materials(data,mesh,mesh,{}, {}, {}, {})
        self.assertIs(candidate,mesh)
        self.assertEqual(additions,[])

    def test_extra_slots_resources_save_and_reload(self):
        data,target=fixture()
        mesh=replace(target,faces=target.faces*2,uv_indices=target.uv_indices*2,material_ids=[(4,1),(9,1)])
        candidate,updates,additions=import_mesh_materials(data,target,mesh,{77:Image.new('RGBA',(8,8),'red')},{4:77,9:77},{}, {})
        self.assertEqual(candidate.material_ids,[(0,1),(1,1)])
        self.assertEqual(len(additions),2) # only the excess material and its texture
        updates[0]=_build_static_mesh_replacement(data,target,candidate,True)
        entries=_parse_pop_file_entries(data)
        project=JadeProject()
        project.mesh_patches[0]={i:(entries[i].key,data[entries[i].data_offset:entries[i].data_offset+entries[i].size],raw) for i,raw in updates.items()}
        project.resource_additions[0]={i:(key,entries[0].magic,raw) for i,(key,raw) in enumerate(additions,len(entries))}
        result=project.apply_mesh_patches(0,data)
        self.assertEqual(project.apply_mesh_patches(0,result),result)
        meshes=_scan_pop_meshes(result)
        _associate_mesh_material_packs(result,meshes)
        packs,_=_scan_pop_materials(result)
        self.assertEqual(meshes[0].material_ids,[(0,1),(1,1)])
        self.assertEqual(len(packs[meshes[0].material_pack_key]),2)
        self.assertEqual(len(_scan_pop_textures(result)),2)
        self.assertEqual(meshes[0].material_pack_key, 200)
        self.assertEqual(packs[200][0], 201) # original slot/key reused
        self.assertEqual(sum(e.key==201 for e in _parse_pop_file_entries(result)),1)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'materials.dec'
            output.write_bytes(result)
            reopened = JadeProject()
            reopened.open_dec(output)
            self.assertEqual(reopened.read_asset(reopened.assets[0]), result)

    def test_mixed_materials_keep_untextured_slot(self):
        data,target=fixture()
        mesh=replace(target,faces=target.faces*2,uv_indices=target.uv_indices*2,material_ids=[(4,1),(9,1)])
        candidate,updates,additions=import_mesh_materials(data,target,mesh,{77:Image.new('RGBA',(7,5),'red')},{4:77},{}, {})
        pack=updates[1]
        self.assertEqual(struct.unpack_from('<I',pack,16)[0],201)
        texture_raw=updates[3]
        self.assertEqual(struct.unpack_from('<III',texture_raw,40),(0,7,5))

    def test_obj_attached_texture_is_available_for_apply(self):
        from bf_lab.mesh_import import _load_obj_mesh_for_swap
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            Image.new('RGBA',(8,8),'green').save(root/'albedo.png')
            (root/'mesh.mtl').write_text('newmtl paint\nKd 1 1 1\nmap_Kd albedo.png\n')
            (root/'mesh.obj').write_text('mtllib mesh.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl paint\nf 1 2 3\n')
            mesh,images,textures,colors=_load_obj_mesh_for_swap(root/'mesh.obj')
            self.assertIn(textures[mesh.material_ids[0][0]],images)

if __name__=='__main__': unittest.main()

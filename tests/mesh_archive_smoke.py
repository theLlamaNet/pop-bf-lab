"""Read-only integration test against an explicitly supplied BF and GLB.

Example: python -B tests/mesh_archive_smoke.py --bf ... --asset 0101_Entree --glb ...
Only the optional JSON report is written; all replacement payloads stay in memory.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import struct
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pop_bf_lab as lab
import jade_mesh as jm


def run(bf,asset_match,glb,compression=False):
    project=lab.JadeProject(); project.open_bf(bf)
    assets=[a for a in project.assets if asset_match.lower() in a.name.lower()]
    if len(assets)!=1: raise ValueError(f'Select one asset, found {len(assets)} for {asset_match}')
    asset=assets[0]; original=project.read_asset(asset)
    entries=lab._parse_pop_file_entries(original)
    meshes=lab._scan_pop_meshes(original)
    lab._associate_mesh_material_packs(original,meshes)
    candidate,*_=lab._load_mesh_for_swap(glb)
    successes=Counter(); errors=[]; samples={}; strides=Counter()
    for target in meshes:
        try:
            new=lab._build_static_mesh_replacement(original,target,candidate)
            rli=lab._mesh_rli_replacements(original,target,candidate)
            resource=struct.pack('<III',len(new),0xEEFFC099,target.key)+new
            decoded=lab._scan_pop_meshes(resource)
            assert len(decoded)==1
            decoded=decoded[0]
            assert len(decoded.vertices)==len(candidate.vertices)
            assert decoded.faces==candidate.faces and decoded.uv_indices==candidate.uv_indices
            assert len(decoded.skin_bones or [])==len(target.skin_bones or [])
            kind='character' if target.skin_bones else 'static'
            layout=jm.read_layout(new)
            cooked=jm.read_cooked(new,layout,decoded.material_ids,len(decoded.faces))
            strides[str(cooked['stride']) if cooked else 'no cooked']+=1
            if target.skin_bones:
                for vi in range(len(candidate.vertices)):
                    total=sum(jm.decode_weight(w) for b in layout.bones for v,w in b.weights if v==vi)
                    assert abs(total-1)<.02,(target.key,vi,total)
            successes[kind]+=1
            if kind not in samples:
                samples[kind]=(target,new,rli)
        except Exception as exc:
            errors.append({'key':f'{target.key:08X}','error':str(exc)})
    full_roundtrips=[]
    # Exercise actual project staging, re-read, resource size changes and LZO.
    for kind,(target,new,rli) in samples.items():
        updates={**rli,target.entry_index:new}
        project.mesh_patches[asset.index]={i:(entries[i].key,
             original[entries[i].data_offset:entries[i].data_offset+entries[i].size],payload)
             for i,payload in updates.items()}
        patched=project.read_asset(asset)
        after=lab._parse_pop_file_entries(patched)
        assert len(after)==len(entries)
        for before,e in zip(entries,after):
            assert (before.key,before.magic)==(e.key,e.magic)
            actual=patched[e.data_offset:e.data_offset+e.size]
            expected=updates.get(e.index,original[before.data_offset:before.data_offset+before.size])
            assert actual==expected
        selected=next(m for m in lab._scan_pop_meshes(patched) if m.key==target.key)
        assert selected.faces==candidate.faces
        # Replace a second time using the newly scanned topology and rig.
        repeated=lab._build_static_mesh_replacement(patched,selected,candidate)
        assert lab._scan_pop_meshes(struct.pack('<III',len(repeated),0xEEFFC099,target.key)+repeated)[0].faces==candidate.faces
        if compression:
            encoded=lab.compress_pop_lzo(patched)
            assert lab.decompress_pop_lzo(encoded)==patched
        full_roundtrips.append({'kind':kind,'key':f'{target.key:08X}',
            'bones':len(target.skin_bones or []),'modified_resources':len(updates),
            'unrelated_resources_identical':True,'lzo_roundtrip':compression})
    return {'bf':bf.name,'asset':asset.name,'glb':glb.name,
            'candidate_vertices':len(candidate.vertices),'candidate_faces':len(candidate.faces),
            'mesh_count':len(meshes),'passed':dict(successes),'cooked_strides':dict(strides),
            'errors':errors,'full_asset_roundtrips':full_roundtrips,'game_runtime_tested':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bf',type=Path,required=True)
    parser.add_argument('--asset',required=True)
    parser.add_argument('--glb',type=Path,required=True)
    parser.add_argument('--compression',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    result=run(args.bf,args.asset,args.glb,args.compression)
    output=json.dumps(result,indent=2)
    print(output)
    if args.report: args.report.write_text(output+'\n',encoding='utf-8')
    sys.exit(bool(result['errors']))

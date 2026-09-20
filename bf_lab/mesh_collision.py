"""Retail triangle COB authoring, based on Jade Collision.cpp / OBJload.c."""
import math
import struct
import uuid
from .resources import _parse_pop_file_entries


def build_mesh_collider(mesh, game_material=0xFFFFFFFF):
    # Weld OBJ/GLB seam vertices so adjacency describes physical edges.
    lookup, vertices, remap = {}, [], []
    for v in mesh.vertices:
        p = tuple(v)
        if any(not math.isfinite(x) for x in p):
            raise ValueError('Collision vertices must be finite.')
        if p not in lookup:
            lookup[p] = len(vertices)
            vertices.append(p)
        remap.append(lookup[p])
    if len(vertices) > 65535:
        raise ValueError('A Jade collider supports at most 65535 vertices.')
    faces, normals = [], []
    for triangle in mesh.faces:
        f = tuple(remap[i] for i in triangle)
        if len(set(f)) != 3:
            continue
        a,b,c = (vertices[i] for i in f)
        u,v = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
        normal = (u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
        length = math.hypot(*normal)
        if length < 1e-12:
            continue
        faces.append(f)
        normals.append(tuple(x/length for x in normal))
    if not 1 <= len(faces) <= 65534:
        raise ValueError('A Jade collider needs 1 to 65534 non-degenerate triangles.')
    proximity = [[65535]*3 for _ in faces]
    edges = {}
    for i,(a,b,c) in enumerate(faces):
        for slot,pair in enumerate(((a,b),(a,c),(b,c))):
            edges.setdefault(tuple(sorted(pair)),[]).append((i,slot))
    for neighbours in edges.values():
        if len(neighbours) == 2:
            (a,sa),(b,sb) = neighbours
            proximity[a][sa],proximity[b][sb] = b,a
    # No stale OK3 tree or climb-edge table: recompute adjacency, linear scan.
    raw = bytearray(struct.pack('<IBBI',game_material,5,int(game_material!=0xFFFFFFFF),len(vertices)))
    raw += b''.join(struct.pack('<3f',*v) for v in vertices)
    raw += struct.pack('<I',len(faces))+b''.join(struct.pack('<3f',*n) for n in normals)
    raw += struct.pack('<IHBBi',1,len(faces),1,0,65 if game_material!=0xFFFFFFFF else -1)
    raw += b''.join(struct.pack('<3H',*f) for f in faces)
    raw += b''.join(struct.pack('<3H',*p) for p in proximity)+struct.pack('<I',0)
    return bytes(raw)


def colmap_keys(raw, by_key, data):
    if len(raw) == 4:
        keys = [struct.unpack('<I',raw)[0]]
    elif len(raw) >= 8 and 1 <= raw[0] <= 8 and len(raw) == 4+raw[0]*4:
        keys = list(struct.unpack_from('<'+'I'*raw[0],raw,4))
    else:
        return []
    for key in keys:
        e = by_key.get(key)
        if not e or e.size < 10 or data[e.data_offset+4] not in (1,2,3,5):
            return []
    return keys


def recalculate_mesh_collision(data, target, mesh, updates):
    """Attach a private triangle collider to each direct static GAO instance.

    Existing ColMaps are rebound, so their old geometry is no longer active for
    that object. Instances without a ColMap get one, using retail EXT fields.
    """
    if target.skin_bones:
        raise ValueError('Triangle collision recalculation supports static meshes. Character collision is driven by its rig and collision zones.')
    entries = _parse_pop_file_entries(data)
    by_key = {e.key:e for e in entries}
    used = set(by_key)
    maps = {e.key:colmap_keys(data[e.data_offset:e.data_offset+e.size],by_key,data) for e in entries}
    maps = {key:values for key,values in maps.items() if values}
    changes, additions = dict(updates), []
    def key():
        value = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        while value in used:
            value = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        used.add(value)
        return value
    count = 0
    for e in entries:
        if e.data_type != struct.unpack('<I',b'.gao')[0]:
            continue
        raw = bytearray(changes.get(e.index,data[e.data_offset:e.data_offset+e.size]))
        identity,name_len = struct.unpack_from('<II',raw,12)
        matrix_at = 20+name_len+10
        bounds_at = matrix_at+68
        visual = bounds_at+(48 if identity&0x80000 else 24)
        if not identity&0x4000 or visual+8>len(raw) or struct.unpack_from('<I',raw,visual)[0]!=target.key:
            continue
        version = struct.unpack_from('<I',raw,4)[0]
        if version not in (10,11,12):
            raise ValueError('Unsupported GAO version for collision attachment.')
        # Retail POP records omit the duplicate editor-name tail. Do not edit
        # editor-layout streams using offsets for runtime objects.
        name=bytes(raw[20:20+name_len])
        if name and raw.find(name,20+name_len)>=0:
            raise ValueError('Editor GAO collision layout is not supported.')
        old_cobs=[]
        if identity&0x100:
            hits=[o for o in range(visual+8,len(raw)-3)
                  if struct.unpack_from('<I',raw,o)[0] in maps]
            if len(hits)!=1 or hits[0]!=len(raw)-4:
                raise ValueError('Cannot locate a unique terminal ColMap reference; collision unchanged.')
            old_cobs=maps[struct.unpack_from('<I',raw,hits[0])[0]]
        gmat = 0xCC000228 if 0xCC000228 in by_key else 0xFFFFFFFF
        if old_cobs:
            old=by_key[old_cobs[0]]
            gmat=struct.unpack_from('<I',data,old.data_offset)[0]
        collider_key,map_key = key(),key()
        if identity&0x100:
            struct.pack_into('<I',raw,len(raw)-4,map_key)
        else:
            if not identity&0x2000:
                # group, modifiers, version, sectors, capacities, priority.
                raw += struct.pack('<6I',0xFFFFFFFF,0,0,0,0,0x00007F00)
            raw += struct.pack('<I',map_key)
        struct.pack_into('<I',raw,12,identity|0x2100)
        status_at=20+name_len
        flags=struct.unpack_from('<I',raw,status_at)[0]
        struct.pack_into('<I',raw,status_at,flags|0x80)
        # Refresh broad-phase bounds in global and local object coordinates.
        matrix=struct.unpack_from('<16f',raw,matrix_at)
        scales=[matrix[i*4+3] if abs(matrix[i*4+3])>1e-9 else 1.0 for i in range(3)]
        global_points=[tuple(matrix[12+j]+sum(matrix[i*4+j]*scales[i]*v[i] for i in range(3)) for j in range(3)) for v in mesh.vertices]
        world_bounds=[tuple(fn(p[j] for p in global_points) for j in range(3)) for fn in (min,max)]
        local_bounds=[tuple(fn(p[j] for p in mesh.vertices) for j in range(3)) for fn in (min,max)]
        for i,bounds in enumerate(world_bounds+(local_bounds if identity&0x80000 else [])):
            struct.pack_into('<3f',raw,bounds_at+i*12,*bounds)
        changes[e.index]=bytes(raw)
        additions.extend([(map_key,e.magic,struct.pack('<II',0xFF01,collider_key),e.key),
                          (collider_key,e.magic,build_mesh_collider(mesh,gmat),e.key)])
        count+=1
    if not count:
        raise ValueError('Collision recalculation needs a direct static GAO instance in this asset.')
    return changes, additions


def insert_collision_resources(data, additions):
    if not additions:
        return data
    entries=_parse_pop_file_entries(data)
    by_key={e.key:e for e in entries}
    inserts={}
    for key,magic,raw,host in additions:
        if key in by_key:
            continue
        if host not in by_key:
            raise ValueError('Collider owner is missing from the stream.')
        e=by_key[host]
        inserts.setdefault(e.data_offset+e.size,[]).append(struct.pack('<III',len(raw),magic,key)+raw)
    for offset in sorted(inserts,reverse=True):
        data=data[:offset]+b''.join(inserts[offset])+data[offset:]
    return data

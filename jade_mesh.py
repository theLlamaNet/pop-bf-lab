"""Retail Jade GEO/skin codec. No Blender or third-party runtime dependency.

Layout references: Jade Toolkit Geometry.cpp/Gltf.cpp; Jade GEO_SKIN.c.
Skeleton resources stay in the BF: matrix IDs/bind matrices are retained while
vertex influences are transferred spatially. Unknown topology metadata is refused.
"""
from dataclasses import dataclass, replace
import heapq
import math
import struct


@dataclass
class SkinBone:
    index: int
    matrix: tuple
    matrix_type: int
    weights: list


@dataclass
class GeoLayout:
    header: tuple
    vertex_offset: int
    color_offset: int
    body_end: int
    normals: bool
    skin_flags: int
    bones: list


def read_layout(raw):
    if len(raw) < 44:
        raise ValueError('Truncated GEO header.')
    h = struct.unpack_from('<10I', raw)
    if h[0] != 1 or h[1] not in (7, 8):
        raise ValueError('Retail GEO v7/v8 layout required.')
    nv, nc, colors, nu, ne, marker = h[4:]
    if not 0 < nv <= 500000 or nu > 1000000 or ne > 4096:
        raise ValueError('Invalid GEO counts.')
    pos = 40
    def take(fmt):
        nonlocal pos
        size = struct.calcsize(fmt)
        if pos + size > len(raw):
            raise ValueError('Truncated GEO/skin block.')
        values = struct.unpack_from(fmt, raw, pos)
        pos += size
        return values
    bones, skin_flags = [], 0
    if marker == 0xC0DE2002:
        skin_flags, nb = take('<HH')
        if nb > 4096:
            raise ValueError('Invalid GEO bone count.')
        seen = set()
        for _ in range(nb):
            index, count = take('<HH')
            matrix = take('<16f')
            matrix_type, = take('<i')
            if index in seen or not all(math.isfinite(x) for x in matrix):
                raise ValueError('Duplicate or invalid skin matrix.')
            seen.add(index)
            weights = [take('<HH') for _ in range(count)]
            if any(v >= nv for v, w in weights):
                raise ValueError('Skin vertex index is out of range.')
            bones.append(SkinBone(index, matrix, matrix_type, weights))
    elif marker != 0:
        raise ValueError(f'Unrecognized MRM metadata 0x{marker:08X}: replacement cancelled.')
    has_normals, = take('<I')
    if has_normals not in (0, 1):
        raise ValueError('Unrecognized GEO normals flag.')
    vertex_offset = pos
    color_offset = pos + nv * 12 * (2 if has_normals else 1)
    pos = color_offset + (min(nv, nc) * 4 if colors else 0) + nu * 8
    nf = 0
    for _ in range(ne):
        count, material = take('<Ii')
        nf += count
    if nf > 2000000 or pos + nf * 16 > len(raw):
        raise ValueError('Truncated GEO triangles.')
    return GeoLayout(h, vertex_offset, color_offset, pos + nf * 16,
                     bool(has_normals), skin_flags, bones)


def read_cooked(raw, layout, elements, face_count):
    """Locate the tail structurally; accept shipped unpadded and aligned IBs."""
    tail = raw[layout.body_end:]
    if tail in (b'', bytes(8)):
        return None
    for prefix in (8, 16):
        pos = prefix
        if tail[:prefix] != bytes(prefix) or pos + 4 > len(tail):
            continue
        ne, = struct.unpack_from('<I', tail, pos); pos += 4
        if ne != len(elements) or pos + ne * 8 + 16 > len(tail):
            continue
        actual = [struct.unpack_from('<iI', tail, pos + i * 8) for i in range(ne)]
        if actual != elements:
            continue
        pos += ne * 8
        blob, magic, count, stride = struct.unpack_from('<4I', tail, pos); pos += 16
        if (magic, stride) not in ((0,20),(2,32),(5,32),(6,44),(8,44),(3,52),(3,64)):
            continue
        if not 0 < count <= 65536 or blob != 8 + count * stride:
            continue
        vb_start = pos
        pos += count * stride
        if pos + 4 > len(tail):
            continue
        ni, = struct.unpack_from('<I', tail, pos); pos += 4
        if ni not in (face_count * 3, face_count * 6):
            continue
        end = pos + face_count * 6
        if end > len(tail) or tail[end:] not in (b'', bytes(2)):
            continue
        if any(i[0] >= count for i in struct.iter_unpack('<H', tail[pos:end])):
            continue
        return dict(prefix=prefix, magic=magic, stride=stride,
                    index_bytes=ni == face_count * 6, padding=len(tail)-end,
                    count=count, vertices=tail[vb_start:vb_start+count*stride])
    raise ValueError('Unrecognized GEO tail (LOD/MRM, elements or buffers): no data changed.')


class NearestPoints:
    """Balanced KD tree, avoiding a quadratic all-vertices weight transfer."""
    def __init__(self, points, indices=None):
        self.points = points
        def build(items, depth):
            if not items: return None
            axis = depth % 3
            items.sort(key=lambda i: (points[i][axis], i))
            mid = len(items)//2
            return (items[mid], axis, build(items[:mid], depth+1), build(items[mid+1:], depth+1))
        self.root = build(list(range(len(points))) if indices is None else list(indices), 0)

    def nearest(self, point, count=3):
        heap = []
        def visit(node):
            if node is None: return
            index, axis, left, right = node
            delta = point[axis] - self.points[index][axis]
            near, far = (left, right) if delta <= 0 else (right, left)
            visit(near)
            distance = sum((a-b)**2 for a,b in zip(point, self.points[index]))
            item = (-distance, -index)
            if len(heap) < count: heapq.heappush(heap, item)
            elif item > heap[0]: heapq.heapreplace(heap, item)
            if len(heap) < count or delta*delta <= -heap[0][0]: visit(far)
        visit(self.root)
        return sorted((-d, -i) for d,i in heap)


def decode_weight(word):
    # GEO_SKN_Compress_VP stores a float32, then overwrites its low 16 bits
    # with the vertex index. The second ushort is float bits, NOT UNORM16.
    value = struct.unpack("<f", struct.pack("<I", word << 16))[0]
    if not math.isfinite(value) or not 0 <= value <= 1.01:
        raise ValueError("Invalid Jade weight (16-bit compressed float).")
    return value


def encode_weight(value):
    return struct.unpack("<I", struct.pack("<f", max(0.0, min(1.0, value))))[0] >> 16


def transfer_skin(bones, old_vertices, new_vertices, max_influences=None):
    influences = [{} for _ in old_vertices]
    for bi, bone in enumerate(bones):
        for vi, word in bone.weights:
            weight = decode_weight(word)
            if weight:
                influences[vi][bi] = influences[vi].get(bi, 0.0) + weight
    valid = [i for i,v in enumerate(influences) if v]
    if not valid:
        raise ValueError('The original skin contains no usable weights.')
    tree = NearestPoints(old_vertices, valid)
    result = [replace(b, weights=[]) for b in bones]
    cache = {}
    for vi, vertex in enumerate(new_vertices):
        point = tuple(vertex)
        if point not in cache:
            neighbours = tree.nearest(point)
            if neighbours[0][0] < 1e-18:
                neighbours = neighbours[:1]
            accum = {}
            for distance, old_index in neighbours:
                factor = 1.0/max(distance, 1e-18)
                total = sum(influences[old_index].values())
                for bi,w in influences[old_index].items():
                    accum[bi] = accum.get(bi,0.0) + factor*w/total
            ranked = sorted(accum.items(), key=lambda p: (-p[1], p[0]))
            if max_influences: ranked = ranked[:max_influences]
            total = sum(w for _,w in ranked)
            cache[point] = [(bi, encode_weight(w/total)) for bi,w in ranked]
        for bi,w in cache[point]:
            if w: result[bi].weights.append((vi,w))
    return result


def pack_skin(flags, bones):
    data = bytearray(struct.pack('<HH', flags, len(bones)))
    for bone in bones:
        if len(bone.weights) > 65535:
            raise ValueError('Too many vertices assigned to the same skin matrix.')
        data.extend(struct.pack('<HH16fi', bone.index, len(bone.weights), *bone.matrix, bone.matrix_type))
        for vi,w in bone.weights: data.extend(struct.pack('<HH', vi,w))
    return data


def vertex_tangents(mesh, normals):
    acc = [[0.0]*3 for _ in mesh.vertices]
    for face, uv in zip(mesh.faces, mesh.uv_indices):
        p,q,r = [mesh.vertices[i] for i in face]
        a,b,c = [mesh.uvs[i] for i in uv]
        du1,dv1,du2,dv2 = b[0]-a[0], b[1]-a[1], c[0]-a[0], c[1]-a[1]
        det = du1*dv2-du2*dv1
        if abs(det) < 1e-12: continue
        tangent = [((q[k]-p[k])*dv2-(r[k]-p[k])*dv1)/det for k in range(3)]
        for i in face:
            for k in range(3): acc[i][k] += tangent[k]
    result = []
    for n,t in zip(normals,acc):
        dot = sum(a*b for a,b in zip(n,t))
        t = [t[k]-n[k]*dot for k in range(3)]
        if math.hypot(*t) < 1e-12:
            axis = min(range(3), key=lambda k: abs(n[k]))
            t = [float(k==axis)-n[k]*n[axis] for k in range(3)]
        size = math.hypot(*t) or 1
        result.append(tuple(v/size for v in t))
    return result


def fitted_mesh(mesh, target):
    """Uniform scale and center; never mutate the imported candidate."""
    def bounds(vertices):
        low = [min(p[k] for p in vertices) for k in range(3)]
        high = [max(p[k] for p in vertices) for k in range(3)]
        return [(a+b)/2 for a,b in zip(low,high)], max(b-a for a,b in zip(low,high))
    center, extent = bounds(mesh.vertices)
    target_center, target_extent = bounds(target.vertices)
    if extent < 1e-15 or target_extent < 1e-15:
        raise ValueError('Cannot fit a zero-size mesh.')
    scale = target_extent/extent
    return replace(mesh, vertices=[tuple((p[k]-center[k])*scale+target_center[k] for k in range(3)) for p in mesh.vertices])


def triangulate_polygon(points):
    """Ear clipping for simple planar OBJ n-gons (including concave faces)."""
    if len(points) < 3: raise ValueError('OBJ face has fewer than three vertices.')
    normal = [sum((p[(k+1)%3]-q[(k+1)%3])*(p[(k+2)%3]+q[(k+2)%3])
                  for p,q in zip(points, points[1:]+points[:1])) for k in range(3)]
    drop = max(range(3), key=lambda k: abs(normal[k]))
    axes = [k for k in range(3) if k != drop]
    xy = [(p[axes[0]],p[axes[1]]) for p in points]
    span = max(max(p[k] for p in xy)-min(p[k] for p in xy) for k in (0,1))
    eps = max(span*span*1e-12,1e-30)
    def cross(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    area = sum(p[0]*q[1]-q[0]*p[1] for p,q in zip(xy,xy[1:]+xy[:1]))
    if abs(area) <= eps: raise ValueError('Degenerate OBJ face.')
    sign = 1 if area > 0 else -1
    remaining = list(range(len(points))); result = []
    while len(remaining) > 3:
        for j,b in enumerate(remaining):
            a,c = remaining[j-1],remaining[(j+1)%len(remaining)]
            if sign*cross(xy[a],xy[b],xy[c]) <= eps: continue
            if any(all(sign*cross(xy[u],xy[v],xy[p]) >= -eps for u,v in ((a,b),(b,c),(c,a)))
                   for p in remaining if p not in (a,b,c)): continue
            result.append((a,b,c)); remaining.pop(j); break
        else: raise ValueError('Cannot triangulate OBJ polygon: check for duplicate vertices or self-intersections.')
    result.append(tuple(remaining))
    return result


def build_replacement(raw, target, mesh):
    layout = read_layout(raw)
    kind, version, flags, flags2, nv, nc, colors, nu, ne, marker = layout.header
    if (nv,nu,ne) != (len(target.vertices),len(target.uvs),len(target.material_ids)):
        raise ValueError('The source geometry and the selection do not match: rescan.')
    if not 1 <= len(mesh.vertices) <= 32768 or not 1 <= len(mesh.uvs) <= 32768:
        raise ValueError('Jade supports up to 32768 vertices and UVs in primary geometry.')
    for values,size in ((mesh.vertices,3),(mesh.uvs,2),(mesh.normals or [],3)):
        if any(len(v)!=size or any(not math.isfinite(x) or abs(x)>3.4e38 for x in v) for v in values):
            raise ValueError('Coordinates or normals are invalid for float32.')
    if not mesh.faces or len(mesh.faces)!=len(mesh.uv_indices):
        raise ValueError('Missing faces or UV indices.')
    for faces,count in ((mesh.faces,len(mesh.vertices)),(mesh.uv_indices,len(mesh.uvs))):
        if any(len(f)!=3 or any(not isinstance(i,int) or not 0<=i<count for i in f) for f in faces):
            raise ValueError('Triangle indices are out of range.')
    if not target.material_ids or not mesh.material_ids or any(c<=0 for _,c in mesh.material_ids) or sum(c for _,c in mesh.material_ids)!=len(mesh.faces):
        raise ValueError('Invalid material slots/primitive counts.')
    counts = [c for _,c in mesh.material_ids]
    if len(counts)>ne: counts=counts[:ne-1]+[sum(counts[ne-1:])]
    elements=[(mat, counts[i] if i<len(counts) else 0) for i,(mat,_) in enumerate(target.material_ids)]
    normals = mesh.normals
    if normals is None or len(normals)!=len(mesh.vertices):
        raise ValueError('One normal per vertex is required before serialization.')
    normals=[tuple(x/length for x in n) if (length:=math.hypot(*n))>1e-20 else (0.0,0.0,1.0) for n in normals]
    cooked=read_cooked(raw,layout,target.material_ids,len(target.faces))
    bones=layout.bones
    if cooked and cooked['magic']==3 and not bones:
        raise ValueError('Skinned buffer has no bone assignments.')
    if bones:
        bones=transfer_skin(bones,target.vertices,mesh.vertices,3 if cooked and cooked['magic']==3 else None)
    result=bytearray(struct.pack('<10I',kind,version,flags,flags2,len(mesh.vertices),
                    len(mesh.vertices) if colors else 0,colors,len(mesh.uvs),ne,marker))
    if marker==0xC0DE2002: result.extend(pack_skin(layout.skin_flags,bones))
    result.extend(struct.pack('<I',1))
    for v in mesh.vertices+normals: result.extend(struct.pack('<3f',*v))
    if colors:
        cmap={tuple(round(x,5) for x in target.vertices[i]): raw[layout.color_offset+i*4:layout.color_offset+i*4+4]
              for i in range(min(nv,nc))}
        for v in mesh.vertices: result.extend(cmap.get(tuple(round(x,5) for x in v),b'\xff'*4))
    for uv in mesh.uvs: result.extend(struct.pack('<2f',*uv))
    for mat,count in elements: result.extend(struct.pack('<Ii',count,mat))
    expanded,lookup,indices=[],{},[]
    for face,uv in zip(mesh.faces,mesh.uv_indices):
        result.extend(struct.pack('<6HI',*face,*uv,1))
        for pair in zip(face,uv):
            if pair not in lookup:
                lookup[pair]=len(expanded); expanded.append(pair)
            indices.append(lookup[pair])
    if len(expanded)>65536: raise ValueError('UV seams exceed 65536 vertices in the expanded buffer.')
    if cooked is None:
        result.extend(raw[layout.body_end:])
    else:
        magic,stride=cooked['magic'],cooked['stride']
        tangents=vertex_tangents(mesh,normals) if stride in (44,64) else None
        influences=[[] for _ in mesh.vertices]
        for b in bones:
            if magic==3 and b.index*3>65535: raise ValueError('Matrix ID is out of range for the skin buffer.')
            for vi,w in b.weights: influences[vi].append((b.index,decode_weight(w)))
        result.extend(bytes(cooked['prefix']))
        result.extend(struct.pack('<I',ne))
        for mat,count in elements: result.extend(struct.pack('<iI',mat,count))
        result.extend(struct.pack('<4I',8+len(expanded)*stride,magic,len(expanded),stride))
        for vi,ui in expanded:
            v,n,uv=mesh.vertices[vi],normals[vi],mesh.uvs[ui]
            if magic==3:
                inf=sorted(influences[vi],key=lambda p:(-p[1],p[0]))[:3]
                total=sum(w for _,w in inf)
                if not total: raise ValueError('Vertex has no weights after skin transfer.')
                ids=[bi*3 for bi,w in inf]+[0]*4
                weights=[w/total for bi,w in inf]+[0.0]*3
                result.extend(struct.pack('<6f4H5f',*v,*n,*ids[:4],*weights[:3],*uv))
                if stride==64: result.extend(struct.pack('<3f',*tangents[vi]))
            elif stride==20: result.extend(struct.pack('<5f',*v,*uv))
            else:
                if magic==8: n=(0.0,0.0,0.0)
                result.extend(struct.pack('<8f',*v,*n,*uv))
                if stride==44: result.extend(struct.pack('<3f',*tangents[vi]))
        result.extend(struct.pack('<I',len(indices)*(2 if cooked['index_bytes'] else 1)))
        for i in indices: result.extend(struct.pack('<H',i))
        # Retail prefix-8 packets have no padding; prefix-16 editor packets do.
        if len(indices)%2 and (cooked['padding'] or cooked['prefix']==16): result.extend(bytes(2))
    rebuilt=bytes(result)
    checked=read_layout(rebuilt)
    read_cooked(rebuilt,checked,elements,len(mesh.faces))
    if [(b.index,b.matrix,b.matrix_type) for b in checked.bones] != [(b.index,b.matrix,b.matrix_type) for b in layout.bones]:
        raise ValueError('Skin matrix verification failed.')
    return rebuilt

"""Place imported dependencies in Jade's material/header/pixel load phases."""
import struct
from .resources import _parse_pop_file_entries, _split_pop_footer


def is_texture(raw):
    return (len(raw) >= 56 and raw[4:8] == b'\xff'*4
            and raw[24:28] == struct.pack('<I', 0xCAD01234)
            and raw[32:36] == struct.pack('<I', 0xC0DEC0DE))


def texture_header_size(raw):
    fmt = struct.unpack_from('<I', raw, 40)[0]
    return 60 if fmt in (1, 5) else (64 if fmt == 11 and struct.unpack_from('<I', raw, 36)[0] >= 4 else 56)


def insert_material_resources(data, additions):
    """Insert (key, magic, payload) records; repeat application is idempotent.

    Retail texture loading first consumes descriptors, then pixel records with
    the same key. A flat append creates backward references and omits that first
    pass. Keep shipped records in their original relative order.
    """
    if not additions:
        return data
    body, footer = _split_pop_footer(data)
    entries = _parse_pop_file_entries(body)
    present = {entry.key for entry in entries}
    pending = [(k, m, raw) for k, m, raw in additions if k not in present]
    if not pending:
        return data
    textures = [(e, body[e.data_offset:e.data_offset+e.size]) for e in entries
                if is_texture(body[e.data_offset:e.data_offset+e.size])]
    if not textures:
        raise ValueError('No native texture loading phase found for imported materials.')
    descriptors = [e for e, raw in textures if len(raw) <= texture_header_size(raw)+4]
    material_at = textures[0][0].offset
    descriptor_at = max((e.data_offset+e.size for e in descriptors), default=material_at)
    packs, leaves, headers, pixels = [], [], [], []
    for key, magic, raw in pending:
        raw = bytes(raw)
        def record(payload):
            return struct.pack('<III', len(payload), magic, key)+payload
        if is_texture(raw):
            raw = struct.pack('<I', key)+raw[4:]
            if descriptors:
                headers.append(record(raw[:texture_header_size(raw)]))
            pixels.append(record(raw))
        elif struct.unpack_from('<I', raw)[0] == 4:
            packs.append(record(raw))
        elif struct.unpack_from('<I', raw)[0] == 5:
            leaves.append(record(raw))
        else:
            raise ValueError('Unsupported imported material dependency.')
    inserts = {}
    for offset, blobs in ((material_at, packs+leaves), (descriptor_at, headers), (len(body), pixels)):
        inserts.setdefault(offset, []).extend(blobs)
    for offset in sorted(inserts, reverse=True):
        body = body[:offset]+b''.join(inserts[offset])+body[offset:]
    return body+footer


def validate_material_resources(data, keys):
    """Check new dependency direction, embedded texture keys and both passes."""
    entries = _parse_pop_file_entries(data)
    by_key = {}
    for entry in entries:
        by_key.setdefault(entry.key, []).append(entry)
    from .materials import _scan_pop_materials
    packs, leaves = _scan_pop_materials(data)
    two_pass = any(is_texture(data[e.data_offset:e.data_offset+e.size]) and
                   e.size <= texture_header_size(data[e.data_offset:e.data_offset+e.size])+4
                   for e in entries)
    for key in keys:
        resources = by_key.get(key, [])
        if not resources:
            raise ValueError(f'Missing imported resource 0x{key:08X}.')
        first = resources[0]
        raw = data[first.data_offset:first.data_offset+first.size]
        children = packs.get(key, [leaves[key]] if key in leaves else [])
        for child in children:
            if child not in keys:
                continue  # Existing shared resources may already be cached.
            if child not in by_key or by_key[child][0].offset <= first.offset:
                raise ValueError(f'Invalid Jade load order: 0x{key:08X} -> 0x{child:08X}.')
        if is_texture(raw):
            if two_pass and len(resources) != 2:
                raise ValueError(f'Texture 0x{key:08X} is missing one of its two loading passes.')
            for e in resources:
                if struct.unpack_from('<I', data, e.data_offset)[0] != key:
                    raise ValueError(f'Texture 0x{key:08X} has a mismatched embedded key.')
            if len(resources) == 2 and resources[0].size > texture_header_size(raw)+4:
                raise ValueError('Texture pixels precede their descriptor.')


def repair_legacy_material_tail(data):
    """Repair only the recognizable suffix written by the previous importer.

    Refuse ambiguous layouts; preserve every pre-existing record byte for byte.
    """
    body, footer = _split_pop_footer(data)
    entries = _parse_pop_file_entries(body)
    if not entries or entries[-1].data_type != 4:
        raise ValueError('No legacy imported material pack at the stream tail.')
    from .materials import _scan_pop_materials
    packs, leaves = _scan_pop_materials(body)
    root = entries[-1].key
    children = packs.get(root, [])
    by_key = {e.key: e for e in entries}
    imported_textures = {leaves[k] for k in children if k in leaves and leaves[k] in by_key and
                         is_texture(body[by_key[leaves[k]].data_offset:by_key[leaves[k]].data_offset+by_key[leaves[k]].size]) and
                         struct.unpack_from('<I', body, by_key[leaves[k]].data_offset)[0] != leaves[k]}
    if not imported_textures:
        raise ValueError('No mismatched texture keys from the legacy importer found.')
    new_keys = {root} | imported_textures | {k for k in children if leaves.get(k) in imported_textures}
    first = min(by_key[k].index for k in new_keys)
    tail = entries[first:]
    if {e.key for e in tail} != new_keys or len(tail) != len(new_keys):
        raise ValueError('Legacy material suffix is ambiguous; no automatic repair performed.')
    additions = [(e.key, e.magic, body[e.data_offset:e.data_offset+e.size]) for e in tail]
    result = insert_material_resources(body[:tail[0].offset]+footer, additions)
    validate_material_resources(result, new_keys)
    return result, new_keys

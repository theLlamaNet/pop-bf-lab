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
    fulls = [e for e, raw in textures if e not in descriptors]
    if not fulls:
        raise ValueError('No native full texture loading phase found for imported materials.')
    from .materials import _scan_pop_materials
    material_packs, _ = _scan_pop_materials(body)
    by_key = {entry.key: entry for entry in entries}
    packs, leaves, headers, pixels = [], [], [], []
    for key, magic, raw in pending:
        raw = bytes(raw)
        def record(payload):
            return struct.pack('<III', len(payload), magic, key)+payload
        if is_texture(raw):
            raw = struct.pack('<I', key)+raw[4:]
            if descriptors:
                headers.append((key, record(raw[:texture_header_size(raw)])))
            pixels.append((key, record(raw)))
        elif struct.unpack_from('<I', raw)[0] == 4:
            packs.append(record(raw))
        elif struct.unpack_from('<I', raw)[0] == 5:
            parents = [slots for slots in material_packs.values() if key in slots]
            anchor = None
            if parents:
                siblings = [by_key[slot] for slots in parents for slot in slots
                            if slot in by_key and by_key[slot].data_type == 5]
                if siblings:
                    anchor = max(sibling.data_offset + sibling.size for sibling in siblings)
                    if anchor > material_at:
                        raise ValueError(
                            f'Cannot add material 0x{key:08X}: its pack follows the texture load phase.')
            leaves.append((anchor, key, record(raw)))
        else:
            raise ValueError('Unsupported imported material dependency.')
    def wave_offset(wave, key):
        following = next((entry for entry in wave if entry.key > key), None)
        return following.offset if following else wave[-1].data_offset + wave[-1].size
    inserts = {material_at: [(0, blob) for blob in packs]}
    for anchor, key, blob in leaves:
        inserts.setdefault(anchor if anchor is not None else material_at, []).append((key, blob))
    for wave, records in ((descriptors, headers), (fulls, pixels)):
        for key, blob in records:
            inserts.setdefault(wave_offset(wave, key), []).append((key, blob))
    for offset in sorted(inserts, reverse=True):
        # Equal priorities preserve the dependency order assembled above:
        # private packs first, then their material leaves.  Comparing the blob
        # as a secondary tuple item can put a smaller leaf record ahead of its
        # pack and produces a backward Jade load reference.
        body = body[:offset]+b''.join(
            blob for _key, blob in sorted(inserts[offset], key=lambda item: item[0])
        )+body[offset:]
    return body+footer


def validate_material_resources(data, keys):
    """Check new dependency direction, embedded texture keys and both passes."""
    entries = _parse_pop_file_entries(data)
    by_key = {}
    for entry in entries:
        by_key.setdefault(entry.key, []).append(entry)
    from .materials import _scan_pop_materials
    packs, leaves = _scan_pop_materials(data)
    for pack_key, slots in packs.items():
        if not any(slot in keys for slot in slots):
            continue
        pack_entries = by_key.get(pack_key, [])
        if not pack_entries:
            raise ValueError(f'Missing parent material pack 0x{pack_key:08X}.')
        pack_offset = pack_entries[0].offset
        for slot in slots:
            if slot not in by_key or by_key[slot][0].offset <= pack_offset:
                raise ValueError(f'Invalid Jade pack reference: 0x{pack_key:08X} -> 0x{slot:08X}.')
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
            header_size = texture_header_size(raw)
            full = max(resources, key=lambda entry: entry.size)
            full_raw = data[full.data_offset:full.data_offset+full.size]
            fmt, width, height = struct.unpack_from('<III', full_raw, 40)
            bytes_per_block = {5: 8, 6: 16, 7: 16}.get(fmt)
            expected = None
            if bytes_per_block is not None:
                mip_count = struct.unpack_from('<I', full_raw, 52)[0] + 1
                if not 1 <= mip_count <= 16:
                    raise ValueError(f'Texture 0x{key:08X} has an invalid mip count.')
                expected = 0
                level_width, level_height = width, height
                for _ in range(mip_count):
                    expected += max(1, (level_width + 3) // 4) * max(1, (level_height + 3) // 4) * bytes_per_block
                    level_width, level_height = max(1, level_width // 2), max(1, level_height // 2)
            if expected is not None:
                from .textures import _infer_mip_count
                try:
                    actual_mips = _infer_mip_count(width, height, full.size - header_size, bytes_per_block)
                except ValueError as exc:
                    raise ValueError(
                        f'Texture 0x{key:08X} payload does not match its format/dimensions.') from exc
                if actual_mips < mip_count:
                    raise ValueError(f'Texture 0x{key:08X} has fewer pixel levels than its header.')


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

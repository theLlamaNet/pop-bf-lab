"""Create local Jade material/texture resources for imported mesh slots."""
from dataclasses import replace
import struct
import uuid

from .materials import _scan_pop_materials, _scan_pop_material_records
from .resources import _parse_pop_file_entries
from .textures import _scan_pop_textures, _encode_dxt5


def import_mesh_materials(data, target, mesh, images, texture_map, colors, updates):
    """Return remapped geometry, resource updates and appended (key, raw) pairs.

    Reuse native slots and keys, retaining each game's rendering flags. Only
    excess slots or conflicting texture assignments require new resources.
    """
    slots = list(dict.fromkeys(slot for slot, _ in mesh.material_ids))
    if len(mesh.material_ids) > 4096:
        raise ValueError("Jade supports at most 4096 geometry elements.")
    if not any(texture_map.get(slot) in images for slot in slots):
        return mesh, updates, []
    entries = _parse_pop_file_entries(data)
    by_key = {e.key: e for e in entries}
    packs, _ = _scan_pop_materials(data)
    records = _scan_pop_material_records(data)
    original = packs.get(target.material_pack_key, [])
    if not original and target.material_pack_key in records:
        original = [target.material_pack_key]
    templates = [key for key in original if key in records and records[key].texture_offset is not None]
    textures = _scan_pop_textures(data)
    if not templates or not textures:
        raise ValueError("Cannot import materials: native material/texture templates are missing from this asset.")
    used = set(by_key)
    additions = []

    def add(raw):
        key = uuid.uuid4().int & 0xFFFFFFFF
        while key in used or key in (0, 0xFFFFFFFF):
            key = uuid.uuid4().int & 0xFFFFFFFF
        used.add(key)
        from .material_stream import is_texture
        if is_texture(raw):
            raw = struct.pack("<I", key) + raw[4:]
        additions.append((key, bytes(raw)))
        return key

    from .archives import _patch_texture_key_in_asset
    updates = dict(updates)
    texture_cache, texture_owners, material_owners = {}, {}, set()
    material_keys = list(original)
    native_slots = list(dict.fromkeys(max(0, slot) for slot, _ in target.material_ids
                                      if max(0, slot) < len(original)))
    native_slots += [slot for slot in range(len(original)) if slot not in native_slots]
    remap = {}
    working = data
    for position, slot in enumerate(slots):
        reuse_slot = position < len(native_slots) and native_slots[position] < len(original)
        native_slot = native_slots[position] if reuse_slot else len(material_keys)
        native_key = original[native_slot] if reuse_slot else templates[-1]
        destination_key = native_key if reuse_slot else None
        if native_key not in records or records[native_key].texture_offset is None:
            native_key = templates[0]
        image_key = texture_map.get(slot)
        remap[slot] = native_slot
        if image_key not in images:
            if not reuse_slot:
                material_keys.append(native_key)
            continue
        factor = colors.get(slot, (1., 1., 1., 1.))
        cache_key = (image_key, tuple(factor), records[native_key].texture_key if reuse_slot else None)
        if cache_key not in texture_cache:
            image = images[image_key].convert("RGBA")
            if factor != (1., 1., 1., 1.):
                from PIL import Image
                image = Image.merge("RGBA", tuple(channel.point(
                    [round(i * max(0., min(1., factor[c]))) for i in range(256)])
                    for c, channel in enumerate(image.split())))
            w, h = image.size
            if not (1 <= w <= 8192 and 1 <= h <= 8192):
                raise ValueError("Imported texture dimensions must be between 1 and 8192.")
            template = next((t for t in textures if t.key == records[native_key].texture_key), textures[0])
            fmt = 7 if w % 4 == 0 and h % 4 == 0 else 0
            pixels = _encode_dxt5(image, 1) if fmt == 7 else image.tobytes("raw", "BGRA")
            can_reuse = (reuse_slot and template.key == records[native_key].texture_key
                         and template.key not in texture_owners
                         and any(t.key == template.key and t.data_end-t.data_offset > 8 for t in textures))
            if can_reuse:
                working, count = _patch_texture_key_in_asset(
                    working, template.key, pixels, template.texture_type, fmt,
                    template.width, template.height, (w, h))
                if not count:
                    raise ValueError("Original texture could not be replaced.")
                texture_cache[cache_key] = template.key
                texture_owners[template.key] = cache_key
            else:
                header = bytearray(data[template.offset:template.offset+56])
                struct.pack_into("<hh", header, 12, w, h)
                struct.pack_into("<4I", header, 40, fmt, w, h, 0)
                texture_cache[cache_key] = add(header+pixels)
        entry = by_key[native_key]
        raw = bytearray(data[entry.data_offset:entry.data_offset+entry.size])
        version = struct.unpack_from("<I", raw, 4)[0]
        if 4 <= version <= 9:
            struct.pack_into("<I", raw, 12, 0xFFFFFFFF)
        struct.pack_into("<I", raw, records[native_key].texture_offset-entry.data_offset,
                         texture_cache[cache_key])
        if reuse_slot and destination_key not in material_owners:
            updates[by_key[destination_key].index] = bytes(raw)
            material_owners.add(destination_key)
            material_keys[native_slot] = destination_key
        else:
            key = add(raw)
            if reuse_slot:
                material_keys[native_slot] = key
            else:
                material_keys.append(key)
    # Retain the GAO's GRM key and all existing slots. Only excess slots extend
    # the existing pack, preserving the runtime's original dependency anchors.
    pack_entry = by_key.get(target.material_pack_key)
    if pack_entry is None:
        raise ValueError("The original material pack is missing.")
    if pack_entry.data_type == 4:
        updates[pack_entry.index] = (data[pack_entry.data_offset:pack_entry.data_offset+8] +
                                     struct.pack("<I", len(material_keys)) +
                                     struct.pack("<"+"I"*len(material_keys), *material_keys))
    elif len(material_keys) != 1:
        raise ValueError("A direct single material cannot be expanded without a native material pack.")
    working_entries = _parse_pop_file_entries(working)
    for before, after in zip(entries, working_entries):
        raw = working[after.data_offset:after.data_offset+after.size]
        if raw != data[before.data_offset:before.data_offset+before.size]:
            updates[before.index] = raw
    return replace(mesh, material_ids=[(remap[slot], count) for slot, count in mesh.material_ids]), updates, additions

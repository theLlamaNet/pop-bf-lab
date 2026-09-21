"""Apply imported textures to the material slots already owned by a mesh."""
from dataclasses import replace
import struct
import uuid

from .materials import _scan_pop_materials, _scan_pop_material_import_records
from .resources import _parse_pop_file_entries
from .textures import _scan_pop_textures, _encode_dxt5


def import_mesh_materials(data, target, mesh, images, texture_map, colors, updates):
    """Replace native mesh material slots without extending Jade material packs.

    Jade's resource loader is sensitive to material-pack growth and to extra
    geometry elements. Therefore only existing GRM slots and geometry elements
    are used. Imported groups beyond that capacity are folded into the final
    native element and do not create material resources or pack entries.
    """
    if len(mesh.material_ids) > 4096:
        raise ValueError("Jade supports at most 4096 geometry elements.")
    if not any(texture_map.get(slot) in images for slot, _ in mesh.material_ids):
        return mesh, updates, []
    entries = _parse_pop_file_entries(data)
    by_key = {entry.key: entry for entry in entries}
    packs, _ = _scan_pop_materials(data)
    textures = _scan_pop_textures(data)
    records = _scan_pop_material_import_records(data, {texture.key for texture in textures})
    original = packs.get(target.material_pack_key, [])
    if not original and target.material_pack_key in records:
        original = [target.material_pack_key]
    if not original or not textures:
        raise ValueError("Cannot replace materials: native material/texture templates are missing from this asset.")

    element_count = len(target.material_ids)
    if not element_count:
        raise ValueError("Cannot replace materials: the selected mesh has no material elements.")
    groups = list(mesh.material_ids[:element_count])
    if len(mesh.material_ids) > element_count:
        groups[-1] = (groups[-1][0], groups[-1][1] + sum(count for _, count in mesh.material_ids[element_count:]))

    native_slots = []
    for slot, _ in target.material_ids:
        if 0 <= slot < len(original) and slot not in native_slots:
            native_slots.append(slot)
    native_slots.extend(slot for slot in range(len(original)) if slot not in native_slots)
    usable_slots = [slot for slot in native_slots if original[slot] in records and
                    records[original[slot]].texture_offset is not None and original[slot] in by_key]
    if not usable_slots:
        raise ValueError("Cannot replace materials: no editable native material slot was found.")

    used_keys, additions = set(by_key), []
    texture_cache, texture_is_opaque = {}, {}
    texture_donor = max(textures, key=lambda texture: texture.data_end - texture.data_offset)

    def add_texture(raw):
        key = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        while key in used_keys:
            key = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        used_keys.add(key)
        additions.append((key, struct.pack("<I", key) + raw[4:]))
        return key

    def texture_for(image_key, factor):
        cache_key = (image_key, tuple(factor))
        if cache_key in texture_cache:
            return texture_cache[cache_key], texture_is_opaque[cache_key]
        image = images[image_key].convert("RGBA")
        if factor != (1., 1., 1., 1.):
            from PIL import Image
            image = Image.merge("RGBA", tuple(channel.point(
                [round(value * max(0., min(1., factor[index]))) for value in range(256)])
                for index, channel in enumerate(image.split())))
        if factor[3] >= 1.0:
            image.putalpha(255)
        source_w, source_h = image.size
        if not (1 <= source_w <= 8192 and 1 <= source_h <= 8192):
            raise ValueError("Imported texture dimensions must be between 1 and 8192.")
        def nearest_power_of_two(value):
            lower = 1 << (value.bit_length() - 1)
            upper = min(8192, lower << 1)
            return lower if value - lower <= upper - value else upper
        width, height = nearest_power_of_two(source_w), nearest_power_of_two(source_h)
        if (width, height) != image.size:
            from PIL import Image
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        header = bytearray(data[texture_donor.offset:texture_donor.offset + 56])
        struct.pack_into("<4I", header, 40, 7, width, height, 0)
        texture_cache[cache_key] = add_texture(header + _encode_dxt5(image, 1))
        texture_is_opaque[cache_key] = image.getextrema()[3][0] == 255
        return texture_cache[cache_key], texture_is_opaque[cache_key]

    updates, result_groups = dict(updates), []
    for position, (source_slot, count) in enumerate(groups):
        native_slot = usable_slots[min(position, len(usable_slots) - 1)]
        native_key = original[native_slot]
        image_key = texture_map.get(source_slot)
        result_groups.append((native_slot, count))
        if image_key not in images:
            continue
        entry = by_key[native_key]
        raw = bytearray(data[entry.data_offset:entry.data_offset + entry.size])
        texture_key, opaque = texture_for(image_key, colors.get(source_slot, (1., 1., 1., 1.)))
        version = struct.unpack_from("<I", raw, 4)[0]
        if 4 <= version <= 9 and opaque:
            struct.pack_into("<I", raw, 12, 0xFFFFFFFF)
            struct.pack_into("<f", raw, 24, 1.0)
            flags_offset = 38 if version in (8, 9) else 28
            if flags_offset + 4 <= len(raw):
                flags = struct.unpack_from("<I", raw, flags_offset)[0]
                flags &= ~((0xF << 16) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 10))
                struct.pack_into("<I", raw, flags_offset, flags)
        struct.pack_into("<I", raw, records[native_key].texture_offset - entry.data_offset, texture_key)
        updates[entry.index] = bytes(raw)
    return replace(mesh, material_ids=result_groups), updates, additions

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
    geometry elements. Therefore only existing GRM slots are used. Keep every
    OBJ run here: the geometry pass later regroups its faces into the fixed
    native element list, and prematurely folding runs would attach a texture
    to the wrong faces after Blender reorders loose objects.
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
    groups = list(mesh.material_ids)

    native_slots = []
    for slot, _ in target.material_ids:
        if 0 <= slot < len(original) and slot not in native_slots:
            native_slots.append(slot)
    native_slots.extend(slot for slot in range(len(original)) if slot not in native_slots)
    usable_slots = [slot for slot in native_slots if original[slot] in records and
                    records[original[slot]].texture_offset is not None and original[slot] in by_key]
    if not usable_slots:
        raise ValueError("Cannot replace materials: no editable native material slot was found.")

    textured_sources = list(dict.fromkeys(
        slot for slot, _ in groups if texture_map.get(slot) in images))
    if len(textured_sources) > len({original[slot] for slot in usable_slots}):
        raise ValueError(
            "Cannot replace all materials: the imported mesh uses more textured materials "
            "than the target has distinct editable native materials. Choose a target with "
            "more material slots or combine imported materials first.")

    used_keys, additions = set(by_key), []
    texture_cache, texture_is_opaque = {}, {}
    dxt5_donors = [texture for texture in textures if texture.texture_type == 7
                   and texture.format == "DXT5"]
    if not dxt5_donors:
        raise ValueError("Cannot replace materials: this asset has no native DXT5 texture template.")
    texture_donor = dxt5_donors[0]

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
        # Jade stores dimensions twice: the 16-bit surface size at +12 and
        # the 32-bit pixel size at +44. Leaving the donor surface size here
        # makes the game allocate/decode a different surface from the payload.
        struct.pack_into("<hh", header, 12, width, height)
        # Native Jade DXT5 records keep levels down to 8x8 (or 4x4 for
        # smaller surfaces); +52 stores the number of extra mip levels.
        mip_count = max(1, max(width, height).bit_length() - 3)
        struct.pack_into("<4I", header, 40, 7, width, height, mip_count - 1)
        # Retail rectangular surfaces carry extra small mip levels until the
        # compressed pixel stream reaches a 64-byte boundary. These bytes are
        # image blocks, not zero padding.
        pixel_mip_count = mip_count
        while pixel_mip_count < 16:
            level_sizes = [(max(1, width >> level), max(1, height >> level))
                           for level in range(pixel_mip_count)]
            byte_count = sum(max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16
                             for w, h in level_sizes)
            if byte_count % 64 == 0:
                break
            pixel_mip_count += 1
        pixels = _encode_dxt5(image, pixel_mip_count)
        texture_cache[cache_key] = add_texture(header + pixels)
        texture_is_opaque[cache_key] = image.getextrema()[3][0] == 255
        return texture_cache[cache_key], texture_is_opaque[cache_key]

    # Stable names (mat_N) map directly.  For arbitrary material names, assign
    # each distinct source material once rather than using the run position;
    # the same material can occur in several non-contiguous OBJ groups.
    stable_native_slots = "OBJ" in mesh.layout_name
    direct_slots = ({source_slot for source_slot, _ in groups
                     if source_slot in usable_slots}
                    if stable_native_slots else set())
    fallback_slots = [slot for slot in usable_slots if slot not in direct_slots]
    fallback_slots.extend(slot for slot in usable_slots if slot in direct_slots)
    fallback_by_source = {}
    assigned_textures = {}
    updates, result_groups = dict(updates), []
    for position, (source_slot, count) in enumerate(groups):
        # PoP BF Lab OBJ exports name materials mat_N.  Blender can reorder
        # objects/groups after Separate by Loose Parts, so retain that native
        # slot identity instead of assigning textures by encounter order.
        if stable_native_slots and source_slot in usable_slots:
            native_slot = source_slot
        else:
            if source_slot not in fallback_by_source:
                fallback_by_source[source_slot] = fallback_slots[
                    min(len(fallback_by_source), len(fallback_slots) - 1)]
            native_slot = fallback_by_source[source_slot]
        native_key = original[native_slot]
        image_key = texture_map.get(source_slot)
        result_groups.append((native_slot, count))
        if image_key not in images:
            continue
        previous_image = assigned_textures.get(native_key)
        if previous_image is not None and previous_image != image_key:
            raise ValueError(
                f"Cannot replace all materials: native material 0x{native_key:08X} "
                "is shared by different imported textures.")
        assigned_textures[native_key] = image_key
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

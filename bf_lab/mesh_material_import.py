"""Import mesh textures and, when requested, extend native material packs."""
from dataclasses import replace
import struct
import uuid

from .materials import _scan_pop_materials, _scan_pop_material_import_records
from .resources import _parse_pop_file_entries
from .textures import _scan_pop_textures, _encode_dxt5


def material_source_aliases(mesh, texture_map, colors):
    """Treat duplicated GLB materials with the same image/factor as one slot."""
    by_appearance = {}
    aliases = {}
    for source, count in mesh.material_ids:
        if count <= 0 or source in aliases:
            continue
        appearance = (texture_map.get(source),
                      tuple(colors.get(source, (1., 1., 1., 1.))))
        aliases[source] = by_appearance.setdefault(appearance, source)
    return aliases


def _set_material_alpha_mode(raw: bytearray, alpha_mode: str) -> None:
    version = struct.unpack_from("<I", raw, 4)[0]
    if not 4 <= version <= 9:
        return
    struct.pack_into("<f", raw, 24, 1.0)
    flags_offset = 38 if version in (8, 9) else 28
    if flags_offset + 4 > len(raw):
        return
    flags = struct.unpack_from("<I", raw, flags_offset)[0]
    flags &= ~((0xF << 16) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 9) | (1 << 10))
    if alpha_mode == "cutout":
        flags &= ~(0x3F << 24)
        flags |= 1 << 4
    elif alpha_mode == "blend":
        flags |= 1 << 16
    struct.pack_into("<I", raw, flags_offset, flags)


def import_mesh_materials(data, target, mesh, images, texture_map, colors, updates,
                          add_excess=False):
    """Replace material images, preserving source runs until GEO regrouping."""
    if len(mesh.material_ids) > 4096:
        raise ValueError("Jade supports at most 4096 geometry elements.")
    if not add_excess and not any(texture_map.get(slot) in images for slot, _ in mesh.material_ids):
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

    if not target.material_ids:
        raise ValueError("Cannot replace materials: the selected mesh has no material elements.")
    aliases = material_source_aliases(mesh, texture_map, colors)
    groups = [(aliases.get(slot, slot), count) for slot, count in mesh.material_ids]

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
    excess_sources = set(textured_sources[len({original[slot] for slot in usable_slots}):])

    used_keys, additions = set(by_key), []
    texture_cache, texture_alpha_mode = {}, {}
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

    def new_key():
        key = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        while key in used_keys:
            key = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        used_keys.add(key)
        return key

    def texture_for(image_key, factor):
        cache_key = (image_key, tuple(factor))
        if cache_key in texture_cache:
            return texture_cache[cache_key], texture_alpha_mode[cache_key]
        image = images[image_key].convert("RGBA")
        if factor != (1., 1., 1., 1.):
            from PIL import Image
            image = Image.merge("RGBA", tuple(channel.point(
                [round(value * max(0., min(1., factor[index]))) for value in range(256)])
                for index, channel in enumerate(image.split())))
        source_alpha = image.getchannel("A").histogram()
        # Jade's character materials normally use alpha test. PNGs exported
        # from BF often contain broad intermediate alpha values; blending all
        # of them makes the whole character translucent in the game.
        alpha_mode = ("opaque" if sum(source_alpha[:255]) == 0 else
                      "blend" if factor[3] < 0.98 else "cutout")
        # baseColorFactor alpha multiplies the PNG alpha; an opaque factor
        # must not erase cutouts already present in the image.
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
            if alpha_mode == "cutout":
                image.putalpha(image.getchannel("A").point(
                    [0 if value < 128 else 255 for value in range(256)]))
        header = bytearray(data[texture_donor.offset:texture_donor.offset + 56])
        if alpha_mode != "opaque":
            # This word contains TEX_FP settings, not TEX_uw runtime flags.
            # Keep quality/mipmap bits and request alpha-aware mipmaps.
            texture_flags = struct.unpack_from("<H", header, 8)[0]
            struct.pack_into("<H", header, 8, texture_flags | 0x10)
        # Jade stores dimensions twice: the 16-bit surface size at +12 and
        # the 32-bit pixel size at +44. Leaving the donor surface size here
        # makes the game allocate/decode a different surface from the payload.
        struct.pack_into("<hh", header, 12, width, height)
        # Native Jade DXT5 records keep levels down to 8x8 (or 4x4 for
        # smaller surfaces); +52 stores the number of extra mip levels.
        mip_count = max(1, max(width, height).bit_length() - 3)
        struct.pack_into("<4I", header, 40, 7, width, height, mip_count - 1)
        pixels = _encode_dxt5(image, mip_count)
        texture_cache[cache_key] = add_texture(header + pixels)
        texture_alpha_mode[cache_key] = alpha_mode
        return texture_cache[cache_key], alpha_mode

    # Stable names (mat_N) map directly.  For arbitrary material names, assign
    # each distinct source material once rather than using the run position;
    # the same material can occur in several non-contiguous OBJ groups.
    if add_excess:
        pack_entry = by_key.get(target.material_pack_key)
        if pack_entry is None or pack_entry.data_type != 4:
            raise ValueError("This mesh has no editable multi-material pack for excess materials.")
        sources = list(dict.fromkeys(slot for slot, count in groups if count > 0))
        distinct_slots = list(dict.fromkeys(original[slot] for slot in usable_slots))
        # A shared leaf can only represent one source texture. Use its first slot.
        reusable = list(dict.fromkeys(
            next(slot for slot in usable_slots if original[slot] == key)
            for key in distinct_slots))
        source_slots = {}
        claimed_slots = set()
        if "OBJ" in mesh.layout_name:
            for source in sources:
                if source in reusable and source not in claimed_slots:
                    source_slots[source] = source
                    claimed_slots.add(source)
        free_slots = [slot for slot in reusable if slot not in claimed_slots]
        for source in sources:
            if source not in source_slots and free_slots:
                source_slots[source] = free_slots.pop(0)
        extra_sources = [source for source in sources if source not in source_slots]
        extra_count = len(extra_sources)
        if len(original) + extra_count > 255:
            raise ValueError("The Jade engine supports at most 255 slots in a multi-material pack.")
        template_key = original[usable_slots[0]]
        template = by_key[template_key]
        template_record = records[template_key]
        template_raw = data[template.data_offset:template.data_offset + template.size]
        source_slots.update({source: len(original) + index
                             for index, source in enumerate(extra_sources)})
        result_groups = [(source_slots[source], count) for source, count in groups]
        updates = dict(updates)
        new_leaf_keys = []
        for source_slot in sources:
            image_key = texture_map.get(source_slot)
            factor = colors.get(source_slot, (1., 1., 1., 1.))
            if image_key not in images:
                from PIL import Image
                image_key = new_key()
                images = dict(images)
                images[image_key] = Image.new("RGBA", (4, 4), (255, 255, 255, 255))
            texture_key, alpha_mode = texture_for(image_key, factor)
            if source_slots[source_slot] < len(original):
                leaf_key = original[source_slots[source_slot]]
                leaf_entry = by_key[leaf_key]
                leaf_record = records[leaf_key]
                leaf = bytearray(data[leaf_entry.data_offset:leaf_entry.data_offset + leaf_entry.size])
                offset = leaf_record.texture_offset - leaf_entry.data_offset
            else:
                leaf_key = new_key()
                leaf = bytearray(template_raw)
                offset = template_record.texture_offset - template.data_offset
            struct.pack_into("<I", leaf, offset, texture_key)
            _set_material_alpha_mode(leaf, alpha_mode)
            if source_slots[source_slot] < len(original):
                updates[leaf_entry.index] = bytes(leaf)
            else:
                additions.append((leaf_key, bytes(leaf)))
                new_leaf_keys.append(leaf_key)
        if new_leaf_keys:
            pack_raw = bytearray(data[pack_entry.data_offset:pack_entry.data_offset + pack_entry.size])
            if len(pack_raw) != 12 + 4 * len(original):
                raise ValueError("Unexpected multi-material pack layout; no materials changed.")
            struct.pack_into("<I", pack_raw, 8, len(original) + len(new_leaf_keys))
            pack_raw.extend(struct.pack("<" + "I" * len(new_leaf_keys), *new_leaf_keys))
            updates[pack_entry.index] = bytes(pack_raw)
        return replace(mesh, material_ids=result_groups), updates, additions

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
        if image_key not in images or source_slot in excess_sources:
            continue
        previous_image = assigned_textures.get(native_key)
        if previous_image is not None and previous_image != image_key:
            raise ValueError(
                f"Cannot replace all materials: native material 0x{native_key:08X} "
                "is shared by different imported textures.")
        assigned_textures[native_key] = image_key
        entry = by_key[native_key]
        raw = bytearray(data[entry.data_offset:entry.data_offset + entry.size])
        texture_key, alpha_mode = texture_for(image_key, colors.get(source_slot, (1., 1., 1., 1.)))
        _set_material_alpha_mode(raw, alpha_mode)
        struct.pack_into("<I", raw, records[native_key].texture_offset - entry.data_offset, texture_key)
        updates[entry.index] = bytes(raw)
    return replace(mesh, material_ids=result_groups), updates, additions

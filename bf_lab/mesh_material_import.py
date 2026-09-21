"""Create local Jade material/texture resources for imported mesh slots."""
from dataclasses import replace
import struct
import uuid

from .materials import _scan_pop_materials, _scan_pop_material_import_records
from .resources import _parse_pop_file_entries
from .textures import _scan_pop_textures, _encode_dxt5


def import_mesh_materials(data, target, mesh, images, texture_map, colors, updates):
    """Return remapped geometry, resource updates and appended (key, raw) pairs.

    Clone native leaf layouts to retain each game's rendering contract, while
    appending new slots without mutating any native material or texture.
    """
    slots = list(dict.fromkeys(slot for slot, _ in mesh.material_ids))
    if len(mesh.material_ids) > 4096:
        raise ValueError("Jade supports at most 4096 geometry elements.")
    if not any(texture_map.get(slot) in images for slot in slots):
        return mesh, updates, []
    entries = _parse_pop_file_entries(data)
    by_key = {e.key: e for e in entries}
    packs, _ = _scan_pop_materials(data)
    textures = _scan_pop_textures(data)
    records = _scan_pop_material_import_records(data, {texture.key for texture in textures})
    original = packs.get(target.material_pack_key, [])
    if not original and target.material_pack_key in records:
        original = [target.material_pack_key]
    templates = [key for key in original if key in records and records[key].texture_offset is not None]
    if not templates or not textures:
        raise ValueError("Cannot import materials: native material/texture templates are missing from this asset.")
    used = set(by_key)
    additions = []
    # Jade Toolkit seeds every added TEX from the largest full texture in the
    # stream.  The first two shorts are logical sampling dimensions and must
    # remain those of that donor; only the cooked POT surface fields change.
    texture_donor = max(textures, key=lambda texture: texture.data_end - texture.data_offset)

    def add(raw):
        key = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        while key in used:
            key = 0x7A000000 | (uuid.uuid4().int & 0xFFFFFF)
        used.add(key)
        from .material_stream import is_texture
        if is_texture(raw):
            raw = struct.pack("<I", key) + raw[4:]
        additions.append((key, bytes(raw)))
        return key

    updates = dict(updates)
    texture_cache, texture_is_opaque = {}, {}
    # Preserve every native slot byte-for-byte and append imported materials
    # after it.  Shared GRMs are common in retail levels: replacing their
    # existing slots changes unrelated scene objects.  Creating a brand-new
    # GRM is not safe either, because speed-mode loads material packs in an
    # earlier wave and a late private pack resolves to a null runtime object.
    # Extending the native pack is the same operation used by Jade Toolkit's
    # set_multi_material_slot and keeps all existing element indices stable.
    material_keys = list(original)
    native_slots = list(dict.fromkeys(max(0, slot) for slot, _ in target.material_ids
                                      if max(0, slot) < len(original)))
    native_slots += [slot for slot in range(len(original)) if slot not in native_slots]
    remap = {}
    for position, slot in enumerate(slots):
        reuse_slot = position < len(native_slots) and native_slots[position] < len(original)
        native_slot = native_slots[position] if reuse_slot else native_slots[-1]
        native_key = original[native_slot] if reuse_slot else templates[-1]
        image_key = texture_map.get(slot)
        if image_key in images and (native_key not in records or records[native_key].texture_offset is None):
            native_key = templates[0]
        entry = by_key.get(native_key)
        if entry is None or entry.data_type != 5:
            native_key = templates[0]
            entry = by_key[native_key]
        raw = bytearray(data[entry.data_offset:entry.data_offset+entry.size])
        if image_key not in images:
            key = add(raw)
            remap[slot] = len(material_keys)
            material_keys.append(key)
            continue
        factor = colors.get(slot, (1., 1., 1., 1.))
        cache_key = (image_key, tuple(factor))
        if cache_key not in texture_cache:
            image = images[image_key].convert("RGBA")
            if factor != (1., 1., 1., 1.):
                from PIL import Image
                image = Image.merge("RGBA", tuple(channel.point(
                    [round(i * max(0., min(1., factor[c]))) for i in range(256)])
                    for c, channel in enumerate(image.split())))
            if factor[3] >= 1.0:
                image.putalpha(255)
            source_w, source_h = image.size
            if not (1 <= source_w <= 8192 and 1 <= source_h <= 8192):
                raise ValueError("Imported texture dimensions must be between 1 and 8192.")
            def nearest_power_of_two(value):
                lower = 1 << (value.bit_length()-1)
                upper = min(8192, lower << 1)
                return lower if value-lower <= upper-value else upper
            w, h = nearest_power_of_two(source_w), nearest_power_of_two(source_h)
            if (w, h) != image.size:
                from PIL import Image
                image = image.resize((w, h), Image.Resampling.LANCZOS)
            # Toolkit's add_texture default is format 7 (DXT5).  Using one
            # layout for both opaque and alpha images also avoids changing the
            # four-byte DXT1 prefix contract based on image contents.
            fmt = 7
            pixels = _encode_dxt5(image, 1)
            header = bytearray(data[texture_donor.offset:texture_donor.offset+56])
            # Keep the donor's logical TEX dimensions. The cooked surface has
            # its own POT dimensions, exactly as Jade Toolkit's add_texture.
            struct.pack_into("<4I", header, 40, fmt, w, h, 0)
            texture_cache[cache_key] = add(header+pixels)
            texture_is_opaque[cache_key] = image.getextrema()[3][0] == 255
        version = struct.unpack_from("<I", raw, 4)[0]
        if 4 <= version <= 9:
            struct.pack_into("<I", raw, 12, 0xFFFFFFFF)
            if texture_is_opaque[cache_key]:
                struct.pack_into("<f", raw, 24, 1.0)
                flags_offset = 38 if version in (8, 9) else 28
                if flags_offset + 4 <= len(raw):
                    flags = struct.unpack_from("<I", raw, flags_offset)[0]
                    flags &= ~((0xF << 16) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 10))
                    struct.pack_into("<I", raw, flags_offset, flags)
        struct.pack_into("<I", raw, records[native_key].texture_offset-entry.data_offset,
                         texture_cache[cache_key])
        key = add(raw)
        remap[slot] = len(material_keys)
        material_keys.append(key)

    pack_entry = by_key.get(target.material_pack_key)
    if pack_entry is None or pack_entry.data_type != 4:
        raise ValueError("Cannot append imported materials: the mesh has no native multi-material pack.")
    updates[pack_entry.index] = (
        data[pack_entry.data_offset:pack_entry.data_offset + 8]
        + struct.pack("<I", len(material_keys))
        + struct.pack("<" + "I" * len(material_keys), *material_keys)
    )
    return replace(mesh, material_ids=[(remap[slot], count) for slot, count in mesh.material_ids]), updates, additions

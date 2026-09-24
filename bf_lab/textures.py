"""POP texture discovery, image conversion and DDS/TGA codecs."""
from __future__ import annotations

from pathlib import Path
import io
import struct
from .models import TextureInfo
from .resources import _parse_pop_file_entries


def _scan_pop_textures(data: bytes) -> list[TextureInfo]:
    """Find Jade texture entries, including PAL8, DXT1, DXT3 and 4-bit formats."""
    textures: list[TextureInfo] = []
    for entry in _parse_pop_file_entries(data):
        if entry.size < 56 or entry.data_type is None:
            continue
        # FileEntry data begins with four bytes before the Jade texture header.
        if (struct.unpack_from("<I", data, entry.data_offset + 4)[0] != 0xFFFFFFFF
                or struct.unpack_from("<I", data, entry.data_offset + 24)[0] != 0xCAD01234
                or struct.unpack_from("<I", data, entry.data_offset + 32)[0] != 0xC0DEC0DE):
            continue
        width, height = struct.unpack_from("<hh", data, entry.data_offset + 12)
        texture_type = struct.unpack_from("<I", data, entry.data_offset + 40)[0]
        version = struct.unpack_from("<I", data, entry.data_offset + 36)[0]
        if texture_type not in (0, 1, 5, 6, 7, 11) or not (1 <= width <= 8192 and 1 <= height <= 8192):
            continue
        data_offset = entry.data_offset + 56
        # Demo type-0 entries with version 0x20000 have a 54-byte header.
        # Their final two bytes at +54 are already the first image pixel;
        # skipping 56 bytes makes a 32x32 BGRA surface appear 2 bytes short.
        if texture_type == 0 and version == 0x20000:
            data_offset -= 2
        if texture_type == 1:
            data_offset += 4
        elif texture_type == 11 and version >= 4:
            data_offset += 8
        data_end = entry.data_offset + entry.size
        if data_end <= data_offset:
            continue
        actual_w, actual_h = struct.unpack_from("<II", data, entry.data_offset + 44)
        # Jade Toolkit uses these actual dimensions directly; the old /2 rule
        # made modded textures appear missing or with a wrong data contract.
        stored_w, stored_h = (actual_w, actual_h) if 1 <= actual_w <= 8192 and 1 <= actual_h <= 8192 else (width, height)
        payload_size = data_end - data_offset
        blocks = max(1, (stored_w + 3) // 4) * max(1, (stored_h + 3) // 4)
        if texture_type == 7:
            fmt = "Raw BGRA8 (reference)" if payload_size == 4 else ("Raw BGRA8" if payload_size == stored_w * stored_h * 4 else "DXT5")
        elif texture_type == 6:
            fmt = "DXT3" if payload_size >= blocks * 16 else "DXT3 (truncated)"
        elif texture_type == 5:
            fmt = "DXT1" if payload_size >= blocks * 8 else "DXT1 (truncated)"
        elif texture_type == 0:
            fmt = "TGA/BGR24" if payload_size == stored_w * stored_h * 3 else ("TGA/BGRA8" if payload_size >= stored_w * stored_h * 4 else "Type 0 (truncated)")
        elif texture_type == 1:
            fmt = "8-bit palette"
        else:
            fmt = "4-bit grayscale"
        textures.append(TextureInfo(len(textures), entry.data_offset, data_offset, data_end,
                                    stored_w, stored_h, texture_type, entry.key, fmt,
                                    stored_w, stored_h))
    return textures


def _decode_pop_texture_image(data: bytes, tex: TextureInfo):
    """Return a PIL RGBA image for every previewable POP texture type."""
    from PIL import Image
    if tex.texture_type == 7:
        blob = _dds_blob_for_dump(data, tex)
    elif tex.texture_type in (5, 6):
        blob = _compressed_dds_for_preview(bytes(data[tex.data_offset:tex.data_end]), tex)
    elif tex.texture_type == 0:
        pixel_bytes = tex.storage_width * tex.storage_height
        payload = data[tex.data_offset:tex.data_end]
        depth = 32 if len(payload) >= pixel_bytes * 4 else 24
        blob = _build_tga_header(tex.storage_width, tex.storage_height, depth) + payload[:pixel_bytes * (depth // 8)]
    elif tex.texture_type == 1:
        entries = _parse_pop_file_entries(data)
        if tex.data_offset < 4:
            raise ValueError("Palette texture has an invalid offset.")
        palette_id = struct.unpack_from("<I", data, tex.data_offset - 4)[0]
        palette_entry = next((entry for entry in entries if entry.key == palette_id), None)
        if palette_entry is None:
            raise ValueError(f"Palette 0x{palette_id:08X} not found.")
        palette = data[palette_entry.data_offset + 4:palette_entry.data_offset + palette_entry.size]
        blob = _build_palette_tga(tex.width, tex.height, palette, data[tex.data_offset:tex.data_end])
    elif tex.texture_type == 11:
        blob = _build_4bit_tga(tex.width, tex.height, data[tex.data_offset:tex.data_end])
    else:
        raise ValueError(f"Unsupported POP texture format {tex.texture_type}.")
    return Image.open(io.BytesIO(blob)).convert("RGBA")


def _dds_payload_and_info(path: Path) -> tuple[bytes, int, int, str, bytes]:
    """Read a standard DDS and return compressed payload plus dimensions/format."""
    raw = path.read_bytes()
    if len(raw) < 128 or raw[:4] != b"DDS ":
        raise ValueError("The imported file is not a valid DDS.")
    height, width = struct.unpack_from("<II", raw, 12)
    fourcc = raw[84:88].decode("ascii", errors="replace").strip("\0")
    if not width or not height or not fourcc:
        raise ValueError("DDS is missing dimensions or a compression FourCC.")
    return raw[128:], width, height, fourcc, raw[:128]


def _build_dds(raw_payload: bytes, width: int, height: int, header_template: bytes) -> bytes:
    header = bytearray(header_template)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    return bytes(header) + raw_payload


def _dds_blob_for_dump(texture_data: bytes | bytearray, tex: TextureInfo) -> bytes:
    """Build a standalone DDS from the compressed bytes in a POP texture entry."""
    if tex.texture_type not in (5, 6, 7):
        raise ValueError("Only POP DXT1, DXT3 or DXT5 textures can be dumped as DDS.")
    payload = bytes(texture_data[tex.data_offset:tex.data_end])
    if tex.texture_type in (5, 6):
        bytes_per_block = 8 if tex.texture_type == 5 else 16
        try:
            mip_count = _infer_mip_count(tex.storage_width, tex.storage_height, len(payload), bytes_per_block)
        except ValueError:
            # Native POP type-5 entries can end with a four-byte container
            # trailer after the BC1 levels. It is not DDS image data, and its
            # value is not guaranteed to be zero. Accept it only when removing
            # exactly one DWORD produces a complete mip chain.
            if tex.texture_type != 5 or len(payload) <= 4:
                raise
            dds_payload = payload[:-4]
            mip_count = _infer_mip_count(
                tex.storage_width, tex.storage_height, len(dds_payload), bytes_per_block
            )
        else:
            dds_payload = payload
        return _build_dds(
            dds_payload, tex.storage_width, tex.storage_height,
            _build_dds_header(tex.storage_width, tex.storage_height, mip_count - 1, tex.texture_type),
        )
    if len(payload) == tex.storage_width * tex.storage_height * 4:
        return _build_tga_header(tex.storage_width, tex.storage_height, 32) + payload
    return _build_type7_dds(payload, tex.storage_width, tex.storage_height)


def _build_dds_header(width: int, height: int, num_mipmaps: int, compression: int) -> bytes:
    """Create a standard 128-byte DDS header without relying on PopTools."""
    if compression not in (0, 1, 2, 5, 6, 7, 11):
        raise ValueError(f"Unsupported POP DDS compression: {compression}.")
    mip_count = max(1, num_mipmaps + 1)
    flags = 0x0002100F if mip_count == 1 else 0x000A1007
    caps = 0x00001000 if mip_count == 1 else 0x00401008
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, flags)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 28, mip_count)
    struct.pack_into("<I", header, 76, 32)
    struct.pack_into("<I", header, 80, 0x00000004)
    fourcc = {2: b"DXT1", 5: b"DXT1", 6: b"DXT3", 7: b"DXT5", 11: b"DXT5"}.get(compression)
    if fourcc is not None:
        header[84:88] = fourcc
    else:
        # 32-bit BGRA fallback for the uncompressed POP formats.
        struct.pack_into("<I", header, 80, 0x00000041)
        struct.pack_into("<I", header, 88, 32)
        struct.pack_into("<I", header, 92, 0x00FF0000)
        struct.pack_into("<I", header, 96, 0x0000FF00)
        struct.pack_into("<I", header, 100, 0x000000FF)
        struct.pack_into("<I", header, 104, 0xFF000000)
    struct.pack_into("<I", header, 108, caps)
    return bytes(header)


def _infer_dxt5_mip_count(width: int, height: int, payload_size: int) -> int:
    """Infer the number of DXT5 mip levels from the embedded payload size."""
    total = 0
    mip_count = 0
    w, h = width, height
    while mip_count < 16:
        total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16
        mip_count += 1
        if total == payload_size:
            return mip_count
        if total > payload_size or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    raise ValueError(
        f"Original DXT5 payload of {payload_size:,} B does not match a whole number of mipmaps for {width}x{height}."
    )


def _infer_mip_count(width: int, height: int, payload_size: int, bytes_per_block: int) -> int:
    """Infer mip levels for a block-compressed POP payload."""
    total = 0
    w, h = width, height
    for mip_count in range(1, 17):
        total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bytes_per_block
        if total == payload_size:
            return mip_count
        if total > payload_size or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    raise ValueError("The payload does not match a complete mipmap chain.")


def _full_mip_count(width: int, height: int) -> int:
    count = 1
    while width > 1 or height > 1:
        width, height = max(1, width // 2), max(1, height // 2)
        count += 1
    return count


def _dxt5_payload_for_converted_texture(path: Path, texture: TextureInfo) -> bytes:
    """Encode DXT5 for PAL8 textures, retaining their mip count when it is known."""
    payload_size = texture.data_end - texture.data_offset
    try:
        # PAL8 stores one byte per pixel at every mip level.
        total, w, h, mip_count = 0, texture.width, texture.height, 0
        while mip_count < 16:
            total += w * h
            mip_count += 1
            if total == payload_size:
                break
            if total > payload_size or (w == 1 and h == 1):
                raise ValueError
            w, h = max(1, w // 2), max(1, h // 2)
        else:
            raise ValueError
    except ValueError:
        mip_count = _full_mip_count(texture.width, texture.height)
    return _encode_dxt5(_image_rgba(path, texture.width, texture.height), mip_count)


def _compressed_dds_for_preview(payload: bytes, tex: TextureInfo) -> bytes:
    """Wrap a DXT1/DXT3 payload for Pillow without altering embedded bytes."""
    if tex.texture_type not in (5, 6):
        raise ValueError(f"Unsupported compressed format: type {tex.texture_type}.")
    return _build_dds(payload, tex.width, tex.height, _build_dds_header(tex.width, tex.height, 0, tex.texture_type))


def _build_type7_dds(payload: bytes, width: int, height: int) -> bytes:
    """Build a DDS for POP type 7, accepting DXT5 or raw BGRA payloads."""
    try:
        mip_count = _infer_dxt5_mip_count(width, height, len(payload))
        return _build_dds(payload, width, height, _build_dds_header(width, height, mip_count - 1, 7))
    except ValueError as dxt5_error:
        try:
            mip_count = _infer_raw_bgra_mip_count(width, height, len(payload))
        except ValueError:
            raise dxt5_error
        return _build_dds(payload, width, height, _build_dds_header(width, height, mip_count - 1, 0))


def _infer_raw_bgra_mip_count(width: int, height: int, payload_size: int) -> int:
    """Infer mip levels for POP texture payloads stored as raw 32-bit BGRA."""
    total = 0
    mip_count = 0
    w, h = width, height
    while mip_count < 16:
        total += max(1, w) * max(1, h) * 4
        mip_count += 1
        if total == payload_size:
            return mip_count
        if total > payload_size or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    raise ValueError(
        f"Raw BGRA payload of {payload_size:,} B does not match a whole number of mipmaps for {width}x{height}."
    )


def _dxt5_mip_sizes(width: int, height: int, mip_count: int) -> list[tuple[int, int]]:
    sizes: list[tuple[int, int]] = []
    w, h = width, height
    for _ in range(mip_count):
        sizes.append((w, h))
        w, h = max(1, w // 2), max(1, h // 2)
    return sizes


def _rgb565(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return ((r * 31 + 127) // 255 << 11) | ((g * 63 + 127) // 255 << 5) | ((b * 31 + 127) // 255)


def _unpack565(value: int) -> tuple[int, int, int]:
    r = ((value >> 11) & 31) * 255 // 31
    g = ((value >> 5) & 63) * 255 // 63
    b = (value & 31) * 255 // 31
    return r, g, b


def _encode_dxt5_block(pixels: list[tuple[int, int, int, int]]) -> bytes:
    alphas = [p[3] for p in pixels]
    a0, a1 = max(alphas), min(alphas)
    if a0 == a1:
        a0 = min(255, a0 + 1)
    alpha_palette = [a0, a1]
    if a0 > a1:
        alpha_palette.extend((
            (6 * a0 + a1) // 7,
            (5 * a0 + 2 * a1) // 7,
            (4 * a0 + 3 * a1) // 7,
            (3 * a0 + 4 * a1) // 7,
            (2 * a0 + 5 * a1) // 7,
            (a0 + 6 * a1) // 7,
        ))
    else:
        alpha_palette.extend(((4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
                              (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5, 0, 255))
    alpha_indices = 0
    for i, alpha in enumerate(alphas):
        index = min(range(8), key=lambda n: abs(alpha_palette[n] - alpha))
        alpha_indices |= index << (3 * i)

    colors = [(p[0], p[1], p[2]) for p in pixels]
    min_rgb = tuple(min(c[i] for c in colors) for i in range(3))
    max_rgb = tuple(max(c[i] for c in colors) for i in range(3))
    c0, c1 = _rgb565(max_rgb), _rgb565(min_rgb)
    if c0 == c1:
        c0 = min(0xFFFF, c0 + 1)
    if c0 < c1:
        c0, c1 = c1, c0
    rgb0, rgb1 = _unpack565(c0), _unpack565(c1)
    color_palette = [
        rgb0,
        rgb1,
        tuple((2 * rgb0[i] + rgb1[i]) // 3 for i in range(3)),
        tuple((rgb0[i] + 2 * rgb1[i]) // 3 for i in range(3)),
    ]
    color_indices = 0
    for i, color in enumerate(colors):
        index = min(range(4), key=lambda n: sum((color[j] - color_palette[n][j]) ** 2 for j in range(3)))
        color_indices |= index << (2 * i)
    return bytes((a0, a1)) + alpha_indices.to_bytes(6, "little") + c0.to_bytes(2, "little") + c1.to_bytes(2, "little") + color_indices.to_bytes(4, "little")


def _encode_dxt5(image, mip_count: int) -> bytes:
    """Pure-Python DXT5 encoder used so conversion has no external binary dependency."""
    from PIL import Image

    out = bytearray()
    for level, (width, height) in enumerate(_dxt5_mip_sizes(*image.size, mip_count)):
        level_image = image if level == 0 else image.resize((width, height), Image.Resampling.LANCZOS)
        rgba = level_image.load()
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                pixels: list[tuple[int, int, int, int]] = []
                for y in range(by, by + 4):
                    for x in range(bx, bx + 4):
                        pixels.append(rgba[min(x, width - 1), min(y, height - 1)])
                out.extend(_encode_dxt5_block(pixels))
    return bytes(out)


def _encode_dxt1_block(pixels: list[tuple[int, int, int, int]]) -> bytes:
    """Encode one BC1/DXT1 block, including DXT1 punch-through alpha."""
    transparent = [pixel[3] < 128 for pixel in pixels]
    opaque = [pixel for pixel, is_transparent in zip(pixels, transparent) if not is_transparent]
    if not opaque:
        return b"\x00\x00\x00\x00\xff\xff\xff\xff"

    colors = [(pixel[0], pixel[1], pixel[2]) for pixel in opaque]
    min_rgb = tuple(min(color[i] for color in colors) for i in range(3))
    max_rgb = tuple(max(color[i] for color in colors) for i in range(3))
    c0, c1 = _rgb565(max_rgb), _rgb565(min_rgb)
    has_transparency = any(transparent)
    if has_transparency:
        # BC1's three-colour mode (c0 <= c1) reserves index 3 for alpha 0.
        if c0 > c1:
            c0, c1 = c1, c0
    else:
        # Four-colour mode needs c0 > c1. Avoid accidentally selecting the
        # transparent BC1 mode for a flat, fully opaque block.
        if c0 <= c1:
            c0 = min(0xFFFF, c1 + 1)
            if c0 <= c1:
                c1 = max(0, c0 - 1)

    rgb0, rgb1 = _unpack565(c0), _unpack565(c1)
    if has_transparency:
        palette = [
            rgb0,
            rgb1,
            tuple((rgb0[i] + rgb1[i]) // 2 for i in range(3)),
        ]
    else:
        palette = [
            rgb0,
            rgb1,
            tuple((2 * rgb0[i] + rgb1[i]) // 3 for i in range(3)),
            tuple((rgb0[i] + 2 * rgb1[i]) // 3 for i in range(3)),
        ]

    indices = 0
    for i, pixel in enumerate(pixels):
        if transparent[i]:
            index = 3
        else:
            color = pixel[:3]
            index = min(
                range(len(palette)),
                key=lambda n: sum((color[channel] - palette[n][channel]) ** 2 for channel in range(3)),
            )
        indices |= index << (2 * i)
    return c0.to_bytes(2, "little") + c1.to_bytes(2, "little") + indices.to_bytes(4, "little")


def _encode_dxt1(image, mip_count: int = 1) -> bytes:
    """Pure-Python DXT1 encoder for POP type-5 textures."""
    from PIL import Image

    out = bytearray()
    for level, (width, height) in enumerate(_dxt5_mip_sizes(*image.size, mip_count)):
        level_image = image if level == 0 else image.resize((width, height), Image.Resampling.LANCZOS)
        rgba = level_image.load()
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                pixels = [
                    rgba[min(x, width - 1), min(y, height - 1)]
                    for y in range(by, by + 4)
                    for x in range(bx, bx + 4)
                ]
                out.extend(_encode_dxt1_block(pixels))
    return bytes(out)


def _dxt1_payload_from_file(path: Path, texture: TextureInfo) -> bytes:
    """Encode a replacement as POP DXT1 (type 5), as Jade Toolkit does.

    Jade's writer emits one DXT1 base level and clears the texture mip field.
    Keeping format 5 is essential: changing a game DXT1 texture to type 7
    changes the entry layout and can make the renderer read wrong bytes.
    """
    return _encode_dxt1(_image_rgba(path, texture.width, texture.height))


def _build_tga_header(width: int, height: int, pixel_depth: int = 32) -> bytes:
    """Build the small TGA header needed for POP's raw BGRA texture payloads."""
    if not (1 <= width <= 65535 and 1 <= height <= 65535):
        raise ValueError(f"Invalid TGA dimensions: {width}x{height}.")
    if pixel_depth not in (24, 32):
        raise ValueError(f"Unsupported TGA depth: {pixel_depth} bits.")
    # Jade raw/palette rows have the same top-left origin as DDS and our
    # encoders. Bottom-left here flips only these formats during decoding.
    header = bytearray(18)
    header[2] = 2
    struct.pack_into("<H", header, 12, width)
    struct.pack_into("<H", header, 14, height)
    header[16] = pixel_depth
    header[17] = 0x20 | (8 if pixel_depth == 32 else 0)
    return bytes(header)


def _build_4bit_tga(width: int, height: int, packed: bytes) -> bytes:
    """Expand Jade's low-nibble-first 4-bit grayscale texture format."""
    pixel_count = width * height
    if len(packed) < (pixel_count + 1) // 2:
        raise ValueError("Insufficient 4-bit data for this texture.")
    decoded = bytearray(pixel_count * 4)
    for pixel in range(pixel_count):
        nibble = (packed[pixel // 2] & 0x0F) if pixel % 2 == 0 else (packed[pixel // 2] >> 4)
        value = nibble * 17
        decoded[pixel * 4:pixel * 4 + 4] = bytes((value, value, value, 255))
    return _build_tga_header(width, height, 32) + bytes(decoded)


def _build_palette_tga(width: int, height: int, palette: bytes, indices: bytes) -> bytes:
    """Expand an 8-bit POP palette texture to a standalone 32-bit TGA."""
    pixel_count = width * height
    if len(palette) < 1024 or len(indices) < pixel_count:
        raise ValueError("Insufficient palette/index data for this texture.")
    decoded = bytearray(pixel_count * 4)
    for i, index in enumerate(indices[:pixel_count]):
        palette_pos = index * 4
        decoded[i * 4:i * 4 + 4] = palette[palette_pos:palette_pos + 4]
    return _build_tga_header(width, height, 32) + bytes(decoded)


def _image_rgba(path: Path, width: int, height: int):
    """Load any Pillow-supported image and normalize it to the target dimensions."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is unavailable. Install it with: python -m pip install Pillow") from exc
    try:
        image = Image.open(path).convert("RGBA")
    except Exception as exc:
        raise ValueError(f"Cannot read image: {exc}") from exc
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    return image


def _transform_texture_image(image, rotation: int, flip_x: bool, flip_y: bool):
    """Apply the non-destructive Texture Swap orientation controls to a Pillow image."""
    from PIL import Image
    if flip_x:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip_y:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if rotation:
        image = image.rotate(-rotation, expand=False, resample=Image.Resampling.BICUBIC)
    return image


def _dds_payload_from_file(path: Path, texture: TextureInfo, original: bytes) -> bytes:
    """Convert any Pillow-readable image to the exact POP DXT5 payload size."""
    expected = texture.data_end - texture.data_offset

    # Preserve an already-compatible DDS bitstream byte-for-byte. This also
    # supports unusual DDS headers while enforcing the game's payload contract.
    raw = path.read_bytes()
    if raw[:4] == b"DDS ":
        if len(raw) < 128:
            raise ValueError("DDS is too short: missing header.")
        width, height = struct.unpack_from("<II", raw, 12)
        fourcc = raw[84:88]
        if width == texture.width and height == texture.height and fourcc == b"DXT5":
            payload = raw[128:]
            if len(payload) == expected:
                return payload
        # A DDS with another size/format is treated like every other input
        # image and decoded/re-encoded to the original POP contract below.

    try:
        from PIL import Image  # noqa: F401 - validates the bundled runtime early
    except ImportError as exc:
        raise ValueError("Pillow is unavailable. The bundled runtime should contain vendor/PIL.") from exc

    # Encode DXT5 ourselves. This preserves the exact block/mipmap count of the
    # original payload and does not depend on ImageMagick or another executable.
    image = _image_rgba(path, texture.width, texture.height)
    mip_count = _infer_dxt5_mip_count(texture.width, texture.height, expected)
    payload = _encode_dxt5(image, mip_count)
    if len(payload) != expected:
        raise ValueError(f"Incompatible DDS conversion: original {expected:,} B, converted {len(payload):,} B.")
    return payload


def _tga_payload_from_file(path: Path, texture: TextureInfo, original: bytes) -> bytes:
    """Convert an image to POP type-0 pixels while preserving any extra native data.

    POP's type-0 entries are not guaranteed to contain only width*height*4 bytes.
    The legacy tools read the first image-sized region and keep the remainder of
    the FileEntry untouched. Some game assets therefore have a larger payload
    than the visible RGBA surface (for example 43,776 B for a 128x64 image,
    whose base surface is 32,768 B). Replacing only the base surface preserves
    that extra data and keeps the FileEntry size byte-for-byte compatible.
    """
    image = _image_rgba(path, texture.width, texture.height)
    rgba = image.tobytes()
    base_size = texture.width * texture.height * 4
    expected = texture.data_end - texture.data_offset

    # A few legacy type-0 assets are 24-bit. Match that representation when the
    # original payload size proves it unambiguously; otherwise use the normal
    # 32-bit BGRA representation used by the existing TGA export path.
    if expected == texture.width * texture.height * 3:
        payload = bytearray(texture.width * texture.height * 3)
        for src, dst in zip(range(0, len(rgba), 4), range(0, len(payload), 3)):
            r, g, b, _a = rgba[src:src + 4]
            payload[dst:dst + 3] = bytes((b, g, r))
        return bytes(payload)

    payload = bytearray(base_size)
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i:i + 4]
        payload[i:i + 4] = bytes((b, g, r, a))

    if expected < base_size:
        raise ValueError(
            f"Incompatible TGA conversion: original {expected:,} B, "
            f"at least {base_size:,} B required for {texture.width}x{texture.height} RGBA."
        )

    # Preserve the bytes after the visible base surface. The original POP
    # tools do the same when decoding type-0 entries, and those bytes may carry
    # legacy mip/detail data or padding that must remain in the FileEntry.
    if expected > base_size:
        tail_start = texture.data_offset + base_size
        original_tail = original[tail_start:texture.data_end]
        if len(original_tail) != expected - base_size:
            raise ValueError(
                f"TGA conversion: cannot read original tail ({len(original_tail):,} B, "
                f"expected {expected - base_size:,} B)."
            )
        payload.extend(original_tail)
    return bytes(payload)


def _palette_payload_from_image(image, texture: TextureInfo, palette: bytes,
                                original_payload: bytes) -> bytes:
    """Encode the base PAL8 level and preserve native mipmap/padding bytes."""
    if len(palette) < 1024:
        raise ValueError("Incomplete POP palette: 256 RGBA colors are required.")
    palette_rgba = [tuple(palette[i:i + 4]) for i in range(0, 1024, 4)]
    pixels = image.get_flattened_data()
    indices = bytearray(texture.width * texture.height)
    cache: dict[tuple[int, int, int, int], int] = {}
    for i, pixel in enumerate(pixels):
        if pixel not in cache:
            cache[pixel] = min(range(256), key=lambda n: sum((pixel[c] - palette_rgba[n][c]) ** 2 for c in range(4)))
        indices[i] = cache[pixel]
    expected = texture.data_end - texture.data_offset
    if expected < len(indices) or len(original_payload) != expected:
        raise ValueError(f"Incompatible palette payload: original {expected:,} B, base {len(indices):,} B.")
    return bytes(indices) + original_payload[len(indices):]


def _palette_payload_from_file(path: Path, texture: TextureInfo, palette: bytes,
                               original_payload: bytes) -> bytes:
    return _palette_payload_from_image(
        _image_rgba(path, texture.width, texture.height), texture, palette, original_payload
    )


def _texture_replacement_from_file(path: Path, texture: TextureInfo, original: bytes,
                                   keep_dimensions: bool = False) -> tuple[bytes, int, int, int]:
    """Encode a replacement and report the dimensions used by its pixel data."""
    from PIL import Image

    target_type = 7 if texture.texture_type == 1 else texture.texture_type
    if target_type not in (0, 5, 7):
        raise ValueError(f"Unsupported POP texture format: type {texture.texture_type}.")
    width, height = texture.width, texture.height
    if keep_dimensions:
        with Image.open(path) as image:
            width, height = image.size
        if not (1 <= width <= 8192 and 1 <= height <= 8192):
            raise ValueError("POP texture dimensions must be between 1 and 8192 pixels.")
    if target_type in (5, 7) and (width % 4 or height % 4):
        raise ValueError("Jade DXT textures require dimensions divisible by 4.")
    if (width, height) != (texture.width, texture.height):
        image = _image_rgba(path, width, height)
        # A changed surface gets a new base level; old mips/padding describe
        # the old dimensions and cannot be appended to the resized texture.
        if target_type == 5:
            payload = _encode_dxt1(image)
        elif target_type == 7:
            payload = _encode_dxt5(image, 1)
        else:
            is_bgr24 = texture.data_end - texture.data_offset == texture.width * texture.height * 3
            payload = image.convert("RGB").tobytes("raw", "BGR") if is_bgr24 else image.tobytes("raw", "BGRA")
    elif texture.texture_type == 7:
        payload = _dds_payload_from_file(path, texture, original)
    elif texture.texture_type == 5:
        payload = _dxt1_payload_from_file(path, texture)
    elif texture.texture_type == 1:
        payload = _dxt5_payload_for_converted_texture(path, texture)
    else:
        payload = _tga_payload_from_file(path, texture, original)
    return payload, target_type, width, height

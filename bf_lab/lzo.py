"""POP-LZO compression and decompression using the bundled runtime."""
from __future__ import annotations

from pathlib import Path
import ctypes
import struct
import subprocess
import tempfile
from .config import (
    LZO_BLOCK_SIZE,
    ROOT,
)


def _looks_like_pop_lzo(data: bytes) -> bool:
    if len(data) < 18:
        return False
    dec_size, enc_size = struct.unpack_from("<2I", data, 0)
    if dec_size <= enc_size or enc_size <= 0 or dec_size > 16 * 1024 * 1024:
        return False
    # Retail POP uses 0x99C0FFEE. Some later tools use 0x99C0FFFE.
    # Both are the same block-LZO wrapper; rejecting FE makes compressed WOW
    # payloads look raw and the FileEntry parser then reads compressed bytes as sizes.
    markers = (b"\x99\xC0\xFF\xEE", b"\x99\xC0\xFF\xFE")
    return any(data[13:17] == marker or data[14:18] == marker for marker in markers)


def _decompress_lzo_block(block: bytes, expected_size: int) -> bytes:
    """Pure-Python LZO1X decoder for POP blocks (including match streams)."""
    ip = 0
    op = bytearray()
    last_match = False

    def need(n: int) -> None:
        if ip + n > len(block):
            raise ValueError("Truncated LZO block.")

    def copy_literals(n: int) -> None:
        nonlocal ip
        need(n)
        op.extend(block[ip:ip + n])
        ip += n

    if not block:
        raise ValueError("Empty LZO block.")
    t = block[ip]
    ip += 1
    if t > 17:
        copy_literals(t - 17)
        last_match = True
    else:
        t = 0

    while ip < len(block):
        if t <= 15:
            if t == 0:
                zeros = 0
                while ip < len(block) and block[ip] == 0:
                    zeros += 1
                    ip += 1
                if ip >= len(block):
                    raise ValueError("Truncated LZO literal length.")
                t = 18 + zeros
            else:
                t += 3
            copy_literals(t)
            if ip >= len(block):
                break
            t = block[ip]
            ip += 1
        elif t <= 31:
            need(1)
            m_off = (t & 8) << 11
            m_off += block[ip] << 3
            m_off += (t >> 2) & 7
            m_off += 1
            match_len = (t & 3) + 2
            ip += 1
            need(1)
            m_off += block[ip] >> 5
            ip += 1
            if m_off > len(op):
                raise ValueError("Invalid LZO match offset.")
            for _ in range(match_len):
                op.append(op[-m_off])
            last_match = True
            t = block[ip] if ip < len(block) else 17
            if ip < len(block):
                ip += 1
        else:
            if t >= 64:
                need(1)
                m_off = ((t >> 2) & 7) | (block[ip] << 3)
                m_off += 1
                match_len = (t >> 5) + 1
                ip += 1
            elif t >= 32:
                match_len = (t & 31) + 2
                need(2)
                m_off = (block[ip] >> 2) | ((t & 8) << 11)
                m_off += 1
                ip += 2
            else:
                if t == 17 and ip + 2 <= len(block):
                    break
                match_len = t & 7
                need(2)
                m_off = (block[ip] >> 2) | ((t & 8) << 11)
                m_off += 1
                ip += 2
                if match_len == 0:
                    while ip < len(block) and block[ip] == 0:
                        match_len += 255
                        ip += 1
                    need(1)
                    match_len += 31 + block[ip] + 2
                    ip += 1
                else:
                    match_len += 2
            if m_off > len(op):
                raise ValueError("Invalid LZO match offset.")
            for _ in range(match_len):
                op.append(op[-m_off])
            last_match = True
            if ip >= len(block):
                break
            t = block[ip]
            ip += 1

        # LZO1X's state transition after a literal run can introduce the
        # short match form. The decoder above handles the control byte on the
        # next loop; keep the marker only for readability/debugging.
        _ = last_match

    if len(op) != expected_size:
        raise ValueError(f"LZO decompression: got {len(op)} B, expected {expected_size} B.")
    return bytes(op)


def decompress_pop_lzo(data: bytes) -> bytes:
    """Decode POP's consecutive little-endian LZO blocks (up to 0x20000 each)."""
    compat = ROOT / "lzo_compat.dll"
    native = None
    if compat.is_file():
        try:
            lib = ctypes.CDLL(str(compat))
            native = lib.lzo_bridge_decompress
            native.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
                               ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_size_t)]
            native.restype = ctypes.c_int
        except (OSError, AttributeError):
            native = None
    pos = 0
    output = bytearray()
    while pos + 8 <= len(data):
        dec_size, enc_size = struct.unpack_from("<2I", data, pos)
        if dec_size == 0 or enc_size == 0:
            break  # physical padding or optional terminator
        pos += 8
        if enc_size > len(data) - pos:
            raise ValueError("Truncated POP LZO block.")
        block = data[pos:pos + enc_size]
        pos += enc_size
        if dec_size == enc_size:
            output.extend(block)
        elif native is not None:
            src = (ctypes.c_ubyte * len(block)).from_buffer_copy(block)
            dst = (ctypes.c_ubyte * dec_size)()
            out_size = ctypes.c_size_t(dec_size)
            rc = native(src, len(block), dst, ctypes.byref(out_size))
            if rc != 0 or out_size.value != dec_size:
                raise ValueError(f"POP LZO decompression failed (rc={rc}, output={out_size.value}, expected={dec_size}).")
            output.extend(bytes(dst[:out_size.value]))
        else:
            output.extend(_decompress_lzo_block(block, dec_size))
        # A short final block ends the stream; a full block may be followed by another.
        if dec_size < LZO_BLOCK_SIZE:
            break
    if not output:
        raise ValueError("Empty or invalid POP LZO wrapper.")
    return bytes(output)


def _encode_literal_lzo(block: bytes) -> bytes:
    """Encode a small literal-only LZO1X stream; larger blocks stay raw."""
    if not block:
        return b"\x11\x00\x00\x00"
    if len(block) <= 238:
        return bytes((17 + len(block),)) + block + b"\x11\x00\x00"
    return block


def compress_pop_lzo(data: bytes) -> bytes:
    """Compress POP blocks using the bundled LZO 1.08 binary."""
    # POP retail assets use FFEE, while later/prototype WOWs use FFFE.
    # The marker identifies the FileEntry stream, not the LZO codec; retain
    # the original bytes and accept either wrapper when recompressing.
    pop_markers = (b"\x99\xC0\xFF\xEE", b"\x99\xC0\xFF\xFE")
    if len(data) < 8 or data[4:8] not in pop_markers:
        raise ValueError("The BIN does not contain a valid POP magic (0x99C0FFEE/0x99C0FFFE) at offset 4.")
    helper = ROOT / "pop_lzo_native.ps1"
    dll = ROOT / "lzo.dll"
    if not helper.is_file() or not dll.is_file():
        raise RuntimeError("Incomplete standalone LZO support: pop_lzo_native.ps1 or lzo.dll is missing.")
    temp_dir = Path(tempfile.mkdtemp(prefix="mini_jade_lzo_"))
    source = temp_dir / "input.dec"
    target = temp_dir / "output.enc"
    try:
        source.write_bytes(data)
        powershell = Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe")
        if not powershell.is_file():
            raise RuntimeError("32-bit Windows PowerShell is unavailable for the standalone LZO runtime.")
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper),
             "-InputFile", str(source), "-OutputFile", str(target)],
            capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"POP LZO compression failed: {details or 'unknown error'}")
        if not target.is_file():
            raise RuntimeError("The standalone LZO runtime did not produce any output.")
        encoded = target.read_bytes()
        if len(encoded) < 8:
            raise RuntimeError("The LZO runtime produced a wrapper that is too short.")
        if decompress_pop_lzo(encoded) != data:
            raise RuntimeError("LZO verification failed: the recompressed BIN does not decode to the original .DEC data.")
        return encoded
    finally:
        for child in temp_dir.glob("*"):
            child.unlink(missing_ok=True)
        temp_dir.rmdir()

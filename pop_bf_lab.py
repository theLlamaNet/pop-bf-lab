"""PoP BF Lab launcher and backwards-compatible import facade.

Implementation lives in bf_lab; run this file or run_pop_bf_lab.bat as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
from typing import Optional
import base64
import ctypes
import io
import bf_lab  # Initializes paths for bundled dependencies.
import jade_mesh
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from bf_lab.archives import (
    _collect_texture_key_replacements,
    _legacy_asset_path,
    _legacy_file_data_length,
    _legacy_folder_entries,
    _legacy_folder_path,
    _patch_texture_key_in_asset,
    _read_big_header,
    _read_legacy_bigfile,
    _repack_legacy_bigfile,
    _repack_legacy_bigfile_changes,
    _update_legacy_size_grs_payload,
    build_legacy_bf_from_folder,
    extract_legacy_bf_as_root,
    read_bigfile,
    read_bigfile_entry,
)
from bf_lab.config import (
    AI_MAX_LEN_VAR,
    LEGACY_BF_FILE_ENTRY_SIZE,
    LEGACY_BF_FILE_TABLE_ENTRY_SIZE,
    LEGACY_BF_HEADER_SIZE,
    LZO_BLOCK_SIZE,
    MARKER,
    OVA_INFO_SIZE,
    ROOT,
    VENDOR_DIR,
)
from bf_lab.lzo import (
    _decompress_lzo_block,
    _encode_literal_lzo,
    _looks_like_pop_lzo,
    compress_pop_lzo,
    decompress_pop_lzo,
)
from bf_lab.materials import (
    _associate_mesh_material_packs,
    _classic_jade_material,
    _jade_color_rgba,
    _modern_jade_material,
    _scan_pop_material_records,
    _scan_pop_materials,
)
from bf_lab.mesh_import import (
    _build_static_mesh_replacement,
    _glb_accessor_values,
    _glb_image,
    _glb_mat_mul,
    _glb_node_matrix,
    _glb_transform_normal,
    _glb_transform_point,
    _load_glb_mesh_for_swap,
    _load_mesh_for_swap,
    _load_obj_mesh_for_swap,
    _mesh_rli_replacements,
    _mesh_vertex_normals,
    _read_glb_for_mesh_swap,
)
from bf_lab.mesh_parser import (
    _scan_pop3_direct_mesh,
    _scan_pop_meshes,
)
from bf_lab.models import (
    Asset,
    BigFileEntry,
    BigFileInfo,
    LegacyFolderEntry,
    MaterialInfo,
    MeshInfo,
    MeshMaterial,
    OvaStructure,
    OvaVariable,
    PopFileEntry,
    TextureInfo,
)
from bf_lab.ova import (
    _detect_pop_name_span,
    _find_ascii_fallback,
    _find_init_buffer_after_names,
    _find_pop_ova_structures,
    _pop_type_storage_size,
    _recover_pop_init_buffer,
    _valid_identifier,
    find_ova_structures,
    find_variables,
    ova_diagnostic_report,
)
from bf_lab.project import (
    JadeProject,
)
from bf_lab.resources import (
    _PopReader,
    _parse_pop_file_entries,
    _pop_hex,
)
from bf_lab.textures import (
    _build_4bit_tga,
    _build_dds,
    _build_dds_header,
    _build_palette_tga,
    _build_tga_header,
    _build_type7_dds,
    _compressed_dds_for_preview,
    _dds_blob_for_dump,
    _dds_payload_and_info,
    _dds_payload_from_file,
    _decode_pop_texture_image,
    _dxt1_payload_from_file,
    _dxt5_mip_sizes,
    _dxt5_payload_for_converted_texture,
    _encode_dxt1,
    _encode_dxt1_block,
    _encode_dxt5,
    _encode_dxt5_block,
    _full_mip_count,
    _image_rgba,
    _infer_dxt5_mip_count,
    _infer_mip_count,
    _infer_raw_bgra_mip_count,
    _palette_payload_from_file,
    _palette_payload_from_image,
    _rgb565,
    _scan_pop_textures,
    _tga_payload_from_file,
    _transform_texture_image,
    _unpack565,
)
from bf_lab.ui.app import (
    JadeToolkit,
)
from bf_lab.viewports import (
    GL,
    GLU,
    MaterialViewport,
    MeshViewport,
    OpenGLFrame,
)


if __name__ == "__main__":
    app = JadeToolkit()
    app.iconbitmap(r"bf_lab\icons\bigfile.ico")
    app.mainloop()

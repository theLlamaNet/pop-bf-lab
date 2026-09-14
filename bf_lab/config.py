"""Installation paths and shared binary format constants."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "vendor"
MARKER = b"ova"
AI_MAX_LEN_VAR = 30
OVA_INFO_SIZE = 12
LEGACY_BF_HEADER_SIZE = 68
LEGACY_BF_FILE_ENTRY_SIZE = 84
LEGACY_BF_FILE_TABLE_ENTRY_SIZE = 8
LZO_BLOCK_SIZE = 131072

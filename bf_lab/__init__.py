"""PoP BF Lab application modules.

Resolve bundled dependencies from the installation directory, regardless of cwd.
"""
import sys
from .config import ROOT, VENDOR_DIR

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

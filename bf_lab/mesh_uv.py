"""UV-space conversions used by native Jade meshes and standard 3D formats."""
from __future__ import annotations


def _jade_uv_to_standard(uv: tuple[float, float]) -> tuple[float, float]:
    """Convert Jade's top-left V origin to the bottom-left OBJ/OpenGL origin."""
    u, v = uv
    return u, 1.0 - v


def _standard_uv_to_jade(uv: tuple[float, float]) -> tuple[float, float]:
    """Convert a bottom-left OBJ UV to the internal Jade top-left convention."""
    u, v = uv
    return u, 1.0 - v

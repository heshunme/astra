from __future__ import annotations

from ..models import ToolSpec
from ..tools import build_all_tools


def load_builtin_tools() -> dict[str, ToolSpec]:
    return build_all_tools()

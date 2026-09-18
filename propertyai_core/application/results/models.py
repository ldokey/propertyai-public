from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    status: str
    code: str
    reused: bool = False
    result: Optional[Dict[str, Any]] = None

"""Safe atomic writes for Claude configuration files."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, data: Any, *, backup: bool = False) -> None:
    """Atomically write JSON without making an existing private file more permissive.

    Existing file permissions are preserved. New files are created as mode 0600 because
    Claude configuration may contain MCP environment values and other sensitive settings.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existed = target.exists()
    mode = stat.S_IMODE(target.stat().st_mode) if existed else 0o600
    if backup and existed:
        shutil.copy2(target, target.with_name(f"{target.name}.bak"))

    temporary = target.with_name(f"{target.name}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise

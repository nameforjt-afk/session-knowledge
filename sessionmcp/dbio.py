"""Permission-safe helpers for local SQLite databases."""

from __future__ import annotations

import os
from pathlib import Path


PRIVATE_MODE = 0o600


def prepare_private_database(path: Path) -> None:
    """Create or tighten a database before SQLite can create sidecars from its mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_MODE)
    except FileExistsError:
        os.chmod(path, PRIVATE_MODE)
    else:
        os.close(descriptor)
    restrict_sqlite_artifacts(path)


def restrict_sqlite_artifacts(path: Path) -> None:
    """Tighten the main database and any WAL/SHM sidecars that currently exist."""
    for suffix in ("", "-wal", "-shm"):
        artifact = Path(f"{path}{suffix}")
        if artifact.exists():
            os.chmod(artifact, PRIVATE_MODE)

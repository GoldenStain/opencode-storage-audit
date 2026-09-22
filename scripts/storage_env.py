"""Locate OpenCode's data directory and artifacts, and detect running OpenCode processes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DB_NAME = "opencode.db"


@dataclass(frozen=True)
class Artifacts:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME

    @property
    def wal_path(self) -> Path:
        return self.data_dir / f"{DB_NAME}-wal"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @property
    def diff_dir(self) -> Path:
        return self.storage_dir / "session_diff"

    @property
    def tool_output_dir(self) -> Path:
        return self.data_dir / "tool-output"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshot"

    @property
    def worktree_dir(self) -> Path:
        return self.data_dir / "worktree"


def resolve_data_dir(override: Path | str | None = None) -> Path:
    """Resolve the data directory from an explicit path, environment, or platform default."""
    if override is not None:
        return Path(override).expanduser()
    if os.environ.get("OPENCODE_DATA_DIR"):
        return Path(os.environ["OPENCODE_DATA_DIR"]).expanduser()
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]).expanduser() / "opencode"
    return Path.home() / ".local/share/opencode"


def running_processes() -> list[dict[str, str]]:
    """Return live processes named opencode as pid/command pairs."""
    processes: list[dict[str, str]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return processes
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "opencode":
                continue
            cmdline = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
                .strip()
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        processes.append({"pid": entry.name, "command": cmdline or "opencode"})
    return processes


def dir_size(path: Path) -> int:
    """Return the total size in bytes of every regular file under a path."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def usage_entries(data_dir: Path) -> list[dict[str, object]]:
    """Return per-entry usage for each child of the data directory, largest first."""
    entries: list[dict[str, object]] = []
    if not data_dir.is_dir():
        return entries
    for child in data_dir.iterdir():
        try:
            if child.is_dir():
                size = dir_size(child)
            else:
                size = child.stat().st_size
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "bytes": size,
                "is_dir": child.is_dir(),
            }
        )
    entries.sort(key=lambda entry: entry["bytes"], reverse=True)
    return entries
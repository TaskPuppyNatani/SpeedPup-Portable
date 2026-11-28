# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-platform per-user application data directory and history DB path resolution.

This module is strictly Qt-independent and never resolves default paths relative
to the current working directory or the source repository.
"""

from __future__ import annotations

from collections.abc import Mapping
import ntpath
import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import posixpath
import sys

from speedpup.core.history import HistoryAccessError, HistoryPathError

DATA_DIR_ENV = "SPEEDPUP_DATA_DIR"
POSIX_APP_DIR = "speedpup-portable"
WINDOWS_ORG_DIR = "TaskPuppyKreations"
DESKTOP_APP_DIR = "SpeedPup Portable"
HISTORY_DB_FILENAME = "history.sqlite3"


def _normalize_platform(platform_name: str | None) -> str:
    raw = (platform_name or sys.platform).strip().lower()
    if raw.startswith("win"):
        return "win32"
    if raw == "darwin" or raw.startswith("mac"):
        return "darwin"
    return "linux"


def _is_windows_absolute(path_str: str) -> bool:
    if not path_str or "\x00" in path_str:
        return False
    drive, rest = ntpath.splitdrive(path_str)
    if drive and rest.startswith(("\\", "/")):
        return True
    return path_str.startswith("\\\\") and len(path_str) > 2


def _is_posix_absolute(path_str: str) -> bool:
    return bool(path_str) and "\x00" not in path_str and posixpath.isabs(path_str)


def _is_absolute_for_platform(path_str: str, platform_key: str) -> bool:
    if platform_key == "win32":
        return _is_windows_absolute(path_str)
    return _is_posix_absolute(path_str)


def _path_for_platform(raw: str | PurePath, platform_key: str) -> PurePath:
    native_key = _normalize_platform(sys.platform)
    if platform_key == native_key:
        return Path(raw)
    if platform_key == "win32":
        return PureWindowsPath(raw)
    return PurePosixPath(raw)


def _resolve_home(home: Path | PurePath | str | None, platform_key: str) -> PurePath:
    if home is not None:
        raw = str(home).strip()
        if not _is_absolute_for_platform(raw, platform_key):
            raise HistoryPathError("Resolved home directory must be an absolute path.")
        return _path_for_platform(raw, platform_key)
    try:
        detected = Path.home()
    except RuntimeError as exc:
        raise HistoryPathError(
            "Could not determine the user home directory for local history storage."
        ) from exc
    return _path_for_platform(str(detected), platform_key)


def resolve_data_dir(
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | PurePath | str | None = None,
) -> PurePath:
    """Resolve the writable per-user application data directory.

    Resolution order:
    1. ``SPEEDPUP_DATA_DIR`` when set (must be an absolute path; intended for
       portable launchers or explicit user relocation).
    2. Platform per-user data location:
       - Linux/POSIX: ``$XDG_DATA_HOME/speedpup-portable`` when
         ``XDG_DATA_HOME`` is absolute, else ``~/.local/share/speedpup-portable``.
       - Windows: ``%LOCALAPPDATA%\\TaskPuppyKreations\\SpeedPup Portable``
         (falling back to ``%APPDATA%``, then ``~\\AppData\\Local``).
       - macOS: ``~/Library/Application Support/SpeedPup Portable``.

    Never creates directories and never depends on the current working directory.
    """
    environ = os.environ if env is None else env
    platform_key = _normalize_platform(platform_name)

    override = environ.get(DATA_DIR_ENV, "").strip()
    if override:
        if not _is_absolute_for_platform(override, platform_key):
            raise HistoryPathError(
                f"{DATA_DIR_ENV} must be an absolute path without NUL bytes."
            )
        return _path_for_platform(override, platform_key)

    if platform_key == "win32":
        for var_name in ("LOCALAPPDATA", "APPDATA"):
            candidate = environ.get(var_name, "").strip()
            if _is_windows_absolute(candidate):
                return (
                    _path_for_platform(candidate, platform_key)
                    / WINDOWS_ORG_DIR
                    / DESKTOP_APP_DIR
                )
        base_home = _resolve_home(home, platform_key)
        return base_home / "AppData" / "Local" / WINDOWS_ORG_DIR / DESKTOP_APP_DIR

    if platform_key == "darwin":
        base_home = _resolve_home(home, platform_key)
        return base_home / "Library" / "Application Support" / DESKTOP_APP_DIR

    xdg_data_home = environ.get("XDG_DATA_HOME", "").strip()
    if _is_posix_absolute(xdg_data_home):
        return _path_for_platform(xdg_data_home, platform_key) / POSIX_APP_DIR
    base_home = _resolve_home(home, platform_key)
    return base_home / ".local" / "share" / POSIX_APP_DIR


def resolve_history_db_path(
    explicit_path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | PurePath | str | None = None,
) -> PurePath:
    """Resolve the local SQLite history database path without creating files."""
    if explicit_path is not None:
        raw = str(explicit_path)
        if not raw.strip() or "\x00" in raw:
            raise HistoryPathError("Explicit history database path must be non-empty.")
        if raw.strip() == ":memory:" or raw.strip().startswith("file:"):
            raise HistoryPathError(
                "Explicit history database path must be a local filesystem path."
            )
        return Path(explicit_path).expanduser()

    return resolve_data_dir(
        env=env,
        platform_name=platform_name,
        home=home,
    ) / HISTORY_DB_FILENAME


def ensure_storage_directory(db_path: Path) -> None:
    """Ensure the parent directory for ``db_path`` exists and is usable."""
    if db_path.exists() and db_path.is_dir():
        raise HistoryAccessError(
            f"History database path points to a directory, not a file: {db_path}"
        )
    parent = db_path.parent
    if parent.exists() and not parent.is_dir():
        raise HistoryAccessError(
            f"History storage directory path is not a directory: {parent}"
        )
    existed = parent.exists()
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HistoryAccessError(
            f"Could not create history storage directory {parent}: {exc}"
        ) from exc
    if not existed and os.name == "posix":
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass

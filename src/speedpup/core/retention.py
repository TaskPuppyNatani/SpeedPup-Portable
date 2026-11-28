# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent, fail-safe retention policy configuration for local history.

This module is strictly Qt-independent and defaults to ``Keep Forever`` so
existing or newly installed instances never delete measurements automatically
without explicit user opt-in. Unreadable or corrupted configuration files fail
safely to ``Keep Forever`` with an explicit warning rather than enabling
destructive cleanup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import tempfile

from speedpup.core.history import (
    HistoryAccessError,
    HistoryPathError,
    format_utc_timestamp,
    parse_utc_timestamp,
)
from speedpup.core.history_paths import (
    ensure_storage_directory,
    resolve_data_dir,
)

RETENTION_SETTINGS_FILENAME = "retention.json"
RETENTION_SETTINGS_VERSION = 1


class RetentionPolicy(StrEnum):
    """Supported user-configurable automatic history retention policies."""

    KEEP_FOREVER = "keep_forever"
    DAYS_30 = "30d"
    DAYS_90 = "90d"
    DAYS_180 = "180d"
    DAYS_365 = "365d"

    @property
    def days(self) -> int | None:
        return {
            RetentionPolicy.KEEP_FOREVER: None,
            RetentionPolicy.DAYS_30: 30,
            RetentionPolicy.DAYS_90: 90,
            RetentionPolicy.DAYS_180: 180,
            RetentionPolicy.DAYS_365: 365,
        }[self]

    @property
    def label(self) -> str:
        return {
            RetentionPolicy.KEEP_FOREVER: "Keep Forever",
            RetentionPolicy.DAYS_30: "30 days",
            RetentionPolicy.DAYS_90: "90 days",
            RetentionPolicy.DAYS_180: "180 days",
            RetentionPolicy.DAYS_365: "365 days",
        }[self]

    @property
    def is_finite(self) -> bool:
        return self.days is not None

    def is_stricter_than(self, previous: RetentionPolicy) -> bool:
        """Return True if switching ``previous -> self`` enables or shortens retention."""
        if self.days is None:
            return False
        if previous.days is None:
            return True
        return self.days < previous.days

    def cutoff_utc(self, now: datetime | None = None) -> datetime | None:
        """Return the strict UTC expiration cutoff, or ``None`` for Keep Forever.

        Boundary semantics:
        - Measurements with ``completed_at_utc < cutoff_utc`` are expired.
        - Measurements with ``completed_at_utc >= cutoff_utc`` (including exact
          equality with the cutoff timestamp) are retained.
        """
        if self.days is None:
            return None
        ref = now if now is not None else datetime.now(timezone.utc)
        if ref.tzinfo is None or ref.utcoffset() is None:
            raise ValueError("Retention cutoff reference timestamp must be timezone-aware.")
        return ref.astimezone(timezone.utc) - timedelta(days=self.days)


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """Loaded retention configuration with explicit fail-safe diagnostic state."""

    policy: RetentionPolicy = RetentionPolicy.KEEP_FOREVER
    settings_version: int = RETENTION_SETTINGS_VERSION
    updated_at: datetime | None = None
    settings_path: str | None = None
    load_warning: str | None = None


def resolve_retention_settings_path(
    explicit_path: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | str | None = None,
) -> Path:
    """Resolve the local JSON settings path for retention without creating files."""
    if explicit_path is not None:
        raw = str(explicit_path)
        if not raw.strip() or "\x00" in raw:
            raise HistoryPathError("Explicit retention settings path must be non-empty.")
        return Path(explicit_path).expanduser()
    if db_path is not None:
        raw_db = str(db_path)
        if not raw_db.strip() or "\x00" in raw_db:
            raise HistoryPathError("Database path for retention settings must be non-empty.")
        return Path(db_path).expanduser().parent / RETENTION_SETTINGS_FILENAME
    return Path(
        resolve_data_dir(env=env, platform_name=platform_name, home=home)
    ) / RETENTION_SETTINGS_FILENAME


def load_retention_settings(path: Path | str) -> RetentionSettings:
    """Load persisted retention settings, failing safely to ``Keep Forever``.

    Missing files silently return ``Keep Forever`` without writing to disk.
    Unreadable, malformed, unknown-version, or invalid-policy files never
    enable automatic deletion; they return ``Keep Forever`` with ``load_warning``.
    """
    target = Path(path)
    if not target.exists():
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
        )
    if target.is_dir():
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
            load_warning=(
                "Retention settings path is a directory; automatic deletion has been "
                "disabled (Keep Forever)."
            ),
        )
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
            load_warning=(
                f"Could not read retention settings ({exc.__class__.__name__}); "
                "defaulted safely to Keep Forever."
            ),
        )

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
            load_warning=(
                "Retention settings file is not valid JSON; automatic deletion has "
                "been disabled (Keep Forever)."
            ),
        )

    if not isinstance(payload, dict):
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
            load_warning=(
                "Retention settings file has an unexpected structure; defaulted safely "
                "to Keep Forever."
            ),
        )

    version = payload.get("settings_version")
    if isinstance(version, bool) or version != RETENTION_SETTINGS_VERSION:
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
            load_warning=(
                f"Unsupported retention settings version ({version!r}); defaulted safely "
                "to Keep Forever."
            ),
        )

    raw_policy = payload.get("policy")
    if not isinstance(raw_policy, str):
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
            load_warning=(
                "Retention policy value is missing or invalid; defaulted safely "
                "to Keep Forever."
            ),
        )
    try:
        policy = RetentionPolicy(raw_policy)
    except ValueError:
        return RetentionSettings(
            policy=RetentionPolicy.KEEP_FOREVER,
            settings_path=str(target),
            load_warning=(
                f"Unknown retention policy {raw_policy!r}; automatic deletion has "
                "been disabled (Keep Forever)."
            ),
        )

    updated_at: datetime | None = None
    raw_updated = payload.get("updated_at_utc")
    if raw_updated is not None:
        if not isinstance(raw_updated, str):
            return RetentionSettings(
                policy=RetentionPolicy.KEEP_FOREVER,
                settings_path=str(target),
                load_warning=(
                    "Retention settings timestamp is invalid; defaulted safely "
                    "to Keep Forever."
                ),
            )
        try:
            updated_at = parse_utc_timestamp(raw_updated)
        except ValueError:
            return RetentionSettings(
                policy=RetentionPolicy.KEEP_FOREVER,
                settings_path=str(target),
                load_warning=(
                    "Retention settings timestamp could not be parsed; defaulted safely "
                    "to Keep Forever."
                ),
            )

    return RetentionSettings(
        policy=policy,
        settings_version=RETENTION_SETTINGS_VERSION,
        updated_at=updated_at,
        settings_path=str(target),
        load_warning=None,
    )


def save_retention_settings(
    path: Path | str,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> RetentionSettings:
    """Persist ``policy`` atomically to ``path`` using temp-file + fsync + replace."""
    if not isinstance(policy, RetentionPolicy):
        raise ValueError("policy must be a RetentionPolicy enum value.")
    target = Path(path)
    ensure_storage_directory(target)

    ts = now if now is not None else datetime.now(timezone.utc)
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("updated_at timestamp must be timezone-aware.")
    ts_utc = ts.astimezone(timezone.utc)

    payload = {
        "settings_version": RETENTION_SETTINGS_VERSION,
        "policy": policy.value,
        "retention_days": policy.days,
        "updated_at_utc": format_utc_timestamp(ts_utc),
    }
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(target.parent),
            prefix=".speedpup-retention-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
        os.replace(tmp_path, target)
        tmp_path = None
    except OSError as exc:
        raise HistoryAccessError(
            f"Could not save retention settings: {exc.__class__.__name__}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return RetentionSettings(
        policy=policy,
        settings_version=RETENTION_SETTINGS_VERSION,
        updated_at=ts_utc,
        settings_path=str(target),
        load_warning=None,
    )

# Phase 4A — Local History & SQLite Storage Foundation

## 1. Overview & Scope

Phase 4A introduces a durable, local, versioned SQLite persistence foundation for completed **Speed Test** and **Diagnostics** measurements using Python's standard-library `sqlite3` module (no ORM or third-party database dependency).

This slice provides the storage layer, typed repository/query contracts, and nonfatal off-GUI-thread persistence integration. It intentionally does **not** implement the Analysis page GUI, History page GUI, historical charts, connection intelligence rules, retention enforcement, deletion controls, CSV/JSON exports, continuous Live Monitor persistence, Rust acceleration, or packaging changes.

---

## 2. Persistence Architecture

The persistence foundation establishes explicit boundaries across four layers:

1. **Measurement execution (unchanged sealed core)**
   - `speedpup.core.speedtest` / `speedpup.core.models` (`Server`, `TestProgress`, `TestResult`)
   - `speedpup.core.diagnostics` / `speedpup.core.diagnostics_backend` (`DiagnosticResult`, `Measurement`, `DnsPerformance`, `DiagnosticCheck`, `DiagnosticCoverageItem`, `DiagnosticRecommendation`, `LatencyLoadSample`, `ConnectionDetails`)
   - Measurement execution remains decoupled from SQLite and unaware of database storage.
2. **Qt-independent persistent contracts (`speedpup.core.history`)**
   - Defines `HistoryRecord`, `SpeedTestHistoryDetail`, `DiagnosticsHistoryDetail`, `NetworkContext`, `ServerIdentity`, `SpeedTestSample`, `HistoryQuery`, `HistoryPage`, `SaveResult`, typed `HistoryStorageError` exceptions, and the `HistoryRepository` protocol.
   - Reuses sealed measurement models (`Measurement`, `DiagnosticProfile`, `DiagnosticLoadBudget`, `BufferbloatGrade`, `DnsPerformance`, `DiagnosticCheck`, `DiagnosticCoverageItem`, `DiagnosticRecommendation`, `LatencyLoadSample`) directly rather than duplicating them.
   - Converts completed runtime outcomes via `record_from_speed_test()` and `record_from_diagnostics()` while enforcing privacy sanitization and measurement invariants.
3. **Storage location & SQLite engine (`speedpup.core.history_paths`, `speedpup.core.history_store`)**
   - `history_paths.py` resolves cross-platform per-user storage locations without touching the filesystem or depending on the current working directory.
   - `history_store.py` implements `SQLiteHistoryStore`, schema initialization, transactional migrations, atomic multi-table writes, deduplication, and parameterized queries.
   - Both modules are strictly Qt-independent and never import `PySide6`.
4. **Application integration (`speedpup.gui.history_writer`, `speedpup.gui.workers`, `speedpup.gui.main_window`, `speedpup.gui.diagnostics_view`)**
   - Connects completed Speed Test and Diagnostics worker results to `HistoryWriter`, a self-draining off-GUI-thread Qt coordinator.
   - Leaves the GUI thread unblocked, preserves successful measurements when storage fails, and exposes `MainWindow.history_store` for future Phase 4 Analysis and History views.

---

## 3. Storage Location

Database path resolution is handled by `speedpup.core.history_paths.resolve_data_dir()` and `resolve_history_db_path()`:

1. **Explicit path override (tests / programmatic callers):**
   - Passing `db_path` to `SQLiteHistoryStore(db_path)` or `resolve_history_db_path(explicit_path)` uses that local filesystem path directly (rejecting empty strings, NUL bytes, `:memory:`, and `file:` URIs).
2. **Portable / custom data directory (`SPEEDPUP_DATA_DIR`):**
   - If environment variable `SPEEDPUP_DATA_DIR` is set, the database is placed at `<SPEEDPUP_DATA_DIR>/history.sqlite3`.
   - `SPEEDPUP_DATA_DIR` **must** be an absolute path; a relative path raises `HistoryPathError` rather than silently depending on the current working directory or falling back to the user's home directory. This preserves clean compatibility with future portable launchers.
3. **Platform per-user application data directory (default):**
   - **Linux / POSIX:** `$XDG_DATA_HOME/speedpup-portable/history.sqlite3` when `XDG_DATA_HOME` is set and absolute (per XDG Base Directory Specification, relative values are ignored); otherwise `~/.local/share/speedpup-portable/history.sqlite3`.
   - **Windows:** `%LOCALAPPDATA%\TaskPuppyKreations\SpeedPup Portable\history.sqlite3` (falling back to `%APPDATA%`, then `~\AppData\Local`), aligning with `QApplication` organization/application naming (`TaskPuppyKreations` / `SpeedPup Portable`) while keeping local SQLite files out of roaming profiles.
   - **macOS:** `~/Library/Application Support/SpeedPup Portable/history.sqlite3`.

### Directory & Permission Behavior

- Path resolution is pure and performs no filesystem I/O.
- `SQLiteHistoryStore` is lazy: constructing it or performing reads before any history exists does not create directories or files on disk.
- Parent directories are created lazily on `initialize()` or the first write via `ensure_storage_directory()`. On POSIX systems, newly created storage directories use mode `0o700` and newly created database files use mode `0o600` (best-effort where supported by the filesystem).
- Default resolution never depends on `os.getcwd()` and never writes into the source repository.
- Directory creation failures, permission denials, read-only targets, or path conflicts (such as a file blocking directory creation) raise `HistoryAccessError`.

---

## 4. Database Schema & Versioning

Current schema version: **`SCHEMA_VERSION = 1`**.

Schema version is tracked in **both**:
- SQLite's database header `PRAGMA user_version` (integer `1`), and
- Table `schema_migrations (version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL, description TEXT NOT NULL)`.

### Timestamp Semantics

All persisted timestamps (`started_at_utc`, `completed_at_utc`, `applied_at_utc`, and optional sample `timestamp_utc`) require timezone-aware `datetime` inputs (naive datetimes are rejected with `ValueError`) and are normalized to UTC in canonical fixed-width 27-character ISO-8601 format:

```text
YYYY-MM-DDTHH:MM:SS.ffffffZ
```

Fixed-width UTC formatting ensures unambiguous timezone semantics, lossless microsecond round-tripping, and exact chronological lexicographical comparison in SQLite `ORDER BY` and range filters.

### Tables (Schema Version 1)

| Table | Purpose | Key Constraints & Relations |
| --- | --- | --- |
| `schema_migrations` | Ordered record of applied schema revisions | `version PRIMARY KEY CHECK (version >= 1)` |
| `measurements` | Shared metadata and privacy-minimized network context for every completed record | `record_id PRIMARY KEY`, `dedupe_key UNIQUE`, `measurement_type IN ('speed_test', 'diagnostics')`, `outcome IN ('completed', 'completed_with_unavailable')`, availability `CHECK` invariants |
| `speed_test_results` | Speed Test throughput, HTTP latency, and sanitized server identity | 1:1 `record_id REFERENCES measurements ON DELETE CASCADE`, `latency_protocol = 'http'` |
| `speed_test_samples` | Genuine download/upload `TestProgress` throughput samples | 1:N `REFERENCES speed_test_results ON DELETE CASCADE`, PK `(record_id, sample_index)` |
| `diagnostic_results` | Diagnostics profile, ICMP latency, jitter, packet loss, DNS, bufferbloat grade/qualifications, loaded latencies/increases, sparklines | 1:1 `record_id REFERENCES measurements ON DELETE CASCADE`, `latency_protocol = 'icmp'`, `CHECK` constraints enforcing unavailable values remain SQL `NULL` |
| `diagnostic_checks` | Ordered checklist outcomes (`DiagnosticCheck`) | 1:N `REFERENCES diagnostic_results ON DELETE CASCADE`, PK `(record_id, check_order)`, `UNIQUE (record_id, check_id)` |
| `diagnostic_coverage` | Ordered coverage status (`DiagnosticCoverageItem`) | 1:N `REFERENCES diagnostic_results ON DELETE CASCADE`, PK `(record_id, item_order)`, `UNIQUE (record_id, item_id)` |
| `diagnostic_recommendations` | Ordered evidence-backed recommendations (`DiagnosticRecommendation`) | 1:N `REFERENCES diagnostic_results ON DELETE CASCADE`, PK `(record_id, rec_order)` |
| `diagnostic_samples` | Ordered `LatencyLoadSample` chart series preserving `NULL` gaps | 1:N `REFERENCES diagnostic_results ON DELETE CASCADE`, PK `(record_id, sample_index)` |

### Indexes

- `idx_measurements_completed_at (completed_at_utc DESC, record_id DESC)` — chronological listing, pagination, and future retention cutoffs.
- `idx_measurements_type_completed_at (measurement_type, completed_at_utc DESC, record_id DESC)` — type + date-range filtering.
- `idx_measurements_interface_completed_at (interface_name, completed_at_utc DESC, record_id DESC)` — future Connection Analysis interface filtering.
- Composite primary keys on all child tables index `record_id` lookups and `ON DELETE CASCADE` cleanup.

---

## 5. Record Identities & Measurement Contracts

- **Stable identity:** Each persisted record has a non-empty UUIDv4 `record_id` (`PRIMARY KEY`).
- **Duplicate insertion prevention:** `record_from_speed_test()` and `record_from_diagnostics()` compute a deterministic SHA-256 `dedupe_key` from the completed measurement's canonical facts (type, timestamps, server/request parameters, and measured values). Both `record_id` (`PRIMARY KEY`) and `dedupe_key` (`UNIQUE`) are enforced inside the write transaction and across concurrent writers. Submitting the same completed record or runtime result again returns `SaveResult(record_id=existing_id, inserted=False)` without duplicating parent or child rows.
- **HTTP vs ICMP latency distinction:** Speed Test latency is stored in `speed_test_results.http_latency_ms` (`latency_protocol = 'http'`) and `SpeedTestHistoryDetail.http_latency_ms`. Diagnostics latency is stored in `diagnostic_results.icmp_latency_ms` (`latency_protocol = 'icmp'`) and `DiagnosticsHistoryDetail.icmp_latency_ms`.
- **No throughput conflation:** Diagnostic load transfer activity (used during Full profile bufferbloat estimation) is never inserted into `speed_test_results` or represented as Speed Test throughput.
- **Unavailable measurement preservation:** Every optional/fallible metric uses `Measurement[T]` and paired SQL columns (`<metric>`, `<metric>_available`, `<metric>_reason`) protected by SQLite `CHECK` constraints so unavailable measurements always persist as SQL `NULL` (`available=False, value=None`) with their failure/unavailability reason, never as `0`. Completed diagnostics containing unavailable individual metrics remain valid history records with `outcome = CompletionOutcome.COMPLETED_WITH_UNAVAILABLE`.
- **Bufferbloat qualification:** `DiagnosticsHistoryDetail.bufferbloat_saturation_established` is persisted explicitly (`0` / `False` for current diagnostics) alongside the full `bufferbloat_note` and `qualification_notes` preserving the qualification that access-link saturation is not established.
- **Load failures (Phase 4E.4D):** The supervisor discards child stderr and uses application-defined messages for launch failure, missing or invalid results, nonzero exit, deadline expiry, endpoint failure, and cleanup failure. Only bounded numeric statuses and recognized endpoint conditions are passed through as reconstructed messages. Successful survivor and partial-worker qualifications retain their existing meaning. Raw exception messages and tracebacks are not copied into new load reasons or the opt-in trace.
- **Earlier unsealed builds:** History screens known legacy raw-load wrappers in qualification fields, and browsing, analysis, search, and export use the screened bufferbloat note. This does not rewrite legacy records or existing exports, and it is not a general scrubber for arbitrary private text. Keep older databases, backups, and exports local until reviewed. If a record should be removed, use the explicit History deletion workflow after review. Deletion is not forensic erasure. Re-export from the corrected build and inspect previously generated reports before sharing.
- **Cancellation & incomplete runs:** `record_from_diagnostics()` rejects any `DiagnosticResult` whose `state` is not `DiagnosticRunState.COMPLETED`. Both `NetworkWorker` / `DiagnosticsWorker` and `MainWindow` check `cancel_event` before emitting/persisting results, so cancelled, failed, or unavailable runs are never recorded as completed history.

---

## 6. Connection Ownership, Concurrency & Transaction Boundaries

- **Connection ownership:** `SQLiteHistoryStore` opens a fresh `sqlite3.Connection` per operation on the calling thread and closes it deterministically in `contextlib.closing(...)`. No `sqlite3.Connection` is retained across calls or shared across threads, avoiding cross-thread SQLite misuse, preventing dangling Windows file locks, and allowing SQLite to checkpoint the WAL cleanly when idle.
- **Manual transaction control:** Connections open with `isolation_level=None` so both DDL (schema initialization and migrations) and multi-table writes run inside explicit `BEGIN IMMEDIATE` ... `COMMIT` / `ROLLBACK` transactions consistently across Python 3.11–3.14.
- **Pragmas:**
  - Every connection sets `PRAGMA foreign_keys = ON` and `PRAGMA busy_timeout = 5000` (configurable).
  - Write connections set `PRAGMA synchronous = FULL` and enable `PRAGMA journal_mode = WAL` **only after** preflight checks confirm the database is either empty (`user_version = 0` with no user objects) or a valid SpeedPup database at a supported version.
  - SQLite can require a writable history directory even for browsing, because it may need to create or update WAL sidecars. Read-only WAL failures report storage access guidance. SpeedPup does not change the journal mode, permissions, or data to force a read.
  - A write that violates a SQLite constraint rolls back with `HistoryIntegrityError`. That error identifies rejected application data without logging raw SQLite values in the user-facing message. Corruption and incompatible or newer schemas continue to use their distinct errors.
- **Atomic writes & read snapshots:**
  - `save_record()` inserts `measurements`, its detail table row, and all child rows (`speed_test_samples` or `diagnostic_checks`, `diagnostic_coverage`, `diagnostic_recommendations`, `diagnostic_samples`) in one `BEGIN IMMEDIATE` transaction. Any error triggers `ROLLBACK`, leaving zero partial rows.
  - `list_records()` runs `SELECT COUNT(*)`, paged parent retrieval, and chunked child hydration within one `BEGIN DEFERRED` read transaction so `total_count` and returned rows reflect a consistent snapshot.
- **Qt worker integration:** `HistoryWriter` (owned by `MainWindow`) queues completed `HistoryRecord` objects and drains them sequentially on a lazily spawned `_HistoryDrainThread(QThread)`. When idle, no background thread remains running. If `MainWindow.closeEvent()` is called while a persistence write is in flight, closing defers via `_close_when_idle` until `HistoryWriter.idle` fires after the atomic commit completes; if `MainWindow` owns the default store, it calls `store.close()` before accepting shutdown.

---

## 7. Migration Strategy

Forward schema upgrades follow an explicit, transactional mechanism in `speedpup.core.history_store`:

- Registered migrations live in `MIGRATIONS: Mapping[int, SchemaMigration]`, keyed by target version (`2` upgrades `1 -> 2`, etc.).
- Because Phase 4A is schema version 1, `MIGRATIONS` is intentionally empty in production (no speculative migrations).
- When opening a database with `0 < user_version < SCHEMA_VERSION`:
  1. Preflight verifies `schema_migrations` exists and matches `user_version`.
  2. Inside `BEGIN IMMEDIATE`, each step `v -> v + 1` runs `migration.apply(conn)`, records a row in `schema_migrations`, and updates `PRAGMA user_version`.
  3. On any error or missing step, the transaction rolls back completely and raises `HistoryMigrationError`, leaving the prior version and data intact.
- **Safety for unsupported, foreign, or corrupt files:**
  - `user_version > SCHEMA_VERSION` (or `MAX(schema_migrations.version) > SCHEMA_VERSION`) raises `UnsupportedSchemaVersionError`.
  - Unversioned SQLite databases containing existing tables (`user_version == 0`) or incomplete versioned databases raise `HistorySchemaError`.
  - Malformed or non-SQLite files raise `HistoryCorruptionError`.
  - In all of these cases, the file is **never** overwritten, reset, deleted, or header-modified.

---

## 8. Privacy Boundaries & Failure Handling

### What Is Stored Locally

- Measurement timestamps (UTC), outcome, summary, and application version.
- Privacy-minimized interface context: active interface name (e.g., `eth0`, `wlo1`), reported link speed, operational status (`Up`/`Down`), and optional future `connection_type`.
- Speed Test metrics: download/upload Mbps, HTTP latency ms, genuine download/upload throughput samples, and sanitized server identity (`server_id`, `name`, `sponsor`, and hostname/non-default port only).
- Diagnostics metrics: selected profile, configured target IP (if explicitly set), configured resolver (if explicitly set), load budget, ICMP latency, jitter, packet loss, DNS timing/failure counts, bufferbloat estimated grade/loaded latencies/increases/qualification note, checks, coverage, recommendations, sparklines, and `LatencyLoadSample` chart series.

### Explicitly Excluded From Storage

- Credentials or authentication secrets (stripped from any URL before persistence).
- Raw HTTP request/response bodies.
- Endpoint paths, fragments, and query parameters (e.g. tokens or `ckSize`).
- Raw `DiagnosticTrace` JSONL files.
- Unnecessary local network identifiers: MAC addresses, host IPv4 addresses, host IPv6 addresses, default gateway IPs, and Wi-Fi SSIDs/BSSIDs.
- Continuous Live Monitor counter samples (`NetworkMonitor` / `MonitorPoint`).

### Privacy Limitations

- History is stored in an unencrypted local SQLite file within the user's application data directory (protected by OS user account permissions, `0o700`/`0o600` on POSIX). Any local process running under the same user account can read it.
- Interface names (e.g., `wlo1`, `Ethernet`) and public server names/hosts can reveal approximate network environment or region.
- If a user explicitly runs Diagnostics against a private/internal target IP or custom resolver IP, that configured parameter is stored locally as measurement provenance.
- No cloud synchronization, remote telemetry, background tests, or automatic network configuration changes exist.

### Nonfatal Failure Handling

- Core store methods raise explicit typed errors (`HistoryPathError`, `HistoryAccessError`, `HistorySchemaError`, `UnsupportedSchemaVersionError`, `HistoryMigrationError`, `HistoryCorruptionError`).
- At the application boundary (`HistoryWriter` + `MainWindow`), persistence errors are strictly nonfatal:
  - Completed Speed Test and Diagnostics results remain displayed and usable.
  - `MainWindow` logs a warning to `speedpup.history`, records `last_persistence_error`, emits `history_save_failed(measurement_type, message)`, and appends a nonfatal warning notice in `WARNING` style to the active page's `status_label` without clearing measurements or showing a blocking modal dialog.

---

## 9. Public Repository & Query Interface

Future Phase 4 consumers (`Analysis` and `History` pages) should depend on the typed `HistoryRepository` protocol in `speedpup.core.history` (exposed on `MainWindow.history_store`):

- `save_record(record: HistoryRecord) -> SaveResult`
- `save_speed_test(result: TestResult, *, started_at=None, network=None, samples=(), record_id=None) -> SaveResult`
- `save_diagnostics(result: DiagnosticResult, *, completed_at=None, record_id=None) -> SaveResult`
- `get_record(record_id: str) -> HistoryRecord | None`
- `list_records(query: HistoryQuery | None = None) -> HistoryPage`
- `count_records(*, measurement_type=None, start_at=None, end_at=None, interface_name=None) -> int`
- `close() -> None`

`HistoryQuery` supports:
- `measurement_type`: `None`, `MeasurementType.SPEED_TEST`, or `MeasurementType.DIAGNOSTICS`
- `start_at` / `end_at`: inclusive timezone-aware bounds on `completed_at`
- `interface_name`: exact active interface name filter (for Analysis interface dropdown)
- `order`: `SortOrder.NEWEST_FIRST` or `SortOrder.OLDEST_FIRST` (deterministic tie-break on `record_id`)
- `limit` (`1..1000`, default `50`) and `offset` (`>= 0`) for bounded pagination
- `include_samples` (default `True`; callers rendering compact lists may set `False` to skip sample rows)

`HistoryPage` returns `records`, `total_count`, `limit`, `offset`, and `has_more`.

---

## 10. Consequential Architectural Decisions & Alternatives Considered

1. **Standard-library `sqlite3` with explicit parameterized SQL vs. ORM / third-party library**
   - *Decision:* Standard-library `sqlite3` only.
   - *Rationale:* Zero added runtime dependencies, predictable portable packaging, direct control over `PRAGMA`s, `isolation_level=None` transaction boundaries, and exact SQLite `CHECK` constraints.
2. **Normalized detail & child tables vs. single JSON document column**
   - *Decision:* Shared `measurements` table + separate `speed_test_results` and `diagnostic_results` detail tables + normalized child tables for samples, checks, coverage, and recommendations (with bounded JSON arrays only for small sparkline float tuples and qualification note strings).
   - *Rationale:* Enforces HTTP vs ICMP separation and `NULL`/availability invariants at the database schema level, keeps future Analysis/History queries indexed and simple, and supports clean `ON DELETE CASCADE` deletion without parsing large blobs.
3. **Per-operation scoped connections vs. long-lived shared connection**
   - *Decision:* Short-lived per-operation connections closed via `closing(...)` on the calling thread.
   - *Rationale:* Eliminates cross-thread SQLite ownership bugs, avoids dangling Windows file handles during cleanup/backup, and lets SQLite checkpoint WAL files automatically between tests while keeping human-triggered save latency negligible.
4. **Dedicated self-draining `HistoryWriter` vs. writing directly inside `NetworkWorker` / `DiagnosticsWorker`**
   - *Decision:* Dedicated `HistoryWriter` coordinated from `MainWindow`'s completed-result handlers.
   - *Rationale:* Preserves existing worker signatures and signal contracts (avoiding breaking sealed worker lifecycles/tests), serializes all DB writes cleanly off the GUI thread, keeps idle thread count at zero, and integrates cleanly with `MainWindow.closeEvent()`.
5. **Privacy-minimized `NetworkContext` vs. persisting full `ConnectionDetails` / raw `Server` URLs**
   - *Decision:* Persist only `interface_name`, `link_speed_mbps`, `status`, optional `connection_type`, and sanitized `server_host`; omit MAC addresses, local IPv4/IPv6 addresses, gateway IPs, and URL query strings/credentials.
   - *Rationale:* Retains everything needed for History and Connection Analysis (such as per-interface filtering and link-speed context) while avoiding unnecessary local network fingerprinting or credential/query leakage.

---

## 11. Known Limitations & Future Integration Points

- **No UI yet:** Analysis and History pages remain placeholder views in Phase 4A; they will read directly from `MainWindow.history_store` (`HistoryRepository`) in upcoming Phase 4 slices.
- **No retention, deletion, or export APIs yet:** The schema already includes indexed `completed_at_utc`, stable `record_id`, and `ON DELETE CASCADE` foreign keys so future Phase 4 slices can add retention pruning, single/bulk deletion, and CSV/JSON export without a schema redesign.
- **No pre-history backfill:** Measurements executed before Phase 4A are not fabricated or imported.
- **Independent review pending:** Per project policy, an independent durability, migration, and privacy review is still required before Phase 4 can be sealed.

# Phase 4D — History Management, Export & Retention

Phase 4D adds local, user-controlled history export (CSV, structured JSON, and plain-text connection reports), transactional measurement deletion across individual/multi-selected/filtered/all scopes, and persistent, fail-safe automatic retention. All operations execute locally with zero network traffic and preserve SQLite schema version 1, Phase 4A privacy exclusions, and Phase 4C intelligence rules.

## Management architecture

Phase 4D keeps database I/O, serialization, retention policy, and Qt presentation cleanly separated:

| Layer | Module | Responsibility |
| --- | --- | --- |
| Persistence contracts | `src/speedpup/core/history.py` | `DeleteScopeKind`, `DeleteScopeAuthorization`, `DeleteResult`, `HistoryScopeChangedError`. |
| SQLite operations | `src/speedpup/core/history_store.py` | Transactional deletes, scope-fingerprint authorization, retention cutoff count/cleanup, and single-transaction WAL export snapshots (`open_export_snapshot`). |
| Export & report engine | `src/speedpup/core/history_export.py` | Qt-independent CSV/JSON streaming serializers, plain-text connection report renderer, formula-injection sanitization, destination validation, and atomic file publication. |
| Retention settings | `src/speedpup/core/retention.py` | Qt-independent `RetentionPolicy`, fail-safe `retention.json` loading/saving, and UTC cutoff boundary calculation. |
| Serialized mutation writer | `src/speedpup/gui/history_writer.py` | Single off-GUI-thread FIFO writer executing completed-run saves, authorized manual deletions, and automatic/manual retention cleanups sequentially. |
| Async export worker | `src/speedpup/gui/history_exporter.py` | Dedicated off-GUI-thread read-only export worker coordinated behind pending writes. |
| Confirmation & History UI | `src/speedpup/gui/history_dialogs.py`, `src/speedpup/gui/history_view.py`, `src/speedpup/gui/main_window.py` | Explicit scope/privacy/destructive confirmation dialogs, multi-row page selection, retention controls, cross-page invalidation, and clean shutdown draining. |

### Consequential decisions & alternatives considered

- **Single serialized mutation writer (`HistoryWriter`) vs. separate deletion/retention worker:** Extending `HistoryWriter` into one FIFO queue guarantees saves, manual deletions, and retention cleanups never contend or race each other on SQLite locks, and allows `MainWindow.closeEvent()` to drain all mutations uniformly.
- **Streaming SHA-256 scope fingerprint vs. materializing all record IDs in memory for filtered/clear-all confirmation:** Computing `(expected_count, sha256(ordered record_id|completed_at_utc))` in bounded `fetchmany(500)` batches uses O(1) memory even for very large histories while guaranteeing that any concurrent insert or delete between confirmation and execution aborts cleanly via `HistoryScopeChangedError`.
- **Dedicated `retention.json` file vs. SQLite settings table:** Keeping retention settings in a small versioned JSON file alongside `history.sqlite3` avoids a schema v2 migration, requires no general-purpose settings framework, keeps the database strictly measurement-only, and lets unreadable/corrupted settings fail safely to `Keep Forever` without touching SQLite.
- **Plain-text UTF-8 report vs. PDF/DOCX:** Avoids introducing third-party document/PDF dependencies while producing an accessible, inspectable report easy to paste or attach when contacting an ISP or support technician.

## Export formats & scopes

Supported explicit scopes (`ExportScopeKind`):

1. **Selected measurement (single)** — exact stable `record_id`.
2. **Selected measurements (multiple)** — exact stable `record_id`s selected on the current History page.
3. **All measurements matching current filters** — complete filtered database population across *all* pages (never limited to the currently visible page).
4. **All saved measurements** — entire local measurement history.

Before opening the Qt save dialog, `ExportOptionsDialog` requires explicit confirmation of format, exact scope, record count, and privacy notice. All SQLite exports execute inside one `BEGIN DEFERRED` WAL snapshot (`open_export_snapshot`), yielding records in bounded chunks (default `100` rows per batch) so memory stays bounded and concurrent writes cannot produce torn exports. A long-running read snapshot can temporarily hold back checkpoint progress and let the WAL grow while other connections write. Closing the snapshot releases that constraint and SQLite can reclaim or reuse WAL space according to its checkpoint behavior.

### CSV export (`CSV_COLUMNS`)

- **Encoding & dialect:** UTF-8 (`encoding="utf-8"`), RFC 4180 escaping via Python's `csv.writer`, CRLF (`\r\n`) line endings.
- **Timestamps:** Canonical UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SS.ffffffZ`).
- **Missing vs. zero vs. not-applicable values:**
  - Numeric measurement columns contain finite decimal numbers (`0`, `14.2`, `342.8`) when measured, or empty string `""` when unavailable or not applicable.
  - Explicit status/protocol columns (`measurement_type`, `diagnostic_profile`, `latency_protocol`, `packet_loss_status`, `dns_status`, `bufferbloat_grade_estimated`, `bufferbloat_saturation_established`) distinguish `measured` `0` from `unavailable` and `not_applicable`, separate HTTP vs. ICMP latency, separate Speed Test throughput from Diagnostics load, and carry estimated bufferbloat qualifications.
- **Columns:** `record_id`, `measurement_type`, `diagnostic_profile`, `completed_at_utc`, `started_at_utc`, `network_interface`, `interface_status`, `link_speed_mbps`, `completion_status`, `speed_test_download_mbps`, `speed_test_upload_mbps`, `http_latency_ms`, `latency_protocol`, `server_id`, `server_name`, `server_sponsor`, `server_host`, `diagnostic_target`, `diagnostic_icmp_latency_ms`, `diagnostic_jitter_ms`, `diagnostic_packet_loss_pct`, `packet_loss_status`, `dns_resolver`, `dns_median_lookup_ms`, `dns_slowest_lookup_ms`, `dns_failures_count`, `dns_status`, `bufferbloat_grade_estimated`, `bufferbloat_saturation_established`, `baseline_icmp_latency_ms`, `download_loaded_latency_increase_ms`, `upload_loaded_latency_increase_ms`, `load_budget`, `availability_summary`, `measurement_qualifications`.
- **Formula-prefix protection (`sanitize_csv_text`):** User-influenced textual cells get a leading single quote (`'`) if the raw text starts with tab, CR, or LF, or if the first character after leading Unicode whitespace, control (`Cc`), and format (`Cf`) characters is `=`, `+`, `-`, or `@`. The original text, numeric cells, and JSON serialization retain their existing handling. Spreadsheet applications and import settings can interpret CSV differently, so this protection does not guarantee safety in every importer.

### JSON export (`export_format_version = 1`)

- **Versioning:** Uses `export_format_version` (`1`) and `export_format` (`"speedpup.history.json"`), intentionally independent of SQLite `SCHEMA_VERSION`.
- **Envelope:** Includes `exported_at_utc`, `app_version`, `scope` (`kind`, `description`, `record_count`, active `filters`), `privacy_notice`, and a streamed `records` array.
- **Structure & numeric safety:** Preserves complete stored details (including Speed Test throughput samples, Diagnostics checks, coverage, recommendations, sparklines, and latency/load samples). Unavailable measurements serialize as structured objects (`{"available": false, "value": null, "unit": ..., "reason": ...}`). Serialized with `allow_nan=False` plus explicit `math.isfinite()` checks so non-finite floats are rejected rather than emitting non-standard JSON.
- **Scope boundary:** JSON export is strictly an export format (not import, backup restoration, or sync).

### Human-readable connection report (`REPORT_FORMAT_VERSION = 1`)

- Plain UTF-8 `.txt` report generated **strictly from the requested export scope's snapshot** (never reusing unrelated on-screen Analysis state).
- Clearly separates five sections:
  1. **Measured Facts** (descriptive statistics for Speed Test HTTP metrics, Diagnostics ICMP/DNS metrics, and Full Diagnostics estimated bufferbloat grade distribution & load increases).
  2. **Historical Intelligence Observations** (deterministic heuristics with matching and evaluated counts by category and profile and a shared 512-run cap; omitted with explanation for single-record reports and explicitly scoped for manual multi-record subsets).
  3. **Individual Measurement Summaries** (up to 50 most recent runs in the scope, with an explicit note pointing to aggregate stats and CSV/JSON exports for larger scopes).
  4. **Possible Explanations & Measurement Limitations** (no unsupported ISP/router causal claims; absence of findings is never framed as proof of overall connection health).
  5. **Suggested Investigation Steps** (clearly qualified as investigation steps, not diagnoses or guaranteed remedies).
- Individual Full Diagnostics summaries include the stored, screened load qualification where available. They distinguish grading criteria under observed load from evidence of saturation, and distinguish an unavailable Full grade from a check excluded by Quick or Standard.
- DNS failure reporting separately identifies no Diagnostics, unavailable failure-count data, partial or complete data coverage, and measured zero or nonzero failures. Timing availability does not establish the outcome of every DNS lookup.

## Export privacy & atomic file publication

- **Privacy boundaries:** Preserves all Phase 4A exclusions (no local IPv4/IPv6 addresses, MAC addresses, gateways, URL credentials, query strings, raw diagnostic traces, or internal DB paths). Export options always display `EXPORT_PRIVACY_NOTICE` before writing. SpeedPup never uploads or auto-opens exported files.
- New load failure reasons use controlled classifications rather than raw child errors. The known legacy forms are withheld when read and exported. Databases and exports created by earlier unsealed builds can still contain private text on disk. Review older reports before sharing. See [History Storage](phase-4a-history-storage.md) for the legacy handling and explicit removal path.
- **Protected destinations (`validate_export_destination`):** Rejects writing to the active SQLite database (`history.sqlite3`), its SQLite sidecars (`-wal`, `-shm`, `-journal`), `retention.json`, and equivalent resolved paths, symlinks, or hard links (`os.path.samefile`).
- **Overwrite confirmation:** SpeedPup applies the required extension before checking the final filename. The application shows the full path and defaults to **Cancel** when the destination already exists. Replacement approval applies to the observed regular file at that path. An existing destination symlink is ineligible for replacement.
- **Atomic publication (`atomic_write_text_file`):** Serializes into a synced temporary file in the destination directory with owner-only permissions on POSIX. New files are published with an exclusive hard link, so a path created during export is preserved. Filesystems without the required hard-link operation reject new exports with no truncation fallback. For approved replacements SpeedPup checks the destination path, file identity and metadata again immediately before atomic `os.replace()`. A detectable change aborts the export. Portable replacement has no atomic compare-and-swap, so there is a race between the final check and the replace. Failures before publication leave existing files intact and the worker removes temporary files where the filesystem permits.

## Measurement deletion & authorization

- **Manual deletion:** Use `prepare_delete_authorization` to snapshot selected, filtered, or all records, obtain confirmation, then pass that authorization to the only public manual mutation, `execute_authorized_delete`. Retention uses the separate cutoff-specific cleanup path.
- **Confirmation safeguards (`DeleteConfirmationDialog`):**
  - Displays the operation, scope, exact affected count, cascade notice, non-reversibility notice, and honest disclosure that SQLite deletion does not constitute forensic secure erasure.
  - Defaults focus and Enter to **Cancel**.
  - **Clear All History** requires checking an explicit confirmation checkbox before the destructive button enables.
- **Scope-change guard:** For filtered and clear-all deletions, `execute_authorized_delete` verifies `(count, snapshot_fingerprint)` inside `BEGIN IMMEDIATE`; if new runs arrived or records changed since confirmation, the transaction rolls back and raises `HistoryScopeChangedError`, requiring renewed confirmation.
- **Selected records:** Duplicate IDs count once in first-seen order. The request count is the number of distinct requested IDs. The affected count is the number that existed at confirmation, and the missing count is the number excluded at that point. A later arrival with a previously missing ID stays excluded. The transaction revalidates the confirmed records, and if any disappear or their identity fingerprint changes, it rejects the entire deletion and requires renewed confirmation. The deleted count reports actual deleted rows.
- **Transaction & cascade behavior:** All deletions run inside `BEGIN IMMEDIATE` with `PRAGMA foreign_keys = ON` and parameterized chunked SQL (`_IN_CHUNK_SIZE = 200`). Deleting from `measurements` cascades automatically to `speed_test_results`, `speed_test_samples`, `diagnostic_results`, `diagnostic_checks`, `diagnostic_coverage`, `diagnostic_recommendations`, and `diagnostic_samples`, leaving zero orphaned rows. Clear All preserves the SQLite file, `schema_migrations`, and `PRAGMA user_version = 1`. It does not run `VACUUM` automatically.

## Retention settings & enforcement

- **Policies (`RetentionPolicy`):** `Keep Forever` (**default**), `30 days`, `90 days`, `180 days`, `365 days`.
- **Storage location:** `retention.json` in the per-user application data directory alongside `history.sqlite3` (or alongside an explicit DB path / explicit test path), written atomically via temp-file + `fsync` + `os.replace` (`0o600` on POSIX). Saving settings can create the parent directory even before the first measurement is saved.
- **Fail-safe validation:** Missing files default cleanly to `Keep Forever` without creating files. Unreadable, corrupted, unknown-version, invalid-policy, or bad-timestamp files fail safely to `Keep Forever` with a nonfatal UI warning (`retention_warning_label`), never reverting to a destructive policy.
- **UTC cutoff semantics:** `cutoff_utc = now_utc - timedelta(days=N)`. Records with `completed_at_utc < cutoff_utc` are expired; records with `completed_at_utc >= cutoff_utc` (including exact boundary equality) are retained.
- **Confirmation & separation:** Enabling or shortening a finite policy shows `RetentionConfirmationDialog` with the selected period, current eligible record count, and disclosure that future expired records will be removed automatically without per-run confirmation. Saving configuration does *not* immediately run cleanup; immediate enforcement is a separate, explicitly confirmed `Run Cleanup Now` action.
- **Automatic enforcement boundaries:** When a finite policy is active, automatic cleanup is queued on `HistoryWriter` (1) at application startup and (2) after a completed measurement is saved (`trigger="after_save"`). No permanent background polling thread is created. Retention failures are nonfatal and never invalidate a completed measurement.
- **Policy ordering:** The writer persists policy requests in request order, ahead of cleanup that is still queued. A successful policy change advances the revision and invalidates cleanup queued for older revisions. Once the worker takes a cleanup, any subsequent policy request waits until that operation commits or rolls back. While waiting, the GUI shows the prior policy as active. If saving fails, the prior policy remains active and the GUI reports the failure. This ordering is local to one application instance. Separate processes using the same storage do not coordinate retention policy changes, even though SQLite serializes their database writes.

## Cross-page coherence, failure recovery & shutdown

- Every successful deletion or retention cleanup that removes records invokes `_invalidate_and_refresh_historical_views()`, incrementing `_history_mutation_version`, invalidating `HistoryReader`, `AnalysisReader`, and the Speed Test `AnalysisReader`, discarding stale pre-mutation async responses, and refreshing active views coherently.
- Current-session Speed Test live gauges, throughput chart, and `Current Speed Test` card remain completely unaffected when historical copies are deleted.
- Exports started while writes are pending wait cleanly for `HistoryWriter` to drain before opening their WAL snapshot.
- `MainWindow.closeEvent()` coordinates and waits for in-flight `HistoryWriter` mutations, `HistoryExporter` jobs, and all reader threads before closing the store.

## Native acceptance procedure

Using an isolated test directory (`SPEEDPUP_DATA_DIR=/tmp/speedpup-4d-test`):

1. Run at least 2 Speed Tests and 1 Full Diagnostic; open **History** and verify `Keep Forever` is active by default.
2. Select 1 row, then Ctrl/Shift-select multiple rows; verify the page-local selection badge updates and `Export…` / `Delete Selected` reflect the selection accurately.
3. Export CSV, JSON, and Connection Report (`.txt`) across selected, filtered, and all scopes; inspect output files in a spreadsheet/editor to confirm units, UTF-8 text, formula protection, structured JSON details, and plain-text report clarity.
4. Attempt to export directly onto `history.sqlite3` and confirm SpeedPup refuses safely without altering the database.
5. Delete one selected record, apply a filter and delete filtered history, and test cancelling a confirmation dialog (confirm Cancel is default and leaves data intact).
6. Enable a finite retention policy (verify confirmation shows eligible count and saving does not surprise-delete), test `Run Cleanup Now`, restart the app to verify policy persistence, and verify `Clear All History` requires checkbox confirmation, updates Analysis & Speed Test sidebars, preserves any live current-session Speed Test result, and allows new measurements to persist normally afterward.

## Known limitations

- History import, backup restoration, cloud sync, PDF export, and automated ISP submission are intentionally out of scope.
- Deletion removes rows transactionally from SQLite without automatic `VACUUM` and does not provide forensic secure erasure across SQLite free pages, filesystem snapshots, or storage media.
- Phase 4E.4C defines the category and profile allocation and documents the generic repository fallback in [Connection Intelligence](phase-4c-connection-intelligence.md).
- Remaining pre-seal checks include independent verification, native Linux chart and disclosure acceptance, retention and overwrite exercises with disposable data, and Windows and macOS hosted verification. No cross-platform or case-insensitive filesystem behavior is inferred from the Linux regression suite.
- Documented limits remain: an authorized replacement can race after its final metadata check, and retention policy ordering is local to one application instance. CSV protection has not been verified across spreadsheet importers. The bounded intelligence sample can omit older comparable groups, and the nonproduction repository fallback can materialize matching records.

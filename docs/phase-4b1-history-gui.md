# Phase 4B.1 — History GUI

## 1. Overview & Scope

Phase 4B.1 replaces the **History** placeholder page in `MainWindow` (navigation index `4`) with a read-only measurement history browser backed directly by the Phase 4A local SQLite persistence layer (`MainWindow.history_store`).

This slice covers read-only browsing, full-history search and filtering, deterministic database-side sorting, bounded pagination, adaptive measurement details, local storage inspection, and live refresh when new measurements complete. Export, deletion, retention enforcement, and Connection Analysis remain deferred to later Phase 4 slices.

---

## 2. Visual Reference & Styling

- **Visual assets inspected:** `assets/UI/Mockup UI History.png` and `assets/UI/Mockup UI Analysis.png`, together with the established styling in `src/speedpup/gui/theme.py`, `src/speedpup/gui/diagnostics_view.py`, and `src/speedpup/gui/main_window.py`.
- **Workspace note:** At implementation time, `assets/UI/Mockup UI History.png` in the workspace is byte-for-byte identical to `assets/UI/Mockup UI Analysis.png` (both showing the Connection Analysis visual reference). Both files were preserved untouched. The History page implements the full Phase 4B.1 layout specification using SpeedPup Portable's established dark purple surfaces (`#101017`, `#171520`, `#211e2d`), lime-green (`#98ea52`) and violet/purple (`#b86be7`) accents, rounded `#card` panels, status badges, and responsive two-column shell.
- The original TaskPuppy Kreations logo, sidebar navigation (7 items), window header, and application-wide `APP_STYLESHEET` are preserved without modification.

---

## 3. History Page Architecture

The History UI follows the same separation of concerns used by Diagnostics and Live Monitor:

1. **Core query extensions (`speedpup.core.history`, `speedpup.core.history_store`)**
   - Narrowly extended `HistoryQuery` with optional `search_text: str | None = None` and `sort_by: HistorySortField = HistorySortField.COMPLETED_AT` (preserving full backwards compatibility with Phase 4A defaults).
   - Added `StorageInfo` dataclass and read-only `SQLiteHistoryStore` helpers: `list_interfaces()`, `storage_info()`, and `browse_snapshot(query)` (reading one page, distinct stored interfaces, total count, and file/schema metadata inside a single consistent `BEGIN DEFERRED` snapshot).
   - Core persistence remains strictly Qt-independent and `HistoryRepository` remains backwards-compatible.
2. **Off-GUI-thread query coordinator (`speedpup.gui.history_reader`)**
   - `HistoryReader` executes bounded queries on a lazily spawned, self-draining `_HistoryQueryThread(QThread)` using a single-slot **latest-wins** queue and monotonically increasing `request_id` tokens.
   - Emits `snapshot_ready(request_id, HistorySnapshot)`, `load_failed(request_id, message, storage_unavailable, db_path)`, and `idle`.
3. **Pure presentation widget (`speedpup.gui.history_view`)**
   - `HistoryView(QScrollArea)` renders:
     - **Header & Filters card:** title, read-only badge, search box (120 ms debounce on typing; immediate on Enter), Refresh, Clear Filters, Date range (`All time`, `Today`, `Last 7 days`, `Last 30 days`), Measurement type (`All measurements`, `Speed Test`, `Diagnostics`), and Interface selector (populated from all stored interfaces in SQLite).
     - **Measurement History table card:** result counter, sort selector + ascending/descending toggle, clickable column headers mapped to database-side sorting, 9-column read-only `QTableWidget`, state stack for empty/no-match/loading/error panels, and bounded pagination (`25`, `50`, or `100` rows per page).
     - **Measurement Details card (sidebar top):** scrollable panel adapting cleanly to no selection, Speed Test records, or Diagnostics records.
     - **Local Storage card (sidebar bottom):** database location, file size, total stored count, schema version, local-only notice, and explicitly disabled deferred Phase 4D controls (`Export (Planned)`, `Delete (Planned)`).
4. **Application shell integration (`speedpup.gui.main_window`)**
   - Connects `HistoryView.query_requested`, navigation transitions (`HISTORY_PAGE_INDEX = 4`), `HistoryWriter` completion events, and `MainWindow.closeEvent()` lifecycle draining.

---

## 4. Filtering, Sorting & Pagination Semantics

- **Complete-history filtering:** Every filter (search text, date range, measurement type, interface) executes in SQLite across the entire database before applying `LIMIT ? OFFSET ?`. Searching never filters only the visible page.
- **Search coverage & safety:** `search_text` performs case-insensitive literal substring matching (`LIKE LOWER(?) ESCAPE '\'` with `\`, `%`, and `_` escaped) across meaningful stored metadata: `record_id`, measurement type/protocol keywords, outcome, summary, interface name, availability summary, sanitized server fields (`server_id`, `server_name`, `server_sponsor`, `server_host`), diagnostic profile, target, resolver, DNS resolver, bufferbloat grade, and bufferbloat note.
- **Date range semantics:**
  - `All time`: unbounded.
  - `Today`: local midnight (`00:00:00` in the user's local timezone) onward, converted to UTC for SQLite range comparison.
  - `Last 7 days` / `Last 30 days`: rolling 7-day / 30-day UTC cutoff onward.
- **Deterministic sorting:** `QTableWidget` client-side sorting is disabled (`setSortingEnabled(False)`). Clicking column headers or changing the sort controls re-queries SQLite using static hardcoded `ORDER BY` clauses with deterministic tie-breaking on `(completed_at_utc, record_id)`. Unavailable or not-applicable (`NULL`) values always sort last in both ascending and descending directions—never as `0`. Bufferbloat grades rank semantically (`A+` through `F`), not lexicographically.
- **Bounded pagination:** Page size is bounded to `25`, `50`, or `100` rows per query with `include_samples=False` so table browsing never loads unbounded history or raw sample series into memory. Changing filters, search, sort, or page size resets to page 1; if a refresh causes the current offset to exceed remaining matches, the view automatically returns to page 1.

---

## 5. Record Selection, Refresh & Write Coherence

- **Stable identity:** Each table row stores `record.record_id` in `Qt.ItemDataRole.UserRole` on column 0. Refreshing or sorting preserves selection by matching `record_id` rather than row index—even when newly saved measurements shift earlier rows downward.
- **Navigation refresh:** Navigating to the History page triggers an asynchronous refresh. No database read occurs during `MainWindow.__init__()` before History is visited.
- **Live completion visibility:** When `HistoryWriter.record_saved` fires while the History page is active, History automatically refreshes to show the new completed measurement without restarting the application.
- **Write-coherence (no write/read race):** If a refresh is requested while `HistoryWriter.is_running()` is `True`, `MainWindow` shows the loading state, invalidates older reads, and defers launching the query until `HistoryWriter.idle` fires after the atomic commit (or nonfatal save failure) completes.
- **Stale response suppression:** Each query receives a monotonically increasing `request_id`. Both `HistoryReader` and `MainWindow` drop responses whose `request_id` no longer matches the latest active request, preventing slower older queries from overwriting newer filter results.

---

## 6. Thread Ownership & Cleanup

- All SQLite read operations run inside `_HistoryQueryThread` on its worker thread, reusing `SQLiteHistoryStore`'s per-operation `closing(conn)` discipline so no `sqlite3.Connection` crosses threads or leaks open handles.
- When idle, neither `HistoryWriter` nor `HistoryReader` keeps a background thread alive.
- Navigating away from History cancels any pending search debounce timer and invalidates in-flight read responses.
- `MainWindow.closeEvent()` cancels debounce timers, invalidates pending reads, and defers window close via `_close_when_idle` until both `HistoryWriter` and `HistoryReader` emit `idle`, then closes `history_store` if owned by `MainWindow`.

---

## 7. Measurement Presentation Rules

- **HTTP vs. ICMP latency:** Speed Test latency is labeled `(HTTP)` in the table and `HTTP Latency` (`HTTP request timing · not ICMP ping`) in details. Diagnostics latency is labeled `(ICMP)` in the table and `ICMP latency` in details.
- **No throughput conflation:** Diagnostics rows display `—` in Download and Upload columns with an explicit tooltip noting that diagnostic load traffic is not Speed Test throughput; Diagnostics details never show Speed Test throughput metrics.
- **Unavailable vs. not-applicable values:** Unavailable metrics render explicitly as `Unavailable` (or `Unavailable (ICMP)`) with their stored reason—never `0` or `0.0`. Metrics excluded by type or profile (such as Bufferbloat on Quick/Standard or Speed Test) render as `—` / `Not included in this profile`.
- **Bufferbloat qualifications:** Full Diagnostics records preserve `Est. <grade>` (or `Unavailable`), the explicit `Link saturation established: No` qualification derived from `bufferbloat_saturation_established`, full `bufferbloat_note`, and baseline/loaded latencies and increases.
- **No cross-record synthesis:** Selecting a record renders strictly that record's stored values; fields from other records are never combined to fill missing metrics.
- **Privacy preservation:** Only Phase 4A's sanitized server identity and privacy-minimized interface context are read and displayed; no credentials, endpoint query parameters, MAC addresses, or local IPv4/IPv6/gateway addresses are shown or stored.

---

## 8. Empty & Error States

`HistoryView` distinguishes six explicit states:

1. **Loading (`loading`):** Status banner and empty-panel placeholder while querying or waiting for an in-flight write.
2. **Empty database (`empty`):** Shown when no measurements exist locally (`No measurements recorded yet`), without creating an empty database file on disk.
3. **No matching filters (`no_matches`):** Shown when filters or search exclude all stored records, with a quick `Clear Filters` button and stored-record count.
4. **No measurement selected:** Measurement Details panel prompts the user to select a row.
5. **History storage unavailable (`storage_unavailable`):** Shown on path, access, permission, schema, unsupported-version, or corruption errors; displays guidance and DB path without ever modifying, resetting, or deleting the file.
6. **Query failed (`query_failed`):** Shown on unexpected query errors with a `Retry` action.

Storage metadata failures alone (e.g., `storage_info()` failing) surface a nonfatal warning inside the Local Storage card without preventing history browsing. All History failures remain nonfatal to Speed Test, Live Monitor, and Diagnostics.

---

## 9. Deferred Phase 4D Functionality

The History page is strictly read-only in Phase 4B.1:
- `QTableWidget` editing is disabled (`NoEditTriggers`).
- `Export (Planned)` and `Delete (Planned)` buttons in the Local Storage card are visibly disabled with tooltips explaining they belong to Phase 4D.
- CSV/JSON export, individual/bulk deletion, and retention rules are intentionally not implemented.

---

## 10. Consequential Implementation Decisions

1. **Database-side sorting & full-history search via narrow `HistoryQuery` extensions vs. client-side table filtering/sorting:** Chosen database-side execution so search, filtering, and column sorting remain accurate and bounded across multi-page databases without loading all rows into memory.
2. **Single-slot latest-wins `HistoryReader` vs. unbounded per-keystroke worker threads:** Caps concurrent read threads to at most one, eliminates stale response races, and aligns cleanly with `MainWindow.closeEvent()`.
3. **Page hydration with `include_samples=False`:** `HistoryRecord` already contains all Measurement Details fields (server, DNS, bufferbloat, checks, coverage, recommendations, qualifications) without loading raw chart samples, making keyboard row navigation instant while keeping memory tightly bounded.
4. **Explicit `StrEnum.value` storage in `QComboBox` userData:** Avoids PySide6 coercing `StrEnum` items into plain `str` values that would fail strict `isinstance(..., Enum)` checks.

---

## 11. Native Acceptance Procedure

Run SpeedPup Portable natively (e.g., `.venv/bin/speedpup-portable`) and verify:

1. **Initial load & existing records:** Click **History** in the sidebar. Confirm previously persisted Phase 4A Speed Test and Diagnostics records appear newest-first with readable columns, accurate local timestamps, and accurate Local Storage path, file size, record count, and schema version (`v1`).
2. **Speed Test details:** Select a Speed Test row. Confirm download, upload, `HTTP Latency`, sanitized server info, interface context, and qualifications display cleanly without credentials or query parameters.
3. **Diagnostics details:** Select a Diagnostics row (and a Full/partial diagnostic if present). Confirm `ICMP latency`, jitter, packet loss, DNS performance, checks, coverage, recommendations, and bufferbloat qualifications (`Link saturation established: No` when applicable) display accurately, with `—` in table Download/Upload columns and `Unavailable` (never `0`) for unavailable metrics.
4. **Search, filters & Clear Filters:** Filter by Date range (`Today`, `Last 7 days`, `Last 30 days`, `All time`), Type (`Speed Test`, `Diagnostics`), Interface, and Search text. Confirm unmatched searches display the no-match state and **Clear Filters** restores all rows.
5. **Sorting & pagination:** Click table column headers and toggle the sort direction button. Confirm deterministic ordering and that unavailable/`—` rows remain at the bottom. Confirm pagination controls behave accurately when record count exceeds page size.
6. **Live persistence visibility & selection preservation:** Select an older row in History, switch to **Speed Test** or **Diagnostics**, complete a new run, and return to **History**. Confirm the new measurement appears at the top without restarting and the previously selected record remains selected (or selectable) accurately.
7. **Read-only & narrow window checks:** Confirm table cells cannot be edited and `Export (Planned)` / `Delete (Planned)` are disabled. Resize the window down to minimum size (`1000×680`) and verify readable columns, scrolling, wrapped details, and arrow-key row navigation.
8. **Error resilience (optional isolated check with `SPEEDPUP_DATA_DIR`):** Point `SPEEDPUP_DATA_DIR` at a temporary directory containing a non-SQLite file named `history.sqlite3`, open History, confirm `History storage is unavailable` is shown without crashing or modifying the file, and verify Speed Test / Diagnostics still open normally.

---

## 12. Known Limitations

- Visual reference `assets/UI/Mockup UI History.png` currently duplicates `assets/UI/Mockup UI Analysis.png`; once a distinct History mockup is added, visual polish adjustments may be desired if any layout tweaks are requested.
- Detailed raw throughput / latency-load sample plots inside History details are not rendered in Phase 4B.1 (historical charts and trend analysis belong to upcoming Phase 4 slices).
- Export, deletion, and retention enforcement are deferred to Phase 4D.
- Native interactive acceptance on Linux/Windows/macOS and independent pre-seal review remain pending.

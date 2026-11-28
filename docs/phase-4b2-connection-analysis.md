# Phase 4B.2 — Connection Analysis GUI

## 1. Overview & Scope

Phase 4B.2 replaces the **Analysis** placeholder page in `MainWindow` (navigation index `3`) with a descriptive historical **Connection Analysis** dashboard backed by the existing Phase 4A local SQLite history database (`SCHEMA_VERSION = 1`, no schema redesign).

This slice provides historical trend charts, complete-population descriptive statistics, explicit measurement coverage, and comparability context without generating new network traffic. Rule-based Connection Intelligence, automated trend classification, ISP/router fault attribution, overall health scores, and automated historical recommendations are intentionally deferred to Phase 4C.

---

## 2. Visual Reference & Styling

- **Primary visual reference:** `assets/UI/Mockup UI Analysis.png`, combined with the established SpeedPup styling in `src/speedpup/gui/theme.py`, `src/speedpup/gui/main_window.py`, `src/speedpup/gui/diagnostics_view.py`, and `src/speedpup/gui/history_view.py`.
- Both `assets/UI/Mockup UI Analysis.png` and `assets/UI/Mockup UI History.png` remain untouched.
- Illustrative mockup labels (such as `"Sample Data"` or `"Connection stable"` / `"Temporary download degradation observed"`) are **not** reproduced as synthetic or rule-based claims; Phase 4B.2 displays strictly factual descriptive status and coverage summaries from actual saved measurements.
- The existing 7-item sidebar navigation, TaskPuppy Kreations logo, window header, dark purple surfaces, lime-green/violet accents, rounded `#card` panels, and History page (index `4`) remain intact.

---

## 3. Analysis Architecture

Phase 4B.2 maintains strict boundaries across four layers:

1. **Qt-independent historical aggregation (`speedpup.core.analysis`)**
   - Defines `AnalysisQuery`, `MetricSummary`, `DnsAnalysisSummary`, `BufferbloatAnalysisSummary`, `HistoricalPoint`, `HistoricalSeries`, `BufferbloatHistoryPoint`, `BufferbloatSeries`, `AnalysisResult`, `ExtremaBucketDownsampler`, `compute_numeric_summary()`, `build_coverage_notes()`, `analyze_records()`, and `analyze_repository()`.
   - Imports no Qt modules (`PySide6`).
2. **Database-side aggregation fast path (`speedpup.core.history_store`)**
   - `SQLiteHistoryStore.analysis_snapshot(query)` executes inside one `BEGIN DEFERRED` read transaction so summary cards, descriptive statistics, charts, interface lists, and sidebar counts reflect a single consistent database snapshot.
   - Queries only scalar columns from `measurements`, `speed_test_results`, and `diagnostic_results`; never loads `speed_test_samples`, `diagnostic_samples`, `diagnostic_checks`, `diagnostic_coverage`, or `diagnostic_recommendations`.
3. **Interactive PySide6 historical charts (`speedpup.gui.widgets.analysis_chart`)**
   - Pure-`QPainter` `HistoricalSeriesChart` and `BufferbloatHistoryChart` with no third-party plotting dependencies.
   - Support mouse hover tooltips, click selection, and Left/Right/Home/End keyboard inspection.
4. **Off-GUI-thread analysis coordinator & presentation (`speedpup.gui.analysis_reader`, `speedpup.gui.analysis_view`, `speedpup.gui.main_window`)**
   - `AnalysisReader` runs `analyze_repository()` off the GUI thread using a lazily spawned, self-draining single-slot latest-wins `QThread` and monotonic `request_id` stale-response suppression.
   - `AnalysisView(QScrollArea)` renders the header/filters, 4 summary metric cards, 6 historical charts (3 two-column rows), a fixed 10-row Descriptive Statistics Summary table, and the right-side status/coverage/about sidebar.

---

## 4. Metric Definitions & Aggregation Methodology

All descriptive statistics are computed over the **complete filtered measurement population** in SQLite—never from downsampled display chart points and never limited to a single History table page.

### Per-Metric Descriptive Statistics (`MetricSummary`)

For each homogeneous metric population in scope:
- **Valid runs (`valid_count`):** Count of completed test runs (not individual in-run probes or transfer chunks) with an available measured value.
- **Unavailable runs (`unavailable_count`):** Count of relevant runs in scope where the metric was recorded as unavailable (`*_available = 0` / SQL `NULL`).
- **Excluded runs (`excluded_count`):** Count of Diagnostics runs whose profile excluded the metric (e.g., Quick and Standard profiles for Full load-latency increases).
- **Latest:** Value, timestamp, and `record_id` of the most recent valid run ordered deterministically by `(completed_at_utc DESC, record_id DESC)`.
- **Median:** Exact mathematical median computed directly in SQLite over all valid values ordered ascending (`LIMIT 1 OFFSET n/2` for odd `n`, average of middle two rows via `LIMIT 2 OFFSET n/2 - 1` for even `n`).
- **Minimum & Maximum:** Exact `MIN` and `MAX` over valid values in scope.
- **Observation period (`first_at`, `last_at`):** Earliest and latest valid observation timestamps in scope.
- When `valid_count == 0`, `latest_value`, `median`, `minimum`, and `maximum` are strictly `None` (never `0`).

### Measurement Population Separation

| Metric Population | Source Table / Column | Method Qualification |
| --- | --- | --- |
| Download & Upload throughput | `speed_test_results.download_mbps`, `upload_mbps` | Ordinary HTTP Speed Tests only; Full Diagnostics load transfers are never included |
| HTTP latency | `speed_test_results.http_latency_ms` | HTTP request latency from Speed Tests only; separate card/chart from ICMP |
| Baseline ICMP latency & Jitter | `diagnostic_results.icmp_latency_ms`, `jitter_ms` | ICMP Diagnostics only; never merged with HTTP latency |
| Packet loss | `diagnostic_results.packet_loss_pct` | ICMP Diagnostics; measured `0.0%` preserved distinctly from unavailable `NULL` |
| DNS lookup latency & failures | `diagnostic_results.dns_median_lookup_ms`, `dns_slowest_lookup_ms`, `dns_failures` | Successful lookup timing aggregated separately from DNS failure counts (`runs_with_any_failure`, `total_failures`) |
| Estimated bufferbloat | `diagnostic_results.bufferbloat_grade`, `download_latency_increase_ms`, `upload_latency_increase_ms` | Full Diagnostics only. Letter grades remain categorical. Descriptive load increases include available measurements from ungraded runs, with directional counts disclosed in the chart, statistic method notes, and reports. |

---

## 5. Filter Semantics

`AnalysisView` uses filter semantics consistent with `HistoryView`:
- **Date range:** `All time` (default), `Today` (local midnight onward, converted to UTC), `Last 7 days` (rolling 7-day UTC cutoff), and `Last 30 days` (rolling 30-day UTC cutoff).
- **Interface:** `All interfaces` or any distinct available `interface_name` stored in the SQLite database (populated across the entire database, not only the active window).
- **Category:** `All measurements`, `Speed Test`, or `Diagnostics`.
- **Refresh** and **Clear Filters** actions re-run analysis immediately; all summary cards, charts, descriptive statistics table rows, and sidebar counts update atomically from the same `AnalysisResult` snapshot.

---

## 6. Chart Behavior, Point Limits & Downsampling

Six dedicated charts render chronological history:
1. **Throughput History** (Speed Test Download & Upload Mbps)
2. **Diagnostic Latency & Jitter** (Diagnostics Baseline ICMP latency & Jitter ms)
3. **Packet Loss History** (Diagnostics Packet Loss %, plotting `0.0%` on baseline and breaking on `None`)
4. **DNS Lookup Performance** (Diagnostics Median & Slowest DNS lookup ms, with per-point DNS failure counts in hover/selection readout and failure totals in chart footer)
5. **Estimated Bufferbloat History** (Full Diagnostics categorical grade badges `A+`–`F` / `N/A` in a top band + Download and Upload loaded latency increases `+ms` below)
6. **Speed Test HTTP Latency** (Speed Test HTTP latency ms, explicitly separated from ICMP)

### Integrity & Bounded Rendering Rules

- **Actual timestamps:** X positions map chronologically using real UTC timestamps (`(t - t_start) / (t_end - t_start)`). Overlapping identical timestamps tie-break deterministically by `(completed_at_utc, record_id)` and space evenly when `t_span == 0`.
- **Gap preservation:** `None` values break line paths rather than interpolating across unavailable observations. Single-point series render centered dots with full interactive inspection.
- **Bounded point cap (`DEFAULT_MAX_CHART_POINTS = 120`, valid range `10..500`):**
  - Each chart has one observation budget shared by its numeric series. The production cap is 120 observations. When the population fits, the chart retains every observation. Custom caps from 10 to 500 are supported. Smaller limits are rejected, so every supported cap can accommodate two endpoints and up to four distinct numeric extrema.
  - Above the cap, the sampler reserves the first and last observations, then divides the remainder into chronological buckets with two slots per numeric series and distributes any leftover slots among the buckets. Each bucket retains the earliest source observation at the minimum and maximum of **each** numeric series. Any unused slots are filled from observations spread through that bucket. A record chosen for multiple metrics takes only one slot. A bucket smaller than its quota retains all its observations and can leave a slot unused.
  - The sampler counts missing observations separately for each metric as it reads the original stream. It attaches a line-break flag when an interval between retained observations contains a missing value for that metric. The renderer draws separate paths and fills at those breaks and at explicit unavailable points. The budget can omit individual missing observations or whole segments, but it never joins measurements across an original unavailable value. Measured zeros and isolated available observations remain plot points.
  - Each retained point still refers to its actual record, timestamp, and measurements. Ties follow record ID order. The grade band shows the categorical grades of the selected Full Diagnostics observations. Chart footers disclose how many observations are shown. Descriptive statistics use the complete filtered population, and Connection Intelligence uses its own evidence selection.

---

## 7. Comparability & Qualification Rules

- **Summary card labels:** Numeric summary cards explicitly state `"Median <metric>"` when `valid_count >= 2` or `"Latest <metric>"` when `valid_count == 1`, along with sample counts and unavailable run counts.
- **Bufferbloat provenance:**
  - Displays `"Est. <grade>"`, exact measurement timestamp, load budget (`Light`/`Standard`/`Extended`), and `"Under observed load · saturation not established"` (`bufferbloat_saturation_established == False`).
  - When a newer Diagnostics run occurred after the latest available grade (for example, a newer Quick/Standard run or an ungraded Full run), the card explicitly notes that distinction rather than presenting the older grade as the newest diagnostic result.
  - Categorical grade distributions (`A (3), B (1)`) are reported descriptively without averaging letter grades.
- **Comparability context:** Coverage notes explicitly disclose when runs in scope span multiple network interfaces, multiple Full Diagnostics load budgets, or multiple DNS resolvers.

---

## 8. Thread Ownership & Cleanup

- `AnalysisReader` owns a single lazily spawned `_AnalysisQueryThread(QThread)` draining a latest-wins pending slot (`self._pending`). No idle thread runs when Analysis is inactive.
- `MainWindow._navigate()` invalidates in-flight analysis queries when leaving `ANALYSIS_PAGE_INDEX = 3` and triggers a fresh query when entering it.
- **Write-coherence:** If `HistoryWriter.is_running()` is `True` when Analysis refreshes, `MainWindow` shows the loading state and defers starting `AnalysisReader` until `HistoryWriter.idle` fires, preventing read/write races on newly completed measurements.
- **Live updates:** When `HistoryWriter.record_saved` fires while Analysis is active, Analysis automatically refreshes.
- **Shutdown:** `MainWindow.closeEvent()` invalidates pending reads and defers close via `_close_when_idle` until `HistoryWriter`, `HistoryReader`, and `AnalysisReader` are all idle, then closes an owned `SQLiteHistoryStore`.

---

## 9. Empty, Incomplete & Error States

Distinct states supported without fabricating metrics or modifying the database:
- `empty` (`No History Recorded`): zero records stored locally (does not create a DB file on read).
- `no_matches` (`No Matching History`): stored records exist, but active filters match zero runs (offers `Clear Filters`).
- `limited` (`Limited History`): fewer than 2 runs in every active category; still presents useful latest values and single-point charts while noting additional runs are needed to observe change over time.
- `partial` (`Partial Coverage`): one category missing or some diagnostics completed with unavailable individual metrics; available categories remain fully displayed.
- `loading` (`Loading…`): asynchronous query or pending write drain in progress.
- `storage_unavailable` (`Storage Unavailable`): path, permission, schema, unsupported newer version, or corruption error; preserves the file untouched and keeps Speed Test, Live Monitor, Diagnostics, and History usable.
- `query_failed` (`Analysis Failed`): transient query failure with a `Retry` action.

---

## 10. Performance Considerations

- SQLite queries run inside a single `BEGIN DEFERRED` snapshot without hydrating child sample tables (`speed_test_samples`, `diagnostic_samples`, `diagnostic_checks`, `diagnostic_coverage`, `diagnostic_recommendations`).
- Exact descriptive statistics (`COUNT`, `MIN`, `MAX`, `LATEST`, `MEDIAN`) execute in SQLite using `O(1)` Python memory.
- SQLite streams scalar chart rows in batches of 512 through the same sampler used by the Qt-independent analysis. Chart selection uses `O(max_chart_points)` Python memory plus the fixed-size cursor batch, with no hydrated samples or growing list of missing observations.
- The Descriptive Statistics Summary table has a fixed 10 rows updated in place, avoiding unbounded Qt widget creation.
- No background polling or automatic network activity exists.

---

## 11. Consequential Architectural Decisions & Alternatives Considered

1. **SQLite-side statistics + streaming extrema downsampler vs. loading all `HistoryRecord` objects via `list_records()`**
   - *Decision:* Added `SQLiteHistoryStore.analysis_snapshot()` computing exact SQLite statistics and streaming scalar chart rows through `ExtremaBucketDownsampler`, while retaining `analyze_records()` as a pure-Python reference and fallback for generic `HistoryRepository` mocks.
   - *Rationale:* The production SQLite chart path keeps memory `O(max_chart_points)` plus fixed-size cursor batches, avoids loading raw sample tables or child checklist rows, and shares a snapshot across cards and charts. The nonproduction generic repository fallback accumulates matching records, can hydrate child details, and leaves database-wide interfaces unenumerated if the repository lacks `list_interfaces()`.
2. **Custom `QPainter` historical charts vs. adding `matplotlib` / `PySide6-Addons` (`QtCharts`)**
   - *Decision:* Lightweight pure-`QPainter` `HistoricalSeriesChart` and `BufferbloatHistoryChart` widgets.
   - *Rationale:* Zero added dependencies (matching `pyproject.toml`), exact theme parity with `diagnostics_chart.py`, full control over `None` gap handling, categorical bufferbloat grade bands, and responsive narrow-window rendering.
3. **Separate HTTP Latency chart vs. overlaying HTTP and ICMP on one latency chart**
   - *Decision:* Dedicated **Speed Test HTTP Latency** chart separate from **Diagnostic Latency & Jitter (ICMP)**.
   - *Rationale:* Prevents visually or statistically conflating HTTP request round-trip latency with ICMP baseline ping latency.
4. **Separate `AnalysisReader` alongside `HistoryReader` vs. multiplexing one combined reader**
   - *Decision:* Dedicated `AnalysisReader` mirroring `HistoryReader`.
   - *Rationale:* Preserves Phase 4B.1's verified `HistoryReader` untouched, keeps each page's stale-request generation and invalidation isolated, and adds negligible complexity.

---

## 12. Integration Points for Phase 4C (Connection Intelligence)

Phase 4C can consume `AnalysisResult` directly from `speedpup.core.analysis` without redesigning storage or chart plumbing:
- `AnalysisResult` already supplies complete-population `MetricSummary` objects (`valid_count`, `unavailable_count`, `median`, `minimum`, `maximum`, `latest_value`, observation span), `DnsAnalysisSummary`, `BufferbloatAnalysisSummary`, `interfaces_in_scope`, and comparability context (`load_budgets_observed`, `resolvers_observed`).
- Phase 4C can evaluate qualified, rule-based observations on top of `AnalysisResult` (and/or individual recent `HistoryRecord` entries) and surface them inside the right-side observations card and Speed Test sidebar in a clean Qt-independent rules module.

---

## 13. Native Acceptance Procedure

Launch SpeedPup Portable natively (e.g., `.venv/bin/speedpup-portable`) and verify:

1. **Initial Analysis load:** Click **Analysis** in the sidebar. Confirm existing locally saved Speed Test and Diagnostics records render across the 4 summary cards, 6 historical charts, 10-row descriptive statistics table, and right-side status/coverage cards without initiating any network traffic.
2. **Summary card labels & qualifications:** Confirm throughput cards explicitly label `Median` or `Latest` with Speed Test run counts, latency card explicitly labels `(ICMP)`, and the bufferbloat card displays `Est. <grade>` (or `Unavailable`), measurement timestamp, load budget, saturation qualification (`saturation not established`), and distinction if a newer diagnostic lacked a grade.
3. **Chart semantics & interaction:** Hover, click, and press Left/Right arrow keys on charts. Confirm HTTP latency and ICMP latency remain separate charts, measured `0.0%` packet loss plots on the baseline while unavailable runs create gaps, DNS failures display separately from lookup latency, and Full Diagnostics transfer never appears in Throughput History.
4. **Filters & consistency:** Test `Today`, `Last 7 days`, `Last 30 days`, `All time`, Interface, and Category filters plus **Clear Filters** and **Refresh**. Confirm all cards, charts, stats table rows, and sidebar counts update consistently together.
5. **Live persistence visibility:** While on Analysis (or right before switching to Analysis), complete a new Speed Test or Diagnostics run and confirm Analysis reflects the newly saved measurement without restarting.
6. **Responsive & error checks:** Resize down to minimum window width (`1000×680`) and verify readability and scrolling. Optionally launch with `SPEEDPUP_DATA_DIR` pointed at an isolated temporary directory containing a non-SQLite `history.sqlite3` file and confirm nonfatal `History storage is unavailable` handling while Speed Test, Diagnostics, and History remain usable.

---

## 14. Known Limitations

- Automated trend detection, anomaly classification, historical recommendations, and overall connection health scoring are intentionally absent (planned for Phase 4C).
- History GUI nonblocking polish items noted in scope (table column truncation, redundant qualification wording) were intentionally left untouched in Phase 4B.2.
- Automated tests ran offscreen on Linux Python 3.13.5 / SQLite 3.46.1; native interactive desktop acceptance across Linux/Windows/macOS and independent pre-seal review remain pending.

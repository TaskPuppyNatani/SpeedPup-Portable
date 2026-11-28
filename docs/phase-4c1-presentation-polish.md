# Phase 4C.1 — GUI Presentation Polish

Phase 4C.1 refines the layout, readability, and visual hierarchy of the **Connection Analysis**, **Measurement History**, and **Speed Test** pages without changing measurement contracts, intelligence rules, analytical thresholds, the 512-record evidence selection algorithm, or the SQLite storage schema.

## Identified presentation problems & root causes

1. **Excessive vertical expansion of Analysis summary cards**
   - *Symptom:* On desktop and ultrawide windows, the four Analysis summary cards expanded vertically to leave large blank areas beneath their captions, pushing charts away from the summary row.
   - *Root cause:* `AnalysisView` placed `main_col` and `side_col` side by side in a shared `QHBoxLayout`, and `main_layout` had no trailing stretch item. When the Phase 4C Historical Observations sidebar grew tall, Qt stretched `main_col` vertically to match `side_col` and distributed that surplus height across the header, summary cards, charts, and statistics table. Each summary card also contained an internal vertical stretch that amplified the blank gap.
2. **Verbose, repetitive Historical Observations sidebar**
   - *Symptom:* Sparse or mixed histories emitted eight or more separate `coverage.*` observations, repeating identical interface/comparability limitation text on every entry and burying actionable findings and next steps inside a long wall of text.
   - *Root cause:* `_render_intelligence()` iterated over raw `intelligence.observations[:12]` linearly, rendering each observation's full limitations block independently without population-safe grouping, prioritization headers, or collapsible evidence disclosure.
3. **Unbalanced Analysis Descriptive Statistics table**
   - *Symptom:* The final `Unavailable` column absorbed excess horizontal space on wide and ultrawide screens while `Metric`, `Method`, and numeric cells stayed cramped at fixed widths without tooltips.
   - *Root cause:* `stats_table` used `header.setStretchLastSection(True)` with static column widths (`(170, 145, 86, 86, 82, 82, 74, 82)`).
4. **History table truncation and repeated detail qualifications**
   - *Symptom:* Bold `Diagnostics`, `Standard`, and throughput values (`342.8 Mbps`) truncated in the History table while `Status` stretched disproportionately. In the Measurement Details panel for Full Diagnostics, saturation qualifications appeared repeatedly across the Bufferbloat section and the Qualifications & Provenance section.
   - *Root cause:* Fixed narrow initial widths paired with `header.setStretchLastSection(True)`, plus verbatim rendering of `bufferbloat_note` alongside both a hardcoded saturation sentence and the overlapping `record.qualification_notes` tuple.
5. **Speed Test sidebar verbosity and narrow-window polish**
   - *Symptom:* Sparse histories dumped the first three alphabetical `coverage.*` observations directly onto the Speed Test sidebar without expandable supporting evidence, and narrow viewports (`1280 × 720`) cramped the Analysis filter bar and Speed Test sidebar card heading.
   - *Root cause:* `_on_speed_history_result()` sliced `intelligence.observations[:3]` directly, `AnalysisView` placed all three filter dropdowns plus action buttons on one row, and Speed Test sidebar card titles used `15pt` non-wrapping headings.

## Layout corrections

- **Analysis summary cards & vertical decoupling (`src/speedpup/gui/analysis_view.py`):**
  - Added a trailing `main_layout.addStretch(1)` and reduced main-column spacing to `10px` so surplus height from a taller sidebar is absorbed below the statistics table rather than stretching summary cards or charts.
  - Wrapped the four summary cards in `summary_cards_container` (`Preferred` vertical policy, no brittle `setFixedHeight`), aligned them to the Diagnostics metric-card visual conventions (`#diagMetricGreen` / `#diagMetricPurple`), and set `QSizePolicy(Preferred, Minimum)` on word-wrapped `QLabel`s so wrapped text never clips under narrow widths or font scaling.
  - Moved `↻ Refresh`, `Clear Filters`, and the updated `"Local · Offline"` pill onto the header row so `Date range`, `Interface`, and `Category` dropdowns remain uncrowded at `1280 × 720` and `1000 × 680`.
  - Updated the outdated `"Rule-based connection intelligence is planned for Phase 4C."` tooltip and docstring.
- **Font-metric-aware proportional table sizing (`src/speedpup/gui/table_layout.py`):**
  - Introduced `ColumnSpec`, `compute_column_widths()`, `measure_required_column_widths()`, and `ProportionalTableController` with `setStretchLastSection(False)` and `QHeaderView.ResizeMode.Interactive`.
  - Each column's required readable width is measured from actual `QFontMetrics` (including bold cells and sort-indicator padding) so DPI and font scaling are respected naturally.
  - On narrow viewports (`available < required`), columns retain readable widths and smooth pixel-level horizontal scrolling activates instead of crushing values.
  - On wider viewports (`1920 × 1080`, `3440 × 1440`), surplus width is distributed proportionally by column weight with upper caps on compact count/label columns (`Valid Runs ≤ 135px`, `Unavailable ≤ 150px` in Analysis; bounded `Type`, `Profile`, and `Bufferbloat` in History).
  - Added full-value tooltips on every cell of both tables and guarded `eventFilter()` safely against C++ widget deletion during Qt teardown.
  - Sized `stats_table` height dynamically to its 10 rows plus header and scrollbar allowance so the table never clips or bloats vertically.
- **History detail panel consolidation & sort verification (`src/speedpup/gui/history_view.py`):**
  - Consolidated redundant saturation qualification wording between the Bufferbloat & Load Response section and Qualifications & Provenance (skipping exact duplicates and generic saturation boilerplate when `bufferbloat_note` already states it), while preserving all distinct provenance notes (e.g., ICMP vs HTTP latency distinction and worker notes).
  - Verified default newest-first chronological ordering (`HistorySortField.COMPLETED_AT`, `SortOrder.NEWEST_FIRST`) and preservation of user-selected sorting and stable `record_id` selection across refreshes and newly persisted measurements.
- **Speed Test sidebar polish (`src/speedpup/gui/main_window.py`):**
  - Preserved clear separation between `Current Speed Test` (`Current session only · separate from saved history`) and `Historical Observations` (`All saved history · up to 512 runs across categories and profiles`).
  - Adopted `build_intelligence_presentation()` to display a prioritized summary and a collapsible `▸ Supporting evidence (N)` panel. Phase 4E.4C added scope and evaluated counts by category and profile and distinguishes limited comparable evidence from a connection fault.
  - Reduced sidebar card heading font size to `12pt` with word wrap enabled so narrow viewports (`1280 × 720`) never clip headings, and kept `Suggested Next Steps` explicitly qualified as investigation steps rather than diagnoses.

## Observation grouping and disclosure behavior

`src/speedpup/gui/intelligence_presentation.py` provides a pure, Qt-independent transformation (`build_intelligence_presentation()`) of `IntelligenceResult`:

1. **Concise summary & explicit 512-run limit:** Summarizes finding and coverage counts without claiming an overall health score. The `Bounded evidence (512-run cap)` notice appears when evaluation is truncated.
2. **Priority ordering:**
   - **Actionable Findings** (`evidence_sufficient=True`, `Severity.WARNING`) appear first with amber visual accent and key formatted metrics.
   - **Measured Observations** (`evidence_sufficient=True`, `Severity.INFO`, e.g., `packet_loss.measured_zero` or `packet_loss.isolated`) appear next with distinguishing condition tags (such as `Profile full` vs `Profile standard`) so separate comparable populations remain visually distinct.
   - **History & Coverage Limits** (`evidence_sufficient=False`) are grouped strictly within matching measurement populations:
     - `Speed Tests` (`coverage.speed_test*`)
     - `Diagnostics (ICMP & DNS)` (`coverage.diagnostics*`, `coverage.icmp`, `coverage.jitter`, `coverage.loss`, `coverage.dns_*`)
     - `Full Diagnostics (Bufferbloat)` (`coverage.bufferbloat*`)
     - Any other category falls back to per-category grouping.
3. **Expandable lossless evidence:** Every finding, measured observation, and grouped coverage card provides a collapsible disclosure toggle (`_ObservationDisclosureWidget`) exposing every underlying `Observation` verbatim (title, explanation, run and matching counts, observation period, statistics, conditions, and limitations).
4. **Qualification consolidation:** Identical population-level limitations (such as general comparability, ICMP filtering, DNS resolver/cache caveats, and estimated bufferbloat caveats) are consolidated once under `Measurement Qualifications` with explicit scope labels, while finding-specific notes (such as worker timeout qualifications) stay surfaced directly on the finding card as well as inside its expanded details.
5. **Suggested Next Steps:** Rendered in a distinct, always-visible card prioritizing actionable findings' recommendations before coverage next steps.

## Preservation of analytical semantics

- `src/speedpup/core/intelligence.py`, `src/speedpup/core/analysis.py`, `src/speedpup/core/history.py`, and `src/speedpup/core/history_store.py` were **not modified**.
- Comparability boundaries, thresholds, and deduplication are shared with Phase 4C. The bounded evidence allocation is described in [Connection Intelligence](phase-4c-connection-intelligence.md).
- Speed Tests, Diagnostics (ICMP/DNS), and Full Diagnostics (estimated bufferbloat) are never merged into combined conclusions.
- Zero measured packet loss remains distinct from unavailable packet loss and insufficient history.

## Responsive layout considerations

- Verified at `1280 × 720`, `1920 × 1080`, and `3440 × 1440` (as well as the `1000 × 680` minimum window size).
- Avoided fixed pixel heights on text-bearing cards; relied on `QFontMetrics`, word wrap (`QSizePolicy.Policy.Minimum` vertical policy), layout settling on dynamic rebuild/toggle, and scroll areas so font scaling and DPI changes adapt without text clipping.
- Tables remain usable on narrow viewports via smooth pixel-level horizontal scrolling while distributing space harmoniously on standard and ultrawide monitors.

## Verification performed

- Temporary offscreen screenshots rendered and visually inspected across `1280 × 720`, `1920 × 1080`, and `3440 × 1440` for Analysis, History, and Speed Test (stored strictly outside the repository in scratch storage).
- Focused regression tests added in `tests/test_presentation_polish.py` covering summary card height bounds, population-safe grouping, expandable evidence disclosure, worker and 512-run window qualification visibility, `Suggested Next Steps`, Analysis and History table sizing, History qualification consolidation, newest-first default sorting with preserved user sort and stable selection, Speed Test sidebar framing/disclosure, and Qt-independence of `intelligence_presentation`.
- Full automated regression suite, dependency integrity (`pip check`), bytecode compilation (`compileall`), and whitespace checks executed cleanly against isolated temporary SQLite databases with no external network traffic.

## Remaining limitations

- Interactive native desktop acceptance (real window manager DPI scaling, platform fonts, mouse drag resizing, and high-DPI displays on Linux, Windows, and macOS) remains to be performed by the user.
- History export, deletion, and retention controls remain intentionally deferred to Phase 4D.
- Phase 4E.4C allocates the shared intelligence budget by category and diagnostic profile and documents the nonproduction repository fallback.

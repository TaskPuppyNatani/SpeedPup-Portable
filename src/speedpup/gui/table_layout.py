# SPDX-License-Identifier: GPL-3.0-or-later
"""Font-metric-aware table column sizing helpers for SpeedPup GUI tables.

Prevents narrow columns from truncating core labels/values and prevents the
final column from absorbing disproportionate surplus horizontal space on wide
or ultrawide screens, while preserving horizontal scrolling on narrow viewports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QHeaderView, QTableWidget


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """Sizing constraints for one ``QTableWidget`` column."""

    min_width: int
    max_content_width: int
    weight: float = 1.0
    max_width: int | None = None


def compute_column_widths(
    required_widths: Sequence[int],
    specs: Sequence[ColumnSpec],
    available_width: int,
) -> list[int]:
    """Distribute ``available_width`` across columns without starving content.

    When ``available_width`` is narrower than the sum of readable column widths,
    every column retains its required readable width so the table's horizontal
    scrollbar activates instead of truncating values. When surplus width exists,
    it is distributed proportionally by ``spec.weight`` up to each column's
    optional ``spec.max_width``.
    """
    if len(required_widths) != len(specs):
        raise ValueError("required_widths and specs must have the same length.")
    if not specs:
        return []

    widths = [max(1, int(w)) for w in required_widths]
    base_total = sum(widths)
    if available_width <= base_total:
        return widths

    remaining = float(available_width - base_total)
    active: list[int] = [
        idx
        for idx, spec in enumerate(specs)
        if spec.weight > 0.0 and (spec.max_width is None or widths[idx] < spec.max_width)
    ]

    while remaining >= 1.0 and active:
        total_weight = sum(specs[idx].weight for idx in active)
        if total_weight <= 0.0:
            break
        next_active: list[int] = []
        spent = 0.0
        capped_any = False
        for idx in active:
            spec = specs[idx]
            share = remaining * (spec.weight / total_weight)
            if spec.max_width is not None and widths[idx] + share >= spec.max_width:
                delta = max(0.0, float(spec.max_width - widths[idx]))
                widths[idx] = max(widths[idx], spec.max_width)
                spent += delta
                capped_any = True
            else:
                next_active.append(idx)
        if not capped_any:
            # Distribute all remaining width cleanly across uncapped active columns.
            int_shares = [
                int(remaining * (specs[idx].weight / total_weight))
                for idx in active
            ]
            for idx, add in zip(active, int_shares):
                widths[idx] += add
            leftover = int(round(remaining)) - sum(int_shares)
            if leftover > 0:
                best_idx = max(active, key=lambda i: specs[i].weight)
                widths[best_idx] += leftover
            remaining = 0.0
            break
        remaining = max(0.0, remaining - spent)
        active = next_active

    return widths


def measure_required_column_widths(
    table: QTableWidget,
    specs: Sequence[ColumnSpec],
    *,
    cell_padding: int = 24,
    header_padding: int = 28,
    sort_indicator_padding: int = 16,
) -> list[int]:
    """Measure readable column widths using actual Qt font metrics."""
    header = table.horizontalHeader()
    header_metrics = QFontMetrics(header.font())
    table_metrics = QFontMetrics(table.font())
    show_sort = header.isSortIndicatorShown()

    required: list[int] = []
    row_count = table.rowCount()
    for col_idx, spec in enumerate(specs):
        header_item = table.horizontalHeaderItem(col_idx)
        header_text = header_item.text() if header_item is not None else ""
        header_width = (
            header_metrics.horizontalAdvance(header_text)
            + header_padding
            + (sort_indicator_padding if show_sort else 0)
        )

        content_width = 0
        for row_idx in range(row_count):
            item = table.item(row_idx, col_idx)
            if item is None:
                continue
            text = item.text()
            if not text:
                continue
            item_font = item.font()
            metrics = (
                QFontMetrics(item_font)
                if item_font.bold() != table.font().bold()
                else table_metrics
            )
            w = metrics.horizontalAdvance(text) + cell_padding
            if w > content_width:
                content_width = w

        natural = max(header_width, content_width)
        bounded = max(spec.min_width, min(natural, spec.max_content_width))
        # Never clip the header text itself under larger DPI or font scaling.
        required.append(max(bounded, header_width))

    return required


def apply_proportional_column_widths(
    table: QTableWidget,
    specs: Sequence[ColumnSpec],
) -> list[int]:
    """Compute and apply balanced interactive column widths for ``table``."""
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    for col_idx in range(len(specs)):
        header.setSectionResizeMode(col_idx, QHeaderView.ResizeMode.Interactive)

    required = measure_required_column_widths(table, specs)
    available = max(0, table.viewport().width())
    widths = compute_column_widths(required, specs, available)
    for col_idx, width in enumerate(widths):
        table.setColumnWidth(col_idx, width)
    return widths


class ProportionalTableController(QObject):
    """Keeps a ``QTableWidget``'s columns balanced on data load and resize."""

    def __init__(self, table: QTableWidget, specs: Sequence[ColumnSpec]) -> None:
        super().__init__(table)
        self._table = table
        self._specs = tuple(specs)
        self._applying = False
        self._last_viewport_width = -1
        table.viewport().installEventFilter(self)

    @property
    def specs(self) -> tuple[ColumnSpec, ...]:
        return self._specs

    def fit_columns(self) -> list[int]:
        if self._applying:
            return []
        self._applying = True
        try:
            self._last_viewport_width = self._table.viewport().width()
            return apply_proportional_column_widths(self._table, self._specs)
        finally:
            self._applying = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.FontChange,
        ):
            try:
                viewport = self._table.viewport()
            except RuntimeError:
                return False
            if watched is viewport:
                new_width = viewport.width()
                if event.type() is QEvent.Type.FontChange or new_width != self._last_viewport_width:
                    self.fit_columns()
        return super().eventFilter(watched, event)

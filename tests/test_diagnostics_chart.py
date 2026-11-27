"""Offscreen checks that LatencyLoadChart stage labels stay inside their bands.

Regression guard for finding D4: with a realistic Full-profile timeline the
post-upload ``Idle`` band is only ~1-2 px wide, and the old label rect used a
24 px minimum that bled past the next band start while long names such as
"Download Load" exceeded the space available at narrow widths.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from speedpup.core.diagnostics import DiagnosticStage, LatencyLoadSample
from speedpup.gui.widgets.diagnostics_chart import LatencyLoadChart

_BASE = datetime(2026, 11, 26, 10, 0, 0)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _sample(elapsed: float, stage: DiagnosticStage, *, baseline=None, loaded=None) -> LatencyLoadSample:
    return LatencyLoadSample(
        elapsed,
        stage,
        baseline_latency_ms=baseline,
        loaded_latency_ms=loaded,
        timestamp=_BASE + timedelta(seconds=elapsed),
    )


def _full_profile_samples() -> list[LatencyLoadSample]:
    """Six bands: Idle, Download Load, Idle, Upload Load, Idle, Recovery (~43 s).

    Mirrors the backend emission pattern in ``diagnostics_backend``: the stage-end
    sample and the following IDLE sample share an elapsed time, which is what
    produces the near-zero-width Idle band after each load stage. The post-upload
    gap is ~0.15 s of a ~43 s span (the measured degenerate case).
    """
    return [
        _sample(0.0, DiagnosticStage.IDLE, baseline=18.0, loaded=18.0),
        _sample(3.0, DiagnosticStage.IDLE, baseline=18.2, loaded=18.2),
        _sample(3.0, DiagnosticStage.DOWNLOAD_LOAD, loaded=140.0),
        _sample(12.0, DiagnosticStage.DOWNLOAD_LOAD, loaded=152.0),
        _sample(12.0, DiagnosticStage.IDLE, baseline=18.4, loaded=18.4),
        _sample(15.0, DiagnosticStage.IDLE, baseline=18.1, loaded=18.1),
        _sample(15.0, DiagnosticStage.UPLOAD_LOAD, loaded=190.0),
        _sample(26.0, DiagnosticStage.UPLOAD_LOAD, loaded=205.0),
        _sample(26.0, DiagnosticStage.IDLE, baseline=18.3, loaded=18.3),
        _sample(26.15, DiagnosticStage.IDLE, baseline=18.0, loaded=18.0),
        _sample(26.15, DiagnosticStage.RECOVERY, loaded=45.0),
        _sample(43.0, DiagnosticStage.RECOVERY, loaded=20.0),
    ]


def _chart_at_width(width: int) -> tuple[LatencyLoadChart, QRectF, list, QFont]:
    _app()
    chart = LatencyLoadChart()
    chart.set_samples(_full_profile_samples())
    chart.resize(width, 240)
    plot = QRectF(chart.rect().adjusted(62, 12, -14, -44))
    positioned = chart.sample_x_positions(plot.left(), plot.width())
    bands = chart._compute_stage_bands(plot, positioned)
    axis_font = QFont(chart.font())
    axis_font.setPointSize(8)
    return chart, plot, bands, axis_font


def test_full_profile_timeline_produces_six_bands_with_narrow_idle_gaps() -> None:
    _chart, _plot, bands, _font = _chart_at_width(1100)
    stages = [stage for stage, _x0, _x1, _ts in bands]
    assert stages == [
        DiagnosticStage.IDLE,
        DiagnosticStage.DOWNLOAD_LOAD,
        DiagnosticStage.IDLE,
        DiagnosticStage.UPLOAD_LOAD,
        DiagnosticStage.IDLE,
        DiagnosticStage.RECOVERY,
    ]
    # The post-upload Idle band is the degenerate one this fix must survive.
    idle_widths = [x1 - x0 for stage, x0, x1, _ts in bands if stage is DiagnosticStage.IDLE]
    assert min(idle_widths) < 24.0


def test_label_rects_never_extend_past_their_band_at_all_widths() -> None:
    for width in (1100, 760, 560, 420):
        chart, plot, bands, font = _chart_at_width(width)
        labels = chart.stage_label_layout(bands, plot, font)
        assert labels, f"no labels drawn at width {width}"
        band_bounds = [(x0, x1) for _stage, x0, x1, _ts in bands]
        for rect, text in labels:
            assert text.strip()
            # Every drawn rect must sit inside exactly one band.
            containing = [b for b in band_bounds if rect.left() >= b[0] - 1e-6 and rect.right() <= b[1] + 1e-6]
            assert len(containing) == 1, (
                f"width {width}: label rect {rect.left():.2f}-{rect.right():.2f} "
                f"is not contained in a single band {band_bounds}"
            )
        # No duplicate or dropped labels beyond intentionally omitted narrow bands.
        assert len(labels) <= len(bands)


def test_label_text_never_exceeds_available_space_at_all_widths() -> None:
    for width in (1100, 760, 560, 420):
        chart, plot, bands, font = _chart_at_width(width)
        metrics = QFontMetrics(font)
        band_bounds = [(x0, x1) for _stage, x0, x1, _ts in bands]
        for rect, text in chart.stage_label_layout(bands, plot, font):
            available = rect.width()
            for line in text.splitlines():
                advance = metrics.horizontalAdvance(line)
                assert advance <= available + 1e-6, (
                    f"width {width}: label {line!r} is {advance:.1f}px wide "
                    f"but only {available:.1f}px of band space is available"
                )
            # Rects are clamped to their band: width equals the band width.
            containing = [b for b in band_bounds if rect.left() >= b[0] - 1e-6 and rect.right() <= b[1] + 1e-6]
            assert containing, f"width {width}: label rect {rect} outside every band"
            assert abs(rect.width() - (containing[0][1] - containing[0][0])) < 1e-6


def test_narrow_bands_are_omitted_rather_than_overlapping_neighbours() -> None:
    """A band narrower than its stage name draws no label instead of overlapping."""
    _app()
    chart = LatencyLoadChart()
    plot = QRectF(0, 0, 200, 100)
    font = QFont(chart.font())
    font.setPointSize(8)
    metrics = QFontMetrics(font)
    narrow = 6.0
    wide = 120.0
    bands = [
        (DiagnosticStage.IDLE, 0.0, narrow, None),
        (DiagnosticStage.DOWNLOAD_LOAD, narrow, narrow + wide, None),
    ]
    labels = chart.stage_label_layout(bands, plot, font)
    assert len(labels) == 1
    rect, text = labels[0]
    assert text.startswith("Download")
    assert rect.left() >= narrow and rect.right() <= narrow + wide
    # Sanity: the omitted band really cannot fit its name.
    assert metrics.horizontalAdvance(DiagnosticStage.IDLE.label) > narrow


def test_long_names_are_elided_not_clipped_at_narrow_widths() -> None:
    chart, plot, bands, font = _chart_at_width(420)
    metrics = QFontMetrics(font)
    labels = chart.stage_label_layout(bands, plot, font)
    for rect, text in labels:
        first_line = text.splitlines()[0]
        if metrics.horizontalAdvance(DiagnosticStage.DOWNLOAD_LOAD.label) > rect.width():
            assert first_line.endswith("…") or len(first_line) < len(DiagnosticStage.DOWNLOAD_LOAD.label)


def test_chart_paints_without_error_at_all_widths() -> None:
    from PySide6.QtGui import QImage
    from PySide6.QtCore import Qt

    for width in (1100, 760, 560, 420):
        _app()
        chart = LatencyLoadChart()
        chart.set_samples(_full_profile_samples())
        chart.resize(width, 240)
        image = QImage(chart.width(), chart.height(), QImage.Format.Format_ARGB32)
        image.fill(0)
        chart.render(image)
        assert not image.isNull()

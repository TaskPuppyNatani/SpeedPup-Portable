# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit confirmation and export-configuration dialogs for History management."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from speedpup.core.history import DeleteScopeAuthorization, DeleteScopeKind
from speedpup.core.history_export import (
    EXPORT_PRIVACY_NOTICE,
    ExportFormat,
    ExportScopeKind,
)
from speedpup.core.retention import RetentionPolicy
from speedpup.gui.theme import BORDER, ERROR, GREEN, MUTED, PURPLE, SUBTLE, TEXT, WARNING


def _dlg_label(
    text: str,
    *,
    size: int = 9,
    bold: bool = False,
    color: str = TEXT,
    word_wrap: bool = True,
) -> QLabel:
    lbl = QLabel(text)
    weight = "font-weight: 700; " if bold else ""
    lbl.setStyleSheet(f"font-size: {size}pt; {weight}color: {color}; background: transparent;")
    lbl.setWordWrap(word_wrap)
    return lbl


class ExportOptionsDialog(QDialog):
    """Modal confirmation of export format, explicit record scope, and privacy notice."""

    def __init__(
        self,
        *,
        selected_count: int,
        filtered_count: int,
        total_stored: int,
        has_active_filters: bool,
        filter_summary: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Measurement History")
        self.setModal(True)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        layout.addWidget(_dlg_label("Export Local Measurement History", size=13, bold=True))
        layout.addWidget(
            _dlg_label(
                "Confirm the export format and exact scope before choosing a local destination file.",
                size=9,
                color=MUTED,
            )
        )

        # Format selection
        fmt_frame = QFrame()
        fmt_frame.setObjectName("card")
        fmt_layout = QVBoxLayout(fmt_frame)
        fmt_layout.setContentsMargins(14, 11, 14, 11)
        fmt_layout.setSpacing(6)
        fmt_layout.addWidget(_dlg_label("Export format", size=10, bold=True, color=PURPLE))

        self._format_group = QButtonGroup(self)
        self.csv_radio = QRadioButton("CSV Spreadsheet (.csv) — one row per measurement, formula-protected")
        self.json_radio = QRadioButton("Structured JSON (.json) — versioned full details, checks, and samples")
        self.report_radio = QRadioButton("Connection Report (.txt) — plain-text summary for support/ISP review")
        self.csv_radio.setChecked(True)
        for idx, (btn, fmt) in enumerate(
            (
                (self.csv_radio, ExportFormat.CSV),
                (self.json_radio, ExportFormat.JSON),
                (self.report_radio, ExportFormat.REPORT_TXT),
            )
        ):
            btn.setProperty("exportFormat", fmt.value)
            self._format_group.addButton(btn, idx)
            fmt_layout.addWidget(btn)
        layout.addWidget(fmt_frame)

        # Scope selection
        scope_frame = QFrame()
        scope_frame.setObjectName("card")
        scope_layout = QVBoxLayout(scope_frame)
        scope_layout.setContentsMargins(14, 11, 14, 11)
        scope_layout.setSpacing(6)
        scope_layout.addWidget(_dlg_label("Export scope", size=10, bold=True, color=PURPLE))

        self._scope_group = QButtonGroup(self)
        sel_text = (
            "Selected measurement (1 record on current page)"
            if selected_count == 1
            else f"Selected measurements ({selected_count} records on current page)"
        )
        self.selected_radio = QRadioButton(sel_text)
        self.selected_radio.setEnabled(selected_count > 0)
        self.selected_radio.setProperty(
            "exportScope",
            ExportScopeKind.SELECTED_ONE.value if selected_count == 1 else ExportScopeKind.SELECTED_MULTIPLE.value,
        )

        filt_label = (
            f"All measurements matching current filters ({filtered_count} record(s) across all pages)"
            if has_active_filters
            else f"All measurements matching current filters (no filters active; {filtered_count} total)"
        )
        self.filtered_radio = QRadioButton(filt_label)
        self.filtered_radio.setEnabled(has_active_filters and filtered_count > 0)
        self.filtered_radio.setProperty("exportScope", ExportScopeKind.FILTERED.value)

        self.all_radio = QRadioButton(f"All saved measurements ({total_stored} record(s) total)")
        self.all_radio.setEnabled(total_stored > 0)
        self.all_radio.setProperty("exportScope", ExportScopeKind.ALL.value)

        for idx, btn in enumerate((self.selected_radio, self.filtered_radio, self.all_radio)):
            self._scope_group.addButton(btn, idx)
            scope_layout.addWidget(btn)

        if has_active_filters and filter_summary:
            scope_layout.addWidget(
                _dlg_label(f"Active filters: {filter_summary}", size=8, color=SUBTLE)
            )

        # Pick most specific enabled default scope cleanly
        if selected_count > 0:
            self.selected_radio.setChecked(True)
        elif has_active_filters and filtered_count > 0:
            self.filtered_radio.setChecked(True)
        elif total_stored > 0:
            self.all_radio.setChecked(True)

        layout.addWidget(scope_frame)

        # Privacy notice
        notice_frame = QFrame()
        notice_frame.setObjectName("diagBanner")
        nl = QVBoxLayout(notice_frame)
        nl.setContentsMargins(12, 9, 12, 9)
        nl.setSpacing(3)
        nl.addWidget(_dlg_label("Privacy Notice", size=9, bold=True, color=WARNING))
        nl.addWidget(_dlg_label(EXPORT_PRIVACY_NOTICE, size=8, color=MUTED))
        layout.addWidget(notice_frame)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)

        self.confirm_button = QPushButton("Choose Destination…")
        self.confirm_button.setEnabled(total_stored > 0)
        self.confirm_button.setStyleSheet(
            f"background: #2d243b; border: 1px solid {GREEN}; color: {GREEN}; font-weight: 700;"
        )
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)

    def selected_format(self) -> ExportFormat:
        checked = self._format_group.checkedButton()
        if checked is not None:
            raw = checked.property("exportFormat")
            if isinstance(raw, str):
                return ExportFormat(raw)
        return ExportFormat.CSV

    def selected_scope_kind(self) -> ExportScopeKind:
        checked = self._scope_group.checkedButton()
        if checked is not None:
            raw = checked.property("exportScope")
            if isinstance(raw, str):
                return ExportScopeKind(raw)
        return ExportScopeKind.ALL


class DeleteConfirmationDialog(QDialog):
    """Explicit destructive-operation confirmation with safe Cancel default."""

    def __init__(
        self,
        authorization: DeleteScopeAuthorization,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._authorization = authorization
        is_clear_all = authorization.scope_kind is DeleteScopeKind.ALL

        titles = {
            DeleteScopeKind.SELECTED: "Confirm Deletion of Selected Measurement(s)",
            DeleteScopeKind.FILTERED: "Confirm Deletion of Filtered History",
            DeleteScopeKind.ALL: "Confirm Complete History Deletion",
            DeleteScopeKind.RETENTION: "Confirm Retention Cleanup",
        }
        self.setWindowTitle(titles.get(authorization.scope_kind, "Confirm History Deletion"))
        self.setModal(True)
        self.setMinimumWidth(470)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(11)

        op_label = {
            DeleteScopeKind.SELECTED: "Delete selected measurement(s)",
            DeleteScopeKind.FILTERED: "Delete all measurements matching current filters",
            DeleteScopeKind.ALL: "Clear all locally saved measurement history",
            DeleteScopeKind.RETENTION: "Remove expired measurements",
        }[authorization.scope_kind]
        layout.addWidget(_dlg_label(op_label, size=13, bold=True, color=ERROR if is_clear_all else WARNING))

        summary_frame = QFrame()
        summary_frame.setObjectName("card")
        sl = QVBoxLayout(summary_frame)
        sl.setContentsMargins(14, 11, 14, 11)
        sl.setSpacing(5)
        sl.addWidget(
            _dlg_label(
                f"Affected measurements: {authorization.expected_count}",
                size=10,
                bold=True,
                color=TEXT,
            )
        )
        scope_desc = authorization.description or op_label
        sl.addWidget(_dlg_label(f"Scope: {scope_desc}", size=9, color=MUTED))
        if authorization.scope_kind is DeleteScopeKind.SELECTED:
            missing = len(authorization.record_ids) - authorization.expected_count
            if missing:
                sl.addWidget(_dlg_label(
                    f"{missing} requested record(s) were missing and are excluded.",
                    size=9, color=MUTED,
                ))
        sl.addWidget(
            _dlg_label(
                "Associated details will also be removed, including Speed Test throughput "
                "samples, Diagnostic checks, coverage records, recommendations, and latency samples.",
                size=8,
                color=MUTED,
            )
        )
        layout.addWidget(summary_frame)

        warning_frame = QFrame()
        warning_frame.setObjectName("diagBanner")
        wl = QVBoxLayout(warning_frame)
        wl.setContentsMargins(12, 9, 12, 9)
        wl.setSpacing(4)
        wl.addWidget(
            _dlg_label(
                "This operation cannot be undone through SpeedPup.",
                size=9,
                bold=True,
                color=ERROR,
            )
        )
        wl.addWidget(
            _dlg_label(
                "Deletion removes rows transactionally from the local SQLite database while "
                "preserving the database schema. It does not provide forensic secure erasure; "
                "SQLite pages, filesystem snapshots, backups, or storage hardware may retain prior bytes.",
                size=8,
                color=MUTED,
            )
        )
        layout.addWidget(warning_frame)

        self.confirm_checkbox: QCheckBox | None = None
        if is_clear_all:
            self.confirm_checkbox = QCheckBox(
                f"I understand this permanently deletes all {authorization.expected_count} saved measurement(s)."
            )
            self.confirm_checkbox.setStyleSheet(f"color: {TEXT}; font-size: 9pt;")
            self.confirm_checkbox.toggled.connect(self._on_checkbox_toggled)
            layout.addWidget(self.confirm_checkbox)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)

        confirm_text = (
            f"Clear All {authorization.expected_count} Measurement(s)"
            if is_clear_all
            else f"Delete {authorization.expected_count} Measurement(s)"
        )
        self.delete_button = QPushButton(confirm_text)
        self.delete_button.setAutoDefault(False)
        self.delete_button.setDefault(False)
        self.delete_button.setEnabled(not is_clear_all)
        self.delete_button.setStyleSheet(
            f"QPushButton {{ background: #3b1e29; border: 1px solid {ERROR}; color: {ERROR}; font-weight: 700; }} "
            f"QPushButton:disabled {{ background: #221d28; border-color: {BORDER}; color: {SUBTLE}; }}"
        )
        self.delete_button.clicked.connect(self.accept)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)

        self.cancel_button.setFocus(Qt.FocusReason.PopupFocusReason)

    def _on_checkbox_toggled(self, checked: bool) -> None:
        self.delete_button.setEnabled(checked)


class RetentionConfirmationDialog(QDialog):
    """Explicit confirmation before enabling/shortening retention or running cleanup."""

    def __init__(
        self,
        policy: RetentionPolicy,
        *,
        eligible_count: int | None,
        immediate_cleanup_only: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            "Confirm Retention Cleanup"
            if immediate_cleanup_only
            else "Confirm Automatic Retention Policy"
        )
        self.setModal(True)
        self.setMinimumWidth(470)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(11)

        title = (
            f"Run Cleanup for {policy.label} Retention Policy"
            if immediate_cleanup_only
            else f"Enable {policy.label} Automatic Retention"
        )
        layout.addWidget(_dlg_label(title, size=13, bold=True, color=WARNING))

        info_frame = QFrame()
        info_frame.setObjectName("card")
        il = QVBoxLayout(info_frame)
        il.setContentsMargins(14, 11, 14, 11)
        il.setSpacing(5)
        il.addWidget(
            _dlg_label(
                f"Selected retention period: {policy.label} (UTC cutoff)",
                size=10,
                bold=True,
                color=TEXT,
            )
        )
        eligible_text = (
            f"Currently eligible expired measurements: {eligible_count}"
            if eligible_count is not None
            else "Currently eligible expired measurements: unavailable"
        )
        il.addWidget(_dlg_label(eligible_text, size=9, color=WARNING if eligible_count else MUTED))
        if immediate_cleanup_only:
            il.addWidget(
                _dlg_label(
                    "Running cleanup now will permanently remove expired measurements and "
                    "their associated details from local storage. This cannot be undone through SpeedPup.",
                    size=8,
                    color=MUTED,
                )
            )
        else:
            il.addWidget(
                _dlg_label(
                    "Automatic cleanup runs locally on application startup and after completed "
                    "measurements are saved. Future expired measurements and associated details "
                    "will be deleted automatically without individual confirmation.",
                    size=8,
                    color=MUTED,
                )
            )
            il.addWidget(
                _dlg_label(
                    "Saving this policy updates your retention configuration without immediately "
                    "deleting existing records until automatic cleanup triggers or you click "
                    "'Run Cleanup Now'. Changing retention later does not restore previously deleted records.",
                    size=8,
                    color=SUBTLE,
                )
            )
        layout.addWidget(info_frame)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)

        confirm_label = "Run Cleanup Now" if immediate_cleanup_only else f"Save {policy.label} Policy"
        self.confirm_button = QPushButton(confirm_label)
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.setDefault(False)
        self.confirm_button.setStyleSheet(
            f"background: #35281a; border: 1px solid {WARNING}; color: {WARNING}; font-weight: 700;"
        )
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)

        self.cancel_button.setFocus(Qt.FocusReason.PopupFocusReason)

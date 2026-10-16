from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QTextEdit,
    QDialogButtonBox,
    QLabel,
    QCheckBox,
    QDateEdit,
    QScrollArea,
    QWidget,
    QPushButton,
    QToolButton,
    QFrame,
    QComboBox,
    QCompleter,
    QMessageBox,
    QFileDialog,
)
from typing import NamedTuple

from PySide6.QtCore import QDate, Qt, QTimer, Signal

import os

import database as db
from label_dialog import color_icon
from widgets import file_icon, local_paths_from_mime, open_attachment


class CardValues(NamedTuple):
    title: str
    description: str
    due_date: str | None
    label_ids: list[int]
    subtasks: list[tuple[str, bool]]  # (text, done), in order
    action_start: str | None
    action_end: str | None  # same as action_start for a single-day action
    links: list[tuple[str, int]]  # (kind, other_card_id); kind is "blocked_by", "blocks" or "relates"
    attachments: list[str]  # absolute paths of attached files, in order


LINK_KIND_NAMES = {"blocked_by": "Blocked by", "blocks": "Blocks", "relates": "Related to"}


class _SubtaskEdit(QLineEdit):
    """Line edit where Enter adds a new sub-task instead of closing the dialog."""

    enter_pressed = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            self.enter_pressed.emit()
            return
        super().keyPressEvent(event)


class CardDialog(QDialog):
    """Modal dialog for entering/editing a card's title, description,
    due date, action date(s), labels, checklist sub-tasks, links to
    other cards, and attached files (files can also be dropped anywhere on
    the dialog to attach them).
    """

    SUBTASK_AREA_MAX_HEIGHT = 200

    def __init__(
        self,
        board_id: int,
        title: str = "",
        description: str = "",
        due_date: str | None = None,
        selected_label_ids: set[int] | None = None,
        subtasks=None,
        action_start: str | None = None,
        action_end: str | None = None,
        card_id: int | None = None,
        links: list[tuple[str, int]] | None = None,
        attachments: list[str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.board_id = board_id
        self.card_id = card_id  # None while creating a new card
        self.setWindowTitle("Card details")
        self.setMinimumWidth(420)

        selected_label_ids = selected_label_ids or set()

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("e.g. Write project proposal")
        # Clear the "title required" red border as soon as they start typing.
        self.title_edit.textChanged.connect(lambda: self.title_edit.setStyleSheet(""))
        layout.addWidget(self.title_edit)

        layout.addWidget(QLabel("Description"))
        self.description_edit = QTextEdit()
        # Not QTextEdit(description): that reads the text as HTML, which
        # turns line breaks into spaces.
        self.description_edit.setPlainText(description)
        self.description_edit.setPlaceholderText("Optional details...")
        self.description_edit.setFixedHeight(90)
        layout.addWidget(self.description_edit)

        due_row = QHBoxLayout()
        self.due_checkbox = QCheckBox("Due date")
        self.due_edit = QDateEdit()
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("yyyy-MM-dd")
        if due_date:
            self.due_edit.setDate(QDate.fromString(due_date, "yyyy-MM-dd"))
            self.due_checkbox.setChecked(True)
        else:
            self.due_edit.setDate(QDate.currentDate())
            self.due_checkbox.setChecked(False)
        self.due_edit.setEnabled(self.due_checkbox.isChecked())
        self.due_checkbox.toggled.connect(self.due_edit.setEnabled)
        due_row.addWidget(self.due_checkbox)
        due_row.addWidget(self.due_edit)
        due_row.addStretch()
        layout.addLayout(due_row)

        # Action date: the day the work is planned for, or -- with "to"
        # ticked -- a range of days for longer tasks.
        action_row = QHBoxLayout()
        self.action_checkbox = QCheckBox("Action date")
        self.action_start_edit = QDateEdit()
        self.action_multi_checkbox = QCheckBox("to")
        self.action_end_edit = QDateEdit()
        for date_edit in (self.action_start_edit, self.action_end_edit):
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("ddd d MMM yyyy")
        start = QDate.fromString(action_start, "yyyy-MM-dd") if action_start else QDate.currentDate()
        end = QDate.fromString(action_end, "yyyy-MM-dd") if action_end else start
        self.action_start_edit.setDate(start)
        self.action_end_edit.setMinimumDate(start)
        self.action_end_edit.setDate(end)
        self.action_checkbox.setChecked(bool(action_start))
        self.action_multi_checkbox.setChecked(bool(action_start) and end > start)
        # The end can never be before the start: moving the start past it
        # drags the end along with it.
        self.action_start_edit.dateChanged.connect(self.action_end_edit.setMinimumDate)
        self.action_checkbox.toggled.connect(self._update_action_enabled)
        self.action_multi_checkbox.toggled.connect(self._update_action_enabled)
        self._update_action_enabled()
        action_row.addWidget(self.action_checkbox)
        action_row.addWidget(self.action_start_edit)
        action_row.addWidget(self.action_multi_checkbox)
        action_row.addWidget(self.action_end_edit)
        action_row.addStretch()
        layout.addLayout(action_row)

        layout.addWidget(QLabel("Labels"))
        self._label_checkboxes: list[tuple[int, QCheckBox]] = []
        labels = db.get_labels(board_id)
        if labels:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(120, 28 * len(labels) + 10))
            container = QWidget()
            container_layout = QVBoxLayout(container)
            container_layout.setContentsMargins(4, 4, 4, 4)
            for label in labels:
                checkbox = QCheckBox(label["name"])
                checkbox.setIcon(color_icon(label["color"]))
                checkbox.setChecked(label["id"] in selected_label_ids)
                container_layout.addWidget(checkbox)
                self._label_checkboxes.append((label["id"], checkbox))
            container_layout.addStretch()
            scroll.setWidget(container)
            layout.addWidget(scroll)
        else:
            no_labels_hint = QLabel(
                'No labels yet. Use "Manage Labels" in the toolbar to create some.'
            )
            no_labels_hint.setStyleSheet("color: #667085; font-size: 11px;")
            no_labels_hint.setWordWrap(True)
            layout.addWidget(no_labels_hint)

        layout.addWidget(QLabel("Sub-tasks"))
        self._subtask_rows: list[tuple[QWidget, QCheckBox, QLineEdit]] = []
        self._subtask_container = QWidget()
        self._subtask_layout = QVBoxLayout(self._subtask_container)
        self._subtask_layout.setContentsMargins(0, 0, 0, 0)
        self._subtask_layout.setSpacing(4)
        self._subtask_scroll = QScrollArea()
        self._subtask_scroll.setWidgetResizable(True)
        self._subtask_scroll.setFrameShape(QFrame.NoFrame)
        self._subtask_scroll.setWidget(self._subtask_container)
        layout.addWidget(self._subtask_scroll)
        for subtask in subtasks or []:
            self._add_subtask_row(subtask["text"], bool(subtask["done"]))
        add_subtask_button = QPushButton("+ Add sub-task")
        add_subtask_button.clicked.connect(lambda: self._add_subtask_row(focus=True))
        layout.addWidget(add_subtask_button, 0, Qt.AlignLeft)
        self._resize_subtask_area()

        self._build_links_section(layout, links or [])
        self._build_attachments_section(layout, attachments or [])

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.title_edit.setFocus()

    def _update_action_enabled(self):
        on = self.action_checkbox.isChecked()
        self.action_start_edit.setEnabled(on)
        self.action_multi_checkbox.setEnabled(on)
        self.action_end_edit.setEnabled(on and self.action_multi_checkbox.isChecked())

    # --- links ---

    def _build_links_section(self, layout: QVBoxLayout, links: list[tuple[str, int]]):
        layout.addWidget(QLabel("Links"))
        self._links = list(links)
        # Every other card on the board, for the picker and for showing names.
        self._other_cards = {
            card["id"]: f'{card["title"]}  ({card["column_name"]})'
            for card in sorted(db.get_board_cards(self.board_id), key=lambda c: c["title"].lower())
            if card["id"] != self.card_id
        }
        # Blocking links elsewhere on the board, used to refuse loops
        # (A blocks B blocks A). This card's own links come from self._links.
        self._other_block_edges = [
            edge for edge in db.get_block_edges(self.board_id) if self.card_id not in edge
        ]

        self._links_container = QWidget()
        self._links_layout = QVBoxLayout(self._links_container)
        self._links_layout.setContentsMargins(0, 0, 0, 0)
        self._links_layout.setSpacing(2)
        layout.addWidget(self._links_container)

        if not self._other_cards:
            hint = QLabel("No other cards on this board to link to yet.")
            hint.setStyleSheet("color: #667085; font-size: 11px;")
            layout.addWidget(hint)
            self._rebuild_link_rows()
            return

        add_row = QHBoxLayout()
        self.link_kind_combo = QComboBox()
        for kind, name in LINK_KIND_NAMES.items():
            self.link_kind_combo.addItem(name, kind)
        self.link_card_combo = QComboBox()
        self.link_card_combo.setEditable(True)  # type to search
        self.link_card_combo.setInsertPolicy(QComboBox.NoInsert)
        for other_id, name in self._other_cards.items():
            self.link_card_combo.addItem(name, other_id)
        self.link_card_combo.completer().setFilterMode(Qt.MatchContains)
        self.link_card_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.link_card_combo.setCurrentIndex(-1)
        self.link_card_combo.lineEdit().setPlaceholderText("Type to find a card...")
        add_link_button = QPushButton("Add link")
        add_link_button.clicked.connect(self._on_add_link)
        add_row.addWidget(self.link_kind_combo)
        add_row.addWidget(self.link_card_combo, 1)
        add_row.addWidget(add_link_button)
        layout.addLayout(add_row)
        self._rebuild_link_rows()

    def _rebuild_link_rows(self):
        while self._links_layout.count():
            widget = self._links_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self._links_container.setVisible(bool(self._links))
        for kind, other_id in self._links:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            kind_label = QLabel(f"{LINK_KIND_NAMES[kind]}:")
            kind_label.setStyleSheet("font-weight: 600;" + (" color: #d92d20;" if kind == "blocked_by" else ""))
            name_label = QLabel(self._other_cards.get(other_id, "(missing card)"))
            name_label.setTextFormat(Qt.PlainText)
            remove_button = QToolButton()
            remove_button.setText("✕")
            remove_button.setToolTip("Remove link")
            remove_button.clicked.connect(lambda _=False, link=(kind, other_id): self._remove_link(link))
            row_layout.addWidget(kind_label)
            row_layout.addWidget(name_label, 1)
            row_layout.addWidget(remove_button)
            self._links_layout.addWidget(row)

    def _remove_link(self, link: tuple[str, int]):
        self._links.remove(link)
        self._rebuild_link_rows()

    def _on_add_link(self):
        # Only accept text that exactly matches a card in the list.
        index = self.link_card_combo.findText(self.link_card_combo.currentText())
        if index < 0:
            self.link_card_combo.setFocus()
            return
        other_id = self.link_card_combo.itemData(index)
        kind = self.link_kind_combo.currentData()
        if any(other == other_id and k == kind for k, other in self._links):
            return  # already linked this way
        if kind != "relates":
            me = self.card_id or 0  # 0 stands in for a card not saved yet
            edge = (other_id, me) if kind == "blocked_by" else (me, other_id)
            if self._creates_loop(edge):
                QMessageBox.warning(
                    self,
                    "Can't add link",
                    "That would make a loop: these cards would be waiting on each "
                    "other, so neither could ever start.",
                )
                return
        self._links.append((kind, other_id))
        self._rebuild_link_rows()
        self.link_card_combo.setCurrentIndex(-1)
        self.link_card_combo.clearEditText()

    def _creates_loop(self, new_edge: tuple[int, int]) -> bool:
        """Would adding blocker->blocked edge `new_edge` close a loop, i.e. can
        the blocked card already reach the blocker through existing blocks?
        """
        me = self.card_id or 0
        edges = list(self._other_block_edges)
        for kind, other_id in self._links:
            if kind == "blocked_by":
                edges.append((other_id, me))
            elif kind == "blocks":
                edges.append((me, other_id))
        blocker, blocked = new_edge
        to_visit, seen = [blocked], set()
        while to_visit:
            node = to_visit.pop()
            if node == blocker:
                return True
            if node in seen:
                continue
            seen.add(node)
            to_visit.extend(b for a, b in edges if a == node)
        return False

    # --- attachments ---

    def _build_attachments_section(self, layout: QVBoxLayout, attachments: list[str]):
        layout.addWidget(QLabel("Attachments"))
        self._attachments = list(attachments)
        self._attachments_container = QWidget()
        self._attachments_layout = QVBoxLayout(self._attachments_container)
        self._attachments_layout.setContentsMargins(0, 0, 0, 0)
        self._attachments_layout.setSpacing(2)
        layout.addWidget(self._attachments_container)

        add_row = QHBoxLayout()
        attach_button = QPushButton("+ Attach files...")
        attach_button.clicked.connect(self._on_attach_files)
        hint = QLabel("or drop files here")
        hint.setStyleSheet("color: #667085; font-size: 11px;")
        add_row.addWidget(attach_button)
        add_row.addWidget(hint)
        add_row.addStretch()
        layout.addLayout(add_row)
        self.setAcceptDrops(True)
        self._rebuild_attachment_rows()

    def _rebuild_attachment_rows(self):
        while self._attachments_layout.count():
            widget = self._attachments_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self._attachments_container.setVisible(bool(self._attachments))
        for path in self._attachments:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            open_button = QToolButton()
            open_button.setIcon(file_icon(path))
            open_button.setText(os.path.basename(path.rstrip(os.sep)) or path)
            open_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            open_button.setAutoRaise(True)
            open_button.setToolTip(path if os.path.exists(path) else f"Not found (moved or renamed?):\n{path}")
            open_button.clicked.connect(lambda _=False, p=path, b=open_button: open_attachment(p, b))
            remove_button = QToolButton()
            remove_button.setText("✕")
            remove_button.setToolTip("Remove attachment (the file itself is kept)")
            remove_button.clicked.connect(lambda _=False, p=path: self._remove_attachment(p))
            row_layout.addWidget(open_button, 1, Qt.AlignLeft)
            row_layout.addWidget(remove_button)
            self._attachments_layout.addWidget(row)

    def _add_attachments(self, paths: list[str]):
        for path in paths:
            if path not in self._attachments:
                self._attachments.append(path)
        self._rebuild_attachment_rows()

    def _remove_attachment(self, path: str):
        self._attachments.remove(path)
        self._rebuild_attachment_rows()

    def _on_attach_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        self._add_attachments(paths)

    def dragEnterEvent(self, event):
        if local_paths_from_mime(event.mimeData()):
            # Link/copy, never move: a move would delete the original.
            event.setDropAction(Qt.LinkAction if event.possibleActions() & Qt.LinkAction else Qt.CopyAction)
            event.accept()

    def dropEvent(self, event):
        paths = local_paths_from_mime(event.mimeData())
        if paths:
            event.setDropAction(Qt.LinkAction if event.possibleActions() & Qt.LinkAction else Qt.CopyAction)
            event.accept()
            self._add_attachments(paths)

    # --- sub-tasks ---

    def _add_subtask_row(self, text: str = "", done: bool = False, focus: bool = False, after=None):
        """Add an editable sub-task row (checkbox, text, remove button),
        appended at the end or inserted just below the row `after`.
        """
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        checkbox = QCheckBox()
        checkbox.setChecked(done)
        edit = _SubtaskEdit(text)
        edit.setPlaceholderText("Sub-task")
        edit.enter_pressed.connect(lambda: self._add_subtask_row(focus=True, after=row))
        remove_button = QToolButton()
        remove_button.setText("✕")
        remove_button.setToolTip("Remove sub-task")
        remove_button.clicked.connect(lambda: self._remove_subtask_row(row))
        row_layout.addWidget(checkbox)
        row_layout.addWidget(edit, 1)
        row_layout.addWidget(remove_button)

        index = len(self._subtask_rows)
        if after is not None:
            index = next(i for i, r in enumerate(self._subtask_rows) if r[0] is after) + 1
        self._subtask_rows.insert(index, (row, checkbox, edit))
        self._subtask_layout.insertWidget(index, row)
        self._resize_subtask_area()
        if focus:
            edit.setFocus()
            # Scroll once the layout has settled, so the new row is visible.
            QTimer.singleShot(0, lambda: self._subtask_scroll.ensureWidgetVisible(row))

    def _remove_subtask_row(self, row: QWidget):
        self._subtask_rows = [r for r in self._subtask_rows if r[0] is not row]
        row.hide()
        self._subtask_layout.removeWidget(row)
        row.deleteLater()
        self._resize_subtask_area()

    def _resize_subtask_area(self):
        """Fit the scroll area to its rows, up to a cap, then scroll."""
        self._subtask_scroll.setVisible(bool(self._subtask_rows))
        # Sum the rows directly: a just-added row isn't shown until the next
        # event-loop pass, so the container's own sizeHint() would miss it.
        rows = [row for row, _, _ in self._subtask_rows]
        spacing = self._subtask_layout.spacing() * max(len(rows) - 1, 0)
        content_height = sum(row.sizeHint().height() for row in rows) + spacing
        self._subtask_scroll.setFixedHeight(min(content_height, self.SUBTASK_AREA_MAX_HEIGHT))

    def _on_accept(self):
        if not self.title_edit.text().strip():
            self.title_edit.setStyleSheet("border: 1px solid #d92d20;")
            return
        self.accept()

    def values(self) -> CardValues:
        """The dialog's contents. Dates are "yyyy-MM-dd" strings or None, and
        sub-tasks left blank are dropped.
        """
        title = self.title_edit.text().strip()
        description = self.description_edit.toPlainText().strip()
        due_date = (
            self.due_edit.date().toString("yyyy-MM-dd")
            if self.due_checkbox.isChecked()
            else None
        )
        selected_ids = [lid for lid, cb in self._label_checkboxes if cb.isChecked()]
        subtasks = [
            (edit.text().strip(), checkbox.isChecked())
            for _, checkbox, edit in self._subtask_rows
            if edit.text().strip()
        ]
        action_start = action_end = None
        if self.action_checkbox.isChecked():
            action_start = self.action_start_edit.date().toString("yyyy-MM-dd")
            action_end = action_start
            if self.action_multi_checkbox.isChecked():
                action_end = self.action_end_edit.date().toString("yyyy-MM-dd")
        return CardValues(
            title,
            description,
            due_date,
            selected_ids,
            subtasks,
            action_start,
            action_end,
            list(self._links),
            list(self._attachments),
        )

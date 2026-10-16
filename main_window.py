from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QToolBar,
    QInputDialog,
    QMessageBox,
    QToolButton,
    QMenu,
    QStackedWidget,
    QSizePolicy,
    QApplication,
    QLineEdit,
    QFileDialog,
    QLabel,
    QSlider,
    QWidgetAction,
    QTabBar,
    QVBoxLayout,
)
import shutil
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QStandardPaths, QPoint, QSize
from PySide6.QtGui import QAction, QActionGroup, QImageReader, QKeySequence, QShortcut

import database as db
from widgets import BackgroundImage, BoardArea, BoardScrollArea, ColumnWidget, board_lanes, unfinished_blockers
from card_dialog import CardDialog
from label_dialog import ManageLabelsDialog, color_icon
from gantt_view import GanttView, CardPopup
from calendar_sync import CalendarSync
import links
import theme


APP_TITLE = "Bonsai - kanban for Linux"


class _CentredSlot(QWidget):
    """Stretchy toolbar space holding a box (the search box) centred on the
    whole toolbar -- not just on the gap this space fills, which is
    off-centre when the buttons either side differ in width -- with
    `trailing` (the match count) just after it.

    To stay centred, the box gives up width (down to its minimum) when the
    buttons on the left would otherwise be in the way; only if even that
    isn't enough does it slide aside. It never overlaps the buttons.
    """

    GAP = 12  # kept clear between the box and the buttons on its left

    def __init__(self, toolbar: QWidget, box: QWidget, trailing: QWidget):
        super().__init__(toolbar)
        self._toolbar, self._box, self._trailing = toolbar, box, trailing
        box.setParent(self)
        trailing.setParent(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(self.GAP + box.minimumWidth() + trailing.width())

    def sizeHint(self):
        return QSize(self.GAP + self._box.maximumWidth() + self._trailing.width(), self._box.sizeHint().height())

    def _place(self):
        box, trailing = self._box, self._trailing
        centre = self.mapFrom(self._toolbar, QPoint(self._toolbar.width() // 2, 0)).x()
        # As wide as it can be while still centred, within its min/max width.
        room = min(2 * (centre - self.GAP), 2 * (self.width() - centre - trailing.width()))
        width = max(box.minimumWidth(), min(box.maximumWidth(), room))
        x = centre - width // 2
        x = max(self.GAP, min(x, self.width() - trailing.width() - width))  # slide only if it must
        box_height, trailing_height = box.sizeHint().height(), trailing.sizeHint().height()
        box.setGeometry(x, (self.height() - box_height) // 2, width, box_height)
        trailing.setGeometry(
            x + width, (self.height() - trailing_height) // 2, trailing.width(), trailing_height
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def moveEvent(self, event):  # e.g. the Filter button's text got longer
        super().moveEvent(event)
        self._place()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 700)

        self.board_id = self._initial_board_id()
        self.columns: dict[int, ColumnWidget] = {}
        self.active_label_filter: list[int] = []  # empty = show all cards
        self.swimlanes = db.get_setting(self.SWIMLANES_SETTING) == "on"
        self._lane_align_pending = False
        self._syncing_scroll = False

        self._build_toolbar()
        self._build_board_area()
        self._load_background()
        self.reload_board()

        self._calendar_status: dict[int, str] = {}  # board_id -> latest sync result
        self.calendar_sync = CalendarSync(self)
        self.calendar_sync.synced.connect(self._on_calendar_synced)
        self.calendar_sync.failed.connect(self._on_calendar_sync_failed)
        self._show_calendar_status()
        self.calendar_sync.sync_all()

        # Cards show dates relative to today ("3 days", "Today", overdue in red),
        # so redraw when the date changes. Checked every minute rather than with
        # one timer set for midnight, which could go off hours late if the
        # computer sleeps through it.
        self._today = date.today()
        self._day_timer = QTimer(self)
        self._day_timer.setInterval(60_000)
        self._day_timer.timeout.connect(self._check_new_day)
        self._day_timer.start()

    # --- setup ---

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.add_column_action = toolbar.addAction("+ Add Column")
        self.add_column_action.triggered.connect(self._on_add_column)

        manage_labels_action = toolbar.addAction("Manage Labels")
        manage_labels_action.triggered.connect(self._on_manage_labels)

        toolbar.addSeparator()

        self.filter_button = QToolButton()
        self.filter_button.setText("Filter: All")
        self.filter_button.setPopupMode(QToolButton.InstantPopup)
        toolbar.addWidget(self.filter_button)
        self._rebuild_filter_menu()

        # Search: hides cards on this board that don't contain every word typed.
        self.search_words: list[str] = []
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search cards (Ctrl+F)")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumWidth(120)  # shrinks on narrow windows rather than vanishing
        self.search_box.setMaximumWidth(220)
        self.search_box.setToolTip(
            "Shows only cards containing every word you type, in their title,\n"
            "description, sub-tasks, labels or attached files' names.\nEsc clears it."
        )
        self.search_count_label = QLabel()
        self.search_count_label.setContentsMargins(6, 0, 0, 0)
        self.search_count_label.setFixedWidth(90)  # room for "No matches" without the box shifting
        # Wait for a pause in typing, rather than redrawing on every key.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_search)
        self.search_box.textChanged.connect(self._search_timer.start)
        QShortcut(QKeySequence.Find, self, activated=self._focus_search)
        QShortcut(QKeySequence(Qt.Key_Escape), self.search_box, activated=self.search_box.clear,
                  context=Qt.WidgetShortcut)

        toolbar.addSeparator()

        # Calendar sync: new events become cards in the Calendar column.
        self.calendar_button = QToolButton()
        self.calendar_button.setText("Calendar")
        self.calendar_button.setPopupMode(QToolButton.InstantPopup)
        calendar_menu = QMenu(self.calendar_button)
        # Each board has its own calendar; this menu is about the open board's.
        calendar_menu.addAction("Sync now").triggered.connect(lambda: self._sync_calendar(self.board_id))
        calendar_menu.addAction("Set calendar link for this board...").triggered.connect(self._on_set_calendar_link)
        calendar_menu.addSeparator()
        calendar_menu.addAction("Teams links...").triggered.connect(self._on_set_teams_command)
        calendar_menu.addSeparator()
        self.calendar_status_action = calendar_menu.addAction("")
        self.calendar_status_action.setEnabled(False)
        self.calendar_button.setMenu(calendar_menu)
        toolbar.addWidget(self.calendar_button)

        # Appearance: light/dark theme, and the background image.
        self.appearance_button = QToolButton()
        self.appearance_button.setText("Appearance")
        self.appearance_button.setPopupMode(QToolButton.InstantPopup)
        appearance_menu = QMenu(self.appearance_button)
        appearance_menu.addSection("Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for mode, name in theme.MODES.items():
            action = appearance_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(mode == theme.current_mode())
            action.triggered.connect(lambda _=False, m=mode: theme.apply(m))
            theme_group.addAction(action)
        appearance_menu.addSection("Background")
        appearance_menu.addAction("Choose image...").triggered.connect(self._on_choose_background)
        self.remove_background_action = appearance_menu.addAction("Remove background")
        self.remove_background_action.triggered.connect(lambda: self._set_background(None))
        appearance_menu.addSection("Tint over the background")
        self._opacity_labels: dict[str, QLabel] = {}
        self.opacity_sliders = {
            kind: self._add_opacity_slider(appearance_menu, kind, label)
            for kind, label in (("kanban", "Kanban columns"), ("gantt", "Gantt chart"))
        }
        self.appearance_button.setMenu(appearance_menu)
        toolbar.addWidget(self.appearance_button)

        # Kanban / Gantt switch, pushed to the right-hand end of the toolbar.
        # The stretchy middle of the toolbar, holding the search box centred on
        # the window (pushing the Kanban/Gantt switch to the right-hand end).
        toolbar.addWidget(_CentredSlot(toolbar, self.search_box, self.search_count_label))

        # Swimlanes: split every column into one band per label (Kanban only).
        self.swimlanes_action = toolbar.addAction("No Swimlanes" if self.swimlanes else "Swimlanes")
        self.swimlanes_action.setToolTip("Split the columns into a band (swimlane) per label")
        self.swimlanes_action.triggered.connect(self._toggle_swimlanes)
        toolbar.addSeparator()

        view_group = QActionGroup(self)
        view_group.setExclusive(True)
        self.kanban_action = toolbar.addAction("Kanban")
        self.gantt_action = toolbar.addAction("Gantt")
        for action in (self.kanban_action, self.gantt_action):
            action.setCheckable(True)
            view_group.addAction(action)
        self.kanban_action.setChecked(True)
        self.kanban_action.triggered.connect(lambda: self._set_view(gantt=False))
        self.gantt_action.triggered.connect(lambda: self._set_view(gantt=True))

    def _rebuild_filter_menu(self):
        """Rebuild the label-filter dropdown from the current label set.
        Called on startup and after labels are added/renamed/deleted.
        """
        menu = QMenu(self.filter_button)
        labels = db.get_labels(self.board_id)

        if not labels:
            no_labels_action = menu.addAction("No labels yet")
            no_labels_action.setEnabled(False)
        else:
            for label in labels:
                action = QAction(color_icon(label["color"]), label["name"], menu)
                action.setCheckable(True)
                action.setChecked(label["id"] in self.active_label_filter)
                action.toggled.connect(
                    lambda checked, lid=label["id"]: self._on_filter_toggled(lid, checked)
                )
                menu.addAction(action)
            menu.addSeparator()
            clear_action = menu.addAction("Clear filter")
            clear_action.triggered.connect(self._on_clear_filter)

        self.filter_button.setMenu(menu)
        self._update_filter_button_text()

    def _on_filter_toggled(self, label_id: int, checked: bool):
        if checked and label_id not in self.active_label_filter:
            self.active_label_filter.append(label_id)
        elif not checked and label_id in self.active_label_filter:
            self.active_label_filter.remove(label_id)
        self._update_filter_button_text()
        self._apply_filter()

    def _on_clear_filter(self):
        self.active_label_filter = []
        self._rebuild_filter_menu()
        self._apply_filter()

    def _update_filter_button_text(self):
        if not self.active_label_filter:
            self.filter_button.setText("Filter: All")
        else:
            self.filter_button.setText(f"Filter: {len(self.active_label_filter)} label(s)")

    def _focus_search(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _apply_search(self):
        self.search_words = db.search_words(self.search_box.text())
        self.gantt_view.search_words = self.search_words
        for column_widget in self.columns.values():
            column_widget.search_words = self.search_words
        self._apply_filter()

    def _update_search_count(self):
        """How many cards on the board match, shown next to the box while searching."""
        if not self.search_words:
            self.search_count_label.setText("")
            return
        count = len(db.filter_cards_by_search(
            db.get_board_cards(self.board_id, self.active_label_filter or None), self.search_words
        ))
        self.search_count_label.setText(
            "No matches" if count == 0 else f"{count} card{'s' if count != 1 else ''}"
        )
        self.search_count_label.setStyleSheet("color: #d92d20;" if count == 0 else "")

    def _apply_filter(self):
        """Redraw whichever view is showing. The hidden one catches up when
        it's switched to, since _set_view() calls this too.
        """
        if self._gantt_showing():
            self.gantt_view.reload(self.board_id, self.active_label_filter)
        else:
            for column_widget in self.columns.values():
                column_widget.refresh(self.active_label_filter)
        self._update_search_count()

    # --- view switching ---

    def _gantt_showing(self) -> bool:
        return self.view_stack.currentWidget() is self.gantt_view

    def _set_view(self, gantt: bool):
        self.view_stack.setCurrentWidget(self.gantt_view if gantt else self.scroll_area)
        self.add_column_action.setEnabled(not gantt)
        self.swimlanes_action.setEnabled(not gantt)
        self._apply_filter()

    def _on_gantt_card_clicked(self, card_id: int):
        popup = CardPopup(card_id, parent=self)
        if popup.exec() == CardPopup.EDIT:
            self._on_edit_card(card_id)
        # Sub-tasks may have been ticked in the popup, or the card edited.
        self.gantt_view.reload(self.board_id, self.active_label_filter)

    def _on_manage_labels(self):
        dialog = ManageLabelsDialog(self.board_id, parent=self)
        dialog.exec()
        # Labels may have been added/renamed/recolored/deleted -- refresh
        # everything that depends on them.
        valid_ids = {l["id"] for l in db.get_labels(self.board_id)}
        self.active_label_filter = [lid for lid in self.active_label_filter if lid in valid_ids]
        self._rebuild_filter_menu()
        self._apply_filter()

    # --- background image ---

    BACKGROUND_SETTING = "board_background"
    OPACITY_SETTINGS = {"kanban": "kanban_panel_alpha", "gantt": "gantt_panel_alpha"}

    def _add_opacity_slider(self, menu: QMenu, kind: str, label: str) -> QSlider:
        """A labelled 0-100% slider inside the menu, for how solid the tint
        over the background image is. Changes show straight away.
        """
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(24, 2, 12, 2)
        name = QLabel(label)
        name.setMinimumWidth(110)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setMinimumWidth(140)
        value_label = QLabel()
        self._opacity_labels[kind] = value_label
        value_label.setMinimumWidth(36)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda percent: value_label.setText(f"{percent}%"))
        slider.valueChanged.connect(lambda percent: self._on_opacity_changed(kind, percent))
        layout.addWidget(name)
        layout.addWidget(slider, 1)
        layout.addWidget(value_label)
        action = QWidgetAction(menu)
        action.setDefaultWidget(container)
        menu.addAction(action)
        slider.setToolTip("How solid the tint over the background image is: 0% = clear, 100% = solid")
        return slider

    def _on_opacity_changed(self, kind: str, percent: int):
        alpha = round(percent * 255 / 100)
        if kind == "kanban":
            self.background.kanban_alpha = alpha
            for column_widget in self.columns.values():
                column_widget.set_panel(self.background.is_set(), alpha)
        else:
            self.background.gantt_alpha = alpha
            self.gantt_view.viewport().update()
        self._opacity_save_timer.start()

    def _save_opacity(self):
        db.set_setting(self.OPACITY_SETTINGS["kanban"], str(self.background.kanban_alpha))
        db.set_setting(self.OPACITY_SETTINGS["gantt"], str(self.background.gantt_alpha))

    def _load_background(self):
        path = db.get_setting(self.BACKGROUND_SETTING)
        if path and not self.background.load(path):
            path = None  # file gone or unreadable: carry on without it
        self.remove_background_action.setEnabled(bool(path))
        # Save slider changes once the slider stops moving, not on every step.
        self._opacity_save_timer = QTimer(self)
        self._opacity_save_timer.setSingleShot(True)
        self._opacity_save_timer.setInterval(500)
        self._opacity_save_timer.timeout.connect(self._save_opacity)
        for kind, setting in self.OPACITY_SETTINGS.items():
            default = getattr(self.background, f"{kind}_alpha")
            try:
                alpha = min(255, max(0, int(db.get_setting(setting) or default)))
            except ValueError:
                alpha = default
            setattr(self.background, f"{kind}_alpha", alpha)
            slider = self.opacity_sliders[kind]
            slider.blockSignals(True)  # just show the value; nothing to redraw or save yet
            slider.setValue(round(alpha * 100 / 255))
            slider.blockSignals(False)
            self._opacity_labels[kind].setText(f"{slider.value()}%")
        self._update_opacity_sliders()

    def _update_opacity_sliders(self):
        """The tint only shows over a background image, so the sliders are
        only usable when there is one.
        """
        for slider in self.opacity_sliders.values():
            slider.parentWidget().setEnabled(self.background.is_set())

    def _on_choose_background(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a background image",
            # The desktop's own Pictures folder (named differently in other languages).
            QStandardPaths.writableLocation(QStandardPaths.PicturesLocation) or str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if path:
            self._set_background(path)

    def _set_background(self, source: str | None):
        """Use a copy of the image (kept with the board's data, so moving or
        deleting the original doesn't matter), or remove the background.
        """
        old = db.get_setting(self.BACKGROUND_SETTING)
        new = None
        if source:
            if not QImageReader(source).canRead():
                QMessageBox.warning(self, "Background", "That file couldn't be opened as an image.")
                return
            new = str(db.DB_PATH.parent / f"background{Path(source).suffix.lower()}")
            if Path(source).resolve() != Path(new).resolve():
                shutil.copyfile(source, new)
        self.background.load(new)
        self.scroll_area.viewport().update()
        self.gantt_view.viewport().update()
        if old and old != new:
            Path(old).unlink(missing_ok=True)
        db.set_setting(self.BACKGROUND_SETTING, new)
        self.remove_background_action.setEnabled(bool(new))
        self._update_opacity_sliders()
        for column_widget in self.columns.values():
            column_widget.set_panel(new is not None, self.background.kanban_alpha)

    # --- calendar sync ---

    def _set_calendar_status(self, board_id: int, text: str):
        self._calendar_status[board_id] = text
        if board_id == self.board_id:
            self._show_calendar_status()

    def _show_calendar_status(self):
        """The open board's calendar status, in the Calendar menu and tooltip."""
        if not CalendarSync.url(self.board_id):
            text = "No calendar link set for this board"
        else:
            text = self._calendar_status.get(self.board_id, "Not synced yet")
        self.calendar_status_action.setText(text)
        self.calendar_button.setToolTip(text)

    def _sync_calendar(self, board_id: int):
        if CalendarSync.url(board_id):
            self._set_calendar_status(board_id, "Syncing...")
            self.calendar_sync.sync_board(board_id)

    def _on_set_calendar_link(self):
        board_name = self._board_name(self.board_id)
        url, ok = QInputDialog.getText(
            self,
            f"Calendar link for \u201c{board_name}\u201d",
            f"Events from this calendar become cards on the \u201c{board_name}\u201d board.\n"
            "Each board can have its own calendar.\n\n"
            "Paste the calendar's published .ics link (often called an ICS or\n"
            "iCal address). For example:\n\n"
            "Outlook on the web: Settings → Calendar → Shared calendars →\n"
            "    Publish a calendar, then copy the ICS link.\n"
            "Google Calendar: Settings → your calendar → Integrate calendar →\n"
            "    Secret address in iCal format.\n\n"
            "Leave it empty to stop syncing.",
            QLineEdit.Normal,
            CalendarSync.url(self.board_id) or "",
        )
        if not ok:
            return
        CalendarSync.set_url(self.board_id, url.strip())
        self._calendar_status.pop(self.board_id, None)
        self._show_calendar_status()
        self._sync_calendar(self.board_id)

    def _on_set_teams_command(self):
        """Show which app Teams links open in, and let a command be set by hand."""
        launcher = links.find_teams_launcher()
        found = (
            f"Found: {launcher.name}  ({launcher.path})"
            if launcher
            else "No Teams app found, so Teams links open in your web browser."
        )
        command, ok = QInputDialog.getText(
            self,
            "Teams links",
            "Teams links (e.g. meeting join links) open in a Teams app if one is\n"
            "installed: a web app, a site installed from your browser, or a\n"
            "native app.\n\n"
            f"{found}\n\n"
            "To use something else, enter its command here, with {url} where the\n"
            "link goes, e.g.   chromium --app={url}\n"
            "Leave it empty to pick automatically.",
            QLineEdit.Normal,
            db.get_setting(links.COMMAND_SETTING) or "",
        )
        if ok:
            db.set_setting(links.COMMAND_SETTING, command.strip() or None)

    def _on_calendar_synced(self, board_id: int, result):
        parts = [
            f"{count} {word}"
            for count, word in ((result.created, "new"), (result.updated, "updated"), (result.cancelled, "cancelled"))
            if count
        ]
        self._set_calendar_status(
            board_id, f"Last synced {datetime.now():%H:%M}" + (f" — {', '.join(parts)}" if parts else " — no changes")
        )
        # Other boards' changes show when you switch to them (they reload then).
        if result.changed and board_id == self.board_id:
            self._refresh_after_sync(result.column_created)

    def _on_calendar_sync_failed(self, board_id: int, message: str):
        self._set_calendar_status(board_id, f"Sync failed at {datetime.now():%H:%M}: {message}")

    def _check_new_day(self):
        if date.today() != self._today:
            self._today = date.today()
            self._refresh_after_sync(False)  # redraws cards and the Gantt chart's today line

    def _refresh_after_sync(self, columns_changed: bool):
        """Show synced changes, but not in the middle of a card drag or while
        a dialog is open -- rebuilding the cards then would pull them out
        from under it. Tries again a second later instead.
        """
        if QApplication.mouseButtons() != Qt.NoButton or QApplication.activeModalWidget() is not None:
            QTimer.singleShot(1000, lambda: self._refresh_after_sync(columns_changed))
            return
        self._rebuild_filter_menu()  # the "Cancelled" label may be new
        if columns_changed:
            self.reload_board()
        self._apply_filter()

    def _build_board_area(self):
        self.background = BackgroundImage()
        self.scroll_area = BoardScrollArea(self.background)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.board_container = BoardArea()
        self.board_container.column_dropped.connect(self._on_column_dropped)
        self.board_layout = QHBoxLayout(self.board_container)
        self.board_layout.setAlignment(Qt.AlignLeft)
        self.board_layout.setSpacing(12)
        self.board_layout.setContentsMargins(12, 12, 12, 12)

        self.scroll_area.setWidget(self.board_container)
        # setWidget() turns this on; keep the board see-through so a
        # background image shows behind the columns.
        self.board_container.setAutoFillBackground(False)

        self.gantt_view = GanttView()
        self.gantt_view.background = self.background
        self.gantt_view.card_clicked.connect(self._on_gantt_card_clicked)

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.scroll_area)
        self.view_stack.addWidget(self.gantt_view)

        # One tab per board, then a "+" tab for making a new one.
        self.board_tabs = QTabBar()
        self.board_tabs.setDocumentMode(True)
        self.board_tabs.setExpanding(False)
        self.board_tabs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.board_tabs.currentChanged.connect(self._on_board_tab_changed)
        self.board_tabs.tabBarDoubleClicked.connect(self._on_board_tab_double_clicked)
        self.board_tabs.customContextMenuRequested.connect(self._on_board_tab_menu)
        self._rebuild_board_tabs()

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.board_tabs)
        central_layout.addWidget(self.view_stack, 1)
        self.setCentralWidget(central)

    # --- boards ---

    CURRENT_BOARD_SETTING = "current_board"

    @staticmethod
    def _board_ids() -> list[int]:
        return [board["id"] for board in db.get_boards()]

    def _initial_board_id(self) -> int:
        """The board that was open last time, if it still exists."""
        saved = db.get_setting(self.CURRENT_BOARD_SETTING)
        return int(saved) if saved and saved.isdigit() and int(saved) in self._board_ids() else self._board_ids()[0]

    def _board_name(self, board_id: int) -> str:
        return next((board["name"] for board in db.get_boards() if board["id"] == board_id), "")

    def _rebuild_board_tabs(self):
        self.board_tabs.blockSignals(True)
        while self.board_tabs.count():
            self.board_tabs.removeTab(0)
        for board in db.get_boards():
            index = self.board_tabs.addTab(board["name"])
            self.board_tabs.setTabData(index, board["id"])
            if board["id"] == self.board_id:
                self.board_tabs.setCurrentIndex(index)
        plus = self.board_tabs.addTab("+")
        self.board_tabs.setTabToolTip(plus, "New board")
        self.board_tabs.blockSignals(False)

    def _on_board_tab_changed(self, index: int):
        board_id = self.board_tabs.tabData(index)
        if board_id is None:  # the "+" tab
            self._on_new_board()
        else:
            self._switch_board(board_id)

    def _switch_board(self, board_id: int):
        """Show another board: its columns and cards, labels, Gantt chart."""
        self.board_id = board_id
        db.set_setting(self.CURRENT_BOARD_SETTING, str(board_id))
        self.active_label_filter = []  # labels belong to a board
        self._rebuild_filter_menu()
        # A search is about the board it was typed on: start afresh.
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self.search_words = self.gantt_view.search_words = []
        self._update_search_count()
        self.reload_board()
        if self._gantt_showing():
            self.gantt_view.reload(self.board_id, self.active_label_filter)
        self._rebuild_board_tabs()
        self._show_calendar_status()

    def _on_new_board(self):
        name, ok = QInputDialog.getText(self, "New board", "Board name:")
        if ok and name.strip():
            self._switch_board(db.add_board(name.strip()))
        else:
            self._rebuild_board_tabs()  # back to the board that was showing

    def _on_board_tab_double_clicked(self, index: int):
        board_id = self.board_tabs.tabData(index)
        if board_id is not None:
            self._rename_board(board_id)

    def _on_board_tab_menu(self, pos):
        board_id = self.board_tabs.tabData(self.board_tabs.tabAt(pos))
        if board_id is None:
            return
        menu = QMenu(self)
        menu.addAction("Rename board...").triggered.connect(lambda: self._rename_board(board_id))
        delete = menu.addAction("Delete board...")
        delete.setEnabled(len(self._board_ids()) > 1)
        delete.triggered.connect(lambda: self._delete_board(board_id))
        menu.exec(self.board_tabs.mapToGlobal(pos))

    def _rename_board(self, board_id: int):
        name, ok = QInputDialog.getText(
            self, "Rename board", "Board name:", QLineEdit.Normal, self._board_name(board_id)
        )
        if ok and name.strip():
            db.rename_board(board_id, name.strip())
            self._rebuild_board_tabs()

    def _delete_board(self, board_id: int):
        if len(self._board_ids()) <= 1:
            return
        card_count = len(db.get_board_cards(board_id))
        reply = QMessageBox.warning(
            self,
            "Delete board",
            f'Delete the board "{self._board_name(board_id)}" and everything on it '
            f"({card_count} card{'s' if card_count != 1 else ''}, its columns and labels)?\n\n"
            "This can't be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        db.delete_board(board_id)  # its calendar link and events go with it
        self._calendar_status.pop(board_id, None)
        if board_id == self.board_id:
            self._switch_board(self._board_ids()[0])
        else:
            self._rebuild_board_tabs()

    # --- board loading ---

    def reload_board(self):
        # Clear existing column widgets.
        while self.board_layout.count():
            item = self.board_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()  # gone at once, not when Qt gets round to deleting it
                widget.deleteLater()
        self.columns.clear()

        for column_row in db.get_columns(self.board_id):
            column_widget = ColumnWidget(column_row, self.active_label_filter, self.swimlanes, self.search_words)
            column_widget.lanes_changed.connect(self._schedule_lane_alignment)
            column_widget.list_widget.verticalScrollBar().valueChanged.connect(
                lambda value, cid=column_row["id"]: self._sync_lane_scroll(cid, value)
            )
            column_widget.add_card_requested.connect(self._on_add_card)
            column_widget.edit_card_requested.connect(self._on_edit_card)
            column_widget.card_dropped.connect(self._on_card_dropped)
            column_widget.delete_requested.connect(self._on_delete_card)
            column_widget.files_attached.connect(self._on_files_attached)
            column_widget.move_requested.connect(self._on_move_column)
            column_widget.delete_column_requested.connect(self._on_delete_column)
            column_widget.set_panel(self.background.is_set(), self.background.kanban_alpha)
            self.board_layout.addWidget(column_widget)
            self.columns[column_row["id"]] = column_widget
        self._schedule_lane_alignment()

    # --- swimlanes ---

    SWIMLANES_SETTING = "swimlanes"

    def _toggle_swimlanes(self):
        self.swimlanes = not self.swimlanes
        db.set_setting(self.SWIMLANES_SETTING, "on" if self.swimlanes else "off")
        self.swimlanes_action.setText("No Swimlanes" if self.swimlanes else "Swimlanes")
        for column_widget in self.columns.values():
            column_widget.set_swimlanes(self.swimlanes)
        self._update_lane_lines()

    def _schedule_lane_alignment(self):
        """Line the lanes up once things settle: several columns usually
        change at once, and one alignment afterwards covers them all.
        """
        if self.swimlanes and not self._lane_align_pending:
            self._lane_align_pending = True
            QTimer.singleShot(0, self._align_lanes)

    def _align_lanes(self):
        """Make each lane the same height in every column (the height it needs
        in its fullest column), so lanes run straight across the board.
        """
        self._lane_align_pending = False
        if not self.swimlanes or not self.columns:
            return
        columns = list(self.columns.values())
        # A column refreshed on its own may have an out-of-date set of lanes
        # (e.g. a card there got a new label): bring those up to date first.
        lane_ids = [
            lane_id for lane_id, _, _ in board_lanes(self.board_id, self.active_label_filter, self.search_words)
        ]
        for column_widget in columns:
            if column_widget.lane_ids != lane_ids:
                column_widget.refresh()
        heights = [column_widget.lane_content_heights() for column_widget in columns]
        tallest = [max(lane) for lane in zip(*heights)]
        for column_widget in columns:
            column_widget.set_lane_heights(tallest)
        first = columns[0].list_widget.verticalScrollBar().value()
        self._sync_lane_scroll(columns[0].column_id, first)
        self._update_lane_lines()

    def _sync_lane_scroll(self, column_id: int, value: int):
        """With swimlanes on, all columns scroll together so lanes stay lined up."""
        if not self.swimlanes or self._syncing_scroll:
            return
        self._syncing_scroll = True
        for other_id, column_widget in self.columns.items():
            if other_id != column_id:
                column_widget.list_widget.verticalScrollBar().setValue(value)
        self._syncing_scroll = False
        self._update_lane_lines()

    def _update_lane_lines(self):
        """Tell the board where the lines between lanes are, so it can draw
        them through the gaps between columns too (each column draws its own
        stretch). All columns line up, so the first one's positions do.
        """
        if not self.swimlanes or not self.columns:
            self.board_container.set_lane_lines([])
            return
        first = next(iter(self.columns.values()))
        viewport = first.list_widget.viewport()
        to_board = lambda y: viewport.mapTo(self.board_container, QPoint(0, y)).y()
        self.board_container.set_lane_lines(
            [to_board(y) for y in first.lane_divider_ys()], to_board(0), to_board(viewport.height())
        )

    # --- column actions ---

    def _on_add_column(self):
        name, ok = QInputDialog.getText(self, "New column", "Column name:")
        if ok and name.strip():
            db.add_column(self.board_id, name.strip())
            self.reload_board()

    def _on_column_dropped(self, column_id: int, index: int):
        """A column header was dragged to slot `index` (counted among the
        columns as they were before the move).
        """
        order = list(self.columns)
        old_index = order.index(column_id)
        if index > old_index:
            index -= 1  # its own old slot no longer counts
        order.insert(index, order.pop(old_index))
        self._set_column_order(order)

    def _on_move_column(self, column_id: int, step: int):
        order = list(self.columns)
        old_index = order.index(column_id)
        if 0 <= old_index + step < len(order):
            order.insert(old_index + step, order.pop(old_index))
            self._set_column_order(order)

    def _on_delete_column(self, column_id: int):
        column = db.get_column(column_id)
        if column is None:
            return
        card_count = len(db.get_cards(column_id))
        if card_count:
            detail = f"and the {card_count} card{'s' if card_count != 1 else ''} in it"
            message = f'Delete the column "{column["name"]}" {detail}?\n\nThis can\'t be undone.'
        else:
            message = f'Delete the empty column "{column["name"]}"?'
        reply = QMessageBox.warning(
            self, "Delete column", message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        db.delete_column(column_id)
        # Rebuilt later, not now: this runs from the column's own header menu.
        QTimer.singleShot(0, self._after_column_deleted)

    def _after_column_deleted(self):
        self.reload_board()
        self._update_search_count()
        if self._gantt_showing():
            self.gantt_view.reload(self.board_id, self.active_label_filter)

    def _set_column_order(self, order: list[int]):
        """Save the new order and move the existing column widgets to match.
        Moving rather than rebuilding them matters: this runs while the
        dragged column's header is still finishing its drag.
        """
        if order == list(self.columns):
            return
        db.reorder_columns(order)
        for column_widget in self.columns.values():
            self.board_layout.removeWidget(column_widget)
        for column_id in order:
            self.board_layout.addWidget(self.columns[column_id])
        self.columns = {column_id: self.columns[column_id] for column_id in order}

    # --- card actions ---

    def _on_add_card(self, column_id: int):
        dialog = CardDialog(self.board_id, parent=self)
        if dialog.exec():
            v = dialog.values()
            card_id = db.add_card(column_id, v.title, v.description, v.due_date, v.action_start, v.action_end)
            db.set_card_labels(card_id, v.label_ids)
            db.set_card_subtasks(card_id, v.subtasks)
            db.set_card_links(card_id, v.links)
            db.set_card_attachments(card_id, v.attachments)
            self._refresh_all_columns()

    def _on_edit_card(self, card_id: int):
        card = db.get_card(card_id)
        if card is None:
            return
        current_label_ids = {l["id"] for l in db.get_card_labels(card_id)}
        dialog = CardDialog(
            self.board_id,
            card["title"],
            card["description"],
            card["due_date"],
            current_label_ids,
            db.get_subtasks(card_id),
            card["action_start"],
            card["action_end"],
            card_id=card_id,
            links=[(link["kind"], link["other_id"]) for link in db.get_links_for_cards([card_id]).get(card_id, [])],
            attachments=[a["path"] for a in db.get_attachments(card_id)],
            parent=self,
        )
        if dialog.exec():
            v = dialog.values()
            db.update_card(card_id, v.title, v.description, v.due_date, v.action_start, v.action_end)
            db.set_card_labels(card_id, v.label_ids)
            db.set_card_subtasks(card_id, v.subtasks)
            db.set_card_links(card_id, v.links)
            db.set_card_attachments(card_id, v.attachments)
            self._refresh_all_columns()

    def _on_files_attached(self, card_id: int, paths: list[str]):
        """Files dropped or pasted straight onto a card on the board."""
        card = db.get_card(card_id)
        if card is None:
            return
        db.add_attachments(card_id, paths)
        self.columns[card["column_id"]].refresh()

    def _on_delete_card(self, card_id: int):
        card = db.get_card(card_id)
        if card is None:
            return
        reply = QMessageBox.question(
            self,
            "Delete card",
            f'Delete "{card["title"]}"? This cannot be undone.',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            db.delete_card(card_id)
            self._refresh_all_columns()

    def _refresh_all_columns(self):
        """Refresh every column, not just the one that changed: a card's
        title, column or links also show on the cards it's linked to.
        """
        for column_widget in self.columns.values():
            column_widget.refresh()
        self._update_search_count()  # an edit can make a card start or stop matching

    def _confirm_start_if_blocked(self, card_id: int, target_column_id: int) -> bool:
        """Moving a card out of the first column counts as starting it. If
        it's still blocked, warn and let them cancel or move it anyway.
        """
        first_column_id = next(iter(self.columns), None)
        if target_column_id == first_column_id:
            return True
        blockers = unfinished_blockers(db.get_links_for_cards([card_id]).get(card_id, []))
        if not blockers:
            return True
        card = db.get_card(card_id)
        waiting_on = "\n".join(f'  • {b["other_title"]}  ({b["other_column"]})' for b in blockers)
        reply = QMessageBox.warning(
            self,
            "Card is blocked",
            f'"{card["title"]}" is waiting on:\n\n{waiting_on}\n\nMove it anyway?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _on_card_dropped(self, card_id: int, source_column_id: int, target_column_id: int, before_card_id: int):
        if before_card_id == card_id:
            return  # dropped onto its own upper half -- nothing moves
        if source_column_id != target_column_id and not self._confirm_start_if_blocked(card_id, target_column_id):
            return
        # Insert relative to a card id rather than a row number, so the drop
        # lands in the right place even when a label filter hides some cards
        # and regardless of where the dragged card was removed from.
        target_ids = [c["id"] for c in db.get_cards(target_column_id) if c["id"] != card_id]
        if before_card_id in target_ids:
            target_ids.insert(target_ids.index(before_card_id), card_id)
        else:
            target_ids.append(card_id)
        db.reorder_column(target_column_id, target_ids)

        if source_column_id == target_column_id:
            self.columns[target_column_id].refresh()
        else:
            # Moving into or out of Done changes whether the cards it blocks
            # show as blocked, and those can be in any column.
            self._refresh_all_columns()

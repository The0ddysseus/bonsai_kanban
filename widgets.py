"""
UI building blocks: CardWidget (visual card), CardListWidget (a column's
drag-and-drop-enabled list of cards), and ColumnWidget (header + list +
add-card button, wrapping one board column).

Drag-and-drop approach: native QListWidget drag/drop across *separate*
list widgets is fragile once you're using custom itemWidgets, so this
implements it manually: mouse-press/move starts a QDrag carrying the
card id + source column id as custom MIME data; the target list's
dropEvent reads that data and emits a signal. No widget state is
mutated directly during the drag -- the signal handler updates SQLite
and then both the source and target ColumnWidgets do a full refresh()
from the database. Slightly more DB reads than a fully optimistic UI
update, but far simpler to keep correct.

Files dragged in from a file manager (or pasted with Ctrl+V) arrive as
file:// URLs instead; dropped on a card, they become attachments.
"""

import html
import os
import re
from pathlib import Path
from datetime import date

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView,
    QFrame,
    QMenu,
    QCheckBox,
    QGridLayout,
    QToolButton,
    QToolTip,
    QFileDialog,
    QFileIconProvider,
    QApplication,
    QStyle,
    QScrollArea,
)
from PySide6.QtCore import Qt, QMimeData, QSize, Signal, QUrl, QFileInfo, QMimeDatabase, QEvent
from PySide6.QtGui import QDrag, QAction, QDesktopServices, QIcon, QKeySequence, QCursor, QPainter, QColor, QPixmap, QPalette

import database as db
from links import open_link

CARD_MIME_TYPE = "application/x-kanban-card-id"

# Sub-task checkboxes on cards are styled by hand (cards are white in every
# theme, so the theme's own checkbox colours don't suit them); this is the tick.
CHECK_ICON = (Path(__file__).resolve().parent / "assets" / "check.svg").as_posix()
COLUMN_MIME_TYPE = "application/x-kanban-column-id"


# Columns with one of these names (case-insensitive) count as finished work,
# so their cards' due dates are shown plainly rather than as overdue.
DONE_COLUMN_NAMES = {"done", "complete", "completed", "finished"}


def is_done_column(column_name: str) -> bool:
    return column_name.strip().lower() in DONE_COLUMN_NAMES


def unfinished_blockers(links) -> list[dict]:
    """The "blocked by" links whose blocking card isn't in a done column yet."""
    return [
        link for link in links or []
        if link["kind"] == "blocked_by" and not is_done_column(link["other_column"])
    ]


def _due_date_style(due_date_str: str, done: bool = False) -> tuple[str, str]:
    """Returns (display_text, color) for a due date, styled by urgency, in the
    same "Mon 20 Oct" form as the action date, with how far off it is:
    green when it's coming up, amber on the day, red once overdue. Cards in a
    done column are never shown as overdue or due today, and go grey (with no
    countdown) like their action date.
    """
    due = date.fromisoformat(due_date_str)
    today = date.today()
    if done:
        return f"Due: {_short_date(due)}", "#667085"
    when = f"{_short_date(due)} · {days_away(due, due)}"
    if due < today:
        return f"Overdue: {when}", "#d92d20"
    if due == today:
        return f"Due: {when}", "#b54708"
    return f"Due: {when}", "#079455"


def days_away(start: date, end: date) -> str:
    """How far off a day (or a run of days) is: "3 days", "1 day", "Today"
    (while it's on), or "2 days ago" once it's over.
    """
    today = date.today()
    if start > today:
        n = (start - today).days
    elif end < today:
        n = (today - end).days
    else:
        return "Today"
    # Non-breaking spaces: if the line wraps, "3 days ago" stays in one piece.
    text = f"{n}\u00a0day{'s' if n != 1 else ''}"
    return text if start > today else f"{text}\u00a0ago"


def action_dates_with_countdown(action_start: str, action_end: str | None) -> str:
    """The action date(s) plus how far off they are, e.g.
    "Fri 16 Oct – Sat 17 Oct · 1 day".
    """
    start = date.fromisoformat(action_start)
    end = max(start, date.fromisoformat(action_end) if action_end else start)
    return f"{format_action_dates(action_start, action_end)} · {days_away(start, end)}"


def _short_date(day: date) -> str:
    """e.g. "Mon 20 Oct", with the year added when it isn't this year."""
    # Non-breaking spaces keep a date in one piece if its line wraps.
    text = f"{day:%a}\u00a0{day.day}\u00a0{day:%b}"
    return text if day.year == date.today().year else f"{text}\u00a0{day.year}"


def format_action_dates(action_start: str, action_end: str | None) -> str:
    """e.g. "Mon 20 Oct" for a single day, "Mon 20 Oct – Fri 24 Oct" for a range."""
    start = date.fromisoformat(action_start)
    end = date.fromisoformat(action_end) if action_end else start
    if end <= start:
        return _short_date(start)
    return f"{_short_date(start)} – {_short_date(end)}"


def readable_text_color(hex_color: str) -> str:
    """Black or white text, whichever is more readable on this background."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#1d2939" if luminance > 0.6 else "#ffffff"


# Web addresses in a description: http(s)://... or www.... up to the next space.
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"]+", re.IGNORECASE)
# Sentence punctuation that usually follows a URL rather than belonging to it.
_URL_TRAILING_PUNCTUATION = ".,;:!?'\")]}"


def _split_url(match_text: str) -> tuple[str, str]:
    """(url, trailing text): drops punctuation stuck to the end of a URL, but
    keeps a closing bracket that has a matching opening one in the URL, as in
    https://en.wikipedia.org/wiki/Python_(programming_language).
    """
    url = match_text
    while url and url[-1] in _URL_TRAILING_PUNCTUATION:
        closer = url[-1]
        opener = {")": "(", "]": "[", "}": "{"}.get(closer)
        if opener and url.count(opener) >= url.count(closer):
            break
        url = url[:-1]
    return url, match_text[len(url):]


HIGHLIGHT_STYLE = "background-color: #fde68a; color: #1d2939;"  # search matches on cards


def highlighted(text: str, words: list[str] | None = None) -> str:
    """Plain text as escaped HTML, with any of the (search) words marked up
    so they stand out. Matching ignores case.
    """
    if not words:
        return html.escape(text)
    pattern = re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)), re.IGNORECASE)
    parts, last = [], 0
    for match in pattern.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        parts.append(f'<span style="{HIGHLIGHT_STYLE}">{html.escape(match.group())}</span>')
        last = match.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)


def linkify(text: str, words: list[str] | None = None) -> str:
    """Plain text as HTML, with every web address turned into a clickable
    link, and any search words highlighted. Everything else is escaped, so
    "<...>" still shows as typed, and line breaks and runs of spaces are kept.
    """
    parts = []
    last = 0
    for match in _URL_RE.finditer(text):
        url, trailing = _split_url(match.group())
        href = url if "://" in url else f"https://{url}"
        parts.append(highlighted(text[last : match.start()], words))
        parts.append(f'<a href="{html.escape(href)}" style="color: #1570ef;">{highlighted(url, words)}</a>')
        parts.append(highlighted(trailing, words))
        last = match.end()
    parts.append(highlighted(text[last:], words))
    return f'<span style="white-space: pre-wrap;">{"".join(parts)}</span>'


def local_paths_from_mime(mime_data: QMimeData | None) -> list[str]:
    """Existing local files/folders carried by a drag or the clipboard: file
    URLs from a file manager, or a single path copied as plain text. (An
    empty clipboard has no data at all: None.)
    """
    if mime_data is None:
        return []
    if mime_data.hasUrls():
        candidates = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()]
    elif mime_data.hasText():
        text = mime_data.text().strip()
        candidates = [QUrl(text).toLocalFile() if text.startswith("file://") else os.path.expanduser(text)]
    else:
        candidates = []
    return [os.path.abspath(path) for path in candidates if path and os.path.exists(path)]


_mime_db = QMimeDatabase()
_icon_provider = None  # created on first use; needs a running QApplication


def file_icon(path: str) -> QIcon:
    """The desktop theme's icon for a file's type (PDF, spreadsheet, ...),
    like a file manager shows. Missing files get a "missing" icon.
    """
    global _icon_provider
    if not os.path.exists(path):
        # A greyed-out plain file icon.
        plain = QApplication.style().standardIcon(QStyle.SP_FileIcon)
        return QIcon(plain.pixmap(32, 32, QIcon.Disabled))
    mime_type = _mime_db.mimeTypeForFile(path)
    icon = QIcon.fromTheme(mime_type.iconName())
    if icon.isNull():
        icon = QIcon.fromTheme(mime_type.genericIconName())
    if icon.isNull():
        if _icon_provider is None:
            _icon_provider = QFileIconProvider()
        icon = _icon_provider.icon(QFileInfo(path))
    return icon


def open_attachment(path: str, anchor: QWidget) -> None:
    """Open a file in its default app, or say so if it's gone."""
    if os.path.exists(path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    else:
        QToolTip.showText(QCursor.pos(), f"File not found (moved or renamed?):\n{path}", anchor)


def _two_line_name(name: str, metrics, width: int) -> str:
    """A file name wrapped onto at most two lines of `width` pixels. If it's
    still too long, the second line is elided in the middle so the end of
    the name (and its extension) stays visible.
    """
    if metrics.horizontalAdvance(name) <= width:
        return name
    # Break at the last space/dash/underscore/dot that fits on line one,
    # or mid-word if there isn't one.
    fit = len(name)
    while fit > 1 and metrics.horizontalAdvance(name[:fit]) > width:
        fit -= 1
    cut = max(name.rfind(ch, 0, fit) for ch in " -_.")
    cut = cut + 1 if cut > 0 else fit
    first, rest = name[:cut].rstrip(), name[cut:]
    return f"{first}\n{metrics.elidedText(rest, Qt.ElideMiddle, width)}"


class AttachmentTile(QToolButton):
    """An attached file on a card: its type's icon, with its name below.
    Clicking it opens the file.
    """

    WIDTH = 80

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        missing = not os.path.exists(path)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIcon(file_icon(path))
        self.setIconSize(QSize(32, 32))
        self.setFixedWidth(self.WIDTH)
        # Set the font size here rather than in the style sheet, so
        # fontMetrics() below measures the text at the size it's drawn.
        font = self.font()
        font.setPixelSize(10)
        self.setFont(font)
        name = os.path.basename(path.rstrip(os.sep)) or path
        self.setText(_two_line_name(name, self.fontMetrics(), self.WIDTH - 8))
        self.setToolTip(f"Not found (moved or renamed?):\n{path}" if missing else path)
        self.setStyleSheet(
            f"""
            QToolButton {{
                color: {"#98a2b3" if missing else "#344054"};
                border: none;
                border-radius: 4px;
                padding: 2px;
            }}
            QToolButton:hover {{ background-color: #f2f4f7; }}
            """
        )
        self.clicked.connect(lambda: open_attachment(path, self))


def _style_subtask_label(label: QLabel, checked: bool) -> None:
    """Ticked sub-tasks are struck through and greyed out."""
    if checked:
        label.setStyleSheet("font-size: 12px; color: #98a2b3; text-decoration: line-through;")
    else:
        label.setStyleSheet("font-size: 12px; color: #344054;")


class _DescriptionLabel(QLabel):
    """Description text with clickable links (see open_link). Dragging still
    moves the card, even when the drag starts on a link.
    """

    def __init__(self, text: str, words: list[str] | None = None, parent=None):
        super().__init__(linkify(text, words), parent)
        self.setTextFormat(Qt.RichText)
        self.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.linkActivated.connect(open_link)
        self.setWordWrap(True)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)  # updates the hand cursor over links
        if event.buttons() & Qt.LeftButton:
            # A link label keeps mouse moves for itself; pass them on so the
            # card list sees the drag.
            event.ignore()


class CardWidget(QFrame):
    """Visual representation of one card (a cards row from the database),
    placed inside a QListWidgetItem on the board or shown in a popup.
    """

    subtask_toggled = Signal(int, bool)  # subtask_id, checked

    def __init__(
        self,
        card,
        labels,
        done: bool = False,
        subtasks=None,
        links=None,
        attachments=None,
        highlight: list[str] | None = None,  # search words to mark in the title and description
        parent=None,
    ):
        super().__init__(parent)
        title = card["title"]
        description = card["description"] or ""
        due_date = card["due_date"]
        self.setObjectName("CardWidget")
        self.setFrameShape(QFrame.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # A row of colored, labeled chips along the top of the card (one
        # per label), tall enough to show the label's name -- not just a
        # thin color-only strip. Skipped entirely when there are none.
        if labels:
            chip_bar = QWidget()
            chip_layout = QHBoxLayout(chip_bar)
            chip_layout.setContentsMargins(8, 6, 8, 0)
            chip_layout.setSpacing(4)
            for label in labels:
                text_color = readable_text_color(label["color"])
                chip = QLabel(label["name"])
                chip.setStyleSheet(
                    f"""
                    background-color: {label['color']};
                    color: {text_color};
                    font-size: 11px;
                    font-weight: 600;
                    border-radius: 4px;
                    padding: 3px 8px;
                    """
                )
                chip_layout.addWidget(chip)
            chip_layout.addStretch()
            outer.addWidget(chip_bar)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 8, 10, 8)
        body_layout.setSpacing(4)

        title_label = QLabel(title)
        if highlight:
            title_label.setTextFormat(Qt.RichText)
            title_label.setText(highlighted(title, highlight))
        else:
            title_label.setTextFormat(Qt.PlainText)
        title_label.setObjectName("CardTitle")
        title_label.setWordWrap(True)
        body_layout.addWidget(title_label)

        # The full description, line breaks kept, wrapped to the card's width,
        # with web addresses as links that open in the default browser.
        # The card grows taller to fit it (see CardListWidget.update_item_sizes).
        if description.strip():
            desc_label = _DescriptionLabel(description.strip(), highlight)
            desc_label.setObjectName("CardDescription")
            body_layout.addWidget(desc_label)

        # Checklist: a real checkbox per sub-task, so it can be ticked straight
        # from the board. Ticking only restyles the text (same height), so the
        # card doesn't need to be rebuilt.
        for subtask in subtasks or []:
            row = QHBoxLayout()
            row.setSpacing(6)
            checkbox = QCheckBox()
            checkbox.setChecked(bool(subtask["done"]))
            text_label = QLabel(subtask["text"])
            text_label.setTextFormat(Qt.PlainText)
            text_label.setWordWrap(True)
            _style_subtask_label(text_label, bool(subtask["done"]))
            checkbox.toggled.connect(
                lambda checked, sid=subtask["id"], lbl=text_label: self._on_subtask_toggled(sid, lbl, checked)
            )
            row.addWidget(checkbox, 0, Qt.AlignTop)
            row.addWidget(text_label, 1)
            body_layout.addLayout(row)

        if card["action_start"]:
            # Finished work (a done column) needs no countdown.
            dates = (format_action_dates if done else action_dates_with_countdown)(card["action_start"], card["action_end"])
            action_label = QLabel(f"Action: {dates}")
            action_label.setWordWrap(True)  # a date range plus countdown can outgrow a narrow card
            action_color = "#667085" if done else "#1570ef"
            action_label.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {action_color};")
            body_layout.addWidget(action_label)

        if due_date:
            text, color = _due_date_style(due_date, done)
            due_label = QLabel(text)
            due_label.setWordWrap(True)
            due_label.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {color};")
            body_layout.addWidget(due_label)

        # Links to other cards. "Blocked by" only lists blockers that aren't
        # done yet, so the line disappears once the card is free to start.
        blockers = [] if done else unfinished_blockers(links)
        link_lines = [
            ("Blocked by", blockers, "#d92d20"),
            ("Blocks", [l for l in links or [] if l["kind"] == "blocks"], "#667085"),
            ("Related", [l for l in links or [] if l["kind"] == "relates"], "#667085"),
        ]
        for heading, line_links, color in link_lines:
            if not line_links:
                continue
            link_label = QLabel(f"{heading}: " + ", ".join(l["other_title"] for l in line_links))
            link_label.setTextFormat(Qt.PlainText)
            link_label.setWordWrap(True)
            link_label.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {color};")
            body_layout.addWidget(link_label)

        # Attached files, as icon tiles three to a row (the narrowest card fits
        # three). Clicking one opens it.
        if attachments:
            tiles = QGridLayout()
            tiles.setContentsMargins(0, 4, 0, 0)
            tiles.setSpacing(4)
            for i, attachment in enumerate(attachments):
                tiles.addWidget(AttachmentTile(attachment["path"]), i // 3, i % 3, Qt.AlignLeft | Qt.AlignTop)
            tiles.setColumnStretch(3, 1)
            body_layout.addLayout(tiles)

        outer.addWidget(body)

        self.setStyleSheet(
            """
            #CardWidget {
                background-color: #ffffff;
                border: 1px solid #d0d5dd;
                border-radius: 6px;
            }
            #CardTitle {
                font-weight: 600;
                font-size: 13px;
                color: #1d2939;
            }
            #CardDescription {
                font-size: 11px;
                color: #667085;
            }
            QCheckBox::indicator {
                width: 13px;
                height: 13px;
                border: 1px solid #98a2b3;
                border-radius: 3px;
                background-color: #ffffff;
            }
            QCheckBox::indicator:hover {
                border-color: #1570ef;
            }
            QCheckBox::indicator:checked {
                border-color: #1570ef;
                background-color: #1570ef;
                image: url(%s);
            }
            """ % CHECK_ICON
        )

    def _on_subtask_toggled(self, subtask_id: int, label: QLabel, checked: bool):
        _style_subtask_label(label, checked)
        self.subtask_toggled.emit(subtask_id, checked)


NO_LABEL_LANE = "No label"


def board_lanes(
    board_id: int, label_filter: list[int] | None = None, search: list[str] | None = None
) -> list[tuple[int | None, str, str | None]]:
    """The swimlanes for a board, as (label_id, name, color): one per label
    that's the lane of at least one (visible) card, alphabetically, then
    (label_id None) "No label" if any card has no labels.

    A card's lane is its first label alphabetically -- the same label that
    colours its Gantt bar -- so each card is in exactly one lane.
    """
    cards = db.filter_cards_by_search(db.get_board_cards(board_id, label_filter or None), search or [])
    labels_by_card = db.get_labels_for_cards([card["id"] for card in cards])
    lanes, unlabelled = {}, False
    for card in cards:
        labels = labels_by_card.get(card["id"])
        if labels:
            lanes[labels[0]["id"]] = (labels[0]["id"], labels[0]["name"], labels[0]["color"])
        else:
            unlabelled = True
    result = sorted(lanes.values(), key=lambda lane: lane[1].lower())
    return result + ([(None, NO_LABEL_LANE, None)] if unlabelled else [])


LANE_LINE_WIDTH = 3


def lane_line_color(palette: QPalette) -> QColor:
    """The colour of the lines between swimlanes: the theme's text colour,
    softened a little, so it stands out in light and dark themes alike.
    """
    color = QColor(palette.color(QPalette.WindowText))
    color.setAlpha(150)
    return color


class _LaneDivider(QWidget):
    """The line across a column where a new swimlane starts. The same line
    continues through the gaps between columns (see BoardArea), so it reads
    as one line across the board.
    """

    HEIGHT = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        y = (self.height() - LANE_LINE_WIDTH) // 2
        painter.fillRect(0, y, self.width(), LANE_LINE_WIDTH, lane_line_color(self.palette()))


class CardListWidget(QListWidget):
    """The drag-and-drop-enabled list of cards for a single column."""

    card_dropped = Signal(int, int, int, int)  # card_id, source_col, target_col, before_card_id (-1 = end)
    delete_requested = Signal(int)  # card_id
    edit_requested = Signal(int)  # card_id
    add_requested = Signal()  # "Add card..." from the right-click menu
    files_attached = Signal(int, list)  # card_id, [absolute file paths]
    sizes_updated = Signal()  # item heights were recalculated (e.g. on resize)

    def __init__(self, column_id: int, parent=None):
        super().__init__(parent)
        self.column_id = column_id
        self._apply_column_color()
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        # Scroll by pixels, not whole rows: smoother past tall cards, and with
        # swimlanes on, columns can then scroll in step (see MainWindow).
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(20)
        # We handle drag/drop entirely ourselves (see class docstring).
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setAcceptDrops(True)
        self.setSpacing(6)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._drag_start_pos = None

    # --- colour ---

    def _apply_column_color(self):
        """The area behind the cards: a soft grey in a light theme, so white
        cards stand out from it; the usual dark base colour in a dark one.
        """
        palette = self.palette()
        window = palette.color(QPalette.Window)
        color = window.darker(108) if window.lightness() > 128 else palette.color(QPalette.Base)
        viewport_palette = self.viewport().palette()
        viewport_palette.setColor(QPalette.Base, color)
        self.viewport().setPalette(viewport_palette)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange:
            self._apply_column_color()  # the theme changed

    # --- sizing ---

    def update_item_sizes(self):
        """Give each card the height it needs at the list's current width.

        A card's plain sizeHint() is computed before it knows how wide it
        will be, so word-wrapped titles and descriptions come out too tall
        or too short. Asking for the height at the real width fixes that.
        """
        width = self.viewport().width() - 2 * self.spacing()
        if width <= 0:
            return
        for row in range(self.count()):
            item = self.item(row)
            widget = self.itemWidget(item)
            if widget is None:
                continue
            if widget.hasHeightForWidth():
                height = widget.heightForWidth(width)
            else:
                height = widget.sizeHint().height()
            item.setSizeHint(QSize(width, height))
        self.sizes_updated.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_item_sizes()

    def row_rect(self, item: QListWidgetItem):
        """Where a row sits in the whole list (not just the visible part), so
        the answer doesn't depend on how far the list is scrolled.
        """
        return self.rectForIndex(self.indexFromItem(item))

    def card_item_at(self, pos) -> QListWidgetItem | None:
        """The card under pos, or None -- including over a swimlane header
        or spacer, which are list rows too but not cards.
        """
        item = self.itemAt(pos)
        return item if item is not None and item.data(Qt.UserRole) is not None else None

    # --- drag source ---

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._drag_start_pos is None:
            super().mouseMoveEvent(event)
            return

        moved = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
        if moved < 10:
            super().mouseMoveEvent(event)
            return

        item = self.card_item_at(self._drag_start_pos)
        if item is None:
            return

        card_id = item.data(Qt.UserRole)
        mime_data = QMimeData()
        mime_data.setData(CARD_MIME_TYPE, str(card_id).encode())
        mime_data.setText(str(self.column_id))  # source column id

        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.MoveAction)

    # --- drop target ---

    def _accept_file_drop(self, event) -> bool:
        """Accept files dragged over a card (not over empty space), as a
        link/copy -- never a move, which would make the file manager delete
        the original. Highlights the card they'd attach to.
        """
        item = self.card_item_at(event.position().toPoint())
        if item is None or not local_paths_from_mime(event.mimeData()):
            self.clearSelection()
            event.ignore()
            return False
        self.setCurrentItem(item)
        action = Qt.LinkAction if event.possibleActions() & Qt.LinkAction else Qt.CopyAction
        event.setDropAction(action)
        event.accept()
        return True

    def _board_area(self):
        widget = self.parentWidget()
        while widget is not None and not isinstance(widget, BoardArea):
            widget = widget.parentWidget()
        return widget

    def _forward_column_drag(self, event) -> bool:
        """Column drags belong to the board, not this list: pass them on."""
        area = self._board_area()
        return area is not None and area.handle_column_drag(event, self.viewport())

    def dragEnterEvent(self, event):
        if self._forward_column_drag(event):
            return
        if event.mimeData().hasFormat(CARD_MIME_TYPE):
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            # Accept for now so dragMoveEvent runs; it decides per card.
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._forward_column_drag(event):
            return
        if event.mimeData().hasFormat(CARD_MIME_TYPE):
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            self._accept_file_drop(event)

    def dragLeaveEvent(self, event):
        self.clearSelection()
        area = self._board_area()
        if area is not None:
            area._update_marker(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if self._forward_column_drag(event):
            return
        mime_data = event.mimeData()
        if not mime_data.hasFormat(CARD_MIME_TYPE):
            if mime_data.hasUrls() and self._accept_file_drop(event):
                card_id = self.card_item_at(event.position().toPoint()).data(Qt.UserRole)
                self.files_attached.emit(card_id, local_paths_from_mime(mime_data))
            return

        card_id = int(bytes(mime_data.data(CARD_MIME_TYPE)).decode())
        source_column_id = int(mime_data.text())

        # Identify the drop spot by the card it lands in front of, not by a row
        # number: rows only count the cards currently visible, which differs
        # from the full column when a label filter is active. Comparing
        # against each card's midpoint also handles drops in the gaps
        # between cards and on the lower half of a card.
        drop_y = event.position().toPoint().y()
        before_card_id = -1  # -1 = dropped after the last card
        for row in range(self.count()):
            item = self.item(row)
            if item.data(Qt.UserRole) is None:
                continue  # a swimlane header or spacer, not a card
            if drop_y < self.visualItemRect(item).center().y():
                before_card_id = item.data(Qt.UserRole)
                break

        event.acceptProposedAction()
        self.card_dropped.emit(card_id, source_column_id, self.column_id, before_card_id)

    def keyPressEvent(self, event):
        # Ctrl+V with a card selected attaches files copied in a file manager.
        current = self.currentItem()
        if event.matches(QKeySequence.Paste) and current is not None and current.data(Qt.UserRole) is not None:
            paths = local_paths_from_mime(QApplication.clipboard().mimeData())
            if paths:
                self.files_attached.emit(self.currentItem().data(Qt.UserRole), paths)
                return
        super().keyPressEvent(event)

    def _choose_attachments(self, card_id: int):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        if paths:
            self.files_attached.emit(card_id, paths)

    def contextMenuEvent(self, event):
        self.context_menu(event.pos()).exec(event.globalPos())

    def context_menu(self, pos) -> QMenu:
        """The right-click menu for a spot in the list: "Add card..." anywhere,
        plus the card's own actions when it's on a card.
        """
        menu = QMenu(self)
        menu.addAction("Add card...").triggered.connect(self.add_requested.emit)
        item = self.card_item_at(pos)
        if item is None:  # empty space in the column: adding is all there is to do
            return menu
        card_id = item.data(Qt.UserRole)
        menu.addSeparator()

        edit_action = QAction("Edit card", self)
        edit_action.triggered.connect(lambda: self.edit_requested.emit(card_id))
        menu.addAction(edit_action)

        attach_action = QAction("Attach files...", self)
        attach_action.triggered.connect(lambda: self._choose_attachments(card_id))
        menu.addAction(attach_action)

        clipboard_paths = local_paths_from_mime(QApplication.clipboard().mimeData())
        paste_action = QAction("Paste file" + ("s" if len(clipboard_paths) > 1 else ""), self)
        paste_action.setEnabled(bool(clipboard_paths))
        paste_action.triggered.connect(lambda: self.files_attached.emit(card_id, clipboard_paths))
        menu.addAction(paste_action)

        delete_action = QAction("Delete card", self)
        delete_action.triggered.connect(lambda: self.delete_requested.emit(card_id))
        menu.addAction(delete_action)
        return menu


class _ColumnHeader(QWidget):
    """A column's title bar. Dragging it moves the whole column (dropped on
    the board, see BoardArea); right-click offers Add card, Move left,
    Move right and Delete column.
    """

    move_requested = Signal(int)  # -1 = left, +1 = right
    add_card_requested = Signal()
    delete_requested = Signal()

    def __init__(self, column_id: int, parent=None):
        super().__init__(parent)
        self.column_id = column_id
        self._drag_start_pos = None
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Drag to move this column")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._drag_start_pos is None:
            return
        if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < 10:
            return
        self._drag_start_pos = None
        mime_data = QMimeData()
        mime_data.setData(COLUMN_MIME_TYPE, str(self.column_id).encode())
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.setPixmap(self.parentWidget().grab().scaledToWidth(160, Qt.SmoothTransformation))
        drag.exec(Qt.MoveAction)

    def mouseReleaseEvent(self, event):
        self._drag_start_pos = None

    def contextMenuEvent(self, event):
        self.context_menu().exec(event.globalPos())

    def context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Add card...").triggered.connect(self.add_card_requested.emit)
        menu.addSeparator()
        menu.addAction("Move left").triggered.connect(lambda: self.move_requested.emit(-1))
        menu.addAction("Move right").triggered.connect(lambda: self.move_requested.emit(1))
        menu.addSeparator()
        menu.addAction("Delete column...").triggered.connect(self.delete_requested.emit)
        return menu



class BackgroundImage:
    """The board's background picture, shared by the Kanban and Gantt views.
    It's drawn to fill a view (cropped to fit, never stretched), and the
    fitted copy is kept until the view's size changes.
    """

    MAX_WIDTH = 3840  # bigger photos are shrunk once, on loading

    def __init__(self):
        # How solid the tint over the picture is, 0 (see-through) to 255
        # (solid): behind each Kanban column, and over the Gantt chart. Set
        # from the Appearance menu's sliders; these are the starting values.
        self.kanban_alpha = 180
        self.gantt_alpha = 215
        self._image: QPixmap | None = None
        self._scaled: QPixmap | None = None  # _image fitted to _scaled_for
        self._scaled_for = None

    def is_set(self) -> bool:
        return self._image is not None

    def load(self, path: str | None) -> bool:
        """Use this image, or none. False if the file isn't a readable image."""
        pixmap = QPixmap(path) if path else QPixmap()
        if path and pixmap.isNull():
            return False
        if pixmap.width() > self.MAX_WIDTH:
            pixmap = pixmap.scaledToWidth(self.MAX_WIDTH, Qt.SmoothTransformation)
        self._image = None if pixmap.isNull() else pixmap
        self._scaled = None
        return True

    def paint(self, painter: QPainter, size: QSize) -> None:
        if self._image is None:
            return
        if self._scaled is None or self._scaled_for != size:
            self._scaled = self._image.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self._scaled_for = size
        painter.drawPixmap(
            (size.width() - self._scaled.width()) // 2,
            (size.height() - self._scaled.height()) // 2,
            self._scaled,
        )


class BoardScrollArea(QScrollArea):
    """The board's sideways-scrolling area, with the background image (if
    any) behind the columns. The image stays put while the columns scroll.
    """

    def __init__(self, background: BackgroundImage, parent=None):
        super().__init__(parent)
        self.background = background

    def paintEvent(self, event):
        if not self.background.is_set():
            super().paintEvent(event)
            return
        self.background.paint(QPainter(self.viewport()), self.viewport().size())


class BoardArea(QWidget):
    """The strip of columns on the board, and the drop target for dragged
    column headers. While one is dragged over it, a blue line marks where
    the column will land.
    """

    column_dropped = Signal(int, int)  # column_id, new index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._marker_x = None
        self._lane_lines: list[int] = []  # y positions of swimlane dividers
        self._lane_clip = (0, 0)  # (top, bottom): only draw them inside the card lists' height

    def set_lane_lines(self, ys: list[int], top: int = 0, bottom: int = 0):
        """Draw the swimlane dividers right across the board, at these y
        positions (in this widget), so the lines in each column join up
        through the gaps between them. Empty list: no lines.
        """
        self._lane_lines, self._lane_clip = ys, (top, bottom)
        self.update()

    def _columns(self) -> list[QWidget]:
        layout = self.layout()
        widgets = (layout.itemAt(i).widget() for i in range(layout.count()))
        return [w for w in widgets if isinstance(w, ColumnWidget)]

    def _drop_index(self, x: int) -> int:
        """Where a column dropped at x goes: before the first column whose
        middle is to the right of x.
        """
        columns = self._columns()
        for index, column in enumerate(columns):
            if x < column.geometry().center().x():
                return index
        return len(columns)

    def _update_marker(self, x: int | None):
        if x is None:
            self._marker_x = None
        else:
            columns = self._columns()
            index = self._drop_index(x)
            gap = self.layout().spacing() // 2
            if index < len(columns):
                self._marker_x = columns[index].geometry().left() - gap
            else:
                self._marker_x = columns[-1].geometry().right() + gap
        self.update()

    def handle_column_drag(self, event, receiver: QWidget | None = None) -> bool:
        """Handle a drag enter/move/drop event carrying a column. Also called
        by each column's card list, which gets the drag first when it's over
        the cards (`receiver` is the widget whose coordinates the event uses).
        Returns False for any other drag.
        """
        if not event.mimeData().hasFormat(COLUMN_MIME_TYPE):
            return False
        x = (receiver or self).mapTo(self, event.position().toPoint()).x() if receiver else event.position().toPoint().x()
        event.acceptProposedAction()
        if event.type() == QEvent.Drop:
            self._update_marker(None)
            column_id = int(bytes(event.mimeData().data(COLUMN_MIME_TYPE)).decode())
            self.column_dropped.emit(column_id, self._drop_index(x))
        else:
            self._update_marker(x)
        return True

    def dragEnterEvent(self, event):
        self.handle_column_drag(event)

    def dragMoveEvent(self, event):
        self.handle_column_drag(event)

    def dragLeaveEvent(self, event):
        self._update_marker(None)

    def dropEvent(self, event):
        self.handle_column_drag(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        top, bottom = self._lane_clip
        for y in self._lane_lines:
            if top <= y <= bottom:
                painter.fillRect(0, y - LANE_LINE_WIDTH // 2, self.width(), LANE_LINE_WIDTH, lane_line_color(self.palette()))
        if self._marker_x is not None:
            painter.fillRect(self._marker_x - 2, 6, 4, self.height() - 12, QColor("#1570ef"))


class ColumnWidget(QWidget):
    """One board column: header (name + count), card list, add-card button."""

    add_card_requested = Signal(int)  # column_id
    edit_card_requested = Signal(int)  # card_id
    card_dropped = Signal(int, int, int, int)  # forwarded from CardListWidget
    delete_requested = Signal(int)  # forwarded from CardListWidget
    files_attached = Signal(int, list)  # forwarded from CardListWidget
    move_requested = Signal(int, int)  # column_id, -1 (left) or +1 (right)
    delete_column_requested = Signal(int)  # column_id
    lanes_changed = Signal()  # swimlane rows rebuilt or resized: lanes need lining up again

    def __init__(
        self,
        column_row,
        label_filter: list[int] | None = None,
        swimlanes: bool = False,
        search_words: list[str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.column_id = column_row["id"]
        self.board_id = column_row["board_id"]
        self.swimlanes = swimlanes
        self.search_words: list[str] = search_words or []  # from the main window's search box; [] = show all
        self.lane_ids: list[int | None] = []  # the lanes shown, in order (when swimlanes is on)
        self._lane_rows = []  # per lane: (header item, [card items], spacer item)
        self.column_name = column_row["name"]
        self.is_done = is_done_column(self.column_name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        header = _ColumnHeader(self.column_id)
        header.move_requested.connect(lambda step: self.move_requested.emit(self.column_id, step))
        header.add_card_requested.connect(lambda: self.add_card_requested.emit(self.column_id))
        header.delete_requested.connect(lambda: self.delete_column_requested.emit(self.column_id))
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        title_label = QLabel(self.column_name)
        title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.count_label = QLabel("0")
        self.count_label.setStyleSheet("color: #667085;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.count_label)
        layout.addWidget(header)

        self.list_widget = CardListWidget(self.column_id)
        self.list_widget.card_dropped.connect(self.card_dropped)
        self.list_widget.delete_requested.connect(self.delete_requested)
        self.list_widget.edit_requested.connect(self.edit_card_requested)
        self.list_widget.add_requested.connect(lambda: self.add_card_requested.emit(self.column_id))
        self.list_widget.files_attached.connect(self.files_attached)
        self.list_widget.sizes_updated.connect(lambda: self.swimlanes and self.lanes_changed.emit())
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.list_widget)

        add_button = QPushButton("+ Add card")
        add_button.clicked.connect(lambda: self.add_card_requested.emit(self.column_id))
        layout.addWidget(add_button)

        self.setMinimumWidth(260)
        self.setMaximumWidth(340)

        self._active_label_filter: list[int] | None = label_filter or None
        self._panel = False
        self._panel_alpha = 180
        self.refresh()

    def set_panel(self, on: bool, alpha: int = 180):
        """Over a background image, draw the column on a see-through panel
        (alpha: 0 = invisible, 255 = solid), and let the panel show through
        the card list, so its heading and cards stay readable.
        """
        self._panel = on
        self._panel_alpha = alpha
        self.list_widget.viewport().setAutoFillBackground(not on)
        self.list_widget.update()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._panel:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            color = QColor(self.palette().window().color())
            color.setAlpha(self._panel_alpha)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(self.rect(), 8, 8)

    def refresh(self, label_filter: list[int] | None = None):
        """Reload cards from the database. If label_filter is omitted, the
        previously-applied filter (if any) is kept, so callers that just
        want to reflect a data change don't need to know the current
        filter state.
        """
        if label_filter is not None:
            self._active_label_filter = label_filter or None

        self.list_widget.clear()
        cards = db.filter_cards_by_search(db.get_cards(self.column_id, self._active_label_filter), self.search_words)
        card_ids = [card["id"] for card in cards]
        labels_by_card = db.get_labels_for_cards(card_ids)
        subtasks_by_card = db.get_subtasks_for_cards(card_ids)
        links_by_card = db.get_links_for_cards(card_ids)
        attachments_by_card = db.get_attachments_for_cards(card_ids)

        def add_card(card) -> QListWidgetItem:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, card["id"])
            widget = CardWidget(
                card,
                labels_by_card.get(card["id"], []),
                self.is_done,
                subtasks_by_card.get(card["id"], []),
                links_by_card.get(card["id"], []),
                attachments_by_card.get(card["id"], []),
                self.search_words,
            )
            widget.subtask_toggled.connect(db.set_subtask_done)
            item.setSizeHint(widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)
            return item

        def add_row(widget: QWidget | None = None, height: int = 1) -> QListWidgetItem:
            """A non-card row: a lane divider, or an empty filler row (at least
            1px tall: rows with no height don't report a reliable position).
            """
            item = QListWidgetItem()
            item.setFlags(Qt.NoItemFlags)  # can't be selected, dragged or dropped on
            item.setSizeHint(QSize(1, widget.sizeHint().height() if widget else height))
            self.list_widget.addItem(item)
            if widget is not None:
                self.list_widget.setItemWidget(item, widget)
            return item

        self._lane_rows = []
        if self.swimlanes:
            # Every lane appears in every column (even where it's empty here),
            # so lanes can line up across the board; see set_lane_heights().
            lanes = board_lanes(self.board_id, self._active_label_filter, self.search_words)
            self.lane_ids = [lane_id for lane_id, _, _ in lanes]
            by_lane = {lane_id: [] for lane_id in self.lane_ids}
            for card in cards:
                labels = labels_by_card.get(card["id"])
                lane_id = labels[0]["id"] if labels else None
                by_lane.setdefault(lane_id, []).append(card)
            for index, (lane_id, _, _) in enumerate(lanes):
                # Each lane starts with a divider line -- except the first,
                # which gets an invisible zero-height row instead, so every
                # lane has a start row to measure from.
                start = add_row(_LaneDivider()) if index else add_row()
                card_items = [add_card(card) for card in by_lane[lane_id]]
                self._lane_rows.append((start, card_items, add_row()))
        else:
            self.lane_ids = []
            for card in cards:
                add_card(card)
        self.list_widget.update_item_sizes()
        self.count_label.setText(str(len(cards)))
        if self.swimlanes:
            self.lanes_changed.emit()

    def set_swimlanes(self, on: bool):
        self.swimlanes = on
        self.refresh()

    def lane_divider_ys(self) -> list[int]:
        """Where the lines between lanes are, as y positions in the list's
        viewport (one per lane after the first).
        """
        lw = self.list_widget
        scroll = lw.verticalScrollBar().value()
        return [lw.row_rect(start).center().y() - scroll for start, _, _ in self._lane_rows[1:]]

    def lane_content_heights(self) -> list[int]:
        """How tall each lane is in this column without its spacer, in lane
        order: from the top of its start row to the top of its spacer, measured
        from the laid-out rows (so the list's gaps between rows count too).
        """
        lw = self.list_widget
        lw.doItemsLayout()
        return [lw.row_rect(spacer).top() - lw.row_rect(start).top() for start, _, spacer in self._lane_rows]

    def set_lane_heights(self, heights: list[int]):
        """Pad each lane out to the given height with its spacer row, so lanes
        line up with the same lanes in the other columns.
        """
        for (_, _, spacer), own, target in zip(self._lane_rows, self.lane_content_heights(), heights):
            spacer.setSizeHint(QSize(1, 1 + max(0, target - own)))  # 1px minimum, as in add_row()
        self.list_widget.doItemsLayout()

    def _on_item_double_clicked(self, item):
        if item.data(Qt.UserRole) is not None:  # not a swimlane header
            self.edit_card_requested.emit(item.data(Qt.UserRole))

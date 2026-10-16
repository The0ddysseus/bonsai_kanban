"""
Gantt view: every dated card on the board as a horizontal bar on a
day-by-day timeline, plus the popup shown when a bar is clicked.

Which days a card's bar covers:
    - it has an action date  -> that day, or the action date range
    - otherwise a due date   -> from the day it was created to its due date
    - neither                -> it isn't shown
A card with both follows its action date, and a small diamond marks the
due date on the same row.

Blocking links ("X must be done before Y starts") are drawn as arrows from
the end of X's bar to the start of Y's. An arrow turns red when Y is
scheduled to start before X has finished.

Scrolling: drag the chart with the mouse to move around, or use the
mouse wheel: the wheel alone moves through time, Shift + wheel scrolls the
rows up and down, and Ctrl + wheel zooms (wider or narrower days -- rows
keep their height). The zoom is remembered between runs.

Bars take the colour of the card's first label (alphabetically); any
further labels share a thin band along the bar's bottom edge. Cards in a done column are drawn faded.

The chart is painted by hand on a QAbstractScrollArea rather than built
from widgets: hundreds of bars stay cheap, and the date header can stay
pinned at the top while the rows scroll underneath it.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QToolTip,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
)
from PySide6.QtCore import Qt, QRectF, QPointF, QEvent, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QPainterPath, QPolygonF, QPen, QFont, QCursor

import database as db
from widgets import CardWidget, is_done_column, readable_text_color, format_action_dates, unfinished_blockers

NO_LABEL_COLOR = "#98a2b3"
OVERDUE_COLOR = "#d92d20"
TODAY_COLOR = "#1570ef"


@dataclass
class GanttBar:
    card_id: int
    title: str
    start: date
    end: date  # inclusive
    colors: list[str]  # label colours; the first fills the bar
    done: bool
    due: date | None  # drawn as a marker when the bar follows the action date
    tooltip: str
    from_action: bool  # True: the bar is the action date(s); False: created -> due date


def card_span(card) -> tuple[date, date] | None:
    """The (start, end) days a card's bar covers, inclusive, or None if the
    card has no dates.
    """
    if card["action_start"]:
        start = date.fromisoformat(card["action_start"])
        end = date.fromisoformat(card["action_end"] or card["action_start"])
        return start, max(start, end)
    if card["due_date"]:
        due = date.fromisoformat(card["due_date"])
        created = datetime.fromisoformat(card["created_at"]).astimezone().date()
        return min(created, due), due
    return None


class GanttView(QAbstractScrollArea):
    card_clicked = Signal(int)  # card_id

    DAY_WIDTH = 32  # default zoom: pixels per day
    MIN_DAY_WIDTH = 4  # zoomed right out: about a year across the screen
    MAX_DAY_WIDTH = 400  # zoomed right in: a few days across the screen
    ZOOM_STEP = 1.15  # per wheel notch
    ZOOM_SETTING = "gantt_day_width"
    ROW_HEIGHT = 30
    BAR_HEIGHT = 20
    MONTH_ROW_HEIGHT = 20
    DAY_ROW_HEIGHT = 30
    HEADER_HEIGHT = MONTH_ROW_HEIGHT + DAY_ROW_HEIGHT
    DAYS_BEFORE = 7  # empty days shown before the earliest bar (or today)
    DAYS_AFTER = 21  # ...and after the latest

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bars: list[GanttBar] = []
        self.block_edges: list[tuple[int, int]] = []  # (blocker_id, blocked_id), both shown as bars
        self.first_day = date.today()
        self.num_days = 0
        self._has_scrolled_to_today = False
        try:
            saved = float(db.get_setting(self.ZOOM_SETTING) or self.DAY_WIDTH)
        except ValueError:
            saved = self.DAY_WIDTH
        self.day_width = min(self.MAX_DAY_WIDTH, max(self.MIN_DAY_WIDTH, saved))
        # Save the zoom once the wheel stops, not on every notch.
        self._save_zoom_timer = QTimer(self)
        self._save_zoom_timer.setSingleShot(True)
        self._save_zoom_timer.setInterval(1000)
        self._save_zoom_timer.timeout.connect(lambda: db.set_setting(self.ZOOM_SETTING, f"{self.day_width:.2f}"))
        self.background = None  # a widgets.BackgroundImage, set by the main window
        self.search_words: list[str] = []  # set by the main window's search box; [] = show all
        self.viewport().setMouseTracking(True)
        self.viewport().setCursor(Qt.OpenHandCursor)
        self._pan_origin = None  # (press position, h scroll, v scroll) while the button is down
        self._panning = False
        self._resize = None  # (bar, "start" or "end", original start, original end) while dragging a handle
        self._board_id, self._label_filter = None, None
        self.setFocusPolicy(Qt.ClickFocus)  # so Esc can cancel a handle drag

    # --- data ---

    def reload(self, board_id: int, label_filter: list[int] | None = None):
        """Rebuild the bars from the database, keeping the same date at the
        left edge of the view (or starting near today the first time).
        """
        left_edge_day = self._day_at_x(0) if self.num_days else None
        self._board_id, self._label_filter = board_id, label_filter

        cards = db.filter_cards_by_search(db.get_board_cards(board_id, label_filter or None), self.search_words)
        card_ids = [card["id"] for card in cards]
        labels_by_card = db.get_labels_for_cards(card_ids)
        links_by_card = db.get_links_for_cards(card_ids)
        self.bars = []
        for card in cards:
            span = card_span(card)
            if span is None:
                continue
            labels = labels_by_card.get(card["id"], [])
            self.bars.append(
                GanttBar(
                    card_id=card["id"],
                    title=card["title"],
                    start=span[0],
                    end=span[1],
                    colors=[label["color"] for label in labels] or [NO_LABEL_COLOR],
                    done=is_done_column(card["column_name"]),
                    due=date.fromisoformat(card["due_date"])
                    if card["action_start"] and card["due_date"]
                    else None,
                    tooltip=self._tooltip(card, labels, links_by_card.get(card["id"], [])),
                    from_action=bool(card["action_start"]),
                )
            )
        self.bars.sort(key=lambda bar: (bar.start, bar.end, bar.title.lower()))
        shown = {bar.card_id for bar in self.bars}
        self.block_edges = [
            (card_id, link["other_id"])
            for card_id, links in links_by_card.items()
            for link in links
            if link["kind"] == "blocks" and card_id in shown and link["other_id"] in shown
        ]

        today = date.today()
        earliest = min([bar.start for bar in self.bars] + [bar.due for bar in self.bars if bar.due] + [today])
        latest = max([bar.end for bar in self.bars] + [bar.due for bar in self.bars if bar.due] + [today])
        self.first_day = earliest - timedelta(days=self.DAYS_BEFORE)
        self.num_days = (latest - self.first_day).days + 1 + self.DAYS_AFTER
        self._update_scrollbars()

        if left_edge_day is not None and self._has_scrolled_to_today:
            self.horizontalScrollBar().setValue(round((left_edge_day - self.first_day).days * self.day_width))
        else:
            self.scroll_to_today()
            self._has_scrolled_to_today = True
        self.viewport().update()

    @staticmethod
    def _tooltip(card, labels, links) -> str:
        lines = [card["title"], f"Column: {card['column_name']}"]
        if card["action_start"]:
            lines.append(f"Action: {format_action_dates(card['action_start'], card['action_end'])}")
        if card["due_date"]:
            lines.append(f"Due: {format_action_dates(card['due_date'], None)}")
        if not card["action_start"] and card["due_date"]:
            created = datetime.fromisoformat(card["created_at"]).astimezone().date()
            lines.append(f"Created: {format_action_dates(created.isoformat(), None)}")
        if labels:
            lines.append("Labels: " + ", ".join(label["name"] for label in labels))
        blockers = [] if is_done_column(card["column_name"]) else unfinished_blockers(links)
        if blockers:
            lines.append("Blocked by: " + ", ".join(link["other_title"] for link in blockers))
        for kind, heading in (("blocks", "Blocks"), ("relates", "Related")):
            titles = [link["other_title"] for link in links if link["kind"] == kind]
            if titles:
                lines.append(f"{heading}: " + ", ".join(titles))
        return "\n".join(lines)

    def scroll_to_today(self):
        days_from_start = (date.today() - self.first_day).days
        self.horizontalScrollBar().setValue(round((days_from_start - 3) * self.day_width))

    # --- geometry ---

    def _total_days(self) -> int:
        """Days on the timeline: the cards' range, or more when zoomed out so
        far that it wouldn't fill the view.
        """
        return max(self.num_days, int(self.viewport().width() / self.day_width) + 2)

    def _update_scrollbars(self):
        viewport = self.viewport()
        content_width = round(self._total_days() * self.day_width)
        content_height = self.HEADER_HEIGHT + len(self.bars) * self.ROW_HEIGHT
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        hbar.setRange(0, max(0, content_width - viewport.width()))
        hbar.setPageStep(viewport.width())
        hbar.setSingleStep(max(1, round(self.day_width)))
        vbar.setRange(0, max(0, content_height - viewport.height()))
        vbar.setPageStep(viewport.height())
        vbar.setSingleStep(self.ROW_HEIGHT)

    def _x_for_day(self, day: date) -> float:
        return (day - self.first_day).days * self.day_width - self.horizontalScrollBar().value()

    def _day_at_x(self, x: float) -> date:
        return self.first_day + timedelta(days=int((x + self.horizontalScrollBar().value()) // self.day_width))

    def _row_top(self, row: int) -> float:
        return self.HEADER_HEIGHT + row * self.ROW_HEIGHT - self.verticalScrollBar().value()

    def _bar_rect(self, row: int, bar: GanttBar) -> QRectF:
        days = (bar.end - bar.start).days + 1
        inset = min(2.0, self.day_width / 4)  # gap between neighbouring bars
        return QRectF(
            self._x_for_day(bar.start) + inset,
            self._row_top(row) + (self.ROW_HEIGHT - self.BAR_HEIGHT) / 2,
            max(2.0, days * self.day_width - 2 * inset),
            self.BAR_HEIGHT,
        )

    def _title_rect(self, bar_rect: QRectF, title: str) -> tuple[QRectF, bool]:
        """Where the bar's title goes: inside the bar when it fits, else just
        to its right. Returns (rect, inside).
        """
        text_width = self.fontMetrics().horizontalAdvance(title)
        if text_width + 20 <= bar_rect.width():  # room for the end grips too
            return bar_rect.adjusted(10, 0, -10, 0), True
        return QRectF(bar_rect.right() + 6, bar_rect.top(), text_width + 2, bar_rect.height()), False

    def _visible_rows(self) -> range:
        first = max(0, self.verticalScrollBar().value() // self.ROW_HEIGHT)
        last = min(len(self.bars), first + self.viewport().height() // self.ROW_HEIGHT + 2)
        return range(first, last)

    def bar_at(self, pos) -> GanttBar | None:
        if pos.y() < self.HEADER_HEIGHT:
            return None
        for row in self._visible_rows():
            bar = self.bars[row]
            bar_rect = self._bar_rect(row, bar)
            title_rect, inside = self._title_rect(bar_rect, bar.title)
            if bar_rect.contains(pos) or (not inside and title_rect.contains(pos)):
                return bar
        return None

    # --- events ---

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scrollbars()

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def wheelEvent(self, event):
        # angleDelta is 120 per wheel notch. Some setups report Shift + wheel
        # (and sideways touchpad swipes) as x rather than y, so use whichever
        # is there.
        delta = event.angleDelta()
        notches = (delta.y() or delta.x()) / 120
        event.accept()
        if event.modifiers() & Qt.ControlModifier:
            self.zoom(self.ZOOM_STEP ** notches, event.position().x())
        elif event.modifiers() & Qt.ShiftModifier:
            vbar = self.verticalScrollBar()
            vbar.setValue(vbar.value() - round(notches * 3 * self.ROW_HEIGHT))
        else:
            # An eighth of the view per notch, so it feels the same at any zoom.
            hbar = self.horizontalScrollBar()
            hbar.setValue(hbar.value() - round(notches * self.viewport().width() / 8))

    def zoom(self, factor: float, anchor_x: float):
        """Widen or narrow the days, keeping the moment under anchor_x (a
        viewport x position, usually the mouse) in the same place.
        """
        new_width = min(self.MAX_DAY_WIDTH, max(self.MIN_DAY_WIDTH, self.day_width * factor))
        if new_width == self.day_width:
            return
        days_from_start = (self.horizontalScrollBar().value() + anchor_x) / self.day_width
        self.day_width = new_width
        self._update_scrollbars()
        self.horizontalScrollBar().setValue(round(days_from_start * new_width - anchor_x))
        self.viewport().update()
        self._save_zoom_timer.start()

    # Grab-and-drag: pressing anywhere (on a bar too) and moving pans the
    # chart. A press that doesn't move further than this is a click instead.
    PAN_THRESHOLD = 6

    def _handle_at(self, pos) -> tuple[GanttBar, str] | None:
        """(bar, "start" or "end") if pos is on one of a bar's resize handles:
        a strip just inside (and a little outside) each end. Bars that run
        from creation to due date only have an end handle.
        """
        if pos.y() < self.HEADER_HEIGHT:
            return None
        for row in self._visible_rows():
            bar = self.bars[row]
            rect = self._bar_rect(row, bar)
            if not rect.top() <= pos.y() <= rect.bottom():
                continue
            grab = min(8.0, rect.width() / 3)
            if rect.right() - grab <= pos.x() <= rect.right() + 3:
                return bar, "end"
            if bar.from_action and rect.left() - 3 <= pos.x() <= rect.left() + grab:
                return bar, "start"
        return None

    def _resize_to(self, x: float):
        """Move the dragged handle to the day boundary nearest x."""
        bar, edge, _, _ = self._resize
        boundary = self.first_day + timedelta(days=round((x + self.horizontalScrollBar().value()) / self.day_width))
        if edge == "start":
            bar.start = min(boundary, bar.end)
        else:
            bar.end = max(boundary - timedelta(days=1), bar.start)
        label = "Action" if bar.from_action else "Due"
        dates = format_action_dates(bar.start.isoformat(), bar.end.isoformat()) if bar.from_action else f"{bar.end:%a} {bar.end.day} {bar.end:%b}"
        QToolTip.showText(QCursor.pos(), f"{label}: {dates}", self.viewport())
        self.viewport().update()

    def _finish_resize(self, keep: bool):
        """Save the dragged dates (keep=True) or put the bar back as it was."""
        bar, _, old_start, old_end = self._resize
        self._resize = None
        QToolTip.hideText()
        if not keep or (bar.start, bar.end) == (old_start, old_end):
            bar.start, bar.end = old_start, old_end
            self.viewport().update()
            return
        if bar.from_action:
            db.set_action_dates(bar.card_id, bar.start.isoformat(), bar.end.isoformat())
        else:
            db.set_due_date(bar.card_id, bar.end.isoformat())
        self.reload(self._board_id, self._label_filter)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._resize is not None:
            self._finish_resize(keep=False)
            return
        super().keyPressEvent(event)

    def _update_cursor(self, pos):
        if self._pan_origin is not None and self._panning:
            cursor = Qt.ClosedHandCursor
        elif self._resize is not None or self._handle_at(pos) is not None:
            cursor = Qt.SizeHorCursor
        elif self.bar_at(pos) is not None:
            cursor = Qt.PointingHandCursor
        else:
            cursor = Qt.OpenHandCursor
        self.viewport().setCursor(cursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self._handle_at(event.position())
            if handle is not None:
                bar, edge = handle
                self._resize = (bar, edge, bar.start, bar.end)
                self._resize_origin = event.position()
                self._resize_moved = False
                event.accept()
                return
            # Where the press was, and the scroll position at that moment.
            self._pan_origin = (
                event.position(),
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self._panning = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize is not None and event.buttons() & Qt.LeftButton:
            # Ignore the first few pixels, so a click on a handle stays a click.
            if (event.position() - self._resize_origin).manhattanLength() >= self.PAN_THRESHOLD:
                self._resize_moved = True
            if self._resize_moved:
                self._resize_to(event.position().x())
            event.accept()
            return
        if self._pan_origin is not None and event.buttons() & Qt.LeftButton:
            start, h_start, v_start = self._pan_origin
            moved = event.position() - start
            if not self._panning and moved.manhattanLength() >= self.PAN_THRESHOLD:
                self._panning = True
                QToolTip.hideText()
            if self._panning:
                # The chart follows the mouse, like dragging a sheet of paper.
                self.horizontalScrollBar().setValue(round(h_start - moved.x()))
                self.verticalScrollBar().setValue(round(v_start - moved.y()))
            self._update_cursor(event.position())
            event.accept()
            return
        self._update_cursor(event.position())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._resize is not None:
            bar = self._resize[0]
            clicked = not self._resize_moved
            self._finish_resize(keep=True)
            self._update_cursor(event.position())
            if clicked:
                self.card_clicked.emit(bar.card_id)  # just a click on the end of the bar
            return
        if event.button() == Qt.LeftButton and self._pan_origin is not None:
            was_panning = self._panning
            self._pan_origin = None
            self._panning = False
            self._update_cursor(event.position())
            if not was_panning:
                bar = self.bar_at(event.position())
                if bar is not None:
                    self.card_clicked.emit(bar.card_id)
            return
        super().mouseReleaseEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.ToolTip:
            if self._panning or self._resize is not None:
                return True  # no tooltips mid-drag
            bar = self.bar_at(QPointF(event.pos()))
            if bar is not None:
                QToolTip.showText(event.globalPos(), bar.tooltip, self.viewport())
            else:
                QToolTip.hideText()
            return True
        return super().viewportEvent(event)

    # --- painting ---

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)
        palette = self.palette()
        width, height = self.viewport().width(), self.viewport().height()
        text_color = palette.text().color()

        if self.background is not None and self.background.is_set():
            # The picture, tinted like the board's columns so the bars and
            # day lines stay readable on top of it.
            self.background.paint(painter, self.viewport().size())
            tint = QColor(palette.base().color())
            tint.setAlpha(self.background.gantt_alpha)
            painter.fillRect(0, 0, width, height, tint)
        else:
            painter.fillRect(0, 0, width, height, palette.base())

        if not self.bars:
            painter.setPen(text_color)
            painter.drawText(
                QRectF(0, 0, width, height),
                Qt.AlignCenter | Qt.TextWordWrap,
                "No cards with dates to show.\n\n"
                "Give a card a due date or an action date and it will appear here.",
            )
            return

        def faint(alpha: int) -> QColor:
            color = QColor(text_color)
            color.setAlpha(alpha)
            return color

        hscroll = self.horizontalScrollBar().value()
        first_visible = max(0, int(hscroll // self.day_width))
        last_visible = min(self._total_days(), int((hscroll + width) // self.day_width) + 1)
        visible_days = [self.first_day + timedelta(days=d) for d in range(first_visible, last_visible)]
        today = date.today()

        # ---- body: row stripes, weekends, day lines, today, bars
        painter.save()
        painter.setClipRect(0, self.HEADER_HEIGHT, width, height - self.HEADER_HEIGHT)
        for row in self._visible_rows():
            if row % 2:
                painter.fillRect(QRectF(0, self._row_top(row), width, self.ROW_HEIGHT), faint(8))
        for day in visible_days:
            x = self._x_for_day(day)
            column = QRectF(x, self.HEADER_HEIGHT, self.day_width, height)
            if day == today:
                today_fill = QColor(TODAY_COLOR)
                today_fill.setAlpha(40)
                painter.fillRect(column, today_fill)
            elif day.weekday() >= 5:
                painter.fillRect(column, faint(14))
            # A line between days -- or, zoomed far out, only between weeks.
            if day.day == 1 or self.day_width >= 8 or day.weekday() == 0:
                painter.setPen(QPen(faint(60 if day.day == 1 else 18), 1))
                painter.drawLine(QPointF(x, self.HEADER_HEIGHT), QPointF(x, height))
        self._paint_dependency_arrows(painter, faint(150))
        for row in self._visible_rows():
            self._paint_bar(painter, row, self.bars[row], text_color, today)
        painter.restore()

        # ---- header: month names, then weekday + day number (both adapting
        # to the zoom: "Monday" when there's room, down to just Mondays' dates)
        painter.fillRect(QRectF(0, 0, width, self.HEADER_HEIGHT), palette.window())
        bold = QFont(self.font())
        bold.setBold(True)
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, self.font().pointSizeF() * 0.8))

        month_starts = [d for d in visible_days if d.day == 1 or d == visible_days[0]]
        for i, month_start in enumerate(month_starts):
            x = max(0.0, self._x_for_day(month_start))
            next_x = self._x_for_day(month_starts[i + 1]) if i + 1 < len(month_starts) else width
            painter.setFont(bold)
            painter.setPen(text_color)
            label = painter.fontMetrics().elidedText(f"{month_start:%B %Y}", Qt.ElideRight, int(next_x - x - 8))
            painter.drawText(QRectF(x + 6, 0, next_x - x - 8, self.MONTH_ROW_HEIGHT), Qt.AlignVCenter | Qt.AlignLeft, label)

        for day in visible_days:
            x = self._x_for_day(day)
            if day.day == 1:
                painter.setPen(QPen(faint(60), 1))
                painter.drawLine(QPointF(x, 0), QPointF(x, self.HEADER_HEIGHT))
            weekday_rect = QRectF(x, self.MONTH_ROW_HEIGHT, self.day_width, 13)
            number_rect = QRectF(x, self.MONTH_ROW_HEIGHT + 13, self.day_width, self.DAY_ROW_HEIGHT - 13)
            if self.day_width >= 22:
                weekday = f"{day:%A}" if self.day_width >= 90 else f"{day:%a}" if self.day_width >= 40 else f"{day:%a}"[:2]
                painter.setFont(small)
                painter.setPen(faint(150))
                painter.drawText(weekday_rect, Qt.AlignCenter, weekday)
            if self.day_width < 16:
                # Too narrow for every date: mark today, and label Mondays.
                if day == today:
                    painter.fillRect(number_rect.adjusted(0, 2, 0, -3), QColor(TODAY_COLOR))
                if day.weekday() == 0:
                    painter.setFont(small)
                    painter.setPen(text_color)
                    painter.drawText(
                        QRectF(x + 2, number_rect.top(), 7 * self.day_width, number_rect.height()),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        str(day.day),
                    )
                continue
            if day == today:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(TODAY_COLOR))
                painter.drawRoundedRect(number_rect.adjusted(4, 1, -4, -2), 4, 4)
                painter.setBrush(Qt.NoBrush)
                painter.setFont(bold)
                painter.setPen(QColor("#ffffff"))
            else:
                painter.setFont(self.font())
                painter.setPen(faint(110) if day.weekday() >= 5 else text_color)
            painter.drawText(number_rect, Qt.AlignCenter, str(day.day))

        painter.setPen(QPen(faint(60), 1))
        painter.drawLine(QPointF(0, self.HEADER_HEIGHT - 0.5), QPointF(width, self.HEADER_HEIGHT - 0.5))

    def _paint_dependency_arrows(self, painter: QPainter, normal_color: QColor):
        """An elbow arrow from each blocker's bar end to the blocked bar's
        start. Drawn before the bars, so the bars sit on top of the lines.
        """
        row_of = {bar.card_id: row for row, bar in enumerate(self.bars)}
        for blocker_id, blocked_id in self.block_edges:
            blocker_row, blocked_row = row_of[blocker_id], row_of[blocked_id]
            blocker, blocked = self.bars[blocker_row], self.bars[blocked_row]
            start_rect = self._bar_rect(blocker_row, blocker)
            end_rect = self._bar_rect(blocked_row, blocked)
            x1, y1 = start_rect.right(), start_rect.center().y()
            x2, y2 = end_rect.left(), end_rect.center().y()

            path = QPainterPath(QPointF(x1, y1))
            if x2 - x1 >= 16:
                # Room to go straight across: out, down/up, then in.
                path.lineTo(x1 + 8, y1)
                path.lineTo(x1 + 8, y2)
                path.lineTo(x2, y2)
            else:
                # Blocked card starts before the blocker ends: loop back
                # along the gap between rows.
                gap_y = y1 + (self.ROW_HEIGHT / 2 if y2 >= y1 else -self.ROW_HEIGHT / 2)
                path.lineTo(x1 + 8, y1)
                path.lineTo(x1 + 8, gap_y)
                path.lineTo(x2 - 8, gap_y)
                path.lineTo(x2 - 8, y2)
                path.lineTo(x2, y2)

            clash = blocked.start <= blocker.end and not blocked.done
            color = QColor(OVERDUE_COLOR) if clash else normal_color
            painter.save()
            if blocker.done and blocked.done:
                painter.setOpacity(0.45)
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([QPointF(x2, y2), QPointF(x2 - 6, y2 - 4), QPointF(x2 - 6, y2 + 4)]))
            painter.restore()

    def _paint_bar(self, painter: QPainter, row: int, bar: GanttBar, text_color: QColor, today: date):
        rect = self._bar_rect(row, bar)
        painter.save()
        if bar.done:
            painter.setOpacity(0.45)

        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)
        painter.fillPath(path, QColor(bar.colors[0]))
        if len(bar.colors) > 1:
            # Extra labels: a thin band of their colours along the bottom edge.
            painter.save()
            painter.setClipPath(path, Qt.IntersectClip)
            extra_colors = bar.colors[1:]
            band_width = rect.width() / len(extra_colors)
            for i, color in enumerate(extra_colors):
                painter.fillRect(
                    QRectF(rect.left() + i * band_width, rect.bottom() - 4, band_width, 4), QColor(color)
                )
            painter.restore()

        # Grips at the ends that can be dragged to change the dates: two short
        # lines each, on bars wide enough to have room for them.
        if rect.width() >= 24:
            grip = QColor(readable_text_color(bar.colors[0]))
            grip.setAlpha(110)
            painter.setPen(QPen(grip, 1))
            cy = rect.center().y()
            edges = [rect.right() - 5] + ([rect.left() + 4] if bar.from_action else [])
            for x in edges:
                for dx in (0, 2):
                    painter.drawLine(QPointF(x + dx - 1, cy - 4), QPointF(x + dx - 1, cy + 4))

        title_rect, inside = self._title_rect(rect, bar.title)
        if not inside and bar.due is not None and bar.due > bar.end:
            # The title sits after the bar, where the due-date diamond is drawn:
            # start it just past the diamond instead.
            diamond_right = self._x_for_day(bar.due) + self.day_width / 2 + 8
            title_rect.moveLeft(max(title_rect.left(), diamond_right))
        painter.setFont(self.font())
        painter.setPen(QColor(readable_text_color(bar.colors[0])) if inside else text_color)
        painter.drawText(title_rect, Qt.AlignVCenter | Qt.AlignLeft, bar.title)

        if bar.due is not None:
            cx = self._x_for_day(bar.due) + self.day_width / 2
            cy = rect.center().y()
            r = 6
            diamond = QPolygonF([QPointF(cx, cy - r), QPointF(cx + r, cy), QPointF(cx, cy + r), QPointF(cx - r, cy)])
            overdue = bar.due < today and not bar.done
            painter.setPen(QPen(self.palette().base().color(), 1.5))
            painter.setBrush(QColor(OVERDUE_COLOR) if overdue else text_color)
            painter.drawPolygon(diamond)
        painter.restore()


class CardPopup(QDialog):
    """A card shown just as it looks on the board, opened from the Gantt
    view. Sub-tasks can be ticked right here; "Edit..." closes the popup
    with result EDIT so the caller can open the full card dialog.
    """

    EDIT = 2

    def __init__(self, card_id: int, parent=None):
        super().__init__(parent)
        card = db.get_card(card_id)
        column = db.get_column(card["column_id"])
        self.setWindowTitle(card["title"])

        layout = QVBoxLayout(self)
        column_label = QLabel(f"Column: {column['name']}")
        column_label.setStyleSheet("color: #667085; font-size: 11px;")
        layout.addWidget(column_label)

        card_widget = CardWidget(
            card,
            db.get_card_labels(card_id),
            is_done_column(column["name"]),
            db.get_subtasks(card_id),
            db.get_links_for_cards([card_id]).get(card_id, []),
            db.get_attachments(card_id),
        )
        card_widget.subtask_toggled.connect(db.set_subtask_done)
        card_widget.setFixedWidth(320)
        layout.addWidget(card_widget)

        buttons = QHBoxLayout()
        buttons.addStretch()
        edit_button = QPushButton("Edit...")
        edit_button.clicked.connect(lambda: self.done(self.EDIT))
        close_button = QPushButton("Close")
        close_button.setDefault(True)
        close_button.clicked.connect(self.reject)
        buttons.addWidget(edit_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

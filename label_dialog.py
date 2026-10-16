from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QLineEdit,
    QColorDialog,
    QMessageBox,
    QLabel,
    QInputDialog,
)
from PySide6.QtGui import QColor, QPixmap, QIcon
from PySide6.QtCore import Qt, QSize

import database as db

DEFAULT_LABEL_COLOR = "#4dabf7"


def color_icon(hex_color: str, size: int = 14) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(hex_color))
    return QIcon(pixmap)


class ManageLabelsDialog(QDialog):
    """Add / rename / recolor / delete labels for the current board.

    The caller should refresh label-dependent UI (filter menu, any open
    card dialogs) after this closes, since labels may have changed.
    """

    def __init__(self, board_id: int, parent=None):
        super().__init__(parent)
        self.board_id = board_id
        self.setWindowTitle("Manage Labels")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        new_row = QHBoxLayout()
        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText("New label name")
        self._new_color = DEFAULT_LABEL_COLOR
        self.color_button = QPushButton()
        self.color_button.setFixedWidth(32)
        self._update_color_button()
        self.color_button.clicked.connect(self._pick_new_color)
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._on_add)
        new_row.addWidget(self.new_name_edit)
        new_row.addWidget(self.color_button)
        new_row.addWidget(add_button)
        layout.addLayout(new_row)

        action_row = QHBoxLayout()
        recolor_button = QPushButton("Change color")
        recolor_button.clicked.connect(self._on_recolor)
        rename_button = QPushButton("Rename")
        rename_button.clicked.connect(self._on_rename)
        delete_button = QPushButton("Delete")
        delete_button.clicked.connect(self._on_delete)
        action_row.addWidget(recolor_button)
        action_row.addWidget(rename_button)
        action_row.addWidget(delete_button)
        layout.addLayout(action_row)

        hint = QLabel("Deleting a label removes it from any cards using it.")
        hint.setStyleSheet("color: #667085; font-size: 11px;")
        layout.addWidget(hint)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

        self._reload_list()

    def _update_color_button(self):
        self.color_button.setIcon(color_icon(self._new_color, 18))
        self.color_button.setIconSize(QSize(18, 18))

    def _pick_new_color(self):
        color = QColorDialog.getColor(QColor(self._new_color), self, "Label color")
        if color.isValid():
            self._new_color = color.name()
            self._update_color_button()

    def _reload_list(self):
        self.list_widget.clear()
        for label in db.get_labels(self.board_id):
            item = QListWidgetItem(color_icon(label["color"]), label["name"])
            item.setData(Qt.UserRole, label["id"])
            self.list_widget.addItem(item)

    def _selected_label(self):
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole), item.text()

    def _on_add(self):
        name = self.new_name_edit.text().strip()
        if not name:
            return
        db.add_label(self.board_id, name, self._new_color)
        self.new_name_edit.clear()
        self._new_color = DEFAULT_LABEL_COLOR
        self._update_color_button()
        self._reload_list()

    def _on_recolor(self):
        selected = self._selected_label()
        if not selected:
            return
        label_id, name = selected
        current = next(
            (l["color"] for l in db.get_labels(self.board_id) if l["id"] == label_id),
            DEFAULT_LABEL_COLOR,
        )
        color = QColorDialog.getColor(QColor(current), self, "Label color")
        if color.isValid():
            db.update_label(label_id, name, color.name())
            self._reload_list()

    def _on_rename(self):
        selected = self._selected_label()
        if not selected:
            return
        label_id, old_name = selected
        new_name, ok = QInputDialog.getText(self, "Rename label", "Name:", text=old_name)
        if ok and new_name.strip():
            current = next(
                (l["color"] for l in db.get_labels(self.board_id) if l["id"] == label_id),
                DEFAULT_LABEL_COLOR,
            )
            db.update_label(label_id, new_name.strip(), current)
            self._reload_list()

    def _on_delete(self):
        selected = self._selected_label()
        if not selected:
            return
        label_id, name = selected
        reply = QMessageBox.question(
            self,
            "Delete label",
            f'Delete label "{name}"? It will be removed from all cards.',
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            db.delete_label(label_id)
            self._reload_list()

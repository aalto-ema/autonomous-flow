"""Consistent SVG icon vocabulary, protocol overview and actual-step timeline."""

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from engine.protocol_visual import ACTIONS, preview_protocol

ASSETS = Path(__file__).parent / "assets" / "protocol"


def icon(action):
    return QIcon(str(ASSETS / (action + ".svg")))


class ProtocolView(QWidget):
    def __init__(self, *, live=False):
        super().__init__()
        self.live = live
        self.previous = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel("Actual protocol steps" if live else "Protocol overview")
        self.heading.setStyleSheet("font-size:17px;font-weight:600;")
        layout.addWidget(self.heading)
        row = QHBoxLayout()
        layout.addLayout(row)
        self.current_icon = QLabel()
        self.current_icon.setFixedSize(48, 48)
        row.addWidget(self.current_icon)
        self.current = QLabel(
            "Review a recipe to see its protocol."
            if not live
            else "No instrument steps recorded yet."
        )
        self.current.setWordWrap(True)
        row.addWidget(self.current, 1)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Step / action", "Liquid", "Route", "Status"])
        self.tree.setIconSize(QSize(28, 28))
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(not live)
        self.tree.setMinimumHeight(180)
        self.tree.setMaximumHeight(300 if live else 420)
        layout.addWidget(self.tree)
        self.tree.header().resizeSection(0, 300)
        self.tree.header().resizeSection(1, 150)
        self.tree.header().resizeSection(2, 270)
        note = QLabel(
            "Click a step for details. This shows software commands, not sensor-confirmed liquid delivery."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#526579;")
        layout.addWidget(note)
        self.tree.itemClicked.connect(self.select)

    def select(self, item, column):
        step = item.data(0, Qt.ItemDataRole.UserRole)
        if step:
            self.show_current(step)

    def show_current(self, step):
        self.current_icon.setPixmap(icon(step.action).pixmap(48, 48))
        self.current.setText(
            f"{step.title} • {step.status.capitalize()}\nLiquid: {step.liquid} • Route: {step.route}\n{step.detail}"
        )

    def show_plan(self, plan):
        self.heading.setText(
            "Protocol overview • " + (plan.measurement_name or "Selected experiment")
        )
        self.show_steps(preview_protocol(plan))

    def show_steps(self, steps):
        if steps == self.previous:
            return
        self.previous = steps
        self.tree.clear()
        groups = {}
        active = None
        for s in steps:
            parent = self.tree
            if not self.live:
                if s.stage not in groups:
                    groups[s.stage] = QTreeWidgetItem(self.tree, [s.stage])
                    groups[s.stage].setExpanded(True)
                parent = groups[s.stage]
            item = QTreeWidgetItem(
                parent,
                [f"{s.number}. {s.title}", s.liquid, s.route, s.status.capitalize()],
            )
            item.setIcon(0, icon(s.action))
            item.setData(0, Qt.ItemDataRole.UserRole, s)
            for col in range(4):
                item.setToolTip(col, s.detail)
            if s.status == "running":
                active = item
                for col in range(4):
                    item.setBackground(col, QBrush(QColor("#e3efff")))
            elif s.status in {"failed", "aborted"}:
                for col in range(4):
                    item.setForeground(col, QBrush(QColor("#b42f2f")))
            elif s.status == "completed":
                item.setForeground(3, QBrush(QColor("#187545")))
        if steps:
            current = next(
                (s for s in reversed(steps) if s.status == "running"), steps[-1]
            )
            self.show_current(current)
            if active:
                self.tree.scrollToItem(active)
            elif self.live:
                self.tree.scrollToBottom()


class ProtocolLegend(QWidget):
    def __init__(self):
        super().__init__()
        layout = QGridLayout(self)
        for i, (key, (title, description)) in enumerate(ACTIONS.items()):
            box = QGroupBox()
            row = QHBoxLayout(box)
            picture = QLabel()
            picture.setPixmap(icon(key).pixmap(34, 34))
            row.addWidget(picture)
            label = QLabel(title)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            box.setToolTip(description)
            box.setAccessibleName(title + ": " + description)
            layout.addWidget(box, i // 3, i % 3)

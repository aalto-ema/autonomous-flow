import threading

from PySide6.QtCore import QAbstractTableModel, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    LinearSweepVoltammetrySettings,
)


class Tasks(QObject):
    finished = Signal(str, object, object)

    def run(self, key, function):
        def work():
            try:
                self.finished.emit(key, function(), None)
            except BaseException as error:
                self.finished.emit(key, None, str(error))

        threading.Thread(target=work, name=f"ui-{key}", daemon=True).start()


def number(value, minimum: float = 0, maximum: float = 10000, decimals=4, suffix=""):
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setDecimals(decimals)
    widget.setValue(value)
    widget.setSuffix(suffix)
    widget.setKeyboardTracking(False)
    return widget


def integer(value, minimum=1, maximum=1000):
    widget = QSpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setKeyboardTracking(False)
    return widget


class MethodEditor(QWidget):
    changed = Signal()
    definitions = (
        (
            "Chronoamperometry",
            ChronoamperometrySettings,
            [
                ("potential_v", "Applied potential", 0.5, -10, 10, " V"),
                ("interval_time_s", "Sample interval", 0.1, 0.0001, 1000, " s"),
                ("run_time_s", "Measurement duration", 5, 0.01, 86400, " s"),
                (
                    "equilibration_time_s",
                    "Equilibration before measurement",
                    0,
                    0,
                    86400,
                    " s",
                ),
            ],
        ),
        (
            "Cyclic voltammetry",
            CyclicVoltammetrySettings,
            [
                ("begin_potential_v", "Starting potential", 0, -10, 10, " V"),
                ("vertex1_potential_v", "First turning potential", 0.5, -10, 10, " V"),
                (
                    "vertex2_potential_v",
                    "Second turning potential",
                    -0.5,
                    -10,
                    10,
                    " V",
                ),
                ("step_potential_v", "Potential step", 0.01, 0.0001, 10, " V"),
                ("scan_rate_v_per_s", "Scan rate", 0.1, 0.0001, 100, " V/s"),
                ("scan_count", "Number of scans", 3, 1, 1000, ""),
                ("equilibration_time_s", "Equilibration", 0, 0, 86400, " s"),
            ],
        ),
        (
            "Linear sweep voltammetry",
            LinearSweepVoltammetrySettings,
            [
                ("begin_potential_v", "Starting potential", 0, -10, 10, " V"),
                ("end_potential_v", "Ending potential", 1, -10, 10, " V"),
                ("step_potential_v", "Potential step", 0.01, 0.0001, 10, " V"),
                ("scan_rate_v_per_s", "Scan rate", 0.1, 0.0001, 100, " V/s"),
                ("sweep_count", "Number of sweeps", 1, 1, 1000, ""),
                ("equilibration_time_s", "Equilibration", 0, 0, 86400, " s"),
            ],
        ),
    )

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        self.kind.addItems([d[0] for d in self.definitions])
        layout.addWidget(self.kind)
        self.pages = QStackedWidget()
        self.pages.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self.inputs = []
        layout.addWidget(self.pages)
        for label, cls, fields in self.definitions:
            page = QWidget()
            form = QFormLayout(page)
            values = {}
            for key, title, value, minimum, maximum, suffix in fields:
                widget = (
                    integer(value, minimum, maximum)
                    if key.endswith("_count")
                    else number(value, minimum, maximum, 4, suffix)
                )
                widget.setObjectName(key)
                widget.setToolTip(title + suffix)
                widget.valueChanged.connect(self.changed.emit)
                values[key] = widget
                form.addRow(title, widget)
            self.inputs.append(values)
            self.pages.addWidget(page)
        self.kind.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.kind.currentIndexChanged.connect(self.changed.emit)

    def settings(self):
        index = self.kind.currentIndex()
        return self.definitions[index][1](
            **{key: w.value() for key, w in self.inputs[index].items()}
        )

    def adopt(self, settings):
        for index, (_, cls, _) in enumerate(self.definitions):
            if isinstance(settings, cls):
                self.kind.setCurrentIndex(index)
                for key, w in self.inputs[index].items():
                    w.setValue(getattr(settings, key))
                return


class RowsModel(QAbstractTableModel):
    def __init__(self, headers):
        super().__init__()
        self.headers = headers
        self.rows = []

    def rowCount(self, parent=None):
        return len(self.rows)

    def columnCount(self, parent=None):
        return len(self.headers)

    def data(self, index, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid():
            return str(self.rows[index.row()][index.column()])

    def headerData(self, section, orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                self.headers[section]
                if orientation == Qt.Orientation.Horizontal
                else str(section + 1)
            )

    def replace(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()


def table(headers):
    view = QTableView()
    model = RowsModel(headers)
    view.setModel(model)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.horizontalHeader().setStretchLastSection(True)
    return view, model

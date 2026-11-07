"""Guided desktop workspace. All instrument actions go through the engine."""

from typing import cast

from matplotlib.backends.backend_qt import NavigationToolbar2QT
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.runze_setup import RUNZE_PLUMBING
from controllers.experiment_controller import (
    ExperimentCleanupSettings,
    ManualExperimentPlanRequest,
)
from storage.experiment_repository import (
    SourceConfigurationEntry,
    SourceStockComponentInput,
)
from ui.qt.protocol_view import ProtocolLegend, ProtocolView
from ui.qt.widgets import MethodEditor, Tasks, integer, number, table

STYLE = """
QMainWindow, QDialog { background: #f4f6fa; color: #18283b; }
QLabel { color: #18283b; }
QGroupBox { background: white; border: 1px solid #dce3ed; border-radius: 9px; margin-top: 16px; padding: 16px 12px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 4px; }
QPushButton { background: white; color: #213650; border: 1px solid #c5d1e0; border-radius: 6px; padding: 8px 14px; min-height: 19px; }
QPushButton:hover { background: #eaf1fc; border-color: #8bafe0; }
QPushButton:disabled { color: #7d8999; background: #edf0f5; border-color: #dde3eb; }
QPushButton[primary="true"] { background: #235fba; color: white; border-color: #235fba; }
QPushButton[primary="true"]:disabled { background: #a0b8da; border-color: #a0b8da; }
QPushButton[danger="true"] { background: #bd3434; color: white; border-color: #bd3434; font-weight: 700; }
QPushButton[danger="true"]:disabled { background: #d2a0a0; border-color: #d2a0a0; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; color: #18283b; border: 1px solid #c5d1e0; border-radius: 5px; padding: 5px; min-height: 22px; }
QTableView { background: white; color: #18283b; alternate-background-color: #f3f6fb; gridline-color: #e5ebf3; selection-background-color: #d8e7fc; selection-color: #18283b; }
QHeaderView::section { background: #eaf0f8; color: #284463; padding: 7px; border: none; }
QListWidget { background: #172e49; color: #e7eef9; border: none; padding: 8px; }
QListWidget::item { padding: 13px 10px; border-radius: 5px; }
QListWidget::item:selected { background: #2c527f; }
QPlainTextEdit { background: white; color: #284463; border: 1px solid #dce3ed; border-radius: 6px; }
QCheckBox { color: #18283b; spacing: 7px; }
"""


class MainWindow(QMainWindow):
    page_names = [
        "Start here",
        "Instruments and sources",
        "Plan experiments",
        "Live measurement",
        "Saved results",
        "Maintenance and recovery",
        "Manual controls",
        "Help",
    ]

    def __init__(self, client):
        super().__init__()
        self.client = client
        self._closed_ready = False
        self.setWindowTitle("Autonomous Flow — laboratory workspace")
        self.resize(1380, 920)
        self.setMinimumSize(1000, 700)
        self.setStyleSheet(STYLE)
        self.settings = QSettings("AutonomousFlow", "Desktop")
        self.tasks = Tasks(self)
        self.tasks.finished.connect(self._finished)
        self.callbacks = {}
        self.pending = set()
        self.action_buttons = []
        self.snapshot = None
        self.plans = ()
        self.draft_revision = getattr(self, "draft_revision", 0) + 1
        self.configuration = None
        self.stocks = ()
        self.substances = ()
        self.source_widgets = {}
        self.variable_stocks = []
        self.dirty = False
        self.plot_id = None
        self.plot_revision = None
        self.plot_indices = {}
        self.plot_series = {}
        self.result_records = []
        self.session_records = []
        self._last_events = ()
        self._last_measurement = None
        base = QWidget()
        self.setCentralWidget(base)
        outer = QVBoxLayout(base)
        outer.setContentsMargins(0, 0, 0, 0)
        top = QWidget()
        top.setObjectName("topBar")
        top.setStyleSheet("QWidget#topBar {background:white;}")
        bar = QHBoxLayout(top)
        bar.setContentsMargins(20, 12, 20, 12)
        title = QLabel("AUTONOMOUS FLOW")
        title.setStyleSheet("font-weight:700; font-size:18px; letter-spacing:1px;")
        bar.addWidget(title)
        self.mode = QLabel(
            "SIMULATION • generated data, no hardware"
            if client.info["simulation"]
            else "INSTRUMENT CONTROL"
        )
        self.mode.setStyleSheet(
            "color:#9c6500;" if client.info["simulation"] else "color:#235fba;"
        )
        bar.addWidget(self.mode)
        bar.addStretch()
        self.status = QLabel("Starting engine…")
        bar.addWidget(self.status)
        self.pause = self.button(
            "Pause after this stage",
            lambda: self.call("engine", "request_pause"),
            control=True,
        )
        bar.addWidget(self.pause)
        self.stop = self.button("Stop instruments", self.stop_instruments, control=True)
        self.stop.setProperty("danger", True)
        self.stop.style().unpolish(self.stop)
        self.stop.style().polish(self.stop)
        self.stop.setToolTip(
            "Request an immediate device stop. This may require manual recovery."
        )
        bar.addWidget(self.stop)
        outer.addWidget(top)
        split = QSplitter()
        outer.addWidget(split, 1)
        self.navigation = QListWidget()
        self.navigation.addItems(self.page_names)
        self.navigation.setMaximumWidth(240)
        self.navigation.setMinimumWidth(195)
        split.addWidget(self.navigation)
        self.pages = QStackedWidget()
        split.addWidget(self.pages)
        split.setStretchFactor(1, 1)
        self.layouts: list[QVBoxLayout] = []
        for title in self.page_names:
            content = QWidget()
            layout = QVBoxLayout(content)
            self.layouts.append(layout)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(14)
            heading = QLabel(title)
            heading.setStyleSheet("font-size:26px; font-weight:650;")
            layout.addWidget(heading)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(content)
            self.pages.addWidget(scroll)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self._overview()
        self._sources()
        self._experiments()
        self._live()
        self._results()
        self._maintenance()
        self._manual()
        self._help()
        self.message = QLabel("Connect instruments or explore with simulation.")
        self.message.setWordWrap(True)
        self.message.setStyleSheet("padding:10px 20px;background:#e8eff8;")
        outer.addWidget(self.message)
        refresh = QAction("Refresh status", self)
        refresh.setShortcut(QKeySequence("Ctrl+R"))
        refresh.triggered.connect(self.poll)
        self.addAction(refresh)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(300)
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.reload_sources()
        self.poll()

    def button(self, text, action, *, primary=False, control=False):
        b = QPushButton(text)
        b.clicked.connect(action)
        b.setProperty("primary", primary)
        b.setAccessibleName(text)
        if not control:
            self.action_buttons.append(b)
        return b

    def group(self, title, layout):
        box = QGroupBox(title)
        inner = QVBoxLayout(box)
        layout.addWidget(box)
        return inner

    def note(self, layout, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color:#526579;")
        layout.addWidget(label)
        return label

    def run(self, key, fn, callback=None):
        if key in self.pending:
            return
        self.pending.add(key)
        self.callbacks[key] = callback
        self.tasks.run(key, fn)

    def call(self, target, name, kwargs=None, callback=None):
        self.message.setText(name.replace("_", " ").capitalize() + "…")
        self.run(
            "command-" + name,
            lambda: self.client.request(
                "call", target, name, kwargs=kwargs or {}
            ).result(),
            callback,
        )

    def _finished(self, key, value, error):
        self.pending.discard(key)
        callback = self.callbacks.pop(key, None)
        if error:
            self.message.setText(error)
            if key not in {"snapshot", "live-points", "events"}:
                QMessageBox.warning(self, "Action could not complete", error)
        else:
            if key.startswith("command-"):
                self.message.setText("Action completed.")
            if callback:
                try:
                    callback(value)
                except Exception as error:
                    self.message.setText(str(error))
                    QMessageBox.warning(self, "Check your inputs", str(error))
            elif key.startswith("command-"):
                self.message.setText(
                    "Action completed. Check the current status before continuing."
                )
        if key.startswith("command-"):
            self.poll()

    def go(self, index):
        self.navigation.setCurrentRow(index)

    def _overview(self):
        layout = self.layouts[0]
        self.note(
            layout,
            "Follow these steps for your first experiment. The engine checks each action and saves progress as it runs.",
        )
        grid = QGridLayout()
        layout.addLayout(grid)
        for index, (title, text, page) in enumerate(
            [
                (
                    "1  Connect and identify sources",
                    "Choose the pump serial port, connect the measurement instrument, and assign your stock solutions.",
                    1,
                ),
                (
                    "2  Build and review a recipe",
                    "Set a measurement method and compositions. Review the rounded volumes before starting.",
                    2,
                ),
                (
                    "3  Run and monitor",
                    "Run the queue or use individual stage controls. View live curves and pause at a stage boundary.",
                    3,
                ),
                (
                    "4  Inspect and maintain",
                    "Compare saved results, reuse a protocol, and follow the ordered cleaning and recovery steps.",
                    4,
                ),
            ]
        ):
            box = QGroupBox(title)
            inside = QVBoxLayout(box)
            self.note(inside, text)
            inside.addWidget(
                self.button(
                    "Open " + self.page_names[page].lower(),
                    lambda checked=False, p=page: self.go(p),
                    control=True,
                )
            )
            grid.addWidget(box, index // 2, index % 2)
        self.next_step = QLabel("Next: connect instruments.")
        self.next_step.setWordWrap(True)
        self.next_step.setStyleSheet(
            "font-size:18px;padding:18px;background:#e5efff;border-radius:8px;"
        )
        layout.addWidget(self.next_step)
        self.state_details = QLabel()
        self.state_details.setWordWrap(True)
        layout.addWidget(self.state_details)
        layout.addStretch()

    def _sources(self):
        layout = self.layouts[1]
        self.note(
            layout,
            "Connect instruments first. Sources are the bottles attached to the selector valve; water is the balance diluent.",
        )
        connection = self.group("Instrument connections", layout)
        row = QHBoxLayout()
        connection.addLayout(row)
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(280)
        self.port.setToolTip(
            "Select a discovered serial port or enter COM5 / /dev/cu.usbserial…"
        )
        self.discover_ports()
        row.addWidget(self.port, 1)
        row.addWidget(self.button("Refresh ports", self.discover_ports, control=True))
        self.pump_connect = self.button(
            "Connect pump and valve",
            lambda: self.call(
                "runze",
                "connect",
                {"port": self.port.currentText()},
                self.after_connect,
            ),
            primary=True,
        )
        row.addWidget(self.pump_connect)
        self.palm_connect = self.button(
            "Connect EmStat",
            lambda: self.call("emstat", "connect", callback=self.after_connect),
        )
        row.addWidget(self.palm_connect)
        row2 = QHBoxLayout()
        connection.addLayout(row2)
        row2.addWidget(
            self.button("Disconnect pump", lambda: self.call("runze", "disconnect"))
        )
        row2.addWidget(
            self.button("Disconnect EmStat", lambda: self.call("emstat", "disconnect"))
        )
        row2.addWidget(
            self.button(
                "Refresh hardware status", lambda: self.call("runze", "refresh_status")
            )
        )
        row2.addStretch()
        box = self.group("Source assignments", layout)
        self.note(
            box,
            "Enable each source you intend to use. Assign its recorded stock, then choose a variable or fixed fraction. Confirm line condition only after checking the apparatus.",
        )
        self.sources = QTableWidget(0, 6)
        self.sources.setHorizontalHeaderLabels(
            [
                "Source / port",
                "Use",
                "Stock solution",
                "Recipe policy",
                "Fixed fraction",
                "Line condition",
            ]
        )
        self.sources.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.sources.setMinimumHeight(180)
        box.addWidget(self.sources)
        row = QHBoxLayout()
        box.addLayout(row)
        row.addWidget(
            self.button("Save source assignments", self.save_sources, primary=True)
        )
        row.addWidget(self.button("Record a stock solution…", self.new_stock))
        row.addWidget(
            self.button("Reload assignments", self.reload_sources, control=True)
        )
        row.addStretch()
        self.source_hint = self.note(box, "Save assignments before building a plan.")
        layout.addWidget(
            self.button("Next: plan experiments", lambda: self.go(2), control=True)
        )
        layout.addStretch()

    def discover_ports(self):
        if not hasattr(self, "port"):
            return
        previous = self.port.currentText()
        self.port.clear()
        from serial.tools.list_ports import comports

        ports = [p.device for p in comports()]
        if self.client.info["simulation"]:
            ports.insert(0, "simulation")
        self.port.addItems(ports)
        if previous:
            self.port.setEditText(previous)

    def after_connect(self, value):
        self.message.setText(
            "Connected. Check source assignments and line conditions next."
        )
        self.poll()

    def reload_sources(self):
        def fetch():
            return (
                self.client.experiment.source_configuration,
                self.client.repository.list_source_stocks(),
                self.client.repository.list_substances(),
                self.client.experiment.snapshot,
            )

        self.run("sources", fetch, self.show_sources)

    def show_sources(self, data):
        config, stocks, substances, snapshot = data
        changed_revision = (
            self.configuration is not None
            and self.configuration.revision != config.revision
        )
        self.configuration = config
        self.stocks = stocks
        self.substances = substances
        states = {p.selector_port: p.status for p in snapshot.priming}
        assignments = {e.selector_port: e for e in config.entries}
        self.sources.setRowCount(len(RUNZE_PLUMBING.liquid_sources))
        self.source_widgets = {}
        for row, p in enumerate(RUNZE_PLUMBING.liquid_sources):
            entry = assignments.get(
                p.number, SourceConfigurationEntry(p.number, p.key, None, False)
            )
            self.sources.setItem(
                row, 0, QTableWidgetItem(f"{p.label} • port {p.number}")
            )
            enabled = QCheckBox()
            enabled.setChecked(entry.enabled)
            enabled.setEnabled(p.key != "water")
            self.sources.setCellWidget(row, 1, enabled)
            stock = QComboBox()
            stock.addItem(
                "Water (built in)" if p.key == "water" else "Choose a stock…", None
            )
            if p.key != "water":
                for item in stocks:
                    stock.addItem(f"{item.id}: {item.label}", item.id)
                stock.setCurrentIndex(max(0, stock.findData(entry.source_stock_id)))
            else:
                stock.setEnabled(False)
            self.sources.setCellWidget(row, 2, stock)
            policy = QComboBox()
            policy.addItems(["Balance" if p.key == "water" else "Variable", "Fixed"])
            policy.setCurrentIndex(entry.locked_fraction is not None)
            self.sources.setCellWidget(row, 3, policy)
            fraction = number(entry.locked_fraction or 0, 0, 1, 6)
            fraction.setEnabled(policy.currentIndex() == 1)
            self.sources.setCellWidget(row, 4, fraction)
            condition = self.button(
                states.get(p.number, "unknown").replace("_", " ").capitalize()
                + " — confirm…",
                lambda checked=False, port=p.number: self.confirm_line(port),
                control=True,
            )
            self.sources.setCellWidget(row, 5, condition)
            policy.currentIndexChanged.connect(
                lambda i, w=fraction: w.setEnabled(i == 1)
            )
            for widget in (enabled, stock, policy, fraction):
                signal = (
                    widget.toggled
                    if isinstance(widget, QCheckBox)
                    else widget.currentIndexChanged
                    if isinstance(widget, QComboBox)
                    else widget.valueChanged
                )
                signal.connect(self.source_changed)
            self.source_widgets[p.number] = (enabled, stock, policy, fraction)
        self.sources.resizeRowsToContents()
        self.dirty = False
        self.source_hint.setText(
            f"Saved source configuration revision {config.revision}."
        )
        if changed_revision:
            for row in range(self.queue.rowCount()):
                for col in range(1, self.queue.columnCount()):
                    self.queue.removeCellWidget(row, col)
        self.update_queue_columns()
        self.update_buttons()

    def source_changed(self, *args):
        self.dirty = True
        self.invalidate_plan()
        self.source_hint.setText(
            "Unsaved source assignments. Save them before planning."
        )
        self.update_buttons()

    def save_sources(self):
        if self.configuration is None:
            return
        entries = tuple(
            SourceConfigurationEntry(
                p.number,
                p.key,
                w[1].currentData(),
                w[0].isChecked(),
                w[3].value() if w[2].currentIndex() == 1 else None,
            )
            for p in RUNZE_PLUMBING.liquid_sources
            for w in [self.source_widgets[p.number]]
        )
        self.call(
            "experiment",
            "replace_source_configuration",
            {"entries": entries, "expected_revision": self.configuration.revision},
            lambda _: self.reload_sources(),
        )

    def confirm_line(self, port):
        state, ok = QInputDialog.getItem(
            self,
            "Confirm physical line condition",
            "Inspect this tube. What does it actually contain?",
            ["Air filled", "Primed with its assigned stock", "Unknown"],
            0,
            False,
        )
        if not ok:
            return
        status = {
            "Air filled": "air_filled",
            "Primed with its assigned stock": "primed",
            "Unknown": "unknown",
        }[state]
        if (
            QMessageBox.question(
                self,
                "Confirm line inspection",
                f"Confirm you inspected port {port} and its assigned bottle.\n\nThis records a physical condition; it does not prime or empty the tube.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.call(
            "experiment",
            "set_source_line_state",
            {"selector_port": port, "status": status},
            lambda _: self.reload_sources(),
        )

    def new_stock(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Record a prepared stock solution")
        dialog.resize(650, 380)
        layout = QVBoxLayout(dialog)
        self.note(
            layout,
            "Record the solution already in your bottle. Concentrations are in mol/L; include every dissolved component.",
        )
        batch = QLineEdit()
        batch.setPlaceholderText("Bottle / batch label")
        layout.addWidget(batch)
        grid = QTableWidget(0, 2)
        grid.setHorizontalHeaderLabels(["Substance", "Concentration (mol/L)"])
        grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(grid)

        def add():
            row = grid.rowCount()
            grid.insertRow(row)
            combo = QComboBox()
            for s in self.substances:
                combo.addItem(s.label, s.id)
            grid.setCellWidget(row, 0, combo)
            grid.setCellWidget(row, 1, number(0.1, 0.000001, 100, 6))

        row = QHBoxLayout()
        layout.addLayout(row)
        row.addWidget(self.button("Add component", add, control=True))
        row.addWidget(
            self.button(
                "Remove selected component",
                lambda: grid.removeRow(grid.currentRow()),
                control=True,
            )
        )

        def substance():
            label, ok = QInputDialog.getText(dialog, "New substance", "Substance name:")
            if not ok or not label.strip():
                return
            key = label.strip().lower().replace(" ", "_")

            def created(result):
                def refreshed(items):
                    self.substances = items
                    for r in range(grid.rowCount()):
                        combo = cast(QComboBox, grid.cellWidget(r, 0))
                        for item in items:
                            if combo.findData(item.id) < 0:
                                combo.addItem(item.label, item.id)

                self.run(
                    "stock-substances",
                    lambda: self.client.repository.list_substances(),
                    refreshed,
                )

            self.call(
                "repository",
                "find_or_create_substance",
                {"key": key, "label": label.strip()},
                created,
            )

        row.addWidget(self.button("New substance…", substance, control=True))
        add()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def save():
            try:
                components = tuple(
                    SourceStockComponentInput(
                        cast(QComboBox, grid.cellWidget(r, 0)).currentData(),
                        cast(QDoubleSpinBox, grid.cellWidget(r, 1)).value(),
                    )
                    for r in range(grid.rowCount())
                )
                if not components or any(c.substance_id is None for c in components):
                    raise ValueError("Add a substance before recording a stock")
                label = batch.text().strip() or None
                self.call(
                    "repository",
                    "create_source_stock",
                    {"components": components, "batch_name": label},
                    lambda _: (dialog.accept(), self.reload_sources()),
                )
            except Exception as error:
                QMessageBox.warning(dialog, "Check stock details", str(error))

        buttons.accepted.connect(save)
        dialog.exec()

    def _experiments(self):
        layout = self.layouts[2]
        self.note(
            layout,
            "Choose a method, enter stock fractions, then review the delivered volumes before starting. Water fills the remaining fraction.",
        )
        group = self.group("1. Measurement and liquid handling", layout)
        self.method = MethodEditor()
        protocol_row = QHBoxLayout()
        group.addLayout(protocol_row)
        protocol_row.addWidget(self.method, 1, Qt.AlignmentFlag.AlignTop)
        self.method.changed.connect(self.invalidate_plan)
        handling_panel = QWidget()
        form = QFormLayout(handling_panel)
        protocol_row.addWidget(handling_panel, 1)
        self.workflow = QComboBox()
        self.workflow.addItems(["Stopped flow", "Continuous flow"])
        self.count = integer(1)
        self.displacement = number(0.25, 0.001, 100, 4, " mL")
        self.handling = integer(10, 1, 100)
        self.approach = integer(8, 1, 100)
        self.flow = integer(1, 1, 100)
        self.inlet_rpm = integer(10, 1, 100)
        self.line_rinse = number(0.1, 0, 100, 3, " mL")
        self.water = QComboBox()
        self.water.addItem("One water addition", "single")
        self.water.addItem("Water between components", "between_components")
        self.rinse = number(1, 0.01, 100, 3, " mL")
        self.cycles = integer(1)
        self.cell_rinse = number(1, 0.01, 100, 3, " mL")
        advanced_panel = QWidget()
        advanced_form = QFormLayout(advanced_panel)
        advanced_toggle = QCheckBox("Show handling and cleaning settings")
        advanced_toggle.toggled.connect(advanced_panel.setVisible)
        advanced_panel.setVisible(False)
        for label, w in [
            ("Workflow", self.workflow),
            ("Measurements per mixture", self.count),
            ("Displacement between measurements", self.displacement),
            ("Handling speed (RPM)", self.handling),
            ("Inlet aspiration speed (RPM)", self.inlet_rpm),
            ("Additional source line rinse", self.line_rinse),
            ("Cell approach speed (RPM)", self.approach),
            ("Continuous measurement flow (RPM)", self.flow),
            ("Water distribution", self.water),
            ("Mixing vessel rinse volume", self.rinse),
            ("Rinse cycles", self.cycles),
            ("Flow cell rinse volume", self.cell_rinse),
        ]:
            (
                form
                if w in (self.workflow, self.count, self.displacement)
                else advanced_form
            ).addRow(label, w)
            (
                w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged
            ).connect(self.invalidate_plan)
        form.addRow(advanced_toggle)
        form.addRow(advanced_panel)
        self.workflow.currentIndexChanged.connect(self.protocol_controls)
        self.count.valueChanged.connect(self.protocol_controls)
        self.protocol_controls()
        group = self.group("2. Experiment queue", layout)
        self.queue = QTableWidget(0, 1)
        self.queue.setHorizontalHeaderLabels(["Experiment name"])
        self.queue.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.queue.setMinimumHeight(160)
        group.addWidget(self.queue)
        row = QHBoxLayout()
        group.addLayout(row)
        row.addWidget(self.button("Add experiment", self.add_queue))
        row.addWidget(self.button("Remove selected", self.remove_queue))
        self.queue.itemChanged.connect(self.invalidate_plan)
        self.plan_button = self.button(
            "Review delivery plan", self.build_plan, primary=True
        )
        row.addWidget(self.plan_button)
        self.preview, self.preview_model = table(
            [
                "Experiment",
                "Source",
                "Requested mL",
                "Commanded mL",
                "Steps",
                "Strokes",
                "Rounding %",
            ]
        )
        self.preview.setMinimumHeight(190)
        layout.addWidget(self.preview)
        self.plan_summary = self.note(
            layout, "Review a plan to see exact step rounding and total liquid usage."
        )
        row = QHBoxLayout()
        layout.addLayout(row)
        self.start = self.button(
            "Start reviewed queue", lambda: self.start_queue(False), primary=True
        )
        row.addWidget(self.start)
        self.resume = self.button("Resume paused queue", lambda: self.start_queue(True))
        row.addWidget(self.resume)
        self.discard = self.button(
            "Discard paused queue",
            lambda: self.call("experiment", "discard_manual_play"),
        )
        row.addWidget(self.discard)
        self.protocol_preview = ProtocolView()
        layout.addWidget(self.protocol_preview)
        stages = self.group("Individual experiment stages (advanced)", layout)
        self.note(
            stages,
            "These actions use the selected reviewed recipe. Prime, prepare, measure, then clean. The engine checks prerequisites.",
        )
        self.stage_index = integer(1)
        stages.addWidget(self.stage_index)
        for title, name in [
            ("Prime required sources", "prime_required_sources"),
            ("Prepare selected mixture", "prepare_mixture"),
            ("Measure prepared mixture", "run_flow_cell_measurement"),
            ("Clean prepared mixture", "clean_after_experiment"),
        ]:
            stages.addWidget(
                self.button(title, lambda checked=False, n=name: self.single_stage(n))
            )
        layout.addStretch()

    def single_stage(self, name):
        if name == "prime_required_sources":
            kwargs = {"rpm": self.handling.value()}
        elif name == "prepare_mixture":
            if not self.plans or self.stage_index.value() > len(self.plans):
                self.message.setText(
                    "Review a recipe and choose an existing queue index first."
                )
                return
            kwargs = {"plan": self.plans[self.stage_index.value() - 1]}
        else:
            self.run(
                "prepared-mixture",
                lambda: self.client.experiment.prepared_mixture,
                lambda prepared: (
                    self.call(
                        "experiment",
                        name,
                        {"prepared_mixture": prepared},
                        lambda _: self.refresh_results(),
                    )
                    if prepared
                    else self.message.setText("Prepare a mixture first.")
                ),
            )
            return
        self.call("experiment", name, kwargs)

    def protocol_controls(self, *args):
        stopped = self.workflow.currentIndex() == 0
        self.count.setEnabled(stopped)
        self.flow.setEnabled(not stopped)
        self.displacement.setEnabled(stopped and self.count.value() > 1)

    def invalidate_plan(self, *args):
        self.plans = ()
        self.draft_revision = getattr(self, "draft_revision", 0) + 1
        if hasattr(self, "plan_summary"):
            self.plan_summary.setText(
                "Inputs changed. Review the delivery plan again before starting."
            )
        if hasattr(self, "start"):
            self.start.setEnabled(False)

    def update_queue_columns(self):
        if self.configuration is None:
            return
        self.invalidate_plan()
        self.variable_stocks = [
            e
            for e in self.configuration.entries
            if e.enabled
            and e.source_config_key != "water"
            and e.locked_fraction is None
        ]
        self.queue.setColumnCount(1 + len(self.variable_stocks))
        self.queue.setHorizontalHeaderLabels(
            ["Experiment name"]
            + [f"{e.source_config_key} fraction (0–1)" for e in self.variable_stocks]
        )
        for r in range(self.queue.rowCount()):
            self.fill_queue(r)
        if not self.queue.rowCount():
            self.add_queue()

    def fill_queue(self, row):
        for col in range(1, self.queue.columnCount()):
            if self.queue.cellWidget(row, col) is None:
                w = number(0.1, 0, 1, 6)
                w.valueChanged.connect(self.invalidate_plan)
                self.queue.setCellWidget(row, col, w)

    def add_queue(self):
        row = self.queue.rowCount()
        self.queue.insertRow(row)
        self.queue.setItem(row, 0, QTableWidgetItem(f"Experiment {row + 1}"))
        self.fill_queue(row)
        self.invalidate_plan()

    def remove_queue(self):
        if self.queue.currentRow() >= 0:
            self.queue.removeRow(self.queue.currentRow())
            self.invalidate_plan()

    def build_plan(self):
        if self.dirty:
            QMessageBox.information(
                self,
                "Save sources first",
                "Save your source assignments on Instruments and sources.",
            )
            return
        kwargs = dict(
            measurement_settings=self.method.settings(),
            handling_rpm=self.handling.value(),
            inlet_aspiration_rpm=self.inlet_rpm.value(),
            approach_rpm=self.approach.value(),
            flow_rpm=None if self.workflow.currentIndex() == 0 else self.flow.value(),
            workflow_kind="stopped_flow"
            if self.workflow.currentIndex() == 0
            else "continuous_flow",
            measurement_count=self.count.value()
            if self.workflow.currentIndex() == 0
            else 1,
            between_measurement_displacement_volume_ml=self.displacement.value()
            if self.workflow.currentIndex() == 0 and self.count.value() > 1
            else None,
            water_distribution=self.water.currentData(),
            cleanup_settings=ExperimentCleanupSettings(
                self.rinse.value(),
                self.cycles.value(),
                self.cell_rinse.value(),
                self.line_rinse.value(),
            ),
        )
        requests = tuple(
            ManualExperimentPlanRequest(
                {
                    e.source_stock_id: cast(
                        QDoubleSpinBox, self.queue.cellWidget(r, c)
                    ).value()
                    for c, e in enumerate(self.variable_stocks, 1)
                },
                cast(QTableWidgetItem, self.queue.item(r, 0)).text(),
            )
            for r in range(self.queue.rowCount())
        )

        revision = self.draft_revision

        def plan():
            protocol = self.client.experiment.estimate_experiment_protocol(**kwargs)
            return self.client.experiment.build_manual_experiment_plans(
                requests=requests, protocol=protocol
            )

        self.run(
            "command-plan",
            plan,
            lambda plans: (
                self.show_plan(plans)
                if revision == self.draft_revision
                else self.message.setText(
                    "Inputs changed during planning. Review them again."
                )
            ),
        )

    def show_plan(self, plans):
        self.plans = plans
        rows = []
        for i, p in enumerate(plans, 1):
            for t in p.mixture_transfers:
                rows.append(
                    [
                        i,
                        t.source_config_key,
                        f"{t.requested_volume_ml:.6f}",
                        f"{t.nominal_volume_ml:.6f}",
                        t.syringe_steps,
                        t.liquid_stroke_count,
                        f"{t.rounding_error_percent:+.4f}",
                    ]
                )
        self.preview_model.replace(rows)
        self.preview.resizeColumnsToContents()
        self.plan_summary.setText(
            f"{len(plans)} experiment(s), {sum(p.total_mixture_volume_ml for p in plans):.3f} mL requested mixture total. Commanded volumes are nominal step conversions; delivery requires physical calibration. Priming and cleanup use additional liquid."
        )
        self.stage_index.setMaximum(max(1, len(plans)))
        if plans:
            self.protocol_preview.show_plan(plans[0])
        self.update_buttons()

    def start_queue(self, resume):
        kwargs = {"resume": True} if resume else {"plans": self.plans}
        self.go(3)
        self.call(
            "engine",
            "run_queue",
            kwargs,
            lambda _: (
                self.invalidate_plan(),
                self.refresh_results(),
                self.reload_sources(),
            ),
        )

    def _live(self):
        layout = self.layouts[3]
        self.live_status = self.note(layout, "No measurement yet.")
        self.protocol_live = ProtocolView(live=True)
        layout.addWidget(self.protocol_live)
        self.figure = Figure(figsize=(8, 4), tight_layout=True)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.axes = self.figure.add_subplot(111)
        layout.addWidget(NavigationToolbar2QT(self.canvas, self))
        layout.addWidget(self.canvas, 1)
        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumHeight(180)
        layout.addWidget(self.event_log)

    def draw_series(self, series, kind="chronoamperometry"):
        self.axes.clear()
        for label, x, y in series:
            stride = max(1, len(x) // 10000)
            self.axes.plot(x[::stride], y[::stride], label=label)
        self.axes.set_xlabel(
            "Time (s)" if kind == "chronoamperometry" else "Potential (V)"
        )
        self.axes.set_ylabel("Current (µA)")
        self.axes.grid(alpha=0.2)
        if series:
            self.axes.legend()
        self.canvas.draw_idle()

    def show_points(self, data):
        if (
            data.measurement_id != self.plot_id
            or data.data_revision != self.plot_revision
        ):
            self.plot_series = {}
            self.plot_indices = {}
        self.plot_id = data.measurement_id
        self.plot_revision = data.data_revision
        for s in data.series:
            label, x, y = self.plot_series.setdefault(s.series_id, (s.label, [], []))
            x.extend(s.x_values)
            y.extend(s.currents_ua)
            self.plot_indices[s.series_id] = s.next_index
        if data.series:
            self.draw_series(
                list(self.plot_series.values()),
                self.snapshot.measurement.measurement_type
                if self.snapshot
                else "chronoamperometry",
            )

    def _results(self):
        layout = self.layouts[4]
        self.note(
            layout,
            "Select a saved measurement to inspect its curve. Native instrument sessions and the database are retained in the data folder.",
        )
        row = QHBoxLayout()
        layout.addLayout(row)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by experiment, method, date, or status")
        self.search.textChanged.connect(self.filter_results)
        row.addWidget(self.search)
        row.addWidget(
            self.button("Refresh results", self.refresh_results, control=True)
        )
        row.addWidget(
            self.button("Load selected curve", self.load_result, control=True)
        )
        row.addWidget(
            self.button("Reuse measurement method", self.reuse_method, control=True)
        )
        row.addWidget(
            self.button("Export loaded curve (CSV)…", self.export_curve, control=True)
        )
        self.result_table, self.result_model = table(
            ["Mixture", "Measurement", "Name", "Method", "Status", "Created"]
        )
        layout.addWidget(self.result_table)
        self.result_table.doubleClicked.connect(self.load_result)
        self.result_table.selectionModel().selectionChanged.connect(
            self.load_objectives
        )
        self.objective_table, self.objective_model = table(
            ["Objective", "Value", "Unit", "Standard error", "Analysis version"]
        )
        layout.addWidget(self.objective_table)
        layout.addWidget(
            self.button(
                "Reanalyze completed measurements",
                lambda: self.call(
                    "experiment",
                    "analyze_completed_measurements",
                    callback=lambda _: self.refresh_results(),
                ),
            )
        )
        layout.addWidget(
            self.button(
                "Browse standalone measurement sessions…",
                self.browse_sessions,
                control=True,
            )
        )
        self.loaded_note = self.note(
            layout, "Loaded curves appear on Live measurement."
        )
        layout.addStretch()
        self.refresh_results()

    def refresh_results(self):
        self.run(
            "results",
            lambda: self.client.experiment.list_saved_experiment_runs(),
            self.show_results,
        )

    def show_results(self, runs):
        self.result_records = [(run, m) for run in runs for m in run.measurements]
        self.filter_results()

    def filter_results(self, *args):
        query = self.search.text().lower()
        self.filtered_records = []
        rows = []
        for run, m in self.result_records:
            row = [
                run.mixture_id,
                m.measurement_id,
                m.measurement_name or "",
                m.measurement_type,
                m.status,
                m.created_at,
            ]
            if query in " ".join(map(str, row)).lower():
                rows.append(row)
                self.filtered_records.append((run, m))
        self.result_model.replace(rows)
        self.result_table.resizeColumnsToContents()

    def selected_result(self):
        row = self.result_table.currentIndex().row()
        if row < 0:
            raise ValueError("Select a saved measurement first.")
        return self.filtered_records[row]

    def load_result(self, *args):
        try:
            run, m = self.selected_result()
        except ValueError as error:
            self.message.setText(str(error))
            return
        self.run(
            "load-result",
            lambda: self.client.experiment.load_saved_experiment_measurement(
                run=run, measurement=m
            ),
            self.show_loaded,
        )

    def show_loaded(self, measurement):
        self._last_measurement = measurement
        self.draw_series(
            [
                (s.label, list(s.x_values), list(s.currents_ua))
                for s in measurement.series
            ],
            measurement.measurement_type,
        )
        self.loaded_note.setText(
            f"Loaded {measurement.file_name}: {measurement.point_count} points."
        )
        self.go(3)

    def reuse_method(self):
        try:
            run, _ = self.selected_result()
        except ValueError as error:
            self.message.setText(str(error))
            return
        self.run(
            "reuse",
            lambda: self.client.experiment.saved_protocol_for_run(run),
            self.adopt_protocol,
        )

    def adopt_protocol(self, p):
        self.method.adopt(p.measurement_settings)
        self.workflow.setCurrentIndex(p.workflow_kind == "continuous_flow")
        self.count.setValue(p.measurement_count)
        self.inlet_rpm.setValue(p.inlet_aspiration_rpm or p.handling_rpm)
        self.line_rinse.setValue(p.source_line_additional_rinse_volume_ml)
        self.handling.setValue(p.handling_rpm)
        self.approach.setValue(p.approach_rpm)
        self.flow.setValue(p.flow_rpm or 1)
        self.water.setCurrentIndex(self.water.findData(p.water_distribution))
        self.rinse.setValue(p.mixing_rinse_volume_ml)
        self.cycles.setValue(p.mixing_rinse_cycles)
        self.cell_rinse.setValue(p.flow_cell_rinse_through_volume_ml)
        if p.between_measurement_displacement_volume_ml:
            self.displacement.setValue(p.between_measurement_displacement_volume_ml)
        self.invalidate_plan()
        self.go(2)

    def load_objectives(self, *args):
        try:
            _, m = self.selected_result()
        except ValueError:
            return
        self.run(
            "objectives",
            lambda: self.client.experiment.current_measurement_objectives(
                measurement_id=m.measurement_id
            ),
            lambda items: self.objective_model.replace(
                [
                    [
                        i.metric_name,
                        i.value,
                        i.unit,
                        i.standard_error,
                        i.analysis_version,
                    ]
                    for i in items
                ]
            ),
        )

    def browse_sessions(self):
        def choose(items):
            if not items:
                self.message.setText("No standalone measurements saved yet.")
                return
            labels = [f"{item.file_name}" for item in items]
            label, ok = QInputDialog.getItem(
                self, "Saved sessions", "Choose a measurement:", labels, 0, False
            )
            if ok:
                self.run(
                    "load-session",
                    lambda: self.client.emstat.load_saved_measurement(file_name=label),
                    self.show_loaded,
                )

        self.run(
            "sessions", lambda: self.client.emstat.list_saved_measurements(), choose
        )

    def export_curve(self):
        if self._last_measurement is None:
            self.message.setText("Load a saved curve before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export loaded curve", "measurement.csv", "CSV (*.csv)"
        )
        if not path:
            return
        import csv

        try:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["series", "x", "current_uA"])
                for s in self._last_measurement.series:
                    writer.writerows(
                        (s.label, x, y) for x, y in zip(s.x_values, s.currents_ua)
                    )
            self.message.setText("Curve exported to " + path)
        except OSError as error:
            QMessageBox.warning(self, "Export failed", str(error))

    def _maintenance(self):
        layout = self.layouts[5]
        self.recovery_note = self.note(
            layout,
            "Use maintenance after a run or when the engine requests recovery. Inspect the real instrument before acknowledging a recovered state.",
        )
        group = self.group("Source line maintenance — follow this order", layout)
        group.addWidget(
            self.button("1. Recover source lines and clear cell", self.recover_lines)
        )
        group.addWidget(
            self.button(
                "2. Rinse recovered source lines",
                lambda: self.call("experiment", "rinse_recovered_source_lines"),
            )
        )
        group.addWidget(
            self.button(
                "3. Empty maintained source lines with air",
                lambda: self.call("experiment", "air_empty_maintained_source_lines"),
            )
        )
        group.addWidget(
            self.button("Acknowledge inspected recovery…", self.acknowledge_recovery)
        )
        group.addWidget(
            self.button(
                "Restart unresponsive measurement worker…",
                self.recover_worker,
                control=True,
            )
        )
        layout.addStretch()

    def recover_lines(self):
        self.run(
            "active-plan",
            lambda: self.client.experiment.active_manual_experiment_plan,
            lambda plan: (
                self.call(
                    "experiment",
                    "recover_source_lines_and_clear_flow_cell",
                    {"plan": plan},
                )
                if plan
                else self.message.setText(
                    "Review a plan first to specify maintenance volumes."
                )
            ),
        )

    def acknowledge_recovery(self):
        if (
            QMessageBox.question(
                self,
                "Verify recovery",
                "Have you inspected the syringe, selector routing, source lines, vessel and cell, and completed the required recovery?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.call(
                "experiment",
                "acknowledge_experiment_recovery",
                callback=lambda _: self.reload_sources(),
            )

    def recover_worker(self):
        if (
            QMessageBox.question(
                self,
                "Verify measurement device stopped",
                "Verify the measurement device has stopped physically. Restarting its software worker cannot guarantee the instrument is stopped. Continue?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.call("engine", "recover_instrument", {"verified_stopped": True})

    def _manual(self):
        layout = self.layouts[6]
        self.note(
            layout,
            "Manual actions can invalidate the prepared mixture and source line state. Use these controls for commissioning and troubleshooting.",
        )
        group = self.group("Syringe controls", layout)
        form = QFormLayout()
        group.addLayout(form)
        self.manual_volume = number(0.5, 0.001, 5, 4, " mL")
        self.manual_rpm = integer(10, 1, 100)
        self.manual_port = QComboBox()
        self.manual_port.addItems(["inlet", "outlet"])
        form.addRow("Volume", self.manual_volume)
        form.addRow("Speed (RPM)", self.manual_rpm)
        form.addRow("Syringe valve route", self.manual_port)
        group.addWidget(
            self.button(
                "Aspirate into syringe",
                lambda: self.call(
                    "runze",
                    "aspirate",
                    {
                        "volume_ml": self.manual_volume.value(),
                        "rpm": self.manual_rpm.value(),
                        "source": self.manual_port.currentText(),
                    },
                ),
            )
        )
        group.addWidget(
            self.button(
                "Dispense from syringe",
                lambda: self.call(
                    "runze",
                    "dispense",
                    {
                        "volume_ml": self.manual_volume.value(),
                        "rpm": self.manual_rpm.value(),
                        "destination": self.manual_port.currentText(),
                    },
                ),
            )
        )
        group.addWidget(
            self.button(
                "Empty syringe to selected route",
                lambda: self.call(
                    "runze",
                    "empty_syringe",
                    {
                        "rpm": self.manual_rpm.value(),
                        "destination": self.manual_port.currentText(),
                    },
                ),
            )
        )
        self.selector_port = QComboBox()
        for p in RUNZE_PLUMBING.selector_ports:
            self.selector_port.addItem(f"{p.label} (port {p.number})", p.number)
        form.addRow("Selector destination", self.selector_port)
        group.addWidget(
            self.button(
                "Move selector to chosen port",
                lambda: self.call(
                    "runze",
                    "move_selector_to_port",
                    {"port": self.selector_port.currentData()},
                ),
            )
        )
        group.addWidget(
            self.button(
                "Initialize syringe", lambda: self.call("runze", "reset_syringe")
            )
        )
        group.addWidget(
            self.button(
                "Home selector", lambda: self.call("runze", "reset_selector_to_home")
            )
        )
        group = self.group("Standalone electrochemical measurement", layout)
        self.manual_method = MethodEditor()
        group.addWidget(self.manual_method)
        group.addWidget(
            self.button("Measure without experiment fluidics", self.manual_measure)
        )
        layout.addStretch()

    def manual_measure(self):
        settings = self.manual_method.settings()
        index = self.manual_method.kind.currentIndex()
        name = [
            "measure_chronoamperometry",
            "measure_cyclic_voltammetry",
            "measure_linear_sweep_voltammetry",
        ][index]
        self.go(3)
        self.call(
            "emstat", name, {"settings": settings}, lambda _: self.refresh_results()
        )

    def _help(self):
        layout = self.layouts[7]
        for title, text in [
            (
                "First experiment",
                "Connect both instruments. Record stock concentrations and assign bottles to ports. Inspect and confirm line contents. Save sources. Choose a method and recipe fractions. Review the plan, then start the queue. The engine primes, prepares, measures and cleans automatically.",
            ),
            (
                "Pause and stop",
                "Pause finishes the current experiment stage and preserves a resumable queue. Stop requests an immediate hardware abort; it can leave liquid partway through a transfer. Inspect and recover before continuing. Closing during a run detaches the UI and requests a stage-boundary pause.",
            ),
            (
                "Volume accuracy",
                "Requested volumes describe the recipe. Commanded volumes describe rounded syringe steps, not measured delivery. Tubing dead volume, bubbles, leaks, viscosity, valve condition and calibration still affect delivered amounts. The provisional 0.05 mL minimum stroke is a configuration safeguard, not a verified accuracy limit.",
            ),
            (
                "Simulation",
                "Simulation exercises the production workflow with synthetic curves and no hardware. Its sessions are generated data. Use a separate data folder; never treat simulated results as experimental measurements.",
            ),
            (
                "Connection troubleshooting",
                "Choose the serial device path, such as /dev/cu.usbserial…, rather than a Windows COM number on macOS. Real PalmSens measurements require the compatible .NET runtime. The SDK runs in an isolated worker, so startup and UI operation do not require loading .NET.",
            ),
            (
                "Optimization",
                "The queue supports manual experiment recipes and stored objectives. Autonomous Bayesian proposal selection is not implemented by this migration.",
            ),
        ]:
            self.note(self.group(title, layout), text)
        self.note(
            layout,
            "Protocol icon key — hover over an icon for its meaning. Only applicable steps appear in a reviewed recipe; the live timeline records actual substeps.",
        )
        layout.addWidget(ProtocolLegend())
        self.note(layout, "Data folder: " + str(self.client.root))
        layout.addStretch()

    def poll(self):
        if self.client._closed:
            return
        self.run("snapshot", lambda: self.client.engine.snapshot(), self.show_snapshot)

    def show_snapshot(self, s):
        self.snapshot = s
        self.protocol_live.show_steps(s.protocol_steps)
        e = s.experiment
        self.status.setText(
            s.operation or e.workflow_phase.replace("_", " ").capitalize()
        )
        self.state_details.setText(
            f"Pump: {'connected' if s.runze_connected else 'disconnected'} • Measurement: {'connected' if s.emstat_connected else 'disconnected'}\nWorkflow: {e.workflow_phase} • Queue: {e.manual_play_state} • Stage: {e.manual_play_next_stage or 'none'}"
        )
        self.next_step.setText(
            "Recovery required: open Maintenance and recovery."
            if e.workflow_phase == "recovery_required"
            else "Connect both instruments and configure your sources."
            if not (s.runze_connected and s.emstat_connected)
            else "Review an experiment plan, then start the queue."
        )
        self.recovery_note.setText(
            f"Workflow: {e.workflow_phase}. Source maintenance: {e.line_maintenance_phase}. "
            + (e.error_message or s.error or "")
        )
        self.live_status.setText(
            f"{s.measurement.state.capitalize()} • {s.measurement.point_count} points • {e.operation or s.operation or 'idle'}"
        )
        self.update_buttons()
        if (
            s.measurement.measurement_id
            and self.pages.currentIndex() == 3
            and self._last_measurement is None
        ):
            kwargs = dict(
                measurement_id=self.plot_id,
                data_revision=self.plot_revision,
                start_indices=dict(self.plot_indices),
            )
            self.run(
                "live-points",
                lambda: self.client.emstat.data_since(**kwargs),
                self.show_points,
            )
        if s.measurement.busy:
            self._last_measurement = None
        self.run("events", lambda: self.client.engine.events(), self.show_events)

    def show_events(self, events):
        if events != self._last_events:
            self.event_log.setPlainText(
                "\n".join(f"{kind}: {message}" for kind, message in events)
            )
            self._last_events = events

    def update_buttons(self):
        if self.snapshot is None:
            return
        s = self.snapshot
        e = s.experiment
        for b in self.action_buttons:
            b.setEnabled(not s.busy)
        self.pause.setEnabled(s.busy)
        self.stop.setEnabled(s.busy or s.runze_connected or s.emstat_connected)
        self.start.setEnabled(
            bool(self.plans)
            and not self.dirty
            and not s.busy
            and s.runze_connected
            and s.emstat_connected
            and e.workflow_phase != "recovery_required"
        )
        self.resume.setEnabled(not s.busy and e.manual_play_state == "paused")
        self.discard.setEnabled(not s.busy and e.manual_play_state == "paused")
        self.plan_button.setEnabled(
            not s.busy and not self.dirty and s.runze_connected and s.emstat_connected
        )

    def stop_instruments(self):
        self.call("engine", "abort", callback=lambda _: self.go(5))

    def closeEvent(self, event):
        if getattr(self, "_closed_ready", False):
            event.accept()
            return
        event.ignore()
        if "close" in self.pending:
            return
        if self.snapshot and self.snapshot.busy:
            if (
                QMessageBox.question(
                    self,
                    "Pause and detach",
                    "Request a pause after the current stage and close this window? The engine stays running so you can reconnect.",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return

            def close():
                self.client.engine.request_pause()
                self.client.detach()
        else:
            close = self.client.close
        self.settings.setValue("geometry", self.saveGeometry())

        def done(_):
            self.timer.stop()
            self._closed_ready = True
            self.close()

        self.run("close", close, done)

"""Exercise the desktop UI with the same isolated engine used in production."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from engine.process import EngineClient
from ui.qt.window import MainWindow


class DesktopTests(unittest.TestCase):
    def wait(self, predicate, seconds=10):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_guided_simulated_experiment_and_results(self):
        self.app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            client = EngineClient(Path(directory), simulate=True)
            window = None
            errors = []
            original = QMessageBox.warning
            QMessageBox.warning = lambda *args: errors.append(str(args[-1]))
            try:
                window = MainWindow(client)
                window.show()
                self.wait(
                    lambda: (
                        window.configuration is not None and window.snapshot is not None
                    )
                )
                client.runze.connect(port="simulation")
                client.emstat.connect()
                client.experiment.set_source_line_state(
                    selector_port=7, status="air_filled"
                )
                window.poll()
                self.wait(
                    lambda: (
                        window.snapshot.runze_connected
                        and window.snapshot.emstat_connected
                    )
                )
                window.build_plan()
                self.wait(lambda: bool(window.plans) or bool(errors))
                self.assertFalse(errors)
                self.assertTrue(window.start.isEnabled())
                window.go(2)
                self.app.processEvents()
                window.grab().save("/private/tmp/autonomous-flow-plan.png")
                window.start_queue(False)
                self.wait(lambda: "command-run_queue" not in window.pending)
                self.wait(lambda: bool(window.result_records))
                self.assertEqual(window.result_records[0][1].status, "completed")
                self.assertFalse(errors)
                window.go(3)
                window.poll()
                self.wait(lambda: bool(window.protocol_live.previous))
                self.app.processEvents()
                window.grab().save("/private/tmp/autonomous-flow-protocol.png")
                self.assertGreater(window.protocol_live.tree.topLevelItemCount(), 10)
                window.result_table.selectRow(0)
                window.load_result()
                self.wait(lambda: window._last_measurement is not None)
                self.assertGreater(window._last_measurement.point_count, 0)
                window.go(0)
                self.app.processEvents()
                window.grab().save("/private/tmp/autonomous-flow-desktop.png")
            finally:
                QMessageBox.warning = original
                if window:
                    window.timer.stop()
                    window._closed_ready = True
                    window.close()
                client.close()

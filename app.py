"""Desktop launcher; importing this module does not initialize instrument SDKs."""

import argparse
import multiprocessing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Autonomous Flow laboratory workspace")
    parser.add_argument("--ui", choices=("qt", "nicegui"), default="qt")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Use simulated instruments and generated measurements",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory for engine state, database and measurements",
    )
    options = parser.parse_args()
    project_root = Path(__file__).resolve().parent
    base = (
        project_root
        if (project_root / "pyproject.toml").exists()
        else Path.home() / ".autonomous-flow"
    )
    root = options.data_dir or base / ("simulation" if options.simulate else ".")
    if options.ui == "nicegui":
        from gui.legacy import main as legacy

        legacy(root, simulate=options.simulate)
        return
    from PySide6.QtWidgets import QApplication, QMessageBox

    from engine.process import EngineClient
    from ui.qt.window import MainWindow

    application = QApplication.instance() or QApplication([])
    application.setApplicationName("Autonomous Flow")
    try:
        client = EngineClient(root, simulate=options.simulate)
    except Exception as error:
        QMessageBox.critical(None, "Engine could not start", str(error))
        return
    window = MainWindow(client)
    window.show()
    application.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

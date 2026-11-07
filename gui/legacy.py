import logging
import threading
from pathlib import Path

from nicegui import Client, ui

from common.logging_setup import configure_logging
from config.runze_setup import RUNZE_PLUMBING
from controllers.emstat_controller import EmStatController
from controllers.experiment_controller import ExperimentController
from controllers.runze_controller import RunzeController
from gui.runze_app import RunzeApp
from palmsens.emstat4s import EmStat4S
from palmsens.emstat_session_repository import EmStatSessionRepository
from storage.experiment_repository import ExperimentRepository

logger = logging.getLogger("gui.app")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_DATABASE_FILE = PROJECT_ROOT / "data" / "autonomous_flow.sqlite3"


def main(root=None, *, simulate=False) -> None:
    log_file = configure_logging()

    from engine.process import EngineClient

    client = EngineClient(root or PROJECT_ROOT, simulate=simulate)
    controller = client.runze
    emstat_controller = client.emstat
    experiment_controller = client.experiment

    client_gate_lock = threading.Lock()
    active_client_id: str | None = None

    def release_client(client_id: str) -> None:
        nonlocal active_client_id

        with client_gate_lock:
            if active_client_id == client_id:
                active_client_id = None

    def build_app(client: Client) -> None:
        nonlocal active_client_id

        client_id = client.id

        with client_gate_lock:
            if active_client_id not in (None, client_id):
                ui.label(
                    "Runze control is already open in another browser tab"
                ).classes("text-xl font-bold")
                ui.label("Close the other tab, then refresh this page.")
                logger.warning("Rejected additional Runze GUI client")
                return

            active_client_id = client_id

        client.on_delete(lambda: release_client(client_id))
        RunzeApp(controller, emstat_controller, experiment_controller)

    logger.info("Starting Runze control GUI; log file: %s", log_file)

    try:
        ui.run(
            root=build_app,
            host="127.0.0.1",
            reload=False,
            # unocss="wind4",
            uvicorn_logging_level="warning",
        )
    except KeyboardInterrupt:
        logger.info("Terminal shutdown requested")
    finally:
        if client.engine.snapshot().busy:
            client.engine.request_pause()
            client.detach()
        else:
            client.close()

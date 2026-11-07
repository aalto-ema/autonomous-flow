from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from nicegui import run, ui

from controllers.experiment_controller import ExperimentController
from gui.emstat_plot import EmStatPlot
from gui.emstat_plot_panel import EmStatPlotPanelState
from storage.experiment_repository import SavedExperimentMeasurement, SavedExperimentRun

logger = logging.getLogger(__name__)


class ExperimentMeasurementPanel:
    """Browse experiment runs and independent PalmSens measurement sessions."""

    LIVE_SOURCE = "__live__"
    RUN_PREFIX = "run:"
    SESSION_PREFIX = "session:"

    def __init__(
        self,
        controller: ExperimentController,
        *,
        state: EmStatPlotPanelState,
        on_busy_change: Callable[[bool], None],
        on_view_results: Callable[
            [SavedExperimentRun, SavedExperimentMeasurement], Any
        ],
        on_view_protocol: Callable[[SavedExperimentRun], Any],
        on_use_protocol: Callable[[], Any],
        on_discard_protocol: Callable[[], Any],
    ) -> None:
        self.controller = controller
        self.state = state
        self.on_busy_change = on_busy_change
        self.on_view_results = on_view_results
        self.on_view_protocol = on_view_protocol
        self.on_use_protocol = on_use_protocol
        self.on_discard_protocol = on_discard_protocol
        self._system_busy = controller.busy
        self._load_active = False
        self._preview_active = False
        self._runs: tuple[SavedExperimentRun, ...] = ()
        self._run_by_key: dict[str, SavedExperimentRun] = {}
        self._selected_run: SavedExperimentRun | None = None
        self._selected_measurement: SavedExperimentMeasurement | None = None
        self._live_progress_key: tuple[int | None, int, int, str] | None = None

        self._build()
        self.refresh_saved_measurement_options()
        self._sync_controls()

    def _build(self) -> None:
        ui.add_css(
            """
            .experiment-protocol-action-anchor {
                min-height: 2.5rem;
            }
            .experiment-protocol-actions-floating {
                border-radius: 0.5rem;
                box-shadow:
                    0 0 0 0.5rem white,
                    0 0.5rem 1rem rgb(0 0 0 / 18%);
                z-index: 2100;
            }
            .experiment-protocol-primary-action {
                height: 2.5rem;
                min-height: 2.5rem;
            }
            """
        )
        ui.add_head_html(
            """
            <script>
            window.__updateExperimentProtocolActions = () => {
                const anchor = document.querySelector(
                    '.experiment-protocol-action-anchor');
                const normal = document.querySelector(
                    '.experiment-protocol-actions-normal');
                const floating = document.querySelector(
                    '.experiment-protocol-actions-floating');
                if (!anchor || !normal || !floating) return;
                const anchorRect = anchor.getBoundingClientRect();
                floating.style.left = `${anchorRect.left}px`;
                floating.style.width = `${anchorRect.width}px`;
                const active = anchor.classList.contains('preview-active');
                const belowViewport = active
                    && anchorRect.top > window.innerHeight;
                normal.classList.toggle('invisible', belowViewport);
                floating.classList.toggle('hidden', !belowViewport);
            };
            if (!window.__experimentProtocolActionsScrollInstalled) {
                window.addEventListener(
                    'scroll',
                    () => window.__updateExperimentProtocolActions?.(),
                    true,
                );
                window.addEventListener(
                    'resize',
                    () => window.__updateExperimentProtocolActions?.(),
                );
                window.__experimentProtocolActionsScrollInstalled = true;
            }
            </script>
            """
        )
        with ui.row().classes("w-full items-stretch gap-4 flex-wrap"):
            with (
                ui.column()
                .classes("min-w-0 self-stretch")
                .style("flex: 1 1 560px; min-width: min(100%, 560px);")
            ):
                self.plot = EmStatPlot(self.controller.emstat_controller)

            with (
                ui.column()
                .classes("gap-3")
                .style("flex: 0 1 24rem; min-width: min(100%, 24rem);")
            ):
                ui.label("Plot data").classes("text-lg font-bold")
                self.source_select = ui.select(
                    options={self.LIVE_SOURCE: "Live measurement"},
                    label="Experiment / session",
                    value=self.LIVE_SOURCE,
                    on_change=self._source_changed,
                ).classes("w-full")
                self.measurement_select = ui.select(
                    options={},
                    label="Measurement in experiment",
                    on_change=self._measurement_changed,
                ).classes("hidden w-full")
                self.history_status = ui.label("").classes("text-sm text-gray-600")
                self.protocol_action_anchor = (
                    ui.row()
                    .classes(
                        "experiment-protocol-action-anchor w-full items-center gap-2"
                    )
                    .style(
                        "display: grid; "
                        "grid-template-columns: repeat(2, minmax(0, 1fr));"
                    )
                )
                with self.protocol_action_anchor:
                    self.view_results_button = (
                        ui.button(
                            "View results",
                            icon="analytics",
                            on_click=self._view_results,
                        )
                        .props("outline")
                        .classes("experiment-protocol-primary-action w-full")
                        .style("grid-column: 1;")
                    )
                    self.view_protocol_button = (
                        ui.button(
                            "View protocol",
                            icon="visibility",
                            on_click=self._view_protocol,
                        )
                        .props("outline")
                        .classes("experiment-protocol-primary-action w-full")
                        .style("grid-column: 2;")
                    )
                    self.protocol_action_row = (
                        ui.row()
                        .classes(
                            "hidden experiment-protocol-actions-normal w-full gap-2"
                        )
                        .style(
                            "grid-column: 1 / -1; display: grid; "
                            "grid-template-columns: repeat(2, minmax(0, 1fr));"
                        )
                    )
                    with self.protocol_action_row:
                        ui.button("Discard", on_click=self.on_discard_protocol).props(
                            "flat"
                        ).classes("w-full")
                        ui.button(
                            "Use protocol", icon="edit", on_click=self.on_use_protocol
                        ).classes("experiment-protocol-primary-action w-full")

        self.floating_protocol_actions = (
            ui.element("div")
            .classes("hidden experiment-protocol-actions-floating")
            .style("position: fixed; bottom: 1rem; z-index: 2100;")
        )
        with (
            self.floating_protocol_actions,
            ui.row()
            .classes("w-full gap-2 rounded-lg bg-white")
            .style("display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));"),
        ):
            ui.button("Discard", on_click=self.on_discard_protocol).props(
                "flat"
            ).classes("w-full")
            ui.button(
                "Use protocol", icon="edit", on_click=self.on_use_protocol
            ).classes("experiment-protocol-primary-action w-full")

        ui.element("q-scroll-observer").on(
            "scroll",
            js_handler="""
            () => {
                const anchor = document.querySelector(
                    '.experiment-protocol-action-anchor');
                const normal = document.querySelector(
                    '.experiment-protocol-actions-normal');
                const floating = document.querySelector(
                    '.experiment-protocol-actions-floating');
                if (!anchor || !normal || !floating) return;
                const anchorRect = anchor.getBoundingClientRect();
                floating.style.left = `${anchorRect.left}px`;
                floating.style.width = `${anchorRect.width}px`;
                const active = anchor.classList.contains('preview-active');
                const belowViewport = active
                    && anchorRect.top > window.innerHeight;
                normal.classList.toggle('invisible', belowViewport);
                floating.classList.toggle('hidden', !belowViewport);
            }
            """,
        )

    @property
    def selected_run(self) -> SavedExperimentRun | None:
        return self._selected_run

    def set_system_busy(self, busy: bool) -> None:
        self._system_busy = busy
        self._sync_controls()

    def set_protocol_preview_active(self, active: bool) -> None:
        self._preview_active = active
        if active:
            self.protocol_action_anchor.classes(add="preview-active")
            self.view_results_button.classes(add="hidden")
            self.view_protocol_button.classes(add="hidden")
            self.protocol_action_row.classes(remove="hidden")
            self.floating_protocol_actions.classes(add="hidden")
        else:
            self.protocol_action_anchor.classes(remove="preview-active")
            self.view_results_button.classes(remove="hidden")
            self.view_protocol_button.classes(remove="hidden")
            self.protocol_action_row.classes(add="hidden", remove="invisible")
            self.floating_protocol_actions.classes(add="hidden")
            ui.run_javascript(
                "document.querySelector('.experiment-protocol-actions-floating')"
                "?.classList.add('hidden');"
            )
        self._sync_controls()

    def show_live(self) -> None:
        self.source_select.set_value(self.LIVE_SOURCE)
        self.state.plot_source_file_name = None
        self._selected_run = None
        self._selected_measurement = None
        self.plot.show_live()
        self._refresh_live_measurement_progress()

    def refresh(self) -> None:
        self.plot.refresh()
        self._refresh_live_measurement_progress()

    def _refresh_live_measurement_progress(self) -> None:
        if self.source_select.value != self.LIVE_SOURCE:
            self._live_progress_key = None
            return
        snapshot = self.controller.snapshot
        sequence_number = snapshot.measurement_sequence_number
        measurement_count = snapshot.measurement_count
        if (
            sequence_number is None
            or measurement_count is None
            or measurement_count <= 1
        ):
            self._live_progress_key = None
            self.measurement_select.classes(add="hidden")
            if self.history_status.text.startswith("Live experiment"):
                self.history_status.set_text("")
            return
        progress_key = (
            snapshot.mixture_id,
            sequence_number,
            measurement_count,
            snapshot.state,
        )
        if progress_key == self._live_progress_key:
            return
        self._live_progress_key = progress_key
        options: dict[int, str] = {}
        for number in range(1, measurement_count + 1):
            if number < sequence_number:
                status = "Completed"
            elif number > sequence_number:
                status = "Pending"
            elif snapshot.state == "failed":
                status = "Failed"
            elif snapshot.state == "aborted":
                status = "Aborted"
            elif snapshot.state == "completed":
                status = "Completed"
            else:
                status = "Live"
            options[number] = f"{number}. Measurement — {status}"
        self.measurement_select.set_options(options, value=sequence_number)
        self.measurement_select.classes(remove="hidden")
        experiment_label = (
            "pending" if snapshot.mixture_id is None else str(snapshot.mixture_id)
        )
        self.history_status.set_text(
            f"Live experiment {experiment_label} · measurement "
            f"{sequence_number} of {measurement_count}"
        )

    def refresh_saved_measurement_options(self) -> None:
        try:
            self._runs = self.controller.list_saved_experiment_runs()
            sessions = self.controller.emstat_controller.list_saved_measurements()
        except Exception as error:
            logger.exception("Failed to list saved experiment measurements")
            ui.notify(str(error), type="negative")
            return

        options: dict[str, str] = {self.LIVE_SOURCE: "Live measurement"}
        self._run_by_key = {}
        linked_paths: set[str] = set()
        for saved_run in self._runs:
            key = f"{self.RUN_PREFIX}{saved_run.mixture_id}"
            self._run_by_key[key] = saved_run
            options[key] = self._run_label(saved_run)
            for measurement in saved_run.measurements:
                if measurement.session_file_path is not None:
                    linked_paths.add(measurement.session_file_path.as_posix())
                    linked_paths.add(measurement.session_file_path.name)

        for session in sessions:
            if (
                session.file_name in linked_paths
                or session.file_name.split("/")[-1] in linked_paths
            ):
                continue
            options[f"{self.SESSION_PREFIX}{session.file_name}"] = session.display_label

        current = self.source_select.value
        persisted = self.state.plot_source_file_name
        selected = persisted if persisted in options else current
        if selected not in options:
            selected = self.LIVE_SOURCE
        self.source_select.set_options(options, value=selected)

    async def _source_changed(self, event: Any) -> None:
        value = event.value
        if not isinstance(value, str):
            return
        self.state.plot_source_file_name = None if value == self.LIVE_SOURCE else value
        self._selected_run = self._run_by_key.get(value)
        self._selected_measurement = None
        if value == self.LIVE_SOURCE:
            self.plot.show_live()
            self._refresh_live_measurement_progress()
        elif self._selected_run is not None:
            self._show_run_measurement_options(self._selected_run)
            measurement = self._first_plottable_measurement(self._selected_run)
            if measurement is not None:
                self._selected_measurement = measurement
                await self._load_run_measurement(self._selected_run, measurement)
        elif value.startswith(self.SESSION_PREFIX):
            self.measurement_select.classes(add="hidden")
            self._selected_measurement = None
            await self._load_standalone_session(value.removeprefix(self.SESSION_PREFIX))
        self._sync_controls()

    def _show_run_measurement_options(self, saved_run: SavedExperimentRun) -> None:
        options = {
            measurement.measurement_id: self._measurement_label(measurement)
            for measurement in saved_run.measurements
        }
        first = self._first_plottable_measurement(saved_run)
        value = first.measurement_id if first is not None else next(iter(options), None)
        self.measurement_select.set_options(options, value=value)
        self.measurement_select.classes(remove="hidden")
        self.history_status.set_text(
            f"Experiment {saved_run.mixture_id} · "
            f"{len(saved_run.measurements)} measurement(s)"
        )

    async def _measurement_changed(self, event: Any) -> None:
        saved_run = self._selected_run
        if saved_run is None or not isinstance(event.value, int):
            return
        measurement = next(
            (
                item
                for item in saved_run.measurements
                if item.measurement_id == event.value
            ),
            None,
        )
        if measurement is None:
            return
        if measurement.status != "completed" or measurement.session_file_path is None:
            ui.notify(
                "That measurement has no completed session to plot", type="warning"
            )
            return
        self._selected_measurement = measurement
        await self._load_run_measurement(saved_run, measurement)

    async def _load_run_measurement(
        self, saved_run: SavedExperimentRun, measurement: SavedExperimentMeasurement
    ) -> None:
        self._set_load_active(True)
        try:
            loaded = await run.io_bound(
                self.controller.load_saved_experiment_measurement,
                run=saved_run,
                measurement=measurement,
            )
            if loaded is None:
                raise RuntimeError(
                    "Saved experiment measurement loader returned no result"
                )
            self.plot.show_saved_measurement(loaded)
        except Exception as error:
            logger.exception("Failed to load experiment measurement")
            ui.notify(str(error), type="negative")
        finally:
            self._set_load_active(False)

    async def _load_standalone_session(self, file_name: str) -> None:
        self._set_load_active(True)
        try:
            loaded = await run.io_bound(
                partial(
                    self.controller.emstat_controller.load_saved_measurement,
                    file_name=file_name,
                )
            )
            if loaded is None:
                raise RuntimeError("Saved session loader returned no result")
            self.plot.show_saved_measurement(loaded)
        except Exception as error:
            logger.exception("Failed to load standalone measurement session")
            ui.notify(str(error), type="negative")
        finally:
            self._set_load_active(False)

    def _view_protocol(self) -> None:
        if self._selected_run is not None:
            self.on_view_protocol(self._selected_run)

    def _view_results(self) -> None:
        if self._selected_run is not None and self._selected_measurement is not None:
            self.on_view_results(self._selected_run, self._selected_measurement)

    def _set_load_active(self, active: bool) -> None:
        if self._load_active == active:
            return
        self._load_active = active
        self.on_busy_change(active)
        self._sync_controls()

    def _sync_controls(self) -> None:
        busy = self._system_busy or self._load_active or self.controller.busy
        self.source_select.set_enabled(not busy and not self._preview_active)
        self.measurement_select.set_enabled(not busy)
        self.view_results_button.set_enabled(
            not busy
            and not self._preview_active
            and self._selected_run is not None
            and self._selected_measurement is not None
        )
        self.view_protocol_button.set_enabled(
            not busy and not self._preview_active and self._selected_run is not None
        )

    @staticmethod
    def _first_plottable_measurement(
        saved_run: SavedExperimentRun,
    ) -> SavedExperimentMeasurement | None:
        return next(
            (
                item
                for item in saved_run.measurements
                if item.status == "completed" and item.session_file_path is not None
            ),
            None,
        )

    @staticmethod
    def _run_label(saved_run: SavedExperimentRun) -> str:
        name = next(
            (
                item.measurement_name
                for item in saved_run.measurements
                if item.measurement_name
            ),
            None,
        )
        prefix = f"{name} — " if name else ""
        timestamp = saved_run.created_at.replace("T", " ")[:19]
        return f"{prefix}Experiment {saved_run.mixture_id} — {timestamp}"

    @staticmethod
    def _measurement_label(measurement: SavedExperimentMeasurement) -> str:
        method = measurement.measurement_type.replace("_", " ").title()
        return f"{measurement.sequence_number}. {method} — {measurement.status.title()}"

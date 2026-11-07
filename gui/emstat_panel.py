import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from nicegui import run, ui

from controllers.emstat_controller import EmStatController
from gui.emstat_method_editor import EmStatMethodEditor, EmStatMethodEditorState
from gui.emstat_plot_panel import EmStatPlotPanel, EmStatPlotPanelState
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    LinearSweepVoltammetrySettings,
    MeasurementAbortedError,
)

logger = logging.getLogger(__name__)


@dataclass
class EmStatPanelState(EmStatMethodEditorState):
    measurement_name: str = ""
    plot: EmStatPlotPanelState = field(default_factory=EmStatPlotPanelState)


class EmStatPanel:
    AUTO_ESTIMATE_DELAY_S = 0.5

    def __init__(
        self,
        controller: EmStatController,
        *,
        state: EmStatPanelState | None = None,
        on_busy_change: Callable[[bool], None],
        external_busy: Callable[[], bool] | None = None,
    ) -> None:
        self.controller = controller
        self.state = state if state is not None else EmStatPanelState()
        self.on_busy_change = on_busy_change
        self._external_busy = external_busy or (lambda: False)

        self._ui_operation_active = False
        self._abort_request_active = False
        self._estimate_operation_active = False
        self._plot_panel_busy = False
        self._estimate_dirty = True
        self._estimate_invalidated_at = time.monotonic()
        self._last_local_busy_state = self.local_busy
        self._last_connected_state = controller.connected

        self._build()
        self._register_state_handlers()
        self._sync_controls(notify_busy_change=False)

        self._sync_timer = ui.timer(0.2, self._sync_controls)
        self._estimate_timer = ui.timer(0.2, self._auto_estimate_duration)

    @property
    def local_busy(self) -> bool:
        return (
            self._ui_operation_active
            or self._estimate_operation_active
            or self._plot_panel_busy
        )

    @property
    def busy(self) -> bool:
        return self.local_busy or self.controller.busy or self._external_busy()

    def _build(self) -> None:
        with ui.card().classes("w-full p-4 gap-4"):
            ui.label("EmStat4S").classes("text-xl font-bold")
            ui.separator()

            with ui.row().classes("w-full items-stretch gap-8 flex-wrap"):
                with (
                    ui.column()
                    .classes("gap-4")
                    .style("flex: 0 1 380px; min-width: min(100%, 380px);")
                ):
                    ui.label("Manual measurement").classes("text-lg font-bold")
                    self.method_editor = EmStatMethodEditor(
                        state=self.state,
                        on_change=self._invalidate_duration_estimate,
                        panel_height_rem=18,
                    )

                    self.measurement_name_input = ui.input(
                        "Measurement name (optional)", value=self.state.measurement_name
                    ).classes("w-full")

                    with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                        ui.label("Estimated total").classes("text-sm text-gray-500")
                        self.estimated_duration_label = ui.label("—").classes(
                            "text-sm font-medium whitespace-nowrap"
                        )
                        ui.space()
                        self.estimate_duration_button = (
                            ui.button(icon="refresh", on_click=self._estimate_duration)
                            .props("flat round dense")
                            .tooltip(
                                "Estimate total measurement duration; includes "
                                "equilibration and excludes connection time"
                            )
                        )

                    with ui.row().classes("w-full gap-3"):
                        self.start_button = ui.button(
                            "Start measurement", on_click=self._start_measurement
                        ).classes("flex-1 whitespace-nowrap")
                        self.abort_button = (
                            ui.button(
                                "Abort",
                                on_click=self._abort_measurement,
                                color="negative",
                            )
                            .props("outline")
                            .classes("w-28 whitespace-nowrap")
                        )

                with (
                    ui.column()
                    .classes("min-w-0 self-stretch")
                    .style("flex: 1 1 620px; min-width: min(100%, 620px);")
                ):
                    self.plot_panel = EmStatPlotPanel(
                        self.controller,
                        state=self.state.plot,
                        on_busy_change=self._set_plot_panel_busy,
                    )

    def _register_state_handlers(self) -> None:
        self.measurement_name_input.on_value_change(self._store_measurement_name)

    def _store_measurement_name(self, event: Any) -> None:
        self.state.measurement_name = str(event.value or "")

    def _set_plot_panel_busy(self, busy: bool) -> None:
        self._plot_panel_busy = busy
        self._sync_controls()

    def _invalidate_duration_estimate(self) -> None:
        self._estimate_dirty = True
        self._estimate_invalidated_at = time.monotonic()
        if hasattr(self, "estimated_duration_label"):
            self.estimated_duration_label.set_text("—")

    def _sync_controls(self, *, notify_busy_change: bool = True) -> None:
        connected = self.controller.connected
        if connected != self._last_connected_state:
            self._last_connected_state = connected
            if connected:
                self._invalidate_duration_estimate()

        busy = self.busy
        self.method_editor.set_enabled(not busy)
        self.measurement_name_input.set_enabled(not busy)
        self.start_button.set_enabled(not busy)
        self.estimate_duration_button.set_enabled(not busy)
        self.plot_panel.set_system_busy(busy)
        self.abort_button.set_enabled(
            self.controller.abort_available and not self._abort_request_active
        )

        local_busy = self.local_busy
        if local_busy != self._last_local_busy_state:
            self._last_local_busy_state = local_busy
            if notify_busy_change:
                self.on_busy_change(local_busy)

    def _read_measurement_name(self) -> str | None:
        value = self.measurement_name_input.value
        if value is None:
            return None
        measurement_name = str(value).strip()
        return measurement_name or None

    @staticmethod
    def _format_estimated_duration(duration_s: float) -> str:
        # Do not ceil PalmSens' sub-second estimate: e.g. 1.01 s is not 2 s.
        rounded_s = round(duration_s, 3)
        hours = int(rounded_s // 3600)
        remainder_s = rounded_s - hours * 3600
        minutes = int(remainder_s // 60)
        seconds = remainder_s - minutes * 60
        seconds_text = f"{seconds:.3f}".rstrip("0").rstrip(".")
        if hours:
            return f"{hours} h {minutes} min {seconds_text} s"
        if minutes:
            return f"{minutes} min {seconds_text} s"
        return f"{seconds_text} s"

    async def _auto_estimate_duration(self) -> None:
        if (
            not self.controller.connected
            or not self._estimate_dirty
            or self.busy
            or time.monotonic() - self._estimate_invalidated_at
            < self.AUTO_ESTIMATE_DELAY_S
        ):
            return
        await self._estimate_duration(notify_errors=False)

    async def _estimate_duration(self, *, notify_errors: bool = True) -> None:
        try:
            settings = self.method_editor.read_settings()
            if isinstance(settings, ChronoamperometrySettings):
                action = partial(
                    self.controller.estimate_chronoamperometry_duration_s,
                    settings=settings,
                )
            elif isinstance(settings, CyclicVoltammetrySettings):
                action = partial(
                    self.controller.estimate_cyclic_voltammetry_duration_s,
                    settings=settings,
                )
            elif isinstance(settings, LinearSweepVoltammetrySettings):
                action = partial(
                    self.controller.estimate_linear_sweep_voltammetry_duration_s,
                    settings=settings,
                )
            else:
                raise TypeError("Unsupported EmStat measurement settings")
        except (TypeError, ValueError) as error:
            if notify_errors:
                ui.notify(str(error), type="negative")
            return

        self._estimate_operation_active = True
        self.estimated_duration_label.set_text("Estimating…")
        self._sync_controls()
        try:
            duration_s = await run.io_bound(action)
            if duration_s is None:
                raise RuntimeError("EmStat duration estimation returned no result")
        except Exception as error:
            self._estimate_dirty = False
            self.estimated_duration_label.set_text("Unavailable")
            logger.error(
                "EmStat duration estimation failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            if notify_errors:
                ui.notify(str(error), type="negative")
        else:
            self._estimate_dirty = False
            self.estimated_duration_label.set_text(
                self._format_estimated_duration(duration_s)
            )
        finally:
            self._estimate_operation_active = False
            self._sync_controls()

    async def _start_measurement(self) -> None:
        try:
            measurement_type = self.method_editor.measurement_type
            measurement_name = self._read_measurement_name()
            settings = self.method_editor.read_settings()
            if isinstance(settings, ChronoamperometrySettings):
                action = partial(
                    self.controller.measure_chronoamperometry,
                    settings=settings,
                    measurement_name=measurement_name,
                )
            elif isinstance(settings, CyclicVoltammetrySettings):
                action = partial(
                    self.controller.measure_cyclic_voltammetry,
                    settings=settings,
                    measurement_name=measurement_name,
                )
            elif isinstance(settings, LinearSweepVoltammetrySettings):
                action = partial(
                    self.controller.measure_linear_sweep_voltammetry,
                    settings=settings,
                    measurement_name=measurement_name,
                )
            else:
                raise TypeError("Unsupported EmStat measurement settings")
        except (TypeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return

        measurement_label = EmStatMethodEditor.METHOD_LABELS[measurement_type]
        self.plot_panel.show_live()
        self._ui_operation_active = True
        self._sync_controls()

        try:
            measurement = await run.io_bound(action)
            if measurement is None:
                raise RuntimeError(f"{measurement_label} returned no result")
        except MeasurementAbortedError:
            ui.notify(f"{measurement_label} aborted", type="warning")
        except Exception as error:
            logger.error(
                "Manual %s failed: %s",
                measurement_type.replace("_", " "),
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
        else:
            self.plot_panel.refresh_saved_measurement_options()
            ui.notify(f"{measurement_label} completed", type="positive")
        finally:
            self._ui_operation_active = False
            self.plot_panel.refresh()
            self._sync_controls()

    async def _abort_measurement(self) -> None:
        self._abort_request_active = True
        self._sync_controls()
        try:
            requested = await run.io_bound(self.controller.abort_measurement)
        except Exception as error:
            logger.error(
                "EmStat abort request failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
        else:
            if not requested:
                logger.warning(
                    "Manual EmStat abort requested while abort was unavailable"
                )
                ui.notify("Measurement is not currently abortable", type="warning")
        finally:
            self._abort_request_active = False
            self._sync_controls()

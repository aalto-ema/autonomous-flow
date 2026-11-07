from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from nicegui import run, ui

from controllers.emstat_controller import EmStatController
from gui.emstat_plot import EmStatPlot

logger = logging.getLogger(__name__)


@dataclass
class EmStatPlotPanelState:
    plot_source_file_name: str | None = None


class EmStatPlotPanel:
    """Reusable live plot and saved-session browser for EmStat measurements."""

    LIVE_PLOT_SOURCE = "__live__"

    def __init__(
        self,
        controller: EmStatController,
        *,
        state: EmStatPlotPanelState | None = None,
        on_busy_change: Callable[[bool], None] | None = None,
        external_busy: Callable[[], bool] | None = None,
    ) -> None:
        self.controller = controller
        self.state = state if state is not None else EmStatPlotPanelState()
        self.on_busy_change = on_busy_change or (lambda _: None)
        self._external_busy = external_busy or (lambda: False)

        self._system_busy = False
        self._load_operation_active = False
        self._last_local_busy_state = self.local_busy

        self._build()
        self._register_state_handlers()
        self.refresh_saved_measurement_options()
        self._sync_controls(notify_busy_change=False)

    @property
    def local_busy(self) -> bool:
        return self._load_operation_active

    @property
    def busy(self) -> bool:
        return (
            self.local_busy
            or self._system_busy
            or self.controller.busy
            or self._external_busy()
        )

    def _build(self) -> None:
        with ui.row().classes("w-full items-stretch gap-4 flex-wrap"):
            with (
                ui.column()
                .classes("min-w-0 self-stretch")
                .style("flex: 1 1 620px; min-width: min(100%, 620px);")
            ):
                self.plot = EmStatPlot(self.controller)

            with (
                ui.column()
                .classes("gap-3")
                .style("flex: 0 1 18rem; min-width: min(100%, 18rem);")
            ):
                ui.label("Plot data").classes("text-lg font-bold")
                self.plot_source_select = ui.select(
                    options={self.LIVE_PLOT_SOURCE: "Live measurement"},
                    label="Measurement",
                    value=self.LIVE_PLOT_SOURCE,
                ).classes("w-full")

    def _register_state_handlers(self) -> None:
        self.plot_source_select.on_value_change(self._plot_source_changed)

    def set_system_busy(self, busy: bool) -> None:
        self._system_busy = busy
        self._sync_controls()

    def show_live(self) -> None:
        """Select and display the current live controller measurement."""

        self.plot_source_select.set_value(self.LIVE_PLOT_SOURCE)
        self.state.plot_source_file_name = None
        self.plot.show_live()

    def refresh(self) -> None:
        self.plot.refresh()

    def refresh_saved_measurement_options(self) -> None:
        try:
            saved_measurements = self.controller.list_saved_measurements()
        except Exception as error:
            logger.error(
                "Failed to list saved EmStat measurements: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
            return

        options = {self.LIVE_PLOT_SOURCE: "Live measurement"}
        options.update(
            {
                measurement.file_name: measurement.display_label
                for measurement in saved_measurements
            }
        )

        current_value = self.plot_source_select.value
        persisted_value = self.state.plot_source_file_name
        preferred_value = (
            persisted_value
            if persisted_value is not None and persisted_value in options
            else current_value
        )
        selected_value = (
            preferred_value
            if preferred_value is not None and preferred_value in options
            else self.LIVE_PLOT_SOURCE
        )
        self.plot_source_select.set_options(options, value=selected_value)
        if selected_value == self.LIVE_PLOT_SOURCE:
            self.state.plot_source_file_name = None

    async def _plot_source_changed(self, event: Any) -> None:
        value = event.value
        self.state.plot_source_file_name = (
            value if isinstance(value, str) and value != self.LIVE_PLOT_SOURCE else None
        )
        await self._show_plot_source(value)

    async def _show_plot_source(self, value: object) -> None:
        if value == self.LIVE_PLOT_SOURCE:
            self.plot.show_live()
            return
        if not isinstance(value, str) or not value:
            return

        self._set_load_operation_active(True)
        try:
            measurement = await run.io_bound(
                partial(self.controller.load_saved_measurement, file_name=value)
            )
            if measurement is None:
                raise RuntimeError("Saved measurement loader returned no result")
        except Exception as error:
            logger.error(
                "Failed to load saved EmStat measurement %s: %s",
                value,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
        else:
            self.state.plot_source_file_name = value
            self.plot.show_saved_measurement(measurement)
        finally:
            self._set_load_operation_active(False)

    def _set_load_operation_active(self, active: bool) -> None:
        self._load_operation_active = active
        self._sync_controls()

    def _sync_controls(self, *, notify_busy_change: bool = True) -> None:
        local_busy = self.local_busy
        if local_busy != self._last_local_busy_state:
            self._last_local_busy_state = local_busy
            if notify_busy_change:
                self.on_busy_change(local_busy)

        enabled = not self.busy
        self.plot_source_select.set_enabled(enabled)

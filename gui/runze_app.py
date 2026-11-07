from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nicegui import ui

from controllers.emstat_controller import EmStatController
from controllers.runze_controller import RunzeController, RunzeSnapshot
from gui.emstat_panel import EmStatPanelState
from gui.experiment_workspace import ExperimentWorkspace, ExperimentWorkspaceState
from gui.runze_control_bar import EmergencyStopResult, RunzeControlBar
from gui.runze_manual_page import RunzeManualPage, RunzeManualPageState
from gui.runze_procedures_page import RunzeProceduresPage, RunzeProceduresPageState

if TYPE_CHECKING:
    from controllers.experiment_controller import ExperimentController

logger = logging.getLogger(__name__)


@runtime_checkable
class RunzeBusyView(Protocol):
    """Optional interface for pages containing independent Runze controls."""

    def set_busy(self, busy: bool) -> None: ...


@runtime_checkable
class SystemBusyView(Protocol):
    """Optional interface for pages which coordinate both instruments."""

    def set_system_busy(self, busy: bool) -> None: ...


@runtime_checkable
class RunzeStatusView(Protocol):
    def show_snapshot(self, snapshot: RunzeSnapshot) -> None: ...

    def show_disconnected(self) -> None: ...

    def show_status_unknown(self) -> None: ...


@runtime_checkable
class ExperimentPlayView(Protocol):
    async def resume_experiment_play(self) -> bool: ...

    def sync_experiment_play_state(self) -> None: ...


class RunzeApp:
    def __init__(
        self,
        runze_controller: RunzeController,
        emstat_controller: EmStatController,
        experiment_controller: ExperimentController | None = None,
    ) -> None:
        self.runze_controller = runze_controller
        self.emstat_controller = emstat_controller
        self.experiment_controller = experiment_controller

        self._active_view: RunzeBusyView | SystemBusyView | None = None
        self._active_path = "/"

        self._manual_page_state = RunzeManualPageState()
        self._procedures_page_state = RunzeProceduresPageState()
        self._emstat_panel_state = EmStatPanelState()
        self._experiment_workspace_state = ExperimentWorkspaceState()

        self._build()

    @property
    def busy(self) -> bool:
        return self.control_bar.system_busy

    # UI construction ---

    def _build(self) -> None:
        self._build_page_header()

        self.control_bar = RunzeControlBar(
            self.runze_controller,
            self.emstat_controller,
            experiment_controller=self.experiment_controller,
            on_snapshot=self._show_snapshot,
            on_disconnected=self._show_disconnected,
            on_status_unknown=self._show_status_unknown,
            on_runze_busy_change=self._set_runze_busy,
            on_system_busy_change=self._set_system_busy,
            on_sequence_state_change=self._sync_experiment_play_state,
            resume_experiment_play=self._resume_experiment_play,
            emergency_stop_available=self._emergency_stop_available,
            emergency_stop_action=self._emergency_stop_active,
        )

        pages = {
            "/": self._build_manual_page,
            "/procedures": self._build_procedures_page,
        }
        if self.experiment_controller is not None:
            pages["/experiment"] = self._build_experiment_page

        ui.sub_pages(pages).classes("w-full").style("width: 100%")

    def _build_page_header(self) -> None:
        with ui.row().classes("w-full items-center"):
            self.page_title = ui.label("Manual Control").classes("text-2xl font-bold")

            ui.space()

            self.manual_page_button = ui.button(
                "Manual control", on_click=lambda: self._navigate_to("/")
            ).props("outline")

            self.procedures_page_button = ui.button(
                "Procedures", on_click=lambda: self._navigate_to("/procedures")
            ).props("outline")

            self.experiment_page_button = ui.button(
                "Experiment", on_click=lambda: self._navigate_to("/experiment")
            ).props("outline")

    def _build_manual_page(self) -> None:
        self._active_path = "/"
        self._sync_page_header()

        self._activate_view(
            RunzeManualPage(
                self.runze_controller,
                self.control_bar,
                self.emstat_controller,
                state=self._manual_page_state,
                emstat_panel_state=self._emstat_panel_state,
                on_emstat_panel_busy_change=self._set_emstat_panel_local_busy,
            )
        )

    def _build_procedures_page(self) -> None:
        self._active_path = "/procedures"
        self._sync_page_header()

        self._activate_view(
            RunzeProceduresPage(
                self.runze_controller,
                self.control_bar,
                state=self._procedures_page_state,
            )
        )

    def _build_experiment_page(self) -> None:
        self._active_path = "/experiment"
        self._sync_page_header()

        if self.experiment_controller is None:
            ui.label("Experiment workspace is unavailable").classes("text-lg font-bold")
            return

        self._activate_view(
            ExperimentWorkspace(
                self.experiment_controller,
                state=self._experiment_workspace_state,
                on_ui_operation_change=self._set_experiment_ui_operation,
            )
        )

    # Active-view coordination ---

    def _activate_view(self, view: RunzeBusyView | SystemBusyView) -> None:
        self._active_view = view

        if isinstance(view, RunzeStatusView):
            snapshot = self.runze_controller.last_snapshot

            if not self.runze_controller.connected:
                view.show_disconnected()
            elif not self.runze_controller.status_known or snapshot is None:
                view.show_status_unknown()
            else:
                view.show_snapshot(snapshot)

        if isinstance(view, RunzeBusyView):
            view.set_busy(self.control_bar.runze_busy)

        if isinstance(view, SystemBusyView):
            view.set_system_busy(self.control_bar.system_busy)

    def _show_snapshot(self, snapshot: RunzeSnapshot) -> None:
        if isinstance(self._active_view, RunzeStatusView):
            self._active_view.show_snapshot(snapshot)

    def _show_disconnected(self) -> None:
        if isinstance(self._active_view, RunzeStatusView):
            self._active_view.show_disconnected()

    def _show_status_unknown(self) -> None:
        if isinstance(self._active_view, RunzeStatusView):
            self._active_view.show_status_unknown()

    def _set_runze_busy(self, busy: bool) -> None:
        if isinstance(self._active_view, RunzeBusyView):
            self._active_view.set_busy(busy)

        self._sync_page_header()

    def _set_system_busy(self, busy: bool) -> None:
        if isinstance(self._active_view, SystemBusyView):
            self._active_view.set_system_busy(busy)

        self._sync_page_header()

    def _set_emstat_panel_local_busy(self, busy: bool) -> None:
        self.control_bar.set_emstat_panel_local_busy(busy)

    def _set_experiment_ui_operation(self, operation: str | None) -> None:
        self.control_bar.set_experiment_ui_operation(operation)

    async def _resume_experiment_play(self) -> bool:
        if not isinstance(self._active_view, ExperimentPlayView):
            return False
        return await self._active_view.resume_experiment_play()

    def _sync_experiment_play_state(self) -> None:
        if isinstance(self._active_view, ExperimentPlayView):
            self._active_view.sync_experiment_play_state()
        self._sync_page_header()

    # Emergency stop coordination ---

    def _emergency_stop_available(self) -> bool:
        runze_stop_available = (
            self.runze_controller.connected
            and self.runze_controller.busy
            and self.runze_controller.active_target is not None
        )

        return runze_stop_available or self.emstat_controller.abort_available

    def _emergency_stop_active(self) -> EmergencyStopResult:
        if self.experiment_controller is not None and self.experiment_controller.busy:
            result = self.experiment_controller.emergency_stop()
            return EmergencyStopResult(
                runze_stop_requested=result.runze_stop_requested,
                emstat_abort_requested=result.emstat_abort_requested,
                errors=result.errors,
            )

        runze_stop_requested = False
        emstat_abort_requested = False
        errors: list[str] = []

        runze_stop_available = (
            self.runze_controller.connected
            and self.runze_controller.busy
            and self.runze_controller.active_target is not None
        )

        if runze_stop_available:
            try:
                runze_stop_requested = self.runze_controller.emergency_stop_active()
            except Exception as error:
                logger.error(
                    "Runze emergency stop failed: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                errors.append(f"Runze emergency stop failed: {error}")

        if self.emstat_controller.abort_available:
            try:
                emstat_abort_requested = self.emstat_controller.abort_measurement()
            except Exception as error:
                logger.error(
                    "EmStat measurement abort failed: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                errors.append(f"EmStat measurement abort failed: {error}")

        return EmergencyStopResult(
            runze_stop_requested=runze_stop_requested,
            emstat_abort_requested=emstat_abort_requested,
            errors=tuple(errors),
        )

    # Navigation ---

    def _navigate_to(self, path: str) -> None:
        if self.busy or (self._experiment_play_active and path != "/experiment"):
            ui.notify(
                (
                    "Return to the Experiment workspace to resume or discard Play"
                    if self._experiment_play_active and not self.busy
                    else "Wait for the current operation to finish before changing page"
                ),
                type="warning",
            )
            return

        ui.navigate.to(path)

    def _sync_page_header(self) -> None:
        titles = {
            "/": "Manual Control",
            "/procedures": "Runze Procedures",
            "/experiment": "Experiment Workspace",
        }
        self.page_title.set_text(titles[self._active_path])

        self.manual_page_button.set_visibility(self._active_path != "/")
        self.procedures_page_button.set_visibility(self._active_path != "/procedures")
        self.experiment_page_button.set_visibility(
            self.experiment_controller is not None
            and self._active_path != "/experiment"
        )

        navigation_locked = self._navigation_locked
        self.manual_page_button.set_enabled(not navigation_locked)
        self.procedures_page_button.set_enabled(not navigation_locked)
        self.experiment_page_button.set_enabled(not self.busy)

    @property
    def _experiment_play_active(self) -> bool:
        return (
            self.experiment_controller is not None
            and self.experiment_controller.snapshot.manual_play_state != "inactive"
        )

    @property
    def _navigation_locked(self) -> bool:
        return self.busy or self._experiment_play_active

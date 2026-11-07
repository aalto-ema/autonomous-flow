from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from nicegui import run, ui

from controllers.emstat_controller import EmStatController
from controllers.runze_controller import (
    OperationStoppedError,
    RunzeController,
    RunzeSnapshot,
    SequenceStoppedError,
)

if TYPE_CHECKING:
    from controllers.experiment_controller import ExperimentController

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmergencyStopResult:
    runze_stop_requested: bool
    emstat_abort_requested: bool
    errors: tuple[str, ...] = ()


def require_result[T](result: T | None, *, operation: str) -> T:
    if result is None:
        raise RuntimeError(f"{operation} returned no result")

    return result


def report_error(context: str, error: Exception) -> None:
    logger.error(
        "%s failed: %s",
        context,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    ui.notify(str(error), type="negative")


class RunzeControlBar:
    """Global device controls shared by every application page.

    The historical class name is retained for now to avoid a broad rename while
    the experiment pages are still being designed.
    """

    def __init__(
        self,
        controller: RunzeController,
        emstat_controller: EmStatController,
        *,
        experiment_controller: ExperimentController | None = None,
        on_snapshot: Callable[[RunzeSnapshot], None],
        on_disconnected: Callable[[], None],
        on_status_unknown: Callable[[], None],
        on_runze_busy_change: Callable[[bool], None],
        on_system_busy_change: Callable[[bool], None],
        on_sequence_state_change: Callable[[], None],
        resume_experiment_play: Callable[[], Awaitable[bool]],
        emergency_stop_available: Callable[[], bool],
        emergency_stop_action: Callable[[], EmergencyStopResult],
    ) -> None:
        self.controller = controller
        self.emstat_controller = emstat_controller
        self.experiment_controller = experiment_controller
        self.on_snapshot = on_snapshot
        self.on_disconnected = on_disconnected
        self.on_status_unknown = on_status_unknown
        self.on_runze_busy_change = on_runze_busy_change
        self.on_system_busy_change = on_system_busy_change
        self.on_sequence_state_change = on_sequence_state_change
        self.resume_experiment_play = resume_experiment_play
        self.emergency_stop_available = emergency_stop_available
        self.emergency_stop_action = emergency_stop_action

        self._ui_operation: str | None = None
        self._emstat_ui_operation: str | None = None
        self._experiment_ui_operation: str | None = None
        self._emstat_panel_local_busy = False
        self._displayed_snapshot: RunzeSnapshot | None = controller.last_snapshot
        self._last_runze_busy_state = self.runze_busy
        self._last_system_busy_state = self.system_busy
        self._last_experiment_play_state = self._experiment_play_state
        self._emergency_stop_requested = False

        self._build_reset_syringe_dialog()
        self._build()
        self._sync_controls(notify_busy_change=False)

        self._sync_timer = ui.timer(0.2, self._sync_controls)

    # UI construction ---

    def _build_reset_syringe_dialog(self) -> None:
        with ui.dialog() as self.reset_syringe_dialog, ui.card():
            ui.label("Reset syringe?").classes("text-lg font-bold")

            ui.label(
                "Reset moves the syringe to position 0. "
                "Ensure that the fluid path is open and safe."
            ).classes("max-w-md")

            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel", on_click=self.reset_syringe_dialog.close)
                ui.button(
                    "Reset syringe", on_click=self._reset_syringe, color="negative"
                )

    def _build(self) -> None:
        with ui.card().classes("sticky top-0 z-50 w-full p-4 gap-2"):
            with ui.row().classes("w-full items-center gap-x-8 gap-y-2 flex-wrap"):
                with ui.row().classes("items-center gap-x-8 gap-y-2 flex-wrap"):
                    with ui.row().classes("items-center gap-3 flex-nowrap"):
                        with ui.row().classes("items-center gap-2 whitespace-nowrap"):
                            ui.label("Runze:").classes("font-medium")
                            self.connection_status = ui.label("Disconnected").classes(
                                "font-medium min-w-24 whitespace-nowrap"
                            )

                            self.status_refresh_button = (
                                ui.button(
                                    icon="refresh", on_click=self._refresh_runze_status
                                )
                                .props("flat round dense")
                                .tooltip("Retry Runze status query")
                            )

                            self.status_refresh_button.set_visibility(False)

                        from serial.tools.list_ports import comports

                        self.port_input = (
                            ui.input(
                                "Serial port", placeholder="COM5 or /dev/cu.usbserial…"
                            )
                            .props("dense")
                            .classes("w-56")
                        )
                        ports = [port.device for port in comports()]
                        if ports:
                            self.port_input.set_value(ports[0])

                        self.trace_checkbox = ui.checkbox("Trace", value=False)

                        self.connection_button = ui.button(
                            "Connect", on_click=self._toggle_runze_connection
                        ).classes("w-32 shrink-0 whitespace-nowrap")

                    with ui.row().classes("items-center gap-3 flex-nowrap"):
                        ui.label("EmStat4S:").classes("font-medium")
                        self.emstat_connection_status = ui.label(
                            "Disconnected"
                        ).classes("font-medium min-w-24 whitespace-nowrap")

                        self.emstat_connection_button = ui.button(
                            "Connect", on_click=self._toggle_emstat_connection
                        ).classes("w-32 shrink-0 whitespace-nowrap")

                ui.space()

                with ui.row().classes("items-center gap-3 flex-nowrap"):
                    with ui.row().classes("items-center gap-2 whitespace-nowrap"):
                        ui.label("Operation:").classes("text-sm text-gray-500")
                        self.operation_status = ui.label("Disconnected").classes(
                            "font-medium"
                        )

                    with ui.element("div").classes("w-40 shrink-0"):
                        self.stop_sequence_button = (
                            ui.button(
                                "Pause sequence",
                                on_click=self._control_sequence,
                                color="warning",
                            )
                            .props("outline")
                            .classes("w-full whitespace-nowrap")
                        )

                        with (
                            (
                                ui.dropdown_button(
                                    "Resume",
                                    on_click=self._control_sequence,
                                    color="warning",
                                    split=True,
                                    auto_close=True,
                                )
                                .props("outline")
                                .classes("w-full whitespace-nowrap")
                            ) as self.paused_sequence_button,
                            ui.item(on_click=self._discard_sequence).classes(
                                "text-negative"
                            ) as self.discard_sequence_item,
                        ):
                            self.discard_sequence_label = ui.item_label(
                                "Discard sequence"
                            )

                        self.paused_sequence_button.set_visibility(False)

                    self.emergency_stop_button = ui.button(
                        "EMERGENCY STOP",
                        on_click=self._emergency_stop_active,
                        color="negative",
                    ).classes("whitespace-nowrap")

            with ui.column().classes("w-full gap-2") as self.recovery_section:
                ui.separator()

                ui.label("Recovery required").classes("text-sm font-medium")

                with ui.row().classes(
                    "w-full items-center gap-3 flex-wrap"
                ) as self.syringe_recovery_row:
                    ui.label(
                        "SY01B syringe position is unknown — reset required"
                    ).classes("text-sm")

                    ui.space()

                    self.reset_syringe_button = (
                        ui.button(
                            "Reset syringe",
                            on_click=self.reset_syringe_dialog.open,
                            color="warning",
                        )
                        .props("outline")
                        .classes("whitespace-nowrap")
                    )

                with ui.row().classes(
                    "w-full items-center gap-3 flex-wrap"
                ) as self.selector_recovery_row:
                    ui.label(
                        "SV07M is not aligned with a valid port — home required"
                    ).classes("text-sm")

                    ui.space()

                    self.selector_home_button = (
                        ui.button(
                            "Reset selector to home",
                            on_click=self._reset_selector_to_home,
                            color="warning",
                        )
                        .props("outline")
                        .classes("whitespace-nowrap")
                    )

    # Public display and enabled-state helpers ---

    @property
    def runze_busy(self) -> bool:
        """Whether controls which use Runze must be disabled."""

        return (
            self._ui_operation is not None
            or self.controller.busy
            or self.experiment_busy
        )

    @property
    def emstat_busy(self) -> bool:
        """Whether controls which use EmStat must be disabled."""

        return (
            self._emstat_ui_operation is not None
            or self.emstat_controller.busy
            or self.experiment_busy
        )

    @property
    def experiment_busy(self) -> bool:
        return self._experiment_ui_operation is not None or (
            self.experiment_controller is not None and self.experiment_controller.busy
        )

    @property
    def system_busy(self) -> bool:
        """Whether navigation or a whole-system action must be blocked."""

        return self.runze_busy or self.emstat_busy or self._emstat_panel_local_busy

    @property
    def _experiment_play_state(self) -> str:
        if self.experiment_controller is None:
            return "inactive"
        return self.experiment_controller.snapshot.manual_play_state

    def show_snapshot(self, snapshot: RunzeSnapshot) -> None:
        self._displayed_snapshot = snapshot
        self.connection_status.set_text("Connected")
        self.on_snapshot(snapshot)

    def show_disconnected(self) -> None:
        self._displayed_snapshot = None
        self.connection_status.set_text("Disconnected")
        self.operation_status.set_text("Disconnected")
        self.on_disconnected()

    def show_status_unknown(self) -> None:
        self._displayed_snapshot = None
        self.connection_status.set_text("Connected — status unknown")
        self.operation_status.set_text("Status unknown")
        self.on_status_unknown()

    def set_busy(self, *, operation: str) -> None:
        self._ui_operation = operation
        self._sync_controls()

    def set_idle(self) -> None:
        self._ui_operation = None
        self._sync_controls()

    def set_emstat_panel_local_busy(self, busy: bool) -> None:
        """Report a local EmStat-panel transition to the global coordinator."""

        if self._emstat_panel_local_busy == busy:
            return

        self._emstat_panel_local_busy = busy
        self._sync_controls()

    def set_experiment_ui_operation(self, operation: str | None) -> None:
        """Report an experiment-workspace transition to the global coordinator."""

        if self._experiment_ui_operation == operation:
            return

        self._experiment_ui_operation = operation
        self._sync_controls()

    # Display and enabled-state helpers ---

    def _sync_controls(self, *, notify_busy_change: bool = True) -> None:
        connected = self.controller.connected
        emstat_connected = self.emstat_controller.connected
        controller_snapshot = self.controller.last_snapshot
        status_known = self.controller.status_known and controller_snapshot is not None
        status_unknown = connected and not status_known
        busy = self.runze_busy
        runze_busy = self.runze_busy
        emstat_busy = self.emstat_busy or self._emstat_panel_local_busy
        sequence_control_state = self.controller.sequence_control_state
        play_state = self._experiment_play_state
        play_active = play_state != "inactive"
        emergency_stop_available = self.emergency_stop_available()

        if connected:
            if controller_snapshot is None:
                if self._displayed_snapshot is not None:
                    self.show_status_unknown()
            elif controller_snapshot is not self._displayed_snapshot:
                self.show_snapshot(controller_snapshot)

        if not emergency_stop_available:
            self._emergency_stop_requested = False

        self.port_input.set_enabled(not connected and not runze_busy)
        self.trace_checkbox.set_enabled(not connected and not runze_busy)
        self.status_refresh_button.set_visibility(status_unknown)
        self.status_refresh_button.set_enabled(status_unknown and not runze_busy)
        self.connection_button.set_text("Disconnect" if connected else "Connect")
        self.connection_button.set_enabled(not runze_busy)

        self.emstat_connection_button.set_text(
            "Disconnect" if emstat_connected else "Connect"
        )
        self.emstat_connection_button.set_enabled(not emstat_busy)

        if play_state == "running":
            sequence_button_text = "Pause after stage"
        elif play_state == "pause_requested":
            sequence_button_text = "Cancel pause"
        elif play_state == "paused":
            sequence_button_text = "Resume Play"
        else:
            sequence_button_text = {
                "unavailable": "Pause sequence",
                "stop": "Pause sequence",
                "cancel_stop": "Cancel stop",
                "resume": "Resume",
            }[sequence_control_state]
        self.stop_sequence_button.set_text(sequence_button_text)
        self.paused_sequence_button.set_text("Resume Play" if play_active else "Resume")
        self.discard_sequence_label.set_text(
            "Discard Play" if play_active else "Discard sequence"
        )
        experiment_measuring = (
            self.experiment_controller is not None
            and self.experiment_controller.snapshot.state == "measuring"
        )
        pause_blocked_by_measurement = (
            not play_active
            and experiment_measuring
            and sequence_control_state == "stop"
        )
        if play_active:
            sequence_control_enabled = play_state in {
                "running",
                "pause_requested",
                "paused",
            }
            sequence_paused = play_state == "paused"
        else:
            sequence_control_enabled = (
                connected
                and sequence_control_state != "unavailable"
                and not pause_blocked_by_measurement
            )
            sequence_paused = sequence_control_state == "resume"

        self.stop_sequence_button.set_visibility(not sequence_paused)
        self.stop_sequence_button.set_enabled(sequence_control_enabled)
        self.paused_sequence_button.set_visibility(sequence_paused)
        self.paused_sequence_button.set_enabled(sequence_control_enabled)
        self.discard_sequence_item.set_enabled(
            sequence_paused and (play_active or connected)
        )

        self.emergency_stop_button.set_enabled(
            emergency_stop_available and not self._emergency_stop_requested
        )

        syringe_reset_required = (
            connected
            and status_known
            and self.controller.syringe_state == "reset_required"
        )
        selector_home_required = (
            connected
            and status_known
            and self.controller.selector_state == "home_required"
        )

        self.recovery_section.set_visibility(
            syringe_reset_required or selector_home_required
        )

        self.syringe_recovery_row.set_visibility(syringe_reset_required)
        self.reset_syringe_button.set_enabled(syringe_reset_required and not busy)

        self.selector_recovery_row.set_visibility(selector_home_required)
        self.selector_home_button.set_enabled(selector_home_required and not busy)

        if not connected:
            self.connection_status.set_text("Disconnected")
        elif status_known:
            self.connection_status.set_text("Connected")
        else:
            self.connection_status.set_text("Connected — status unknown")

        self.emstat_connection_status.set_text(
            "Connected" if emstat_connected else "Disconnected"
        )

        displayed_operation = self._displayed_operation()

        if displayed_operation is not None and sequence_paused:
            self.operation_status.set_text(f"{displayed_operation} — paused")
        elif displayed_operation is not None and (
            play_state == "pause_requested"
            or (not play_active and sequence_control_state == "cancel_stop")
        ):
            self.operation_status.set_text(f"{displayed_operation} — pause requested")
        elif displayed_operation is not None:
            self.operation_status.set_text(displayed_operation)
        elif play_state == "paused" and self.experiment_controller is not None:
            next_stage = self.experiment_controller.snapshot.manual_play_next_stage
            stage_label = "next stage" if next_stage is None else next_stage
            self.operation_status.set_text(f"Play paused before {stage_label}")
        elif play_state == "pause_requested":
            self.operation_status.set_text("Play pause requested")
        elif play_state == "running":
            self.operation_status.set_text("Play running")
        elif connected and not status_known:
            self.operation_status.set_text("Status unknown")
        else:
            any_connected = connected or emstat_connected
            self.operation_status.set_text("Idle" if any_connected else "Disconnected")

        if runze_busy != self._last_runze_busy_state:
            self._last_runze_busy_state = runze_busy

            if notify_busy_change:
                self.on_runze_busy_change(runze_busy)

        system_busy = self.system_busy
        if system_busy != self._last_system_busy_state:
            self._last_system_busy_state = system_busy

            if notify_busy_change:
                self.on_system_busy_change(system_busy)

        if play_state != self._last_experiment_play_state:
            self._last_experiment_play_state = play_state
            if notify_busy_change:
                self.on_sequence_state_change()

    def _displayed_operation(self) -> str | None:
        if self._experiment_ui_operation is not None:
            return self._experiment_ui_operation

        if self.experiment_controller is not None and self.experiment_controller.busy:
            snapshot = self.experiment_controller.snapshot
            return snapshot.operation or snapshot.state.replace("_", " ").capitalize()

        operations: list[str] = []
        runze_operation = self._ui_operation or self.controller.active_operation
        if runze_operation is not None:
            operations.append(runze_operation)

        if self._emstat_ui_operation is not None:
            operations.append(self._emstat_ui_operation)
        elif self.emstat_controller.busy:
            emstat_state = self.emstat_controller.snapshot.state.replace("_", " ")
            operations.append(f"EmStat4S {emstat_state}")

        return " · ".join(operations) if operations else None

    # Async UI callbacks ---

    async def _toggle_runze_connection(self) -> None:
        if self.controller.connected:
            await self._disconnect()
        else:
            await self._connect()

    async def _toggle_emstat_connection(self) -> None:
        if self.emstat_controller.connected:
            await self._disconnect_emstat()
        else:
            await self._connect_emstat()

    async def _connect(self) -> None:
        port = str(self.port_input.value or "").strip()
        if not port:
            ui.notify("Choose or enter the instrument serial port", type="negative")
            return

        self.set_busy(operation="Connecting")

        try:
            snapshot = require_result(
                await run.io_bound(
                    partial(
                        self.controller.connect,
                        port=port,
                        trace=bool(self.trace_checkbox.value),
                    )
                ),
                operation="Connect",
            )
        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            self.show_disconnected()
            report_error("Connect", error)
        else:
            self.show_snapshot(snapshot)
            ui.notify("Connected", type="positive")

        finally:
            self.set_idle()

    async def _disconnect(self) -> None:
        if not self.controller.connected:
            ui.notify("Runze devices are not connected", type="warning")
            return

        self.set_busy(operation="Disconnecting")

        try:
            await run.io_bound(self.controller.disconnect)
        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            if self.controller.connected:
                self.show_status_unknown()
            else:
                self.show_disconnected()
            report_error("Disconnect", error)
        else:
            self.show_disconnected()
            ui.notify("Disconnected", type="positive")
        finally:
            self.set_idle()

    async def _connect_emstat(self) -> None:
        self._emstat_ui_operation = "Connecting EmStat4S"
        self._sync_controls()

        try:
            connected = await run.io_bound(self.emstat_controller.connect)
        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            report_error("EmStat4S connection", error)
        else:
            if connected:
                ui.notify("EmStat4S connected", type="positive")
            else:
                ui.notify("EmStat4S is already connected", type="warning")
        finally:
            self._emstat_ui_operation = None
            self._sync_controls()

    async def _disconnect_emstat(self) -> None:
        self._emstat_ui_operation = "Disconnecting EmStat4S"
        self._sync_controls()

        try:
            disconnected = await run.io_bound(self.emstat_controller.disconnect)
        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            report_error("EmStat4S disconnection", error)
        else:
            if disconnected:
                ui.notify("EmStat4S disconnected", type="positive")
            else:
                ui.notify("EmStat4S is not persistently connected", type="warning")
        finally:
            self._emstat_ui_operation = None
            self._sync_controls()

    async def _refresh_runze_status(self) -> None:
        if not self.controller.connected:
            ui.notify("Runze devices are not connected", type="warning")
            return

        self.set_busy(operation="Refreshing Runze status")

        try:
            await self._refresh_status_or_unknown(operation="Runze status refresh")
        finally:
            self.set_idle()

    async def _refresh_status_or_unknown(self, *, operation: str) -> None:
        try:
            snapshot = require_result(
                await run.io_bound(self.controller.refresh_status), operation=operation
            )
        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            self.show_status_unknown()
            report_error(operation, error)
        else:
            self.show_snapshot(snapshot)

    async def run_operation(
        self,
        *,
        action: Callable[[], RunzeSnapshot],
        operation: str,
        success_message: str,
    ) -> None:
        """Run a controller operation which returns a Runze snapshot."""

        self.set_busy(operation=operation)

        try:
            snapshot = require_result(await run.io_bound(action), operation=operation)
        except (OperationStoppedError, SequenceStoppedError) as error:
            ui.notify(str(error), type="warning")
            await self._refresh_status_or_unknown(operation="Status refresh after stop")

        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            report_error(operation, error)
            await self._refresh_status_or_unknown(
                operation="Status refresh after operation failure"
            )

        else:
            self.show_snapshot(snapshot)
            ui.notify(success_message, type="positive")

        finally:
            self.set_idle()

    async def _control_sequence(self) -> None:
        self.stop_sequence_button.set_enabled(False)
        self.paused_sequence_button.set_enabled(False)
        play_state = self._experiment_play_state
        control_state = self.controller.sequence_control_state

        try:
            if play_state != "inactive":
                experiment_controller = self.experiment_controller
                if experiment_controller is None:
                    raise RuntimeError("Experiment Play control is unavailable")
                if play_state == "running":
                    changed = await run.io_bound(
                        experiment_controller.request_manual_play_pause
                    )
                    success_message = "Play will pause after the current stage"
                    unavailable_message = "Play pause is not available"
                elif play_state == "pause_requested":
                    changed = await run.io_bound(
                        experiment_controller.cancel_manual_play_pause
                    )
                    success_message = "Play pause request cancelled"
                    unavailable_message = "Play has already paused"
                elif play_state == "paused":
                    changed = await self.resume_experiment_play()
                    success_message = "Play resumed"
                    unavailable_message = "Play resume is not available"
                else:
                    changed = False
                    success_message = ""
                    unavailable_message = "Play control is not available"

            elif control_state == "stop":
                changed = require_result(
                    await run.io_bound(self.controller.request_sequence_stop),
                    operation="Sequence pause request",
                )
                success_message = "Sequence will pause after the current step"
                unavailable_message = "Sequence pause is not available"

            elif control_state == "cancel_stop":
                changed = require_result(
                    await run.io_bound(self.controller.cancel_sequence_stop),
                    operation="Sequence pause cancellation",
                )
                success_message = "Sequence pause request cancelled"
                unavailable_message = "Sequence has already paused"

            elif control_state == "resume":
                changed = require_result(
                    await run.io_bound(self.controller.resume_sequence),
                    operation="Sequence resume",
                )
                success_message = "Sequence resumed"
                unavailable_message = "Sequence resume is not available"

            else:
                changed = False
                success_message = ""
                unavailable_message = "Sequence control is not available"

        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            report_error("Sequence control", error)
        else:
            if changed:
                ui.notify(success_message, type="warning")
            else:
                ui.notify(unavailable_message, type="warning")

        finally:
            self._sync_controls()

    async def _discard_sequence(self) -> None:
        self.paused_sequence_button.close()
        self.discard_sequence_item.set_enabled(False)

        try:
            if self._experiment_play_state == "paused":
                experiment_controller = self.experiment_controller
                if experiment_controller is None:
                    raise RuntimeError("Experiment Play control is unavailable")
                discarded = await run.io_bound(
                    experiment_controller.discard_manual_play
                )
                discarded_message = (
                    "Play discarded; the current workflow remains available manually"
                )
                unavailable_message = "No paused Play workflow is available"
            else:
                discarded = require_result(
                    await run.io_bound(self.controller.discard_paused_sequence),
                    operation="Sequence discard",
                )
                discarded_message = "Discarding paused sequence"
                unavailable_message = "No paused sequence is available"
        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            report_error("Sequence discard", error)
        else:
            if discarded:
                ui.notify(discarded_message, type="warning")
            else:
                ui.notify(unavailable_message, type="warning")

        finally:
            self._sync_controls()

    async def _emergency_stop_active(self) -> None:
        logger.warning("Emergency stop requested from GUI")

        self._emergency_stop_requested = True
        self._sync_controls()

        try:
            result = require_result(
                await run.io_bound(self.emergency_stop_action),
                operation="Emergency stop",
            )
        except Exception as error:  # noqa: BLE001 - GUI hardware-operation boundary
            self._emergency_stop_requested = False
            report_error("Emergency stop", error)
        else:
            messages: list[str] = []

            if result.runze_stop_requested:
                messages.append("Runze emergency stop requested")

            if result.emstat_abort_requested:
                messages.append("EmStat measurement abort requested")

            messages.extend(result.errors)

            if result.errors:
                self._emergency_stop_requested = False
                ui.notify("; ".join(messages), type="negative")
            elif messages:
                ui.notify("; ".join(messages), type="warning")
            else:
                logger.warning(
                    "No active operation accepted the GUI emergency-stop request"
                )
                ui.notify("No active operation accepted a stop request", type="warning")

        finally:
            self._sync_controls()

    async def _reset_syringe(self) -> None:
        self.reset_syringe_dialog.close()

        await self.run_operation(
            action=self.controller.reset_syringe,
            operation="Resetting syringe",
            success_message="Syringe reset completed",
        )

    async def _reset_selector_to_home(self) -> None:
        await self.run_operation(
            action=self.controller.reset_selector_to_home,
            operation="Resetting selector to home",
            success_message="Selector reset to home port 1",
        )

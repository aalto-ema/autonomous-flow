import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import Literal

from nicegui import ui

from controllers.emstat_controller import EmStatController
from controllers.runze_controller import RunzeController, RunzeSnapshot
from gui.emstat_panel import EmStatPanel, EmStatPanelState
from gui.runze_control_bar import RunzeControlBar
from gui.runze_input_validation import (
    read_integer_in_range,
    read_positive_number,
    read_rpm,
)
from runze.sy01b import (
    SY01B_MAX_DYNAMIC_SPEED_RPM,
    SY01B_MIN_DYNAMIC_SPEED_RPM,
    SY01B_VALVE_STATE_LABELS,
    SY01BFlowRateOptions,
    SY01BFluidPort,
)

FlowDirection = Literal["inlet_to_outlet", "outlet_to_inlet"]


@dataclass
class RunzeManualPageState:
    rpm: float | None = 10
    flow_direction: FlowDirection = "inlet_to_outlet"
    aspirate_volume_ml: float | None = 0.5
    dispense_volume_ml: float | None = 0.5
    selector_target_port: int | None = 1
    requested_flow_rate_ml_per_min: float | None = 1.0


class RunzeManualPage:
    MANUAL_COLUMN_STYLE = "flex: 1 1 560px; min-width: min(100%, 560px)"

    CARD_CLASSES = "w-full p-4 gap-4"

    CARD_TITLE_CLASSES = "text-xl font-bold"

    SECTION_TITLE_CLASSES = "text-lg font-bold"

    HELP_TEXT_CLASSES = "max-w-3xl text-sm text-gray-600"

    DEVICE_BODY_CLASSES = "w-full items-start gap-8 flex-nowrap"

    STATUS_COLUMN_CLASSES = "w-60 shrink-0 gap-3"

    CONTROLS_COLUMN_CLASSES = "flex-1 min-w-0"

    CONTROL_GRID_CLASSES = "w-full gap-x-4 gap-y-4 items-end"

    def __init__(
        self,
        controller: RunzeController,
        control_bar: RunzeControlBar,
        emstat_controller: EmStatController,
        *,
        state: RunzeManualPageState | None = None,
        emstat_panel_state: EmStatPanelState | None = None,
        on_emstat_panel_busy_change: Callable[[bool], None],
    ) -> None:
        self.controller = controller
        self.control_bar = control_bar
        self.emstat_controller = emstat_controller
        self.state = state if state is not None else RunzeManualPageState()
        self.emstat_panel_state = (
            emstat_panel_state if emstat_panel_state is not None else EmStatPanelState()
        )
        self.on_emstat_panel_busy_change = on_emstat_panel_busy_change

        configured_port_labels = {
            port.number: port.label for port in controller.selector_ports
        }
        self.selector_port_options = {
            number: (
                f"{number} - {configured_port_labels[number]}"
                if number in configured_port_labels
                else f"{number} - Unconfigured"
            )
            for number in range(1, 11)
        }

        self.sy01b_motion_buttons = []
        self.sv07m_motion_buttons = []
        self.control_inputs = []
        self._page_busy = control_bar.runze_busy
        self._lower_rpm_option_available = False
        self._upper_rpm_option_available = False
        self._syringe_position: int | None = None

        self._build_page()
        self._register_value_change_handlers()
        self._update_sy01b_action_labels()

    # UI construction ---

    def _build_page(self) -> None:
        with ui.row().classes("w-full items-start gap-6 flex-wrap"):
            with ui.column().classes("gap-6").style(self.MANUAL_COLUMN_STYLE):
                self._build_sy01b_card()

            with ui.column().classes("gap-6").style(self.MANUAL_COLUMN_STYLE):
                self._build_sv07m_card()
                self._build_flow_rate_card()

        self.emstat_panel = EmStatPanel(
            self.emstat_controller,
            state=self.emstat_panel_state,
            on_busy_change=self.on_emstat_panel_busy_change,
            external_busy=lambda: self.control_bar.emstat_busy,
        )

        self.control_inputs.extend(
            [
                self.rpm_input,
                self.flow_direction_input,
                self.requested_flow_rate_input,
                self.aspirate_volume_input,
                self.dispense_volume_input,
                self.selector_target_port_input,
            ]
        )

        self.sy01b_motion_buttons.extend(
            [self.aspirate_button, self.dispense_button, self.empty_syringe_button]
        )

        self.sv07m_motion_buttons.extend([self.selector_move_button])

    @contextmanager
    def _manual_card(self, *, title: str) -> Iterator[None]:
        with ui.card().classes(self.CARD_CLASSES):
            ui.label(title).classes(self.CARD_TITLE_CLASSES)
            ui.separator()
            yield

    @staticmethod
    def _build_status_row(label: str):
        with (
            ui.element("div")
            .classes("w-full")
            .style(
                "display: grid; "
                "grid-template-columns: 7rem minmax(0, 1fr); "
                "column-gap: 1rem; "
                "align-items: center;"
            )
        ):
            ui.label(label).classes("whitespace-nowrap")

            return ui.label("—").classes("whitespace-nowrap")

    def _build_sy01b_card(self) -> None:
        with self._manual_card(title="SY01B"):
            with ui.row().classes(self.DEVICE_BODY_CLASSES):
                self._build_sy01b_status()
                self._build_sy01b_controls()

    def _build_sy01b_status(self) -> None:
        with ui.column().classes(self.STATUS_COLUMN_CLASSES):
            ui.label("Status").classes(self.SECTION_TITLE_CLASSES)

            self.syringe_preparation_status = self._build_status_row("Syringe state")
            self.syringe_motor = self._build_status_row("Motor status")
            self.syringe_position = self._build_status_row("Syringe position")
            self.syringe_volume = self._build_status_row("Syringe volume")
            self.syringe_valve_status = self._build_status_row("Valve status")
            self.syringe_valve_state = self._build_status_row("Valve state")

    def _build_sy01b_controls(self) -> None:
        with ui.column().classes(self.CONTROLS_COLUMN_CLASSES):
            ui.label("Controls").classes(self.SECTION_TITLE_CLASSES)

            with ui.row().classes("items-end gap-4"):
                self.rpm_input = ui.number(
                    "RPM",
                    value=self.state.rpm,
                    min=SY01B_MIN_DYNAMIC_SPEED_RPM,
                    max=SY01B_MAX_DYNAMIC_SPEED_RPM,
                    step=1,
                    precision=0,
                ).classes("w-24")

                with ui.column().classes("gap-0"):
                    ui.label("Nominal flow rate").classes(self.HELP_TEXT_CLASSES)
                    self.rpm_flow_rate = ui.label("—")

            with ui.column().classes("w-full gap-1"):
                ui.label("Flow direction").classes(self.HELP_TEXT_CLASSES)

                self.flow_direction_input = (
                    ui.toggle(
                        {"inlet_to_outlet": "IN → OUT", "outlet_to_inlet": "OUT → IN"},
                        value=self.state.flow_direction,
                    )
                    .props("spread")
                    .classes("w-full")
                )

            with ui.grid(columns=2).classes(self.CONTROL_GRID_CLASSES):
                self.aspirate_volume_input = ui.number(
                    "Aspirate volume (mL)",
                    value=self.state.aspirate_volume_ml,
                    min=0.001,
                    step=0.001,
                    precision=3,
                ).classes("w-full")

                self.aspirate_button = ui.button(
                    "Aspirate from IN", on_click=self._aspirate
                ).classes("w-full whitespace-nowrap")

                self.dispense_volume_input = ui.number(
                    "Dispense volume (mL)",
                    value=self.state.dispense_volume_ml,
                    min=0.001,
                    step=0.001,
                    precision=3,
                ).classes("w-full")

                self.dispense_button = ui.button(
                    "Dispense to OUT", on_click=self._dispense
                ).classes("w-full whitespace-nowrap")

                self.empty_syringe_button = ui.button(
                    "Empty to OUT", on_click=self._empty_syringe
                ).classes("col-span-2 w-full whitespace-nowrap")

    def _build_flow_rate_card(self) -> None:
        with self._manual_card(title="Flow rate to RPM"):
            self.requested_flow_rate_input = ui.number(
                "Requested flow rate (mL/min)",
                value=self.state.requested_flow_rate_ml_per_min,
                min=0.001,
                step=0.001,
                precision=3,
            ).classes("w-48 shrink-0")

            with ui.column().classes("w-full gap-3"):
                with ui.row().classes("w-full items-center gap-3 flex-nowrap"):
                    ui.label("Lower").classes("w-14 shrink-0")

                    self.lower_rpm_option = ui.label("—").classes(
                        "flex-1 min-w-0 whitespace-nowrap"
                    )

                    self.use_lower_rpm_button = ui.button(
                        "Use RPM", on_click=self._use_lower_rpm
                    ).classes("w-24 shrink-0")

                with ui.row().classes("w-full items-center gap-3 flex-nowrap"):
                    ui.label("Upper").classes("w-14 shrink-0")

                    self.upper_rpm_option = ui.label("—").classes(
                        "flex-1 min-w-0 whitespace-nowrap"
                    )

                    self.use_upper_rpm_button = ui.button(
                        "Use RPM", on_click=self._use_upper_rpm
                    ).classes("w-24 shrink-0")

    def _build_sv07m_card(self) -> None:
        with self._manual_card(title="SV07M"):
            with ui.row().classes(self.DEVICE_BODY_CLASSES):
                self._build_sv07m_status()
                self._build_sv07m_controls()

    def _build_sv07m_status(self) -> None:
        with ui.column().classes(self.STATUS_COLUMN_CLASSES):
            ui.label("Status").classes(self.SECTION_TITLE_CLASSES)

            self.selector_preparation_status = self._build_status_row("Selector state")
            self.selector_motor = self._build_status_row("Motor status")
            self.selector_port = self._build_status_row("Current port")

    def _build_sv07m_controls(self) -> None:
        with ui.column().classes(self.CONTROLS_COLUMN_CLASSES):
            ui.label("Controls").classes(self.SECTION_TITLE_CLASSES)

            with ui.grid(columns=2).classes(self.CONTROL_GRID_CLASSES):
                self.selector_target_port_input = ui.select(
                    options=self.selector_port_options,
                    label="Target port",
                    value=self.state.selector_target_port,
                ).classes("w-full")

                self.selector_move_button = ui.button(
                    "Move to port", on_click=self._move_selector
                ).classes("w-full whitespace-nowrap")

    def _register_value_change_handlers(self) -> None:
        state_bindings = (
            (self.rpm_input, "rpm"),
            (self.flow_direction_input, "flow_direction"),
            (self.aspirate_volume_input, "aspirate_volume_ml"),
            (self.dispense_volume_input, "dispense_volume_ml"),
            (self.selector_target_port_input, "selector_target_port"),
            (self.requested_flow_rate_input, "requested_flow_rate_ml_per_min"),
        )

        for element, attribute in state_bindings:
            element.on_value_change(
                lambda event, attribute=attribute: setattr(
                    self.state, attribute, event.value
                )
            )

        self.rpm_input.on_value_change(lambda _: self._update_rpm_dependent_values())
        self.aspirate_volume_input.on_value_change(
            lambda _: self._update_sy01b_action_labels()
        )
        self.dispense_volume_input.on_value_change(
            lambda _: self._update_sy01b_action_labels()
        )
        self.requested_flow_rate_input.on_value_change(
            lambda _: self._update_rpm_options_from_flow_rate()
        )
        self.flow_direction_input.on_value_change(
            lambda _: self._update_sy01b_action_labels()
        )

    def _update_rpm_dependent_values(self) -> None:
        self._update_flow_rate_from_rpm()
        self._update_sy01b_action_labels()

    # Display and enabled-state helpers ---

    def show_disconnected(self) -> None:
        self._show_status_placeholder("—")
        self._sync_page_state()

    def show_status_unknown(self) -> None:
        self._show_status_placeholder("Unknown")
        self._sync_page_state()

    def _show_status_placeholder(self, text: str) -> None:
        self._syringe_position = None

        self.syringe_position.set_text(text)
        self.syringe_volume.set_text(text)
        self.syringe_motor.set_text(text)
        self.syringe_valve_status.set_text(text)
        self.syringe_valve_state.set_text(text)
        self.syringe_preparation_status.set_text(text)

        self.selector_preparation_status.set_text(text)
        self.selector_motor.set_text(text)
        self.selector_port.set_text(text)

    def show_snapshot(self, snapshot: RunzeSnapshot) -> None:
        position = snapshot.syringe.position
        volume_ml = snapshot.syringe.volume_ml
        self._syringe_position = position

        if position is None or volume_ml is None:
            self.syringe_position.set_text("Unknown")
            self.syringe_volume.set_text("Unknown")
        else:
            self.syringe_position.set_text(f"{position} / 6000")
            self.syringe_volume.set_text(f"{volume_ml:.3f} mL")

        self.syringe_motor.set_text(snapshot.syringe.motor_status.name)
        self.syringe_valve_status.set_text(snapshot.syringe.valve_status.name)

        valve_state = snapshot.syringe.valve_state
        valve_state_label = SY01B_VALVE_STATE_LABELS.get(valve_state, "Unknown")
        self.syringe_valve_state.set_text(f"{valve_state} ({valve_state_label})")

        if snapshot.syringe_state == "ready":
            self.syringe_preparation_status.set_text("Ready")
        else:
            self.syringe_preparation_status.set_text("Reset required")

        if snapshot.selector_state == "ready":
            self.selector_preparation_status.set_text("Ready")
        else:
            self.selector_preparation_status.set_text("Home required")

        self.selector_motor.set_text(snapshot.selector.motor_status.name)

        if snapshot.selector.current_port is None:
            self.selector_port.set_text("Unknown")
        else:
            port = snapshot.selector.current_port
            self.selector_port.set_text(self.selector_port_options.get(port, str(port)))

        self._sync_page_state()

    def set_busy(self, busy: bool) -> None:
        self._page_busy = busy
        self._sync_page_state()

    def _sync_page_state(self) -> None:
        busy = self._page_busy or self.controller.busy
        connected = self.controller.connected
        status_known = self.controller.status_known
        syringe_ready = self.controller.syringe_ready
        selector_ready = self.controller.selector_ready

        inputs_enabled = connected and status_known and not busy

        for element in self.control_inputs:
            element.set_enabled(inputs_enabled)

        self.selector_target_port_input.set_enabled(
            connected and status_known and selector_ready and not busy
        )

        for button in self.sy01b_motion_buttons:
            button.set_enabled(
                connected and status_known and syringe_ready and not busy
            )

        for button in self.sv07m_motion_buttons:
            button.set_enabled(
                connected and status_known and selector_ready and not busy
            )

        if connected and status_known and not busy:
            self._update_flow_rate_from_rpm()
            self._update_rpm_options_from_flow_rate()
        elif not connected or not status_known:
            self.rpm_flow_rate.set_text("—")
            self._clear_rpm_options_from_flow_rate()

        self.use_lower_rpm_button.set_enabled(
            connected and status_known and not busy and self._lower_rpm_option_available
        )
        self.use_upper_rpm_button.set_enabled(
            connected and status_known and not busy and self._upper_rpm_option_available
        )

        self._update_sy01b_action_labels()

    # Input and derived-value helpers ---

    def _read_selector_port(self) -> int:
        return read_integer_in_range(
            self.selector_target_port_input.value,
            name="Selector port",
            minimum=1,
            maximum=10,
        )

    def _read_rpm(self) -> int:
        return read_rpm(self.rpm_input.value, name="RPM")

    def _read_requested_flow_rate(self) -> float:
        return read_positive_number(
            self.requested_flow_rate_input.value, name="Requested flow rate"
        )

    def _read_rpm_options_from_flow_rate(self) -> SY01BFlowRateOptions:
        return self.controller.syringe_rpm_options_from_ml_per_min(
            self._read_requested_flow_rate()
        )

    def _read_flow_direction(self) -> FlowDirection:
        value = self.flow_direction_input.value

        if value == "inlet_to_outlet":
            return "inlet_to_outlet"

        if value == "outlet_to_inlet":
            return "outlet_to_inlet"

        raise ValueError("Flow direction is required")

    def _read_flow_path(self) -> tuple[SY01BFluidPort, SY01BFluidPort]:
        direction = self._read_flow_direction()

        if direction == "inlet_to_outlet":
            return "inlet", "outlet"

        return "outlet", "inlet"

    def _set_rpm(self, rpm: int) -> None:
        self.state.rpm = rpm
        self.rpm_input.set_value(rpm)

    @staticmethod
    def _format_estimated_duration(duration_s: float) -> str:
        total_seconds = math.ceil(duration_s)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}:{seconds:02d}"

    @classmethod
    def _button_text_with_duration(cls, text: str, duration_s: float | None) -> str:
        if duration_s is None:
            return text

        return f"{text} · ≈{cls._format_estimated_duration(duration_s)}"

    def _estimated_volume_move_duration_s(
        self, value: float | None, *, rpm: int
    ) -> float | None:
        try:
            volume_ml = read_positive_number(value, name="Volume")
            return self.controller.syringe_estimated_move_duration_s_from_volume_ml(
                volume_ml, rpm=rpm
            )
        except (TypeError, ValueError, RuntimeError):
            return None

    def _estimated_empty_move_duration_s(self, *, rpm: int) -> float | None:
        if self._syringe_position is None:
            return None

        try:
            return self.controller.syringe_estimated_move_duration_s_from_steps(
                self._syringe_position, rpm=rpm
            )
        except (TypeError, ValueError, RuntimeError):
            return None

    def _update_sy01b_action_labels(self) -> None:
        try:
            source, destination = self._read_flow_path()
        except ValueError:
            self.aspirate_button.set_text("Aspirate")
            self.dispense_button.set_text("Dispense")
            self.empty_syringe_button.set_text("Empty syringe")
            return

        source_label = "IN" if source == "inlet" else "OUT"
        destination_label = "IN" if destination == "inlet" else "OUT"

        aspirate_duration_s: float | None = None
        dispense_duration_s: float | None = None
        empty_duration_s: float | None = None

        if self.controller.connected and self.controller.status_known:
            try:
                rpm = self._read_rpm()
            except (TypeError, ValueError):
                pass
            else:
                aspirate_duration_s = self._estimated_volume_move_duration_s(
                    self.aspirate_volume_input.value, rpm=rpm
                )
                dispense_duration_s = self._estimated_volume_move_duration_s(
                    self.dispense_volume_input.value, rpm=rpm
                )
                empty_duration_s = self._estimated_empty_move_duration_s(rpm=rpm)

        self.aspirate_button.set_text(
            self._button_text_with_duration(
                f"Aspirate from {source_label}", aspirate_duration_s
            )
        )
        self.dispense_button.set_text(
            self._button_text_with_duration(
                f"Dispense to {destination_label}", dispense_duration_s
            )
        )
        self.empty_syringe_button.set_text(
            self._button_text_with_duration(
                f"Empty to {destination_label}", empty_duration_s
            )
        )

    def _clear_rpm_options_from_flow_rate(self) -> None:
        self.lower_rpm_option.set_text("—")
        self.upper_rpm_option.set_text("—")

        self._lower_rpm_option_available = False
        self._upper_rpm_option_available = False

    def _update_flow_rate_from_rpm(self) -> None:
        if not self.controller.connected or not self.controller.status_known:
            self.rpm_flow_rate.set_text("—")
            return

        try:
            rpm = self._read_rpm()
            flow_ml_per_min = self.controller.syringe_flow_rate_ml_per_min_from_rpm(rpm)
        except (TypeError, ValueError, RuntimeError):
            self.rpm_flow_rate.set_text("Invalid RPM")
            return

        self.rpm_flow_rate.set_text(f"{flow_ml_per_min:.3f} mL/min")

    def _update_rpm_options_from_flow_rate(self) -> None:
        if not self.controller.connected or not self.controller.status_known:
            self._clear_rpm_options_from_flow_rate()
            return

        try:
            options = self._read_rpm_options_from_flow_rate()
        except (TypeError, ValueError, RuntimeError):
            self.lower_rpm_option.set_text("Invalid flow rate")
            self.upper_rpm_option.set_text("—")

            self._lower_rpm_option_available = False
            self._upper_rpm_option_available = False
            return

        self.lower_rpm_option.set_text(
            f"{options.lower_rpm} rpm → {options.lower_flow_ml_per_min:.3f} mL/min"
        )
        self.upper_rpm_option.set_text(
            f"{options.upper_rpm} rpm → {options.upper_flow_ml_per_min:.3f} mL/min"
        )

        self._lower_rpm_option_available = True
        self._upper_rpm_option_available = options.upper_rpm != options.lower_rpm

    def _use_lower_rpm(self) -> None:
        try:
            options = self._read_rpm_options_from_flow_rate()
        except (TypeError, ValueError, RuntimeError) as error:
            ui.notify(str(error), type="negative")
            return

        self._set_rpm(options.lower_rpm)

    def _use_upper_rpm(self) -> None:
        try:
            options = self._read_rpm_options_from_flow_rate()
        except (TypeError, ValueError, RuntimeError) as error:
            ui.notify(str(error), type="negative")
            return

        self._set_rpm(options.upper_rpm)

    # Async UI callbacks ---

    async def _aspirate(self) -> None:
        try:
            rpm = self._read_rpm()
            volume_ml = read_positive_number(
                self.aspirate_volume_input.value, name="Volume"
            )
            source, _ = self._read_flow_path()
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.aspirate, volume_ml=volume_ml, rpm=rpm, source=source
            ),
            operation=f"Aspirating from {source}",
            success_message=(f"Aspiration from {source} completed"),
        )

    async def _dispense(self) -> None:
        try:
            rpm = self._read_rpm()
            volume_ml = read_positive_number(
                self.dispense_volume_input.value, name="Volume"
            )
            _, destination = self._read_flow_path()
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.dispense,
                volume_ml=volume_ml,
                rpm=rpm,
                destination=destination,
            ),
            operation=f"Dispensing to {destination}",
            success_message=(f"Dispensing to {destination} completed"),
        )

    async def _empty_syringe(self) -> None:
        try:
            rpm = self._read_rpm()
            _, destination = self._read_flow_path()
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.empty_syringe, rpm=rpm, destination=destination
            ),
            operation=(f"Emptying syringe to {destination}"),
            success_message=(f"Syringe emptied to {destination}"),
        )

    async def _move_selector(self) -> None:
        try:
            port = self._read_selector_port()
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        port_label = self.selector_port_options.get(port, str(port))

        await self.control_bar.run_operation(
            action=partial(self.controller.move_selector_to_port, port=port),
            operation=f"Moving selector to port {port_label}",
            success_message=f"Selector moved to port {port_label}",
        )

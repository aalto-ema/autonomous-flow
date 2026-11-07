import math
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from nicegui import ui
from nicegui.elements.number import Number
from nicegui.elements.row import Row

from controllers.runze_controller import RunzeController, RunzeSnapshot
from fluidics.runze_fluidics import WaterDistribution
from gui.runze_control_bar import RunzeControlBar
from gui.runze_input_validation import (
    read_non_negative_number,
    read_positive_integer,
    read_positive_number,
    read_rpm,
)
from runze.sy01b import SY01B_MAX_DYNAMIC_SPEED_RPM, SY01B_MIN_DYNAMIC_SPEED_RPM


@dataclass
class RunzeProceduresPageState:
    prime_rpm: float | None = 10

    mixture_source_fractions: dict[str, float | None] = field(default_factory=dict)
    mixture_volume_ml: float | None = 1.0
    mixture_rpm: float | None = 10
    mixture_water_distribution: WaterDistribution | None = "single"

    transfer_volume_ml: float | None = 1.0
    transfer_rpm: float | None = 10
    inlet_aspiration_rpm: float | None = 10
    transfer_source_port: int | None = None
    transfer_destination_port: int | None = None

    fill_flow_volume_ml: float | None = 1.0
    fill_flow_rpm: float | None = 10
    fill_flow_approach_rpm: float | None = None
    fill_flow_cell_rpm: float | None = 10

    plug_volume_ml: float | None = 1.0
    plug_rpm: float | None = 10
    plug_approach_rpm: float | None = None
    plug_flow_rpm: float | None = 10

    drain_volume_ml: float | None = 1.0
    drain_rpm: float | None = 10

    flush_volume_ml: float | None = 1.0
    flush_rpm: float | None = 10
    flush_cycles: float | None = 1

    rinse_vessel_volume_ml: float | None = 1.0
    rinse_vessel_rpm: float | None = 10
    rinse_vessel_cycles: float | None = 2

    rinse_flow_cell_volume_ml: float | None = 1.0
    rinse_flow_cell_handling_rpm: float | None = 10
    rinse_flow_cell_rinsing_rpm: float | None = 10

    working_routes_volume_ml: float | None = 1.0
    working_routes_rpm: float | None = 10
    working_routes_cycles: float | None = 1

    line_maintenance_handling_rpm: float | None = 10
    line_maintenance_approach_rpm: float | None = 10
    line_maintenance_additional_rinse_volume_ml: float | None = 0.0


class RunzeProceduresPage:
    PROCEDURE_COLUMN_STYLE = "flex: 1 1 560px; min-width: min(100%, 560px)"

    def __init__(
        self,
        controller: RunzeController,
        control_bar: RunzeControlBar,
        *,
        state: RunzeProceduresPageState | None = None,
    ) -> None:
        self.controller = controller
        self.control_bar = control_bar
        self.state = state if state is not None else RunzeProceduresPageState()
        self.mixture_sources = controller.liquid_source_ports
        plumbing = controller.plumbing
        if plumbing is None:
            raise ValueError("Runze procedures require plumbing configuration")
        self.line_maintenance_source_ports = tuple(
            source.number
            for source in plumbing.liquid_sources
            if source.number != plumbing.water.number
        )
        self.transfer_source_port_options = {
            port.number: f"{port.number} - {port.label}"
            for port in controller.liquid_source_ports
        }
        self.transfer_destination_port_options = {
            port.number: f"{port.number} - {port.label}"
            for port in controller.destination_ports
        }

        if self.state.transfer_source_port not in self.transfer_source_port_options:
            self.state.transfer_source_port = next(
                iter(self.transfer_source_port_options), None
            )

        if (
            self.state.transfer_destination_port
            not in self.transfer_destination_port_options
        ):
            self.state.transfer_destination_port = next(
                iter(self.transfer_destination_port_options), None
            )

        self.mixture_fraction_inputs: dict[str, Number] = {}
        self.procedure_inputs = []
        self.procedure_buttons = []
        self._page_busy = control_bar.runze_busy

        self._build_page()
        self._register_state_handlers()

    # UI construction ---

    def _build_page(self) -> None:
        with ui.row().classes("w-full items-start gap-6 flex-wrap"):
            with ui.column().classes("gap-6").style(self.PROCEDURE_COLUMN_STYLE):
                self._build_preparation_card()
                self._build_cleaning_card()
                self._build_line_maintenance_card()

            with ui.column().classes("gap-6").style(self.PROCEDURE_COLUMN_STYLE):
                self._build_operation_card()

    def _build_preparation_card(self) -> None:
        with (
            self._procedure_card(title="Preparation"),
            self._procedure_section(
                subtitle="Prime liquid source lines",
                description="Prime every configured liquid source line slightly past "
                "the selector, then push the overdraw to selector waste.",
            ),
        ):
            self.prime_rpm_input = self._rpm_input(value=self.state.prime_rpm)
            self.prime_button = ui.button(
                "Prime source lines", on_click=self._prime_all_liquid_source_lines
            ).classes("whitespace-nowrap")

        self._register_procedure_controls(
            inputs=[self.prime_rpm_input], buttons=[self.prime_button]
        )

    def _build_operation_card(self) -> None:
        with self._procedure_card(title="Operation"):
            with self._procedure_section(
                subtitle="Transfer aspiration speed",
                description=(
                    "Optional override used only while aspirating source liquid "
                    "through IN during mixture preparation and standalone transfers. "
                    "Leave blank to use each operation's normal RPM."
                ),
            ):
                self.inlet_aspiration_rpm_input = self._rpm_input(
                    label="IN aspiration RPM", value=self.state.inlet_aspiration_rpm
                )

            self._register_procedure_controls(
                inputs=[self.inlet_aspiration_rpm_input], buttons=[]
            )

            with self._procedure_section(
                subtitle="Prepare mixture",
                description=(
                    "Set a fraction for each configured liquid source with the "
                    "fractions summing to 1. Water can be transferred once in "
                    "configuration order or divided between non-water components. "
                    "The common-line-stroke option keeps liquid out of the SY01B "
                    "valve and syringe."
                ),
            ):
                if not self.mixture_sources:
                    ui.label("No eligible mixture sources are configured.").classes(
                        "text-sm text-gray-600"
                    )
                else:
                    with ui.column().classes("w-full gap-4"):
                        with ui.grid(columns=2).classes("w-full gap-3"):
                            for source in self.mixture_sources:
                                fraction_input = ui.number(
                                    f"{source.label} fraction",
                                    value=self.state.mixture_source_fractions.get(
                                        source.key, 0.0
                                    ),
                                    min=0.0,
                                    max=1.0,
                                    step=0.001,
                                    precision=3,
                                ).classes("w-full")
                                self.mixture_fraction_inputs[source.key] = (
                                    fraction_input
                                )

                        with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                            self.mixture_volume_input = self._ml_input(
                                label="Mixture volume",
                                value=self.state.mixture_volume_ml,
                            )
                            self.mixture_rpm_input = self._rpm_input(
                                value=self.state.mixture_rpm
                            )
                            self.mixture_water_distribution_input = ui.select(
                                options={
                                    "single": "Single water transfer",
                                    "between_components": "Water between components",
                                },
                                label="Water distribution",
                                value=self.state.mixture_water_distribution,
                            ).classes("w-56")
                            self.prepare_single_aspiration_mixture_button = ui.button(
                                "Prepare — single aspiration",
                                on_click=self._prepare_mixture_with_single_aspiration,
                            ).classes("whitespace-nowrap")
                            self.prepare_common_line_stroke_mixture_button = ui.button(
                                "Prepare — common-line strokes",
                                on_click=(
                                    self._prepare_mixture_with_common_line_strokes
                                ),
                            ).classes("whitespace-nowrap")

                    self._register_procedure_controls(
                        inputs=[
                            *self.mixture_fraction_inputs.values(),
                            self.mixture_volume_input,
                            self.mixture_rpm_input,
                            self.mixture_water_distribution_input,
                        ],
                        buttons=[
                            self.prepare_single_aspiration_mixture_button,
                            self.prepare_common_line_stroke_mixture_button,
                        ],
                    )

            with self._procedure_section(
                subtitle="Transfer liquid",
                description=(
                    "Both methods finish with a destination-route air chase. "
                    "Single aspiration may draw liquid into the SY01B valve and "
                    "syringe; common-line strokes keep the liquid front before "
                    "the SY01B valve."
                ),
            ):
                self.transfer_volume_input = self._ml_input(
                    label="Transfer volume", value=self.state.transfer_volume_ml
                )
                self.transfer_rpm_input = self._rpm_input(value=self.state.transfer_rpm)
                self.transfer_source_port_input = ui.select(
                    options=self.transfer_source_port_options,
                    label="Source port",
                    value=self.state.transfer_source_port,
                ).classes("w-48")
                self.transfer_destination_port_input = ui.select(
                    options=self.transfer_destination_port_options,
                    label="Destination port",
                    value=self.state.transfer_destination_port,
                ).classes("w-48")
                self.single_aspiration_transfer_button = ui.button(
                    "Transfer — single aspiration",
                    on_click=self._transfer_liquid_with_single_aspiration,
                ).classes("whitespace-nowrap")
                self.common_line_stroke_transfer_button = ui.button(
                    "Transfer — common-line strokes",
                    on_click=self._transfer_liquid_with_common_line_strokes,
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.transfer_volume_input,
                    self.transfer_rpm_input,
                    self.transfer_source_port_input,
                    self.transfer_destination_port_input,
                ],
                buttons=[
                    self.single_aspiration_transfer_button,
                    self.common_line_stroke_transfer_button,
                ],
            )

            with self._procedure_section(
                subtitle="Fill flow-cell from mixing vessel",
                description=(
                    "Condition and aspirate at Handling RPM, advance the configured "
                    "route volume at Approach RPM, then prefill slightly beyond the "
                    "flow cell at Flow RPM before passing the selected volume. Leave "
                    "Approach RPM blank to use Flow RPM. The path remains "
                    "liquid-filled."
                ),
            ):
                self.fill_flow_volume_input = self._ml_input(
                    label="Passing volume", value=self.state.fill_flow_volume_ml
                )
                self.fill_flow_rpm_input = self._rpm_input(
                    label="Handling RPM", value=self.state.fill_flow_rpm
                )
                self.fill_flow_approach_rpm_input = self._rpm_input(
                    label="Approach RPM", value=self.state.fill_flow_approach_rpm
                ).props("stack-label")
                self.fill_flow_cell_rpm_input = self._rpm_input(
                    label="Flow RPM", value=self.state.fill_flow_cell_rpm
                )
                self.fill_flow_button = ui.button(
                    "Fill flow-cell", on_click=self._fill_flow_cell_from_mixing_vessel
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.fill_flow_volume_input,
                    self.fill_flow_rpm_input,
                    self.fill_flow_approach_rpm_input,
                    self.fill_flow_cell_rpm_input,
                ],
                buttons=[self.fill_flow_button],
            )

            with self._procedure_section(
                subtitle="Pass mixing-vessel plug through flow-cell",
                description=(
                    "Pass one continuous plug no larger than the common-line dead "
                    "volume. Handle the plug at Handling RPM, approach the flow-cell "
                    "boundary at Approach RPM, and pass the plug at Flow RPM. Leave "
                    "Approach RPM blank to use Flow RPM. The common and flow-cell "
                    "routes finish air-filled."
                ),
            ):
                self.plug_volume_input = self._ml_input(
                    label="Plug volume", value=self.state.plug_volume_ml
                )
                self.plug_rpm_input = self._rpm_input(
                    label="Handling RPM", value=self.state.plug_rpm
                )
                self.plug_approach_rpm_input = self._rpm_input(
                    label="Approach RPM", value=self.state.plug_approach_rpm
                ).props("stack-label")
                self.plug_flow_rpm_input = self._rpm_input(
                    label="Flow RPM", value=self.state.plug_flow_rpm
                )
                self.plug_button = ui.button(
                    "Pass liquid plug",
                    on_click=self._pass_mixing_plug_through_flow_cell,
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.plug_volume_input,
                    self.plug_rpm_input,
                    self.plug_approach_rpm_input,
                    self.plug_flow_rpm_input,
                ],
                buttons=[self.plug_button],
            )

            with self._procedure_section(
                subtitle="Drain mixing vessel",
                description=(
                    "Discard the expected vessel contents through SY01B OUT and "
                    "air-clear the mixing route, common line, and OUT line. Use "
                    "zero only when the vessel is already empty."
                ),
            ):
                self.drain_volume_input = self._ml_input(
                    label="Expected vessel volume",
                    value=self.state.drain_volume_ml,
                    minimum=0.0,
                )
                self.drain_rpm_input = self._rpm_input(
                    label="Drain RPM", value=self.state.drain_rpm
                )
                self.drain_button = ui.button(
                    "Drain mixing vessel", on_click=self._drain_mixing_vessel_to_outlet
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[self.drain_volume_input, self.drain_rpm_input],
                buttons=[self.drain_button],
            )

    def _build_cleaning_card(self) -> None:
        with self._procedure_card(title="Cleaning"):
            with self._procedure_section(
                subtitle="Flush system",
                description=(
                    "Flush water through SY01B OUT for the selected number of "
                    "cycles, then purge the common and OUT lines with air."
                ),
            ):
                self.flush_volume_input = self._ml_input(
                    label="Water per cycle", value=self.state.flush_volume_ml
                )
                self.flush_rpm_input = self._rpm_input(value=self.state.flush_rpm)
                self.flush_cycles_input = self._cycles_input(
                    value=self.state.flush_cycles
                )
                self.flush_system_button = ui.button(
                    "Flush system", on_click=self._flush_system
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.flush_volume_input,
                    self.flush_rpm_input,
                    self.flush_cycles_input,
                ],
                buttons=[self.flush_system_button],
            )

            with self._procedure_section(
                subtitle="Rinse mixing vessel",
                description=(
                    "Run Drain mixing vessel first. This then repeatedly adds water "
                    "and drains it through SY01B OUT, providing hydraulic rinsing "
                    "without agitation."
                ),
            ):
                self.rinse_vessel_volume_input = self._ml_input(
                    label="Water per rinse", value=self.state.rinse_vessel_volume_ml
                )
                self.rinse_vessel_rpm_input = self._rpm_input(
                    label="Rinse RPM", value=self.state.rinse_vessel_rpm
                )
                self.rinse_vessel_cycles_input = self._cycles_input(
                    value=self.state.rinse_vessel_cycles
                )
                self.rinse_vessel_button = ui.button(
                    "Rinse mixing vessel", on_click=self._rinse_mixing_vessel
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.rinse_vessel_volume_input,
                    self.rinse_vessel_rpm_input,
                    self.rinse_vessel_cycles_input,
                ],
                buttons=[self.rinse_vessel_button],
            )

            with self._procedure_section(
                subtitle="Rinse flow-cell with water",
                description=(
                    "Replace the flow-cell contents with fresh water without an "
                    "air break. Water handling and final air restoration use "
                    "Handling RPM; the entire flow-cell-directed dispense uses "
                    "Rinsing RPM. The flow-cell route remains water-filled while "
                    "the common and SY01B OUT paths finish air-filled."
                ),
            ):
                self.rinse_flow_cell_volume_input = self._ml_input(
                    label="Rinse-through volume",
                    value=self.state.rinse_flow_cell_volume_ml,
                )
                self.rinse_flow_cell_handling_rpm_input = self._rpm_input(
                    label="Handling RPM", value=self.state.rinse_flow_cell_handling_rpm
                )
                self.rinse_flow_cell_rinsing_rpm_input = self._rpm_input(
                    label="Rinsing RPM", value=self.state.rinse_flow_cell_rinsing_rpm
                )
                self.rinse_flow_cell_button = ui.button(
                    "Rinse flow-cell", on_click=self._rinse_flow_cell_with_water
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.rinse_flow_cell_volume_input,
                    self.rinse_flow_cell_handling_rpm_input,
                    self.rinse_flow_cell_rinsing_rpm_input,
                ],
                buttons=[self.rinse_flow_cell_button],
            )

            with self._procedure_section(
                subtitle="Flush working destination routes",
                description=(
                    "Water-flush and air-purge the selector-waste and flow-cell "
                    "routes. Other source lines and the mixing-vessel route are "
                    "not flushed."
                ),
            ):
                self.working_routes_volume_input = self._ml_input(
                    label="Rinse volume per route",
                    value=self.state.working_routes_volume_ml,
                )
                self.working_routes_rpm_input = self._rpm_input(
                    value=self.state.working_routes_rpm
                )
                self.working_routes_cycles_input = self._cycles_input(
                    label="System cycles", value=self.state.working_routes_cycles
                )
                self.working_routes_button = ui.button(
                    "Flush working routes",
                    on_click=self._flush_working_destination_routes,
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.working_routes_volume_input,
                    self.working_routes_rpm_input,
                    self.working_routes_cycles_input,
                ],
                buttons=[self.working_routes_button],
            )

    def _build_line_maintenance_card(self) -> None:
        with self._procedure_card(title="Line maintenance"):
            with self._procedure_section(
                subtitle="1. Recover sources and clear flow cell",
                description=(
                    "Required start: syringe empty; common and SY01B OUT lines "
                    "bulk-air-filled. Keep each non-water tube in its matching "
                    "source container and route the flow-cell output to waste."
                ),
            ):
                self.line_maintenance_handling_rpm_input = self._rpm_input(
                    label="Handling RPM", value=self.state.line_maintenance_handling_rpm
                )
                self.line_maintenance_approach_rpm_input = self._rpm_input(
                    label="Approach RPM", value=self.state.line_maintenance_approach_rpm
                )
                self.recover_source_lines_button = ui.button(
                    "Recover sources and clear flow cell",
                    on_click=self._recover_source_lines_and_clear_flow_cell,
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[
                    self.line_maintenance_handling_rpm_input,
                    self.line_maintenance_approach_rpm_input,
                ],
                buttons=[self.recover_source_lines_button],
            )

            with self._procedure_section(
                subtitle="2. Water-rinse source lines",
                description=(
                    "Move every non-water source tube to waste and leave the water "
                    "tube in its reservoir. Uses the handling RPM above."
                ),
            ):
                self.line_maintenance_additional_rinse_volume_input = self._ml_input(
                    label="Additional rinse per line",
                    value=self.state.line_maintenance_additional_rinse_volume_ml,
                    minimum=0,
                )
                self.rinse_source_lines_button = ui.button(
                    "Water-rinse source lines", on_click=self._rinse_source_lines
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[self.line_maintenance_additional_rinse_volume_input],
                buttons=[self.rinse_source_lines_button],
            )

            with self._procedure_section(
                subtitle="3. Air-empty all source lines",
                description=(
                    "Move every source tube, including water, to waste. Uses the "
                    "handling RPM above and finishes by purging SY01B OUT with air."
                ),
            ):
                self.air_empty_source_lines_button = ui.button(
                    "Air-empty all source lines", on_click=self._air_empty_source_lines
                ).classes("whitespace-nowrap")

            self._register_procedure_controls(
                inputs=[], buttons=[self.air_empty_source_lines_button]
            )

    @contextmanager
    def _procedure_card(self, *, title: str) -> Generator[None, Any, None]:
        with ui.card().classes("w-full p-4 gap-4"):
            ui.label(title).classes("text-xl font-bold")
            ui.separator()
            yield

    @contextmanager
    def _procedure_section(
        self, *, subtitle: str, description: str | None = None
    ) -> Generator[Row, Any, None]:
        ui.label(subtitle).classes("text-lg font-bold")
        if description:
            ui.label(description).classes("max-w-3xl text-sm text-gray-600")
        with ui.row().classes("w-full items-end gap-4 flex-wrap") as row:
            yield row

    def _register_procedure_controls(self, *, inputs: list, buttons: list) -> None:
        self.procedure_inputs.extend(inputs)
        self.procedure_buttons.extend(buttons)

    def _register_state_handlers(self) -> None:
        state_bindings = [
            (self.prime_rpm_input, "prime_rpm"),
            (self.transfer_volume_input, "transfer_volume_ml"),
            (self.transfer_rpm_input, "transfer_rpm"),
            (self.inlet_aspiration_rpm_input, "inlet_aspiration_rpm"),
            (self.transfer_source_port_input, "transfer_source_port"),
            (self.transfer_destination_port_input, "transfer_destination_port"),
            (self.fill_flow_volume_input, "fill_flow_volume_ml"),
            (self.fill_flow_rpm_input, "fill_flow_rpm"),
            (self.fill_flow_approach_rpm_input, "fill_flow_approach_rpm"),
            (self.fill_flow_cell_rpm_input, "fill_flow_cell_rpm"),
            (self.plug_volume_input, "plug_volume_ml"),
            (self.plug_rpm_input, "plug_rpm"),
            (self.plug_approach_rpm_input, "plug_approach_rpm"),
            (self.plug_flow_rpm_input, "plug_flow_rpm"),
            (self.drain_volume_input, "drain_volume_ml"),
            (self.drain_rpm_input, "drain_rpm"),
            (self.flush_volume_input, "flush_volume_ml"),
            (self.flush_rpm_input, "flush_rpm"),
            (self.flush_cycles_input, "flush_cycles"),
            (self.rinse_vessel_volume_input, "rinse_vessel_volume_ml"),
            (self.rinse_vessel_rpm_input, "rinse_vessel_rpm"),
            (self.rinse_vessel_cycles_input, "rinse_vessel_cycles"),
            (self.rinse_flow_cell_volume_input, "rinse_flow_cell_volume_ml"),
            (self.rinse_flow_cell_handling_rpm_input, "rinse_flow_cell_handling_rpm"),
            (self.rinse_flow_cell_rinsing_rpm_input, "rinse_flow_cell_rinsing_rpm"),
            (self.working_routes_volume_input, "working_routes_volume_ml"),
            (self.working_routes_rpm_input, "working_routes_rpm"),
            (self.working_routes_cycles_input, "working_routes_cycles"),
            (self.line_maintenance_handling_rpm_input, "line_maintenance_handling_rpm"),
            (self.line_maintenance_approach_rpm_input, "line_maintenance_approach_rpm"),
            (
                self.line_maintenance_additional_rinse_volume_input,
                "line_maintenance_additional_rinse_volume_ml",
            ),
        ]

        if self.mixture_sources:
            state_bindings.extend(
                [
                    (self.mixture_volume_input, "mixture_volume_ml"),
                    (self.mixture_rpm_input, "mixture_rpm"),
                    (
                        self.mixture_water_distribution_input,
                        "mixture_water_distribution",
                    ),
                ]
            )

        for element, attribute in state_bindings:
            element.on_value_change(partial(self._store_state_value, attribute))

        for source_key, element in self.mixture_fraction_inputs.items():
            element.on_value_change(partial(self._store_mixture_fraction, source_key))

    def _store_state_value(self, attribute: str, event: Any) -> None:
        setattr(self.state, attribute, event.value)

    def _store_mixture_fraction(self, source_key: str, event: Any) -> None:
        self.state.mixture_source_fractions[source_key] = event.value

    def _rpm_input(self, *, label="RPM", value: float | None = 10) -> Number:
        return ui.number(
            label,
            value=value,
            min=SY01B_MIN_DYNAMIC_SPEED_RPM,
            max=SY01B_MAX_DYNAMIC_SPEED_RPM,
            step=1,
            precision=0,
        ).classes("w-28")

    def _ml_input(
        self,
        *,
        label: str,
        value: float | None = 1.0,
        minimum=0.001,
        width="w-38",  # w-28
    ) -> Number:
        return ui.number(
            label, value=value, min=minimum, step=0.001, precision=3
        ).classes(width)

    def _cycles_input(self, *, label="Cycles", value: float | None = 1) -> Number:
        return ui.number(label, value=value, min=1, step=1, precision=0).classes("w-28")

    # Display and enabled-state helpers ---

    def show_snapshot(self, snapshot: RunzeSnapshot) -> None:
        self._sync_page_state()

    def show_disconnected(self) -> None:
        self._sync_page_state()

    def show_status_unknown(self) -> None:
        self._sync_page_state()

    def set_busy(self, busy: bool) -> None:
        self._page_busy = busy
        self._sync_page_state()

    def _sync_page_state(self) -> None:
        busy = self._page_busy or self.controller.busy

        procedures_ready = (
            self.controller.connected
            and self.controller.status_known
            and self.controller.fluidics_available
            and self.controller.syringe_ready
            and self.controller.selector_ready
        )

        controls_enabled = procedures_ready and not busy

        for element in self.procedure_inputs:
            element.set_enabled(controls_enabled)

        for button in self.procedure_buttons:
            button.set_enabled(controls_enabled)

        self.transfer_source_port_input.set_enabled(
            controls_enabled and bool(self.transfer_source_port_options)
        )
        self.transfer_destination_port_input.set_enabled(
            controls_enabled and bool(self.transfer_destination_port_options)
        )
        transfer_enabled = (
            controls_enabled
            and bool(self.transfer_source_port_options)
            and bool(self.transfer_destination_port_options)
        )

        self.single_aspiration_transfer_button.set_enabled(transfer_enabled)
        self.common_line_stroke_transfer_button.set_enabled(transfer_enabled)

    def _read_mixture_inputs(
        self,
    ) -> tuple[dict[str, float], float, int, WaterDistribution]:
        source_fractions = {
            source.key: read_non_negative_number(
                self.mixture_fraction_inputs[source.key].value,
                name=f"{source.label} fraction",
            )
            for source in self.mixture_sources
        }
        fraction_sum = math.fsum(source_fractions.values())
        if not math.isclose(fraction_sum, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError(
                f"Mixture fractions must sum to 1; got {fraction_sum:.12g}"
            )

        total_volume_ml = read_positive_number(
            self.mixture_volume_input.value, name="Total mixture volume"
        )
        rpm = read_rpm(self.mixture_rpm_input.value, name="Mixture RPM")

        distribution_value = self.mixture_water_distribution_input.value
        if distribution_value == "single":
            water_distribution: WaterDistribution = "single"
        elif distribution_value == "between_components":
            water_distribution = "between_components"
        else:
            raise ValueError("Water distribution is required")

        return source_fractions, total_volume_ml, rpm, water_distribution

    def _read_transfer_inputs(self) -> tuple[float, int, int, int]:
        volume_ml = read_positive_number(
            self.transfer_volume_input.value, name="Liquid volume"
        )
        rpm = read_rpm(self.transfer_rpm_input.value, name="Transfer RPM")
        source = read_positive_integer(
            self.transfer_source_port_input.value, name="Source port"
        )
        destination = read_positive_integer(
            self.transfer_destination_port_input.value, name="Destination port"
        )

        return volume_ml, rpm, source, destination

    @staticmethod
    def _read_optional_rpm(value: float | None, *, name: str) -> int | None:
        if value is None:
            return None

        return read_rpm(value, name=name)

    # Async UI callbacks ---

    async def _prime_all_liquid_source_lines(self) -> None:
        try:
            rpm = read_rpm(self.prime_rpm_input.value, name="Prime RPM")
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(self.controller.prime_all_liquid_source_lines, rpm=rpm),
            operation="Priming liquid source lines",
            success_message="Liquid source lines primed",
        )

    async def _prepare_mixture_with_single_aspiration(self) -> None:
        try:
            source_fractions, total_volume_ml, rpm, water_distribution = (
                self._read_mixture_inputs()
            )
            inlet_aspiration_rpm = self._read_optional_rpm(
                self.inlet_aspiration_rpm_input.value, name="IN aspiration RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.prepare_mixture_with_single_aspiration_transfers,
                source_fractions=source_fractions,
                total_volume_ml=total_volume_ml,
                rpm=rpm,
                inlet_aspiration_rpm=inlet_aspiration_rpm,
                water_distribution=water_distribution,
            ),
            operation="Preparing mixture with single-aspiration transfers",
            success_message="Single-aspiration mixture prepared",
        )

    async def _prepare_mixture_with_common_line_strokes(self) -> None:
        try:
            source_fractions, total_volume_ml, rpm, water_distribution = (
                self._read_mixture_inputs()
            )
            inlet_aspiration_rpm = self._read_optional_rpm(
                self.inlet_aspiration_rpm_input.value, name="IN aspiration RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.prepare_mixture_with_common_line_stroke_transfers,
                source_fractions=source_fractions,
                total_volume_ml=total_volume_ml,
                rpm=rpm,
                inlet_aspiration_rpm=inlet_aspiration_rpm,
                water_distribution=water_distribution,
            ),
            operation="Preparing mixture with common-line-stroke transfers",
            success_message="Common-line-stroke mixture prepared",
        )

    async def _transfer_liquid_with_single_aspiration(self) -> None:
        try:
            volume_ml, rpm, source, destination = self._read_transfer_inputs()
            inlet_aspiration_rpm = self._read_optional_rpm(
                self.inlet_aspiration_rpm_input.value, name="IN aspiration RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.transfer_liquid_to_route_with_single_aspiration,
                volume_ml=volume_ml,
                rpm=rpm,
                inlet_aspiration_rpm=inlet_aspiration_rpm,
                source_port=source,
                destination_port=destination,
            ),
            operation="Transferring liquid with one source aspiration",
            success_message="Single-aspiration liquid transfer completed",
        )

    async def _transfer_liquid_with_common_line_strokes(self) -> None:
        try:
            volume_ml, rpm, source, destination = self._read_transfer_inputs()
            inlet_aspiration_rpm = self._read_optional_rpm(
                self.inlet_aspiration_rpm_input.value, name="IN aspiration RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.transfer_liquid_to_route_with_common_line_strokes,
                volume_ml=volume_ml,
                rpm=rpm,
                inlet_aspiration_rpm=inlet_aspiration_rpm,
                source_port=source,
                destination_port=destination,
            ),
            operation="Transferring liquid with common-line strokes",
            success_message="Common-line-stroke liquid transfer completed",
        )

    async def _fill_flow_cell_from_mixing_vessel(self) -> None:
        try:
            volume_ml = read_positive_number(
                self.fill_flow_volume_input.value, name="Passing volume"
            )
            rpm = read_rpm(self.fill_flow_rpm_input.value, name="Handling RPM")
            approach_rpm = self._read_optional_rpm(
                self.fill_flow_approach_rpm_input.value, name="Approach RPM"
            )
            flow_rpm = read_rpm(self.fill_flow_cell_rpm_input.value, name="Flow RPM")
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.fill_flow_cell_from_mixing_vessel,
                volume_ml=volume_ml,
                rpm=rpm,
                approach_rpm=approach_rpm,
                flow_rpm=flow_rpm,
            ),
            operation="Filling flow-cell from mixing vessel",
            success_message="Flow-cell fill completed",
        )

    async def _pass_mixing_plug_through_flow_cell(self) -> None:
        try:
            volume_ml = read_positive_number(
                self.plug_volume_input.value, name="Plug volume"
            )
            rpm = read_rpm(self.plug_rpm_input.value, name="Handling RPM")
            approach_rpm = self._read_optional_rpm(
                self.plug_approach_rpm_input.value, name="Approach RPM"
            )
            flow_rpm = read_rpm(self.plug_flow_rpm_input.value, name="Flow RPM")
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.pass_mixing_plug_through_flow_cell,
                volume_ml=volume_ml,
                rpm=rpm,
                approach_rpm=approach_rpm,
                flow_rpm=flow_rpm,
            ),
            operation="Passing mixing-vessel plug through flow-cell",
            success_message="Mixing-vessel plug passed through flow-cell",
        )

    async def _drain_mixing_vessel_to_outlet(self) -> None:
        try:
            expected_volume_ml = read_non_negative_number(
                self.drain_volume_input.value, name="Expected vessel volume"
            )
            rpm = read_rpm(self.drain_rpm_input.value, name="Drain RPM")
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.drain_mixing_vessel_to_outlet,
                expected_vessel_volume_ml=expected_volume_ml,
                rpm=rpm,
            ),
            operation="Draining mixing vessel",
            success_message="Mixing vessel drained",
        )

    async def _flush_system(self) -> None:
        try:
            volume_ml = read_positive_number(
                self.flush_volume_input.value, name="Water volume"
            )
            rpm = read_rpm(self.flush_rpm_input.value, name="Flush RPM")
            cycles = read_positive_integer(self.flush_cycles_input.value, name="Cycles")
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.flush_system,
                volume_ml=volume_ml,
                rpm=rpm,
                cycles=cycles,
            ),
            operation="Flushing system",
            success_message="System flush completed",
        )

    async def _rinse_mixing_vessel(self) -> None:
        try:
            rinse_volume_ml = read_positive_number(
                self.rinse_vessel_volume_input.value, name="Water per rinse"
            )
            rpm = read_rpm(self.rinse_vessel_rpm_input.value, name="Rinse RPM")
            cycles = read_positive_integer(
                self.rinse_vessel_cycles_input.value, name="Cycles"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.rinse_mixing_vessel,
                rinse_volume_ml=rinse_volume_ml,
                rpm=rpm,
                cycles=cycles,
            ),
            operation="Rinsing mixing vessel",
            success_message="Mixing-vessel rinse completed",
        )

    async def _rinse_flow_cell_with_water(self) -> None:
        try:
            rinse_through_volume_ml = read_positive_number(
                self.rinse_flow_cell_volume_input.value, name="Rinse-through volume"
            )
            handling_rpm = read_rpm(
                self.rinse_flow_cell_handling_rpm_input.value, name="Handling RPM"
            )
            rinsing_rpm = read_rpm(
                self.rinse_flow_cell_rinsing_rpm_input.value, name="Rinsing RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.rinse_flow_cell_with_water,
                rinse_through_volume_ml=rinse_through_volume_ml,
                handling_rpm=handling_rpm,
                rinsing_rpm=rinsing_rpm,
            ),
            operation="Rinsing flow-cell with water",
            success_message="Flow-cell water rinse completed",
        )

    async def _flush_working_destination_routes(self) -> None:
        try:
            rinse_volume_ml = read_positive_number(
                self.working_routes_volume_input.value, name="Rinse volume per route"
            )
            rpm = read_rpm(self.working_routes_rpm_input.value, name="Flush RPM")
            flush_cycles = read_positive_integer(
                self.working_routes_cycles_input.value, name="Cycles"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.flush_working_destination_routes,
                rinse_volume_ml=rinse_volume_ml,
                rpm=rpm,
                flush_cycles=flush_cycles,
            ),
            operation="Flushing working destination routes",
            success_message="Working destination routes flushed",
        )

    async def _recover_source_lines_and_clear_flow_cell(self) -> None:
        try:
            handling_rpm = read_rpm(
                self.line_maintenance_handling_rpm_input.value, name="Handling RPM"
            )
            approach_rpm = read_rpm(
                self.line_maintenance_approach_rpm_input.value, name="Approach RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.recover_source_lines_and_clear_flow_cell,
                source_ports=self.line_maintenance_source_ports,
                handling_rpm=handling_rpm,
                approach_rpm=approach_rpm,
            ),
            operation="Recovering source lines and clearing flow-cell route",
            success_message="Sources recovered and flow-cell route cleared",
        )

    async def _rinse_source_lines(self) -> None:
        try:
            additional_volume_ml = read_non_negative_number(
                self.line_maintenance_additional_rinse_volume_input.value,
                name="Additional rinse per line",
            )
            rpm = read_rpm(
                self.line_maintenance_handling_rpm_input.value, name="Handling RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.rinse_source_lines_with_water,
                source_ports=self.line_maintenance_source_ports,
                additional_volume_ml=additional_volume_ml,
                rpm=rpm,
            ),
            operation="Water-rinsing source lines",
            success_message="Source lines rinsed with water",
        )

    async def _air_empty_source_lines(self) -> None:
        try:
            rpm = read_rpm(
                self.line_maintenance_handling_rpm_input.value, name="Handling RPM"
            )
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return

        await self.control_bar.run_operation(
            action=partial(
                self.controller.air_empty_source_lines,
                source_ports=self.line_maintenance_source_ports,
                rpm=rpm,
            ),
            operation="Air-emptying all source lines",
            success_message="All source lines and SY01B OUT air-emptied",
        )

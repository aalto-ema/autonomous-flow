from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, fields
from functools import partial
from typing import Any, ClassVar

from nicegui import run, ui
from nicegui.elements.number import Number

from common.validation import require_finite_number
from controllers.experiment_controller import (
    ExperimentCleanupSettings,
    ExperimentController,
    ExperimentProtocolEstimate,
    ExperimentWorkflowKind,
    SavedExperimentProtocol,
)
from fluidics.runze_fluidics import WaterDistribution
from gui.emstat_method_editor import EmStatMethodEditor, EmStatMethodEditorState
from gui.runze_input_validation import read_rpm
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    LinearSweepVoltammetrySettings,
)

logger = logging.getLogger(__name__)


@dataclass
class ExperimentProtocolPanelState(EmStatMethodEditorState):
    expanded: bool = True
    workflow_kind: ExperimentWorkflowKind = "continuous_flow"
    measurement_count: float | None = 1
    between_measurement_displacement_volume_ml: float | None = None
    handling_rpm: float | None = 75
    inlet_aspiration_rpm: float | None = 10
    approach_rpm: float | None = 5
    flow_rpm: float | None = 1
    water_distribution: WaterDistribution | None = "single"
    mixing_rinse_volume_ml: float | None = 1.0
    mixing_rinse_cycles: float | None = 2
    flow_cell_rinse_through_volume_ml: float | None = 1.0
    source_line_additional_rinse_volume_ml: float | None = 0.0
    estimate: ExperimentProtocolEstimate | None = None


class ExperimentProtocolPanel:
    """Shared measurement and fluid settings for manual and optimized runs."""

    WATER_DISTRIBUTION_OPTIONS: ClassVar[dict[WaterDistribution, str]] = {
        "single": "Single water transfer",
        "between_components": "Water between components",
    }
    WORKFLOW_OPTIONS: ClassVar[dict[ExperimentWorkflowKind, str]] = {
        "continuous_flow": "Continuous flow",
        "stopped_flow": "Stopped flow",
    }

    def __init__(
        self,
        controller: ExperimentController,
        *,
        state: ExperimentProtocolPanelState | None = None,
        on_estimate_change: Callable[[ExperimentProtocolEstimate | None], None],
        on_ui_operation_change: Callable[[str | None], None],
    ) -> None:
        self.controller = controller
        self.state = state if state is not None else ExperimentProtocolPanelState()
        if (
            self.state.workflow_kind == "stopped_flow"
            and self.state.measurement_count is not None
            and self.state.measurement_count > 1
            and self.state.between_measurement_displacement_volume_ml is None
        ):
            self.state.between_measurement_displacement_volume_ml = (
                controller.default_stopped_flow_displacement_volume_ml
            )
        self.on_estimate_change = on_estimate_change
        self.on_ui_operation_change = on_ui_operation_change
        self._system_busy = controller.busy
        self._ui_operation: str | None = None
        self._previewing_saved_protocol = False
        self._syncing_state = False

        self._build()
        self._register_state_handlers()
        self._show_estimate(self.state.estimate)
        self._sync_controls()

    def _build(self) -> None:
        with (
            ui.card().classes("w-full p-0 gap-0 experiment-protocol-panel"),
            (
                ui.expansion(
                    self._summary_text(),
                    value=self.state.expanded,
                    on_value_change=self._remember_expanded,
                )
                .classes("w-full")
                .props("header-class='text-lg font-bold px-4 py-3'")
            ) as self.expansion,
            ui.column().classes("w-full p-4 gap-4"),
        ):
            self.preview_label = ui.label("").classes(
                "hidden w-full rounded bg-blue-50 px-3 py-2 text-sm text-blue-900"
            )
            with ui.row().classes("w-full items-start gap-8 flex-wrap"):
                with (
                    ui.column()
                    .classes("gap-2")
                    .style("flex: 1 1 430px; min-width: min(100%, 430px);")
                ):
                    ui.label("Measurement").classes("text-lg font-bold")
                    self.method_editor = EmStatMethodEditor(
                        state=self.state,
                        on_change=self._invalidate_estimate,
                        panel_height_rem=16.5,
                    )

                with (
                    ui.column()
                    .classes("gap-3")
                    .style("flex: 1 1 360px; min-width: min(100%, 360px);")
                ):
                    ui.label("Fluid handling").classes("text-lg font-bold")
                    with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-3 items-end"):
                        self.workflow_select = ui.select(
                            options=self.WORKFLOW_OPTIONS,
                            value=self.state.workflow_kind,
                            label="Measurement workflow",
                        ).classes("w-full")
                        self.measurement_count_input = ui.number(
                            "Measurements per experiment",
                            value=self.state.measurement_count,
                            min=1,
                            step=1,
                            precision=0,
                        ).classes("w-full")
                        self.between_measurement_displacement_input = (
                            self._volume_input(
                                "Between measurements (mL)",
                                self.state.between_measurement_displacement_volume_ml,
                            ).tooltip(
                                "Mixture displacement after each stopped measurement "
                                "except the last"
                            )
                        )
                        self.handling_rpm_input = self._rpm_input(
                            "Handling RPM", self.state.handling_rpm
                        )
                        self.inlet_aspiration_rpm_input = self._rpm_input(
                            "IN aspiration RPM", self.state.inlet_aspiration_rpm
                        ).tooltip(
                            "Optional override for source-liquid aspiration during "
                            "Prepare mixture; blank uses Handling RPM"
                        )
                        self.flow_rpm_input = self._rpm_input(
                            "Flow RPM", self.state.flow_rpm
                        )
                        self.approach_rpm_input = self._rpm_input(
                            "Approach RPM", self.state.approach_rpm
                        ).tooltip(
                            "Blank uses Flow RPM for continuous flow; stopped flow "
                            "requires a value"
                        )

                        self.water_distribution_select = ui.select(
                            options=self.WATER_DISTRIBUTION_OPTIONS,
                            value=self.state.water_distribution,
                            label="Water distribution",
                        ).classes("w-full")

                    ui.label("Cleanup").classes("text-base font-bold mt-2")
                    with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-3 items-end"):
                        self.mixing_rinse_volume_input = self._volume_input(
                            "Mixing rinse (mL)", self.state.mixing_rinse_volume_ml
                        )
                        self.mixing_rinse_cycles_input = ui.number(
                            "Mixing rinse cycles",
                            value=self.state.mixing_rinse_cycles,
                            min=1,
                            step=1,
                            precision=0,
                        ).classes("w-full")
                        self.flow_cell_rinse_volume_input = self._volume_input(
                            "Flow-cell rinse (mL)",
                            self.state.flow_cell_rinse_through_volume_ml,
                        )
                        self.source_line_additional_rinse_volume_input = (
                            ui.number(
                                "Additional source-line rinse per line (mL)",
                                value=(
                                    self.state.source_line_additional_rinse_volume_ml
                                ),
                                min=0,
                                precision=3,
                            )
                            .props('step="any"')
                            .classes("w-full")
                            .tooltip(
                                "Added separately to every non-water source line "
                                "during final line maintenance"
                            )
                        )

            ui.separator()
            with ui.row().classes("w-full items-end gap-6 flex-wrap"):
                with (
                    ui.element("div")
                    .classes("flex-1")
                    .style(
                        "display: grid; "
                        "grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); "
                        "column-gap: 2rem; row-gap: 0.75rem;"
                    )
                ):
                    self.estimated_duration_value = self._build_value(
                        "Measurement / series time"
                    )
                    self.flow_rate_value = self._build_value("Continuous-flow rate")
                    self.measurement_volume_value = self._build_value(
                        "Flow / interval volume"
                    )
                    self.total_volume_value = self._build_value("Total mixture volume")

                self.update_estimates_button = ui.button(
                    "Update estimates", on_click=self._update_estimates
                ).classes("whitespace-nowrap")

            with (
                ui.expansion("Volume details")
                .classes("w-full")
                .props("dense header-class='text-sm font-medium px-0'"),
                ui.element("div")
                .classes("w-full pb-2")
                .style(
                    "display: grid; "
                    "grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); "
                    "column-gap: 2rem; row-gap: 0.75rem;"
                ),
            ):
                self.condition_volume_value = self._build_value(
                    "Mixing-path conditioning"
                )
                self.flow_cell_load_value = self._build_value("Flow-cell load")
                self.reserve_volume_value = self._build_value("Mixing-vessel reserve")

            self.status_label = ui.label("").classes("text-sm text-gray-600")

        self.inputs = [
            self.workflow_select,
            self.measurement_count_input,
            self.between_measurement_displacement_input,
            self.handling_rpm_input,
            self.inlet_aspiration_rpm_input,
            self.approach_rpm_input,
            self.flow_rpm_input,
            self.water_distribution_select,
            self.mixing_rinse_volume_input,
            self.mixing_rinse_cycles_input,
            self.flow_cell_rinse_volume_input,
            self.source_line_additional_rinse_volume_input,
        ]
        self._sync_workflow_controls()

    @staticmethod
    def _rpm_input(label: str, value: float | None) -> Number:
        return ui.number(label, value=value, min=1, step=1, precision=0).classes(
            "w-full"
        )

    @staticmethod
    def _volume_input(label: str, value: float | None) -> Number:
        return (
            ui.number(label, value=value, min=0.001, precision=3)
            .props('step="any"')
            .classes("w-full")
        )

    @staticmethod
    def _build_value(label: str):
        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-xs text-gray-500")
            return ui.label("—").classes("font-medium")

    def set_system_busy(self, busy: bool) -> None:
        self._system_busy = busy
        self._sync_controls()

    def capture_state(self) -> ExperimentProtocolPanelState:
        return deepcopy(self.state)

    def restore_state(self, saved_state: ExperimentProtocolPanelState) -> None:
        for item in fields(ExperimentProtocolPanelState):
            setattr(self.state, item.name, deepcopy(getattr(saved_state, item.name)))
        self._refresh_inputs_from_state()

    def show_saved_protocol(
        self, protocol: SavedExperimentProtocol, *, preview: bool
    ) -> None:
        self._write_saved_protocol(protocol)
        self.state.estimate = self._historical_estimate(protocol)
        self._previewing_saved_protocol = preview
        self._refresh_inputs_from_state()

    def focus(self) -> None:
        """Expand the protocol editor and smoothly bring it into view."""

        self.state.expanded = True
        self.expansion.set_value(True)
        ui.run_javascript(
            "requestAnimationFrame(() => "
            "document.querySelector('.experiment-protocol-panel')"
            "?.scrollIntoView({behavior: 'smooth', block: 'start'}));"
        )

    def finish_saved_protocol_use(self) -> None:
        self._previewing_saved_protocol = False
        self.state.estimate = None
        self.on_estimate_change(None)
        self._show_estimate(None)
        self._sync_controls()

    def set_previewing_saved_protocol(self, previewing: bool) -> None:
        self._previewing_saved_protocol = previewing
        self._sync_controls()

    def _write_saved_protocol(self, protocol: SavedExperimentProtocol) -> None:
        self.state.workflow_kind = protocol.workflow_kind
        self.state.measurement_count = protocol.measurement_count
        self.state.between_measurement_displacement_volume_ml = (
            protocol.between_measurement_displacement_volume_ml
        )
        self.state.measurement_type = protocol.measurement_type
        settings = protocol.measurement_settings
        if isinstance(settings, ChronoamperometrySettings):
            self.state.chronoamperometry_potential_v = settings.potential_v
            self.state.chronoamperometry_duration_s = settings.run_time_s
            self.state.chronoamperometry_interval_s = settings.interval_time_s
            self.state.chronoamperometry_equilibration_time_s = (
                settings.equilibration_time_s
            )
        elif isinstance(settings, CyclicVoltammetrySettings):
            self.state.cyclic_voltammetry_begin_potential_v = settings.begin_potential_v
            self.state.cyclic_voltammetry_vertex1_potential_v = (
                settings.vertex1_potential_v
            )
            self.state.cyclic_voltammetry_vertex2_potential_v = (
                settings.vertex2_potential_v
            )
            self.state.cyclic_voltammetry_step_potential_v = settings.step_potential_v
            self.state.cyclic_voltammetry_scan_rate_v_per_s = settings.scan_rate_v_per_s
            self.state.cyclic_voltammetry_scan_count = settings.scan_count
            self.state.cyclic_voltammetry_equilibration_time_s = (
                settings.equilibration_time_s
            )
        elif isinstance(settings, LinearSweepVoltammetrySettings):
            self.state.linear_sweep_voltammetry_begin_potential_v = (
                settings.begin_potential_v
            )
            self.state.linear_sweep_voltammetry_end_potential_v = (
                settings.end_potential_v
            )
            self.state.linear_sweep_voltammetry_step_potential_v = (
                settings.step_potential_v
            )
            self.state.linear_sweep_voltammetry_scan_rate_v_per_s = (
                settings.scan_rate_v_per_s
            )
            self.state.linear_sweep_voltammetry_sweep_count = settings.sweep_count
            self.state.linear_sweep_voltammetry_equilibration_time_s = (
                settings.equilibration_time_s
            )
        self.state.handling_rpm = protocol.handling_rpm
        self.state.inlet_aspiration_rpm = protocol.inlet_aspiration_rpm
        self.state.approach_rpm = protocol.approach_rpm
        self.state.flow_rpm = protocol.flow_rpm
        self.state.water_distribution = protocol.water_distribution
        self.state.mixing_rinse_volume_ml = protocol.mixing_rinse_volume_ml
        self.state.mixing_rinse_cycles = protocol.mixing_rinse_cycles
        self.state.flow_cell_rinse_through_volume_ml = (
            protocol.flow_cell_rinse_through_volume_ml
        )
        self.state.source_line_additional_rinse_volume_ml = (
            protocol.source_line_additional_rinse_volume_ml
        )

    def _historical_estimate(
        self, protocol: SavedExperimentProtocol
    ) -> ExperimentProtocolEstimate:
        values = protocol.historical_estimate
        estimated_duration_s = self._required_historical_float(
            values, "estimated_measurement_duration_s"
        )
        flow_rate_ml_min = (
            self._required_historical_float(values, "flow_rate_ml_min")
            if protocol.workflow_kind == "continuous_flow"
            else None
        )
        measurement_flow_volume_ml = (
            self._required_historical_float(values, "measurement_flow_volume_ml")
            if protocol.workflow_kind == "continuous_flow"
            else None
        )
        condition_volume_ml = self._required_historical_float(
            values, "mixing_path_condition_volume_ml"
        )
        flow_cell_load_volume_ml = self._required_historical_float(
            values, "flow_cell_load_volume_ml"
        )
        reserve_volume_ml = self._required_historical_float(
            values, "mixing_vessel_reserve_volume_ml"
        )
        total_volume_ml = self._required_historical_float(
            values, "total_mixture_volume_ml"
        )
        return ExperimentProtocolEstimate(
            measurement_type=protocol.measurement_type,
            measurement_settings=protocol.measurement_settings,
            handling_rpm=protocol.handling_rpm,
            inlet_aspiration_rpm=protocol.inlet_aspiration_rpm,
            approach_rpm=protocol.approach_rpm,
            flow_rpm=protocol.flow_rpm,
            water_distribution=protocol.water_distribution,
            cleanup_settings=ExperimentCleanupSettings(
                mixing_rinse_volume_ml=protocol.mixing_rinse_volume_ml,
                mixing_rinse_cycles=protocol.mixing_rinse_cycles,
                flow_cell_rinse_through_volume_ml=(
                    protocol.flow_cell_rinse_through_volume_ml
                ),
                source_line_additional_rinse_volume_ml=(
                    protocol.source_line_additional_rinse_volume_ml
                ),
            ),
            estimated_measurement_duration_s=estimated_duration_s,
            flow_rate_ml_min=flow_rate_ml_min,
            measurement_flow_volume_ml=measurement_flow_volume_ml,
            mixing_path_condition_volume_ml=condition_volume_ml,
            flow_cell_load_volume_ml=flow_cell_load_volume_ml,
            mixing_vessel_reserve_volume_ml=reserve_volume_ml,
            total_mixture_volume_ml=total_volume_ml,
            workflow_kind=protocol.workflow_kind,
            measurement_count=protocol.measurement_count,
            between_measurement_displacement_volume_ml=(
                protocol.between_measurement_displacement_volume_ml
            ),
        )

    @staticmethod
    def _required_historical_float(values: Mapping[str, object], key: str) -> float:
        value = values.get(key)
        if value is None:
            raise ValueError(f"Saved protocol is missing {key!r}")
        return float(require_finite_number(value, name=key))

    def _refresh_inputs_from_state(self) -> None:
        self._syncing_state = True
        try:
            self.method_editor.refresh_from_state()
            self.workflow_select.set_value(self.state.workflow_kind)
            self.measurement_count_input.set_value(self.state.measurement_count)
            self.between_measurement_displacement_input.set_value(
                self.state.between_measurement_displacement_volume_ml
            )
            self.water_distribution_select.set_value(self.state.water_distribution)
            bindings = (
                (self.handling_rpm_input, self.state.handling_rpm),
                (self.inlet_aspiration_rpm_input, self.state.inlet_aspiration_rpm),
                (self.approach_rpm_input, self.state.approach_rpm),
                (self.flow_rpm_input, self.state.flow_rpm),
                (self.mixing_rinse_volume_input, self.state.mixing_rinse_volume_ml),
                (self.mixing_rinse_cycles_input, self.state.mixing_rinse_cycles),
                (
                    self.flow_cell_rinse_volume_input,
                    self.state.flow_cell_rinse_through_volume_ml,
                ),
                (
                    self.source_line_additional_rinse_volume_input,
                    self.state.source_line_additional_rinse_volume_ml,
                ),
            )
            for element, value in bindings:
                element.set_value(value)
        finally:
            self._syncing_state = False
        self._sync_workflow_controls()
        self._show_estimate(self.state.estimate)
        self._sync_controls()

    def _register_state_handlers(self) -> None:
        bindings = (
            (self.measurement_count_input, "measurement_count"),
            (
                self.between_measurement_displacement_input,
                "between_measurement_displacement_volume_ml",
            ),
            (self.handling_rpm_input, "handling_rpm"),
            (self.inlet_aspiration_rpm_input, "inlet_aspiration_rpm"),
            (self.approach_rpm_input, "approach_rpm"),
            (self.flow_rpm_input, "flow_rpm"),
            (self.mixing_rinse_volume_input, "mixing_rinse_volume_ml"),
            (self.mixing_rinse_cycles_input, "mixing_rinse_cycles"),
            (self.flow_cell_rinse_volume_input, "flow_cell_rinse_through_volume_ml"),
            (
                self.source_line_additional_rinse_volume_input,
                "source_line_additional_rinse_volume_ml",
            ),
        )
        for element, attribute in bindings:
            element.on_value_change(partial(self._store_value, attribute))
        self.workflow_select.on_value_change(self._store_workflow_kind)
        self.water_distribution_select.on_value_change(self._store_water_distribution)

    def _remember_expanded(self, event: Any) -> None:
        self.state.expanded = bool(event.value)

    def _store_value(self, attribute: str, event: Any) -> None:
        if self._syncing_state:
            return
        setattr(self.state, attribute, event.value)
        if attribute == "measurement_count":
            self._ensure_between_measurement_default()
            self._sync_workflow_controls()
        self._invalidate_estimate()

    def _store_water_distribution(self, event: Any) -> None:
        if self._syncing_state:
            return
        value = event.value
        if value not in self.WATER_DISTRIBUTION_OPTIONS:
            return
        self.state.water_distribution = value
        self._invalidate_estimate()

    def _store_workflow_kind(self, event: Any) -> None:
        if self._syncing_state:
            return
        value = event.value
        if value not in self.WORKFLOW_OPTIONS:
            return
        self.state.workflow_kind = value
        if value == "continuous_flow":
            self.state.measurement_count = 1
            self._syncing_state = True
            try:
                self.measurement_count_input.set_value(1)
            finally:
                self._syncing_state = False
        self._ensure_between_measurement_default()
        self._sync_workflow_controls()
        self._invalidate_estimate()

    def _ensure_between_measurement_default(self) -> None:
        count = self.state.measurement_count
        if (
            self.state.workflow_kind != "stopped_flow"
            or count is None
            or count < 2
            or self.state.between_measurement_displacement_volume_ml is not None
        ):
            return
        displacement_ml = self.controller.default_stopped_flow_displacement_volume_ml
        self.state.between_measurement_displacement_volume_ml = displacement_ml
        self._syncing_state = True
        try:
            self.between_measurement_displacement_input.set_value(displacement_ml)
        finally:
            self._syncing_state = False

    def _sync_workflow_controls(self) -> None:
        stopped_flow = self.state.workflow_kind == "stopped_flow"
        multiple_measurements = (
            stopped_flow
            and self.state.measurement_count is not None
            and self.state.measurement_count > 1
        )
        self.measurement_count_input.set_visibility(stopped_flow)
        self.between_measurement_displacement_input.set_visibility(
            multiple_measurements
        )
        self.flow_rpm_input.set_visibility(not stopped_flow)

    def _invalidate_estimate(self) -> None:
        if self._syncing_state:
            return
        if self.state.estimate is not None:
            self.state.estimate = None
            self.on_estimate_change(None)
        if hasattr(self, "estimated_duration_value"):
            self._show_estimate(None)
            self._sync_controls()

    async def _update_estimates(self) -> None:
        try:
            arguments = self._read_estimate_arguments()
        except (TypeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return

        self._set_ui_operation("Updating protocol estimates")
        try:
            estimate = await run.io_bound(
                partial(self.controller.estimate_experiment_protocol, **arguments)
            )
            if estimate is None:
                raise RuntimeError("Protocol estimation returned no result")
        except Exception as error:
            logger.error(
                "Experiment protocol estimation failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
        else:
            self.state.estimate = estimate
            self.on_estimate_change(estimate)
            self._show_estimate(estimate)
            ui.notify("Protocol estimates updated", type="positive")
        finally:
            self._set_ui_operation(None)

    def _read_estimate_arguments(self) -> dict[str, object]:
        if self.state.water_distribution not in self.WATER_DISTRIBUTION_OPTIONS:
            raise ValueError("Water distribution is required")
        if self.state.workflow_kind not in self.WORKFLOW_OPTIONS:
            raise ValueError("Measurement workflow is required")
        measurement_count = (
            1
            if self.state.workflow_kind == "continuous_flow"
            else self._read_positive_integer(
                self.measurement_count_input.value, name="Measurements per experiment"
            )
        )
        between_measurement_displacement_volume_ml = (
            self._read_positive_number(
                self.between_measurement_displacement_input.value,
                name="Between-measurement displacement",
            )
            if self.state.workflow_kind == "stopped_flow" and measurement_count > 1
            else None
        )
        approach_value = self.approach_rpm_input.value
        approach_rpm = (
            None
            if approach_value is None
            else read_rpm(approach_value, name="Approach RPM")
        )
        inlet_aspiration_value = self.inlet_aspiration_rpm_input.value
        inlet_aspiration_rpm = (
            None
            if inlet_aspiration_value is None
            else read_rpm(inlet_aspiration_value, name="IN aspiration RPM")
        )
        return {
            "workflow_kind": self.state.workflow_kind,
            "measurement_count": measurement_count,
            "between_measurement_displacement_volume_ml": (
                between_measurement_displacement_volume_ml
            ),
            "measurement_settings": self.method_editor.read_settings(),
            "handling_rpm": read_rpm(
                self.handling_rpm_input.value, name="Handling RPM"
            ),
            "inlet_aspiration_rpm": inlet_aspiration_rpm,
            "approach_rpm": approach_rpm,
            "flow_rpm": (
                None
                if self.state.workflow_kind == "stopped_flow"
                else read_rpm(self.flow_rpm_input.value, name="Flow RPM")
            ),
            "water_distribution": self.state.water_distribution,
            "cleanup_settings": ExperimentCleanupSettings(
                mixing_rinse_volume_ml=self._read_positive_number(
                    self.mixing_rinse_volume_input.value, name="Mixing rinse volume"
                ),
                mixing_rinse_cycles=self._read_positive_integer(
                    self.mixing_rinse_cycles_input.value, name="Mixing rinse cycles"
                ),
                flow_cell_rinse_through_volume_ml=self._read_positive_number(
                    self.flow_cell_rinse_volume_input.value,
                    name="Flow-cell rinse volume",
                ),
                source_line_additional_rinse_volume_ml=(
                    self._read_non_negative_number(
                        self.source_line_additional_rinse_volume_input.value,
                        name="Additional source-line rinse volume",
                    )
                ),
            ),
        }

    @staticmethod
    def _read_positive_number(value: float | None, *, name: str) -> float:
        if value is None:
            raise ValueError(f"{name} is required")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError(f"{name} must be a positive finite number")
        return normalized

    @staticmethod
    def _read_positive_integer(value: float | None, *, name: str) -> int:
        normalized = ExperimentProtocolPanel._read_positive_number(value, name=name)
        if not normalized.is_integer():
            raise ValueError(f"{name} must be a whole number")
        return int(normalized)

    @staticmethod
    def _read_non_negative_number(value: float | None, *, name: str) -> float:
        if value is None:
            raise ValueError(f"{name} is required")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"{name} must be a non-negative finite number")
        return normalized

    def _show_estimate(self, estimate: ExperimentProtocolEstimate | None) -> None:
        if estimate is None:
            values = ("—",) * 7
        else:
            duration_text = self._format_duration(
                estimate.estimated_measurement_duration_s
            )
            if (
                estimate.workflow_kind == "stopped_flow"
                and estimate.measurement_count > 1
            ):
                series_duration_s = (
                    estimate.estimated_measurement_duration_s
                    * estimate.measurement_count
                )
                duration_text = (
                    f"{duration_text} each · "
                    f"{self._format_duration(series_duration_s)} series"
                )
            values = (
                duration_text,
                (
                    "—"
                    if estimate.flow_rate_ml_min is None
                    else f"{estimate.flow_rate_ml_min:.3f} mL/min"
                ),
                (
                    f"{estimate.between_measurement_displacement_volume_ml:.3f} mL"
                    if estimate.workflow_kind == "stopped_flow"
                    and estimate.between_measurement_displacement_volume_ml is not None
                    else "—"
                    if estimate.measurement_flow_volume_ml is None
                    else f"{estimate.measurement_flow_volume_ml:.3f} mL"
                ),
                f"{estimate.total_mixture_volume_ml:.3f} mL",
                f"{estimate.mixing_path_condition_volume_ml:.3f} mL",
                f"{estimate.flow_cell_load_volume_ml:.3f} mL",
                f"{estimate.mixing_vessel_reserve_volume_ml:.3f} mL",
            )
        labels = (
            self.estimated_duration_value,
            self.flow_rate_value,
            self.measurement_volume_value,
            self.total_volume_value,
            self.condition_volume_value,
            self.flow_cell_load_value,
            self.reserve_volume_value,
        )
        for label, value in zip(labels, values, strict=True):
            label.set_text(value)
        self.status_label.set_text(self._status_text())
        self.expansion.set_text(self._summary_text())

    def _summary_text(self) -> str:
        method = EmStatMethodEditor.METHOD_LABELS[self.state.measurement_type]
        workflow = self.WORKFLOW_OPTIONS[self.state.workflow_kind]
        measurement_count = self.state.measurement_count
        formatted_count = (
            "—" if measurement_count is None else f"{float(measurement_count):g}"
        )
        count = (
            ""
            if self.state.workflow_kind == "continuous_flow"
            else (
                f" · {formatted_count} measurement"
                f"{'s' if measurement_count != 1 else ''}"
            )
        )
        aspiration = (
            ""
            if self.state.inlet_aspiration_rpm is None
            else f" · aspiration {float(self.state.inlet_aspiration_rpm):g} RPM"
        )
        estimate = self.state.estimate
        if estimate is None:
            return (
                f"Experiment protocol · {workflow}{count}{aspiration} · {method} · "
                "estimates not calculated"
            )
        return (
            f"Experiment protocol · {workflow}{count}{aspiration} · {method} · "
            f"{self._format_duration(estimate.estimated_measurement_duration_s)} · "
            f"{estimate.total_mixture_volume_ml:.3f} mL"
        )

    def _status_text(self) -> str:
        if self._ui_operation is not None:
            return self._ui_operation
        if self._previewing_saved_protocol:
            return "Viewing a locked historical protocol"
        if self.state.estimate is None:
            return "Update estimates after changing protocol settings"
        return "Estimates are current"

    @staticmethod
    def _format_duration(duration_s: float) -> str:
        # Preserve PalmSens' sub-second estimate instead of rounding it up to
        # the next whole second (1.01 s must not be displayed as 0:02).
        rounded_s = round(duration_s, 3)
        hours = int(rounded_s // 3600)
        remainder_s = rounded_s - hours * 3600
        minutes = int(remainder_s // 60)
        seconds = remainder_s - minutes * 60
        if math.isclose(seconds, round(seconds), abs_tol=5e-4):
            seconds_text = f"{round(seconds):02d}"
        else:
            seconds_text = f"{seconds:06.3f}".rstrip("0").rstrip(".")
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds_text}"
        return f"{minutes:d}:{seconds_text}"

    @property
    def _busy(self) -> bool:
        return (
            self._system_busy
            or self._ui_operation is not None
            or self.controller.busy
            or self.controller.prepared_mixture is not None
        )

    def _sync_controls(self) -> None:
        busy = self._busy or self._previewing_saved_protocol
        self.method_editor.set_enabled(not busy)
        for element in self.inputs:
            element.set_enabled(not busy)
        self.update_estimates_button.set_enabled(not busy)
        self.status_label.set_text(self._status_text())
        self.expansion.set_text(self._summary_text())
        if self._previewing_saved_protocol:
            self.preview_label.set_text(
                f"Historical {self.WORKFLOW_OPTIONS[self.state.workflow_kind].lower()} "
                "protocol preview · missing values are shown blank"
            )
            self.preview_label.classes(remove="hidden")
        else:
            self.preview_label.classes(add="hidden")

    def _set_ui_operation(self, operation: str | None) -> None:
        self._ui_operation = operation
        self.on_ui_operation_change(operation)
        self._sync_controls()

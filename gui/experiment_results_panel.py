from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from functools import partial
from typing import Literal, cast

from nicegui import run, ui
from nicegui.elements.button import Button
from nicegui.elements.number import Number
from nicegui.elements.select import Select
from nicegui.events import GenericEventArguments, ValueChangeEventArguments

from analysis.measurement_objectives import (
    DEFAULT_OBJECTIVE,
    OBJECTIVE_DEFINITIONS,
    ObjectiveName,
)
from controllers.experiment_controller import (
    ExperimentController,
    ExperimentObjectiveSummary,
    ExperimentProtocolEstimate,
    MeasurementObjectiveSummary,
    ResultSourceScope,
)
from storage.experiment_repository import (
    WATER_SUBSTANCE_KEY,
    CurrentSourceConfiguration,
    ExperimentRepository,
    MeasurementType,
    MixtureConcentrationConstraint,
    SavedExperimentMeasurement,
    SavedExperimentRun,
)

logger = logging.getLogger(__name__)

ConcentrationUnit = Literal["M", "mM", "µM"]
ResultFlowMode = Literal["all", "continuous_flow", "stopped_flow"]

_MEASUREMENT_TYPE_OPTIONS: dict[MeasurementType, str] = {
    "chronoamperometry": "Chronoamperometry",
    "cyclic_voltammetry": "Cyclic voltammetry",
    "linear_sweep_voltammetry": "Linear sweep voltammetry",
}
_FLOW_MODE_OPTIONS: dict[ResultFlowMode, str] = {
    "all": "All flow modes",
    "continuous_flow": "Continuous flow",
    "stopped_flow": "Stopped flow",
}
_FLOW_MODE_PROTOCOL_KEYS: dict[ResultFlowMode, str | None] = {
    "all": None,
    "continuous_flow": "liquid_filled_flow_cell",
    "stopped_flow": "stopped_flow_stationary",
}
_SCOPE_LABELS: dict[ResultSourceScope, str] = {
    "all_setups": "All setups",
    "same_components": "Same component layout",
    "same_relative_formulation": "Same relative stock formulation",
    "same_exact_composition": "Same exact stock composition",
    "current_stocks": "Current stock records",
}
_CONCENTRATION_UNIT_OPTIONS: dict[ConcentrationUnit, str] = {
    "M": "M",
    "mM": "mM",
    "µM": "µM",
}
_CONCENTRATION_TO_MOL_L: dict[ConcentrationUnit, float] = {
    "M": 1.0,
    "mM": 1e-3,
    "µM": 1e-6,
}
_MEASUREMENT_COLORS = ("#1976d2", "#7b1fa2", "#00897b", "#ef6c00", "#c62828", "#455a64")


@dataclass
class ResultConcentrationFilterState:
    substance_id: int | None = None
    target: float | None = None
    tolerance: float | None = None
    unit: ConcentrationUnit = "mM"


@dataclass
class ExperimentResultsPanelState:
    objective_name: ObjectiveName = DEFAULT_OBJECTIVE
    measurement_type: MeasurementType = "chronoamperometry"
    flow_mode: ResultFlowMode = "all"
    scope: ResultSourceScope = "current_stocks"
    compatible_protocol_only: bool = False
    concentration_filters: list[ResultConcentrationFilterState] = field(
        default_factory=list
    )
    selected_mixture_id: int | None = None
    selected_measurement_id: int | None = None
    detail_level: Literal["experiment", "measurement"] = "experiment"


class ExperimentResultsPanel:
    """Browse versioned objectives and inspect the selected mixture composition."""

    def __init__(
        self,
        controller: ExperimentController,
        *,
        state: ExperimentResultsPanelState | None = None,
        protocol_estimate: ExperimentProtocolEstimate | None = None,
        on_busy_change: Callable[[bool], None],
    ) -> None:
        self.controller = controller
        self.state = state if state is not None else ExperimentResultsPanelState()
        self.on_busy_change = on_busy_change
        self._protocol_estimate = protocol_estimate
        self._system_busy = controller.busy
        self._ui_busy = False
        self._summaries: tuple[ExperimentObjectiveSummary, ...] = ()
        self._selected_summary: ExperimentObjectiveSummary | None = None
        self._source_scope_options: dict[ResultSourceScope, str] = {}
        self._substance_options: dict[int, str] = {}
        self._concentration_filter_controls: list[Select | Number | Button] = []

        self._refresh_filter_options()
        self._build()
        self.refresh()
        self._sync_controls()

    def _build(self) -> None:
        with ui.column().classes("w-full gap-3 experiment-results-panel"):
            with ui.row().classes("w-full items-end gap-4 flex-wrap"):
                self.objective_select = (
                    ui.select(
                        {
                            name: definition.label
                            for name, definition in OBJECTIVE_DEFINITIONS.items()
                            if self.state.measurement_type
                            in definition.supported_measurement_types
                        },
                        label="Objective",
                        value=self.state.objective_name,
                        on_change=self._filters_changed,
                    )
                    .classes("w-64")
                    .props("options-dense")
                )
                self.measurement_type_select = (
                    ui.select(
                        _MEASUREMENT_TYPE_OPTIONS,
                        label="Measurement type",
                        value=self.state.measurement_type,
                        on_change=self._filters_changed,
                    )
                    .classes("w-64")
                    .props("options-dense")
                )
                self.flow_mode_select = (
                    ui.select(
                        _FLOW_MODE_OPTIONS,
                        label="Flow mode",
                        value=self.state.flow_mode,
                        on_change=self._filters_changed,
                    )
                    .classes("w-48")
                    .props("options-dense")
                )
                self.scope_select = (
                    ui.select(
                        self._source_scope_options,
                        label="Source scope",
                        value=self.state.scope,
                        on_change=self._filters_changed,
                    )
                    .classes("w-56")
                    .props("options-dense")
                )
                self.compatibility_switch = ui.switch(
                    "Current protocol only",
                    value=self.state.compatible_protocol_only,
                    on_change=self._filters_changed,
                )
                ui.space()
                self.analyze_button = ui.button(
                    "Analyze completed experiments",
                    icon="analytics",
                    on_click=self._analyze_saved_measurements,
                ).props("outline")
                self.refresh_button = (
                    ui.button(icon="refresh", on_click=self.refresh)
                    .props("flat round")
                    .tooltip("Refresh results")
                )

            self.status_label = ui.label("").classes("text-sm text-gray-600")

            with (
                ui.expansion(
                    "Final concentration filters",
                    value=bool(self.state.concentration_filters),
                )
                .classes("w-full")
                .props("dense header-class='text-sm font-medium px-0'"),
                ui.column().classes("w-full gap-2 pb-2"),
            ):
                self.concentration_filters_container = ui.column().classes(
                    "w-full gap-2"
                )
                with ui.row().classes("w-full items-center justify-end gap-2"):
                    self.add_concentration_filter_button = ui.button(
                        "Add component constraint",
                        icon="add",
                        on_click=self._add_concentration_filter,
                    ).props("flat")
            self._render_concentration_filters()

            self._build_results_overview()
            with ui.row().classes("w-full items-stretch gap-6 flex-wrap"):
                self._build_mixture_detail()
                self._build_analysis_detail()

    def _build_results_overview(self) -> None:
        with ui.column().classes("w-full min-w-0 gap-2"):
            ui.label("Objective results").classes("text-lg font-bold")
            self.results_chart = (
                ui.echart(
                    {
                        "animation": False,
                        "grid": {"left": 72, "right": 24, "top": 24, "bottom": 58},
                        "tooltip": {
                            "trigger": "item",
                            ":formatter": self._result_tooltip_formatter(),
                        },
                        "xAxis": {
                            "type": "value",
                            "name": "Experiment",
                            "nameLocation": "middle",
                            "nameGap": 32,
                            "minInterval": 1,
                            "scale": True,
                        },
                        "yAxis": {
                            "type": "value",
                            "name": "Objective (µA)",
                            "nameLocation": "middle",
                            "nameGap": 52,
                            "scale": True,
                        },
                        "series": [
                            {
                                "name": "Experiment mean",
                                "type": "scatter",
                                "symbolSize": 10,
                                "data": [],
                            },
                            {
                                "name": "Between-measurement SD",
                                "type": "custom",
                                "silent": True,
                                ":renderItem": self._error_bar_renderer(),
                                "data": [],
                            },
                        ],
                    }
                )
                .classes("w-full")
                .style("height: 22rem; min-height: 18rem;")
            )
            self.results_chart.on("chart:click", self._point_clicked, ["data", "value"])

    def _build_mixture_detail(self) -> None:
        with (
            ui.column()
            .classes("gap-2 min-w-0 self-stretch")
            .style("flex: 1 1 360px; min-width: min(100%, 360px);")
        ):
            with ui.row().classes("w-full items-center").style("min-height: 2rem;"):
                ui.label("Selected experiment mixture").classes("text-lg font-bold")
            self.selection_label = ui.label("No compatible experiment selected").style(
                "min-height: 1.5rem;"
            )
            self.composition_chart = (
                ui.echart(
                    {
                        "animation": False,
                        "grid": {
                            "left": 110,
                            "right": 32,
                            "top": 12,
                            "bottom": 42,
                            "containLabel": True,
                        },
                        "tooltip": {"trigger": "item"},
                        "xAxis": {
                            "type": "value",
                            "name": "Concentration",
                            "nameLocation": "middle",
                            "nameGap": 28,
                        },
                        "yAxis": {"type": "category", "data": []},
                        "series": [
                            {
                                "name": "Final concentration",
                                "type": "bar",
                                "data": [],
                                "barMaxWidth": 28,
                            }
                        ],
                    }
                )
                .classes("w-full")
                .style("height: 18rem; min-height: 16rem;")
            )
            ui.label("Stock fractions").classes("font-medium")
            self.fractions_container = ui.column().classes("w-full gap-1")

    def _build_analysis_detail(self) -> None:
        with (
            ui.column()
            .classes("gap-2 min-w-0 self-stretch")
            .style("flex: 2 1 680px; min-width: min(100%, 680px);")
        ):
            with (
                ui.row().classes("w-full items-center gap-2").style("min-height: 2rem;")
            ):
                self.detail_back_button = ui.button(
                    icon="arrow_back", on_click=self._show_selected_experiment
                ).props("flat round dense")
                self.detail_title = ui.label("Experiment detail").classes(
                    "text-lg font-bold"
                )
            self.detail_summary = (
                ui.label("")
                .classes("text-sm text-gray-600")
                .style("min-height: 1.5rem;")
            )
            self.detail_chart = (
                ui.echart(
                    {
                        "animation": False,
                        "grid": {"left": 72, "right": 24, "top": 24, "bottom": 58},
                        "tooltip": {
                            "trigger": "item",
                            ":formatter": self._detail_tooltip_formatter(),
                        },
                        "xAxis": {
                            "type": "value",
                            "name": "Measurement sequence",
                            "nameLocation": "middle",
                            "nameGap": 32,
                            "minInterval": 1,
                        },
                        "yAxis": {
                            "type": "value",
                            "name": "Objective",
                            "nameLocation": "middle",
                            "nameGap": 52,
                            "scale": True,
                        },
                        "series": [
                            {
                                "name": "Measurement mean",
                                "type": "scatter",
                                "symbolSize": 11,
                                "data": [],
                            }
                        ],
                    }
                )
                .classes("w-full")
                .style("height: 18rem; min-height: 16rem;")
            )
            self.detail_chart.on(
                "chart:click", self._detail_point_clicked, ["data", "value"]
            )
            ui.label("Selected measurement objectives").classes("font-medium")
            self.objective_details_container = ui.column().classes("w-full gap-1")

    def _refresh_filter_options(self) -> None:
        available_scopes = self.controller.available_result_source_scopes()
        self._source_scope_options = {
            scope: _SCOPE_LABELS[scope] for scope in available_scopes
        }
        if self.state.scope not in self._source_scope_options:
            self.state.scope = (
                "same_components"
                if "same_components" in self._source_scope_options
                else "all_setups"
            )

        self._substance_options = {
            substance.id: substance.label
            for substance in self.controller.repository.list_substances()
            if substance.key != WATER_SUBSTANCE_KEY
        }
        valid_substance_ids = set(self._substance_options)
        for item in self.state.concentration_filters:
            if item.substance_id not in valid_substance_ids:
                item.substance_id = None

    def show_source_configuration(
        self, _configuration: CurrentSourceConfiguration
    ) -> None:
        """Refresh setup-dependent scopes after saving source assignments."""

        self._refresh_filter_options()
        self.scope_select.set_options(
            self._source_scope_options, value=self.state.scope
        )
        self._render_concentration_filters()
        self.refresh()

    def _render_concentration_filters(self) -> None:
        self.concentration_filters_container.clear()
        self._concentration_filter_controls.clear()
        with self.concentration_filters_container:
            if not self.state.concentration_filters:
                ui.label("No final-concentration constraints").classes(
                    "text-sm text-gray-500"
                )
                return

            for index, item in enumerate(self.state.concentration_filters):
                with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                    substance_select = (
                        ui.select(
                            self._substance_options,
                            label="Component",
                            value=item.substance_id,
                            on_change=partial(
                                self._concentration_substance_changed, index
                            ),
                        )
                        .classes("min-w-56 flex-1")
                        .props("options-dense")
                    )
                    target_input = ui.number(
                        "Target",
                        value=item.target,
                        min=0,
                        step=0.01,
                        on_change=partial(self._concentration_target_changed, index),
                    ).classes("w-36")
                    tolerance_input = ui.number(
                        "Tolerance",
                        value=item.tolerance,
                        min=0,
                        step=0.01,
                        on_change=partial(self._concentration_tolerance_changed, index),
                    ).classes("w-36")
                    unit_select = (
                        ui.select(
                            _CONCENTRATION_UNIT_OPTIONS,
                            label="Unit",
                            value=item.unit,
                            on_change=partial(self._concentration_unit_changed, index),
                        )
                        .classes("w-24")
                        .props("options-dense")
                    )
                    remove_button = (
                        ui.button(
                            icon="close",
                            on_click=partial(self._remove_concentration_filter, index),
                        )
                        .props("flat round dense color=grey")
                        .tooltip("Remove concentration constraint")
                    )
                    self._concentration_filter_controls.extend(
                        (
                            substance_select,
                            target_input,
                            tolerance_input,
                            unit_select,
                            remove_button,
                        )
                    )
        self._sync_controls()

    def _add_concentration_filter(self) -> None:
        used_ids = {
            item.substance_id
            for item in self.state.concentration_filters
            if item.substance_id is not None
        }
        available_id = next(
            (
                substance_id
                for substance_id in self._substance_options
                if substance_id not in used_ids
            ),
            None,
        )
        self.state.concentration_filters.append(
            ResultConcentrationFilterState(
                substance_id=available_id, target=None, tolerance=0.0
            )
        )
        self._render_concentration_filters()

    def _remove_concentration_filter(self, index: int) -> None:
        if 0 <= index < len(self.state.concentration_filters):
            self.state.concentration_filters.pop(index)
            self._render_concentration_filters()
            self.refresh()

    def _concentration_substance_changed(
        self, index: int, event: ValueChangeEventArguments
    ) -> None:
        value = event.value
        if value is not None and not isinstance(value, int):
            return
        if value is not None and any(
            other_index != index and item.substance_id == value
            for other_index, item in enumerate(self.state.concentration_filters)
        ):
            ui.notify(
                "A component can have only one concentration constraint", type="warning"
            )
            self._render_concentration_filters()
            return
        self.state.concentration_filters[index].substance_id = value
        self.refresh()

    def _concentration_target_changed(
        self, index: int, event: ValueChangeEventArguments
    ) -> None:
        value = event.value
        self.state.concentration_filters[index].target = (
            None if value is None else float(value)
        )
        self.refresh()

    def _concentration_tolerance_changed(
        self, index: int, event: ValueChangeEventArguments
    ) -> None:
        value = event.value
        self.state.concentration_filters[index].tolerance = (
            None if value is None else float(value)
        )
        self.refresh()

    def _concentration_unit_changed(
        self, index: int, event: ValueChangeEventArguments
    ) -> None:
        value = event.value
        if value not in _CONCENTRATION_UNIT_OPTIONS:
            return
        item = self.state.concentration_filters[index]
        new_unit = cast(ConcentrationUnit, value)
        old_scale = _CONCENTRATION_TO_MOL_L[item.unit]
        new_scale = _CONCENTRATION_TO_MOL_L[new_unit]
        if item.target is not None:
            item.target = item.target * old_scale / new_scale
        if item.tolerance is not None:
            item.tolerance = item.tolerance * old_scale / new_scale
        item.unit = new_unit
        self._render_concentration_filters()
        self.refresh()

    def set_system_busy(self, busy: bool) -> None:
        self._system_busy = busy
        self._sync_controls()

    def show_protocol_estimate(
        self, estimate: ExperimentProtocolEstimate | None
    ) -> None:
        self._protocol_estimate = estimate
        if self.state.compatible_protocol_only:
            self.refresh()

    def refresh(self) -> None:
        self._read_state_from_controls()
        objective_options = {
            name: definition.label
            for name, definition in OBJECTIVE_DEFINITIONS.items()
            if self.state.measurement_type in definition.supported_measurement_types
        }
        if self.state.objective_name not in objective_options:
            self.state.objective_name = next(iter(objective_options))
        if self.objective_select.options != objective_options:
            self.objective_select.set_options(
                objective_options, value=self.state.objective_name
            )
        definition = OBJECTIVE_DEFINITIONS[self.state.objective_name]
        settings_fingerprint: str | None = None
        protocol_key = _FLOW_MODE_PROTOCOL_KEYS[self.state.flow_mode]

        if self.state.compatible_protocol_only:
            estimate = self._protocol_estimate
            if (
                estimate is None
                or estimate.measurement_type != self.state.measurement_type
            ):
                self._summaries = ()
                self._update_results_chart()
                self.status_label.set_text(
                    "Update estimates for the selected measurement type to apply "
                    "the protocol filter"
                )
                self._clear_selection()
                return

            settings_fingerprint = ExperimentRepository.settings_fingerprint(
                cast(Mapping[str, object], asdict(estimate.measurement_settings))
            )
            current_protocol_key = (
                "stopped_flow_stationary"
                if estimate.workflow_kind == "stopped_flow"
                else "liquid_filled_flow_cell"
            )
            if protocol_key is not None and protocol_key != current_protocol_key:
                self._summaries = ()
                self._update_results_chart()
                self.status_label.set_text(
                    "The selected flow mode does not match the current protocol"
                )
                self._clear_selection()
                return
            protocol_key = current_protocol_key

        self._summaries = self.controller.objective_experiment_summaries(
            measurement_type=self.state.measurement_type,
            objective_name=self.state.objective_name,
            source_scope=self.state.scope,
            concentration_constraints=self._concentration_constraints(),
            settings_fingerprint=settings_fingerprint,
            protocol_key=protocol_key,
        )
        self.results_chart.options["yAxis"]["name"] = (
            f"{definition.label} ({definition.unit})"
        )
        self._update_results_chart()
        count = len(self._summaries)
        suffix = "experiment" if count == 1 else "experiments"
        self.status_label.set_text(
            f"{count} compatible {suffix} · analysis {definition.analysis_version}"
        )
        self._restore_or_select_latest()

    def _concentration_constraints(self) -> tuple[MixtureConcentrationConstraint, ...]:
        constraints: list[MixtureConcentrationConstraint] = []
        for item in self.state.concentration_filters:
            if item.substance_id is None or item.target is None:
                continue
            scale = _CONCENTRATION_TO_MOL_L[item.unit]
            constraints.append(
                MixtureConcentrationConstraint(
                    substance_id=item.substance_id,
                    target_mol_l=item.target * scale,
                    tolerance_mol_l=(item.tolerance or 0.0) * scale,
                )
            )
        return tuple(constraints)

    async def _analyze_saved_measurements(self) -> None:
        self._set_ui_busy(True)
        try:
            result = await run.io_bound(
                self.controller.analyze_completed_measurements,
                measurement_type=self.state.measurement_type,
                objective_name=self.state.objective_name,
            )
        except Exception as error:
            logger.error(
                "Failed to analyze completed experiment measurements: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
        else:
            if result is None:
                ui.notify(
                    "Saved-measurement analysis returned no result", type="negative"
                )
                return
            if result.errors:
                ui.notify(
                    f"Analyzed {result.analyzed_count}; "
                    f"{len(result.errors)} session(s) failed",
                    type="warning",
                )
            else:
                ui.notify(
                    f"Analyzed {result.analyzed_count} completed measurement(s)",
                    type="positive",
                )
            self.refresh()
        finally:
            self._set_ui_busy(False)

    def _filters_changed(self) -> None:
        self.refresh()

    def _read_state_from_controls(self) -> None:
        objective = self.objective_select.value
        measurement_type = self.measurement_type_select.value
        flow_mode = self.flow_mode_select.value
        scope = self.scope_select.value
        if objective in OBJECTIVE_DEFINITIONS:
            self.state.objective_name = cast(ObjectiveName, objective)
        if measurement_type in _MEASUREMENT_TYPE_OPTIONS:
            self.state.measurement_type = cast(MeasurementType, measurement_type)
        if flow_mode in _FLOW_MODE_OPTIONS:
            self.state.flow_mode = cast(ResultFlowMode, flow_mode)
        if scope in self._source_scope_options:
            self.state.scope = cast(ResultSourceScope, scope)
        self.state.compatible_protocol_only = bool(self.compatibility_switch.value)

    def _update_results_chart(self) -> None:
        self.results_chart.options["series"][0]["data"] = [
            {
                "value": [summary.experiment_id, summary.value],
                "experimentId": summary.experiment_id,
                "measurementCount": len(summary.measurements),
                "standardDeviation": summary.between_measurement_standard_deviation,
                "unit": summary.unit,
            }
            for summary in self._summaries
        ]
        self.results_chart.options["series"][1]["data"] = [
            [
                summary.experiment_id,
                summary.value - summary.between_measurement_standard_deviation,
                summary.value + summary.between_measurement_standard_deviation,
            ]
            for summary in self._summaries
            if summary.between_measurement_standard_deviation is not None
        ]
        self.results_chart.update()

    def _point_clicked(self, event: GenericEventArguments) -> None:
        data = event.args.get("data")
        if not isinstance(data, dict):
            return
        experiment_id = data.get("experimentId")
        if not isinstance(experiment_id, int):
            return

        summary = next(
            (item for item in self._summaries if item.experiment_id == experiment_id),
            None,
        )
        if summary is None:
            return

        self.state.selected_mixture_id = experiment_id
        self.state.selected_measurement_id = None
        self._show_experiment(summary)

    def _restore_or_select_latest(self) -> None:
        summary = next(
            (
                item
                for item in self._summaries
                if item.experiment_id == self.state.selected_mixture_id
            ),
            None,
        )
        if summary is None:
            if not self._summaries:
                self._clear_selection()
                return
            summary = self._summaries[-1]
            self.state.selected_mixture_id = summary.experiment_id
            self.state.selected_measurement_id = None
        selected_measurement = next(
            (
                item
                for item in summary.measurements
                if item.measurement_id == self.state.selected_measurement_id
            ),
            None,
        )
        if selected_measurement is None or self.state.detail_level == "experiment":
            self._show_experiment(summary)
        else:
            self._show_measurement(summary, selected_measurement)

    def _show_experiment(self, summary: ExperimentObjectiveSummary) -> None:
        self._selected_summary = summary
        self.state.selected_mixture_id = summary.experiment_id
        self.state.selected_measurement_id = None
        self.state.detail_level = "experiment"
        self.selection_label.set_text(f"Experiment {summary.experiment_id}")
        self._show_composition(summary)
        definition = OBJECTIVE_DEFINITIONS[self.state.objective_name]
        self.detail_title.set_text(f"Experiment {summary.experiment_id} · measurements")
        deviation = summary.between_measurement_standard_deviation
        deviation_text = "—" if deviation is None else f"{deviation:.6g} {summary.unit}"
        completed_count = sum(
            item.status == "completed" for item in summary.measurement_statuses
        )
        total_count = len(summary.measurement_statuses) or len(summary.measurements)
        self.detail_summary.set_text(
            f"Mean {summary.value:.6g} {summary.unit} · SD {deviation_text} · "
            f"{completed_count or len(summary.measurements)}/{total_count} completed"
        )
        self.detail_back_button.set_enabled(False)
        self.detail_chart.options["xAxis"]["name"] = "Measurement sequence"
        self.detail_chart.options["yAxis"]["name"] = (
            f"{definition.label} ({definition.unit})"
        )
        self.detail_chart.options["series"][0]["name"] = "Measurement mean"
        self.detail_chart.options["series"][0]["data"] = [
            {
                "value": [item.sequence_number, item.value],
                "measurementId": item.measurement_id,
                "sequenceNumber": item.sequence_number,
                "unit": item.unit,
                "standardError": item.within_measurement_standard_error,
                "itemStyle": {
                    "color": _MEASUREMENT_COLORS[
                        (item.sequence_number - 1) % len(_MEASUREMENT_COLORS)
                    ]
                },
            }
            for item in summary.measurements
        ]
        self.detail_chart.update()
        self.objective_details_container.clear()
        with self.objective_details_container:
            ui.label("Select a measurement to inspect all of its objectives.").classes(
                "text-sm text-gray-500"
            )
            for item in summary.measurement_statuses:
                ui.label(
                    f"Measurement {item.sequence_number}: "
                    f"{item.status.replace('_', ' ').title()}"
                ).classes("text-xs text-gray-500")

    def _show_measurement(
        self,
        summary: ExperimentObjectiveSummary,
        measurement: MeasurementObjectiveSummary,
    ) -> None:
        self._selected_summary = summary
        self.state.selected_mixture_id = summary.experiment_id
        self.state.selected_measurement_id = measurement.measurement_id
        self.state.detail_level = "measurement"
        self._show_composition(summary)
        self.selection_label.set_text(
            f"Experiment {summary.experiment_id} · measurement "
            f"{measurement.sequence_number}"
        )
        definition = OBJECTIVE_DEFINITIONS[self.state.objective_name]
        self.detail_title.set_text(
            f"Experiment {summary.experiment_id} › measurement "
            f"{measurement.sequence_number}"
        )
        contribution_status = (
            ""
            if measurement.contributions
            else " · scan/sweep detail unavailable until reanalysis"
        )
        self.detail_summary.set_text(
            f"Mean {measurement.value:.6g} {measurement.unit} · "
            f"analysis {definition.analysis_version}{contribution_status}"
        )
        self.detail_back_button.set_enabled(True)
        contribution_kind = (
            measurement.contributions[0].kind
            if measurement.contributions
            else "scan/sweep"
        )
        self.detail_chart.options["xAxis"]["name"] = (
            f"{contribution_kind.title()} sequence"
        )
        self.detail_chart.options["yAxis"]["name"] = (
            f"{definition.component_label} ({definition.unit})"
        )
        self.detail_chart.options["series"][0]["name"] = (
            f"{contribution_kind.title()} value"
        )
        self.detail_chart.options["series"][0]["data"] = [
            {
                "value": [item.sequence_number, item.value],
                "componentKind": item.kind,
                "unit": measurement.unit,
                "itemStyle": {
                    "color": _MEASUREMENT_COLORS[
                        (measurement.sequence_number - 1) % len(_MEASUREMENT_COLORS)
                    ]
                },
            }
            for item in measurement.contributions
        ]
        self.detail_chart.update()
        self._show_measurement_objectives(measurement.measurement_id)

    def _show_composition(self, summary: ExperimentObjectiveSummary) -> None:

        concentrations = self.controller.repository.mixture_substance_concentrations(
            summary.experiment_id
        )
        scale, unit = self._concentration_scale(
            tuple(item.concentration_mol_l for item in concentrations)
        )
        self.composition_chart.options["xAxis"]["name"] = f"Concentration ({unit})"
        self.composition_chart.options["yAxis"]["data"] = [
            item.substance_label for item in concentrations
        ]
        self.composition_chart.options["series"][0]["data"] = [
            item.concentration_mol_l * scale for item in concentrations
        ]
        self.composition_chart.update()

        stock_labels = {
            stock.id: stock.label
            for stock in self.controller.repository.list_source_stocks()
        }
        self.fractions_container.clear()
        with self.fractions_container:
            if not summary.fractions:
                ui.label("No recorded source fractions").classes(
                    "text-sm text-gray-500"
                )
            for fraction in summary.fractions:
                label = (
                    stock_labels.get(fraction.source_stock_id, "Unknown stock")
                    if fraction.source_stock_id is not None
                    else "Water"
                )
                ui.label(f"{label}: {fraction.fraction:.4f}").classes("text-sm")

    def _show_measurement_objectives(self, measurement_id: int) -> None:
        metrics = {
            metric.metric_name: metric
            for metric in self.controller.current_measurement_objectives(
                measurement_id=measurement_id
            )
        }
        self.objective_details_container.clear()
        with self.objective_details_container:
            for name, definition in OBJECTIVE_DEFINITIONS.items():
                if self.state.measurement_type not in (
                    definition.supported_measurement_types
                ):
                    continue
                metric = metrics.get(name)
                text = (
                    f"{definition.label}: Not analyzed"
                    if metric is None
                    else f"{definition.label}: {metric.value:.6g} {metric.unit} "
                    f"· analysis {metric.analysis_version}"
                )
                classes = "text-sm"
                if name == self.state.objective_name:
                    classes += " font-bold"
                ui.label(text).classes(classes)

    def _detail_point_clicked(self, event: GenericEventArguments) -> None:
        if self.state.detail_level != "experiment":
            return
        data = event.args.get("data")
        if not isinstance(data, dict):
            return
        measurement_id = data.get("measurementId")
        summary = self._selected_summary
        if not isinstance(measurement_id, int) or summary is None:
            return
        measurement = next(
            (
                item
                for item in summary.measurements
                if item.measurement_id == measurement_id
            ),
            None,
        )
        if measurement is not None:
            self._show_measurement(summary, measurement)

    def _show_selected_experiment(self) -> None:
        if self._selected_summary is not None:
            self._show_experiment(self._selected_summary)

    def _clear_selection(self) -> None:
        self._selected_summary = None
        self.state.selected_mixture_id = None
        self.state.selected_measurement_id = None
        self.selection_label.set_text("No compatible experiment selected")
        self.composition_chart.options["xAxis"]["name"] = "Concentration"
        self.composition_chart.options["yAxis"]["data"] = []
        self.composition_chart.options["series"][0]["data"] = []
        self.composition_chart.update()
        self.fractions_container.clear()
        self.detail_title.set_text("Experiment detail")
        self.detail_summary.set_text("")
        self.detail_back_button.set_enabled(False)
        self.detail_chart.options["series"][0]["data"] = []
        self.detail_chart.update()
        self.objective_details_container.clear()

    def focus_result(
        self, *, run: SavedExperimentRun, measurement: SavedExperimentMeasurement
    ) -> None:
        """Focus an exact saved result without changing comparison scope filters."""

        self.state.measurement_type = measurement.measurement_type
        self.measurement_type_select.set_value(measurement.measurement_type)
        metrics = self.controller.current_measurement_objectives(
            measurement_id=measurement.measurement_id
        )
        available_names = {metric.metric_name for metric in metrics}
        if self.state.objective_name not in available_names:
            replacement = next(
                (name for name in OBJECTIVE_DEFINITIONS if name in available_names),
                None,
            )
            if replacement is None:
                ui.notify(
                    "The selected measurement has no current objective result; "
                    "analyze completed experiments first",
                    type="warning",
                )
                return
            self.state.objective_name = replacement
            self.objective_select.set_value(replacement)
        summaries = self.controller.objective_experiment_summaries(
            measurement_type=measurement.measurement_type,
            objective_name=self.state.objective_name,
            source_scope="all_setups",
        )
        summary = next(
            (item for item in summaries if item.experiment_id == run.mixture_id), None
        )
        if summary is None:
            ui.notify(
                "The selected experiment has no compatible objective summary",
                type="warning",
            )
            return
        selected = next(
            (
                item
                for item in summary.measurements
                if item.measurement_id == measurement.measurement_id
            ),
            None,
        )
        if selected is None:
            ui.notify(
                "The selected measurement has no current objective result",
                type="warning",
            )
            return
        self._show_measurement(summary, selected)
        ui.run_javascript(
            "document.querySelector('.experiment-results-panel')"
            "?.scrollIntoView({behavior: 'smooth', block: 'start'});"
        )

    def _set_ui_busy(self, busy: bool) -> None:
        self._ui_busy = busy
        self.on_busy_change(busy)
        self._sync_controls()

    def _sync_controls(self) -> None:
        enabled = (
            not self._system_busy
            and not self._ui_busy
            and not self.controller.busy
            and not self.controller.emstat_controller.busy
        )
        for element in (
            self.objective_select,
            self.measurement_type_select,
            self.flow_mode_select,
            self.scope_select,
            self.compatibility_switch,
            self.refresh_button,
            self.analyze_button,
            self.add_concentration_filter_button,
            *self._concentration_filter_controls,
        ):
            element.set_enabled(enabled)
        self.detail_back_button.set_enabled(
            enabled
            and self._selected_summary is not None
            and self.state.detail_level == "measurement"
        )

    @staticmethod
    def _error_bar_renderer() -> str:
        return """
            (params, api) => {
                const x = api.value(0);
                const low = api.coord([x, api.value(1)]);
                const high = api.coord([x, api.value(2)]);
                const color = api.visual('color') || '#455a64';
                return {
                    type: 'group',
                    children: [
                        {type: 'line', shape: {x1: low[0], y1: low[1],
                            x2: high[0], y2: high[1]},
                            style: {stroke: color, lineWidth: 1.5}},
                        {type: 'line', shape: {x1: low[0] - 5, y1: low[1],
                            x2: low[0] + 5, y2: low[1]},
                            style: {stroke: color, lineWidth: 1.5}},
                        {type: 'line', shape: {x1: high[0] - 5, y1: high[1],
                            x2: high[0] + 5, y2: high[1]},
                            style: {stroke: color, lineWidth: 1.5}},
                    ],
                };
            }
        """

    @staticmethod
    def _detail_tooltip_formatter() -> str:
        return """
            (params) => {
                const data = params.data || {};
                const value = Array.isArray(data.value) ? data.value : [];
                const number = value[0] ?? '—';
                const objective = Number(value[1]);
                const objectiveText = Number.isFinite(objective)
                    ? objective.toPrecision(6)
                    : String(value[1] ?? '—');
                if (data.measurementId != null) {
                    const sem = Number(data.standardError);
                    const semText = Number.isFinite(sem)
                        ? `<br/>Within-measurement SEM: <b>`
                            + `${sem.toPrecision(4)} ${data.unit}</b>`
                        : '';
                    return `Measurement <b>${number}</b>`
                        + `<br/>Mean: <b>${objectiveText} ${data.unit}</b>`
                        + semText;
                }
                const kind = data.componentKind || 'Series';
                return `${kind.charAt(0).toUpperCase() + kind.slice(1)} `
                    + `<b>${number}</b><br/>Value: `
                    + `<b>${objectiveText} ${data.unit}</b>`;
            }
        """

    @staticmethod
    def _concentration_scale(values_mol_l: tuple[float, ...]) -> tuple[float, str]:
        maximum = max(values_mol_l, default=0.0)
        if maximum >= 1.0:
            return 1.0, "M"
        if maximum >= 1e-3:
            return 1e3, "mM"
        if maximum >= 1e-6:
            return 1e6, "µM"
        return 1e9, "nM"

    @staticmethod
    def _result_tooltip_formatter() -> str:
        return """
            (params) => {
                const data = params.data || {};
                const value = Array.isArray(data.value) ? data.value : [];
                const experiment = data.experimentId ?? value[0] ?? '—';
                const objective = Number(value[1]);
                const objectiveText = Number.isFinite(objective)
                    ? objective.toPrecision(6)
                    : String(value[1] ?? '—');
                const sd = Number(data.standardDeviation);
                const sdText = Number.isFinite(sd)
                    ? `<br/>Between-measurement SD: <b>`
                        + `${sd.toPrecision(4)} ${data.unit}</b>`
                    : '';
                const count = data.measurementCount ?? '—';
                return `Experiment <b>${experiment}</b> · ${count} measurement(s)`
                    + `<br/>Objective: <b>${objectiveText} ${data.unit}</b>`
                    + sdText;
            }
        """

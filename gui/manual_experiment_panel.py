from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from nicegui import run, ui
from nicegui.elements.number import Number

from controllers.experiment_controller import (
    ExperimentController,
    ExperimentPlan,
    ExperimentProtocolEstimate,
    ManualExperimentPlanRequest,
    ManualPlayStage,
)
from gui.runze_input_validation import read_non_negative_number
from storage.experiment_repository import (
    WATER_SOURCE_CONFIG_KEY,
    CurrentSourceConfiguration,
    SourceConfigurationEntry,
    SourceStock,
    format_molar_concentration,
)

logger = logging.getLogger(__name__)


@dataclass
class ManualExperimentDraft:
    measurement_name: str = ""
    variable_fractions_by_stock_id: dict[int, float | None] = field(
        default_factory=dict
    )


@dataclass
class ManualExperimentPanelState:
    source_configuration_revision: int | None = None
    experiments: list[ManualExperimentDraft] = field(
        default_factory=lambda: [ManualExperimentDraft()]
    )
    selected_experiment_index: int = 0
    plans: tuple[ExperimentPlan, ...] = ()


class ManualExperimentPanel:
    """Manual-only composition inputs and executable plan construction."""

    WATER_BALANCE_DISPLAY_DECIMALS = 12
    PROVISIONAL_CHARACTERIZED_TRANSFER_MINIMUM_ML = 0.05
    TRANSFER_ROUNDING_WARNING_PERCENT = 1.0

    FRACTION_GRID_STYLE = (
        "display: grid; "
        "grid-template-columns: minmax(13rem, 1.5fr) minmax(9rem, 1fr) "
        "7rem 8rem 8rem; "
        "column-gap: 0.75rem; row-gap: 0.4rem; align-items: center; "
        "width: 100%; min-width: 52rem;"
    )

    def __init__(
        self,
        controller: ExperimentController,
        *,
        source_configuration: CurrentSourceConfiguration,
        source_configuration_dirty: bool,
        protocol_estimate: ExperimentProtocolEstimate | None,
        state: ManualExperimentPanelState | None = None,
        on_plan_change: Callable[[ExperimentPlan | None], None],
        on_ui_operation_change: Callable[[str | None], None],
        on_measurement_starting: Callable[[], None] | None = None,
        on_measurement_completed: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self.repository = controller.repository
        self.state = state if state is not None else ManualExperimentPanelState()
        self.on_plan_change = on_plan_change
        self.on_ui_operation_change = on_ui_operation_change
        self.on_measurement_starting = on_measurement_starting or (lambda: None)
        self.on_measurement_completed = on_measurement_completed or (lambda: None)

        plumbing = controller.runze_controller.plumbing
        if plumbing is None:
            raise ValueError("Manual experiment planning requires Runze plumbing")
        self.plumbing = plumbing

        self._configuration = source_configuration
        self._source_configuration_dirty = source_configuration_dirty
        self._protocol_estimate = protocol_estimate
        self._system_busy = controller.busy
        self._ui_operation: str | None = None
        self._stocks: tuple[SourceStock, ...] = self.repository.list_source_stocks()
        self._fraction_inputs: dict[int, Number] = {}
        self._concentration_inputs: dict[int, Number] = {}
        self._variable_stock_by_port: dict[int, int] = {}
        self._syncing_values = False

        if not self.state.experiments:
            self.state.experiments.append(ManualExperimentDraft())
        self.state.selected_experiment_index = min(
            max(0, self.state.selected_experiment_index),
            len(self.state.experiments) - 1,
        )
        self._adopt_source_configuration(source_configuration)
        self._build()
        self._show_plan(self._selected_plan())
        self._sync_controls()

    def _build(self) -> None:
        ui.add_css(
            """
            .manual-experiment-grid {
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 1rem;
                align-items: start;
            }
            .manual-experiment-composition {
                grid-column: span 2;
            }
            @media (max-width: 70rem) {
                .manual-experiment-grid {
                    grid-template-columns: minmax(0, 1fr);
                }
                .manual-experiment-composition {
                    grid-column: span 1;
                }
            }
            """
        )

        with ui.element("div").classes("w-full manual-experiment-grid"):
            with ui.column().classes(
                "w-full gap-2 min-w-0 manual-experiment-composition"
            ):
                ui.label("Mixture composition").classes("text-lg font-bold")
                self.fractions_container = ui.column().classes("w-full gap-0")
                self.fraction_total_label = ui.label("").classes(
                    "text-sm text-gray-600"
                )
                self.composition_summary_label = ui.label("").classes(
                    "text-sm text-gray-600"
                )

            with ui.column().classes("w-full gap-2 min-w-0"):
                ui.label("Plan details").classes("text-lg font-bold")
                with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                    self.previous_experiment_button = (
                        ui.button(
                            icon="chevron_left", on_click=self._show_previous_experiment
                        )
                        .props("flat round dense")
                        .tooltip("Previous experiment")
                    )
                    self.experiment_position_label = ui.label().classes(
                        "font-medium text-center flex-1 whitespace-nowrap"
                    )
                    self.next_experiment_button = (
                        ui.button(
                            icon="chevron_right", on_click=self._show_next_experiment
                        )
                        .props("flat round dense")
                        .tooltip("Next experiment")
                    )
                self.measurement_name_input = ui.input(
                    "Measurement name (optional)",
                    value=self._current_draft().measurement_name,
                ).classes("w-full")
                self.measurement_name_input.on_value_change(
                    self._store_measurement_name
                )

                with ui.row().classes("w-full gap-2 flex-nowrap"):
                    self.add_experiment_button = (
                        ui.button(
                            "Add experiment", icon="add", on_click=self._add_experiment
                        )
                        .props("outline no-caps")
                        .classes("flex-1 whitespace-nowrap")
                        .tooltip("Copy the selected composition as the next experiment")
                    )
                    self.remove_experiment_button = (
                        ui.button(
                            "Remove",
                            icon="delete_outline",
                            on_click=self._remove_experiment,
                        )
                        .props("flat no-caps")
                        .classes("whitespace-nowrap")
                        .tooltip("Remove the selected experiment from the plan")
                    )

                with (
                    ui.element("div")
                    .classes("w-full")
                    .style(
                        "display: grid; grid-template-columns: 1fr 1fr; "
                        "column-gap: 1rem; row-gap: 0.5rem;"
                    )
                ):
                    self.plan_revision_value = self._build_value("Source revision")
                    self.plan_method_value = self._build_value("Method")

                self.plan_status_label = ui.label("").classes("text-sm text-gray-600")
                self.transfer_warning_label = ui.label("").classes(
                    "w-full whitespace-pre-line rounded bg-amber-50 px-3 py-2 "
                    "text-sm text-amber-900"
                )
                self.transfer_warning_label.set_visibility(False)
                self.build_plan_button = ui.button(
                    "Build plan", on_click=self._build_plan
                ).classes("w-full whitespace-nowrap")

        ui.label("Execution").classes("text-lg font-bold mt-2")
        with (
            ui.element("div")
            .classes("w-full manual-experiment-grid")
            .style("align-items: stretch;")
        ):
            with ui.column().classes("w-full gap-2"):
                ui.label("Prime once").classes("font-bold h-8 flex items-center")
                self.prime_button = (
                    ui.button(
                        "Prime required sources", on_click=self._prime_required_sources
                    )
                    .classes("w-full whitespace-nowrap")
                    .tooltip(
                        "Prime the required sources before preparing the first mixture."
                    )
                )

            with ui.column().classes("w-full gap-2"):
                with ui.row().classes("w-full h-8 items-center gap-2 flex-nowrap"):
                    self.workflow_heading_label = ui.label(
                        "Experiment workflow"
                    ).classes("font-bold")
                    ui.space()
                    self.play_button = (
                        ui.button(
                            "Play plan", icon="play_arrow", on_click=self._play_workflow
                        )
                        .props("flat dense no-caps")
                        .classes("whitespace-nowrap")
                        .tooltip(
                            "Run every planned experiment: prime if needed, then "
                            "prepare, measure, and clean each mixture."
                        )
                    )
                self.prepare_button = ui.button(
                    "Prepare mixture", on_click=self._prepare_mixture
                ).classes("w-full whitespace-nowrap")
                self.measure_button = ui.button(
                    "Run measurement", on_click=self._run_measurement
                ).classes("w-full whitespace-nowrap")
                self.clean_button = ui.button(
                    "Clean after experiment", on_click=self._clean_after_experiment
                ).classes("w-full whitespace-nowrap")

            with ui.column().classes("w-full gap-2"):
                ui.label("Final line maintenance").classes(
                    "font-bold h-8 flex items-center"
                )
                self.recover_lines_button = (
                    ui.button(
                        "1. Recover sources and clear flow cell",
                        on_click=self._recover_source_lines,
                    )
                    .classes("w-full whitespace-nowrap")
                    .tooltip(
                        "Keep non-water tubes in their matching source containers "
                        "and route the flow-cell output to waste."
                    )
                )
                self.rinse_lines_button = (
                    ui.button(
                        "2. Water-rinse source lines", on_click=self._rinse_source_lines
                    )
                    .classes("w-full whitespace-nowrap")
                    .tooltip(
                        "Move the maintained non-water source tubes to waste but "
                        "keep water in its reservoir."
                    )
                )
                self.air_empty_lines_button = (
                    ui.button(
                        "3. Air-empty all source lines",
                        on_click=self._air_empty_source_lines,
                    )
                    .classes("w-full whitespace-nowrap")
                    .tooltip("Move the water source tube to waste.")
                )

        self.execution_status_label = ui.label("").classes("text-sm text-gray-600")

        self._render_fraction_rows()

    @staticmethod
    def _build_value(label: str):
        with ui.column().classes("gap-0"):
            ui.label(label).classes("text-xs text-gray-500")
            return ui.label("—").classes("font-medium")

    def _render_fraction_rows(self) -> None:
        self._fraction_inputs.clear()
        self._concentration_inputs.clear()
        self._variable_stock_by_port.clear()
        self.fractions_container.clear()

        entry_by_port = {
            entry.selector_port: entry for entry in self._configuration.entries
        }
        stock_by_id = {stock.id: stock for stock in self._stocks}
        self._variable_stock_by_port = {
            entry.selector_port: entry.source_stock_id
            for entry in self._configuration.enabled_entries
            if entry.locked_fraction is None
            and entry.source_stock_id is not None
            and entry.source_config_key != WATER_SOURCE_CONFIG_KEY
        }

        with (
            self.fractions_container,
            ui.element("div").classes("w-full overflow-x-auto"),
            ui.element("div").style(self.FRACTION_GRID_STYLE),
        ):
            for heading in (
                "Source stock",
                "Controlled component",
                "Policy",
                "Fraction",
                "Final conc. (mM)",
            ):
                ui.label(heading).classes("text-sm font-medium text-gray-500")

            for port in sorted(
                self.plumbing.liquid_sources,
                key=lambda configured_port: configured_port.number,
            ):
                entry = entry_by_port.get(port.number)
                stock = self._stock_for_entry(entry, stock_by_id=stock_by_id)

                with ui.column().classes("gap-0 min-w-0"):
                    stock_label = (
                        "Water"
                        if entry is not None
                        and entry.source_config_key == WATER_SOURCE_CONFIG_KEY
                        else "Unassigned"
                        if stock is None
                        else stock.label
                    )
                    ui.label(stock_label).classes("font-medium")
                    ui.label(port.label).classes("text-xs text-gray-500")
                component = self._controlled_component(entry, stock)
                ui.label(
                    "Balance"
                    if entry is not None
                    and entry.source_config_key == WATER_SOURCE_CONFIG_KEY
                    else "—"
                    if component is None
                    else component.substance_label
                ).classes("text-sm")
                ui.label(self._fraction_policy(entry)).classes("text-sm")

                fraction_input = (
                    ui.number(
                        value=self._display_fraction_value(entry),
                        min=0.0,
                        max=1.0,
                        precision=4,
                    )
                    .props('step="any"')
                    .classes("w-full")
                )
                self._fraction_inputs[port.number] = fraction_input

                if (
                    entry is not None
                    and entry.enabled
                    and entry.locked_fraction is None
                    and entry.source_stock_id is not None
                ):
                    stock_id = entry.source_stock_id
                    fraction_input.on_value_change(
                        partial(self._store_variable_fraction, port.number, stock_id)
                    )

                if component is None:
                    ui.label("—").classes("text-gray-400")
                else:
                    concentration_input = (
                        ui.number(
                            value=self._final_concentration_mm(entry, stock),
                            min=0.0,
                            precision=6,
                        )
                        .props('step="any"')
                        .classes("w-full")
                    )
                    self._concentration_inputs[port.number] = concentration_input
                    if (
                        entry is not None
                        and entry.enabled
                        and entry.locked_fraction is None
                        and entry.source_stock_id is not None
                    ):
                        concentration_input.on_value_change(
                            partial(
                                self._store_variable_concentration,
                                port.number,
                                entry.source_stock_id,
                                component.substance_id,
                            )
                        )

        self._update_fraction_total()
        self._update_composition_summary()

    def set_system_busy(self, busy: bool) -> None:
        self._system_busy = busy
        self._sync_controls()

    def set_source_configuration_dirty(self, dirty: bool) -> None:
        if self._source_configuration_dirty == dirty:
            return
        self._source_configuration_dirty = dirty
        self._sync_controls()

    def show_source_configuration(
        self, configuration: CurrentSourceConfiguration
    ) -> None:
        revision_changed = (
            self.state.source_configuration_revision != configuration.revision
        )
        self._configuration = configuration
        self._stocks = self.repository.list_source_stocks()
        self._adopt_source_configuration(configuration)
        if revision_changed:
            self.on_plan_change(None)
        self._render_fraction_rows()
        self._show_selected_experiment()
        self._sync_controls()

    def show_protocol_estimate(
        self, estimate: ExperimentProtocolEstimate | None
    ) -> None:
        if self._protocol_estimate == estimate:
            return
        self._protocol_estimate = estimate
        self._invalidate_plan()
        self._sync_controls()

    def _store_measurement_name(self, event: Any) -> None:
        if self._syncing_values:
            return
        self._current_draft().measurement_name = str(event.value or "")
        self._invalidate_plan()

    def _store_variable_fraction(
        self, selector_port: int, source_stock_id: int, event: Any
    ) -> None:
        if self._syncing_values:
            return
        self._current_draft().variable_fractions_by_stock_id[source_stock_id] = (
            event.value
        )
        self._invalidate_plan()
        self._sync_concentration_from_fraction(selector_port)
        self._sync_water_balance_input()
        self._update_fraction_total()
        self._update_composition_summary()

    def _store_variable_concentration(
        self, selector_port: int, source_stock_id: int, substance_id: int, event: Any
    ) -> None:
        if self._syncing_values or event.value is None:
            return
        try:
            fraction = self.controller.source_fraction_for_concentration(
                source_stock_id=source_stock_id,
                substance_id=substance_id,
                final_concentration_mol_l=float(event.value) * 1e-3,
            )
        except (TypeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            self._sync_concentration_from_fraction(selector_port)
            return

        self._current_draft().variable_fractions_by_stock_id[source_stock_id] = fraction
        self._syncing_values = True
        try:
            self._fraction_inputs[selector_port].set_value(fraction)
        finally:
            self._syncing_values = False
        self._invalidate_plan()
        self._sync_water_balance_input()
        self._update_fraction_total()
        self._update_composition_summary()

    def _current_draft(self) -> ManualExperimentDraft:
        return self.state.experiments[self.state.selected_experiment_index]

    def _selected_plan(self) -> ExperimentPlan | None:
        index = self.state.selected_experiment_index
        if index >= len(self.state.plans):
            return None
        return self.state.plans[index]

    def _show_previous_experiment(self) -> None:
        self._select_experiment(self.state.selected_experiment_index - 1)

    def _show_next_experiment(self) -> None:
        self._select_experiment(self.state.selected_experiment_index + 1)

    def _select_experiment(self, index: int) -> None:
        if index < 0 or index >= len(self.state.experiments):
            return
        self.state.selected_experiment_index = index
        self._show_selected_experiment()
        self._sync_controls()

    def _show_selected_experiment(self) -> None:
        draft = self._current_draft()
        self._syncing_values = True
        try:
            self.measurement_name_input.set_value(draft.measurement_name)
        finally:
            self._syncing_values = False
        self._render_fraction_rows()
        selected_plan = self._selected_plan()
        self._show_plan(selected_plan)
        self.on_plan_change(selected_plan)

    def _add_experiment(self) -> None:
        current = self._current_draft()
        self.state.experiments.append(
            ManualExperimentDraft(
                measurement_name=current.measurement_name,
                variable_fractions_by_stock_id=dict(
                    current.variable_fractions_by_stock_id
                ),
            )
        )
        self._invalidate_plan()
        self._select_experiment(len(self.state.experiments) - 1)

    def _remove_experiment(self) -> None:
        if len(self.state.experiments) == 1:
            return
        del self.state.experiments[self.state.selected_experiment_index]
        self.state.selected_experiment_index = min(
            self.state.selected_experiment_index, len(self.state.experiments) - 1
        )
        self._invalidate_plan()
        self._show_selected_experiment()
        self._sync_controls()

    def _show_play_experiment(self, plan: ExperimentPlan) -> None:
        index = next(
            (
                candidate_index
                for candidate_index, candidate in enumerate(self.state.plans)
                if candidate is plan
            ),
            None,
        )
        if index is None:
            index = next(
                (
                    candidate_index
                    for candidate_index, candidate in enumerate(self.state.plans)
                    if candidate == plan
                ),
                None,
            )
        if index is not None and index != self.state.selected_experiment_index:
            self.state.selected_experiment_index = index
            self._show_selected_experiment()

    async def _build_plan(self) -> None:
        try:
            protocol = self._protocol_estimate
            if protocol is None:
                raise ValueError("Update the shared protocol estimates first")
            pending_requests: list[ManualExperimentPlanRequest] = []
            for index, draft in enumerate(self.state.experiments, start=1):
                try:
                    fractions = self._read_draft_fractions(draft)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"Experiment {index}: {error}") from error
                pending_requests.append(
                    ManualExperimentPlanRequest(
                        variable_source_fractions=fractions,
                        measurement_name=self._normalized_measurement_name(
                            draft.measurement_name
                        ),
                    )
                )
            requests = tuple(pending_requests)
        except (TypeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return

        self._set_ui_operation("Building manual experiment plan")
        try:
            plans = await run.io_bound(
                partial(
                    self.controller.build_manual_experiment_plans,
                    requests=requests,
                    protocol=protocol,
                )
            )
            if not plans:
                raise RuntimeError("Manual experiment planning returned no plan")
        except Exception as error:
            logger.error(
                "Manual experiment plan failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
        else:
            self.state.plans = plans
            selected_plan = self._selected_plan()
            self.on_plan_change(selected_plan)
            self._show_plan(selected_plan)
            ui.notify(
                f"Manual plan built with {len(plans)} experiment"
                f"{'s' if len(plans) != 1 else ''}",
                type="positive",
            )
        finally:
            self._set_ui_operation(None)

    async def _prime_required_sources(self) -> None:
        try:
            plan = self._select_plan_for_manual_stage()
        except (RuntimeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return
        await self._run_stage(
            operation="Priming required source lines",
            action=partial(
                self.controller.prime_required_sources, rpm=plan.handling_rpm
            ),
            success_message="Required source lines are primed",
        )

    async def _prepare_mixture(self) -> None:
        try:
            plan = self._select_plan_for_manual_stage()
        except (RuntimeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return
        await self._run_stage(
            operation="Preparing experiment mixture",
            action=partial(self.controller.prepare_mixture, plan=plan),
            success_message="Experiment mixture prepared",
        )

    async def _run_measurement(self) -> None:
        try:
            self._select_plan_for_manual_stage()
        except (RuntimeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return
        prepared = self.controller.prepared_mixture
        if prepared is None:
            ui.notify(
                "Prepare a mixture before starting the measurement", type="negative"
            )
            return
        self.on_measurement_starting()
        stopped_flow = prepared.plan.workflow_kind == "stopped_flow"
        stopped_flow_series = stopped_flow and prepared.plan.measurement_count > 1
        completed = await self._run_stage(
            operation=(
                "Running stopped-flow measurement series"
                if stopped_flow_series
                else "Running stopped-flow measurement"
                if stopped_flow
                else "Running continuous-flow measurement"
            ),
            action=partial(
                self.controller.run_flow_cell_measurement, prepared_mixture=prepared
            ),
            success_message=(
                "Stopped-flow measurement series completed"
                if stopped_flow_series
                else "Stopped-flow measurement completed"
                if stopped_flow
                else "Continuous-flow measurement completed"
            ),
        )
        if completed:
            self.on_measurement_completed()

    async def _clean_after_experiment(self) -> None:
        try:
            self._select_plan_for_manual_stage()
        except (RuntimeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return
        prepared = self.controller.prepared_mixture
        if prepared is None:
            ui.notify("There is no prepared mixture to clean", type="negative")
            return
        await self._run_stage(
            operation="Cleaning experiment paths",
            action=partial(
                self.controller.clean_after_experiment, prepared_mixture=prepared
            ),
            success_message="Experiment paths cleaned",
        )

    async def _play_workflow(self) -> None:
        try:
            plans = self._require_current_plans()
        except (ValueError, RuntimeError) as error:
            ui.notify(str(error), type="negative")
            return
        self.on_measurement_starting()
        await self._run_stage(
            operation="Running experiment queue",
            action=partial(self.controller.engine.run_queue, plans=plans),
            success_message="Queue completed or paused at a stage boundary",
        )
        self.on_measurement_completed()
        self._sync_controls()

    async def resume_play_workflow(self) -> bool:
        result = await self._run_stage(
            operation="Resuming experiment queue",
            action=partial(self.controller.engine.run_queue, resume=True),
            success_message="Queue completed or paused at a stage boundary",
        )
        self.on_measurement_completed()
        self._sync_controls()
        return result

    def _exit_play_for_manual_stage(self) -> None:
        if self.controller.snapshot.manual_play_state == "paused":
            self.controller.discard_manual_play()
            ui.notify("Play mode exited; running the selected stage manually")

    def _select_plan_for_manual_stage(self) -> ExperimentPlan:
        plan = self._require_current_plan()
        self._exit_play_for_manual_stage()
        selected = self.controller.select_manual_experiment_plan(
            index=self.state.selected_experiment_index
        )
        if selected != plan:
            raise RuntimeError("The selected experiment no longer matches the plan")
        return plan

    async def _recover_source_lines(self) -> None:
        try:
            plan = self._require_current_plan()
        except ValueError as error:
            ui.notify(str(error), type="negative")
            return
        await self._run_stage(
            operation="Recovering sources and clearing flow cell",
            action=partial(
                self.controller.recover_source_lines_and_clear_flow_cell, plan=plan
            ),
            success_message=(
                "Sources recovered and flow-cell route cleared; move the maintained "
                "non-water source tubes to waste"
            ),
        )

    async def _rinse_source_lines(self) -> None:
        await self._run_stage(
            operation="Water-rinsing source lines",
            action=self.controller.rinse_recovered_source_lines,
            success_message=(
                "Source lines rinsed; move the water source tube to waste"
            ),
        )

    async def _air_empty_source_lines(self) -> None:
        await self._run_stage(
            operation="Air-emptying all source lines",
            action=self.controller.air_empty_maintained_source_lines,
            success_message="Final line maintenance completed",
        )

    async def _run_stage(
        self, *, operation: str, action: Callable[[], object], success_message: str
    ) -> bool:
        self._set_ui_operation(operation)
        try:
            await run.io_bound(action)
        except Exception as error:
            logger.error(
                "%s failed: %s",
                operation,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            ui.notify(str(error), type="negative")
            return False
        else:
            ui.notify(success_message, type="positive")
            return True
        finally:
            self._set_ui_operation(None)

    def _require_current_plan(self) -> ExperimentPlan:
        plan = self._selected_plan()
        if plan is None:
            raise ValueError("Build a manual plan first")
        if self._source_configuration_dirty:
            raise ValueError("Save source-configuration changes first")
        if plan.source_configuration_revision != self._configuration.revision:
            raise ValueError("The source configuration changed; build a new plan")
        return plan

    def _require_current_plans(self) -> tuple[ExperimentPlan, ...]:
        plans = self.state.plans
        if not plans:
            raise ValueError("Build a manual plan first")
        if self._source_configuration_dirty:
            raise ValueError("Save source-configuration changes first")
        if any(
            plan.source_configuration_revision != self._configuration.revision
            for plan in plans
        ):
            raise ValueError("The source configuration changed; build a new plan")
        return plans

    def _read_draft_fractions(self, draft: ManualExperimentDraft) -> dict[int, float]:
        if self._source_configuration_dirty:
            raise ValueError("Save the source configuration before building a plan")
        if not self._configuration.entries:
            raise ValueError("Save a source configuration before building a plan")

        fractions: dict[int, float] = {}
        for selector_port, source_stock_id in self._variable_stock_by_port.items():
            value = read_non_negative_number(
                draft.variable_fractions_by_stock_id.get(source_stock_id),
                name=f"Port {selector_port} fraction",
            )
            if value > 1:
                raise ValueError(f"Port {selector_port} fraction must not exceed 1")
            fractions[source_stock_id] = value
        return fractions

    @staticmethod
    def _normalized_measurement_name(value: object) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _adopt_source_configuration(
        self, configuration: CurrentSourceConfiguration
    ) -> None:
        if self.state.source_configuration_revision == configuration.revision:
            return

        variable_entries = tuple(
            entry
            for entry in configuration.enabled_entries
            if entry.locked_fraction is None and entry.source_stock_id is not None
        )
        initial_fractions = {
            entry.source_stock_id: 0.0
            for entry in variable_entries
            if entry.source_stock_id is not None
        }
        self.state.experiments = [
            ManualExperimentDraft(
                variable_fractions_by_stock_id=dict(initial_fractions)
            )
        ]
        self.state.selected_experiment_index = 0
        self.state.source_configuration_revision = configuration.revision
        self.state.plans = ()

    def _invalidate_plan(self) -> None:
        if self.state.plans:
            self.state.plans = ()
            self.on_plan_change(None)
        if hasattr(self, "plan_revision_value"):
            self._show_plan(None)

    def _show_plan(self, plan: ExperimentPlan | None) -> None:
        if plan is None:
            self.plan_revision_value.set_text("—")
            self.plan_method_value.set_text("—")
        else:
            self.plan_revision_value.set_text(str(plan.source_configuration_revision))
            self.plan_method_value.set_text(
                f"{plan.measurement_type.replace('_', ' ').title()} · "
                f"{plan.workflow_kind.replace('_', ' ').title()}"
            )
        self.plan_status_label.set_text(self._plan_status_text())
        self._show_transfer_warnings(plan)

    def _show_transfer_warnings(self, plan: ExperimentPlan | None) -> None:
        warnings: list[str] = []
        if plan is not None:
            for transfer in plan.mixture_transfers:
                reasons: list[str] = []
                if (
                    transfer.requested_volume_ml
                    < self.PROVISIONAL_CHARACTERIZED_TRANSFER_MINIMUM_ML
                ):
                    reasons.append("below the provisional 0.05 mL characterized range")
                if (
                    abs(transfer.rounding_error_percent)
                    > self.TRANSFER_ROUNDING_WARNING_PERCENT
                ):
                    reasons.append(
                        f"{transfer.rounding_error_percent:+.2f}% step-rounding error"
                    )
                if not reasons:
                    continue
                source = self.plumbing.port_by_key(transfer.source_config_key)
                warnings.append(
                    f"{source.label}: {transfer.requested_volume_ml:.6g} mL → "
                    f"{transfer.nominal_volume_ml:.6g} mL "
                    f"({transfer.syringe_steps} steps); {', '.join(reasons)}."
                )

        if not warnings:
            self.transfer_warning_label.set_text("")
            self.transfer_warning_label.set_visibility(False)
            return

        recommendation = (
            "Use a more dilute stock to increase the transferred component volume."
        )
        if plan is not None and plan.workflow_kind == "stopped_flow":
            recommendation += (
                " A larger stopped-flow series also increases total mixture volume, "
                "but only use extra measurements when scientifically appropriate."
            )
        self.transfer_warning_label.set_text(
            "Small-volume transfer warning\n"
            + "\n".join(warnings)
            + "\n"
            + recommendation
        )
        self.transfer_warning_label.set_visibility(True)

    def _update_fraction_total(self) -> None:
        locked_sum = math.fsum(
            entry.locked_fraction
            for entry in self._configuration.enabled_entries
            if entry.locked_fraction is not None
        )
        active_variable_ids = set(self._variable_stock_by_port.values())
        non_water_variable_sum = math.fsum(
            float(value or 0.0)
            for stock_id, value in (
                self._current_draft().variable_fractions_by_stock_id.items()
            )
            if stock_id in active_variable_ids
        )
        water_entry = self._water_entry()
        water_balance = (
            self._water_balance_fraction()
            if water_entry is not None and water_entry.locked_fraction is None
            else 0.0
        )
        variable_sum = non_water_variable_sum + water_balance
        self.fraction_total_label.set_text(
            f"Locked {locked_sum:.4f} · variable {variable_sum:.4f} · total "
            f"{locked_sum + variable_sum:.4f}"
        )

    def _plan_status_text(self) -> str:
        if self._ui_operation is not None:
            return self._ui_operation
        if self._source_configuration_dirty:
            return "Save source-configuration changes before building a plan"
        if self._protocol_estimate is None:
            return "Update the shared protocol estimates first"
        if not self.state.plans:
            count = len(self.state.experiments)
            return (
                f"Ready to build a {count}-experiment manual plan"
                if count != 1
                else "Ready to build a manual plan"
            )
        selected_plan = self._selected_plan()
        if selected_plan is None:
            return "Built plan is unavailable"
        workflow = (
            "Stopped-flow"
            if selected_plan.workflow_kind == "stopped_flow"
            else "Continuous-flow"
        )
        count = len(self.state.plans)
        return (
            f"{workflow} manual plan with {count} experiments is current"
            if count != 1
            else f"{workflow} manual plan is current"
        )

    def _execution_status_text(self) -> str:
        if self._ui_operation is not None:
            return self._ui_operation

        snapshot = self.controller.snapshot
        maintenance_phase = snapshot.line_maintenance_phase
        if maintenance_phase == "sources_recovered":
            return (
                "Sources recovered · move maintained non-water tubes to waste, "
                "then run the water rinse"
            )
        if maintenance_phase == "sources_rinsed":
            return (
                "Source lines rinsed · move the water tube to waste, then "
                "air-empty all source lines"
            )
        if maintenance_phase == "complete":
            return "Final line maintenance completed"
        if maintenance_phase == "recovery_required":
            return (
                "Experiment physical state is uncertain · recover it, correct known "
                "line states, then acknowledge recovery above"
            )

        play_position = (
            ""
            if snapshot.manual_play_plan_index is None
            else (
                f" · experiment {snapshot.manual_play_plan_index}/"
                f"{snapshot.manual_play_plan_count}"
            )
        )
        if snapshot.manual_play_state == "pause_requested":
            return (
                f"Play is running{play_position} · pause requested after the "
                "current stage"
            )
        if snapshot.manual_play_state == "paused":
            next_stage = snapshot.manual_play_next_stage
            stage_label = "next stage" if next_stage is None else next_stage
            return f"Play paused{play_position} · ready to resume before {stage_label}"
        if snapshot.manual_play_state == "running":
            next_stage = snapshot.manual_play_next_stage
            stage_label = "next stage" if next_stage is None else next_stage
            return f"Play is running{play_position} · next stage: {stage_label}"

        plan = self._selected_plan()
        if plan is None:
            return "Build a manual plan to enable workflow stages"
        if not self._runze_ready:
            return "Connect and prepare the Runze devices"
        prepared = self.controller.prepared_mixture
        volume_state = self.controller.prepared_mixture_volume_state
        if prepared is None:
            if self._plan_sources_primed(plan):
                return "Sources primed · ready to prepare mixture"
            return "Prime the required source lines"
        if volume_state == "prepared":
            run_label = (
                (
                    "run stopped-flow measurement"
                    if plan.measurement_count == 1
                    else "run stopped-flow series"
                )
                if plan.workflow_kind == "stopped_flow"
                else "run continuous-flow measurement"
            )
            return f"Mixture prepared · ready to {run_label} or clean"
        if volume_state == "flow_completed":
            return "Measurement run completed · ready to clean"
        return "Physical mixture state is unknown · recover manually"

    @staticmethod
    def _fraction_policy(entry: SourceConfigurationEntry | None) -> str:
        if entry is None or not entry.enabled:
            return "Disabled"
        if entry.locked_fraction is not None:
            return "Locked"
        return "Variable"

    def _fraction_value(self, entry: SourceConfigurationEntry | None) -> float:
        if entry is None or not entry.enabled:
            return 0.0
        if entry.locked_fraction is not None:
            return entry.locked_fraction
        if entry.source_config_key == WATER_SOURCE_CONFIG_KEY:
            return self._water_balance_fraction()
        if entry.source_stock_id is None:
            return 0.0
        return float(
            self._current_draft().variable_fractions_by_stock_id.get(
                entry.source_stock_id
            )
            or 0.0
        )

    def _water_entry(self) -> SourceConfigurationEntry | None:
        return next(
            (
                entry
                for entry in self._configuration.enabled_entries
                if entry.source_config_key == WATER_SOURCE_CONFIG_KEY
            ),
            None,
        )

    def _water_balance_fraction(self) -> float:
        locked_sum = math.fsum(
            entry.locked_fraction
            for entry in self._configuration.enabled_entries
            if entry.locked_fraction is not None
        )
        non_water_variable_sum = math.fsum(
            float(value or 0.0)
            for source_stock_id, value in (
                self._current_draft().variable_fractions_by_stock_id.items()
            )
            if source_stock_id in set(self._variable_stock_by_port.values())
        )
        return max(0.0, 1.0 - locked_sum - non_water_variable_sum)

    def _display_fraction_value(self, entry: SourceConfigurationEntry | None) -> float:
        if (
            entry is not None
            and entry.source_config_key == WATER_SOURCE_CONFIG_KEY
            and entry.locked_fraction is None
        ):
            return self._display_water_balance_fraction()
        return self._fraction_value(entry)

    def _display_water_balance_fraction(self) -> float:
        return round(
            self._water_balance_fraction(), self.WATER_BALANCE_DISPLAY_DECIMALS
        )

    @staticmethod
    def _controlled_component(
        entry: SourceConfigurationEntry | None, stock: SourceStock | None
    ):
        if entry is None or stock is None or entry.control_substance_id is None:
            return None
        return stock.component(entry.control_substance_id)

    def _final_concentration_mm(
        self, entry: SourceConfigurationEntry | None, stock: SourceStock | None
    ) -> float | None:
        component = self._controlled_component(entry, stock)
        if component is None:
            return None
        return self._fraction_value(entry) * component.concentration_mol_l * 1e3

    def _sync_concentration_from_fraction(self, selector_port: int) -> None:
        concentration_input = self._concentration_inputs.get(selector_port)
        if concentration_input is None:
            return
        entry = next(
            (
                item
                for item in self._configuration.entries
                if item.selector_port == selector_port
            ),
            None,
        )
        stock = self._stock_for_entry(
            entry, stock_by_id={stock.id: stock for stock in self._stocks}
        )
        self._syncing_values = True
        try:
            concentration_input.set_value(self._final_concentration_mm(entry, stock))
        finally:
            self._syncing_values = False

    def _sync_water_balance_input(self) -> None:
        water_entry = self._water_entry()
        if water_entry is None or water_entry.locked_fraction is not None:
            return
        water_input = self._fraction_inputs.get(water_entry.selector_port)
        if water_input is None:
            return
        self._syncing_values = True
        try:
            water_input.set_value(self._display_water_balance_fraction())
        finally:
            self._syncing_values = False

    def _update_composition_summary(self) -> None:
        concentrations: dict[int, list[float]] = {}
        labels: dict[int, str] = {}
        stock_by_id = {stock.id: stock for stock in self._stocks}
        for entry in self._configuration.enabled_entries:
            if entry.source_stock_id is None:
                continue
            stock = stock_by_id.get(entry.source_stock_id)
            if stock is None:
                continue
            fraction = self._fraction_value(entry)
            for component in stock.components:
                labels[component.substance_id] = component.substance_label
                concentrations.setdefault(component.substance_id, []).append(
                    fraction * component.concentration_mol_l
                )
        if not concentrations:
            self.composition_summary_label.set_text(
                "Final solute composition: no solute concentration"
            )
            return
        parts = [
            f"{labels[substance_id]} {format_molar_concentration(math.fsum(values))}"
            for substance_id, values in sorted(
                concentrations.items(), key=lambda item: labels[item[0]].lower()
            )
        ]
        self.composition_summary_label.set_text(
            "Final solute composition: " + " · ".join(parts)
        )

    @staticmethod
    def _stock_for_entry(
        entry: SourceConfigurationEntry | None, *, stock_by_id: dict[int, SourceStock]
    ) -> SourceStock | None:
        if entry is None or entry.source_stock_id is None:
            return None
        return stock_by_id.get(entry.source_stock_id)

    @property
    def _busy(self) -> bool:
        return (
            self._system_busy or self._ui_operation is not None or self.controller.busy
        )

    @property
    def _runze_ready(self) -> bool:
        controller = self.controller.runze_controller
        return (
            controller.connected
            and controller.status_known
            and controller.syringe_ready
            and controller.selector_ready
            and controller.fluidics_available
        )

    def _plan_sources_primed(self, plan: ExperimentPlan) -> bool:
        priming_by_port = {
            source.selector_port: source for source in self.controller.snapshot.priming
        }
        return all(
            (state := priming_by_port.get(source.selector_port)) is not None
            and state.source_stock_id == source.source_stock_id
            and state.status == "primed"
            for source in plan.sources
        )

    def _sync_controls(self) -> None:
        busy = self._busy
        snapshot = self.controller.snapshot
        maintenance_phase = snapshot.line_maintenance_phase
        play_state = snapshot.manual_play_state
        play_active = play_state != "inactive"
        maintenance_pending = maintenance_phase in {
            "sources_recovered",
            "sources_rinsed",
            "recovery_required",
        }
        prepared = self.controller.prepared_mixture
        editing_enabled = (
            not busy
            and prepared is None
            and not maintenance_pending
            and not play_active
        )
        self.measurement_name_input.set_enabled(editing_enabled)
        for selector_port, fraction_input in self._fraction_inputs.items():
            entry = next(
                (
                    item
                    for item in self._configuration.entries
                    if item.selector_port == selector_port
                ),
                None,
            )
            fraction_input.set_enabled(
                editing_enabled
                and not self._source_configuration_dirty
                and selector_port in self._variable_stock_by_port
            )
            concentration_input = self._concentration_inputs.get(selector_port)
            if concentration_input is not None:
                concentration_input.set_enabled(
                    editing_enabled
                    and not self._source_configuration_dirty
                    and selector_port in self._variable_stock_by_port
                    and entry is not None
                    and entry.control_substance_id is not None
                )
        self.build_plan_button.set_enabled(
            editing_enabled
            and not self._source_configuration_dirty
            and bool(self._configuration.entries)
            and self._protocol_estimate is not None
        )
        experiment_count = len(self.state.experiments)
        self.build_plan_button.set_text(
            "Build plan"
            if experiment_count == 1
            else f"Build plan ({experiment_count} experiments)"
        )
        can_navigate = not busy and prepared is None and not play_active
        selected_index = self.state.selected_experiment_index
        self.previous_experiment_button.set_enabled(can_navigate and selected_index > 0)
        self.next_experiment_button.set_enabled(
            can_navigate and selected_index + 1 < experiment_count
        )
        self.add_experiment_button.set_enabled(editing_enabled)
        self.remove_experiment_button.set_enabled(
            editing_enabled and experiment_count > 1
        )
        self.experiment_position_label.set_text(
            f"Experiment {selected_index + 1} of {experiment_count}"
        )

        plan = self._selected_plan()
        plans_current = bool(self.state.plans) and all(
            queued_plan.source_configuration_revision == self._configuration.revision
            for queued_plan in self.state.plans
        )
        plan_current = (
            plan is not None
            and plan.source_configuration_revision == self._configuration.revision
        )
        sources_primed = plan is not None and self._plan_sources_primed(plan)
        workflow_label = (
            "Stopped-flow workflow"
            if plan is not None and plan.workflow_kind == "stopped_flow"
            else "Continuous-flow workflow"
        )
        self.workflow_heading_label.set_text(workflow_label)
        has_primed_non_water_source = any(
            source.source_config_key != WATER_SOURCE_CONFIG_KEY
            and source.status == "primed"
            for source in snapshot.priming
        )
        volume_state = self.controller.prepared_mixture_volume_state

        self.play_button.set_enabled(
            not busy
            and self._runze_ready
            and plans_current
            and play_state == "inactive"
            and volume_state != "unknown"
            and maintenance_phase in {"not_started", "complete"}
        )

        self.prime_button.set_enabled(
            not busy
            and play_state in {"inactive", "paused"}
            and self._runze_ready
            and plan_current
            and prepared is None
            and not sources_primed
            and maintenance_phase in {"not_started", "complete"}
        )
        self.prepare_button.set_enabled(
            not busy
            and play_state in {"inactive", "paused"}
            and self._runze_ready
            and plan_current
            and prepared is None
            and sources_primed
            and maintenance_phase in {"not_started", "complete"}
        )
        self.measure_button.set_enabled(
            not busy
            and play_state in {"inactive", "paused"}
            and self._runze_ready
            and prepared is not None
            and volume_state == "prepared"
        )
        self.clean_button.set_enabled(
            not busy
            and play_state in {"inactive", "paused"}
            and self._runze_ready
            and prepared is not None
            and volume_state in ("prepared", "flow_completed")
        )
        self.recover_lines_button.set_enabled(
            not busy
            and play_state == "inactive"
            and self._runze_ready
            and plan_current
            and prepared is None
            and has_primed_non_water_source
            and maintenance_phase in {"not_started", "complete"}
        )
        self.rinse_lines_button.set_enabled(
            not busy
            and play_state == "inactive"
            and self._runze_ready
            and prepared is None
            and maintenance_phase == "sources_recovered"
        )
        self.air_empty_lines_button.set_enabled(
            not busy
            and play_state == "inactive"
            and self._runze_ready
            and prepared is None
            and maintenance_phase == "sources_rinsed"
        )
        self.plan_status_label.set_text(self._plan_status_text())
        self.execution_status_label.set_text(self._execution_status_text())

    def _set_ui_operation(self, operation: str | None) -> None:
        self._ui_operation = operation
        self.on_ui_operation_change(operation)
        self._sync_controls()

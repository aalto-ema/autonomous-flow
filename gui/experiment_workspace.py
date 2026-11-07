from collections.abc import Callable
from dataclasses import dataclass, field

from nicegui import ui

from controllers.experiment_controller import (
    ExperimentController,
    ExperimentPlan,
    ExperimentProtocolEstimate,
    SavedExperimentProtocol,
)
from gui.emstat_plot_panel import EmStatPlotPanelState
from gui.experiment_measurement_panel import ExperimentMeasurementPanel
from gui.experiment_protocol_panel import (
    ExperimentProtocolPanel,
    ExperimentProtocolPanelState,
)
from gui.experiment_results_panel import (
    ExperimentResultsPanel,
    ExperimentResultsPanelState,
)
from gui.manual_experiment_panel import (
    ManualExperimentDraft,
    ManualExperimentPanel,
    ManualExperimentPanelState,
)
from gui.source_configuration_panel import (
    SourceConfigurationPanel,
    SourceConfigurationPanelState,
)
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    LinearSweepVoltammetrySettings,
)
from storage.experiment_repository import (
    CurrentSourceConfiguration,
    SavedExperimentMeasurement,
    SavedExperimentRun,
)


@dataclass
class ExperimentWorkspaceState:
    source_configuration: SourceConfigurationPanelState = field(
        default_factory=SourceConfigurationPanelState
    )
    protocol: ExperimentProtocolPanelState = field(
        default_factory=ExperimentProtocolPanelState
    )
    manual: ManualExperimentPanelState = field(
        default_factory=ManualExperimentPanelState
    )
    plot: EmStatPlotPanelState = field(default_factory=EmStatPlotPanelState)
    results: ExperimentResultsPanelState = field(
        default_factory=ExperimentResultsPanelState
    )


class ExperimentWorkspace:
    """Source setup, shared protocol, and mode-specific experiment controls."""

    def __init__(
        self,
        controller: ExperimentController,
        *,
        state: ExperimentWorkspaceState | None = None,
        on_ui_operation_change: Callable[[str | None], None],
    ) -> None:
        self.controller = controller
        self.state = state if state is not None else ExperimentWorkspaceState()
        self.on_ui_operation_change = on_ui_operation_change
        self._system_busy = controller.busy
        self._adopt_restored_checkpoint()
        self._manual_plan: ExperimentPlan | None = (
            self.state.manual.plans[self.state.manual.selected_experiment_index]
            if self.state.manual.plans
            else None
        )
        self._protocol_draft_before_preview: ExperimentProtocolPanelState | None = None
        self._previewed_protocol: SavedExperimentProtocol | None = None

        self._build()
        self._sync_controls()

    def _build(self) -> None:
        configuration = self.controller.source_configuration

        ui.add_css(
            """
            .experiment-workspace > .nicegui-card > .q-expansion-item
            > .q-expansion-item__container > .q-expansion-item__content
            > .nicegui-expansion-content {
                padding: 0 !important;
            }
            """
        )

        with ui.column().classes("w-full gap-4 experiment-workspace"):
            self.source_configuration_panel = SourceConfigurationPanel(
                self.controller,
                state=self.state.source_configuration,
                on_configuration_change=self._show_source_configuration,
                on_dirty_change=self._set_source_configuration_dirty,
                on_ui_operation_change=self.on_ui_operation_change,
            )

            self.protocol_panel = ExperimentProtocolPanel(
                self.controller,
                state=self.state.protocol,
                on_estimate_change=self._show_protocol_estimate,
                on_ui_operation_change=self.on_ui_operation_change,
            )

            with ui.card().classes("w-full p-4 gap-3"):
                ui.label("Experiment execution").classes("text-xl font-bold")
                with (
                    ui.row()
                    .classes("w-full items-center gap-3 flex-wrap rounded p-3")
                    .style("background-color: #fff8e1; border: 1px solid #f0ad4e;")
                ) as self.recovery_warning:
                    ui.icon("warning", color="warning").classes("text-2xl")
                    with ui.column().classes("gap-0 min-w-64 flex-1"):
                        ui.label("Experiment recovery required").classes("font-bold")
                        ui.label(
                            "Establish and record a safe physical baseline before "
                            "starting another experiment."
                        ).classes("text-sm")
                    self.experiment_recovery_button = (
                        ui.button(
                            "Acknowledge experiment recovery",
                            on_click=self._open_experiment_recovery_dialog,
                            color="warning",
                        )
                        .props("outline")
                        .classes("whitespace-nowrap")
                    )

                snapshot = self.controller.snapshot
                if (
                    snapshot.checkpoint_updated_at is not None
                    and snapshot.workflow_phase != "inactive"
                ):
                    checkpoint_time = (
                        snapshot.checkpoint_updated_at.astimezone().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    )
                    ui.label(
                        f"Restored {snapshot.workflow_phase.replace('_', ' ')} "
                        f"checkpoint from {checkpoint_time}."
                    ).classes("text-sm text-gray-600")

                with ui.tabs() as self.workspace_tabs:
                    self.manual_workflow_tab = ui.tab("Manual workflow")
                    self.optimization_tab = ui.tab("Bayesian optimization")

                with ui.tab_panels(
                    self.workspace_tabs, value=self.manual_workflow_tab
                ).classes("w-full bg-transparent"):
                    with ui.tab_panel(self.manual_workflow_tab).classes("p-0"):
                        self.manual_panel = ManualExperimentPanel(
                            self.controller,
                            source_configuration=configuration,
                            source_configuration_dirty=(
                                self.state.source_configuration.dirty
                            ),
                            protocol_estimate=self.state.protocol.estimate,
                            state=self.state.manual,
                            on_plan_change=self._show_manual_plan,
                            on_ui_operation_change=self.on_ui_operation_change,
                            on_measurement_starting=self._show_live_measurement,
                            on_measurement_completed=(self._refresh_saved_measurements),
                        )

                    with ui.tab_panel(self.optimization_tab).classes("p-0"):
                        ui.label("Bayesian optimization").classes("text-lg font-bold")
                        ui.label("Not implemented").classes("text-sm text-gray-500")

                self._build_experiment_recovery_dialog()

            with ui.card().classes("w-full p-4 gap-3"):
                ui.label("Measurement data").classes("text-xl font-bold")
                self.plot_panel = ExperimentMeasurementPanel(
                    self.controller,
                    state=self.state.plot,
                    on_busy_change=self._set_plot_panel_busy,
                    on_view_results=self._view_saved_results,
                    on_view_protocol=self._view_saved_protocol,
                    on_use_protocol=self._use_saved_protocol,
                    on_discard_protocol=self._discard_saved_protocol,
                )

            with ui.card().classes("w-full p-4 gap-3"):
                ui.label("Experiment results").classes("text-xl font-bold")
                self.results_panel = ExperimentResultsPanel(
                    self.controller,
                    state=self.state.results,
                    protocol_estimate=self.state.protocol.estimate,
                    on_busy_change=self._set_results_panel_busy,
                )

        self.optimization_tab.disable()
        self.optimization_tab.tooltip(
            "Bayesian optimization will use the shared protocol settings"
        )

    def set_system_busy(self, busy: bool) -> None:
        self._system_busy = busy
        self._sync_controls()

    async def resume_experiment_play(self) -> bool:
        """Resume paused Play execution from the global control bar."""

        return await self.manual_panel.resume_play_workflow()

    def sync_experiment_play_state(self) -> None:
        """Refresh workspace controls after a global Play-control action."""

        self._sync_controls()

    def _sync_controls(self) -> None:
        snapshot = self.controller.snapshot
        maintenance_phase = snapshot.line_maintenance_phase
        maintenance_pending = maintenance_phase in {
            "sources_recovered",
            "sources_rinsed",
            "recovery_required",
        }
        play_active = snapshot.manual_play_state != "inactive"
        self.source_configuration_panel.set_system_busy(
            self._system_busy or play_active
        )
        self.protocol_panel.set_system_busy(
            self._system_busy or maintenance_pending or play_active
        )
        self.manual_panel.set_system_busy(self._system_busy)
        self.plot_panel.set_system_busy(self._system_busy)
        self.results_panel.set_system_busy(self._system_busy)
        self.manual_workflow_tab.set_enabled(not self._system_busy)
        self.optimization_tab.set_enabled(False)
        recovery_required = maintenance_phase == "recovery_required"
        self.recovery_warning.set_visibility(recovery_required)
        self.experiment_recovery_button.set_enabled(
            recovery_required and not self._system_busy
        )

    def _open_experiment_recovery_dialog(self) -> None:
        self.experiment_recovery_dialog.open()

    def _build_experiment_recovery_dialog(self) -> None:
        with (
            ui.dialog() as self.experiment_recovery_dialog,
            ui.card().classes("max-w-xl gap-3"),
        ):
            ui.label("Acknowledge experiment recovery?").classes("text-lg font-bold")
            ui.label(
                "First use the Procedures page or manual controls to establish a "
                "safe physical baseline: empty the syringe; discard and rinse any "
                "abandoned mixture; leave the mixing-vessel route, common line, "
                "and OUT line air-filled; and rinse the flow-cell route with water "
                "if its contents are uncertain. Record each source line's actual "
                "physical state above."
            )
            ui.label(
                "Acknowledgement does not move hardware. It abandons the active "
                "mixture, clears the blocked experiment checkpoint, and preserves "
                "the source-line states you confirmed."
            ).classes("text-sm text-gray-600")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=self.experiment_recovery_dialog.close)
                ui.button(
                    "Acknowledge recovery",
                    on_click=self._acknowledge_experiment_recovery,
                    color="warning",
                )

    def _acknowledge_experiment_recovery(self) -> None:
        try:
            self.controller.acknowledge_experiment_recovery()
        except RuntimeError as error:
            ui.notify(str(error), type="negative")
        else:
            self.experiment_recovery_dialog.close()
            self._sync_controls()
            ui.notify("Experiment recovery acknowledged", type="positive")

    def _adopt_restored_checkpoint(self) -> None:
        plans = self.controller.manual_experiment_plans
        if not plans or self.state.manual.plans:
            return

        snapshot = self.controller.snapshot
        selected_index = (
            0
            if snapshot.manual_play_plan_index is None
            else snapshot.manual_play_plan_index - 1
        )
        selected_index = min(selected_index, len(plans) - 1)
        plan = plans[selected_index]
        self.state.manual.plans = plans
        self.state.manual.source_configuration_revision = (
            plan.source_configuration_revision
        )
        self.state.manual.selected_experiment_index = selected_index
        self.state.manual.experiments = [
            ManualExperimentDraft(
                measurement_name=queued_plan.measurement_name or "",
                variable_fractions_by_stock_id={
                    source.source_stock_id: source.fraction
                    for source in queued_plan.sources
                    if source.source_stock_id is not None
                    and source.locked_fraction is None
                },
            )
            for queued_plan in plans
        ]

        protocol = self.state.protocol
        protocol.workflow_kind = plan.workflow_kind
        protocol.measurement_count = plan.measurement_count
        protocol.between_measurement_displacement_volume_ml = (
            plan.between_measurement_displacement_volume_ml
        )
        protocol.measurement_type = plan.measurement_type
        settings = plan.measurement_settings
        if isinstance(settings, ChronoamperometrySettings):
            protocol.chronoamperometry_potential_v = settings.potential_v
            protocol.chronoamperometry_duration_s = settings.run_time_s
            protocol.chronoamperometry_interval_s = settings.interval_time_s
            protocol.chronoamperometry_equilibration_time_s = (
                settings.equilibration_time_s
            )
        elif isinstance(settings, CyclicVoltammetrySettings):
            protocol.cyclic_voltammetry_begin_potential_v = settings.begin_potential_v
            protocol.cyclic_voltammetry_vertex1_potential_v = (
                settings.vertex1_potential_v
            )
            protocol.cyclic_voltammetry_vertex2_potential_v = (
                settings.vertex2_potential_v
            )
            protocol.cyclic_voltammetry_step_potential_v = settings.step_potential_v
            protocol.cyclic_voltammetry_scan_rate_v_per_s = settings.scan_rate_v_per_s
            protocol.cyclic_voltammetry_scan_count = settings.scan_count
            protocol.cyclic_voltammetry_equilibration_time_s = (
                settings.equilibration_time_s
            )
        elif isinstance(settings, LinearSweepVoltammetrySettings):
            protocol.linear_sweep_voltammetry_begin_potential_v = (
                settings.begin_potential_v
            )
            protocol.linear_sweep_voltammetry_end_potential_v = settings.end_potential_v
            protocol.linear_sweep_voltammetry_step_potential_v = (
                settings.step_potential_v
            )
            protocol.linear_sweep_voltammetry_scan_rate_v_per_s = (
                settings.scan_rate_v_per_s
            )
            protocol.linear_sweep_voltammetry_sweep_count = settings.sweep_count
            protocol.linear_sweep_voltammetry_equilibration_time_s = (
                settings.equilibration_time_s
            )

        protocol.handling_rpm = plan.handling_rpm
        protocol.approach_rpm = plan.approach_rpm
        protocol.flow_rpm = plan.flow_rpm
        protocol.water_distribution = plan.water_distribution
        protocol.mixing_rinse_volume_ml = plan.cleanup_settings.mixing_rinse_volume_ml
        protocol.mixing_rinse_cycles = plan.cleanup_settings.mixing_rinse_cycles
        protocol.flow_cell_rinse_through_volume_ml = (
            plan.cleanup_settings.flow_cell_rinse_through_volume_ml
        )
        protocol.source_line_additional_rinse_volume_ml = (
            plan.cleanup_settings.source_line_additional_rinse_volume_ml
        )
        protocol.estimate = ExperimentProtocolEstimate(
            measurement_type=plan.measurement_type,
            measurement_settings=plan.measurement_settings,
            handling_rpm=plan.handling_rpm,
            approach_rpm=plan.approach_rpm,
            flow_rpm=plan.flow_rpm,
            water_distribution=plan.water_distribution,
            cleanup_settings=plan.cleanup_settings,
            estimated_measurement_duration_s=plan.estimated_measurement_duration_s,
            flow_rate_ml_min=plan.flow_rate_ml_min,
            measurement_flow_volume_ml=plan.measurement_flow_volume_ml,
            mixing_path_condition_volume_ml=plan.mixing_path_condition_volume_ml,
            flow_cell_load_volume_ml=plan.flow_cell_load_volume_ml,
            mixing_vessel_reserve_volume_ml=plan.mixing_vessel_reserve_volume_ml,
            total_mixture_volume_ml=plan.total_mixture_volume_ml,
            workflow_kind=plan.workflow_kind,
            measurement_count=plan.measurement_count,
            between_measurement_displacement_volume_ml=(
                plan.between_measurement_displacement_volume_ml
            ),
        )

    def _show_source_configuration(
        self, configuration: CurrentSourceConfiguration
    ) -> None:
        self.manual_panel.show_source_configuration(configuration)
        self.results_panel.show_source_configuration(configuration)

    def _set_source_configuration_dirty(self, dirty: bool) -> None:
        self.manual_panel.set_source_configuration_dirty(dirty)

    def _show_protocol_estimate(
        self, estimate: ExperimentProtocolEstimate | None
    ) -> None:
        self.manual_panel.show_protocol_estimate(estimate)
        self.results_panel.show_protocol_estimate(estimate)

    def _show_manual_plan(self, plan: ExperimentPlan | None) -> None:
        self._manual_plan = plan

    def _show_live_measurement(self) -> None:
        self.plot_panel.show_live()

    def _refresh_saved_measurements(self) -> None:
        self.plot_panel.refresh()
        self.plot_panel.refresh_saved_measurement_options()
        self.results_panel.refresh()

    def _set_plot_panel_busy(self, busy: bool) -> None:
        self.on_ui_operation_change(
            "Loading saved EmStat measurement" if busy else None
        )

    def _set_results_panel_busy(self, busy: bool) -> None:
        self.on_ui_operation_change(
            "Analyzing saved experiment measurements" if busy else None
        )

    def _view_saved_results(
        self, run: SavedExperimentRun, measurement: SavedExperimentMeasurement
    ) -> None:
        self.results_panel.focus_result(run=run, measurement=measurement)

    def _view_saved_protocol(self, saved_run: SavedExperimentRun) -> None:
        if self._protocol_draft_before_preview is not None:
            return
        try:
            protocol = self.controller.saved_protocol_for_run(saved_run)
        except (TypeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return
        self._protocol_draft_before_preview = self.protocol_panel.capture_state()
        self._previewed_protocol = protocol
        self.protocol_panel.show_saved_protocol(protocol, preview=True)
        self.plot_panel.set_protocol_preview_active(True)
        self.protocol_panel.focus()

    def _discard_saved_protocol(self) -> None:
        draft = self._protocol_draft_before_preview
        if draft is None:
            return
        self.protocol_panel.set_previewing_saved_protocol(False)
        self.protocol_panel.restore_state(draft)
        self._show_protocol_estimate(draft.estimate)
        self._protocol_draft_before_preview = None
        self._previewed_protocol = None
        self.plot_panel.set_protocol_preview_active(False)

    def _use_saved_protocol(self) -> None:
        protocol = self._previewed_protocol
        if protocol is None:
            return
        self.protocol_panel.show_saved_protocol(protocol, preview=False)
        self.protocol_panel.finish_saved_protocol_use()
        self._show_protocol_estimate(None)
        self._protocol_draft_before_preview = None
        self._previewed_protocol = None
        self.plot_panel.set_protocol_preview_active(False)
        ui.notify(
            "Historical protocol loaded; update estimates before planning",
            type="positive",
        )

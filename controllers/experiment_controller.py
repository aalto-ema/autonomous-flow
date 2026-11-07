from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from palmsens.data_types import Measurement

from analysis.measurement_objectives import (
    DEFAULT_OBJECTIVE,
    OBJECTIVE_DEFINITIONS,
    ObjectiveName,
    ObjectiveResult,
    calculate_objective,
)
from common.validation import (
    require_finite_number,
    require_integer,
    require_non_negative_finite_number,
    require_positive_finite_number,
    require_positive_integer,
)
from controllers.emstat_controller import (
    EmStatController,
    EmStatLoadedMeasurement,
    EmStatMeasurementSettings,
)
from controllers.runze_controller import (
    OperationStoppedError,
    RunzeController,
    SequenceStoppedError,
)
from fluidics.runze_fluidics import (
    FlowPhaseHooks,
    MixtureTransferDiagnostic,
    MixtureTransferRegime,
    StoppedFlowHooks,
    WaterDistribution,
    build_mixture_transfer_diagnostics,
)
from fluidics.runze_plumbing import SelectorPortConfig
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    EmStatMeasurementType,
    LinearSweepVoltammetrySettings,
    MeasurementAbortedError,
)
from storage.experiment_repository import (
    WATER_SOURCE_CONFIG_KEY,
    CurrentSourceConfiguration,
    ExperimentRepository,
    MeasurementMetric,
    MeasurementStatus,
    MeasurementType,
    MetricContribution,
    MetricObservation,
    MixtureComponent,
    MixtureConcentrationConstraint,
    PersistedExperimentWorkspaceState,
    PersistedSourceLineState,
    PersistedWorkflowPhase,
    PhysicalSourceLineStatus,
    SavedExperimentMeasurement,
    SavedExperimentRun,
    SourceConfigurationEntry,
    SourceFraction,
    SourceSetupIdentity,
    SourceSetupMatch,
    SourceStock,
)

logger = logging.getLogger(__name__)

ExperimentControllerState = Literal[
    "idle",
    "planning",
    "priming",
    "preparing_mixture",
    "measuring",
    "cleaning",
    "line_maintenance",
    "stopping",
    "completed",
    "aborted",
    "failed",
]
SourcePrimingStatus = PhysicalSourceLineStatus
PreparedMixtureVolumeState = Literal["prepared", "flow_completed", "unknown"]
ExperimentWorkflowKind = Literal["continuous_flow", "stopped_flow"]
ManualPlayState = Literal["inactive", "running", "pause_requested", "paused"]
ManualPlayStage = Literal["prime", "prepare", "measure", "clean"]
LineMaintenancePhase = Literal[
    "not_started",
    "sources_recovered",
    "sources_rinsed",
    "complete",
    "recovery_required",
]
type ResultSourceScope = Literal[
    "all_setups",
    "same_components",
    "same_relative_formulation",
    "same_exact_composition",
    "current_stocks",
]


@dataclass(frozen=True, slots=True)
class MeasurementObjectiveSummary:
    measurement_id: int
    sequence_number: int
    measurement_name: str | None
    value: float
    unit: str
    within_measurement_standard_error: float | None
    contributions: tuple[MetricContribution, ...]


@dataclass(frozen=True, slots=True)
class ExperimentMeasurementStatus:
    measurement_id: int
    sequence_number: int
    status: MeasurementStatus


@dataclass(frozen=True, slots=True)
class ExperimentObjectiveSummary:
    experiment_id: int
    value: float
    unit: str
    between_measurement_standard_deviation: float | None
    measurements: tuple[MeasurementObjectiveSummary, ...]
    measurement_statuses: tuple[ExperimentMeasurementStatus, ...]
    fractions: tuple[SourceFraction, ...]


def _validate_workflow(
    workflow_kind: ExperimentWorkflowKind, measurement_count: int
) -> None:
    if workflow_kind not in ("continuous_flow", "stopped_flow"):
        raise ValueError(f"Unsupported experiment workflow: {workflow_kind!r}")
    require_positive_integer(measurement_count, name="measurement_count")
    if workflow_kind == "continuous_flow" and measurement_count != 1:
        raise ValueError("Continuous-flow experiments must have one measurement")


def _validate_workflow_flow_rpm(
    workflow_kind: ExperimentWorkflowKind, flow_rpm: int | None
) -> None:
    if workflow_kind == "continuous_flow" and flow_rpm is None:
        raise ValueError("flow_rpm is required for continuous-flow experiments")
    if workflow_kind == "stopped_flow" and flow_rpm is not None:
        raise ValueError("Stopped-flow experiments do not use flow_rpm")


def _validate_between_measurement_displacement(
    workflow_kind: ExperimentWorkflowKind,
    measurement_count: int,
    volume_ml: float | None,
) -> None:
    if workflow_kind == "continuous_flow":
        if volume_ml is not None:
            raise ValueError(
                "Continuous-flow experiments do not use "
                "between_measurement_displacement_volume_ml"
            )
        return
    if measurement_count == 1:
        if volume_ml is not None:
            raise ValueError(
                "Single-measurement stopped flow does not use "
                "between_measurement_displacement_volume_ml"
            )
        return
    if volume_ml is None:
        raise ValueError(
            "between_measurement_displacement_volume_ml is required for "
            "stopped-flow experiments"
        )
    require_positive_finite_number(
        volume_ml, name="between_measurement_displacement_volume_ml"
    )


_BUSY_STATES = {
    "planning",
    "priming",
    "preparing_mixture",
    "measuring",
    "cleaning",
    "line_maintenance",
    "stopping",
}
_FRACTION_SUM_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class SourcePrimingState:
    selector_port: int
    source_config_key: str
    source_label: str
    source_stock_id: int | None
    status: SourcePrimingStatus
    primed_at: datetime | None
    updated_at: datetime | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ExperimentPlanSource:
    source_stock_id: int | None
    selector_port: int
    source_config_key: str
    fraction: float
    locked_fraction: float | None
    control_substance_id: int | None
    control_substance_label: str | None
    stock_control_concentration_mol_l: float | None

    @property
    def controlled_concentration_mol_l(self) -> float | None:
        if self.stock_control_concentration_mol_l is None:
            return None
        return self.fraction * self.stock_control_concentration_mol_l


@dataclass(frozen=True, slots=True)
class ExperimentPlanSubstanceConcentration:
    substance_id: int
    substance_label: str
    concentration_mol_l: float


@dataclass(frozen=True, slots=True)
class ExperimentCleanupSettings:
    mixing_rinse_volume_ml: float
    mixing_rinse_cycles: int
    flow_cell_rinse_through_volume_ml: float
    source_line_additional_rinse_volume_ml: float = 0.0

    def __post_init__(self) -> None:
        require_positive_finite_number(
            self.mixing_rinse_volume_ml, name="mixing_rinse_volume_ml"
        )
        require_positive_integer(self.mixing_rinse_cycles, name="mixing_rinse_cycles")
        require_positive_finite_number(
            self.flow_cell_rinse_through_volume_ml,
            name="flow_cell_rinse_through_volume_ml",
        )
        require_non_negative_finite_number(
            self.source_line_additional_rinse_volume_ml,
            name="source_line_additional_rinse_volume_ml",
        )


@dataclass(frozen=True, slots=True)
class ExperimentProtocolEstimate:
    measurement_type: EmStatMeasurementType
    measurement_settings: EmStatMeasurementSettings
    handling_rpm: int
    approach_rpm: int
    flow_rpm: int | None
    water_distribution: WaterDistribution
    cleanup_settings: ExperimentCleanupSettings
    estimated_measurement_duration_s: float
    flow_rate_ml_min: float | None
    measurement_flow_volume_ml: float | None
    mixing_path_condition_volume_ml: float
    flow_cell_load_volume_ml: float
    mixing_vessel_reserve_volume_ml: float
    total_mixture_volume_ml: float
    inlet_aspiration_rpm: int | None = None
    workflow_kind: ExperimentWorkflowKind = "continuous_flow"
    measurement_count: int = 1
    between_measurement_displacement_volume_ml: float | None = None

    def __post_init__(self) -> None:
        _validate_workflow(self.workflow_kind, self.measurement_count)
        _validate_workflow_flow_rpm(self.workflow_kind, self.flow_rpm)
        _validate_between_measurement_displacement(
            self.workflow_kind,
            self.measurement_count,
            self.between_measurement_displacement_volume_ml,
        )


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    source_configuration_revision: int
    sources: tuple[ExperimentPlanSource, ...]
    measurement_type: EmStatMeasurementType
    measurement_settings: EmStatMeasurementSettings
    measurement_name: str | None
    handling_rpm: int
    approach_rpm: int
    flow_rpm: int | None
    water_distribution: WaterDistribution
    cleanup_settings: ExperimentCleanupSettings
    estimated_measurement_duration_s: float
    flow_rate_ml_min: float | None
    measurement_flow_volume_ml: float | None
    mixing_path_condition_volume_ml: float
    flow_cell_load_volume_ml: float
    mixing_vessel_reserve_volume_ml: float
    total_mixture_volume_ml: float
    substance_concentrations: tuple[ExperimentPlanSubstanceConcentration, ...]
    mixture_transfers: tuple[MixtureTransferDiagnostic, ...] = ()
    inlet_aspiration_rpm: int | None = None
    workflow_kind: ExperimentWorkflowKind = "continuous_flow"
    measurement_count: int = 1
    between_measurement_displacement_volume_ml: float | None = None

    def __post_init__(self) -> None:
        _validate_workflow(self.workflow_kind, self.measurement_count)
        _validate_workflow_flow_rpm(self.workflow_kind, self.flow_rpm)
        _validate_between_measurement_displacement(
            self.workflow_kind,
            self.measurement_count,
            self.between_measurement_displacement_volume_ml,
        )

    @property
    def fractions_by_source_key(self) -> dict[str, float]:
        return {source.source_config_key: source.fraction for source in self.sources}


@dataclass(frozen=True, slots=True)
class ManualExperimentPlanRequest:
    variable_source_fractions: Mapping[int, float]
    measurement_name: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedMixture:
    mixture_id: int
    plan: ExperimentPlan


@dataclass(frozen=True, slots=True)
class ExperimentMeasurementResult:
    measurement_id: int
    emstat_measurement_id: int
    session_file_path: Path


@dataclass(frozen=True, slots=True)
class ExperimentRunResult:
    mixture_id: int
    measurements: tuple[ExperimentMeasurementResult, ...]
    plan: ExperimentPlan


@dataclass(frozen=True, slots=True)
class SavedExperimentProtocol:
    workflow_kind: ExperimentWorkflowKind
    measurement_type: EmStatMeasurementType
    measurement_settings: EmStatMeasurementSettings
    measurement_name: str | None
    handling_rpm: int
    approach_rpm: int
    flow_rpm: int | None
    water_distribution: WaterDistribution
    mixing_rinse_volume_ml: float
    mixing_rinse_cycles: int
    flow_cell_rinse_through_volume_ml: float
    source_line_additional_rinse_volume_ml: float
    historical_estimate: Mapping[str, object]
    inlet_aspiration_rpm: int | None = None
    measurement_count: int = 1
    between_measurement_displacement_volume_ml: float | None = None


@dataclass(frozen=True, slots=True)
class ObjectiveAnalysisBatchResult:
    analyzed_measurement_ids: tuple[int, ...]
    errors: tuple[str, ...]

    @property
    def analyzed_count(self) -> int:
        return len(self.analyzed_measurement_ids)


@dataclass(frozen=True, slots=True)
class ExperimentStopResult:
    runze_stop_requested: bool
    emstat_abort_requested: bool
    errors: tuple[str, ...]

    @property
    def requested(self) -> bool:
        return self.runze_stop_requested or self.emstat_abort_requested


@dataclass(frozen=True, slots=True)
class ExperimentSnapshot:
    state: ExperimentControllerState
    operation: str | None
    source_configuration_revision: int
    mixture_id: int | None
    measurement_id: int | None
    measurement_sequence_number: int | None
    measurement_count: int | None
    workflow_kind: ExperimentWorkflowKind | None
    measurement_type: EmStatMeasurementType | None
    estimated_measurement_duration_s: float | None
    measurement_flow_volume_ml: float | None
    total_mixture_volume_ml: float | None
    error_message: str | None
    priming: tuple[SourcePrimingState, ...]
    line_maintenance_phase: LineMaintenancePhase
    line_maintenance_source_ports: tuple[int, ...]
    workflow_phase: PersistedWorkflowPhase
    checkpoint_updated_at: datetime | None
    manual_play_state: ManualPlayState = "inactive"
    manual_play_next_stage: ManualPlayStage | None = None
    manual_play_plan_index: int | None = None
    manual_play_plan_count: int = 0

    @property
    def busy(self) -> bool:
        return self.state in _BUSY_STATES


@dataclass(frozen=True, slots=True)
class _SourceLineIdentity:
    source_stock_id: int | None
    status: SourcePrimingStatus
    updated_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class _PendingLineMaintenance:
    source_ports: tuple[int, ...]
    handling_rpm: int
    additional_rinse_volume_ml: float


@dataclass(slots=True)
class _ManualPlaySequence:
    plans: tuple[ExperimentPlan, ...]
    active_index: int = 0
    state: ManualPlayState = "inactive"


class ExperimentController:
    """Coordinate one reproducible mixture and electrochemical measurement.

    The Runze and EmStat controllers remain responsible for their own devices.
    This controller owns cross-device planning, source configuration snapshots,
    durable source-line physical knowledge, synchronization, and database
    lifecycle records.
    """

    def __init__(
        self,
        *,
        runze_controller: RunzeController,
        emstat_controller: EmStatController,
        repository: ExperimentRepository,
    ) -> None:
        plumbing = runze_controller.plumbing
        if plumbing is None:
            raise ValueError("ExperimentController requires Runze plumbing")
        if plumbing.flow_cell is None:
            raise ValueError("ExperimentController requires a configured flow cell")

        self.runze_controller = runze_controller
        self.emstat_controller = emstat_controller
        self.repository = repository
        self._plumbing = plumbing

        self._operation_lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._state: ExperimentControllerState = "idle"
        self._operation: str | None = None
        self._source_configuration = repository.load_current_source_configuration()
        self._line_state_by_port: dict[int, _SourceLineIdentity] = {}
        self._line_maintenance_phase: LineMaintenancePhase = "not_started"
        self._pending_line_maintenance: _PendingLineMaintenance | None = None
        self._prepared_mixture: PreparedMixture | None = None
        self._prepared_mixture_volume_state: PreparedMixtureVolumeState | None = None
        self._mixture_id: int | None = None
        self._measurement_id: int | None = None
        self._measurement_sequence_number: int | None = None
        self._active_plan: ExperimentPlan | None = None
        self._manual_play_sequence: _ManualPlaySequence | None = None
        self._error_message: str | None = None
        self._workflow_phase: PersistedWorkflowPhase = "inactive"
        self._checkpoint_updated_at: datetime | None = None
        self._physical_operation_checkpointed = False
        self._persistence_available = all(
            hasattr(repository, method)
            for method in (
                "load_experiment_workspace_state",
                "save_experiment_workspace_state",
            )
        )
        self._restore_workspace_checkpoint()
        if hasattr(self.runze_controller, "set_before_physical_operation"):
            self.runze_controller.set_before_physical_operation(
                self._handle_runze_physical_operation
            )

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._state in _BUSY_STATES

    @property
    def source_configuration(self) -> CurrentSourceConfiguration:
        with self._state_lock:
            return self._source_configuration

    @property
    def prepared_mixture(self) -> PreparedMixture | None:
        with self._state_lock:
            return self._prepared_mixture

    @property
    def prepared_mixture_volume_state(self) -> PreparedMixtureVolumeState | None:
        with self._state_lock:
            return self._prepared_mixture_volume_state

    @property
    def manual_experiment_plans(self) -> tuple[ExperimentPlan, ...]:
        with self._state_lock:
            if self._manual_play_sequence is not None:
                return self._manual_play_sequence.plans
            return () if self._active_plan is None else (self._active_plan,)

    @property
    def active_manual_experiment_plan(self) -> ExperimentPlan | None:
        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None:
                return self._active_plan
            if sequence.active_index >= len(sequence.plans):
                return None
            return sequence.plans[sequence.active_index]

    @property
    def source_configuration_locked(self) -> bool:
        with self._state_lock:
            return self._workflow_phase in {
                "prepared",
                "flow_completed",
                "sources_recovered",
                "sources_rinsed",
                "recovery_required",
            }

    @property
    def snapshot(self) -> ExperimentSnapshot:
        with self._state_lock:
            plan = self._active_plan
            play = self._manual_play_sequence
            return ExperimentSnapshot(
                state=self._state,
                operation=self._operation,
                source_configuration_revision=self._source_configuration.revision,
                mixture_id=self._mixture_id,
                measurement_id=self._measurement_id,
                measurement_sequence_number=self._measurement_sequence_number,
                measurement_count=None if plan is None else plan.measurement_count,
                workflow_kind=None if plan is None else plan.workflow_kind,
                measurement_type=None if plan is None else plan.measurement_type,
                estimated_measurement_duration_s=(
                    None if plan is None else plan.estimated_measurement_duration_s
                ),
                measurement_flow_volume_ml=(
                    None if plan is None else plan.measurement_flow_volume_ml
                ),
                total_mixture_volume_ml=(
                    None if plan is None else plan.total_mixture_volume_ml
                ),
                error_message=self._error_message,
                priming=self._priming_snapshot_locked(),
                line_maintenance_phase=self._line_maintenance_phase,
                line_maintenance_source_ports=(
                    ()
                    if self._pending_line_maintenance is None
                    else self._pending_line_maintenance.source_ports
                ),
                workflow_phase=self._workflow_phase,
                checkpoint_updated_at=self._checkpoint_updated_at,
                manual_play_state="inactive" if play is None else play.state,
                manual_play_next_stage=self._manual_play_next_stage_locked(),
                manual_play_plan_index=(
                    None
                    if play is None or play.active_index >= len(play.plans)
                    else play.active_index + 1
                ),
                manual_play_plan_count=0 if play is None else len(play.plans),
            )

    @property
    def default_stopped_flow_displacement_volume_ml(self) -> float:
        """Return the configured overtravel used as the stopped-flow UI default."""

        return self._plumbing.flow_cell_overtravel_volume_ml

    # Source configuration and durable physical state ---

    def replace_source_configuration(
        self,
        *,
        entries: Sequence[SourceConfigurationEntry],
        expected_revision: int | None = None,
    ) -> CurrentSourceConfiguration:
        if self.source_configuration_locked:
            raise RuntimeError(
                "Cannot change the source configuration while a mixture or line-"
                "maintenance stage is active; clean or finish recovery first"
            )
        self._acquire_operation(
            state="idle", operation="updating source configuration", visible=False
        )
        try:
            self._validate_configuration_ports(entries)

            with self._state_lock:
                old_configuration = self._source_configuration
                effective_expected_revision = (
                    old_configuration.revision
                    if expected_revision is None
                    else expected_revision
                )

            self._validate_source_configuration(
                CurrentSourceConfiguration(
                    revision=old_configuration.revision, entries=tuple(entries)
                )
            )
            self._validate_source_configuration_transition(
                old_configuration=old_configuration, new_entries=entries
            )

            saved = self.repository.replace_current_source_configuration(
                entries=entries, expected_revision=effective_expected_revision
            )
            self._validate_source_configuration(saved)

            with self._state_lock:
                self._source_configuration = saved
                self._active_plan = None
                self._manual_play_sequence = None
                self._save_checkpoint_locked(
                    last_operation="source configuration changed"
                )

            logger.info("Saved source configuration revision %s", saved.revision)
            return saved
        finally:
            self._release_operation(final_state=None)

    def mark_source_unknown(self, *, selector_port: int) -> None:
        require_positive_integer(selector_port, name="selector_port")
        configured_ports = {source.number for source in self._plumbing.liquid_sources}
        if selector_port not in configured_ports:
            raise ValueError(
                f"Selector port {selector_port} is not a configured liquid source"
            )

        with self._state_lock:
            if self._state in _BUSY_STATES:
                raise RuntimeError("Cannot change priming state during an experiment")
            entry = next(
                (
                    entry
                    for entry in self._source_configuration.entries
                    if entry.selector_port == selector_port
                ),
                None,
            )
            if not self._source_line_state_editable_locked(
                selector_port=selector_port,
                proposed_enabled=None if entry is None else entry.enabled,
            ):
                raise RuntimeError(
                    "This physical line state cannot be edited at the current "
                    "experiment stage"
                )
            self._set_line_state_locked(
                selector_port, "unknown", reason="Set unknown by operator"
            )
            self._save_checkpoint_locked(last_operation="line state corrected")

    def mark_all_sources_unknown(self) -> None:
        with self._state_lock:
            if self._state in _BUSY_STATES:
                raise RuntimeError("Cannot change priming state during an experiment")
            if self._workflow_phase == "ready":
                raise RuntimeError(
                    "Cannot mark active source lines unknown while mixture preparation "
                    "is available"
                )
            for source in self._plumbing.liquid_sources:
                self._set_line_state_locked(
                    source.number,
                    "unknown",
                    reason="All line states set unknown by operator",
                )
            self._save_checkpoint_locked(last_operation="all line states corrected")

    def set_source_line_state(
        self,
        *,
        selector_port: int,
        status: SourcePrimingStatus,
        proposed_enabled: bool | None = None,
        proposed_source_stock_id: int | None = None,
    ) -> None:
        """Record an operator-confirmed physical line state without moving hardware."""

        if status not in {"unknown", "primed", "air_filled"}:
            raise ValueError(f"Unsupported source line state: {status!r}")
        configured_ports = {source.number for source in self._plumbing.liquid_sources}
        if selector_port not in configured_ports:
            raise ValueError(
                f"Selector port {selector_port} is not a configured liquid source"
            )
        with self._state_lock:
            if self._state in _BUSY_STATES:
                raise RuntimeError("Cannot change line state during an experiment")
            entry = next(
                (
                    entry
                    for entry in self._source_configuration.entries
                    if entry.selector_port == selector_port
                ),
                None,
            )
            if not self._source_line_state_editable_locked(
                selector_port=selector_port, proposed_enabled=proposed_enabled
            ):
                raise RuntimeError(
                    "This physical line state cannot be edited at the current "
                    "experiment stage"
                )
            effective_enabled = (
                entry.enabled
                if proposed_enabled is None and entry is not None
                else bool(proposed_enabled)
            )
            effective_stock_id = None if entry is None else entry.source_stock_id
            if proposed_source_stock_id is not None:
                effective_stock_id = proposed_source_stock_id
            if status == "primed" and not effective_enabled:
                raise RuntimeError(
                    "An unconfigured or disabled source cannot be marked primed"
                )
            if (
                status == "primed"
                and entry is not None
                and entry.source_stock_id != effective_stock_id
            ):
                raise RuntimeError(
                    "A line cannot be marked primed for a newly assigned stock; "
                    "confirm that it is air-filled, save, then prime it"
                )
            self._set_line_state_locked(
                selector_port,
                status,
                reason="Physical state confirmed by operator",
                source_stock_id=effective_stock_id,
            )
            self._save_checkpoint_locked(last_operation="line state confirmed")

    def source_line_state_editable(
        self, *, selector_port: int, proposed_enabled: bool | None = None
    ) -> bool:
        with self._state_lock:
            return self._source_line_state_editable_locked(
                selector_port=selector_port, proposed_enabled=proposed_enabled
            )

    def prime_required_sources(self, *, rpm: int) -> bool:
        require_positive_integer(rpm, name="rpm")
        self._require_no_prepared_mixture("prime source lines")
        self._require_no_pending_line_maintenance("prime source lines")
        self._acquire_operation(state="priming", operation="priming source lines")
        try:
            primed = self._prime_required_sources(rpm=rpm)
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            with self._state_lock:
                if self._active_plan is not None:
                    self._workflow_phase = "ready"
            self._finish_physical_success(last_operation="priming completed")
            return primed

    def recover_source_lines_and_clear_flow_cell(self, *, plan: ExperimentPlan) -> None:
        """Recover currently primed non-water sources and clear the flow cell."""
        self._require_no_prepared_mixture("start line maintenance")
        self._require_plan_current(plan)
        self._require_no_pending_line_maintenance("start line maintenance")

        water_port = self._plumbing.water.number
        with self._state_lock:
            source_ports = tuple(
                state.selector_port
                for state in self._priming_snapshot_locked()
                if state.selector_port != water_port and state.status == "primed"
            )
        if not source_ports:
            raise RuntimeError(
                "No currently primed non-water source lines are available"
            )

        pending = _PendingLineMaintenance(
            source_ports=source_ports,
            handling_rpm=plan.handling_rpm,
            additional_rinse_volume_ml=(
                plan.cleanup_settings.source_line_additional_rinse_volume_ml
            ),
        )
        self._acquire_operation(
            state="line_maintenance",
            operation="recovering source lines and clearing flow cell",
        )
        with self._state_lock:
            self._pending_line_maintenance = pending
            self._mark_operation_started_locked(
                "recovering source lines and clearing flow cell"
            )
        try:
            self.runze_controller.recover_source_lines_and_clear_flow_cell(
                source_ports=source_ports,
                handling_rpm=plan.handling_rpm,
                approach_rpm=plan.approach_rpm,
            )
        except BaseException as error:
            with self._state_lock:
                for port in source_ports:
                    self._set_line_state_locked(
                        port, "unknown", reason="Source recovery did not complete"
                    )
                self._line_maintenance_phase = "recovery_required"
                self._workflow_phase = "recovery_required"
            self._finish_with_error(error)
            raise
        else:
            with self._state_lock:
                for port in source_ports:
                    self._set_line_state_locked(
                        port, "air_filled", reason="Recovered to source container"
                    )
                self._line_maintenance_phase = "sources_recovered"
                self._workflow_phase = "sources_recovered"
            self._finish_physical_success(last_operation="source recovery completed")

    def rinse_recovered_source_lines(self) -> None:
        """Water-rinse the non-water source lines captured by maintenance."""
        pending = self._require_line_maintenance_phase("sources_recovered")
        self._acquire_operation(
            state="line_maintenance", operation="rinsing source lines with water"
        )
        self._mark_operation_started("rinsing source lines with water")
        try:
            self.runze_controller.rinse_source_lines_with_water(
                source_ports=pending.source_ports,
                additional_volume_ml=pending.additional_rinse_volume_ml,
                rpm=pending.handling_rpm,
            )
        except BaseException as error:
            with self._state_lock:
                for port in pending.source_ports:
                    self._set_line_state_locked(
                        port, "unknown", reason="Water rinse did not complete"
                    )
                self._line_maintenance_phase = "recovery_required"
                self._workflow_phase = "recovery_required"
            self._finish_with_error(error)
            raise
        else:
            with self._state_lock:
                self._line_maintenance_phase = "sources_rinsed"
                for port in pending.source_ports:
                    self._set_line_state_locked(
                        port, "unknown", reason="Line contains rinse water"
                    )
                self._workflow_phase = "sources_rinsed"
            self._finish_physical_success(last_operation="source rinse completed")

    def air_empty_maintained_source_lines(self) -> None:
        """Air-empty captured source lines, including water, and finish maintenance."""
        pending = self._require_line_maintenance_phase("sources_rinsed")
        affected_ports = (*pending.source_ports, self._plumbing.water.number)
        self._acquire_operation(
            state="line_maintenance", operation="air-emptying all source lines"
        )
        self._mark_operation_started("air-emptying all source lines")
        try:
            self.runze_controller.air_empty_source_lines(
                source_ports=pending.source_ports, rpm=pending.handling_rpm
            )
        except BaseException as error:
            with self._state_lock:
                for port in affected_ports:
                    self._set_line_state_locked(
                        port, "unknown", reason="Air-emptying did not complete"
                    )
                self._line_maintenance_phase = "recovery_required"
                self._workflow_phase = "recovery_required"
            self._finish_with_error(error)
            raise
        else:
            with self._state_lock:
                for port in affected_ports:
                    self._set_line_state_locked(
                        port, "air_filled", reason="Line air-emptied after maintenance"
                    )
                self._line_maintenance_phase = "complete"
                self._pending_line_maintenance = None
                self._workflow_phase = "complete"
            self._finish_physical_success(last_operation="line maintenance completed")

    def acknowledge_experiment_recovery(self) -> None:
        """Clear the blocked experiment checkpoint after explicit recovery."""
        with self._state_lock:
            if self._state in _BUSY_STATES:
                raise RuntimeError("Cannot acknowledge experiment recovery while busy")
            if self._line_maintenance_phase != "recovery_required":
                raise RuntimeError("Experiment recovery is not required")
            self._line_maintenance_phase = "complete"
            self._pending_line_maintenance = None
            if self._manual_play_sequence is not None:
                self._manual_play_sequence.state = "inactive"
            self._error_message = None
            self._prepared_mixture = None
            self._prepared_mixture_volume_state = None
            self._workflow_phase = "complete"
            self._save_checkpoint_locked(last_operation="manual recovery acknowledged")

    # Planning ---

    def source_fraction_for_concentration(
        self,
        *,
        source_stock_id: int,
        substance_id: int,
        final_concentration_mol_l: float,
    ) -> float:
        """Convert a requested final concentration to a physical stock fraction."""

        require_positive_integer(source_stock_id, name="source_stock_id")
        require_positive_integer(substance_id, name="substance_id")
        require_non_negative_finite_number(
            final_concentration_mol_l, name="final_concentration_mol_l"
        )
        stock = self.repository.source_stock(source_stock_id)
        component = stock.component(substance_id)
        if component is None:
            raise ValueError(
                f"Substance {substance_id} is not present in source stock "
                f"{source_stock_id}"
            )
        fraction = float(final_concentration_mol_l) / component.concentration_mol_l
        if fraction > 1 + _FRACTION_SUM_TOLERANCE:
            raise ValueError(
                "Requested final concentration exceeds the selected stock "
                f"concentration ({component.concentration_mol_l:g} mol/L)"
            )
        return min(1.0, fraction)

    def estimate_experiment_protocol(
        self,
        *,
        measurement_settings: EmStatMeasurementSettings,
        handling_rpm: int,
        inlet_aspiration_rpm: int | None = None,
        flow_rpm: int | None,
        approach_rpm: int | None = None,
        water_distribution: WaterDistribution = "single",
        cleanup_settings: ExperimentCleanupSettings,
        workflow_kind: ExperimentWorkflowKind = "continuous_flow",
        measurement_count: int = 1,
        between_measurement_displacement_volume_ml: float | None = None,
    ) -> ExperimentProtocolEstimate:
        _validate_workflow(workflow_kind, measurement_count)
        _validate_workflow_flow_rpm(workflow_kind, flow_rpm)
        _validate_between_measurement_displacement(
            workflow_kind, measurement_count, between_measurement_displacement_volume_ml
        )
        self._validate_protocol_inputs(
            handling_rpm=handling_rpm,
            inlet_aspiration_rpm=inlet_aspiration_rpm,
            flow_rpm=flow_rpm,
            approach_rpm=approach_rpm,
            water_distribution=water_distribution,
            cleanup_settings=cleanup_settings,
            workflow_kind=workflow_kind,
        )
        self._require_no_prepared_mixture("estimate another experiment protocol")
        self._acquire_operation(state="planning", operation="estimating protocol")
        try:
            estimate = self._estimate_experiment_protocol(
                measurement_settings=measurement_settings,
                handling_rpm=handling_rpm,
                inlet_aspiration_rpm=inlet_aspiration_rpm,
                flow_rpm=flow_rpm,
                approach_rpm=approach_rpm,
                water_distribution=water_distribution,
                cleanup_settings=cleanup_settings,
                workflow_kind=workflow_kind,
                measurement_count=measurement_count,
                between_measurement_displacement_volume_ml=(
                    between_measurement_displacement_volume_ml
                ),
            )
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            self._release_operation(final_state="idle")
            return estimate

    def build_experiment_plan(
        self,
        *,
        variable_source_fractions: Mapping[int, float],
        protocol: ExperimentProtocolEstimate,
        measurement_name: str | None = None,
    ) -> ExperimentPlan:
        return self.build_manual_experiment_plans(
            requests=(
                ManualExperimentPlanRequest(
                    variable_source_fractions=dict(variable_source_fractions),
                    measurement_name=measurement_name,
                ),
            ),
            protocol=protocol,
        )[0]

    def build_manual_experiment_plans(
        self,
        *,
        requests: Sequence[ManualExperimentPlanRequest],
        protocol: ExperimentProtocolEstimate,
    ) -> tuple[ExperimentPlan, ...]:
        """Build one ordered manual queue under a shared protocol."""

        if not isinstance(protocol, ExperimentProtocolEstimate):
            raise TypeError("protocol must be an ExperimentProtocolEstimate")
        normalized_requests = tuple(requests)
        if not normalized_requests:
            raise ValueError("A manual experiment plan must contain at least one entry")
        if not all(
            isinstance(request, ManualExperimentPlanRequest)
            for request in normalized_requests
        ):
            raise TypeError("requests must contain ManualExperimentPlanRequest values")

        self._require_no_prepared_mixture("build another experiment plan")
        self._acquire_operation(
            state="planning", operation="planning manual experiment sequence"
        )
        try:
            planned_experiments: list[ExperimentPlan] = []
            for index, request in enumerate(normalized_requests, start=1):
                try:
                    plan = self._build_experiment_plan(
                        variable_source_fractions=request.variable_source_fractions,
                        protocol=protocol,
                        measurement_name=request.measurement_name,
                    )
                except (TypeError, ValueError) as error:
                    raise ValueError(f"Experiment {index}: {error}") from error
                planned_experiments.append(plan)
            plans = tuple(planned_experiments)
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            try:
                with self._state_lock:
                    self._active_plan = plans[0]
                    self._manual_play_sequence = _ManualPlaySequence(plans=plans)
                    self._measurement_sequence_number = None
                    self._save_checkpoint_locked(
                        last_operation=(
                            "manual experiment plan built"
                            if len(plans) == 1
                            else f"manual plan with {len(plans)} experiments built"
                        )
                    )
            except BaseException as error:
                self._finish_with_error(error)
                raise
            else:
                self._release_operation(final_state="idle")
                return plans

    # Manual Play orchestration ---

    def select_manual_experiment_plan(self, *, index: int) -> ExperimentPlan:
        """Select a built queue entry for explicit manual stage execution."""

        require_integer(index, name="index")
        if index < 0:
            raise ValueError("index must not be negative")
        if self.busy or self.runze_controller.busy or self.emstat_controller.busy:
            raise RuntimeError(
                "Cannot select an experiment while an operation is active"
            )
        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or index >= len(sequence.plans):
                raise ValueError("The selected experiment is not in the built plan")
            if sequence.state != "inactive":
                raise RuntimeError("Pause and discard Play before selecting manually")
            plan = sequence.plans[index]
            if (
                self._prepared_mixture is not None
                and self._prepared_mixture.plan != plan
            ):
                raise RuntimeError(
                    "Clean the active prepared mixture before selecting another "
                    "experiment"
                )
            sequence.active_index = index
            self._active_plan = plan
            self._save_checkpoint_locked(last_operation="manual experiment selected")
            return plan

    def start_manual_play(self, *, plans: Sequence[ExperimentPlan]) -> None:
        """Activate staged automatic execution for an ordered manual plan."""

        requested_plans = tuple(plans)
        if not requested_plans:
            raise ValueError("Play requires at least one planned experiment")
        shared_protocol = _manual_plan_protocol_key(requested_plans[0])
        for plan in requested_plans:
            self._require_implemented_workflow(plan)
            self._require_plan_current(plan)
            if _manual_plan_protocol_key(plan) != shared_protocol:
                raise ValueError(
                    "All experiments in a manual plan must use the same protocol"
                )
        self._require_no_pending_line_maintenance("start Play")

        if self.busy or self.runze_controller.busy or self.emstat_controller.busy:
            raise RuntimeError("Cannot start Play while another operation is active")

        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or sequence.plans != requested_plans:
                sequence = _ManualPlaySequence(plans=requested_plans)
                self._manual_play_sequence = sequence
            if sequence.state != "inactive":
                raise RuntimeError("Play is already active")
            if sequence.active_index >= len(sequence.plans):
                sequence.active_index = 0

            plan = sequence.plans[sequence.active_index]
            prepared = self._prepared_mixture
            if prepared is not None and prepared.plan != plan:
                raise RuntimeError(
                    "The active prepared mixture belongs to a different plan"
                )
            if self._prepared_mixture_volume_state == "unknown":
                raise RuntimeError(
                    "Cannot start Play while the prepared-mixture state is unknown"
                )

            unready_sources = [
                source.source_config_key
                for queued_plan in requested_plans
                for source in queued_plan.sources
                if not self._source_is_primed_locked(
                    selector_port=source.selector_port,
                    source_stock_id=source.source_stock_id,
                )
                and self._line_status_locked(source.selector_port) != "air_filled"
            ]
            if unready_sources:
                raise RuntimeError(
                    "Cannot start Play with unknown source-line contents; confirm "
                    "these lines as Primed or Air-filled first: "
                    + ", ".join(dict.fromkeys(unready_sources))
                )
            sequence.state = "running"
            self._active_plan = plan
            self._save_checkpoint_locked(last_operation="manual Play started")

    def request_manual_play_pause(self) -> bool:
        """Request a pause after the currently running experiment stage."""

        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or sequence.state != "running":
                return False
            sequence.state = "pause_requested"
            self._save_checkpoint_locked(
                last_operation="manual Play pause requested",
                operation_in_progress=self._physical_operation_checkpointed,
            )
            return True

    def cancel_manual_play_pause(self) -> bool:
        """Cancel a stage-boundary pause request which has not taken effect."""

        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or sequence.state != "pause_requested":
                return False
            sequence.state = "running"
            self._save_checkpoint_locked(
                last_operation="manual Play pause cancelled",
                operation_in_progress=self._physical_operation_checkpointed,
            )
            return True

    def resume_manual_play(self) -> bool:
        """Reactivate a Play sequence paused at an experiment-stage boundary."""

        if self.busy or self.runze_controller.busy or self.emstat_controller.busy:
            return False
        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or sequence.state != "paused":
                return False
            sequence.state = "running"
            self._save_checkpoint_locked(last_operation="manual Play resumed")
            return True

    def discard_manual_play(self) -> bool:
        """Deactivate Play at a safe stage boundary without changing fluids."""

        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or sequence.state != "paused":
                return False
            sequence.state = "inactive"
            self._save_checkpoint_locked(last_operation="manual Play discarded")
            return True

    def manual_play_next_stage(self) -> ManualPlayStage | None:
        """Return the next authoritative stage while Play is running."""

        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None:
                return None
            if sequence.state == "pause_requested":
                sequence.state = "paused"
                next_stage = self._manual_play_next_stage_locked()
                self._save_checkpoint_locked(
                    last_operation=(
                        "manual Play paused"
                        if next_stage is None
                        else f"manual Play paused before {next_stage}"
                    )
                )
                return None
            if sequence.state != "running":
                return None
            return self._manual_play_next_stage_locked()

    def finish_manual_play_stage(self, stage: ManualPlayStage) -> ManualPlayState:
        """Record a successful Play stage and apply a requested boundary pause."""

        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or sequence.state not in {"running", "pause_requested"}:
                raise RuntimeError("No running Play sequence is available")

            if stage == "clean":
                if (
                    self._prepared_mixture is not None
                    or self._prepared_mixture_volume_state is not None
                    or self._workflow_phase != "ready"
                ):
                    raise RuntimeError(
                        "Play clean stage completed without clearing the active mixture"
                    )
                sequence.active_index += 1
                self._active_plan = (
                    None
                    if sequence.active_index >= len(sequence.plans)
                    else sequence.plans[sequence.active_index]
                )
            else:
                expected_after_stage: dict[ManualPlayStage, ManualPlayStage] = {
                    "prime": "prepare",
                    "prepare": "measure",
                    "measure": "clean",
                    "clean": "prime",
                }
                actual_next_stage = self._manual_play_next_stage_locked()
                if actual_next_stage != expected_after_stage[stage]:
                    raise RuntimeError(
                        f"Play stage {stage!r} completed without advancing to "
                        f"{expected_after_stage[stage]!r}"
                    )

            next_stage = self._manual_play_next_stage_locked()
            if next_stage is None:
                sequence.state = "inactive"
                last_operation = "manual Play completed"
            elif sequence.state == "pause_requested":
                sequence.state = "paused"
                last_operation = f"manual Play paused before {next_stage}"
            else:
                last_operation = f"manual Play completed {stage}"

            self._save_checkpoint_locked(last_operation=last_operation)
            return sequence.state

    def deactivate_manual_play_after_failure(self) -> None:
        """Prevent automatic continuation after a stage-level failure."""

        with self._state_lock:
            sequence = self._manual_play_sequence
            if sequence is None or sequence.state == "inactive":
                return
            sequence.state = "inactive"
            if not self._physical_operation_checkpointed:
                self._save_checkpoint_locked(
                    last_operation="manual Play deactivated after failure"
                )

    # Individual experiment stages ---

    def prepare_mixture(self, *, plan: ExperimentPlan) -> PreparedMixture:
        self._require_implemented_workflow(plan)
        self._acquire_operation(
            state="preparing_mixture", operation="preparing experiment mixture"
        )
        try:
            self._mark_operation_started("preparing experiment mixture", plan=plan)
            prepared = self._prepare_mixture(plan=plan)
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            with self._state_lock:
                self._workflow_phase = "prepared"
            self._finish_physical_success(
                last_operation="mixture preparation completed"
            )
            return prepared

    def run_flow_cell_measurement(
        self, *, prepared_mixture: PreparedMixture, command_start_timeout_s: float = 5.0
    ) -> ExperimentRunResult:
        self._require_implemented_workflow(prepared_mixture.plan)
        require_non_negative_finite_number(
            command_start_timeout_s, name="command_start_timeout_s"
        )
        plan = prepared_mixture.plan
        operation = (
            (
                "running stopped-flow measurement"
                if plan.measurement_count == 1
                else "running stopped-flow measurement series"
            )
            if plan.workflow_kind == "stopped_flow"
            else "running continuous-flow measurement"
        )
        self._acquire_operation(state="measuring", operation=operation)
        try:
            self._mark_operation_started(operation, plan=prepared_mixture.plan)
            result = self._run_flow_cell_measurement(
                prepared_mixture=prepared_mixture,
                command_start_timeout_s=float(command_start_timeout_s),
            )
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            with self._state_lock:
                self._workflow_phase = "flow_completed"
            self._finish_physical_success(last_operation=f"{operation} completed")
            return result

    def clean_after_experiment(self, *, prepared_mixture: PreparedMixture) -> None:
        self._acquire_operation(state="cleaning", operation="cleaning experiment paths")
        try:
            self._mark_operation_started(
                "cleaning experiment paths", plan=prepared_mixture.plan
            )
            self._clean_after_experiment(prepared_mixture=prepared_mixture)
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            with self._state_lock:
                self._workflow_phase = "ready"
            self._finish_physical_success(last_operation="experiment cleanup completed")

    def run_experiment(
        self,
        *,
        plan: ExperimentPlan,
        perform_cleanup: bool = True,
        command_start_timeout_s: float = 5.0,
    ) -> ExperimentRunResult:
        """Run priming-if-needed, mixture preparation, flow measurement, and cleanup."""

        self._require_implemented_workflow(plan)
        require_non_negative_finite_number(
            command_start_timeout_s, name="command_start_timeout_s"
        )
        self._acquire_operation(state="priming", operation="running experiment")

        try:
            self._require_plan_current(plan)
            self._set_active_stage(state="priming", operation="priming source lines")
            with self._state_lock:
                self._active_plan = plan
            self._prime_required_sources(rpm=plan.handling_rpm)
            with self._state_lock:
                self._workflow_phase = "ready"
                self._save_checkpoint_locked(last_operation="priming completed")

            self._set_active_stage(
                state="preparing_mixture", operation="preparing experiment mixture"
            )
            self._mark_operation_started("preparing experiment mixture", plan=plan)
            prepared = self._prepare_mixture(plan=plan)
            with self._state_lock:
                self._workflow_phase = "prepared"
                self._save_checkpoint_locked(
                    last_operation="mixture preparation completed"
                )

            self._set_active_stage(
                state="measuring", operation="running flow-cell measurement"
            )
            self._mark_operation_started(
                "running flow-cell measurement", plan=prepared.plan
            )
            result = self._run_flow_cell_measurement(
                prepared_mixture=prepared,
                command_start_timeout_s=float(command_start_timeout_s),
            )
            with self._state_lock:
                self._workflow_phase = "flow_completed"
                self._save_checkpoint_locked(last_operation="measurement completed")

            if perform_cleanup:
                self._set_active_stage(
                    state="cleaning", operation="cleaning experiment paths"
                )
                self._mark_operation_started(
                    "cleaning experiment paths", plan=prepared.plan
                )
                self._clean_after_experiment(prepared_mixture=prepared)
                with self._state_lock:
                    self._workflow_phase = "ready"
                    self._save_checkpoint_locked(
                        last_operation="experiment cleanup completed"
                    )
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            self._release_operation(final_state="completed")
            return result

    def clear_prepared_mixture_state(self) -> None:
        """Forget a manually recovered or discarded physical mixture."""

        with self._state_lock:
            if self._state in _BUSY_STATES:
                raise RuntimeError("Cannot clear mixture state during an experiment")
            self._prepared_mixture = None
            self._prepared_mixture_volume_state = None
            if self._active_plan is not None:
                self._workflow_phase = "ready"
            self._save_checkpoint_locked(
                last_operation="prepared mixture state cleared"
            )

    # Objective analysis and result retrieval ---

    def current_source_setup_id(self) -> int | None:
        """Return the saved setup matching the currently enabled stock identities."""

        with self._state_lock:
            source_stock_ids = tuple(
                cast(int, entry.source_stock_id)
                for entry in self._source_configuration.enabled_entries
                if entry.source_stock_id is not None
            )

        return self.repository.find_source_setup_id(source_stock_ids=source_stock_ids)

    def current_source_setup_identity(self) -> SourceSetupIdentity | None:
        """Return chemical and record-level identities for enabled assigned stocks."""

        with self._state_lock:
            source_stock_ids = tuple(
                cast(int, entry.source_stock_id)
                for entry in self._source_configuration.enabled_entries
                if entry.source_stock_id is not None
            )
        if not source_stock_ids:
            return None
        return self.repository.source_setup_identity(source_stock_ids=source_stock_ids)

    def available_result_source_scopes(self) -> tuple[ResultSourceScope, ...]:
        identity = self.current_source_setup_identity()
        if identity is None:
            return ("all_setups",)

        scopes: list[ResultSourceScope] = ["all_setups", "same_components"]
        if identity.has_multicomponent_stock:
            scopes.append("same_relative_formulation")
        scopes.extend(("same_exact_composition", "current_stocks"))
        return tuple(scopes)

    def objective_observations(
        self,
        *,
        measurement_type: MeasurementType,
        objective_name: ObjectiveName = DEFAULT_OBJECTIVE,
        source_scope: ResultSourceScope = "current_stocks",
        concentration_constraints: Sequence[MixtureConcentrationConstraint] = (),
        settings_fingerprint: str | None = None,
        protocol_key: str | None = None,
    ) -> tuple[MetricObservation, ...]:
        definition = OBJECTIVE_DEFINITIONS[objective_name]
        source_setup_match: SourceSetupMatch | None = None
        if source_scope != "all_setups":
            identity = self.current_source_setup_identity()
            if identity is None:
                return ()
            if source_scope == "same_components":
                source_setup_match = SourceSetupMatch(
                    "component_layout", identity.component_layout_fingerprint
                )
            elif source_scope == "same_relative_formulation":
                source_setup_match = SourceSetupMatch(
                    "relative_formulation", identity.relative_formulation_fingerprint
                )
            elif source_scope == "same_exact_composition":
                source_setup_match = SourceSetupMatch(
                    "exact_composition", identity.exact_composition_fingerprint
                )
            elif source_scope == "current_stocks":
                source_setup_match = SourceSetupMatch(
                    "stock_records", identity.stock_fingerprint
                )
            else:
                raise ValueError(f"Unsupported result source scope: {source_scope!r}")

        return self.repository.list_metric_observations(
            source_setup_match=source_setup_match,
            concentration_constraints=concentration_constraints,
            measurement_type=measurement_type,
            metric_name=definition.name,
            analysis_method=definition.analysis_method,
            analysis_version=definition.analysis_version,
            settings_fingerprint=settings_fingerprint,
            protocol_key=protocol_key,
        )

    def objective_experiment_summaries(
        self,
        *,
        measurement_type: MeasurementType,
        objective_name: ObjectiveName = DEFAULT_OBJECTIVE,
        source_scope: ResultSourceScope = "current_stocks",
        concentration_constraints: Sequence[MixtureConcentrationConstraint] = (),
        settings_fingerprint: str | None = None,
        protocol_key: str | None = None,
    ) -> tuple[ExperimentObjectiveSummary, ...]:
        """Aggregate compatible measurement objectives by experiment."""

        observations = self.objective_observations(
            measurement_type=measurement_type,
            objective_name=objective_name,
            source_scope=source_scope,
            concentration_constraints=concentration_constraints,
            settings_fingerprint=settings_fingerprint,
            protocol_key=protocol_key,
        )
        grouped: dict[int, list[MetricObservation]] = {}
        for observation in observations:
            grouped.setdefault(observation.mixture_id, []).append(observation)

        runs_by_id = {
            run.mixture_id: run for run in self.repository.list_saved_experiment_runs()
        }
        summaries: list[ExperimentObjectiveSummary] = []
        for experiment_id, items in grouped.items():
            ordered = sorted(
                items, key=lambda item: (item.run_sequence_number, item.measurement_id)
            )
            units = {item.unit for item in ordered}
            if len(units) != 1:
                raise RuntimeError(
                    f"Experiment {experiment_id} has incompatible objective units"
                )
            values = tuple(item.value for item in ordered)
            saved_run = runs_by_id.get(experiment_id)
            summaries.append(
                ExperimentObjectiveSummary(
                    experiment_id=experiment_id,
                    value=math.fsum(values) / len(values),
                    unit=ordered[0].unit,
                    between_measurement_standard_deviation=(
                        statistics.stdev(values) if len(values) > 1 else None
                    ),
                    measurements=tuple(
                        MeasurementObjectiveSummary(
                            measurement_id=item.measurement_id,
                            sequence_number=item.run_sequence_number,
                            measurement_name=item.measurement_name,
                            value=item.value,
                            unit=item.unit,
                            within_measurement_standard_error=item.standard_error,
                            contributions=item.contributions,
                        )
                        for item in ordered
                    ),
                    measurement_statuses=(
                        ()
                        if saved_run is None
                        else tuple(
                            ExperimentMeasurementStatus(
                                measurement_id=item.measurement_id,
                                sequence_number=item.sequence_number,
                                status=item.status,
                            )
                            for item in saved_run.measurements
                        )
                    ),
                    fractions=ordered[0].fractions,
                )
            )
        return tuple(summaries)

    def current_measurement_objectives(
        self, *, measurement_id: int
    ) -> tuple[MeasurementMetric, ...]:
        """Return only registered current-version objectives for a measurement."""

        metrics = self.repository.list_measurement_metrics(
            measurement_id=measurement_id
        )
        current_keys = {
            (definition.name, definition.analysis_method, definition.analysis_version)
            for definition in OBJECTIVE_DEFINITIONS.values()
        }
        return tuple(
            metric
            for metric in metrics
            if (metric.metric_name, metric.analysis_method, metric.analysis_version)
            in current_keys
        )

    def analyze_completed_measurements(
        self,
        *,
        measurement_type: MeasurementType | None = None,
        objective_name: ObjectiveName = DEFAULT_OBJECTIVE,
    ) -> ObjectiveAnalysisBatchResult:
        """Recalculate an objective for all matching completed raw sessions.

        Metric persistence is an upsert, so rerunning this operation is safe.
        An unreadable session is reported without preventing the remaining
        completed measurements from being analyzed.
        """

        if self.busy or self.emstat_controller.busy:
            raise RuntimeError("Cannot analyze saved sessions during an operation")

        analyzed: list[int] = []
        errors: list[str] = []

        with self._analysis_lock:
            records = self.repository.list_completed_measurements_for_analysis(
                measurement_type=measurement_type
            )
            for record in records:
                try:
                    measurement = self.emstat_controller.load_saved_measurement_session(
                        file_name=record.session_file_path.as_posix()
                    )
                    measurement_settings = _measurement_settings_from_payload(
                        record.measurement_type, record.settings
                    )
                    self._calculate_and_save_objective(
                        measurement_id=record.measurement_id,
                        measurement_type=record.measurement_type,
                        measurement=measurement,
                        measurement_settings=measurement_settings,
                        objective_name=objective_name,
                    )
                except Exception as error:
                    logger.exception(
                        "Failed to analyze completed measurement %s",
                        record.measurement_id,
                    )
                    errors.append(f"Measurement {record.measurement_id}: {error}")
                else:
                    analyzed.append(record.measurement_id)

        return ObjectiveAnalysisBatchResult(tuple(analyzed), tuple(errors))

    def list_saved_experiment_runs(self) -> tuple[SavedExperimentRun, ...]:
        return self.repository.list_saved_experiment_runs()

    def saved_protocol_for_run(
        self, run: SavedExperimentRun
    ) -> SavedExperimentProtocol:
        return _saved_protocol_from_snapshot(run.protocol_snapshot)

    def load_saved_experiment_measurement(
        self, *, run: SavedExperimentRun, measurement: SavedExperimentMeasurement
    ) -> EmStatLoadedMeasurement:
        path = measurement.session_file_path
        if path is None:
            raise RuntimeError(
                f"Measurement {measurement.measurement_id} has no saved session"
            )
        if path.parts[:1] != (run.artifact_directory,):
            raise RuntimeError(
                f"Measurement {measurement.measurement_id} session is outside its "
                "experiment artifact directory"
            )
        return self.emstat_controller.load_saved_measurement(file_name=path.as_posix())

    # Stop coordination ---

    def emergency_stop(self) -> ExperimentStopResult:
        with self._state_lock:
            if self._state not in _BUSY_STATES:
                return ExperimentStopResult(False, False, ())
            self._state = "stopping"
            self._operation = "stopping experiment"

        runze_stop_requested = False
        emstat_abort_requested = False
        errors: list[str] = []

        if (
            self.runze_controller.connected
            and self.runze_controller.busy
            and self.runze_controller.active_target is not None
        ):
            try:
                runze_stop_requested = self.runze_controller.emergency_stop_active()
            except Exception as error:
                logger.exception("Runze emergency stop failed")
                errors.append(f"Runze emergency stop failed: {error}")

        if self.emstat_controller.abort_available:
            try:
                emstat_abort_requested = self.emstat_controller.abort_measurement()
            except Exception as error:
                logger.exception("EmStat abort failed")
                errors.append(f"EmStat abort failed: {error}")

        return ExperimentStopResult(
            runze_stop_requested=runze_stop_requested,
            emstat_abort_requested=emstat_abort_requested,
            errors=tuple(errors),
        )

    # Internal planning and source helpers ---

    @staticmethod
    def _require_implemented_workflow(plan: ExperimentPlan) -> None:
        _validate_workflow(plan.workflow_kind, plan.measurement_count)

    def _set_measurement_sequence_number(self, sequence_number: int) -> None:
        require_positive_integer(sequence_number, name="sequence_number")
        with self._state_lock:
            plan = self._active_plan
            if plan is None:
                raise RuntimeError("Cannot start a measurement without an active plan")
            if sequence_number > plan.measurement_count:
                raise ValueError(
                    f"Measurement sequence {sequence_number} exceeds the planned "
                    f"count of {plan.measurement_count}"
                )
            self._measurement_sequence_number = sequence_number

    def _build_experiment_plan(
        self,
        *,
        variable_source_fractions: Mapping[int, float],
        protocol: ExperimentProtocolEstimate,
        measurement_name: str | None,
    ) -> ExperimentPlan:
        with self._state_lock:
            source_configuration = self._source_configuration
        self._validate_source_configuration(source_configuration)
        sources = self._resolve_plan_sources(
            source_configuration=source_configuration,
            variable_source_fractions=variable_source_fractions,
        )
        plumbing = self.runze_controller.plumbing
        if plumbing is None:
            raise RuntimeError("Runze plumbing is required for mixture planning")
        fractions_by_source_key = {
            source.source_config_key: source.fraction for source in sources
        }
        mixture_transfers = build_mixture_transfer_diagnostics(
            plumbing=plumbing,
            syringe_volume_ml=self.runze_controller.syringe_capacity_ml,
            source_fractions=fractions_by_source_key,
            total_volume_ml=protocol.total_mixture_volume_ml,
            water_distribution=protocol.water_distribution,
            use_common_line_strokes=protocol.workflow_kind == "stopped_flow",
        )

        normalized_name = None
        if measurement_name is not None:
            normalized_name = measurement_name.strip() or None

        return ExperimentPlan(
            source_configuration_revision=source_configuration.revision,
            sources=sources,
            measurement_type=protocol.measurement_type,
            measurement_settings=protocol.measurement_settings,
            measurement_name=normalized_name,
            handling_rpm=protocol.handling_rpm,
            inlet_aspiration_rpm=protocol.inlet_aspiration_rpm,
            approach_rpm=protocol.approach_rpm,
            flow_rpm=protocol.flow_rpm,
            water_distribution=protocol.water_distribution,
            cleanup_settings=protocol.cleanup_settings,
            estimated_measurement_duration_s=(
                protocol.estimated_measurement_duration_s
            ),
            flow_rate_ml_min=protocol.flow_rate_ml_min,
            measurement_flow_volume_ml=protocol.measurement_flow_volume_ml,
            mixing_path_condition_volume_ml=(protocol.mixing_path_condition_volume_ml),
            flow_cell_load_volume_ml=protocol.flow_cell_load_volume_ml,
            mixing_vessel_reserve_volume_ml=(protocol.mixing_vessel_reserve_volume_ml),
            total_mixture_volume_ml=protocol.total_mixture_volume_ml,
            substance_concentrations=self._plan_substance_concentrations(sources),
            mixture_transfers=mixture_transfers,
            workflow_kind=protocol.workflow_kind,
            measurement_count=protocol.measurement_count,
            between_measurement_displacement_volume_ml=(
                protocol.between_measurement_displacement_volume_ml
            ),
        )

    def _estimate_experiment_protocol(
        self,
        *,
        measurement_settings: EmStatMeasurementSettings,
        handling_rpm: int,
        inlet_aspiration_rpm: int | None = None,
        flow_rpm: int | None,
        approach_rpm: int | None,
        water_distribution: WaterDistribution,
        cleanup_settings: ExperimentCleanupSettings,
        workflow_kind: ExperimentWorkflowKind,
        measurement_count: int,
        between_measurement_displacement_volume_ml: float | None,
    ) -> ExperimentProtocolEstimate:
        self._ensure_emstat_connected()
        measurement_type, estimated_duration_s = self._measurement_details(
            measurement_settings
        )
        require_positive_finite_number(
            estimated_duration_s, name="estimated_measurement_duration_s"
        )

        mixing_vessel = self._require_destination(
            self._plumbing.mixing_vessel, "mixing"
        )
        flow_cell = self._require_destination(self._plumbing.flow_cell, "flow cell")
        mixing_route_volume_ml = self._require_route_volume(mixing_vessel)
        flow_cell_route_volume_ml = self._require_route_volume(flow_cell)

        if workflow_kind == "continuous_flow":
            if flow_rpm is None:
                raise ValueError("flow_rpm is required for continuous flow")
            flow_rate_ml_min: float | None = (
                self.runze_controller.syringe_flow_rate_ml_per_min_from_rpm(flow_rpm)
            )
            require_positive_finite_number(flow_rate_ml_min, name="flow_rate_ml_min")
            measurement_flow_volume_ml: float | None = (
                flow_rate_ml_min * float(estimated_duration_s) / 60.0
            )
            require_positive_finite_number(
                measurement_flow_volume_ml, name="measurement_flow_volume_ml"
            )
            mixing_path_condition_volume_ml = math.fsum(
                (
                    mixing_route_volume_ml,
                    self._plumbing.common_line_dead_volume_ml,
                    self._plumbing.clearance_margin_ml,
                )
            )
            flow_cell_load_volume_ml = math.fsum(
                (
                    flow_cell_route_volume_ml,
                    self._plumbing.flow_cell_overtravel_volume_ml,
                    measurement_flow_volume_ml,
                )
            )
            resolved_approach_rpm = flow_rpm if approach_rpm is None else approach_rpm
        else:
            if approach_rpm is None:
                raise ValueError("approach_rpm is required for stopped flow")
            if (
                measurement_count > 1
                and between_measurement_displacement_volume_ml is None
            ):
                raise ValueError(
                    "between-measurement displacement is required for stopped flow"
                )
            total_between_measurement_volume_ml = (
                0.0
                if measurement_count == 1
                else (measurement_count - 1)
                * cast(float, between_measurement_displacement_volume_ml)
            )
            flow_rate_ml_min = None
            measurement_flow_volume_ml = None
            mixing_path_condition_volume_ml = math.fsum(
                (mixing_route_volume_ml, self._plumbing.clearance_margin_ml)
            )
            flow_cell_load_volume_ml = math.fsum(
                (
                    flow_cell_route_volume_ml,
                    2 * self._plumbing.flow_cell_overtravel_volume_ml,
                    total_between_measurement_volume_ml,
                )
            )
            resolved_approach_rpm = approach_rpm
        total_mixture_volume_ml = math.fsum(
            (
                mixing_path_condition_volume_ml,
                flow_cell_load_volume_ml,
                self._plumbing.mixing_vessel_reserve_volume_ml,
            )
        )

        syringe_capacity_ml = self.runze_controller.syringe_capacity_ml
        if mixing_path_condition_volume_ml > syringe_capacity_ml:
            raise ValueError(
                "Mixing-path conditioning volume exceeds syringe capacity: "
                f"{mixing_path_condition_volume_ml:g} mL > "
                f"{syringe_capacity_ml:g} mL"
            )
        if workflow_kind == "continuous_flow" and (
            flow_cell_load_volume_ml > syringe_capacity_ml
        ):
            raise ValueError(
                "Flow-cell loading and measurement volume exceed syringe capacity: "
                f"{flow_cell_load_volume_ml:g} mL > {syringe_capacity_ml:g} mL"
            )
        if workflow_kind == "stopped_flow":
            stopped_chase_volume_ml = math.fsum(
                (
                    flow_cell_route_volume_ml,
                    self._plumbing.flow_cell_overtravel_volume_ml,
                    cleanup_settings.flow_cell_rinse_through_volume_ml,
                )
            )
            available_stroke_capacity_ml = math.fsum(
                (syringe_capacity_ml, -stopped_chase_volume_ml)
            )
            if available_stroke_capacity_ml <= 0:
                raise ValueError(
                    "Stopped-flow chase water leaves no syringe capacity for a "
                    "mixture stroke"
                )

        return ExperimentProtocolEstimate(
            measurement_type=measurement_type,
            measurement_settings=measurement_settings,
            handling_rpm=handling_rpm,
            inlet_aspiration_rpm=inlet_aspiration_rpm,
            approach_rpm=resolved_approach_rpm,
            flow_rpm=flow_rpm,
            water_distribution=water_distribution,
            cleanup_settings=cleanup_settings,
            estimated_measurement_duration_s=float(estimated_duration_s),
            flow_rate_ml_min=flow_rate_ml_min,
            measurement_flow_volume_ml=measurement_flow_volume_ml,
            mixing_path_condition_volume_ml=mixing_path_condition_volume_ml,
            flow_cell_load_volume_ml=flow_cell_load_volume_ml,
            mixing_vessel_reserve_volume_ml=(
                self._plumbing.mixing_vessel_reserve_volume_ml
            ),
            total_mixture_volume_ml=total_mixture_volume_ml,
            workflow_kind=workflow_kind,
            measurement_count=measurement_count,
            between_measurement_displacement_volume_ml=(
                between_measurement_displacement_volume_ml
            ),
        )

    @staticmethod
    def _validate_protocol_inputs(
        *,
        handling_rpm: int,
        inlet_aspiration_rpm: int | None,
        flow_rpm: int | None,
        approach_rpm: int | None,
        water_distribution: WaterDistribution,
        cleanup_settings: ExperimentCleanupSettings,
        workflow_kind: ExperimentWorkflowKind,
    ) -> None:
        require_positive_integer(handling_rpm, name="handling_rpm")
        if inlet_aspiration_rpm is not None:
            require_positive_integer(inlet_aspiration_rpm, name="inlet_aspiration_rpm")
        if flow_rpm is not None:
            require_positive_integer(flow_rpm, name="flow_rpm")
        if approach_rpm is not None:
            require_positive_integer(approach_rpm, name="approach_rpm")
        if workflow_kind == "stopped_flow" and approach_rpm is None:
            raise ValueError("Approach RPM is required for stopped-flow experiments")
        if water_distribution not in ("single", "between_components"):
            raise ValueError(f"Unsupported water distribution: {water_distribution}")
        if not isinstance(cleanup_settings, ExperimentCleanupSettings):
            raise TypeError("cleanup_settings must be an ExperimentCleanupSettings")

    def _resolve_plan_sources(
        self,
        *,
        source_configuration: CurrentSourceConfiguration,
        variable_source_fractions: Mapping[int, float],
    ) -> tuple[ExperimentPlanSource, ...]:
        enabled_entries = source_configuration.enabled_entries
        locked_entries = tuple(
            entry for entry in enabled_entries if entry.locked_fraction is not None
        )
        variable_entries = tuple(
            entry
            for entry in enabled_entries
            if entry.locked_fraction is None
            and entry.source_config_key != WATER_SOURCE_CONFIG_KEY
        )
        variable_by_stock = {
            cast(int, entry.source_stock_id): entry for entry in variable_entries
        }

        supplied: dict[int, float] = {}
        for source_stock_id, fraction in variable_source_fractions.items():
            require_positive_integer(source_stock_id, name="source_stock_id")
            require_non_negative_finite_number(
                fraction, name=f"fraction[{source_stock_id}]"
            )
            normalized_fraction = float(fraction)
            if normalized_fraction > 1:
                raise ValueError(
                    f"Fraction for source stock {source_stock_id} must not exceed 1"
                )
            supplied[source_stock_id] = normalized_fraction

        expected_ids = set(variable_by_stock)
        supplied_ids = set(supplied)
        if supplied_ids != expected_ids:
            missing = expected_ids - supplied_ids
            unexpected = supplied_ids - expected_ids
            details: list[str] = []
            if missing:
                details.append(
                    "missing " + ", ".join(str(value) for value in sorted(missing))
                )
            if unexpected:
                details.append(
                    "unexpected "
                    + ", ".join(str(value) for value in sorted(unexpected))
                )
            raise ValueError(
                "Variable fractions must match the enabled unlocked sources: "
                + "; ".join(details)
            )

        locked_sum = math.fsum(
            cast(float, entry.locked_fraction) for entry in locked_entries
        )
        variable_sum = math.fsum(supplied.values())
        water_entry = next(
            (
                entry
                for entry in enabled_entries
                if entry.source_config_key == WATER_SOURCE_CONFIG_KEY
            ),
            None,
        )
        if water_entry is None:
            raise ValueError("The configured water source must remain enabled")

        available_after_locked = 1.0 - locked_sum
        if variable_sum > available_after_locked + _FRACTION_SUM_TOLERANCE:
            raise ValueError(
                "Unlocked non-water fractions exceed the fraction remaining after "
                f"locked sources ({available_after_locked:g}); got {variable_sum:g}"
            )

        water_is_balance = water_entry.locked_fraction is None
        if water_is_balance:
            water_fraction = max(0.0, available_after_locked - variable_sum)
        else:
            water_fraction = cast(float, water_entry.locked_fraction)
            expected_variable_sum = max(0.0, 1.0 - locked_sum)
            if not math.isclose(
                variable_sum,
                expected_variable_sum,
                rel_tol=0.0,
                abs_tol=_FRACTION_SUM_TOLERANCE,
            ):
                raise ValueError(
                    "Unlocked source fractions must sum to the fraction remaining "
                    f"after locked sources ({expected_variable_sum:g}); got "
                    f"{variable_sum:g}"
                )

        stock_by_id = {
            stock.id: stock for stock in self.repository.list_source_stocks()
        }

        sources: list[ExperimentPlanSource] = []
        for entry in enabled_entries:
            source_stock_id = entry.source_stock_id
            if entry.locked_fraction is not None:
                fraction = entry.locked_fraction
            elif entry.source_config_key == WATER_SOURCE_CONFIG_KEY:
                fraction = water_fraction
            else:
                fraction = supplied[cast(int, source_stock_id)]

            control_label: str | None = None
            stock_control_concentration: float | None = None
            if source_stock_id is not None and entry.control_substance_id is not None:
                stock = stock_by_id.get(source_stock_id)
                if stock is None:
                    raise ValueError(f"Unknown source stock ID: {source_stock_id}")
                component = stock.component(entry.control_substance_id)
                if component is None:
                    raise ValueError(
                        f"Controlled substance {entry.control_substance_id} is not "
                        f"present in source stock {source_stock_id}"
                    )
                control_label = component.substance_label
                stock_control_concentration = component.concentration_mol_l

            sources.append(
                ExperimentPlanSource(
                    source_stock_id=source_stock_id,
                    selector_port=entry.selector_port,
                    source_config_key=entry.source_config_key,
                    fraction=fraction,
                    locked_fraction=entry.locked_fraction,
                    control_substance_id=entry.control_substance_id,
                    control_substance_label=control_label,
                    stock_control_concentration_mol_l=(stock_control_concentration),
                )
            )

        return tuple(sorted(sources, key=lambda source: source.selector_port))

    def _plan_substance_concentrations(
        self, sources: Sequence[ExperimentPlanSource]
    ) -> tuple[ExperimentPlanSubstanceConcentration, ...]:
        stock_by_id: dict[int, SourceStock] = {
            stock.id: stock for stock in self.repository.list_source_stocks()
        }
        values: dict[int, list[float]] = {}
        labels: dict[int, str] = {}
        for source in sources:
            if source.source_stock_id is None or source.fraction == 0:
                continue
            stock = stock_by_id[source.source_stock_id]
            for component in stock.components:
                labels[component.substance_id] = component.substance_label
                values.setdefault(component.substance_id, []).append(
                    source.fraction * component.concentration_mol_l
                )
        return tuple(
            ExperimentPlanSubstanceConcentration(
                substance_id=substance_id,
                substance_label=labels[substance_id],
                concentration_mol_l=math.fsum(contributions),
            )
            for substance_id, contributions in sorted(
                values.items(), key=lambda item: labels[item[0]].lower()
            )
        )

    def _measurement_details(
        self, settings: EmStatMeasurementSettings
    ) -> tuple[EmStatMeasurementType, float]:
        if isinstance(settings, ChronoamperometrySettings):
            return (
                "chronoamperometry",
                self.emstat_controller.estimate_chronoamperometry_duration_s(
                    settings=settings
                ),
            )
        if isinstance(settings, CyclicVoltammetrySettings):
            return (
                "cyclic_voltammetry",
                self.emstat_controller.estimate_cyclic_voltammetry_duration_s(
                    settings=settings
                ),
            )
        if isinstance(settings, LinearSweepVoltammetrySettings):
            return (
                "linear_sweep_voltammetry",
                self.emstat_controller.estimate_linear_sweep_voltammetry_duration_s(
                    settings=settings
                ),
            )
        raise TypeError("Unsupported EmStat measurement settings")

    def _prime_required_sources(self, *, rpm: int) -> bool:
        with self._state_lock:
            configuration = self._source_configuration
        self._validate_source_configuration(configuration)

        required_entries = configuration.enabled_entries
        with self._state_lock:
            unprimed_entries = tuple(
                entry
                for entry in required_entries
                if not self._entry_is_primed_locked(entry)
            )
            unknown_entries = tuple(
                entry
                for entry in unprimed_entries
                if self._line_status_locked(entry.selector_port) != "air_filled"
            )

        if not unprimed_entries:
            logger.info("All enabled source lines are already marked primed")
            return False
        if unknown_entries:
            raise RuntimeError(
                "Cannot prime a source line with unknown physical contents; "
                "confirm that these enabled lines are air-filled first: "
                + ", ".join(entry.source_config_key for entry in unknown_entries)
            )

        involved_ports = {entry.selector_port for entry in unprimed_entries}
        water_port = self._plumbing.water.number
        prime_water = water_port in involved_ports

        self._mark_operation_started("priming source lines")

        with self._state_lock:
            for port in involved_ports:
                self._set_line_state_locked(
                    port, "unknown", reason="Priming operation in progress"
                )
            self._save_checkpoint_locked(
                last_operation="priming source lines", operation_in_progress=True
            )

        try:
            self.runze_controller.prime_selected_liquid_source_lines(
                source_ports=tuple(sorted(involved_ports)),
                rpm=rpm,
                prime_water=prime_water,
            )
        except BaseException:
            with self._state_lock:
                for port in involved_ports:
                    self._set_line_state_locked(
                        port, "unknown", reason="Priming did not complete"
                    )
            raise

        assignment_by_port = {entry.selector_port: entry for entry in required_entries}
        primed_at = datetime.now(UTC)
        with self._state_lock:
            for port in involved_ports:
                entry = assignment_by_port.get(port)
                if entry is not None:
                    self._line_state_by_port[port] = _SourceLineIdentity(
                        source_stock_id=entry.source_stock_id,
                        status="primed",
                        updated_at=primed_at,
                        reason="Primed by experiment workflow",
                    )
            self._line_maintenance_phase = "not_started"
            self._pending_line_maintenance = None

        return True

    # Internal physical and persistence stages ---

    def _prepare_mixture(self, *, plan: ExperimentPlan) -> PreparedMixture:
        with self._state_lock:
            if self._prepared_mixture is not None:
                raise RuntimeError(
                    "A prepared mixture is already active; clean it or explicitly "
                    "clear its state before preparing another"
                )
        self._require_plan_current(plan)
        self._require_plan_sources_primed(plan)

        source_setup_id = self.repository.find_or_create_source_setup(
            source_stock_ids=tuple(
                cast(int, source.source_stock_id)
                for source in plan.sources
                if source.source_stock_id is not None
            )
        )
        mixture_id = self.repository.create_mixture(
            source_setup_id=source_setup_id,
            source_configuration_revision=plan.source_configuration_revision,
            total_volume_ml=plan.total_mixture_volume_ml,
            components=tuple(
                MixtureComponent(
                    source_stock_id=source.source_stock_id,
                    fraction=source.fraction,
                    selector_port=source.selector_port,
                    source_config_key=source.source_config_key,
                    locked_fraction=source.locked_fraction,
                )
                for source in plan.sources
            ),
            context=self._plan_volume_context(plan),
            commanded_volumes_ml={
                source.source_config_key: math.fsum(
                    t.nominal_volume_ml
                    for t in plan.mixture_transfers
                    if t.source_config_key == source.source_config_key
                )
                for source in plan.sources
            },
            protocol_snapshot=_experiment_protocol_snapshot_to_payload(plan),
        )
        with self._state_lock:
            self._mixture_id = mixture_id
            self._active_plan = plan

        self.repository.set_mixture_status(mixture_id, "running")
        try:
            if plan.workflow_kind == "continuous_flow":
                self._prepare_continuous_flow_mixture(plan)
            else:
                self._prepare_stopped_flow_mixture(plan)
        except (OperationStoppedError, SequenceStoppedError) as error:
            self.repository.set_mixture_status(
                mixture_id, "aborted", error_message=str(error)
            )
            raise
        except Exception as error:
            self.repository.set_mixture_status(
                mixture_id, "failed", error_message=str(error)
            )
            raise

        self.repository.set_mixture_status(mixture_id, "completed")
        prepared = PreparedMixture(mixture_id=mixture_id, plan=plan)
        with self._state_lock:
            self._prepared_mixture = prepared
            self._prepared_mixture_volume_state = "prepared"
        return prepared

    def _prepare_continuous_flow_mixture(self, plan: ExperimentPlan) -> None:
        self.runze_controller.prepare_mixture_with_single_aspiration_transfers(
            source_fractions=plan.fractions_by_source_key,
            total_volume_ml=plan.total_mixture_volume_ml,
            rpm=plan.handling_rpm,
            inlet_aspiration_rpm=plan.inlet_aspiration_rpm,
            water_distribution=plan.water_distribution,
        )

    def _prepare_stopped_flow_mixture(self, plan: ExperimentPlan) -> None:
        self.runze_controller.prepare_mixture_with_common_line_stroke_transfers(
            source_fractions=plan.fractions_by_source_key,
            total_volume_ml=plan.total_mixture_volume_ml,
            rpm=plan.handling_rpm,
            inlet_aspiration_rpm=plan.inlet_aspiration_rpm,
            water_distribution=plan.water_distribution,
        )

    def _run_flow_cell_measurement(
        self, *, prepared_mixture: PreparedMixture, command_start_timeout_s: float
    ) -> ExperimentRunResult:
        self._require_implemented_workflow(prepared_mixture.plan)
        if prepared_mixture.plan.workflow_kind == "stopped_flow":
            return self._run_stopped_flow_measurements(
                prepared_mixture=prepared_mixture
            )
        return self._run_continuous_flow_measurement(
            prepared_mixture=prepared_mixture,
            command_start_timeout_s=command_start_timeout_s,
        )

    def _run_continuous_flow_measurement(
        self, *, prepared_mixture: PreparedMixture, command_start_timeout_s: float
    ) -> ExperimentRunResult:
        with self._state_lock:
            if self._prepared_mixture != prepared_mixture:
                raise RuntimeError("The selected prepared mixture is not active")
            if self._prepared_mixture_volume_state != "prepared":
                raise RuntimeError(
                    "This prepared mixture has already been used for a flow run"
                )
            self._active_plan = prepared_mixture.plan
        self._set_measurement_sequence_number(1)

        plan = prepared_mixture.plan
        flow_rpm = plan.flow_rpm
        if flow_rpm is None:
            raise RuntimeError("Continuous-flow plan has no Flow RPM")
        measurement_flow_volume_ml = plan.measurement_flow_volume_ml
        if measurement_flow_volume_ml is None:
            raise RuntimeError("Continuous-flow plan has no measurement flow volume")
        self._ensure_emstat_connected()
        measurement_id = self.repository.create_measurement(
            mixture_id=prepared_mixture.mixture_id,
            measurement_type=plan.measurement_type,
            origin="experiment",
            measurement_name=plan.measurement_name,
            settings=cast(Mapping[str, object], asdict(plan.measurement_settings)),
            protocol_key="liquid_filled_flow_cell",
            flow_rpm=flow_rpm,
            flow_rate_ml_min=plan.flow_rate_ml_min,
            flow_volume_ml=plan.measurement_flow_volume_ml,
            context=self._plan_volume_context(plan),
            run_sequence_number=1,
        )
        with self._state_lock:
            self._measurement_id = measurement_id

        session = _MeasurementFlowSession(
            controller=self.emstat_controller,
            action=lambda on_command_started: self._measure(
                plan,
                on_command_started=on_command_started,
                session_directory=f"{prepared_mixture.mixture_id:06d}_experiment",
                session_sequence_number=1,
            ),
            command_start_timeout_s=command_start_timeout_s,
        )
        flow_started_at: str | None = None

        def before_flow() -> None:
            nonlocal flow_started_at
            session.start()
            flow_started_at = _utc_now()
            self.repository.set_measurement_status(
                measurement_id, "running", flow_started_at=flow_started_at
            )

        def before_fluid_handling() -> None:
            # From this point a stopped or failed Runze sequence may have consumed
            # an unknown portion of the mixture.
            with self._state_lock:
                self._prepared_mixture_volume_state = "unknown"

        def after_flow() -> None:
            with self._state_lock:
                self._prepared_mixture_volume_state = "flow_completed"
            session.finish()

        def flow_failed(_: BaseException) -> None:
            with self._state_lock:
                self._prepared_mixture_volume_state = "unknown"
            session.abort_and_wait()

        hooks = FlowPhaseHooks(
            before_flow=before_flow,
            after_flow=after_flow,
            flow_failed=flow_failed,
            before_fluid_handling=before_fluid_handling,
        )

        try:
            self.runze_controller.fill_flow_cell_from_mixing_vessel(
                volume_ml=measurement_flow_volume_ml,
                rpm=plan.handling_rpm,
                flow_rpm=flow_rpm,
                approach_rpm=plan.approach_rpm,
                flow_hooks=hooks,
            )
            if session.measurement is None:
                raise RuntimeError("EmStat measurement returned no result")

            absolute_session_file_path = self.emstat_controller.last_saved_session_path
            if absolute_session_file_path is None:
                raise RuntimeError(
                    "EmStat measurement completed but its PalmSens session was "
                    "not saved"
                )
            session_file_path = (
                Path(f"{prepared_mixture.mixture_id:06d}_experiment")
                / absolute_session_file_path.name
            )

            emstat_snapshot = self.emstat_controller.snapshot
            self.repository.set_measurement_status(
                measurement_id,
                "completed",
                session_file_path=session_file_path,
                point_count=emstat_snapshot.point_count,
                elapsed_s=session.elapsed_s,
                flow_started_at=flow_started_at,
            )
            try:
                self._calculate_and_save_objective(
                    measurement_id=measurement_id,
                    measurement_type=plan.measurement_type,
                    measurement=session.measurement,
                    measurement_settings=plan.measurement_settings,
                )
            except Exception:
                # A derived metric must never reclassify a physically successful
                # measurement. The raw session remains available for reanalysis.
                logger.exception(
                    "Automatic objective analysis failed for measurement %s",
                    measurement_id,
                )
        except (
            MeasurementAbortedError,
            OperationStoppedError,
            SequenceStoppedError,
        ) as error:
            self.repository.set_measurement_status(
                measurement_id,
                "aborted",
                point_count=self.emstat_controller.snapshot.point_count,
                elapsed_s=session.elapsed_s,
                flow_started_at=flow_started_at,
                error_message=str(error),
            )
            raise
        except Exception as error:
            self.repository.set_measurement_status(
                measurement_id,
                "failed",
                point_count=self.emstat_controller.snapshot.point_count,
                elapsed_s=session.elapsed_s,
                flow_started_at=flow_started_at,
                error_message=str(error),
            )
            raise
        finally:
            session.close()

        return ExperimentRunResult(
            mixture_id=prepared_mixture.mixture_id,
            measurements=(
                ExperimentMeasurementResult(
                    measurement_id=measurement_id,
                    emstat_measurement_id=session.measurement_id,
                    session_file_path=absolute_session_file_path,
                ),
            ),
            plan=plan,
        )

    def _run_stopped_flow_measurements(
        self, *, prepared_mixture: PreparedMixture
    ) -> ExperimentRunResult:
        with self._state_lock:
            if self._prepared_mixture != prepared_mixture:
                raise RuntimeError("The selected prepared mixture is not active")
            if self._prepared_mixture_volume_state != "prepared":
                raise RuntimeError(
                    "This prepared mixture has already been used for a measurement run"
                )
            self._active_plan = prepared_mixture.plan

        plan = prepared_mixture.plan
        displacement_ml = plan.between_measurement_displacement_volume_ml
        if plan.measurement_count > 1 and displacement_ml is None:
            raise RuntimeError(
                "Stopped-flow plan has no between-measurement displacement"
            )
        self._ensure_emstat_connected()
        results: list[ExperimentMeasurementResult] = []

        def before_fluid_handling() -> None:
            with self._state_lock:
                self._prepared_mixture_volume_state = "unknown"

        def measure_at_stop(sequence_number: int) -> None:
            self._set_measurement_sequence_number(sequence_number)
            measurement_context = self._plan_volume_context(plan)
            measurement_context["measurement_sequence_number"] = sequence_number
            measurement_id = self.repository.create_measurement(
                mixture_id=prepared_mixture.mixture_id,
                measurement_type=plan.measurement_type,
                origin="experiment",
                measurement_name=plan.measurement_name,
                settings=cast(Mapping[str, object], asdict(plan.measurement_settings)),
                protocol_key="stopped_flow_stationary",
                context=measurement_context,
                run_sequence_number=sequence_number,
            )
            with self._state_lock:
                self._measurement_id = measurement_id

            started_at = time.monotonic()
            self.repository.set_measurement_status(measurement_id, "running")
            try:
                measurement = self._measure(
                    plan,
                    on_command_started=lambda _: None,
                    session_directory=f"{prepared_mixture.mixture_id:06d}_experiment",
                    session_sequence_number=sequence_number,
                )
                absolute_path = self.emstat_controller.last_saved_session_path
                if absolute_path is None:
                    raise RuntimeError(
                        "EmStat measurement completed but its PalmSens session was "
                        "not saved"
                    )
                relative_path = (
                    Path(f"{prepared_mixture.mixture_id:06d}_experiment")
                    / absolute_path.name
                )
                snapshot = self.emstat_controller.snapshot
                elapsed_s = max(0.0, time.monotonic() - started_at)
                self.repository.set_measurement_status(
                    measurement_id,
                    "completed",
                    session_file_path=relative_path,
                    point_count=snapshot.point_count,
                    elapsed_s=elapsed_s,
                )
                results.append(
                    ExperimentMeasurementResult(
                        measurement_id=measurement_id,
                        emstat_measurement_id=snapshot.measurement_id,
                        session_file_path=absolute_path,
                    )
                )
                try:
                    self._calculate_and_save_objective(
                        measurement_id=measurement_id,
                        measurement_type=plan.measurement_type,
                        measurement=measurement,
                        measurement_settings=plan.measurement_settings,
                    )
                except Exception:
                    logger.exception(
                        "Automatic objective analysis failed for measurement %s",
                        measurement_id,
                    )
            except (MeasurementAbortedError, OperationStoppedError) as error:
                self.repository.set_measurement_status(
                    measurement_id,
                    "aborted",
                    point_count=self.emstat_controller.snapshot.point_count,
                    elapsed_s=max(0.0, time.monotonic() - started_at),
                    error_message=str(error),
                )
                raise
            except Exception as error:
                self.repository.set_measurement_status(
                    measurement_id,
                    "failed",
                    point_count=self.emstat_controller.snapshot.point_count,
                    elapsed_s=max(0.0, time.monotonic() - started_at),
                    error_message=str(error),
                )
                raise

        hooks = StoppedFlowHooks(
            measure_at_stop=measure_at_stop, before_fluid_handling=before_fluid_handling
        )
        self.runze_controller.run_stopped_flow_series_from_mixing_vessel(
            measurement_count=plan.measurement_count,
            between_measurement_displacement_volume_ml=displacement_ml,
            rinse_through_volume_ml=(
                plan.cleanup_settings.flow_cell_rinse_through_volume_ml
            ),
            rpm=plan.handling_rpm,
            approach_rpm=plan.approach_rpm,
            hooks=hooks,
        )
        with self._state_lock:
            self._prepared_mixture_volume_state = "flow_completed"
        if len(results) != plan.measurement_count:
            raise RuntimeError(
                "Stopped-flow series completed without all planned measurements"
            )
        return ExperimentRunResult(
            mixture_id=prepared_mixture.mixture_id,
            measurements=tuple(results),
            plan=plan,
        )

    def _calculate_and_save_objective(
        self,
        *,
        measurement_id: int,
        measurement_type: MeasurementType,
        measurement: Measurement,
        measurement_settings: EmStatMeasurementSettings | None = None,
        objective_name: ObjectiveName = DEFAULT_OBJECTIVE,
    ) -> ObjectiveResult:
        expected_lsv_sweep_count = (
            measurement_settings.sweep_count
            if isinstance(measurement_settings, LinearSweepVoltammetrySettings)
            else None
        )
        result = calculate_objective(
            measurement,
            measurement_type=measurement_type,
            objective_name=objective_name,
            expected_lsv_sweep_count=expected_lsv_sweep_count,
        )
        self.repository.save_measurement_metric(
            measurement_id=measurement_id,
            metric_name=result.definition.name,
            value=result.value,
            unit=result.definition.unit,
            standard_error=result.standard_error,
            analysis_method=result.definition.analysis_method,
            analysis_version=result.definition.analysis_version,
            contributions=tuple(
                MetricContribution(
                    sequence_number=item.sequence_number,
                    kind=item.kind,
                    value=item.value,
                )
                for item in result.contributions
            ),
        )
        return result

    def _clean_after_experiment(self, *, prepared_mixture: PreparedMixture) -> None:
        with self._state_lock:
            if self._prepared_mixture != prepared_mixture:
                raise RuntimeError("The selected prepared mixture is not active")
            volume_state = self._prepared_mixture_volume_state

        if volume_state == "unknown":
            raise RuntimeError(
                "The remaining mixing-vessel volume is unknown after an incomplete "
                "flow run; recover and drain it manually"
            )
        if volume_state == "prepared":
            expected_vessel_volume_ml = prepared_mixture.plan.total_mixture_volume_ml
        elif volume_state == "flow_completed":
            expected_vessel_volume_ml = (
                prepared_mixture.plan.mixing_vessel_reserve_volume_ml
            )
        else:
            raise RuntimeError("Prepared-mixture volume state is unavailable")

        # A partial drain cannot be retried safely using the previous estimate.
        with self._state_lock:
            self._prepared_mixture_volume_state = "unknown"

        plan = prepared_mixture.plan
        settings = plan.cleanup_settings
        self.runze_controller.drain_mixing_vessel_to_outlet(
            expected_vessel_volume_ml=expected_vessel_volume_ml, rpm=plan.handling_rpm
        )
        self.runze_controller.rinse_mixing_vessel(
            rinse_volume_ml=settings.mixing_rinse_volume_ml,
            rpm=plan.handling_rpm,
            cycles=settings.mixing_rinse_cycles,
        )
        if plan.workflow_kind == "continuous_flow":
            self._clean_continuous_flow_cell(plan)
        else:
            self._clean_stopped_flow_cell(plan)

        with self._state_lock:
            self._prepared_mixture = None
            self._prepared_mixture_volume_state = None

    def _clean_continuous_flow_cell(self, plan: ExperimentPlan) -> None:
        self.runze_controller.rinse_flow_cell_with_water(
            rinse_through_volume_ml=(
                plan.cleanup_settings.flow_cell_rinse_through_volume_ml
            ),
            handling_rpm=plan.handling_rpm,
            rinsing_rpm=plan.approach_rpm,
        )

    @staticmethod
    def _clean_stopped_flow_cell(plan: ExperimentPlan) -> None:
        """Skip flow-cell rinsing because stopped-flow Run already completed it."""

    def _measure(
        self,
        plan: ExperimentPlan,
        *,
        on_command_started: Callable[[int], None],
        session_directory: str | Path | None = None,
        session_sequence_number: int | None = None,
    ) -> Measurement:
        if plan.measurement_type == "chronoamperometry":
            return self.emstat_controller.measure_chronoamperometry(
                settings=cast(ChronoamperometrySettings, plan.measurement_settings),
                measurement_name=plan.measurement_name,
                on_command_started=on_command_started,
                session_directory=session_directory,
                session_sequence_number=session_sequence_number,
            )
        if plan.measurement_type == "cyclic_voltammetry":
            return self.emstat_controller.measure_cyclic_voltammetry(
                settings=cast(CyclicVoltammetrySettings, plan.measurement_settings),
                measurement_name=plan.measurement_name,
                on_command_started=on_command_started,
                session_directory=session_directory,
                session_sequence_number=session_sequence_number,
            )
        if plan.measurement_type == "linear_sweep_voltammetry":
            return self.emstat_controller.measure_linear_sweep_voltammetry(
                settings=cast(
                    LinearSweepVoltammetrySettings, plan.measurement_settings
                ),
                measurement_name=plan.measurement_name,
                on_command_started=on_command_started,
                session_directory=session_directory,
                session_sequence_number=session_sequence_number,
            )
        raise ValueError(f"Unsupported measurement type: {plan.measurement_type}")

    # Validation and state helpers ---

    def _validate_configuration_ports(
        self, entries: Sequence[SourceConfigurationEntry]
    ) -> None:
        for index, entry in enumerate(entries):
            if not isinstance(entry, SourceConfigurationEntry):
                raise TypeError(f"entries[{index}] must be a SourceConfigurationEntry")

        configured_by_port = {
            source.number: source for source in self._plumbing.liquid_sources
        }
        supplied_by_port = {entry.selector_port: entry for entry in entries}
        if len(supplied_by_port) != len(entries):
            raise ValueError("Source configuration contains duplicate selector ports")
        if set(supplied_by_port) != set(configured_by_port):
            missing = set(configured_by_port) - set(supplied_by_port)
            unexpected = set(supplied_by_port) - set(configured_by_port)
            details: list[str] = []
            if missing:
                details.append(
                    "missing ports "
                    + ", ".join(str(value) for value in sorted(missing))
                )
            if unexpected:
                details.append(
                    "unexpected ports "
                    + ", ".join(str(value) for value in sorted(unexpected))
                )
            raise ValueError(
                "Source configuration must describe every configured liquid port: "
                + "; ".join(details)
            )

        for port, source in configured_by_port.items():
            entry = supplied_by_port[port]
            if entry.source_config_key != source.key:
                raise ValueError(
                    f"Selector port {port} must use source key {source.key!r}; "
                    f"got {entry.source_config_key!r}"
                )

    def _validate_source_configuration(
        self, configuration: CurrentSourceConfiguration
    ) -> None:
        self._validate_configuration_ports(configuration.entries)
        enabled_entries = configuration.enabled_entries
        if not enabled_entries:
            raise ValueError("At least one liquid source must be enabled")

        water_entry = next(
            (
                entry
                for entry in configuration.entries
                if entry.selector_port == self._plumbing.water.number
            ),
            None,
        )
        if water_entry is None or not water_entry.enabled:
            raise ValueError("The configured water source must remain enabled")
        if water_entry.source_stock_id is not None:
            raise ValueError(
                "The configured water source uses the built-in water identity and "
                "must not have a source-stock assignment"
            )

        non_water_entries = tuple(
            entry
            for entry in enabled_entries
            if entry.source_config_key != WATER_SOURCE_CONFIG_KEY
        )
        active_stock_ids = [entry.source_stock_id for entry in non_water_entries]
        if any(source_stock_id is None for source_stock_id in active_stock_ids):
            raise ValueError(
                "Every enabled non-water liquid source requires a stock assignment"
            )
        if len(set(active_stock_ids)) != len(active_stock_ids):
            raise ValueError("Enabled liquid sources must use distinct source stocks")

    def _validate_source_configuration_transition(
        self,
        *,
        old_configuration: CurrentSourceConfiguration,
        new_entries: Sequence[SourceConfigurationEntry],
    ) -> None:
        old_by_port = {
            entry.selector_port: entry for entry in old_configuration.entries
        }
        with self._state_lock:
            physical_by_port = {
                state.selector_port: state for state in self._priming_snapshot_locked()
            }

        unknown_enabled: list[str] = []
        for entry in new_entries:
            physical_state = physical_by_port[entry.selector_port]
            old_entry = old_by_port.get(entry.selector_port)
            stock_changed = (
                old_entry is not None
                and old_entry.source_stock_id != entry.source_stock_id
            )
            if stock_changed and physical_state.status != "air_filled":
                raise RuntimeError(
                    f"{physical_state.source_label} can use a different stock only "
                    "while its line is confirmed air-filled"
                )

            primed_for_selected_stock = (
                physical_state.status == "primed"
                and physical_state.source_stock_id == entry.source_stock_id
            )
            if entry.enabled and not (
                physical_state.status == "air_filled" or primed_for_selected_stock
            ):
                unknown_enabled.append(physical_state.source_label)

        if unknown_enabled:
            raise RuntimeError(
                "Every enabled source line must be confirmed Primed or Air-filled "
                "before saving: " + ", ".join(unknown_enabled)
            )

    def _require_plan_sources_primed(self, plan: ExperimentPlan) -> None:
        with self._state_lock:
            missing = [
                source.source_config_key
                for source in plan.sources
                if not self._source_is_primed_locked(
                    selector_port=source.selector_port,
                    source_stock_id=source.source_stock_id,
                )
            ]
        if missing:
            raise RuntimeError(
                "The following enabled source lines are not primed for their current "
                "stocks: " + ", ".join(missing)
            )

    def _require_plan_current(self, plan: ExperimentPlan) -> None:
        with self._state_lock:
            current_revision = self._source_configuration.revision
        if plan.source_configuration_revision != current_revision:
            raise RuntimeError(
                "The source configuration changed after this experiment was planned; "
                "build a new plan"
            )

    def _require_no_prepared_mixture(self, action: str) -> None:
        with self._state_lock:
            if self._prepared_mixture is not None:
                raise RuntimeError(
                    f"Cannot {action} while a prepared mixture is active; "
                    "run it, clean it, or explicitly clear its state first"
                )

    def _require_no_pending_line_maintenance(self, action: str) -> None:
        with self._state_lock:
            phase = self._line_maintenance_phase
        if phase in {"sources_recovered", "sources_rinsed"}:
            raise RuntimeError(
                f"Cannot {action}; finish the ordered line-maintenance stages first"
            )
        if phase == "recovery_required":
            raise RuntimeError(
                f"Cannot {action}; line-maintenance recovery is required"
            )

    def _require_line_maintenance_phase(
        self, expected_phase: LineMaintenancePhase
    ) -> _PendingLineMaintenance:
        self._require_no_prepared_mixture("continue line maintenance")
        with self._state_lock:
            phase = self._line_maintenance_phase
            pending = self._pending_line_maintenance
        if phase != expected_phase or pending is None:
            raise RuntimeError(
                "Line-maintenance stages must be completed in order; expected "
                f"{expected_phase.replace('_', ' ')}"
            )
        return pending

    def _entry_is_primed_locked(self, entry: SourceConfigurationEntry) -> bool:
        return self._source_is_primed_locked(
            selector_port=entry.selector_port, source_stock_id=entry.source_stock_id
        )

    def _source_is_primed_locked(
        self, *, selector_port: int, source_stock_id: int | None
    ) -> bool:
        state = self._line_state_by_port.get(selector_port)
        return (
            state is not None
            and state.status == "primed"
            and state.source_stock_id == source_stock_id
        )

    def _line_status_locked(self, selector_port: int) -> SourcePrimingStatus:
        state = self._line_state_by_port.get(selector_port)
        return "unknown" if state is None else state.status

    def _manual_play_next_stage_locked(self) -> ManualPlayStage | None:
        sequence = self._manual_play_sequence
        if sequence is None or sequence.active_index >= len(sequence.plans):
            return None
        if self._line_maintenance_phase in {
            "sources_recovered",
            "sources_rinsed",
            "recovery_required",
        }:
            return None

        plan = sequence.plans[sequence.active_index]
        prepared = self._prepared_mixture
        if prepared is not None:
            if prepared.plan != plan:
                return None
            if self._prepared_mixture_volume_state == "prepared":
                return "measure"
            if self._prepared_mixture_volume_state == "flow_completed":
                return "clean"
            return None

        sources_primed = all(
            self._source_is_primed_locked(
                selector_port=source.selector_port,
                source_stock_id=source.source_stock_id,
            )
            for source in plan.sources
        )
        return "prepare" if sources_primed else "prime"

    def _active_plan_payload_locked(self) -> dict[str, object] | None:
        sequence = self._manual_play_sequence
        if sequence is not None:
            return _manual_play_sequence_to_payload(
                sequence, next_stage=self._manual_play_next_stage_locked()
            )
        if self._active_plan is None:
            return None
        return _manual_play_sequence_to_payload(
            _ManualPlaySequence(plans=(self._active_plan,)), next_stage=None
        )

    def _priming_snapshot_locked(self) -> tuple[SourcePrimingState, ...]:
        assignment_by_port = {
            entry.selector_port: entry for entry in self._source_configuration.entries
        }
        states: list[SourcePrimingState] = []
        for source in self._plumbing.liquid_sources:
            entry = assignment_by_port.get(source.number)
            source_stock_id = None if entry is None else entry.source_stock_id
            line_state = self._line_state_by_port.get(source.number)
            identity_matches = line_state is not None and (
                line_state.status != "primed"
                or line_state.source_stock_id == source_stock_id
            )
            status: SourcePrimingStatus = (
                line_state.status
                if line_state is not None and identity_matches
                else "unknown"
            )
            states.append(
                SourcePrimingState(
                    selector_port=source.number,
                    source_config_key=source.key,
                    source_label=source.label,
                    source_stock_id=source_stock_id,
                    status=status,
                    primed_at=(
                        line_state.updated_at
                        if line_state is not None and status == "primed"
                        else None
                    ),
                    updated_at=(None if line_state is None else line_state.updated_at),
                    reason=(
                        "No saved physical state"
                        if line_state is None
                        else line_state.reason
                    ),
                )
            )
        return tuple(states)

    def _set_line_state_locked(
        self,
        selector_port: int,
        status: SourcePrimingStatus,
        *,
        reason: str,
        updated_at: datetime | None = None,
        source_stock_id: int | None = None,
    ) -> None:
        self._line_state_by_port[selector_port] = _SourceLineIdentity(
            source_stock_id=(source_stock_id if status == "primed" else None),
            status=status,
            updated_at=datetime.now(UTC) if updated_at is None else updated_at,
            reason=reason,
        )

    def _source_line_state_editable_locked(
        self, *, selector_port: int, proposed_enabled: bool | None
    ) -> bool:
        if self._state in _BUSY_STATES or self._prepared_mixture is not None:
            return False
        if self._workflow_phase in {
            "prepared",
            "flow_completed",
            "sources_recovered",
            "sources_rinsed",
        }:
            return False
        if self._workflow_phase != "ready":
            return True

        entry = next(
            (
                entry
                for entry in self._source_configuration.entries
                if entry.selector_port == selector_port
            ),
            None,
        )
        current_state = self._line_state_by_port.get(selector_port)
        return (
            entry is not None
            and not entry.enabled
            and proposed_enabled is True
            and (current_state is None or current_state.status == "unknown")
        )

    def _mark_operation_started(
        self, operation: str, *, plan: ExperimentPlan | None = None
    ) -> None:
        with self._state_lock:
            self._mark_operation_started_locked(operation, plan=plan)

    def _mark_operation_started_locked(
        self, operation: str, *, plan: ExperimentPlan | None = None
    ) -> None:
        if plan is not None:
            self._active_plan = plan
        self._save_checkpoint_locked(
            last_operation=operation, operation_in_progress=True
        )
        self._physical_operation_checkpointed = True

    def _save_checkpoint_locked(
        self, *, last_operation: str, operation_in_progress: bool = False
    ) -> None:
        updated_at = datetime.now(UTC)
        self._checkpoint_updated_at = updated_at
        if not self._persistence_available:
            return
        self.repository.save_experiment_workspace_state(
            PersistedExperimentWorkspaceState(
                workflow_phase=self._workflow_phase,
                operation_in_progress=operation_in_progress,
                active_plan=(self._active_plan_payload_locked()),
                mixture_id=self._mixture_id,
                prepared_volume_state=self._prepared_mixture_volume_state,
                maintenance_source_ports=(
                    ()
                    if self._pending_line_maintenance is None
                    else self._pending_line_maintenance.source_ports
                ),
                last_operation=last_operation,
                updated_at=updated_at.isoformat(),
                source_lines=tuple(
                    PersistedSourceLineState(
                        selector_port=source.number,
                        source_config_key=source.key,
                        source_stock_id=(
                            None
                            if (state := self._line_state_by_port.get(source.number))
                            is None
                            else state.source_stock_id
                        ),
                        status="unknown" if state is None else state.status,
                        updated_at=(
                            updated_at.isoformat()
                            if state is None
                            else state.updated_at.isoformat()
                        ),
                        reason=(
                            "No saved physical state" if state is None else state.reason
                        ),
                    )
                    for source in self._plumbing.liquid_sources
                ),
            )
        )

    def _restore_workspace_checkpoint(self) -> None:
        if not self._persistence_available:
            return
        try:
            persisted = self.repository.load_experiment_workspace_state()
            if persisted.active_plan is None:
                plan = None
                sequence = None
            else:
                plan, sequence = _workspace_execution_from_payload(
                    persisted.active_plan
                )
            if sequence is not None and any(
                item.source_configuration_revision
                != self._source_configuration.revision
                for item in sequence.plans
            ):
                raise ValueError(
                    "Saved experiment execution uses a different source configuration"
                )

            self._active_plan = plan
            self._manual_play_sequence = sequence
            self._mixture_id = persisted.mixture_id
            self._prepared_mixture_volume_state = cast(
                PreparedMixtureVolumeState | None, persisted.prepared_volume_state
            )
            self._workflow_phase = persisted.workflow_phase
            self._checkpoint_updated_at = _parse_utc_datetime(persisted.updated_at)
            configured_by_port = {
                source.number: source for source in self._plumbing.liquid_sources
            }
            for line in persisted.source_lines:
                source = configured_by_port.get(line.selector_port)
                if source is None or source.key != line.source_config_key:
                    raise ValueError("Saved source-line layout does not match plumbing")
                self._line_state_by_port[line.selector_port] = _SourceLineIdentity(
                    source_stock_id=line.source_stock_id,
                    status=line.status,
                    updated_at=_parse_utc_datetime(line.updated_at),
                    reason=line.reason,
                )

            if persisted.operation_in_progress:
                if sequence is not None:
                    sequence.state = "inactive"
                self._workflow_phase = "recovery_required"
                self._prepared_mixture_volume_state = "unknown"
                self._line_maintenance_phase = "recovery_required"
                for source in self._plumbing.liquid_sources:
                    self._set_line_state_locked(
                        source.number,
                        "unknown",
                        reason="Application stopped during a physical operation",
                    )
                self._save_checkpoint_locked(
                    last_operation="interrupted operation requires recovery"
                )
                return

            restore_play_as_paused = False
            if sequence is not None and sequence.state in {
                "running",
                "pause_requested",
            }:
                sequence.state = "paused"
                restore_play_as_paused = True

            if (
                plan is not None
                and persisted.mixture_id is not None
                and persisted.prepared_volume_state is not None
            ):
                self._prepared_mixture = PreparedMixture(
                    mixture_id=persisted.mixture_id, plan=plan
                )
            if persisted.workflow_phase in {"sources_recovered", "sources_rinsed"}:
                self._line_maintenance_phase = cast(
                    LineMaintenancePhase, persisted.workflow_phase
                )
                if plan is None:
                    raise ValueError("Saved maintenance checkpoint has no plan")
                self._pending_line_maintenance = _PendingLineMaintenance(
                    source_ports=persisted.maintenance_source_ports,
                    handling_rpm=plan.handling_rpm,
                    additional_rinse_volume_ml=(
                        plan.cleanup_settings.source_line_additional_rinse_volume_ml
                    ),
                )
            elif persisted.workflow_phase == "recovery_required":
                self._line_maintenance_phase = "recovery_required"
            elif persisted.workflow_phase == "complete":
                self._line_maintenance_phase = "complete"
            if restore_play_as_paused:
                self._save_checkpoint_locked(
                    last_operation="manual Play restored at stage boundary"
                )
        except Exception as error:
            logger.exception("Could not restore experiment workspace checkpoint")
            self._active_plan = None
            self._manual_play_sequence = None
            self._prepared_mixture = None
            self._prepared_mixture_volume_state = "unknown"
            self._workflow_phase = "recovery_required"
            self._line_maintenance_phase = "recovery_required"
            self._error_message = f"Saved workspace state could not be trusted: {error}"
            for source in self._plumbing.liquid_sources:
                self._set_line_state_locked(
                    source.number,
                    "unknown",
                    reason="Saved workspace state could not be trusted",
                )
            self._save_checkpoint_locked(last_operation="checkpoint restore failed")

    def _handle_runze_physical_operation(self, operation: str) -> None:
        """Invalidate resumability before a Runze movement outside this controller."""

        with self._state_lock:
            if self._physical_operation_checkpointed:
                return
            for source in self._plumbing.liquid_sources:
                self._set_line_state_locked(
                    source.number,
                    "unknown",
                    reason=f"External Runze operation started: {operation}",
                )
            self._prepared_mixture_volume_state = (
                "unknown" if self._prepared_mixture is not None else None
            )
            if self._manual_play_sequence is not None:
                self._manual_play_sequence.state = "inactive"
            self._workflow_phase = "recovery_required"
            self._line_maintenance_phase = "recovery_required"
            self._save_checkpoint_locked(
                last_operation=f"external Runze operation: {operation}"
            )

    def _ensure_emstat_connected(self) -> None:
        if not self.emstat_controller.connected:
            self.emstat_controller.connect()
        if not self.emstat_controller.connected:
            raise RuntimeError("Persistent EmStat connection could not be established")

    @staticmethod
    def _require_destination(
        destination: SelectorPortConfig | None, label: str
    ) -> SelectorPortConfig:
        if destination is None:
            raise RuntimeError(f"No {label} destination is configured")
        return destination

    @staticmethod
    def _require_route_volume(destination: SelectorPortConfig) -> float:
        route_volume_ml = destination.destination_route_dead_volume_ml
        if route_volume_ml is None:
            raise RuntimeError(
                f"Destination route volume is not configured for {destination.label}"
            )
        return route_volume_ml

    @staticmethod
    def _plan_volume_context(plan: ExperimentPlan) -> dict[str, object]:
        return {
            "workflow_kind": plan.workflow_kind,
            "measurement_count": plan.measurement_count,
            "between_measurement_displacement_volume_ml": (
                plan.between_measurement_displacement_volume_ml
            ),
            "source_configuration_revision": plan.source_configuration_revision,
            "estimated_measurement_duration_s": (plan.estimated_measurement_duration_s),
            "mixing_path_condition_volume_ml": (plan.mixing_path_condition_volume_ml),
            "flow_cell_load_volume_ml": plan.flow_cell_load_volume_ml,
            "measurement_flow_volume_ml": plan.measurement_flow_volume_ml,
            "mixing_vessel_reserve_volume_ml": (plan.mixing_vessel_reserve_volume_ml),
            "total_mixture_volume_ml": plan.total_mixture_volume_ml,
            "handling_rpm": plan.handling_rpm,
            "inlet_aspiration_rpm": plan.inlet_aspiration_rpm,
            "approach_rpm": plan.approach_rpm,
            "flow_rpm": plan.flow_rpm,
            "mixing_rinse_volume_ml": plan.cleanup_settings.mixing_rinse_volume_ml,
            "mixing_rinse_cycles": plan.cleanup_settings.mixing_rinse_cycles,
            "flow_cell_rinse_through_volume_ml": (
                plan.cleanup_settings.flow_cell_rinse_through_volume_ml
            ),
            "source_line_additional_rinse_volume_ml": (
                plan.cleanup_settings.source_line_additional_rinse_volume_ml
            ),
            "volume_basis": "requested recipe; nominal commands are step-rounded, not measured",
            "commanded_total_volume_ml": math.fsum(
                t.nominal_volume_ml for t in plan.mixture_transfers
            ),
            "commanded_components": [
                {
                    "source_config_key": key,
                    "nominal_volume_ml": math.fsum(
                        t.nominal_volume_ml
                        for t in plan.mixture_transfers
                        if t.source_config_key == key
                    ),
                    "fraction": math.fsum(
                        t.nominal_volume_ml
                        for t in plan.mixture_transfers
                        if t.source_config_key == key
                    )
                    / math.fsum(t.nominal_volume_ml for t in plan.mixture_transfers),
                }
                for key in dict.fromkeys(
                    t.source_config_key for t in plan.mixture_transfers
                )
            ],
            "mixture_transfers": [
                asdict(transfer) for transfer in plan.mixture_transfers
            ],
        }

    def _acquire_operation(
        self, *, state: ExperimentControllerState, operation: str, visible: bool = True
    ) -> None:
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another experiment-controller operation is in progress")

        if self.runze_controller.busy or self.emstat_controller.busy:
            self._operation_lock.release()
            raise RuntimeError(
                "A Runze or EmStat operation outside ExperimentController is active"
            )

        with self._state_lock:
            self._state = state
            self._operation = operation if visible else None
            self._error_message = None

    def _set_active_stage(
        self, *, state: ExperimentControllerState, operation: str
    ) -> None:
        with self._state_lock:
            self._state = state
            self._operation = operation

    def _release_operation(
        self, *, final_state: ExperimentControllerState | None
    ) -> None:
        with self._state_lock:
            if final_state is not None:
                self._state = final_state
            self._operation = None
            self._physical_operation_checkpointed = False
        self._operation_lock.release()

    def _finish_physical_success(self, *, last_operation: str) -> None:
        try:
            with self._state_lock:
                self._save_checkpoint_locked(last_operation=last_operation)
        except BaseException as error:
            self._finish_with_error(error)
            raise
        else:
            self._release_operation(final_state="completed")

    def _finish_with_error(self, error: BaseException) -> None:
        final_state: ExperimentControllerState = (
            "aborted"
            if isinstance(
                error,
                (MeasurementAbortedError, OperationStoppedError, SequenceStoppedError),
            )
            else "failed"
        )
        with self._state_lock:
            self._state = final_state
            self._operation = None
            self._error_message = str(error)
            play_was_active = (
                self._manual_play_sequence is not None
                and self._manual_play_sequence.state != "inactive"
            )
            if self._manual_play_sequence is not None:
                self._manual_play_sequence.state = "inactive"
            if self._physical_operation_checkpointed:
                self._workflow_phase = "recovery_required"
                self._line_maintenance_phase = "recovery_required"
                try:
                    self._save_checkpoint_locked(
                        last_operation=f"physical operation failed: {error}"
                    )
                except Exception:
                    logger.exception("Could not persist failed physical operation")
            elif play_was_active:
                try:
                    self._save_checkpoint_locked(
                        last_operation=f"manual Play stage failed: {error}"
                    )
                except Exception:
                    logger.exception("Could not persist failed manual Play stage")
            self._physical_operation_checkpointed = False
        self._operation_lock.release()


def _experiment_plan_to_payload(plan: ExperimentPlan) -> dict[str, object]:
    return {
        "version": 3,
        "workflow_kind": plan.workflow_kind,
        "measurement_count": plan.measurement_count,
        "between_measurement_displacement_volume_ml": (
            plan.between_measurement_displacement_volume_ml
        ),
        "source_configuration_revision": plan.source_configuration_revision,
        "sources": [asdict(source) for source in plan.sources],
        "measurement_type": plan.measurement_type,
        "measurement_settings": asdict(plan.measurement_settings),
        "measurement_name": plan.measurement_name,
        "handling_rpm": plan.handling_rpm,
        "inlet_aspiration_rpm": plan.inlet_aspiration_rpm,
        "approach_rpm": plan.approach_rpm,
        "flow_rpm": plan.flow_rpm,
        "water_distribution": plan.water_distribution,
        "cleanup_settings": asdict(plan.cleanup_settings),
        "estimated_measurement_duration_s": plan.estimated_measurement_duration_s,
        "flow_rate_ml_min": plan.flow_rate_ml_min,
        "measurement_flow_volume_ml": plan.measurement_flow_volume_ml,
        "mixing_path_condition_volume_ml": plan.mixing_path_condition_volume_ml,
        "flow_cell_load_volume_ml": plan.flow_cell_load_volume_ml,
        "mixing_vessel_reserve_volume_ml": plan.mixing_vessel_reserve_volume_ml,
        "total_mixture_volume_ml": plan.total_mixture_volume_ml,
        "substance_concentrations": [
            asdict(concentration) for concentration in plan.substance_concentrations
        ],
        "mixture_transfers": [asdict(transfer) for transfer in plan.mixture_transfers],
    }


def _manual_plan_protocol_key(plan: ExperimentPlan) -> tuple[object, ...]:
    return (
        plan.measurement_type,
        plan.measurement_settings,
        plan.handling_rpm,
        plan.inlet_aspiration_rpm,
        plan.approach_rpm,
        plan.flow_rpm,
        plan.water_distribution,
        plan.cleanup_settings,
        plan.estimated_measurement_duration_s,
        plan.flow_rate_ml_min,
        plan.measurement_flow_volume_ml,
        plan.mixing_path_condition_volume_ml,
        plan.flow_cell_load_volume_ml,
        plan.mixing_vessel_reserve_volume_ml,
        plan.total_mixture_volume_ml,
        plan.workflow_kind,
        plan.measurement_count,
        plan.between_measurement_displacement_volume_ml,
    )


def _manual_play_sequence_to_payload(
    sequence: _ManualPlaySequence, *, next_stage: ManualPlayStage | None
) -> dict[str, object]:
    return {
        "workspace_execution_version": 1,
        "plans": [_experiment_plan_to_payload(plan) for plan in sequence.plans],
        "active_index": sequence.active_index,
        "play_state": sequence.state,
        "next_stage": next_stage,
    }


def _workspace_execution_from_payload(
    payload: Mapping[str, object],
) -> tuple[ExperimentPlan, _ManualPlaySequence]:
    if payload.get("workspace_execution_version") != 1:
        raise ValueError("Unsupported saved workspace-execution version")
    plans = tuple(
        _experiment_plan_from_payload(item)
        for item in _payload_mapping_list(payload, "plans")
    )
    if not plans:
        raise ValueError("Saved workspace execution must contain at least one plan")
    active_index = require_integer(payload.get("active_index"), name="active_index")
    if active_index < 0 or active_index > len(plans):
        raise ValueError("Saved workspace execution has an invalid active index")
    state_value = payload.get("play_state")
    if state_value not in {"inactive", "running", "pause_requested", "paused"}:
        raise ValueError(f"Unsupported saved Play state: {state_value!r}")
    state = cast(ManualPlayState, state_value)
    if active_index == len(plans) and state != "inactive":
        raise ValueError("A completed workspace execution cannot have active Play")
    next_stage = payload.get("next_stage")
    if next_stage is not None and next_stage not in {
        "prime",
        "prepare",
        "measure",
        "clean",
    }:
        raise ValueError(f"Unsupported saved Play stage: {next_stage!r}")

    active_plan = plans[min(active_index, len(plans) - 1)]
    return active_plan, _ManualPlaySequence(
        plans=plans, active_index=active_index, state=state
    )


def _experiment_protocol_snapshot_to_payload(plan: ExperimentPlan) -> dict[str, object]:
    """Encode the immutable operator protocol separately from mixture composition."""

    return {
        "version": 2,
        "workflow": {
            "kind": plan.workflow_kind,
            "measurement_count": plan.measurement_count,
            "between_measurement_displacement_volume_ml": (
                plan.between_measurement_displacement_volume_ml
            ),
        },
        "measurement_type": plan.measurement_type,
        "measurement_settings": asdict(plan.measurement_settings),
        "measurement_name": plan.measurement_name,
        "fluid_handling": {
            "handling_rpm": plan.handling_rpm,
            "inlet_aspiration_rpm": plan.inlet_aspiration_rpm,
            "approach_rpm": plan.approach_rpm,
            "flow_rpm": plan.flow_rpm,
            "water_distribution": plan.water_distribution,
        },
        "cleanup": asdict(plan.cleanup_settings),
        "historical_estimate": {
            "estimated_measurement_duration_s": plan.estimated_measurement_duration_s,
            "flow_rate_ml_min": plan.flow_rate_ml_min,
            "measurement_flow_volume_ml": plan.measurement_flow_volume_ml,
            "mixing_path_condition_volume_ml": plan.mixing_path_condition_volume_ml,
            "flow_cell_load_volume_ml": plan.flow_cell_load_volume_ml,
            "mixing_vessel_reserve_volume_ml": plan.mixing_vessel_reserve_volume_ml,
            "total_mixture_volume_ml": plan.total_mixture_volume_ml,
        },
    }


def _saved_protocol_from_snapshot(
    payload: Mapping[str, object],
) -> SavedExperimentProtocol:
    version = payload.get("version")
    if version != 2:
        raise ValueError("Unsupported saved experiment-protocol version")
    measurement_type = _payload_measurement_type(payload)
    settings = _measurement_settings_from_payload(
        measurement_type, _payload_mapping(payload, "measurement_settings")
    )
    fluid = _payload_mapping(payload, "fluid_handling")
    cleanup = _payload_mapping(payload, "cleanup")
    estimate = _payload_mapping(payload, "historical_estimate")
    workflow = _payload_mapping(payload, "workflow")
    workflow_kind = workflow.get("kind")
    if workflow_kind not in ("continuous_flow", "stopped_flow"):
        raise ValueError(f"Unsupported saved experiment workflow: {workflow_kind!r}")
    normalized_workflow_kind = cast(ExperimentWorkflowKind, workflow_kind)
    measurement_count = require_integer(
        workflow.get("measurement_count"), name="measurement_count"
    )
    _validate_workflow(normalized_workflow_kind, measurement_count)
    between_measurement_displacement_volume_ml = _optional_mapping_float(
        workflow, "between_measurement_displacement_volume_ml"
    )
    _validate_between_measurement_displacement(
        normalized_workflow_kind,
        measurement_count,
        between_measurement_displacement_volume_ml,
    )
    water_value = _payload_str(fluid, "water_distribution")
    if water_value not in {"single", "between_components"}:
        raise ValueError(f"Unsupported water distribution: {water_value!r}")
    water_distribution = cast(WaterDistribution, water_value)
    flow_rpm = _optional_mapping_int(fluid, "flow_rpm")
    _validate_workflow_flow_rpm(normalized_workflow_kind, flow_rpm)
    return SavedExperimentProtocol(
        workflow_kind=normalized_workflow_kind,
        measurement_type=measurement_type,
        measurement_settings=settings,
        measurement_name=_payload_optional_str(payload, "measurement_name"),
        handling_rpm=_payload_int(fluid, "handling_rpm"),
        inlet_aspiration_rpm=_nullable_mapping_int(fluid, "inlet_aspiration_rpm"),
        approach_rpm=_payload_int(fluid, "approach_rpm"),
        flow_rpm=flow_rpm,
        water_distribution=water_distribution,
        mixing_rinse_volume_ml=_payload_float(cleanup, "mixing_rinse_volume_ml"),
        mixing_rinse_cycles=_payload_int(cleanup, "mixing_rinse_cycles"),
        flow_cell_rinse_through_volume_ml=_payload_float(
            cleanup, "flow_cell_rinse_through_volume_ml"
        ),
        source_line_additional_rinse_volume_ml=_payload_float(
            cleanup, "source_line_additional_rinse_volume_ml"
        ),
        historical_estimate=estimate,
        measurement_count=measurement_count,
        between_measurement_displacement_volume_ml=(
            between_measurement_displacement_volume_ml
        ),
    )


def _optional_mapping_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    return require_integer(value, name=key)


def _nullable_mapping_int(payload: Mapping[str, object], key: str) -> int | None:
    if key not in payload:
        raise ValueError(f"Saved payload is missing {key!r}")
    return _optional_mapping_int(payload, key)


def _optional_mapping_float(payload: Mapping[str, object], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    return float(require_finite_number(value, name=key))


def _experiment_plan_from_payload(payload: Mapping[str, object]) -> ExperimentPlan:
    version = payload.get("version")
    if version != 3:
        raise ValueError("Unsupported saved experiment-plan version")
    workflow_value = _payload_str(payload, "workflow_kind")
    if workflow_value not in ("continuous_flow", "stopped_flow"):
        raise ValueError(f"Unsupported saved workflow: {workflow_value!r}")
    workflow_kind = cast(ExperimentWorkflowKind, workflow_value)
    measurement_count = _payload_int(payload, "measurement_count")
    _validate_workflow(workflow_kind, measurement_count)
    measurement_type = _payload_measurement_type(payload)
    measurement_settings = _measurement_settings_from_payload(
        measurement_type, _payload_mapping(payload, "measurement_settings")
    )
    water_distribution_value = _payload_str(payload, "water_distribution")
    if water_distribution_value not in {"single", "between_components"}:
        raise ValueError(
            f"Unsupported saved water distribution: {water_distribution_value!r}"
        )
    water_distribution = cast(WaterDistribution, water_distribution_value)
    return ExperimentPlan(
        source_configuration_revision=_payload_int(
            payload, "source_configuration_revision"
        ),
        sources=tuple(
            _plan_source_from_payload(source)
            for source in _payload_mapping_list(payload, "sources")
        ),
        measurement_type=measurement_type,
        measurement_settings=measurement_settings,
        measurement_name=_payload_optional_str(payload, "measurement_name"),
        handling_rpm=_payload_int(payload, "handling_rpm"),
        inlet_aspiration_rpm=_nullable_mapping_int(payload, "inlet_aspiration_rpm"),
        approach_rpm=_payload_int(payload, "approach_rpm"),
        flow_rpm=_optional_mapping_int(payload, "flow_rpm"),
        water_distribution=water_distribution,
        cleanup_settings=_cleanup_settings_from_payload(
            _payload_mapping(payload, "cleanup_settings")
        ),
        estimated_measurement_duration_s=_payload_float(
            payload, "estimated_measurement_duration_s"
        ),
        flow_rate_ml_min=_optional_mapping_float(payload, "flow_rate_ml_min"),
        measurement_flow_volume_ml=_optional_mapping_float(
            payload, "measurement_flow_volume_ml"
        ),
        mixing_path_condition_volume_ml=_payload_float(
            payload, "mixing_path_condition_volume_ml"
        ),
        flow_cell_load_volume_ml=_payload_float(payload, "flow_cell_load_volume_ml"),
        mixing_vessel_reserve_volume_ml=_payload_float(
            payload, "mixing_vessel_reserve_volume_ml"
        ),
        total_mixture_volume_ml=_payload_float(payload, "total_mixture_volume_ml"),
        substance_concentrations=tuple(
            _plan_concentration_from_payload(concentration)
            for concentration in _payload_mapping_list(
                payload, "substance_concentrations"
            )
        ),
        mixture_transfers=tuple(
            _mixture_transfer_from_payload(transfer)
            for transfer in _payload_mapping_list(payload, "mixture_transfers")
        ),
        workflow_kind=workflow_kind,
        measurement_count=measurement_count,
        between_measurement_displacement_volume_ml=_optional_mapping_float(
            payload, "between_measurement_displacement_volume_ml"
        ),
    )


def _payload_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"Saved experiment plan field {key!r} must be an object")
    return value


def _payload_mapping_list(
    payload: Mapping[str, object], key: str
) -> tuple[Mapping[str, object], ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise TypeError(f"Saved experiment plan field {key!r} must be a list")
    mappings: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TypeError(
                f"Saved experiment plan field {key}[{index}] must be an object"
            )
        mappings.append(item)
    return tuple(mappings)


def _payload_measurement_type(payload: Mapping[str, object]) -> EmStatMeasurementType:
    value = _payload_str(payload, "measurement_type")
    if value == "chronoamperometry":
        return "chronoamperometry"
    if value == "cyclic_voltammetry":
        return "cyclic_voltammetry"
    if value == "linear_sweep_voltammetry":
        return "linear_sweep_voltammetry"
    raise ValueError(f"Unsupported saved measurement type: {value!r}")


def _measurement_settings_from_payload(
    measurement_type: EmStatMeasurementType, payload: Mapping[str, object]
) -> EmStatMeasurementSettings:
    if measurement_type == "chronoamperometry":
        return ChronoamperometrySettings(
            interval_time_s=_payload_float(payload, "interval_time_s"),
            potential_v=_payload_float(payload, "potential_v"),
            run_time_s=_payload_float(payload, "run_time_s"),
            equilibration_time_s=_payload_float(payload, "equilibration_time_s"),
        )
    if measurement_type == "cyclic_voltammetry":
        return CyclicVoltammetrySettings(
            begin_potential_v=_payload_float(payload, "begin_potential_v"),
            vertex1_potential_v=_payload_float(payload, "vertex1_potential_v"),
            vertex2_potential_v=_payload_float(payload, "vertex2_potential_v"),
            step_potential_v=_payload_float(payload, "step_potential_v"),
            scan_rate_v_per_s=_payload_float(payload, "scan_rate_v_per_s"),
            scan_count=_payload_int(payload, "scan_count"),
            equilibration_time_s=_payload_float(payload, "equilibration_time_s"),
        )
    return LinearSweepVoltammetrySettings(
        begin_potential_v=_payload_float(payload, "begin_potential_v"),
        end_potential_v=_payload_float(payload, "end_potential_v"),
        step_potential_v=_payload_float(payload, "step_potential_v"),
        scan_rate_v_per_s=_payload_float(payload, "scan_rate_v_per_s"),
        sweep_count=_payload_int(payload, "sweep_count"),
        equilibration_time_s=_payload_float(payload, "equilibration_time_s"),
    )


def _cleanup_settings_from_payload(
    payload: Mapping[str, object],
) -> ExperimentCleanupSettings:
    return ExperimentCleanupSettings(
        mixing_rinse_volume_ml=_payload_float(payload, "mixing_rinse_volume_ml"),
        mixing_rinse_cycles=_payload_int(payload, "mixing_rinse_cycles"),
        flow_cell_rinse_through_volume_ml=_payload_float(
            payload, "flow_cell_rinse_through_volume_ml"
        ),
        source_line_additional_rinse_volume_ml=_payload_float(
            payload, "source_line_additional_rinse_volume_ml"
        ),
    )


def _plan_source_from_payload(payload: Mapping[str, object]) -> ExperimentPlanSource:
    return ExperimentPlanSource(
        source_stock_id=_payload_optional_int(payload, "source_stock_id"),
        selector_port=_payload_int(payload, "selector_port"),
        source_config_key=_payload_str(payload, "source_config_key"),
        fraction=_payload_float(payload, "fraction"),
        locked_fraction=_payload_optional_float(payload, "locked_fraction"),
        control_substance_id=_payload_optional_int(payload, "control_substance_id"),
        control_substance_label=_payload_optional_str(
            payload, "control_substance_label"
        ),
        stock_control_concentration_mol_l=_payload_optional_float(
            payload, "stock_control_concentration_mol_l"
        ),
    )


def _mixture_transfer_from_payload(
    payload: Mapping[str, object],
) -> MixtureTransferDiagnostic:
    regime_value = _payload_str(payload, "regime")
    if regime_value not in {
        "short_single_aspiration",
        "bulk_single_aspiration",
        "common_line_strokes",
    }:
        raise ValueError(f"Unsupported mixture-transfer regime: {regime_value!r}")
    return MixtureTransferDiagnostic(
        source_config_key=_payload_str(payload, "source_config_key"),
        selector_port=_payload_int(payload, "selector_port"),
        requested_volume_ml=_payload_float(payload, "requested_volume_ml"),
        syringe_steps=_payload_int(payload, "syringe_steps"),
        nominal_volume_ml=_payload_float(payload, "nominal_volume_ml"),
        regime=cast(MixtureTransferRegime, regime_value),
        liquid_stroke_count=_payload_int(payload, "liquid_stroke_count"),
    )


def _plan_concentration_from_payload(
    payload: Mapping[str, object],
) -> ExperimentPlanSubstanceConcentration:
    return ExperimentPlanSubstanceConcentration(
        substance_id=_payload_int(payload, "substance_id"),
        substance_label=_payload_str(payload, "substance_label"),
        concentration_mol_l=_payload_float(payload, "concentration_mol_l"),
    )


def _payload_float(payload: Mapping[str, object], key: str) -> float:
    return float(require_finite_number(_payload_value(payload, key), name=key))


def _payload_optional_float(payload: Mapping[str, object], key: str) -> float | None:
    value = _payload_value(payload, key)
    if value is None:
        return None
    return float(require_finite_number(value, name=key))


def _payload_int(payload: Mapping[str, object], key: str) -> int:
    return require_integer(_payload_value(payload, key), name=key)


def _payload_optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = _payload_value(payload, key)
    if value is None:
        return None
    return require_integer(value, name=key)


def _payload_str(payload: Mapping[str, object], key: str) -> str:
    value = _payload_value(payload, key)
    if not isinstance(value, str):
        raise TypeError(f"Saved experiment plan field {key!r} must be text")
    return value


def _payload_optional_str(payload: Mapping[str, object], key: str) -> str | None:
    value = _payload_value(payload, key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Saved experiment plan field {key!r} must be text or null")
    return value


def _payload_value(payload: Mapping[str, object], key: str) -> object:
    if key not in payload:
        raise ValueError(f"Saved experiment plan is missing field {key!r}")
    return payload[key]


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class _MeasurementFlowSession:
    """Run one blocking EmStat measurement beside one Runze flow movement."""

    def __init__(
        self,
        *,
        controller: EmStatController,
        action: Callable[[Callable[[int], None]], Measurement],
        command_start_timeout_s: float,
    ) -> None:
        self._controller = controller
        self._action = action
        self._command_start_timeout_s = command_start_timeout_s
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future[Measurement] | None = None
        self._started_at: float | None = None
        self._elapsed_s: float | None = None
        self._command_started = threading.Event()
        self._measurement_id: int | None = None
        self.measurement: Measurement | None = None

    @property
    def elapsed_s(self) -> float | None:
        if self._elapsed_s is not None:
            return self._elapsed_s
        if self._started_at is None:
            return None
        return max(0.0, time.perf_counter() - self._started_at)

    @property
    def measurement_id(self) -> int:
        if self._measurement_id is None:
            raise RuntimeError("EmStat measurement command did not start")
        return self._measurement_id

    def start(self) -> None:
        if self._future is not None:
            raise RuntimeError("EmStat flow measurement was already started")
        if self._controller.busy:
            raise RuntimeError("An EmStat measurement is already in progress")

        self._started_at = time.perf_counter()
        self._future = Future()

        def measure():
            try:
                result = self._action(self._mark_command_started)
            except BaseException as error:
                self._future.set_exception(error)
            else:
                self._future.set_result(result)

        threading.Thread(target=measure, name="emstat-experiment", daemon=True).start()

        started = self._command_started.wait(self._command_start_timeout_s)
        if started:
            return

        try:
            if self._future.done():
                self._future.result()
            else:
                threading.Thread(
                    target=self._controller.abort_measurement,
                    name="emstat-timeout-abort",
                    daemon=True,
                ).start()
        finally:
            self.close()
        raise TimeoutError(
            "EmStat measurement did not reach the PalmSens command boundary within "
            f"{self._command_start_timeout_s:g} s"
        )

    def finish(self) -> None:
        future = self._require_future()
        try:
            self.measurement = future.result()
        finally:
            self._record_elapsed()
            self.close()

    def abort_and_wait(self) -> None:
        future = self._future
        if future is None:
            return

        try:
            if self._controller.abort_available:
                threading.Thread(
                    target=self._controller.abort_measurement,
                    name="emstat-abort",
                    daemon=True,
                ).start()
        except Exception:
            logger.exception("Failed to abort EmStat after Runze flow failure")

        try:
            future.result(timeout=5.0)
        except MeasurementAbortedError:
            pass
        except Exception:
            logger.exception("EmStat also failed after Runze flow failure")
        finally:
            self._record_elapsed()
            self.close()

    def close(self) -> None:
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _require_future(self) -> Future[Measurement]:
        if self._future is None:
            raise RuntimeError("EmStat flow measurement was not started")
        return self._future

    def _record_elapsed(self) -> None:
        if self._started_at is not None and self._elapsed_s is None:
            self._elapsed_s = max(0.0, time.perf_counter() - self._started_at)

    def _mark_command_started(self, measurement_id: int) -> None:
        self._measurement_id = measurement_id
        self._command_started.set()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")

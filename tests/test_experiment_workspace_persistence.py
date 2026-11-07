import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from config.runze_setup import RUNZE_PLUMBING
from controllers.emstat_controller import EmStatController
from controllers.experiment_controller import (
    ExperimentCleanupSettings,
    ExperimentController,
    ExperimentPlan,
    ExperimentPlanSource,
    ExperimentPlanSubstanceConcentration,
    ExperimentProtocolEstimate,
    ManualExperimentPlanRequest,
    PreparedMixture,
    _experiment_plan_from_payload,
    _experiment_plan_to_payload,
    _experiment_protocol_snapshot_to_payload,
    _saved_protocol_from_snapshot,
)
from controllers.runze_controller import RunzeController
from fluidics.runze_fluidics import MixtureTransferDiagnostic
from gui.experiment_protocol_panel import ExperimentProtocolPanelState
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    EmStat4S,
    LinearSweepVoltammetrySettings,
)
from storage.experiment_repository import (
    ExperimentRepository,
    MetricContribution,
    MixtureComponent,
    PersistedExperimentWorkspaceState,
    PersistedSourceLineState,
    PersistedWorkflowPhase,
)


class ExperimentWorkspaceRepositoryTests(unittest.TestCase):
    @staticmethod
    def _stopped_flow_plan(*, source_revision: int) -> ExperimentPlan:
        return ExperimentPlan(
            source_configuration_revision=source_revision,
            sources=(),
            measurement_type="chronoamperometry",
            measurement_settings=ChronoamperometrySettings(0.1, 0.5, 5.0),
            measurement_name="restart test",
            handling_rpm=10,
            approach_rpm=8,
            flow_rpm=None,
            water_distribution="single",
            cleanup_settings=ExperimentCleanupSettings(1.0, 2, 1.0, 0.1),
            estimated_measurement_duration_s=5.0,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            mixing_path_condition_volume_ml=0.8667,
            flow_cell_load_volume_ml=0.525,
            mixing_vessel_reserve_volume_ml=0.2,
            total_mixture_volume_ml=1.5917,
            substance_concentrations=(),
            workflow_kind="stopped_flow",
            measurement_count=3,
            between_measurement_displacement_volume_ml=0.05,
        )

    @staticmethod
    def _workspace_execution_payload(
        plan: ExperimentPlan,
        *,
        active_index: int = 0,
        play_state: str = "inactive",
        next_stage: str | None = None,
    ) -> dict[str, object]:
        return {
            "workspace_execution_version": 1,
            "plans": [_experiment_plan_to_payload(plan)],
            "active_index": active_index,
            "play_state": play_state,
            "next_stage": next_stage,
        }

    @staticmethod
    def _create_test_mixture(
        repository: ExperimentRepository, plan: ExperimentPlan
    ) -> int:
        source_setup_id = repository.find_or_create_source_setup(source_stock_ids=())
        return repository.create_mixture(
            source_setup_id=source_setup_id,
            source_configuration_revision=plan.source_configuration_revision,
            total_volume_ml=plan.total_mixture_volume_ml,
            components=(
                MixtureComponent(
                    source_stock_id=None,
                    fraction=1.0,
                    selector_port=RUNZE_PLUMBING.water.number,
                    source_config_key=RUNZE_PLUMBING.water.key,
                ),
            ),
            protocol_snapshot=_experiment_protocol_snapshot_to_payload(plan),
        )

    def test_workspace_state_round_trip_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "workspace.sqlite3")
            state = PersistedExperimentWorkspaceState(
                workflow_phase="ready",
                operation_in_progress=False,
                active_plan={
                    "workspace_execution_version": 1,
                    "plans": [{"name": "test"}],
                    "active_index": 0,
                    "play_state": "inactive",
                    "next_stage": None,
                },
                mixture_id=None,
                prepared_volume_state=None,
                maintenance_source_ports=(9, 6),
                last_operation="priming completed",
                updated_at="2026-08-20T12:00:00+00:00",
                source_lines=(
                    PersistedSourceLineState(
                        selector_port=9,
                        source_config_key="source_a",
                        source_stock_id=None,
                        status="air_filled",
                        updated_at="2026-08-20T12:00:00+00:00",
                        reason="test",
                    ),
                ),
            )

            repository.save_experiment_workspace_state(state)

            self.assertEqual(repository.load_experiment_workspace_state(), state)

    def test_fresh_database_uses_current_baseline_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.sqlite3"
            ExperimentRepository(path)
            with closing(sqlite3.connect(path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                protocol_column = next(
                    row
                    for row in connection.execute("PRAGMA table_info(mixtures)")
                    if row[1] == "protocol_snapshot_json"
                )
            self.assertEqual(version, 1)
            self.assertEqual(protocol_column[3], 1)

    def test_nonbaseline_database_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsupported.sqlite3"
            ExperimentRepository(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA user_version = 7")

            with self.assertRaisesRegex(RuntimeError, "not supported"):
                ExperimentRepository(path)

    def test_metric_components_and_experiment_summary_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "results.sqlite3")
            plan = self._stopped_flow_plan(
                source_revision=(
                    repository.load_current_source_configuration().revision
                )
            )
            mixture_id = self._create_test_mixture(repository, plan)
            repository.set_mixture_status(mixture_id, "completed")
            measurement_ids: list[int] = []
            for sequence_number, value in ((1, 1.0), (2, 3.0)):
                measurement_id = repository.create_measurement(
                    mixture_id=mixture_id,
                    measurement_type="chronoamperometry",
                    origin="experiment",
                    settings={"run_time_s": 5.0},
                    protocol_key="stopped_flow_stationary",
                    run_sequence_number=sequence_number,
                )
                repository.set_measurement_status(
                    measurement_id,
                    "completed",
                    session_file_path=f"result-{sequence_number}.pssession",
                )
                repository.save_measurement_metric(
                    measurement_id=measurement_id,
                    metric_name="mean_scan_max_current",
                    value=value,
                    unit="µA",
                    standard_error=0.1,
                    analysis_method="logical_series_maxima",
                    analysis_version="1.2.0",
                    contributions=(
                        MetricContribution(1, "series", value - 0.1),
                        MetricContribution(2, "series", value + 0.1),
                    ),
                )
                measurement_ids.append(measurement_id)

            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )
            summaries = controller.objective_experiment_summaries(
                measurement_type="chronoamperometry", source_scope="all_setups"
            )

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].experiment_id, mixture_id)
            self.assertAlmostEqual(summaries[0].value, 2.0)
            self.assertAlmostEqual(
                cast(float, summaries[0].between_measurement_standard_deviation), 2**0.5
            )
            self.assertEqual(
                [item.measurement_id for item in summaries[0].measurements],
                measurement_ids,
            )
            self.assertEqual(
                [item.value for item in summaries[0].measurements[0].contributions],
                [0.9, 1.1],
            )
            repository.save_measurement_metric(
                measurement_id=measurement_ids[0],
                metric_name="mean_scan_max_current",
                value=1.0,
                unit="µA",
                analysis_method="logical_series_maxima",
                analysis_version="1.2.0",
                contributions=(MetricContribution(1, "series", 1.0),),
            )
            reloaded = repository.list_measurement_metrics(
                measurement_id=measurement_ids[0]
            )
            self.assertEqual(
                reloaded[0].contributions, (MetricContribution(1, "series", 1.0),)
            )

    def test_interrupted_checkpoint_restores_as_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "interrupted.sqlite3")
            repository.save_experiment_workspace_state(
                PersistedExperimentWorkspaceState(
                    workflow_phase="inactive",
                    operation_in_progress=True,
                    active_plan=None,
                    mixture_id=None,
                    prepared_volume_state=None,
                    maintenance_source_ports=(),
                    last_operation="manual movement",
                    updated_at="2026-08-20T12:00:00+00:00",
                    source_lines=(),
                )
            )

            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )

            self.assertEqual(controller.snapshot.workflow_phase, "recovery_required")
            self.assertTrue(
                all(state.status == "unknown" for state in controller.snapshot.priming)
            )

    def test_stopped_flow_checkpoint_restores_between_completed_stages(self) -> None:
        for workflow_phase, volume_state in (
            ("prepared", "prepared"),
            ("flow_completed", "flow_completed"),
        ):
            with (
                self.subTest(workflow_phase=workflow_phase),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository = ExperimentRepository(Path(directory) / "restart.sqlite3")
                plan = self._stopped_flow_plan(
                    source_revision=(
                        repository.load_current_source_configuration().revision
                    )
                )
                mixture_id = self._create_test_mixture(repository, plan)
                repository.save_experiment_workspace_state(
                    PersistedExperimentWorkspaceState(
                        workflow_phase=cast(PersistedWorkflowPhase, workflow_phase),
                        operation_in_progress=False,
                        active_plan=self._workspace_execution_payload(plan),
                        mixture_id=mixture_id,
                        prepared_volume_state=volume_state,
                        maintenance_source_ports=(),
                        last_operation=f"{workflow_phase} completed",
                        updated_at="2026-08-24T12:00:00+00:00",
                        source_lines=(),
                    )
                )

                controller = ExperimentController(
                    runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                    emstat_controller=EmStatController(EmStat4S()),
                    repository=repository,
                )

                self.assertEqual(controller.active_manual_experiment_plan, plan)
                self.assertEqual(controller.snapshot.workflow_phase, workflow_phase)
                self.assertEqual(controller.prepared_mixture_volume_state, volume_state)
                prepared = controller.prepared_mixture
                self.assertIsNotNone(prepared)
                if prepared is None:
                    self.fail("Prepared mixture was not restored")
                self.assertEqual(prepared.mixture_id, mixture_id)
                restored_plan = controller.active_manual_experiment_plan
                self.assertIsNotNone(restored_plan)
                if restored_plan is None:
                    self.fail("Stopped-flow plan was not restored")
                self.assertEqual(
                    restored_plan.between_measurement_displacement_volume_ml, 0.05
                )

    def test_manual_play_pause_can_be_cancelled_resumed_and_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "play.sqlite3")
            plan = self._stopped_flow_plan(
                source_revision=repository.load_current_source_configuration().revision
            )
            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )

            controller.start_manual_play(plans=(plan,))
            self.assertEqual(controller.snapshot.manual_play_next_stage, "prepare")
            self.assertTrue(controller.request_manual_play_pause())
            self.assertEqual(controller.snapshot.manual_play_state, "pause_requested")
            self.assertTrue(controller.cancel_manual_play_pause())
            self.assertEqual(controller.snapshot.manual_play_state, "running")
            self.assertTrue(controller.request_manual_play_pause())

            self.assertIsNone(controller.manual_play_next_stage())
            self.assertEqual(controller.snapshot.manual_play_state, "paused")
            self.assertTrue(controller.resume_manual_play())
            self.assertEqual(controller.snapshot.manual_play_state, "running")
            self.assertTrue(controller.request_manual_play_pause())
            self.assertIsNone(controller.manual_play_next_stage())
            self.assertTrue(controller.discard_manual_play())
            self.assertEqual(controller.snapshot.manual_play_state, "inactive")
            self.assertEqual(controller.snapshot.manual_play_next_stage, "prepare")

    def test_multiple_manual_plan_requests_are_built_as_one_ordered_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "build-queue.sqlite3")
            first = self._stopped_flow_plan(
                source_revision=repository.load_current_source_configuration().revision
            )
            second = replace(first, measurement_name="second experiment")
            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )
            requests = (
                ManualExperimentPlanRequest({}, "first experiment"),
                ManualExperimentPlanRequest({}, "second experiment"),
            )

            with patch.object(
                controller, "_build_experiment_plan", side_effect=(first, second)
            ) as build:
                plans = controller.build_manual_experiment_plans(
                    requests=requests,
                    protocol=ExperimentProtocolEstimate(
                        measurement_type=first.measurement_type,
                        measurement_settings=first.measurement_settings,
                        handling_rpm=first.handling_rpm,
                        approach_rpm=first.approach_rpm,
                        flow_rpm=first.flow_rpm,
                        water_distribution=first.water_distribution,
                        cleanup_settings=first.cleanup_settings,
                        estimated_measurement_duration_s=(
                            first.estimated_measurement_duration_s
                        ),
                        flow_rate_ml_min=first.flow_rate_ml_min,
                        measurement_flow_volume_ml=first.measurement_flow_volume_ml,
                        mixing_path_condition_volume_ml=(
                            first.mixing_path_condition_volume_ml
                        ),
                        flow_cell_load_volume_ml=first.flow_cell_load_volume_ml,
                        mixing_vessel_reserve_volume_ml=(
                            first.mixing_vessel_reserve_volume_ml
                        ),
                        total_mixture_volume_ml=first.total_mixture_volume_ml,
                        workflow_kind=first.workflow_kind,
                        measurement_count=first.measurement_count,
                        between_measurement_displacement_volume_ml=(
                            first.between_measurement_displacement_volume_ml
                        ),
                    ),
                )

            self.assertEqual(plans, (first, second))
            self.assertEqual(controller.manual_experiment_plans, plans)
            self.assertEqual(controller.snapshot.manual_play_plan_count, 2)
            self.assertEqual(build.call_count, 2)

    def test_running_manual_play_restores_paused_at_safe_stage_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "play-restart.sqlite3"
            repository = ExperimentRepository(path)
            plan = self._stopped_flow_plan(
                source_revision=repository.load_current_source_configuration().revision
            )
            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )
            controller.start_manual_play(plans=(plan,))

            restored = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=ExperimentRepository(path),
            )

            self.assertEqual(restored.active_manual_experiment_plan, plan)
            self.assertEqual(restored.snapshot.manual_play_state, "paused")
            self.assertEqual(restored.snapshot.manual_play_next_stage, "prepare")
            self.assertEqual(restored.snapshot.manual_play_plan_index, 1)
            self.assertEqual(restored.snapshot.manual_play_plan_count, 1)

    def test_manual_play_advances_from_prepare_through_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "play-stages.sqlite3")
            plan = self._stopped_flow_plan(
                source_revision=repository.load_current_source_configuration().revision
            )
            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )
            controller.start_manual_play(plans=(plan,))
            mixture_id = self._create_test_mixture(repository, plan)
            controller._prepared_mixture = PreparedMixture(mixture_id, plan)
            controller._prepared_mixture_volume_state = "prepared"
            controller._workflow_phase = "prepared"

            controller.finish_manual_play_stage("prepare")
            self.assertEqual(controller.snapshot.manual_play_next_stage, "measure")
            controller._prepared_mixture_volume_state = "flow_completed"
            controller._workflow_phase = "flow_completed"

            controller.finish_manual_play_stage("measure")
            self.assertEqual(controller.snapshot.manual_play_next_stage, "clean")
            controller._prepared_mixture = None
            controller._prepared_mixture_volume_state = None
            controller._workflow_phase = "ready"

            self.assertEqual(controller.finish_manual_play_stage("clean"), "inactive")
            self.assertIsNone(controller.snapshot.manual_play_plan_index)
            self.assertEqual(controller.snapshot.manual_play_plan_count, 1)

    def test_multi_experiment_play_advances_and_restores_next_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multi-play.sqlite3"
            repository = ExperimentRepository(path)
            first = self._stopped_flow_plan(
                source_revision=repository.load_current_source_configuration().revision
            )
            second = replace(first, measurement_name="second experiment")
            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )
            controller.start_manual_play(plans=(first, second))
            mixture_id = self._create_test_mixture(repository, first)
            controller._prepared_mixture = PreparedMixture(mixture_id, first)
            controller._prepared_mixture_volume_state = "prepared"
            controller._workflow_phase = "prepared"
            controller.finish_manual_play_stage("prepare")
            controller._prepared_mixture_volume_state = "flow_completed"
            controller._workflow_phase = "flow_completed"
            controller.finish_manual_play_stage("measure")
            self.assertTrue(controller.request_manual_play_pause())
            controller._prepared_mixture = None
            controller._prepared_mixture_volume_state = None
            controller._workflow_phase = "ready"

            self.assertEqual(controller.finish_manual_play_stage("clean"), "paused")
            self.assertEqual(controller.snapshot.manual_play_plan_index, 2)
            self.assertEqual(controller.snapshot.manual_play_plan_count, 2)
            self.assertEqual(controller.snapshot.manual_play_next_stage, "prepare")
            self.assertEqual(controller.active_manual_experiment_plan, second)

            restored = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=ExperimentRepository(path),
            )
            self.assertEqual(restored.manual_experiment_plans, (first, second))
            self.assertEqual(restored.snapshot.manual_play_state, "paused")
            self.assertEqual(restored.snapshot.manual_play_plan_index, 2)
            self.assertEqual(restored.active_manual_experiment_plan, second)

    def test_multi_experiment_play_rejects_mixed_protocols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "mixed.sqlite3")
            first = self._stopped_flow_plan(
                source_revision=repository.load_current_source_configuration().revision
            )
            changed_protocol = replace(first, approach_rpm=first.approach_rpm + 1)
            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )

            with self.assertRaisesRegex(ValueError, "same protocol"):
                controller.start_manual_play(plans=(first, changed_protocol))

    def test_interrupted_stopped_flow_checkpoint_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "interrupted.sqlite3")
            plan = self._stopped_flow_plan(
                source_revision=repository.load_current_source_configuration().revision
            )
            mixture_id = self._create_test_mixture(repository, plan)
            repository.save_experiment_workspace_state(
                PersistedExperimentWorkspaceState(
                    workflow_phase="prepared",
                    operation_in_progress=True,
                    active_plan=self._workspace_execution_payload(plan),
                    mixture_id=mixture_id,
                    prepared_volume_state="prepared",
                    maintenance_source_ports=(),
                    last_operation="running stopped-flow measurement series",
                    updated_at="2026-08-24T12:00:00+00:00",
                    source_lines=(),
                )
            )

            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )

            self.assertEqual(controller.snapshot.workflow_phase, "recovery_required")
            self.assertEqual(controller.prepared_mixture_volume_state, "unknown")
            self.assertIsNone(controller.prepared_mixture)
            self.assertEqual(controller.active_manual_experiment_plan, plan)

    def test_external_runze_operation_invalidates_workspace_before_motion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "external.sqlite3")
            controller = ExperimentController(
                runze_controller=RunzeController(plumbing=RUNZE_PLUMBING),
                emstat_controller=EmStatController(EmStat4S()),
                repository=repository,
            )

            controller._handle_runze_physical_operation("manual syringe movement")

            self.assertEqual(controller.snapshot.workflow_phase, "recovery_required")
            self.assertEqual(
                repository.load_experiment_workspace_state().last_operation,
                "external Runze operation: manual syringe movement",
            )

    def test_experiment_run_groups_ordered_measurements_and_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "history.sqlite3")
            source_setup_id = repository.find_or_create_source_setup(
                source_stock_ids=()
            )
            plan = self._stopped_flow_plan(
                source_revision=(
                    repository.load_current_source_configuration().revision
                )
            )
            protocol = _experiment_protocol_snapshot_to_payload(plan)
            mixture_id = repository.create_mixture(
                source_setup_id=source_setup_id,
                source_configuration_revision=plan.source_configuration_revision,
                total_volume_ml=1.0,
                components=(
                    MixtureComponent(
                        source_stock_id=None,
                        fraction=1.0,
                        selector_port=1,
                        source_config_key="water",
                    ),
                ),
                protocol_snapshot=protocol,
            )
            second = repository.create_measurement(
                mixture_id=mixture_id,
                measurement_type="chronoamperometry",
                origin="experiment",
                settings={"run_time_s": 5.0},
                run_sequence_number=2,
            )
            first = repository.create_measurement(
                mixture_id=mixture_id,
                measurement_type="chronoamperometry",
                origin="experiment",
                settings={"run_time_s": 5.0},
                run_sequence_number=1,
            )

            (saved_run,) = repository.list_saved_experiment_runs()

            self.assertEqual(saved_run.protocol_snapshot, protocol)
            self.assertEqual(saved_run.artifact_directory, "000001_experiment")
            self.assertEqual(
                [item.measurement_id for item in saved_run.measurements],
                [first, second],
            )

    def test_experiment_measurement_requires_explicit_sequence_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ExperimentRepository(Path(directory) / "sequence.sqlite3")
            plan = self._stopped_flow_plan(
                source_revision=(
                    repository.load_current_source_configuration().revision
                )
            )
            mixture_id = self._create_test_mixture(repository, plan)

            with self.assertRaisesRegex(ValueError, "run sequence number"):
                repository.create_measurement(
                    mixture_id=mixture_id,
                    measurement_type="chronoamperometry",
                    origin="experiment",
                    settings={"run_time_s": 5.0},
                )


class ExperimentPlanCodecTests(unittest.TestCase):
    def _plan(self) -> ExperimentPlan:
        return ExperimentPlan(
            source_configuration_revision=3,
            sources=(
                ExperimentPlanSource(
                    source_stock_id=2,
                    selector_port=9,
                    source_config_key="source_a",
                    fraction=0.25,
                    locked_fraction=None,
                    control_substance_id=4,
                    control_substance_label="Analyte",
                    stock_control_concentration_mol_l=0.1,
                ),
            ),
            measurement_type="chronoamperometry",
            measurement_settings=ChronoamperometrySettings(0.1, 0.5, 5.0),
            measurement_name="checkpoint",
            handling_rpm=10,
            inlet_aspiration_rpm=5,
            approach_rpm=8,
            flow_rpm=6,
            water_distribution="single",
            cleanup_settings=ExperimentCleanupSettings(1.0, 2, 1.0, 0.1),
            estimated_measurement_duration_s=5.0,
            flow_rate_ml_min=0.4,
            measurement_flow_volume_ml=0.2,
            mixing_path_condition_volume_ml=0.3,
            flow_cell_load_volume_ml=0.5,
            mixing_vessel_reserve_volume_ml=0.1,
            total_mixture_volume_ml=0.9,
            substance_concentrations=(
                ExperimentPlanSubstanceConcentration(4, "Analyte", 0.025),
            ),
            mixture_transfers=(
                MixtureTransferDiagnostic(
                    source_config_key="source_a",
                    selector_port=9,
                    requested_volume_ml=0.225,
                    syringe_steps=270,
                    nominal_volume_ml=0.225,
                    regime="short_single_aspiration",
                    liquid_stroke_count=1,
                ),
            ),
        )

    def test_all_measurement_setting_types_round_trip(self) -> None:
        base = self._plan()
        plans = (
            base,
            replace(
                base,
                measurement_type="cyclic_voltammetry",
                measurement_settings=CyclicVoltammetrySettings(
                    0.0, -0.5, 0.5, 0.01, 0.1, 2
                ),
            ),
            replace(
                base,
                measurement_type="linear_sweep_voltammetry",
                measurement_settings=LinearSweepVoltammetrySettings(
                    -0.5, 0.5, 0.01, 0.1, 2
                ),
            ),
        )

        for plan in plans:
            with self.subTest(measurement_type=plan.measurement_type):
                self.assertEqual(
                    _experiment_plan_from_payload(_experiment_plan_to_payload(plan)),
                    plan,
                )

    def test_complete_protocol_snapshot_round_trip(self) -> None:
        plan = self._plan()

        restored = _saved_protocol_from_snapshot(
            _experiment_protocol_snapshot_to_payload(plan)
        )

        self.assertEqual(restored.measurement_settings, plan.measurement_settings)
        self.assertEqual(restored.water_distribution, plan.water_distribution)
        self.assertEqual(restored.handling_rpm, plan.handling_rpm)
        self.assertEqual(restored.inlet_aspiration_rpm, plan.inlet_aspiration_rpm)

    def test_stopped_flow_strategy_and_count_round_trip(self) -> None:
        plan = replace(
            self._plan(),
            workflow_kind="stopped_flow",
            measurement_count=4,
            flow_rpm=None,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            between_measurement_displacement_volume_ml=0.075,
        )

        plan_payload = _experiment_plan_to_payload(plan)
        protocol_payload = _experiment_protocol_snapshot_to_payload(plan)
        self.assertEqual(plan_payload["version"], 3)
        self.assertEqual(protocol_payload["version"], 2)

        restored_plan = _experiment_plan_from_payload(plan_payload)
        restored_protocol = _saved_protocol_from_snapshot(protocol_payload)

        self.assertEqual(restored_plan, plan)
        self.assertEqual(restored_protocol.workflow_kind, "stopped_flow")
        self.assertEqual(restored_protocol.measurement_count, 4)
        self.assertEqual(
            restored_protocol.between_measurement_displacement_volume_ml, 0.075
        )
        self.assertIsNone(restored_protocol.flow_rpm)

    def test_single_stopped_flow_protocol_round_trip_has_no_displacement(self) -> None:
        plan = replace(
            self._plan(),
            workflow_kind="stopped_flow",
            measurement_count=1,
            flow_rpm=None,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            between_measurement_displacement_volume_ml=None,
        )

        restored_plan = _experiment_plan_from_payload(_experiment_plan_to_payload(plan))
        restored_protocol = _saved_protocol_from_snapshot(
            _experiment_protocol_snapshot_to_payload(plan)
        )

        self.assertEqual(restored_plan, plan)
        self.assertEqual(restored_protocol.measurement_count, 1)
        self.assertIsNone(restored_protocol.between_measurement_displacement_volume_ml)

    def test_stopped_flow_displacement_is_required_only_for_multiple_measurements(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "does not use"):
            replace(
                self._plan(),
                workflow_kind="stopped_flow",
                measurement_count=1,
                flow_rpm=None,
                flow_rate_ml_min=None,
                measurement_flow_volume_ml=None,
                between_measurement_displacement_volume_ml=0.05,
            )
        with self.assertRaisesRegex(ValueError, "is required"):
            replace(
                self._plan(),
                workflow_kind="stopped_flow",
                measurement_count=2,
                flow_rpm=None,
                flow_rate_ml_min=None,
                measurement_flow_volume_ml=None,
                between_measurement_displacement_volume_ml=None,
            )

    def test_protocol_editor_uses_conservative_rpm_defaults(self) -> None:
        state = ExperimentProtocolPanelState()

        self.assertEqual(state.handling_rpm, 75)
        self.assertEqual(state.inlet_aspiration_rpm, 10)
        self.assertEqual(state.flow_rpm, 1)
        self.assertEqual(state.approach_rpm, 5)

    def test_incomplete_baseline_plan_is_rejected(self) -> None:
        payload = _experiment_plan_to_payload(self._plan())
        payload.pop("workflow_kind")
        payload.pop("measurement_count")

        with self.assertRaises((TypeError, ValueError)):
            _experiment_plan_from_payload(payload)

    def test_stopped_flow_plan_is_accepted_for_execution(self) -> None:
        plan = replace(
            self._plan(),
            workflow_kind="stopped_flow",
            measurement_count=3,
            flow_rpm=None,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            between_measurement_displacement_volume_ml=0.05,
        )

        ExperimentController._require_implemented_workflow(plan)

    def test_unsupported_protocol_version_is_rejected(self) -> None:
        plan = replace(
            self._plan(),
            workflow_kind="stopped_flow",
            measurement_count=3,
            flow_rpm=None,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            between_measurement_displacement_volume_ml=0.05,
        )
        payload = _experiment_protocol_snapshot_to_payload(plan)
        payload["version"] = 3

        with self.assertRaisesRegex(ValueError, "Unsupported"):
            _saved_protocol_from_snapshot(payload)


if __name__ == "__main__":
    unittest.main()

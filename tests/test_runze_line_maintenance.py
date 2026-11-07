import math
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from palmsens.data_types import Measurement

from config.runze_setup import RUNZE_PLUMBING
from controllers.emstat_controller import EmStatSnapshot
from controllers.experiment_controller import (
    ExperimentCleanupSettings,
    ExperimentController,
    ExperimentPlan,
    ExperimentPlanSource,
    PreparedMixture,
    _SourceLineIdentity,
)
from controllers.runze_controller import RunzeController
from fluidics.runze_fluidics import (
    RunzeFluidics,
    StoppedFlowHooks,
    build_mixture_transfer_diagnostics,
)
from palmsens.emstat4s import ChronoamperometrySettings
from runze.sv07m import SV07M
from runze.sy01b import SY01B
from storage.experiment_repository import (
    CurrentSourceConfiguration,
    ExperimentRepository,
    SourceConfigurationEntry,
)


class _FakeSyringe:
    def __init__(self, *, capacity_ml: float = 5.0) -> None:
        self.syringe_volume_ml = capacity_ml
        self.steps_per_ml = 10_000
        self.ml_per_step = 1 / self.steps_per_ml
        self.movements: list[tuple[str, float | None, int, str]] = []

    @staticmethod
    def steps_from_volume_ml(volume_ml: float) -> int:
        return round(volume_ml * 10_000)

    def aspirate_ml(self, *, volume_ml: float, rpm: int, source: str) -> None:
        self.movements.append(("aspirate", volume_ml, rpm, source))

    def dispense_ml(self, *, volume_ml: float, rpm: int, destination: str) -> None:
        self.movements.append(("dispense", volume_ml, rpm, destination))

    def empty_syringe(self, *, rpm: int, destination: str) -> None:
        self.movements.append(("empty", None, rpm, destination))


class _FakeSelector:
    def __init__(self) -> None:
        self.selected_ports: list[int] = []

    def ensure_port(self, port: int) -> None:
        self.selected_ports.append(port)


class _FakeRunzeController:
    plumbing = RUNZE_PLUMBING
    busy = False

    def __init__(self, *, fail_recovery: bool = False) -> None:
        self.fail_recovery = fail_recovery
        self.calls: list[tuple[str, tuple[int, ...]]] = []
        self.prime_calls: list[tuple[tuple[int, ...], bool]] = []
        self.stopped_flow_arguments: dict[str, Any] | None = None
        self.mixture_arguments: dict[str, Any] | None = None

    @property
    def syringe_capacity_ml(self) -> float:
        return 5.0

    def prime_selected_liquid_source_lines(self, **arguments: Any) -> None:
        self.prime_calls.append(
            (tuple(arguments["source_ports"]), bool(arguments["prime_water"]))
        )

    def recover_source_lines_and_clear_flow_cell(self, **arguments: Any) -> None:
        ports = tuple(arguments["source_ports"])
        self.calls.append(("recover", ports))
        if self.fail_recovery:
            raise RuntimeError("simulated recovery failure")

    def rinse_source_lines_with_water(self, **arguments: Any) -> None:
        self.calls.append(("rinse", tuple(arguments["source_ports"])))

    def air_empty_source_lines(self, **arguments: Any) -> None:
        self.calls.append(("air", tuple(arguments["source_ports"])))

    def prepare_mixture_with_single_aspiration_transfers(
        self, **arguments: Any
    ) -> None:
        self.mixture_arguments = {"method": "single", **arguments}

    def prepare_mixture_with_common_line_stroke_transfers(
        self, **arguments: Any
    ) -> None:
        self.mixture_arguments = {"method": "strokes", **arguments}

    def run_stopped_flow_series_from_mixing_vessel(self, **arguments: Any) -> None:
        self.stopped_flow_arguments = arguments
        hooks = arguments["hooks"]
        if hooks.before_fluid_handling is not None:
            hooks.before_fluid_handling()
        for sequence in range(1, int(arguments["measurement_count"]) + 1):
            hooks.measure_at_stop(sequence)


class _FakeEmstatController:
    busy = False

    def __init__(self) -> None:
        self.connected = True
        self.last_saved_session_path: Path | None = None
        self.measurement_number = 0

    def connect(self) -> None:
        self.connected = True

    @staticmethod
    def estimate_chronoamperometry_duration_s(**_: Any) -> float:
        return 1.0

    @property
    def snapshot(self) -> EmStatSnapshot:
        return EmStatSnapshot(
            measurement_id=self.measurement_number,
            state="completed",
            measurement_type="chronoamperometry",
            settings=None,
            point_count=5,
            error_message=None,
        )


class _FakeRepository:
    def __init__(self) -> None:
        self.created_measurements: list[dict[str, Any]] = []
        self.measurement_statuses: list[tuple[int, str]] = []

    def load_current_source_configuration(self) -> CurrentSourceConfiguration:
        return CurrentSourceConfiguration(
            revision=1,
            entries=(
                SourceConfigurationEntry(9, "test_liquid_2", 2, True),
                SourceConfigurationEntry(7, "water", None, True),
                SourceConfigurationEntry(6, "test_liquid_1", 1, True),
            ),
        )

    def create_measurement(self, **arguments: Any) -> int:
        self.created_measurements.append(arguments)
        return len(self.created_measurements)

    def set_measurement_status(
        self, measurement_id: int, status: str, **_: Any
    ) -> None:
        self.measurement_statuses.append((measurement_id, status))


class RunzeLineMaintenanceTests(unittest.TestCase):
    def _fluidics(
        self, *, capacity_ml: float = 5.0
    ) -> tuple[RunzeFluidics, _FakeSyringe, _FakeSelector, list[str]]:
        syringe = _FakeSyringe(capacity_ml=capacity_ml)
        selector = _FakeSelector()
        step_names: list[str] = []

        def run_step(*, name: str, target: str, action: Any) -> None:
            del target
            step_names.append(name)
            action()

        fluidics = RunzeFluidics(
            syringe=cast(SY01B, syringe),
            selector=cast(SV07M, selector),
            plumbing=RUNZE_PLUMBING,
            run_step=run_step,
        )
        return fluidics, syringe, selector, step_names

    def test_recovery_uses_config_order_and_full_flow_cell_volume(self) -> None:
        fluidics, syringe, selector, _ = self._fluidics()

        fluidics.recover_source_lines_and_clear_flow_cell(
            source_ports=(6, 9), handling_rpm=12, approach_rpm=4
        )

        self.assertEqual(selector.selected_ports, [8, 9, 6, 8, 3])
        aspirations = [move for move in syringe.movements if move[0] == "aspirate"]
        expected_source_volume = 2 * (
            RUNZE_PLUMBING.common_line_dead_volume_ml
            + RUNZE_PLUMBING.clearance_margin_ml
        )
        self.assertTrue(math.isclose(aspirations[0][1] or 0, expected_source_volume))
        self.assertEqual(aspirations[0][2:], (12, "inlet"))
        self.assertTrue(math.isclose(aspirations[1][1] or 0, 0.7))
        self.assertEqual(aspirations[1][2:], (12, "inlet"))
        self.assertEqual(syringe.movements[-1], ("empty", None, 4, "inlet"))

    def test_water_rinse_conditions_common_then_rinses_non_water_sources(self) -> None:
        fluidics, syringe, selector, _ = self._fluidics()

        fluidics.rinse_source_lines_with_water(
            source_ports=(6, 9), additional_volume_ml=0.1, rpm=11
        )

        self.assertEqual(selector.selected_ports, [7, 7, 9, 6])
        self.assertEqual(syringe.movements[1], ("empty", None, 11, "outlet"))
        route_aspiration = syringe.movements[2]
        expected_volume = 2 * (
            RUNZE_PLUMBING.common_line_dead_volume_ml
            + RUNZE_PLUMBING.clearance_margin_ml
            + 0.1
        )
        self.assertEqual(route_aspiration[0], "aspirate")
        self.assertTrue(math.isclose(route_aspiration[1] or 0, expected_volume))

    def test_final_air_stage_places_water_last_and_purges_outlet(self) -> None:
        fluidics, syringe, selector, _ = self._fluidics()

        fluidics.air_empty_source_lines(source_ports=(6, 9), rpm=9)

        self.assertEqual(selector.selected_ports, [8, 8, 9, 6, 7, 8])
        self.assertEqual(syringe.movements[1], ("empty", None, 9, "outlet"))
        self.assertEqual(syringe.movements[-1], ("empty", None, 9, "outlet"))

    def test_full_route_capacity_failure_happens_before_movement(self) -> None:
        fluidics, syringe, selector, step_names = self._fluidics(capacity_ml=0.6)

        with self.assertRaisesRegex(ValueError, "full_route_clear"):
            fluidics.recover_source_lines_and_clear_flow_cell(
                source_ports=(9,), handling_rpm=10, approach_rpm=5
            )

        self.assertEqual(step_names, [])
        self.assertEqual(selector.selected_ports, [])
        self.assertEqual(syringe.movements, [])

    def test_full_route_volume_must_cover_selector_to_cell_route(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be smaller"):
            replace(RUNZE_PLUMBING, flow_cell_full_route_dead_volume_ml=0.1)

    def test_partial_prime_does_not_reprime_or_flush_water(self) -> None:
        fluidics, _, selector, step_names = self._fluidics()

        fluidics.prime_selected_liquid_source_lines(
            source_ports=(9,), rpm=10, prime_water=False
        )

        self.assertEqual(selector.selected_ports[0], 9)
        self.assertFalse(any("prime Water source line" in name for name in step_names))
        self.assertFalse(
            any("dispense all water to outlet" in name for name in step_names)
        )

    def test_initial_prime_keeps_water_first_wetting_cycle(self) -> None:
        fluidics, _, selector, step_names = self._fluidics()

        fluidics.prime_selected_liquid_source_lines(source_ports=(9,), rpm=10)

        self.assertEqual(selector.selected_ports[0], RUNZE_PLUMBING.water.number)
        self.assertTrue(any("prime Water source line" in name for name in step_names))
        self.assertTrue(
            any("dispense all water to outlet" in name for name in step_names)
        )
        prime_index = next(
            index
            for index, name in enumerate(step_names)
            if "prime Water source line" in name
        )
        waste_index = next(
            index
            for index, name in enumerate(step_names[prime_index + 1 :], prime_index + 1)
            if "select Waste" in name
        )
        purge_index = next(
            index
            for index, name in enumerate(step_names[waste_index + 1 :], waste_index + 1)
            if "aspirate additional selector-waste purge air" in name
        )
        self.assertLess(prime_index, waste_index)
        self.assertLess(waste_index, purge_index)

    def test_short_transfers_select_destination_before_chase_air(self) -> None:
        mixing_vessel = RUNZE_PLUMBING.mixing_vessel
        if mixing_vessel is None:
            self.fail("Test plumbing requires a mixing-vessel destination")

        method_names = (
            "transfer_liquid_to_route_with_single_aspiration",
            "transfer_liquid_to_route_with_common_line_strokes",
        )
        for method_name in method_names:
            with self.subTest(method=method_name):
                fluidics, _, _, step_names = self._fluidics()
                transfer = getattr(fluidics, method_name)
                transfer(
                    source_port=RUNZE_PLUMBING.water.number,
                    destination_port=mixing_vessel.number,
                    volume_ml=0.1,
                    rpm=10,
                )

                aspiration_index = next(
                    index
                    for index, name in enumerate(step_names)
                    if "aspirate Water into common line" in name
                )
                destination_index = next(
                    index
                    for index, name in enumerate(step_names)
                    if "select Mixing vessel" in name
                )
                chase_index = next(
                    index
                    for index, name in enumerate(step_names)
                    if "aspirate destination-route chase air" in name
                )
                self.assertLess(aspiration_index, destination_index)
                self.assertLess(destination_index, chase_index)

    def test_common_line_stroke_margin_has_its_own_threshold(self) -> None:
        mixing_vessel = RUNZE_PLUMBING.mixing_vessel
        if mixing_vessel is None:
            self.fail("Test plumbing requires a mixing-vessel destination")

        fluidics, syringe, _, _ = self._fluidics()
        fluidics.transfer_liquid_to_route_with_common_line_strokes(
            source_port=RUNZE_PLUMBING.water.number,
            destination_port=mixing_vessel.number,
            volume_ml=0.55,
            rpm=10,
        )
        self.assertEqual(
            sum(
                movement[0] == "aspirate" and movement[3] == "inlet"
                for movement in syringe.movements
            ),
            1,
        )

        fluidics, syringe, _, _ = self._fluidics()
        fluidics.transfer_liquid_to_route_with_common_line_strokes(
            source_port=RUNZE_PLUMBING.water.number,
            destination_port=mixing_vessel.number,
            volume_ml=0.6,
            rpm=10,
        )
        self.assertEqual(
            sum(
                movement[0] == "aspirate" and movement[3] == "inlet"
                for movement in syringe.movements
            ),
            2,
        )

        with self.assertRaisesRegex(
            ValueError, "common_line_stroke_clearance_margin_ml"
        ):
            replace(
                RUNZE_PLUMBING,
                common_line_stroke_clearance_margin_ml=(
                    RUNZE_PLUMBING.common_line_dead_volume_ml
                ),
            )

    def test_mixture_diagnostics_distinguish_workflow_at_point_six_ml(self) -> None:
        arguments = {
            "plumbing": RUNZE_PLUMBING,
            "syringe_volume_ml": 5.0,
            "source_fractions": {RUNZE_PLUMBING.water.key: 1.0},
            "total_volume_ml": 0.6,
            "water_distribution": "single",
        }
        continuous = build_mixture_transfer_diagnostics(
            **arguments, use_common_line_strokes=False
        )
        stopped = build_mixture_transfer_diagnostics(
            **arguments, use_common_line_strokes=True
        )

        self.assertEqual(continuous[0].regime, "short_single_aspiration")
        self.assertEqual(continuous[0].liquid_stroke_count, 1)
        self.assertEqual(stopped[0].regime, "common_line_strokes")
        self.assertEqual(stopped[0].liquid_stroke_count, 2)

    def test_large_single_aspiration_empties_to_destination_before_air_chase(
        self,
    ) -> None:
        mixing_vessel = RUNZE_PLUMBING.mixing_vessel
        if mixing_vessel is None:
            self.fail("Test plumbing requires a mixing-vessel destination")
        mixing_route_volume_ml = mixing_vessel.destination_route_dead_volume_ml
        if mixing_route_volume_ml is None:
            self.fail("Test plumbing requires a mixing-vessel route volume")

        fluidics, syringe, selector, step_names = self._fluidics()
        volume_ml = RUNZE_PLUMBING.common_line_dead_volume_ml + 0.1

        fluidics.transfer_liquid_to_route_with_single_aspiration(
            source_port=RUNZE_PLUMBING.water.number,
            destination_port=mixing_vessel.number,
            volume_ml=volume_ml,
            rpm=10,
        )

        expected_chase_volume_ml = math.fsum(
            (
                RUNZE_PLUMBING.common_line_dead_volume_ml,
                mixing_route_volume_ml,
                RUNZE_PLUMBING.clearance_margin_ml,
            )
        )
        self.assertEqual(
            syringe.movements,
            [
                ("aspirate", volume_ml, 10, "inlet"),
                ("empty", None, 10, "inlet"),
                ("aspirate", expected_chase_volume_ml, 10, "outlet"),
                ("empty", None, 10, "inlet"),
            ],
        )
        self.assertEqual(
            selector.selected_ports, [RUNZE_PLUMBING.water.number, mixing_vessel.number]
        )
        aspiration_index = next(
            index for index, name in enumerate(step_names) if name == "aspirate Water"
        )
        destination_index = next(
            index
            for index, name in enumerate(step_names)
            if name == "select Mixing vessel"
        )
        first_empty_index = next(
            index
            for index, name in enumerate(step_names)
            if name == "dispense Water to Mixing vessel"
        )
        chase_index = next(
            index
            for index, name in enumerate(step_names)
            if name == "aspirate final chase air from outlet"
        )
        self.assertLess(aspiration_index, destination_index)
        self.assertLess(destination_index, first_empty_index)
        self.assertLess(first_empty_index, chase_index)

    def test_transfer_override_changes_only_inlet_aspiration_rpm(self) -> None:
        mixing_vessel = RUNZE_PLUMBING.mixing_vessel
        if mixing_vessel is None:
            self.fail("Test plumbing requires a mixing-vessel destination")

        method_volumes = (
            ("transfer_liquid_to_route_with_single_aspiration", 0.5),
            ("transfer_liquid_to_route_with_common_line_strokes", 2.0),
        )
        for method_name, volume_ml in method_volumes:
            with self.subTest(method=method_name):
                fluidics, syringe, _, _ = self._fluidics()
                transfer = getattr(fluidics, method_name)
                transfer(
                    source_port=RUNZE_PLUMBING.water.number,
                    destination_port=mixing_vessel.number,
                    volume_ml=volume_ml,
                    rpm=50,
                    inlet_aspiration_rpm=5,
                )

                inlet_aspirations = [
                    movement
                    for movement in syringe.movements
                    if movement[0] == "aspirate" and movement[3] == "inlet"
                ]
                other_movements = [
                    movement
                    for movement in syringe.movements
                    if movement not in inlet_aspirations
                ]
                self.assertTrue(inlet_aspirations)
                self.assertTrue(all(item[2] == 5 for item in inlet_aspirations))
                self.assertTrue(all(item[2] == 50 for item in other_movements))

    def test_mixture_override_changes_only_inlet_aspiration_rpm(self) -> None:
        method_names = (
            "prepare_mixture_with_single_aspiration_transfers",
            "prepare_mixture_with_common_line_stroke_transfers",
        )
        for method_name in method_names:
            with self.subTest(method=method_name):
                fluidics, syringe, _, _ = self._fluidics()
                prepare = getattr(fluidics, method_name)
                prepare(
                    source_fractions={RUNZE_PLUMBING.water.key: 1.0},
                    total_volume_ml=2.0,
                    rpm=50,
                    inlet_aspiration_rpm=5,
                )

                inlet_aspirations = [
                    movement
                    for movement in syringe.movements
                    if movement[0] == "aspirate" and movement[3] == "inlet"
                ]
                other_movements = [
                    movement
                    for movement in syringe.movements
                    if movement not in inlet_aspirations
                ]
                self.assertTrue(inlet_aspirations)
                self.assertTrue(all(item[2] == 5 for item in inlet_aspirations))
                self.assertTrue(all(item[2] == 50 for item in other_movements))

    def test_large_single_aspiration_mixture_component_uses_simplified_sequence(
        self,
    ) -> None:
        fluidics, syringe, _, _ = self._fluidics()
        volume_ml = RUNZE_PLUMBING.common_line_dead_volume_ml + 0.1

        fluidics.prepare_mixture_with_single_aspiration_transfers(
            source_fractions={RUNZE_PLUMBING.water.key: 1.0},
            total_volume_ml=volume_ml,
            rpm=10,
        )

        self.assertEqual(
            [movement[0] for movement in syringe.movements],
            ["aspirate", "empty", "aspirate", "empty"],
        )
        self.assertFalse(
            any(movement[0] == "dispense" for movement in syringe.movements)
        )

    def test_large_single_aspiration_chase_capacity_is_preflighted(self) -> None:
        mixing_vessel = RUNZE_PLUMBING.mixing_vessel
        if mixing_vessel is None:
            self.fail("Test plumbing requires a mixing-vessel destination")
        fluidics, syringe, selector, step_names = self._fluidics(capacity_ml=1.0)

        with self.assertRaisesRegex(ValueError, "final_air_chase_volume_ml"):
            fluidics.transfer_liquid_to_route_with_single_aspiration(
                source_port=RUNZE_PLUMBING.water.number,
                destination_port=mixing_vessel.number,
                volume_ml=0.8,
                rpm=10,
            )

        self.assertEqual(step_names, [])
        self.assertEqual(selector.selected_ports, [])
        self.assertEqual(syringe.movements, [])

    def test_mixing_plug_isolates_source_before_each_chase_air_aspiration(self) -> None:
        fluidics, _, _, step_names = self._fluidics()

        fluidics.pass_mixing_plug_through_flow_cell(
            volume_ml=0.1, rpm=12, flow_rpm=1, approach_rpm=5
        )

        prime_index = next(
            index
            for index, name in enumerate(step_names)
            if "prime mixing-vessel route" in name
        )
        waste_index = next(
            index for index, name in enumerate(step_names) if "select Waste" in name
        )
        purge_index = next(
            index
            for index, name in enumerate(step_names)
            if "aspirate additional selector-waste purge air" in name
        )
        plug_index = next(
            index
            for index, name in enumerate(step_names)
            if "aspirate continuous mixing-vessel plug" in name
        )
        flow_cell_index = next(
            index for index, name in enumerate(step_names) if "select Flow cell" in name
        )
        chase_index = next(
            index
            for index, name in enumerate(step_names)
            if "aspirate flow-cell chase air" in name
        )
        self.assertLess(prime_index, waste_index)
        self.assertLess(waste_index, purge_index)
        self.assertLess(plug_index, flow_cell_index)
        self.assertLess(flow_cell_index, chase_index)

    def test_continuous_flow_uses_approach_rpm_for_route_and_overtravel(self) -> None:
        fluidics, syringe, _, step_names = self._fluidics()

        fluidics.fill_flow_cell_from_mixing_vessel(
            volume_ml=0.2, rpm=12, flow_rpm=1, approach_rpm=5
        )

        flow_cell_dispenses = [
            move for move in syringe.movements if move[0] == "dispense"
        ]
        self.assertEqual(len(flow_cell_dispenses), 2)
        self.assertTrue(all(move[2:] == (5, "inlet") for move in flow_cell_dispenses))
        self.assertEqual(syringe.movements[-1], ("empty", None, 1, "inlet"))
        boundary_index = next(
            index for index, name in enumerate(step_names) if "boundary" in name
        )
        overtravel_index = next(
            index
            for index, name in enumerate(step_names)
            if "beyond route boundary" in name
        )
        self.assertLess(boundary_index, overtravel_index)

    def test_stopped_flow_uses_water_backed_strokes_and_measurement_stops(self) -> None:
        fluidics, syringe, selector, step_names = self._fluidics()
        measurements: list[tuple[int, int]] = []

        fluidics.run_stopped_flow_series_from_mixing_vessel(
            measurement_count=3,
            between_measurement_displacement_volume_ml=0.8,
            rinse_through_volume_ml=1.0,
            rpm=12,
            approach_rpm=4,
            hooks=StoppedFlowHooks(
                measure_at_stop=lambda sequence: measurements.append(
                    (sequence, len(syringe.movements))
                )
            ),
        )

        expected_stroke_limit_ml = math.floor(
            (
                RUNZE_PLUMBING.common_line_dead_volume_ml
                - RUNZE_PLUMBING.common_line_stroke_clearance_margin_ml
            )
            * syringe.steps_per_ml
        ) / syringe.steps_per_ml
        self.assertTrue(all(
            (move[1] or 0.0) <= expected_stroke_limit_ml + 1e-9
            for move in syringe.movements if move[0] == "dispense"
        ))

        self.assertEqual([sequence for sequence, _ in measurements], [1, 2, 3])
        self.assertLess(measurements[0][1], measurements[1][1])
        self.assertLess(measurements[1][1], measurements[2][1])
        flow_route_volume = cast(
            Any, RUNZE_PLUMBING.flow_cell
        ).destination_route_dead_volume_ml
        chase_volume = math.fsum(
            (flow_route_volume, RUNZE_PLUMBING.flow_cell_overtravel_volume_ml, 1.0)
        )
        self.assertTrue(
            any(
                move[0] == "aspirate"
                and math.isclose(move[1] or 0.0, chase_volume)
                and move[2:] == (12, "inlet")
                for move in syringe.movements
            )
        )
        flow_dispenses = [move for move in syringe.movements if move[0] == "dispense"]
        self.assertTrue(flow_dispenses)
        self.assertTrue(all(move[2:] == (4, "inlet") for move in flow_dispenses))
        expected_mixture_advance = math.fsum(
            (
                flow_route_volume,
                2 * RUNZE_PLUMBING.flow_cell_overtravel_volume_ml,
                2 * 0.8,
            )
        )
        self.assertTrue(
            math.isclose(
                math.fsum(move[1] or 0.0 for move in flow_dispenses),
                expected_mixture_advance,
                abs_tol=1e-9,
            )
        )
        self.assertEqual(syringe.movements[-1], ("empty", None, 4, "inlet"))
        self.assertEqual(
            selector.selected_ports[-1], cast(Any, RUNZE_PLUMBING.flow_cell).number
        )
        boundary_index = next(
            index
            for index, name in enumerate(step_names)
            if "approach flow-cell boundary" in name
        )
        initial_overtravel_index = next(
            index
            for index, name in enumerate(step_names)
            if "initial overtravel" in name
        )
        self.assertLess(boundary_index, initial_overtravel_index)
        self.assertTrue(any("final overtravel" in name for name in step_names))
        prime_index = next(
            index
            for index, name in enumerate(step_names)
            if "prime mixing-vessel route" in name
        )
        waste_index = next(
            index for index, name in enumerate(step_names) if "select Waste" in name
        )
        purge_index = next(
            index
            for index, name in enumerate(step_names)
            if "aspirate additional selector-waste purge air" in name
        )
        self.assertLess(prime_index, waste_index)
        self.assertLess(waste_index, purge_index)

    def test_single_stopped_flow_measurement_skips_between_displacement(self) -> None:
        fluidics, syringe, _, step_names = self._fluidics()
        measurements: list[tuple[int, int]] = []

        fluidics.run_stopped_flow_series_from_mixing_vessel(
            measurement_count=1,
            between_measurement_displacement_volume_ml=None,
            rinse_through_volume_ml=1.0,
            rpm=12,
            approach_rpm=4,
            hooks=StoppedFlowHooks(
                measure_at_stop=lambda sequence: measurements.append(
                    (sequence, len(syringe.movements))
                )
            ),
        )

        self.assertEqual([sequence for sequence, _ in measurements], [1])
        self.assertFalse(
            any("measurement 2 displacement" in name for name in step_names)
        )
        flow_route_volume = cast(
            Any, RUNZE_PLUMBING.flow_cell
        ).destination_route_dead_volume_ml
        flow_dispenses = [move for move in syringe.movements if move[0] == "dispense"]
        expected_mixture_advance = math.fsum(
            (flow_route_volume, 2 * RUNZE_PLUMBING.flow_cell_overtravel_volume_ml)
        )
        self.assertTrue(
            math.isclose(
                math.fsum(move[1] or 0.0 for move in flow_dispenses),
                expected_mixture_advance,
                abs_tol=1e-9,
            )
        )
        self.assertTrue(any("initial overtravel" in name for name in step_names))
        self.assertTrue(any("final overtravel" in name for name in step_names))
        self.assertEqual(syringe.movements[-1], ("empty", None, 4, "inlet"))

    def test_stopped_flow_capacity_failure_happens_before_movement(self) -> None:
        flow_route_volume = cast(
            Any, RUNZE_PLUMBING.flow_cell
        ).destination_route_dead_volume_ml
        chase_volume = math.fsum(
            (flow_route_volume, RUNZE_PLUMBING.flow_cell_overtravel_volume_ml, 1.0)
        )
        fluidics, syringe, selector, step_names = self._fluidics(
            capacity_ml=chase_volume
        )

        with self.assertRaisesRegex(ValueError, "no syringe capacity"):
            fluidics.run_stopped_flow_series_from_mixing_vessel(
                measurement_count=2,
                between_measurement_displacement_volume_ml=0.05,
                rinse_through_volume_ml=1.0,
                rpm=12,
                approach_rpm=4,
                hooks=StoppedFlowHooks(measure_at_stop=lambda _: None),
            )

        self.assertEqual(step_names, [])
        self.assertEqual(selector.selected_ports, [])
        self.assertEqual(syringe.movements, [])


class ExperimentLineMaintenanceStateTests(unittest.TestCase):
    @staticmethod
    def _plan() -> ExperimentPlan:
        cleanup = ExperimentCleanupSettings(
            mixing_rinse_volume_ml=1.0,
            mixing_rinse_cycles=1,
            flow_cell_rinse_through_volume_ml=1.0,
            source_line_additional_rinse_volume_ml=0.2,
        )
        return ExperimentPlan(
            source_configuration_revision=1,
            sources=(
                ExperimentPlanSource(
                    2, 9, "test_liquid_2", 0.25, None, None, None, None
                ),
                ExperimentPlanSource(None, 7, "water", 0.5, None, None, None, None),
                ExperimentPlanSource(
                    1, 6, "test_liquid_1", 0.25, None, None, None, None
                ),
            ),
            measurement_type="chronoamperometry",
            measurement_settings=ChronoamperometrySettings(0.1, 0.2, 1.0),
            measurement_name=None,
            handling_rpm=12,
            approach_rpm=4,
            flow_rpm=6,
            water_distribution="single",
            cleanup_settings=cleanup,
            estimated_measurement_duration_s=1.0,
            flow_rate_ml_min=1.0,
            measurement_flow_volume_ml=1.0,
            mixing_path_condition_volume_ml=1.0,
            flow_cell_load_volume_ml=1.0,
            mixing_vessel_reserve_volume_ml=0.2,
            total_mixture_volume_ml=2.0,
            substance_concentrations=(),
        )

    @staticmethod
    def _controller(
        *, fail_recovery: bool = False
    ) -> tuple[ExperimentController, _FakeRunzeController]:
        runze = _FakeRunzeController(fail_recovery=fail_recovery)
        controller = ExperimentController(
            runze_controller=cast(RunzeController, runze),
            emstat_controller=cast(Any, _FakeEmstatController()),
            repository=cast(ExperimentRepository, _FakeRepository()),
        )
        primed_at = datetime.now(UTC)
        controller._line_state_by_port = {
            9: _SourceLineIdentity(2, "primed", primed_at, "test"),
            7: _SourceLineIdentity(None, "primed", primed_at, "test"),
            6: _SourceLineIdentity(1, "primed", primed_at, "test"),
        }
        return controller, runze

    def test_three_stages_capture_ports_and_update_priming(self) -> None:
        controller, runze = self._controller()

        controller.recover_source_lines_and_clear_flow_cell(plan=self._plan())
        self.assertEqual(
            controller.snapshot.line_maintenance_phase, "sources_recovered"
        )
        self.assertEqual(controller.snapshot.line_maintenance_source_ports, (9, 6))
        self.assertEqual(
            [state.status for state in controller.snapshot.priming],
            ["air_filled", "primed", "air_filled"],
        )

        controller.rinse_recovered_source_lines()
        self.assertEqual(controller.snapshot.line_maintenance_phase, "sources_rinsed")

        controller.air_empty_maintained_source_lines()
        self.assertEqual(controller.snapshot.line_maintenance_phase, "complete")
        self.assertEqual(controller.snapshot.line_maintenance_source_ports, ())
        self.assertTrue(
            all(state.status == "air_filled" for state in controller.snapshot.priming)
        )
        self.assertEqual(
            runze.calls, [("recover", (9, 6)), ("rinse", (9, 6)), ("air", (9, 6))]
        )

    def test_stopped_flow_estimate_uses_route_intervals_and_two_overtravels(
        self,
    ) -> None:
        controller, _ = self._controller()
        base = self._plan()

        estimate = controller._estimate_experiment_protocol(
            measurement_settings=base.measurement_settings,
            handling_rpm=base.handling_rpm,
            flow_rpm=None,
            approach_rpm=base.approach_rpm,
            water_distribution=base.water_distribution,
            cleanup_settings=base.cleanup_settings,
            workflow_kind="stopped_flow",
            measurement_count=3,
            between_measurement_displacement_volume_ml=0.08,
        )

        mixing_route_ml = cast(
            Any, RUNZE_PLUMBING.mixing_vessel
        ).destination_route_dead_volume_ml
        flow_route_ml = cast(
            Any, RUNZE_PLUMBING.flow_cell
        ).destination_route_dead_volume_ml
        expected_condition_ml = math.fsum(
            (mixing_route_ml, RUNZE_PLUMBING.clearance_margin_ml)
        )
        expected_load_ml = math.fsum(
            (flow_route_ml, 2 * RUNZE_PLUMBING.flow_cell_overtravel_volume_ml, 2 * 0.08)
        )
        self.assertTrue(
            math.isclose(
                estimate.mixing_path_condition_volume_ml, expected_condition_ml
            )
        )
        self.assertTrue(
            math.isclose(estimate.flow_cell_load_volume_ml, expected_load_ml)
        )
        self.assertTrue(
            math.isclose(
                estimate.total_mixture_volume_ml,
                expected_condition_ml
                + expected_load_ml
                + RUNZE_PLUMBING.mixing_vessel_reserve_volume_ml,
            )
        )
        self.assertIsNone(estimate.flow_rate_ml_min)
        self.assertIsNone(estimate.measurement_flow_volume_ml)

    def test_experiment_prepare_forwards_inlet_aspiration_rpm(self) -> None:
        controller, runze = self._controller()
        plan = replace(self._plan(), inlet_aspiration_rpm=5)

        controller._prepare_continuous_flow_mixture(plan)
        self.assertEqual(
            cast(dict[str, Any], runze.mixture_arguments)["inlet_aspiration_rpm"], 5
        )
        self.assertEqual(
            cast(dict[str, Any], runze.mixture_arguments)["method"], "single"
        )

        controller._prepare_stopped_flow_mixture(
            replace(plan, workflow_kind="stopped_flow", flow_rpm=None)
        )
        self.assertEqual(
            cast(dict[str, Any], runze.mixture_arguments)["inlet_aspiration_rpm"], 5
        )
        self.assertEqual(
            cast(dict[str, Any], runze.mixture_arguments)["method"], "strokes"
        )

    def test_single_stopped_flow_estimate_excludes_between_displacement(self) -> None:
        controller, _ = self._controller()
        base = self._plan()

        estimate = controller._estimate_experiment_protocol(
            measurement_settings=base.measurement_settings,
            handling_rpm=base.handling_rpm,
            flow_rpm=None,
            approach_rpm=base.approach_rpm,
            water_distribution=base.water_distribution,
            cleanup_settings=base.cleanup_settings,
            workflow_kind="stopped_flow",
            measurement_count=1,
            between_measurement_displacement_volume_ml=None,
        )

        flow_route_ml = cast(
            Any, RUNZE_PLUMBING.flow_cell
        ).destination_route_dead_volume_ml
        expected_load_ml = math.fsum(
            (flow_route_ml, 2 * RUNZE_PLUMBING.flow_cell_overtravel_volume_ml)
        )
        self.assertTrue(
            math.isclose(estimate.flow_cell_load_volume_ml, expected_load_ml)
        )
        self.assertIsNone(estimate.between_measurement_displacement_volume_ml)

    def test_failed_first_stage_invalidates_only_non_water_sources(self) -> None:
        controller, _ = self._controller(fail_recovery=True)

        with self.assertRaisesRegex(RuntimeError, "simulated recovery failure"):
            controller.recover_source_lines_and_clear_flow_cell(plan=self._plan())

        self.assertEqual(
            controller.snapshot.line_maintenance_phase, "recovery_required"
        )
        status_by_port = {
            state.selector_port: state.status for state in controller.snapshot.priming
        }
        self.assertEqual(status_by_port, {9: "unknown", 7: "primed", 6: "unknown"})

    def test_acknowledging_recovery_preserves_confirmed_source_states(self) -> None:
        controller, _ = self._controller(fail_recovery=True)
        plan = self._plan()

        with self.assertRaisesRegex(RuntimeError, "simulated recovery failure"):
            controller.recover_source_lines_and_clear_flow_cell(plan=plan)

        controller.set_source_line_state(selector_port=9, status="air_filled")
        controller.set_source_line_state(selector_port=6, status="primed")
        expected_states = {
            state.selector_port: (state.status, state.source_stock_id)
            for state in controller.snapshot.priming
        }
        controller._prepared_mixture = PreparedMixture(mixture_id=1, plan=plan)
        controller._prepared_mixture_volume_state = "unknown"

        controller.acknowledge_experiment_recovery()

        actual_states = {
            state.selector_port: (state.status, state.source_stock_id)
            for state in controller.snapshot.priming
        }
        self.assertEqual(actual_states, expected_states)
        self.assertEqual(controller.snapshot.workflow_phase, "complete")
        self.assertEqual(controller.snapshot.line_maintenance_phase, "complete")
        self.assertEqual(controller.snapshot.line_maintenance_source_ports, ())
        self.assertIsNone(controller.prepared_mixture)
        self.assertIsNone(controller.prepared_mixture_volume_state)

    def test_recovery_acknowledgement_requires_recovery_state(self) -> None:
        controller, _ = self._controller()

        with self.assertRaisesRegex(RuntimeError, "recovery is not required"):
            controller.acknowledge_experiment_recovery()

    def test_prime_required_sources_skips_already_primed_water(self) -> None:
        controller, runze = self._controller()
        now = datetime.now(UTC)
        controller._line_state_by_port[9] = _SourceLineIdentity(
            None, "air_filled", now, "test"
        )

        self.assertTrue(controller.prime_required_sources(rpm=10))

        self.assertEqual(runze.prime_calls, [((9,), False)])
        self.assertTrue(
            all(state.status == "primed" for state in controller.snapshot.priming)
        )

    def test_unknown_enabled_source_cannot_be_primed(self) -> None:
        controller, runze = self._controller()
        controller._line_state_by_port.pop(9)

        with self.assertRaisesRegex(RuntimeError, "unknown physical contents"):
            controller.prime_required_sources(rpm=10)

        self.assertEqual(runze.prime_calls, [])

    def test_ready_phase_only_allows_unknown_disabled_line_correction(self) -> None:
        controller, _ = self._controller()
        now = datetime.now(UTC)
        controller._workflow_phase = "ready"
        controller._source_configuration = CurrentSourceConfiguration(
            revision=2,
            entries=(
                SourceConfigurationEntry(9, "test_liquid_2", 2, False),
                SourceConfigurationEntry(7, "water", None, True),
                SourceConfigurationEntry(6, "test_liquid_1", 1, True),
            ),
        )
        controller._line_state_by_port[9] = _SourceLineIdentity(
            None, "unknown", now, "test"
        )

        self.assertTrue(
            controller.source_line_state_editable(
                selector_port=9, proposed_enabled=True
            )
        )
        self.assertFalse(
            controller.source_line_state_editable(
                selector_port=9, proposed_enabled=False
            )
        )
        self.assertFalse(
            controller.source_line_state_editable(
                selector_port=6, proposed_enabled=True
            )
        )

    def test_enabled_unknown_line_cannot_be_saved(self) -> None:
        controller, _ = self._controller()
        now = datetime.now(UTC)
        controller._line_state_by_port[9] = _SourceLineIdentity(
            None, "unknown", now, "test"
        )
        old = CurrentSourceConfiguration(
            revision=2,
            entries=(
                SourceConfigurationEntry(9, "test_liquid_2", 2, False),
                SourceConfigurationEntry(7, "water", None, True),
                SourceConfigurationEntry(6, "test_liquid_1", 1, True),
            ),
        )
        enabled = (
            SourceConfigurationEntry(9, "test_liquid_2", 2, True),
            *old.entries[1:],
        )

        with self.assertRaisesRegex(RuntimeError, "must be confirmed"):
            controller._validate_source_configuration_transition(
                old_configuration=old, new_entries=enabled
            )

        controller._line_state_by_port[9] = _SourceLineIdentity(
            None, "air_filled", now, "test"
        )
        controller._validate_source_configuration_transition(
            old_configuration=old, new_entries=enabled
        )

    def test_stock_replacement_requires_air_filled_line(self) -> None:
        controller, _ = self._controller()
        replacement = (
            SourceConfigurationEntry(9, "test_liquid_2", 3, True),
            *controller.source_configuration.entries[1:],
        )

        with self.assertRaisesRegex(RuntimeError, "only while.*air-filled"):
            controller._validate_source_configuration_transition(
                old_configuration=controller.source_configuration,
                new_entries=replacement,
            )

    def test_stopped_flow_controller_persists_each_stationary_measurement(self) -> None:
        runze = _FakeRunzeController()
        emstat = _FakeEmstatController()
        repository = _FakeRepository()
        controller = ExperimentController(
            runze_controller=cast(RunzeController, runze),
            emstat_controller=cast(Any, emstat),
            repository=cast(ExperimentRepository, repository),
        )
        plan = replace(
            self._plan(),
            workflow_kind="stopped_flow",
            measurement_count=3,
            flow_rpm=None,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            between_measurement_displacement_volume_ml=0.08,
        )
        prepared = PreparedMixture(mixture_id=12, plan=plan)
        controller._prepared_mixture = prepared
        controller._prepared_mixture_volume_state = "prepared"
        controller._active_plan = plan

        def measure(*_: Any, **arguments: Any) -> Measurement:
            sequence = int(arguments["session_sequence_number"])
            emstat.measurement_number = sequence
            emstat.last_saved_session_path = Path(
                f"measurement_{sequence:03d}.pssession"
            )
            return cast(Measurement, object())

        cast(Any, controller)._measure = measure
        cast(Any, controller)._calculate_and_save_objective = lambda **_: None

        result = controller._run_stopped_flow_measurements(prepared_mixture=prepared)

        self.assertEqual(
            [item.measurement_id for item in result.measurements], [1, 2, 3]
        )
        self.assertEqual(
            [item["run_sequence_number"] for item in repository.created_measurements],
            [1, 2, 3],
        )
        self.assertTrue(
            all(
                item["protocol_key"] == "stopped_flow_stationary"
                for item in repository.created_measurements
            )
        )
        self.assertEqual(
            repository.measurement_statuses,
            [
                (1, "running"),
                (1, "completed"),
                (2, "running"),
                (2, "completed"),
                (3, "running"),
                (3, "completed"),
            ],
        )
        self.assertEqual(controller.prepared_mixture_volume_state, "flow_completed")
        self.assertEqual(controller.snapshot.measurement_sequence_number, 3)
        self.assertEqual(
            cast(dict[str, Any], runze.stopped_flow_arguments)[
                "between_measurement_displacement_volume_ml"
            ],
            0.08,
        )

    def test_single_stopped_flow_measurement_is_persisted_without_displacement(
        self,
    ) -> None:
        runze = _FakeRunzeController()
        emstat = _FakeEmstatController()
        repository = _FakeRepository()
        controller = ExperimentController(
            runze_controller=cast(RunzeController, runze),
            emstat_controller=cast(Any, emstat),
            repository=cast(ExperimentRepository, repository),
        )
        plan = replace(
            self._plan(),
            workflow_kind="stopped_flow",
            measurement_count=1,
            flow_rpm=None,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            between_measurement_displacement_volume_ml=None,
        )
        prepared = PreparedMixture(mixture_id=12, plan=plan)
        controller._prepared_mixture = prepared
        controller._prepared_mixture_volume_state = "prepared"
        controller._active_plan = plan

        def measure(*_: Any, **arguments: Any) -> Measurement:
            emstat.measurement_number = int(arguments["session_sequence_number"])
            emstat.last_saved_session_path = Path("measurement_001.pssession")
            return cast(Measurement, object())

        cast(Any, controller)._measure = measure
        cast(Any, controller)._calculate_and_save_objective = lambda **_: None

        result = controller._run_stopped_flow_measurements(prepared_mixture=prepared)

        self.assertEqual(len(result.measurements), 1)
        self.assertEqual(repository.created_measurements[0]["run_sequence_number"], 1)
        self.assertEqual(
            repository.measurement_statuses, [(1, "running"), (1, "completed")]
        )
        self.assertIsNone(
            cast(dict[str, Any], runze.stopped_flow_arguments)[
                "between_measurement_displacement_volume_ml"
            ]
        )

    def test_stopped_flow_failure_preserves_completed_rows_and_unknown_volume(
        self,
    ) -> None:
        runze = _FakeRunzeController()
        emstat = _FakeEmstatController()
        repository = _FakeRepository()
        controller = ExperimentController(
            runze_controller=cast(RunzeController, runze),
            emstat_controller=cast(Any, emstat),
            repository=cast(ExperimentRepository, repository),
        )
        plan = replace(
            self._plan(),
            workflow_kind="stopped_flow",
            measurement_count=3,
            flow_rpm=None,
            flow_rate_ml_min=None,
            measurement_flow_volume_ml=None,
            between_measurement_displacement_volume_ml=0.08,
        )
        prepared = PreparedMixture(mixture_id=12, plan=plan)
        controller._prepared_mixture = prepared
        controller._prepared_mixture_volume_state = "prepared"
        controller._active_plan = plan

        def measure(*_: Any, **arguments: Any) -> Measurement:
            sequence = int(arguments["session_sequence_number"])
            emstat.measurement_number = sequence
            if sequence == 2:
                raise RuntimeError("simulated measurement failure")
            emstat.last_saved_session_path = Path(
                f"measurement_{sequence:03d}.pssession"
            )
            return cast(Measurement, object())

        cast(Any, controller)._measure = measure
        cast(Any, controller)._calculate_and_save_objective = lambda **_: None

        with self.assertRaisesRegex(RuntimeError, "simulated measurement failure"):
            controller._run_stopped_flow_measurements(prepared_mixture=prepared)

        self.assertEqual(
            repository.measurement_statuses,
            [(1, "running"), (1, "completed"), (2, "running"), (2, "failed")],
        )
        self.assertEqual(len(repository.created_measurements), 2)
        self.assertEqual(controller.prepared_mixture_volume_state, "unknown")


if __name__ == "__main__":
    unittest.main()

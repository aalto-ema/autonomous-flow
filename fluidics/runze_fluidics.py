import math
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from common.validation import (
    require_integer_in_range,
    require_non_negative_finite_number,
    require_positive_finite_number,
    require_positive_integer,
)
from fluidics.runze_plumbing import RunzePlumbingConfig, SelectorPortConfig
from runze.sv07m import SV07M
from runze.sy01b import (
    SY01B,
    SY01B_MAX_DYNAMIC_SPEED_RPM,
    SY01B_MIN_DYNAMIC_SPEED_RPM,
    syringe_steps_from_volume_ml,
    syringe_volume_ml_from_steps,
)
from runze.types import RunzeDeviceTarget

StepAction = Callable[[], object]
RoutePlan = tuple[SelectorPortConfig, str, float]
RouteBatch = tuple[RoutePlan, ...]
MixtureTransferSpec = tuple[SelectorPortConfig, float, str]
WaterDistribution = Literal["single", "between_components"]
MixtureTransferRegime = Literal[
    "short_single_aspiration", "bulk_single_aspiration", "common_line_strokes"
]


def partition_stroke_steps(total_steps: int, maximum_steps: int) -> tuple[int, ...]:
    """Balance integer strokes, avoiding a tiny trailing remainder."""
    if total_steps < 1 or maximum_steps < 1:
        raise ValueError("Stroke counts must be positive")
    count = (total_steps + maximum_steps - 1) // maximum_steps
    base, extra = divmod(total_steps, count)
    return tuple(base + (index < extra) for index in range(count))


def common_line_maximum_steps(plumbing, ml_per_step: float, capacity_ml: float) -> int:
    limit = min(
        plumbing.common_line_dead_volume_ml
        - plumbing.common_line_stroke_clearance_margin_ml,
        capacity_ml,
    )
    return math.floor(limit / ml_per_step)


@dataclass(frozen=True)
class MixtureTransferDiagnostic:
    source_config_key: str
    selector_port: int
    requested_volume_ml: float
    syringe_steps: int
    nominal_volume_ml: float
    regime: MixtureTransferRegime
    liquid_stroke_count: int

    @property
    def rounding_error_percent(self) -> float:
        return (
            (self.nominal_volume_ml - self.requested_volume_ml)
            / self.requested_volume_ml
            * 100.0
        )


def _mixture_transfer_specs(
    *,
    plumbing: RunzePlumbingConfig,
    source_fractions: Mapping[str, float],
    total_volume_ml: float,
    water_distribution: WaterDistribution,
) -> tuple[SelectorPortConfig, tuple[MixtureTransferSpec, ...]]:
    """Validate and split a mixture recipe into physical source transfers."""

    if not isinstance(source_fractions, Mapping):
        raise TypeError("source_fractions must be a mapping")
    if not source_fractions:
        raise ValueError("source_fractions must not be empty")
    require_positive_finite_number(total_volume_ml, name="total_volume_ml")
    if water_distribution not in ("single", "between_components"):
        raise ValueError("water_distribution must be 'single' or 'between_components'")

    fractions_by_key: dict[str, float] = {}
    for source_key, fraction in source_fractions.items():
        if not isinstance(source_key, str) or not source_key.strip():
            raise ValueError("source fraction keys must be non-empty strings")
        source = plumbing.port_by_key(source_key)
        if not source.is_liquid_source:
            raise ValueError(
                f"Selector port {source.number} ({source.label}) is not a liquid source"
            )
        require_non_negative_finite_number(fraction, name=f"{source_key}_fraction")
        fractions_by_key[source.key] = float(fraction)

    fraction_sum = math.fsum(fractions_by_key.values())
    if not math.isclose(fraction_sum, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"source fractions must sum to 1; got {fraction_sum:.12g}")

    mixing_vessel = next(iter(plumbing.destinations_of_kind("mixing_vessel")))
    positive_sources = tuple(
        (source, fractions_by_key[source.key])
        for source in plumbing.liquid_sources
        if fractions_by_key.get(source.key, 0.0) > 0.0
    )
    transfer_specs: list[MixtureTransferSpec] = []
    if water_distribution == "single":
        transfer_specs.extend(
            (source, total_volume_ml * fraction, f"{source.key}_mixture_volume_ml")
            for source, fraction in positive_sources
        )
        return mixing_vessel, tuple(transfer_specs)

    water = plumbing.water
    water_fraction = fractions_by_key.get(water.key, 0.0)
    non_water_sources = tuple(
        (source, fraction)
        for source, fraction in positive_sources
        if source.number != water.number
    )
    if water_fraction <= 0.0:
        raise ValueError(
            "water_distribution='between_components' requires a positive water fraction"
        )
    if len(non_water_sources) < 2:
        raise ValueError(
            "water_distribution='between_components' requires at least two "
            "positive non-water components"
        )

    water_volume_ml = total_volume_ml * water_fraction
    water_portion_count = len(non_water_sources) - 1
    equal_water_portion_ml = water_volume_ml / water_portion_count
    water_portions_ml = [equal_water_portion_ml] * water_portion_count
    water_portions_ml[-1] = math.fsum(
        (water_volume_ml, -math.fsum(water_portions_ml[:-1]))
    )
    for index, (source, fraction) in enumerate(non_water_sources):
        transfer_specs.append(
            (source, total_volume_ml * fraction, f"{source.key}_mixture_volume_ml")
        )
        if index < water_portion_count:
            transfer_specs.append(
                (
                    water,
                    water_portions_ml[index],
                    f"{water.key}_mixture_portion_{index + 1}_volume_ml",
                )
            )
    return mixing_vessel, tuple(transfer_specs)


def build_mixture_transfer_diagnostics(
    *,
    plumbing: RunzePlumbingConfig,
    syringe_volume_ml: float,
    source_fractions: Mapping[str, float],
    total_volume_ml: float,
    water_distribution: WaterDistribution,
    use_common_line_strokes: bool,
) -> tuple[MixtureTransferDiagnostic, ...]:
    """Resolve the nominal syringe movements used to prepare one mixture."""

    mixing_vessel, transfer_specs = _mixture_transfer_specs(
        plumbing=plumbing,
        source_fractions=source_fractions,
        total_volume_ml=total_volume_ml,
        water_distribution=water_distribution,
    )
    route_volume_ml = mixing_vessel.destination_route_dead_volume_ml
    if route_volume_ml is None:
        raise RuntimeError("Mixing-vessel route dead volume is not configured")
    capacity_steps = syringe_steps_from_volume_ml(
        syringe_volume_ml, syringe_volume_ml=syringe_volume_ml
    )
    route_steps = syringe_steps_from_volume_ml(
        route_volume_ml + plumbing.clearance_margin_ml,
        syringe_volume_ml=syringe_volume_ml,
    )
    maximum_stroke_steps = common_line_maximum_steps(
        plumbing, syringe_volume_ml / capacity_steps, syringe_volume_ml
    )
    if maximum_stroke_steps < 1:
        raise ValueError("Common-line stroke limit cannot produce one syringe step")

    diagnostics: list[MixtureTransferDiagnostic] = []
    for source, requested_volume_ml, _ in transfer_specs:
        if use_common_line_strokes:
            liquid_steps = math.floor(
                requested_volume_ml * capacity_steps / syringe_volume_ml + 0.5
            )
            if liquid_steps < 1:
                raise ValueError(
                    f"{source.key}_mixture_volume_ml is too small to move even "
                    "one syringe step"
                )
            nominal_volume_ml = liquid_steps * syringe_volume_ml / capacity_steps
        else:
            liquid_steps = syringe_steps_from_volume_ml(
                requested_volume_ml, syringe_volume_ml=syringe_volume_ml
            )
            nominal_volume_ml = syringe_volume_ml_from_steps(
                liquid_steps, syringe_volume_ml=syringe_volume_ml
            )

        if not use_common_line_strokes:
            regime: MixtureTransferRegime = (
                "short_single_aspiration"
                if requested_volume_ml <= plumbing.common_line_dead_volume_ml
                else "bulk_single_aspiration"
            )
            stroke_count = 1
        elif (
            liquid_steps <= maximum_stroke_steps
            and liquid_steps + route_steps <= capacity_steps
        ):
            regime = "short_single_aspiration"
            stroke_count = 1
        else:
            regime = "common_line_strokes"
            stroke_count = (
                liquid_steps + maximum_stroke_steps - 1
            ) // maximum_stroke_steps
        diagnostics.append(
            MixtureTransferDiagnostic(
                source_config_key=source.key,
                selector_port=source.number,
                requested_volume_ml=requested_volume_ml,
                syringe_steps=liquid_steps,
                nominal_volume_ml=nominal_volume_ml,
                regime=regime,
                liquid_stroke_count=stroke_count,
            )
        )
    return tuple(diagnostics)


@dataclass(frozen=True)
class _SingleAspirationTransferPlan:
    source: SelectorPortConfig
    destination: SelectorPortConfig
    volume_ml: float
    continuous_plug: bool
    route_chase_air_ml: float = 0.0
    final_air_chase_volume_ml: float = 0.0


@dataclass(frozen=True)
class _CommonLineStrokeTransferPlan:
    source: SelectorPortConfig
    destination: SelectorPortConfig
    liquid_steps: int
    maximum_stroke_steps: int
    route_chase_steps: int
    combine_liquid_and_chase: bool


@dataclass(frozen=True)
class _SelectorWasteRinsePlan:
    water_batches: tuple[RouteBatch, ...]
    air_batches: tuple[RouteBatch, ...]
    outlet_purge_volume_ml: float


@dataclass(frozen=True)
class FlowPhaseHooks:
    before_flow: Callable[[], None]
    after_flow: Callable[[], None]
    flow_failed: Callable[[BaseException], None] | None = None
    before_fluid_handling: Callable[[], None] | None = None


@dataclass(frozen=True)
class StoppedFlowHooks:
    """Controller callbacks invoked at safe stopped-flow phase boundaries."""

    measure_at_stop: Callable[[int], None]
    before_fluid_handling: Callable[[], None] | None = None


class RunStep(Protocol):
    """Controller-provided function for running one physical step."""

    def __call__(
        self, *, name: str, target: RunzeDeviceTarget, action: StepAction
    ) -> None: ...


class RunzeFluidics:
    """Coordinated fluid handling using SY01B and SV07M.

    Public procedures assume that the controller has verified that the
    syringe starts at position 0.
    """

    def __init__(
        self,
        *,
        syringe: SY01B,
        selector: SV07M,
        plumbing: RunzePlumbingConfig,
        run_step: RunStep,
    ) -> None:
        self._syringe = syringe
        self._selector = selector
        self._plumbing = plumbing
        self._run_step = run_step

    @staticmethod
    def _step_name(name: str, suffix: str) -> str:
        suffix = suffix.strip()
        return name if not suffix else f"{name} {suffix}"

    def _require_syringe_capacity(self, volume_ml: float, *, name: str) -> None:
        require_positive_finite_number(volume_ml, name=name)

        if volume_ml > self._syringe.syringe_volume_ml:
            raise ValueError(
                f"{name} ({volume_ml:g} ml) exceeds syringe capacity "
                f"({self._syringe.syringe_volume_ml:g} ml)"
            )

    def _require_syringe_movement_volume(self, volume_ml: float, *, name: str) -> int:
        """Require one volume to fit and produce a valid syringe movement."""

        self._require_syringe_capacity(volume_ml, name=name)

        try:
            steps = self._syringe.steps_from_volume_ml(volume_ml)
        except ValueError as error:
            raise ValueError(
                f"{name} cannot be represented as a syringe movement: {error}"
            ) from error

        return steps

    @staticmethod
    def _resolve_inlet_aspiration_rpm(
        *, rpm: int, inlet_aspiration_rpm: int | None
    ) -> int:
        require_positive_integer(rpm, name="rpm")
        if inlet_aspiration_rpm is None:
            return rpm
        require_integer_in_range(
            inlet_aspiration_rpm,
            name="inlet_aspiration_rpm",
            minimum=SY01B_MIN_DYNAMIC_SPEED_RPM,
            maximum=SY01B_MAX_DYNAMIC_SPEED_RPM,
        )
        return inlet_aspiration_rpm

    def _require_liquid_source(self, port_number: int) -> SelectorPortConfig:
        port = self._plumbing.port_by_number(port_number)

        if not port.is_liquid_source:
            raise ValueError(
                f"Selector port {port.number} ({port.label}) is not a liquid source"
            )

        return port

    def _ordered_non_water_sources(
        self, source_ports: Collection[int]
    ) -> tuple[SelectorPortConfig, ...]:
        requested_ports = tuple(source_ports)
        if not requested_ports:
            raise ValueError("source_ports must include at least one non-water source")
        if len(set(requested_ports)) != len(requested_ports):
            raise ValueError("source_ports must not contain duplicates")

        sources = tuple(self._require_liquid_source(port) for port in requested_ports)
        water_port = self._plumbing.water.number
        if any(source.number == water_port for source in sources):
            raise ValueError("source_ports must not include the water source")

        selected_ports = set(requested_ports)
        return tuple(
            source
            for source in self._plumbing.liquid_sources
            if source.number in selected_ports
        )

    def _require_destination(
        self, *, source_port: int, destination_port: int
    ) -> SelectorPortConfig:
        destination = self._plumbing.port_by_number(destination_port)

        if not destination.is_destination:
            raise ValueError(
                f"Selector port {destination.number} ({destination.label}) "
                "is not configured as a destination"
            )

        if destination_port == source_port:
            raise ValueError("source_port and destination_port must be different")

        return destination

    def _require_mixing_vessel(self) -> tuple[SelectorPortConfig, float]:
        mixing_vessels = self._plumbing.destinations_of_kind("mixing_vessel")

        if len(mixing_vessels) != 1:
            raise RuntimeError(
                "Exactly one mixing vessel destination must be configured"
            )

        mixing_vessel = mixing_vessels[0]
        mixing_route_dead_volume_ml = mixing_vessel.destination_route_dead_volume_ml

        if mixing_route_dead_volume_ml is None:
            raise RuntimeError(
                "Mixing vessel destination route dead volume is not configured"
            )

        return mixing_vessel, mixing_route_dead_volume_ml

    def _require_mixing_vessel_flow_path(
        self,
    ) -> tuple[SelectorPortConfig, SelectorPortConfig, float, float]:
        mixing_vessel, mixing_route_dead_volume_ml = self._require_mixing_vessel()
        flow_cell = self._plumbing.flow_cell

        if flow_cell is None:
            raise RuntimeError("Flow cell destination is not configured")

        flow_cell = self._require_destination(
            source_port=mixing_vessel.number, destination_port=flow_cell.number
        )

        flow_cell_route_dead_volume_ml = flow_cell.destination_route_dead_volume_ml
        if flow_cell_route_dead_volume_ml is None:
            raise RuntimeError(
                "Flow cell destination route dead volume is not configured"
            )

        return (
            mixing_vessel,
            flow_cell,
            mixing_route_dead_volume_ml,
            flow_cell_route_dead_volume_ml,
        )

    def _destination_route_extension_ml(self, destination: SelectorPortConfig) -> float:
        if destination.destination_kind == "flow_cell":
            return self._plumbing.flow_cell_overtravel_volume_ml

        return self._plumbing.clearance_margin_ml

    @staticmethod
    def _resolve_approach_rpm(*, approach_rpm: int | None, flow_rpm: int) -> int:
        require_positive_integer(flow_rpm, name="flow_rpm")

        if approach_rpm is None:
            return flow_rpm

        require_positive_integer(approach_rpm, name="approach_rpm")
        return approach_rpm

    def _require_flow_cell_liquid_dispense_steps(
        self,
        *,
        route_volume_ml: float,
        overtravel_volume_ml: float,
        requested_flow_volume_ml: float,
    ) -> None:
        """Preflight a liquid-filled flow-cell dispense after syringe rounding.

        The route approach and overtravel are separate syringe movements. The
        remaining rounded syringe content must therefore still contain at least
        one step for the requested flow phase.
        """

        total_volume_ml = math.fsum(
            (route_volume_ml, overtravel_volume_ml, requested_flow_volume_ml)
        )
        total_steps = self._require_syringe_movement_volume(
            total_volume_ml, name="flow_cell_aspiration_volume_ml"
        )
        route_steps = (
            self._require_syringe_movement_volume(
                route_volume_ml, name="flow_cell_route_approach_volume_ml"
            )
            if route_volume_ml > 0
            else 0
        )
        overtravel_steps = (
            self._require_syringe_movement_volume(
                overtravel_volume_ml, name="flow_cell_overtravel_volume_ml"
            )
            if overtravel_volume_ml > 0
            else 0
        )

        if total_steps - route_steps - overtravel_steps < 1:
            raise ValueError(
                "requested flow volume is too small after syringe-step rounding"
            )

    def _require_flow_cell_plug_dispense_steps(
        self,
        *,
        route_volume_ml: float,
        plug_volume_ml: float,
        overtravel_volume_ml: float,
    ) -> None:
        """Preflight a route approach followed by one plug-and-air dispense."""

        plug_steps = self._require_syringe_movement_volume(
            plug_volume_ml, name="flow_cell_plug_volume_ml"
        )
        chase_volume_ml = math.fsum((route_volume_ml, overtravel_volume_ml))
        chase_steps = self._require_syringe_movement_volume(
            chase_volume_ml, name="flow_cell_chase_air_ml"
        )
        total_steps = plug_steps + chase_steps
        syringe_capacity_steps = self._syringe.steps_from_volume_ml(
            self._syringe.syringe_volume_ml
        )

        if total_steps > syringe_capacity_steps:
            raise ValueError(
                "flow-cell plug and chase exceed syringe capacity after "
                "syringe-step rounding"
            )

        route_steps = (
            self._require_syringe_movement_volume(
                route_volume_ml, name="flow_cell_route_approach_volume_ml"
            )
            if route_volume_ml > 0
            else 0
        )

        if total_steps - route_steps < 1:
            raise ValueError(
                "plug volume plus flow-cell overtravel is too small after "
                "syringe-step rounding"
            )

    def _condition_common_line_from_source(
        self, *, source: SelectorPortConfig, rpm: int, suffix: str = ""
    ) -> None:
        """Fill the common line from source and discard the transition via OUT.

        The syringe starts and finishes empty.
        """

        if source.source_kind is None:
            raise ValueError(
                f"Selector port {source.number} ({source.label}) is not a source"
            )

        require_positive_integer(rpm, name="rpm")

        condition_volume_ml = (
            self._plumbing.common_line_dead_volume_ml
            + self._plumbing.clearance_margin_ml
        )

        self._require_syringe_capacity(
            condition_volume_ml, name="common_line_condition_volume_ml"
        )

        self._run_step(
            name=self._step_name(f"select {source.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(source.number),
        )

        self._run_step(
            name=self._step_name(f"condition common line with {source.label}", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=condition_volume_ml, rpm=rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name("discard common-line transition to outlet", suffix),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="outlet"),
        )

    def _build_route_plans(
        self,
        *,
        source_lines: tuple[SelectorPortConfig, ...] = (),
        destinations: tuple[SelectorPortConfig, ...] = (),
        extra_volume_ml: float,
    ) -> tuple[RoutePlan, ...]:
        """Build selector-route volumes with route-specific extensions."""

        require_non_negative_finite_number(extra_volume_ml, name="extra_volume_ml")

        route_plans: list[RoutePlan] = []

        for source in source_lines:
            source_dead_volume_ml = source.source_line_dead_volume_ml

            if source_dead_volume_ml is None:
                raise RuntimeError(
                    f"Source line dead volume is not configured for {source.label}"
                )

            route_volume_ml = (
                source_dead_volume_ml
                + self._plumbing.clearance_margin_ml
                + extra_volume_ml
            )
            require_positive_finite_number(
                route_volume_ml, name=f"{source.key}_source_route_volume_ml"
            )
            route_plans.append((source, f"{source.label} source line", route_volume_ml))

        for destination in destinations:
            route_dead_volume_ml = destination.destination_route_dead_volume_ml

            if route_dead_volume_ml is None:
                raise RuntimeError(
                    "Destination route dead volume is not configured "
                    f"for {destination.label}"
                )

            route_volume_ml = (
                route_dead_volume_ml
                + self._destination_route_extension_ml(destination)
                + extra_volume_ml
            )
            require_positive_finite_number(
                route_volume_ml, name=f"{destination.key}_route_volume_ml"
            )
            route_plans.append(
                (destination, f"{destination.label} destination route", route_volume_ml)
            )

        return tuple(route_plans)

    def _build_route_batches(
        self, route_plans: tuple[RoutePlan, ...]
    ) -> tuple[RouteBatch, ...]:
        """Fit complete routes into syringe-sized batches when possible."""

        if not route_plans:
            raise RuntimeError("No selector routes are configured for this procedure")

        syringe_capacity_ml = self._syringe.syringe_volume_ml
        require_positive_finite_number(syringe_capacity_ml, name="syringe_capacity_ml")

        tolerance_ml = max(1e-12, syringe_capacity_ml * 1e-12)
        unverified_batches: list[RouteBatch] = []
        current_batch: list[RoutePlan] = []
        current_volume_ml = 0.0

        for port, route_label, route_volume_ml in route_plans:
            require_positive_finite_number(
                route_volume_ml, name=f"{port.key}_route_volume_ml"
            )
            remaining_volume_ml = route_volume_ml

            if current_batch and (
                remaining_volume_ml > syringe_capacity_ml + tolerance_ml
                or current_volume_ml + remaining_volume_ml
                > syringe_capacity_ml + tolerance_ml
            ):
                unverified_batches.append(tuple(current_batch))
                current_batch = []
                current_volume_ml = 0.0

            while remaining_volume_ml > syringe_capacity_ml + tolerance_ml:
                unverified_batches.append(((port, route_label, syringe_capacity_ml),))
                remaining_volume_ml = math.fsum(
                    (remaining_volume_ml, -syringe_capacity_ml)
                )

            if remaining_volume_ml > tolerance_ml:
                remaining_volume_ml = min(remaining_volume_ml, syringe_capacity_ml)
                current_batch.append((port, route_label, remaining_volume_ml))
                current_volume_ml = math.fsum(plan[2] for plan in current_batch)

                if math.isclose(
                    current_volume_ml,
                    syringe_capacity_ml,
                    rel_tol=0.0,
                    abs_tol=tolerance_ml,
                ):
                    unverified_batches.append(tuple(current_batch))
                    current_batch = []
                    current_volume_ml = 0.0

        if current_batch:
            unverified_batches.append(tuple(current_batch))

        rounding_safe_batches: list[RouteBatch] = []

        for batch in unverified_batches:
            route_steps = tuple(
                self._syringe.steps_from_volume_ml(plan[2]) for plan in batch
            )
            batch_steps = self._syringe.steps_from_volume_ml(
                self._batch_volume_ml(batch)
            )

            if sum(route_steps[:-1]) >= batch_steps:
                rounding_safe_batches.extend((plan,) for plan in batch)
            else:
                rounding_safe_batches.append(batch)

        return tuple(rounding_safe_batches)

    def _batch_volume_ml(self, batch: RouteBatch) -> float:
        volume_ml = math.fsum(plan[2] for plan in batch)
        syringe_capacity_ml = self._syringe.syringe_volume_ml

        if math.isclose(
            volume_ml,
            syringe_capacity_ml,
            rel_tol=0.0,
            abs_tol=max(1e-12, syringe_capacity_ml * 1e-12),
        ):
            return syringe_capacity_ml

        return volume_ml

    def _run_route_batches(
        self,
        *,
        source: SelectorPortConfig,
        batches: tuple[RouteBatch, ...],
        rpm: int,
        operation: str,
        suffix: str = "",
    ) -> None:
        """Aspirate each batch and dispense it across its planned routes."""

        require_positive_integer(rpm, name="rpm")

        for batch_index, batch in enumerate(batches, start=1):
            batch_volume_ml = self._batch_volume_ml(batch)
            self._require_syringe_capacity(
                batch_volume_ml, name=f"{operation}_batch_volume_ml"
            )
            batch_suffix = f"{suffix} (batch {batch_index}/{len(batches)})".strip()

            self._run_step(
                name=self._step_name(
                    f"select {source.label} for route batch", batch_suffix
                ),
                target="sv07m",
                action=lambda source=source: self._selector.ensure_port(source.number),
            )

            self._run_step(
                name=self._step_name(
                    f"aspirate {batch_volume_ml:g} ml {source.label}", batch_suffix
                ),
                target="sy01b",
                action=lambda batch_volume_ml=batch_volume_ml: (
                    self._syringe.aspirate_ml(
                        volume_ml=batch_volume_ml, rpm=rpm, source="inlet"
                    )
                ),
            )

            for route_index, (port, route_label, route_volume_ml) in enumerate(
                batch, start=1
            ):
                self._run_step(
                    name=self._step_name(f"select {port.label}", batch_suffix),
                    target="sv07m",
                    action=lambda port=port: self._selector.ensure_port(port.number),
                )

                if route_index == len(batch):
                    self._run_step(
                        name=self._step_name(
                            f"{operation} {route_label} with remaining "
                            f"{source.label} (nominal {route_volume_ml:g} ml)",
                            batch_suffix,
                        ),
                        target="sy01b",
                        action=lambda: self._syringe.empty_syringe(
                            rpm=rpm, destination="inlet"
                        ),
                    )
                else:
                    self._run_step(
                        name=self._step_name(
                            f"{operation} {route_label} with "
                            f"{route_volume_ml:g} ml {source.label}",
                            batch_suffix,
                        ),
                        target="sy01b",
                        action=lambda route_volume_ml=route_volume_ml: (
                            self._syringe.dispense_ml(
                                volume_ml=route_volume_ml, rpm=rpm, destination="inlet"
                            )
                        ),
                    )

    def _build_single_aspiration_transfer_plan(
        self,
        *,
        source_port: int,
        destination_port: int,
        volume_ml: float,
        volume_name: str = "volume_ml",
    ) -> _SingleAspirationTransferPlan:
        """Resolve and preflight one single-liquid-aspiration transfer."""

        source = self._require_liquid_source(source_port)
        destination = self._require_destination(
            source_port=source.number, destination_port=destination_port
        )

        require_positive_finite_number(volume_ml, name=volume_name)

        route_dead_volume_ml = destination.destination_route_dead_volume_ml
        if route_dead_volume_ml is None:
            raise RuntimeError(
                "Destination route dead volume is not configured "
                f"for {destination.label}"
            )

        common_dead_volume_ml = self._plumbing.common_line_dead_volume_ml
        destination_route_extension_ml = self._destination_route_extension_ml(
            destination
        )
        syringe_capacity_steps = self._syringe.steps_from_volume_ml(
            self._syringe.syringe_volume_ml
        )

        if volume_ml <= common_dead_volume_ml:
            route_chase_air_ml = route_dead_volume_ml + destination_route_extension_ml
            required_syringe_volume_ml = volume_ml + route_chase_air_ml

            self._require_syringe_capacity(
                required_syringe_volume_ml, name="required_syringe_volume_ml"
            )
            liquid_steps = self._require_syringe_movement_volume(
                volume_ml, name=volume_name
            )
            chase_steps = self._require_syringe_movement_volume(
                route_chase_air_ml, name="route_chase_air_ml"
            )

            if liquid_steps + chase_steps > syringe_capacity_steps:
                raise ValueError(
                    "The rounded liquid and route-chase movements exceed "
                    "syringe capacity"
                )

            return _SingleAspirationTransferPlan(
                source=source,
                destination=destination,
                volume_ml=volume_ml,
                continuous_plug=True,
                route_chase_air_ml=route_chase_air_ml,
            )

        final_air_chase_volume_ml = (
            common_dead_volume_ml
            + route_dead_volume_ml
            + destination_route_extension_ml
        )

        self._require_syringe_movement_volume(volume_ml, name=volume_name)
        self._require_syringe_movement_volume(
            final_air_chase_volume_ml, name="final_air_chase_volume_ml"
        )

        return _SingleAspirationTransferPlan(
            source=source,
            destination=destination,
            volume_ml=volume_ml,
            continuous_plug=False,
            final_air_chase_volume_ml=final_air_chase_volume_ml,
        )

    def _build_common_line_stroke_transfer_plan(
        self,
        *,
        source_port: int,
        destination_port: int,
        volume_ml: float,
        volume_name: str = "volume_ml",
    ) -> _CommonLineStrokeTransferPlan:
        """Resolve and fully preflight one common-line-stroke transfer."""

        source = self._require_liquid_source(source_port)
        destination = self._require_destination(
            source_port=source.number, destination_port=destination_port
        )

        require_positive_finite_number(volume_ml, name=volume_name)

        route_dead_volume_ml = destination.destination_route_dead_volume_ml
        if route_dead_volume_ml is None:
            raise RuntimeError(
                "Destination route dead volume is not configured "
                f"for {destination.label}"
            )

        maximum_stroke_steps = common_line_maximum_steps(
            self._plumbing, self._syringe.ml_per_step, self._syringe.syringe_volume_ml
        )

        if maximum_stroke_steps < 1:
            raise ValueError(
                "common_line_dead_volume_ml minus "
                "common_line_stroke_clearance_margin_ml is too small to produce "
                "one syringe step"
            )

        liquid_steps = math.floor(volume_ml * self._syringe.steps_per_ml + 0.5)
        if liquid_steps < 1:
            raise ValueError(
                f"{volume_name} is too small to move even one syringe step"
            )

        route_chase_air_ml = (
            route_dead_volume_ml + self._destination_route_extension_ml(destination)
        )
        route_chase_steps = self._require_syringe_movement_volume(
            route_chase_air_ml, name="route_chase_air_ml"
        )
        syringe_capacity_steps = self._syringe.steps_from_volume_ml(
            self._syringe.syringe_volume_ml
        )

        return _CommonLineStrokeTransferPlan(
            source=source,
            destination=destination,
            liquid_steps=liquid_steps,
            maximum_stroke_steps=maximum_stroke_steps,
            route_chase_steps=route_chase_steps,
            combine_liquid_and_chase=(
                liquid_steps <= maximum_stroke_steps
                and liquid_steps + route_chase_steps <= syringe_capacity_steps
            ),
        )

    def _build_mixture_transfer_specs(
        self,
        *,
        source_fractions: Mapping[str, float],
        total_volume_ml: float,
        water_distribution: WaterDistribution,
    ) -> tuple[SelectorPortConfig, tuple[MixtureTransferSpec, ...]]:
        """Validate a mixture recipe and return ordered component transfers."""
        return _mixture_transfer_specs(
            plumbing=self._plumbing,
            source_fractions=source_fractions,
            total_volume_ml=total_volume_ml,
            water_distribution=water_distribution,
        )

    def _build_single_aspiration_mixture_transfer_plans(
        self,
        *,
        source_fractions: Mapping[str, float],
        total_volume_ml: float,
        water_distribution: WaterDistribution,
    ) -> tuple[_SingleAspirationTransferPlan, ...]:
        """Preflight every single-aspiration transfer in a mixture recipe."""

        mixing_vessel, transfer_specs = self._build_mixture_transfer_specs(
            source_fractions=source_fractions,
            total_volume_ml=total_volume_ml,
            water_distribution=water_distribution,
        )

        return tuple(
            self._build_single_aspiration_transfer_plan(
                source_port=source.number,
                destination_port=mixing_vessel.number,
                volume_ml=volume_ml,
                volume_name=volume_name,
            )
            for source, volume_ml, volume_name in transfer_specs
        )

    def _build_common_line_stroke_mixture_transfer_plans(
        self,
        *,
        source_fractions: Mapping[str, float],
        total_volume_ml: float,
        water_distribution: WaterDistribution,
    ) -> tuple[_CommonLineStrokeTransferPlan, ...]:
        """Preflight every common-line-stroke transfer in one mixture recipe."""

        mixing_vessel, transfer_specs = self._build_mixture_transfer_specs(
            source_fractions=source_fractions,
            total_volume_ml=total_volume_ml,
            water_distribution=water_distribution,
        )

        return tuple(
            self._build_common_line_stroke_transfer_plan(
                source_port=source.number,
                destination_port=mixing_vessel.number,
                volume_ml=volume_ml,
                volume_name=volume_name,
            )
            for source, volume_ml, volume_name in transfer_specs
        )

    def _run_single_aspiration_transfer_plan(
        self,
        *,
        plan: _SingleAspirationTransferPlan,
        rpm: int,
        inlet_aspiration_rpm: int,
        suffix: str = "",
    ) -> None:
        """Execute one already-preflighted single-aspiration transfer plan."""

        source = plan.source
        destination = plan.destination

        self._run_step(
            name=self._step_name(f"select {source.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(source.number),
        )

        if plan.continuous_plug:
            self._run_step(
                name=self._step_name(
                    f"aspirate {source.label} into common line", suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.aspirate_ml(
                    volume_ml=plan.volume_ml, rpm=inlet_aspiration_rpm, source="inlet"
                ),
            )

            self._run_step(
                name=self._step_name(f"select {destination.label}", suffix),
                target="sv07m",
                action=lambda: self._selector.ensure_port(destination.number),
            )

            self._run_step(
                name=self._step_name(
                    "aspirate destination-route chase air from outlet", suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.aspirate_ml(
                    volume_ml=plan.route_chase_air_ml, rpm=rpm, source="outlet"
                ),
            )

            self._run_step(
                name=self._step_name(
                    f"air-chase {source.label} through {destination.label}", suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.empty_syringe(
                    rpm=rpm, destination="inlet"
                ),
            )

            return

        self._run_step(
            name=self._step_name(f"aspirate {source.label}", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=plan.volume_ml, rpm=inlet_aspiration_rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name(f"select {destination.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(destination.number),
        )

        self._run_step(
            name=self._step_name(
                f"dispense {source.label} to {destination.label}", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="inlet"),
        )

        self._run_step(
            name=self._step_name("aspirate final chase air from outlet", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=plan.final_air_chase_volume_ml, rpm=rpm, source="outlet"
            ),
        )

        self._run_step(
            name=self._step_name(
                f"air-chase remaining liquid through {destination.label}", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="inlet"),
        )

    def _run_common_line_stroke_transfer_plan(
        self,
        *,
        plan: _CommonLineStrokeTransferPlan,
        rpm: int,
        inlet_aspiration_rpm: int,
        suffix: str = "",
    ) -> None:
        """Execute one already-preflighted common-line-stroke transfer plan."""

        source = plan.source
        destination = plan.destination
        route_chase_volume_ml = plan.route_chase_steps * self._syringe.ml_per_step

        if plan.combine_liquid_and_chase:
            self._run_single_aspiration_transfer_plan(
                plan=_SingleAspirationTransferPlan(
                    source=source,
                    destination=destination,
                    volume_ml=plan.liquid_steps * self._syringe.ml_per_step,
                    continuous_plug=True,
                    route_chase_air_ml=route_chase_volume_ml,
                ),
                rpm=rpm,
                inlet_aspiration_rpm=inlet_aspiration_rpm,
                suffix=suffix,
            )
            return

        stroke_count = (
            plan.liquid_steps + plan.maximum_stroke_steps - 1
        ) // plan.maximum_stroke_steps
        remaining_steps = plan.liquid_steps

        for stroke_index, stroke_steps in enumerate(
            partition_stroke_steps(plan.liquid_steps, plan.maximum_stroke_steps),
            start=1,
        ):
            stroke_volume_ml = stroke_steps * self._syringe.ml_per_step
            stroke_suffix = (
                f"{suffix} (liquid stroke {stroke_index}/{stroke_count})".strip()
            )

            self._run_step(
                name=self._step_name(f"select {source.label}", stroke_suffix),
                target="sv07m",
                action=lambda: self._selector.ensure_port(source.number),
            )

            self._run_step(
                name=self._step_name(
                    f"aspirate {stroke_volume_ml:g} ml {source.label}", stroke_suffix
                ),
                target="sy01b",
                action=lambda stroke_volume_ml=stroke_volume_ml: (
                    self._syringe.aspirate_ml(
                        volume_ml=stroke_volume_ml,
                        rpm=inlet_aspiration_rpm,
                        source="inlet",
                    )
                ),
            )

            self._run_step(
                name=self._step_name(f"select {destination.label}", stroke_suffix),
                target="sv07m",
                action=lambda: self._selector.ensure_port(destination.number),
            )

            self._run_step(
                name=self._step_name(
                    f"dispense {source.label} to {destination.label}", stroke_suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.empty_syringe(
                    rpm=rpm, destination="inlet"
                ),
            )

            remaining_steps -= stroke_steps

        self._run_step(
            name=self._step_name(
                "aspirate destination-route chase air from outlet", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=route_chase_volume_ml, rpm=rpm, source="outlet"
            ),
        )

        self._run_step(
            name=self._step_name(
                f"air-chase {source.label} through {destination.label}", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="inlet"),
        )

    def _outlet_purge_volume_ml(self) -> float:
        outlet_purge_volume_ml = (
            self._plumbing.syringe_outlet_to_waste_dead_volume_ml
            + self._plumbing.clearance_margin_ml
        )
        self._require_syringe_capacity(
            outlet_purge_volume_ml, name="outlet_purge_volume_ml"
        )
        return outlet_purge_volume_ml

    def _purge_syringe_outlet_with_air(
        self, *, volume_ml: float, rpm: int, suffix: str = ""
    ) -> None:
        air = self._plumbing.air

        self._run_step(
            name=self._step_name("select air for outlet purge", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(air.number),
        )

        self._run_step(
            name=self._step_name("aspirate pure air for outlet purge", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=volume_ml, rpm=rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name("dispense all outlet-purge air", suffix),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="outlet"),
        )

    def transfer_liquid_to_route_with_single_aspiration(
        self,
        *,
        source_port: int,
        destination_port: int,
        volume_ml: float,
        rpm: int,
        inlet_aspiration_rpm: int | None = None,
        suffix: str = "",
    ) -> None:
        """Transfer liquid with one source aspiration and finish with an air chase.

        Preconditions:
        - The syringe starts empty.
        - The source line is primed.
        - The common line, destination route, and SY01B OUT line contain air.

        For volumes up to the common-line dead volume, the liquid and route
        chase fit in one syringe movement. For larger volumes, the syringe is
        emptied toward the destination before the final air chase is aspirated.
        inlet_aspiration_rpm overrides rpm only for source-liquid aspiration
        through IN; None uses rpm for every movement.

        The final air chase delivers all nominal liquid and leaves the syringe,
        common line, destination route, and SY01B OUT line air-filled.
        """

        resolved_aspiration_rpm = self._resolve_inlet_aspiration_rpm(
            rpm=rpm, inlet_aspiration_rpm=inlet_aspiration_rpm
        )
        plan = self._build_single_aspiration_transfer_plan(
            source_port=source_port,
            destination_port=destination_port,
            volume_ml=volume_ml,
        )
        self._run_single_aspiration_transfer_plan(
            plan=plan,
            rpm=rpm,
            inlet_aspiration_rpm=resolved_aspiration_rpm,
            suffix=suffix,
        )

    def transfer_liquid_to_route_with_common_line_strokes(
        self,
        *,
        source_port: int,
        destination_port: int,
        volume_ml: float,
        rpm: int,
        inlet_aspiration_rpm: int | None = None,
        suffix: str = "",
    ) -> None:
        """Transfer liquid without drawing it into the SY01B valve or syringe.

        Preconditions:
        - The syringe starts empty.
        - The source line is primed.
        - The common line, destination route, and SY01B OUT line contain air.

        Each liquid stroke is at most common_line_dead_volume_ml minus
        common_line_stroke_clearance_margin_ml. A completed stroke leaves the
        common line air-filled and the destination route liquid-filled at the
        selector, so consecutive strokes join without requiring a selector-side
        reserve.

        A short transfer and its route chase are combined when they fit in one
        syringe movement. Otherwise, every liquid stroke is fully dispensed
        before the next one is aspirated. The final destination-route chase is
        then aspirated from OUT and dispensed through the selected destination.
        inlet_aspiration_rpm overrides rpm only for liquid strokes aspirated
        through IN; None uses rpm for every movement.
        """

        resolved_aspiration_rpm = self._resolve_inlet_aspiration_rpm(
            rpm=rpm, inlet_aspiration_rpm=inlet_aspiration_rpm
        )
        plan = self._build_common_line_stroke_transfer_plan(
            source_port=source_port,
            destination_port=destination_port,
            volume_ml=volume_ml,
        )
        self._run_common_line_stroke_transfer_plan(
            plan=plan,
            rpm=rpm,
            inlet_aspiration_rpm=resolved_aspiration_rpm,
            suffix=suffix,
        )

    def prepare_mixture_with_single_aspiration_transfers(
        self,
        *,
        source_fractions: Mapping[str, float],
        total_volume_ml: float,
        rpm: int,
        inlet_aspiration_rpm: int | None = None,
        water_distribution: WaterDistribution = "single",
        suffix: str = "",
    ) -> None:
        """Prepare a keyed liquid-source mixture in the mixing vessel.

        Fractions are keyed by SelectorPortConfig.key and must sum to 1.
        Positive components are transferred in plumbing configuration order.
        With water_distribution='between_components', all requested water is
        divided evenly between consecutive positive non-water components; no
        water transfer follows the final component.

        Every transfer is preflighted before movement begins. The syringe must
        start empty, all selected source lines must be primed, and the common
        line, mixing-vessel route, and SY01B OUT line must contain air. The
        mixing vessel must have room for total_volume_ml.
        inlet_aspiration_rpm affects only source-liquid aspiration through IN.
        """

        require_positive_integer(rpm, name="rpm")
        resolved_aspiration_rpm = self._resolve_inlet_aspiration_rpm(
            rpm=rpm, inlet_aspiration_rpm=inlet_aspiration_rpm
        )
        transfer_plans = self._build_single_aspiration_mixture_transfer_plans(
            source_fractions=source_fractions,
            total_volume_ml=total_volume_ml,
            water_distribution=water_distribution,
        )

        for index, plan in enumerate(transfer_plans, start=1):
            transfer_suffix = (
                f"{suffix} (mixture transfer {index}/{len(transfer_plans)})".strip()
            )
            self._run_single_aspiration_transfer_plan(
                plan=plan,
                rpm=rpm,
                inlet_aspiration_rpm=resolved_aspiration_rpm,
                suffix=transfer_suffix,
            )

    def prepare_mixture_with_common_line_stroke_transfers(
        self,
        *,
        source_fractions: Mapping[str, float],
        total_volume_ml: float,
        rpm: int,
        inlet_aspiration_rpm: int | None = None,
        water_distribution: WaterDistribution = "single",
        suffix: str = "",
    ) -> None:
        """Prepare a mixture without drawing liquid into the SY01B valve or syringe.

        Recipe validation, component order, and water distribution are identical
        to prepare_mixture_with_single_aspiration_transfers. Every component
        transfer is fully preflighted before movement begins. Each component is
        delivered to the mixing vessel using common-line strokes
        followed by a destination-route chase.
        inlet_aspiration_rpm affects only source-liquid aspiration through IN.
        """

        require_positive_integer(rpm, name="rpm")
        resolved_aspiration_rpm = self._resolve_inlet_aspiration_rpm(
            rpm=rpm, inlet_aspiration_rpm=inlet_aspiration_rpm
        )
        transfer_plans = self._build_common_line_stroke_mixture_transfer_plans(
            source_fractions=source_fractions,
            total_volume_ml=total_volume_ml,
            water_distribution=water_distribution,
        )

        for index, plan in enumerate(transfer_plans, start=1):
            transfer_suffix = (
                f"{suffix} (mixture transfer {index}/{len(transfer_plans)})".strip()
            )
            self._run_common_line_stroke_transfer_plan(
                plan=plan,
                rpm=rpm,
                inlet_aspiration_rpm=resolved_aspiration_rpm,
                suffix=transfer_suffix,
            )

    def fill_flow_cell_from_mixing_vessel(
        self,
        *,
        volume_ml: float,
        rpm: int,
        flow_rpm: int,
        approach_rpm: int | None = None,
        suffix: str = "",
        flow_hooks: FlowPhaseHooks | None = None,
    ) -> None:
        """Fill the flow-cell route from the mixing vessel and leave it liquid-filled.

        The mixing-vessel route and common line are conditioned together and
        the transition is discarded through SY01B OUT. The configured flow-cell
        route volume is then dispensed at approach_rpm. The calibrated
        flow_cell_overtravel_volume_ml is also dispensed at approach_rpm to
        replace the liquid in and immediately beyond the flow cell before the
        flow hooks begin. The remaining syringe content is then dispensed at
        flow_rpm while the hooks are active. If approach_rpm is omitted,
        flow_rpm is used for the approach.

        The syringe starts empty. The mixing vessel must contain enough
        homogeneous mixture to condition both upstream routes, fill the
        flow-cell route, supply the flow-cell overtravel volume, and deliver
        volume_ml. The syringe finishes empty, while the mixing-vessel route,
        common line, and flow-cell route finish liquid-filled.
        """

        require_positive_finite_number(volume_ml, name="volume_ml")
        require_positive_integer(rpm, name="rpm")
        approach_rpm = self._resolve_approach_rpm(
            approach_rpm=approach_rpm, flow_rpm=flow_rpm
        )

        (
            mixing_vessel,
            flow_cell,
            mixing_route_dead_volume_ml,
            flow_cell_route_dead_volume_ml,
        ) = self._require_mixing_vessel_flow_path()

        common_clearance_margin_ml = self._plumbing.clearance_margin_ml
        flow_cell_overtravel_volume_ml = self._plumbing.flow_cell_overtravel_volume_ml
        mixing_path_condition_volume_ml = (
            mixing_route_dead_volume_ml
            + self._plumbing.common_line_dead_volume_ml
            + common_clearance_margin_ml
        )
        aspiration_volume_ml = (
            flow_cell_route_dead_volume_ml + flow_cell_overtravel_volume_ml + volume_ml
        )

        self._require_syringe_capacity(
            mixing_path_condition_volume_ml, name="mixing_path_condition_volume_ml"
        )
        self._require_flow_cell_liquid_dispense_steps(
            route_volume_ml=flow_cell_route_dead_volume_ml,
            overtravel_volume_ml=flow_cell_overtravel_volume_ml,
            requested_flow_volume_ml=volume_ml,
        )

        if flow_hooks is not None and flow_hooks.before_fluid_handling is not None:
            flow_hooks.before_fluid_handling()

        self._run_step(
            name=self._step_name(f"select {mixing_vessel.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(mixing_vessel.number),
        )

        self._run_step(
            name=self._step_name(
                "condition mixing-vessel route and common line", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=mixing_path_condition_volume_ml, rpm=rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name("discard mixing-path transition to outlet", suffix),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="outlet"),
        )

        self._run_step(
            name=self._step_name(
                f"aspirate {mixing_vessel.label} for liquid-filled flow-cell dispense",
                suffix,
            ),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=aspiration_volume_ml, rpm=rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name(f"select {flow_cell.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(flow_cell.number),
        )

        if flow_cell_route_dead_volume_ml > 0:
            self._run_step(
                name=self._step_name(f"approach {flow_cell.label} boundary", suffix),
                target="sy01b",
                action=lambda: self._syringe.dispense_ml(
                    volume_ml=flow_cell_route_dead_volume_ml,
                    rpm=approach_rpm,
                    destination="inlet",
                ),
            )

        if flow_cell_overtravel_volume_ml > 0:
            self._run_step(
                name=self._step_name(
                    f"advance through {flow_cell.label} beyond route boundary", suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.dispense_ml(
                    volume_ml=flow_cell_overtravel_volume_ml,
                    rpm=approach_rpm,
                    destination="inlet",
                ),
            )

        if flow_hooks is not None:
            flow_hooks.before_flow()
        try:
            self._run_step(
                name=self._step_name(
                    f"continuously dispense {mixing_vessel.label} "
                    f"through {flow_cell.label}",
                    suffix,
                ),
                target="sy01b",
                action=lambda: self._syringe.empty_syringe(
                    rpm=flow_rpm, destination="inlet"
                ),
            )
        except BaseException as error:
            if flow_hooks is not None and flow_hooks.flow_failed is not None:
                flow_hooks.flow_failed(error)
            raise
        else:
            if flow_hooks is not None:
                flow_hooks.after_flow()

    def run_stopped_flow_series_from_mixing_vessel(
        self,
        *,
        measurement_count: int,
        between_measurement_displacement_volume_ml: float | None,
        rinse_through_volume_ml: float,
        rpm: int,
        approach_rpm: int,
        hooks: StoppedFlowHooks,
        suffix: str = "",
    ) -> None:
        """Move one water-backed mixture plug through a stopped-flow series.

        The mixing-vessel route is primed to selector waste, after which the
        common line is conditioned with water and a flow-cell rinse chase is
        held in the syringe at a fixed baseline volume. Mixture is then moved in
        common-line-sized strokes. Each aspiration raises the syringe position
        above that baseline and the matching dispense uses the identical rounded
        step count to return to it; the syringe is not emptied between strokes.

        A stroke is limited to common_line_dead_volume_ml minus
        common_line_stroke_clearance_margin_ml. Thus the margin remains
        water-filled at the syringe side while mixture is aspirated, keeping
        mixture out of the syringe. No mixture guard is retained in the common
        line at the selector side. After
        the matching dispense, the common line is water-filled and the selected
        flow-cell route remains mixture-filled to the selector, so commanded
        syringe rounding cannot introduce water between consecutive strokes.

        The initial route approach and overtravel are separate completed
        movements. A measurement callback runs after overtravel and after each
        subsequent displacement. A one-measurement run has no between-
        measurement displacement. After the last measurement, one final overtravel
        of mixture is followed by the complete water chase. On success the syringe
        is empty and both the common line and flow-cell route are water-filled.
        """

        require_positive_integer(measurement_count, name="measurement_count")
        if measurement_count == 1:
            if between_measurement_displacement_volume_ml is not None:
                raise ValueError(
                    "A single stopped-flow measurement does not use a "
                    "between-measurement displacement"
                )
        else:
            require_positive_finite_number(
                between_measurement_displacement_volume_ml,
                name="between_measurement_displacement_volume_ml",
            )
        require_positive_finite_number(
            rinse_through_volume_ml, name="rinse_through_volume_ml"
        )
        require_positive_integer(rpm, name="rpm")
        require_positive_integer(approach_rpm, name="approach_rpm")
        if not isinstance(hooks, StoppedFlowHooks):
            raise TypeError("hooks must be StoppedFlowHooks")

        (
            mixing_vessel,
            flow_cell,
            mixing_route_dead_volume_ml,
            flow_cell_route_dead_volume_ml,
        ) = self._require_mixing_vessel_flow_path()
        waste_ports = self._plumbing.destinations_of_kind("waste")
        if len(waste_ports) != 1:
            raise RuntimeError(
                "Exactly one selector waste destination must be configured"
            )
        selector_waste = waste_ports[0]
        waste_dead_volume_ml = selector_waste.destination_route_dead_volume_ml
        if waste_dead_volume_ml is None:
            raise RuntimeError("Selector waste route dead volume is not configured")

        margin_ml = self._plumbing.clearance_margin_ml
        stroke_margin_ml = self._plumbing.common_line_stroke_clearance_margin_ml
        overtravel_ml = self._plumbing.flow_cell_overtravel_volume_ml
        mixing_route_prime_ml = math.fsum((mixing_route_dead_volume_ml, margin_ml))
        required_waste_purge_ml = math.fsum((waste_dead_volume_ml, 2 * margin_ml))
        additional_waste_purge_air_ml = max(
            0.0, required_waste_purge_ml - mixing_route_prime_ml
        )
        chase_water_ml = math.fsum(
            (flow_cell_route_dead_volume_ml, overtravel_ml, rinse_through_volume_ml)
        )
        prime_steps = self._require_syringe_movement_volume(
            mixing_route_prime_ml, name="mixing_route_prime_volume_ml"
        )
        purge_steps = (
            self._require_syringe_movement_volume(
                additional_waste_purge_air_ml,
                name="additional_selector_waste_purge_air_ml",
            )
            if additional_waste_purge_air_ml > 0
            else 0
        )
        capacity_steps = self._syringe.steps_from_volume_ml(
            self._syringe.syringe_volume_ml
        )
        if prime_steps + purge_steps > capacity_steps:
            raise ValueError(
                "mixing-route prime and purge air exceed syringe capacity after "
                "syringe-step rounding"
            )

        chase_steps = self._require_syringe_movement_volume(
            chase_water_ml, name="stopped_flow_chase_water_volume_ml"
        )
        self._require_syringe_movement_volume(
            math.fsum((self._plumbing.common_line_dead_volume_ml, margin_ml)),
            name="common_line_condition_volume_ml",
        )
        common_stroke_limit_ml = math.fsum(
            (self._plumbing.common_line_dead_volume_ml, -stroke_margin_ml)
        )
        common_stroke_limit_steps = math.floor(
            common_stroke_limit_ml / self._syringe.ml_per_step
        )
        maximum_stroke_steps = min(
            common_stroke_limit_steps, capacity_steps - chase_steps
        )
        if maximum_stroke_steps < 1:
            raise ValueError(
                "stopped-flow chase water leaves no syringe capacity for one "
                "common-line mixture stroke"
            )

        def rounded_transfer_steps(volume_ml: float, *, name: str) -> int:
            steps = math.floor(volume_ml * self._syringe.steps_per_ml + 0.5)
            if steps < 1:
                raise ValueError(f"{name} is too small to move one syringe step")
            return steps

        route_approach_steps = (
            rounded_transfer_steps(
                flow_cell_route_dead_volume_ml,
                name="stopped_flow_route_approach_volume_ml",
            )
            if flow_cell_route_dead_volume_ml > 0
            else 0
        )
        between_steps = (
            0
            if between_measurement_displacement_volume_ml is None
            else rounded_transfer_steps(
                between_measurement_displacement_volume_ml,
                name="between_measurement_displacement_volume_ml",
            )
        )
        overtravel_steps = (
            rounded_transfer_steps(
                overtravel_ml, name="stopped_flow_overtravel_volume_ml"
            )
            if overtravel_ml > 0
            else 0
        )

        if hooks.before_fluid_handling is not None:
            hooks.before_fluid_handling()

        self._run_step(
            name=self._step_name(f"select {mixing_vessel.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(mixing_vessel.number),
        )
        self._run_step(
            name=self._step_name("prime mixing-vessel route", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=mixing_route_prime_ml, rpm=rpm, source="inlet"
            ),
        )
        self._run_step(
            name=self._step_name(f"select {selector_waste.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(selector_waste.number),
        )
        if additional_waste_purge_air_ml > 0:
            self._run_step(
                name=self._step_name(
                    "aspirate additional selector-waste purge air", suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.aspirate_ml(
                    volume_ml=additional_waste_purge_air_ml, rpm=rpm, source="outlet"
                ),
            )
        self._run_step(
            name=self._step_name(
                "push mixing-vessel overdraw to selector waste", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="inlet"),
        )

        self._condition_common_line_from_source(
            source=self._plumbing.water,
            rpm=rpm,
            suffix=self._step_name("for stopped flow", suffix),
        )
        self._run_step(
            name=self._step_name("aspirate stopped-flow chase water", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=chase_water_ml, rpm=rpm, source="inlet"
            ),
        )

        def advance_mixture(*, requested_steps: int, phase: str) -> None:
            stroke_count = (
                requested_steps + maximum_stroke_steps - 1
            ) // maximum_stroke_steps
            remaining_steps = requested_steps
            for stroke_index, stroke_steps in enumerate(
                partition_stroke_steps(requested_steps, maximum_stroke_steps), start=1
            ):
                stroke_ml = stroke_steps * self._syringe.ml_per_step
                stroke_suffix = self._step_name(
                    f"{phase} stroke {stroke_index}/{stroke_count}", suffix
                )
                self._run_step(
                    name=self._step_name(
                        f"select {mixing_vessel.label}", stroke_suffix
                    ),
                    target="sv07m",
                    action=lambda: self._selector.ensure_port(mixing_vessel.number),
                )
                self._run_step(
                    name=self._step_name(
                        "aspirate mixture into common line", stroke_suffix
                    ),
                    target="sy01b",
                    action=lambda stroke_ml=stroke_ml: self._syringe.aspirate_ml(
                        volume_ml=stroke_ml, rpm=rpm, source="inlet"
                    ),
                )
                self._run_step(
                    name=self._step_name(f"select {flow_cell.label}", stroke_suffix),
                    target="sv07m",
                    action=lambda: self._selector.ensure_port(flow_cell.number),
                )
                self._run_step(
                    name=self._step_name(
                        "advance mixture toward flow cell", stroke_suffix
                    ),
                    target="sy01b",
                    action=lambda stroke_ml=stroke_ml: self._syringe.dispense_ml(
                        volume_ml=stroke_ml, rpm=approach_rpm, destination="inlet"
                    ),
                )
                remaining_steps -= stroke_steps

        if route_approach_steps > 0:
            advance_mixture(
                requested_steps=route_approach_steps,
                phase="approach flow-cell boundary",
            )
        if overtravel_steps > 0:
            advance_mixture(
                requested_steps=overtravel_steps, phase="initial overtravel"
            )
        hooks.measure_at_stop(1)
        for sequence_number in range(2, measurement_count + 1):
            advance_mixture(
                requested_steps=between_steps,
                phase=f"measurement {sequence_number} displacement",
            )
            hooks.measure_at_stop(sequence_number)

        if overtravel_steps > 0:
            advance_mixture(requested_steps=overtravel_steps, phase="final overtravel")
        self._run_step(
            name=self._step_name(f"select {flow_cell.label} for final rinse", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(flow_cell.number),
        )
        self._run_step(
            name=self._step_name("dispense stopped-flow chase water", suffix),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(
                rpm=approach_rpm, destination="inlet"
            ),
        )

    def pass_mixing_plug_through_flow_cell(
        self,
        *,
        volume_ml: float,
        rpm: int,
        flow_rpm: int,
        approach_rpm: int | None = None,
        suffix: str = "",
        flow_hooks: FlowPhaseHooks | None = None,
    ) -> None:
        """Pass one continuous mixing-vessel liquid plug through the flow cell.

        volume_ml must not exceed the common-line dead volume. The
        mixing-vessel route is first primed while the common line is returned
        to air. A volume_ml liquid plug is then drawn into the common line and
        chased through the flow cell with air from SY01B OUT. The configured
        flow-cell route volume is advanced at approach_rpm before the plug is
        passed and chased beyond the cell by flow_cell_overtravel_volume_ml at
        flow_rpm. If approach_rpm is omitted, flow_rpm is used for the approach.

        The syringe starts empty. The mixing vessel must contain enough
        homogeneous mixture to prime its route and supply volume_ml. The
        mixing-vessel route finishes liquid-filled, while the syringe, common
        line, selector-waste route, and flow-cell route finish air-filled.
        """

        require_positive_finite_number(volume_ml, name="volume_ml")
        require_positive_integer(rpm, name="rpm")
        approach_rpm = self._resolve_approach_rpm(
            approach_rpm=approach_rpm, flow_rpm=flow_rpm
        )

        (
            mixing_vessel,
            flow_cell,
            mixing_route_dead_volume_ml,
            flow_cell_route_dead_volume_ml,
        ) = self._require_mixing_vessel_flow_path()

        common_dead_volume_ml = self._plumbing.common_line_dead_volume_ml
        if volume_ml > common_dead_volume_ml:
            raise ValueError(
                f"volume_ml ({volume_ml:g} ml) must not exceed the common-line "
                f"dead volume ({common_dead_volume_ml:g} ml)"
            )

        waste_ports = self._plumbing.destinations_of_kind("waste")

        if len(waste_ports) != 1:
            raise RuntimeError(
                "Exactly one selector waste destination must be configured"
            )

        selector_waste = waste_ports[0]
        waste_dead_volume_ml = selector_waste.destination_route_dead_volume_ml

        if waste_dead_volume_ml is None:
            raise RuntimeError("Selector waste route dead volume is not configured")

        common_clearance_margin_ml = self._plumbing.clearance_margin_ml
        flow_cell_overtravel_volume_ml = self._plumbing.flow_cell_overtravel_volume_ml

        mixing_route_prime_volume_ml = (
            mixing_route_dead_volume_ml + common_clearance_margin_ml
        )
        required_waste_purge_volume_ml = (
            waste_dead_volume_ml + 2 * common_clearance_margin_ml
        )
        additional_purge_air_ml = max(
            0.0, required_waste_purge_volume_ml - mixing_route_prime_volume_ml
        )
        flow_cell_chase_air_ml = (
            flow_cell_route_dead_volume_ml + flow_cell_overtravel_volume_ml
        )

        mixing_route_prime_steps = self._require_syringe_movement_volume(
            mixing_route_prime_volume_ml, name="mixing_route_prime_volume_ml"
        )
        additional_purge_air_steps = (
            self._require_syringe_movement_volume(
                additional_purge_air_ml, name="additional_selector_waste_purge_air_ml"
            )
            if additional_purge_air_ml > 0
            else 0
        )
        syringe_capacity_steps = self._syringe.steps_from_volume_ml(
            self._syringe.syringe_volume_ml
        )
        if mixing_route_prime_steps + additional_purge_air_steps > (
            syringe_capacity_steps
        ):
            raise ValueError(
                "mixing-route prime and purge air exceed syringe capacity after "
                "syringe-step rounding"
            )

        self._require_flow_cell_plug_dispense_steps(
            route_volume_ml=flow_cell_route_dead_volume_ml,
            plug_volume_ml=volume_ml,
            overtravel_volume_ml=flow_cell_overtravel_volume_ml,
        )

        if flow_hooks is not None and flow_hooks.before_fluid_handling is not None:
            flow_hooks.before_fluid_handling()

        self._run_step(
            name=self._step_name(f"select {mixing_vessel.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(mixing_vessel.number),
        )

        self._run_step(
            name=self._step_name("prime mixing-vessel route", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=mixing_route_prime_volume_ml, rpm=rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name(f"select {selector_waste.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(selector_waste.number),
        )

        if additional_purge_air_ml > 0:
            self._run_step(
                name=self._step_name(
                    "aspirate additional selector-waste purge air", suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.aspirate_ml(
                    volume_ml=additional_purge_air_ml, rpm=rpm, source="outlet"
                ),
            )

        self._run_step(
            name=self._step_name(
                "push mixing-vessel overdraw to selector waste", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(rpm=rpm, destination="inlet"),
        )

        self._run_step(
            name=self._step_name(f"select {mixing_vessel.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(mixing_vessel.number),
        )

        self._run_step(
            name=self._step_name("aspirate continuous mixing-vessel plug", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=volume_ml, rpm=rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name(f"select {flow_cell.label}", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(flow_cell.number),
        )

        self._run_step(
            name=self._step_name("aspirate flow-cell chase air from outlet", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=flow_cell_chase_air_ml, rpm=rpm, source="outlet"
            ),
        )

        if flow_cell_route_dead_volume_ml > 0:
            self._run_step(
                name=self._step_name(
                    f"approach {flow_cell.label} boundary with mixing-vessel plug",
                    suffix,
                ),
                target="sy01b",
                action=lambda: self._syringe.dispense_ml(
                    volume_ml=flow_cell_route_dead_volume_ml,
                    rpm=approach_rpm,
                    destination="inlet",
                ),
            )

        if flow_hooks is not None:
            flow_hooks.before_flow()
        try:
            self._run_step(
                name=self._step_name(
                    f"pass {mixing_vessel.label} plug through {flow_cell.label}", suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.empty_syringe(
                    rpm=flow_rpm, destination="inlet"
                ),
            )
        except BaseException as error:
            if flow_hooks is not None and flow_hooks.flow_failed is not None:
                flow_hooks.flow_failed(error)
            raise
        else:
            if flow_hooks is not None:
                flow_hooks.after_flow()

    def drain_mixing_vessel_to_outlet(
        self, *, expected_vessel_volume_ml: float, rpm: int, suffix: str = ""
    ) -> None:
        """Drain the expected mixing-vessel contents through SY01B OUT.

        expected_vessel_volume_ml is the liquid believed to be in the vessel
        itself; it excludes the mixing-vessel route and common-line volumes.
        The total displacement also includes those upstream volumes and the
        clearance margin. It is divided into syringe-sized strokes.

        The syringe starts empty. The mixing vessel must be vented, its pickup
        must remain submerged until the bulk liquid has been removed, and
        SY01B OUT must be routed safely to waste. The syringe, mixing-vessel
        route, common line, and SY01B OUT finish bulk-air-filled.
        """

        require_non_negative_finite_number(
            expected_vessel_volume_ml, name="expected_vessel_volume_ml"
        )
        require_positive_integer(rpm, name="rpm")

        mixing_vessel, mixing_route_dead_volume_ml = self._require_mixing_vessel()
        drain_displacement_ml = math.fsum(
            (
                expected_vessel_volume_ml,
                mixing_route_dead_volume_ml,
                self._plumbing.common_line_dead_volume_ml,
                self._plumbing.clearance_margin_ml,
            )
        )
        syringe_capacity_ml = self._syringe.syringe_volume_ml

        require_positive_finite_number(
            drain_displacement_ml, name="mixing_vessel_drain_displacement_ml"
        )
        require_positive_finite_number(syringe_capacity_ml, name="syringe_capacity_ml")

        outlet_purge_volume_ml = self._outlet_purge_volume_ml()
        tolerance_ml = max(1e-12, syringe_capacity_ml * 1e-12)
        remaining_volume_ml = drain_displacement_ml
        stroke_volumes_ml: list[float] = []

        while remaining_volume_ml > syringe_capacity_ml + tolerance_ml:
            stroke_volumes_ml.append(syringe_capacity_ml)
            remaining_volume_ml = math.fsum((remaining_volume_ml, -syringe_capacity_ml))

        if remaining_volume_ml > tolerance_ml:
            stroke_volumes_ml.append(min(remaining_volume_ml, syringe_capacity_ml))

        for index, stroke_volume_ml in enumerate(stroke_volumes_ml, start=1):
            self._require_syringe_movement_volume(
                stroke_volume_ml, name=f"mixing_vessel_drain_stroke_{index}_volume_ml"
            )

        self._run_step(
            name=self._step_name(f"select {mixing_vessel.label} for draining", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(mixing_vessel.number),
        )

        for index, stroke_volume_ml in enumerate(stroke_volumes_ml, start=1):
            stroke_suffix = f"{suffix} ({index}/{len(stroke_volumes_ml)})".strip()

            self._run_step(
                name=self._step_name(
                    f"aspirate {stroke_volume_ml:g} ml from {mixing_vessel.label}",
                    stroke_suffix,
                ),
                target="sy01b",
                action=lambda stroke_volume_ml=stroke_volume_ml: (
                    self._syringe.aspirate_ml(
                        volume_ml=stroke_volume_ml, rpm=rpm, source="inlet"
                    )
                ),
            )

            self._run_step(
                name=self._step_name(
                    f"discard {mixing_vessel.label} contents to outlet", stroke_suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.empty_syringe(
                    rpm=rpm, destination="outlet"
                ),
            )

        self._purge_syringe_outlet_with_air(
            volume_ml=outlet_purge_volume_ml,
            rpm=rpm,
            suffix=self._step_name("after mixing-vessel drain", suffix),
        )

    def flush_system(self, *, volume_ml: float, rpm: int, cycles: int = 1) -> None:
        """Flush volume_ml water per cycle, then air-purge common and OUT lines."""

        require_positive_finite_number(volume_ml, name="volume_ml")
        require_positive_integer(rpm, name="rpm")
        require_positive_integer(cycles, name="cycles")

        self._require_syringe_capacity(volume_ml, name="volume_ml")
        outlet_purge_volume_ml = self._outlet_purge_volume_ml()

        water = self._plumbing.water
        air = self._plumbing.air

        self._condition_common_line_from_source(source=water, rpm=rpm)

        for cycle in range(1, cycles + 1):
            cycle_suffix = f"({cycle}/{cycles})"

            self._run_step(
                name=self._step_name(f"aspirate {water.label}", cycle_suffix),
                target="sy01b",
                action=lambda: self._syringe.aspirate_ml(
                    volume_ml=volume_ml, rpm=rpm, source="inlet"
                ),
            )

            self._run_step(
                name=self._step_name("dispense all water to outlet", cycle_suffix),
                target="sy01b",
                action=lambda: self._syringe.empty_syringe(
                    rpm=rpm, destination="outlet"
                ),
            )

        self._condition_common_line_from_source(source=air, rpm=rpm)

        self._purge_syringe_outlet_with_air(volume_ml=outlet_purge_volume_ml, rpm=rpm)

    def rinse_mixing_vessel(
        self, *, rinse_volume_ml: float, rpm: int, cycles: int = 2, suffix: str = ""
    ) -> None:
        """Repeatedly fill and drain an initially empty mixing vessel with water.

        Preconditions:
        - The vessel contains no known bulk liquid.
        - The mixing-vessel route, common line, and SY01B OUT contain air.
        - The water source line is primed.

        The vessel and associated upstream paths finish bulk-air-filled.
        """

        require_positive_finite_number(rinse_volume_ml, name="rinse_volume_ml")
        require_positive_integer(rpm, name="rpm")
        require_positive_integer(cycles, name="cycles")

        water = self._plumbing.water
        mixing_vessel, _ = self._require_mixing_vessel()

        for cycle in range(1, cycles + 1):
            cycle_suffix = f"{suffix} ({cycle}/{cycles})".strip()

            self.transfer_liquid_to_route_with_single_aspiration(
                source_port=water.number,
                destination_port=mixing_vessel.number,
                volume_ml=rinse_volume_ml,
                rpm=rpm,
                suffix=cycle_suffix,
            )

            self.drain_mixing_vessel_to_outlet(
                expected_vessel_volume_ml=rinse_volume_ml, rpm=rpm, suffix=cycle_suffix
            )

    def rinse_flow_cell_with_water(
        self,
        *,
        rinse_through_volume_ml: float,
        handling_rpm: int,
        rinsing_rpm: int,
        suffix: str = "",
    ) -> None:
        """Replace the flow-cell route contents with water without an air break.

        The water source line must be primed and the syringe must start empty.
        Handling RPM is used to condition the common line, aspirate water, and
        restore the common and SY01B OUT paths to air. The entire flow-cell-
        directed dispense is one continuous movement at Rinsing RPM.

        The flow-cell route finishes water-filled. The common line and SY01B OUT
        finish air-filled, the syringe finishes empty, and the mixing-vessel
        route is not selected or changed.
        """

        require_positive_finite_number(
            rinse_through_volume_ml, name="rinse_through_volume_ml"
        )
        require_positive_integer(handling_rpm, name="handling_rpm")
        require_positive_integer(rinsing_rpm, name="rinsing_rpm")

        _, flow_cell, _, flow_cell_route_dead_volume_ml = (
            self._require_mixing_vessel_flow_path()
        )
        water = self._plumbing.water
        air = self._plumbing.air
        overtravel_volume_ml = self._plumbing.flow_cell_overtravel_volume_ml
        flow_cell_rinse_volume_ml = math.fsum(
            (
                flow_cell_route_dead_volume_ml,
                overtravel_volume_ml,
                rinse_through_volume_ml,
            )
        )
        common_condition_volume_ml = math.fsum(
            (
                self._plumbing.common_line_dead_volume_ml,
                self._plumbing.clearance_margin_ml,
            )
        )
        outlet_purge_volume_ml = self._outlet_purge_volume_ml()

        # Complete all representability and capacity checks before movement.
        self._require_syringe_movement_volume(
            common_condition_volume_ml, name="common_line_condition_volume_ml"
        )
        self._require_syringe_movement_volume(
            flow_cell_rinse_volume_ml, name="flow_cell_rinse_dispense_volume_ml"
        )
        self._require_syringe_movement_volume(
            outlet_purge_volume_ml, name="outlet_purge_volume_ml"
        )

        self._condition_common_line_from_source(
            source=water,
            rpm=handling_rpm,
            suffix=self._step_name("for flow-cell rinse", suffix),
        )

        self._run_step(
            name=self._step_name("aspirate flow-cell rinse water", suffix),
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=flow_cell_rinse_volume_ml, rpm=handling_rpm, source="inlet"
            ),
        )

        self._run_step(
            name=self._step_name(f"select {flow_cell.label} for water rinse", suffix),
            target="sv07m",
            action=lambda: self._selector.ensure_port(flow_cell.number),
        )

        self._run_step(
            name=self._step_name(
                f"continuously rinse {flow_cell.label} with water", suffix
            ),
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(
                rpm=rinsing_rpm, destination="inlet"
            ),
        )

        self._condition_common_line_from_source(
            source=air,
            rpm=handling_rpm,
            suffix=self._step_name("after flow-cell rinse", suffix),
        )
        self._purge_syringe_outlet_with_air(
            volume_ml=outlet_purge_volume_ml,
            rpm=handling_rpm,
            suffix=self._step_name("after flow-cell rinse", suffix),
        )

    def recover_source_lines_and_clear_flow_cell(
        self, *, source_ports: Collection[int], handling_rpm: int, approach_rpm: int
    ) -> None:
        """Recover selected source contents, then clear the full flow-cell route.

        The syringe, common line, and SY01B OUT must start bulk-air-filled. Keep
        each selected source tube in its matching stock container and route the
        flow-cell output to waste. Selected source lines and the complete
        flow-cell route finish bulk-air-filled; the water source is untouched.
        """
        require_positive_integer(handling_rpm, name="handling_rpm")
        require_positive_integer(approach_rpm, name="approach_rpm")

        sources = self._ordered_non_water_sources(source_ports)
        source_batches = self._build_route_batches(
            self._build_route_plans(source_lines=sources, extra_volume_ml=0.0)
        )
        flow_cell = self._plumbing.flow_cell
        if flow_cell is None:
            raise RuntimeError("No flow-cell destination is configured")
        flow_cell_clear_volume_ml = math.fsum(
            (
                self._plumbing.flow_cell_full_route_dead_volume_ml,
                self._plumbing.flow_cell_clearance_margin_ml,
            )
        )
        self._require_syringe_movement_volume(
            flow_cell_clear_volume_ml, name="flow_cell_full_route_clear_volume_ml"
        )

        self._run_route_batches(
            source=self._plumbing.air,
            batches=source_batches,
            rpm=handling_rpm,
            operation="source recovery",
            suffix="line maintenance",
        )
        self._run_step(
            name="select air for full flow-cell route clearing",
            target="sv07m",
            action=lambda: self._selector.ensure_port(self._plumbing.air.number),
        )
        self._run_step(
            name=f"aspirate {flow_cell_clear_volume_ml:g} ml air for flow-cell route",
            target="sy01b",
            action=lambda: self._syringe.aspirate_ml(
                volume_ml=flow_cell_clear_volume_ml, rpm=handling_rpm, source="inlet"
            ),
        )
        self._run_step(
            name=f"select {flow_cell.label} for full-route clearing",
            target="sv07m",
            action=lambda: self._selector.ensure_port(flow_cell.number),
        )
        self._run_step(
            name=f"clear full {flow_cell.label} route with air",
            target="sy01b",
            action=lambda: self._syringe.empty_syringe(
                rpm=approach_rpm, destination="inlet"
            ),
        )

    def rinse_source_lines_with_water(
        self, *, source_ports: Collection[int], additional_volume_ml: float, rpm: int
    ) -> None:
        """Water-rinse selected non-water source lines into external waste."""
        require_non_negative_finite_number(
            additional_volume_ml, name="additional_volume_ml"
        )
        require_positive_integer(rpm, name="rpm")

        sources = self._ordered_non_water_sources(source_ports)
        batches = self._build_route_batches(
            self._build_route_plans(
                source_lines=sources, extra_volume_ml=additional_volume_ml
            )
        )
        condition_volume_ml = math.fsum(
            (
                self._plumbing.common_line_dead_volume_ml,
                self._plumbing.clearance_margin_ml,
            )
        )
        self._require_syringe_movement_volume(
            condition_volume_ml, name="water_common_line_condition_volume_ml"
        )

        self._condition_common_line_from_source(
            source=self._plumbing.water,
            rpm=rpm,
            suffix="for source-line maintenance rinse",
        )
        self._run_route_batches(
            source=self._plumbing.water,
            batches=batches,
            rpm=rpm,
            operation="source-line water rinse",
            suffix="line maintenance",
        )

    def air_empty_source_lines(
        self, *, source_ports: Collection[int], rpm: int
    ) -> None:
        """Air-empty selected non-water source lines and water, then SY01B OUT."""
        require_positive_integer(rpm, name="rpm")

        sources = self._ordered_non_water_sources(source_ports)
        route_plans = self._build_route_plans(
            source_lines=(*sources, self._plumbing.water), extra_volume_ml=0.0
        )
        batches = self._build_route_batches(route_plans)
        condition_volume_ml = math.fsum(
            (
                self._plumbing.common_line_dead_volume_ml,
                self._plumbing.clearance_margin_ml,
            )
        )
        self._require_syringe_movement_volume(
            condition_volume_ml, name="air_common_line_condition_volume_ml"
        )
        outlet_purge_volume_ml = self._outlet_purge_volume_ml()

        self._condition_common_line_from_source(
            source=self._plumbing.air, rpm=rpm, suffix="for final source-line emptying"
        )
        self._run_route_batches(
            source=self._plumbing.air,
            batches=batches,
            rpm=rpm,
            operation="source-line air emptying",
            suffix="line maintenance",
        )
        self._purge_syringe_outlet_with_air(
            volume_ml=outlet_purge_volume_ml,
            rpm=rpm,
            suffix="after source-line maintenance",
        )

    def flush_working_destination_routes(
        self, *, rinse_volume_ml: float, rpm: int, flush_cycles: int = 1
    ) -> None:
        """Water-flush and air-purge selector-waste and flow-cell routes.

        Source lines are not flushed. Destination outlets must be routed safely
        to waste, and air must be permitted through the configured flow cell.
        Each water flush sends the route dead volume, its configured route
        extension, and rinse_volume_ml through the route. Each air purge sends the
        route dead volume plus its configured route extension.
        """

        require_positive_finite_number(rinse_volume_ml, name="rinse_volume_ml")
        require_positive_integer(rpm, name="rpm")
        require_positive_integer(flush_cycles, name="flush_cycles")
        self._require_syringe_capacity(rinse_volume_ml, name="rinse_volume_ml")

        destinations = tuple(
            destination
            for destination in self._plumbing.destinations
            if destination.destination_kind in {"waste", "flow_cell"}
        )

        if not destinations:
            raise RuntimeError(
                "No selector-waste or flow-cell destinations are configured"
            )

        water = self._plumbing.water
        air = self._plumbing.air
        water_route_plans = self._build_route_plans(
            destinations=destinations, extra_volume_ml=rinse_volume_ml
        )
        air_route_plans = self._build_route_plans(
            destinations=destinations, extra_volume_ml=0.0
        )
        water_route_batches = self._build_route_batches(water_route_plans)
        air_route_batches = self._build_route_batches(air_route_plans)
        outlet_purge_volume_ml = self._outlet_purge_volume_ml()

        self.flush_system(volume_ml=rinse_volume_ml, rpm=rpm, cycles=flush_cycles)
        self._condition_common_line_from_source(
            source=water, rpm=rpm, suffix="for working-route water flush"
        )
        self._run_route_batches(
            source=water,
            batches=water_route_batches,
            rpm=rpm,
            operation="water-flush",
            suffix="working destinations",
        )
        self._condition_common_line_from_source(
            source=air, rpm=rpm, suffix="for working-route air purge"
        )
        self._run_route_batches(
            source=air,
            batches=air_route_batches,
            rpm=rpm,
            operation="air-purge",
            suffix="working destinations",
        )
        self._purge_syringe_outlet_with_air(
            volume_ml=outlet_purge_volume_ml,
            rpm=rpm,
            suffix="after working destinations",
        )

    def _build_selector_waste_rinse_plan(
        self, *, rinse_through_volume_ml: float
    ) -> _SelectorWasteRinsePlan:
        """Preflight a water rinse and air purge of selector waste."""

        require_positive_finite_number(
            rinse_through_volume_ml, name="rinse_through_volume_ml"
        )

        waste_ports = self._plumbing.destinations_of_kind("waste")

        if len(waste_ports) != 1:
            raise RuntimeError(
                "Exactly one selector waste destination must be configured"
            )

        selector_waste = waste_ports[0]
        water_route_plans = self._build_route_plans(
            destinations=(selector_waste,), extra_volume_ml=rinse_through_volume_ml
        )
        air_route_plans = self._build_route_plans(
            destinations=(selector_waste,), extra_volume_ml=0.0
        )
        water_batches = self._build_route_batches(water_route_plans)
        air_batches = self._build_route_batches(air_route_plans)
        outlet_purge_volume_ml = self._outlet_purge_volume_ml()
        self._require_syringe_movement_volume(
            outlet_purge_volume_ml, name="selector_waste_outlet_purge_volume_ml"
        )

        common_condition_volume_ml = math.fsum(
            (
                self._plumbing.common_line_dead_volume_ml,
                self._plumbing.clearance_margin_ml,
            )
        )
        self._require_syringe_movement_volume(
            common_condition_volume_ml,
            name="selector_waste_rinse_common_condition_volume_ml",
        )

        for operation, batches in (("water", water_batches), ("air", air_batches)):
            for index, batch in enumerate(batches, start=1):
                self._require_syringe_movement_volume(
                    self._batch_volume_ml(batch),
                    name=f"selector_waste_{operation}_batch_{index}_volume_ml",
                )

        return _SelectorWasteRinsePlan(
            water_batches=water_batches,
            air_batches=air_batches,
            outlet_purge_volume_ml=outlet_purge_volume_ml,
        )

    def _run_selector_waste_rinse_plan(
        self, *, plan: _SelectorWasteRinsePlan, rpm: int, suffix: str = ""
    ) -> None:
        """Execute a preflighted selector-waste rinse plan."""

        require_positive_integer(rpm, name="rpm")

        water = self._plumbing.water
        air = self._plumbing.air

        self._condition_common_line_from_source(
            source=water,
            rpm=rpm,
            suffix=self._step_name("for selector-waste rinse", suffix),
        )
        self._run_route_batches(
            source=water,
            batches=plan.water_batches,
            rpm=rpm,
            operation="water-rinse",
            suffix=self._step_name("selector waste", suffix),
        )
        self._condition_common_line_from_source(
            source=air,
            rpm=rpm,
            suffix=self._step_name("after selector-waste rinse", suffix),
        )
        self._run_route_batches(
            source=air,
            batches=plan.air_batches,
            rpm=rpm,
            operation="air-purge",
            suffix=self._step_name("selector waste", suffix),
        )
        self._purge_syringe_outlet_with_air(
            volume_ml=plan.outlet_purge_volume_ml,
            rpm=rpm,
            suffix=self._step_name("after selector-waste rinse", suffix),
        )

    def rinse_selector_waste_route_with_water(
        self, *, rinse_through_volume_ml: float, rpm: int, suffix: str = ""
    ) -> None:
        """Water-rinse and air-purge the selector-waste destination route.

        The water source line must be primed. The syringe must start empty and
        the common line bulk-air-filled. The configured route dead volume,
        ordinary destination clearance, and rinse_through_volume_ml are sent as
        water through the waste route. The route is then displaced with air.

        The syringe finishes empty. The selector-waste route, common line, and
        SY01B OUT finish bulk-air-filled; other selector routes are untouched.
        """

        plan = self._build_selector_waste_rinse_plan(
            rinse_through_volume_ml=rinse_through_volume_ml
        )
        self._run_selector_waste_rinse_plan(plan=plan, rpm=rpm, suffix=suffix)

    def prime_all_liquid_source_lines(self, *, rpm: int, suffix: str = "") -> None:
        """Prime every configured liquid source line, with water first."""

        self.prime_selected_liquid_source_lines(
            source_ports=tuple(
                source.number for source in self._plumbing.liquid_sources
            ),
            rpm=rpm,
            suffix=suffix,
        )

    def prime_selected_liquid_source_lines(
        self,
        *,
        source_ports: Collection[int],
        rpm: int,
        prime_water: bool = True,
        suffix: str = "",
    ) -> None:
        """Prime selected air-filled liquid source lines.

        Preconditions:
        - The syringe starts at position 0.
        - The common line initially contains air.
        - Every selected source line initially contains air.
        - SY01B OUT can supply air if additional purge volume is required.

        When prime_water is true, the air-filled water line is primed slightly
        past the selector first. One full-syringe flush_system cycle then wets
        the syringe and SY01B OUT path before the selected non-water sources are
        primed in plumbing configuration order. When prime_water is false, the
        water line must already be primed; it is not re-primed or flushed.
        After all selected sources have been primed, water rinses the shared
        selector-waste route and air restores its previous bulk-air-filled
        state.

        Final state:
        - Water and every selected liquid source line are primed to their ports.
        - The selector-waste route has been rinsed with water after receiving
          the source-prime overdraw.
        - The common line, selector waste route, and SY01B OUT contain air.
        - The syringe finishes at position 0.
        """

        require_positive_integer(rpm, name="rpm")
        if not isinstance(prime_water, bool):
            raise TypeError("prime_water must be a bool")

        configured_sources = self._plumbing.liquid_sources

        if not configured_sources:
            raise RuntimeError("No liquid source ports are configured")

        water = self._plumbing.water
        requested_sources = tuple(
            self._require_liquid_source(port_number) for port_number in source_ports
        )
        requested_numbers = tuple(source.number for source in requested_sources)

        if len(set(requested_numbers)) != len(requested_numbers):
            raise ValueError("source_ports must not contain duplicates")

        selected_numbers = set(requested_numbers)
        if not prime_water and water.number in selected_numbers:
            raise ValueError(
                "The water port cannot be selected when prime_water is false"
            )
        sources = ((water,) if prime_water else ()) + tuple(
            source
            for source in configured_sources
            if source.number != water.number and source.number in selected_numbers
        )
        waste_ports = self._plumbing.destinations_of_kind("waste")

        if len(waste_ports) != 1:
            raise RuntimeError(
                "Exactly one selector waste destination must be configured"
            )

        selector_waste = waste_ports[0]
        waste_dead_volume_ml = selector_waste.destination_route_dead_volume_ml

        if waste_dead_volume_ml is None:
            raise RuntimeError("Selector waste route dead volume is not configured")

        clearance_margin_ml = self._plumbing.clearance_margin_ml
        required_purge_volume_ml = waste_dead_volume_ml + 2 * clearance_margin_ml
        wetting_volume_ml = self._syringe.syringe_volume_ml
        common_condition_volume_ml = (
            self._plumbing.common_line_dead_volume_ml + clearance_margin_ml
        )

        self._require_syringe_movement_volume(
            wetting_volume_ml, name="water_wetting_volume_ml"
        )
        self._require_syringe_movement_volume(
            common_condition_volume_ml, name="common_line_condition_volume_ml"
        )
        selector_waste_rinse_plan = self._build_selector_waste_rinse_plan(
            rinse_through_volume_ml=(
                self._plumbing.selector_waste_rinse_through_volume_ml
            )
        )
        syringe_capacity_steps = self._syringe.steps_from_volume_ml(
            self._syringe.syringe_volume_ml
        )

        prime_plans: list[tuple[SelectorPortConfig, float, float]] = []

        for source in sources:
            source_dead_volume_ml = source.source_line_dead_volume_ml

            if source_dead_volume_ml is None:
                raise RuntimeError(
                    f"Source line dead volume is not configured for {source.label}"
                )

            aspiration_volume_ml = source_dead_volume_ml + clearance_margin_ml

            additional_purge_air_ml = max(
                0.0, required_purge_volume_ml - aspiration_volume_ml
            )

            aspiration_steps = self._require_syringe_movement_volume(
                aspiration_volume_ml, name=f"{source.key}_prime_volume_ml"
            )
            additional_purge_air_steps = (
                self._require_syringe_movement_volume(
                    additional_purge_air_ml,
                    name=f"{source.key}_additional_purge_air_ml",
                )
                if additional_purge_air_ml > 0
                else 0
            )
            if aspiration_steps + additional_purge_air_steps > syringe_capacity_steps:
                raise ValueError(
                    f"{source.key} prime and purge air exceed syringe capacity "
                    "after syringe-step rounding"
                )

            prime_plans.append((source, aspiration_volume_ml, additional_purge_air_ml))

        for index, (source, aspiration_volume_ml, additional_purge_air_ml) in enumerate(
            prime_plans, start=1
        ):
            source_suffix = f"{suffix} ({index}/{len(prime_plans)})".strip()

            self._run_step(
                name=self._step_name(f"select {source.label}", source_suffix),
                target="sv07m",
                action=lambda source=source: self._selector.ensure_port(source.number),
            )

            self._run_step(
                name=self._step_name(
                    f"prime {source.label} source line", source_suffix
                ),
                target="sy01b",
                action=lambda aspiration_volume_ml=aspiration_volume_ml: (
                    self._syringe.aspirate_ml(
                        volume_ml=aspiration_volume_ml, rpm=rpm, source="inlet"
                    )
                ),
            )

            self._run_step(
                name=self._step_name(f"select {selector_waste.label}", source_suffix),
                target="sv07m",
                action=lambda: self._selector.ensure_port(selector_waste.number),
            )

            if additional_purge_air_ml > 0:
                self._run_step(
                    name=self._step_name(
                        "aspirate additional selector-waste purge air", source_suffix
                    ),
                    target="sy01b",
                    action=lambda additional_purge_air_ml=additional_purge_air_ml: (
                        self._syringe.aspirate_ml(
                            volume_ml=additional_purge_air_ml, rpm=rpm, source="outlet"
                        )
                    ),
                )

            self._run_step(
                name=self._step_name(
                    f"push {source.label} overdraw to selector waste", source_suffix
                ),
                target="sy01b",
                action=lambda: self._syringe.empty_syringe(
                    rpm=rpm, destination="inlet"
                ),
            )

            if source.number == water.number:
                self.flush_system(volume_ml=wetting_volume_ml, rpm=rpm, cycles=1)

        self._run_selector_waste_rinse_plan(
            plan=selector_waste_rinse_plan,
            rpm=rpm,
            suffix=self._step_name("after source priming", suffix),
        )

from dataclasses import dataclass
from typing import Literal

from common.validation import (
    require_integer_in_range,
    require_non_negative_finite_number,
    require_positive_finite_number,
)

SourceKind = Literal["air", "liquid"]
DestinationKind = Literal["waste", "mixing_vessel", "flow_cell", "other"]

SOURCE_KINDS = {"air", "liquid"}
DESTINATION_KINDS = {"waste", "mixing_vessel", "flow_cell", "other"}


@dataclass(frozen=True)
class SelectorPortConfig:
    """Physical and procedural configuration for one connected SV07M port.

    Source ports are persistent supply lines included in generic source and
    priming operations. Destination routes may be used in reverse by specialized
    procedures, such as drawing liquid back from the mixing vessel.
    """

    key: str
    label: str
    number: int

    source_kind: SourceKind | None = None
    source_line_dead_volume_ml: float | None = None

    destination_kind: DestinationKind | None = None
    destination_route_dead_volume_ml: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("key must be a non-empty string")

        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("label must be a non-empty string")

        require_integer_in_range(
            self.number, name=f"{self.key}.number", minimum=1, maximum=10
        )

        if self.source_kind is not None and self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"Unsupported source kind: {self.source_kind}")

        if (
            self.destination_kind is not None
            and self.destination_kind not in DESTINATION_KINDS
        ):
            raise ValueError(f"Unsupported destination kind: {self.destination_kind}")

        if self.source_kind is None and self.destination_kind is None:
            raise ValueError(
                f"{self.key} must be configured as a source or destination"
            )

        if self.source_kind is not None and self.destination_kind is not None:
            raise ValueError(
                f"{self.key} cannot currently be configured as both a source and "
                "destination"
            )

        if self.source_kind == "liquid":
            if self.source_line_dead_volume_ml is None:
                raise ValueError(
                    f"{self.key} is configured as a liquid source but "
                    "source_line_dead_volume_ml is missing"
                )

            require_non_negative_finite_number(
                self.source_line_dead_volume_ml,
                name=f"{self.key}.source_line_dead_volume_ml",
            )
        elif self.source_line_dead_volume_ml is not None:
            raise ValueError(
                f"{self.key}.source_line_dead_volume_ml requires source_kind='liquid'"
            )

        if self.destination_kind is None:
            if self.destination_route_dead_volume_ml is not None:
                raise ValueError(
                    f"{self.key}.destination_route_dead_volume_ml requires "
                    "destination_kind"
                )
        else:
            if self.destination_route_dead_volume_ml is None:
                raise ValueError(
                    f"{self.key} is configured as a destination but "
                    "destination_route_dead_volume_ml is missing"
                )

            require_non_negative_finite_number(
                self.destination_route_dead_volume_ml,
                name=f"{self.key}.destination_route_dead_volume_ml",
            )

    @property
    def is_air_source(self) -> bool:
        return self.source_kind == "air"

    @property
    def is_liquid_source(self) -> bool:
        return self.source_kind == "liquid"

    @property
    def is_destination(self) -> bool:
        return self.destination_kind is not None


@dataclass(frozen=True)
class RunzePlumbingConfig:
    """Physical route volumes and conservative procedural allowances.

    clearance_margin_ml is the general safety allowance used when crossing an
    uncertain common-line or ordinary destination boundary.
    common_line_stroke_clearance_margin_ml is the liquid-free margin retained
    at the syringe side of the common line during repeated liquid strokes.
    selector_waste_rinse_through_volume_ml is the water volume deliberately
    passed beyond the nominal selector-waste route boundary after source
    priming.
    flow_cell_overtravel_volume_ml is different: it is a calibrated additional
    displacement beyond the nominal flow-cell route boundary, used to replace
    liquid in and immediately beyond the cell. flow_cell_full_route_dead_volume_ml
    covers the complete syringe-common-line-to-flow-cell-output route, while
    flow_cell_clearance_margin_ml is only its physical clearance allowance.
    mixing_vessel_reserve_volume_ml is bulk liquid deliberately left available
    in the vessel for later experiment-volume planning; low-level movement
    procedures do not consume it automatically.
    """

    selector_ports: tuple[SelectorPortConfig, ...]
    common_line_dead_volume_ml: float
    syringe_outlet_to_waste_dead_volume_ml: float
    clearance_margin_ml: float
    common_line_stroke_clearance_margin_ml: float
    selector_waste_rinse_through_volume_ml: float
    flow_cell_overtravel_volume_ml: float
    flow_cell_full_route_dead_volume_ml: float
    flow_cell_clearance_margin_ml: float
    mixing_vessel_reserve_volume_ml: float

    def __post_init__(self) -> None:
        if not isinstance(self.selector_ports, tuple):
            raise TypeError("selector_ports must be a tuple")

        if not self.selector_ports:
            raise ValueError("selector_ports must not be empty")

        for index, port in enumerate(self.selector_ports):
            if not isinstance(port, SelectorPortConfig):
                raise TypeError(f"selector_ports[{index}] must be a SelectorPortConfig")

        numbers = [port.number for port in self.selector_ports]
        keys = [port.key for port in self.selector_ports]

        if len(set(numbers)) != len(numbers):
            raise ValueError("Selector port numbers must be unique")

        if len(set(keys)) != len(keys):
            raise ValueError("Selector port keys must be unique")

        if len(self.air_sources) != 1:
            raise ValueError("Exactly one selector air source must be configured")

        if len(self.destinations_of_kind("flow_cell")) > 1:
            raise ValueError("At most one flow cell destination may be configured")

        if len(self.destinations_of_kind("mixing_vessel")) != 1:
            raise ValueError("Exactly one mixing vessel destination must be configured")

        require_positive_finite_number(
            self.common_line_dead_volume_ml, name="common_line_dead_volume_ml"
        )

        require_non_negative_finite_number(
            self.clearance_margin_ml, name="clearance_margin_ml"
        )

        if self.clearance_margin_ml >= self.common_line_dead_volume_ml:
            raise ValueError(
                "clearance_margin_ml must be smaller than common_line_dead_volume_ml"
            )

        require_non_negative_finite_number(
            self.common_line_stroke_clearance_margin_ml,
            name="common_line_stroke_clearance_margin_ml",
        )
        if (
            self.common_line_stroke_clearance_margin_ml
            >= self.common_line_dead_volume_ml
        ):
            raise ValueError(
                "common_line_stroke_clearance_margin_ml must be smaller than "
                "common_line_dead_volume_ml"
            )

        require_positive_finite_number(
            self.selector_waste_rinse_through_volume_ml,
            name="selector_waste_rinse_through_volume_ml",
        )

        require_non_negative_finite_number(
            self.flow_cell_overtravel_volume_ml, name="flow_cell_overtravel_volume_ml"
        )

        require_positive_finite_number(
            self.flow_cell_full_route_dead_volume_ml,
            name="flow_cell_full_route_dead_volume_ml",
        )

        require_non_negative_finite_number(
            self.flow_cell_clearance_margin_ml, name="flow_cell_clearance_margin_ml"
        )

        flow_cell = self.flow_cell
        if (
            flow_cell is not None
            and flow_cell.destination_route_dead_volume_ml is not None
            and self.flow_cell_full_route_dead_volume_ml
            < flow_cell.destination_route_dead_volume_ml
        ):
            raise ValueError(
                "flow_cell_full_route_dead_volume_ml must not be smaller than "
                "the selector-to-flow-cell route dead volume"
            )

        require_non_negative_finite_number(
            self.mixing_vessel_reserve_volume_ml, name="mixing_vessel_reserve_volume_ml"
        )

        require_positive_finite_number(
            self.syringe_outlet_to_waste_dead_volume_ml,
            name="syringe_outlet_to_waste_dead_volume_ml",
        )

    @property
    def active_ports(self) -> tuple[int, ...]:
        return tuple(port.number for port in self.selector_ports)

    @property
    def air_sources(self) -> tuple[SelectorPortConfig, ...]:
        return tuple(port for port in self.selector_ports if port.is_air_source)

    @property
    def air(self) -> SelectorPortConfig:
        return self.air_sources[0]

    @property
    def liquid_sources(self) -> tuple[SelectorPortConfig, ...]:
        return tuple(port for port in self.selector_ports if port.is_liquid_source)

    @property
    def destinations(self) -> tuple[SelectorPortConfig, ...]:
        return tuple(port for port in self.selector_ports if port.is_destination)

    @property
    def flow_cell(self) -> SelectorPortConfig | None:
        ports = self.destinations_of_kind("flow_cell")
        return ports[0] if ports else None

    @property
    def mixing_vessel(self) -> SelectorPortConfig | None:
        ports = self.destinations_of_kind("mixing_vessel")
        return ports[0] if ports else None

    @property
    def water(self) -> SelectorPortConfig:
        port = self.port_by_key("water")

        if not port.is_liquid_source:
            raise ValueError("The configured water port must be a liquid source")

        return port

    def destinations_of_kind(
        self, kind: DestinationKind
    ) -> tuple[SelectorPortConfig, ...]:
        return tuple(
            port for port in self.selector_ports if port.destination_kind == kind
        )

    def port_by_number(self, number: int) -> SelectorPortConfig:
        require_integer_in_range(number, name="port number", minimum=1, maximum=10)

        for port in self.selector_ports:
            if port.number == number:
                return port

        raise ValueError(f"Selector port {number} is not configured")

    def port_by_key(self, key: str) -> SelectorPortConfig:
        for port in self.selector_ports:
            if port.key == key:
                return port

        raise ValueError(f"Selector port key {key!r} is not configured")

from fluidics.runze_plumbing import RunzePlumbingConfig, SelectorPortConfig

DEFAULT_CLEARANCE_MARGIN = 0.2
COMMON_LINE_STROKE_CLEARANCE_MARGIN = 0.1
COMMON_DEAD_VOLUME = 2.0 / 3  # all tubes were initially cut to be the same length
SELECTOR_TO_FLOW_CELL_DEAD_VOLUME = 0.650 / 2  # approximate value
FLOW_CELL_FULL_ROUTE_DEAD_VOLUME = 0.650  # common line through cell to output
SELECTOR_WASTE_RINSE_THROUGH_VOLUME = DEFAULT_CLEARANCE_MARGIN
FLOW_CELL_OVERTRAVEL_VOLUME = 0.05  # empirical volume beyond the route boundary
FLOW_CELL_CLEARANCE_MARGIN = 0.05  # physical allowance for full-route clearing
MIXING_VESSEL_RESERVE_VOLUME = 0.2  # TODO: calibrate before automated experiments


RUNZE_PLUMBING = RunzePlumbingConfig(
    selector_ports=(
        SelectorPortConfig(
            key="test_liquid_2",
            label="Test liquid 2",
            number=9,
            source_kind="liquid",
            source_line_dead_volume_ml=COMMON_DEAD_VOLUME,
        ),
        SelectorPortConfig(key="air", label="Air", number=8, source_kind="air"),
        SelectorPortConfig(
            key="water",
            label="Water",
            number=7,
            source_kind="liquid",
            source_line_dead_volume_ml=COMMON_DEAD_VOLUME,
        ),
        SelectorPortConfig(
            key="test_liquid_1",
            label="Test liquid 1",
            number=6,
            source_kind="liquid",
            source_line_dead_volume_ml=COMMON_DEAD_VOLUME,
        ),
        SelectorPortConfig(
            key="mixing",
            label="Mixing vessel",
            number=5,
            destination_kind="mixing_vessel",
            destination_route_dead_volume_ml=COMMON_DEAD_VOLUME,
        ),
        SelectorPortConfig(
            key="waste",
            label="Waste",
            number=4,
            destination_kind="waste",
            destination_route_dead_volume_ml=COMMON_DEAD_VOLUME,
        ),
        SelectorPortConfig(
            key="flow_cell",
            label="Flow cell",
            number=3,
            destination_kind="flow_cell",
            destination_route_dead_volume_ml=SELECTOR_TO_FLOW_CELL_DEAD_VOLUME,
        ),
    ),
    common_line_dead_volume_ml=COMMON_DEAD_VOLUME,
    syringe_outlet_to_waste_dead_volume_ml=COMMON_DEAD_VOLUME,
    clearance_margin_ml=DEFAULT_CLEARANCE_MARGIN,
    common_line_stroke_clearance_margin_ml=(COMMON_LINE_STROKE_CLEARANCE_MARGIN),
    selector_waste_rinse_through_volume_ml=SELECTOR_WASTE_RINSE_THROUGH_VOLUME,
    flow_cell_overtravel_volume_ml=FLOW_CELL_OVERTRAVEL_VOLUME,
    flow_cell_full_route_dead_volume_ml=FLOW_CELL_FULL_ROUTE_DEAD_VOLUME,
    flow_cell_clearance_margin_ml=FLOW_CELL_CLEARANCE_MARGIN,
    mixing_vessel_reserve_volume_ml=MIXING_VESSEL_RESERVE_VOLUME,
)

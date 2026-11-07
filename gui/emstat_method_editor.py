from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, ClassVar

from nicegui import ui

from controllers.emstat_controller import EmStatMeasurementSettings
from gui.runze_input_validation import (
    read_non_negative_number,
    read_positive_integer,
    read_positive_number,
)
from palmsens.emstat4s import (
    ChronoamperometrySettings,
    CyclicVoltammetrySettings,
    EmStatMeasurementType,
    LinearSweepVoltammetrySettings,
)


@dataclass
class EmStatMethodEditorState:
    measurement_type: EmStatMeasurementType = "chronoamperometry"

    chronoamperometry_potential_v: float | None = 0.5
    chronoamperometry_duration_s: float | None = 5.0
    chronoamperometry_interval_s: float | None = 0.1
    chronoamperometry_equilibration_time_s: float | None = 0.0

    cyclic_voltammetry_begin_potential_v: float | None = 0.0
    cyclic_voltammetry_vertex1_potential_v: float | None = -0.5
    cyclic_voltammetry_vertex2_potential_v: float | None = 0.5
    cyclic_voltammetry_step_potential_v: float | None = 0.01
    cyclic_voltammetry_scan_rate_v_per_s: float | None = 0.1
    cyclic_voltammetry_scan_count: float | None = 1
    cyclic_voltammetry_equilibration_time_s: float | None = 0.0

    linear_sweep_voltammetry_equilibration_time_s: float | None = 8.0
    linear_sweep_voltammetry_begin_potential_v: float | None = -0.5
    linear_sweep_voltammetry_end_potential_v: float | None = 0.5
    linear_sweep_voltammetry_step_potential_v: float | None = 0.005
    linear_sweep_voltammetry_scan_rate_v_per_s: float | None = 0.5
    linear_sweep_voltammetry_sweep_count: float | None = 1


class EmStatMethodEditor:
    """Reusable electrochemical method selector and validated settings editor."""

    METHOD_LABELS: ClassVar[dict[EmStatMeasurementType, str]] = {
        "chronoamperometry": "Chronoamperometry",
        "cyclic_voltammetry": "Cyclic voltammetry",
        "linear_sweep_voltammetry": "Linear sweep voltammetry",
    }
    METHOD_ABBREVIATIONS: ClassVar[dict[EmStatMeasurementType, str]] = {
        "chronoamperometry": "CA",
        "cyclic_voltammetry": "CV",
        "linear_sweep_voltammetry": "LSV",
    }

    def __init__(
        self,
        *,
        state: EmStatMethodEditorState | None = None,
        on_change: Callable[[], None] | None = None,
        panel_height_rem: float = 15.5,
    ) -> None:
        self.state = state if state is not None else EmStatMethodEditorState()
        self.on_change = on_change or (lambda: None)
        self._enabled = True
        self._syncing_values = False

        self._build(panel_height_rem=panel_height_rem)
        self._register_state_handlers()

    @property
    def measurement_type(self) -> EmStatMeasurementType:
        value: object = self.measurement_tabs.value
        if value is self.chronoamperometry_tab or value == "chronoamperometry":
            return "chronoamperometry"
        if value is self.cyclic_voltammetry_tab or value == "cyclic_voltammetry":
            return "cyclic_voltammetry"
        if (
            value is self.linear_sweep_voltammetry_tab
            or value == "linear_sweep_voltammetry"
        ):
            return "linear_sweep_voltammetry"
        raise ValueError("Measurement type is required")

    @property
    def measurement_label(self) -> str:
        return self.METHOD_LABELS[self.measurement_type]

    def read_settings(self) -> EmStatMeasurementSettings:
        if self.measurement_type == "chronoamperometry":
            return ChronoamperometrySettings(
                potential_v=self._required_number(
                    self.potential_input.value, name="Potential"
                ),
                run_time_s=read_positive_number(
                    self.duration_input.value, name="Duration"
                ),
                interval_time_s=read_positive_number(
                    self.interval_input.value, name="Interval"
                ),
                equilibration_time_s=read_non_negative_number(
                    self.chronoamperometry_equilibration_input.value,
                    name="Equilibration time",
                ),
            )

        if self.measurement_type == "cyclic_voltammetry":
            return CyclicVoltammetrySettings(
                begin_potential_v=self._required_number(
                    self.begin_potential_input.value, name="Begin potential"
                ),
                vertex1_potential_v=self._required_number(
                    self.vertex1_potential_input.value, name="Vertex 1 potential"
                ),
                vertex2_potential_v=self._required_number(
                    self.vertex2_potential_input.value, name="Vertex 2 potential"
                ),
                step_potential_v=read_positive_number(
                    self.step_potential_input.value, name="Step potential"
                ),
                scan_rate_v_per_s=read_positive_number(
                    self.scan_rate_input.value, name="Scan rate"
                ),
                scan_count=read_positive_integer(
                    self.scan_count_input.value, name="Scan count"
                ),
                equilibration_time_s=read_non_negative_number(
                    self.cyclic_voltammetry_equilibration_input.value,
                    name="Equilibration time",
                ),
            )

        return LinearSweepVoltammetrySettings(
            equilibration_time_s=read_non_negative_number(
                self.linear_sweep_voltammetry_equilibration_input.value,
                name="Equilibration time",
            ),
            begin_potential_v=self._required_number(
                self.linear_sweep_voltammetry_begin_potential_input.value,
                name="Begin potential",
            ),
            end_potential_v=self._required_number(
                self.linear_sweep_voltammetry_end_potential_input.value,
                name="End potential",
            ),
            step_potential_v=read_positive_number(
                self.linear_sweep_voltammetry_step_potential_input.value,
                name="Step potential",
            ),
            scan_rate_v_per_s=read_positive_number(
                self.linear_sweep_voltammetry_scan_rate_input.value, name="Scan rate"
            ),
            sweep_count=read_positive_integer(
                self.linear_sweep_voltammetry_sweep_count_input.value,
                name="Sweep count",
            ),
        )

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.chronoamperometry_tab.set_enabled(enabled)
        self.cyclic_voltammetry_tab.set_enabled(enabled)
        self.linear_sweep_voltammetry_tab.set_enabled(enabled)
        for element in self.inputs:
            element.set_enabled(enabled)

    def refresh_from_state(self) -> None:
        """Refresh controls after an owning panel replaces the shared state."""

        self._syncing_values = True
        tabs = {
            "chronoamperometry": self.chronoamperometry_tab,
            "cyclic_voltammetry": self.cyclic_voltammetry_tab,
            "linear_sweep_voltammetry": self.linear_sweep_voltammetry_tab,
        }
        try:
            self.measurement_tabs.set_value(tabs[self.state.measurement_type])
            bindings = (
                (self.potential_input, "chronoamperometry_potential_v"),
                (self.duration_input, "chronoamperometry_duration_s"),
                (self.interval_input, "chronoamperometry_interval_s"),
                (
                    self.chronoamperometry_equilibration_input,
                    "chronoamperometry_equilibration_time_s",
                ),
                (self.begin_potential_input, "cyclic_voltammetry_begin_potential_v"),
                (
                    self.vertex1_potential_input,
                    "cyclic_voltammetry_vertex1_potential_v",
                ),
                (
                    self.vertex2_potential_input,
                    "cyclic_voltammetry_vertex2_potential_v",
                ),
                (self.step_potential_input, "cyclic_voltammetry_step_potential_v"),
                (self.scan_rate_input, "cyclic_voltammetry_scan_rate_v_per_s"),
                (self.scan_count_input, "cyclic_voltammetry_scan_count"),
                (
                    self.cyclic_voltammetry_equilibration_input,
                    "cyclic_voltammetry_equilibration_time_s",
                ),
                (
                    self.linear_sweep_voltammetry_equilibration_input,
                    "linear_sweep_voltammetry_equilibration_time_s",
                ),
                (
                    self.linear_sweep_voltammetry_begin_potential_input,
                    "linear_sweep_voltammetry_begin_potential_v",
                ),
                (
                    self.linear_sweep_voltammetry_end_potential_input,
                    "linear_sweep_voltammetry_end_potential_v",
                ),
                (
                    self.linear_sweep_voltammetry_step_potential_input,
                    "linear_sweep_voltammetry_step_potential_v",
                ),
                (
                    self.linear_sweep_voltammetry_scan_rate_input,
                    "linear_sweep_voltammetry_scan_rate_v_per_s",
                ),
                (
                    self.linear_sweep_voltammetry_sweep_count_input,
                    "linear_sweep_voltammetry_sweep_count",
                ),
            )
            for element, attribute in bindings:
                element.set_value(getattr(self.state, attribute))
        finally:
            self._syncing_values = False

    def _build(self, *, panel_height_rem: float) -> None:
        self._install_responsive_tab_styles()
        with ui.element("div").classes("w-full emstat-method-editor"):
            self.measurement_tabs = (
                ui.tabs()
                .props("align=justify")
                .classes(
                    f"w-full emstat-method-tabs method-count-{len(self.METHOD_LABELS)}"
                )
            )
            with self.measurement_tabs:
                self.chronoamperometry_tab = ui.tab(
                    "chronoamperometry", label="Chronoamperometry"
                ).classes("emstat-tab-ca")
                self.chronoamperometry_tab.tooltip("Chronoamperometry")
                self.cyclic_voltammetry_tab = ui.tab(
                    "cyclic_voltammetry", label="Cyclic voltammetry"
                ).classes("emstat-tab-cv")
                self.cyclic_voltammetry_tab.tooltip("Cyclic voltammetry")
                self.linear_sweep_voltammetry_tab = ui.tab(
                    "linear_sweep_voltammetry", label="Linear sweep voltammetry"
                ).classes("emstat-tab-lsv")
                self.linear_sweep_voltammetry_tab.tooltip("Linear sweep voltammetry")

        initial_tabs = {
            "chronoamperometry": self.chronoamperometry_tab,
            "cyclic_voltammetry": self.cyclic_voltammetry_tab,
            "linear_sweep_voltammetry": self.linear_sweep_voltammetry_tab,
        }
        initial_tab = initial_tabs[self.state.measurement_type]
        with (
            ui.tab_panels(self.measurement_tabs, value=initial_tab)
            .classes("w-full bg-transparent")
            .style(f"height: {panel_height_rem:g}rem;")
        ):
            with ui.tab_panel(self.chronoamperometry_tab).classes("p-0"):
                self._build_chronoamperometry_inputs()
            with ui.tab_panel(self.cyclic_voltammetry_tab).classes("p-0"):
                self._build_cyclic_voltammetry_inputs()
            with ui.tab_panel(self.linear_sweep_voltammetry_tab).classes("p-0"):
                self._build_linear_sweep_voltammetry_inputs()

        self.inputs = [
            self.potential_input,
            self.chronoamperometry_equilibration_input,
            self.duration_input,
            self.interval_input,
            self.begin_potential_input,
            self.cyclic_voltammetry_equilibration_input,
            self.vertex1_potential_input,
            self.vertex2_potential_input,
            self.step_potential_input,
            self.scan_rate_input,
            self.scan_count_input,
            self.linear_sweep_voltammetry_equilibration_input,
            self.linear_sweep_voltammetry_begin_potential_input,
            self.linear_sweep_voltammetry_end_potential_input,
            self.linear_sweep_voltammetry_step_potential_input,
            self.linear_sweep_voltammetry_scan_rate_input,
            self.linear_sweep_voltammetry_sweep_count_input,
        ]

    @staticmethod
    def _install_responsive_tab_styles() -> None:
        ui.add_css(
            """
            .emstat-method-editor { container-type: inline-size; }
            .emstat-method-tabs .q-tab {
                flex: 1 1 0;
                min-width: 0;
            }
            @container (max-width: 22rem) {
                .emstat-method-tabs.method-count-2 .q-tab__label {
                    font-size: 0;
                }
                .emstat-method-tabs.method-count-2 .emstat-tab-ca .q-tab__label::after {
                    content: "CA";
                    font-size: 0.875rem;
                }
                .emstat-method-tabs.method-count-2 .emstat-tab-cv .q-tab__label::after {
                    content: "CV";
                    font-size: 0.875rem;
                }
            }
            @container (max-width: 38rem) {
                .emstat-method-tabs.method-count-3 .q-tab__label {
                    font-size: 0;
                }
                .emstat-method-tabs.method-count-3 .emstat-tab-ca .q-tab__label::after {
                    content: "CA";
                    font-size: 0.875rem;
                }
                .emstat-method-tabs.method-count-3 .emstat-tab-cv .q-tab__label::after {
                    content: "CV";
                    font-size: 0.875rem;
                }
                .emstat-method-tabs.method-count-3
                .emstat-tab-lsv .q-tab__label::after {
                    content: "LSV";
                    font-size: 0.875rem;
                }
            }
            """
        )

    def _build_chronoamperometry_inputs(self) -> None:
        with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-3 items-end"):
            self.chronoamperometry_equilibration_input = self._free_number(
                "Equilibration (s)",
                value=self.state.chronoamperometry_equilibration_time_s,
                minimum=0.0,
                precision=0,
            )
            self.potential_input = self._free_number(
                "Potential (V)",
                value=self.state.chronoamperometry_potential_v,
                precision=3,
            )
            self.duration_input = self._free_number(
                "Duration (s)",
                value=self.state.chronoamperometry_duration_s,
                minimum=0.0,
                precision=3,
            )
            self.interval_input = self._free_number(
                "Interval (s)",
                value=self.state.chronoamperometry_interval_s,
                minimum=0.0,
                precision=3,
            )

    def _build_cyclic_voltammetry_inputs(self) -> None:
        with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-3 items-end"):
            self.cyclic_voltammetry_equilibration_input = self._free_number(
                "Equilibration (s)",
                value=self.state.cyclic_voltammetry_equilibration_time_s,
                minimum=0.0,
                precision=0,
            )
            self.begin_potential_input = self._free_number(
                "Begin potential (V)",
                value=self.state.cyclic_voltammetry_begin_potential_v,
                precision=3,
            )
            self.vertex1_potential_input = self._free_number(
                "Vertex 1 (V)",
                value=self.state.cyclic_voltammetry_vertex1_potential_v,
                precision=3,
            )
            self.vertex2_potential_input = self._free_number(
                "Vertex 2 (V)",
                value=self.state.cyclic_voltammetry_vertex2_potential_v,
                precision=3,
            )
            self.step_potential_input = self._free_number(
                "Step potential (V)",
                value=self.state.cyclic_voltammetry_step_potential_v,
                minimum=0.0,
                precision=6,
            )
            self.scan_rate_input = self._free_number(
                "Scan rate (V/s)",
                value=self.state.cyclic_voltammetry_scan_rate_v_per_s,
                minimum=0.0,
                precision=6,
            )
            self.scan_count_input = ui.number(
                "Scan count",
                value=self.state.cyclic_voltammetry_scan_count,
                min=1,
                step=1,
                precision=0,
            ).classes("w-full")

    def _build_linear_sweep_voltammetry_inputs(self) -> None:
        with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-3 items-end"):
            self.linear_sweep_voltammetry_equilibration_input = self._free_number(
                "Equilibration (s)",
                value=self.state.linear_sweep_voltammetry_equilibration_time_s,
                minimum=0.0,
                precision=0,
            )
            self.linear_sweep_voltammetry_begin_potential_input = self._free_number(
                "Begin potential (V)",
                value=self.state.linear_sweep_voltammetry_begin_potential_v,
                precision=3,
            )
            self.linear_sweep_voltammetry_end_potential_input = self._free_number(
                "End potential (V)",
                value=self.state.linear_sweep_voltammetry_end_potential_v,
                precision=3,
            )
            self.linear_sweep_voltammetry_step_potential_input = self._free_number(
                "Step potential (V)",
                value=self.state.linear_sweep_voltammetry_step_potential_v,
                minimum=0.0,
                precision=6,
            )
            self.linear_sweep_voltammetry_scan_rate_input = self._free_number(
                "Scan rate (V/s)",
                value=self.state.linear_sweep_voltammetry_scan_rate_v_per_s,
                minimum=0.0,
                precision=6,
            )
            self.linear_sweep_voltammetry_sweep_count_input = ui.number(
                "Sweep count",
                value=self.state.linear_sweep_voltammetry_sweep_count,
                min=1,
                step=1,
                precision=0,
            ).classes("w-full")

    @staticmethod
    def _free_number(
        label: str, *, value: float | None, precision: int, minimum: float | None = None
    ):
        return (
            ui.number(label, value=value, min=minimum, precision=precision)
            .props('step="any"')
            .classes("w-full")
        )

    def _register_state_handlers(self) -> None:
        bindings = (
            (self.potential_input, "chronoamperometry_potential_v"),
            (self.duration_input, "chronoamperometry_duration_s"),
            (self.interval_input, "chronoamperometry_interval_s"),
            (
                self.chronoamperometry_equilibration_input,
                "chronoamperometry_equilibration_time_s",
            ),
            (self.begin_potential_input, "cyclic_voltammetry_begin_potential_v"),
            (self.vertex1_potential_input, "cyclic_voltammetry_vertex1_potential_v"),
            (self.vertex2_potential_input, "cyclic_voltammetry_vertex2_potential_v"),
            (self.step_potential_input, "cyclic_voltammetry_step_potential_v"),
            (self.scan_rate_input, "cyclic_voltammetry_scan_rate_v_per_s"),
            (self.scan_count_input, "cyclic_voltammetry_scan_count"),
            (
                self.cyclic_voltammetry_equilibration_input,
                "cyclic_voltammetry_equilibration_time_s",
            ),
            (
                self.linear_sweep_voltammetry_equilibration_input,
                "linear_sweep_voltammetry_equilibration_time_s",
            ),
            (
                self.linear_sweep_voltammetry_begin_potential_input,
                "linear_sweep_voltammetry_begin_potential_v",
            ),
            (
                self.linear_sweep_voltammetry_end_potential_input,
                "linear_sweep_voltammetry_end_potential_v",
            ),
            (
                self.linear_sweep_voltammetry_step_potential_input,
                "linear_sweep_voltammetry_step_potential_v",
            ),
            (
                self.linear_sweep_voltammetry_scan_rate_input,
                "linear_sweep_voltammetry_scan_rate_v_per_s",
            ),
            (
                self.linear_sweep_voltammetry_sweep_count_input,
                "linear_sweep_voltammetry_sweep_count",
            ),
        )
        for element, attribute in bindings:
            element.on_value_change(partial(self._store_value, attribute))
        self.measurement_tabs.on_value_change(self._store_measurement_type)

    def _store_value(self, attribute: str, event: Any) -> None:
        if self._syncing_values:
            return
        setattr(self.state, attribute, event.value)
        self.on_change()

    def _store_measurement_type(self, _: Any) -> None:
        if self._syncing_values:
            return
        self.state.measurement_type = self.measurement_type
        self.on_change()

    @staticmethod
    def _required_number(value: float | None, *, name: str) -> float:
        if value is None:
            raise ValueError(f"{name} is required")
        return float(value)

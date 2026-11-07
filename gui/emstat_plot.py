from typing import ClassVar

from nicegui import ui
from nicegui.events import GenericEventArguments

from controllers.emstat_controller import EmStatController, EmStatLoadedMeasurement
from palmsens.emstat4s import EmStatMeasurementType


class EmStatPlot:
    GRID_LEFT_PX = 72
    CV_GRID_RIGHT_PX = 132
    CV_COLORS = (
        "#4E79A7",
        "#F28E2B",
        "#E15759",
        "#76B7B2",
        "#59A14F",
        "#EDC948",
        "#B07AA1",
        "#FF9DA7",
        "#9C755F",
        "#BAB0AC",
    )
    PLOT_TITLES: ClassVar[dict[EmStatMeasurementType, str]] = {
        "chronoamperometry": "Chronoamperometry",
        "cyclic_voltammetry": "Cyclic voltammetry",
        "linear_sweep_voltammetry": "Linear sweep voltammetry",
    }
    X_AXIS_NAMES: ClassVar[dict[EmStatMeasurementType, str]] = {
        "chronoamperometry": "Time (s)",
        "cyclic_voltammetry": "Potential (V)",
        "linear_sweep_voltammetry": "Potential (V)",
    }
    X_AXIS_SCALE: ClassVar[dict[EmStatMeasurementType, bool]] = {
        "chronoamperometry": False,
        "cyclic_voltammetry": True,
        "linear_sweep_voltammetry": True,
    }
    STATE_LABELS: ClassVar[dict[str, str]] = {
        "idle": "Idle",
        "measuring": "Measuring",
        "aborting": "Aborting",
        "completed": "Completed",
        "aborted": "Aborted",
        "failed": "Failed",
    }
    TIGHT_AXIS_MIN = "(value) => value.min - (value.max - value.min || 1) * 0.05"
    TIGHT_AXIS_MAX = "(value) => value.max + (value.max - value.min || 1) * 0.05"
    POTENTIAL_SWEEP_TOOLTIP_FORMATTER = """
        (params) => {
            const point = Array.isArray(params) ? params[0] : params;
            if (!point) {
                return '';
            }

            const values = Array.isArray(point.value) ? point.value : point.data;
            if (!Array.isArray(values) || values.length < 2) {
                return '';
            }

            const formatValue = (value, decimals) => {
                const number = Number(value);
                return Number.isFinite(number)
                    ? Number(number.toFixed(decimals)).toString()
                    : String(value);
            };

            const marker = point.marker || '';
            const seriesName = point.seriesName || '';
            const potential = formatValue(values[0], 6);
            const current = formatValue(values[1], 3);

            return `${marker}${seriesName}<br/>`
                + `Potential: <b>${potential} V</b><br/>`
                + `Current: <b>${current} µA</b>`;
        }
    """

    def __init__(
        self, controller: EmStatController, *, update_interval_s: float = 0.25
    ) -> None:
        self.controller = controller
        self._showing_saved_measurement = False
        self._measurement_id: int | None = None
        self._data_revision: int | None = None
        self._next_indices: dict[int, int] = {}
        self._series_order: list[int] = []
        self._series_labels: dict[int, str] = {}
        self._points_by_series: dict[int, list[list[float]]] = {}
        self._pinned_series_name: str | None = None
        self._displayed_measurement_type: EmStatMeasurementType | None = None
        self._displayed_state: str | None = None
        self._displayed_point_count = 0
        self._displayed_error_message: str | None = None

        self._build()
        self._refresh()

        self._refresh_timer = ui.timer(update_interval_s, self._refresh)

    def _build(self) -> None:
        with ui.column().classes("w-full h-full gap-2"):
            with (
                ui.element("div")
                .classes("w-full")
                .style(
                    "display: grid; "
                    f"grid-template-columns: {self.GRID_LEFT_PX}px minmax(0, 1fr) auto; "
                    "align-items: center; "
                    "row-gap: 0.25rem;"
                )
            ):
                ui.element("div")

                self.title_label = ui.label("Chronoamperometry").classes(
                    "text-lg font-bold"
                )

                with ui.row().classes("items-center gap-3 justify-self-end"):
                    self.state_label = ui.label("Idle").classes("font-medium")
                    self.point_count_label = ui.label("0 points").classes(
                        "text-sm text-gray-500 whitespace-nowrap"
                    )

                self.error_label = (
                    ui.label("")
                    .classes("text-sm text-red-600 break-words")
                    .style("grid-column: 2 / -1;")
                )

            self.error_label.set_visibility(False)

            self.chart = (
                ui.echart(
                    {
                        "animation": False,
                        "color": self.CV_COLORS,
                        "grid": {
                            "left": self.GRID_LEFT_PX,
                            "right": 24,
                            "top": 24,
                            "bottom": 58,
                        },
                        "legend": {
                            "show": False,
                            "type": "scroll",
                            "orient": "vertical",
                            "right": 4,
                            "top": 24,
                            "bottom": 40,
                            "itemGap": 0,
                            "textStyle": {"lineHeight": 22},
                            "selectedMode": True,
                        },
                        "tooltip": {"trigger": "axis"},
                        "xAxis": {
                            "type": "value",
                            "name": "Time (s)",
                            "nameLocation": "middle",
                            "nameGap": 32,
                            "scale": False,
                        },
                        "yAxis": {
                            "type": "value",
                            "name": "Current (µA)",
                            "nameLocation": "middle",
                            "nameGap": 52,
                            "scale": False,
                        },
                        "series": [],
                    }
                )
                .classes("w-full flex-1")
                .style("min-height: 30rem;")
            )
            self.chart.on(
                "chart:legendselectchanged",
                self._handle_legend_selection_changed,
                ["name"],
            )

    def refresh(self) -> None:
        """Immediately synchronize a live plot with the controller."""

        self._refresh()

    def show_live(self) -> None:
        """Return to the current controller measurement and resume live updates."""

        self._showing_saved_measurement = False
        self._reset_series_state()
        self._measurement_id = None
        self._data_revision = None
        self._displayed_state = None
        self._displayed_error_message = None
        self._refresh()

    def show_saved_measurement(self, measurement: EmStatLoadedMeasurement) -> None:
        """Display one loaded session without modifying live controller state."""

        self._showing_saved_measurement = True
        self._reset_series_state()
        self._measurement_id = None
        self._data_revision = None

        for series in measurement.series:
            self._series_order.append(series.series_id)
            self._series_labels[series.series_id] = series.label
            self._points_by_series[series.series_id] = [
                [x_value, current_ua]
                for x_value, current_ua in zip(
                    series.x_values, series.currents_ua, strict=True
                )
            ]

        self._set_measurement_type(measurement.measurement_type)
        self._set_state_label("Saved")
        self._set_point_count(measurement.point_count)
        self._set_error_message(None)
        self._update_chart()

    def _refresh(self) -> None:
        if self._showing_saved_measurement:
            return

        snapshot = self.controller.snapshot
        data_slice = self.controller.data_since(
            measurement_id=self._measurement_id,
            data_revision=self._data_revision,
            start_indices=self._next_indices,
        )

        data_reset = (
            data_slice.measurement_id != self._measurement_id
            or data_slice.data_revision != self._data_revision
        )

        if data_reset:
            self._reset_series_state()
            self._measurement_id = data_slice.measurement_id
            self._data_revision = data_slice.data_revision

        chart_changed = data_reset

        for series_slice in data_slice.series:
            if series_slice.series_id not in self._points_by_series:
                self._series_order.append(series_slice.series_id)
                self._series_labels[series_slice.series_id] = series_slice.label
                self._points_by_series[series_slice.series_id] = []
                self._next_indices[series_slice.series_id] = 0
                chart_changed = True

            expected_start_index = self._next_indices[series_slice.series_id]
            if series_slice.start_index != expected_start_index:
                raise RuntimeError(
                    f"Unexpected start index for EmStat series {series_slice.series_id}"
                )

            if self._series_labels[series_slice.series_id] != series_slice.label:
                self._series_labels[series_slice.series_id] = series_slice.label
                chart_changed = True

            new_points = [
                [x_value, current_ua]
                for x_value, current_ua in zip(
                    series_slice.x_values, series_slice.currents_ua, strict=True
                )
            ]

            if new_points:
                self._points_by_series[series_slice.series_id].extend(new_points)
                chart_changed = True

            self._next_indices[series_slice.series_id] = series_slice.next_index

        measurement_type = snapshot.measurement_type or "chronoamperometry"
        if measurement_type != self._displayed_measurement_type:
            self._set_measurement_type(measurement_type)
            chart_changed = True

        if chart_changed:
            self._update_chart()

        self._set_state_label(self.STATE_LABELS[snapshot.state])
        self._set_point_count(
            sum(len(points) for points in self._points_by_series.values())
        )
        self._set_error_message(snapshot.error_message)

    def _set_measurement_type(self, measurement_type: EmStatMeasurementType) -> None:
        self._displayed_measurement_type = measurement_type
        self.title_label.set_text(self.PLOT_TITLES[measurement_type])

        x_axis = self.chart.options["xAxis"]
        y_axis = self.chart.options["yAxis"]

        x_axis["name"] = self.X_AXIS_NAMES[measurement_type]
        x_axis["scale"] = self.X_AXIS_SCALE[measurement_type]

        potential_sweep = measurement_type in (
            "cyclic_voltammetry",
            "linear_sweep_voltammetry",
        )
        y_axis["scale"] = potential_sweep

        for axis in (x_axis, y_axis):
            axis_label = axis.setdefault("axisLabel", {})
            axis_label["showMinLabel"] = not potential_sweep
            axis_label["showMaxLabel"] = not potential_sweep

            if potential_sweep:
                axis.pop("min", None)
                axis.pop("max", None)

                axis[":min"] = self.TIGHT_AXIS_MIN
                axis[":max"] = self.TIGHT_AXIS_MAX
            else:
                axis.pop(":min", None)
                axis.pop(":max", None)

                axis["min"] = None
                axis["max"] = None

        tooltip = self.chart.options["tooltip"]
        tooltip["trigger"] = "item" if potential_sweep else "axis"

        if potential_sweep:
            tooltip[":formatter"] = self.POTENTIAL_SWEEP_TOOLTIP_FORMATTER
        else:
            tooltip.pop(":formatter", None)

        if measurement_type not in ("cyclic_voltammetry", "linear_sweep_voltammetry"):
            self._pinned_series_name = None

    def _update_chart(self) -> None:
        series_selection_enabled = self._series_selection_enabled()
        available_names = {
            self._series_labels[series_id] for series_id in self._series_order
        }
        if self._pinned_series_name not in available_names:
            self._pinned_series_name = None
        if not series_selection_enabled:
            self._pinned_series_name = None

        self.chart.options["grid"]["right"] = (
            self.CV_GRID_RIGHT_PX if series_selection_enabled else 24
        )
        self.chart.options["legend"]["show"] = series_selection_enabled

        self.chart.options["legend"]["selected"] = {
            name: True for name in available_names
        }
        self.chart.options["series"] = [
            self._build_series_option(series_id) for series_id in self._series_order
        ]
        self.chart.update()

    def _build_series_option(self, series_id: int) -> dict[str, object]:
        name = self._series_labels[series_id]
        series_selection_enabled = self._series_selection_enabled()
        single_potential_series = self._single_potential_series()
        pinned = self._pinned_series_name
        muted = series_selection_enabled and pinned is not None and name != pinned
        selected = series_selection_enabled and name == pinned

        show_symbols = single_potential_series or (
            series_selection_enabled and selected
        )
        symbol_opacity = 0 if single_potential_series else (0.12 if muted else 1)

        series: dict[str, object] = {
            "name": name,
            "type": "line",
            "showSymbol": show_symbols,
            "symbolSize": 7 if single_potential_series else 5,
            "data": self._points_by_series[series_id],
            "silent": muted,
            "legendHoverLink": series_selection_enabled and pinned is None,
            "lineStyle": {
                "width": 3 if selected else 2,
                "opacity": 0.12 if muted else 1,
            },
            "itemStyle": {"opacity": symbol_opacity},
            "tooltip": {"show": not muted},
        }

        if series_selection_enabled:
            series["emphasis"] = {
                "focus": "series",
                "blurScope": "coordinateSystem",
                "lineStyle": {"width": 3, "opacity": 1},
                "itemStyle": {"opacity": 1},
            }
            series["blur"] = {
                "lineStyle": {"opacity": 0.12},
                "itemStyle": {"opacity": 0.12},
            }
        elif single_potential_series:
            series["emphasis"] = {
                "scale": True,
                "lineStyle": {"width": 2, "opacity": 1},
                "itemStyle": {"opacity": 1},
            }

        return series

    def _handle_legend_selection_changed(self, event: GenericEventArguments) -> None:
        name = event.args.get("name")

        if (
            not self._series_selection_enabled()
            or not isinstance(name, str)
            or name not in self._series_labels.values()
        ):
            return

        if self._pinned_series_name == name:
            self._pinned_series_name = None
        else:
            self._pinned_series_name = name

        self._update_chart()

    def _series_selection_enabled(self) -> bool:
        return (
            self._displayed_measurement_type
            in ("cyclic_voltammetry", "linear_sweep_voltammetry")
            and len(self._series_order) > 1
        )

    def _single_potential_series(self) -> bool:
        return (
            self._displayed_measurement_type
            in ("cyclic_voltammetry", "linear_sweep_voltammetry")
            and len(self._series_order) == 1
        )

    def _reset_series_state(self) -> None:
        self._next_indices.clear()
        self._series_order.clear()
        self._series_labels.clear()
        self._points_by_series.clear()
        self._pinned_series_name = None

    def _set_state_label(self, text: str) -> None:
        if text == self._displayed_state:
            return

        self._displayed_state = text
        self.state_label.set_text(text)

    def _set_point_count(self, point_count: int) -> None:
        if point_count == self._displayed_point_count:
            return

        self._displayed_point_count = point_count
        suffix = "point" if point_count == 1 else "points"
        self.point_count_label.set_text(f"{point_count} {suffix}")

    def _set_error_message(self, error_message: str | None) -> None:
        if error_message == self._displayed_error_message:
            return

        self._displayed_error_message = error_message

        if error_message is None:
            self.error_label.set_text("")
            self.error_label.set_visibility(False)
        else:
            self.error_label.set_text(error_message)
            self.error_label.set_visibility(True)

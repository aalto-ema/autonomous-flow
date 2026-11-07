from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, cast

from nicegui import run, ui
from nicegui.elements.checkbox import Checkbox
from nicegui.elements.number import Number
from nicegui.elements.select import Select
from nicegui.elements.tooltip import Tooltip

from controllers.experiment_controller import (
    ExperimentController,
    SourcePrimingState,
    SourcePrimingStatus,
)
from fluidics.runze_plumbing import SelectorPortConfig
from gui.runze_input_validation import (
    CONCENTRATION_FACTORS_TO_MOL_L,
    read_concentration_mol_l,
    read_non_negative_number,
)
from storage.experiment_repository import (
    WATER_SOURCE_CONFIG_KEY,
    CurrentSourceConfiguration,
    SourceConfigurationEntry,
    SourceStock,
    SourceStockComponentInput,
    Substance,
)

logger = logging.getLogger(__name__)


@dataclass
class SourceConfigurationRowState:
    source_stock_id: int | None = None
    control_substance_id: int | None = None
    enabled: bool = False
    fraction_locked: bool = False
    locked_fraction: float | None = 0.0


@dataclass
class SourceConfigurationPanelState:
    expanded: bool = True
    initialized: bool = False
    dirty: bool = False
    revision: int = 0
    rows: dict[int, SourceConfigurationRowState] = field(default_factory=dict)


@dataclass
class _StockComponentEditor:
    row: Any
    substance_select: Select
    formula_input: Any
    concentration_input: Number
    unit_select: Select
    pending_substance_name: str | None = None
    active: bool = True


class SourceConfigurationPanel:
    """Edit physical stock assignments and fixed recipe policies."""

    GRID_STYLE = (
        "display: grid; "
        "grid-template-columns: 3rem minmax(9rem, 12rem) "
        "minmax(18rem, 1fr) 2.5rem minmax(10rem, 12rem) "
        "4.5rem 4rem 8rem 8rem 9rem; "
        "column-gap: 0.75rem; row-gap: 0.5rem; align-items: center; "
        "min-width: 82rem;"
    )

    def __init__(
        self,
        controller: ExperimentController,
        *,
        state: SourceConfigurationPanelState | None = None,
        on_configuration_change: Callable[[CurrentSourceConfiguration], None],
        on_dirty_change: Callable[[bool], None] | None = None,
        on_ui_operation_change: Callable[[str | None], None],
    ) -> None:
        self.controller = controller
        self.repository = controller.repository
        self.state = state if state is not None else SourceConfigurationPanelState()
        self.on_configuration_change = on_configuration_change
        self.on_dirty_change = on_dirty_change or (lambda _: None)
        self.on_ui_operation_change = on_ui_operation_change

        plumbing = controller.runze_controller.plumbing
        if plumbing is None:
            raise ValueError("Source configuration requires Runze plumbing")
        self.plumbing = plumbing

        self._system_busy = controller.busy
        self._ui_operation: str | None = None
        self._syncing_values = False
        self._stocks: tuple[SourceStock, ...] = ()
        self._substances: tuple[Substance, ...] = ()
        self._stock_selects: dict[int, Select] = {}
        self._stock_select_tooltips: dict[int, Tooltip] = {}
        self._control_containers: dict[int, Any] = {}
        self._control_selects: dict[int, Select] = {}
        self._enabled_checkboxes: dict[int, Checkbox] = {}
        self._locked_checkboxes: dict[int, Checkbox] = {}
        self._fraction_inputs: dict[int, Number] = {}
        self._concentration_inputs: dict[int, Number] = {}
        self._line_state_selects: dict[int, Select] = {}
        self._line_state_tooltips: dict[int, Tooltip] = {}
        self._pending_line_state_ports: set[int] = set()
        self._programmatic_line_state_values: dict[int, SourcePrimingStatus] = {}
        self._component_editors: list[_StockComponentEditor] = []

        if not self.state.initialized:
            self._apply_configuration_to_state(controller.source_configuration)

        self._refresh_reference_data()
        self._build_add_stock_dialog()
        self._build()
        self._sync_controls()

    # UI construction ---

    def _build_add_stock_dialog(self) -> None:
        with (
            ui.dialog() as self.add_stock_dialog,
            ui.card().classes("w-[42rem] max-w-full gap-4"),
        ):
            ui.label("Add source stock").classes("text-lg font-bold")

            with ui.row().classes("w-full items-end gap-3"):
                self.new_stock_solvent_select = ui.select(
                    options=self._substance_options(),
                    value=self.repository.water_substance().id,
                    label="Solvent",
                ).classes("flex-1")
                self.new_stock_batch_input = ui.input(
                    "Batch / alias (optional)"
                ).classes("flex-1")

            ui.label("Solute components").classes("font-medium")
            self.stock_components_container = ui.column().classes("w-full gap-2")
            self._add_stock_component_row()

            with ui.row().classes("w-full items-center"):
                ui.button(
                    "Add component", on_click=self._add_stock_component_row
                ).props("outline")
                ui.space()
                ui.button("Cancel", on_click=self._close_stock_dialog).props("flat")
                self.create_stock_button = ui.button(
                    "Add stock", on_click=self._create_stock
                )

    def _add_stock_component_row(self) -> None:
        with self.stock_components_container:
            row = ui.row().classes("w-full items-end gap-2 flex-nowrap")
            with row:
                substance_select = (
                    ui.select(
                        options=self._solute_substance_options(),
                        label="Substance",
                        clearable=True,
                        new_value_mode="add",
                        key_generator=lambda value: value,
                    )
                    .props("hide-selected fill-input input-debounce=0")
                    .classes("flex-[2]")
                )
                formula_input = ui.input("Molecular formula").classes("flex-1")
                concentration_input = (
                    ui.number("Concentration", min=0.0, value=1.0, precision=6)
                    .props('step="any"')
                    .classes("w-36")
                )
                unit_select = ui.select(
                    options=list(CONCENTRATION_FACTORS_TO_MOL_L),
                    value="mM",
                    label="Unit",
                ).classes("w-24")
                remove_button = ui.button(icon="close").props(
                    "flat round dense color=grey"
                )

        editor = _StockComponentEditor(
            row=row,
            substance_select=substance_select,
            formula_input=formula_input,
            concentration_input=concentration_input,
            unit_select=unit_select,
        )
        self._component_editors.append(editor)
        formula_input.set_enabled(False)
        substance_select.on_value_change(
            partial(self._stock_component_substance_changed, editor)
        )
        remove_button.on_click(lambda: self._remove_stock_component(editor))

    def _stock_component_substance_changed(
        self, editor: _StockComponentEditor, event: Any
    ) -> None:
        value = event.value

        if isinstance(value, str):
            name = value.strip()
            if not name:
                self._clear_stock_component_substance(editor)
                return

            existing = self._substance_by_label(name)
            if existing is not None:
                self._select_existing_substance(editor, existing)
                return

            name_changed = editor.pending_substance_name != name
            editor.pending_substance_name = name
            if name_changed:
                options: dict[int | str, str] = {}
                for (
                    substance_id,
                    substance_label,
                ) in self._solute_substance_options().items():
                    options[substance_id] = substance_label
                options[name] = name
                editor.substance_select.set_options(options, value=name)
                editor.formula_input.set_value("")
            editor.formula_input.set_enabled(True)
            return

        if value is None:
            self._clear_stock_component_substance(editor)
            return

        substance_id = self._optional_positive_integer(value, name="Substance")
        if substance_id is None:
            self._clear_stock_component_substance(editor)
            return
        substance = self._substance_by_id(substance_id)
        if substance is None:
            self._clear_stock_component_substance(editor)
            ui.notify("The selected substance no longer exists", type="negative")
            return
        self._select_existing_substance(editor, substance)

    def _clear_stock_component_substance(self, editor: _StockComponentEditor) -> None:
        had_pending_value = editor.pending_substance_name is not None
        editor.pending_substance_name = None
        if had_pending_value:
            editor.substance_select.set_options(
                self._solute_substance_options(), value=None
            )
        editor.formula_input.set_value("")
        editor.formula_input.set_enabled(False)

    def _select_existing_substance(
        self, editor: _StockComponentEditor, substance: Substance
    ) -> None:
        had_pending_value = editor.pending_substance_name is not None
        editor.pending_substance_name = None
        if had_pending_value or editor.substance_select.value != substance.id:
            editor.substance_select.set_options(
                self._solute_substance_options(), value=substance.id
            )
        editor.formula_input.set_value(substance.molecular_formula or "")
        editor.formula_input.set_enabled(False)

    def _remove_stock_component(self, editor: _StockComponentEditor) -> None:
        active_count = sum(component.active for component in self._component_editors)
        if active_count <= 1:
            ui.notify("A stock requires at least one solute", type="warning")
            return
        editor.active = False
        editor.row.set_visibility(False)

    def _close_stock_dialog(self) -> None:
        self.add_stock_dialog.close()

    def _build(self) -> None:
        with (
            ui.card().classes("w-full p-0 gap-0"),
            (
                ui.expansion(
                    self._summary_text(),
                    value=self.state.expanded,
                    on_value_change=self._remember_expanded,
                )
                .classes("w-full")
                .props("header-class='text-lg font-bold px-4 py-3'")
            ) as self.expansion,
            ui.column().classes("w-full p-4 gap-4"),
        ):
            with (
                ui.element("div").classes("w-full overflow-x-auto"),
                ui.element("div").style(self.GRID_STYLE),
            ):
                self._build_grid_header()
                for port in sorted(
                    self.plumbing.selector_ports,
                    key=lambda configured_port: configured_port.number,
                ):
                    self._build_port_row(port)

            with (
                ui.element("div").classes("w-full overflow-x-auto"),
                ui.element("div").style(self.GRID_STYLE),
            ):
                self.configuration_status_label = (
                    ui.label(self._configuration_status_text())
                    .classes("text-sm text-gray-600")
                    .style("grid-column: 1 / 3;")
                )
                self.add_stock_button = (
                    ui.button("Add source stock", on_click=self.add_stock_dialog.open)
                    .props("outline")
                    .classes("whitespace-nowrap")
                    .style("grid-column: 3; justify-self: end;")
                )
                with (
                    ui.row()
                    .classes("items-center gap-2 justify-self-end")
                    .style("grid-column: 8 / 11;")
                ):
                    self.reload_button = ui.button(
                        icon="refresh", on_click=self._reload_saved_configuration
                    ).props("flat round dense color=primary")
                    self.reload_button_tooltip = self._create_tooltip(
                        self.reload_button,
                        "Revert configuration fields to the saved revision; "
                        "physical line states are unchanged",
                    )
                    self.save_button = ui.button(
                        "Save configuration", on_click=self._save_configuration
                    ).classes("whitespace-nowrap")
                    self.save_button_tooltip = self._create_tooltip(
                        self.save_button, "Save the source configuration"
                    )

    @staticmethod
    def _build_grid_header() -> None:
        for text in (
            "Port",
            "Configured route",
            "Source stock",
            "",
            "Controlled component",
            "Enabled",
            "Lock",
            "Fraction",
            "Final conc. (mM)",
            "Line state",
        ):
            ui.label(text).classes("text-sm font-medium text-gray-500")

    def _build_port_row(self, port: SelectorPortConfig) -> None:
        ui.label(str(port.number)).classes("font-medium")
        with ui.column().classes("gap-0 min-w-0"):
            ui.label(port.label).classes("font-medium")
            ui.label(self._port_role(port)).classes("text-xs text-gray-500")

        if not port.is_liquid_source:
            ui.label("Not an editable liquid source").classes("text-sm text-gray-500")
            ui.element("div")
            for _ in range(6):
                ui.label("—").classes("text-gray-400")
            return

        row_state = self.state.rows[port.number]
        is_water = port.key == WATER_SOURCE_CONFIG_KEY
        if is_water:
            with ui.column().classes("gap-0 min-w-0"):
                ui.label("Built-in water").classes("font-medium")
                ui.label("Balance diluent").classes("text-xs text-gray-500")
            ui.element("div")
            ui.label("Balance").classes("text-sm text-gray-500")
        else:
            stock_select = ui.select(
                options=self._stock_options(),
                value=row_state.source_stock_id,
                label="Stock",
                clearable=True,
                on_change=lambda event, selector_port=port.number: self._stock_changed(
                    selector_port, event.value
                ),
            ).classes("w-full")
            self._stock_selects[port.number] = stock_select
            self._stock_select_tooltips[port.number] = self._create_tooltip(
                stock_select,
                "A stock assignment can be changed only while its line is air-filled",
            )
            ui.element("div")
            control_container = ui.column().classes("w-full gap-0")
            self._control_containers[port.number] = control_container
            with control_container:
                self._build_control_cell(port.number)

        enabled_checkbox = ui.checkbox(
            value=row_state.enabled,
            on_change=lambda event, selector_port=port.number: self._enabled_changed(
                selector_port, bool(event.value)
            ),
        )
        self._enabled_checkboxes[port.number] = enabled_checkbox

        locked_checkbox = ui.checkbox(
            value=row_state.fraction_locked,
            on_change=lambda event, selector_port=port.number: self._locked_changed(
                selector_port, bool(event.value)
            ),
        )
        self._locked_checkboxes[port.number] = locked_checkbox

        fraction_input = (
            ui.number(
                value=row_state.locked_fraction,
                min=0.0,
                max=1.0,
                precision=6,
                on_change=lambda event, selector_port=port.number: (
                    self._fraction_changed(selector_port, event.value)
                ),
            )
            .props('step="any"')
            .classes("w-full")
        )
        self._fraction_inputs[port.number] = fraction_input

        if is_water:
            ui.label("—").classes("text-gray-400")
        else:
            concentration_input = (
                ui.number(
                    value=self._locked_concentration_mm(port.number),
                    min=0.0,
                    precision=6,
                    on_change=lambda event, selector_port=port.number: (
                        self._concentration_changed(selector_port, event.value)
                    ),
                )
                .props('step="any"')
                .classes("w-full")
            )
            self._concentration_inputs[port.number] = concentration_input

        physical_state = next(
            state
            for state in self.controller.snapshot.priming
            if state.selector_port == port.number
        )
        # QSelect briefly transforms its arrow and popup while opening. Keep those
        # visual effects from contributing to this horizontally scrollable grid's
        # measured width.
        with (
            ui.element("div")
            .classes("w-full min-w-0 max-w-full")
            .style("overflow-x: clip; contain: inline-size;")
        ):
            line_state_select = (
                ui.select(
                    options={
                        "unknown": "Unknown",
                        "air_filled": "Air-filled",
                        "primed": "Primed",
                    },
                    value=physical_state.status,
                    on_change=lambda event, selector_port=port.number: (
                        self._line_state_changed(selector_port, event.value)
                    ),
                )
                .props("options-cover")
                .classes("w-full min-w-0 max-w-full")
            )
        self._line_state_selects[port.number] = line_state_select
        self._line_state_tooltips[port.number] = self._create_tooltip(
            line_state_select, self._line_state_tooltip(physical_state)
        )

    def _build_control_cell(self, selector_port: int) -> None:
        row_state = self.state.rows[selector_port]
        stock = self._stock_by_id(row_state.source_stock_id)
        if stock is None:
            ui.label("—").classes("text-gray-400")
            return
        if len(stock.components) == 1:
            row_state.control_substance_id = stock.components[0].substance_id
            ui.label(stock.components[0].substance_label).classes("text-sm")
            return
        control_select = ui.select(
            options={
                component.substance_id: component.substance_label
                for component in stock.components
            },
            value=row_state.control_substance_id,
            label="Component",
            on_change=lambda event, port=selector_port: self._control_changed(
                port, event.value
            ),
        ).classes("w-full")
        self._control_selects[selector_port] = control_select

    # State callbacks ---

    def set_system_busy(self, busy: bool) -> None:
        self._system_busy = busy
        self._sync_controls()

    def _remember_expanded(self, event: Any) -> None:
        self.state.expanded = bool(event.value)

    async def _line_state_changed(self, selector_port: int, value: object) -> None:
        status = cast(SourcePrimingStatus, value)
        programmatic_value = self._programmatic_line_state_values.pop(
            selector_port, None
        )
        if programmatic_value == status:
            return
        if self._syncing_values:
            return
        if selector_port in self._pending_line_state_ports:
            return
        previous = cast(
            SourcePrimingStatus,
            next(
                state.status
                for state in self.controller.snapshot.priming
                if state.selector_port == selector_port
            ),
        )
        if status == previous:
            return

        self._pending_line_state_ports.add(selector_port)
        operation_started = False
        try:
            if status != "unknown":
                label = "Primed" if status == "primed" else "Air-filled"
                with ui.dialog() as dialog, ui.card().classes("max-w-md gap-3"):
                    ui.label("Confirm physical line state").classes("text-lg font-bold")
                    ui.label(
                        f"Confirm that source port {selector_port} is physically "
                        f"{label.lower()}. This records state only and does not move "
                        "the hardware."
                    )
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button(
                            "Cancel", on_click=lambda: dialog.submit(False)
                        ).props("flat")
                        ui.button("Confirm", on_click=lambda: dialog.submit(True))
                if not await dialog:
                    self._set_line_state_select_value(selector_port, previous)
                    return

            self._set_ui_operation("Updating physical line state")
            operation_started = True
            row = self.state.rows[selector_port]
            await run.io_bound(
                self.controller.set_source_line_state,
                selector_port=selector_port,
                status=status,
                proposed_enabled=row.enabled,
                proposed_source_stock_id=row.source_stock_id,
            )
        except Exception as error:
            logger.exception("Could not update physical source-line state")
            ui.notify(str(error), type="negative")
            self._set_line_state_select_value(selector_port, previous)
        finally:
            self._pending_line_state_ports.discard(selector_port)
            if operation_started:
                self._set_ui_operation(None)
            self._sync_controls()

    def _stock_changed(self, selector_port: int, value: object) -> None:
        row_state = self.state.rows[selector_port]
        stock_id = self._optional_stock_id(value)
        if row_state.source_stock_id == stock_id:
            return
        row_state.source_stock_id = stock_id
        stock = self._stock_by_id(stock_id)
        row_state.control_substance_id = (
            stock.components[0].substance_id
            if stock is not None and len(stock.components) == 1
            else None
        )
        if stock_id is None:
            row_state.fraction_locked = False
        self._mark_dirty()
        self._rebuild_control_cell(selector_port)
        self._sync_locked_concentration(selector_port)
        self._rebuild_for_reference_change()

    def _control_changed(self, selector_port: int, value: object) -> None:
        substance_id = self._optional_positive_integer(value, name="component")
        row_state = self.state.rows[selector_port]
        if row_state.control_substance_id != substance_id:
            row_state.control_substance_id = substance_id
            self._mark_dirty()
            self._sync_locked_concentration(selector_port)

    def _enabled_changed(self, selector_port: int, enabled: bool) -> None:
        row_state = self.state.rows[selector_port]
        normalized = True if selector_port == self.plumbing.water.number else enabled
        if row_state.enabled != normalized:
            row_state.enabled = normalized
            self._mark_dirty()
        if selector_port == self.plumbing.water.number:
            self._enabled_checkboxes[selector_port].set_value(True)
        self._sync_controls()

    def _locked_changed(self, selector_port: int, locked: bool) -> None:
        row_state = self.state.rows[selector_port]
        if row_state.fraction_locked != locked:
            row_state.fraction_locked = locked
            self._mark_dirty()
        self._sync_controls()

    def _fraction_changed(self, selector_port: int, value: float | None) -> None:
        if self._syncing_values:
            return
        row_state = self.state.rows[selector_port]
        if row_state.locked_fraction != value:
            row_state.locked_fraction = value
            self._mark_dirty()
            self._sync_locked_concentration(selector_port)

    def _concentration_changed(self, selector_port: int, value: float | None) -> None:
        if self._syncing_values or value is None:
            return
        row_state = self.state.rows[selector_port]
        stock_concentration = self._controlled_stock_concentration(row_state)
        if stock_concentration is None:
            return
        fraction = float(value) * 1e-3 / stock_concentration
        if fraction > 1:
            ui.notify(
                "Final concentration cannot exceed the selected stock concentration",
                type="negative",
            )
            return
        self._syncing_values = True
        try:
            row_state.locked_fraction = fraction
            self._fraction_inputs[selector_port].set_value(fraction)
        finally:
            self._syncing_values = False
        self._mark_dirty()

    # Repository actions ---

    async def _create_stock(self) -> None:
        try:
            components = await self._read_new_stock_components()
            solvent_id = self._optional_positive_integer(
                self.new_stock_solvent_select.value, name="Solvent"
            )
            if solvent_id is None:
                raise ValueError("Solvent is required")
        except (TypeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return

        self._set_ui_operation("Creating source stock")
        try:
            stock_id = await run.io_bound(
                partial(
                    self.repository.create_source_stock,
                    components=components,
                    solvent_substance_id=solvent_id,
                    batch_name=str(self.new_stock_batch_input.value or "").strip()
                    or None,
                )
            )
        except Exception as error:
            logger.exception("Creating source stock failed")
            ui.notify(str(error), type="negative")
        else:
            self._refresh_reference_data()
            self._reset_stock_dialog()
            self.add_stock_dialog.close()
            self._rebuild_for_reference_change()
            ui.notify(f"Added source stock ID {stock_id}", type="positive")
        finally:
            self._set_ui_operation(None)

    async def _read_new_stock_components(self) -> tuple[SourceStockComponentInput, ...]:
        components: list[SourceStockComponentInput] = []
        for index, editor in enumerate(
            component for component in self._component_editors if component.active
        ):
            value = editor.substance_select.value
            if isinstance(value, str):
                label = value.strip()
                if not label:
                    raise ValueError(f"Component {index + 1} substance is required")
                molecular_formula = str(editor.formula_input.value or "").strip()
                if not molecular_formula:
                    raise ValueError(
                        f"Component {index + 1} molecular formula is required "
                        "for a new substance"
                    )
                substance_id = await run.io_bound(
                    partial(
                        self.repository.find_or_create_substance,
                        key=self._substance_key(label),
                        label=label,
                        molecular_formula=molecular_formula,
                    )
                )
            else:
                substance_id = self._optional_positive_integer(
                    value, name=f"Component {index + 1} substance"
                )
            if substance_id is None:
                raise ValueError(f"Component {index + 1} substance is required")
            unit = str(editor.unit_select.value or "")
            concentration = read_concentration_mol_l(
                editor.concentration_input.value,
                unit=unit,
                name=f"Component {index + 1} concentration",
            )
            components.append(
                SourceStockComponentInput(
                    substance_id=cast(int, substance_id),
                    concentration_mol_l=concentration,
                )
            )
        return tuple(components)

    async def _save_configuration(self) -> None:
        try:
            entries = self._read_entries()
        except (TypeError, ValueError) as error:
            ui.notify(str(error), type="negative")
            return
        self._set_ui_operation("Saving source configuration")
        try:
            saved = await run.io_bound(
                partial(
                    self.controller.replace_source_configuration,
                    entries=entries,
                    expected_revision=self.state.revision,
                )
            )
        except Exception as error:
            logger.exception("Saving source configuration failed")
            ui.notify(str(error), type="negative")
        else:
            if saved is None:
                logger.error("Saving the source configuration returned no result")
                ui.notify(
                    "Saving the source configuration returned no result",
                    type="negative",
                )
            else:
                saved_configuration = cast(CurrentSourceConfiguration, saved)
                self._apply_configuration_to_state(saved_configuration)
                self.on_dirty_change(False)
                self.on_configuration_change(saved_configuration)
                self._sync_widgets_from_state()
                ui.notify(
                    "Source configuration revision "
                    f"{saved_configuration.revision} saved",
                    type="positive",
                )
        finally:
            self._set_ui_operation(None)

    def _reload_saved_configuration(self) -> None:
        if self._busy:
            return
        configuration = self.controller.source_configuration
        self._refresh_reference_data()
        self._apply_configuration_to_state(configuration)
        self.on_dirty_change(False)
        self.on_configuration_change(configuration)
        self._sync_widgets_from_state()
        ui.notify("Saved source configuration reloaded", type="positive")

    def _read_entries(self) -> tuple[SourceConfigurationEntry, ...]:
        entries: list[SourceConfigurationEntry] = []
        for port in self.plumbing.liquid_sources:
            is_water = port.key == WATER_SOURCE_CONFIG_KEY
            stock_id = (
                None
                if is_water
                else self._optional_stock_id(self._stock_selects[port.number].value)
            )
            enabled = (
                True if is_water else bool(self._enabled_checkboxes[port.number].value)
            )
            fraction_locked = bool(self._locked_checkboxes[port.number].value)
            locked_fraction = None
            if fraction_locked:
                locked_fraction = read_non_negative_number(
                    self._fraction_inputs[port.number].value,
                    name=f"{port.label} locked fraction",
                )
                if locked_fraction > 1:
                    raise ValueError(f"{port.label} locked fraction must not exceed 1")
            entries.append(
                SourceConfigurationEntry(
                    selector_port=port.number,
                    source_config_key=port.key,
                    source_stock_id=stock_id,
                    enabled=enabled,
                    locked_fraction=locked_fraction,
                    control_substance_id=(
                        None
                        if is_water
                        else self.state.rows[port.number].control_substance_id
                    ),
                )
            )
        return tuple(entries)

    # Display helpers ---

    @property
    def _busy(self) -> bool:
        return (
            self._system_busy
            or self._ui_operation is not None
            or self.controller.busy
            or self.controller.prepared_mixture is not None
        )

    def _sync_controls(self) -> None:
        busy = self._busy
        configuration_locked = busy or self.controller.source_configuration_locked
        priming_by_port = {
            state.selector_port: state for state in self.controller.snapshot.priming
        }
        for selector_port in self._line_state_selects:
            physical_state = priming_by_port[selector_port]
            if selector_port not in self._pending_line_state_ports:
                self._set_line_state_select_value(selector_port, physical_state.status)
            self._line_state_tooltips[selector_port].set_text(
                self._line_state_tooltip(physical_state)
            )
        for port in self.plumbing.liquid_sources:
            row = self.state.rows[port.number]
            is_water = port.key == WATER_SOURCE_CONFIG_KEY
            if not is_water:
                physical_state = priming_by_port[port.number]
                stock_editable = (
                    not configuration_locked and physical_state.status == "air_filled"
                )
                self._stock_selects[port.number].set_enabled(stock_editable)
                self._stock_select_tooltips[port.number].set_text(
                    "Select a source stock"
                    if stock_editable
                    else "A stock assignment can be changed only while its line "
                    "is air-filled"
                )
                control = self._control_selects.get(port.number)
                if control is not None:
                    control.set_enabled(
                        not configuration_locked and row.source_stock_id is not None
                    )
            self._enabled_checkboxes[port.number].set_enabled(
                not configuration_locked and not is_water
            )
            lock_available = is_water or row.source_stock_id is not None
            self._locked_checkboxes[port.number].set_enabled(
                not configuration_locked and lock_available
            )
            self._fraction_inputs[port.number].set_enabled(
                not configuration_locked and lock_available and row.fraction_locked
            )
            concentration = self._concentration_inputs.get(port.number)
            if concentration is not None:
                concentration.set_enabled(
                    not configuration_locked
                    and row.fraction_locked
                    and self._controlled_stock_concentration(row) is not None
                )

            self._line_state_selects[port.number].set_enabled(
                not busy
                and self.controller.source_line_state_editable(
                    selector_port=port.number, proposed_enabled=row.enabled
                )
            )

        enabled_lines_known = self._enabled_lines_have_known_state()
        self.add_stock_button.set_enabled(not busy)
        self.reload_button.set_enabled(not configuration_locked)
        self.save_button.set_enabled(not configuration_locked and enabled_lines_known)
        self.save_button_tooltip.set_text(
            "Save the source configuration"
            if enabled_lines_known
            else "Every enabled line must be marked Primed or Air-filled"
        )
        self.create_stock_button.set_enabled(not busy)
        self.configuration_status_label.set_text(self._configuration_status_text())
        self.expansion.set_text(self._summary_text())

    def _rebuild_for_reference_change(self) -> None:
        # Source rows contain conditional widgets (single-component label versus
        # multi-component selector). Rebuilding the page is intentionally left to
        # the next sub-page construction; update existing choices immediately.
        options = self._stock_options()
        for port, select in self._stock_selects.items():
            select.set_options(options, value=self.state.rows[port].source_stock_id)
        self._sync_controls()

    def _sync_widgets_from_state(self) -> None:
        options = self._stock_options()
        for port in self.plumbing.liquid_sources:
            row = self.state.rows[port.number]
            if port.key != WATER_SOURCE_CONFIG_KEY:
                self._stock_selects[port.number].set_options(
                    options, value=row.source_stock_id
                )
                self._rebuild_control_cell(port.number)
            self._enabled_checkboxes[port.number].set_value(row.enabled)
            self._locked_checkboxes[port.number].set_value(row.fraction_locked)
            self._fraction_inputs[port.number].set_value(row.locked_fraction)
            self._sync_locked_concentration(port.number)
            physical_state = next(
                state
                for state in self.controller.snapshot.priming
                if state.selector_port == port.number
            )
            self._set_line_state_select_value(port.number, physical_state.status)
            self._line_state_tooltips[port.number].set_text(
                self._line_state_tooltip(physical_state)
            )
        self._sync_controls()

    @staticmethod
    def _create_tooltip(element: Any, text: str) -> Tooltip:
        # Keep the tooltip in the control's slot instead of adding it as another
        # CSS-grid child. A targeted sibling can briefly affect grid overflow
        # while Quasar mounts or animates an overlay.
        with element:
            tooltip = Tooltip(text)
        return tooltip

    @staticmethod
    def _line_state_tooltip(state: SourcePrimingState) -> str:
        timestamp = (
            "not recorded"
            if state.updated_at is None
            else state.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        )
        return f"Updated {timestamp}. {state.reason}"

    def _set_line_state_select_value(
        self, selector_port: int, status: SourcePrimingStatus
    ) -> None:
        select = self._line_state_selects[selector_port]
        if select.value == status:
            return
        self._programmatic_line_state_values[selector_port] = status
        select.set_value(status)

    def _rebuild_control_cell(self, selector_port: int) -> None:
        container = self._control_containers.get(selector_port)
        if container is None:
            return
        self._control_selects.pop(selector_port, None)
        container.clear()
        with container:
            self._build_control_cell(selector_port)

    def _sync_locked_concentration(self, selector_port: int) -> None:
        concentration_input = self._concentration_inputs.get(selector_port)
        if concentration_input is None:
            return
        self._syncing_values = True
        try:
            concentration_input.set_value(self._locked_concentration_mm(selector_port))
        finally:
            self._syncing_values = False

    def _locked_concentration_mm(self, selector_port: int) -> float | None:
        row = self.state.rows[selector_port]
        concentration = self._controlled_stock_concentration(row)
        if concentration is None or row.locked_fraction is None:
            return None
        return row.locked_fraction * concentration * 1e3

    def _controlled_stock_concentration(
        self, row: SourceConfigurationRowState
    ) -> float | None:
        stock = self._stock_by_id(row.source_stock_id)
        if stock is None or row.control_substance_id is None:
            return None
        component = stock.component(row.control_substance_id)
        return None if component is None else component.concentration_mol_l

    def _set_ui_operation(self, operation: str | None) -> None:
        self._ui_operation = operation
        self.on_ui_operation_change(operation)
        self._sync_controls()

    def _mark_dirty(self) -> None:
        if self.state.dirty:
            return
        self.state.dirty = True
        self.on_dirty_change(True)

    def _refresh_reference_data(self) -> None:
        self._stocks = self.repository.list_source_stocks()
        self._substances = self.repository.list_substances()

    def _reset_stock_dialog(self) -> None:
        self.new_stock_batch_input.set_value("")
        water_id = self.repository.water_substance().id
        self.new_stock_solvent_select.set_options(
            self._substance_options(), value=water_id
        )
        for editor in self._component_editors:
            editor.active = False
            editor.row.set_visibility(False)
        self._add_stock_component_row()

    def _stock_options(self) -> dict[int, str]:
        return {stock.id: self._stock_option_label(stock) for stock in self._stocks}

    def _substance_options(self) -> dict[int, str]:
        return {substance.id: substance.label for substance in self._substances}

    def _solute_substance_options(self) -> dict[int, str]:
        return {
            substance.id: substance.label
            for substance in self._substances
            if substance.key != "water"
        }

    def _substance_by_id(self, substance_id: int) -> Substance | None:
        return next(
            (
                substance
                for substance in self._substances
                if substance.id == substance_id
            ),
            None,
        )

    def _substance_by_label(self, label: str) -> Substance | None:
        normalized = label.casefold()
        return next(
            (
                substance
                for substance in self._substances
                if substance.label.casefold() == normalized
            ),
            None,
        )

    @staticmethod
    def _stock_option_label(stock: SourceStock) -> str:
        batch = "" if stock.batch_name is None else f" · {stock.batch_name}"
        return f"{stock.label}{batch} · ID {stock.id}"

    def _stock_by_id(self, source_stock_id: int | None) -> SourceStock | None:
        if source_stock_id is None:
            return None
        return next(
            (stock for stock in self._stocks if stock.id == source_stock_id), None
        )

    def _apply_configuration_to_state(
        self, configuration: CurrentSourceConfiguration
    ) -> None:
        entry_by_port = {entry.selector_port: entry for entry in configuration.entries}
        rows: dict[int, SourceConfigurationRowState] = {}
        for port in self.plumbing.liquid_sources:
            entry = entry_by_port.get(port.number)
            locked_fraction = None if entry is None else entry.locked_fraction
            rows[port.number] = SourceConfigurationRowState(
                source_stock_id=(
                    None
                    if port.key == WATER_SOURCE_CONFIG_KEY or entry is None
                    else entry.source_stock_id
                ),
                control_substance_id=(
                    None if entry is None else entry.control_substance_id
                ),
                enabled=(
                    True
                    if port.key == WATER_SOURCE_CONFIG_KEY
                    else entry is not None and entry.enabled
                ),
                fraction_locked=locked_fraction is not None,
                locked_fraction=0.0 if locked_fraction is None else locked_fraction,
            )
        self.state.rows = rows
        self.state.revision = configuration.revision
        self.state.initialized = True
        self.state.dirty = False

    def _summary_text(self) -> str:
        enabled_count = sum(row.enabled for row in self.state.rows.values())
        assigned_count = sum(
            row.source_stock_id is not None for row in self.state.rows.values()
        )
        summary = (
            f"Source configuration · revision {self.state.revision} · "
            f"{enabled_count} enabled · {assigned_count} prepared stocks"
        )
        return summary + (" · unsaved changes" if self.state.dirty else "")

    def _configuration_status_text(self) -> str:
        if self._ui_operation is not None:
            return self._ui_operation
        if not self._enabled_lines_have_known_state():
            return "Enabled source lines cannot have Unknown physical state"
        if self.state.dirty:
            return f"Unsaved changes based on revision {self.state.revision}"
        if self.state.revision == 0:
            return "No source configuration has been saved yet"
        return f"Saved revision {self.state.revision}"

    def _enabled_lines_have_known_state(self) -> bool:
        state_by_port = {
            state.selector_port: state for state in self.controller.snapshot.priming
        }
        for port, row in self.state.rows.items():
            if not row.enabled:
                continue
            physical_state = state_by_port.get(port)
            if physical_state is None:
                return False
            if physical_state.status == "air_filled":
                continue
            if (
                physical_state.status == "primed"
                and physical_state.source_stock_id == row.source_stock_id
            ):
                continue
            return False
        return True

    @staticmethod
    def _port_role(port: SelectorPortConfig) -> str:
        if port.source_kind == "liquid":
            return "Liquid source"
        if port.source_kind == "air":
            return "Air source"
        if port.destination_kind is not None:
            return f"Destination · {port.destination_kind.replace('_', ' ')}"
        return "Configured route"

    @staticmethod
    def _optional_stock_id(value: object) -> int | None:
        return SourceConfigurationPanel._optional_positive_integer(
            value, name="Source stock"
        )

    @staticmethod
    def _optional_positive_integer(value: object, *, name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} selection is invalid")
        numeric = float(value)
        if not numeric.is_integer() or numeric < 1:
            raise ValueError(f"{name} selection is invalid")
        return int(numeric)

    @staticmethod
    def _substance_key(label: str) -> str:
        ascii_label = (
            unicodedata.normalize("NFKD", label)
            .encode("ascii", "ignore")
            .decode("ascii")
            .lower()
        )
        key = re.sub(r"[^a-z0-9]+", "_", ascii_label).strip("_")
        if not key:
            raise ValueError("Substance name must contain a letter or number")
        return key

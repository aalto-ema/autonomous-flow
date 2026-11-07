from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from common.validation import (
    require_finite_number,
    require_integer_in_range,
    require_non_negative_finite_number,
    require_positive_finite_number,
    require_positive_integer,
)

MixtureStatus = Literal["planned", "running", "completed", "failed", "aborted"]
MeasurementStatus = Literal["planned", "running", "completed", "failed", "aborted"]
MeasurementOrigin = Literal["manual", "experiment", "optimization", "imported", "test"]
MeasurementType = Literal[
    "chronoamperometry", "cyclic_voltammetry", "linear_sweep_voltammetry"
]
SourceSetupMatchKind = Literal[
    "stock_records", "component_layout", "relative_formulation", "exact_composition"
]
PhysicalSourceLineStatus = Literal["primed", "air_filled", "unknown"]
PersistedWorkflowPhase = Literal[
    "inactive",
    "ready",
    "prepared",
    "flow_completed",
    "sources_recovered",
    "sources_rinsed",
    "complete",
    "recovery_required",
]

MIXTURE_STATUSES = {"planned", "running", "completed", "failed", "aborted"}
MEASUREMENT_STATUSES = {"planned", "running", "completed", "failed", "aborted"}
MEASUREMENT_ORIGINS = {"manual", "experiment", "optimization", "imported", "test"}
MEASUREMENT_TYPES = {
    "chronoamperometry",
    "cyclic_voltammetry",
    "linear_sweep_voltammetry",
}
TERMINAL_STATUSES = {"completed", "failed", "aborted"}

SCHEMA_VERSION = 1
FRACTION_SUM_TOLERANCE = 1e-9
CONCENTRATION_MATCH_EPSILON_MOL_L = 1e-12
WATER_SUBSTANCE_KEY = "water"
WATER_SOURCE_CONFIG_KEY = "water"


@dataclass(frozen=True, slots=True)
class Substance:
    """Stable chemical identity shared by stocks and mixture snapshots."""

    id: int
    key: str
    label: str
    molecular_formula: str | None


@dataclass(frozen=True, slots=True)
class SourceStockComponent:
    substance_id: int
    substance_key: str
    substance_label: str
    molecular_formula: str | None
    concentration_mol_l: float


@dataclass(frozen=True, slots=True)
class SourceStockComponentInput:
    substance_id: int
    concentration_mol_l: float


@dataclass(frozen=True, slots=True)
class SourceStock:
    """One recorded stock solution or source bottle."""

    id: int
    label: str
    solvent_substance_id: int
    solvent_label: str
    components: tuple[SourceStockComponent, ...]
    component_layout_fingerprint: str
    exact_composition_fingerprint: str
    relative_formulation_fingerprint: str
    batch_name: str | None
    prepared_at: str | None

    def component(self, substance_id: int) -> SourceStockComponent | None:
        return next(
            (
                component
                for component in self.components
                if component.substance_id == substance_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class SourceSetupIdentity:
    """Order-independent identity of the enabled non-water source stocks."""

    source_stock_ids: tuple[int, ...]
    stock_fingerprint: str
    component_layout_fingerprint: str
    relative_formulation_fingerprint: str
    exact_composition_fingerprint: str
    has_multicomponent_stock: bool


@dataclass(frozen=True, slots=True)
class SourceSetupMatch:
    kind: SourceSetupMatchKind
    fingerprint: str


@dataclass(frozen=True, slots=True)
class MixtureConcentrationConstraint:
    substance_id: int
    target_mol_l: float
    tolerance_mol_l: float


@dataclass(frozen=True, slots=True)
class SourceConfigurationEntry:
    """Current assignment and recipe policy for one physical liquid port.

    Disabled entries may retain a stock and locked fraction as an operator
    convenience. They do not participate in active source-set identity,
    priming, or mixture construction.
    """

    selector_port: int
    source_config_key: str
    source_stock_id: int | None
    enabled: bool
    locked_fraction: float | None = None
    control_substance_id: int | None = None


@dataclass(frozen=True, slots=True)
class CurrentSourceConfiguration:
    revision: int
    entries: tuple[SourceConfigurationEntry, ...]

    @property
    def enabled_entries(self) -> tuple[SourceConfigurationEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)


@dataclass(frozen=True, slots=True)
class MixtureComponent:
    """One source contribution, identified independently of port or order."""

    source_stock_id: int | None
    fraction: float
    selector_port: int | None = None
    source_config_key: str | None = None
    locked_fraction: float | None = None


@dataclass(frozen=True, slots=True)
class SourceFraction:
    source_stock_id: int | None
    source_config_key: str | None
    fraction: float


@dataclass(frozen=True, slots=True)
class MixtureSubstanceConcentration:
    substance_id: int
    substance_key: str
    substance_label: str
    concentration_mol_l: float


@dataclass(frozen=True, slots=True)
class MetricObservation:
    """One completed result with the composition needed by an optimizer."""

    mixture_id: int
    measurement_id: int
    source_setup_id: int
    measurement_type: MeasurementType
    measurement_name: str | None
    run_sequence_number: int
    settings_fingerprint: str
    protocol_key: str | None
    analysis_version: str
    value: float
    unit: str
    standard_error: float | None
    computed_at: str
    contributions: tuple[MetricContribution, ...]
    fractions: tuple[SourceFraction, ...]


@dataclass(frozen=True, slots=True)
class MetricContribution:
    sequence_number: int
    kind: str
    value: float


@dataclass(frozen=True, slots=True)
class MeasurementMetric:
    measurement_id: int
    metric_name: str
    value: float
    unit: str
    standard_error: float | None
    analysis_method: str
    analysis_version: str
    computed_at: str
    contributions: tuple[MetricContribution, ...]


@dataclass(frozen=True, slots=True)
class CompletedMeasurementForAnalysis:
    """A completed, mixture-linked raw session that can be reanalyzed."""

    measurement_id: int
    mixture_id: int
    measurement_type: MeasurementType
    settings: dict[str, object]
    session_file_path: Path


@dataclass(frozen=True, slots=True)
class SavedExperimentMeasurement:
    measurement_id: int
    sequence_number: int
    measurement_type: MeasurementType
    measurement_name: str | None
    status: MeasurementStatus
    settings: dict[str, object]
    context: dict[str, object]
    session_file_path: Path | None
    created_at: str


@dataclass(frozen=True, slots=True)
class SavedExperimentRun:
    mixture_id: int
    artifact_directory: str
    protocol_snapshot: dict[str, object]
    mixture_context: dict[str, object]
    created_at: str
    measurements: tuple[SavedExperimentMeasurement, ...]


@dataclass(frozen=True, slots=True)
class PersistedSourceLineState:
    selector_port: int
    source_config_key: str
    source_stock_id: int | None
    status: PhysicalSourceLineStatus
    updated_at: str
    reason: str


@dataclass(frozen=True, slots=True)
class PersistedExperimentWorkspaceState:
    workflow_phase: PersistedWorkflowPhase
    operation_in_progress: bool
    active_plan: dict[str, object] | None
    mixture_id: int | None
    prepared_volume_state: str | None
    maintenance_source_ports: tuple[int, ...]
    last_operation: str | None
    updated_at: str
    source_lines: tuple[PersistedSourceLineState, ...]


class ExperimentRepository:
    """SQLite persistence for source setups, mixtures, and measurements.

    Each operation uses a short-lived connection, allowing the repository to be
    called from NiceGUI and worker threads without sharing a SQLite connection.
    PalmSens session files remain the source of raw curve data; the database
    stores their experimental context and calculated metrics.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def find_or_create_substance(
        self,
        *,
        key: str,
        label: str,
        molecular_formula: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> int:
        """Return a stable substance identity, creating it when necessary."""

        key = _normalize_key(key, name="key")
        label = _require_text(label, name="label")
        molecular_formula = _optional_text(molecular_formula, name="molecular_formula")

        with self._connection() as connection, connection:
            existing = connection.execute(
                "SELECT id, label, molecular_formula FROM substances WHERE key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                existing_formula = cast(str | None, existing["molecular_formula"])
                if cast(str, existing["label"]) != label:
                    raise ValueError(
                        f"Substance key {key!r} already uses label "
                        f"{existing['label']!r}"
                    )
                if existing_formula != molecular_formula:
                    raise ValueError(
                        f"Substance key {key!r} already uses molecular formula "
                        f"{existing_formula!r}"
                    )
                return cast(int, existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO substances (
                    key, label, molecular_formula, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, label, molecular_formula, _canonical_json(metadata), _utc_now()),
            )
            return _last_inserted_id(cursor)

    def list_substances(self) -> tuple[Substance, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, key, label, molecular_formula
                FROM substances
                ORDER BY CASE WHEN key = 'water' THEN 0 ELSE 1 END, lower(label), id
                """
            ).fetchall()
        return tuple(_substance_from_row(row) for row in rows)

    def water_substance(self) -> Substance:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, key, label, molecular_formula
                FROM substances
                WHERE key = ?
                """,
                (WATER_SUBSTANCE_KEY,),
            ).fetchone()
        if row is None:
            raise RuntimeError("The built-in Water substance is missing")
        return _substance_from_row(row)

    def create_source_stock(
        self,
        *,
        components: Sequence[SourceStockComponentInput],
        solvent_substance_id: int | None = None,
        batch_name: str | None = None,
        prepared_at: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> int:
        """Record one actual stock preparation with molar solute concentrations."""

        normalized_components = _validate_stock_component_inputs(components)
        batch_name = _optional_text(batch_name, name="batch_name")
        prepared_at = _optional_text(prepared_at, name="prepared_at")

        with self._connection() as connection, connection:
            if solvent_substance_id is None:
                water_row = connection.execute(
                    "SELECT id FROM substances WHERE key = ?", (WATER_SUBSTANCE_KEY,)
                ).fetchone()
                if water_row is None:
                    raise RuntimeError("The built-in Water substance is missing")
                solvent_substance_id = cast(int, water_row["id"])
            else:
                solvent_substance_id = _positive_integer(
                    solvent_substance_id, name="solvent_substance_id"
                )

            substance_ids = (
                solvent_substance_id,
                *(component.substance_id for component in normalized_components),
            )
            substance_rows = _substance_rows(connection, substance_ids)
            substance_by_id = {cast(int, row["id"]): row for row in substance_rows}
            missing = set(substance_ids) - set(substance_by_id)
            if missing:
                values = ", ".join(str(value) for value in sorted(missing))
                raise ValueError(f"Unknown substance IDs: {values}")
            if any(
                component.substance_id == solvent_substance_id
                for component in normalized_components
            ):
                raise ValueError("A stock solvent cannot also be a solute component")

            (component_layout_fingerprint, exact_fingerprint, relative_fingerprint) = (
                _stock_fingerprints(
                    solvent_substance_id=solvent_substance_id,
                    components=normalized_components,
                )
            )
            label = _stock_label(
                components=normalized_components, substance_by_id=substance_by_id
            )
            cursor = connection.execute(
                """
                INSERT INTO source_stocks (
                    label,
                    solvent_substance_id,
                    component_layout_fingerprint,
                    exact_composition_fingerprint,
                    relative_formulation_fingerprint,
                    batch_name,
                    prepared_at,
                    metadata_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    label,
                    solvent_substance_id,
                    component_layout_fingerprint,
                    exact_fingerprint,
                    relative_fingerprint,
                    batch_name,
                    prepared_at,
                    _canonical_json(metadata),
                    _utc_now(),
                ),
            )
            source_stock_id = _last_inserted_id(cursor)
            connection.executemany(
                """
                INSERT INTO source_stock_components (
                    source_stock_id,
                    substance_id,
                    concentration_mol_l,
                    display_order
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        source_stock_id,
                        component.substance_id,
                        component.concentration_mol_l,
                        index,
                    )
                    for index, component in enumerate(normalized_components)
                ),
            )
            return source_stock_id

    def list_source_stocks(self) -> tuple[SourceStock, ...]:
        """Return every recorded source stock in stable display order."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    source_stocks.*,
                    substances.label AS solvent_label
                FROM source_stocks
                JOIN substances ON substances.id = source_stocks.solvent_substance_id
                ORDER BY lower(source_stocks.label), source_stocks.id
                """
            ).fetchall()
            return tuple(_source_stock_from_row(connection, row) for row in rows)

    def source_stock(self, source_stock_id: int) -> SourceStock:
        source_stock_id = _positive_integer(source_stock_id, name="source_stock_id")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    source_stocks.*,
                    substances.label AS solvent_label
                FROM source_stocks
                JOIN substances ON substances.id = source_stocks.solvent_substance_id
                WHERE source_stocks.id = ?
                """,
                (source_stock_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Source stock {source_stock_id} does not exist")
            return _source_stock_from_row(connection, row)

    def load_current_source_configuration(self) -> CurrentSourceConfiguration:
        """Load the one editable source configuration used by the application."""

        with self._connection() as connection:
            revision_row = connection.execute(
                """
                SELECT revision
                FROM current_source_configuration
                WHERE singleton_id = 1
                """
            ).fetchone()
            rows = connection.execute(
                """
                SELECT
                    selector_port,
                    source_config_key,
                    source_stock_id,
                    enabled,
                    locked_fraction,
                    control_substance_id
                FROM current_source_assignments
                ORDER BY selector_port
                """
            ).fetchall()

        revision = 0 if revision_row is None else cast(int, revision_row["revision"])
        return CurrentSourceConfiguration(
            revision=revision,
            entries=tuple(_source_configuration_entry_from_row(row) for row in rows),
        )

    def replace_current_source_configuration(
        self,
        *,
        entries: Sequence[SourceConfigurationEntry],
        expected_revision: int | None = None,
    ) -> CurrentSourceConfiguration:
        """Atomically replace the persisted editable source configuration.

        expected_revision provides optimistic concurrency protection for a
        future GUI. Disabled entries are persisted for convenience but are
        ignored when active source setups and mixtures are created.
        """

        normalized_entries = _validate_source_configuration_entries(entries)
        if expected_revision is not None:
            require_integer_in_range(
                expected_revision,
                name="expected_revision",
                minimum=0,
                maximum=2**63 - 1,
            )

        assigned_stock_ids = tuple(
            entry.source_stock_id
            for entry in normalized_entries
            if entry.source_stock_id is not None
        )

        with self._connection() as connection, connection:
            current = connection.execute(
                """
                SELECT revision
                FROM current_source_configuration
                WHERE singleton_id = 1
                """
            ).fetchone()
            current_revision = 0 if current is None else cast(int, current["revision"])

            if expected_revision is not None and expected_revision != current_revision:
                raise RuntimeError(
                    "Source configuration changed since it was loaded: "
                    f"expected revision {expected_revision}, "
                    f"current revision {current_revision}"
                )

            if assigned_stock_ids:
                found_ids = {
                    cast(int, row["id"])
                    for row in _source_rows(connection, assigned_stock_ids)
                }
                missing_ids = set(assigned_stock_ids) - found_ids
                if missing_ids:
                    missing = ", ".join(str(value) for value in sorted(missing_ids))
                    raise ValueError(f"Unknown source stock IDs: {missing}")

            normalized_entries = _normalize_configuration_controls(
                connection, normalized_entries
            )

            new_revision = current_revision + 1
            now = _utc_now()
            connection.execute("DELETE FROM current_source_assignments")
            connection.executemany(
                """
                INSERT INTO current_source_assignments (
                    selector_port,
                    source_config_key,
                    source_stock_id,
                    enabled,
                    locked_fraction,
                    control_substance_id,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        entry.selector_port,
                        entry.source_config_key,
                        entry.source_stock_id,
                        int(entry.enabled),
                        entry.locked_fraction,
                        entry.control_substance_id,
                        now,
                    )
                    for entry in normalized_entries
                ),
            )
            connection.execute(
                """
                INSERT INTO current_source_configuration (
                    singleton_id,
                    revision,
                    updated_at
                )
                VALUES (1, ?, ?)
                ON CONFLICT (singleton_id)
                DO UPDATE SET
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (new_revision, now),
            )

        return CurrentSourceConfiguration(
            revision=new_revision, entries=normalized_entries
        )

    def find_or_create_source_setup(
        self, *, source_stock_ids: Sequence[int], name: str | None = None
    ) -> int:
        """Return the setup for an unordered set of source-stock identities.

        Selector ports and configuration order do not participate in setup
        identity. Calling this method with the same stocks in another order
        returns the existing setup ID.
        """

        source_stock_ids = _normalize_source_stock_ids(source_stock_ids)
        name = _optional_text(name, name="name")

        with self._connection() as connection, connection:
            source_rows = _source_rows(connection, source_stock_ids)
            identity = _source_setup_identity(source_stock_ids, source_rows)

            existing = connection.execute(
                "SELECT id FROM source_setups WHERE stock_fingerprint = ?",
                (identity.stock_fingerprint,),
            ).fetchone()
            if existing is not None:
                return cast(int, existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO source_setups (
                    name,
                    stock_fingerprint,
                    component_layout_fingerprint,
                    exact_composition_fingerprint,
                    relative_formulation_fingerprint,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    identity.stock_fingerprint,
                    identity.component_layout_fingerprint,
                    identity.exact_composition_fingerprint,
                    identity.relative_formulation_fingerprint,
                    _utc_now(),
                ),
            )
            source_setup_id = _last_inserted_id(cursor)
            connection.executemany(
                """
                INSERT INTO source_setup_members (source_setup_id, source_stock_id)
                VALUES (?, ?)
                """,
                (
                    (source_setup_id, source_stock_id)
                    for source_stock_id in source_stock_ids
                ),
            )

            return source_setup_id

    def source_setup_identity(
        self, *, source_stock_ids: Sequence[int]
    ) -> SourceSetupIdentity:
        """Calculate setup fingerprints without creating a setup record."""

        source_stock_ids = _normalize_source_stock_ids(source_stock_ids)
        with self._connection() as connection:
            source_rows = _source_rows(connection, source_stock_ids)
        return _source_setup_identity(source_stock_ids, source_rows)

    def find_source_setup_id(self, *, source_stock_ids: Sequence[int]) -> int | None:
        """Find an unordered source-stock setup without creating database rows."""

        source_stock_ids = _normalize_source_stock_ids(source_stock_ids)
        identity = self.source_setup_identity(source_stock_ids=source_stock_ids)

        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM source_setups WHERE stock_fingerprint = ?",
                (identity.stock_fingerprint,),
            ).fetchone()

        return None if row is None else cast(int, row["id"])

    def source_setup_members(self, source_setup_id: int) -> tuple[int, ...]:
        source_setup_id = _positive_integer(source_setup_id, name="source_setup_id")

        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM source_setups WHERE id = ?", (source_setup_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"Source setup {source_setup_id} does not exist")
            rows = connection.execute(
                """
                SELECT source_stock_id
                FROM source_setup_members
                WHERE source_setup_id = ?
                ORDER BY source_stock_id
                """,
                (source_setup_id,),
            ).fetchall()

        return tuple(cast(int, row["source_stock_id"]) for row in rows)

    def create_mixture(
        self,
        *,
        source_setup_id: int,
        total_volume_ml: float,
        components: Sequence[MixtureComponent],
        source_configuration_revision: int,
        context: Mapping[str, object] | None = None,
        protocol_snapshot: Mapping[str, object],
        artifact_directory: str | None = None,
        commanded_volumes_ml: Mapping[str, float] | None = None,
    ) -> int:
        """Create one requested mixture and its identity-bound fractions."""

        source_setup_id = _positive_integer(source_setup_id, name="source_setup_id")
        require_positive_finite_number(total_volume_ml, name="total_volume_ml")
        require_integer_in_range(
            source_configuration_revision,
            name="source_configuration_revision",
            minimum=0,
            maximum=2**63 - 1,
        )
        normalized_components = _validate_components(components)
        if commanded_volumes_ml is not None:
            for component in normalized_components:
                value = commanded_volumes_ml[component.source_config_key]
                require_non_negative_finite_number(value, name="commanded_volume_ml")

        with self._connection() as connection, connection:
            setup_exists = connection.execute(
                "SELECT 1 FROM source_setups WHERE id = ?", (source_setup_id,)
            ).fetchone()
            if setup_exists is None:
                raise ValueError(f"Source setup {source_setup_id} does not exist")
            setup_source_ids = {
                cast(int, row["source_stock_id"])
                for row in connection.execute(
                    """
                    SELECT source_stock_id
                    FROM source_setup_members
                    WHERE source_setup_id = ?
                    """,
                    (source_setup_id,),
                ).fetchall()
            }
            component_source_ids = {
                component.source_stock_id
                for component in normalized_components
                if component.source_stock_id is not None
            }
            if component_source_ids != setup_source_ids:
                raise ValueError(
                    _component_membership_error(
                        setup_source_ids=setup_source_ids,
                        component_source_ids=component_source_ids,
                    )
                )

            created_at = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO mixtures (
                    source_setup_id,
                    source_configuration_revision,
                    status,
                    total_volume_ml,
                    context_json, protocol_snapshot_json, artifact_directory,
                    created_at
                )
                VALUES (?, ?, 'planned', ?, ?, ?, ?, ?)
                """,
                (
                    source_setup_id,
                    source_configuration_revision,
                    float(total_volume_ml),
                    _canonical_json(context),
                    _canonical_json(protocol_snapshot),
                    _artifact_directory(artifact_directory),
                    created_at,
                ),
            )
            mixture_id = _last_inserted_id(cursor)
            if artifact_directory is None:
                artifact_directory = f"{mixture_id:06d}_experiment"
                connection.execute(
                    "UPDATE mixtures SET artifact_directory = ? WHERE id = ?",
                    (artifact_directory, mixture_id),
                )
            connection.executemany(
                """
                INSERT INTO mixture_components (
                    mixture_id,
                    source_stock_id,
                    fraction,
                    commanded_volume_ml,
                    selector_port,
                    source_config_key,
                    locked_fraction
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        mixture_id,
                        component.source_stock_id,
                        component.fraction,
                        (
                            float(total_volume_ml) * component.fraction
                            if commanded_volumes_ml is None
                            else commanded_volumes_ml[component.source_config_key]
                        ),
                        component.selector_port,
                        component.source_config_key,
                        component.locked_fraction,
                    )
                    for component in normalized_components
                ),
            )

            concentrations = _calculate_mixture_substance_concentrations(
                connection, normalized_components
            )
            connection.executemany(
                """
                INSERT INTO mixture_substance_concentrations (
                    mixture_id, substance_id, concentration_mol_l
                )
                VALUES (?, ?, ?)
                """,
                (
                    (mixture_id, substance_id, concentration_mol_l)
                    for substance_id, concentration_mol_l in concentrations.items()
                ),
            )

            return mixture_id

    def mixture_substance_concentrations(
        self, mixture_id: int
    ) -> tuple[MixtureSubstanceConcentration, ...]:
        mixture_id = _positive_integer(mixture_id, name="mixture_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    substances.id AS substance_id,
                    substances.key AS substance_key,
                    substances.label AS substance_label,
                    mixture_substance_concentrations.concentration_mol_l
                FROM mixture_substance_concentrations
                JOIN substances
                    ON substances.id = mixture_substance_concentrations.substance_id
                WHERE mixture_substance_concentrations.mixture_id = ?
                ORDER BY lower(substances.label), substances.id
                """,
                (mixture_id,),
            ).fetchall()
        return tuple(
            MixtureSubstanceConcentration(
                substance_id=cast(int, row["substance_id"]),
                substance_key=cast(str, row["substance_key"]),
                substance_label=cast(str, row["substance_label"]),
                concentration_mol_l=cast(float, row["concentration_mol_l"]),
            )
            for row in rows
        )

    def set_mixture_status(
        self,
        mixture_id: int,
        status: MixtureStatus,
        *,
        error_message: str | None = None,
    ) -> None:
        mixture_id = _positive_integer(mixture_id, name="mixture_id")
        status = _mixture_status(status)
        error_message = _optional_text(error_message, name="error_message")
        now = _utc_now()

        with self._connection() as connection, connection:
            current = connection.execute(
                "SELECT started_at FROM mixtures WHERE id = ?", (mixture_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"Mixture {mixture_id} does not exist")

            started_at = cast(str | None, current["started_at"])
            if started_at is None and status == "running":
                started_at = now

            connection.execute(
                """
                UPDATE mixtures
                SET status = ?, started_at = ?, finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    status,
                    started_at,
                    now if status in TERMINAL_STATUSES else None,
                    error_message,
                    mixture_id,
                ),
            )

    def create_measurement(
        self,
        *,
        measurement_type: MeasurementType,
        origin: MeasurementOrigin,
        settings: Mapping[str, object],
        mixture_id: int | None = None,
        measurement_name: str | None = None,
        protocol_key: str | None = None,
        flow_rpm: int | None = None,
        flow_rate_ml_min: float | None = None,
        flow_volume_ml: float | None = None,
        flow_started_at: str | None = None,
        context: Mapping[str, object] | None = None,
        run_sequence_number: int | None = None,
    ) -> int:
        """Create a planned CA, CV, or LSV measurement.

        mixture_id may be None for independent manual or dummy-cell tests.
        """

        measurement_type = _measurement_type(measurement_type)
        origin = _measurement_origin(origin)
        if mixture_id is not None:
            mixture_id = _positive_integer(mixture_id, name="mixture_id")
        if flow_rpm is not None:
            flow_rpm = _positive_integer(flow_rpm, name="flow_rpm")
        if flow_rate_ml_min is not None:
            require_positive_finite_number(flow_rate_ml_min, name="flow_rate_ml_min")
        if flow_volume_ml is not None:
            require_non_negative_finite_number(flow_volume_ml, name="flow_volume_ml")
        if run_sequence_number is not None:
            run_sequence_number = _positive_integer(
                run_sequence_number, name="run_sequence_number"
            )
        if origin == "experiment":
            if mixture_id is None:
                raise ValueError("Experiment measurements require a mixture")
            if run_sequence_number is None:
                raise ValueError(
                    "Experiment measurements require a run sequence number"
                )

        settings_json = _canonical_json(settings)
        with self._connection() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO measurements (
                    mixture_id,
                    origin,
                    measurement_type,
                    status,
                    measurement_name,
                    settings_json,
                    settings_fingerprint,
                    protocol_key,
                    flow_rpm,
                    flow_rate_ml_min,
                    flow_volume_ml,
                    flow_started_at,
                    context_json,
                    run_sequence_number,
                    created_at
                )
                VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mixture_id,
                    origin,
                    measurement_type,
                    _optional_text(measurement_name, name="measurement_name"),
                    settings_json,
                    _fingerprint("settings", (settings_json,)),
                    _optional_text(protocol_key, name="protocol_key"),
                    flow_rpm,
                    flow_rate_ml_min,
                    flow_volume_ml,
                    _optional_text(flow_started_at, name="flow_started_at"),
                    _canonical_json(context),
                    run_sequence_number,
                    _utc_now(),
                ),
            )

            return _last_inserted_id(cursor)

    def set_measurement_status(
        self,
        measurement_id: int,
        status: MeasurementStatus,
        *,
        session_file_path: str | Path | None = None,
        point_count: int | None = None,
        elapsed_s: float | None = None,
        flow_started_at: str | None = None,
        error_message: str | None = None,
    ) -> None:
        measurement_id = _positive_integer(measurement_id, name="measurement_id")
        status = _measurement_status(status)
        if point_count is not None:
            require_integer_in_range(
                point_count, name="point_count", minimum=0, maximum=2**63 - 1
            )
        if elapsed_s is not None:
            require_non_negative_finite_number(elapsed_s, name="elapsed_s")
        flow_started_at = _optional_text(flow_started_at, name="flow_started_at")

        path = (
            Path(session_file_path).as_posix()
            if session_file_path is not None
            else None
        )
        now = _utc_now()

        with self._connection() as connection, connection:
            current = connection.execute(
                "SELECT * FROM measurements WHERE id = ?", (measurement_id,)
            ).fetchone()
            if current is None:
                raise ValueError(f"Measurement {measurement_id} does not exist")

            path = path or cast(str | None, current["session_file_path"])
            if status == "completed" and path is None:
                raise ValueError("A completed measurement requires session_file_path")

            started_at = cast(str | None, current["started_at"])
            if started_at is None and status == "running":
                started_at = now

            connection.execute(
                """
                UPDATE measurements
                SET
                    status = ?,
                    session_file_path = ?,
                    point_count = ?,
                    elapsed_s = ?,
                    flow_started_at = ?,
                    started_at = ?,
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    status,
                    path,
                    point_count if point_count is not None else current["point_count"],
                    elapsed_s if elapsed_s is not None else current["elapsed_s"],
                    flow_started_at
                    if flow_started_at is not None
                    else current["flow_started_at"],
                    started_at,
                    now if status in TERMINAL_STATUSES else None,
                    _optional_text(error_message, name="error_message"),
                    measurement_id,
                ),
            )

    def save_measurement_metric(
        self,
        *,
        measurement_id: int,
        metric_name: str,
        value: float,
        unit: str,
        analysis_method: str,
        analysis_version: str,
        standard_error: float | None = None,
        analysis_settings: Mapping[str, object] | None = None,
        contributions: Sequence[MetricContribution] = (),
    ) -> None:
        """Insert or replace one deterministic, versioned analysis result."""

        measurement_id = _positive_integer(measurement_id, name="measurement_id")
        metric_name = _require_text(metric_name, name="metric_name")
        require_finite_number(value, name="value")
        unit = _require_text(unit, name="unit")
        analysis_method = _require_text(analysis_method, name="analysis_method")
        analysis_version = _require_text(analysis_version, name="analysis_version")
        if standard_error is not None:
            require_non_negative_finite_number(standard_error, name="standard_error")
        normalized_contributions: list[MetricContribution] = []
        seen_component_keys: set[tuple[str, int]] = set()
        for contribution in contributions:
            if not isinstance(contribution, MetricContribution):
                raise TypeError("contributions must contain MetricContribution values")
            sequence_number = _positive_integer(
                contribution.sequence_number, name="contribution.sequence_number"
            )
            kind = _require_text(contribution.kind, name="contribution.kind")
            require_finite_number(contribution.value, name="contribution.value")
            key = (kind, sequence_number)
            if key in seen_component_keys:
                raise ValueError("Metric contribution kind and sequence must be unique")
            seen_component_keys.add(key)
            normalized_contributions.append(
                MetricContribution(sequence_number, kind, float(contribution.value))
            )

        settings_json = _canonical_json(analysis_settings)
        with self._connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO measurement_metrics (
                    measurement_id,
                    metric_name,
                    value,
                    unit,
                    standard_error,
                    analysis_method,
                    analysis_version,
                    analysis_settings_json,
                    analysis_fingerprint,
                    computed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    measurement_id,
                    metric_name,
                    analysis_method,
                    analysis_version,
                    analysis_fingerprint
                )
                DO UPDATE SET
                    value = excluded.value,
                    unit = excluded.unit,
                    standard_error = excluded.standard_error,
                    computed_at = excluded.computed_at
                """,
                (
                    measurement_id,
                    metric_name,
                    float(value),
                    unit,
                    standard_error,
                    analysis_method,
                    analysis_version,
                    settings_json,
                    _fingerprint("analysis-settings", (settings_json,)),
                    _utc_now(),
                ),
            )
            metric_row = connection.execute(
                """
                SELECT id
                FROM measurement_metrics
                WHERE measurement_id = ?
                  AND metric_name = ?
                  AND analysis_method = ?
                  AND analysis_version = ?
                  AND analysis_fingerprint = ?
                """,
                (
                    measurement_id,
                    metric_name,
                    analysis_method,
                    analysis_version,
                    _fingerprint("analysis-settings", (settings_json,)),
                ),
            ).fetchone()
            if metric_row is None:
                raise RuntimeError("Saved measurement metric could not be reloaded")
            metric_id = cast(int, metric_row["id"])
            connection.execute(
                "DELETE FROM measurement_metric_components "
                "WHERE measurement_metric_id = ?",
                (metric_id,),
            )
            connection.executemany(
                """
                INSERT INTO measurement_metric_components (
                    measurement_metric_id, sequence_number, component_kind, value
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    (metric_id, item.sequence_number, item.kind, item.value)
                    for item in normalized_contributions
                ),
            )

    def list_measurement_metrics(
        self, *, measurement_id: int
    ) -> tuple[MeasurementMetric, ...]:
        """Return every persisted analysis result for one measurement."""

        measurement_id = _positive_integer(measurement_id, name="measurement_id")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, metric_name, value, unit, standard_error,
                       analysis_method, analysis_version, computed_at
                FROM measurement_metrics
                WHERE measurement_id = ?
                ORDER BY metric_name, analysis_version
                """,
                (measurement_id,),
            ).fetchall()
            contribution_map = _metric_contributions_by_metric_id(
                connection, tuple(cast(int, row["id"]) for row in rows)
            )
        return tuple(
            MeasurementMetric(
                measurement_id=measurement_id,
                metric_name=cast(str, row["metric_name"]),
                value=cast(float, row["value"]),
                unit=cast(str, row["unit"]),
                standard_error=cast(float | None, row["standard_error"]),
                analysis_method=cast(str, row["analysis_method"]),
                analysis_version=cast(str, row["analysis_version"]),
                computed_at=cast(str, row["computed_at"]),
                contributions=contribution_map.get(cast(int, row["id"]), ()),
            )
            for row in rows
        )

    def list_completed_measurements_for_analysis(
        self, *, measurement_type: MeasurementType | None = None
    ) -> tuple[CompletedMeasurementForAnalysis, ...]:
        """Return completed experiment sessions suitable for deterministic analysis."""

        parameters: list[object] = []
        query = """
            SELECT
                measurements.id AS measurement_id,
                measurements.mixture_id,
                measurements.measurement_type,
                measurements.settings_json,
                measurements.session_file_path
            FROM measurements
            JOIN mixtures ON mixtures.id = measurements.mixture_id
            WHERE
                measurements.status = 'completed'
                AND mixtures.status = 'completed'
                AND measurements.session_file_path IS NOT NULL
        """
        if measurement_type is not None:
            query += " AND measurements.measurement_type = ?"
            parameters.append(_measurement_type(measurement_type))
        query += " ORDER BY measurements.id"

        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()

        return tuple(
            CompletedMeasurementForAnalysis(
                measurement_id=cast(int, row["measurement_id"]),
                mixture_id=cast(int, row["mixture_id"]),
                measurement_type=_measurement_type(row["measurement_type"]),
                settings=_json_object(row["settings_json"], name="settings"),
                session_file_path=Path(cast(str, row["session_file_path"])),
            )
            for row in rows
        )

    def list_saved_experiment_runs(self) -> tuple[SavedExperimentRun, ...]:
        """Return workspace experiment runs with ordered measurement records."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    mixtures.id AS mixture_id,
                    mixtures.artifact_directory,
                    mixtures.protocol_snapshot_json,
                    mixtures.context_json AS mixture_context_json,
                    mixtures.created_at AS mixture_created_at,
                    measurements.id AS measurement_id,
                    measurements.run_sequence_number,
                    measurements.measurement_type,
                    measurements.measurement_name,
                    measurements.status AS measurement_status,
                    measurements.settings_json,
                    measurements.context_json AS measurement_context_json,
                    measurements.session_file_path,
                    measurements.created_at AS measurement_created_at
                FROM mixtures
                JOIN measurements ON measurements.mixture_id = mixtures.id
                WHERE measurements.origin = 'experiment'
                ORDER BY mixtures.id DESC,
                         measurements.run_sequence_number,
                         measurements.id
                """
            ).fetchall()

        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(cast(int, row["mixture_id"]), []).append(row)

        runs: list[SavedExperimentRun] = []
        for run_rows in grouped.values():
            first = run_rows[0]
            measurements = tuple(
                SavedExperimentMeasurement(
                    measurement_id=cast(int, row["measurement_id"]),
                    sequence_number=cast(int, row["run_sequence_number"]),
                    measurement_type=_measurement_type(row["measurement_type"]),
                    measurement_name=cast(str | None, row["measurement_name"]),
                    status=_measurement_status(row["measurement_status"]),
                    settings=_json_object(row["settings_json"], name="settings"),
                    context=_json_object(
                        row["measurement_context_json"], name="measurement context"
                    ),
                    session_file_path=(
                        None
                        if row["session_file_path"] is None
                        else Path(cast(str, row["session_file_path"]))
                    ),
                    created_at=cast(str, row["measurement_created_at"]),
                )
                for row in run_rows
            )
            runs.append(
                SavedExperimentRun(
                    mixture_id=cast(int, first["mixture_id"]),
                    artifact_directory=_require_text(
                        first["artifact_directory"], name="artifact directory"
                    ),
                    protocol_snapshot=_json_object(
                        first["protocol_snapshot_json"], name="protocol snapshot"
                    ),
                    mixture_context=_json_object(
                        first["mixture_context_json"], name="mixture context"
                    ),
                    created_at=cast(str, first["mixture_created_at"]),
                    measurements=measurements,
                )
            )
        return tuple(runs)

    def list_metric_observations(
        self,
        *,
        measurement_type: MeasurementType,
        metric_name: str,
        analysis_method: str,
        analysis_version: str,
        source_setup_match: SourceSetupMatch | None = None,
        concentration_constraints: Sequence[MixtureConcentrationConstraint] = (),
        settings_fingerprint: str | None = None,
        protocol_key: str | None = None,
    ) -> tuple[MetricObservation, ...]:
        """Return completed compatible results for a future optimizer."""

        setup_column: str | None = None
        setup_fingerprint: str | None = None
        if source_setup_match is not None:
            if not isinstance(source_setup_match, SourceSetupMatch):
                raise TypeError("source_setup_match must be a SourceSetupMatch")
            setup_column = _source_setup_match_column(source_setup_match.kind)
            setup_fingerprint = _require_text(
                source_setup_match.fingerprint, name="source_setup_match.fingerprint"
            )
        normalized_constraints = _validate_concentration_constraints(
            concentration_constraints
        )
        measurement_type = _measurement_type(measurement_type)
        metric_name = _require_text(metric_name, name="metric_name")
        analysis_method = _require_text(analysis_method, name="analysis_method")
        analysis_version = _require_text(analysis_version, name="analysis_version")

        query = """
            SELECT
                measurement_metrics.id AS metric_id,
                mixtures.id AS mixture_id,
                mixtures.source_setup_id,
                measurements.id AS measurement_id,
                measurements.measurement_type,
                measurements.measurement_name,
                measurements.run_sequence_number,
                measurements.settings_fingerprint,
                measurements.protocol_key,
                measurement_metrics.value,
                measurement_metrics.unit,
                measurement_metrics.standard_error,
                measurement_metrics.analysis_version,
                measurement_metrics.computed_at
            FROM measurement_metrics
            JOIN measurements
                ON measurements.id = measurement_metrics.measurement_id
            JOIN mixtures
                ON mixtures.id = measurements.mixture_id
            JOIN source_setups
                ON source_setups.id = mixtures.source_setup_id
            WHERE
                mixtures.status = 'completed'
                AND measurements.status = 'completed'
                AND measurements.origin = 'experiment'
                AND measurements.run_sequence_number IS NOT NULL
                AND measurements.measurement_type = ?
                AND measurement_metrics.metric_name = ?
                AND measurement_metrics.analysis_method = ?
                AND measurement_metrics.analysis_version = ?
        """
        parameters: list[object] = [
            measurement_type,
            metric_name,
            analysis_method,
            analysis_version,
        ]
        if setup_column is not None and setup_fingerprint is not None:
            query += f" AND source_setups.{setup_column} = ?"
            parameters.append(setup_fingerprint)
        for constraint in normalized_constraints:
            query += """
                AND ABS(
                    COALESCE(
                        (
                            SELECT concentration_mol_l
                            FROM mixture_substance_concentrations
                            WHERE
                                mixture_id = mixtures.id
                                AND substance_id = ?
                        ),
                        0.0
                    ) - ?
                ) <= ?
            """
            parameters.extend(
                (
                    constraint.substance_id,
                    constraint.target_mol_l,
                    constraint.tolerance_mol_l,
                )
            )
        if settings_fingerprint is not None:
            query += " AND measurements.settings_fingerprint = ?"
            parameters.append(
                _require_text(settings_fingerprint, name="settings_fingerprint")
            )
        if protocol_key is not None:
            query += " AND measurements.protocol_key = ?"
            parameters.append(_require_text(protocol_key, name="protocol_key"))
        query += " ORDER BY measurements.id"

        with self._connection() as connection:
            result_rows = connection.execute(query, parameters).fetchall()
            contribution_map = _metric_contributions_by_metric_id(
                connection, tuple(cast(int, row["metric_id"]) for row in result_rows)
            )
            observations = [
                MetricObservation(
                    mixture_id=cast(int, row["mixture_id"]),
                    measurement_id=cast(int, row["measurement_id"]),
                    source_setup_id=cast(int, row["source_setup_id"]),
                    measurement_type=_measurement_type(row["measurement_type"]),
                    measurement_name=cast(str | None, row["measurement_name"]),
                    run_sequence_number=(cast(int, row["run_sequence_number"])),
                    settings_fingerprint=cast(str, row["settings_fingerprint"]),
                    protocol_key=cast(str | None, row["protocol_key"]),
                    analysis_version=cast(str, row["analysis_version"]),
                    value=cast(float, row["value"]),
                    unit=cast(str, row["unit"]),
                    standard_error=cast(float | None, row["standard_error"]),
                    computed_at=cast(str, row["computed_at"]),
                    contributions=contribution_map.get(cast(int, row["metric_id"]), ()),
                    fractions=_mixture_fractions(
                        connection, cast(int, row["mixture_id"])
                    ),
                )
                for row in result_rows
            ]

        return tuple(observations)

    @staticmethod
    def settings_fingerprint(settings: Mapping[str, object]) -> str:
        """Return the canonical fingerprint stored for measurement settings."""

        return _fingerprint("settings", (_canonical_json(settings),))

    def load_experiment_workspace_state(self) -> PersistedExperimentWorkspaceState:
        """Load the single durable experiment-workspace checkpoint."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT workflow_phase, operation_in_progress, active_plan_json,
                       mixture_id, prepared_volume_state,
                       maintenance_source_ports_json, last_operation, updated_at
                FROM experiment_workspace_state
                WHERE singleton_id = 1
                """
            ).fetchone()
            line_rows = connection.execute(
                """
                SELECT selector_port, source_config_key, source_stock_id, status,
                       updated_at, reason
                FROM source_line_physical_states
                ORDER BY selector_port
                """
            ).fetchall()

        if row is None:
            raise RuntimeError("The experiment workspace state row is missing")
        return PersistedExperimentWorkspaceState(
            workflow_phase=_workflow_phase(row["workflow_phase"]),
            operation_in_progress=bool(row["operation_in_progress"]),
            active_plan=_optional_json_object(
                row["active_plan_json"], name="active plan"
            ),
            mixture_id=cast(int | None, row["mixture_id"]),
            prepared_volume_state=_prepared_volume_state(row["prepared_volume_state"]),
            maintenance_source_ports=_json_integer_tuple(
                row["maintenance_source_ports_json"], name="maintenance source ports"
            ),
            last_operation=cast(str | None, row["last_operation"]),
            updated_at=cast(str, row["updated_at"]),
            source_lines=tuple(
                PersistedSourceLineState(
                    selector_port=cast(int, line["selector_port"]),
                    source_config_key=cast(str, line["source_config_key"]),
                    source_stock_id=cast(int | None, line["source_stock_id"]),
                    status=_physical_source_line_status(line["status"]),
                    updated_at=cast(str, line["updated_at"]),
                    reason=cast(str, line["reason"]),
                )
                for line in line_rows
            ),
        )

    def save_experiment_workspace_state(
        self, state: PersistedExperimentWorkspaceState
    ) -> None:
        """Atomically replace the workspace checkpoint and physical line states."""

        if not isinstance(state, PersistedExperimentWorkspaceState):
            raise TypeError("state must be a PersistedExperimentWorkspaceState")
        workflow_phase = _workflow_phase(state.workflow_phase)
        if state.mixture_id is not None:
            _positive_integer(state.mixture_id, name="state.mixture_id")
        updated_at = _require_text(state.updated_at, name="state.updated_at")
        prepared_volume_state = _prepared_volume_state(state.prepared_volume_state)
        maintenance_ports = tuple(state.maintenance_source_ports)
        for port in maintenance_ports:
            require_integer_in_range(port, minimum=1, maximum=10, name="selector port")
        if len(set(maintenance_ports)) != len(maintenance_ports):
            raise ValueError("maintenance source ports must be unique")

        normalized_lines: list[PersistedSourceLineState] = []
        seen_ports: set[int] = set()
        for line in state.source_lines:
            port = line.selector_port
            require_integer_in_range(port, minimum=1, maximum=10, name="selector port")
            if port in seen_ports:
                raise ValueError(f"Duplicate physical source line for port {port}")
            seen_ports.add(port)
            normalized_lines.append(
                PersistedSourceLineState(
                    selector_port=port,
                    source_config_key=_require_text(
                        line.source_config_key, name="source_config_key"
                    ),
                    source_stock_id=(
                        None
                        if line.source_stock_id is None
                        else _positive_integer(
                            line.source_stock_id, name="source_stock_id"
                        )
                    ),
                    status=_physical_source_line_status(line.status),
                    updated_at=_require_text(line.updated_at, name="line.updated_at"),
                    reason=_require_text(line.reason, name="line.reason"),
                )
            )

        with self._connection() as connection, connection:
            connection.execute(
                """
                UPDATE experiment_workspace_state
                SET workflow_phase = ?, operation_in_progress = ?,
                    active_plan_json = ?, mixture_id = ?,
                    prepared_volume_state = ?, maintenance_source_ports_json = ?,
                    last_operation = ?, updated_at = ?
                WHERE singleton_id = 1
                """,
                (
                    workflow_phase,
                    int(state.operation_in_progress),
                    (
                        None
                        if state.active_plan is None
                        else _canonical_json(state.active_plan)
                    ),
                    state.mixture_id,
                    prepared_volume_state,
                    json.dumps(maintenance_ports, separators=(",", ":")),
                    state.last_operation,
                    updated_at,
                ),
            )
            connection.execute("DELETE FROM source_line_physical_states")
            connection.executemany(
                """
                INSERT INTO source_line_physical_states (
                    selector_port, source_config_key, source_stock_id,
                    status, updated_at, reason
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        line.selector_port,
                        line.source_config_key,
                        line.source_stock_id,
                        line.status,
                        line.updated_at,
                        line.reason,
                    )
                    for line in normalized_lines
                ),
            )

    def _initialize_database(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])

            if version == 0:
                connection.executescript(_SCHEMA)
                return

            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is not supported; "
                    f"expected version {SCHEMA_VERSION}"
                )

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")

        try:
            yield connection
        finally:
            connection.close()


def _source_rows(
    connection: sqlite3.Connection, source_stock_ids: Sequence[int]
) -> list[sqlite3.Row]:
    if not source_stock_ids:
        return []
    placeholders = ", ".join("?" for _ in source_stock_ids)
    return connection.execute(
        f"""
        SELECT
            id,
            component_layout_fingerprint,
            exact_composition_fingerprint,
            relative_formulation_fingerprint,
            (
                SELECT COUNT(*)
                FROM source_stock_components
                WHERE source_stock_id = source_stocks.id
            ) AS component_count
        FROM source_stocks
        WHERE id IN ({placeholders})
        """,
        tuple(source_stock_ids),
    ).fetchall()


def _metric_contributions_by_metric_id(
    connection: sqlite3.Connection, metric_ids: Sequence[int]
) -> dict[int, tuple[MetricContribution, ...]]:
    if not metric_ids:
        return {}
    placeholders = ", ".join("?" for _ in metric_ids)
    rows = connection.execute(
        f"""
        SELECT measurement_metric_id, sequence_number, component_kind, value
        FROM measurement_metric_components
        WHERE measurement_metric_id IN ({placeholders})
        ORDER BY measurement_metric_id, sequence_number, id
        """,
        tuple(metric_ids),
    ).fetchall()
    grouped: dict[int, list[MetricContribution]] = {}
    for row in rows:
        grouped.setdefault(cast(int, row["measurement_metric_id"]), []).append(
            MetricContribution(
                sequence_number=cast(int, row["sequence_number"]),
                kind=cast(str, row["component_kind"]),
                value=cast(float, row["value"]),
            )
        )
    return {metric_id: tuple(items) for metric_id, items in grouped.items()}


def _source_setup_identity(
    source_stock_ids: Sequence[int], source_rows: Sequence[sqlite3.Row]
) -> SourceSetupIdentity:
    found_ids = {cast(int, row["id"]) for row in source_rows}
    missing_ids = set(source_stock_ids) - found_ids
    if missing_ids:
        missing = ", ".join(str(value) for value in sorted(missing_ids))
        raise ValueError(f"Unknown source stock IDs: {missing}")

    return SourceSetupIdentity(
        source_stock_ids=tuple(source_stock_ids),
        stock_fingerprint=_fingerprint(
            "source-stocks", tuple(str(value) for value in source_stock_ids)
        ),
        component_layout_fingerprint=_fingerprint(
            "stock-component-layouts",
            tuple(
                sorted(
                    cast(str, row["component_layout_fingerprint"])
                    for row in source_rows
                )
            ),
        ),
        relative_formulation_fingerprint=_fingerprint(
            "relative-formulations",
            tuple(
                sorted(
                    cast(str, row["relative_formulation_fingerprint"])
                    for row in source_rows
                )
            ),
        ),
        exact_composition_fingerprint=_fingerprint(
            "stock-compositions",
            tuple(
                sorted(
                    cast(str, row["exact_composition_fingerprint"])
                    for row in source_rows
                )
            ),
        ),
        has_multicomponent_stock=any(
            cast(int, row["component_count"]) > 1 for row in source_rows
        ),
    )


def _substance_rows(
    connection: sqlite3.Connection, substance_ids: Sequence[int]
) -> list[sqlite3.Row]:
    if not substance_ids:
        return []
    placeholders = ", ".join("?" for _ in substance_ids)
    return connection.execute(
        f"""
        SELECT id, key, label, molecular_formula
        FROM substances
        WHERE id IN ({placeholders})
        """,
        tuple(substance_ids),
    ).fetchall()


def _substance_from_row(row: sqlite3.Row) -> Substance:
    return Substance(
        id=cast(int, row["id"]),
        key=cast(str, row["key"]),
        label=cast(str, row["label"]),
        molecular_formula=cast(str | None, row["molecular_formula"]),
    )


def _source_stock_from_row(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> SourceStock:
    component_rows = connection.execute(
        """
        SELECT
            substances.id AS substance_id,
            substances.key AS substance_key,
            substances.label AS substance_label,
            substances.molecular_formula,
            source_stock_components.concentration_mol_l
        FROM source_stock_components
        JOIN substances ON substances.id = source_stock_components.substance_id
        WHERE source_stock_components.source_stock_id = ?
        ORDER BY source_stock_components.display_order, substances.id
        """,
        (row["id"],),
    ).fetchall()
    return SourceStock(
        id=cast(int, row["id"]),
        label=cast(str, row["label"]),
        solvent_substance_id=cast(int, row["solvent_substance_id"]),
        solvent_label=cast(str, row["solvent_label"]),
        components=tuple(
            SourceStockComponent(
                substance_id=cast(int, component["substance_id"]),
                substance_key=cast(str, component["substance_key"]),
                substance_label=cast(str, component["substance_label"]),
                molecular_formula=cast(str | None, component["molecular_formula"]),
                concentration_mol_l=cast(float, component["concentration_mol_l"]),
            )
            for component in component_rows
        ),
        component_layout_fingerprint=cast(str, row["component_layout_fingerprint"]),
        exact_composition_fingerprint=cast(str, row["exact_composition_fingerprint"]),
        relative_formulation_fingerprint=cast(
            str, row["relative_formulation_fingerprint"]
        ),
        batch_name=cast(str | None, row["batch_name"]),
        prepared_at=cast(str | None, row["prepared_at"]),
    )


def _source_configuration_entry_from_row(row: sqlite3.Row) -> SourceConfigurationEntry:
    return SourceConfigurationEntry(
        selector_port=cast(int, row["selector_port"]),
        source_config_key=cast(str, row["source_config_key"]),
        source_stock_id=cast(int | None, row["source_stock_id"]),
        enabled=bool(row["enabled"]),
        locked_fraction=cast(float | None, row["locked_fraction"]),
        control_substance_id=cast(int | None, row["control_substance_id"]),
    )


def _validate_source_configuration_entries(
    entries: Sequence[SourceConfigurationEntry],
) -> tuple[SourceConfigurationEntry, ...]:
    normalized: list[SourceConfigurationEntry] = []
    ports: set[int] = set()
    keys: set[str] = set()
    enabled_stock_ids: set[int] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, SourceConfigurationEntry):
            raise TypeError(f"entries[{index}] must be a SourceConfigurationEntry")

        require_integer_in_range(
            entry.selector_port,
            name=f"entries[{index}].selector_port",
            minimum=1,
            maximum=10,
        )
        selector_port = entry.selector_port
        if selector_port in ports:
            raise ValueError(f"Selector port {selector_port} occurs more than once")
        ports.add(selector_port)

        source_config_key = _require_text(
            entry.source_config_key, name=f"entries[{index}].source_config_key"
        )
        if source_config_key in keys:
            raise ValueError(
                f"Source configuration key {source_config_key!r} occurs more than once"
            )
        keys.add(source_config_key)

        source_stock_id = entry.source_stock_id
        if source_stock_id is not None:
            source_stock_id = _positive_integer(
                source_stock_id, name=f"entries[{index}].source_stock_id"
            )

        if not isinstance(entry.enabled, bool):
            raise TypeError(f"entries[{index}].enabled must be bool")
        is_water = source_config_key == WATER_SOURCE_CONFIG_KEY
        if entry.enabled and source_stock_id is None and not is_water:
            raise ValueError(
                f"Enabled source {source_config_key!r} requires a source stock"
            )
        if entry.enabled and source_stock_id in enabled_stock_ids:
            raise ValueError(
                f"Enabled source stock {source_stock_id} is assigned more than once"
            )
        if entry.enabled and source_stock_id is not None:
            enabled_stock_ids.add(source_stock_id)

        locked_fraction = entry.locked_fraction
        if locked_fraction is not None:
            if source_stock_id is None and not is_water:
                raise ValueError(
                    f"Locked fraction for {source_config_key!r} requires a source stock"
                )
            require_non_negative_finite_number(
                locked_fraction, name=f"entries[{index}].locked_fraction"
            )
            locked_fraction = float(locked_fraction)
            if locked_fraction > 1:
                raise ValueError(f"entries[{index}].locked_fraction must not exceed 1")

        control_substance_id = entry.control_substance_id
        if control_substance_id is not None:
            control_substance_id = _positive_integer(
                control_substance_id, name=f"entries[{index}].control_substance_id"
            )
            if source_stock_id is None:
                raise ValueError(
                    f"Control substance for {source_config_key!r} requires a stock"
                )

        normalized.append(
            SourceConfigurationEntry(
                selector_port=selector_port,
                source_config_key=source_config_key,
                source_stock_id=source_stock_id,
                enabled=entry.enabled,
                locked_fraction=locked_fraction,
                control_substance_id=control_substance_id,
            )
        )

    locked_sum = math.fsum(
        entry.locked_fraction
        for entry in normalized
        if entry.enabled and entry.locked_fraction is not None
    )
    if locked_sum > 1 + FRACTION_SUM_TOLERANCE:
        raise ValueError(
            f"Enabled locked fractions must not exceed 1; got {locked_sum:g}"
        )

    return tuple(sorted(normalized, key=lambda entry: entry.selector_port))


def _normalize_configuration_controls(
    connection: sqlite3.Connection, entries: Sequence[SourceConfigurationEntry]
) -> tuple[SourceConfigurationEntry, ...]:
    normalized: list[SourceConfigurationEntry] = []
    variable_control_ids: set[int] = set()

    for entry in entries:
        if entry.source_stock_id is None:
            normalized.append(
                SourceConfigurationEntry(
                    selector_port=entry.selector_port,
                    source_config_key=entry.source_config_key,
                    source_stock_id=None,
                    enabled=entry.enabled,
                    locked_fraction=entry.locked_fraction,
                    control_substance_id=None,
                )
            )
            continue

        rows = connection.execute(
            """
            SELECT substance_id
            FROM source_stock_components
            WHERE source_stock_id = ?
            ORDER BY display_order, substance_id
            """,
            (entry.source_stock_id,),
        ).fetchall()
        component_ids = tuple(cast(int, row["substance_id"]) for row in rows)
        if not component_ids:
            raise ValueError(
                f"Source stock {entry.source_stock_id} has no solute components"
            )

        control_substance_id = entry.control_substance_id
        if len(component_ids) == 1:
            control_substance_id = component_ids[0]
        elif control_substance_id is None and entry.enabled:
            raise ValueError(
                f"Source stock {entry.source_stock_id} has multiple components; "
                "select the component controlled by this port"
            )
        elif (
            control_substance_id is not None
            and control_substance_id not in component_ids
        ):
            raise ValueError(
                f"Substance {control_substance_id} is not a component of source "
                f"stock {entry.source_stock_id}"
            )

        if (
            entry.enabled
            and entry.locked_fraction is None
            and control_substance_id is not None
        ):
            if control_substance_id in variable_control_ids:
                raise ValueError(
                    "Enabled variable sources must use distinct controlled substances"
                )
            variable_control_ids.add(control_substance_id)

        normalized.append(
            SourceConfigurationEntry(
                selector_port=entry.selector_port,
                source_config_key=entry.source_config_key,
                source_stock_id=entry.source_stock_id,
                enabled=entry.enabled,
                locked_fraction=entry.locked_fraction,
                control_substance_id=control_substance_id,
            )
        )

    return tuple(sorted(normalized, key=lambda entry: entry.selector_port))


def _mixture_fractions(
    connection: sqlite3.Connection, mixture_id: int
) -> tuple[SourceFraction, ...]:
    rows = connection.execute(
        """
        SELECT source_stock_id, source_config_key, fraction
        FROM mixture_components
        WHERE mixture_id = ?
        ORDER BY selector_port, source_config_key
        """,
        (mixture_id,),
    ).fetchall()
    return tuple(
        SourceFraction(
            source_stock_id=cast(int | None, row["source_stock_id"]),
            source_config_key=cast(str | None, row["source_config_key"]),
            fraction=cast(float, row["fraction"]),
        )
        for row in rows
    )


def _normalize_source_stock_ids(source_stock_ids: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(
        _positive_integer(value, name="source_stock_id") for value in source_stock_ids
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError("source_stock_ids must not contain duplicates")

    return tuple(sorted(normalized))


def _source_setup_match_column(kind: SourceSetupMatchKind) -> str:
    columns: dict[SourceSetupMatchKind, str] = {
        "stock_records": "stock_fingerprint",
        "component_layout": "component_layout_fingerprint",
        "relative_formulation": "relative_formulation_fingerprint",
        "exact_composition": "exact_composition_fingerprint",
    }
    try:
        return columns[kind]
    except KeyError as error:
        raise ValueError(f"Unsupported source setup match kind: {kind!r}") from error


def _validate_concentration_constraints(
    constraints: Sequence[MixtureConcentrationConstraint],
) -> tuple[MixtureConcentrationConstraint, ...]:
    normalized: list[MixtureConcentrationConstraint] = []
    substance_ids: set[int] = set()
    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, MixtureConcentrationConstraint):
            raise TypeError(
                f"concentration_constraints[{index}] must be a "
                "MixtureConcentrationConstraint"
            )
        substance_id = _positive_integer(
            constraint.substance_id,
            name=f"concentration_constraints[{index}].substance_id",
        )
        if substance_id in substance_ids:
            raise ValueError(
                f"Substance {substance_id} has more than one concentration constraint"
            )
        substance_ids.add(substance_id)
        require_non_negative_finite_number(
            constraint.target_mol_l,
            name=f"concentration_constraints[{index}].target_mol_l",
        )
        require_non_negative_finite_number(
            constraint.tolerance_mol_l,
            name=f"concentration_constraints[{index}].tolerance_mol_l",
        )
        normalized.append(
            MixtureConcentrationConstraint(
                substance_id=substance_id,
                target_mol_l=float(constraint.target_mol_l),
                tolerance_mol_l=max(
                    float(constraint.tolerance_mol_l), CONCENTRATION_MATCH_EPSILON_MOL_L
                ),
            )
        )
    return tuple(normalized)


def _validate_components(
    components: Sequence[MixtureComponent],
) -> tuple[MixtureComponent, ...]:
    if not components:
        raise ValueError("components must not be empty")

    normalized: list[MixtureComponent] = []
    source_ids: set[int] = set()
    source_keys: set[str] = set()
    selector_ports: set[int] = set()

    for index, component in enumerate(components):
        if not isinstance(component, MixtureComponent):
            raise TypeError(f"components[{index}] must be a MixtureComponent")

        source_stock_id = component.source_stock_id
        if source_stock_id is not None:
            source_stock_id = _positive_integer(
                source_stock_id, name=f"components[{index}].source_stock_id"
            )
            if source_stock_id in source_ids:
                raise ValueError(
                    f"Source stock {source_stock_id} occurs more than once"
                )
            source_ids.add(source_stock_id)

        source_config_key = _optional_text(
            component.source_config_key, name=f"components[{index}].source_config_key"
        )
        if source_config_key is not None:
            if source_config_key in source_keys:
                raise ValueError(
                    f"Source configuration key {source_config_key!r} occurs more "
                    "than once"
                )
            source_keys.add(source_config_key)
        if source_stock_id is None and source_config_key != WATER_SOURCE_CONFIG_KEY:
            raise ValueError(
                "Only the configured water source may omit source_stock_id"
            )

        require_non_negative_finite_number(
            component.fraction, name=f"components[{index}].fraction"
        )
        fraction = float(component.fraction)
        if fraction > 1:
            raise ValueError(f"components[{index}].fraction must not exceed 1")

        selector_port = component.selector_port
        if selector_port is not None:
            require_integer_in_range(
                selector_port,
                name=f"components[{index}].selector_port",
                minimum=1,
                maximum=10,
            )
            if selector_port in selector_ports:
                raise ValueError(f"Selector port {selector_port} occurs more than once")
            selector_ports.add(selector_port)

        locked_fraction = component.locked_fraction
        if locked_fraction is not None:
            require_non_negative_finite_number(
                locked_fraction, name=f"components[{index}].locked_fraction"
            )
            locked_fraction = float(locked_fraction)
            if locked_fraction > 1:
                raise ValueError(
                    f"components[{index}].locked_fraction must not exceed 1"
                )
            if not math.isclose(
                fraction, locked_fraction, rel_tol=0.0, abs_tol=FRACTION_SUM_TOLERANCE
            ):
                raise ValueError(
                    f"components[{index}].fraction must equal its locked fraction"
                )

        normalized.append(
            MixtureComponent(
                source_stock_id=source_stock_id,
                fraction=fraction,
                selector_port=selector_port,
                source_config_key=source_config_key,
                locked_fraction=locked_fraction,
            )
        )

    fraction_sum = math.fsum(component.fraction for component in normalized)
    if not math.isclose(fraction_sum, 1.0, rel_tol=0.0, abs_tol=FRACTION_SUM_TOLERANCE):
        raise ValueError(f"Mixture fractions must sum to 1; got {fraction_sum:g}")

    return tuple(
        sorted(
            normalized,
            key=lambda component: (
                component.selector_port is None,
                component.selector_port or 0,
                component.source_config_key or "",
            ),
        )
    )


def _component_membership_error(
    *, setup_source_ids: set[int], component_source_ids: set[int]
) -> str:
    details: list[str] = []
    missing = setup_source_ids - component_source_ids
    unexpected = component_source_ids - setup_source_ids
    if missing:
        details.append("missing " + ", ".join(str(value) for value in sorted(missing)))
    if unexpected:
        details.append(
            "unexpected " + ", ".join(str(value) for value in sorted(unexpected))
        )
    return "Mixture components must match the source setup exactly: " + "; ".join(
        details
    )


def _validate_stock_component_inputs(
    components: Sequence[SourceStockComponentInput],
) -> tuple[SourceStockComponentInput, ...]:
    if not components:
        raise ValueError("A source stock requires at least one solute component")

    normalized: list[SourceStockComponentInput] = []
    substance_ids: set[int] = set()
    for index, component in enumerate(components):
        if not isinstance(component, SourceStockComponentInput):
            raise TypeError(f"components[{index}] must be a SourceStockComponentInput")
        substance_id = _positive_integer(
            component.substance_id, name=f"components[{index}].substance_id"
        )
        if substance_id in substance_ids:
            raise ValueError(f"Substance {substance_id} occurs more than once")
        substance_ids.add(substance_id)
        require_positive_finite_number(
            component.concentration_mol_l,
            name=f"components[{index}].concentration_mol_l",
        )
        normalized.append(
            SourceStockComponentInput(
                substance_id=substance_id,
                concentration_mol_l=float(component.concentration_mol_l),
            )
        )
    return tuple(normalized)


def _stock_fingerprints(
    *, solvent_substance_id: int, components: Sequence[SourceStockComponentInput]
) -> tuple[str, str, str]:
    ordered = sorted(components, key=lambda component: component.substance_id)
    component_layout_values = [f"solvent:{solvent_substance_id}"]
    component_layout_values.extend(
        f"component:{component.substance_id}" for component in ordered
    )
    exact_values = [f"solvent:{solvent_substance_id}"]
    exact_values.extend(
        f"{component.substance_id}:{component.concentration_mol_l:.15g}"
        for component in ordered
    )

    total = math.fsum(component.concentration_mol_l for component in ordered)
    relative_values = [f"solvent:{solvent_substance_id}"]
    relative_values.extend(
        f"{component.substance_id}:{component.concentration_mol_l / total:.15g}"
        for component in ordered
    )
    return (
        _fingerprint("stock-component-layout", component_layout_values),
        _fingerprint("stock-composition", exact_values),
        _fingerprint("relative-stock-formulation", relative_values),
    )


def _stock_label(
    *,
    components: Sequence[SourceStockComponentInput],
    substance_by_id: Mapping[int, sqlite3.Row],
) -> str:
    return " + ".join(
        f"{substance_by_id[component.substance_id]['label']} "
        f"{format_molar_concentration(component.concentration_mol_l)}"
        for component in components
    )


def format_molar_concentration(concentration_mol_l: float) -> str:
    require_non_negative_finite_number(concentration_mol_l, name="concentration_mol_l")
    value = float(concentration_mol_l)
    if value >= 1:
        return f"{value:.4g} M"
    if value >= 1e-3:
        return f"{value * 1e3:.4g} mM"
    return f"{value * 1e6:.4g} µM"


def _calculate_mixture_substance_concentrations(
    connection: sqlite3.Connection, components: Sequence[MixtureComponent]
) -> dict[int, float]:
    concentrations: dict[int, list[float]] = {}
    for component in components:
        if component.source_stock_id is None or component.fraction == 0:
            continue
        rows = connection.execute(
            """
            SELECT substance_id, concentration_mol_l
            FROM source_stock_components
            WHERE source_stock_id = ?
            """,
            (component.source_stock_id,),
        ).fetchall()
        if not rows:
            raise ValueError(
                f"Source stock {component.source_stock_id} has no components"
            )
        for row in rows:
            substance_id = cast(int, row["substance_id"])
            concentrations.setdefault(substance_id, []).append(
                component.fraction * cast(float, row["concentration_mol_l"])
            )
    return {
        substance_id: math.fsum(values)
        for substance_id, values in concentrations.items()
    }


def _fingerprint(namespace: str, values: Sequence[str]) -> str:
    payload = _canonical_json({"namespace": namespace, "values": sorted(values)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_json(value: Mapping[str, object] | None) -> str:
    return json.dumps(
        dict(value or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _last_inserted_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return an inserted row ID")
    return cursor.lastrowid


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _require_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _normalize_key(value: object, *, name: str) -> str:
    text = _require_text(value, name=name).lower().replace(" ", "_")
    if not all(character.isalnum() or character == "_" for character in text):
        raise ValueError(
            f"{name} may contain only letters, numbers, spaces, and underscores"
        )
    return text


def _optional_text(value: object | None, *, name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, name=name)


def _artifact_directory(value: object | None) -> str | None:
    text = _optional_text(value, name="artifact_directory")
    if text is None:
        return None
    path = Path(text)
    if path.is_absolute() or path.name != text or "\\" in text:
        raise ValueError("artifact_directory must be one relative directory name")
    return text


def _positive_integer(value: int, *, name: str) -> int:
    require_positive_integer(value, name=name)
    return value


def _mixture_status(value: object) -> MixtureStatus:
    if value not in MIXTURE_STATUSES:
        raise ValueError(f"Unsupported mixture status: {value!r}")
    return cast(MixtureStatus, value)


def _measurement_status(value: object) -> MeasurementStatus:
    if value not in MEASUREMENT_STATUSES:
        raise ValueError(f"Unsupported measurement status: {value!r}")
    return cast(MeasurementStatus, value)


def _measurement_origin(value: object) -> MeasurementOrigin:
    if value not in MEASUREMENT_ORIGINS:
        raise ValueError(f"Unsupported measurement origin: {value!r}")
    return cast(MeasurementOrigin, value)


def _measurement_type(value: object) -> MeasurementType:
    if value not in MEASUREMENT_TYPES:
        raise ValueError(f"Unsupported measurement type: {value!r}")
    return cast(MeasurementType, value)


def _workflow_phase(value: object) -> PersistedWorkflowPhase:
    phases = {
        "inactive",
        "ready",
        "prepared",
        "flow_completed",
        "sources_recovered",
        "sources_rinsed",
        "complete",
        "recovery_required",
    }
    if value not in phases:
        raise ValueError(f"Unsupported persisted workflow phase: {value!r}")
    return cast(PersistedWorkflowPhase, value)


def _physical_source_line_status(value: object) -> PhysicalSourceLineStatus:
    if value not in {"primed", "air_filled", "unknown"}:
        raise ValueError(f"Unsupported physical source line status: {value!r}")
    return cast(PhysicalSourceLineStatus, value)


def _prepared_volume_state(value: object) -> str | None:
    if value is not None and value not in {"prepared", "flow_completed", "unknown"}:
        raise ValueError(f"Unsupported prepared-mixture volume state: {value!r}")
    return cast(str | None, value)


def _optional_json_object(value: object, *, name: str) -> dict[str, object] | None:
    if value is None:
        return None
    parsed = json.loads(cast(str, value))
    if not isinstance(parsed, dict):
        raise TypeError(f"Persisted {name} must be a JSON object")
    return cast(dict[str, object], parsed)


def _json_object(value: object, *, name: str) -> dict[str, object]:
    parsed = _optional_json_object(value, name=name)
    if parsed is None:
        raise TypeError(f"Persisted {name} is required")
    return parsed


def _json_integer_tuple(value: object, *, name: str) -> tuple[int, ...]:
    parsed = json.loads(cast(str, value))
    if not isinstance(parsed, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in parsed
    ):
        raise ValueError(f"Persisted {name} must be a JSON integer list")
    return tuple(parsed)


_SCHEMA = """
BEGIN;

CREATE TABLE substances (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    molecular_formula TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

INSERT INTO substances (
    key, label, molecular_formula, metadata_json, created_at
)
VALUES (
    'water', 'Water', 'H2O', '{}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

CREATE TABLE source_stocks (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    solvent_substance_id INTEGER NOT NULL REFERENCES substances(id),
    component_layout_fingerprint TEXT NOT NULL,
    exact_composition_fingerprint TEXT NOT NULL,
    relative_formulation_fingerprint TEXT NOT NULL,
    batch_name TEXT,
    prepared_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_source_stocks_component_layout
    ON source_stocks (component_layout_fingerprint);

CREATE INDEX idx_source_stocks_exact_composition
    ON source_stocks (exact_composition_fingerprint);

CREATE INDEX idx_source_stocks_relative_formulation
    ON source_stocks (relative_formulation_fingerprint);

CREATE TABLE source_stock_components (
    source_stock_id INTEGER NOT NULL REFERENCES source_stocks(id) ON DELETE CASCADE,
    substance_id INTEGER NOT NULL REFERENCES substances(id),
    concentration_mol_l REAL NOT NULL CHECK (concentration_mol_l > 0),
    display_order INTEGER NOT NULL CHECK (display_order >= 0),
    PRIMARY KEY (source_stock_id, substance_id),
    UNIQUE (source_stock_id, display_order)
);

CREATE TABLE current_source_configuration (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_at TEXT NOT NULL
);

INSERT INTO current_source_configuration (singleton_id, revision, updated_at)
VALUES (1, 0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE current_source_assignments (
    selector_port INTEGER PRIMARY KEY CHECK (selector_port BETWEEN 1 AND 10),
    source_config_key TEXT NOT NULL UNIQUE,
    source_stock_id INTEGER REFERENCES source_stocks(id),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    locked_fraction REAL CHECK (
        locked_fraction IS NULL
        OR (locked_fraction >= 0 AND locked_fraction <= 1)
    ),
    control_substance_id INTEGER REFERENCES substances(id),
    updated_at TEXT NOT NULL,
    CHECK (
        enabled = 0
        OR source_stock_id IS NOT NULL
        OR source_config_key = 'water'
    ),
    CHECK (
        locked_fraction IS NULL
        OR source_stock_id IS NOT NULL
        OR source_config_key = 'water'
    ),
    CHECK (control_substance_id IS NULL OR source_stock_id IS NOT NULL)
);

CREATE TABLE source_setups (
    id INTEGER PRIMARY KEY,
    name TEXT,
    stock_fingerprint TEXT NOT NULL UNIQUE,
    component_layout_fingerprint TEXT NOT NULL,
    exact_composition_fingerprint TEXT NOT NULL,
    relative_formulation_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_source_setups_component_layout
    ON source_setups (component_layout_fingerprint);

CREATE INDEX idx_source_setups_exact_composition
    ON source_setups (exact_composition_fingerprint);

CREATE INDEX idx_source_setups_relative_formulation
    ON source_setups (relative_formulation_fingerprint);

CREATE TABLE source_setup_members (
    source_setup_id INTEGER NOT NULL REFERENCES source_setups(id) ON DELETE CASCADE,
    source_stock_id INTEGER NOT NULL REFERENCES source_stocks(id),
    PRIMARY KEY (source_setup_id, source_stock_id)
);

CREATE TABLE mixtures (
    id INTEGER PRIMARY KEY,
    source_setup_id INTEGER NOT NULL REFERENCES source_setups(id),
    source_configuration_revision INTEGER NOT NULL CHECK (
        source_configuration_revision >= 0
    ),
    status TEXT NOT NULL CHECK (
        status IN ('planned', 'running', 'completed', 'failed', 'aborted')
    ),
    total_volume_ml REAL NOT NULL CHECK (total_volume_ml > 0),
    context_json TEXT NOT NULL DEFAULT '{}',
    protocol_snapshot_json TEXT NOT NULL,
    artifact_directory TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT
);

CREATE INDEX idx_mixtures_setup_status
    ON mixtures (source_setup_id, status);

CREATE UNIQUE INDEX idx_mixtures_artifact_directory
    ON mixtures (artifact_directory)
    WHERE artifact_directory IS NOT NULL;

CREATE TABLE mixture_components (
    id INTEGER PRIMARY KEY,
    mixture_id INTEGER NOT NULL REFERENCES mixtures(id) ON DELETE CASCADE,
    source_stock_id INTEGER REFERENCES source_stocks(id),
    fraction REAL NOT NULL CHECK (fraction >= 0 AND fraction <= 1),
    commanded_volume_ml REAL NOT NULL CHECK (commanded_volume_ml >= 0),
    selector_port INTEGER CHECK (selector_port BETWEEN 1 AND 10),
    source_config_key TEXT,
    locked_fraction REAL CHECK (
        locked_fraction IS NULL
        OR (locked_fraction >= 0 AND locked_fraction <= 1)
    ),
    UNIQUE (mixture_id, selector_port),
    UNIQUE (mixture_id, source_config_key),
    CHECK (source_stock_id IS NOT NULL OR source_config_key = 'water')
);

CREATE TABLE mixture_substance_concentrations (
    mixture_id INTEGER NOT NULL REFERENCES mixtures(id) ON DELETE CASCADE,
    substance_id INTEGER NOT NULL REFERENCES substances(id),
    concentration_mol_l REAL NOT NULL CHECK (concentration_mol_l >= 0),
    PRIMARY KEY (mixture_id, substance_id)
);

CREATE TABLE measurements (
    id INTEGER PRIMARY KEY,
    mixture_id INTEGER REFERENCES mixtures(id),
    origin TEXT NOT NULL CHECK (
        origin IN ('manual', 'experiment', 'optimization', 'imported', 'test')
    ),
    measurement_type TEXT NOT NULL CHECK (
        measurement_type IN (
            'chronoamperometry',
            'cyclic_voltammetry',
            'linear_sweep_voltammetry'
        )
    ),
    status TEXT NOT NULL CHECK (
        status IN ('planned', 'running', 'completed', 'failed', 'aborted')
    ),
    measurement_name TEXT,
    settings_json TEXT NOT NULL,
    settings_fingerprint TEXT NOT NULL,
    protocol_key TEXT,
    flow_rpm INTEGER CHECK (flow_rpm > 0),
    flow_rate_ml_min REAL CHECK (flow_rate_ml_min > 0),
    flow_volume_ml REAL CHECK (flow_volume_ml >= 0),
    flow_started_at TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    run_sequence_number INTEGER CHECK (run_sequence_number > 0),
    session_file_path TEXT UNIQUE,
    point_count INTEGER CHECK (point_count >= 0),
    elapsed_s REAL CHECK (elapsed_s >= 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT,
    CHECK (
        origin != 'experiment'
        OR (mixture_id IS NOT NULL AND run_sequence_number IS NOT NULL)
    )
);

CREATE INDEX idx_measurements_mixture_status_type
    ON measurements (mixture_id, status, measurement_type);

CREATE INDEX idx_measurements_settings
    ON measurements (settings_fingerprint, protocol_key);

CREATE UNIQUE INDEX idx_measurements_run_sequence
    ON measurements (mixture_id, run_sequence_number)
    WHERE mixture_id IS NOT NULL AND run_sequence_number IS NOT NULL;

CREATE TABLE measurement_metrics (
    id INTEGER PRIMARY KEY,
    measurement_id INTEGER NOT NULL REFERENCES measurements(id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    standard_error REAL CHECK (standard_error >= 0),
    analysis_method TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    analysis_settings_json TEXT NOT NULL DEFAULT '{}',
    analysis_fingerprint TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE (
        measurement_id,
        metric_name,
        analysis_method,
        analysis_version,
        analysis_fingerprint
    )
);

CREATE INDEX idx_measurement_metrics_lookup
    ON measurement_metrics (metric_name, analysis_method, analysis_version);

CREATE TABLE measurement_metric_components (
    id INTEGER PRIMARY KEY,
    measurement_metric_id INTEGER NOT NULL
        REFERENCES measurement_metrics(id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    component_kind TEXT NOT NULL,
    value REAL NOT NULL,
    UNIQUE (measurement_metric_id, component_kind, sequence_number)
);

CREATE INDEX idx_measurement_metric_components_metric
    ON measurement_metric_components (measurement_metric_id);

CREATE TABLE experiment_workspace_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    workflow_phase TEXT NOT NULL CHECK (
        workflow_phase IN (
            'inactive', 'ready', 'prepared', 'flow_completed',
            'sources_recovered', 'sources_rinsed', 'complete', 'recovery_required'
        )
    ),
    operation_in_progress INTEGER NOT NULL CHECK (operation_in_progress IN (0, 1)),
    active_plan_json TEXT,
    mixture_id INTEGER REFERENCES mixtures(id),
    prepared_volume_state TEXT,
    maintenance_source_ports_json TEXT NOT NULL DEFAULT '[]',
    last_operation TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO experiment_workspace_state (
    singleton_id, workflow_phase, operation_in_progress,
    maintenance_source_ports_json, updated_at
)
VALUES (1, 'inactive', 0, '[]', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE source_line_physical_states (
    selector_port INTEGER PRIMARY KEY CHECK (selector_port BETWEEN 1 AND 10),
    source_config_key TEXT NOT NULL UNIQUE,
    source_stock_id INTEGER REFERENCES source_stocks(id),
    status TEXT NOT NULL CHECK (status IN ('primed', 'air_filled', 'unknown')),
    updated_at TEXT NOT NULL,
    reason TEXT NOT NULL
);

PRAGMA user_version = 1;
COMMIT;
"""

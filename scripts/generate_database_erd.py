"""Generate documentation for the experiment database schema.

Run from the project root:

    python scripts/generate_database_erd.py
    python scripts/generate_database_erd.py --check
    python scripts/generate_database_erd.py --stdout
    python scripts/generate_database_erd.py --help
    python scripts/generate_database_erd.py --database data/autonomous_flow.sqlite3
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from storage.experiment_repository import SCHEMA_VERSION, ExperimentRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "docs" / "architecture" / "database_erd.md"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    declared_type: str
    not_null: bool
    primary_key_position: int


@dataclass(frozen=True, slots=True)
class ForeignKey:
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    nullable: bool
    on_delete: str


@dataclass(frozen=True, slots=True)
class UniqueConstraint:
    table: str
    columns: tuple[str, ...]


def main() -> int:
    args = _parse_args()

    with _database_path(args.database) as database_path:
        document = _build_document(database_path)

    if args.stdout:
        print(document, end="")
        return 0

    output_path = args.output.resolve()

    if args.check:
        existing = (
            output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        )
        if existing == document:
            print(f"Database ERD is current: {output_path}")
            return 0

        print(
            "Database ERD is missing or stale. Run "
            "'python scripts/generate_database_erd.py' to update it.",
            file=sys.stderr,
        )
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    print(f"Updated database ERD: {output_path}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a Mermaid ERD by introspecting the actual SQLite schema. "
            "With no --database argument, a temporary database is created from "
            "the current ExperimentRepository schema."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Introspect an existing SQLite database instead of the current schema.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Markdown destination (default: {DEFAULT_OUTPUT_PATH}).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--stdout",
        action="store_true",
        help="Print the generated Markdown without writing a file.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero if the output file does not match the current schema.",
    )
    return parser.parse_args()


@contextmanager
def _database_path(requested_path: Path | None) -> Generator[Path, None, None]:
    if requested_path is not None:
        database_path = requested_path.resolve()
        if not database_path.is_file():
            raise FileNotFoundError(f"SQLite database does not exist: {database_path}")
        yield database_path
        return

    with tempfile.TemporaryDirectory(prefix="autonomous-flow-erd-") as directory:
        database_path = Path(directory) / "schema.sqlite3"
        ExperimentRepository(database_path)
        yield database_path


def _build_document(database_path: Path) -> str:
    with _read_only_connection(database_path) as connection:
        tables = _read_tables(connection)
        columns_by_table = {table: _read_columns(connection, table) for table in tables}
        foreign_keys = tuple(
            foreign_key
            for table in tables
            for foreign_key in _read_foreign_keys(
                connection, table, columns_by_table[table]
            )
        )
        unique_constraints = tuple(
            constraint
            for table in tables
            for constraint in _read_unique_constraints(connection, table)
        )
        database_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

    lines = [
        "# Experiment database ERD",
        "",
        (
            "> Generated from the live SQLite schema by "
            "`python scripts/generate_database_erd.py`. Do not edit manually."
        ),
        "",
        f"Schema version: `{database_version}` (code supports `{SCHEMA_VERSION}`).",
        "",
        "```mermaid",
        "erDiagram",
    ]

    foreign_key_columns = {
        (foreign_key.child_table, foreign_key.child_column)
        for foreign_key in foreign_keys
    }
    unique_columns = {
        (constraint.table, constraint.columns[0])
        for constraint in unique_constraints
        if len(constraint.columns) == 1
    }

    for table in tables:
        lines.append(f"    {table} {{")
        for column in columns_by_table[table]:
            lines.append(
                _mermaid_column_line(
                    table=table,
                    column=column,
                    foreign_key_columns=foreign_key_columns,
                    unique_columns=unique_columns,
                )
            )
        lines.append("    }")

    for foreign_key in foreign_keys:
        parent_cardinality = "o|" if foreign_key.nullable else "||"
        delete_suffix = (
            ""
            if foreign_key.on_delete in ("", "NO ACTION")
            else f"; ON DELETE {foreign_key.on_delete}"
        )
        relationship_label = (
            f"{foreign_key.child_column} → {foreign_key.parent_column}{delete_suffix}"
        )
        lines.append(
            f"    {foreign_key.parent_table} {parent_cardinality}--o{{ "
            f'{foreign_key.child_table} : "{relationship_label}"'
        )

    lines.extend(
        [
            "```",
            "",
            "## Interpretation notes",
            "",
            (
                "- PalmSens session files remain the source of raw curve data; the "
                "database stores their paths, experimental context, and derived "
                "metrics."
            ),
            (
                "- `current_source_configuration` is a singleton revision counter. "
                "`current_source_assignments` holds the editable port assignments."
            ),
            (
                "- Source-setup identity is based on enabled stock identities, not "
                "port order. A mixture records the exact stock fractions and ports "
                "used."
            ),
            (
                "- A mixture may have multiple measurements, and a measurement may "
                "have multiple versioned analysis metrics."
            ),
        ]
    )

    composite_unique_constraints = tuple(
        constraint for constraint in unique_constraints if len(constraint.columns) > 1
    )
    if composite_unique_constraints:
        lines.extend(["", "## Composite uniqueness constraints", ""])
        lines.extend(
            f"- `{constraint.table}({', '.join(constraint.columns)})`"
            for constraint in composite_unique_constraints
        )

    lines.append("")
    return "\n".join(lines)


@contextmanager
def _read_only_connection(
    database_path: Path,
) -> Generator[sqlite3.Connection, None, None]:
    uri = f"{database_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        yield connection
    finally:
        connection.close()


def _read_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_schema
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return tuple(str(row["name"]) for row in rows)


def _read_columns(connection: sqlite3.Connection, table: str) -> tuple[Column, ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    return tuple(
        Column(
            name=str(row["name"]),
            declared_type=str(row["type"] or "ANY"),
            not_null=bool(row["notnull"]) or int(row["pk"]) > 0,
            primary_key_position=int(row["pk"]),
        )
        for row in rows
    )


def _read_foreign_keys(
    connection: sqlite3.Connection, table: str, columns: tuple[Column, ...]
) -> tuple[ForeignKey, ...]:
    nullable_by_column = {column.name: not column.not_null for column in columns}
    rows = connection.execute(
        f"PRAGMA foreign_key_list({_quote_identifier(table)})"
    ).fetchall()
    return tuple(
        ForeignKey(
            child_table=table,
            child_column=str(row["from"]),
            parent_table=str(row["table"]),
            parent_column=str(row["to"]),
            nullable=nullable_by_column[str(row["from"])],
            on_delete=str(row["on_delete"]),
        )
        for row in rows
    )


def _read_unique_constraints(
    connection: sqlite3.Connection, table: str
) -> tuple[UniqueConstraint, ...]:
    constraints: list[UniqueConstraint] = []
    rows = connection.execute(
        f"PRAGMA index_list({_quote_identifier(table)})"
    ).fetchall()

    for row in rows:
        if not bool(row["unique"]) or str(row["origin"]) == "pk":
            continue

        index_name = str(row["name"])
        index_rows = connection.execute(
            f"PRAGMA index_info({_quote_identifier(index_name)})"
        ).fetchall()
        columns = tuple(str(index_row["name"]) for index_row in index_rows)
        if columns:
            constraints.append(UniqueConstraint(table=table, columns=columns))

    return tuple(sorted(constraints, key=lambda item: item.columns))


def _mermaid_column_line(
    *,
    table: str,
    column: Column,
    foreign_key_columns: set[tuple[str, str]],
    unique_columns: set[tuple[str, str]],
) -> str:
    keys: list[str] = []
    if column.primary_key_position:
        keys.append("PK")
    if (table, column.name) in foreign_key_columns:
        keys.append("FK")
    if (table, column.name) in unique_columns:
        keys.append("UK")

    key_text = "" if not keys else " " + ", ".join(keys)
    nullability = "NOT NULL" if column.not_null else "NULL"
    declared_type = "_".join(column.declared_type.upper().split())
    return f'        {declared_type} {column.name}{key_text} "{nullability}"'


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


if __name__ == "__main__":
    raise SystemExit(main())

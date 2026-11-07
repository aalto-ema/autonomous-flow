# Experiment database ERD

> Generated from the live SQLite schema by `python scripts/generate_database_erd.py`. Do not edit manually.

Schema version: `1` (code supports `1`).

```mermaid
erDiagram
    current_source_assignments {
        INTEGER selector_port PK "NOT NULL"
        TEXT source_config_key UK "NOT NULL"
        INTEGER source_stock_id FK "NULL"
        INTEGER enabled "NOT NULL"
        REAL locked_fraction "NULL"
        INTEGER control_substance_id FK "NULL"
        TEXT updated_at "NOT NULL"
    }
    current_source_configuration {
        INTEGER singleton_id PK "NOT NULL"
        INTEGER revision "NOT NULL"
        TEXT updated_at "NOT NULL"
    }
    experiment_workspace_state {
        INTEGER singleton_id PK "NOT NULL"
        TEXT workflow_phase "NOT NULL"
        INTEGER operation_in_progress "NOT NULL"
        TEXT active_plan_json "NULL"
        INTEGER mixture_id FK "NULL"
        TEXT prepared_volume_state "NULL"
        TEXT maintenance_source_ports_json "NOT NULL"
        TEXT last_operation "NULL"
        TEXT updated_at "NOT NULL"
    }
    measurement_metric_components {
        INTEGER id PK "NOT NULL"
        INTEGER measurement_metric_id FK "NOT NULL"
        INTEGER sequence_number "NOT NULL"
        TEXT component_kind "NOT NULL"
        REAL value "NOT NULL"
    }
    measurement_metrics {
        INTEGER id PK "NOT NULL"
        INTEGER measurement_id FK "NOT NULL"
        TEXT metric_name "NOT NULL"
        REAL value "NOT NULL"
        TEXT unit "NOT NULL"
        REAL standard_error "NULL"
        TEXT analysis_method "NOT NULL"
        TEXT analysis_version "NOT NULL"
        TEXT analysis_settings_json "NOT NULL"
        TEXT analysis_fingerprint "NOT NULL"
        TEXT computed_at "NOT NULL"
    }
    measurements {
        INTEGER id PK "NOT NULL"
        INTEGER mixture_id FK "NULL"
        TEXT origin "NOT NULL"
        TEXT measurement_type "NOT NULL"
        TEXT status "NOT NULL"
        TEXT measurement_name "NULL"
        TEXT settings_json "NOT NULL"
        TEXT settings_fingerprint "NOT NULL"
        TEXT protocol_key "NULL"
        INTEGER flow_rpm "NULL"
        REAL flow_rate_ml_min "NULL"
        REAL flow_volume_ml "NULL"
        TEXT flow_started_at "NULL"
        TEXT context_json "NOT NULL"
        INTEGER run_sequence_number "NULL"
        TEXT session_file_path UK "NULL"
        INTEGER point_count "NULL"
        REAL elapsed_s "NULL"
        TEXT created_at "NOT NULL"
        TEXT started_at "NULL"
        TEXT finished_at "NULL"
        TEXT error_message "NULL"
    }
    mixture_components {
        INTEGER id PK "NOT NULL"
        INTEGER mixture_id FK "NOT NULL"
        INTEGER source_stock_id FK "NULL"
        REAL fraction "NOT NULL"
        REAL commanded_volume_ml "NOT NULL"
        INTEGER selector_port "NULL"
        TEXT source_config_key "NULL"
        REAL locked_fraction "NULL"
    }
    mixture_substance_concentrations {
        INTEGER mixture_id PK, FK "NOT NULL"
        INTEGER substance_id PK, FK "NOT NULL"
        REAL concentration_mol_l "NOT NULL"
    }
    mixtures {
        INTEGER id PK "NOT NULL"
        INTEGER source_setup_id FK "NOT NULL"
        INTEGER source_configuration_revision "NOT NULL"
        TEXT status "NOT NULL"
        REAL total_volume_ml "NOT NULL"
        TEXT context_json "NOT NULL"
        TEXT protocol_snapshot_json "NOT NULL"
        TEXT artifact_directory UK "NULL"
        TEXT created_at "NOT NULL"
        TEXT started_at "NULL"
        TEXT finished_at "NULL"
        TEXT error_message "NULL"
    }
    source_line_physical_states {
        INTEGER selector_port PK "NOT NULL"
        TEXT source_config_key UK "NOT NULL"
        INTEGER source_stock_id FK "NULL"
        TEXT status "NOT NULL"
        TEXT updated_at "NOT NULL"
        TEXT reason "NOT NULL"
    }
    source_setup_members {
        INTEGER source_setup_id PK, FK "NOT NULL"
        INTEGER source_stock_id PK, FK "NOT NULL"
    }
    source_setups {
        INTEGER id PK "NOT NULL"
        TEXT name "NULL"
        TEXT stock_fingerprint UK "NOT NULL"
        TEXT component_layout_fingerprint "NOT NULL"
        TEXT exact_composition_fingerprint "NOT NULL"
        TEXT relative_formulation_fingerprint "NOT NULL"
        TEXT created_at "NOT NULL"
    }
    source_stock_components {
        INTEGER source_stock_id PK, FK "NOT NULL"
        INTEGER substance_id PK, FK "NOT NULL"
        REAL concentration_mol_l "NOT NULL"
        INTEGER display_order "NOT NULL"
    }
    source_stocks {
        INTEGER id PK "NOT NULL"
        TEXT label "NOT NULL"
        INTEGER solvent_substance_id FK "NOT NULL"
        TEXT component_layout_fingerprint "NOT NULL"
        TEXT exact_composition_fingerprint "NOT NULL"
        TEXT relative_formulation_fingerprint "NOT NULL"
        TEXT batch_name "NULL"
        TEXT prepared_at "NULL"
        TEXT metadata_json "NOT NULL"
        TEXT created_at "NOT NULL"
    }
    substances {
        INTEGER id PK "NOT NULL"
        TEXT key UK "NOT NULL"
        TEXT label "NOT NULL"
        TEXT molecular_formula "NULL"
        TEXT metadata_json "NOT NULL"
        TEXT created_at "NOT NULL"
    }
    substances o|--o{ current_source_assignments : "control_substance_id → id"
    source_stocks o|--o{ current_source_assignments : "source_stock_id → id"
    mixtures o|--o{ experiment_workspace_state : "mixture_id → id"
    measurement_metrics ||--o{ measurement_metric_components : "measurement_metric_id → id; ON DELETE CASCADE"
    measurements ||--o{ measurement_metrics : "measurement_id → id; ON DELETE CASCADE"
    mixtures o|--o{ measurements : "mixture_id → id"
    source_stocks o|--o{ mixture_components : "source_stock_id → id"
    mixtures ||--o{ mixture_components : "mixture_id → id; ON DELETE CASCADE"
    substances ||--o{ mixture_substance_concentrations : "substance_id → id"
    mixtures ||--o{ mixture_substance_concentrations : "mixture_id → id; ON DELETE CASCADE"
    source_setups ||--o{ mixtures : "source_setup_id → id"
    source_stocks o|--o{ source_line_physical_states : "source_stock_id → id"
    source_stocks ||--o{ source_setup_members : "source_stock_id → id"
    source_setups ||--o{ source_setup_members : "source_setup_id → id; ON DELETE CASCADE"
    substances ||--o{ source_stock_components : "substance_id → id"
    source_stocks ||--o{ source_stock_components : "source_stock_id → id; ON DELETE CASCADE"
    substances ||--o{ source_stocks : "solvent_substance_id → id"
```

## Interpretation notes

- PalmSens session files remain the source of raw curve data; the database stores their paths, experimental context, and derived metrics.
- `current_source_configuration` is a singleton revision counter. `current_source_assignments` holds the editable port assignments.
- Source-setup identity is based on enabled stock identities, not port order. A mixture records the exact stock fractions and ports used.
- A mixture may have multiple measurements, and a measurement may have multiple versioned analysis metrics.

## Composite uniqueness constraints

- `measurement_metric_components(measurement_metric_id, component_kind, sequence_number)`
- `measurement_metrics(measurement_id, metric_name, analysis_method, analysis_version, analysis_fingerprint)`
- `measurements(mixture_id, run_sequence_number)`
- `mixture_components(mixture_id, selector_port)`
- `mixture_components(mixture_id, source_config_key)`
- `source_stock_components(source_stock_id, display_order)`

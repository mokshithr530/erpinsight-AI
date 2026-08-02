import time

import pytest

from schema_profiler import DatabaseProfile, SchemaProfiler
from schema_profile_history import SchemaProfileHistory
from schema_profile_fixtures import (
    construction_schema,
    finance_schema,
    hr_schema,
    large_schema,
    legacy_schema,
    manufacturing_schema,
    minimal_fk_schema,
    sales_schema,
    table,
    column,
)


FIXTURES = [
    ("construction", construction_schema),
    ("hr", hr_schema),
    ("finance", finance_schema),
    ("manufacturing", manufacturing_schema),
    ("sales", sales_schema),
    ("legacy", legacy_schema),
    ("minimal_fk", minimal_fk_schema),
]


def build(profiler, name, schema, **kwargs):
    return profiler.build(
        database_id=name,
        dialect="synthetic",
        schema=schema,
        schema_fingerprint=f"fingerprint-{name}",
        row_counts={table_name: info.get("row_count") for table_name, info in schema.items()},
        **kwargs,
    )


def table_by_name(profile, name):
    return next(item for item in profile.tables if item.table_name == name)


def column_by_name(profile, table_name, column_name):
    table_profile = table_by_name(profile, table_name)
    return next(item for item in table_profile.columns if item.column_name == column_name)


@pytest.mark.parametrize(("name", "fixture"), FIXTURES)
def test_every_table_gets_an_explainable_profile(tmp_path, name, fixture):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    schema = fixture()

    profile = build(profiler, name, schema)

    assert isinstance(profile, DatabaseProfile)
    assert profile.table_count == len(schema)
    assert {item.table_name for item in profile.tables} == set(schema)
    for table_profile in profile.tables:
        assert table_profile.normalized_tokens
        assert table_profile.likely_role
        assert table_profile.likely_module
        assert table_profile.explanation
        assert table_profile.profile_confidence >= 0.6
        assert table_profile.columns


def test_identifier_tokenization_supports_common_and_legacy_styles(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)

    assert profiler.tokenize_identifier("client_bill_raising") == ["client", "bill", "raising"]
    assert profiler.tokenize_identifier("ProjectMaster") == ["project", "master"]
    assert profiler.tokenize_identifier("employeeID") == ["employee", "id"]
    assert profiler.tokenize_identifier("T_PRJ_HDR") == ["prj", "hdr"]
    assert profiler.tokenize_identifier("tbl_store_issue_dtl") == ["store", "issue", "dtl"]


def test_column_semantic_roles_are_detected_from_names_types_and_samples(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    schema = construction_schema()
    samples = {
        "ProjectMaster": {
            "ProjectID": [1, 2, 3],
            "ProjectName": ["North Link", "Metro East"],
            "ProjectStatus": ["Active", "Delayed"],
            "ApprovedValue": ["100000.00", "250000.00"],
            "StartDate": ["2026-01-01", "2026-02-10"],
        }
    }

    profile = build(profiler, "roles", schema, safe_samples=samples)

    assert column_by_name(profile, "ProjectMaster", "ProjectID").is_identifier_like
    assert column_by_name(profile, "ProjectMaster", "ProjectName").is_name_like
    assert column_by_name(profile, "ProjectMaster", "ProjectStatus").is_status_like
    assert column_by_name(profile, "ProjectMaster", "ApprovedValue").is_amount_like
    start_date = column_by_name(profile, "ProjectMaster", "StartDate")
    assert start_date.is_date_like
    assert start_date.detected_date_format == "YYYY-MM-DD"


def test_quantity_percentage_boolean_code_dimension_and_metric_flags(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    schema = {
        "Measures": table(
            column("record_id", "int", primary_key=True),
            column("item_code"),
            column("issued_qty", "decimal"),
            column("completion_pct", "decimal"),
            column("is_active", "boolean"),
            column("category_name"),
        )
    }
    samples = {"Measures": {"completion_pct": ["10%", "95%"], "is_active": [0, 1]}}

    profile = build(profiler, "flags", schema, safe_samples=samples)

    assert column_by_name(profile, "Measures", "item_code").is_code_like
    assert column_by_name(profile, "Measures", "issued_qty").is_quantity_like
    assert column_by_name(profile, "Measures", "completion_pct").is_percentage_like
    assert column_by_name(profile, "Measures", "is_active").is_boolean_like
    assert column_by_name(profile, "Measures", "category_name").is_dimension
    assert column_by_name(profile, "Measures", "issued_qty").is_metric


def test_indexes_constraints_comments_and_row_counts_are_preserved(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    profile = build(profiler, "metadata", construction_schema())
    projects = table_by_name(profile, "ProjectMaster")

    assert projects.description == "Construction projects"
    assert projects.row_count == 25
    assert projects.is_empty is False
    assert ["ProjectName"] in projects.unique_keys
    assert column_by_name(profile, "ProjectMaster", "ProjectName").is_unique
    assert column_by_name(profile, "ProjectMaster", "ApprovedValue").comment == "Approved contract value"


def test_declared_relationships_are_profiled_with_high_confidence(tmp_path):
    profile = build(SchemaProfiler(profile_dir=tmp_path), "fk", finance_schema())
    relationship = next(
        item for item in profile.relationships
        if item.source_table == "sales_invoices" and item.target_table == "customer_accounts"
    )

    assert relationship.declared is True
    assert relationship.relationship_type == "foreign_key"
    assert relationship.confidence_score >= 0.9
    assert "declared_foreign_key" in relationship.evidence


def test_minimal_fk_schema_gets_bounded_inferred_relationships(tmp_path):
    profile = build(SchemaProfiler(profile_dir=tmp_path), "minimal", minimal_fk_schema())
    inferred = [item for item in profile.relationships if not item.declared]

    assert any({item.source_table, item.target_table} == {"work_log", "people"} for item in inferred)
    assert any({item.source_table, item.target_table} == {"work_log", "teams"} for item in inferred)
    assert all(item.confidence_score < 0.9 for item in inferred)


def test_legacy_abbreviations_use_comments_types_and_relationships(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    samples = {
        "t_prj_hdr": {"st_cd": ["ACT", "HLD"], "appr_amt": [100, 200]},
        "trn_iss": {"iss_dt": ["01-06-2026", "02-06-2026"]},
    }
    profile = build(profiler, "legacy-evidence", legacy_schema(), safe_samples=samples)

    assert column_by_name(profile, "t_prj_hdr", "prj_nm").is_name_like
    assert column_by_name(profile, "t_prj_hdr", "appr_amt").is_amount_like
    assert column_by_name(profile, "t_prj_hdr", "st_cd").is_status_like
    issue_date = column_by_name(profile, "trn_iss", "iss_dt")
    assert issue_date.is_date_like
    assert issue_date.detected_date_format == "DD-MM-YYYY"
    assert table_by_name(profile, "trn_iss").relationship_hints


def test_safe_samples_are_bounded_and_sensitive_columns_are_excluded(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path, sample_limit=3)
    schema = {
        "users": table(
            column("user_id", "int", primary_key=True),
            column("display_name"),
            column("password_hash"),
            column("api_token"),
        )
    }
    samples = {
        "users": {
            "display_name": ["A", "B", "C", "D"],
            "password_hash": ["secret-hash"],
            "api_token": ["secret-token"],
        }
    }

    profile = build(profiler, "safe", schema, safe_samples=samples)

    assert column_by_name(profile, "users", "display_name").sample_values == ["A", "B", "C"]
    assert column_by_name(profile, "users", "password_hash").sample_values == []
    assert column_by_name(profile, "users", "api_token").sample_values == []


def test_business_catalog_is_evidence_not_a_forced_mapping(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    schema = finance_schema()
    catalog = {
        "entities": {
            "receivables": {"tables": [{"table": "sales_invoices"}]},
            "wrong_mapping": {"tables": [{"table": "does_not_exist"}]},
        }
    }

    profile = build(profiler, "catalog", schema, catalog=catalog)

    invoices = table_by_name(profile, "sales_invoices")
    assert "receivables" in invoices.business_concepts
    assert all("wrong_mapping" not in table.business_concepts for table in profile.tables)


def test_only_verified_successful_query_history_is_used(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    history = [
        {"selected_tables": ["sales_invoices"], "validated": True, "execution_success": True, "confidence": 90},
        {"selected_tables": ["sales_invoices"], "validated": False, "execution_success": True, "confidence": 90},
        {"selected_tables": ["cash_receipts"], "validated": True, "execution_success": False, "confidence": 90},
        {"sql": "SELECT * FROM customer_accounts", "validated": True, "execution_success": True, "confidence": 40},
    ]

    profile = build(profiler, "history", finance_schema(), historical_queries=history)

    assert table_by_name(profile, "sales_invoices").historical_success_count == 1
    assert table_by_name(profile, "cash_receipts").historical_success_count == 0
    assert "verified_query_history" in profile.generated_from


def test_cached_profile_refreshes_when_verified_history_changes(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    schema = finance_schema()
    first = profiler.get_or_build(
        database_id="finance",
        dialect="mysql",
        schema=schema,
        schema_fingerprint="history-refresh",
        historical_queries=[],
    )
    history = [
        {
            "selected_tables": ["sales_invoices"],
            "validated": True,
            "execution_success": True,
            "confidence": 100,
        }
    ]

    refreshed = profiler.get_or_build(
        database_id="finance",
        dialect="mysql",
        schema=schema,
        schema_fingerprint="history-refresh",
        historical_queries=history,
    )

    assert refreshed is not first
    assert refreshed.historical_query_count == 1
    assert table_by_name(refreshed, "sales_invoices").historical_success_count == 1


def test_success_history_is_bounded_deduplicated_and_does_not_store_sql(tmp_path):
    history = SchemaProfileHistory(history_dir=tmp_path, max_records=2)
    history.record_success("schema-a", "SELECT * FROM customers", ["customers"])
    history.record_success("schema-a", "SELECT  *  FROM customers", ["customers"])
    history.record_success("schema-a", "SELECT * FROM invoices", ["invoices"])
    history.record_success("schema-a", "SELECT * FROM payments", ["payments"])

    records = history.list("schema-a")

    assert len(records) == 2
    assert [record["selected_tables"] for record in records] == [["invoices"], ["payments"]]
    assert all("sql" not in record for record in records)


def test_weak_suffix_never_overrides_stronger_metadata(tmp_path):
    schema = {
        "sales_mst": table(
            column("event_id", "int", primary_key=True),
            column("customer_id", "int"),
            column("event_date", "date"),
            column("sales_amount", "decimal"),
            foreign_keys=[{"column": "customer_id", "table": "customers", "references": "customer_id"}],
            row_count=1000,
        ),
        "customers": table(column("customer_id", "int", primary_key=True), column("customer_name"), row_count=20),
    }

    profile = build(SchemaProfiler(profile_dir=tmp_path), "weak", schema)

    assert table_by_name(profile, "sales_mst").likely_role == "transaction"


def test_profiles_are_cached_by_schema_fingerprint(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    schema = sales_schema()
    first = profiler.get_or_build(
        database_id="sales",
        dialect="mysql",
        schema=schema,
        schema_fingerprint="same-fingerprint",
    )
    second = profiler.get_or_build(
        database_id="sales-renamed",
        dialect="mysql",
        schema={},
        schema_fingerprint="same-fingerprint",
    )
    changed = profiler.get_or_build(
        database_id="sales",
        dialect="mysql",
        schema={**schema, "new_table": table(column("id", "int", primary_key=True))},
        schema_fingerprint="changed-fingerprint",
    )

    assert second is first
    assert changed.table_count == first.table_count + 1


@pytest.mark.parametrize("size", [500, 1000])
def test_large_schema_profiles_every_table_with_bounded_runtime(tmp_path, size):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    schema = large_schema(size)
    started = time.perf_counter()

    profile = build(profiler, f"large-{size}", schema)
    duration = time.perf_counter() - started

    assert profile.table_count == size
    assert all(table_profile.explanation for table_profile in profile.tables)
    assert duration < 8.0


def test_profile_round_trip_preserves_models(tmp_path):
    profiler = SchemaProfiler(profile_dir=tmp_path)
    original = build(profiler, "roundtrip", manufacturing_schema())

    restored = profiler.profile_from_dict(original.to_dict())

    assert restored.to_dict() == original.to_dict()

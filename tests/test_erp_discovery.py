import json

from business_catalog import business_catalog
from erp_discovery import build_discovery_from_schema
from schema_profile_fixtures import column, construction_schema, finance_schema, table


def _row_counts(schema):
    return {table_name: info.get("row_count") for table_name, info in schema.items()}


def _catalog(schema, fingerprint="discovery-fp"):
    return business_catalog.build(
        schema,
        source_type="synthetic",
        row_counts=_row_counts(schema),
        fingerprint=fingerprint,
    )


def test_discovery_report_groups_tables_columns_and_relationships(tmp_path):
    schema = construction_schema()
    catalog = _catalog(schema)

    report = build_discovery_from_schema(
        database_id="construction",
        dialect="synthetic",
        schema=schema,
        schema_fingerprint="construction-discovery",
        row_counts=_row_counts(schema),
        catalog=catalog,
        safe_samples={
            "ProjectMaster": {
                "ProjectName": ["RDSS-VIZAG", "Metro East"],
                "ProjectStatus": ["Active", "Delayed"],
            },
            "material_issue_dtl": {
                "issued_qty": [10, 15],
                "unit_rate": [100, 120],
            },
        },
        profile_dir=tmp_path / "profiles",
        discovery_dir=tmp_path / "discovery",
    )
    payload = report.to_dict()

    assert payload["schema_fingerprint"] == "construction-discovery"
    assert payload["discovered_modules"]
    assert "master_tables" in payload["table_classes"]
    assert any(item["table"] == "ProjectMaster" for item in payload["table_classes"]["master_tables"])
    assert any(item["table"] == "material_issue_dtl" for item in payload["table_classes"]["transaction_detail_tables"])
    assert any(item["column"] == "ProjectID" for item in payload["column_inventory"]["primary_keys"])
    assert any(item["column"] == "ProjectName" for item in payload["column_inventory"]["entity_display_name_columns"])
    assert any(item["column"] == "issued_qty" for item in payload["column_inventory"]["quantity_columns"])
    assert payload["likely_relationships"]
    assert payload["population_summary"]["populated_table_count"] == len(schema)


def test_discovery_reports_ambiguous_mappings_and_excluded_tables(tmp_path):
    schema = {
        **finance_schema(),
        "invoice_history": table(
            column("invoice_history_id", "bigint", primary_key=True),
            column("invoice_id", "uuid"),
            column("changed_at", "datetime"),
            row_count=900,
        ),
        "tmp_invoice_work": table(
            column("tmp_id", "int", primary_key=True),
            column("invoice_id", "uuid"),
            row_count=0,
        ),
    }
    catalog = {
        "entities": {
            "billing": {
                "tables": [
                    {"table": f"billing_candidate_{index}"}
                    for index in range(12)
                ]
            }
        },
        "unsupported_kpis": ["profit requires cost and revenue definitions"],
    }

    report = build_discovery_from_schema(
        database_id="finance",
        dialect="synthetic",
        schema=schema,
        schema_fingerprint="finance-discovery",
        row_counts=_row_counts(schema),
        catalog=catalog,
        profile_dir=tmp_path / "profiles",
        discovery_dir=tmp_path / "discovery",
    ).to_dict()

    assert any(item["concept"] == "billing" for item in report["ambiguous_mappings"])
    assert any(item["table"] == "invoice_history" for item in report["excluded_from_retrieval"])
    assert any(item["table"] == "tmp_invoice_work" for item in report["excluded_from_retrieval"])
    assert any("Unsupported or incomplete KPI" in item for item in report["missing_business_definitions"])


def test_discovery_detects_duplicate_risk_relationships(tmp_path):
    schema = {
        "project_master": table(column("project_id", "int", primary_key=True), column("project_name"), row_count=10),
        "site_master": table(column("site_id", "int", primary_key=True), column("site_name"), row_count=30),
        "issue_header": table(
            column("issue_id", "int", primary_key=True),
            column("project_id", "int"),
            column("site_id", "int"),
            foreign_keys=[
                {"column": "project_id", "table": "project_master", "references": "project_id"},
                {"column": "site_id", "table": "site_master", "references": "site_id"},
            ],
            row_count=100,
        ),
        "return_header": table(
            column("return_id", "int", primary_key=True),
            column("project_id", "int"),
            column("site_id", "int"),
            foreign_keys=[
                {"column": "project_id", "table": "project_master", "references": "project_id"},
                {"column": "site_id", "table": "site_master", "references": "site_id"},
            ],
            row_count=100,
        ),
        "purchase_header": table(
            column("purchase_id", "int", primary_key=True),
            column("project_id", "int"),
            column("site_id", "int"),
            foreign_keys=[
                {"column": "project_id", "table": "project_master", "references": "project_id"},
                {"column": "site_id", "table": "site_master", "references": "site_id"},
            ],
            row_count=100,
        ),
        "billing_header": table(
            column("bill_id", "int", primary_key=True),
            column("project_id", "int"),
            column("site_id", "int"),
            foreign_keys=[
                {"column": "project_id", "table": "project_master", "references": "project_id"},
                {"column": "site_id", "table": "site_master", "references": "site_id"},
            ],
            row_count=100,
        ),
    }

    report = build_discovery_from_schema(
        database_id="risk",
        dialect="synthetic",
        schema=schema,
        schema_fingerprint="risk-discovery",
        row_counts=_row_counts(schema),
        catalog=_catalog(schema, "risk-discovery"),
        profile_dir=tmp_path / "profiles",
        discovery_dir=tmp_path / "discovery",
    ).to_dict()

    assert any(
        item["risk_type"] == "widely_reused_join_column_name"
        and item["column"] in {"project_id", "site_id"}
        for item in report["duplicate_risk_relationships"]
    )


def test_discovery_is_stored_by_schema_fingerprint(tmp_path):
    schema = construction_schema()
    discovery_dir = tmp_path / "discovery"

    first = build_discovery_from_schema(
        database_id="construction",
        dialect="synthetic",
        schema=schema,
        schema_fingerprint="stored-fingerprint",
        row_counts=_row_counts(schema),
        catalog=_catalog(schema, "stored-fingerprint"),
        profile_dir=tmp_path / "profiles",
        discovery_dir=discovery_dir,
    )
    path = discovery_dir / "stored-fingerprint.json"

    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_fingerprint"] == first.schema_fingerprint

    second = build_discovery_from_schema(
        database_id="construction",
        dialect="synthetic",
        schema=schema,
        schema_fingerprint="stored-fingerprint",
        row_counts=_row_counts(schema),
        catalog=_catalog(schema, "stored-fingerprint"),
        profile_dir=tmp_path / "profiles",
        discovery_dir=discovery_dir,
    )

    assert second.to_dict() == first.to_dict()

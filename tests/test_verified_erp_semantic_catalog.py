import json
from pathlib import Path

from business_catalog import business_catalog
from schema_retriever import SchemaRetriever
from semantic_parser import semantic_parser
from verified_semantic_catalog import (
    ERP_CATALOG_CONTRACT_VERSION,
    VerifiedCatalogError,
    load_verified_semantic_catalog,
    normalize_verified_semantic_catalog,
    validate_verified_semantic_catalog,
)


ERP_SCHEMA_FINGERPRINT = "998cd2da2ba38660fd89084960b4893c56daf7857b78a388772e81ad8a6372a5"
CATALOG_PATH = Path("backend/semantic_catalogs") / f"{ERP_SCHEMA_FINGERPRINT}.json"
MAPPING_SECTIONS = (
    "business_concepts",
    "entity_types",
    "important_tables",
    "metrics",
    "dimensions",
    "date_fields",
    "status_fields",
    "join_paths",
    "business_formulas",
    "duplicate_risk_warnings",
    "unresolved_definitions",
)


def erp_catalog_schema():
    def cols(*names):
        return [{"column_name": name, "data_type": "varchar"} for name in names]

    return {
        "project_mst": {
            "columns": cols(
                "project_id",
                "project_name",
                "project_short_name",
                "name_of_work",
                "project_value",
                "estimated_value",
                "compeny_project_value",
                "client_name",
                "project_state",
                "project_location",
                "status",
                "project_start_date",
                "project_end_date",
            ),
            "foreign_keys": [],
        },
        "client_bill_raising": {
            "columns": cols(
                "billing_id",
                "project_id",
                "bill_date",
                "bill_amount",
                "gross_bill_value",
                "total_amount",
                "billing_status",
            ),
            "foreign_keys": [],
        },
        "client_bills_boq_wisedetails": {
            "columns": cols(
                "client_bills_boq_wisedetails_id",
                "billing_id",
                "boq_total_amount",
                "boq_rasing_amount",
                "raising_qty",
                "client_unit_rate",
                "project_locations_id",
                "locationwise_amt",
            ),
            "foreign_keys": [],
        },
        "po_form": {
            "columns": cols("po_form_id", "po_no", "date", "project", "po_id", "vendor_name", "sub_total", "total_value", "po_validity_date"),
            "foreign_keys": [],
        },
        "po_items": {
            "columns": cols("po_item_id", "po_id", "material_id", "sub_material_id", "qty", "total_price", "item_unit_price", "po_project_id", "status", "is_po_item_status"),
            "foreign_keys": [],
        },
        "po_final_price": {
            "columns": cols("vendor_id"),
            "foreign_keys": [],
        },
        "vendor_mst": {
            "columns": cols(
                "vendor_id",
                "vendor_code",
                "vendor_name",
                "vendor_address",
                "email",
                "contact_no",
                "contact_person_name",
                "vendor_gst_no",
                "vendor_types_id",
            ),
            "foreign_keys": [],
        },
        "vendor_branch_details": {
            "columns": cols(
                "vendor_branch_details_id",
                "vendor_id",
                "address",
                "gst_no",
                "email",
                "contact_person",
                "contact_number",
                "state",
                "country",
                "gst_type",
            ),
            "foreign_keys": [],
        },
        "vendor_types": {
            "columns": cols("vendor_types_id", "vendor_type", "vendor_description"),
            "foreign_keys": [],
        },
        "vendor_material_details": {
            "columns": cols("vendor_material_id", "vendor_id", "sub_material_id"),
            "foreign_keys": [],
        },
        "grn_master": {
            "columns": cols("vendor_name"),
            "foreign_keys": [],
        },
        "goods_receipt_cum_insp_note": {
            "columns": cols("grcin_id", "grn_po_id", "grn_project_id", "grn_sub_material_id", "qty_actual_received", "value", "shortage", "grn_date", "base_price", "supplier_return_qty"),
            "foreign_keys": [],
        },
        "store_usage_quantity_master": {
            "columns": cols("store_usage_quantity_master_id", "project_id", "store_id", "issued_date", "actual_issued_date", "transactiontype", "cancel_status"),
            "foreign_keys": [],
        },
        "store_material_usage_quantity": {
            "columns": cols("store_material_usage_quantity_id", "store_usage_quantity_master_id", "sub_material_id", "requested_quantity", "issued_quantity", "base_charge", "transactiontype"),
            "foreign_keys": [],
        },
        "inventory_master": {
            "columns": cols("inventory_master_id", "sub_store_id", "material_id", "sub_material_id", "total_quanity", "available_quantity", "status"),
            "foreign_keys": [],
        },
        "inventory_master_transaction": {
            "columns": cols("inventory_master_transaction_id", "sub_material_id", "quantity", "is_it_inward"),
            "foreign_keys": [],
        },
        "sub_material": {
            "columns": cols("sub_material_id", "material_id", "sub_material_name", "uom", "submaterialcode", "item_code"),
            "foreign_keys": [],
        },
        "employee_table": {
            "columns": cols("employee_id", "emp_ref_id", "emp_name", "department_id", "project_id", "emp_status", "date_of_joining", "sal_ctc"),
            "foreign_keys": [],
        },
        "department": {
            "columns": cols("department_id", "department_name", "division_id"),
            "foreign_keys": [],
        },
        "emp_attendance_register": {
            "columns": cols("emp_attendance_register_id", "project_id", "project_sites_id", "employee_id", "date", "attendence_type_name", "approval_status"),
            "foreign_keys": [],
        },
        "employee_attendance_punching_time": {
            "columns": cols("employee_attendance_punching_time_id"),
            "foreign_keys": [],
        },
        "subcontract_workorder_details": {
            "columns": cols("subcontract_workorder_details_id", "project_id", "workorder_no", "after_taxes_grandtotal", "date_of_Commencement", "work_order_status", "payment_status"),
            "foreign_keys": [],
        },
        "subcontract_workorder": {
            "columns": cols("subcontract_workorder_id", "grand_total"),
            "foreign_keys": [],
        },
    }


def test_verified_erp_catalog_file_has_required_mapping_metadata():
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    assert payload["schema_fingerprint"] == ERP_SCHEMA_FINGERPRINT
    assert payload["catalog_contract_version"] == ERP_CATALOG_CONTRACT_VERSION
    assert payload["erp_product"] == "RC ERP"
    assert payload["erp_schema_version"]
    for section in MAPPING_SECTIONS:
        assert payload[section], section
        for item in payload[section]:
            assert "source_of_evidence" in item or section == "excluded_from_retrieval"
            assert "confidence" in item
            assert "verification_status" in item
            assert "applicable_module" in item
            assert "alternative_candidates" in item
            assert "ambiguity_notes" in item


def test_verified_erp_catalog_loads_by_schema_fingerprint(monkeypatch):
    monkeypatch.delenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG", raising=False)

    payload = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)

    assert payload["catalog_name"] == "Verified ERP Semantic Catalog"
    assert any(item["concept"] == "project" for item in payload["business_concepts"])
    assert payload["validation"]["valid"] is True
    assert payload["validation"]["verified_mapping_count"] > 0
    assert payload["business_concepts"][0]["mapping_id"]
    assert payload["business_concepts"][0]["confidence_score"] > 0
    assert payload["business_concepts"][0]["evidence_sources"]


def test_verified_erp_catalog_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG", "1")

    assert load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT) is None


def test_verified_erp_catalog_is_compatible_with_expected_schema(monkeypatch):
    monkeypatch.delenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG", raising=False)

    payload = load_verified_semantic_catalog(
        ERP_SCHEMA_FINGERPRINT,
        schema=erp_catalog_schema(),
        strict=True,
    )

    assert payload["validation"]["valid"] is True
    assert payload["validation"]["compatible"] is True
    assert payload["validation"]["errors"] == []


def test_poc_scope_is_explicit_and_supported_modules_have_verified_evidence():
    catalog = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    scope = catalog["poc_scope"]

    assert scope["version"] == "rcerp-poc-scope:v1"
    assert scope["read_only"] is True
    assert scope["unknown_concept_policy"] == "clarify_or_safe_failure"
    assert set(scope["supported_modules"]).isdisjoint(
        scope["out_of_scope_modules"]
    )

    evidence_modules = {
        item["applicable_module"]
        for section in (
            "business_concepts",
            "metrics",
            "dimensions",
            "date_fields",
            "status_fields",
            "join_paths",
        )
        for item in catalog[section]
        if str(item.get("verification_status", "")).startswith("verified")
        and item.get("applicable_module")
    }
    assert set(scope["supported_modules"]) <= evidence_modules


def test_supported_poc_modules_have_complete_verified_business_definitions():
    catalog = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    definitions = catalog["business_definitions"]
    required_fields = {
        "source_columns",
        "metric_definition",
        "formula",
        "aggregation",
        "data_grain",
        "date_field",
        "status_field",
        "valid_status_values",
        "null_handling",
        "approved_joins",
        "duplicate_risk_rules",
        "reference_sql",
        "expected_output",
    }

    assert len(definitions) == 19
    assert {
        definition["applicable_module"]
        for definition in definitions
    } >= set(catalog["poc_scope"]["supported_modules"]) - {"departments"}
    for definition in definitions:
        assert definition["verification_status"] == "verified"
        assert required_fields <= definition.keys()
        assert definition["source_columns"]
        assert definition["expected_output"]["columns"]
        assert definition["reference_sql"].lstrip().lower().startswith(
            ("select ", "with ")
        )


def test_ambiguous_management_kpis_are_formally_unresolved():
    catalog = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    unresolved = {
        item["concept"]: item
        for item in catalog["unresolved_definitions"]
    }

    assert {
        "profit_loss",
        "margin",
        "performance",
        "department_performance",
        "risk_score",
        "project_cost",
    } <= unresolved.keys()
    assert all(
        unresolved[concept]["verification_status"] == "unresolved"
        for concept in (
            "profit_loss",
            "margin",
            "performance",
            "risk_score",
            "project_cost",
        )
    )


def test_incomplete_business_definition_invalidates_poc_catalog():
    catalog = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    catalog = json.loads(json.dumps(catalog))
    catalog["business_definitions"][0].pop("null_handling")

    validation = validate_verified_semantic_catalog(
        catalog,
        expected_schema_fingerprint=ERP_SCHEMA_FINGERPRINT,
    )

    assert validation.valid is False
    assert any(
        "missing 'null_handling'" in error
        for error in validation.errors
    )


def test_every_runtime_relationship_has_complete_live_evidence():
    catalog = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    evidence = catalog["relationship_evidence"]

    assert len(evidence) == len(catalog["join_paths"]) == 15
    assert all(item["runtime_approved"] is True for item in evidence)
    assert all(
        item["observed_cardinality"] == "many_to_one"
        for item in evidence
    )
    assert all(item["right_key_uniqueness"] == 1.0 for item in evidence)
    assert all(item["join_amplification"] <= 1.1 for item in evidence)
    assert all(
        relationship["runtime_approved"] is True
        and relationship["evidence_id"]
        for relationship in catalog["join_paths"]
    )

    orphan_counts = {
        item["evidence_id"]: item["orphan_left_rows"]
        for item in evidence
        if item["orphan_left_rows"]
    }
    assert orphan_counts == {
        "rel-po-item-header": 41,
        "rel-employee-department": 2,
    }


def test_unapproved_relationship_is_removed_from_runtime_graph():
    catalog = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    catalog = json.loads(json.dumps(catalog))
    relationship = catalog["join_paths"][0]
    relationship["runtime_approved"] = False
    relationship["verification_status"] = "candidate"

    runtime_relationships = [
        item
        for item in catalog["join_paths"]
        if item["verification_status"].startswith("verified")
        and item["runtime_approved"] is True
    ]

    assert relationship not in runtime_relationships
    assert len(runtime_relationships) == 14


def test_missing_relationship_evidence_invalidates_poc_catalog():
    catalog = load_verified_semantic_catalog(ERP_SCHEMA_FINGERPRINT)
    catalog = json.loads(json.dumps(catalog))
    catalog["relationship_evidence"].pop()

    validation = validate_verified_semantic_catalog(
        catalog,
        expected_schema_fingerprint=ERP_SCHEMA_FINGERPRINT,
    )

    assert validation.valid is False
    assert any(
        "has no evidence" in error
        for error in validation.errors
    )


def test_verified_erp_catalog_rejects_missing_verified_column(monkeypatch):
    monkeypatch.delenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG", raising=False)
    schema = erp_catalog_schema()
    schema["project_mst"]["columns"] = [
        column
        for column in schema["project_mst"]["columns"]
        if column["column_name"] != "project_value"
    ]

    try:
        load_verified_semantic_catalog(
            ERP_SCHEMA_FINGERPRINT,
            schema=schema,
            strict=True,
        )
    except VerifiedCatalogError as error:
        assert "project_mst.project_value" in str(error)
    else:
        raise AssertionError("Strict verified catalog loading should reject schema drift.")


def test_catalog_contract_rejects_invalid_mapping_metadata():
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["metrics"][0].pop("source_of_evidence")
    normalized = normalize_verified_semantic_catalog(payload)

    result = validate_verified_semantic_catalog(
        normalized,
        expected_schema_fingerprint=ERP_SCHEMA_FINGERPRINT,
    )

    assert result.valid is False
    assert any("source_of_evidence" in error for error in result.errors)


def test_business_catalog_merges_verified_erp_mappings(monkeypatch):
    monkeypatch.delenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG", raising=False)
    business_catalog.clear(ERP_SCHEMA_FINGERPRINT)

    catalog = business_catalog.build(
        erp_catalog_schema(),
        source_type="mysql",
        row_counts={"project_mst": 19, "po_form": 100, "client_bill_raising": 100},
        fingerprint=ERP_SCHEMA_FINGERPRINT,
    )

    verified = catalog["verified_semantic_catalog"]
    assert catalog["catalog_version"] == "verified-erp-semantic-catalog:v12"
    assert verified["authority"] == "verified_erp_primary"
    assert verified["fallback_policy"] == "offline_discovery_only"
    assert verified["validation"]["compatible"] is True
    assert len(verified["business_definitions"]) == 19
    assert any(
        item["concept_id"] == "gross_billing"
        and item["date_storage_format"] == "%d-%m-%Y"
        for item in verified["business_definitions"]
    )
    assert catalog["catalog_authority"] == "verified_erp_primary"
    assert catalog["erp_schema_version"] == "rcerp-jul8:v1"
    assert catalog["catalog_validation"]["verified_mapping_count"] > 0
    assert any(
        "verified_erp_catalog" in edge["evidence"]
        for edge in catalog["verified_relationship_graph"]["edges"]
    )
    assert any(item["concept"] == "project" and item["preferred_table"] == "project_mst" for item in verified["business_concepts"])
    assert any(item["metric"] == "purchase_value" and item["table"] == "po_form" and item["column"] == "total_value" for item in catalog["metrics"])
    assert any(item["dimension"] == "vendor" and item["table"] == "po_form" and item["column"] == "vendor_name" for item in catalog["dimensions"])
    assert any(item["table"] == "store_usage_quantity_master" and item["column"] == "issued_date" for item in catalog["date_fields"])
    assert any(item["table"] == "client_bill_raising" and item["column"] == "billing_status" for item in catalog["status_fields"])
    assert any(item["left_table"] == "client_bill_raising" and item["right_table"] == "project_mst" for item in catalog["join_paths"])
    assert "profit_loss" in catalog["unsupported_kpis"]


def test_verified_catalog_improves_retrieval_for_purchase_value(monkeypatch, tmp_path):
    monkeypatch.delenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG", raising=False)
    business_catalog.clear(ERP_SCHEMA_FINGERPRINT)
    schema = erp_catalog_schema()
    row_counts = {"po_form": 100, "po_items": 1000, "project_mst": 19}
    catalog = business_catalog.build(schema, source_type="mysql", row_counts=row_counts, fingerprint=ERP_SCHEMA_FINGERPRINT)
    retriever = SchemaRetriever(profile_dir=tmp_path)
    plan = semantic_parser.parse("Which vendor has the highest purchase value?").to_dict()

    result = retriever.retrieve(
        question="Which vendor has the highest purchase value?",
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint=ERP_SCHEMA_FINGERPRINT,
        semantic_plan=plan,
        candidate_limit=6,
    )

    assert "po_form" in result.selected_tables
    assert "vendor_name" in result.relevant_columns["po_form"]
    assert "total_value" in result.relevant_columns["po_form"]


def test_verified_catalog_completes_columns_for_selected_project_table(monkeypatch, tmp_path):
    monkeypatch.delenv("SQL_ASSISTANT_DISABLE_VERIFIED_CATALOG", raising=False)
    business_catalog.clear(ERP_SCHEMA_FINGERPRINT)
    schema = erp_catalog_schema()
    row_counts = {"project_mst": 19}
    catalog = business_catalog.build(schema, source_type="mysql", row_counts=row_counts, fingerprint=ERP_SCHEMA_FINGERPRINT)
    retriever = SchemaRetriever(profile_dir=tmp_path)
    plan = semantic_parser.parse("Show the top 5 projects by project value.").to_dict()

    result = retriever.retrieve(
        question="Show the top 5 projects by project value.",
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint=ERP_SCHEMA_FINGERPRINT,
        semantic_plan=plan,
        candidate_limit=6,
    )

    assert "project_mst" in result.selected_tables
    assert "project_value" in result.relevant_columns["project_mst"]

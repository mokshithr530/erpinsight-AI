from business_catalog import BusinessCatalog
from schema_profiler import RelationshipProfile, SchemaProfiler
from schema_retriever import SchemaRetriever
from semantic_parser import semantic_parser
from schema_profile_fixtures import column, table


def _retrieve(tmp_path, question, schema, catalog):
    row_counts = {name: info.get("row_count") for name, info in schema.items()}
    retriever = SchemaRetriever(profile_dir=tmp_path)
    plan = semantic_parser.parse(question, allow_llm_fallback=False).to_dict()
    result = retriever.retrieve(
        question=question,
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint="relationship-hardening",
        semantic_plan=plan,
        candidate_limit=8,
    )
    profiles = retriever.profiles_for(
        schema=schema,
        row_counts=row_counts,
        catalog=catalog,
        schema_fingerprint="relationship-hardening",
    )
    return result, retriever.compact_prompt(result, profiles)


def test_one_to_many_declared_join_remains_low_risk(tmp_path):
    schema = {
        "customers": table(column("customer_id", "int", primary_key=True), column("customer_name"), row_count=100),
        "orders": table(
            column("order_id", "int", primary_key=True),
            column("customer_id", "int", indexed=True),
            column("total_value", "decimal"),
            foreign_keys=[{"column": "customer_id", "table": "customers", "references": "customer_id"}],
            row_count=1000,
        ),
    }
    catalog = BusinessCatalog().build(schema, source_type="synthetic", row_counts={
        "customers": 100,
        "orders": 1000,
    }, fingerprint="relationship-low-risk")

    result, prompt = _retrieve(tmp_path, "Total order value by customer", schema, catalog)

    assert result.relationship_paths
    assert result.relationship_paths[0].declared_edge_count == 1
    assert result.relationship_paths[0].duplicate_amplification_risk == "low"
    assert "pre_aggregate=True" not in prompt


def test_header_detail_relationship_carries_pre_aggregation_warning(tmp_path):
    schema = {
        "invoice_header": table(
            column("invoice_id", "int", primary_key=True),
            column("customer_id", "int", indexed=True),
            column("invoice_total", "decimal"),
            row_count=1000,
        ),
        "invoice_detail": table(
            column("invoice_line_id", "int", primary_key=True),
            column("invoice_id", "int", indexed=True),
            column("product_name"),
            column("line_quantity", "decimal"),
            row_count=5000,
        ),
    }
    catalog = BusinessCatalog().build(schema, source_type="synthetic", row_counts={
        "invoice_header": 1000,
        "invoice_detail": 5000,
    }, fingerprint="relationship-header-detail")
    catalog["join_paths"] = [{
        "left_table": "invoice_detail",
        "left_column": "invoice_id",
        "right_table": "invoice_header",
        "right_column": "invoice_id",
        "relationship_type": "many_to_one",
        "cardinality": "many invoice detail rows to one invoice header",
        "confidence": "high",
        "source_of_evidence": "verified benchmark reference SQL",
        "verification_status": "verified",
        "duplicate_amplification_risk": "medium",
        "required_pre_aggregation": True,
        "applicable_business_modules": ["billing"],
    }]

    result, prompt = _retrieve(tmp_path, "Total invoice amount by product name", schema, catalog)

    assert result.relationship_paths
    path = result.relationship_paths[0]
    assert path.duplicate_amplification_risk == "medium"
    assert path.required_pre_aggregation is True
    assert "pre-aggregate facts before joins" in prompt
    assert "do not sum repeated header amounts" in prompt


def test_many_to_many_relationship_is_high_risk_and_warns(tmp_path):
    schema = {
        "projects": table(column("project_id", "int", primary_key=True), column("project_name"), row_count=10),
        "employees": table(column("employee_id", "int", primary_key=True), column("employee_name"), row_count=100),
        "project_employee": table(
            column("project_id", "int", indexed=True),
            column("employee_id", "int", indexed=True),
            column("allocated_hours", "decimal"),
            row_count=1000,
        ),
    }
    catalog = BusinessCatalog().build(schema, source_type="synthetic", row_counts={
        "projects": 10,
        "employees": 100,
        "project_employee": 1000,
    }, fingerprint="relationship-many-to-many")
    catalog["join_paths"] = [
        {
            "left_table": "project_employee",
            "left_column": "project_id",
            "right_table": "projects",
            "right_column": "project_id",
            "relationship_type": "many_to_one",
            "cardinality": "many bridge rows to one project",
            "confidence": "high",
            "verification_status": "verified",
            "duplicate_amplification_risk": "medium",
            "required_pre_aggregation": True,
        },
        {
            "left_table": "project_employee",
            "left_column": "employee_id",
            "right_table": "employees",
            "right_column": "employee_id",
            "relationship_type": "many_to_one",
            "cardinality": "many bridge rows to one employee; many-to-many project employee association",
            "confidence": "high",
            "verification_status": "verified",
            "duplicate_amplification_risk": "high",
            "required_pre_aggregation": True,
        },
    ]

    result, _prompt = _retrieve(tmp_path, "Compare project allocated hours by employee name", schema, catalog)

    assert result.relationship_paths
    assert any(path.duplicate_amplification_risk == "high" for path in result.relationship_paths)
    assert any("duplicate-amplification risk" in warning for warning in result.warnings)


def test_display_column_join_is_penalized_without_verification():
    retriever = SchemaRetriever()
    relationship = RelationshipProfile(
        "orders",
        ["customer_name"],
        "customers",
        ["customer_name"],
        "inferred",
        False,
        0.8,
        ["shared_identifier_name"],
        "unknown",
        "inferred",
        "medium",
        False,
        [],
    )

    path = retriever._path_profile(["orders", "customers"], [relationship])

    assert path.score < 0.7
    assert any("display-like columns" in warning for warning in path.warnings)


def test_schema_profile_records_required_relationship_fields(tmp_path):
    schema = {
        "po_header": table(column("po_id", "int", primary_key=True), column("total_value", "decimal"), row_count=100),
        "po_line": table(column("po_line_id", "int", primary_key=True), column("po_id", "int"), row_count=500),
    }
    catalog = {
        "source_type": "synthetic",
        "join_paths": [{
            "left_table": "po_line",
            "left_column": "po_id",
            "right_table": "po_header",
            "right_column": "po_id",
            "relationship_type": "many_to_one",
            "cardinality": "many detail rows to one header",
            "confidence": "medium",
            "verification_status": "verified",
            "duplicate_amplification_risk": "medium",
            "required_pre_aggregation": True,
            "applicable_business_modules": ["procurement"],
        }],
    }

    profile = SchemaProfiler(profile_dir=tmp_path).build(
        database_id="synthetic",
        dialect="mysql",
        schema=schema,
        schema_fingerprint="relationship-fields",
        row_counts={"po_header": 100, "po_line": 500},
        catalog=catalog,
    )

    relationship = profile.relationships[0]
    assert relationship.source_table == "po_line"
    assert relationship.target_table == "po_header"
    assert relationship.cardinality == "many detail rows to one header"
    assert relationship.verification_status == "verified"
    assert relationship.duplicate_amplification_risk == "medium"
    assert relationship.required_pre_aggregation is True
    assert relationship.applicable_business_modules == ["procurement"]

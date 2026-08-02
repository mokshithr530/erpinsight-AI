from query_plan import QueryPlan, query_plan_guard


def _schema():
    def column(name):
        return {"column_name": name, "name": name, "data_type": "varchar"}

    return {
        "client_bill_raising": {
            "columns": [
                column("billing_id"),
                column("project_id"),
                column("total_amount"),
                column("bill_amount"),
                column("bill_date"),
                column("when_created"),
                column("billing_status"),
            ]
        },
        "project_mst": {
            "columns": [
                column("project_id"),
                column("project_name"),
                column("project_value"),
                column("project_state"),
            ]
        },
        "billing_log_archive": {
            "columns": [
                column("billing_id"),
                column("project_id"),
                column("total_amount"),
            ]
        },
    }


def _plan():
    relationship = {
        "left_table": "client_bill_raising",
        "left_column": "project_id",
        "right_table": "project_mst",
        "right_column": "project_id",
        "relationship_type": "many_to_one",
    }
    return QueryPlan(
        original_question=(
            "Compare approved billing across projects in 2025 and show top 3."
        ),
        operation="ranking",
        intent_category="data_query",
        requested_outputs=["project"],
        base_entity="project",
        metrics=["billing"],
        aggregation=["sum"],
        dimensions=["project"],
        date_constraints=[
            {
                "type": "year",
                "value": 2025,
                "start": "2025-01-01",
                "end": "2025-12-31",
            }
        ],
        status_constraints=[
            {
                "type": "status",
                "values": ["approved"],
                "normalized_value": "approved",
            }
        ],
        grouping=["project"],
        ordering=[{"target_concept": "billing", "direction": "desc"}],
        limit=3,
        joins=[relationship],
        aggregation_grain=["billing_header", "project"],
        selected_tables=[
            "client_bill_raising",
            "project_mst",
            "billing_log_archive",
        ],
        selected_columns={
            "client_bill_raising": [
                "billing_id",
                "project_id",
                "total_amount",
                "bill_amount",
                "bill_date",
                "when_created",
                "billing_status",
            ],
            "project_mst": [
                "project_id",
                "project_name",
                "project_value",
            ],
            "billing_log_archive": [
                "billing_id",
                "project_id",
                "total_amount",
            ],
        },
        generation_tables=["client_bill_raising", "project_mst"],
        generation_columns={
            "client_bill_raising": [
                "billing_id",
                "project_id",
                "total_amount",
                "bill_date",
                "billing_status",
            ],
            "project_mst": ["project_id", "project_name"],
        },
        catalog_authoritative=True,
        bound_metrics=[
            {
                "metric": "billing",
                "table": "client_bill_raising",
                "column": "total_amount",
                "default_aggregation": "sum",
            }
        ],
        bound_dimensions=[
            {
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
            }
        ],
        bound_date_fields=[
            {
                "semantic": "billing_date",
                "table": "client_bill_raising",
                "column": "bill_date",
            }
        ],
        bound_status_fields=[
            {
                "table": "client_bill_raising",
                "column": "billing_status",
                "semantics": ["pending", "approved", "completed"],
            }
        ],
        approved_relationships=[relationship],
    )


def _valid_sql():
    return """
SELECT
    p.project_name,
    SUM(c.total_amount) AS total_billing
FROM client_bill_raising c
JOIN project_mst p
    ON c.project_id = p.project_id
WHERE YEAR(c.bill_date) = 2025
  AND c.billing_status = 'approved'
GROUP BY p.project_name
ORDER BY total_billing DESC
LIMIT 3
"""


def _validate(sql):
    return query_plan_guard.validate_sql(
        sql,
        _plan(),
        schema=_schema(),
    )


def test_accepts_sql_that_exactly_preserves_verified_plan():
    result = _validate(_valid_sql())

    assert result["valid"] is True
    assert result["analysis"]["tables"] == [
        "client_bill_raising",
        "project_mst",
    ]
    assert result["analysis"]["joins"][0]["column_pairs"][0] == {
        "left_table": "client_bill_raising",
        "left_column": "project_id",
        "right_table": "project_mst",
        "right_column": "project_id",
    }


def test_rejects_similarly_named_table_outside_canonical_context():
    sql = _valid_sql().replace(
        "client_bill_raising c",
        "billing_log_archive c",
    )

    result = _validate(sql)

    assert result["valid"] is False
    assert "outside canonical ERP context" in " ".join(result["errors"])


def test_rejects_wrong_metric_column_even_when_column_exists():
    result = _validate(
        _valid_sql().replace("SUM(c.total_amount)", "SUM(c.bill_amount)")
    )

    errors = " ".join(result["errors"])
    assert result["valid"] is False
    assert "outside canonical ERP DDL" in errors
    assert "does not use verified metric field" in errors


def test_rejects_unapproved_join_columns():
    result = _validate(
        _valid_sql().replace(
            "c.project_id = p.project_id",
            "c.billing_id = p.project_id",
        )
    )

    assert result["valid"] is False
    assert "invented an unapproved ERP join" in " ".join(result["errors"])


def test_rejects_missing_required_table_and_dimension():
    sql = """
SELECT SUM(c.total_amount) AS total_billing
FROM client_bill_raising c
WHERE YEAR(c.bill_date) = 2025
  AND c.billing_status = 'approved'
ORDER BY total_billing DESC
LIMIT 3
"""

    result = _validate(sql)

    assert result["valid"] is False
    assert "dropped required ERP table(s): project_mst" in " ".join(
        result["errors"]
    )


def test_rejects_wrong_date_field():
    result = _validate(
        _valid_sql().replace("YEAR(c.bill_date)", "YEAR(c.when_created)")
    )

    errors = " ".join(result["errors"])
    assert result["valid"] is False
    assert "outside canonical ERP DDL" in errors
    assert "date filter does not use verified field" in errors


def test_rejects_wrong_status_field_or_missing_verified_status_field():
    result = _validate(
        _valid_sql().replace(
            "c.billing_status = 'approved'",
            "c.bill_amount = 'approved'",
        )
    )

    errors = " ".join(result["errors"])
    assert result["valid"] is False
    assert "status filter does not use verified field" in errors


def test_rejects_grouping_by_wrong_dimension():
    result = _validate(
        _valid_sql().replace(
            "GROUP BY p.project_name",
            "GROUP BY p.project_id",
        )
    )

    assert result["valid"] is False
    assert "GROUP BY does not preserve verified dimension" in " ".join(
        result["errors"]
    )


def test_count_entity_by_dimension_only_requires_breakdown_dimension_in_group_by():
    plan = _plan()
    plan.original_question = "How many projects are there by state?"
    plan.operation = "count"
    plan.requested_outputs = ["project", "location"]
    plan.metrics = []
    plan.aggregation = ["count"]
    plan.dimensions = ["project", "location"]
    plan.date_constraints = []
    plan.status_constraints = []
    plan.grouping = ["location"]
    plan.ordering = [{"target_concept": None, "direction": "desc"}]
    plan.limit = None
    plan.generation_tables = ["project_mst"]
    plan.generation_columns = {
        "project_mst": ["project_id", "project_name", "project_state"],
    }
    plan.bound_metrics = []
    plan.bound_dimensions = [
        {
            "dimension": "project",
            "requested_concept": "project",
            "table": "project_mst",
            "column": "project_name",
        },
        {
            "dimension": "location",
            "requested_concept": "location",
            "table": "project_mst",
            "column": "project_state",
        },
    ]
    plan.bound_date_fields = []
    plan.bound_status_fields = []
    plan.approved_relationships = []

    result = query_plan_guard.validate_sql(
        """
        SELECT project_state, COUNT(*) AS project_count
        FROM project_mst
        GROUP BY project_state
        ORDER BY project_count DESC
        """,
        plan,
        schema=_schema(),
    )

    assert result["valid"] is True, result["errors"]


def test_rejects_select_star_in_authoritative_context():
    result = _validate(
        _valid_sql().replace(
            "p.project_name,\n    SUM(c.total_amount) AS total_billing",
            "*, SUM(c.total_amount) AS total_billing",
        )
    )

    assert result["valid"] is False
    assert "explicit columns" in " ".join(result["errors"])


def test_allows_count_star_for_verified_count_query():
    plan = _plan()
    plan.original_question = "How many approved bills exist in 2025?"
    plan.operation = "count"
    plan.requested_outputs = []
    plan.metrics = []
    plan.aggregation = ["count"]
    plan.dimensions = []
    plan.grouping = []
    plan.ordering = []
    plan.limit = None
    plan.generation_tables = ["client_bill_raising"]
    plan.generation_columns = {
        "client_bill_raising": [
            "billing_id",
            "bill_date",
            "billing_status",
        ]
    }
    plan.bound_metrics = []
    plan.bound_dimensions = []
    plan.approved_relationships = []

    result = query_plan_guard.validate_sql(
        """
        SELECT COUNT(*) AS bill_count
        FROM client_bill_raising c
        WHERE YEAR(c.bill_date) = 2025
          AND c.billing_status = 'approved'
        """,
        plan,
        schema=_schema(),
    )

    assert result["valid"] is True, result["errors"]


def test_accepts_duplicate_safe_cte_using_approved_join():
    sql = """
WITH billing_totals AS (
    SELECT
        project_id,
        SUM(total_amount) AS total_billing
    FROM client_bill_raising
    WHERE YEAR(bill_date) = 2025
      AND billing_status = 'approved'
    GROUP BY project_id
)
SELECT
    p.project_name,
    b.total_billing
FROM billing_totals b
JOIN project_mst p
    ON b.project_id = p.project_id
GROUP BY p.project_name, b.total_billing
ORDER BY b.total_billing DESC
LIMIT 3
"""

    result = _validate(sql)

    assert result["valid"] is True, result["errors"]


def test_parse_failure_returns_clear_validation_error():
    result = _validate("SELECT FROM")

    assert result["valid"] is False
    assert "could not be parsed safely" in " ".join(result["errors"])


def test_rejects_unknown_alias_even_when_column_name_exists():
    result = _validate(
        _valid_sql().replace(
            "SUM(c.total_amount)",
            "SUM(x.total_amount)",
        )
    )

    assert result["valid"] is False
    assert "unknown table alias x" in " ".join(result["errors"])


def test_rejects_outer_grouping_when_plan_requires_one_scalar_total():
    plan = _plan()
    plan.original_question = "What is the total billing value?"
    plan.operation = "sum"
    plan.requested_outputs = []
    plan.dimensions = []
    plan.grouping = []
    plan.ordering = []
    plan.limit = None
    plan.generation_tables = ["client_bill_raising"]
    plan.generation_columns = {
        "client_bill_raising": ["total_amount"],
    }
    plan.bound_dimensions = []
    plan.approved_relationships = []

    result = query_plan_guard.validate_sql(
        """
        SELECT project_id, SUM(total_amount) AS total_billing
        FROM client_bill_raising
        GROUP BY project_id
        """,
        plan,
        schema=_schema(),
    )

    assert result["valid"] is False
    assert "unrequested outer GROUP BY" in " ".join(result["errors"])


def test_rejects_ranking_that_hides_verified_metric_from_output():
    result = _validate(
        """
        SELECT p.project_name
        FROM client_bill_raising c
        JOIN project_mst p ON c.project_id = p.project_id
        WHERE YEAR(c.bill_date) = 2025
          AND c.billing_status = 'approved'
        GROUP BY p.project_name
        ORDER BY SUM(c.total_amount) DESC
        LIMIT 3
        """
    )

    assert result["valid"] is False
    assert "does not return the verified metric field" in " ".join(
        result["errors"]
    )


def test_rejects_unrequested_outer_aggregate():
    plan = _plan()
    plan.original_question = "What is the average billing value?"
    plan.operation = "aggregation"
    plan.aggregation = ["avg"]
    plan.dimensions = []
    plan.grouping = []
    plan.ordering = []
    plan.limit = None
    plan.generation_tables = ["client_bill_raising"]
    plan.generation_columns = {
        "client_bill_raising": ["total_amount"],
    }
    plan.bound_dimensions = []
    plan.approved_relationships = []

    result = query_plan_guard.validate_sql(
        """
        SELECT
            AVG(total_amount) AS average_billing,
            SUM(total_amount) AS total_billing
        FROM client_bill_raising
        """,
        plan,
        schema=_schema(),
    )

    assert result["valid"] is False
    assert "unrequested aggregate" in " ".join(result["errors"])


def test_accepts_scalar_min_max_without_limit():
    plan = QueryPlan(
        original_question="What are the minimum and maximum project value?",
        operation="aggregation",
        intent_category="data_query",
        metrics=["project_value"],
        aggregation=["min", "max"],
        selected_tables=["project_mst"],
        selected_columns={"project_mst": ["project_value"]},
        generation_tables=["project_mst"],
        generation_columns={"project_mst": ["project_value"]},
        catalog_authoritative=True,
        bound_metrics=[{
            "metric": "project_value",
            "table": "project_mst",
            "column": "project_value",
        }],
    )

    result = query_plan_guard.validate_sql(
        """
        SELECT
            MIN(project_value) AS minimum_project_value,
            MAX(project_value) AS maximum_project_value
        FROM project_mst
        """,
        plan,
        schema=_schema(),
    )

    assert result["valid"] is True


def test_accepts_grouped_ranking_by_verified_dimension_with_entity_filter():
    schema = {
        **_schema(),
        "store_material_usage_quantity": {
            "columns": [
                {"column_name": "issued_quantity", "name": "issued_quantity"},
                {"column_name": "sub_material_id", "name": "sub_material_id"},
                {
                    "column_name": "store_usage_quantity_master_id",
                    "name": "store_usage_quantity_master_id",
                },
            ]
        },
        "sub_material": {
            "columns": [
                {"column_name": "sub_material_id", "name": "sub_material_id"},
                {"column_name": "sub_material_name", "name": "sub_material_name"},
            ]
        },
        "store_usage_quantity_master": {
            "columns": [
                {
                    "column_name": "store_usage_quantity_master_id",
                    "name": "store_usage_quantity_master_id",
                },
                {"column_name": "project_id", "name": "project_id"},
            ]
        },
    }
    relationships = [
        {
            "left_table": "store_material_usage_quantity",
            "left_column": "sub_material_id",
            "right_table": "sub_material",
            "right_column": "sub_material_id",
        },
        {
            "left_table": "store_material_usage_quantity",
            "left_column": "store_usage_quantity_master_id",
            "right_table": "store_usage_quantity_master",
            "right_column": "store_usage_quantity_master_id",
        },
        {
            "left_table": "store_usage_quantity_master",
            "left_column": "project_id",
            "right_table": "project_mst",
            "right_column": "project_id",
        },
    ]
    plan = QueryPlan(
        original_question="Show the top materials issued for RDSS-VIZAG.",
        operation="ranking",
        intent_category="data_query",
        metrics=["material_consumption"],
        aggregation=["sum"],
        dimensions=["material"],
        ordering=[{"target_concept": "material_consumption", "direction": "desc"}],
        limit=10,
        selected_tables=[
            "store_material_usage_quantity",
            "sub_material",
            "store_usage_quantity_master",
            "project_mst",
        ],
        selected_columns={
            "store_material_usage_quantity": [
                "issued_quantity",
                "sub_material_id",
                "store_usage_quantity_master_id",
            ],
            "sub_material": ["sub_material_id", "sub_material_name"],
            "store_usage_quantity_master": [
                "store_usage_quantity_master_id",
                "project_id",
            ],
            "project_mst": ["project_id", "project_name"],
        },
        generation_tables=[
            "store_material_usage_quantity",
            "sub_material",
            "store_usage_quantity_master",
            "project_mst",
        ],
        generation_columns={
            "store_material_usage_quantity": [
                "issued_quantity",
                "sub_material_id",
                "store_usage_quantity_master_id",
            ],
            "sub_material": ["sub_material_id", "sub_material_name"],
            "store_usage_quantity_master": [
                "store_usage_quantity_master_id",
                "project_id",
            ],
            "project_mst": ["project_id", "project_name"],
        },
        catalog_authoritative=True,
        bound_metrics=[{
            "metric": "material_consumption",
            "table": "store_material_usage_quantity",
            "column": "issued_quantity",
        }],
        bound_dimensions=[{
            "dimension": "material",
            "table": "sub_material",
            "column": "sub_material_name",
        }],
        resolved_entity_constraints=[{
            "original_text": "RDSS-VIZAG",
            "entity_type": "project",
            "table": "project_mst",
            "column": "project_name",
            "value": "RDSS-VIZAG",
            "primary_key_column": "project_id",
            "primary_key_value": 2,
        }],
        approved_relationships=relationships,
    )

    result = query_plan_guard.validate_sql(
        """
        SELECT
            sm.sub_material_name,
            SUM(d.issued_quantity) AS total_material_consumption
        FROM store_material_usage_quantity d
        JOIN sub_material sm ON d.sub_material_id = sm.sub_material_id
        JOIN store_usage_quantity_master m
          ON d.store_usage_quantity_master_id = m.store_usage_quantity_master_id
        JOIN project_mst p ON m.project_id = p.project_id
        WHERE p.project_id = 2
        GROUP BY sm.sub_material_name
        ORDER BY total_material_consumption DESC
        LIMIT 10
        """,
        plan,
        schema=schema,
    )

    assert result["valid"] is True


def test_accepts_inventory_current_stock_by_material():
    def column(name):
        return {"column_name": name, "name": name, "data_type": "varchar"}

    schema = {
        "inventory_master": {
            "columns": [
                column("inventory_master_id"),
                column("sub_store_id"),
                column("material_id"),
                column("sub_material_id"),
                column("available_quantity"),
                column("total_quanity"),
                column("status"),
            ]
        },
        "sub_material": {
            "columns": [
                column("sub_material_id"),
                column("sub_material_name"),
                column("uom"),
                column("submaterialcode"),
                column("item_code"),
            ]
        },
    }
    relationship = {
        "left_table": "inventory_master",
        "left_column": "sub_material_id",
        "right_table": "sub_material",
        "right_column": "sub_material_id",
        "relationship_type": "many_to_one",
    }
    plan = QueryPlan(
        original_question="Show top 5 materials by current stock.",
        operation="ranking",
        intent_category="data_query",
        metrics=["stock"],
        dimensions=["material"],
        aggregation=["sum"],
        grouping=["material"],
        ordering=[{"target_concept": "stock", "direction": "desc"}],
        limit=5,
        selected_tables=["inventory_master", "sub_material"],
        selected_columns={
            "inventory_master": ["sub_material_id", "available_quantity"],
            "sub_material": ["sub_material_id", "sub_material_name", "uom"],
        },
        generation_tables=["inventory_master", "sub_material"],
        generation_columns={
            "inventory_master": ["sub_material_id", "available_quantity"],
            "sub_material": ["sub_material_id", "sub_material_name", "uom"],
        },
        catalog_authoritative=True,
        bound_metrics=[{
            "metric": "current_stock",
            "table": "inventory_master",
            "column": "available_quantity",
            "default_aggregation": "sum",
            "applicable_module": "inventory",
        }],
        bound_dimensions=[{
            "dimension": "material",
            "table": "sub_material",
            "column": "sub_material_name",
            "primary_key_column": "sub_material_id",
            "applicable_module": "inventory",
        }],
        approved_relationships=[relationship],
    )

    result = query_plan_guard.validate_sql(
        """
        SELECT
            sm.sub_material_name,
            sm.uom,
            SUM(COALESCE(i.available_quantity, 0)) AS current_stock
        FROM inventory_master i
        JOIN sub_material sm ON i.sub_material_id = sm.sub_material_id
        GROUP BY sm.sub_material_id, sm.sub_material_name, sm.uom
        ORDER BY current_stock DESC, sm.sub_material_name ASC
        LIMIT 5
        """,
        plan,
        schema=schema,
    )

    assert result["valid"] is True, result["errors"]


def test_accepts_verified_store_quantity_grouped_by_material_without_literal_by():
    def column(name):
        return {"column_name": name, "name": name, "data_type": "varchar"}

    schema = {
        "store_material_usage_quantity": {
            "columns": [
                column("store_material_usage_quantity_id"),
                column("sub_material_id"),
                column("issued_quantity"),
            ]
        },
        "sub_material": {
            "columns": [
                column("sub_material_id"),
                column("sub_material_name"),
            ]
        },
    }
    plan = QueryPlan(
        original_question="show material issued quantity",
        operation="lookup",
        intent_category="data_query",
        metrics=["material_consumption"],
        aggregation=["sum"],
        selected_tables=["store_material_usage_quantity", "sub_material"],
        selected_columns={
            "store_material_usage_quantity": [
                "sub_material_id",
                "issued_quantity",
            ],
            "sub_material": ["sub_material_id", "sub_material_name"],
        },
        generation_tables=["store_material_usage_quantity", "sub_material"],
        generation_columns={
            "store_material_usage_quantity": [
                "sub_material_id",
                "issued_quantity",
            ],
            "sub_material": ["sub_material_id", "sub_material_name"],
        },
        catalog_authoritative=True,
        bound_metrics=[{
            "metric": "material_consumption",
            "table": "store_material_usage_quantity",
            "column": "issued_quantity",
            "default_aggregation": "sum",
        }],
        bound_dimensions=[{
            "dimension": "material",
            "table": "sub_material",
            "column": "sub_material_name",
            "primary_key_column": "sub_material_id",
        }],
        approved_relationships=[{
            "left_table": "store_material_usage_quantity",
            "left_column": "sub_material_id",
            "right_table": "sub_material",
            "right_column": "sub_material_id",
            "relationship_type": "many_to_one",
        }],
    )

    result = query_plan_guard.validate_sql(
        """
        SELECT sm.sub_material_name, SUM(d.issued_quantity) AS issued_quantity
        FROM store_material_usage_quantity d
        JOIN sub_material sm ON d.sub_material_id = sm.sub_material_id
        GROUP BY sm.sub_material_id, sm.sub_material_name
        ORDER BY issued_quantity DESC
        LIMIT 10
        """,
        plan,
        schema=schema,
    )

    assert result["valid"] is True, result["errors"]

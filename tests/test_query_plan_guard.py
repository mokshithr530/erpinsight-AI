from query_plan import query_plan_guard


def _plan(**overrides):
    semantic_plan = {
        "operation": "ranking",
        "intent_category": "data_query",
        "metrics": ["purchase_value"],
        "dimensions": ["project"],
        "aggregations": ["sum"],
        "sort_direction": "desc",
        "limit": 3,
        "date_constraints": [],
        "status_constraints": [],
        "question_contract": {"required_limit": 3},
    }
    semantic_plan.update(overrides.pop("semantic_plan", {}))
    entity_resolution = overrides.pop("entity_resolution", {"matches": [], "entities": [], "unresolved": []})
    retrieval = overrides.pop(
        "retrieval",
        {
            "selected_tables": ["project_mst", "purchase_order"],
            "relevant_columns": {
                "project_mst": ["project_id", "project_name"],
                "purchase_order": ["project_id", "final_price"],
            },
            "relationship_paths": [],
        },
    )
    plan = query_plan_guard.build(
        question="test question",
        semantic_plan=semantic_plan,
        entity_resolution=entity_resolution,
        retrieval=retrieval,
    )
    for key, value in overrides.items():
        setattr(plan, key, value)
    return plan


def test_query_plan_guard_accepts_matching_ranked_aggregate_sql():
    plan = _plan()

    result = query_plan_guard.validate_sql(
        """
        SELECT p.project_name, SUM(po.final_price) AS purchase_value
        FROM project_mst p
        JOIN purchase_order po ON po.project_id = p.project_id
        GROUP BY p.project_name
        ORDER BY purchase_value DESC
        LIMIT 3
        """,
        plan,
    )

    assert result["valid"] is True


def test_query_plan_guard_rejects_missing_limit_and_order():
    plan = _plan()

    result = query_plan_guard.validate_sql(
        """
        SELECT p.project_name, SUM(po.final_price) AS purchase_value
        FROM project_mst p
        JOIN purchase_order po ON po.project_id = p.project_id
        GROUP BY p.project_name
        """,
        plan,
    )

    assert result["valid"] is False
    assert "ORDER BY" in " ".join(result["errors"])
    assert "LIMIT 3" in " ".join(result["errors"])


def test_query_plan_guard_rejects_limit_smaller_than_explicit_request():
    plan = _plan()
    plan.original_question = "Show the top 3 projects by purchase value."

    result = query_plan_guard.validate_sql(
        """
        SELECT p.project_name, SUM(po.final_price) AS purchase_value
        FROM project_mst p
        JOIN purchase_order po ON po.project_id = p.project_id
        GROUP BY p.project_name
        ORDER BY purchase_value DESC
        LIMIT 2
        """,
        plan,
    )

    assert result["valid"] is False
    assert "does not match requested limit 3" in " ".join(result["errors"])


def test_scalar_total_does_not_request_implicit_entity_breakdown():
    plan = _plan(
        semantic_plan={
            "operation": "sum",
            "metrics": ["project_value"],
            "dimensions": ["project"],
            "requested_output": ["project"],
            "aggregations": ["sum"],
            "grouping_hints": [],
            "sort_direction": None,
            "limit": None,
            "question_contract": {
                "required_outputs": ["project"],
                "required_dimensions": ["project"],
                "required_metrics": ["project_value"],
            },
        },
        retrieval={
            "selected_tables": ["project_mst"],
            "relevant_columns": {
                "project_mst": ["project_name", "project_value"],
            },
            "relationship_paths": [],
        },
    )

    assert plan.base_entity == "project"
    assert plan.dimensions == []
    assert plan.requested_outputs == []
    assert plan.grouping == []


def test_count_does_not_inherit_metric_default_sum():
    plan = query_plan_guard.build(
        question="How many material issue lines are available?",
        semantic_plan={
            "operation": "count",
            "intent_category": "data_query",
            "metrics": ["material_issues"],
            "dimensions": ["material"],
            "requested_output": ["material"],
            "aggregations": ["count"],
            "grouping_hints": [],
            "sort_direction": None,
            "limit": None,
        },
        entity_resolution={
            "matches": [],
            "entities": [],
            "unresolved": [],
        },
        retrieval={
            "selected_tables": ["material_issue_line"],
            "relevant_columns": {
                "material_issue_line": ["issue_line_id", "quantity"],
            },
            "relationship_paths": [],
        },
        semantic_binding={
            "bound_metrics": [
                {
                    "metric": "material_consumption",
                    "requested_concept": "material_issues",
                    "default_aggregation": "sum",
                    "table": "material_issue_line",
                    "column": "quantity",
                }
            ],
            "bound_dimensions": [
                {
                    "dimension": "material",
                    "requested_concept": "material",
                    "table": "material_issue_line",
                    "column": "material_id",
                }
            ],
        },
    )

    assert plan.aggregation == ["count"]
    assert plan.dimensions == []
    assert plan.requested_outputs == []


def test_verified_vendor_filters_keep_canonical_entity_tables():
    plan = query_plan_guard.build(
        question="How many vendors have branches in Telangana?",
        semantic_plan={
            "operation": "count",
            "intent_category": "data_query",
            "metrics": [],
            "dimensions": ["vendor"],
            "requested_output": ["vendor"],
            "aggregations": ["count"],
            "filters": [{
                "field_concept": "location",
                "operator": "equals",
                "raw_value": "Telangana",
                "essential": True,
            }],
            "grouping_hints": [],
            "sort_direction": None,
            "limit": None,
        },
        entity_resolution={
            "entities": [{
                "original_text": "Telangana",
                "entity_type": "location",
                "table": "vendor_branch_details",
                "column": "state",
                "value": "Telangana",
                "primary_key_column": "vendor_branch_details_id",
                "primary_key_value": 23,
            }],
            "matches": [],
            "unresolved": [],
        },
        retrieval={
            "selected_tables": ["vendor_mst"],
            "relevant_columns": {"vendor_mst": ["vendor_id", "vendor_name"]},
            "relationship_paths": [],
        },
        semantic_binding={
            "authoritative": True,
            "bound_metrics": [],
            "bound_dimensions": [{
                "dimension": "vendor",
                "table": "vendor_mst",
                "column": "vendor_name",
                "applicable_module": "vendors",
            }],
            "required_tables": ["vendor_mst", "vendor_branch_details"],
            "required_columns": {
                "vendor_mst": ["vendor_id"],
                "vendor_branch_details": ["vendor_id", "state"],
            },
            "approved_relationships": [{
                "left_table": "vendor_branch_details",
                "left_column": "vendor_id",
                "right_table": "vendor_mst",
                "right_column": "vendor_id",
            }],
        },
    )

    assert "vendor_branch_details" in plan.selected_tables
    assert "vendor_branch_details" in plan.generation_tables
    assert plan.resolved_entity_constraints == [{
        "original_text": "Telangana",
        "entity_type": "location",
        "table": "vendor_branch_details",
        "column": "state",
        "value": "Telangana",
        "primary_key_column": "vendor_branch_details_id",
        "primary_key_value": 23,
        "confidence": None,
    }]


def test_explicit_average_overrides_verified_metric_default_sum():
    plan = query_plan_guard.build(
        question="What is the average workorder value?",
        semantic_plan={
            "operation": "average",
            "intent_category": "data_query",
            "metrics": ["workorder_value"],
            "dimensions": ["subcontract_workorder"],
            "requested_output": ["subcontract_workorder"],
            "aggregations": ["average"],
            "grouping_hints": [],
            "sort_direction": None,
            "limit": None,
        },
        entity_resolution={
            "matches": [],
            "entities": [],
            "unresolved": [],
        },
        retrieval={
            "selected_tables": ["subcontract_workorder_details"],
            "relevant_columns": {
                "subcontract_workorder_details": [
                    "after_taxes_grandtotal",
                ],
            },
            "relationship_paths": [],
        },
        semantic_binding={
            "bound_metrics": [
                {
                    "metric": "workorder_value",
                    "requested_concept": "workorder_value",
                    "default_aggregation": "sum",
                    "table": "subcontract_workorder_details",
                    "column": "after_taxes_grandtotal",
                }
            ],
        },
    )

    assert plan.aggregation == ["avg"]


def test_query_plan_guard_allows_top_n_row_level_metric_without_sum():
    plan = _plan(semantic_plan={"aggregations": []})

    result = query_plan_guard.validate_sql(
        """
        SELECT p.project_name, p.project_value
        FROM project_mst p
        WHERE p.project_value IS NOT NULL
        ORDER BY p.project_value DESC
        LIMIT 3
        """,
        plan,
    )

    assert result["valid"] is True


def test_query_plan_guard_rejects_missing_entity_filter():
    plan = _plan(
        entity_resolution={
            "matches": [
                {
                    "mention": "RDSS-VIZAG",
                    "entity": "RDSS-VIZAG",
                    "entity_type": "project",
                    "table": "project_mst",
                    "column": "project_name",
                    "confidence": 95,
                }
            ],
            "entities": [],
            "unresolved": [],
        },
        semantic_plan={"operation": "lookup", "aggregations": [], "sort_direction": None, "limit": None},
        limit=None,
    )

    result = query_plan_guard.validate_sql(
        "SELECT p.project_name FROM project_mst p LIMIT 10",
        plan,
    )

    assert result["valid"] is False
    assert "resolved entity constraints" in " ".join(result["errors"])


def test_query_plan_guard_rejects_missing_date_and_status_filters():
    plan = _plan(
        semantic_plan={
            "operation": "count",
            "aggregations": ["count"],
            "sort_direction": None,
            "limit": None,
            "date_constraints": [{"type": "year", "value": 2025}],
            "status_constraints": [{"raw_value": "pending", "normalized_value": "pending"}],
        }
    )

    result = query_plan_guard.validate_sql(
        "SELECT COUNT(*) AS total_rows FROM purchase_order",
        plan,
    )

    assert result["valid"] is False
    assert "date constraint" in " ".join(result["errors"])
    assert "status constraint" in " ".join(result["errors"])


def test_query_plan_guard_rejects_grouped_metric_without_group_by():
    plan = _plan()

    result = query_plan_guard.validate_sql(
        "SELECT p.project_name, SUM(po.final_price) FROM project_mst p JOIN purchase_order po ON po.project_id = p.project_id ORDER BY 2 DESC LIMIT 3",
        plan,
    )

    assert result["valid"] is False
    assert "GROUP BY" in " ".join(result["errors"])


def test_query_plan_guard_rejects_disconnected_join():
    plan = _plan()

    result = query_plan_guard.validate_sql(
        "SELECT p.project_name, SUM(po.final_price) FROM project_mst p JOIN purchase_order po GROUP BY p.project_name ORDER BY 2 DESC LIMIT 3",
        plan,
    )

    assert result["valid"] is False
    assert "JOIN without" in " ".join(result["errors"])


def test_query_plan_guard_allows_explicit_cross_join():
    plan = _plan()

    result = query_plan_guard.validate_sql(
        """
        WITH max_date AS (SELECT MAX(issue_date) AS max_issue_date FROM usage_master)
        SELECT p.project_name, SUM(q.issued_quantity) AS issued_quantity
        FROM project_mst p
        JOIN usage_master m ON m.project_id = p.project_id
        JOIN usage_detail q ON q.usage_id = m.usage_id
        CROSS JOIN max_date x
        GROUP BY p.project_name
        ORDER BY issued_quantity DESC
        LIMIT 3
        """,
        plan,
    )

    assert "JOIN without" not in " ".join(result["errors"])


def test_query_plan_guard_warns_for_tables_outside_retrieved_context():
    plan = _plan(retrieval={"selected_tables": ["project_mst"], "relevant_columns": {}, "relationship_paths": []})

    result = query_plan_guard.validate_sql(
        "SELECT p.project_name FROM project_mst p JOIN purchase_order po ON po.project_id = p.project_id LIMIT 3",
        plan,
    )

    assert "outside retrieved context" in " ".join(result["warnings"])


def test_query_plan_guard_accepts_where_with_newlines_for_entity_filter():
    plan = _plan(
        semantic_plan={
            "operation": "lookup",
            "metrics": [],
            "dimensions": ["project"],
            "aggregations": [],
            "sort_direction": None,
            "limit": None,
            "candidate_entity_mentions": ["RDSS-VIZAG"],
        },
        entity_resolution={
            "matches": [{
                "entity": "RDSS-VIZAG",
                "entity_type": "project",
                "table": "project_mst",
                "column": "project_name",
                "value": "RDSS-VIZAG",
                "primary_key": 2,
                "primary_key_value": 2,
                "confidence": 99,
            }],
            "entities": [],
            "unresolved": [],
        },
    )
    sql = """
SELECT project_id, project_name
FROM project_mst
WHERE project_id IN (2)
LIMIT 1
"""

    result = query_plan_guard.validate_sql(sql, plan)

    assert result["valid"]


def test_query_plan_guard_allows_count_for_most_employee_ranking():
    plan = _plan(
        semantic_plan={
            "operation": "ranking",
            "dimensions": ["department", "employee"],
            "requested_output": ["department", "employee"],
            "aggregations": ["sum"],
            "sort_direction": "desc",
            "limit": 1,
        },
        original_question="Which departments have the most employees?",
    )
    sql = """
SELECT department_name, COUNT(employee_id) AS employee_count
FROM department
GROUP BY department_name
ORDER BY employee_count DESC
LIMIT 10
"""

    result = query_plan_guard.validate_sql(sql, plan)

    assert result["valid"]

from query_plan import QueryPlan
from verified_sql_compiler import verified_sql_compiler


def _plan(**overrides):
    values = {
        "original_question": "What is the average workorder value?",
        "operation": "average",
        "intent_category": "data_query",
        "metrics": ["workorder_value"],
        "aggregation": ["average"],
        "selected_tables": ["subcontract_workorder_details"],
        "selected_columns": {
            "subcontract_workorder_details": [
                "after_taxes_grandtotal",
            ],
        },
        "generation_tables": ["subcontract_workorder_details"],
        "generation_columns": {
            "subcontract_workorder_details": [
                "after_taxes_grandtotal",
            ],
        },
        "catalog_authoritative": True,
        "bound_metrics": [
            {
                "metric": "workorder_value",
                "table": "subcontract_workorder_details",
                "column": "after_taxes_grandtotal",
            }
        ],
    }
    values.update(overrides)
    return QueryPlan(**values)


def test_compiles_verified_single_table_average():
    sql = verified_sql_compiler.compile(_plan())

    assert sql == (
        "SELECT AVG(`after_taxes_grandtotal`) AS "
        "`average_workorder_value` "
        "FROM `subcontract_workorder_details`"
    )


def test_compiles_verified_single_table_count_without_metric():
    sql = verified_sql_compiler.compile(
        _plan(
            operation="count",
            metrics=[],
            aggregation=["count"],
            bound_metrics=[],
        )
    )

    assert sql == (
        "SELECT COUNT(*) AS `row_count` "
        "FROM `subcontract_workorder_details`"
    )


def test_refuses_filtered_or_joined_plan():
    assert verified_sql_compiler.compile(
        _plan(date_constraints=[{"type": "last_days", "value": 30}])
    ) is None


def test_refuses_unverified_or_unresolved_plan():
    assert verified_sql_compiler.compile(
        _plan(catalog_authoritative=False)
    ) is None
    assert verified_sql_compiler.compile(
        _plan(unresolved_concepts=["workorder value"])
    ) is None


def test_compiles_verified_min_max_without_ranking_limit():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="What are the minimum and maximum project value?",
            operation="aggregation",
            metrics=["project_value"],
            aggregation=["min", "max"],
            selected_tables=["project_mst"],
            selected_columns={"project_mst": ["project_value"]},
            generation_tables=["project_mst"],
            generation_columns={"project_mst": ["project_value"]},
            bound_metrics=[{
                "metric": "project_value",
                "table": "project_mst",
                "column": "project_value",
            }],
        )
    )

    assert sql == (
        "SELECT MIN(CAST(NULLIF(REPLACE(TRIM(p.`project_value`), ',', ''), '') AS DECIMAL(20,2))) AS `min_project_value`, "
        "MAX(CAST(NULLIF(REPLACE(TRIM(p.`project_value`), ',', ''), '') AS DECIMAL(20,2))) AS `max_project_value`\n"
        "FROM `project_mst` p"
    )


def test_compiles_verified_project_top_value_with_numeric_cast():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Show the top 5 projects by project value.",
            operation="ranking",
            metrics=["project_value"],
            dimensions=["project"],
            aggregation=["sum"],
            limit=5,
            selected_tables=["project_mst"],
            selected_columns={"project_mst": ["project_id", "project_name", "client_name", "project_value"]},
            generation_tables=["project_mst"],
            generation_columns={"project_mst": ["project_id", "project_name", "client_name", "project_value"]},
            bound_metrics=[{
                "metric": "project_value",
                "table": "project_mst",
                "column": "project_value",
                "applicable_module": "projects",
            }],
            bound_dimensions=[{
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
                "applicable_module": "projects",
            }],
        )
    )

    assert "FROM `project_mst` p" in sql
    assert "CAST(NULLIF(REPLACE(TRIM(p.`project_value`)" in sql
    assert "ORDER BY `project_value` DESC" in sql
    assert "LIMIT 5" in sql


def test_compiles_basic_project_list_with_plain_limit():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="show 5 projects",
            operation="lookup",
            metrics=[],
            dimensions=["project"],
            requested_outputs=["project"],
            base_entity="project",
            aggregation=[],
            limit=5,
            selected_tables=["project_mst"],
            selected_columns={
                "project_mst": [
                    "project_id",
                    "project_name",
                    "client_name",
                    "project_value",
                ],
            },
            generation_tables=["project_mst"],
            generation_columns={
                "project_mst": [
                    "project_id",
                    "project_name",
                    "client_name",
                    "project_value",
                ],
            },
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
                "applicable_module": "projects",
            }],
        )
    )

    assert "FROM `project_mst` p" in sql
    assert "p.`project_name` AS `project_name`" in sql
    assert "LIMIT 5" in sql


def test_compiles_latest_billing_rows_after_limit_clarification():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Show the latest bills limit 5.",
            operation="lookup",
            metrics=[],
            aggregation=[],
            dimensions=["invoice"],
            requested_outputs=["invoice"],
            limit=5,
            selected_tables=["client_bill_raising"],
            selected_columns={
                "client_bill_raising": [
                    "billing_id",
                    "ra_bill_number",
                    "bill_date",
                    "gross_bill_value",
                    "bill_amount",
                    "billing_status",
                ],
            },
            generation_tables=["client_bill_raising"],
            generation_columns={
                "client_bill_raising": [
                    "billing_id",
                    "ra_bill_number",
                    "bill_date",
                    "gross_bill_value",
                    "bill_amount",
                    "billing_status",
                ],
            },
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "invoice",
                "table": "client_bill_raising",
                "column": "billing_id",
                "applicable_module": "billing",
            }],
        )
    )

    assert "FROM `client_bill_raising` b" in sql
    assert "ORDER BY" in sql
    assert "bill_date" in sql
    assert "LIMIT 5" in sql


def test_compiles_latest_purchase_orders_by_date_not_value():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="show latest 3 purchase orders",
            operation="lookup",
            metrics=["purchase_value"],
            dimensions=["purchase_order"],
            requested_outputs=["purchase_order"],
            aggregation=[],
            limit=3,
            selected_tables=["po_form"],
            selected_columns={
                "po_form": [
                    "po_form_id",
                    "po_no",
                    "vendor_name",
                    "total_value",
                    "date",
                ],
            },
            generation_tables=["po_form"],
            generation_columns={
                "po_form": [
                    "po_form_id",
                    "po_no",
                    "vendor_name",
                    "total_value",
                    "date",
                ],
            },
            bound_metrics=[{
                "metric": "purchase_value",
                "table": "po_form",
                "column": "total_value",
                "applicable_module": "purchase_orders",
            }],
            bound_dimensions=[{
                "dimension": "purchase_order",
                "table": "po_form",
                "column": "po_form_id",
                "applicable_module": "purchase_orders",
            }],
        )
    )

    assert "ORDER BY COALESCE(STR_TO_DATE" in sql
    assert "f.`po_form_id` DESC" in sql
    assert "LIMIT 3" in sql


def test_refuses_broad_material_movement_without_metric():
    assert verified_sql_compiler.compile(
        _plan(
            original_question="show material movement",
            operation="lookup",
            metrics=[],
            dimensions=["material"],
            requested_outputs=["material"],
            base_entity="material",
            aggregation=[],
            selected_tables=["sub_material"],
            selected_columns={"sub_material": ["sub_material_name"]},
            generation_tables=["sub_material"],
            generation_columns={"sub_material": ["sub_material_name"]},
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "material",
                "table": "sub_material",
                "column": "sub_material_name",
                "applicable_module": "stores",
            }],
        )
    ) is None


def test_compiles_verified_project_lookup_by_resolved_project():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Give me details for RDSS-VIZAG project.",
            operation="lookup",
            metrics=[],
            aggregation=[],
            selected_tables=["project_mst"],
            selected_columns={"project_mst": ["project_id", "project_name", "client_name", "project_value", "project_state"]},
            generation_tables=["project_mst"],
            generation_columns={"project_mst": ["project_id", "project_name", "client_name", "project_value", "project_state"]},
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
                "applicable_module": "projects",
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
        )
    )

    assert "FROM `project_mst` p" in sql
    assert "WHERE p.`project_id` = 2" in sql
    assert "p.`project_location` AS `project_location`" in sql


def test_compiles_verified_grouped_ranking_with_project_filter():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Show the top materials issued for RDSS-VIZAG.",
            operation="ranking",
            metrics=["material_consumption"],
            aggregation=["sum"],
            dimensions=["material"],
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
            approved_relationships=[
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
            ],
        )
    )

    assert "SUM(d.`issued_quantity`) AS `issued_quantity`" in sql
    assert "JOIN `project_mst`" in sql
    assert "WHERE p.`project_id` = 2" in sql
    assert "sm.`sub_material_id`, sm.`sub_material_name`" in sql
    assert "LIMIT 10" in sql


def test_compiles_verified_store_issue_value_with_verified_formula():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="For any project in the last 30 days, show materials issued with pricing.",
            operation="ranking",
            metrics=["material_issue_value"],
            aggregation=["sum"],
            dimensions=["project", "material"],
            limit=10,
            date_constraints=[{"type": "last_days", "value": 30}],
            selected_tables=["store_material_usage_quantity", "store_usage_quantity_master", "project_mst", "sub_material"],
            selected_columns={
                "store_material_usage_quantity": ["issued_quantity", "base_charge", "sub_material_id", "store_usage_quantity_master_id"],
                "store_usage_quantity_master": ["store_usage_quantity_master_id", "project_id", "issued_date"],
                "project_mst": ["project_id", "project_name"],
                "sub_material": ["sub_material_id", "sub_material_name"],
            },
            generation_tables=["store_material_usage_quantity", "store_usage_quantity_master", "project_mst", "sub_material"],
            generation_columns={
                "store_material_usage_quantity": ["issued_quantity", "base_charge", "sub_material_id", "store_usage_quantity_master_id"],
                "store_usage_quantity_master": ["store_usage_quantity_master_id", "project_id", "issued_date"],
                "project_mst": ["project_id", "project_name"],
                "sub_material": ["sub_material_id", "sub_material_name"],
            },
            bound_metrics=[{
                "metric": "material_issue_value",
                "table": "store_material_usage_quantity",
                "column": "base_charge",
                "applicable_module": "stores",
            }],
            bound_dimensions=[
                {"dimension": "project", "table": "project_mst", "column": "project_name", "applicable_module": "projects"},
                {"dimension": "material", "table": "sub_material", "column": "sub_material_name", "applicable_module": "stores"},
            ],
            bound_date_fields=[{
                "semantic": "material_issue_date",
                "table": "store_usage_quantity_master",
                "column": "issued_date",
                "applicable_module": "stores",
            }],
            approved_relationships=[
                {"left_table": "store_material_usage_quantity", "left_column": "store_usage_quantity_master_id", "right_table": "store_usage_quantity_master", "right_column": "store_usage_quantity_master_id"},
                {"left_table": "store_usage_quantity_master", "left_column": "project_id", "right_table": "project_mst", "right_column": "project_id"},
                {"left_table": "store_material_usage_quantity", "left_column": "sub_material_id", "right_table": "sub_material", "right_column": "sub_material_id"},
            ],
        )
    )

    assert "SUM(CAST(NULLIF(REPLACE(TRIM(d.`issued_quantity`), ',', ''), '') AS DECIMAL(20,4)) * CAST(NULLIF(REPLACE(TRIM(d.`base_charge`), ',', ''), '') AS DECIMAL(20,4))) AS `issued_value`" in sql
    assert "DATE_SUB((" in sql
    assert "JOIN `project_mst` p" in sql
    assert "JOIN `sub_material` sm" in sql


def test_compiles_verified_po_value_by_vendor():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Which vendor has the highest purchase value?",
            operation="ranking",
            metrics=["purchase_value"],
            dimensions=["vendor"],
            aggregation=["sum"],
            limit=1,
            selected_tables=["po_form"],
            selected_columns={"po_form": ["vendor_name", "total_value"]},
            generation_tables=["po_form"],
            generation_columns={"po_form": ["vendor_name", "total_value"]},
            bound_metrics=[{
                "metric": "purchase_value",
                "table": "po_form",
                "column": "total_value",
                "applicable_module": "purchase_orders",
            }],
            bound_dimensions=[{
                "dimension": "vendor",
                "table": "po_form",
                "column": "vendor_name",
                "applicable_module": "purchase_orders",
            }],
        )
    )

    assert "FROM `po_form` f" in sql
    assert "SUM(f.`total_value`) AS `purchase_value`" in sql
    assert "GROUP BY TRIM(f.`vendor_name`)" in sql
    assert "LIMIT 1" in sql


def test_compiles_verified_vendor_master_count_and_lookup():
    count_sql = verified_sql_compiler.compile(
        _plan(
            original_question="How many vendors are there?",
            operation="count",
            metrics=[],
            dimensions=["vendor"],
            aggregation=["count"],
            selected_tables=["vendor_mst"],
            selected_columns={"vendor_mst": ["vendor_id", "vendor_name"]},
            generation_tables=["vendor_mst"],
            generation_columns={"vendor_mst": ["vendor_id", "vendor_name"]},
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "vendor",
                "table": "vendor_mst",
                "column": "vendor_name",
                "applicable_module": "vendors",
            }],
        )
    )
    details_sql = verified_sql_compiler.compile(
        _plan(
            original_question="Show vendor details for KEI Industries.",
            operation="relationship_lookup",
            metrics=[],
            dimensions=["vendor"],
            aggregation=[],
            selected_tables=["vendor_mst"],
            selected_columns={
                "vendor_mst": ["vendor_id", "vendor_code", "vendor_name", "vendor_address", "contact_person_name", "contact_no", "email", "vendor_gst_no", "vendor_types_id"],
            },
            generation_tables=["vendor_mst"],
            generation_columns={
                "vendor_mst": ["vendor_id", "vendor_code", "vendor_name", "vendor_address", "contact_person_name", "contact_no", "email", "vendor_gst_no", "vendor_types_id"],
            },
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "vendor",
                "table": "vendor_mst",
                "column": "vendor_name",
                "applicable_module": "vendors",
            }],
            resolved_entity_constraints=[{
                "original_text": "KEI Industries",
                "entity_type": "vendor",
                "table": "vendor_mst",
                "column": "vendor_name",
                "value": "KEI Industries",
                "primary_key_column": "vendor_id",
                "primary_key_value": 12,
            }],
        )
    )

    assert count_sql == "SELECT COUNT(*) AS `vendor_count` FROM `vendor_mst`"
    assert "FROM `vendor_mst` m" in details_sql
    assert "m.`vendor_id` = 12" in details_sql


def test_compiles_verified_vendor_groupings_and_po_count():
    state_sql = verified_sql_compiler.compile(
        _plan(
            original_question="Show vendors by state.",
            operation="distribution",
            metrics=[],
            dimensions=["vendor", "state"],
            aggregation=["count"],
            selected_tables=["vendor_mst", "vendor_branch_details"],
            selected_columns={
                "vendor_mst": ["vendor_id"],
                "vendor_branch_details": ["vendor_id", "state"],
            },
            generation_tables=["vendor_mst", "vendor_branch_details"],
            generation_columns={
                "vendor_mst": ["vendor_id"],
                "vendor_branch_details": ["vendor_id", "state"],
            },
            bound_metrics=[],
            bound_dimensions=[
                {"dimension": "vendor", "table": "vendor_mst", "column": "vendor_name", "applicable_module": "vendors"},
                {"dimension": "state", "table": "vendor_branch_details", "column": "state", "applicable_module": "vendors"},
            ],
            approved_relationships=[{
                "left_table": "vendor_branch_details",
                "left_column": "vendor_id",
                "right_table": "vendor_mst",
                "right_column": "vendor_id",
            }],
        )
    )
    po_count_sql = verified_sql_compiler.compile(
        _plan(
            original_question="Which vendor has the most purchase orders?",
            operation="ranking",
            metrics=[],
            dimensions=["vendor", "purchase_order"],
            aggregation=["count"],
            limit=1,
            selected_tables=["po_form"],
            selected_columns={"po_form": ["po_form_id", "vendor_name"]},
            generation_tables=["po_form"],
            generation_columns={"po_form": ["po_form_id", "vendor_name"]},
            bound_metrics=[],
            bound_dimensions=[
                {"dimension": "vendor", "table": "po_form", "column": "vendor_name", "applicable_module": "purchase_orders"},
                {"dimension": "purchase_order", "table": "po_form", "column": "po_form_id", "applicable_module": "purchase_orders"},
            ],
        )
    )

    assert "COUNT(DISTINCT m.`vendor_id`) AS `vendor_count`" in state_sql
    assert "JOIN `vendor_branch_details` b ON b.`vendor_id` = m.`vendor_id`" in state_sql
    assert "COUNT(f.`po_form_id`) AS `purchase_order_count`" in po_count_sql
    assert "LIMIT 1" in po_count_sql


def test_compiles_verified_vendor_filtered_state_and_category_counts():
    state_sql = verified_sql_compiler.compile(
        _plan(
            original_question="How many vendors have branches in Telangana?",
            operation="count",
            metrics=[],
            dimensions=["vendor"],
            aggregation=["count"],
            selected_tables=["vendor_mst", "vendor_branch_details"],
            selected_columns={
                "vendor_mst": ["vendor_id"],
                "vendor_branch_details": ["vendor_id", "state"],
            },
            generation_tables=["vendor_mst", "vendor_branch_details"],
            generation_columns={
                "vendor_mst": ["vendor_id"],
                "vendor_branch_details": ["vendor_id", "state"],
            },
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "vendor",
                "table": "vendor_mst",
                "column": "vendor_name",
                "applicable_module": "vendors",
            }],
            resolved_entity_constraints=[{
                "original_text": "Telangana",
                "entity_type": "location",
                "table": "vendor_branch_details",
                "column": "state",
                "value": "Telangana",
                "primary_key_column": "vendor_branch_details_id",
                "primary_key_value": 23,
            }],
        )
    )
    category_sql = verified_sql_compiler.compile(
        _plan(
            original_question="How many MSME vendors are there?",
            operation="count",
            metrics=[],
            dimensions=["vendor"],
            aggregation=["count"],
            selected_tables=["vendor_mst", "vendor_types"],
            selected_columns={
                "vendor_mst": ["vendor_id", "vendor_types_id"],
                "vendor_types": ["vendor_types_id", "vendor_type"],
            },
            generation_tables=["vendor_mst", "vendor_types"],
            generation_columns={
                "vendor_mst": ["vendor_id", "vendor_types_id"],
                "vendor_types": ["vendor_types_id", "vendor_type"],
            },
            bound_metrics=[],
            bound_dimensions=[{
                "dimension": "vendor",
                "table": "vendor_mst",
                "column": "vendor_name",
                "applicable_module": "vendors",
            }],
            resolved_entity_constraints=[{
                "original_text": "MSME",
                "entity_type": "vendor_category",
                "table": "vendor_types",
                "column": "vendor_type",
                "value": "MSME",
                "primary_key_column": "vendor_types_id",
                "primary_key_value": 1,
            }],
        )
    )

    assert "JOIN `vendor_branch_details` b ON b.`vendor_id` = m.`vendor_id`" in state_sql
    assert "TRIM(b.`state`) = 'Telangana'" in state_sql
    assert "`country`" not in state_sql
    assert "JOIN `vendor_types` t ON t.`vendor_types_id` = m.`vendor_types_id`" in category_sql
    assert "TRIM(t.`vendor_type`) = 'MSME'" in category_sql


def test_compiles_verified_po_project_lookup_with_distinct_bridge():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Show purchase orders for RDSS-VIZAG.",
            operation="relationship_lookup",
            metrics=[],
            dimensions=["purchase_order"],
            aggregation=[],
            selected_tables=["po_form", "po_items", "project_mst"],
            selected_columns={
                "po_form": ["po_form_id", "po_id", "po_no", "vendor_name", "total_value"],
                "po_items": ["po_id", "po_project_id"],
                "project_mst": ["project_id", "project_name"],
            },
            generation_tables=["po_form", "po_items", "project_mst"],
            generation_columns={
                "po_form": ["po_form_id", "po_id", "po_no", "vendor_name", "total_value"],
                "po_items": ["po_id", "po_project_id"],
                "project_mst": ["project_id", "project_name"],
            },
            bound_dimensions=[{
                "dimension": "purchase_order",
                "table": "po_form",
                "column": "po_form_id",
                "applicable_module": "purchase_orders",
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
        )
    )

    assert "SELECT DISTINCT `po_id`, `po_project_id` FROM `po_items`" in sql
    assert "JOIN `po_form` f ON f.`po_id` = x.`po_id`" in sql
    assert "WHERE p.`project_id` = 2" in sql
    assert "f.`po_form_id` AS `po_form_id`" in sql


def test_compiles_verified_inventory_current_stock_by_material():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Show top 5 materials by current stock.",
            operation="ranking",
            metrics=["stock"],
            dimensions=["material"],
            aggregation=["sum"],
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
            bound_metrics=[{
                "metric": "current_stock",
                "table": "inventory_master",
                "column": "available_quantity",
                "applicable_module": "inventory",
            }],
            bound_dimensions=[{
                "dimension": "material",
                "table": "sub_material",
                "column": "sub_material_name",
                "applicable_module": "inventory",
            }],
        )
    )

    assert "FROM `inventory_master` i" in sql
    assert "JOIN `sub_material` sm ON i.`sub_material_id` = sm.`sub_material_id`" in sql
    assert "SUM(COALESCE(i.`available_quantity`, 0)) AS `current_stock`" in sql
    assert "GROUP BY sm.`sub_material_id`, sm.`sub_material_name`, sm.`uom`" in sql
    assert "LIMIT 5" in sql


def test_compiles_verified_inventory_zero_stock_count():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="How many materials are out of stock?",
            operation="count",
            metrics=["stock"],
            dimensions=["material"],
            aggregation=["count"],
            selected_tables=["inventory_master", "sub_material"],
            selected_columns={
                "inventory_master": ["sub_material_id", "available_quantity"],
                "sub_material": ["sub_material_id", "sub_material_name"],
            },
            generation_tables=["inventory_master", "sub_material"],
            generation_columns={
                "inventory_master": ["sub_material_id", "available_quantity"],
                "sub_material": ["sub_material_id", "sub_material_name"],
            },
            bound_metrics=[{
                "metric": "current_stock",
                "table": "inventory_master",
                "column": "available_quantity",
                "applicable_module": "inventory",
            }],
            bound_dimensions=[{
                "dimension": "material",
                "table": "sub_material",
                "column": "sub_material_name",
                "applicable_module": "inventory",
            }],
        )
    )

    assert "SELECT COUNT(*) AS `material_count`" in sql
    assert "GROUP BY i.`sub_material_id`" in sql
    assert "HAVING `current_stock` = 0" in sql


def test_compiles_verified_inventory_scalar_available_quantity():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="What is the total available inventory quantity?",
            operation="sum",
            metrics=["stock"],
            dimensions=[],
            aggregation=["sum"],
            selected_tables=["inventory_master"],
            selected_columns={
                "inventory_master": ["available_quantity"],
            },
            generation_tables=["inventory_master"],
            generation_columns={
                "inventory_master": ["available_quantity"],
            },
            bound_metrics=[{
                "metric": "current_stock",
                "table": "inventory_master",
                "column": "available_quantity",
                "applicable_module": "inventory",
            }],
        )
    )

    assert sql == (
        "SELECT SUM(COALESCE(i.`available_quantity`, 0)) AS `current_stock`\n"
        "FROM `inventory_master` i"
    )


def test_compiles_verified_inventory_scalar_stock_on_hand():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="What is the stock on hand?",
            operation="aggregation",
            metrics=["stock"],
            dimensions=[],
            aggregation=["sum"],
            selected_tables=["inventory_master"],
            selected_columns={
                "inventory_master": ["available_quantity"],
            },
            generation_tables=["inventory_master"],
            generation_columns={
                "inventory_master": ["available_quantity"],
            },
            bound_metrics=[{
                "metric": "current_stock",
                "table": "inventory_master",
                "column": "available_quantity",
                "applicable_module": "inventory",
            }],
        )
    )

    assert sql == (
        "SELECT SUM(COALESCE(i.`available_quantity`, 0)) AS `current_stock`\n"
        "FROM `inventory_master` i"
    )


def test_compiles_material_master_count_and_list():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="how many materials are there and tell me those materials",
            operation="count",
            metrics=[],
            dimensions=[],
            requested_outputs=["material"],
            base_entity="material",
            aggregation=["count"],
            selected_tables=["sub_material"],
            selected_columns={
                "sub_material": ["sub_material_name"],
            },
            generation_tables=["sub_material"],
            generation_columns={
                "sub_material": ["sub_material_name"],
            },
            bound_dimensions=[{
                "dimension": "material",
                "table": "sub_material",
                "column": "sub_material_name",
                "applicable_module": "stores",
            }],
        )
    )

    assert "COUNT(*) OVER () AS `material_count`" in sql
    assert "FROM `sub_material` sm" in sql
    assert "sm.`sub_material_name` AS `sub_material_name`" in sql
    assert "LIMIT 50" in sql


def test_compiles_material_master_name_lookup():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="name any 10 material",
            operation="lookup",
            metrics=[],
            dimensions=["material"],
            requested_outputs=["material"],
            base_entity="material",
            aggregation=[],
            selected_tables=["sub_material"],
            selected_columns={
                "sub_material": ["sub_material_name"],
            },
            generation_tables=["sub_material"],
            generation_columns={
                "sub_material": ["sub_material_name"],
            },
            bound_dimensions=[{
                "dimension": "material",
                "table": "sub_material",
                "column": "sub_material_name",
                "applicable_module": "stores",
            }],
        )
    )

    assert sql == (
        "SELECT sm.`sub_material_name` AS `sub_material_name`\n"
        "FROM `sub_material` sm\n"
        "WHERE NULLIF(TRIM(sm.`sub_material_name`), '') IS NOT NULL\n"
        "ORDER BY sm.`sub_material_name` ASC\n"
        "LIMIT 10"
    )


def test_refuses_unverified_inventory_stock_value():
    assert verified_sql_compiler.compile(
        _plan(
            original_question="What is the inventory stock value?",
            operation="aggregation",
            metrics=["stock_value"],
            dimensions=[],
            aggregation=["sum"],
            selected_tables=["inventory_master"],
            generation_tables=["inventory_master"],
            bound_metrics=[{
                "metric": "current_stock",
                "table": "inventory_master",
                "column": "available_quantity",
                "applicable_module": "inventory",
            }],
        )
    ) is None


def test_compiles_project_cross_module_metrics_with_preaggregated_joins():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="show PO value and material issue value by project",
            operation="ranking",
            metrics=["purchase_value", "material_issue_value"],
            dimensions=["project"],
            requested_outputs=["project"],
            aggregation=["sum"],
            limit=10,
            selected_tables=[
                "project_mst",
                "po_form",
                "po_items",
                "store_material_usage_quantity",
                "store_usage_quantity_master",
            ],
            selected_columns={
                "project_mst": ["project_id", "project_name"],
                "po_form": ["po_id", "total_value"],
                "po_items": ["po_id", "po_project_id"],
                "store_usage_quantity_master": [
                    "store_usage_quantity_master_id",
                    "project_id",
                ],
                "store_material_usage_quantity": [
                    "store_usage_quantity_master_id",
                    "issued_quantity",
                    "base_charge",
                ],
            },
            generation_tables=[
                "project_mst",
                "po_form",
                "po_items",
                "store_material_usage_quantity",
                "store_usage_quantity_master",
            ],
            generation_columns={
                "project_mst": ["project_id", "project_name"],
                "po_form": ["po_id", "total_value"],
                "po_items": ["po_id", "po_project_id"],
                "store_usage_quantity_master": [
                    "store_usage_quantity_master_id",
                    "project_id",
                ],
                "store_material_usage_quantity": [
                    "store_usage_quantity_master_id",
                    "issued_quantity",
                    "base_charge",
                ],
            },
            bound_metrics=[
                {
                    "metric": "purchase_value",
                    "table": "po_form",
                    "column": "total_value",
                    "applicable_module": "purchase_orders",
                },
                {
                    "metric": "material_issue_value",
                    "table": "store_material_usage_quantity",
                    "column": "base_charge",
                    "applicable_module": "stores",
                },
            ],
            bound_dimensions=[{
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
                "applicable_module": "projects",
            }],
            catalog_authoritative=True,
        )
    )

    assert "FROM `project_mst` p" in sql
    assert "LEFT JOIN (" in sql
    assert "SELECT DISTINCT `po_id`, `po_project_id` FROM `po_items`" in sql
    assert "SUM(f.`total_value`) AS `purchase_value`" in sql
    assert "SUM(CAST(NULLIF(REPLACE(TRIM(d.`issued_quantity`), ',', ''), '') AS DECIMAL(20,4)) * COALESCE(CAST(NULLIF(REPLACE(TRIM(d.`base_charge`), ',', ''), '') AS DECIMAL(20,4)), 0)) AS `issued_value`" in sql
    assert "JOIN `store_usage_quantity_master` m" in sql
    assert sql.count(") mi ON") == 1


def test_compiles_project_billing_po_material_comparison_with_gap_columns():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question=(
                "compare project value billed amount procurement spend and "
                "material spent by project"
            ),
            operation="comparison",
            metrics=[
                "project_value",
                "billing",
                "purchase_value",
                "material_issue_value",
            ],
            dimensions=["project"],
            requested_outputs=["project"],
            aggregation=["sum"],
            limit=8,
            selected_tables=[
                "project_mst",
                "client_bill_raising",
                "po_form",
                "po_items",
                "store_material_usage_quantity",
                "store_usage_quantity_master",
            ],
            selected_columns={
                "project_mst": ["project_id", "project_name", "project_value"],
                "client_bill_raising": ["project_id", "gross_bill_value"],
                "po_form": ["po_id", "total_value"],
                "po_items": ["po_id", "po_project_id"],
                "store_usage_quantity_master": [
                    "store_usage_quantity_master_id",
                    "project_id",
                ],
                "store_material_usage_quantity": [
                    "store_usage_quantity_master_id",
                    "issued_quantity",
                    "base_charge",
                ],
            },
            generation_tables=[
                "project_mst",
                "client_bill_raising",
                "po_form",
                "po_items",
                "store_material_usage_quantity",
                "store_usage_quantity_master",
            ],
            generation_columns={
                "project_mst": ["project_id", "project_name", "project_value"],
                "client_bill_raising": ["project_id", "gross_bill_value"],
                "po_form": ["po_id", "total_value"],
                "po_items": ["po_id", "po_project_id"],
                "store_usage_quantity_master": [
                    "store_usage_quantity_master_id",
                    "project_id",
                ],
                "store_material_usage_quantity": [
                    "store_usage_quantity_master_id",
                    "issued_quantity",
                    "base_charge",
                ],
            },
            bound_metrics=[
                {
                    "metric": "project_value",
                    "table": "project_mst",
                    "column": "project_value",
                    "applicable_module": "projects",
                },
                {
                    "metric": "billing",
                    "table": "client_bill_raising",
                    "column": "gross_bill_value",
                    "applicable_module": "billing",
                },
                {
                    "metric": "purchase_value",
                    "table": "po_form",
                    "column": "total_value",
                    "applicable_module": "purchase_orders",
                },
                {
                    "metric": "material_issue_value",
                    "table": "store_material_usage_quantity",
                    "column": "base_charge",
                    "applicable_module": "stores",
                },
            ],
            bound_dimensions=[{
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
                "applicable_module": "projects",
            }],
            catalog_authoritative=True,
        )
    )

    assert "COALESCE(b.`gross_bill_value`, 0) AS `gross_bill_value`" in sql
    assert "COALESCE(po.`purchase_value`, 0) AS `purchase_value`" in sql
    assert "COALESCE(mi.`issued_value`, 0) AS `issued_value`" in sql
    assert "AS `purchase_minus_billing_value`" in sql
    assert "AS `billing_minus_material_issue_value`" in sql
    assert "AS `purchase_minus_material_issue_value`" in sql
    assert sql.count("LEFT JOIN (") == 3
    assert "FROM (SELECT DISTINCT `po_id`, `po_project_id` FROM `po_items`) x" in sql
    assert "LIMIT 8" in sql


def test_orders_cross_module_high_material_low_billing():
    sql = verified_sql_compiler.compile(
        _plan(
            original_question="Which projects have high material spend but low billing?",
            operation="ranking",
            metrics=["billing", "material_issue_value"],
            dimensions=["project"],
            requested_outputs=["project"],
            aggregation=["sum"],
            selected_tables=[
                "project_mst",
                "client_bill_raising",
                "store_material_usage_quantity",
                "store_usage_quantity_master",
            ],
            selected_columns={
                "project_mst": ["project_id", "project_name"],
                "client_bill_raising": ["project_id", "gross_bill_value"],
                "store_usage_quantity_master": [
                    "store_usage_quantity_master_id",
                    "project_id",
                ],
                "store_material_usage_quantity": [
                    "store_usage_quantity_master_id",
                    "issued_quantity",
                    "base_charge",
                ],
            },
            generation_tables=[
                "project_mst",
                "client_bill_raising",
                "store_material_usage_quantity",
                "store_usage_quantity_master",
            ],
            generation_columns={
                "project_mst": ["project_id", "project_name"],
                "client_bill_raising": ["project_id", "gross_bill_value"],
                "store_usage_quantity_master": [
                    "store_usage_quantity_master_id",
                    "project_id",
                ],
                "store_material_usage_quantity": [
                    "store_usage_quantity_master_id",
                    "issued_quantity",
                    "base_charge",
                ],
            },
            bound_metrics=[
                {
                    "metric": "billing",
                    "table": "client_bill_raising",
                    "column": "gross_bill_value",
                    "applicable_module": "billing",
                },
                {
                    "metric": "material_issue_value",
                    "table": "store_material_usage_quantity",
                    "column": "base_charge",
                    "applicable_module": "stores",
                },
            ],
            bound_dimensions=[{
                "dimension": "project",
                "table": "project_mst",
                "column": "project_name",
                "applicable_module": "projects",
            }],
            catalog_authoritative=True,
        )
    )

    assert "ORDER BY `issued_value` DESC, `gross_bill_value` ASC" in sql

"""Verified ground-truth additions for the fixed ERP benchmark.

This module is benchmark data, not runtime query routing. SQL here is manually
reviewable reference SQL used to judge the production pipeline.
"""

from typing import Any


REFERENCE_SQL_BY_SUFFIX: dict[str, str] = {
    "material-issued-last-30": """
        SELECT p.project_name, SUM(d.issued_quantity) AS issued_quantity
        FROM store_usage_quantity_master m
        JOIN store_material_usage_quantity d
          ON d.store_usage_quantity_master_id = m.store_usage_quantity_master_id
        JOIN project_mst p ON p.project_id = m.project_id
        WHERE STR_TO_DATE(NULLIF(m.issued_date, ''), '%d-%m-%Y')
              >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        GROUP BY p.project_id, p.project_name
        ORDER BY issued_quantity DESC
        LIMIT 20
    """,
    "material-by-name": """
        SELECT sm.sub_material_name,
               SUM(d.issued_quantity) AS issued_quantity,
               AVG(d.base_charge) AS average_base_charge
        FROM store_usage_quantity_master m
        JOIN store_material_usage_quantity d
          ON d.store_usage_quantity_master_id = m.store_usage_quantity_master_id
        JOIN sub_material sm ON sm.sub_material_id = d.sub_material_id
        JOIN project_mst p ON p.project_id = m.project_id
        WHERE p.project_name = 'RDSS-VIZAG'
        GROUP BY sm.sub_material_id, sm.sub_material_name
        ORDER BY issued_quantity DESC
        LIMIT 10
    """,
    "year-project-start-2025": """
        SELECT project_id, project_name, project_start_date
        FROM project_mst
        WHERE YEAR(STR_TO_DATE(NULLIF(project_start_date, ''), '%d-%m-%Y')) = 2025
        ORDER BY STR_TO_DATE(project_start_date, '%d-%m-%Y')
        LIMIT 50
    """,
    "fy-project-start-2025": """
        SELECT project_id, project_name, project_start_date
        FROM project_mst
        WHERE STR_TO_DATE(NULLIF(project_start_date, ''), '%d-%m-%Y')
              BETWEEN '2025-04-01' AND '2026-03-31'
        ORDER BY STR_TO_DATE(project_start_date, '%d-%m-%Y')
        LIMIT 50
    """,
    "year-project-end-2026": """
        SELECT project_id, project_name, project_end_date
        FROM project_mst
        WHERE YEAR(STR_TO_DATE(NULLIF(project_end_date, ''), '%d-%m-%Y')) = 2026
        ORDER BY STR_TO_DATE(project_end_date, '%d-%m-%Y')
        LIMIT 50
    """,
    "fy-project-end-2026": """
        SELECT project_id, project_name, project_end_date
        FROM project_mst
        WHERE STR_TO_DATE(NULLIF(project_end_date, ''), '%d-%m-%Y')
              BETWEEN '2026-04-01' AND '2027-03-31'
        ORDER BY STR_TO_DATE(project_end_date, '%d-%m-%Y')
        LIMIT 50
    """,
    "year-billing-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(bill_date, ''), '%d-%m-%Y'), '%Y-%m') AS bill_month,
               SUM(gross_bill_value) AS gross_bill_value
        FROM client_bill_raising
        WHERE YEAR(STR_TO_DATE(NULLIF(bill_date, ''), '%d-%m-%Y')) = 2025
        GROUP BY bill_month
        ORDER BY bill_month
    """,
    "fy-billing-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(bill_date, ''), '%d-%m-%Y'), '%Y-%m') AS bill_month,
               SUM(gross_bill_value) AS gross_bill_value
        FROM client_bill_raising
        WHERE STR_TO_DATE(NULLIF(bill_date, ''), '%d-%m-%Y')
              BETWEEN '2025-04-01' AND '2026-03-31'
        GROUP BY bill_month
        ORDER BY bill_month
    """,
    "year-po-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y'), '%Y-%m') AS po_month,
               COUNT(*) AS purchase_order_count,
               SUM(total_value) AS purchase_value
        FROM po_form
        WHERE YEAR(STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y')) = 2025
        GROUP BY po_month
        ORDER BY po_month
    """,
    "fy-po-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y'), '%Y-%m') AS po_month,
               COUNT(*) AS purchase_order_count,
               SUM(total_value) AS purchase_value
        FROM po_form
        WHERE STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y')
              BETWEEN '2025-04-01' AND '2026-03-31'
        GROUP BY po_month
        ORDER BY po_month
    """,
    "year-grn-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(grn_date, ''), '%d-%m-%Y'), '%Y-%m') AS grn_month,
               COUNT(*) AS receipt_count,
               SUM(value) AS received_value
        FROM goods_receipt_cum_insp_note
        WHERE YEAR(STR_TO_DATE(NULLIF(grn_date, ''), '%d-%m-%Y')) = 2025
        GROUP BY grn_month
        ORDER BY grn_month
    """,
    "fy-grn-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(grn_date, ''), '%d-%m-%Y'), '%Y-%m') AS grn_month,
               COUNT(*) AS receipt_count,
               SUM(value) AS received_value
        FROM goods_receipt_cum_insp_note
        WHERE STR_TO_DATE(NULLIF(grn_date, ''), '%d-%m-%Y')
              BETWEEN '2025-04-01' AND '2026-03-31'
        GROUP BY grn_month
        ORDER BY grn_month
    """,
    "year-issue-2026": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(issued_date, ''), '%d-%m-%Y'), '%Y-%m') AS issue_month,
               COUNT(*) AS issue_count
        FROM store_usage_quantity_master
        WHERE YEAR(STR_TO_DATE(NULLIF(issued_date, ''), '%d-%m-%Y')) = 2026
        GROUP BY issue_month
        ORDER BY issue_month
    """,
    "fy-issue-2026": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(issued_date, ''), '%d-%m-%Y'), '%Y-%m') AS issue_month,
               COUNT(*) AS issue_count
        FROM store_usage_quantity_master
        WHERE STR_TO_DATE(NULLIF(issued_date, ''), '%d-%m-%Y')
              BETWEEN '2026-04-01' AND '2027-03-31'
        GROUP BY issue_month
        ORDER BY issue_month
    """,
    "year-attendance-2026": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y'), '%Y-%m') AS attendance_month,
               COUNT(*) AS attendance_count
        FROM emp_attendance_register
        WHERE YEAR(STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y')) = 2026
        GROUP BY attendance_month
        ORDER BY attendance_month
    """,
    "fy-attendance-2026": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y'), '%Y-%m') AS attendance_month,
               COUNT(*) AS attendance_count
        FROM emp_attendance_register
        WHERE STR_TO_DATE(NULLIF(date, ''), '%d-%m-%Y')
              BETWEEN '2026-04-01' AND '2027-03-31'
        GROUP BY attendance_month
        ORDER BY attendance_month
    """,
    "year-workorder-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(date_of_Commencement, ''), '%d-%m-%Y'), '%Y-%m') AS workorder_month,
               COUNT(*) AS workorder_count,
               SUM(after_taxes_grandtotal) AS workorder_value
        FROM subcontract_workorder_details
        WHERE YEAR(STR_TO_DATE(NULLIF(date_of_Commencement, ''), '%d-%m-%Y')) = 2025
        GROUP BY workorder_month
        ORDER BY workorder_month
    """,
    "fy-workorder-2025": """
        SELECT DATE_FORMAT(STR_TO_DATE(NULLIF(date_of_Commencement, ''), '%d-%m-%Y'), '%Y-%m') AS workorder_month,
               COUNT(*) AS workorder_count,
               SUM(after_taxes_grandtotal) AS workorder_value
        FROM subcontract_workorder_details
        WHERE STR_TO_DATE(NULLIF(date_of_Commencement, ''), '%d-%m-%Y')
              BETWEEN '2025-04-01' AND '2026-03-31'
        GROUP BY workorder_month
        ORDER BY workorder_month
    """,
    "project-billing-vs-value": """
        SELECT p.project_name,
               CAST(NULLIF(p.project_value, '') AS DECIMAL(20,2)) AS project_value,
               COALESCE(b.gross_bill_value, 0) AS gross_bill_value
        FROM project_mst p
        LEFT JOIN (
            SELECT project_id, SUM(gross_bill_value) AS gross_bill_value
            FROM client_bill_raising
            GROUP BY project_id
        ) b ON b.project_id = p.project_id
        ORDER BY project_value DESC
        LIMIT 20
    """,
    "project-po-vs-billing": """
        SELECT p.project_name,
               COALESCE(po.purchase_value, 0) AS purchase_value,
               COALESCE(b.gross_bill_value, 0) AS gross_bill_value
        FROM project_mst p
        LEFT JOIN (
            SELECT x.po_project_id AS project_id, SUM(f.total_value) AS purchase_value
            FROM (
                SELECT DISTINCT po_id, po_project_id
                FROM po_items
            ) x
            JOIN po_form f ON f.po_id = x.po_id
            GROUP BY x.po_project_id
        ) po ON po.project_id = p.project_id
        LEFT JOIN (
            SELECT project_id, SUM(gross_bill_value) AS gross_bill_value
            FROM client_bill_raising
            GROUP BY project_id
        ) b ON b.project_id = p.project_id
        ORDER BY purchase_value DESC
        LIMIT 20
    """,
    "material-issue-project-price": """
        SELECT p.project_name, sm.sub_material_name,
               SUM(d.issued_quantity) AS issued_quantity,
               AVG(d.base_charge) AS average_base_charge
        FROM store_usage_quantity_master m
        JOIN store_material_usage_quantity d
          ON d.store_usage_quantity_master_id = m.store_usage_quantity_master_id
        JOIN sub_material sm ON sm.sub_material_id = d.sub_material_id
        JOIN project_mst p ON p.project_id = m.project_id
        WHERE STR_TO_DATE(NULLIF(m.issued_date, ''), '%d-%m-%Y')
              >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        GROUP BY p.project_id, p.project_name, sm.sub_material_id, sm.sub_material_name
        ORDER BY issued_quantity DESC
        LIMIT 20
    """,
    "employee-attendance-department": """
        SELECT d.department_name, COUNT(a.emp_attendance_register_id) AS attendance_count
        FROM emp_attendance_register a
        JOIN employee_table e ON e.employee_id = a.employee_id
        JOIN department d ON d.department_id = e.department_id
        GROUP BY d.department_id, d.department_name
        ORDER BY attendance_count DESC
        LIMIT 20
    """,
    "po-validity-expiring": """
        SELECT po_no, vendor_name, po_validity_date, total_value
        FROM po_form
        WHERE YEAR(STR_TO_DATE(NULLIF(po_validity_date, ''), '%d-%m-%Y')) = 2026
        ORDER BY STR_TO_DATE(po_validity_date, '%d-%m-%Y')
        LIMIT 50
    """,
    "grn-damage-by-project": """
        SELECT p.project_name, SUM(g.damage) AS damaged_quantity
        FROM goods_receipt_cum_insp_note g
        JOIN project_mst p ON p.project_id = g.grn_project_id
        GROUP BY p.project_id, p.project_name
        ORDER BY damaged_quantity DESC
        LIMIT 20
    """,
    "grn-excess-by-material": """
        SELECT sm.sub_material_name, SUM(g.excess) AS excess_quantity
        FROM goods_receipt_cum_insp_note g
        JOIN sub_material sm ON sm.sub_material_id = g.grn_sub_material_id
        GROUP BY sm.sub_material_id, sm.sub_material_name
        ORDER BY excess_quantity DESC
        LIMIT 20
    """,
    "inventory-zero-2099": """
        SELECT inventory_master_transaction_id, trans_date, sub_material_id, quantity
        FROM inventory_master_transaction
        WHERE YEAR(STR_TO_DATE(NULLIF(trans_date, ''), '%d-%m-%Y')) = 2099
        LIMIT 50
    """,
    "employee-joining-trend": """
        SELECT YEAR(STR_TO_DATE(NULLIF(date_of_joining, ''), '%d-%m-%Y')) AS joining_year,
               COUNT(*) AS employee_count
        FROM employee_table
        WHERE NULLIF(date_of_joining, '') IS NOT NULL
        GROUP BY joining_year
        ORDER BY joining_year
    """,
    "attendance-missing-out-time": """
        SELECT e.employee_id, e.emp_name, a.date, a.in_time, a.out_time
        FROM emp_attendance_register a
        JOIN employee_table e ON e.employee_id = a.employee_id
        WHERE a.out_time IS NULL OR TRIM(a.out_time) = ''
        ORDER BY STR_TO_DATE(NULLIF(a.date, ''), '%d-%m-%Y') DESC
        LIMIT 50
    """,
    "project-apspdcl-vs-apepdcl": """
        SELECT client_name, COUNT(*) AS project_count,
               SUM(CAST(NULLIF(project_value, '') AS DECIMAL(20,2))) AS project_value
        FROM project_mst
        WHERE client_name IN ('APSPDCL', 'APEPDCL')
        GROUP BY client_name
        ORDER BY project_value DESC
    """,
    "project-rdss-vizag-material-receipts": """
        SELECT sm.sub_material_name,
               SUM(g.qty_actual_received) AS received_quantity,
               SUM(g.value) AS received_value
        FROM goods_receipt_cum_insp_note g
        JOIN project_mst p ON p.project_id = g.grn_project_id
        JOIN sub_material sm ON sm.sub_material_id = g.grn_sub_material_id
        WHERE p.project_name = 'RDSS-VIZAG'
        GROUP BY sm.sub_material_id, sm.sub_material_name
        ORDER BY received_value DESC
        LIMIT 20
    """,
    "project-client-state-filter": """
        SELECT project_id, project_name, client_name, project_state, project_value
        FROM project_mst
        WHERE client_name = 'APSPDCL' AND project_state = 'Andhra Pradesh'
        ORDER BY project_name
        LIMIT 50
    """,
    "material-price-variance": """
        SELECT sm.sub_material_name,
               MIN(d.base_charge) AS minimum_base_charge,
               MAX(d.base_charge) AS maximum_base_charge,
               MAX(d.base_charge) - MIN(d.base_charge) AS price_variance
        FROM store_material_usage_quantity d
        JOIN sub_material sm ON sm.sub_material_id = d.sub_material_id
        WHERE d.base_charge IS NOT NULL
        GROUP BY sm.sub_material_id, sm.sub_material_name
        ORDER BY price_variance DESC
        LIMIT 20
    """,
    "entity-rdss-billing": """
        SELECT p.project_name, SUM(b.gross_bill_value) AS gross_bill_value
        FROM project_mst p
        JOIN client_bill_raising b ON b.project_id = p.project_id
        WHERE p.project_name = 'RDSS-VIZAG'
        GROUP BY p.project_id, p.project_name
    """,
    "entity-rdss-po": """
        SELECT p.project_name, f.po_no, f.vendor_name, f.total_value
        FROM project_mst p
        JOIN (
            SELECT DISTINCT po_id, po_project_id
            FROM po_items
        ) x ON x.po_project_id = p.project_id
        JOIN po_form f ON f.po_id = x.po_id
        WHERE p.project_name = 'RDSS-VIZAG'
        ORDER BY f.total_value DESC
        LIMIT 50
    """,
    "entity-vendor-branch": """
        SELECT vendor_id, contact_person, contact_number, state, gst_no
        FROM vendor_branch_details
        WHERE vendor_id = 12
        LIMIT 50
    """,
    "entity-material-vendors": """
        SELECT DISTINCT vendor_id, sub_material_id
        FROM vendor_material_details
        WHERE sub_material_id = 100
        ORDER BY vendor_id
        LIMIT 50
    """,
    "entity-employee-attendance": """
        SELECT e.employee_id, e.emp_name, a.date, a.attendence_type_name, a.in_time, a.out_time
        FROM employee_table e
        JOIN emp_attendance_register a ON a.employee_id = e.employee_id
        WHERE e.employee_id = 1045
        ORDER BY STR_TO_DATE(NULLIF(a.date, ''), '%d-%m-%Y') DESC
        LIMIT 50
    """,
    "entity-two-projects": """
        SELECT p.project_name, SUM(b.gross_bill_value) AS gross_bill_value
        FROM project_mst p
        JOIN client_bill_raising b ON b.project_id = p.project_id
        WHERE p.project_name IN ('RDSS-VIZAG', 'HRDCL')
        GROUP BY p.project_id, p.project_name
        ORDER BY gross_bill_value DESC
    """,
    "entity-two-clients": """
        SELECT client_name, project_id, project_name, project_value
        FROM project_mst
        WHERE client_name IN ('APEPDCL', 'APSPDCL')
        ORDER BY client_name, project_name
        LIMIT 50
    """,
}


# These questions require meanings or relationships that are not verified in the
# catalog. The correct production behaviour is clarification, not guessed SQL.
CASE_OVERRIDES_BY_SUFFIX: dict[str, dict[str, Any]] = {
    "billing-status-year": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "approved_billing_status_value_required",
        "safe_failure": "billing_status_meaning_unverified",
    },
    "billing-pending-by-client": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "pending_billing_status_value_required",
        "safe_failure": "billing_status_meaning_unverified",
    },
    "store-cancelled-issues": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "cancel_status_value_required",
        "safe_failure": "cancel_status_meaning_unverified",
    },
    "store-return-materials": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "safe_failure"},
        "safe_failure": "return_transaction_type_not_present",
    },
    "employee-active-by-project": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "active_employee_status_value_required",
        "safe_failure": "employee_status_meaning_unverified",
    },
    "attendance-late-punches": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "late_punch_definition_and_join_required",
        "safe_failure": "attendance_punch_relationship_unverified",
    },
    "vendor-state-material": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "vendor_relationship_required",
        "safe_failure": "vendor_branch_material_relationship_unverified",
    },
    "boq-workorder-billing": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "boq_workorder_relationship_required",
        "safe_failure": "boq_workorder_relationship_unverified",
    },
    "workorder-closed-status": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "closed_workorder_status_value_required",
        "safe_failure": "workorder_status_meaning_unverified",
    },
    "entity-workorder": {
        "answerable": False,
        "reference_sql": None,
        "expected_result": {"type": "clarification_or_safe_failure"},
        "expected_clarification": "project_workorder_relationship_required",
        "safe_failure": "project_workorder_relationship_unverified",
    },
}


def apply_ground_truth(case: dict[str, Any]) -> dict[str, Any]:
    """Apply reviewed SQL and safe-failure corrections to one generated case."""

    payload = dict(case)
    suffix = payload["suffix"]
    if suffix in REFERENCE_SQL_BY_SUFFIX:
        payload["reference_sql"] = " ".join(
            REFERENCE_SQL_BY_SUFFIX[suffix].split()
        )
    payload.update(CASE_OVERRIDES_BY_SUFFIX.get(suffix, {}))
    return payload

def column(name, data_type="varchar", **metadata):
    return {"column_name": name, "data_type": data_type, **metadata}


def table(*columns, foreign_keys=None, indexes=None, comment=None, row_count=None):
    return {
        "columns": list(columns),
        "foreign_keys": list(foreign_keys or []),
        "indexes": list(indexes or []),
        "comment": comment,
        "row_count": row_count,
    }


def construction_schema():
    return {
        "ProjectMaster": table(
            column("ProjectID", "int", primary_key=True),
            column("ProjectName"),
            column("ApprovedValue", "decimal", comment="Approved contract value"),
            column("ProjectStatus"),
            column("StartDate", "date"),
            indexes=[{"name": "uq_project_name", "columns": ["ProjectName"], "unique": True}],
            comment="Construction projects",
            row_count=25,
        ),
        "tbl_site": table(
            column("site_id", "int", primary_key=True),
            column("project_id", "int", indexed=True),
            column("site_name"),
            foreign_keys=[{"column": "project_id", "table": "ProjectMaster", "references": "ProjectID"}],
            row_count=60,
        ),
        "material_issue_hdr": table(
            column("issue_id", "bigint", primary_key=True),
            column("site_id", "int", indexed=True),
            column("issue_date", "date"),
            column("issue_status"),
            foreign_keys=[{"column": "site_id", "table": "tbl_site", "references": "site_id"}],
            row_count=1000,
        ),
        "material_issue_dtl": table(
            column("issue_line_id", "bigint", primary_key=True),
            column("issue_id", "bigint", indexed=True),
            column("material_code"),
            column("issued_qty", "decimal"),
            column("unit_rate", "decimal"),
            foreign_keys=[{"column": "issue_id", "table": "material_issue_hdr", "references": "issue_id"}],
            row_count=5000,
        ),
    }


def hr_schema():
    return {
        "EmployeeDirectory": table(
            column("employeeId", "int", primary_key=True),
            column("employeeName"),
            column("departmentId", "int", indexed=True),
            column("isActive", "boolean"),
            column("joinDate", "date"),
            foreign_keys=[{"column": "departmentId", "table": "Departments", "references": "departmentId"}],
            row_count=700,
        ),
        "Departments": table(
            column("departmentId", "int", primary_key=True),
            column("departmentName"),
            column("managerId", "int", indexed=True),
            row_count=18,
        ),
        "TimeSheetEntries": table(
            column("entryId", "bigint", primary_key=True),
            column("employeeId", "int", indexed=True),
            column("workDate", "date"),
            column("regularHours", "decimal"),
            column("overtimeHours", "decimal"),
            foreign_keys=[{"column": "employeeId", "table": "EmployeeDirectory", "references": "employeeId"}],
            row_count=80000,
        ),
        "PayrollRun": table(
            column("payrollId", "bigint", primary_key=True),
            column("employeeId", "int", indexed=True),
            column("grossAmount", "decimal"),
            column("netAmount", "decimal"),
            column("payDate", "date"),
            foreign_keys=[{"column": "employeeId", "table": "EmployeeDirectory", "references": "employeeId"}],
            row_count=9000,
        ),
    }


def finance_schema():
    return {
        "customer_accounts": table(
            column("customer_id", "uuid", primary_key=True),
            column("customer_name"),
            column("region_code"),
            row_count=300,
        ),
        "sales_invoices": table(
            column("invoice_id", "uuid", primary_key=True),
            column("invoice_number"),
            column("customer_id", "uuid", indexed=True),
            column("invoice_date", "date"),
            column("gross_amount", "decimal"),
            column("tax_amount", "decimal"),
            column("outstanding_balance", "decimal"),
            column("payment_status"),
            foreign_keys=[{"column": "customer_id", "table": "customer_accounts", "references": "customer_id"}],
            row_count=20000,
        ),
        "cash_receipts": table(
            column("receipt_id", "uuid", primary_key=True),
            column("invoice_id", "uuid", indexed=True),
            column("receipt_date", "datetime"),
            column("received_amount", "decimal"),
            foreign_keys=[{"column": "invoice_id", "table": "sales_invoices", "references": "invoice_id"}],
            row_count=16000,
        ),
    }


def manufacturing_schema():
    return {
        "products": table(column("product_id", "int", primary_key=True), column("sku"), column("product_name"), row_count=2000),
        "productionOrders": table(
            column("productionOrderId", "bigint", primary_key=True),
            column("product_id", "int", indexed=True),
            column("plannedQuantity", "decimal"),
            column("actualQuantity", "decimal"),
            column("scheduledDate", "date"),
            column("orderStatus"),
            foreign_keys=[{"column": "product_id", "table": "products", "references": "product_id"}],
            row_count=12000,
        ),
        "machine_downtime_events": table(
            column("event_id", "bigint", primary_key=True),
            column("machine_code"),
            column("started_at", "datetime"),
            column("downtime_hours", "decimal"),
            column("reason_code"),
            row_count=4500,
        ),
        "quality_failures": table(
            column("failure_id", "bigint", primary_key=True),
            column("productionOrderId", "bigint", indexed=True),
            column("failure_rate", "decimal"),
            column("failure_date", "date"),
            foreign_keys=[{"column": "productionOrderId", "table": "productionOrders", "references": "productionOrderId"}],
            row_count=900,
        ),
    }


def inventory_schema():
    return {
        "catalog_items": table(
            column("item_id", "int", primary_key=True),
            column("item_code"),
            column("item_name"),
            column("category_name"),
            row_count=800,
        ),
        "storage_locations": table(
            column("location_id", "int", primary_key=True),
            column("location_name"),
            row_count=12,
        ),
        "inventory_movements": table(
            column("movement_id", "bigint", primary_key=True),
            column("item_id", "int", indexed=True),
            column("location_id", "int", indexed=True),
            column("movement_date", "date"),
            column("movement_status"),
            column("movement_type"),
            column("returned_quantity", "decimal"),
            foreign_keys=[
                {"column": "item_id", "table": "catalog_items", "references": "item_id"},
                {"column": "location_id", "table": "storage_locations", "references": "location_id"},
            ],
            row_count=250000,
        ),
        "inventory_snapshots": table(
            column("snapshot_id", "bigint", primary_key=True),
            column("item_id", "int", indexed=True),
            column("snapshot_date", "date"),
            column("on_hand_quantity", "decimal"),
            foreign_keys=[{"column": "item_id", "table": "catalog_items", "references": "item_id"}],
            row_count=50000,
        ),
    }


def obfuscated_schema():
    return {
        "a01": table(
            column("k01", "int", primary_key=True, comment="Customer identifier"),
            column("v02", "varchar", comment="Customer display name"),
            column("r03", "varchar", comment="Sales region"),
            comment="Customer account master",
            row_count=300,
        ),
        "z77": table(
            column("k99", "bigint", primary_key=True, comment="Invoice identifier"),
            column("k01", "int", indexed=True, comment="Customer identifier"),
            column("d09", "date", comment="Invoice date"),
            column("x05", "decimal", comment="Invoice total amount"),
            column("s02", "varchar", comment="Payment status"),
            foreign_keys=[{"column": "k01", "table": "a01", "references": "k01"}],
            comment="Issued customer invoice facts",
            row_count=20000,
        ),
        "q12": table(
            column("qk", "bigint", primary_key=True, comment="Audit identifier"),
            column("d09", "datetime", comment="Audit timestamp"),
            column("x05", "text", comment="Serialized audit payload"),
            comment="Technical audit records",
            row_count=900000,
        ),
    }


def sales_schema():
    return {
        "Account": table(column("AccountId", "int", primary_key=True), column("AccountName"), column("Region"), row_count=5000),
        "SalesOrder": table(
            column("OrderId", "bigint", primary_key=True),
            column("AccountId", "int", indexed=True),
            column("OrderDate", "date"),
            column("OrderStatus"),
            column("TotalValue", "decimal"),
            foreign_keys=[{"column": "AccountId", "table": "Account", "references": "AccountId"}],
            row_count=40000,
        ),
        "OrderLine": table(
            column("OrderLineId", "bigint", primary_key=True),
            column("OrderId", "bigint", indexed=True),
            column("ProductCode"),
            column("Quantity", "decimal"),
            column("UnitPrice", "decimal"),
            foreign_keys=[{"column": "OrderId", "table": "SalesOrder", "references": "OrderId"}],
            row_count=180000,
        ),
    }


def legacy_schema():
    return {
        "m_emp": table(
            column("emp_cd", "varchar", primary_key=True, comment="Employee code"),
            column("full_nm", "varchar", comment="Employee full name"),
            column("dept_cd"),
            row_count=450,
        ),
        "t_prj_hdr": table(
            column("prj_id", "int", primary_key=True),
            column("prj_nm", "varchar", comment="Project display name"),
            column("appr_amt", "numeric", comment="Approved amount"),
            column("st_cd", "varchar", comment="Current status code"),
            row_count=30,
        ),
        "trn_iss": table(
            column("iss_no", "varchar", primary_key=True),
            column("prj_id", "int", indexed=True),
            column("iss_dt", "varchar", comment="Issue date as DD-MM-YYYY"),
            column("itm_cd"),
            column("iss_qty", "numeric"),
            foreign_keys=[{"column": "prj_id", "table": "t_prj_hdr", "references": "prj_id"}],
            row_count=4000,
        ),
        "fin_txn1": table(
            column("txn_id", "bigint", primary_key=True),
            column("prj_id", "int", indexed=True),
            column("txn_dt", "date"),
            column("net_amt", "decimal"),
            foreign_keys=[{"column": "prj_id", "table": "t_prj_hdr", "references": "prj_id"}],
            row_count=9000,
        ),
    }


def minimal_fk_schema():
    return {
        "people": table(column("person_id", "int", primary_key=True), column("person_name"), row_count=100),
        "teams": table(column("team_id", "int", primary_key=True), column("team_name"), row_count=10),
        "work_log": table(
            column("work_log_id", "bigint", primary_key=True),
            column("person_id", "int"),
            column("team_id", "int"),
            column("work_date", "date"),
            column("hours", "decimal"),
            row_count=2000,
        ),
    }


def large_schema(size=1000):
    result = {}
    for index in range(size):
        result[f"domain{index}_events"] = table(
            column(f"domain{index}_id", "bigint", primary_key=True),
            column("display_name"),
            column("event_date", "date"),
            column("metric_amount", "decimal"),
            column("record_status"),
            row_count=index,
        )
    return result

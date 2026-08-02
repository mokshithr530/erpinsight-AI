import re


VERIFIED_SQL_COMPILER_VERSION = "verified-sql-compiler:v20"


class VerifiedSQLCompiler:
    _SCALAR_OPERATIONS = {
        "aggregation",
        "average",
        "count",
        "existence_check",
        "maximum",
        "minimum",
        "sum",
    }
    _FUNCTIONS = {
        "average": "AVG",
        "avg": "AVG",
        "maximum": "MAX",
        "max": "MAX",
        "minimum": "MIN",
        "min": "MIN",
        "sum": "SUM",
        "total": "SUM",
    }
    _MIN_MAX = {"min", "minimum", "max", "maximum"}

    def compile(self, plan):
        if not plan.catalog_authoritative:
            return None
        entity_lookup = self._is_entity_lookup(plan)
        if (
            plan.clarification_required
            or (
                plan.unresolved_concepts
                and not (
                    entity_lookup
                    and all(
                        str(item).startswith("relationship:")
                        for item in plan.unresolved_concepts
                    )
                    or (
                        plan.resolved_entity_constraints
                        and all(
                            str(item).startswith("entity:")
                            for item in plan.unresolved_concepts
                        )
                    )
                )
            )
            or (plan.status_constraints and not plan.bound_status_fields)
            or (plan.date_constraints and not plan.bound_date_fields)
        ):
            return None
        if plan.filters and not plan.resolved_entity_constraints:
            return None

        cross_module_sql = self._compile_project_cross_module(plan)
        if cross_module_sql:
            return cross_module_sql

        project_comparison_sql = self._compile_project_comparison(plan)
        if project_comparison_sql:
            return project_comparison_sql

        billing_sql = self._compile_billing(plan)
        if billing_sql:
            return billing_sql

        inventory_sql = self._compile_inventory(plan)
        if inventory_sql:
            return inventory_sql
        if self._is_inventory_plan(plan):
            return None

        store_sql = self._compile_store(plan)
        if store_sql:
            return store_sql

        vendor_sql = self._compile_vendor(plan)
        if vendor_sql:
            return vendor_sql

        po_sql = self._compile_po(plan)
        if po_sql:
            return po_sql

        project_sql = self._compile_project(plan)
        if project_sql:
            return project_sql

        aggregations = self._aggregation_set(plan)
        if aggregations and aggregations <= self._MIN_MAX:
            return self._compile_min_max(plan, list(aggregations))
        if entity_lookup:
            return self._compile_entity_lookup(plan)
        if plan.operation in {"ranking", "top_n", "bottom_n"}:
            return self._compile_ranking(plan)
        if plan.resolved_entity_constraints and plan.bound_dimensions and not plan.bound_metrics:
            return self._compile_joined_lookup(plan)
        if plan.bound_metrics and plan.resolved_entity_constraints:
            return self._compile_filtered_aggregate(plan)
        if plan.operation in self._SCALAR_OPERATIONS:
            return self._compile_scalar(plan)
        return None

    def _compile_scalar(self, plan):
        if (
            plan.resolved_entity_constraints
            or plan.grouping
            or plan.ordering
            or plan.joins
            or plan.approved_relationships
            or plan.dimensions
            or plan.requested_outputs
            or plan.limit is not None
        ):
            return None

        aggregations = [
            str(item).strip().lower()
            for item in plan.aggregation
            if item
        ]
        if set(aggregations) and set(aggregations) <= self._MIN_MAX:
            return self._compile_min_max(plan, aggregations)
        if len(aggregations) != 1:
            return None
        aggregation = aggregations[0]

        if aggregation in {"count", "distinct_count"}:
            table = self._single_generation_table(plan)
            if not table:
                return None
            return (
                f"SELECT COUNT(*) AS {self._quote('row_count')} "
                f"FROM {self._quote(table)}"
            )

        function = self._FUNCTIONS.get(aggregation)
        if not function or len(plan.bound_metrics) != 1:
            return None
        metric = plan.bound_metrics[0]
        table = metric.get("table")
        column = metric.get("column")
        if not self._identifier(table) or not self._identifier(column):
            return None
        if self._single_generation_table(plan) != table:
            return None

        concept = (
            metric.get("bound_concept")
            or metric.get("metric")
            or column
        )
        alias = self._alias(function, concept)
        return (
            f"SELECT {function}({self._quote(column)}) AS {self._quote(alias)} "
            f"FROM {self._quote(table)}"
        )

    def _compile_filtered_aggregate(self, plan):
        metric = plan.bound_metrics[0] if plan.bound_metrics else None
        if not metric:
            return None
        metric_table = metric.get("table")
        metric_column = metric.get("column")
        if not self._identifier(metric_table) or not self._identifier(metric_column):
            return None
        entity = plan.resolved_entity_constraints[0]
        entity_table = entity.get("table")
        entity_column = entity.get("column")
        if not self._identifier(entity_table) or not self._identifier(entity_column):
            return None
        tables = self._tables_for_plan(plan)
        if metric_table not in tables or entity_table not in tables:
            return None
        aliases = self._aliases(tables)
        joins = self._join_sql(tables, plan.approved_relationships, aliases)
        if joins is None:
            return None
        where = self._where_sql(plan, aliases)
        if where is None:
            return None
        aggregations = self._aggregation_set(plan)
        function = "SUM"
        if "avg" in aggregations:
            function = "AVG"
        elif "count" in aggregations:
            function = "COUNT"
        metric_expr = f"{aliases[metric_table]}.{self._quote(metric_column)}"
        entity_expr = f"{aliases[entity_table]}.{self._quote(entity_column)}"
        alias = self._alias(function, metric.get("metric") or metric_column)
        return (
            f"SELECT {entity_expr} AS {self._quote(entity_column)}, "
            f"{function}({metric_expr}) AS {self._quote(alias)}\n"
            f"FROM {self._quote(tables[0])} {aliases[tables[0]]}"
            f"{joins}"
            f"{where}\n"
            f"GROUP BY {entity_expr}"
        )

    def _compile_joined_lookup(self, plan):
        dimension = self._primary_dimension(plan)
        if not dimension:
            return None
        dimension_table = dimension.get("table")
        entity = plan.resolved_entity_constraints[0]
        entity_table = entity.get("table")
        if not self._identifier(dimension_table) or not self._identifier(entity_table):
            return None
        tables = self._tables_for_plan(plan)
        if dimension_table not in tables or entity_table not in tables:
            return None
        aliases = self._aliases(tables)
        joins = self._join_sql(tables, plan.approved_relationships, aliases)
        if joins is None:
            return None
        where = self._where_sql(plan, aliases)
        if where is None:
            return None
        selected = self._lookup_select_columns(plan, dimension_table, entity_table)
        if not selected:
            return None
        select_sql = ", ".join(
            f"{aliases[table]}.{self._quote(column)} AS {self._quote(column)}"
            for table, column in selected
        )
        order = ""
        if (dimension_table, "total_value") in selected:
            order = f"\nORDER BY {aliases[dimension_table]}.{self._quote('total_value')} DESC"
        return (
            f"SELECT DISTINCT {select_sql}\n"
            f"FROM {self._quote(tables[0])} {aliases[tables[0]]}"
            f"{joins}"
            f"{where}"
            f"{order}\n"
            f"LIMIT {int(plan.limit or 50)}"
        )

    def _compile_min_max(self, plan, aggregations):
        if len(plan.bound_metrics) != 1:
            return None
        metric = plan.bound_metrics[0]
        table = metric.get("table")
        column = metric.get("column")
        if not self._identifier(table) or not self._identifier(column):
            return None
        if self._single_generation_table(plan) != table:
            return None
        concept = column
        select_parts = []
        if "min" in aggregations or "minimum" in aggregations:
            select_parts.append(
                f"MIN({self._quote(column)}) AS {self._quote(self._alias('MIN', concept))}"
            )
        if "max" in aggregations or "maximum" in aggregations:
            select_parts.append(
                f"MAX({self._quote(column)}) AS {self._quote(self._alias('MAX', concept))}"
            )
        if not select_parts:
            return None
        return f"SELECT {', '.join(select_parts)} FROM {self._quote(table)}"

    def _compile_ranking(self, plan):
        if not plan.bound_metrics:
            return None
        metric = plan.bound_metrics[0]
        metric_table = metric.get("table")
        metric_column = metric.get("column")
        if not self._identifier(metric_table) or not self._identifier(metric_column):
            return None
        dimension = self._primary_dimension(plan)
        if not dimension:
            return None
        dimension_table = dimension.get("table")
        dimension_column = dimension.get("column")
        if not self._identifier(dimension_table) or not self._identifier(dimension_column):
            return None

        tables = self._tables_for_plan(plan)
        if metric_table not in tables or dimension_table not in tables:
            return None
        aliases = self._aliases(tables)
        joins = self._join_sql(tables, plan.approved_relationships, aliases)
        if joins is None:
            return None

        direction = self._direction(plan)
        limit = int(plan.limit or 10)
        metric_expr = f"{aliases[metric_table]}.{self._quote(metric_column)}"
        metric_alias = self._alias("SUM", metric.get("metric") or metric_column)
        dimension_expr = f"{aliases[dimension_table]}.{self._quote(dimension_column)}"

        if self._uses_aggregate(plan):
            select_parts = [
                f"{dimension_expr} AS {self._quote(dimension_column)}",
                f"SUM({metric_expr}) AS {self._quote(metric_alias)}",
            ]
            group_by = f"\nGROUP BY {dimension_expr}"
            order_expr = self._quote(metric_alias)
        else:
            select_parts = [
                f"{dimension_expr} AS {self._quote(dimension_column)}",
                f"{metric_expr} AS {self._quote(metric_column)}",
            ]
            group_by = ""
            order_expr = metric_expr

        where = self._where_sql(plan, aliases)
        if where is None:
            return None
        sql = (
            f"SELECT {', '.join(select_parts)}\n"
            f"FROM {self._quote(tables[0])} {aliases[tables[0]]}"
            f"{joins}"
            f"{where}"
            f"{group_by}\n"
            f"ORDER BY {order_expr} {direction}\n"
            f"LIMIT {limit}"
        )
        return sql

    def _compile_project(self, plan):
        if not self._is_project_plan(plan):
            return None
        metric = self._project_metric(plan)
        project_dimension = self._dimension_by_name(plan, "project")
        client_dimension = self._dimension_by_name(plan, "client")
        location_dimension = (
            self._dimension_by_name(plan, "location")
            or self._dimension_by_name(plan, "state")
        )
        status_dimension = (
            self._dimension_by_name(plan, "project_status")
            or self._dimension_by_name(plan, "status")
        )
        month_dimension = self._dimension_by_name(plan, "month")
        aggregations = self._aggregation_set(plan)
        operation = str(plan.operation or "").lower()
        question = str(plan.original_question or "").lower()
        workorder_metric = self._workorder_metric(plan)

        if workorder_metric and project_dimension and operation in {"ranking", "top_n", "bottom_n"}:
            direction = self._direction(plan)
            return (
                "SELECT p.`project_name` AS `project_name`, "
                "SUM(w.`after_taxes_grandtotal`) AS `workorder_value`\n"
                "FROM `subcontract_workorder_details` w\n"
                "JOIN `project_mst` p ON w.`project_id` = p.`project_id`\n"
                "GROUP BY p.`project_id`, p.`project_name`\n"
                f"ORDER BY `workorder_value` {direction}\n"
                f"LIMIT {int(plan.limit or 1)}"
            )

        group_dimension = None
        if client_dimension:
            group_dimension = ("client_name", "client_name")
        elif location_dimension:
            group_dimension = ("project_state", "project_state")
        elif status_dimension:
            group_dimension = ("status", "status")
        elif project_dimension:
            group_dimension = ("project_name", "project_id")

        if "status" in {str(item).lower() for item in plan.grouping} and re.search(r"\bprojects?\b", question):
            return (
                "SELECT p.`status` AS `status`, COUNT(p.`project_id`) AS `project_count`\n"
                "FROM `project_mst` p\n"
                "GROUP BY p.`status`\n"
                "ORDER BY `project_count` DESC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if "count" in aggregations and group_dimension and plan.grouping:
            column, pk = group_dimension
            return (
                f"SELECT p.{self._quote(column)} AS {self._quote(column)}, COUNT(p.`project_id`) AS `project_count`\n"
                "FROM `project_mst` p\n"
                f"GROUP BY p.{self._quote(pk)}, p.{self._quote(column)}\n"
                "ORDER BY `project_count` DESC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if operation in {"count", "existence_check"} and not metric:
            return "SELECT COUNT(*) AS `project_count` FROM `project_mst`"

        if self._project_entities(plan) and not metric:
            return self._compile_project_lookup(plan)

        if metric and self._is_project_monthly_plan(plan, month_dimension):
            date_column = self._project_date_column(plan)
            date_expr = self._date_expr("p", date_column)
            where = self._project_date_where(plan, "p", date_column)
            return (
                "SELECT DATE_FORMAT("
                f"{date_expr}, '%Y-%m') AS `project_month`, "
                "COUNT(p.`project_id`) AS `project_count`, "
                f"SUM({self._project_value_expr('p')}) AS `project_value`\n"
                "FROM `project_mst` p"
                f"{where}\n"
                "GROUP BY `project_month`\n"
                "ORDER BY `project_month` ASC"
            )

        if metric and group_dimension and (plan.grouping or group_dimension[0] != "project_name"):
            column, pk = group_dimension
            direction = self._direction(plan)
            where = self._project_date_where(plan, "p", self._project_date_column(plan))
            id_select = "p.`project_id` AS `project_id`, " if column == "project_name" else ""
            return (
                f"SELECT {id_select}p.{self._quote(column)} AS {self._quote(column)}, "
                f"SUM({self._project_value_expr('p')}) AS `project_value`\n"
                "FROM `project_mst` p"
                f"{where}\n"
                f"GROUP BY p.{self._quote(pk)}, p.{self._quote(column)}\n"
                f"ORDER BY `project_value` {direction}\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if metric and operation in {"ranking", "top_n", "bottom_n", "lookup", "relationship_lookup"}:
            direction = self._direction(plan)
            where = self._project_entity_where(plan, "p")
            return (
                "SELECT p.`project_id` AS `project_id`, p.`project_name` AS `project_name`, "
                "p.`client_name` AS `client_name`, "
                f"{self._project_value_expr('p')} AS `project_value`, "
                "p.`project_start_date` AS `project_start_date`, p.`project_end_date` AS `project_end_date`\n"
                "FROM `project_mst` p"
                f"{where}\n"
                f"ORDER BY `project_value` {direction}\n"
                f"LIMIT {int(plan.limit or 10)}"
            )

        if metric and not any([project_dimension, client_dimension, location_dimension, status_dimension, self._project_entities(plan)]) and not plan.grouping:
            if aggregations and aggregations <= self._MIN_MAX:
                parts = []
                if "min" in aggregations:
                    parts.append(f"MIN({self._project_value_expr('p')}) AS `min_project_value`")
                if "max" in aggregations:
                    parts.append(f"MAX({self._project_value_expr('p')}) AS `max_project_value`")
                return "SELECT " + ", ".join(parts) + "\nFROM `project_mst` p"
            function = "SUM"
            if "avg" in aggregations:
                function = "AVG"
            where = self._project_date_where(plan, "p", self._project_date_column(plan))
            return (
                f"SELECT {function}({self._project_value_expr('p')}) AS {self._quote(self._alias(function, 'project_value'))}\n"
                "FROM `project_mst` p"
                f"{where}"
            )

        if not metric and (project_dimension or operation in {"lookup", "relationship_lookup", "metadata"}):
            where = self._project_entity_where(plan, "p")
            date_where = self._project_date_predicate(plan, "p", self._project_date_column(plan))
            where_parts = []
            if where:
                where_parts.append(where.removeprefix("\nWHERE "))
            if date_where:
                where_parts.append(date_where)
            where_sql = ("\nWHERE " + " AND ".join(where_parts)) if where_parts else ""
            order = "p.`project_name` ASC"
            if "ending" in question or "end" in question:
                order = f"{self._date_expr('p', 'project_end_date')} ASC"
            elif "started" in question or "start" in question:
                order = f"{self._date_expr('p', 'project_start_date')} ASC"
            return (
                "SELECT p.`project_id` AS `project_id`, p.`project_name` AS `project_name`, "
                "p.`client_name` AS `client_name`, p.`project_state` AS `project_state`, "
                f"{self._project_value_expr('p')} AS `project_value`, "
                "p.`project_start_date` AS `project_start_date`, p.`project_end_date` AS `project_end_date`, "
                "p.`status` AS `status`\n"
                "FROM `project_mst` p"
                f"{where_sql}\n"
                f"ORDER BY {order}\n"
                f"LIMIT {int(plan.limit or 50)}"
            )
        return None

    def _compile_project_comparison(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables)
        metric_names = {
            str(item.get("metric") or "").lower()
            for item in plan.bound_metrics
            if isinstance(item, dict)
        }
        question = str(plan.original_question or "").lower()
        if not (
            "project_mst" in tables
            and "client_bill_raising" in tables
            and {"project_value", "billing"} <= metric_names
            and "billing" in question
        ):
            return None
        return (
            "SELECT p.`project_id` AS `project_id`, p.`project_name` AS `project_name`, "
            f"{self._project_value_expr('p')} AS `project_value`, "
            "COALESCE(b.`gross_bill_value`, 0) AS `gross_bill_value`\n"
            "FROM `project_mst` p\n"
            "LEFT JOIN (\n"
            "  SELECT `project_id`, SUM(`gross_bill_value`) AS `gross_bill_value`\n"
            "  FROM `client_bill_raising`\n"
            "  GROUP BY `project_id`\n"
            ") b ON b.`project_id` = p.`project_id`\n"
            "ORDER BY `gross_bill_value` DESC\n"
            f"LIMIT {int(plan.limit or 20)}"
        )

    def _compile_project_cross_module(self, plan):
        metrics = self._cross_project_metrics(plan)
        if len(metrics) < 2 or not self._cross_project_supported(plan, metrics):
            return None

        question = str(plan.original_question or "").lower()
        select_parts = [
            "p.`project_id` AS `project_id`",
            "p.`project_name` AS `project_name`",
        ]
        joins = []
        if "project_value" in metrics:
            select_parts.append(f"{self._project_value_expr('p')} AS `project_value`")
        if "billing" in metrics:
            select_parts.append("COALESCE(b.`gross_bill_value`, 0) AS `gross_bill_value`")
            joins.append(self._billing_project_aggregate_join(plan))
        if "purchase_value" in metrics:
            select_parts.append("COALESCE(po.`purchase_value`, 0) AS `purchase_value`")
            joins.append(self._po_project_aggregate_join(plan))
        if "material_consumption" in metrics and "material_issue_value" not in metrics:
            select_parts.append("COALESCE(mi.`issued_quantity`, 0) AS `issued_quantity`")
            joins.append(self._store_project_aggregate_join(plan, include_value=False))
        if "material_issue_value" in metrics:
            select_parts.append("COALESCE(mi.`issued_quantity`, 0) AS `issued_quantity`")
            select_parts.append("COALESCE(mi.`issued_value`, 0) AS `issued_value`")
            joins.append(self._store_project_aggregate_join(plan, include_value=True))
        select_parts.extend(self._cross_project_derived_columns(question, metrics))

        joins = list(dict.fromkeys(join for join in joins if join))
        where = self._project_entity_where(plan, "p")
        return (
            "SELECT " + ", ".join(select_parts) + "\n"
            "FROM `project_mst` p\n"
            + "\n".join(joins)
            + where
            + f"\nORDER BY {self._cross_project_order(question, metrics)}\n"
            + f"LIMIT {int(plan.limit or 10)}"
        )

    def _cross_project_metrics(self, plan):
        metric_names = []
        for metric in plan.bound_metrics:
            name = str(metric.get("metric") or metric.get("bound_concept") or "").lower()
            table = metric.get("table")
            column = metric.get("column")
            if name == "project_value" or (table == "project_mst" and column == "project_value"):
                metric_names.append("project_value")
            elif name == "billing" or (table == "client_bill_raising" and column == "gross_bill_value"):
                metric_names.append("billing")
            elif name == "purchase_value" or (table == "po_form" and column == "total_value"):
                metric_names.append("purchase_value")
            elif name in {"material_consumption", "material_issues"} or (table == "store_material_usage_quantity" and column == "issued_quantity"):
                metric_names.append("material_consumption")
            elif name == "material_issue_value":
                metric_names.append("material_issue_value")
        question = str(plan.original_question or "").lower()
        if re.search(
            r"\b(?:po|purchase(?:\s+order)?|procurement)\s+(?:value|amount|spend|spent|cost|total)\b|"
            r"\b(?:procured|purchased)\s+(?:value|amount|spend|cost|total)\b",
            question,
        ):
            metric_names.append("purchase_value")
        if re.search(
            r"\b(?:billing|billed|bill\s+value|bill\s+amount|gross\s+billing|invoice\s+amount|revenue\s+booked)\b",
            question,
        ):
            metric_names.append("billing")
        if re.search(r"\b(?:project|contract|work\s*order|order\s+book)\s+(?:value|amount|worth)\b", question):
            metric_names.append("project_value")
        if re.search(
            r"\bmaterial(?:s)?\b.*\b(?:issued|used|utili[sz]ed|consum|usage)\b|"
            r"\b(?:issued|used|utili[sz]ed|consum|usage)\b.*\bmaterial",
            question,
        ):
            metric_names.append("material_consumption")
        if re.search(
            r"\bmaterial(?:s)?\b.*\b(value|cost|amount|spend|spent)\b|"
            r"\b(?:issue(?:d)?|used|consumed)\s+material\s+(?:value|cost|amount|spend)\b|"
            r"\bissue(?:d)?\s+value\b",
            question,
        ):
            metric_names.append("material_issue_value")
        if (
            re.search(r"\b(?:summary|overview|dashboard|snapshot)\b", question)
            and "project" in question
            and len(set(metric_names) & {"billing", "purchase_value", "material_consumption", "material_issue_value"}) >= 2
        ):
            metric_names.append("project_value")
        return set(metric_names)

    def _cross_project_supported(self, plan, metrics):
        tables = set(plan.generation_tables or plan.selected_tables)
        question = str(plan.original_question or "").lower()
        metric_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in plan.bound_metrics
            if isinstance(item, dict)
        }
        if len(metric_modules & {"billing", "purchase_orders", "stores", "projects"}) < 2:
            return False
        if "project_mst" not in tables:
            return False
        if "billing" in metrics and "client_bill_raising" not in tables:
            return False
        if "purchase_value" in metrics and not {"po_form", "po_items"} <= tables:
            return False
        if metrics & {"material_consumption", "material_issue_value"} and not {
            "store_material_usage_quantity",
            "store_usage_quantity_master",
        } <= tables:
            return False
        if any(str(item.get("entity_type") or "").lower() != "project" for item in plan.resolved_entity_constraints):
            return False
        return bool(
            re.search(r"\b(project|site|sites|projects|summary|compare|vs|versus|against|by project|project-wise)\b", question)
            or any(str(item).lower() == "project" for item in [*plan.dimensions, *plan.grouping, *plan.requested_outputs])
            or any(str(item.get("dimension") or item.get("bound_concept") or "").lower() == "project" for item in plan.bound_dimensions)
            or plan.resolved_entity_constraints
        )

    def _billing_project_aggregate_join(self, plan):
        predicate = self._billing_date_predicate(plan, "b")
        where = f"\n  WHERE {predicate}" if predicate else ""
        return (
            "LEFT JOIN (\n"
            "  SELECT b.`project_id`, SUM(b.`gross_bill_value`) AS `gross_bill_value`\n"
            "  FROM `client_bill_raising` b"
            f"{where}\n"
            "  GROUP BY b.`project_id`\n"
            ") b ON b.`project_id` = p.`project_id`"
        )

    def _po_project_aggregate_join(self, plan):
        predicate = self._po_date_predicate(plan, "f", self._po_date_column(plan))
        where = f"\n  WHERE {predicate}" if predicate else ""
        return (
            "LEFT JOIN (\n"
            "  SELECT x.`po_project_id` AS `project_id`, SUM(f.`total_value`) AS `purchase_value`\n"
            "  FROM (SELECT DISTINCT `po_id`, `po_project_id` FROM `po_items`) x\n"
            "  JOIN `po_form` f ON f.`po_id` = x.`po_id`"
            f"{where}\n"
            "  GROUP BY x.`po_project_id`\n"
            ") po ON po.`project_id` = p.`project_id`"
        )

    def _store_project_aggregate_join(self, plan, *, include_value):
        predicate = self._store_date_predicate(plan)
        where = f"\n  WHERE {predicate}" if predicate else ""
        issued_quantity = self._numeric_column_expr("d", "issued_quantity")
        base_charge = self._numeric_column_expr("d", "base_charge")
        value_select = f", SUM({issued_quantity} * COALESCE({base_charge}, 0)) AS `issued_value`" if include_value else ""
        return (
            "LEFT JOIN (\n"
            f"  SELECT m.`project_id`, SUM({issued_quantity}) AS `issued_quantity`"
            f"{value_select}\n"
            "  FROM `store_material_usage_quantity` d\n"
            "  JOIN `store_usage_quantity_master` m ON d.`store_usage_quantity_master_id` = m.`store_usage_quantity_master_id`"
            f"{where}\n"
            "  GROUP BY m.`project_id`\n"
            ") mi ON mi.`project_id` = p.`project_id`"
        )

    def _cross_project_order(self, question, metrics):
        if re.search(r"\b(high|highest|more|most|top|large|largest|biggest)\b.*\bmaterial\b.*\blow\b.*\bbill|\blow\b.*\bbill\w*\b.*\b(high|highest|more|most|top|large|largest|biggest)\b.*\bmaterial", question):
            if "material_issue_value" in metrics and "billing" in metrics:
                return "`issued_value` DESC, `gross_bill_value` ASC"
            if "material_consumption" in metrics and "billing" in metrics:
                return "`issued_quantity` DESC, `gross_bill_value` ASC"
        if re.search(r"\b(high|highest|more|most|top|large|largest|biggest)\b.*\bbill\w*\b.*\blow\b.*\bpo|\blow\b.*\bpo\b.*\b(high|highest|more|most|top|large|largest|biggest)\b.*\bbill", question):
            if "billing" in metrics and "purchase_value" in metrics:
                return "`gross_bill_value` DESC, `purchase_value` ASC"
        if re.search(r"\b(high|highest|more|most|top|large|largest|biggest)\b.*\bpo\b|\bpo\b.*\b(high|highest|more|most|top|large|largest|biggest)\b", question):
            if re.search(r"\blow\b.*\bbill|\bbill\w*\b.*\blow\b", question) and "billing" in metrics:
                return "`purchase_value` DESC, `gross_bill_value` ASC"
            return "`purchase_value` DESC"
        if re.search(r"\b(high|highest|more|most|top|large|largest|biggest)\b.*\bbill|\bbill\w*\b.*\b(high|highest|more|most|top|large|largest|biggest)\b", question):
            return "`gross_bill_value` DESC"
        if re.search(r"\bmaterial\b.*\b(value|cost|amount)|\bissue(?:d)?\s+value\b", question) and "material_issue_value" in metrics:
            return "`issued_value` DESC"
        if re.search(r"\bmaterial|issued|consum|usage|used\b", question) and "material_consumption" in metrics:
            return "`issued_quantity` DESC"
        if "project_value" in metrics:
            return "`project_value` DESC"
        if "billing" in metrics:
            return "`gross_bill_value` DESC"
        if "purchase_value" in metrics:
            return "`purchase_value` DESC"
        return "p.`project_name` ASC"

    def _cross_project_derived_columns(self, question, metrics):
        if not re.search(r"\b(?:compare|compared|comparison|vs|versus|against|gap|difference|variance)\b", question):
            return []
        columns = []
        billing = "COALESCE(b.`gross_bill_value`, 0)"
        purchase = "COALESCE(po.`purchase_value`, 0)"
        issued_value = "COALESCE(mi.`issued_value`, 0)"
        if {"billing", "purchase_value"} <= metrics:
            columns.append(f"({purchase} - {billing}) AS `purchase_minus_billing_value`")
        if {"billing", "material_issue_value"} <= metrics:
            columns.append(f"({billing} - {issued_value}) AS `billing_minus_material_issue_value`")
        if {"purchase_value", "material_issue_value"} <= metrics:
            columns.append(f"({purchase} - {issued_value}) AS `purchase_minus_material_issue_value`")
        return columns

    def _compile_store(self, plan):
        if not self._is_store_plan(plan):
            return None
        metric = self._store_metric(plan)
        material_dimension = self._dimension_by_name(plan, "material")
        project_dimension = self._dimension_by_name(plan, "project")
        store_issue_dimension = self._dimension_by_name(plan, "store_issue")
        transaction_dimension = (
            self._dimension_by_name(plan, "transaction_type")
            or self._dimension_by_name(plan, "type")
        )
        month_dimension = self._dimension_by_name(plan, "month")
        aggregations = self._aggregation_set(plan)
        operation = str(plan.operation or "").lower()
        question = str(plan.original_question or "").lower()

        if "return" in question:
            return None
        if "cancel" in question:
            return None
        if (
            not metric
            and re.search(r"\bmaterial\s+movement\b|\bstore\s+movement\b", question)
        ):
            return None

        material_master_only = (
            (
                material_dimension
                or str(plan.base_entity or "").lower() == "material"
                or "material" in {
                    str(item or "").lower()
                    for item in [
                        *(plan.requested_outputs or []),
                        *(plan.metrics or []),
                        *(plan.dimensions or []),
                    ]
                }
            )
            and not metric
            and not project_dimension
            and not store_issue_dimension
            and not transaction_dimension
            and set(plan.generation_tables or plan.selected_tables or []) <= {"sub_material"}
        )
        if material_master_only and operation in {"lookup", "relationship_lookup"}:
            return (
                "SELECT sm.`sub_material_name` AS `sub_material_name`\n"
                "FROM `sub_material` sm\n"
                "WHERE NULLIF(TRIM(sm.`sub_material_name`), '') IS NOT NULL\n"
                "ORDER BY sm.`sub_material_name` ASC\n"
                f"LIMIT {int(plan.limit or 10)}"
            )

        if operation in {"count", "existence_check"}:
            if material_master_only and re.search(r"\btell\s+me\b|\blist\b|\bshow\b|\bname\b", question):
                return (
                    "SELECT COUNT(*) OVER () AS `material_count`, "
                    "sm.`sub_material_name` AS `sub_material_name`\n"
                    "FROM `sub_material` sm\n"
                    "WHERE NULLIF(TRIM(sm.`sub_material_name`), '') IS NOT NULL\n"
                    "ORDER BY sm.`sub_material_name` ASC\n"
                    f"LIMIT {int(plan.limit or 50)}"
                )
            if material_master_only:
                return "SELECT COUNT(*) AS `material_count` FROM `sub_material`"
            if store_issue_dimension or re.search(r"\bstore\s+issues?\b|\bissue\s+slips?\b", question):
                return "SELECT COUNT(`store_usage_quantity_master_id`) AS `row_count` FROM `store_usage_quantity_master`"
            return "SELECT COUNT(`store_material_usage_quantity_id`) AS `row_count` FROM `store_material_usage_quantity`"

        if not metric and re.search(r"\b(?:material|issued|usage|consumption|store)\b", question):
            metric = {
                "metric": "material_consumption",
                "table": "store_material_usage_quantity",
                "column": "issued_quantity",
            }
            aggregations = aggregations or {"sum"}

        if not metric:
            return None

        if transaction_dimension or re.search(r"\btransaction\s+type\b", question):
            return (
                "SELECT m.`transactiontype` AS `transactiontype`, COUNT(m.`store_usage_quantity_master_id`) AS `row_count`\n"
                "FROM `store_usage_quantity_master` m\n"
                "GROUP BY m.`transactiontype`\n"
                "ORDER BY `row_count` DESC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if plan.date_constraints and re.search(r"\bshow\s+material\s+issues?\s+in\b", question):
            date_expr = self._date_expr("m", "issued_date")
            where = self._store_where(plan, include_entities=False)
            return (
                "SELECT DATE_FORMAT("
                f"{date_expr}, '%Y-%m') AS `issue_month`, "
                "COUNT(m.`store_usage_quantity_master_id`) AS `issue_count`\n"
                "FROM `store_usage_quantity_master` m"
                f"{where}\n"
                "GROUP BY `issue_month`\n"
                "ORDER BY `issue_month` ASC"
            )

        if self._is_store_monthly_plan(plan, month_dimension):
            date_expr = self._date_expr("m", "issued_date")
            where = self._store_where(plan, include_entities=True)
            metric_expr = self._store_metric_expr(metric)
            alias = self._store_metric_alias(metric)
            return (
                "SELECT DATE_FORMAT("
                f"{date_expr}, '%Y-%m') AS `issue_month`, "
                f"SUM({metric_expr}) AS {self._quote(alias)}\n"
                "FROM `store_material_usage_quantity` d\n"
                "JOIN `store_usage_quantity_master` m ON d.`store_usage_quantity_master_id` = m.`store_usage_quantity_master_id`\n"
                "JOIN `project_mst` p ON m.`project_id` = p.`project_id`\n"
                "JOIN `sub_material` sm ON d.`sub_material_id` = sm.`sub_material_id`"
                f"{where}\n"
                "GROUP BY `issue_month`\n"
                "ORDER BY `issue_month` ASC"
            )

        group_columns = []
        explicit_project_group = bool(re.search(r"\bby\s+(?:project|site)s?\b", question))
        explicit_material_group = bool(re.search(r"\bby\s+(?:material|item|product)s?\b|\btop\s+\d*\s*(?:material|item|product)s?\b|\bmaterials?\s+issued\s+with\s+pricing\b", question))
        if project_dimension or self._store_project_entities(plan):
            group_columns.append(("p", "project_id", "project_name"))
        if (material_dimension or self._store_material_entities(plan)) and (explicit_material_group or not explicit_project_group):
            group_columns.append(("sm", "sub_material_id", "sub_material_name"))
        if transaction_dimension:
            group_columns.append(("m", "transactiontype", "transactiontype"))

        if group_columns or operation in {"ranking", "top_n", "bottom_n", "relationship_lookup", "lookup"}:
            if not group_columns and re.search(r"\bissued\s+quantit", question):
                direction = self._direction(plan)
                return (
                    "SELECT d.`sub_material_id` AS `sub_material_id`, d.`issued_quantity` AS `issued_quantity`\n"
                    "FROM `store_material_usage_quantity` d\n"
                    f"ORDER BY d.`issued_quantity` {direction}\n"
                    f"LIMIT {int(plan.limit or 10)}"
                )
            if not group_columns:
                group_columns = [("sm", "sub_material_id", "sub_material_name")]
            direction = self._direction(plan)
            where = self._store_where(plan, include_entities=True)
            metric_expr = self._store_metric_expr(metric)
            alias = self._store_metric_alias(metric)
            select_parts = [
                f"{alias_name}.{self._quote(display)} AS {self._quote(display)}"
                for alias_name, _pk, display in group_columns
            ]
            if metric.get("metric") == "material_price":
                select_parts.append(f"AVG(d.`base_charge`) AS `average_base_charge`")
                order_expr = "`average_base_charge`"
            elif metric.get("metric") == "material_issue_value":
                select_parts.extend([
                    "SUM(d.`issued_quantity`) AS `issued_quantity`",
                    "AVG(d.`base_charge`) AS `average_base_charge`",
                    f"SUM({metric_expr}) AS `issued_value`",
                ])
                order_expr = "`issued_value`"
            else:
                select_parts.append(f"SUM({metric_expr}) AS {self._quote(alias)}")
                if self._has_bound_metric(plan, "material_price"):
                    select_parts.append("AVG(d.`base_charge`) AS `average_base_charge`")
                order_expr = self._quote(alias)
            group_by = ", ".join(
                f"{alias_name}.{self._quote(pk)}, {alias_name}.{self._quote(display)}"
                if pk != display
                else f"{alias_name}.{self._quote(display)}"
                for alias_name, pk, display in group_columns
            )
            needs_project = (
                any(alias_name == "p" for alias_name, _pk, _display in group_columns)
                or self._store_project_entities(plan)
            )
            needs_header = bool(
                needs_project
                or transaction_dimension
                or plan.date_constraints
                or any(alias_name == "m" for alias_name, _pk, _display in group_columns)
            )
            joins = []
            if needs_header:
                joins.append("JOIN `store_usage_quantity_master` m ON d.`store_usage_quantity_master_id` = m.`store_usage_quantity_master_id`")
            if needs_project:
                joins.append("JOIN `project_mst` p ON m.`project_id` = p.`project_id`")
            if any(alias_name == "sm" for alias_name, _pk, _display in group_columns) or self._store_material_entities(plan):
                joins.append("JOIN `sub_material` sm ON d.`sub_material_id` = sm.`sub_material_id`")
            return (
                "SELECT " + ", ".join(select_parts) + "\n"
                + "FROM `store_material_usage_quantity` d\n"
                + "\n".join(joins)
                + f"{where}\n"
                + f"GROUP BY {group_by}\n"
                + f"ORDER BY {order_expr} {direction}\n"
                + f"LIMIT {int(plan.limit or 10)}"
            )

        if aggregations and aggregations <= self._MIN_MAX:
            parts = []
            metric_expr = self._store_metric_expr(metric)
            alias = self._store_metric_alias(metric)
            if "min" in aggregations:
                parts.append(f"MIN({metric_expr}) AS {self._quote('min_' + alias)}")
            if "max" in aggregations:
                parts.append(f"MAX({metric_expr}) AS {self._quote('max_' + alias)}")
            return (
                "SELECT " + ", ".join(parts) + "\n"
                "FROM `store_material_usage_quantity` d"
            )

        function = "SUM"
        if "avg" in aggregations:
            function = "AVG"
        metric_expr = self._store_metric_expr(metric)
        alias = self._alias(function, self._store_metric_alias(metric))
        where = self._store_where(plan, include_entities=False)
        if plan.date_constraints:
            return (
                f"SELECT {function}({metric_expr}) AS {self._quote(alias)}\n"
                "FROM `store_material_usage_quantity` d\n"
                "JOIN `store_usage_quantity_master` m ON d.`store_usage_quantity_master_id` = m.`store_usage_quantity_master_id`"
                f"{where}"
            )
        return (
            f"SELECT {function}({metric_expr}) AS {self._quote(alias)}\n"
            "FROM `store_material_usage_quantity` d"
            f"{where}"
        )

    def _compile_inventory(self, plan):
        if not self._is_inventory_plan(plan):
            return None
        question = str(plan.original_question or "").lower()
        if re.search(r"\b(?:stock|inventory)\s+value\b|\bvalue\s+of\s+(?:stock|inventory)\b|\baging\b|\bslow[- ]moving\b|\bnon[- ]moving\b|\bfifo\b|\bweighted\b|\breorder\b|\bsafety\s+stock\b|\bproject\s+stock\b|\bstock\s+by\s+project\b|\binventory\s+by\s+project\b", question):
            return None

        material_dimension = self._dimension_by_name(plan, "material")
        store_dimension = (
            self._dimension_by_name(plan, "store")
            or self._dimension_by_name(plan, "warehouse")
        )
        operation = str(plan.operation or "").lower()
        count_question = (
            operation in {"count", "existence_check"}
            or "count" in self._aggregation_set(plan)
            or re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", question)
        )
        having = self._inventory_having(plan)
        where = self._inventory_where(plan)
        direction = self._direction(plan)
        if "zero stock" in question or "out of stock" in question or "below" in question or "less than" in question:
            direction = "ASC"
        limit = int(plan.limit or (20 if count_question else 10))
        aggregations = self._aggregation_set(plan)

        if (
            not material_dimension
            and not store_dimension
            and not self._inventory_material_entities(plan)
            and operation in self._SCALAR_OPERATIONS | {"sum"}
            and (aggregations or operation in {"sum", "aggregation"})
            and re.search(
                r"\bavailable\s+inventory\s+quantity\b|"
                r"\bcurrent\s+stock\b|"
                r"\bavailable\s+stock\b|"
                r"\bstock\s+on\s+hand\b|"
                r"\bon\s+hand\s+quantity\b|"
                r"\binventory\s+balance\b|"
                r"\bstock\s+balance\b|"
                r"\bavailable\s+quantity\b",
                question,
            )
        ):
            if aggregations and aggregations <= self._MIN_MAX:
                parts = []
                if "min" in aggregations:
                    parts.append("MIN(COALESCE(i.`available_quantity`, 0)) AS `min_current_stock`")
                if "max" in aggregations:
                    parts.append("MAX(COALESCE(i.`available_quantity`, 0)) AS `max_current_stock`")
                return "SELECT " + ", ".join(parts) + "\nFROM `inventory_master` i"
            function = "SUM"
            alias = "current_stock"
            if "avg" in aggregations or operation == "average":
                function = "AVG"
                alias = "average_current_stock"
            return (
                f"SELECT {function}(COALESCE(i.`available_quantity`, 0)) AS `{alias}`\n"
                "FROM `inventory_master` i"
            )

        if count_question:
            join = ""
            if "sub_material" in set(plan.generation_tables or plan.selected_tables or []) or "sm." in where:
                join = "\nJOIN `sub_material` sm ON i.`sub_material_id` = sm.`sub_material_id`"
            where_sql = f"\nWHERE {where}" if where else ""
            having_sql = f"\nHAVING {having}" if having else ""
            return (
                "SELECT COUNT(*) AS `material_count`\n"
                "FROM (\n"
                "  SELECT i.`sub_material_id`, SUM(COALESCE(i.`available_quantity`, 0)) AS `current_stock`\n"
                "  FROM `inventory_master` i"
                f"{join}"
                f"{where_sql}\n"
                "  GROUP BY i.`sub_material_id`"
                f"{having_sql}\n"
                ") stock_summary"
            )

        if store_dimension and not material_dimension and not self._inventory_material_entities(plan):
            having_sql = f"\nHAVING {having}" if having else ""
            where_sql = f"\nWHERE {where}" if where else ""
            return (
                "SELECT i.`sub_store_id` AS `sub_store_id`, "
                "SUM(COALESCE(i.`available_quantity`, 0)) AS `current_stock`\n"
                "FROM `inventory_master` i"
                f"{where_sql}\n"
                "GROUP BY i.`sub_store_id`"
                f"{having_sql}\n"
                f"ORDER BY `current_stock` {direction}, i.`sub_store_id` ASC\n"
                f"LIMIT {limit}"
            )

        having_sql = f"\nHAVING {having}" if having else ""
        where_sql = f"\nWHERE {where}" if where else ""
        return (
            "SELECT sm.`sub_material_name` AS `sub_material_name`, "
            "sm.`uom` AS `uom`, "
            "SUM(COALESCE(i.`available_quantity`, 0)) AS `current_stock`\n"
            "FROM `inventory_master` i\n"
            "JOIN `sub_material` sm ON i.`sub_material_id` = sm.`sub_material_id`"
            f"{where_sql}\n"
            "GROUP BY sm.`sub_material_id`, sm.`sub_material_name`, sm.`uom`"
            f"{having_sql}\n"
            f"ORDER BY `current_stock` {direction}, sm.`sub_material_name` ASC\n"
            f"LIMIT {limit}"
        )

    def _is_inventory_plan(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables or [])
        metric_tables = {
            item.get("table")
            for item in plan.bound_metrics
            if isinstance(item, dict)
        }
        dimension_tables = {
            item.get("table")
            for item in plan.bound_dimensions
            if isinstance(item, dict)
        }
        concepts = {
            str(item.get("metric") or item.get("bound_concept") or "").lower()
            for item in plan.bound_metrics
            if isinstance(item, dict)
        }
        modules = {
            str(item.get("applicable_module") or "").lower()
            for item in [*plan.bound_metrics, *plan.bound_dimensions]
            if isinstance(item, dict)
        }
        return bool(
            "inventory_master" in tables
            or "inventory_master" in metric_tables
            or "inventory_master" in dimension_tables
            or "current_stock" in concepts
            or "inventory" in modules
        )

    def _inventory_where(self, plan):
        predicates = []
        for entity in self._inventory_material_entities(plan):
            predicate = self._entity_predicate(entity, table_alias="sm")
            if predicate and predicate not in predicates:
                predicates.append(predicate)
        return " AND ".join(predicates)

    def _inventory_having(self, plan):
        question = str(plan.original_question or "").lower()
        if re.search(r"\bnegative\s+stock\b|\bstock\s+below\s+zero\b", question):
            return "`current_stock` < 0"
        if re.search(r"\bzero\s+stock\b|\bout\s+of\s+stock\b", question):
            return "`current_stock` = 0"
        if re.search(r"\bin\s+stock\b|\bstocked\s+materials?\b|\bavailable\s+stock\b", question):
            return "`current_stock` > 0"
        for item in plan.numeric_constraints or []:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or item.get("metric") or "").lower()
            if target and "stock" not in target and "quantity" not in target:
                continue
            operator = str(item.get("operator") or "").strip()
            value = item.get("value")
            if operator not in {">", ">=", "<", "<=", "=", "!="}:
                operator = {
                    "above": ">",
                    "greater_than": ">",
                    "below": "<",
                    "less_than": "<",
                    "at_least": ">=",
                    "at_most": "<=",
                }.get(operator)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if operator:
                if numeric.is_integer():
                    numeric = int(numeric)
                return f"`current_stock` {operator} {numeric}"
        threshold_match = re.search(r"\b(?:above|greater than|more than)\s+(-?\d+(?:\.\d+)?)\b", question)
        if threshold_match:
            return f"`current_stock` > {threshold_match.group(1)}"
        threshold_match = re.search(r"\b(?:below|less than|under)\s+(-?\d+(?:\.\d+)?)\b", question)
        if threshold_match:
            return f"`current_stock` < {threshold_match.group(1)}"
        return ""

    def _inventory_material_entities(self, plan):
        return [
            item for item in plan.resolved_entity_constraints
            if isinstance(item, dict)
            and str(item.get("table") or "").lower() == "sub_material"
        ]

    def _compile_billing(self, plan):
        if not self._is_billing_plan(plan):
            return None
        if plan.status_constraints:
            return None
        metric = self._billing_metric(plan)
        project_dimension = self._dimension_by_name(plan, "project")
        client_dimension = self._dimension_by_name(plan, "client")
        status_dimension = self._dimension_by_name(plan, "billing_status")
        invoice_dimension = self._dimension_by_name(plan, "invoice")
        aggregations = self._aggregation_set(plan)
        operation = str(plan.operation or "").lower()

        if operation in {"count", "existence_check"} and invoice_dimension:
            return "SELECT COUNT(*) AS `row_count` FROM `client_bill_raising`"

        if invoice_dimension and re.search(r"\b(latest|recent)\b", str(plan.original_question or "").lower()):
            label = "ra_bill_number" if self._column_selected(plan, "client_bill_raising", "ra_bill_number") else "billing_id"
            date_expr = self._date_expr("b", "bill_date")
            return (
                "SELECT "
                "b.`billing_id` AS `billing_id`, "
                f"b.{self._quote(label)} AS {self._quote(label)}, "
                "b.`bill_date` AS `bill_date`, "
                "b.`gross_bill_value` AS `gross_bill_value`, "
                "b.`bill_amount` AS `bill_amount`, "
                "b.`billing_status` AS `billing_status`\n"
                "FROM `client_bill_raising` b\n"
                f"WHERE {date_expr} IS NOT NULL\n"
                f"ORDER BY {date_expr} DESC, b.`billing_id` DESC\n"
                f"LIMIT {int(plan.limit or 10)}"
            )

        if metric and self._is_billing_monthly_plan(plan):
            column = metric.get("column")
            date_expr = self._date_expr("b", "bill_date")
            where = self._billing_date_where(plan, "b")
            return (
                "SELECT DATE_FORMAT("
                f"{date_expr}, '%Y-%m') AS `bill_month`, "
                f"SUM(b.{self._quote(column)}) AS {self._quote(column)}\n"
                "FROM `client_bill_raising` b"
                f"{where}\n"
                "GROUP BY `bill_month`\n"
                "ORDER BY `bill_month` ASC"
            )

        if metric and not any([project_dimension, client_dimension, status_dimension, plan.resolved_entity_constraints]) and not plan.grouping and operation in self._SCALAR_OPERATIONS | {"aggregation"}:
            if aggregations and aggregations <= self._MIN_MAX:
                return self._compile_min_max(plan, list(aggregations))
            function = "SUM"
            if "avg" in aggregations:
                function = "AVG"
            column = metric.get("column")
            alias = column if plan.date_constraints else self._alias(function, metric.get("metric") or column)
            where = self._billing_date_where(plan, "b")
            return (
                f"SELECT {function}(b.{self._quote(column)}) AS {self._quote(alias)}\n"
                f"FROM `client_bill_raising` b"
                f"{where}"
            )

        if status_dimension and "count" in aggregations:
            return (
                "SELECT b.`billing_status` AS `billing_status`, COUNT(*) AS `row_count`\n"
                "FROM `client_bill_raising` b\n"
                "GROUP BY b.`billing_status`\n"
                "ORDER BY `row_count` DESC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if metric and (project_dimension or self._billing_entity_group(plan)):
            return self._compile_billing_grouped(
                plan,
                metric=metric,
                dimension_table="project_mst",
                dimension_column="project_name",
                dimension_pk="project_id",
                relationship=("client_bill_raising", "project_id", "project_mst", "project_id"),
            )

        if metric and client_dimension:
            return self._compile_billing_grouped(
                plan,
                metric=metric,
                dimension_table="project_mst",
                dimension_column="client_name",
                dimension_pk="client_name",
                relationship=("client_bill_raising", "project_id", "project_mst", "project_id"),
            )

        if metric and invoice_dimension and operation in {"ranking", "top_n", "bottom_n"}:
            direction = self._direction(plan)
            column = metric.get("column")
            label = "ra_bill_number" if self._column_selected(plan, "client_bill_raising", "ra_bill_number") else "billing_id"
            where = self._billing_date_where(plan, "b")
            identifier_select = (
                f"b.`billing_id` AS `billing_id`, "
                f"b.{self._quote(label)} AS {self._quote(label)}, "
                if label != "billing_id"
                else "b.`billing_id` AS `billing_id`, "
            )
            return (
                f"SELECT {identifier_select}"
                f"b.{self._quote(column)} AS {self._quote(column)}\n"
                "FROM `client_bill_raising` b"
                f"{where}\n"
                f"ORDER BY b.{self._quote(column)} {direction}\n"
                f"LIMIT {int(plan.limit or 10)}"
            )

        return None

    def _compile_po(self, plan):
        if not self._is_po_plan(plan):
            return None
        metric = self._po_metric(plan)
        po_dimension = self._dimension_by_name(plan, "purchase_order")
        po_item_dimension = self._dimension_by_name(plan, "purchase_order_item")
        vendor_dimension = self._dimension_by_name(plan, "vendor")
        project_dimension = self._dimension_by_name(plan, "project")
        status_dimension = (
            self._dimension_by_name(plan, "purchase_order_status")
            or self._dimension_by_name(plan, "status")
        )
        month_dimension = self._dimension_by_name(plan, "month")
        aggregations = self._aggregation_set(plan)
        operation = str(plan.operation or "").lower()
        question = str(plan.original_question or "").lower()
        if metric is None and (
            po_dimension
            or project_dimension
            or vendor_dimension
            or self._po_project_entities(plan)
            or self._is_po_validity_lookup(plan)
            or plan.date_constraints
        ):
            metric = {
                "metric": "purchase_value",
                "table": "po_form",
                "column": "total_value",
            }

        if operation in {"count", "existence_check"} and po_item_dimension:
            return "SELECT COUNT(*) AS `row_count` FROM `po_items`"

        if operation in {"count", "existence_check"} and po_dimension:
            return "SELECT COUNT(*) AS `row_count` FROM `po_form`"

        if status_dimension and (po_item_dimension or "po item" in question or "items" in question):
            return (
                "SELECT i.`status` AS `status`, COUNT(i.`po_item_id`) AS `row_count`\n"
                "FROM `po_items` i\n"
                "GROUP BY i.`status`\n"
                "ORDER BY `row_count` DESC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if metric and self._is_po_monthly_plan(plan, metric, month_dimension):
            column = metric.get("column")
            alias = "purchase_value" if column == "total_value" else column
            date_column = self._po_date_column(plan)
            date_expr = self._date_expr("f", date_column)
            where = self._po_date_where(plan, "f", date_column)
            return (
                "SELECT DATE_FORMAT("
                f"{date_expr}, '%Y-%m') AS `po_month`, "
                "COUNT(*) AS `purchase_order_count`, "
                f"SUM(f.{self._quote(column)}) AS {self._quote(alias)}\n"
                "FROM `po_form` f"
                f"{where}\n"
                "GROUP BY `po_month`\n"
                "ORDER BY `po_month` ASC"
            )

        if metric and self._is_po_validity_lookup(plan):
            date_expr = self._date_expr("f", "po_validity_date")
            where = self._po_date_where(plan, "f", "po_validity_date")
            return (
                "SELECT f.`po_no` AS `po_no`, f.`vendor_name` AS `vendor_name`, "
                "f.`po_validity_date` AS `po_validity_date`, f.`total_value` AS `total_value`\n"
                "FROM `po_form` f"
                f"{where}\n"
                f"ORDER BY {date_expr} ASC\n"
                f"LIMIT {int(plan.limit or 50)}"
            )

        if metric and project_dimension:
            if metric.get("table") == "po_form":
                return self._compile_po_project_header_group(plan, metric)
            return self._compile_po_item_project_group(plan, metric)

        if metric and self._po_project_entities(plan):
            return self._compile_po_for_project(plan, metric)

        if metric and vendor_dimension:
            if metric.get("table") != "po_form":
                return None
            return self._compile_po_vendor_group(plan, metric)

        if metric and po_dimension and operation in {"ranking", "top_n", "bottom_n", "lookup", "relationship_lookup"}:
            return self._compile_po_rows(plan, metric)

        if metric and po_item_dimension and operation in self._SCALAR_OPERATIONS | {"aggregation"}:
            return self._compile_po_scalar(plan, metric)

        if metric and not any([vendor_dimension, project_dimension, po_dimension, po_item_dimension, status_dimension, plan.resolved_entity_constraints]) and not plan.grouping and operation in self._SCALAR_OPERATIONS | {"aggregation"}:
            return self._compile_po_scalar(plan, metric)

        if metric and operation in {"ranking", "top_n", "bottom_n"} and metric.get("table") == "po_form":
            return self._compile_po_rows(plan, metric)

        return None

    def _compile_po_scalar(self, plan, metric):
        aggregations = self._aggregation_set(plan)
        if aggregations and aggregations <= self._MIN_MAX:
            return self._compile_min_max(plan, list(aggregations))
        function = "SUM"
        if "avg" in aggregations:
            function = "AVG"
        if "count" in aggregations:
            return f"SELECT COUNT(*) AS `row_count` FROM {self._quote(metric.get('table'))}"
        table = metric.get("table")
        column = metric.get("column")
        if table not in {"po_form", "po_items"} or not self._identifier(column):
            return None
        alias = self._alias(function, column)
        where = ""
        if table == "po_form":
            where = self._po_date_where(plan, "f", self._po_date_column(plan))
            return (
                f"SELECT {function}(f.{self._quote(column)}) AS {self._quote(alias)}\n"
                "FROM `po_form` f"
                f"{where}"
            )
        return (
            f"SELECT {function}(i.{self._quote(column)}) AS {self._quote(alias)}\n"
            "FROM `po_items` i"
        )

    def _compile_po_rows(self, plan, metric):
        if metric.get("table") != "po_form":
            return None
        direction = self._direction(plan)
        column = metric.get("column")
        where = self._po_date_where(plan, "f", self._po_date_column(plan))
        order = f"f.{self._quote(column)} {direction}"
        if (
            str(plan.operation or "").lower() in {"lookup", "relationship_lookup"}
            and re.search(r"\b(?:latest|recent|newest)\b", str(plan.original_question or "").lower())
        ):
            date_expr = self._date_expr("f", self._po_date_column(plan))
            order = f"{date_expr} DESC, f.`po_form_id` DESC"
        return (
            "SELECT f.`po_form_id` AS `po_form_id`, f.`po_no` AS `po_no`, f.`vendor_name` AS `vendor_name`, "
            f"f.{self._quote(column)} AS {self._quote(column)}, f.`date` AS `date`\n"
            "FROM `po_form` f"
            f"{where}\n"
            f"ORDER BY {order}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _compile_po_vendor_group(self, plan, metric):
        column = metric.get("column")
        direction = self._direction(plan)
        where_parts = ["NULLIF(TRIM(f.`vendor_name`), '') IS NOT NULL"]
        date_predicate = self._po_date_predicate(plan, "f", self._po_date_column(plan))
        if date_predicate:
            where_parts.append(date_predicate)
        where = "\nWHERE " + " AND ".join(where_parts)
        return (
            "SELECT TRIM(f.`vendor_name`) AS `vendor_name`, "
            f"SUM(f.{self._quote(column)}) AS `purchase_value`\n"
            "FROM `po_form` f"
            f"{where}\n"
            "GROUP BY TRIM(f.`vendor_name`)\n"
            f"ORDER BY `purchase_value` {direction}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _compile_po_project_header_group(self, plan, metric):
        column = metric.get("column")
        direction = self._direction(plan)
        where_parts = []
        date_predicate = self._po_date_predicate(plan, "f", self._po_date_column(plan))
        if date_predicate:
            where_parts.append(date_predicate)
        entity_predicate = self._po_project_predicate(plan, "p")
        if entity_predicate:
            where_parts.append(entity_predicate)
        where = ("\nWHERE " + " AND ".join(where_parts)) if where_parts else ""
        return (
            "SELECT p.`project_name` AS `project_name`, "
            f"SUM(f.{self._quote(column)}) AS `purchase_value`\n"
            "FROM `project_mst` p\n"
            "JOIN (SELECT DISTINCT `po_id`, `po_project_id` FROM `po_items`) x "
            "ON x.`po_project_id` = p.`project_id`\n"
            "JOIN `po_form` f ON f.`po_id` = x.`po_id`"
            f"{where}\n"
            "GROUP BY p.`project_id`, p.`project_name`\n"
            f"ORDER BY `purchase_value` {direction}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _compile_po_item_project_group(self, plan, metric):
        column = metric.get("column")
        direction = self._direction(plan)
        return (
            "SELECT p.`project_name` AS `project_name`, "
            f"SUM(i.{self._quote(column)}) AS {self._quote(column)}\n"
            "FROM `po_items` i\n"
            "JOIN `project_mst` p ON i.`po_project_id` = p.`project_id`\n"
            "GROUP BY p.`project_id`, p.`project_name`\n"
            f"ORDER BY {self._quote(column)} {direction}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _compile_po_for_project(self, plan, metric):
        where_parts = []
        entity_predicate = self._po_project_predicate(plan, "p")
        if entity_predicate:
            where_parts.append(entity_predicate)
        date_predicate = self._po_date_predicate(plan, "f", self._po_date_column(plan))
        if date_predicate:
            where_parts.append(date_predicate)
        where = ("\nWHERE " + " AND ".join(where_parts)) if where_parts else ""
        return (
            "SELECT p.`project_name` AS `project_name`, f.`po_form_id` AS `po_form_id`, f.`po_no` AS `po_no`, "
            "f.`vendor_name` AS `vendor_name`, f.`total_value` AS `total_value`\n"
            "FROM `project_mst` p\n"
            "JOIN (SELECT DISTINCT `po_id`, `po_project_id` FROM `po_items`) x "
            "ON x.`po_project_id` = p.`project_id`\n"
            "JOIN `po_form` f ON f.`po_id` = x.`po_id`"
            f"{where}\n"
            "ORDER BY f.`total_value` DESC\n"
            f"LIMIT {int(plan.limit or 50)}"
        )

    def _compile_vendor(self, plan):
        if not self._is_vendor_plan(plan):
            return None
        question = str(plan.original_question or "").lower()
        metric = self._po_metric(plan)
        vendor_dimension = self._dimension_by_name(plan, "vendor")
        location_dimension = (
            self._dimension_by_name(plan, "location")
            or self._dimension_by_name(plan, "state")
            or self._entity_dimension(plan, "location")
        )
        category_dimension = (
            self._dimension_by_name(plan, "category")
            or self._dimension_by_name(plan, "vendor_category")
            or self._entity_dimension(plan, "vendor_category")
        )
        po_dimension = self._dimension_by_name(plan, "purchase_order")
        aggregations = self._aggregation_set(plan)
        operation = str(plan.operation or "").lower()

        if plan.status_constraints and not plan.bound_status_fields:
            return None

        if location_dimension and operation in {"distribution", "count", "breakdown"}:
            predicate = self._vendor_location_predicate(plan, "b")
            if predicate and operation == "count":
                return (
                    "SELECT COUNT(DISTINCT m.`vendor_id`) AS `vendor_count`\n"
                    "FROM `vendor_mst` m\n"
                    "JOIN `vendor_branch_details` b ON b.`vendor_id` = m.`vendor_id`\n"
                    f"WHERE {predicate}"
                )
            return (
                "SELECT b.`state` AS `state`, COUNT(DISTINCT m.`vendor_id`) AS `vendor_count`\n"
                "FROM `vendor_mst` m\n"
                "JOIN `vendor_branch_details` b ON b.`vendor_id` = m.`vendor_id`\n"
                "WHERE NULLIF(TRIM(b.`state`), '') IS NOT NULL\n"
                "GROUP BY b.`state`\n"
                "ORDER BY `vendor_count` DESC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if category_dimension and operation in {"distribution", "count", "breakdown"}:
            predicate = self._vendor_category_predicate(plan, "t")
            if predicate and operation == "count":
                return (
                    "SELECT COUNT(m.`vendor_id`) AS `vendor_count`\n"
                    "FROM `vendor_mst` m\n"
                    "JOIN `vendor_types` t ON t.`vendor_types_id` = m.`vendor_types_id`\n"
                    f"WHERE {predicate}"
                )
            return (
                "SELECT COALESCE(t.`vendor_type`, 'Unclassified') AS `vendor_category`, "
                "COUNT(m.`vendor_id`) AS `vendor_count`\n"
                "FROM `vendor_mst` m\n"
                "LEFT JOIN `vendor_types` t ON t.`vendor_types_id` = m.`vendor_types_id`\n"
                "GROUP BY COALESCE(t.`vendor_type`, 'Unclassified')\n"
                "ORDER BY `vendor_count` DESC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )

        if po_dimension and self._vendor_entities(plan):
            return self._compile_vendor_purchase_orders(plan)

        if metric and (vendor_dimension or self._vendor_entities(plan)):
            if metric.get("table") != "po_form":
                return None
            if self._vendor_entities(plan):
                return self._compile_vendor_purchase_comparison(plan, metric)
            return self._compile_po_vendor_group(plan, metric)

        if (
            (operation in {"ranking", "top_n", "bottom_n"} or "most purchase order" in question)
            and vendor_dimension
            and not metric
        ):
            return self._compile_vendor_po_count_ranking(plan)

        if operation in {"count", "existence_check"} and vendor_dimension:
            return "SELECT COUNT(*) AS `vendor_count` FROM `vendor_mst`"

        if self._vendor_entities(plan):
            return self._compile_vendor_details(plan)

        if vendor_dimension and operation in {"lookup", "relationship_lookup", "metadata", "unknown"}:
            return (
                "SELECT m.`vendor_id` AS `vendor_id`, m.`vendor_code` AS `vendor_code`, "
                "m.`vendor_name` AS `vendor_name`, m.`contact_person_name` AS `contact_person_name`, "
                "m.`contact_no` AS `contact_no`, m.`email` AS `email`, m.`vendor_gst_no` AS `vendor_gst_no`\n"
                "FROM `vendor_mst` m\n"
                "WHERE NULLIF(TRIM(m.`vendor_name`), '') IS NOT NULL\n"
                "ORDER BY m.`vendor_name` ASC\n"
                f"LIMIT {int(plan.limit or 20)}"
            )
        return None

    def _compile_vendor_details(self, plan):
        predicate = self._vendor_master_predicate(plan, "m")
        if not predicate:
            return None
        return (
            "SELECT m.`vendor_id` AS `vendor_id`, m.`vendor_code` AS `vendor_code`, "
            "m.`vendor_name` AS `vendor_name`, m.`vendor_address` AS `vendor_address`, "
            "m.`contact_person_name` AS `contact_person_name`, m.`contact_no` AS `contact_no`, "
            "m.`email` AS `email`, m.`vendor_gst_no` AS `vendor_gst_no`, "
            "m.`vendor_types_id` AS `vendor_types_id`\n"
            "FROM `vendor_mst` m\n"
            f"WHERE {predicate}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _compile_vendor_purchase_orders(self, plan):
        predicate = self._vendor_po_predicate(plan, "f")
        if not predicate:
            return None
        return (
            "SELECT f.`po_form_id` AS `po_form_id`, f.`po_no` AS `po_no`, "
            "TRIM(f.`vendor_name`) AS `vendor_name`, f.`total_value` AS `total_value`, f.`date` AS `date`\n"
            "FROM `po_form` f\n"
            f"WHERE {predicate}\n"
            "ORDER BY f.`total_value` DESC\n"
            f"LIMIT {int(plan.limit or 50)}"
        )

    def _compile_vendor_purchase_comparison(self, plan, metric):
        predicate = self._vendor_po_predicate(plan, "f")
        if not predicate:
            return None
        direction = self._direction(plan)
        column = metric.get("column")
        return (
            "SELECT TRIM(f.`vendor_name`) AS `vendor_name`, "
            f"SUM(f.{self._quote(column)}) AS `purchase_value`\n"
            "FROM `po_form` f\n"
            f"WHERE {predicate}\n"
            "GROUP BY TRIM(f.`vendor_name`)\n"
            f"ORDER BY `purchase_value` {direction}\n"
            f"LIMIT {int(plan.limit or 20)}"
        )

    def _compile_vendor_po_count_ranking(self, plan):
        direction = self._direction(plan)
        where = "WHERE NULLIF(TRIM(f.`vendor_name`), '') IS NOT NULL"
        return (
            "SELECT TRIM(f.`vendor_name`) AS `vendor_name`, "
            "COUNT(f.`po_form_id`) AS `purchase_order_count`\n"
            "FROM `po_form` f\n"
            f"{where}\n"
            "GROUP BY TRIM(f.`vendor_name`)\n"
            f"ORDER BY `purchase_order_count` {direction}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _vendor_entities(self, plan):
        return [
            item for item in plan.resolved_entity_constraints
            if str(item.get("entity_type") or "").lower() == "vendor"
            and item.get("table") in {"vendor_mst", "po_form"}
        ]

    def _entity_dimension(self, plan, entity_type):
        return next(
            (
                item for item in plan.resolved_entity_constraints
                if str(item.get("entity_type") or "").lower() == entity_type
            ),
            None,
        )

    def _vendor_master_predicate(self, plan, table_alias):
        predicates = []
        ids = []
        values = []
        for entity in self._vendor_entities(plan):
            if entity.get("table") == "vendor_mst" and entity.get("primary_key_column") == "vendor_id" and entity.get("primary_key_value") not in (None, ""):
                ids.append(int(entity.get("primary_key_value")))
                continue
            value = entity.get("value") or entity.get("matched_value")
            if value not in (None, ""):
                values.append(str(value))
        if ids:
            ids = list(dict.fromkeys(ids))
            if len(ids) == 1:
                predicates.append(f"{table_alias}.`vendor_id` = {ids[0]}")
            else:
                predicates.append(
                    f"{table_alias}.`vendor_id` IN ("
                    + ", ".join(str(value) for value in ids)
                    + ")"
                )
        if values:
            escaped = [self._escape(value) for value in dict.fromkeys(values)]
            if len(escaped) == 1:
                predicates.append(
                    f"TRIM({table_alias}.`vendor_name`) = '{escaped[0]}'"
                )
            else:
                predicates.append(
                    f"TRIM({table_alias}.`vendor_name`) IN ("
                    + ", ".join(f"'{value}'" for value in escaped)
                    + ")"
                )
        return " AND ".join(dict.fromkeys(predicates))

    def _vendor_po_predicate(self, plan, table_alias):
        values = []
        for entity in self._vendor_entities(plan):
            value = entity.get("value") or entity.get("matched_value")
            if value not in (None, ""):
                values.append(str(value))
        if not values:
            return ""
        escaped = [self._escape(value) for value in dict.fromkeys(values)]
        if len(escaped) == 1:
            return f"TRIM({table_alias}.`vendor_name`) = '{escaped[0]}'"
        return (
            f"TRIM({table_alias}.`vendor_name`) IN ("
            + ", ".join(f"'{value}'" for value in escaped)
            + ")"
        )

    def _vendor_location_predicate(self, plan, table_alias):
        values_by_column = {}
        for entity in plan.resolved_entity_constraints:
            if str(entity.get("entity_type") or "").lower() != "location":
                continue
            if entity.get("table") != "vendor_branch_details":
                continue
            column = entity.get("column")
            if column not in {"state", "country"}:
                continue
            value = entity.get("value") or entity.get("matched_value")
            if value not in (None, ""):
                values_by_column.setdefault(column, []).append(str(value))
        if not values_by_column:
            return ""
        predicates = []
        for column, values in values_by_column.items():
            escaped = [self._escape(value) for value in dict.fromkeys(values)]
            if len(escaped) == 1:
                predicates.append(f"TRIM({table_alias}.{self._quote(column)}) = '{escaped[0]}'")
            else:
                predicates.append(
                    f"TRIM({table_alias}.{self._quote(column)}) IN ("
                    + ", ".join(f"'{value}'" for value in escaped)
                    + ")"
                )
        return " AND ".join(predicates)

    def _vendor_category_predicate(self, plan, table_alias):
        values = []
        for entity in plan.resolved_entity_constraints:
            if str(entity.get("entity_type") or "").lower() not in {"vendor_category", "category"}:
                continue
            if entity.get("table") != "vendor_types":
                continue
            value = entity.get("value") or entity.get("matched_value")
            if value not in (None, ""):
                values.append(str(value))
        if not values:
            return ""
        escaped = [self._escape(value) for value in dict.fromkeys(values)]
        if len(escaped) == 1:
            return f"TRIM({table_alias}.`vendor_type`) = '{escaped[0]}'"
        return (
            f"TRIM({table_alias}.`vendor_type`) IN ("
            + ", ".join(f"'{value}'" for value in escaped)
            + ")"
        )

    def _is_project_plan(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables)
        concepts = {
            str(item).lower()
            for item in [*plan.metrics, *plan.dimensions, *plan.requested_outputs, *plan.grouping]
            if item
        }
        metric_tables = {
            item.get("table")
            for item in plan.bound_metrics
            if item.get("table")
        }
        metric_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in plan.bound_metrics
            if item.get("applicable_module")
        }
        if metric_modules & {"stores", "billing", "purchase_orders"}:
            return False
        if metric_tables & {"store_material_usage_quantity", "client_bill_raising", "po_form", "po_items"}:
            return False
        dimension_tables = {
            item.get("table")
            for item in plan.bound_dimensions
            if item.get("table")
        }
        return bool(
            (
                "project_mst" in tables
                and not (tables & {"store_material_usage_quantity", "store_usage_quantity_master", "client_bill_raising", "po_form", "po_items"})
            )
            or "project_mst" in metric_tables
            or "project_mst" in dimension_tables
            or concepts & {"project", "projects", "project_value", "project_status", "client", "location", "state"}
        )

    def _project_metric(self, plan):
        for metric in plan.bound_metrics:
            if metric.get("table") == "project_mst" and metric.get("column") == "project_value":
                return metric
        return None

    def _workorder_metric(self, plan):
        for metric in plan.bound_metrics:
            if metric.get("table") == "subcontract_workorder_details" and metric.get("column") == "after_taxes_grandtotal":
                return metric
        return None

    def _project_value_expr(self, table_alias):
        quoted = f"{table_alias}.`project_value`"
        return f"CAST(NULLIF(REPLACE(TRIM({quoted}), ',', ''), '') AS DECIMAL(20,2))"

    def _project_entities(self, plan):
        return [
            item for item in plan.resolved_entity_constraints
            if item.get("table") == "project_mst"
            and str(item.get("entity_type") or "").lower() == "project"
        ]

    def _project_entity_where(self, plan, table_alias):
        predicates = []
        values = []
        for entity in self._project_entities(plan):
            primary_key = entity.get("primary_key_column")
            primary_value = entity.get("primary_key_value")
            if primary_key == "project_id" and primary_value not in (None, ""):
                values.append(int(primary_value))
                continue
            predicate = self._entity_predicate(entity, table_alias=table_alias)
            if predicate:
                predicates.append(predicate)
        if values:
            values = list(dict.fromkeys(values))
            if len(values) == 1:
                predicates.append(f"{table_alias}.`project_id` = {values[0]}")
            else:
                predicates.append(
                    f"{table_alias}.`project_id` IN ("
                    + ", ".join(str(value) for value in values)
                    + ")"
                )
        return ("\nWHERE " + " AND ".join(dict.fromkeys(predicates))) if predicates else ""

    def _compile_project_lookup(self, plan):
        where = self._project_entity_where(plan, "p")
        if not where:
            return None
        return (
            "SELECT p.`project_id` AS `project_id`, p.`project_name` AS `project_name`, "
            "p.`client_name` AS `client_name`, p.`project_state` AS `project_state`, "
            "p.`project_location` AS `project_location`, "
            f"{self._project_value_expr('p')} AS `project_value`, "
            "p.`project_start_date` AS `project_start_date`, p.`project_end_date` AS `project_end_date`, "
            "p.`status` AS `status`\n"
            "FROM `project_mst` p"
            f"{where}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _project_date_column(self, plan):
        question = str(plan.original_question or "").lower()
        for item in plan.bound_date_fields:
            if item.get("table") == "project_mst" and item.get("column") in {"project_start_date", "project_end_date"}:
                return item.get("column")
        if re.search(r"\b(?:end|ending|finish|completion|complete)\b", question):
            return "project_end_date"
        return "project_start_date"

    def _project_date_where(self, plan, table_alias, column):
        predicate = self._project_date_predicate(plan, table_alias, column)
        return f"\nWHERE {predicate}" if predicate else ""

    def _project_date_predicate(self, plan, table_alias, column):
        if not plan.date_constraints:
            return ""
        expr = self._date_expr(table_alias, column)
        parts = []
        for item in plan.date_constraints:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            end = item.get("end")
            if start and end:
                parts.append(f"{expr} BETWEEN '{self._escape(start)}' AND '{self._escape(end)}'")
            elif item.get("value") and re.fullmatch(r"\d{4}", str(item.get("value"))):
                year = int(item["value"])
                parts.append(f"{expr} BETWEEN '{year}-01-01' AND '{year}-12-31'")
        return " AND ".join(parts)

    def _is_project_monthly_plan(self, plan, month_dimension):
        question = str(plan.original_question or "").lower()
        values = {
            str(item).lower()
            for item in [*plan.grouping, *plan.dimensions, *plan.requested_outputs]
            if item
        }
        return bool(month_dimension or "month" in values or "financial year" in question or re.search(r"\bby\s+(?:year|month)\b", question))

    def _is_store_plan(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables)
        concepts = {
            str(item).lower()
            for item in [*plan.metrics, *plan.dimensions, *plan.requested_outputs, *plan.grouping]
            if item
        }
        metric_tables = {
            item.get("table")
            for item in plan.bound_metrics
            if item.get("table")
        }
        dimension_tables = {
            item.get("table")
            for item in plan.bound_dimensions
            if item.get("table")
        }
        store_tables = {"store_material_usage_quantity", "store_usage_quantity_master", "sub_material"}
        return bool(
            tables & store_tables
            or metric_tables & store_tables
            or dimension_tables & store_tables
            or concepts & {"material_consumption", "requested_quantity", "material_price", "material_issue_value", "material", "store_issue"}
        )

    def _store_metric(self, plan):
        for metric in plan.bound_metrics:
            if metric.get("table") == "store_material_usage_quantity" and metric.get("column") in {"issued_quantity", "requested_quantity", "base_charge"}:
                return metric
        return None

    def _has_bound_metric(self, plan, metric_name):
        return any(
            str(metric.get("metric") or metric.get("bound_concept") or "").lower()
            == metric_name
            for metric in plan.bound_metrics
            if isinstance(metric, dict)
        )

    def _store_metric_expr(self, metric):
        name = str(metric.get("metric") or "").lower()
        column = metric.get("column")
        if name == "material_issue_value":
            return (
                f"{self._numeric_column_expr('d', 'issued_quantity')} * "
                f"{self._numeric_column_expr('d', 'base_charge')}"
            )
        if name == "requested_vs_issued_difference":
            return (
                f"{self._numeric_column_expr('d', 'requested_quantity')} - "
                f"{self._numeric_column_expr('d', 'issued_quantity')}"
            )
        if column == "base_charge":
            return self._numeric_column_expr("d", "base_charge")
        return f"d.{self._quote(column)}"

    def _numeric_column_expr(self, table_alias, column):
        quoted = f"{table_alias}.{self._quote(column)}"
        return f"CAST(NULLIF(REPLACE(TRIM({quoted}), ',', ''), '') AS DECIMAL(20,4))"

    def _store_metric_alias(self, metric):
        name = str(metric.get("metric") or "").lower()
        if name == "material_consumption":
            return "issued_quantity"
        if name == "material_issue_value":
            return "issued_value"
        if name == "requested_vs_issued_difference":
            return "requested_issued_difference"
        return metric.get("column") or name or "metric_value"

    def _store_project_entities(self, plan):
        return [
            item for item in plan.resolved_entity_constraints
            if item.get("table") == "project_mst"
            and str(item.get("entity_type") or "").lower() == "project"
        ]

    def _store_material_entities(self, plan):
        return [
            item for item in plan.resolved_entity_constraints
            if item.get("table") == "sub_material"
            and str(item.get("entity_type") or "").lower() in {"material", "product", "item"}
        ]

    def _store_where(self, plan, include_entities=True):
        predicates = []
        date_predicate = self._store_date_predicate(plan)
        if date_predicate:
            predicates.append(date_predicate)
        if include_entities:
            project_predicate = self._store_project_predicate(plan, "p")
            if project_predicate:
                predicates.append(project_predicate)
            material_predicate = self._store_material_predicate(plan, "sm")
            if material_predicate:
                predicates.append(material_predicate)
        return ("\nWHERE " + " AND ".join(dict.fromkeys(predicates))) if predicates else ""

    def _store_date_predicate(self, plan):
        if not plan.date_constraints:
            return ""
        expr = self._date_expr("m", "issued_date")
        parts = []
        for item in plan.date_constraints:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            end = item.get("end")
            if start and end:
                parts.append(f"{expr} BETWEEN '{self._escape(start)}' AND '{self._escape(end)}'")
            elif item.get("value") and re.fullmatch(r"\d{4}", str(item.get("value"))):
                year = int(item["value"])
                parts.append(f"{expr} BETWEEN '{year}-01-01' AND '{year}-12-31'")
            elif item.get("type") == "last_days" and item.get("value"):
                days = int(item.get("value"))
                max_date = "SELECT MAX(COALESCE(STR_TO_DATE(NULLIF(TRIM(`issued_date`), ''), '%d-%m-%Y'), STR_TO_DATE(NULLIF(TRIM(`issued_date`), ''), '%d-%m-%y'))) FROM `store_usage_quantity_master`"
                parts.append(f"{expr} BETWEEN DATE_SUB(({max_date}), INTERVAL {days} DAY) AND ({max_date})")
        return " AND ".join(parts)

    def _store_project_predicate(self, plan, table_alias):
        values = []
        predicates = []
        for entity in self._store_project_entities(plan):
            primary_key = entity.get("primary_key_column")
            primary_value = entity.get("primary_key_value")
            if primary_key == "project_id" and primary_value not in (None, ""):
                values.append(int(primary_value))
                continue
            predicate = self._entity_predicate(entity, table_alias=table_alias)
            if predicate:
                predicates.append(predicate)
        if values:
            values = list(dict.fromkeys(values))
            if len(values) == 1:
                predicates.append(f"{table_alias}.`project_id` = {values[0]}")
            else:
                predicates.append(
                    f"{table_alias}.`project_id` IN ("
                    + ", ".join(str(value) for value in values)
                    + ")"
                )
        return " AND ".join(dict.fromkeys(predicates))

    def _store_material_predicate(self, plan, table_alias):
        values = []
        predicates = []
        for entity in self._store_material_entities(plan):
            primary_key = entity.get("primary_key_column")
            primary_value = entity.get("primary_key_value")
            if primary_key == "sub_material_id" and primary_value not in (None, ""):
                values.append(int(primary_value))
                continue
            predicate = self._entity_predicate(entity, table_alias=table_alias)
            if predicate:
                predicates.append(predicate)
        if values:
            values = list(dict.fromkeys(values))
            if len(values) == 1:
                predicates.append(f"{table_alias}.`sub_material_id` = {values[0]}")
            else:
                predicates.append(
                    f"{table_alias}.`sub_material_id` IN ("
                    + ", ".join(str(value) for value in values)
                    + ")"
                )
        return " AND ".join(dict.fromkeys(predicates))

    def _is_store_monthly_plan(self, plan, month_dimension):
        question = str(plan.original_question or "").lower()
        values = {
            str(item).lower()
            for item in [*plan.grouping, *plan.dimensions, *plan.requested_outputs]
            if item
        }
        return bool(month_dimension or "month" in values or "financial year" in question or re.search(r"\bby\s+(?:year|month)\b", question))

    def _is_po_plan(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables)
        concepts = {
            str(item).lower()
            for item in [*plan.metrics, *plan.dimensions, *plan.requested_outputs, *plan.grouping]
            if item
        }
        metric_tables = {
            item.get("table")
            for item in plan.bound_metrics
            if item.get("table")
        }
        dimension_tables = {
            item.get("table")
            for item in plan.bound_dimensions
            if item.get("table")
        }
        return bool(
            tables & {"po_form", "po_items"}
            or metric_tables & {"po_form", "po_items"}
            or dimension_tables & {"po_form", "po_items"}
            or concepts & {"purchase_value", "purchase_order", "purchase_order_item", "purchase_order_subtotal", "purchase_order_item_value", "ordered_quantity"}
        )

    def _is_vendor_plan(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables)
        concepts = {
            str(item).lower()
            for item in [*plan.metrics, *plan.dimensions, *plan.requested_outputs, *plan.grouping]
            if item
        }
        metric_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in plan.bound_metrics
            if item.get("applicable_module")
        }
        dimension_modules = {
            str(item.get("applicable_module") or "").lower()
            for item in plan.bound_dimensions
            if item.get("applicable_module")
        }
        entity_types = {
            str(item.get("entity_type") or "").lower()
            for item in plan.resolved_entity_constraints
            if item.get("entity_type")
        }
        return bool(
            tables & {"vendor_mst", "vendor_branch_details", "vendor_material_details", "vendor_types"}
            or concepts & {"vendor", "vendor_branch", "vendor_material", "vendor_category"}
            or metric_modules & {"vendors"}
            or dimension_modules & {"vendors"}
            or "vendor" in entity_types
        )

    def _po_metric(self, plan):
        for metric in plan.bound_metrics:
            if metric.get("table") in {"po_form", "po_items"} and metric.get("column") in {"total_value", "sub_total", "total_price", "qty"}:
                return metric
        return None

    def _is_po_monthly_plan(self, plan, metric, month_dimension):
        if metric.get("table") != "po_form":
            return False
        question = str(plan.original_question or "").lower()
        values = {
            str(item).lower()
            for item in [*plan.grouping, *plan.dimensions, *plan.requested_outputs]
            if item
        }
        return (
            month_dimension is not None
            or "month" in values
            or "financial year" in question
            or (
                bool(plan.date_constraints)
                and re.search(r"\b(?:show|list|display)\s+purchase\s+orders?\s+in\s+\d{4}\b", question)
            )
        )

    def _is_po_validity_lookup(self, plan):
        question = str(plan.original_question or "").lower()
        return bool(re.search(r"\b(?:validity|valid|expir(?:y|ing|e|es)|ending)\b", question))

    def _po_date_column(self, plan):
        for item in plan.bound_date_fields:
            if item.get("table") == "po_form" and item.get("column") in {"date", "po_validity_date"}:
                return item.get("column")
        return "po_validity_date" if self._is_po_validity_lookup(plan) else "date"

    def _po_date_where(self, plan, table_alias, column):
        predicate = self._po_date_predicate(plan, table_alias, column)
        return f"\nWHERE {predicate}" if predicate else ""

    def _po_date_predicate(self, plan, table_alias, column):
        if not plan.date_constraints:
            return ""
        expr = self._date_expr(table_alias, column)
        parts = []
        for item in plan.date_constraints:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            end = item.get("end")
            if start and end:
                parts.append(f"{expr} BETWEEN '{self._escape(start)}' AND '{self._escape(end)}'")
            elif item.get("value") and re.fullmatch(r"\d{4}", str(item.get("value"))):
                year = int(item["value"])
                parts.append(f"{expr} BETWEEN '{year}-01-01' AND '{year}-12-31'")
        return " AND ".join(parts)

    def _po_project_entities(self, plan):
        return [
            item for item in plan.resolved_entity_constraints
            if item.get("table") == "project_mst"
            and str(item.get("entity_type") or "").lower() == "project"
        ]

    def _po_project_predicate(self, plan, table_alias):
        values = []
        predicates = []
        for entity in self._po_project_entities(plan):
            primary_key = entity.get("primary_key_column")
            primary_value = entity.get("primary_key_value")
            if primary_key == "project_id" and primary_value not in (None, ""):
                values.append(int(primary_value))
                continue
            predicate = self._entity_predicate(entity, table_alias=table_alias)
            if predicate:
                predicates.append(predicate)
        if values:
            values = list(dict.fromkeys(values))
            if len(values) == 1:
                predicates.append(f"{table_alias}.`project_id` = {values[0]}")
            else:
                predicates.append(
                    f"{table_alias}.`project_id` IN ("
                    + ", ".join(str(value) for value in values)
                    + ")"
                )
        return " AND ".join(dict.fromkeys(predicates))

    def _compile_billing_grouped(
        self,
        plan,
        *,
        metric,
        dimension_table,
        dimension_column,
        dimension_pk,
        relationship,
    ):
        column = metric.get("column")
        direction = self._direction(plan)
        limit = int(plan.limit or 10)
        metric_alias = column
        where_parts = []
        date_predicate = self._billing_date_predicate(plan, "b")
        if date_predicate:
            where_parts.append(date_predicate)
        entity_predicate = self._billing_entity_predicate(plan, "p")
        if entity_predicate:
            where_parts.append(entity_predicate)
        where = ("\nWHERE " + " AND ".join(where_parts)) if where_parts else ""
        return (
            f"SELECT p.{self._quote(dimension_column)} AS {self._quote(dimension_column)}, "
            f"SUM(b.{self._quote(column)}) AS {self._quote(metric_alias)}\n"
            "FROM `client_bill_raising` b\n"
            "JOIN `project_mst` p ON b.`project_id` = p.`project_id`"
            f"{where}\n"
            f"GROUP BY p.{self._quote(dimension_pk)}, p.{self._quote(dimension_column)}\n"
            f"ORDER BY {self._quote(metric_alias)} {direction}\n"
            f"LIMIT {limit}"
        )

    def _is_billing_plan(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables)
        concepts = {
            str(item).lower()
            for item in [*plan.metrics, *plan.dimensions, *plan.requested_outputs, *plan.grouping]
            if item
        }
        metric_tables = {
            item.get("table")
            for item in plan.bound_metrics
            if item.get("table")
        }
        dimension_tables = {
            item.get("table")
            for item in plan.bound_dimensions
            if item.get("table")
        }
        return (
            "client_bill_raising" in tables
            or "client_bill_raising" in metric_tables
            or "client_bill_raising" in dimension_tables
            or bool(concepts & {"billing", "invoice", "billing_status"})
        )

    def _billing_metric(self, plan):
        for metric in plan.bound_metrics:
            if metric.get("table") == "client_bill_raising" and metric.get("column") in {"gross_bill_value", "bill_amount"}:
                return metric
        return None

    def _dimension_by_name(self, plan, name):
        for dimension in plan.bound_dimensions:
            names = {
                str(dimension.get(key) or "").lower()
                for key in ("dimension", "bound_concept", "requested_concept")
                if dimension.get(key)
            }
            if name in names:
                return dimension
        return None

    def _billing_entity_group(self, plan):
        return len(plan.resolved_entity_constraints) > 1 or any(
            str(item.get("entity_type") or "").lower() == "project"
            for item in plan.resolved_entity_constraints
        )

    def _billing_entity_predicate(self, plan, table_alias):
        predicates = []
        values = []
        for entity in plan.resolved_entity_constraints:
            if entity.get("table") != "project_mst":
                continue
            primary_key = entity.get("primary_key_column")
            primary_value = entity.get("primary_key_value")
            if primary_key == "project_id" and primary_value not in (None, ""):
                values.append(int(primary_value))
                continue
            predicate = self._entity_predicate(entity, table_alias=table_alias)
            if predicate:
                predicates.append(predicate)
        if values:
            values = list(dict.fromkeys(values))
            if len(values) == 1:
                predicates.append(f"{table_alias}.`project_id` = {values[0]}")
            else:
                predicates.append(
                    f"{table_alias}.`project_id` IN ("
                    + ", ".join(str(value) for value in values)
                    + ")"
                )
        return " AND ".join(dict.fromkeys(predicates))

    def _is_billing_monthly_plan(self, plan):
        values = {
            str(item).lower()
            for item in [*plan.grouping, *plan.dimensions, *plan.requested_outputs]
            if item
        }
        question = str(plan.original_question or "").lower()
        return (
            "month" in values
            or ("date" in values and plan.date_constraints)
            or (
                bool(plan.date_constraints)
                and re.search(r"\b(?:show|list|display)\s+billing\s+in\s+\d{4}\b", question)
            )
            or "financial year" in question
        )

    def _billing_date_where(self, plan, table_alias):
        predicate = self._billing_date_predicate(plan, table_alias)
        return f"\nWHERE {predicate}" if predicate else ""

    def _billing_date_predicate(self, plan, table_alias):
        if not plan.date_constraints:
            return ""
        expr = self._date_expr(table_alias, "bill_date")
        parts = []
        for item in plan.date_constraints:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            end = item.get("end")
            if start and end:
                parts.append(f"{expr} BETWEEN '{self._escape(start)}' AND '{self._escape(end)}'")
            elif item.get("value") and re.fullmatch(r"\d{4}", str(item.get("value"))):
                year = int(item["value"])
                parts.append(f"{expr} BETWEEN '{year}-01-01' AND '{year}-12-31'")
        return " AND ".join(parts)

    def _date_expr(self, table_alias, column):
        quoted = f"{table_alias}.{self._quote(column)}"
        return (
            f"COALESCE(STR_TO_DATE(NULLIF(TRIM({quoted}), ''), '%d-%m-%Y'), "
            f"STR_TO_DATE(NULLIF(TRIM({quoted}), ''), '%d-%m-%y'))"
        )

    def _column_selected(self, plan, table, column):
        return column in (plan.generation_columns or plan.selected_columns).get(table, [])

    def _is_entity_lookup(self, plan):
        entity_tables = {
            item.get("table")
            for item in plan.resolved_entity_constraints
            if item.get("table")
        }
        entity_types = {
            str(item.get("entity_type") or "").lower()
            for item in plan.resolved_entity_constraints
            if item.get("entity_type")
        }
        other_dimension_tables = {
            item.get("table")
            for item in plan.bound_dimensions
            if item.get("table")
            and item.get("table") not in entity_tables
            and str(item.get("dimension") or item.get("bound_concept") or "").lower()
            not in entity_types
        }
        return (
            plan.resolved_entity_constraints
            and not plan.bound_metrics
            and not plan.aggregation
            and not other_dimension_tables
            and not plan.date_constraints
            and not plan.status_constraints
            and plan.operation in {"lookup", "relationship_lookup", "metadata", "unknown", "general_complex_sql"}
        )

    def _compile_entity_lookup(self, plan):
        entity = plan.resolved_entity_constraints[0]
        table = entity.get("table")
        column = entity.get("column")
        if not self._identifier(table) or not self._identifier(column):
            return None
        selected = self._lookup_columns(plan, table, entity)
        if not selected:
            return None
        where = self._entity_predicate(entity, table_alias="")
        if not where:
            return None
        return (
            f"SELECT {', '.join(self._quote(column) for column in selected)}\n"
            f"FROM {self._quote(table)}\n"
            f"WHERE {where}\n"
            f"LIMIT {int(plan.limit or 10)}"
        )

    def _lookup_columns(self, plan, table, entity):
        preferred = list((plan.generation_columns or plan.selected_columns).get(table) or [])
        primary = entity.get("primary_key_column")
        entity_column = entity.get("column")
        defaults = {
            "project_mst": [
                "project_id",
                "project_name",
                "client_name",
                "project_value",
                "project_start_date",
                "project_end_date",
                "status",
            ],
            "employee_table": [
                "employee_id",
                "emp_name",
                "department_id",
                "sal_ctc",
                "emp_status",
            ],
        }.get(table, [])
        return [
            column
            for column in dict.fromkeys([primary, entity_column, *preferred, *defaults])
            if self._identifier(column)
        ][:12]

    def _primary_dimension(self, plan):
        if plan.bound_dimensions:
            desired_order = [
                str(item).strip().lower()
                for item in [*plan.grouping, *plan.requested_outputs, *plan.dimensions]
                if item
            ]
            desired_rank = {
                value: index
                for index, value in enumerate(desired_order)
            }
            ranked = sorted(
                plan.bound_dimensions,
                key=lambda item: (
                    desired_rank.get(
                        str(item.get("dimension") or item.get("bound_concept") or "").lower(),
                        999,
                    ),
                    -self._confidence(item),
                ),
            )
            return ranked[0]
        return None

    def _lookup_select_columns(self, plan, dimension_table, entity_table):
        columns = plan.generation_columns or plan.selected_columns
        defaults = {
            "po_form": ["po_form_id", "po_no", "vendor_name", "total_value", "date"],
            "vendor_mst": ["vendor_id", "vendor_code", "vendor_name", "vendor_address", "contact_person_name", "contact_no", "email", "vendor_gst_no", "vendor_types_id"],
            "vendor_branch_details": ["vendor_branch_details_id", "vendor_id", "state", "contact_person", "contact_number", "gst_no"],
            "vendor_types": ["vendor_types_id", "vendor_type"],
            "project_mst": ["project_id", "project_name", "client_name", "project_value", "project_start_date", "project_end_date", "status"],
            "client_bill_raising": ["billing_id", "project_id", "bill_date", "gross_bill_value", "bill_amount", "billing_status"],
        }
        selected = []
        for table in [entity_table, dimension_table]:
            for column in [*(columns.get(table) or []), *defaults.get(table, [])]:
                if self._identifier(column):
                    pair = (table, column)
                    if pair not in selected:
                        selected.append(pair)
        return selected[:12]

    def _aggregation_set(self, plan):
        aliases = {
            "average": "avg",
            "minimum": "min",
            "maximum": "max",
            "total": "sum",
        }
        return {
            aliases.get(str(item).strip().lower(), str(item).strip().lower())
            for item in plan.aggregation
            if item
        }

    def _tables_for_plan(self, plan):
        metric_tables = [
            item.get("table")
            for item in plan.bound_metrics
            if self._identifier(item.get("table"))
        ]
        dimension_tables = [
            item.get("table")
            for item in plan.bound_dimensions
            if self._identifier(item.get("table"))
        ]
        entity_tables = [
            item.get("table")
            for item in plan.resolved_entity_constraints
            if self._identifier(item.get("table"))
        ]
        relationship_tables = []
        for item in plan.approved_relationships:
            relationship_tables.extend([item.get("left_table"), item.get("right_table")])
        return list(dict.fromkeys([
            *metric_tables,
            *relationship_tables,
            *dimension_tables,
            *entity_tables,
        ]))

    def _aliases(self, tables):
        return {table: f"t{index}" for index, table in enumerate(tables)}

    def _join_sql(self, tables, relationships, aliases):
        if len(tables) == 1:
            return ""
        joined = {tables[0]}
        pending = set(tables[1:])
        lines = []
        rels = list(relationships or [])
        while pending:
            matched = None
            for rel in rels:
                left = rel.get("left_table")
                right = rel.get("right_table")
                if left in joined and right in pending:
                    matched = (right, left, right, rel.get("left_column"), rel.get("right_column"))
                    break
                if right in joined and left in pending:
                    matched = (left, right, left, rel.get("right_column"), rel.get("left_column"))
                    break
            if not matched:
                return None
            next_table, joined_table, pending_table, joined_column, pending_column = matched
            if not all(
                self._identifier(value)
                for value in [next_table, joined_table, pending_table, joined_column, pending_column]
            ):
                return None
            lines.append(
                "\nJOIN "
                f"{self._quote(next_table)} {aliases[next_table]} ON "
                f"{aliases[joined_table]}.{self._quote(joined_column)} = "
                f"{aliases[pending_table]}.{self._quote(pending_column)}"
            )
            joined.add(next_table)
            pending.remove(next_table)
        return "".join(lines)

    def _where_sql(self, plan, aliases):
        predicates = []
        for entity in plan.resolved_entity_constraints:
            table = entity.get("table")
            if table not in aliases:
                return None
            predicate = self._entity_predicate(entity, table_alias=aliases[table])
            if not predicate:
                return None
            if predicate not in predicates:
                predicates.append(predicate)
        return ("\nWHERE " + " AND ".join(predicates)) if predicates else ""

    def _entity_predicate(self, entity, table_alias):
        prefix = f"{table_alias}." if table_alias else ""
        primary_key = entity.get("primary_key_column")
        primary_value = entity.get("primary_key_value")
        if self._identifier(primary_key) and primary_value not in (None, ""):
            if re.fullmatch(r"\d+", str(primary_value)):
                return f"{prefix}{self._quote(primary_key)} = {int(primary_value)}"
            return f"{prefix}{self._quote(primary_key)} = '{self._escape(primary_value)}'"
        column = entity.get("column")
        value = entity.get("value") or entity.get("original_text")
        if not self._identifier(column) or value in (None, ""):
            return None
        return f"{prefix}{self._quote(column)} = '{self._escape(value)}'"

    def _uses_aggregate(self, plan):
        return any(
            str(item).strip().lower()
            in {"sum", "total", "count", "average", "avg", "minimum", "min", "maximum", "max"}
            for item in plan.aggregation
        )

    def _direction(self, plan):
        if re.search(r"\b(?:bottom|lowest|least|minimum|min)\b", str(plan.original_question or "").lower()):
            return "ASC"
        for item in plan.ordering:
            if isinstance(item, dict) and str(item.get("direction") or "").lower() == "asc":
                return "ASC"
        if plan.operation == "bottom_n":
            return "ASC"
        return "DESC"

    def _single_generation_table(self, plan):
        tables = list(dict.fromkeys(
            plan.generation_tables or plan.selected_tables
        ))
        if len(tables) != 1 or not self._identifier(tables[0]):
            return None
        return tables[0]

    def _alias(self, function, concept):
        normalized = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(concept).strip().lower(),
        ).strip("_")
        prefix = {
            "COUNT": "count",
            "AVG": "average",
            "MAX": "max",
            "MIN": "min",
            "SUM": "total",
        }[function]
        return f"{prefix}_{normalized or 'value'}"

    def _identifier(self, value):
        return bool(
            value
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", str(value))
        )

    def _confidence(self, item):
        value = item.get("confidence_score", item.get("confidence", 0))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _escape(self, value):
        return str(value).replace("\\", "\\\\").replace("'", "''")

    def _quote(self, identifier):
        return f"`{identifier}`"


verified_sql_compiler = VerifiedSQLCompiler()

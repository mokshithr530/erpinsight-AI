from dataclasses import dataclass, field
import re
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope


VERIFIED_SQL_VALIDATOR_VERSION = "verified-sql-validator:v8"


@dataclass
class SQLAnalysis:
    tables: list[str] = field(default_factory=list)
    columns: list[dict[str, str | None]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)
    version: str = VERIFIED_SQL_VALIDATOR_VERSION

    def to_dict(self):
        return {
            "tables": self.tables,
            "columns": self.columns,
            "joins": self.joins,
            "version": self.version,
        }


class VerifiedSQLValidator:
    def validate(self, sql, plan, schema=None):
        try:
            tree = parse_one(str(sql or ""), read="mysql")
        except ParseError as error:
            return {
                "valid": False,
                "errors": [f"MySQL could not be parsed safely: {error}"],
                "warnings": [],
                "analysis": SQLAnalysis().to_dict(),
            }
        if tree is None or not isinstance(tree, exp.Query):
            return {
                "valid": False,
                "errors": ["SQL must be one read-only query."],
                "warnings": [],
                "analysis": SQLAnalysis().to_dict(),
            }

        schema = schema or {}
        cte_lineage = self._cte_lineage(tree)
        aliases = self._aliases(tree, cte_lineage)
        physical_tables = self._physical_tables(tree, cte_lineage)
        columns = self._columns(tree, aliases, cte_lineage)
        joins = self._joins(tree, aliases, cte_lineage)
        analysis = SQLAnalysis(
            tables=physical_tables,
            columns=columns,
            joins=joins,
        )
        errors = []
        warnings = []

        if plan.catalog_authoritative:
            if any(
                star.find_ancestor(exp.Count) is None
                for star in tree.find_all(exp.Star)
            ):
                errors.append(
                    "Authoritative ERP SQL must use explicit columns, not SELECT *."
                )
            self._check_authoritative_tables(
                physical_tables,
                plan,
                errors,
            )
            self._check_authoritative_columns(
                columns,
                tree,
                plan,
                schema,
                cte_lineage,
                errors,
            )
            self._check_approved_joins(joins, plan, errors)
            self._check_bound_metrics(columns, plan, errors)
            self._check_bound_dates(tree, aliases, cte_lineage, plan, errors)
            self._check_bound_statuses(
                tree,
                aliases,
                cte_lineage,
                plan,
                errors,
            )
            self._check_grouping_columns(
                tree,
                aliases,
                cte_lineage,
                plan,
                errors,
            )
            self._check_projection_contract(
                tree,
                aliases,
                cte_lineage,
                plan,
                errors,
            )
            self._check_outer_aggregation_contract(
                tree,
                plan,
                errors,
            )

        return {
            "valid": not errors,
            "errors": self._unique(errors),
            "warnings": self._unique(warnings),
            "analysis": analysis.to_dict(),
        }

    def _check_authoritative_tables(self, referenced, plan, errors):
        allowed = set(plan.generation_tables or plan.selected_tables)
        required = set(plan.generation_tables)
        outside = sorted(set(referenced) - allowed)
        missing = sorted(required - set(referenced))
        if "vendor_mst" in missing and "po_form" in referenced:
            vendor_from_po = any(
                str(item.get("dimension") or "").lower() == "vendor"
                and item.get("table") == "po_form"
                and item.get("column") == "vendor_name"
                for item in plan.bound_dimensions
            )
            vendor_entities = any(
                str(item.get("entity_type") or "").lower() == "vendor"
                for item in plan.resolved_entity_constraints
            )
            if vendor_from_po and vendor_entities:
                missing.remove("vendor_mst")
        if outside:
            errors.append(
                "SQL uses table(s) outside canonical ERP context: "
                + ", ".join(outside)
                + "."
            )
        if missing:
            errors.append(
                "SQL dropped required ERP table(s): "
                + ", ".join(missing)
                + "."
            )

    def _check_authoritative_columns(
        self,
        columns,
        tree,
        plan,
        schema,
        cte_lineage,
        errors,
    ):
        allowed = {
            table: set(values)
            for table, values in (
                plan.generation_columns or plan.selected_columns
            ).items()
        }
        select_aliases = {
            expression.alias
            for expression in tree.find_all(exp.Alias)
            if expression.alias
        }
        cte_names = set(cte_lineage)
        for item in columns:
            table = item.get("table")
            column = item.get("column")
            qualifier = item.get("qualifier")
            if not column or column == "*":
                if column == "*":
                    errors.append(
                        "Authoritative ERP SQL must use explicit columns, not SELECT *."
                    )
                continue
            if item.get("derived") or qualifier in cte_names or column in select_aliases:
                continue
            if qualifier and table is None:
                errors.append(
                    f"SQL uses unknown table alias {qualifier} for column {column}."
                )
                continue
            if table:
                if column not in allowed.get(table, set()):
                    errors.append(
                        f"SQL uses column {table}.{column} outside canonical ERP DDL."
                    )
                continue
            owners = [
                table_name
                for table_name, names in allowed.items()
                if column in names
            ]
            if not owners and not self._column_exists(schema, column):
                errors.append(
                    f"SQL uses unknown or unapproved unqualified column {column}."
                )
            elif len(owners) > 1:
                errors.append(
                    f"SQL column {column} is ambiguous; qualify it with an approved table alias."
                )

    def _check_approved_joins(self, joins, plan, errors):
        approved = {
            self._relationship_key(
                item.get("left_table"),
                item.get("left_column"),
                item.get("right_table"),
                item.get("right_column"),
            )
            for item in plan.approved_relationships
            if isinstance(item, dict)
        }
        for join in joins:
            if join.get("kind") == "CROSS" and join.get("cte_target"):
                continue
            pairs = join.get("column_pairs") or []
            if not pairs:
                errors.append(
                    f"Join to {join.get('target') or 'unknown table'} has no verifiable equality predicate."
                )
                continue
            for pair in pairs:
                if not pair.get("left_table") or not pair.get("right_table"):
                    continue
                key = self._relationship_key(
                    pair["left_table"],
                    pair["left_column"],
                    pair["right_table"],
                    pair["right_column"],
                )
                if key not in approved:
                    errors.append(
                        "SQL invented an unapproved ERP join: "
                        f"{pair['left_table']}.{pair['left_column']} = "
                        f"{pair['right_table']}.{pair['right_column']}."
                    )

    def _check_bound_metrics(self, columns, plan, errors):
        referenced = {
            (item.get("table"), item.get("column"))
            for item in columns
        }
        for metric in plan.bound_metrics:
            table = metric.get("table") or metric.get("preferred_table")
            column = metric.get("column") or metric.get("preferred_column")
            if table and column and (table, column) not in referenced:
                errors.append(
                    f"SQL does not use verified metric field {table}.{column}."
                )

    def _check_bound_dates(self, tree, aliases, cte_lineage, plan, errors):
        if not plan.date_constraints:
            return
        where_columns = self._predicate_columns(
            tree,
            exp.Where,
            aliases,
            cte_lineage,
        )
        for field in plan.bound_date_fields:
            expected = (
                field.get("table") or field.get("preferred_table"),
                field.get("column") or field.get("preferred_column"),
            )
            if all(expected) and expected not in where_columns:
                errors.append(
                    f"SQL date filter does not use verified field {expected[0]}.{expected[1]}."
                )

    def _check_bound_statuses(self, tree, aliases, cte_lineage, plan, errors):
        if not plan.status_constraints:
            return
        where_columns = self._predicate_columns(
            tree,
            exp.Where,
            aliases,
            cte_lineage,
        )
        for field in plan.bound_status_fields:
            expected = (
                field.get("table") or field.get("preferred_table"),
                field.get("column") or field.get("preferred_column"),
            )
            if all(expected) and expected not in where_columns:
                errors.append(
                    f"SQL status filter does not use verified field {expected[0]}.{expected[1]}."
                )

    def _check_grouping_columns(self, tree, aliases, cte_lineage, plan, errors):
        root_select = self._root_select(tree)
        if root_select is None or not self._has_aggregate(root_select):
            return
        group = root_select.args.get("group")
        if group is not None and not plan.grouping:
            if self._group_by_matches_date_bucket(group, plan):
                return
            if self._group_by_matches_resolved_entity(
                group,
                aliases,
                cte_lineage,
                plan,
                self._physical_tables(tree, cte_lineage),
            ):
                return
            if (
                plan.operation in {"ranking", "top_n", "bottom_n"}
                and self._group_by_matches_bound_dimension(
                    group,
                    aliases,
                    cte_lineage,
                    plan,
                    self._physical_tables(tree, cte_lineage),
                )
            ):
                return
            if self._group_by_matches_requested_dimension(
                group,
                aliases,
                cte_lineage,
                plan,
                self._physical_tables(tree, cte_lineage),
            ):
                return
            if self._group_by_matches_bound_dimension(
                group,
                aliases,
                cte_lineage,
                plan,
                self._physical_tables(tree, cte_lineage),
            ):
                return
            errors.append(
                "SQL adds an unrequested outer GROUP BY and changes the required result grain."
            )
            return
        if not plan.bound_dimensions:
            return
        if group is None:
            return
        physical_tables = self._physical_tables(tree, cte_lineage)
        sole_table = physical_tables[0] if len(physical_tables) == 1 else None
        grouped = set()
        for column in group.find_all(exp.Column):
            resolved_table = self._resolve_table(
                column.table,
                aliases,
                cte_lineage,
            )
            if not resolved_table:
                resolved_table = sole_table
            grouped.add((resolved_table, column.name))
        required_grouping = {
            str(value).strip().lower()
            for value in plan.grouping
            if value
        }
        for dimension in plan.bound_dimensions:
            if self._dimension_is_counted_identity(dimension, plan):
                continue
            dimension_names = {
                str(dimension.get(key) or "").strip().lower()
                for key in ("dimension", "bound_concept", "requested_concept")
                if dimension.get(key)
            }
            if required_grouping and not (dimension_names & required_grouping):
                continue
            expected = (
                dimension.get("table") or dimension.get("preferred_table"),
                dimension.get("column") or dimension.get("preferred_column"),
            )
            if all(expected) and expected not in grouped:
                errors.append(
                    f"SQL GROUP BY does not preserve verified dimension {expected[0]}.{expected[1]}."
                )

    def _group_by_matches_resolved_entity(
        self,
        group,
        aliases,
        cte_lineage,
        plan,
        physical_tables,
    ):
        if not plan.resolved_entity_constraints:
            return False
        sole_table = physical_tables[0] if len(physical_tables) == 1 else None
        grouped = set()
        for column in group.find_all(exp.Column):
            table = self._resolve_table(column.table, aliases, cte_lineage)
            if table is None and not column.table:
                table = sole_table
            grouped.add((table, column.name))
        entity_columns = {
            (item.get("table"), column)
            for item in plan.resolved_entity_constraints
            for column in (item.get("column"), item.get("primary_key_column"))
            if item.get("table") and column
        }
        return bool(grouped and grouped <= entity_columns)

    def _group_by_matches_bound_dimension(
        self,
        group,
        aliases,
        cte_lineage,
        plan,
        physical_tables,
    ):
        if not plan.bound_dimensions:
            return False
        sole_table = physical_tables[0] if len(physical_tables) == 1 else None
        grouped = set()
        for column in group.find_all(exp.Column):
            table = self._resolve_table(column.table, aliases, cte_lineage)
            if table is None and not column.table:
                table = sole_table
            grouped.add((table, column.name))
        dimension_columns = {
            pair
            for item in plan.bound_dimensions
            for pair in (
                (
                    item.get("table") or item.get("preferred_table"),
                    item.get("column") or item.get("preferred_column"),
                ),
                (
                    item.get("table") or item.get("preferred_table"),
                    item.get("primary_key_column"),
                ),
            )
            if pair[0] and pair[1]
        }
        if grouped and grouped <= dimension_columns:
            return True
        group_sql = group.sql(dialect="mysql").lower()
        required_columns = {
            str(column).strip().lower()
            for _table, column in dimension_columns
            if column
        }
        return bool(
            required_columns
            and all(f"`{column}`" in group_sql or re.search(rf"\b{re.escape(column)}\b", group_sql) for column in required_columns)
        )

    def _group_by_matches_requested_dimension(
        self,
        group,
        aliases,
        cte_lineage,
        plan,
        physical_tables,
    ):
        requested = {
            str(value).strip().lower()
            for value in [
                *(getattr(plan, "requested_outputs", []) or []),
                *(getattr(plan, "dimensions", []) or []),
            ]
            if value
        }
        if not requested or not plan.bound_dimensions:
            return False
        if not any(
            {
                str(item.get("dimension") or "").strip().lower(),
                str(item.get("bound_concept") or "").strip().lower(),
                str(item.get("requested_concept") or "").strip().lower(),
            }
            & requested
            for item in plan.bound_dimensions
            if isinstance(item, dict)
        ):
            return False
        return self._group_by_matches_bound_dimension(
            group,
            aliases,
            cte_lineage,
            plan,
            physical_tables,
        )

    def _group_by_matches_date_bucket(self, group, plan):
        if not plan.date_constraints:
            return False
        group_sql = group.sql(dialect="mysql").lower()
        return (
            "bill_month" in group_sql
            or "po_month" in group_sql
            or "date_format" in group_sql
        )

    def _check_projection_contract(
        self,
        tree,
        aliases,
        cte_lineage,
        plan,
        errors,
    ):
        root_select = self._root_select(tree)
        if root_select is None or not root_select.expressions:
            return

        # CTE output lineage can use derived aliases rather than physical column
        # names. The existing canonical-column and aggregation checks remain
        # authoritative for those plans.
        if cte_lineage:
            return

        physical_tables = self._physical_tables(tree, cte_lineage)
        sole_table = physical_tables[0] if len(physical_tables) == 1 else None
        projected = set()
        for expression in root_select.expressions:
            for column in expression.find_all(exp.Column):
                table = self._resolve_table(
                    column.table,
                    aliases,
                    cte_lineage,
                )
                if table is None and not column.table:
                    table = sole_table
                projected.add((table, column.name))

        requires_metric_output = (
            plan.operation
            in {
                "aggregation",
                "sum",
                "average",
                "ranking",
                "top_n",
                "bottom_n",
                "comparison",
                "trend",
                "distribution",
                "business_insight",
            }
            and any(
                str(item).lower()
                in {
                    "sum",
                    "total",
                    "avg",
                    "average",
                    "min",
                    "minimum",
                    "max",
                    "maximum",
                }
                for item in plan.aggregation
            )
        )
        if requires_metric_output and self._is_verified_project_cross_module(plan):
            requires_metric_output = False
        if requires_metric_output:
            for metric in plan.bound_metrics:
                table = metric.get("table") or metric.get("preferred_table")
                column = metric.get("column") or metric.get("preferred_column")
                if table and column and (table, column) not in projected:
                    errors.append(
                        "SQL does not return the verified metric field "
                        f"{table}.{column} requested by the query plan."
                    )

        requires_dimension_output = bool(plan.grouping) or plan.operation in {
            "ranking",
            "top_n",
            "bottom_n",
            "comparison",
            "distribution",
            "relationship_lookup",
        }
        if requires_dimension_output:
            grouped_concepts = {
                str(value).strip().lower()
                for value in plan.grouping
                if value
            }
            for dimension in plan.bound_dimensions:
                if self._dimension_is_counted_identity(dimension, plan):
                    continue
                dimension_concepts = {
                    str(dimension.get(key) or "").strip().lower()
                    for key in (
                        "dimension",
                        "bound_concept",
                        "requested_concept",
                    )
                    if dimension.get(key)
                }
                if (
                    grouped_concepts
                    and not (dimension_concepts & grouped_concepts)
                ):
                    continue
                table = (
                    dimension.get("table")
                    or dimension.get("preferred_table")
                )
                column = (
                    dimension.get("column")
                    or dimension.get("preferred_column")
                )
                if table and column and (table, column) not in projected:
                    errors.append(
                        "SQL does not return the verified dimension field "
                        f"{table}.{column} requested by the query plan."
                    )

    def _check_outer_aggregation_contract(self, tree, plan, errors):
        root_select = self._root_select(tree)
        if root_select is None:
            return
        allowed = {
            self._aggregate_name(item)
            for item in plan.aggregation
        }
        allowed.discard(None)
        if plan.operation in {"count", "existence_check"}:
            allowed.add("count")
        if any(
            str(metric.get("default_aggregation") or "").lower()
            in {"count", "distinct_count"}
            or str(metric.get("metric") or metric.get("bound_concept") or "").lower().endswith("_count")
            for metric in plan.bound_metrics
            if isinstance(metric, dict)
        ):
            allowed.add("count")
        if not allowed:
            return

        actual = {
            self._aggregate_expression_name(function)
            for expression in root_select.expressions
            for function in expression.find_all(exp.AggFunc)
        }
        actual.discard(None)
        if (
            "avg" in actual
            and self._has_bound_metric(plan, "material_price")
            and self._has_bound_metric(plan, "material_consumption")
        ):
            actual.discard("avg")
        unexpected = sorted(actual - allowed)
        if unexpected:
            errors.append(
                "SQL returns unrequested aggregate function(s): "
                + ", ".join(name.upper() for name in unexpected)
                + "."
            )

    def _has_bound_metric(self, plan, metric_name):
        return any(
            str(metric.get("metric") or metric.get("bound_concept") or "").lower()
            == metric_name
            for metric in plan.bound_metrics
            if isinstance(metric, dict)
        )

    def _is_verified_project_cross_module(self, plan):
        tables = set(plan.generation_tables or plan.selected_tables)
        metric_tables = {
            metric.get("table")
            for metric in plan.bound_metrics
            if isinstance(metric, dict)
        }
        return bool(
            "project_mst" in tables
            and len(metric_tables & {
                "client_bill_raising",
                "po_form",
                "store_material_usage_quantity",
                "project_mst",
            }) >= 2
        )

    def _dimension_is_counted_identity(self, dimension, plan):
        table = dimension.get("table") or dimension.get("preferred_table")
        column = dimension.get("column") or dimension.get("preferred_column")
        if not table or not column:
            return False
        return any(
            (metric.get("table") or metric.get("preferred_table")) == table
            and (metric.get("column") or metric.get("preferred_column")) == column
            and (
                str(metric.get("default_aggregation") or "").lower()
                in {"count", "distinct_count"}
                or str(metric.get("metric") or metric.get("bound_concept") or "").lower().endswith("_count")
            )
            for metric in plan.bound_metrics
            if isinstance(metric, dict)
        )

    def _root_select(self, tree):
        if isinstance(tree, exp.Select):
            return tree
        return tree.find(exp.Select)

    def _aggregate_name(self, value):
        normalized = str(value or "").strip().lower()
        return {
            "average": "avg",
            "minimum": "min",
            "maximum": "max",
            "total": "sum",
            "distinct_count": "count",
        }.get(normalized, normalized or None)

    def _aggregate_expression_name(self, expression):
        if isinstance(expression, exp.Count):
            return "count"
        if isinstance(expression, exp.Sum):
            return "sum"
        if isinstance(expression, exp.Avg):
            return "avg"
        if isinstance(expression, exp.Min):
            return "min"
        if isinstance(expression, exp.Max):
            return "max"
        return str(getattr(expression, "key", "") or "").lower() or None

    def _physical_tables(self, tree, cte_lineage):
        cte_names = set(cte_lineage)
        return self._unique([
            table.name
            for table in tree.find_all(exp.Table)
            if table.name not in cte_names
        ])

    def _aliases(self, tree, cte_lineage):
        aliases = {}
        cte_names = set(cte_lineage)
        for table in tree.find_all(exp.Table):
            name = table.name
            alias = table.alias_or_name
            if name in cte_names:
                lineage = cte_lineage.get(name) or []
                if len(lineage) == 1:
                    aliases[alias] = lineage[0]
                continue
            aliases[alias] = name
            aliases[name] = name
        return aliases

    def _cte_lineage(self, tree):
        result = {}
        for cte in tree.find_all(exp.CTE):
            name = cte.alias_or_name
            physical = self._unique([
                table.name
                for table in cte.this.find_all(exp.Table)
                if table.name != name
            ])
            result[name] = physical
        return result

    def _columns(self, tree, aliases, cte_lineage):
        result = []
        for scope in traverse_scope(tree):
            local_sources = self._scope_sources(scope, cte_lineage)
            physical_sources = {
                item["table"]
                for item in local_sources.values()
                if item["table"] and not item["derived"]
            }
            for column in scope.columns:
                source = local_sources.get(column.table)
                if source:
                    table = source["table"]
                    derived = source["derived"]
                elif not column.table and len(physical_sources) == 1:
                    table = next(iter(physical_sources))
                    derived = False
                else:
                    table = self._resolve_table(
                        column.table,
                        aliases,
                        cte_lineage,
                    )
                    derived = False
                result.append({
                    "qualifier": column.table or None,
                    "table": table,
                    "column": column.name,
                    "derived": derived,
                })
        return result

    def _joins(self, tree, aliases, cte_lineage):
        result = []
        for join in tree.find_all(exp.Join):
            target_expression = join.this
            target = (
                target_expression.name
                if isinstance(target_expression, exp.Table)
                else target_expression.alias_or_name
            )
            on = join.args.get("on")
            pairs = []
            if on is not None:
                for equality in on.find_all(exp.EQ):
                    left = equality.left
                    right = equality.right
                    if not isinstance(left, exp.Column) or not isinstance(
                        right,
                        exp.Column,
                    ):
                        continue
                    pairs.append({
                        "left_table": self._resolve_table(
                            left.table,
                            aliases,
                            cte_lineage,
                        ),
                        "left_column": left.name,
                        "right_table": self._resolve_table(
                            right.table,
                            aliases,
                            cte_lineage,
                        ),
                        "right_column": right.name,
                    })
            result.append({
                "target": target,
                "kind": str(join.args.get("kind") or "").upper(),
                "cte_target": target in cte_lineage,
                "column_pairs": pairs,
            })
        return result

    def _predicate_columns(
        self,
        tree,
        predicate_type,
        aliases,
        cte_lineage,
    ):
        result = set()
        for scope in traverse_scope(tree):
            local_sources = self._scope_sources(scope, cte_lineage)
            physical_sources = {
                item["table"]
                for item in local_sources.values()
                if item["table"] and not item["derived"]
            }
            for predicate in scope.expression.find_all(predicate_type):
                for column in predicate.find_all(exp.Column):
                    source = local_sources.get(column.table)
                    if source:
                        table = source["table"]
                    elif not column.table and len(physical_sources) == 1:
                        table = next(iter(physical_sources))
                    else:
                        table = self._resolve_table(
                            column.table,
                            aliases,
                            cte_lineage,
                        )
                    result.add((table, column.name))
        return result

    def _scope_sources(self, scope, cte_lineage):
        local_sources = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                local_sources[alias] = {
                    "table": source.name,
                    "derived": False,
                }
            elif isinstance(source, Scope):
                physical = self._unique([
                    table.name
                    for table in source.expression.find_all(exp.Table)
                    if table.name not in cte_lineage
                ])
                local_sources[alias] = {
                    "table": physical[0] if len(physical) == 1 else None,
                    "derived": True,
                }
        return local_sources

    def _resolve_table(self, qualifier, aliases, cte_lineage):
        if not qualifier:
            return None
        if qualifier in aliases:
            return aliases[qualifier]
        lineage = cte_lineage.get(qualifier) or []
        return lineage[0] if len(lineage) == 1 else None

    def _relationship_key(
        self,
        left_table,
        left_column,
        right_table,
        right_column,
    ):
        left = (str(left_table or ""), str(left_column or ""))
        right = (str(right_table or ""), str(right_column or ""))
        return tuple(sorted((left, right)))

    def _column_exists(self, schema, column):
        return any(
            column in {
                item.get("column_name")
                or item.get("name")
                or item.get("COLUMN_NAME")
                for item in info.get("columns", [])
            }
            for info in schema.values()
        )

    def _has_aggregate(self, tree):
        return any(
            True
            for _ in tree.find_all(exp.AggFunc)
        )

    def _unique(self, values):
        result = []
        seen = set()
        for value in values:
            if value in (None, ""):
                continue
            key = str(value).lower()
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result


verified_sql_validator = VerifiedSQLValidator()

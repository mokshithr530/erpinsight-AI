import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd

from config import (
    MYSQL_DATABASE_PREFIX,
    MYSQL_ALLOWED_DATABASES,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_QUERY_TIMEOUT_SECONDS,
    MYSQL_USERNAME,
    QUERY_ROW_LIMIT,
)
from business_catalog import business_catalog
from database_fingerprint import database_fingerprint, safe_host_identity, schema_fingerprint
from database_memory import database_memory
from entity_resolver import entity_resolver
from relationship_discovery import relationship_discovery
from semantic_parser import semantic_parser
from schema_retriever import schema_retriever
from schema_profile_history import schema_profile_history


class MySQLAdapter:
    MYSQL_MARKERS = (
        "ENGINE=INNODB",
        "AUTO_INCREMENT",
        "LOCK TABLES",
        "UNLOCK TABLES",
        "/*!",
    )
    BLOCKED = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|"
        r"EXECUTE|GRANT|REVOKE|LOAD|LOCK|UNLOCK|USE)\b",
        re.IGNORECASE,
    )
    BLOCKED_STATEMENT = re.compile(r"^\s*(REPLACE)\b", re.IGNORECASE)
    TABLE_REF = re.compile(
        r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w]*)`?(?:\s+(?:AS\s+)?`?[A-Za-z_][\w]*`?)?",
        re.IGNORECASE,
    )
    IMPORTS_DIR = Path(__file__).resolve().parent / "runtime" / "mysql_imports"
    ENTITY_ALIASES = {
        "project": ("project", "projects", "prject", "prjects", "prjct", "prjcts", "job", "jobs", "site", "sites", "work"),
        "client": ("client", "clients", "customer", "customers"),
        "vendor": ("vendor", "vendors", "supplier", "suppliers", "contractor", "contractors"),
        "employee": ("employee", "employees", "staff", "worker", "workers"),
        "department": ("department", "departments", "dept", "depts", "division", "divisions"),
        "product": ("product", "products", "item", "items", "material", "materials"),
        "order": ("order", "orders", "purchase order", "purchase orders", "po", "pos"),
        "invoice": ("invoice", "invoices", "bill", "bills", "billing"),
        "payment": ("payment", "payments", "transaction", "transactions"),
    }
    ENTITY_TABLE_ALIASES = {
        "project": ("project", "job"),
        "client": ("client", "customer"),
        "vendor": ("vendor", "supplier", "contractor"),
        "employee": ("employee", "emp", "staff", "worker"),
        "department": ("department", "dept", "division"),
        "product": ("product", "item", "material"),
        "order": ("order", "purchase_order", "po"),
        "invoice": ("invoice", "bill"),
        "payment": ("payment", "transaction"),
    }
    LIST_WORDS = ("list", "show", "random", "few", "some", "available", "sample", "examples")
    COUNT_WORDS = ("how many", "count", "number of", "total number")

    def __init__(self):
        self._engines = {}
        self._schema_cache = {}
        self._schema_fingerprint_cache = {}
        self._summary_cache = {}
        self._profile_samples_cache = {}

    def _connection_url(self, database=None):
        username = quote_plus(MYSQL_USERNAME)
        password = quote_plus(MYSQL_PASSWORD)
        target = f"/{quote_plus(database)}" if database else ""
        return f"mysql+pymysql://{username}:{password}@{MYSQL_HOST}:{MYSQL_PORT}{target}?charset=utf8mb4"

    def engine(self, database=None):
        key = database or "__server__"
        if key not in self._engines:
            try:
                from sqlalchemy import create_engine
            except ImportError as error:
                raise RuntimeError(
                    "MySQL support requires sqlalchemy and pymysql. "
                    "Run pip install -r requirements.txt."
                ) from error

            self._engines[key] = create_engine(
                self._connection_url(database),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                connect_args={
                    "connect_timeout": MYSQL_QUERY_TIMEOUT_SECONDS,
                    "read_timeout": MYSQL_QUERY_TIMEOUT_SECONDS,
                    "write_timeout": MYSQL_QUERY_TIMEOUT_SECONDS,
                },
            )
        return self._engines[key]

    def test_connection(self):
        with self.engine().connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return True

    def file_fingerprint(self, path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def is_mysql_dump(self, sql_file_path, max_bytes=1024 * 1024):
        sample = Path(sql_file_path).open(
            "r",
            encoding="utf-8-sig",
            errors="replace",
        ).read(max_bytes)
        upper = sample.upper()
        if any(marker in upper for marker in self.MYSQL_MARKERS):
            return True
        return bool(re.search(r"`[A-Za-z_][\w]*`", sample))

    def _safe_database_name(self, session_id, fingerprint):
        safe_session = re.sub(r"[^A-Za-z0-9_]", "_", session_id)[:32] or "session"
        return (
            f"{MYSQL_DATABASE_PREFIX}_{safe_session}_{fingerprint[:8]}_"
            f"{uuid.uuid4().hex[:8]}"
        ).lower()

    def _quote_identifier(self, identifier):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
            raise ValueError(f"Invalid MySQL identifier: {identifier}")
        return f"`{identifier}`"

    def _import_state_path(self, database):
        self.IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
        return self.IMPORTS_DIR / f"{database}.json"

    def _save_import_state(self, database, source_name, fingerprint, schema_fingerprint):
        state = {
            "source_type": "mysql",
            "connection_id": f"mysql:{database}:{fingerprint[:16]}",
            "database": database,
            "name": source_name,
            "fingerprint": fingerprint,
            "schema_fingerprint": schema_fingerprint,
        }
        self._import_state_path(database).write_text(json.dumps(state), encoding="utf-8")
        return state

    def create_database(self, database):
        quoted = self._quote_identifier(database)
        with self.engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE IF NOT EXISTS {quoted} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )

    def drop_database(self, database):
        quoted = self._quote_identifier(database)
        with self.engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {quoted}")
        self.clear_schema_cache(database)

    def native_client(self):
        mysql_path = shutil.which("mysql")
        if mysql_path:
            return {
                "kind": "host",
                "args": [
                    mysql_path,
                    "--protocol=tcp",
                    "-h",
                    MYSQL_HOST,
                    "-P",
                    str(MYSQL_PORT),
                    "-u",
                    MYSQL_USERNAME,
                ],
            }
        docker_path = shutil.which("docker")
        if docker_path and MYSQL_HOST in {"127.0.0.1", "localhost"}:
            try:
                result = subprocess.run(
                    [docker_path, "inspect", "-f", "{{.State.Running}}", "sql-assistant-mysql"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except Exception:
                return None
            if result.returncode == 0 and result.stdout.strip().lower() == "true":
                return {
                    "kind": "docker",
                    "args": [
                        docker_path,
                        "exec",
                        "-i",
                        "-e",
                        "MYSQL_PWD",
                        "sql-assistant-mysql",
                        "mysql",
                        "-u",
                        MYSQL_USERNAME,
                    ],
                }
        return None

    def _native_import(self, sql_file_path, database):
        client = self.native_client()
        if not client:
            raise RuntimeError("mysql client is not available.")

        env = None
        if client["kind"] == "host":
            env = {**__import__("os").environ, "MYSQL_PWD": MYSQL_PASSWORD}
        else:
            env = {**__import__("os").environ, "MYSQL_PWD": MYSQL_PASSWORD}
        args = [*client["args"], database]
        prefix = b"SET SESSION innodb_strict_mode=OFF;\nSET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\n"
        suffix = b"\nSET UNIQUE_CHECKS=1;\nSET FOREIGN_KEY_CHECKS=1;\n"

        with tempfile.TemporaryFile(mode="w+b") as stderr_file:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                env=env,
            )
            assert process.stdin is not None
            try:
                process.stdin.write(prefix)
                with Path(sql_file_path).open("rb") as dump:
                    for chunk in iter(lambda: dump.read(1024 * 1024), b""):
                        process.stdin.write(chunk)
                process.stdin.write(suffix)
                process.stdin.close()
                return_code = process.wait()
            except Exception:
                process.kill()
                process.wait()
                raise

            stderr_file.seek(0)
            stderr = stderr_file.read().decode("utf-8", errors="replace").strip()
            if return_code != 0:
                readable = stderr[-2000:] if stderr else f"mysql exited with code {return_code}"
                raise RuntimeError(readable)
        return client["kind"]

    def _read_statements(self, sql_file_path):
        statement = []
        with open(sql_file_path, "r", encoding="utf-8-sig", errors="replace") as file:
            for line in file:
                stripped = line.strip()
                upper = stripped.upper()
                if not stripped or stripped.startswith("--"):
                    continue
                if upper.startswith("DELIMITER"):
                    raise ValueError(
                        "This MySQL dump uses DELIMITER/stored routine syntax, which this POC importer does not support."
                    )
                statement.append(line)
                if stripped.endswith(";"):
                    sql = "".join(statement).strip().rstrip(";")
                    if sql:
                        yield sql
                    statement = []
        sql = "".join(statement).strip().rstrip(";")
        if sql:
            yield sql

    def _python_import(self, sql_file_path, database):
        raw_connection = self.engine(database).raw_connection()
        try:
            with raw_connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_strict_mode=OFF")
                cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                cursor.execute("SET UNIQUE_CHECKS=0")
                try:
                    for statement in self._read_statements(sql_file_path):
                        try:
                            cursor.execute(statement)
                        except Exception as error:
                            snippet = " ".join(statement.split())[:500]
                            raise RuntimeError(f"MySQL import failed near: {snippet}") from error
                finally:
                    cursor.execute("SET UNIQUE_CHECKS=1")
                    cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            raw_connection.commit()
        except Exception:
            raw_connection.rollback()
            raise
        finally:
            raw_connection.close()

    def import_sql_file(self, sql_file_path, source_name, session_id):
        if not self.is_mysql_dump(sql_file_path):
            raise ValueError("This .sql file does not look like a MySQL dump.")

        started = time.perf_counter()
        progress = ["Preparing database"]
        fingerprint = self.file_fingerprint(sql_file_path)
        database = self._safe_database_name(session_id, fingerprint)
        native_error = None
        import_method = "python"

        self.create_database(database)
        progress.append("Importing")
        try:
            native_started = time.perf_counter()
            import_method = f"native:{self._native_import(sql_file_path, database)}"
            native_duration = time.perf_counter() - native_started
        except Exception as error:
            native_error = str(error)
            self.drop_database(database)
            self.create_database(database)
            python_started = time.perf_counter()
            self._python_import(sql_file_path, database)
            native_duration = None
            python_duration = time.perf_counter() - python_started
        else:
            python_duration = None

        self.clear_schema_cache(database)
        schema_fingerprint = self.schema_fingerprint(database)
        duration = time.perf_counter() - started
        progress.append("Completed")
        return {
            **self._save_import_state(database, source_name, fingerprint, schema_fingerprint),
            "already_imported": False,
            "import_method": import_method,
            "import_duration_seconds": round(duration, 3),
            "native_duration_seconds": round(native_duration, 3) if native_duration is not None else None,
            "python_duration_seconds": round(python_duration, 3) if python_duration is not None else None,
            "native_error": native_error,
            "progress": progress,
        }

    def list_allowed_databases(self):
        with self.engine().connect() as connection:
            rows = connection.exec_driver_sql("SHOW DATABASES").fetchall()
        databases = [row[0] for row in rows]
        system = {"information_schema", "mysql", "performance_schema", "sys"}
        if MYSQL_ALLOWED_DATABASES:
            allowed = set(MYSQL_ALLOWED_DATABASES)
            return sorted(database for database in databases if database in allowed)
        return sorted(
            database
            for database in databases
            if database not in system
        )

    def attach_database(self, database, source_name=None):
        if database not in self.list_allowed_databases():
            raise ValueError("Selected MySQL database is not configured or allowed.")
        schema = self.inspect_schema(database)
        if not schema:
            raise ValueError("Selected MySQL database does not contain any tables.")
        schema_fingerprint = self.schema_fingerprint(database)
        return {
            "source_type": "mysql",
            "connection_id": f"mysql:{database}:{schema_fingerprint[:16]}",
            "database": database,
            "name": source_name or database,
            "schema_fingerprint": schema_fingerprint,
            "already_imported": True,
            "import_method": "existing",
            "import_duration_seconds": 0,
            "progress": ["Preparing database", "Completed"],
        }

    def clear_schema_cache(self, database=None):
        if database:
            fingerprint = self._schema_fingerprint_cache.get(database)
            self._schema_cache.pop(database, None)
            self._schema_fingerprint_cache.pop(database, None)
            self._summary_cache.pop(database, None)
            self._profile_samples_cache.pop(database, None)
            if fingerprint:
                business_catalog.clear(fingerprint)
        else:
            self._schema_cache.clear()
            self._schema_fingerprint_cache.clear()
            self._summary_cache.clear()
            self._profile_samples_cache.clear()
            business_catalog.clear()

    def inspect_schema(self, database):
        if database in self._schema_cache:
            return self._schema_cache[database]

        sql = """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE,
               COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT, ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """
        index_sql = """
        SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, NON_UNIQUE, SEQ_IN_INDEX
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """
        foreign_key_sql = """
        SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME,
               CONSTRAINT_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
        """
        table_sql = """
        SELECT TABLE_NAME, TABLE_COMMENT, COALESCE(TABLE_ROWS, 0) AS TABLE_ROWS
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
        """
        schema = {}
        with self.engine(database).connect() as connection:
            rows = connection.exec_driver_sql(sql).mappings().all()
            index_rows = connection.exec_driver_sql(index_sql).mappings().all()
            foreign_key_rows = connection.exec_driver_sql(foreign_key_sql).mappings().all()
            table_rows = connection.exec_driver_sql(table_sql).mappings().all()
        table_metadata = {row["TABLE_NAME"]: row for row in table_rows}
        for row in rows:
            table = row["TABLE_NAME"]
            metadata = table_metadata.get(table, {})
            schema.setdefault(table, {
                "columns": [],
                "foreign_keys": [],
                "indexes": [],
                "constraints": [],
                "comment": metadata.get("TABLE_COMMENT") or None,
                "row_count": int(metadata.get("TABLE_ROWS") or 0),
            })
            schema[table]["columns"].append(
                {
                    "column_name": row["COLUMN_NAME"],
                    "name": row["COLUMN_NAME"],
                    "data_type": row["DATA_TYPE"],
                    "column_type": row.get("COLUMN_TYPE"),
                    "not_null": row["IS_NULLABLE"] == "NO",
                    "primary_key": row["COLUMN_KEY"] == "PRI",
                    "unique": row["COLUMN_KEY"] == "UNI",
                    "indexed": row["COLUMN_KEY"] in {"PRI", "UNI", "MUL"},
                    "default_value": row.get("COLUMN_DEFAULT"),
                    "generated": "GENERATED" in str(row.get("EXTRA") or "").upper(),
                    "comment": row.get("COLUMN_COMMENT") or None,
                    "ordinal_position": row.get("ORDINAL_POSITION"),
                }
            )
        index_map = {}
        for row in index_rows:
            key = (row["TABLE_NAME"], row["INDEX_NAME"])
            index_map.setdefault(
                key,
                {
                    "name": row["INDEX_NAME"],
                    "columns": [],
                    "unique": int(row["NON_UNIQUE"]) == 0,
                },
            )
            index_map[key]["columns"].append(row["COLUMN_NAME"])
        for (table, _name), index in index_map.items():
            if table in schema:
                schema[table]["indexes"].append(index)
                if index["unique"]:
                    schema[table]["constraints"].append({
                        "name": index["name"],
                        "type": "primary key" if index["name"] == "PRIMARY" else "unique",
                        "columns": index["columns"],
                    })
        for row in foreign_key_rows:
            table = row["TABLE_NAME"]
            if table in schema:
                schema[table]["foreign_keys"].append({
                    "name": row.get("CONSTRAINT_NAME"),
                    "column": row["COLUMN_NAME"],
                    "table": row["REFERENCED_TABLE_NAME"],
                    "references": row["REFERENCED_COLUMN_NAME"],
                })
        self._schema_cache[database] = schema
        return schema

    def schema_prompt(self, database, allowed_tables=None, max_columns=18, include_row_counts=False):
        allowed = set(allowed_tables or [])
        row_counts = self._approx_row_counts(database) if include_row_counts else {}
        prompt = "DATABASE SCHEMA\n"
        for table, info in self.inspect_schema(database).items():
            if allowed and table not in allowed:
                continue
            row_note = f" (~{row_counts.get(table):,} rows)" if table in row_counts else ""
            prompt += f"{table}{row_note}: "
            for column in self._compact_columns(info["columns"], max_columns=max_columns):
                prompt += f"{column['column_name']} {column['data_type']}, "
            prompt = prompt.rstrip(", ") + "\n"
        return prompt

    def _compact_columns(self, columns, max_columns=18):
        if max_columns is None or int(max_columns) <= 0:
            return columns
        priority_markers = (
            "id",
            "name",
            "date",
            "time",
            "amount",
            "total",
            "price",
            "cost",
            "expense",
            "revenue",
            "qty",
            "quantity",
            "status",
            "type",
            "category",
            "department",
            "project",
            "customer",
            "client",
            "vendor",
            "employee",
            "asset",
            "material",
        )
        scored = []
        for index, column in enumerate(columns):
            name = column["column_name"].lower()
            score = 10 if column.get("primary_key") else 0
            score += sum(1 for marker in priority_markers if marker in name)
            scored.append((score, -index, column))
        selected = [column for _, _, column in sorted(scored, reverse=True)[:max_columns]]
        return sorted(selected, key=lambda column: columns.index(column))

    def database_summary(self, database):
        if database in self._summary_cache:
            return self._summary_cache[database]

        schema = self.inspect_schema(database)
        row_counts = {
            item["table"]: item["approx_rows"]
            for item in self._largest_tables(database)
        }
        catalog = business_catalog.overview(
            schema,
            source_type="mysql",
            row_counts=row_counts,
            fingerprint=self.schema_fingerprint(database),
        )
        summary = {
            "database_type": "MySQL",
            "schema_fingerprint": catalog["schema_fingerprint"],
            "table_count": catalog["table_count"],
            "detected_modules": catalog["detected_modules"],
            "important_tables": [
                table["table"] if isinstance(table, dict) else table
                for table in catalog["important_tables"]
            ],
            "largest_tables": self._largest_tables(database)[:8],
            "key_relationships": catalog["key_relationships"],
            "unsupported_kpis": catalog["unsupported_kpis"],
            "short_schema_summary": catalog["short_schema_summary"],
        }
        self._summary_cache[database] = summary
        return summary

    def database_memory(self, database):
        schema = self.inspect_schema(database)
        row_counts = {
            item["table"]: item["approx_rows"]
            for item in self._largest_tables(database)
        }
        return database_memory.get_or_build(
            source_type="mysql",
            database_name=database,
            schema=schema,
            row_counts=row_counts,
            fingerprint=self.schema_fingerprint(database),
        )

    def business_catalog(self, database):
        return business_catalog.build(
            self.inspect_schema(database),
            source_type="mysql",
            row_counts={
                item["table"]: item["approx_rows"]
                for item in self._largest_tables(database)
            },
            fingerprint=self.schema_fingerprint(database),
        )

    def retrieve_schema_context(
        self,
        question,
        database,
        *,
        semantic_plan=None,
        entity_resolution=None,
        candidate_limit=10,
    ):
        schema = self.inspect_schema(database)
        row_counts = {
            item["table"]: item["approx_rows"]
            for item in self._largest_tables(database)
        }
        catalog = self.business_catalog(database)
        schema_fp = self.schema_fingerprint(database)
        safe_samples = self._safe_profile_samples(database, schema)
        historical_queries = schema_profile_history.list(schema_fp)
        result = schema_retriever.retrieve(
            question=question,
            schema=schema,
            row_counts=row_counts,
            catalog=catalog,
            schema_fingerprint=schema_fp,
            semantic_plan=semantic_plan,
            entity_resolution=entity_resolution,
            candidate_limit=candidate_limit,
            safe_samples=safe_samples,
            historical_queries=historical_queries,
        )
        profiles = schema_retriever.profiles_for(
            schema=schema,
            row_counts=row_counts,
            catalog=catalog,
            schema_fingerprint=schema_fp,
            safe_samples=safe_samples,
            historical_queries=historical_queries,
        )
        return {
            "result": result.to_dict(),
            "prompt": schema_retriever.compact_prompt(result, profiles),
            "tables": result.selected_tables or [table.table for table in result.ranked_tables],
            "columns": result.relevant_columns,
        }

    def _safe_profile_samples(self, database, schema, *, table_limit=12, column_limit=6, row_limit=5):
        if database in self._profile_samples_cache:
            return self._profile_samples_cache[database]

        sensitive = re.compile(
            r"(?:password|passwd|secret|token|api.?key|credential|ssn|pan|email|phone|mobile|address)",
            re.IGNORECASE,
        )
        unsupported_types = re.compile(r"(?:blob|binary|varbinary|json|geometry)", re.IGNORECASE)
        role_terms = re.compile(
            r"(?:name|title|status|state|type|category|code|date|time|amount|value|price|cost|qty|quantity|rate|percent)",
            re.IGNORECASE,
        )
        candidates = []
        for table, info in schema.items():
            row_count = int(info.get("row_count") or 0)
            if row_count <= 0:
                continue
            usable = []
            for position, column in enumerate(info.get("columns", [])):
                name = self._column_name(column)
                data_type = str(column.get("column_type") or column.get("data_type") or "")
                if not name or sensitive.search(name) or unsupported_types.search(data_type):
                    continue
                score = 4 if role_terms.search(name) else 0
                score += 3 if column.get("comment") else 0
                score += 2 if column.get("primary_key") else 0
                usable.append((score, -position, name))
            if usable:
                candidates.append((row_count.bit_length(), len(usable), table, usable))

        samples = {}
        for _size_score, _width, table, usable in sorted(candidates, reverse=True)[:table_limit]:
            selected = [name for _score, _position, name in sorted(usable, reverse=True)[:column_limit]]
            select_list = ", ".join(self._quote_identifier(name) for name in selected)
            query = f"SELECT {select_list} FROM {self._quote_identifier(table)} LIMIT {int(row_limit)}"
            try:
                with self.engine(database).connect() as connection:
                    rows = connection.exec_driver_sql(query).mappings().all()
            except Exception:
                continue
            table_samples = {}
            for name in selected:
                values = []
                for row in rows:
                    value = row.get(name)
                    if value is None or isinstance(value, (bytes, bytearray, dict, list, tuple, set)):
                        continue
                    if value not in values:
                        values.append(value)
                if values:
                    table_samples[name] = values[:row_limit]
            if table_samples:
                samples[table] = table_samples

        self._profile_samples_cache[database] = samples
        return samples

    def analyze_question(self, question, database):
        return business_catalog.analyze_question(
            question,
            self.inspect_schema(database),
            source_type="mysql",
            row_counts={
                item["table"]: item["approx_rows"]
                for item in self._largest_tables(database)
            },
            fingerprint=self.schema_fingerprint(database),
        )

    def _semantic_mentions(self, question, semantic_plan=None):
        plan = semantic_plan or semantic_parser.parse(question).to_dict()
        return entity_resolver.candidates_from_plan(plan)

    def resolve_entity(
        self,
        question,
        database,
        entity_type=None,
        semantic_plan=None,
        semantic_binding=None,
    ):
        started = time.perf_counter()
        semantic_plan = semantic_plan or semantic_parser.parse(question).to_dict()
        schema = self.inspect_schema(database)
        row_counts = self._approx_row_counts(database)
        for table, row_count in row_counts.items():
            if table in schema:
                schema[table]["row_count"] = row_count
        catalog = self.business_catalog(database)
        database_fp = self.database_fingerprint(database)
        schema_fp = self.schema_fingerprint(database)
        extracted = self._semantic_mentions(question, semantic_plan)
        retrieval_context = self._entity_retrieval_context(
            question,
            database,
            semantic_plan,
            semantic_binding=semantic_binding,
        )
        if entity_type is None and len(extracted) == 1:
            predicted = extracted[0].get("predicted_types") or []
            entity_type = predicted[0] if len(predicted) == 1 else None
        matches = []
        cache_hits = 0

        for item in extracted:
            entity_text = item["text"]
            cached = entity_resolver.get_cached(database_fp, schema_fp, entity_text, entity_type)
            if cached and cached.get("matches"):
                cache_hits += 1
                matches.extend(cached["matches"])
                continue

            candidates = self._entity_candidates(
                schema=schema,
                catalog=catalog,
                entity_type=entity_type,
                semantic_binding=semantic_binding,
                retrieval_context=retrieval_context,
                max_columns=60 if entity_type else 35,
            )
            entity_matches = self._lookup_entity_value(database, entity_text, candidates)
            if (
                not entity_matches
                and retrieval_context["tables"]
                and not self._has_verified_entity_scope(
                    semantic_binding,
                    entity_type,
                )
            ):
                fallback_candidates = entity_resolver.candidate_columns(
                    schema,
                    catalog=catalog,
                    entity_type=entity_type,
                    max_columns=80 if entity_type else 45,
                )
                entity_matches = self._lookup_entity_value(database, entity_text, fallback_candidates)
            if entity_matches:
                result = {"matches": entity_matches[:8]}
                entity_resolver.set_cached(database_fp, schema_fp, entity_text, result, entity_type)
                matches.extend(entity_matches)

        matches = self._dedupe_entity_matches(matches)
        matches = sorted(
            matches,
            key=lambda item: (-item["confidence"], -item.get("anchor_score", 0), item["table"], item["column"]),
        )[:12]
        ambiguous = self._is_entity_resolution_ambiguous(matches)
        return {
            "question": question,
            "extracted_entities": extracted,
            "matches": matches,
            "ambiguous": ambiguous,
            "clarification_required": ambiguous,
            "lookup_time_ms": round((time.perf_counter() - started) * 1000, 2),
            "cache_hits": cache_hits,
            "schema_fingerprint": schema_fp,
            "catalog_version": catalog.get("catalog_version"),
            "searched_tables_first": retrieval_context["tables"],
        }

    def resolve_entities(
        self,
        question,
        database,
        semantic_plan=None,
        semantic_binding=None,
    ):
        started = time.perf_counter()
        semantic_plan = semantic_plan or semantic_parser.parse(question).to_dict()
        schema = self.inspect_schema(database)
        row_counts = self._approx_row_counts(database)
        for table, row_count in row_counts.items():
            if table in schema:
                schema[table]["row_count"] = row_count
        catalog = self.business_catalog(database)
        database_fp = self.database_fingerprint(database)
        schema_fp = self.schema_fingerprint(database)
        extracted = self._semantic_mentions(question, semantic_plan)
        retrieval_context = self._entity_retrieval_context(
            question,
            database,
            semantic_plan,
            semantic_binding=semantic_binding,
        )
        mention_results = []
        cache_hits = 0

        for item in extracted:
            mention = item["text"]
            predicted_types = item.get("predicted_types") or entity_resolver.predict_types_from_plan(mention, semantic_plan or {})
            predicted_types = [item for item in predicted_types if item]
            if semantic_binding and semantic_binding.get("authoritative"):
                approved_types = set(
                    (semantic_binding.get("entity_locations") or {}).keys()
                )
                verified_predictions = [
                    entity_type for entity_type in predicted_types
                    if self._canonical_entity_type(entity_type) in approved_types
                ]
                if verified_predictions:
                    predicted_types = verified_predictions
            search_types = predicted_types or [None]
            mention_matches = []
            mention_cache_hits = 0
            for entity_type in search_types[:3]:
                cached = entity_resolver.get_cached(database_fp, schema_fp, mention, entity_type)
                if cached and cached.get("matches"):
                    mention_cache_hits += 1
                    mention_matches.extend(cached["matches"])
                    continue
                candidates = self._entity_candidates(
                    schema=schema,
                    catalog=catalog,
                    entity_type=entity_type,
                    semantic_binding=semantic_binding,
                    retrieval_context=retrieval_context,
                    max_columns=60 if entity_type else 35,
                )
                found = self._lookup_entity_value(database, mention, candidates)
                if (
                    not found
                    and retrieval_context["tables"]
                    and not self._has_verified_entity_scope(
                        semantic_binding,
                        entity_type,
                    )
                ):
                    fallback_candidates = entity_resolver.candidate_columns(
                        schema,
                        catalog=catalog,
                        entity_type=entity_type,
                        max_columns=80 if entity_type else 45,
                    )
                    found = self._lookup_entity_value(database, mention, fallback_candidates)
                if found:
                    entity_resolver.set_cached(
                        database_fp,
                        schema_fp,
                        mention,
                        {"matches": found[:8]},
                        entity_type,
                    )
                    mention_matches.extend(found)
            cache_hits += mention_cache_hits
            mention_matches = self._dedupe_entity_matches(mention_matches)
            mention_matches = sorted(
                mention_matches,
                key=lambda match: (-match["confidence"], -match.get("anchor_score", 0), match["table"], match["column"]),
            )[:8]
            mention_matches = entity_resolver.rank_matches(mention_matches, predicted_types)
            mention_results.append({
                "mention": mention,
                "source": item.get("source"),
                "matches": mention_matches,
                "ambiguous": entity_resolver.should_clarify_matches(mention_matches, predicted_types),
                "predicted_types": predicted_types,
                "essential": entity_resolver.mention_is_essential(mention, semantic_plan),
            })

        draft_multi = entity_resolver.build_multi_result(
            question,
            mention_results,
            semantic_plan=semantic_plan,
        )
        relationships = self._validate_entity_relationships(schema, draft_multi["entities"])
        multi = entity_resolver.build_multi_result(
            question,
            mention_results,
            relationships=relationships,
            semantic_plan=semantic_plan,
        )
        flat_matches = [
            match
            for mention in mention_results
            for match in mention["matches"][:3]
        ]
        return {
            **multi,
            "question": question,
            "extracted_entities": extracted,
            "mention_results": mention_results,
            "matches": flat_matches[:12],
            "ambiguous": multi["needs_clarification"],
            "clarification_required": multi["needs_clarification"],
            "lookup_time_ms": round((time.perf_counter() - started) * 1000, 2),
            "cache_hits": cache_hits,
            "schema_fingerprint": schema_fp,
            "catalog_version": catalog.get("catalog_version"),
            "searched_tables_first": retrieval_context["tables"],
        }

    def resolve_verified_entities(
        self,
        question,
        database,
        semantic_plan=None,
        semantic_binding=None,
        expected_entity_types=None,
    ):
        started = time.perf_counter()
        semantic_plan = semantic_plan or semantic_parser.parse(question).to_dict()
        extracted = self._semantic_mentions(question, semantic_plan)
        supported_types = ["project", "vendor", "material", "client", "store"]
        search_types = [
            self._canonical_entity_type(item)
            for item in (expected_entity_types or supported_types)
            if self._canonical_entity_type(item) in supported_types
        ] or supported_types
        if not extracted:
            return {
                "question": question,
                "extracted_entities": [],
                "mention_results": [],
                "entities": [],
                "matches": [],
                "unresolved": [],
                "ambiguities": [],
                "relationships": [],
                "needs_clarification": False,
                "ambiguous": False,
                "clarification_required": False,
                "combined_confidence": 100,
                "lookup_time_ms": round((time.perf_counter() - started) * 1000, 2),
                "cache_hits": 0,
                "schema_fingerprint": None,
                "catalog_version": None,
                "searched_tables_first": [],
                "resolver_mode": "verified_entity_first",
            }
        schema = self.inspect_schema(database)
        row_counts = self._approx_row_counts(database)
        for table, row_count in row_counts.items():
            if table in schema:
                schema[table]["row_count"] = row_count
        catalog = self.business_catalog(database)
        database_fp = self.database_fingerprint(database)
        schema_fp = self.schema_fingerprint(database)
        retrieval_context = {"tables": [], "columns": {}}
        mention_results = []
        cache_hits = 0

        for item in extracted:
            mention = item["text"]
            mention_matches = []
            for entity_type in search_types:
                cached = entity_resolver.get_cached(database_fp, schema_fp, mention, entity_type)
                if cached and cached.get("matches"):
                    cache_hits += 1
                    mention_matches.extend(cached["matches"])
                    continue
                candidates = self._entity_candidates(
                    schema=schema,
                    catalog=catalog,
                    entity_type=entity_type,
                    semantic_binding=semantic_binding,
                    retrieval_context=retrieval_context,
                    max_columns=80,
                )
                found = self._lookup_entity_value(database, mention, candidates)
                found = [
                    match for match in found
                    if match.get("confidence", 0) >= 70
                    and match.get("entity_type") in supported_types
                ]
                if found:
                    entity_resolver.set_cached(
                        database_fp,
                        schema_fp,
                        mention,
                        {"matches": found[:10]},
                        entity_type,
                    )
                    mention_matches.extend(found)
            mention_matches = self._dedupe_entity_matches(mention_matches)
            mention_matches = self._collapse_verified_display_matches(mention_matches)
            mention_matches = sorted(
                mention_matches,
                key=lambda match: (
                    -match["confidence"],
                    self._entity_type_rank(match.get("entity_type")),
                    -match.get("anchor_score", 0),
                    match["table"],
                    match["column"],
                    str(match.get("value")),
                ),
            )[:10]
            mention_results.append({
                "mention": mention,
                "source": item.get("source"),
                "matches": mention_matches,
                "ambiguous": self._verified_mention_is_ambiguous(mention_matches),
                "predicted_types": search_types,
                "essential": entity_resolver.mention_is_essential(mention, semantic_plan),
            })

        entities = []
        unresolved = []
        ambiguities = []
        for result in mention_results:
            matches = result["matches"]
            if not matches:
                if result.get("essential"):
                    unresolved.append({
                        "mention": result["mention"],
                        "reason": "no_verified_entity_match",
                        "expected_entity_types": search_types,
                    })
                continue
            if result["ambiguous"]:
                ambiguities.append({
                    "mention": result["mention"],
                    "reason": self._verified_ambiguity_reason(matches),
                    "options": matches[:6],
                })
                continue
            best = matches[0]
            entities.append({
                "mention": result["mention"],
                "original_text": result["mention"],
                "normalized_text": entity_resolver.normalize(result["mention"]),
                "resolved_type": best["entity_type"],
                "entity_type": best["entity_type"],
                "table": best["table"],
                "column": best["column"],
                "primary_key_column": best.get("primary_key_column"),
                "primary_key": best.get("primary_key"),
                "primary_key_value": best.get("primary_key_value"),
                "value": best.get("value"),
                "matched_value": best.get("matched_value") or best.get("value"),
                "confidence": best["confidence"],
                "match_type": best.get("match_type"),
                "role": entity_resolver.role_for_plan(
                    result["mention"],
                    semantic_plan,
                    best["entity_type"],
                ),
            })

        relationships = self._validate_entity_relationships(schema, entities)
        flat_matches = [
            match for result in mention_results for match in result["matches"][:4]
        ]
        needs_clarification = bool(ambiguities or unresolved)
        return {
            "question": question,
            "extracted_entities": extracted,
            "mention_results": mention_results,
            "entities": entities,
            "matches": flat_matches[:16],
            "unresolved": unresolved,
            "ambiguities": ambiguities,
            "relationships": relationships,
            "needs_clarification": needs_clarification,
            "ambiguous": needs_clarification,
            "clarification_required": needs_clarification,
            "combined_confidence": min([item["confidence"] for item in entities], default=0 if extracted else 100),
            "lookup_time_ms": round((time.perf_counter() - started) * 1000, 2),
            "cache_hits": cache_hits,
            "schema_fingerprint": schema_fp,
            "catalog_version": catalog.get("catalog_version"),
            "searched_tables_first": [],
            "resolver_mode": "verified_entity_first",
        }

    def _entity_type_rank(self, entity_type):
        order = {"project": 0, "vendor": 1, "material": 2, "client": 3, "store": 4}
        return order.get(str(entity_type or ""), 99)

    def _collapse_verified_display_matches(self, matches):
        collapsed = {}
        for match in matches:
            key = (
                match.get("entity_type"),
                entity_resolver.normalize(match.get("matched_value") or match.get("value")),
            )
            current = collapsed.get(key)
            if not current:
                collapsed[key] = match
                continue
            current_score = (
                current.get("confidence", 0),
                current.get("anchor_score", 0),
                -self._entity_type_rank(current.get("entity_type")),
            )
            new_score = (
                match.get("confidence", 0),
                match.get("anchor_score", 0),
                -self._entity_type_rank(match.get("entity_type")),
            )
            if new_score > current_score:
                collapsed[key] = match
        return list(collapsed.values())

    def _verified_mention_is_ambiguous(self, matches):
        if len(matches) <= 1:
            return False
        high = [match for match in matches if match.get("confidence", 0) >= 88]
        distinct = {}
        for match in high:
            key = (
                match.get("entity_type"),
                entity_resolver.normalize(match.get("matched_value") or match.get("value")),
            )
            distinct.setdefault(key, match)
        high = list(distinct.values())
        if len(high) <= 1:
            return False
        high = sorted(
            high,
            key=lambda match: (
                -match.get("confidence", 0),
                -match.get("anchor_score", 0),
                self._entity_type_rank(match.get("entity_type")),
            ),
        )
        best = high[0]
        second = high[1]
        best_match_type = str(best.get("match_type") or "")
        second_match_type = str(second.get("match_type") or "")
        exact_types = {"exact_match", "exact_name", "case_insensitive_match", "trimmed_match"}
        weaker_types = {"alias_match", "partial_match", "fuzzy_match"}
        if (
            best_match_type in exact_types
            and second_match_type in weaker_types
            and (
                best.get("confidence", 0) - second.get("confidence", 0) >= 4
                or best.get("anchor_score", 0) - second.get("anchor_score", 0) >= 100
                or entity_resolver.normalize(second.get("matched_value") or second.get("value")).startswith(
                    entity_resolver.normalize(best.get("matched_value") or best.get("value"))
                )
            )
        ):
            return False
        if len({match.get("entity_type") for match in high}) > 1:
            return True
        same_type_high = [
            match for match in high
            if high and match.get("entity_type") == high[0].get("entity_type")
        ]
        if len(same_type_high) > 1:
            best = same_type_high[0].get("confidence", 0)
            second = same_type_high[1].get("confidence", 0)
            return best - second <= 6
        return False

    def _verified_ambiguity_reason(self, matches):
        if len({match.get("entity_type") for match in matches[:6]}) > 1:
            return "entity_type_ambiguity"
        return "multiple_verified_candidates"

    def _entity_retrieval_context(
        self,
        question,
        database,
        semantic_plan,
        semantic_binding=None,
    ):
        if semantic_binding and semantic_binding.get("authoritative"):
            locations = semantic_binding.get("entity_locations") or {}
            tables = []
            columns = {}
            for values in locations.values():
                for item in values:
                    table = item.get("table")
                    column = item.get("column")
                    if not table or not column:
                        continue
                    if table not in tables:
                        tables.append(table)
                    columns.setdefault(table, [])
                    if column not in columns[table]:
                        columns[table].append(column)
            if tables:
                return {
                    "tables": tables[:20],
                    "columns": columns,
                }
        try:
            context = self.retrieve_schema_context(
                question,
                database,
                semantic_plan=semantic_plan,
                candidate_limit=12,
            )
        except Exception:
            return {"tables": [], "columns": {}}
        return {
            "tables": list(context.get("tables") or [])[:12],
            "columns": dict(context.get("columns") or {}),
        }

    def _entity_candidates(
        self,
        *,
        schema,
        catalog,
        entity_type,
        semantic_binding,
        retrieval_context,
        max_columns,
    ):
        locations = self._verified_entity_locations(
            semantic_binding,
            entity_type,
        )
        if locations:
            candidates = []
            for item in locations:
                table = item.get("table")
                column = item.get("column")
                if table not in schema or not column:
                    continue
                if not any(
                    self._column_name(value) == column
                    for value in schema[table].get("columns", [])
                ):
                    continue
                candidates.append({
                    "table": table,
                    "column": column,
                    "entity_type": self._canonical_entity_type(entity_type)
                    if entity_type
                    else self._entity_type_for_location(
                        semantic_binding,
                        table,
                        column,
                    ),
                    "score": 200 + int(item.get("confidence_score") or 0),
                    "row_count": schema[table].get("row_count"),
                    "source": "verified_erp_catalog",
                })
            return candidates[:max_columns]
        return entity_resolver.candidate_columns(
            schema,
            catalog=catalog,
            entity_type=entity_type,
            max_columns=max_columns,
            relevant_tables=retrieval_context["tables"],
            relevant_columns=retrieval_context["columns"],
        )

    def _verified_entity_locations(self, semantic_binding, entity_type):
        if not semantic_binding or not semantic_binding.get("authoritative"):
            return []
        locations = semantic_binding.get("entity_locations") or {}
        if entity_type:
            return list(
                locations.get(self._canonical_entity_type(entity_type), [])
            )
        return [
            item
            for values in locations.values()
            for item in values
        ]

    def _has_verified_entity_scope(self, semantic_binding, entity_type):
        return bool(self._verified_entity_locations(semantic_binding, entity_type))

    def _entity_type_for_location(
        self,
        semantic_binding,
        table,
        column,
    ):
        for entity_type, locations in (
            semantic_binding.get("entity_locations") or {}
        ).items():
            if any(
                item.get("table") == table and item.get("column") == column
                for item in locations
            ):
                return entity_type
        return "reference"

    def _canonical_entity_type(self, entity_type):
        return {
            "customer": "client",
            "supplier": "vendor",
            "product": "material",
            "staff": "employee",
            "team": "department",
        }.get(str(entity_type or "").lower(), str(entity_type or "").lower())

    def _validate_entity_relationships(self, schema, entities):
        relationships = []
        for index, left in enumerate(entities):
            for right in entities[index + 1:]:
                if left.get("resolved_type") == right.get("resolved_type"):
                    relationships.append({
                        "from_entity": left["mention"],
                        "to_entity": right["mention"],
                        "relationship_type": "same_entity_type_comparison",
                        "confidence": 100,
                        "validated": True,
                        "join_path": [],
                    })
                    continue
                paths = relationship_discovery.find_paths(
                    schema,
                    [left["table"]],
                    [right["table"]],
                    max_hops=4,
                )
                if paths:
                    best = paths[0]
                    relationships.append({
                        "from_entity": left["mention"],
                        "to_entity": right["mention"],
                        "relationship_type": "validated_join_path",
                        "confidence": best["confidence_score"],
                        "validated": best["confidence_score"] >= 55,
                        "join_path": best,
                        "limitations": best.get("limitations", []),
                    })
                else:
                    relationships.append({
                        "from_entity": left["mention"],
                        "to_entity": right["mention"],
                        "relationship_type": "no_reliable_path",
                        "confidence": 0,
                        "validated": False,
                        "join_path": None,
                        "limitations": ["No reliable relationship path was found between these entity tables."],
                    })
        return relationships

    def metadata_prompt(self, database, question):
        memory = self.database_memory(database)
        return json.dumps(
            {
                **self.database_summary(database),
                "cached_database_memory": memory.get("overview_prompt"),
                "question_relevant_tables": self.relevant_tables(question, database, limit=6),
            },
            indent=2,
        )

    def _detected_modules(self, schema):
        module_keywords = {
            "hr": ("employee", "emp_", "payroll", "attendance", "designation", "department"),
            "projects": ("project", "site", "boq", "milestone", "dpr"),
            "finance": ("finance", "payment", "bank", "bill", "budget", "tax", "invoice"),
            "procurement": ("po_", "purchase", "vendor", "quote", "intend", "indent"),
            "assets": ("asset", "machinery", "repair", "breakdown"),
            "inventory": ("store", "material", "stock", "spare", "grn"),
            "logistics": ("logistic", "transport", "vehicle", "freight", "frieght"),
            "clients": ("client", "customer", "correspond"),
            "admin": ("admin", "module", "role", "privilege", "user"),
        }
        tables = [table.lower() for table in schema]
        modules = []
        for module, markers in module_keywords.items():
            if any(any(marker in table for marker in markers) for table in tables):
                modules.append(module)
        return modules

    def _important_tables(self, schema):
        noisy = re.compile(r"(?:hist|history|approval|attachment|access|log|notification)")
        scored = []
        for table, info in schema.items():
            lower = table.lower()
            score = len(info["columns"])
            if lower.endswith("_table") or lower.endswith("_master") or lower.endswith("_mst"):
                score += 20
            if any(marker in lower for marker in ["employee", "project", "client", "vendor", "material", "asset", "payment", "po_", "department"]):
                score += 15
            if noisy.search(lower):
                score -= 15
            scored.append((score, table))
        return [table for _, table in sorted(scored, key=lambda item: (-item[0], item[1]))]

    def _largest_tables(self, database):
        sql = """
        SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0) AS row_count
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_ROWS DESC
        LIMIT 20
        """
        with self.engine(database).connect() as connection:
            rows = connection.exec_driver_sql(sql).mappings().all()
        return [
            {"table": row["TABLE_NAME"], "approx_rows": int(row["row_count"] or 0)}
            for row in rows
        ]

    def _lookup_entity_value(self, database, entity_text, candidates):
        matches = []
        for candidate in candidates:
            table = candidate["table"]
            column = candidate["column"]
            variants = entity_resolver.lookup_variants(entity_text, candidate.get("entity_type"))
            normalized_variants = [entity_resolver.normalize(value) for value in variants]
            compact_variants = [entity_resolver.compact(value) for value in variants]
            like_terms = list(dict.fromkeys(
                term
                for normalized in normalized_variants
                for term in normalized.split()
                if len(term) >= 2
            ))[:6]
            pk = self._primary_key_column(self.inspect_schema(database).get(table, {}).get("columns", []))
            value_expr = f"CAST({self._quote_identifier(column)} AS CHAR)"
            normalized_expr = (
                f"LOWER(TRIM(REPLACE(REPLACE(REPLACE({value_expr}, '-', ' '), '_', ' '), '.', ' ')))"
            )
            compact_expr = (
                f"LOWER(REPLACE(REPLACE(REPLACE(REPLACE(TRIM({value_expr}), '-', ''), '_', ''), '.', ''), ' ', ''))"
            )
            conditions = []
            params = []
            for variant, normalized, compact in zip(variants, normalized_variants, compact_variants):
                conditions.extend([
                    f"LOWER(TRIM({value_expr})) = %s",
                    f"{normalized_expr} = %s",
                    f"{compact_expr} = %s",
                ])
                params.extend([variant.strip().lower(), normalized, compact])
            for term in like_terms:
                conditions.append(f"LOWER({value_expr}) LIKE %s")
                params.append(f"%{term}%")
            order_parts = []
            order_params = []
            for term in like_terms:
                order_parts.append(f"CASE WHEN LOWER({value_expr}) LIKE %s THEN 1 ELSE 0 END")
                order_params.append(f"%{term}%")
            fuzzy_term = max(like_terms, key=len, default="")
            if len(fuzzy_term) >= 3:
                conditions.append(f"SOUNDEX({value_expr}) = SOUNDEX(%s)")
                params.append(fuzzy_term)
            select_pk = self._quote_identifier(pk) if pk else "NULL"
            order_clause = ""
            if order_parts:
                order_clause = "ORDER BY (" + " + ".join(order_parts) + ") DESC "
            sql = (
                f"SELECT /*+ MAX_EXECUTION_TIME(1000) */ {select_pk} AS __pk, {self._quote_identifier(column)} AS __value "
                f"FROM {self._quote_identifier(table)} "
                f"WHERE {self._quote_identifier(column)} IS NOT NULL "
                f"AND ({' OR '.join(conditions)}) "
                f"{order_clause}"
                "LIMIT 300"
            )
            try:
                with self.engine(database).connect() as connection:
                    rows = connection.exec_driver_sql(sql, tuple([*params, *order_params])).mappings().all()
            except Exception:
                continue
            for row in rows:
                scored = entity_resolver.score_resolved_value(
                    entity_text,
                    row["__value"],
                    candidate.get("entity_type"),
                )
                if not scored:
                    continue
                confidence, match_type = scored
                confidence = min(99, confidence + min(8, candidate.get("score", 0) // 15))
                anchor_score = candidate.get("score", 0)
                if table.lower().endswith(("_mst", "_master")):
                    anchor_score += 25
                if column.lower() in {f"{candidate.get('entity_type')}_name", "name"}:
                    anchor_score += 15
                matches.append({
                    "entity": entity_text,
                    "entity_type": candidate.get("entity_type") or "reference",
                    "table": table,
                    "column": column,
                    "primary_key_column": pk,
                    "primary_key": row["__pk"],
                    "primary_key_value": row["__pk"],
                    "value": row["__value"],
                    "matched_value": row["__value"],
                    "confidence": confidence,
                    "anchor_score": anchor_score,
                    "match_type": match_type,
                    "source": "value_lookup",
                })
        return sorted(matches, key=lambda item: (-item["confidence"], -item.get("anchor_score", 0), item["table"], item["column"]))

    def _primary_key_column(self, columns):
        for column in columns:
            if column.get("primary_key") or column.get("COLUMN_KEY") == "PRI":
                return self._column_name(column)
        return self._best_column(columns, ("id", "code", "key"))

    def _dedupe_entity_matches(self, matches):
        deduped = {}
        for match in matches:
            key = (
                match["entity"].lower(),
                match["table"],
                match["column"],
                str(match.get("primary_key")),
                str(match.get("value")),
            )
            if key not in deduped or match["confidence"] > deduped[key]["confidence"]:
                deduped[key] = match
        return list(deduped.values())

    def _is_entity_resolution_ambiguous(self, matches):
        return entity_resolver.should_clarify_matches(matches)

    def _entity_type_hint(self, question):
        question_lower = question.lower()
        for entity_type, aliases in entity_resolver.ENTITY_HINTS.items():
            if any(re.search(rf"\b{re.escape(alias)}s?\b", question_lower) for alias in aliases):
                return entity_type
        return None

    def _relationship_hints(self, schema, tables):
        selected = set(tables[:30])
        column_to_tables = {}
        for table in selected:
            for column in schema[table]["columns"]:
                name = column["column_name"].lower()
                if name.endswith("_id") or name == "id":
                    column_to_tables.setdefault(name, []).append(table)
        return [
            {"column": column, "tables": owners[:6]}
            for column, owners in column_to_tables.items()
            if len(owners) > 1
        ]

    def relevant_tables(self, question, database, limit=20):
        memory = self.database_memory(database)
        memory_tables = database_memory.relevant_tables(question, memory, limit=limit)
        if memory_tables:
            return memory_tables

        question_lower = question.lower()
        words = self._expanded_words(question_lower)
        matches = []
        for table, info in self.inspect_schema(database).items():
            table_lower = table.lower()
            table_words = self._expanded_words(table_lower)
            column_words = set()
            for column in info["columns"]:
                column_words.update(self._expanded_words(column["column_name"].lower()))
            score = 0
            if table_lower in question_lower:
                score += 100
            if any(f"{word}_table" == table_lower for word in words):
                score += 80
            score += len(words & table_words) * 10
            score += len(words & column_words)
            if re.search(r"(?:hist|history|approval|attachment|access|log|notification)", table_lower):
                score -= 5
            if score > 0:
                matches.append((score, table))
        return [
            table
            for _, table in sorted(matches, key=lambda item: (-item[0], item[1]))[:limit]
        ]

    def count_table_sql(self, question, database):
        question_lower = question.lower()
        if not self._is_count_question(question_lower):
            return None
        if self._is_analytical_count_output_question(question_lower):
            return None
        schema = self.inspect_schema(database)
        generic_count = self._generic_entity_count_sql(
            question,
            schema,
            self._approx_row_counts(database),
        )
        if generic_count:
            return generic_count
        words = self._expanded_words(question_lower)
        for table in schema:
            if table.lower() in question_lower:
                return f"SELECT COUNT(*) AS row_count FROM {self._quote_identifier(table)}"
        for table in self.relevant_tables(question, database, limit=10):
            table_lower = table.lower()
            if any(table_lower == word or table_lower == f"{word}_table" for word in words):
                return f"SELECT COUNT(*) AS row_count FROM {self._quote_identifier(table)}"
        return None

    def business_overview_sql(self, question, database):
        question_lower = question.lower()
        if not any(word in question_lower for word in ["insight", "opportunity", "performing well", "overview"]):
            return None
        tables = [
            item["table"]
            for item in self.database_summary(database)["largest_tables"]
            if item["table"] in self.inspect_schema(database)
        ][:6]
        if not tables:
            return None
        parts = [
            f"SELECT '{table}' AS table_name, COUNT(*) AS row_count FROM {self._quote_identifier(table)}"
            for table in tables
        ]
        return "\nUNION ALL\n".join(parts) + "\nORDER BY row_count DESC"

    def catalog_sql(self, question, database):
        question_lower = question.lower()
        schema = self.inspect_schema(database)
        if self._is_count_question(question_lower) and not self._is_analytical_count_output_question(question_lower):
            return None

        if (
            any(word in question_lower for word in ["material", "materials"])
            and any(word in question_lower for word in ["issued", "issue", "usage", "used"])
            and "project" in question_lower
            and {"store_material_usage_quantity", "store_usage_quantity_master", "project_mst"}.issubset(schema)
        ):
            material_join = ""
            material_name = "CONCAT('Sub material ', q.sub_material_id)"
            material_uom = "NULL"
            if "sub_material" in schema:
                material_join = """
LEFT JOIN sub_material sm
    ON sm.sub_material_id = q.sub_material_id
""".rstrip()
                material_name = "COALESCE(sm.sub_material_name, CONCAT('Sub material ', q.sub_material_id))"
                material_uom = "sm.uom"
            return f"""
WITH dated_usage AS (
    SELECT
        m.*,
        STR_TO_DATE(NULLIF(m.issued_date, ''), '%%d-%%m-%%Y') AS issue_date
    FROM store_usage_quantity_master m
),
max_usage_date AS (
    SELECT MAX(issue_date) AS max_issue_date
    FROM dated_usage
),
project_totals AS (
    SELECT
        p.project_id,
        p.project_name,
        COUNT(*) AS issue_lines,
        SUM(q.issued_quantity) AS total_issued_quantity,
        SUM(q.issued_quantity * COALESCE(q.base_charge, 0)) AS total_issued_value
    FROM store_material_usage_quantity q
    JOIN dated_usage m
        ON q.store_usage_quantity_master_id = m.store_usage_quantity_master_id
    CROSS JOIN max_usage_date x
    JOIN project_mst p
        ON p.project_id = m.project_id
    WHERE m.issue_date >= DATE_SUB(x.max_issue_date, INTERVAL 30 DAY)
    GROUP BY p.project_id, p.project_name
),
selected_project AS (
    SELECT project_id
    FROM project_totals
    ORDER BY total_issued_value DESC
    LIMIT 1
)
SELECT
    p.project_name,
    {material_name} AS material_name,
    {material_uom} AS uom,
    ROUND(SUM(q.issued_quantity), 2) AS issued_quantity,
    ROUND(AVG(NULLIF(q.base_charge, 0)), 2) AS avg_base_charge,
    ROUND(MIN(NULLIF(q.base_charge, 0)), 2) AS min_base_charge,
    ROUND(MAX(NULLIF(q.base_charge, 0)), 2) AS max_base_charge,
    ROUND(SUM(q.issued_quantity * COALESCE(q.base_charge, 0)), 2) AS issued_value,
    MIN(m.issue_date) AS from_date,
    MAX(m.issue_date) AS to_date
FROM store_material_usage_quantity q
JOIN dated_usage m
    ON q.store_usage_quantity_master_id = m.store_usage_quantity_master_id
CROSS JOIN max_usage_date x
JOIN project_mst p
    ON p.project_id = m.project_id
JOIN selected_project sp
    ON sp.project_id = p.project_id
{material_join}
WHERE m.issue_date >= DATE_SUB(x.max_issue_date, INTERVAL 30 DAY)
GROUP BY p.project_name, material_name, uom
ORDER BY issued_value DESC
LIMIT 10
""".strip()

        if (
            "billing" in question_lower
            and any(word in question_lower for word in ["client", "customer"])
            and {"client_bill_raising", "project_mst"}.issubset(schema)
        ):
            limit = self._requested_limit(question_lower, default=10)
            return f"""
SELECT
    COALESCE(NULLIF(TRIM(p.client_name), ''), 'Unknown client') AS client_name,
    COUNT(*) AS bill_count,
    SUM(COALESCE(c.gross_bill_value, 0)) AS gross_bill_value,
    SUM(COALESCE(c.bill_amount, 0)) AS bill_amount,
    SUM(COALESCE(c.amount_to_be_received_this_bill, 0)) AS receivable_amount
FROM client_bill_raising c
JOIN project_mst p
    ON c.project_id = p.project_id
GROUP BY COALESCE(NULLIF(TRIM(p.client_name), ''), 'Unknown client')
ORDER BY gross_bill_value DESC
LIMIT {limit}
""".strip()

        if (
            "billing" in question_lower
            and any(word in question_lower for word in ["total", "sum", "overall", "gross"])
            and "client_bill_raising" in schema
        ):
            return """
SELECT
    COUNT(*) AS bill_count,
    ROUND(SUM(COALESCE(gross_bill_value, 0)), 2) AS gross_bill_value,
    ROUND(SUM(COALESCE(bill_amount, 0)), 2) AS bill_amount,
    ROUND(SUM(COALESCE(total_amount, 0)), 2) AS total_amount,
    ROUND(SUM(COALESCE(amount_to_be_received_this_bill, 0)), 2) AS receivable_amount
FROM client_bill_raising
""".strip()

        if (
            "billing" in question_lower
            and "project" in question_lower
            and {"client_bill_raising", "project_mst"}.issubset(schema)
        ):
            limit = self._requested_limit(question_lower, default=10)
            return f"""
SELECT
    p.project_id,
    p.project_name,
    COUNT(*) AS bill_count,
    SUM(COALESCE(c.gross_bill_value, 0)) AS gross_bill_value,
    SUM(COALESCE(c.bill_amount, 0)) AS bill_amount,
    SUM(COALESCE(c.amount_to_be_received_this_bill, 0)) AS receivable_amount
FROM client_bill_raising c
JOIN project_mst p
    ON c.project_id = p.project_id
GROUP BY p.project_id, p.project_name
ORDER BY gross_bill_value DESC
LIMIT {limit}
""".strip()

        generic_sql = self._generic_vendor_purchase_sql(question, schema)
        if generic_sql:
            return generic_sql

        generic_sql = self._generic_project_purchase_sql(question, schema)
        if generic_sql:
            return generic_sql

        generic_rank = self._generic_entity_rank_sql(
            question,
            schema,
            self._approx_row_counts(database),
        )
        if generic_rank:
            return generic_rank

        if (
            self._mentions_project(question_lower)
            and any(word in question_lower for word in ["list", "show", "random", "few", "some", "available"])
            and "project_mst" in schema
            and not any(word in question_lower for word in ["billing", "material", "issued", "used", "usage"])
        ):
            order_clause = "ORDER BY RAND()" if "random" in question_lower else "ORDER BY p.project_id"
            limit = self._requested_limit(question_lower, default=10)
            return f"""
SELECT
    p.project_id,
    p.project_name,
    p.client_name,
    p.project_value,
    p.project_start_date,
    p.project_end_date,
    p.project_state,
    p.status
FROM project_mst p
WHERE p.project_name IS NOT NULL
  AND TRIM(p.project_name) <> ''
{order_clause}
LIMIT {limit}
""".strip()

        generic_entity_list = self._generic_entity_list_sql(
            question,
            schema,
            self._approx_row_counts(database),
        )
        if generic_entity_list:
            return generic_entity_list

        if (
            "employee" in question_lower
            and "department" in question_lower
            and {"employee_table", "department"}.issubset(schema)
        ):
            return """
SELECT
    d.department_id,
    d.department_name,
    COUNT(e.employee_id) AS employee_count
FROM department d
LEFT JOIN employee_table e
    ON e.department_id = d.department_id
GROUP BY d.department_id, d.department_name
ORDER BY employee_count DESC
LIMIT 10
""".strip()

        if (
            "department" in question_lower
            and "perform" in question_lower
            and {"employee_table", "department"}.issubset(schema)
        ):
            return """
SELECT
    d.department_id,
    d.department_name,
    COUNT(e.employee_id) AS employee_count
FROM department d
LEFT JOIN employee_table e
    ON e.department_id = d.department_id
GROUP BY d.department_id, d.department_name
ORDER BY employee_count DESC
LIMIT 10
""".strip()

        return None

    def _generic_vendor_purchase_sql(self, question, schema):
        question_lower = question.lower()
        wants_vendor = any(word in question_lower for word in ["vendor", "supplier", "contractor", "seller"])
        wants_purchase = any(word in question_lower for word in ["purchase", "procurement", "po ", " po", "order value", "spend"])
        wants_rank = any(word in question_lower for word in ["highest", "most", "top", "largest", "maximum", "max", "biggest"])
        if not (wants_vendor and wants_purchase and wants_rank):
            return None

        candidates = []
        for table, info in schema.items():
            table_text = table.lower()
            if not any(marker in table_text for marker in ("purchase", "po", "procure", "indent", "quote", "vendor")):
                continue
            vendor_column = self._best_column(
                info["columns"],
                ("vendor_name", "supplier_name", "contractor_name", "seller_name", "party_name", "vendor"),
            )
            metric = self._best_purchase_metric_expression(info["columns"], alias="p")
            if not vendor_column or not metric:
                continue
            score = metric["score"]
            if table_text in {"po_form", "purchase_orders", "purchase_order", "po_header", "po_master"}:
                score += 25
            candidates.append({
                "table": table,
                "vendor_column": vendor_column,
                "metric_expression": metric["expression"],
                "metric_source": metric["source"],
                "score": score,
            })
        if not candidates:
            return None

        best = sorted(candidates, key=lambda item: (-item["score"], item["table"]))[0]
        limit = self._requested_limit(question_lower, default=1)
        return f"""
SELECT
    COALESCE(NULLIF(TRIM(p.{self._quote_identifier(best["vendor_column"])}), ''), 'Unknown vendor') AS vendor_name,
    COUNT(*) AS purchase_rows,
    ROUND(SUM({best["metric_expression"]}), 2) AS total_purchase_value,
    '{best["table"]}' AS source_table,
    '{best["metric_source"]}' AS value_source
FROM {self._quote_identifier(best["table"])} p
WHERE p.{self._quote_identifier(best["vendor_column"])} IS NOT NULL
GROUP BY COALESCE(NULLIF(TRIM(p.{self._quote_identifier(best["vendor_column"])}), ''), 'Unknown vendor')
HAVING total_purchase_value IS NOT NULL
ORDER BY total_purchase_value DESC
LIMIT {limit}
""".strip()

    def _generic_entity_count_sql(self, question, schema, row_counts=None):
        question_lower = question.lower()
        entity = self._entity_from_question(question_lower)
        if not entity:
            return None
        table = self._best_entity_table(
            schema,
            self._table_aliases_for_entity(entity),
            row_counts=row_counts,
        )
        if not table:
            return None
        name_column = self._best_entity_name_column(schema[table]["columns"], entity)
        alias = f"{self._safe_alias(entity)}_count"
        if name_column:
            return (
                f"SELECT COUNT(*) AS {alias}, "
                f"'{entity}' AS counted_entity, "
                f"'{table}' AS source_table "
                f"FROM {self._quote_identifier(table)} "
                f"WHERE {self._quote_identifier(name_column)} IS NOT NULL "
                f"AND TRIM(CAST({self._quote_identifier(name_column)} AS CHAR)) <> ''"
            )
        return (
            f"SELECT COUNT(*) AS {alias}, "
            f"'{entity}' AS counted_entity, "
            f"'{table}' AS source_table "
            f"FROM {self._quote_identifier(table)}"
        )

    def _generic_entity_rank_sql(self, question, schema, row_counts=None):
        question_lower = question.lower()
        if self._is_count_question(question_lower):
            return None
        if not any(word in question_lower for word in ["top", "highest", "largest", "maximum", "max", "biggest", "rank"]):
            return None
        entity = self._entity_from_question(question_lower)
        if not entity:
            return None
        table = self._best_entity_table(
            schema,
            self._table_aliases_for_entity(entity),
            row_counts=row_counts,
        )
        if not table:
            return None
        metric_column = self._best_rank_metric_column(schema[table]["columns"], entity, question_lower)
        if not metric_column:
            return None
        id_column = self._best_entity_id_column(schema[table]["columns"], entity)
        name_column = self._best_entity_name_column(schema[table]["columns"], entity)
        if not name_column:
            return None
        optional_columns = self._optional_entity_columns(schema[table]["columns"], entity)
        selected = [f"'{entity}' AS entity_type", f"'{table}' AS source_table"]
        if id_column:
            selected.append(f"p.{self._quote_identifier(id_column)} AS {self._safe_alias(entity)}_id")
        selected.append(f"p.{self._quote_identifier(name_column)} AS {self._safe_alias(entity)}_name")
        metric_expression = self._numeric_metric_expression("p", metric_column)
        metric_alias = self._safe_alias(metric_column)
        selected.append(f"ROUND({metric_expression}, 2) AS {metric_alias}")
        selected.append(f"ROUND({metric_expression}, 2) AS metric_value")
        selected.append(f"'{metric_column}' AS metric_column")
        for column in optional_columns:
            if column and column not in {id_column, name_column, metric_column}:
                selected.append(f"p.{self._quote_identifier(column)} AS {self._safe_alias(column)}")
        limit = self._requested_limit(question_lower, default=10)
        return f"""
SELECT
    {", ".join(selected)}
FROM {self._quote_identifier(table)} p
WHERE p.{self._quote_identifier(name_column)} IS NOT NULL
  AND p.{self._quote_identifier(metric_column)} IS NOT NULL
  AND TRIM(CAST(p.{self._quote_identifier(metric_column)} AS CHAR)) <> ''
ORDER BY metric_value DESC
LIMIT {limit}
""".strip()

    def _generic_entity_list_sql(self, question, schema, row_counts=None):
        question_lower = question.lower()
        if self._is_count_question(question_lower):
            return None
        asks_for_people_or_entities = bool(
            re.search(r"\bwho\s+(?:all\s+)?(?:are|is)\b", question_lower)
            or re.search(r"\bwhich\s+\w+s?\s+(?:are|exist|available)\b", question_lower)
        )
        if not (
            asks_for_people_or_entities
            or any(word in question_lower for word in ["list", "show", "random", "few", "some", "available"])
        ):
            return None
        if any(word in question_lower for word in ["billing", "material", "issued", "used", "usage", "purchase", "cost"]):
            return None
        entity = self._entity_from_question(question_lower)
        if not entity:
            return None
        table = self._best_entity_table(
            schema,
            self._table_aliases_for_entity(entity),
            row_counts=row_counts,
        )
        if not table:
            return None
        id_column = self._best_entity_id_column(schema[table]["columns"], entity)
        name_column = self._best_entity_name_column(schema[table]["columns"], entity)
        if not name_column:
            return None
        optional_columns = self._optional_entity_columns(schema[table]["columns"], entity)
        selected = [f"'{entity}' AS entity_type", f"'{table}' AS source_table"]
        if id_column:
            selected.append(f"p.{self._quote_identifier(id_column)} AS {self._safe_alias(entity)}_id")
        selected.append(f"p.{self._quote_identifier(name_column)} AS {self._safe_alias(entity)}_name")
        for column in optional_columns:
            if column and column not in {id_column, name_column}:
                selected.append(f"p.{self._quote_identifier(column)} AS {self._safe_alias(column)}")
        order_clause = "ORDER BY RAND()" if "random" in question_lower else f"ORDER BY p.{self._quote_identifier(id_column or name_column)}"
        limit = self._requested_limit(question_lower, default=10)
        return f"""
SELECT
    {", ".join(selected)}
FROM {self._quote_identifier(table)} p
WHERE p.{self._quote_identifier(name_column)} IS NOT NULL
{order_clause}
LIMIT {limit}
""".strip()

    def _is_count_question(self, question_lower):
        if any(phrase in question_lower for phrase in self.COUNT_WORDS):
            return True
        if re.search(r"\bhow\s+many\b", question_lower):
            return True
        if re.search(r"\b(?:are|is)\s+(?:there|it)\b", question_lower) and re.search(r"\b\d+\b", question_lower):
            return True
        if re.search(r"\b(?:only|ony)\s+\d+\b", question_lower):
            return True
        return bool(re.search(r"\b(?:sure|actually|really)\b", question_lower) and re.search(r"\b\d+\b", question_lower))

    def _is_analytical_count_output_question(self, question_lower):
        has_metric = any(
            word in question_lower
            for word in ["billing", "bill amount", "purchase", "value", "amount", "cost", "spend", "revenue"]
        )
        has_ranking_or_comparison = any(
            word in question_lower
            for word in ["top", "highest", "largest", "compare", "rank", "where"]
        )
        return has_metric and has_ranking_or_comparison

    def _entity_from_question(self, question_lower):
        words = self._expanded_words(question_lower)
        compact = re.sub(r"[^a-z0-9]+", " ", question_lower)
        best = None
        best_score = 0
        for entity, aliases in self.ENTITY_ALIASES.items():
            score = 0
            for alias in aliases:
                alias_words = self._expanded_words(alias)
                if alias in compact:
                    score += 20
                score += len(words & alias_words) * 8
            if score > best_score:
                best = entity
                best_score = score
        return best if best_score else None

    def _best_entity_id_column(self, columns, entity):
        aliases = (
            f"{entity}_id",
            f"{entity}_code",
            "id",
            "code",
            "slno",
            "sno",
        )
        if entity == "project":
            aliases = ("project_id", "pf_project_id", "job_id", "work_order_id", *aliases)
        return self._best_column(columns, aliases)

    def _best_rank_metric_column(self, columns, entity, question_lower):
        if any(word in question_lower for word in ["value", "amount", "worth"]):
            aliases = (
                f"{entity}_value",
                f"{entity}_amount",
                "total_value",
                "total_amount",
                "value",
                "amount",
                "estimated_value",
                "gross_value",
                "net_value",
            )
        elif any(word in question_lower for word in ["price", "rate"]):
            aliases = ("price", "unit_price", "rate", "base_charge", "amount", "value")
        else:
            aliases = (
                f"{entity}_value",
                "total_value",
                "total_amount",
                "value",
                "amount",
                "price",
                "quantity",
                "qty",
                "count",
            )
        return self._best_column(columns, aliases)

    def _numeric_metric_expression(self, table_alias, column):
        quoted = f"{table_alias}.{self._quote_identifier(column)}"
        return f"CAST({quoted} AS DECIMAL(30, 2))"

    def _best_entity_name_column(self, columns, entity):
        aliases = (
            f"{entity}_name",
            f"{entity}_title",
            "name",
            "title",
            "description",
            "code",
        )
        if entity == "client":
            aliases = ("client_name", "customer_name", "party_name", *aliases)
        if entity == "vendor":
            aliases = ("vendor_name", "supplier_name", "contractor_name", *aliases)
        if entity == "employee":
            aliases = ("employee_name", "emp_name", "first_name", "full_name", *aliases)
        if entity == "project":
            aliases = ("project_name", "job_name", "site_name", *aliases)
        return self._best_column(columns, aliases)

    def _optional_entity_columns(self, columns, entity):
        common = [
            ("status", "state", "stage"),
            ("type", "category"),
            ("start_date", "from_date", "created_date", "date"),
            ("end_date", "to_date"),
            ("amount", "value", "total", "price"),
        ]
        entity_specific = {
            "project": [
                ("client_name", "customer_name", "client", "customer"),
                ("project_value", "estimated_value", "value", "amount"),
                ("project_start_date", "start_date", "from_date"),
                ("project_end_date", "end_date", "to_date"),
                ("project_state", "state", "location"),
                ("status", "project_status"),
            ],
            "employee": [
                ("department_name", "department_id", "dept_id"),
                ("designation", "role"),
                ("status", "employee_status"),
            ],
            "product": [
                ("category", "product_category", "uom", "unit"),
                ("price", "rate", "base_charge"),
            ],
        }
        selected = []
        for aliases in entity_specific.get(entity, []) + common:
            column = self._best_column(columns, aliases)
            if column and column not in selected:
                selected.append(column)
        return selected[:8]

    def _mentions_project(self, question_lower):
        return bool(re.search(r"\bproj?e?cts?\b|\bprjcts?\b|\bprjects?\b", question_lower))

    def _safe_alias(self, value):
        alias = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value).lower()).strip("_")
        return alias or "value"

    def _requested_limit(self, question_lower, default=10, maximum=50):
        word_numbers = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
        }
        match = re.search(r"\b(?:top|first|list|show|give me|display)?\s*(\d{1,2})\b", question_lower)
        if match:
            return max(1, min(maximum, int(match.group(1))))
        for word, value in word_numbers.items():
            if re.search(rf"\b{word}\b", question_lower):
                return value
        if "few" in question_lower:
            return 5
        return default

    def _generic_project_purchase_sql(self, question, schema):
        question_lower = question.lower()
        wants_project = any(word in question_lower for word in ["project", "job", "work order", "workorder"])
        wants_purchase = any(word in question_lower for word in ["purchase", "procurement", "po ", " po", "costed", "cost"])
        wants_rank = any(word in question_lower for word in ["highest", "most", "top", "largest", "maximum", "max"])
        if not (wants_project and wants_purchase and wants_rank):
            return None

        project_aliases = ("project", "job", "work_order", "workorder") if "work" in question_lower else ("project", "job")
        project_table = self._best_entity_table(schema, project_aliases)
        if not project_table:
            return None
        project_id = self._best_column(schema[project_table]["columns"], ("project_id", "id", "job_id", "work_order_id"))
        project_name = self._best_column(schema[project_table]["columns"], ("project_name", "name", "title", "description", "code"))
        if not project_id or not project_name:
            return None

        candidates = []
        for table, info in schema.items():
            table_text = table.lower()
            if not any(marker in table_text for marker in ("purchase", "po", "procure", "indent", "quote", "vendor")):
                continue
            join_column = self._best_join_column(info["columns"], project_table, project_id)
            if not join_column:
                continue
            metric = self._best_purchase_metric_expression(info["columns"], alias="m")
            if not metric:
                continue
            score = metric["score"]
            if table_text in {"po_form", "purchase_orders", "purchase_order", "po_header", "po_master"}:
                score += 20
            if "summary" in table_text:
                score += 5
            candidates.append({
                "table": table,
                "join_column": join_column,
                "metric_expression": metric["expression"],
                "metric_source": metric["source"],
                "score": score,
            })
        if not candidates:
            return None
        best = sorted(candidates, key=lambda item: (-item["score"], item["table"]))[0]
        return f"""
SELECT
    p.{self._quote_identifier(project_id)} AS project_id,
    p.{self._quote_identifier(project_name)} AS project_name,
    COUNT(*) AS purchase_rows,
    ROUND(SUM({best["metric_expression"]}), 2) AS total_purchase_value,
    '{best["table"]}' AS source_table,
    '{best["metric_source"]}' AS value_source
FROM {self._quote_identifier(best["table"])} m
JOIN {self._quote_identifier(project_table)} p
    ON p.{self._quote_identifier(project_id)} = m.{self._quote_identifier(best["join_column"])}
GROUP BY p.{self._quote_identifier(project_id)}, p.{self._quote_identifier(project_name)}
HAVING total_purchase_value IS NOT NULL
ORDER BY total_purchase_value DESC
LIMIT 10
""".strip()

    def _best_entity_table(self, schema, aliases, row_counts=None):
        row_counts = row_counts or {}
        scored = []
        noisy = re.compile(r"(hist|history|approval|attachment|access|log|notification)", re.I)
        primary_alias = aliases[0] if aliases else ""
        for table, info in schema.items():
            lower = table.lower()
            text = " ".join([lower, *[self._column_name(column).lower() for column in info["columns"]]])
            score = sum(25 for alias in aliases if alias in lower)
            score += sum(4 for alias in aliases if alias in text)
            for alias in aliases:
                if alias in lower and self._best_column(info["columns"], (f"{alias}_id", "id")):
                    score += 8
                if alias in lower and self._best_column(info["columns"], (f"{alias}_name", "name", "title")):
                    score += 14
                if lower == alias or lower.startswith(f"{alias}_"):
                    score += 10
            if primary_alias and lower in {
                primary_alias,
                f"{primary_alias}_mst",
                f"{primary_alias}_master",
                f"{primary_alias}_table",
            }:
                score += 80
            if lower.endswith(("_mst", "_master", "_table")):
                score += 18
            approx_rows = int(row_counts.get(table) or info.get("row_count") or 0)
            if approx_rows > 0:
                score += min(30, approx_rows.bit_length() * 3)
            elif lower == primary_alias:
                score -= 35
            if primary_alias == "project" and re.search(r"\b(project_)?sites?\b|site_", lower):
                score -= 60
            if "work_order" in lower or "workorder" in lower:
                score -= 12
            if noisy.search(lower):
                score -= 20
            if score > 0:
                scored.append((score, table))
        return sorted(scored, key=lambda item: (-item[0], item[1]))[0][1] if scored else None

    def _approx_row_counts(self, database):
        return {
            item["table"]: item["approx_rows"]
            for item in self._largest_tables(database)
        }

    def _table_aliases_for_entity(self, entity):
        return self.ENTITY_TABLE_ALIASES.get(entity, self.ENTITY_ALIASES.get(entity, (entity,)))

    def _column_name(self, column):
        return column.get("column_name") or column.get("name") or column.get("COLUMN_NAME") or ""

    def _best_column(self, columns, aliases):
        normalized = {self._column_name(column).lower(): self._column_name(column) for column in columns}
        for alias in aliases:
            if alias in normalized:
                return normalized[alias]
        scored = []
        for column in columns:
            name = self._column_name(column)
            lower = name.lower()
            score = sum(10 for alias in aliases if alias in lower)
            if score:
                scored.append((score, name))
        return sorted(scored, key=lambda item: (-item[0], item[1]))[0][1] if scored else None

    def _best_join_column(self, columns, project_table, project_id):
        names = [self._column_name(column) for column in columns]
        lowered = {name.lower(): name for name in names}
        project_base = re.sub(r"(_mst|_master|_table)$", "", project_table.lower())
        aliases = [
            project_id.lower(),
            f"{project_base}_id",
            "project_id",
            "job_id",
            "work_order_id",
            "workorder_id",
            "pf_project_id",
            "po_project_id",
        ]
        for alias in aliases:
            if alias in lowered:
                return lowered[alias]
        return None

    def _best_purchase_metric_expression(self, columns, alias):
        names = [self._column_name(column) for column in columns]
        lowered = {name.lower(): name for name in names}
        direct_priority = [
            "total_value",
            "total_price",
            "grand_total",
            "net_value",
            "net_amount",
            "amount",
            "sub_total",
            "disfinaltotal",
            "base_charge",
        ]
        for name in direct_priority:
            if name in lowered:
                column = lowered[name]
                return {
                    "expression": f"COALESCE({alias}.{self._quote_identifier(column)}, 0)",
                    "source": column,
                    "score": 90 if name.startswith("total") or name in {"net_value", "net_amount"} else 70,
                }
        qty = self._best_column(columns, ("qty", "quantity", "po_qty", "ordered_quantity"))
        price = self._best_column(columns, ("unit_price", "item_unit_price", "unitprices", "price", "rate", "base_charge"))
        if qty and price:
            return {
                "expression": f"COALESCE({alias}.{self._quote_identifier(qty)}, 0) * COALESCE({alias}.{self._quote_identifier(price)}, 0)",
                "source": f"{qty} * {price}",
                "score": 80,
            }
        return None

    def _expanded_words(self, text):
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        expanded = set(words)
        for word in words:
            if word.endswith("ies") and len(word) > 3:
                expanded.add(f"{word[:-3]}y")
            if word.endswith("s") and len(word) > 3:
                expanded.add(word[:-1])
            if word.endswith("es") and len(word) > 4:
                expanded.add(word[:-2])
        return expanded

    def schema_fingerprint(self, database):
        if database not in self._schema_fingerprint_cache:
            self._schema_fingerprint_cache[database] = schema_fingerprint(
                self.inspect_schema(database)
            )
        return self._schema_fingerprint_cache[database]

    def database_fingerprint(self, database, connection_id=None):
        return database_fingerprint(
            source_type="mysql",
            dialect="mysql",
            database_name=database,
            connection_id=connection_id or f"mysql:{database}",
            host_identity=safe_host_identity(MYSQL_HOST, MYSQL_PORT),
        )

    def source_identity(self, database):
        return f"mysql:{safe_host_identity(MYSQL_HOST, MYSQL_PORT)}:{database}"

    def validate_sql(self, sql, database):
        stripped = sql.strip().rstrip(";")
        if ";" in stripped:
            return {"valid": False, "errors": ["Multiple SQL statements are not allowed."]}
        if not re.match(r"^(SELECT|WITH)\b", stripped, re.IGNORECASE):
            return {"valid": False, "errors": ["Only SELECT and WITH queries are allowed."]}
        if self.BLOCKED.search(stripped) or self.BLOCKED_STATEMENT.search(stripped):
            return {"valid": False, "errors": ["Unsafe MySQL keyword detected."]}
        if re.search(r"\b(?:FROM|JOIN)\s+`?[A-Za-z_][\w]*`?\.`?[A-Za-z_][\w]*`?", stripped, re.IGNORECASE):
            return {"valid": False, "errors": ["Cross-database references are not allowed."]}

        schema = self.inspect_schema(database)
        known_tables = set(schema)
        cte_names = set()
        if re.match(r"^WITH\b", stripped, re.IGNORECASE):
            cte_names = {
                match.group(1)
                for match in re.finditer(
                    r"(?:WITH|,)\s+`?([A-Za-z_][\w]*)`?\s+AS\s*\(",
                    stripped,
                    flags=re.IGNORECASE,
                )
            }
        referenced = {match.group(1) for match in self.TABLE_REF.finditer(stripped)}
        unknown = sorted(
            table for table in referenced if table not in known_tables and table not in cte_names
        )
        if unknown:
            return {"valid": False, "errors": [f"Unknown table(s): {', '.join(unknown)}"]}
        duplicate_errors = self._validate_duplicate_safe_aggregations(stripped, database, schema)
        if duplicate_errors:
            return {"valid": False, "errors": duplicate_errors}
        return {"valid": True, "errors": []}

    def _validate_duplicate_safe_aggregations(self, sql, database, schema):
        if not re.search(r"\bSUM\s*\(", sql, re.IGNORECASE) or not re.search(r"\bJOIN\b", sql, re.IGNORECASE):
            return []
        try:
            catalog = self.business_catalog(database)
        except Exception:
            return []
        risky_relationships = [
            item for item in catalog.get("join_paths", [])
            if item.get("required_pre_aggregation")
            or item.get("duplicate_amplification_risk") in {"medium", "high"}
        ]
        if not risky_relationships:
            return []

        alias_map = self._sql_alias_map(sql, set(schema))
        reverse_aliases = {}
        for alias, table in alias_map.items():
            reverse_aliases.setdefault(table, set()).add(alias)
        sum_refs = self._sum_references(sql, alias_map, schema)
        if not sum_refs:
            return []

        errors = []
        for relationship in risky_relationships:
            detail_table = relationship.get("left_table") or relationship.get("source_table")
            header_table = relationship.get("right_table") or relationship.get("target_table")
            detail_column = relationship.get("left_column") or relationship.get("source_column")
            header_column = relationship.get("right_column") or relationship.get("target_column")
            if not detail_table or not header_table:
                continue
            if detail_table not in reverse_aliases or header_table not in reverse_aliases:
                continue
            if not self._has_join_condition(sql, reverse_aliases[detail_table], detail_column, reverse_aliases[header_table], header_column):
                continue
            header_metric_columns = self._header_metric_columns(schema, catalog, header_table)
            risky_sums = [
                f"{table}.{column}"
                for table, column in sum_refs
                if table == header_table and column in header_metric_columns
            ]
            if risky_sums:
                errors.append(
                    "Duplicate-prone aggregation detected: "
                    f"{', '.join(sorted(set(risky_sums)))} is summed after joining "
                    f"{header_table} to detail/fact table {detail_table}. "
                    "Pre-aggregate at the header or detail grain in a CTE/subquery before joining."
                )
        return errors

    def _sql_alias_map(self, sql, known_tables):
        aliases = {}
        for match in re.finditer(
            r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w]*)`?(?:\s+(?:AS\s+)?`?([A-Za-z_][\w]*)`?)?",
            sql,
            flags=re.IGNORECASE,
        ):
            table, alias = match.groups()
            if table not in known_tables:
                continue
            alias = alias or table
            if alias.upper() in {"ON", "WHERE", "GROUP", "ORDER", "LIMIT", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER"}:
                alias = table
            aliases[alias] = table
            aliases[table] = table
        return aliases

    def _sum_references(self, sql, alias_map, schema):
        refs = []
        for match in re.finditer(
            r"\bSUM\s*\(\s*(?:DISTINCT\s+)?(?:(`?[A-Za-z_][\w]*`?)\s*\.\s*)?`?([A-Za-z_][\w]*)`?",
            sql,
            flags=re.IGNORECASE,
        ):
            raw_alias, column = match.groups()
            alias = raw_alias.strip("`") if raw_alias else None
            if alias:
                table = alias_map.get(alias)
                if table:
                    refs.append((table, column))
                continue
            owners = [
                table for table, info in schema.items()
                if any((item.get("column_name") or item.get("name")) == column for item in info.get("columns", []))
            ]
            if len(owners) == 1:
                refs.append((owners[0], column))
        return refs

    def _has_join_condition(self, sql, detail_aliases, detail_column, header_aliases, header_column):
        if not detail_column or not header_column:
            return True
        comparisons = re.findall(
            r"`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?\s*=\s*`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?",
            sql,
            flags=re.IGNORECASE,
        )
        for left_alias, left_column, right_alias, right_column in comparisons:
            direct = (
                left_alias in detail_aliases
                and left_column == detail_column
                and right_alias in header_aliases
                and right_column == header_column
            )
            reverse = (
                right_alias in detail_aliases
                and right_column == detail_column
                and left_alias in header_aliases
                and left_column == header_column
            )
            if direct or reverse:
                return True
        return False

    def _header_metric_columns(self, schema, catalog, table):
        amount_markers = ("amount", "total", "value", "price", "cost", "revenue", "gross", "net", "balance")
        columns = {
            item.get("column")
            for item in catalog.get("metrics", [])
            if item.get("table") == table and item.get("column")
        }
        for column in schema.get(table, {}).get("columns", []):
            name = column.get("column_name") or column.get("name")
            data_type = str(column.get("data_type") or column.get("type") or "")
            if name and any(marker in name.lower() for marker in amount_markers) and re.search(r"int|decimal|numeric|float|double|real", data_type, re.I):
                columns.add(name)
        return columns

    def referenced_tables(self, sql):
        stripped = str(sql or "").strip().rstrip(";")
        cte_names = {
            match.group(1)
            for match in re.finditer(
                r"(?:WITH|,)\s+`?([A-Za-z_][\w]*)`?\s+AS\s*\(",
                stripped,
                flags=re.IGNORECASE,
            )
        }
        return list(dict.fromkeys(
            match.group(1)
            for match in self.TABLE_REF.finditer(stripped)
            if match.group(1) not in cte_names
        ))

    def execute_read_query(self, sql, database):
        validation = self.validate_sql(sql, database)
        if not validation["valid"]:
            raise ValueError("; ".join(validation["errors"]))

        cleaned = sql.strip().rstrip(";")
        limited_sql = cleaned if re.search(r"\bLIMIT\b", cleaned, re.IGNORECASE) else f"SELECT * FROM ({cleaned}) AS limited_query LIMIT {QUERY_ROW_LIMIT}"
        limited_sql = self._escape_percent_date_formats(limited_sql)
        with self.engine(database).connect() as connection:
            result = connection.exec_driver_sql(limited_sql)
            rows = result.mappings().all()
            return pd.DataFrame([dict(row) for row in rows], columns=result.keys())

    def _escape_percent_date_formats(self, sql):
        def repl(match):
            return match.group(0).replace("%", "%%")

        return re.sub(r"'%[A-Za-z][^']*'", repl, sql)

    def dry_run_query(self, sql, database):
        validation = self.validate_sql(sql, database)
        if not validation["valid"]:
            return {**validation, "query_plan": []}
        with self.engine(database).connect() as connection:
            rows = connection.exec_driver_sql(f"EXPLAIN {sql.strip().rstrip(';')}").mappings().all()
        return {**validation, "query_plan": [dict(row) for row in rows]}


mysql_adapter = MySQLAdapter()

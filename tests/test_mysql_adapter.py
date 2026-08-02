import active_database
import mcp_server
from mysql_adapter import MySQLAdapter


def test_mysql_dump_detection(tmp_path):
    adapter = MySQLAdapter()
    mysql_dump = tmp_path / "mysql.sql"
    plain_sql = tmp_path / "plain.sql"
    mysql_dump.write_text(
        "CREATE TABLE `people` (`id` int AUTO_INCREMENT) ENGINE=InnoDB;",
        encoding="utf-8",
    )
    plain_sql.write_text("CREATE TABLE people (id int);", encoding="utf-8")

    assert adapter.is_mysql_dump(mysql_dump) is True
    assert adapter.is_mysql_dump(plain_sql) is False


def test_mysql_statement_reader_streams_without_full_file(tmp_path):
    adapter = MySQLAdapter()
    sql_file = tmp_path / "dump.sql"
    sql_file.write_text(
        "-- comment\nSET FOREIGN_KEY_CHECKS=0;\nCREATE TABLE `people` (`id` int);\n",
        encoding="utf-8",
    )

    assert list(adapter._read_statements(sql_file)) == [
        "SET FOREIGN_KEY_CHECKS=0",
        "CREATE TABLE `people` (`id` int)",
    ]


def test_mysql_statement_reader_rejects_delimiter(tmp_path):
    adapter = MySQLAdapter()
    sql_file = tmp_path / "routine.sql"
    sql_file.write_text("DELIMITER //\nCREATE PROCEDURE p() SELECT 1//", encoding="utf-8")

    try:
        list(adapter._read_statements(sql_file))
        assert False
    except ValueError as error:
        assert "DELIMITER" in str(error)


def test_mysql_native_import_falls_back_to_python(monkeypatch, tmp_path):
    adapter = MySQLAdapter()
    sql_file = tmp_path / "dump.sql"
    sql_file.write_text("CREATE TABLE `people` (`id` int) ENGINE=InnoDB;", encoding="utf-8")
    calls = []

    monkeypatch.setattr(adapter, "file_fingerprint", lambda path: "abc123")
    monkeypatch.setattr(adapter, "create_database", lambda database: calls.append(("create", database)))
    monkeypatch.setattr(adapter, "drop_database", lambda database: calls.append(("drop", database)))
    monkeypatch.setattr(adapter, "_native_import", lambda path, database: (_ for _ in ()).throw(RuntimeError("native failed")))
    monkeypatch.setattr(adapter, "_python_import", lambda path, database: calls.append(("python", database)))
    monkeypatch.setattr(adapter, "schema_fingerprint", lambda database: "schema123")
    monkeypatch.setattr(adapter, "_save_import_state", lambda database, source, fingerprint, schema: {
        "source_type": "mysql",
        "connection_id": f"mysql:{database}:abc123",
        "database": database,
        "name": source,
        "fingerprint": fingerprint,
        "schema_fingerprint": schema,
    })

    state = adapter.import_sql_file(sql_file, "dump.sql", "session-a")

    assert ("python", state["database"]) in calls
    assert state["import_method"] == "python"
    assert state["native_error"] == "native failed"
    assert state["progress"] == ["Preparing database", "Importing", "Completed"]


def test_mysql_native_import_success_skips_python(monkeypatch, tmp_path):
    adapter = MySQLAdapter()
    sql_file = tmp_path / "dump.sql"
    sql_file.write_text("CREATE TABLE `people` (`id` int) ENGINE=InnoDB;", encoding="utf-8")
    calls = []

    monkeypatch.setattr(adapter, "file_fingerprint", lambda path: "abc123")
    monkeypatch.setattr(adapter, "create_database", lambda database: calls.append(("create", database)))
    monkeypatch.setattr(adapter, "_native_import", lambda path, database: "docker")
    monkeypatch.setattr(adapter, "_python_import", lambda path, database: calls.append(("python", database)))
    monkeypatch.setattr(adapter, "schema_fingerprint", lambda database: "schema123")
    monkeypatch.setattr(adapter, "_save_import_state", lambda database, source, fingerprint, schema: {
        "source_type": "mysql",
        "connection_id": f"mysql:{database}:abc123",
        "database": database,
        "name": source,
        "fingerprint": fingerprint,
        "schema_fingerprint": schema,
    })

    state = adapter.import_sql_file(sql_file, "dump.sql", "session-a")

    assert ("python", state["database"]) not in calls
    assert state["import_method"] == "native:docker"


def test_mysql_list_allowed_databases_filters_system_and_prefix(monkeypatch):
    adapter = MySQLAdapter()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def exec_driver_sql(self, sql):
            class Result:
                def fetchall(self):
                    return [
                        ("mysql",),
                        ("sys",),
                        ("sql_assistant_one",),
                        ("other_app",),
                    ]

            return Result()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    monkeypatch.setattr(adapter, "engine", lambda database=None: FakeEngine())

    assert adapter.list_allowed_databases() == ["other_app", "sql_assistant_one"]


def test_mysql_duplicate_upload_database_names_do_not_collide():
    adapter = MySQLAdapter()

    first = adapter._safe_database_name("session-a", "abc123")
    second = adapter._safe_database_name("session-a", "abc123")

    assert first != second
    assert first.startswith("sql_assistant_session_a_abc123")


def test_mysql_validate_accepts_select_and_rejects_unsafe(monkeypatch):
    adapter = MySQLAdapter()
    monkeypatch.setattr(
        adapter,
        "inspect_schema",
        lambda database: {
            "people": {
                "columns": [{"column_name": "id", "name": "id", "data_type": "int"}],
                "foreign_keys": [],
            }
        },
    )

    assert adapter.validate_sql("SELECT id FROM people", "db")["valid"] is True
    assert adapter.validate_sql("WITH p AS (SELECT id FROM people) SELECT * FROM p", "db")["valid"] is True
    assert adapter.validate_sql("DELETE FROM people", "db")["valid"] is False
    assert adapter.validate_sql("SELECT * FROM other_db.people", "db")["valid"] is False
    assert adapter.validate_sql("SELECT * FROM missing", "db")["valid"] is False


def test_mysql_validate_rejects_duplicate_prone_header_sum_after_detail_join(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "invoice_header": {
            "columns": [
                {"column_name": "invoice_id", "name": "invoice_id", "data_type": "int", "primary_key": True},
                {"column_name": "invoice_total", "name": "invoice_total", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
        "invoice_detail": {
            "columns": [
                {"column_name": "invoice_line_id", "name": "invoice_line_id", "data_type": "int", "primary_key": True},
                {"column_name": "invoice_id", "name": "invoice_id", "data_type": "int"},
                {"column_name": "product_name", "name": "product_name", "data_type": "varchar"},
                {"column_name": "line_amount", "name": "line_amount", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
    }
    catalog = {
        "metrics": [{"metric": "billing", "table": "invoice_header", "column": "invoice_total"}],
        "join_paths": [{
            "left_table": "invoice_detail",
            "left_column": "invoice_id",
            "right_table": "invoice_header",
            "right_column": "invoice_id",
            "duplicate_amplification_risk": "medium",
            "required_pre_aggregation": True,
        }],
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(adapter, "business_catalog", lambda database: catalog)

    validation = adapter.validate_sql(
        """
        SELECT d.product_name, SUM(h.invoice_total) AS total_amount
        FROM invoice_header h
        JOIN invoice_detail d ON d.invoice_id = h.invoice_id
        GROUP BY d.product_name
        """,
        "db",
    )

    assert validation["valid"] is False
    assert "Duplicate-prone aggregation detected" in validation["errors"][0]
    assert "Pre-aggregate" in validation["errors"][0]


def test_mysql_validate_allows_detail_sum_on_risky_join(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "invoice_header": {
            "columns": [
                {"column_name": "invoice_id", "name": "invoice_id", "data_type": "int", "primary_key": True},
                {"column_name": "invoice_total", "name": "invoice_total", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
        "invoice_detail": {
            "columns": [
                {"column_name": "invoice_id", "name": "invoice_id", "data_type": "int"},
                {"column_name": "line_amount", "name": "line_amount", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
    }
    catalog = {
        "metrics": [{"metric": "billing", "table": "invoice_header", "column": "invoice_total"}],
        "join_paths": [{
            "left_table": "invoice_detail",
            "left_column": "invoice_id",
            "right_table": "invoice_header",
            "right_column": "invoice_id",
            "duplicate_amplification_risk": "medium",
            "required_pre_aggregation": True,
        }],
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(adapter, "business_catalog", lambda database: catalog)

    validation = adapter.validate_sql(
        """
        SELECT h.invoice_id, SUM(d.line_amount) AS line_total
        FROM invoice_header h
        JOIN invoice_detail d ON d.invoice_id = h.invoice_id
        GROUP BY h.invoice_id
        """,
        "db",
    )

    assert validation["valid"] is True


def test_mysql_validate_allows_preaggregated_header_sum_before_join(monkeypatch):
    adapter = MySQLAdapter()
    schema = {
        "invoice_header": {
            "columns": [
                {"column_name": "invoice_id", "name": "invoice_id", "data_type": "int", "primary_key": True},
                {"column_name": "invoice_total", "name": "invoice_total", "data_type": "decimal"},
            ],
            "foreign_keys": [],
        },
        "invoice_detail": {
            "columns": [
                {"column_name": "invoice_id", "name": "invoice_id", "data_type": "int"},
                {"column_name": "product_name", "name": "product_name", "data_type": "varchar"},
            ],
            "foreign_keys": [],
        },
    }
    catalog = {
        "metrics": [{"metric": "billing", "table": "invoice_header", "column": "invoice_total"}],
        "join_paths": [{
            "left_table": "invoice_detail",
            "left_column": "invoice_id",
            "right_table": "invoice_header",
            "right_column": "invoice_id",
            "duplicate_amplification_risk": "medium",
            "required_pre_aggregation": True,
        }],
    }
    monkeypatch.setattr(adapter, "inspect_schema", lambda database: schema)
    monkeypatch.setattr(adapter, "business_catalog", lambda database: catalog)

    validation = adapter.validate_sql(
        """
        WITH header_totals AS (
            SELECT invoice_id, SUM(invoice_total) AS invoice_total
            FROM invoice_header
            GROUP BY invoice_id
        )
        SELECT d.product_name, SUM(ht.invoice_total) AS total_amount
        FROM header_totals ht
        JOIN invoice_detail d ON d.invoice_id = ht.invoice_id
        GROUP BY d.product_name
        """,
        "db",
    )

    assert validation["valid"] is True


def test_session_can_store_mysql_connection(monkeypatch, tmp_path):
    monkeypatch.setattr(active_database, "SESSIONS_DIR", tmp_path / "sessions")

    active_database.save_session_mysql_connection(
        "mysql-session",
        "mysql:db:fingerprint",
        "sql_assistant_mysql_session",
        "dump.sql",
        "schema123",
    )

    source = active_database.get_session_source("mysql-session")

    assert source["source_type"] == "mysql"
    assert source["connection_id"] == "mysql:db:fingerprint"
    assert active_database.get_session_database_path("mysql-session") is None


def test_mcp_mysql_tools_use_session_source(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "get_session_source",
        lambda session_id: {
            "source_type": "mysql",
            "database": "sql_assistant_test",
            "connection_id": "mysql:sql_assistant_test:abc",
        },
    )
    monkeypatch.setattr(
        mcp_server.mysql_adapter,
        "inspect_schema",
        lambda database: {
            "people": {
                "columns": [{"column_name": "id", "name": "id", "data_type": "int"}],
                "foreign_keys": [],
            }
        },
    )
    monkeypatch.setattr(
        mcp_server.mysql_adapter,
        "validate_sql",
        lambda sql, database: {"valid": True, "errors": []},
    )

    schema = mcp_server.inspect_schema(session_id="mysql-session")
    validation = mcp_server.validate_sql("SELECT id FROM people", session_id="mysql-session")

    assert schema["source_type"] == "mysql"
    assert schema["schema"]["people"]["columns"][0]["column_name"] == "id"
    assert validation["valid"] is True
    assert validation["source_type"] == "mysql"

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from config import (
    UPLOAD_FOLDER,
    SUPPORTED_DATABASES,
    SUPPORTED_DATABASE_NOTES,
    MAX_UPLOAD_BYTES,
    SQLSERVER_DATABASE,
    SQLSERVER_IMPORT_SQL,
    MYSQL_IMPORT_SQL,
    LLM_PROVIDER,
    LLM_MODEL,
    LLM_API_KEYS,
)
import os
import logging
import sqlite3
import uuid
from database_manager import db_manager
from schema_reader import SchemaReader
from session_memory import session_memory
from chat_pipeline import chat_pipeline
from metrics import token_metrics
from clarification_context import (
    clear_clarification_context,
    consume_unrelated_turn,
    get_clarification_context,
    save_clarification_context,
)
from active_database import (
    clear_session_database_path,
    get_session_database_path,
    get_session_source,
    save_session_database_path,
    save_session_mysql_connection,
    save_session_sqlserver_connection,
)
from mysql_adapter import mysql_adapter
from sqlserver_adapter import sqlserver_adapter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


class ChatRequest(BaseModel):
    question: str
    session_id: str


class SessionRequest(BaseModel):
    session_id: str


class MySQLAttachRequest(BaseModel):
    session_id: str
    database: str

app = FastAPI(
    title="Enterprise SQL AI Plugin",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    safe = "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in name
    )
    return safe or "database"


def _unique_upload_path(folder: str, filename: str) -> str:
    safe_name = _safe_filename(filename)
    stem = Path(safe_name).stem or "database"
    suffix = Path(safe_name).suffix.lower()
    return os.path.join(folder, f"{stem}_{uuid.uuid4().hex[:12]}{suffix}")


def _remove_file_safely(file_path: str | None):
    if file_path and os.path.exists(file_path):
        db_manager.disconnect()
        os.remove(file_path)


def _warm_mysql_memory(database: str):
    try:
        mysql_adapter.database_memory(database)
    except Exception as error:
        logging.warning("MySQL database memory warmup failed: %s", error)


os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)


def _present_chat_response(response: dict):
    public = dict(response)
    public.pop("metrics", None)
    public.pop("clarification_context", None)
    if "confidence" not in public:
        public["confidence"] = {
            "confidence_score": 60 if public.get("success") else 35,
            "confidence_level": "medium" if public.get("success") else "low",
            "confidence_reasons": [
                "Response was generated through the validated backend flow."
            ],
            "limitations": [] if public.get("success") else ["The request did not complete successfully."],
        }
    return public


def _finalize_clarification_state(
    session_id: str,
    original_question: str,
    response: dict,
    filled_slots: dict | None = None,
):
    if response.get("sql") or response.get("result"):
        clear_clarification_context(session_id)
        return
    context = chat_pipeline.build_clarification_context(original_question, response)
    if context:
        if filled_slots:
            merged_filled = dict(context.get("filled_slots") or {})
            merged_filled.update(filled_slots)
            context["filled_slots"] = merged_filled
        save_clarification_context(session_id, context)


@app.get("/")
async def root():
    return RedirectResponse(url="/docs")

@app.post("/upload")
async def upload_database(
    file: UploadFile = File(...),
    session_id: str = Form(...),
):

    file_path = None
    try:
        # Validate before using session_id as part of an upload path.
        get_session_database_path(session_id)

        # ---------------------------------
        # Validate File
        # ---------------------------------

        if not file.filename:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file must have a filename."
            )

        extension = Path(file.filename).suffix.lower()

        if extension not in SUPPORTED_DATABASES:

            raise HTTPException(
                status_code=400,
                detail="Unsupported database format."
            )

        # ---------------------------------
        # Save Uploaded File
        # ---------------------------------

        os.makedirs(
            UPLOAD_FOLDER,
            exist_ok=True
        )

        session_upload_folder = os.path.join(UPLOAD_FOLDER, session_id)
        os.makedirs(session_upload_folder, exist_ok=True)
        file_path = _unique_upload_path(session_upload_folder, file.filename)

        with open(file_path, "wb") as buffer:
            size = 0
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Uploaded file is too large. Limit is {MAX_UPLOAD_BYTES} bytes.",
                    )
                buffer.write(chunk)

        if os.path.getsize(file_path) == 0:
            raise HTTPException(
                status_code=400,
                detail="Uploaded database file is empty.",
            )

        # ---------------------------------
        # Connect Database
        # ---------------------------------

        if extension == ".sql" and MYSQL_IMPORT_SQL and mysql_adapter.is_mysql_dump(file_path):
            import_state = mysql_adapter.import_sql_file(
                file_path,
                file.filename,
                session_id,
            )
            schema = mysql_adapter.inspect_schema(import_state["database"])
            table_count = len(schema)
            column_count = sum(len(info["columns"]) for info in schema.values())
            save_session_mysql_connection(
                session_id,
                import_state["connection_id"],
                import_state["database"],
                file.filename,
                import_state.get("schema_fingerprint"),
                table_count,
                column_count,
            )
            clear_clarification_context(session_id)
            session_memory.reset()
            _warm_mysql_memory(import_state["database"])
            return {
                "success": True,
                "database": {
                    "name": file.filename,
                    "type": "MYSQL",
                    "tables": table_count,
                    "columns": column_count,
                    "connection_id": import_state["connection_id"],
                    "already_imported": import_state.get("already_imported", False),
                    "source_type": "mysql",
                    "import_method": import_state.get("import_method"),
                    "import_duration_seconds": import_state.get("import_duration_seconds"),
                    "progress": import_state.get("progress", []),
                },
                "message": "MySQL dump imported into MySQL successfully.",
            }

        if extension == ".sql" and SQLSERVER_IMPORT_SQL:
            import_state = sqlserver_adapter.import_sql_file(file_path, file.filename)
            save_session_sqlserver_connection(
                session_id,
                import_state["connection_id"],
                import_state["database"],
                file.filename,
                import_state.get("schema_fingerprint"),
                import_state.get("imported_tables", []),
            )
            clear_clarification_context(session_id)
            session_memory.reset()
            return {
                "success": True,
                "database": {
                    "name": file.filename,
                    "type": "SQLSERVER",
                    "tables": len(sqlserver_adapter.inspect_schema()),
                    "columns": sum(
                        len(info["columns"])
                        for info in sqlserver_adapter.inspect_schema().values()
                    ),
                    "connection_id": import_state["connection_id"],
                    "already_imported": import_state.get("already_imported", False),
                },
                "message": "SQL dump imported into SQL Server successfully.",
            }

        db_manager.connect(file_path)

        # ---------------------------------
        # Read Schema
        # ---------------------------------

        reader = SchemaReader()

        schema = reader.read_schema()

        summary = reader.get_database_summary()
        if summary["tables"] == 0:
            raise HTTPException(
                status_code=400,
                detail="Uploaded database does not contain any tables.",
            )

        active_database_path = db_manager.database_path or file_path
        save_session_database_path(
            session_id,
            active_database_path,
            file.filename,
        )
        clear_clarification_context(session_id)

        # ---------------------------------
        # Store Session
        # ---------------------------------

        session_memory.reset()

        session_memory.set_database(
            file.filename,
            file_path
        )

        session_memory.set_schema(schema)

        # ---------------------------------
        # Return Success
        # ---------------------------------

        return {
                "success": True,
                "database": {
                "name": file.filename,
                "type": db_manager.get_database_type().upper(),
                "tables": summary["tables"],
                "columns": summary["columns"]
                },
                "message": "Database uploaded successfully."
}

    except HTTPException:
        _remove_file_safely(file_path)
        raise

    except sqlite3.DatabaseError as e:
        _remove_file_safely(file_path)
        raise HTTPException(
            status_code=400,
            detail=f"Database could not be read: {e}",
        )

    except Exception as e:
        _remove_file_safely(file_path)

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        if chat_pipeline.is_casual_message(request.question):
            token_metrics.start()
            response = chat_pipeline.casual_response(
                has_database=bool(get_session_source(request.session_id))
            )
            token_metrics.log_summary()
            return _present_chat_response(response)

        session_source = get_session_source(request.session_id)
        if session_source and session_source.get("source_type") == "sqlserver":
            identity = "|".join(
                [
                    "sqlserver",
                    session_source.get("database", SQLSERVER_DATABASE),
                    session_source.get("schema_fingerprint", ""),
                ]
            )
            return _present_chat_response(
                chat_pipeline.ask_sqlserver(
                    request.question,
                    identity,
                    session_source.get("imported_tables", []),
                )
            )

        if session_source and session_source.get("source_type") == "mysql":
            identity = "|".join(
                [
                    "mysql",
                    session_source.get("database", ""),
                    session_source.get("schema_fingerprint", ""),
                ]
            )
            effective_question = request.question
            pending_context = get_clarification_context(request.session_id)
            filled_slots = {}
            original_clarification_question = request.question
            if pending_context:
                original_clarification_question = (
                    pending_context.get("original_question") or request.question
                )
                merge = chat_pipeline.merge_clarification_followup(
                    request.question,
                    pending_context,
                )
                if merge["unrelated"]:
                    clear_clarification_context(request.session_id)
                elif merge["handled"]:
                    effective_question = merge["question"]
                    filled_slots = merge.get("filled_slots") or {}
                else:
                    consume_unrelated_turn(request.session_id, pending_context)

            response = chat_pipeline.ask_mysql(
                effective_question,
                session_source["database"],
                identity,
            )
            _finalize_clarification_state(
                request.session_id,
                original_clarification_question if pending_context else effective_question,
                response,
                filled_slots,
            )
            return _present_chat_response(response)

        database_path = get_session_database_path(request.session_id)
        if not database_path or not Path(database_path).is_file():
            raise ValueError(
                "No database is available for this session. Upload a database first."
            )

        db_manager.connect(database_path)
        reader = SchemaReader()
        schema = reader.read_schema()
        session_memory.reset()
        session_memory.set_database(Path(database_path).name, database_path)
        session_memory.set_schema(schema)
        return _present_chat_response(chat_pipeline.ask(request.question))

    except ValueError as e:
        token_metrics.log_summary()
        raise HTTPException(status_code=400, detail=str(e))

    except RuntimeError as e:
        token_metrics.log_summary()
        raise HTTPException(status_code=503, detail=str(e))

    except Exception as e:
        token_metrics.log_summary()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status")
async def status(session_id: str | None = None):
    session_source = (
        get_session_source(session_id)
        if session_id
        else None
    )
    database_path = (
        session_source.get("path")
        if session_source and session_source.get("source_type") == "sqlite"
        else None
    )
    sqlserver_connected = bool(
        session_source and session_source.get("source_type") == "sqlserver"
    )
    mysql_connected = bool(
        session_source and session_source.get("source_type") == "mysql"
    )

    return {
        "success": True,
        "backend": "online",
        "database_connected": bool(database_path) or sqlserver_connected or mysql_connected,
        "database": {
            "database_name": (
                Path(database_path).name
                if database_path
                else session_source.get("database")
                if sqlserver_connected or mysql_connected
                else None
            ),
            "database_path": database_path,
            "source_type": session_source.get("source_type") if session_source else None,
            "connection_id": (
                session_source.get("connection_id")
                if sqlserver_connected or mysql_connected
                else None
            ),
            "tables": session_source.get("tables") if session_source else None,
            "columns": session_source.get("columns") if session_source else None,
        },
        "schema_loaded": bool(database_path) or sqlserver_connected or mysql_connected,
        "supported_uploads": SUPPORTED_DATABASE_NOTES,
        "llm": {
            "provider": LLM_PROVIDER,
            "model": LLM_MODEL or "provider default",
            "keys_configured": len(LLM_API_KEYS)
        }
    }


@app.post("/clear")
async def clear(request: SessionRequest):
    clear_session_database_path(request.session_id)
    clear_clarification_context(request.session_id)
    session_memory.reset()
    db_manager.disconnect()

    return {
        "success": True,
        "message": "Session cleared."
    }


@app.get("/mysql/databases")
async def list_mysql_databases():
    try:
        return {
            "success": True,
            "databases": mysql_adapter.list_allowed_databases(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"MySQL is unavailable: {e}",
        )


@app.get("/mysql/test")
async def test_mysql_connection():
    try:
        return {
            "success": True,
            "connected": mysql_adapter.test_connection(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"MySQL is unavailable: {e}",
        )


@app.get("/mysql/schema")
async def inspect_mysql_schema(database: str):
    try:
        if database not in mysql_adapter.list_allowed_databases():
            raise ValueError("Selected MySQL database is not configured or allowed.")
        schema = mysql_adapter.inspect_schema(database)
        return {
            "success": True,
            "database": database,
            "tables": len(schema),
            "columns": sum(len(info["columns"]) for info in schema.values()),
            "schema": schema,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"MySQL schema could not be read: {e}",
        )


@app.post("/mysql/attach")
async def attach_mysql_database(request: MySQLAttachRequest):
    try:
        state = mysql_adapter.attach_database(request.database)
        schema = mysql_adapter.inspect_schema(state["database"])
        table_count = len(schema)
        column_count = sum(len(info["columns"]) for info in schema.values())
        save_session_mysql_connection(
            request.session_id,
            state["connection_id"],
            state["database"],
            state.get("name"),
            state.get("schema_fingerprint"),
            table_count,
            column_count,
        )
        clear_clarification_context(request.session_id)
        session_memory.reset()
        _warm_mysql_memory(state["database"])
        return {
            "success": True,
            "database": {
                "name": state["database"],
                "type": "MYSQL",
                "tables": table_count,
                "columns": column_count,
                "connection_id": state["connection_id"],
                "already_imported": True,
                "source_type": "mysql",
                "import_method": "existing",
                "import_duration_seconds": 0,
                "progress": state.get("progress", []),
            },
            "message": "Connected to existing MySQL database.",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"MySQL database could not be attached: {e}",
        )

"""SQLAlchemy dialect for HawDB's embedded SQL interface."""

from __future__ import annotations

import importlib

from sqlalchemy.engine import default
from sqlalchemy.pool import NullPool, SingletonThreadPool
from sqlalchemy.sql.compiler import GenericTypeCompiler, IdentifierPreparer


class HawDBIdentifierPreparer(IdentifierPreparer):
    """Quote identifiers using SQL-standard double quotes."""

    reserved_words = IdentifierPreparer.reserved_words | {"match", "return", "graph"}


class HawDBTypeCompiler(GenericTypeCompiler):
    """Compile SQLAlchemy types to HawDB's supported PostgreSQL type subset."""

    def visit_big_integer(self, type_, **kw):
        return "BIGINT"

    visit_integer = visit_big_integer
    visit_small_integer = visit_big_integer

    def visit_boolean(self, type_, **kw):
        return "BOOLEAN"

    def visit_double(self, type_, **kw):
        return "DOUBLE PRECISION"

    visit_float = visit_double
    visit_real = visit_double
    visit_numeric = visit_double

    def visit_string(self, type_, **kw):
        return "TEXT"

    visit_text = visit_string
    visit_unicode = visit_string
    visit_unicode_text = visit_string

    def visit_large_binary(self, type_, **kw):
        return "BYTEA"

    def visit_uuid(self, type_, **kw):
        return "UUID"

    def visit_date(self, type_, **kw):
        return "TEXT"

    visit_time = visit_date
    visit_datetime = visit_date

    def visit_enum(self, type_, **kw):
        return "TEXT"


class HawDBDialect(default.DefaultDialect):
    name = "hawdb"
    driver = "embedded"
    supports_statement_cache = True
    supports_native_boolean = True
    supports_native_uuid = True
    supports_sane_rowcount = False
    supports_sane_multi_rowcount = False
    supports_sequences = False
    supports_identity_columns = False
    supports_default_values = False
    supports_empty_insert = False
    supports_alter = False
    supports_comments = False
    postfetch_lastrowid = False
    default_paramstyle = "numeric_dollar"
    preparer = HawDBIdentifierPreparer
    type_compiler_cls = HawDBTypeCompiler

    @classmethod
    def import_dbapi(cls):
        try:
            return importlib.import_module("hawdb.dbapi")
        except ImportError as error:
            raise ImportError(
                "HawDB's Python binding is required; install it with "
                "python -m pip install 'hawdb>=0.23.0,<0.24.0'"
            ) from error

    def create_connect_args(self, url):
        if url.username or url.password or url.host or url.port:
            raise ValueError(
                "Invalid HawDB URL: provide a local database path without "
                "credentials or a network host"
            )
        query = dict(url.query)
        read_only = query.pop("read_only", "false").lower() in {"1", "true", "yes"}
        if query:
            options = ", ".join(sorted(query))
            raise ValueError(f"unsupported HawDB URL option(s): {options}")
        path = url.database
        if path in (None, ":memory:"):
            path = None
        return (
            [path],
            {"read_only": read_only, "autocommit": False},
        )

    def do_begin(self, dbapi_connection):
        # HawDB's PEP 249 connection begins on its first statement.
        return None

    def do_rollback(self, dbapi_connection):
        dbapi_connection.rollback()

    def do_commit(self, dbapi_connection):
        dbapi_connection.commit()

    @classmethod
    def get_pool_class(cls, url):
        # File handles own their path and cannot be shared across threads.
        # In-memory URLs need a per-thread handle to persist across checkouts.
        if url.database in (None, ":memory:"):
            return SingletonThreadPool
        return NullPool

    def get_server_version_info(self, connection):
        import hawdb

        return tuple(int(part) for part in hawdb.__version__.split(".")[:3])

    def get_default_schema_name(self, connection):
        return "public"

    def get_schema_names(self, connection, **kw):
        return ["public"]

    def has_table(self, connection, table_name, schema=None, **kw):
        if schema not in (None, "public"):
            return False
        result = connection.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = $1",
            (table_name,),
        )
        return result.first() is not None

    def get_table_names(self, connection, schema=None, **kw):
        if schema not in (None, "public"):
            return []
        result = connection.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
        return [row[0] for row in result]

    def get_view_names(self, connection, schema=None, **kw):
        if schema not in (None, "public"):
            return []
        result = connection.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'MATERIALIZED VIEW' "
            "ORDER BY table_name"
        )
        return [row[0] for row in result]

    def get_columns(self, connection, table_name, schema=None, **kw):
        if schema not in (None, "public"):
            return []
        result = connection.exec_driver_sql(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1 "
            "ORDER BY ordinal_position",
            (table_name,),
        )
        return [
            {
                "name": row[0],
                "type": self._type_from_name(row[1]),
                "nullable": row[2] == "YES",
                "default": row[3],
                "autoincrement": False,
                "comment": None,
            }
            for row in result
        ]

    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        if schema not in (None, "public"):
            return {"name": None, "constrained_columns": []}
        result = connection.exec_driver_sql(
            "SELECT indexname, indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = 'public' AND tablename = $1 ORDER BY indexname",
            (table_name,),
        )
        primary_key_name = f"{table_name}_pkey"
        for row in result:
            if row[0] == primary_key_name:
                return {
                    "name": row[0],
                    "constrained_columns": self._index_columns(row[1]),
                }
        return {"name": None, "constrained_columns": []}

    def get_indexes(self, connection, table_name, schema=None, **kw):
        if schema not in (None, "public"):
            return []
        result = connection.exec_driver_sql(
            "SELECT indexname, indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = 'public' AND tablename = $1 ORDER BY indexname",
            (table_name,),
        )
        primary_key_name = f"{table_name}_pkey"
        return [
            {
                "name": row[0],
                "column_names": self._index_columns(row[1]),
                "unique": row[1].upper().startswith("CREATE UNIQUE INDEX"),
                "dialect_options": {},
            }
            for row in result
            if row[0] != primary_key_name
        ]

    def get_unique_constraints(self, connection, table_name, schema=None, **kw):
        if schema not in (None, "public"):
            return []
        result = connection.exec_driver_sql(
            "SELECT indexname, indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = 'public' AND tablename = $1 ORDER BY indexname",
            (table_name,),
        )
        primary_key_name = f"{table_name}_pkey"
        return [
            {
                "name": row[0],
                "column_names": self._index_columns(row[1]),
                "duplicates_index": row[0],
            }
            for row in result
            if row[0] != primary_key_name
            and row[1].upper().startswith("CREATE UNIQUE INDEX")
        ]

    @staticmethod
    def _index_columns(definition: str) -> list[str]:
        """Parse the simple parenthesized column list emitted by pg_indexes."""
        opening = closing = -1
        quoted = False
        depth = 0
        index = 0
        while index < len(definition):
            char = definition[index]
            if char == '"':
                if (
                    quoted
                    and index + 1 < len(definition)
                    and definition[index + 1] == '"'
                ):
                    index += 1
                else:
                    quoted = not quoted
            elif not quoted and char == "(":
                if depth == 0:
                    opening = index
                depth += 1
            elif not quoted and char == ")" and depth:
                depth -= 1
                if depth == 0:
                    closing = index
            index += 1
        if opening < 0 or closing <= opening:
            return []
        columns = definition[opening + 1 : closing]
        result = []
        current = []
        quoted = False
        index = 0
        while index < len(columns):
            char = columns[index]
            if char == '"':
                if quoted and index + 1 < len(columns) and columns[index + 1] == '"':
                    current.append('"')
                    index += 1
                else:
                    quoted = not quoted
            elif char == "," and not quoted:
                result.append("".join(current).strip())
                current = []
            else:
                current.append(char)
            index += 1
        if current:
            result.append("".join(current).strip())
        return result

    @staticmethod
    def _type_from_name(name: str):
        from sqlalchemy import types

        normalized = name.lower()
        if normalized in {"smallint", "integer", "bigint"}:
            return types.Integer()
        if normalized in {"real", "double precision", "numeric", "decimal"}:
            return types.Float()
        if normalized in {"boolean", "bool"}:
            return types.Boolean()
        if normalized in {
            "date",
            "time",
            "timestamp",
            "timestamp without time zone",
            "timestamp with time zone",
        }:
            return types.Text()
        if normalized in {"uuid"}:
            return types.Uuid()
        if normalized in {"bytea", "binary", "varbinary"}:
            return types.LargeBinary()
        if normalized in {"text", "character varying", "varchar", "character", "char"}:
            return types.Text()
        return types.NullType()

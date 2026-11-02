import pytest
from sqlalchemy import (
    DECIMAL,
    NUMERIC,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    Time,
    Unicode,
    UnicodeText,
    Uuid,
    column,
    select,
    table,
)
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool, SingletonThreadPool
from sqlalchemy.schema import CreateTable

from sqlalchemy_hawdb.dialect import HawDBDialect


def test_dialect_compiles_positional_dollar_parameters():
    statement = (
        select(column("id")).select_from(table("widgets")).where(column("id") == 7)
    )
    compiled = statement.compile(dialect=HawDBDialect())

    assert "$1" in str(compiled)
    assert compiled.positiontup == ["id_1"]
    assert compiled.params == {"id_1": 7}


def test_type_compiler_uses_hawdb_scalar_types():
    statement = CreateTable(
        Table(
            "values_table",
            MetaData(),
            Column("id", BigInteger, primary_key=True),
            Column("enabled", Boolean),
            Column("amount", Numeric(12, 3)),
            Column("ratio", Float),
            Column("label", String(40)),
            Column("payload", LargeBinary),
            Column("token", Uuid),
        )
    ).compile(dialect=HawDBDialect())

    sql = str(statement)
    assert "id BIGINT" in sql
    assert "enabled BOOLEAN" in sql
    assert "amount NUMERIC(12, 3)" in sql
    assert "ratio DOUBLE PRECISION" in sql
    assert "label TEXT" in sql
    assert "payload BYTEA" in sql
    assert "token UUID" in sql


@pytest.mark.parametrize(
    ("type_", "expected"),
    [
        (Numeric(), "NUMERIC"),
        (Numeric(18), "NUMERIC(18)"),
        (Numeric(scale=4), "NUMERIC(38, 4)"),
        (Numeric(38, 4), "NUMERIC(38, 4)"),
        (NUMERIC(18, 6), "NUMERIC(18, 6)"),
        (DECIMAL(18, 6), "NUMERIC(18, 6)"),
    ],
)
def test_numeric_ddl_preserves_precision_and_scale(type_, expected):
    assert type_.compile(dialect=HawDBDialect()) == expected


@pytest.mark.parametrize(
    "type_",
    [
        String(80),
        Text(80),
        Unicode(80),
        UnicodeText(80),
        Date(),
        Time(),
        DateTime(),
        Enum("short", "longer"),
    ],
)
def test_text_backed_types_compile_without_postgresql_type_modifiers(type_):
    assert type_.compile(dialect=HawDBDialect()) == "TEXT"


@pytest.mark.parametrize(
    "name", ["current_user", "lateral", "graph", "match", "return"]
)
def test_postgresql_and_hawdb_reserved_identifiers_are_quoted(name):
    statement = select(column(name)).select_from(table("widgets"))

    compiled = str(statement.compile(dialect=HawDBDialect()))

    assert f'SELECT "{name}"' in compiled


def test_url_uses_path_and_read_only_option():
    dialect = HawDBDialect()
    args, kwargs = dialect.create_connect_args(
        make_url("hawdb:////tmp/example.hawdb?read_only=true")
    )

    assert args == ["/tmp/example.hawdb"]
    assert kwargs == {"read_only": True, "autocommit": False}


def test_memory_url_opens_an_ephemeral_database():
    args, kwargs = HawDBDialect().create_connect_args(make_url("hawdb:///:memory:"))

    assert args == [None]
    assert kwargs == {"read_only": False, "autocommit": False}


def test_memory_databases_are_pooled_per_thread_but_file_handles_are_not_pooled():
    memory_url = make_url("hawdb:///:memory:")
    file_url = make_url("hawdb:////tmp/example.hawdb")

    assert HawDBDialect.get_pool_class(memory_url) is SingletonThreadPool
    assert HawDBDialect.get_pool_class(file_url) is NullPool


def test_dialect_uses_hawdb_native_dbapi():
    dbapi = HawDBDialect.import_dbapi()

    assert dbapi.paramstyle == "numeric_dollar"
    assert dbapi.threadsafety == 1
    assert callable(dbapi.connect)


def test_primary_key_index_parser_handles_quoted_commas_and_escaped_quotes():
    definition = 'CREATE INDEX x ON public.t USING btree ("a,b", "a""b", "p(q)")'

    assert HawDBDialect._index_columns(definition) == ["a,b", 'a"b', "p(q)"]

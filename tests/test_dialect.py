from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
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
            Column("label", String(40)),
            Column("payload", LargeBinary),
        )
    ).compile(dialect=HawDBDialect())

    sql = str(statement)
    assert "id BIGINT" in sql
    assert "enabled BOOLEAN" in sql
    assert "amount DOUBLE PRECISION" in sql
    assert "label TEXT" in sql
    assert "payload BYTEA" in sql


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

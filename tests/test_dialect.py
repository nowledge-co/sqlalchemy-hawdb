import pytest
from sqlalchemy import (
    DECIMAL,
    NUMERIC,
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
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
    func,
    select,
    table,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP as PG_TIMESTAMP
from sqlalchemy.engine import make_url
from sqlalchemy.exc import CompileError
from sqlalchemy.pool import NullPool, SingletonThreadPool
from sqlalchemy.schema import CreateIndex, CreateTable, DropIndex

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
        Enum("short", "longer"),
    ],
)
def test_text_backed_types_compile_without_postgresql_type_modifiers(type_):
    assert type_.compile(dialect=HawDBDialect()) == "TEXT"


@pytest.mark.parametrize(
    ("type_", "expected"),
    [
        (DateTime(), "TIMESTAMP WITHOUT TIME ZONE"),
        (DateTime(timezone=True), "TIMESTAMP WITH TIME ZONE"),
        (TIMESTAMP(timezone=True), "TIMESTAMP WITH TIME ZONE"),
        (PG_TIMESTAMP(precision=0), "TIMESTAMP(0) WITHOUT TIME ZONE"),
        (PG_TIMESTAMP(precision=6), "TIMESTAMP(6) WITHOUT TIME ZONE"),
        (PG_TIMESTAMP(timezone=True, precision=3), "TIMESTAMP(3) WITH TIME ZONE"),
    ],
)
def test_timestamp_ddl_reuses_postgresql_precision_and_timezone(type_, expected):
    assert type_.compile(dialect=HawDBDialect()) == expected


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


def test_search_index_compilation_preserves_quoting_and_conditional_ddl():
    documents = Table("Documents", MetaData(), Column("Body", Text))
    index = Index(
        "Search Index", documents.c.Body, hawdb_using="bm25", hawdb_analyzer="cjk_ngram"
    )
    dialect = HawDBDialect()
    assert str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)) == (
        'CREATE SEARCH INDEX IF NOT EXISTS "Search Index" ON "Documents" ("Body")'
        " WITH (analyzer = 'cjk_ngram')"
    )
    assert str(DropIndex(index, if_exists=True).compile(dialect=dialect)).strip() == (
        'DROP SEARCH INDEX IF EXISTS "Search Index"'
    )


@pytest.mark.parametrize("using", [None, "btree"])
def test_regular_index_compilation_is_preserved(using):
    documents = Table("documents", MetaData(), Column("body", Text))
    index = Index("body_idx", documents.c.body, hawdb_using=using)
    dialect = HawDBDialect()
    assert str(CreateIndex(index).compile(dialect=dialect)) == (
        "CREATE INDEX body_idx ON documents (body)"
    )
    assert (
        str(DropIndex(index).compile(dialect=dialect)).strip() == "DROP INDEX body_idx"
    )


@pytest.mark.parametrize(
    "case",
    [
        "unique",
        "multiple",
        "integer",
        "expression",
        "analyzer",
        "using",
        "orphan_analyzer",
    ],
)
def test_search_index_refuses_unsupported_options(case):
    documents = Table(
        "documents", MetaData(), Column("id", Integer), Column("body", Text)
    )
    options = {"hawdb_using": "bm25"}
    expressions = [documents.c.body]
    if case == "unique":
        options["unique"] = True
    elif case == "multiple":
        expressions.append(documents.c.id)
    elif case == "integer":
        expressions = [documents.c.id]
    elif case == "expression":
        expressions = [func.lower(documents.c.body)]
    elif case == "analyzer":
        options["hawdb_analyzer"] = "jieba"
    elif case == "using":
        options["hawdb_using"] = "gin"
    else:
        options = {"hawdb_analyzer": "cjk_ngram"}
    index = Index("invalid_index", *expressions, **options)
    with pytest.raises(CompileError):
        CreateIndex(index).compile(dialect=HawDBDialect())


def test_search_index_column_parser_ignores_analyzer_options():
    definition = (
        'CREATE SEARCH INDEX "search(x)" ON public."docs(y)" ("a,b") '
        "WITH (analyzer = 'cjk_ngram')"
    )
    assert HawDBDialect._index_columns(definition) == ["a,b"]

from typing import ClassVar

import hawdb
import pytest
from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    bindparam,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.orm.exc import StaleDataError


def test_single_statement_rowcounts_and_executemany_boundary(tmp_path):
    engine = create_engine(URL.create("hawdb", database=str(tmp_path / "counts.hawdb")))
    records = Table(
        "records",
        MetaData(),
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("body", Text),
    )
    try:
        records.metadata.create_all(engine)
        with engine.begin() as connection:
            result = connection.execute(
                records.insert().execution_options(preserve_rowcount=True),
                {"id": 1, "body": "same"},
            )
            assert result.rowcount == 1
            assert result.supports_sane_rowcount()
            result = connection.execute(
                records.insert(),
                [{"id": 2, "body": "same"}, {"id": 3, "body": "other"}],
            )
            assert result.rowcount == -1
            assert not result.supports_sane_multi_rowcount()
            # Count matched rows even when their values do not change.
            assert (
                connection.execute(
                    records.update().where(records.c.id <= 2).values(body="same")
                ).rowcount
                == 2
            )
            assert (
                connection.execute(
                    records.update().where(records.c.id == 99).values(body="missing")
                ).rowcount
                == 0
            )
            assert (
                connection.execute(records.delete().where(records.c.id == 3)).rowcount
                == 1
            )
            assert (
                connection.execute(records.delete().where(records.c.id == 99)).rowcount
                == 0
            )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    records.update().where(records.c.id == 1).values(body="rolled back")
                ).rowcount
                == 1
            )
            connection.rollback()
            assert (
                connection.scalar(select(records.c.body).where(records.c.id == 1))
                == "same"
            )
    finally:
        engine.dispose()


def test_orm_version_checks_detect_stale_updates(tmp_path):
    engine = create_engine(
        URL.create("hawdb", database=str(tmp_path / "versioned.hawdb"))
    )
    base = declarative_base()

    class Document(base):
        __tablename__ = "documents"
        id = Column(Integer, primary_key=True, autoincrement=False)
        body = Column(Text)
        version = Column(Integer, nullable=False)
        __mapper_args__: ClassVar[dict] = {"version_id_col": version}

    try:
        base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Document(id=1, body="initial"))
            session.commit()
            document = session.get(Document, 1)
            document.body = "updated"
            session.commit()
            assert document.version == 2
            # Advance the row through Core on the same connection; keep the ORM
            # instance stale without opening a second handle to the file.
            session.connection().execute(
                Document.__table__.update().where(Document.id == 1).values(version=3)
            )
            document.body = "stale"
            with pytest.raises(StaleDataError, match="0 were matched"):
                session.flush()
            session.rollback()
            assert session.get(Document, 1).body == "updated"
            assert session.get(Document, 1).version == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize(
    ("values", "kind", "error_class"),
    [
        ({"id": 1, "body": "duplicate"}, "constraint", IntegrityError),
        ({"id": 2, "body": None}, "constraint", IntegrityError),
        ({"id": 3, "body": 3}, "schema", DataError),
    ],
    ids=["duplicate_key", "not_null", "wrong_type"],
)
def test_relational_errors_preserve_typed_kind_and_connection(
    tmp_path, memory, autocommit, values, kind, error_class
):
    url = (
        "hawdb:///:memory:"
        if memory
        else URL.create("hawdb", database=str(tmp_path / "errors.hawdb"))
    )
    engine = create_engine(url, connect_args={"autocommit": autocommit})
    records = Table(
        "records",
        MetaData(),
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("body", Text, nullable=False),
    )
    try:
        records.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(records.insert(), {"id": 1, "body": "first"})
        with engine.begin() as connection:
            with pytest.raises(error_class) as caught:
                connection.execute(records.insert(), values)
            native = (
                hawdb.exceptions.StorageError
                if autocommit
                else hawdb.exceptions.ExecutionError
            )
            assert isinstance(caught.value.orig, native)
            assert caught.value.orig.kind == kind
            assert caught.value.orig.retryable is False
            assert not isinstance(caught.value.orig, hawdb.exceptions.IntegrityError)
            connection.execute(records.insert(), {"id": 4, "body": "after error"})
        with engine.connect() as connection:
            assert connection.scalars(
                select(records.c.id).order_by(records.c.id)
            ).all() == [1, 4]
    finally:
        engine.dispose()


def test_bm25_index_reflection_search_mutation_rollback_and_reopen(tmp_path):
    path = tmp_path / "search.hawdb"
    url = URL.create("hawdb", database=str(path))
    documents = Table(
        "documents",
        MetaData(),
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("stream", Text, nullable=False),
        Column("body", Text),
    )
    search_index = Index(
        "documents_text",
        documents.c.body,
        hawdb_using="bm25",
        hawdb_analyzer="cjk_ngram",
    )
    Index("documents_stream", documents.c.stream)
    search = (
        text(
            "SELECT d.id, score FROM text_search(:index, :query, top_k => :top_k) AS d "
            "WHERE d.stream = :stream ORDER BY score DESC, d.id"
        )
        .bindparams(
            bindparam("index", type_=Text),
            bindparam("query", type_=Text),
            bindparam("top_k", type_=Integer),
            bindparam("stream", type_=Text),
        )
        .columns(id=Integer, score=Float)
    )
    parameters = {
        "index": "documents_text",
        "query": "database",
        "top_k": 1,
        "stream": "wanted",
    }
    engine = create_engine(url)
    try:
        documents.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                documents.insert(),
                [
                    {"id": 1, "stream": "other", "body": "database database database"},
                    {"id": 2, "stream": "wanted", "body": "database engine"},
                    {"id": 3, "stream": "wanted", "body": None},
                ],
            )
        indexes = {
            index["name"]: index for index in inspect(engine).get_indexes("documents")
        }
        assert indexes["documents_text"]["column_names"] == ["body"]
        assert indexes["documents_text"]["dialect_options"] == {
            "hawdb_using": "bm25",
            "hawdb_analyzer": "cjk_ngram",
        }
        assert indexes["documents_stream"]["dialect_options"] == {}
        assert inspect(engine).get_table_names() == [
            "documents",
            "hawdb_schema_migrations",
        ]
        assert inspect(engine).get_view_names() == []
        with engine.connect() as connection:
            rows = connection.execute(search, parameters).all()
            assert [row.id for row in rows] == [2]
            assert isinstance(rows[0].score, float) and rows[0].score > 0
            assert (
                connection.execute(search, {**parameters, "query": "absent"}).all()
                == []
            )
            assert connection.execute(search, parameters).all() == rows
            plan = (
                connection.exec_driver_sql(
                    "EXPLAIN SELECT id, score FROM text_search($1, $2, top_k => $3)",
                    ("documents_text", "database", 1),
                )
                .mappings()
                .all()
            )
            assert any("Bm25IndexScan" in row["id"] for row in plan)
            connection.execute(
                documents.update().where(documents.c.id == 2).values(body="removed")
            )
            assert connection.execute(search, parameters).all() == []
            connection.rollback()
            assert connection.execute(search, parameters).all() == rows
        with engine.begin() as connection:
            connection.execute(documents.delete().where(documents.c.id == 2))
            connection.execute(
                documents.update()
                .where(documents.c.id == 3)
                .values(body="database restored")
            )
    finally:
        engine.dispose()

    with hawdb.open(str(path)) as database:
        database.execute("CHECKPOINT")
    reopened = create_engine(url)
    try:
        inspector = inspect(reopened)
        reflected = Table(
            "documents",
            MetaData(),
            *(
                Column(info["name"], info["type"])
                for info in inspector.get_columns("documents")
            ),
        )
        info = next(
            index
            for index in inspector.get_indexes("documents")
            if index["name"] == search_index.name
        )
        reflected_search = Index(
            info["name"],
            *(reflected.c[name] for name in info["column_names"]),
            unique=info["unique"],
            **info["dialect_options"],
        )
        assert reflected_search.dialect_options["hawdb"]["using"] == "bm25"
        with reopened.connect() as connection:
            assert [row.id for row in connection.execute(search, parameters)] == [3]
        reflected_search.drop(reopened)
        assert "documents_text" not in {
            index["name"] for index in inspect(reopened).get_indexes("documents")
        }
        reflected_search.create(reopened)
        with reopened.connect() as connection:
            assert [row.id for row in connection.execute(search, parameters)] == [3]
    finally:
        reopened.dispose()

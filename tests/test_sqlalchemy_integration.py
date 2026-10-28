import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, declarative_base

pytest.importorskip("hawdb")


def make_engine(path):
    return create_engine(URL.create("hawdb", database=str(path)))


def test_core_create_insert_select_and_reflection(tmp_path):
    engine = make_engine(tmp_path / "core.hawdb")
    metadata = MetaData()
    widgets = Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("name", String(80), nullable=False),
        UniqueConstraint("name"),
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                widgets.insert(),
                [{"id": 1, "name": "first"}, {"id": 2, "name": "second"}],
            )
        with engine.connect() as connection:
            rows = connection.execute(
                select(widgets.c.id, widgets.c.name).order_by(widgets.c.id)
            ).all()
        assert rows == [(1, "first"), (2, "second")]

        inspector = inspect(engine)
        assert "widgets" in inspector.get_table_names()
        assert [column["name"] for column in inspector.get_columns("widgets")] == [
            "id",
            "name",
        ]
        assert inspector.get_pk_constraint("widgets")["constrained_columns"] == ["id"]
        assert [
            constraint["column_names"]
            for constraint in inspector.get_unique_constraints("widgets")
        ] == [["name"]]
    finally:
        engine.dispose()


def test_transaction_rollback(tmp_path):
    engine = make_engine(tmp_path / "rollback.hawdb")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE events (id BIGINT PRIMARY KEY, name TEXT)"
            )
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO events (id, name) VALUES ($1, $2)",
                    (1, "rolled back"),
                )
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM events").all() == []
    finally:
        engine.dispose()


def test_orm_session_round_trip(tmp_path):
    engine = make_engine(tmp_path / "orm.hawdb")
    base = declarative_base()

    class Entry(base):
        __tablename__ = "entries"

        id = Column(Integer, primary_key=True, autoincrement=False)
        value = Column(String(80), nullable=False)

    try:
        base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Entry(id=1, value="orm"))
            session.commit()
            assert session.scalars(select(Entry.value)).all() == ["orm"]
    finally:
        engine.dispose()


def test_in_memory_database_survives_connection_checkin():
    engine = create_engine("hawdb:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE scratch (id BIGINT PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO scratch (id) VALUES ($1)", (9,))
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT id FROM scratch").scalar_one() == 9
            )
    finally:
        engine.dispose()

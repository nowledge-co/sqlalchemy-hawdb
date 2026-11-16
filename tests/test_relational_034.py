from datetime import UTC, datetime

import hawdb
import pytest
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    Time,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.exc import ProgrammingError


@pytest.fixture
def engine(database_url, autocommit):
    engine = create_engine(database_url, connect_args={"autocommit": autocommit})
    try:
        yield engine
    finally:
        engine.dispose()


def test_dbapi_timestamp_constructors_round_trip_through_sqlalchemy(engine):
    dbapi = engine.dialect.dbapi
    stamps = Table(
        "stamps",
        MetaData(),
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("at", DateTime()),
        Column("seen", DateTime(timezone=True)),
    )
    stamps.metadata.create_all(engine)
    values = [
        dbapi.TimestampFromTicks(-1.5),
        dbapi.TimestampFromTicks(0),
        dbapi.TimestampFromTicks(1_794_614_400.25),
        dbapi.Timestamp(2026, 11, 14, 3, 4, 5, 678901, tzinfo=UTC),
    ]
    assert values[:3] == [
        datetime(1969, 12, 31, 23, 59, 58, 500000, tzinfo=UTC),
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(2026, 11, 14, 0, 0, 0, 250000, tzinfo=UTC),
    ]
    with engine.begin() as connection:
        connection.execute(
            stamps.insert(),
            [
                {"id": identifier, "at": value.replace(tzinfo=None), "seen": value}
                for identifier, value in enumerate(values)
            ],
        )
    with engine.connect() as connection:
        rows = connection.execute(select(stamps).order_by(stamps.c.id)).all()
        assert rows == [
            (identifier, value.replace(tzinfo=None), value)
            for identifier, value in enumerate(values)
        ]
        for identifier, value in enumerate(values):
            assert (
                connection.scalar(select(stamps.c.id).where(stamps.c.seen == value))
                == identifier
            )
            assert (
                connection.scalar(
                    select(stamps.c.id).where(stamps.c.at == value.replace(tzinfo=None))
                )
                == identifier
            )
        assert all(row.seen.tzinfo is UTC and row.at.tzinfo is None for row in rows)


def test_date_time_constructors_keep_sql_text_mapping_and_parameter_refusals(engine):
    dbapi = engine.dialect.dbapi
    records = Table(
        "records",
        MetaData(),
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("on_date", Date()),
        Column("at_time", Time()),
    )
    records.metadata.create_all(engine)
    columns = inspect(engine).get_columns("records")
    assert all(isinstance(column["type"], Text) for column in columns[1:])
    with engine.begin() as connection:
        for identifier, (column, value) in enumerate(
            [
                ("on_date", dbapi.Date(2026, 11, 14)),
                ("on_date", dbapi.DateFromTicks(1_794_614_400.25)),
                ("at_time", dbapi.Time(3, 4, 5, 678901)),
                ("at_time", dbapi.TimeFromTicks(1_794_614_400.25)),
            ]
        ):
            with pytest.raises(ProgrammingError) as caught:
                connection.execute(
                    records.insert().values(id=identifier, **{column: value})
                )
            assert isinstance(caught.value.orig, hawdb.exceptions.SemanticError)
            assert not caught.value.connection_invalidated
        assert connection.execute(select(records.c.id)).all() == []
        connection.exec_driver_sql(
            "INSERT INTO records VALUES ($1, $2, $3)",
            (9, "2026-11-14", "03:04:05.678901"),
        )
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT on_date, at_time FROM records WHERE id = $1", (9,)
        ).one() == ("2026-11-14", "03:04:05.678901")

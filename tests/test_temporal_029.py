from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    Numeric,
    Table,
    bindparam,
    create_engine,
    func,
    inspect,
    literal_column,
    select,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, declarative_base


@pytest.mark.parametrize("zoned", [False, True])
def test_native_timestamps_precision_reflection_and_reopen(tmp_path, zoned):
    url = URL.create("hawdb", database=str(tmp_path / "timestamps.hawdb"))
    engine = create_engine(url)
    metadata = MetaData()
    stamps = Table(
        "stamps",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("at", DateTime(timezone=zoned), index=True),
        Column("rounded", TIMESTAMP(timezone=zoned, precision=3)),
    )
    values = [
        datetime(1969, 12, 31, 23, 59, 59, 123500),
        datetime(2026, 11, 9, 12, 0, 0, 123500),
    ]
    if zoned:
        values = [
            values[0].replace(tzinfo=timezone(timedelta(hours=5, minutes=30))),
            values[1].replace(tzinfo=timezone(timedelta(hours=-8))),
        ]
    expected = [value.astimezone(timezone.utc) if zoned else value for value in values]
    # TIMESTAMP(p) halfway rounding is away from the year-2000 epoch.
    rounded = [
        expected[0].replace(microsecond=123000),
        expected[1].replace(microsecond=124000),
    ]
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                stamps.insert(),
                [
                    {"id": identifier, "at": value, "rounded": value}
                    for identifier, value in enumerate(values, start=1)
                ]
                + [{"id": 3, "at": None, "rounded": None}],
            )
    finally:
        engine.dispose()

    reopened = create_engine(url)
    try:
        columns = {
            column["name"]: column for column in inspect(reopened).get_columns("stamps")
        }
        for name, precision in (("at", 6), ("rounded", 3)):
            type_ = columns[name]["type"]
            assert isinstance(type_, DateTime)
            assert type_.timezone is zoned
            assert type_.precision == precision
        reflected = Table(
            "stamps",
            MetaData(),
            Column("id", Integer, primary_key=True, autoincrement=False),
            Column("at", columns["at"]["type"]),
            Column("rounded", columns["rounded"]["type"]),
        )
        with reopened.connect() as connection:
            rows = connection.execute(select(reflected).order_by(reflected.c.id)).all()
            assert rows == [
                (1, expected[0], rounded[0]),
                (2, expected[1], rounded[1]),
                (3, None, None),
            ]
            for row in rows[:2]:
                assert isinstance(row.at, datetime)
                assert row.at.tzinfo is (timezone.utc if zoned else None)
            statement = select(reflected.c.id).where(
                reflected.c.at >= bindparam("bound", type_=columns["at"]["type"])
            )
            assert connection.scalars(statement, {"bound": rows[1].at}).all() == [2]
            assert connection.scalars(
                select(reflected.c.id).where(reflected.c.at.is_(None))
            ).all() == [3]
    finally:
        reopened.dispose()


def test_orm_datetime_parameters_and_clock_default(tmp_path):
    engine = create_engine(
        URL.create("hawdb", database=str(tmp_path / "orm_time.hawdb"))
    )
    base = declarative_base()

    class Event(base):
        __tablename__ = "events"

        id = Column(Integer, primary_key=True, autoincrement=False)
        at = Column(DateTime, nullable=False)
        created_at = Column(DateTime(timezone=True), server_default=func.now())

    value = datetime(2026, 11, 9, 12, 0, 0, 654321)
    try:
        base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(Event(id=1, at=value))
            session.commit()
        with Session(engine) as session:
            event = session.scalars(select(Event)).one()
            assert event.at == value
            assert event.at.tzinfo is None
            assert isinstance(event.created_at, datetime)
            assert event.created_at.tzinfo is timezone.utc
    finally:
        engine.dispose()


def test_clock_defaults_share_transaction_time_and_survive_reopen(tmp_path):
    url = URL.create("hawdb", database=str(tmp_path / "clocks.hawdb"))
    engine = create_engine(url)
    metadata = MetaData()
    events = Table(
        "events",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            server_default=func.current_timestamp(),
        ),
        Column("observed_at", DateTime(timezone=True), server_default=func.now()),
        Column("local_at", DateTime, server_default=func.localtimestamp()),
    )
    clock = select(func.now())
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            started_at = connection.scalar(clock)
            connection.execute(events.insert(), {"id": 1})
            connection.execute(events.insert(), {"id": 2})
            assert connection.scalar(clock) == started_at
            assert connection.scalar(select(func.current_timestamp())) == started_at
            assert connection.scalar(
                select(func.localtimestamp())
            ) == started_at.replace(tzinfo=None)
            rows = connection.execute(select(events).order_by(events.c.id)).all()
            assert rows == [
                (identifier, started_at, started_at, started_at.replace(tzinfo=None))
                for identifier in (1, 2)
            ]
        with engine.begin() as connection:
            assert connection.scalar(clock) > started_at
    finally:
        engine.dispose()

    reopened = create_engine(url)
    try:
        defaults = {
            column["name"]: column["default"]
            for column in inspect(reopened).get_columns("events")
        }
        assert defaults == {
            "id": None,
            "created_at": "CURRENT_TIMESTAMP",
            "observed_at": "CURRENT_TIMESTAMP",
            "local_at": "LOCALTIMESTAMP",
        }
        with reopened.connect() as connection:
            assert (
                connection.execute(select(events).order_by(events.c.id)).all() == rows
            )
    finally:
        reopened.dispose()


@pytest.mark.parametrize("zoned", [False, True])
def test_timestamp_timezone_mismatch_requires_cast(tmp_path, zoned):
    engine = create_engine(
        URL.create("hawdb", database=str(tmp_path / "mismatch.hawdb"))
    )
    metadata = MetaData()
    stamps = Table(
        "stamps",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("at", DateTime(timezone=zoned)),
    )
    value = datetime(2026, 11, 9, tzinfo=timezone.utc if zoned else None)
    mismatch = value.replace(tzinfo=None if zoned else timezone.utc)
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(stamps.insert(), {"id": 1, "at": value})
        with engine.connect() as connection:
            with pytest.raises(DBAPIError, match="incompatible"):
                connection.execute(select(stamps.c.id).where(stamps.c.at == mismatch))
            assert (
                connection.scalar(select(stamps.c.at).where(stamps.c.id == 1)) == value
            )
    finally:
        engine.dispose()


def test_double_literals_coerce_while_decimal_parameters_remain_strict(tmp_path):
    engine = create_engine(
        URL.create("hawdb", database=str(tmp_path / "literals.hawdb"))
    )
    metadata = MetaData()
    accounts = Table(
        "accounts",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("balance", Float),
        Column("amount", Numeric(38, 4)),
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                accounts.insert(),
                {"id": 1, "balance": 10.0, "amount": Decimal("9007199254740993.0100")},
            )
            connection.execute(
                accounts.update()
                .where(accounts.c.id == 1)
                .values(
                    balance=accounts.c.balance + literal_column("12.5", type_=Float),
                    amount=accounts.c.amount + literal_column("0.1", type_=Numeric),
                )
            )
            expected = (22.5, Decimal("9007199254740993.1100"))
            assert (
                connection.execute(select(accounts.c.balance, accounts.c.amount)).one()
                == expected
            )
            with pytest.raises(DBAPIError, match="CAST"):
                connection.execute(
                    accounts.update()
                    .where(accounts.c.id == 1)
                    .values(
                        balance=accounts.c.balance + bindparam("delta", type_=Numeric)
                    ),
                    {"delta": Decimal("0.1")},
                )
            assert (
                connection.execute(select(accounts.c.balance, accounts.c.amount)).one()
                == expected
            )
    finally:
        engine.dispose()


def test_unrepresentable_timestamp_result_retains_lossless_tag():
    engine = create_engine("hawdb:///:memory:")
    try:
        with engine.connect() as connection:
            result = connection.exec_driver_sql(
                "SELECT TIMESTAMP '+12345-01-01 00:00:00' AS stamp"
            ).scalar_one()
            assert result == {"$localdatetime": "+12345-01-01T00:00:00"}
    finally:
        engine.dispose()

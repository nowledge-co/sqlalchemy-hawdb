from decimal import Decimal

import pytest
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    bindparam,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, declarative_base


def make_engine(path):
    return create_engine(URL.create("hawdb", database=str(path)))


def test_engine_initializes_version_and_default_schema(tmp_path):
    engine = make_engine(tmp_path / "initialization.hawdb")
    try:
        with engine.connect() as connection:
            assert engine.dialect.server_version_info[:2] == (0, 26)
            assert inspect(connection).default_schema_name == "public"
    finally:
        engine.dispose()


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
            reordered = connection.execute(
                select(widgets.c.name, widgets.c.id).order_by(widgets.c.id)
            )
            assert list(reordered.keys()) == ["name", "id"]
            assert reordered.all() == [("first", 1), ("second", 2)]

            empty = connection.execute(
                select(widgets.c.name, widgets.c.id).where(widgets.c.id < 0)
            )
            assert list(empty.keys()) == ["name", "id"]
            assert empty.all() == []
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


def test_native_decimal_round_trip_reflection_and_reopen(tmp_path):
    path = tmp_path / "decimal.hawdb"
    engine = make_engine(path)
    metadata = MetaData()
    amounts = Table(
        "amounts",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("amount", Numeric(38, 4)),
        Column("ratio", Float),
    )
    amount = Decimal("9007199254740993.0100")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                amounts.insert(),
                [
                    {"id": 1, "amount": amount, "ratio": 0.125},
                    {"id": 2, "amount": None, "ratio": None},
                ],
            )
        with engine.connect() as connection:
            rows = connection.execute(select(amounts).order_by(amounts.c.id)).all()
            assert rows == [(1, amount, 0.125), (2, None, None)]
            assert isinstance(rows[0].amount, Decimal)
            assert str(rows[0].amount) == str(amount)
            assert isinstance(rows[0].ratio, float)

        with engine.begin() as connection:
            connection.execute(
                amounts.update()
                .where(amounts.c.id == 1)
                .values(amount=amounts.c.amount + Decimal("0.0001"))
            )
    finally:
        engine.dispose()

    reopened = make_engine(path)
    try:
        columns = inspect(reopened).get_columns("amounts")
        numeric_type = columns[1]["type"]
        assert isinstance(numeric_type, Numeric)
        assert not isinstance(numeric_type, Float)
        assert (numeric_type.precision, numeric_type.scale) == (38, 4)
        assert isinstance(columns[2]["type"], Float)

        reflected_amounts = Table(
            "amounts",
            MetaData(),
            Column("id", Integer, primary_key=True, autoincrement=False),
            Column("amount", numeric_type),
        )
        with reopened.begin() as connection:
            connection.execute(reflected_amounts.insert(), {"id": 3, "amount": amount})
        with reopened.connect() as connection:
            values = connection.scalars(
                select(reflected_amounts.c.amount).order_by(reflected_amounts.c.id)
            ).all()
            assert values == [Decimal("9007199254740993.0101"), None, amount]
            assert str(values[0]) == "9007199254740993.0101"
            assert str(values[2]) == str(amount)
    finally:
        reopened.dispose()


@pytest.mark.parametrize("asdecimal", [True, False])
def test_numeric_rounding_and_result_modes(tmp_path, asdecimal):
    engine = make_engine(tmp_path / "rounding.hawdb")
    metadata = MetaData()
    amounts = Table(
        "amounts",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("amount", Numeric(12, 2, asdecimal=asdecimal)),
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                amounts.insert(),
                [
                    {"id": 1, "amount": Decimal("1.235")},
                    {"id": 2, "amount": Decimal("-1.235")},
                ],
            )
        with engine.connect() as connection:
            values = connection.scalars(
                select(amounts.c.amount).order_by(amounts.c.id)
            ).all()
            if asdecimal:
                assert values == [Decimal("1.24"), Decimal("-1.24")]
                assert all(isinstance(value, Decimal) for value in values)
            else:
                assert values == [1.24, -1.24]
                assert all(isinstance(value, float) for value in values)
    finally:
        engine.dispose()


def test_decimal_overflow_leaves_stored_value_unchanged(tmp_path):
    engine = make_engine(tmp_path / "overflow.hawdb")
    metadata = MetaData()
    amounts = Table(
        "amounts",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("amount", Numeric(5, 2)),
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(amounts.insert(), {"id": 1, "amount": Decimal("12.34")})
        with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="decimal result exceeds"):
                connection.execute(
                    amounts.update()
                    .where(amounts.c.id == 1)
                    .values(amount=Decimal("9999.99"))
                )
            assert connection.scalar(select(amounts.c.amount)) == Decimal("12.34")
        with engine.connect() as connection:
            assert connection.scalar(select(amounts.c.amount)) == Decimal("12.34")
    finally:
        engine.dispose()


def test_keyless_tables_preserve_duplicates_defaults_and_reflection(tmp_path):
    engine = make_engine(tmp_path / "keyless.hawdb")
    metadata = MetaData()
    events = Table(
        "events",
        metadata,
        Column("name", String),
        Column("state", String, nullable=False, server_default="new"),
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(events.insert(), [{"name": "same"}, {"name": "same"}])
            connection.exec_driver_sql("INSERT INTO events VALUES ('implicit')")
        with engine.connect() as connection:
            assert connection.execute(select(events).order_by(events.c.name)).all() == [
                ("implicit", "new"),
                ("same", "new"),
                ("same", "new"),
            ]
        inspector = inspect(engine)
        assert [column["name"] for column in inspector.get_columns("events")] == [
            "name",
            "state",
        ]
        assert inspector.get_pk_constraint("events") == {
            "name": None,
            "constrained_columns": [],
        }
        assert inspector.get_indexes("events") == []
    finally:
        engine.dispose()


def test_repeated_dml_rebinds_parameters_and_preserves_rollback(tmp_path):
    engine = make_engine(tmp_path / "dml.hawdb")
    metadata = MetaData()
    accounts = Table(
        "accounts",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("balance", Float),
        Column("amount", Numeric(18, 4)),
    )
    insert_account = accounts.insert()
    update_account = (
        accounts.update()
        .where(accounts.c.id == bindparam("target_id"))
        .values(
            balance=accounts.c.balance + bindparam("delta", type_=Float),
            amount=accounts.c.amount + bindparam("exact_delta", type_=Numeric(18, 4)),
        )
    )
    delete_account = accounts.delete().where(accounts.c.id == bindparam("target_id"))
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            for identifier, initial in ((1, 10), (2, 20)):
                connection.execute(
                    insert_account,
                    {
                        "id": identifier,
                        "balance": float(initial),
                        "amount": Decimal(initial),
                    },
                )
            for identifier, delta, exact_delta in (
                (1, 0.5, "0.1250"),
                (2, 1.5, "0.2500"),
                (1, 2.0, "0.1250"),
            ):
                connection.execute(
                    update_account,
                    {
                        "target_id": identifier,
                        "delta": delta,
                        "exact_delta": Decimal(exact_delta),
                    },
                )
            assert connection.execute(
                select(accounts).order_by(accounts.c.id)
            ).all() == [
                (1, 12.5, Decimal("10.2500")),
                (2, 21.5, Decimal("20.2500")),
            ]

        with engine.connect() as connection:
            connection.execute(
                update_account,
                {"target_id": 2, "delta": 999.0, "exact_delta": Decimal(999)},
            )
            connection.rollback()
            connection.execute(delete_account, {"target_id": 999})
            connection.execute(delete_account, {"target_id": 1})
            connection.execute(
                insert_account, {"id": 3, "balance": 30.0, "amount": Decimal(30)}
            )
            connection.commit()

        with engine.connect() as connection:
            assert connection.execute(
                select(accounts).order_by(accounts.c.id)
            ).all() == [
                (2, 21.5, Decimal("20.2500")),
                (3, 30.0, Decimal("30.0000")),
            ]
    finally:
        engine.dispose()


def test_namespace_anchor_connect_args_and_read_only_reopen(tmp_path):
    url = URL.create("hawdb", database=str(tmp_path / "anchored.hawdb"))
    engine = create_engine(url, connect_args={"namespace_durability_anchor": tmp_path})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE records (id BIGINT PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO records (id) VALUES ($1)", (7,))
    finally:
        engine.dispose()

    read_only = create_engine(
        url.update_query_dict({"read_only": "true"}),
        connect_args={"namespace_durability_anchor": str(tmp_path)},
    )
    try:
        with read_only.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM records").all() == [(7,)]
    finally:
        read_only.dispose()

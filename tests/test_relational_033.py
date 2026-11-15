from datetime import UTC, datetime
from decimal import Decimal

import hawdb
import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
    create_engine,
    select,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError


@pytest.fixture(params=[False, True], ids=["file", "memory"])
def database_url(request, tmp_path):
    if request.param:
        return "hawdb:///:memory:"
    return URL.create("hawdb", database=str(tmp_path / "results.hawdb"))


@pytest.fixture(params=[False, True], ids=["transaction", "autocommit"])
def autocommit(request):
    return request.param


def test_chunked_results_preserve_types_order_and_cursor_lifecycle(
    database_url, autocommit
):
    engine = create_engine(database_url, connect_args={"autocommit": autocommit})
    records = Table(
        "records",
        MetaData(),
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("label", Text),
        Column("amount", Numeric(38, 4)),
        Column("created_at", DateTime(timezone=True)),
        Column("payload", LargeBinary),
        Column("missing", Text),
    )
    count = 2054
    amount = Decimal("9007199254740993.0100")
    created_at = datetime(2026, 11, 15, 12, 0, 0, 123456, tzinfo=UTC)
    try:
        records.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                records.insert(),
                [
                    {
                        "id": identifier,
                        "label": "last" if identifier == count - 1 else None,
                        "amount": amount if identifier == count - 1 else None,
                        "created_at": created_at if identifier == count - 1 else None,
                        "payload": b"\x00\xff" if identifier == count - 1 else None,
                        "missing": None,
                    }
                    for identifier in range(count)
                ],
            )
        with engine.connect() as connection:
            statement = select(records).order_by(records.c.id)
            result = connection.execute(statement)
            assert list(result.keys()) == list(records.c.keys())
            # The first non-null values occur beyond two driver fetch chunks.
            assert [column[1] for column in result.cursor.description] == [
                int,
                str,
                Decimal,
                datetime,
                bytes,
                None,
            ]
            rows = result.fetchmany(3) + result.fetchmany(1023)
            for partition in result.partitions(257):
                rows.extend(partition)
            assert [row.id for row in rows] == list(range(count))
            assert rows[0] == (0, None, None, None, None, None)
            assert rows[-1] == (
                count - 1,
                "last",
                amount,
                created_at,
                b"\x00\xff",
                None,
            )
            assert str(rows[-1].amount) == "9007199254740993.0100"
            assert result.fetchall() == []
            result.close()

            partial = connection.execute(statement)
            assert partial.fetchone().id == 0
            partial.close()
            assert (
                connection.scalar(select(records.c.id).where(records.c.id == count - 1))
                == count - 1
            )
    finally:
        engine.dispose()


def test_payload_budget_error_preserves_details_and_connection(
    database_url, autocommit
):
    engine = create_engine(
        database_url,
        connect_args={"autocommit": autocommit, "max_read_result_payload_bytes": 1024},
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE wide (id BIGINT PRIMARY KEY, v TEXT)"
            )
            connection.exec_driver_sql(
                "INSERT INTO wide VALUES ($1, $2)",
                [(identifier, "x" * 600) for identifier in range(4)],
            )
        with engine.connect() as connection:
            with pytest.raises(OperationalError) as caught:
                connection.exec_driver_sql("SELECT id, v FROM wide").all()
            original = caught.value.orig
            assert isinstance(original, hawdb.exceptions.ResourceBudgetError)
            assert original.resource == "result_payload_bytes"
            assert original.budget_option == "max_read_result_payload_bytes"
            assert original.budget == 1024
            assert (original.requested, original.used) == (None, None)
            assert not caught.value.connection_invalidated
            assert connection.exec_driver_sql(
                "SELECT id FROM wide WHERE id = $1", (3,)
            ).all() == [(3,)]
            connection.rollback()
    finally:
        engine.dispose()


def test_admission_refusal_retains_native_error(database_url, autocommit):
    # Admission reserves the configured result budget before running SQL.
    engine = create_engine(
        database_url,
        connect_args={
            "autocommit": autocommit,
            "max_read_result_payload_bytes": 1 << 60,
        },
    )
    try:
        with engine.connect() as connection:
            with pytest.raises(OperationalError) as caught:
                connection.exec_driver_sql("SELECT 1")
            assert isinstance(caught.value.orig, hawdb.exceptions.AdmissionError)
            assert caught.value.orig.code
            assert isinstance(caught.value.orig.retryable, bool)
            assert not caught.value.connection_invalidated
            connection.rollback()
    finally:
        engine.dispose()

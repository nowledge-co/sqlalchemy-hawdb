import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, Table, create_engine, inspect
from sqlalchemy.engine import URL


@pytest.mark.parametrize("close_mode", ["close", "process_exit"])
def test_025_database_reopens_and_accepts_writes_in_026(tmp_path, close_mode):
    previous_python = os.environ.get("HAWDB_PREVIOUS_PYTHON")
    if previous_python is None:
        pytest.skip("Set HAWDB_PREVIOUS_PYTHON to an isolated HawDB 0.25 interpreter")

    path = tmp_path / "upgrade.hawdb"
    fixture_script = Path(__file__).parent / "fixtures" / "create_025_database.py"
    fixture = subprocess.run(
        [previous_python, str(fixture_script), str(path), close_mode],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert fixture.returncode == 0, fixture.stdout + fixture.stderr

    engine = create_engine(URL.create("hawdb", database=str(path)))
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT id, amount, label, payload FROM accounts ORDER BY id"
            ).all() == [
                (1, Decimal("9007199254740993.0100"), "first", b"\x00\xff"),
                (2, Decimal("0.2500"), "second", b"second"),
            ]
            assert connection.exec_driver_sql("SELECT message FROM events").all() == [
                ("same",),
                ("same",),
            ]
            assert connection.exec_driver_sql(
                "SELECT id FROM accounts WHERE amount = $1",
                (Decimal("9007199254740993.0100"),),
            ).all() == [(1,)]

        inspector = inspect(engine)
        columns = inspector.get_columns("accounts")
        amount_type = columns[1]["type"]
        assert isinstance(amount_type, Numeric)
        assert (amount_type.precision, amount_type.scale) == (38, 4)
        assert inspector.get_pk_constraint("accounts")["constrained_columns"] == ["id"]
        assert any(
            index["column_names"] == ["amount"]
            for index in inspector.get_indexes("accounts")
        )
        assert [
            constraint["column_names"]
            for constraint in inspector.get_unique_constraints("accounts")
        ] == [["label"]]
        assert inspector.get_pk_constraint("events")["constrained_columns"] == []

        accounts = Table(
            "accounts",
            MetaData(),
            Column("id", Integer, primary_key=True, autoincrement=False),
            Column("amount", amount_type),
        )
        with engine.begin() as connection:
            connection.execute(
                accounts.update()
                .where(accounts.c.id == 1)
                .values(amount=accounts.c.amount + Decimal("0.0001"))
            )
    finally:
        engine.dispose()

    reopened = create_engine(URL.create("hawdb", database=str(path)))
    try:
        with reopened.connect() as connection:
            amount = connection.exec_driver_sql(
                "SELECT amount FROM accounts WHERE id = $1", (1,)
            ).scalar_one()
            assert isinstance(amount, Decimal)
            assert str(amount) == "9007199254740993.0101"
    finally:
        reopened.dispose()

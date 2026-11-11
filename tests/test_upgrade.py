import hashlib
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import hawdb
import pytest
from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError


def create_database(python, path, close_mode, version):
    fixture_script = Path(__file__).parent / "fixtures" / "create_previous_database.py"
    fixture = subprocess.run(
        [python, str(fixture_script), str(path), close_mode, version],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert fixture.returncode == 0, fixture.stdout + fixture.stderr


def assert_row_page_format(path, magic):
    manifests = list(path.rglob("relational-row-pages*.manifest.hawdb"))
    assert manifests, "The fixture must contain a real row-page checkpoint"
    for manifest in manifests:
        with manifest.open("rb") as stream:
            assert stream.read(8) == magic, str(manifest)


def storage_digests(path):
    return {
        file.relative_to(path): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in path.rglob("*.hawdb")
        if file.is_file()
    }


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("close_mode", ["close", "process_exit"])
@pytest.mark.parametrize(
    ("previous_version", "interpreter_env"),
    [
        ("0.25.0", "HAWDB_PREVIOUS_PYTHON"),
        ("0.26.0", "HAWDB_026_PYTHON"),
        ("0.27.0", "HAWDB_027_PYTHON"),
        ("0.28.0", "HAWDB_028_PYTHON"),
        ("0.29.0", "HAWDB_029_PYTHON"),
    ],
)
def test_previous_v1_checkpoint_is_refused_without_rewriting_storage(
    tmp_path, close_mode, read_only, previous_version, interpreter_env
):
    previous_python = os.environ.get(interpreter_env)
    if previous_python is None:
        pytest.skip(
            f"Set {interpreter_env} to an isolated HawDB {previous_version} interpreter"
        )
    path = tmp_path / "previous.hawdb"
    create_database(previous_python, path, close_mode, previous_version)
    assert_row_page_format(path, b"SKRPGM01")
    before = storage_digests(path)
    engine = create_engine(
        URL.create(
            "hawdb",
            database=str(path),
            query={"read_only": "true" if read_only else "false"},
        )
    )
    try:
        # Published projects may open metadata before admitting branch data.
        with (
            pytest.raises(
                DBAPIError, match="recreate the development database"
            ) as error,
            engine.connect() as connection,
        ):
            connection.exec_driver_sql("SELECT id FROM accounts WHERE id = 1")
        assert isinstance(error.value.orig, hawdb.exceptions.StorageError)
        assert error.value.orig.kind == "unsupported_storage_format"
    finally:
        engine.dispose()
    assert storage_digests(path) == before


@pytest.mark.parametrize("close_mode", ["close", "process_exit"])
def test_current_v2_checkpoint_reopens_and_accepts_writes(tmp_path, close_mode):
    path = tmp_path / "current.hawdb"
    create_database(sys.executable, path, close_mode, "0.30.0")
    assert_row_page_format(path, b"SKRPGM02")
    engine = create_engine(URL.create("hawdb", database=str(path)))
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT id, amount, label, payload, legacy_at FROM accounts ORDER BY id"
            ).all() == [
                (
                    1,
                    Decimal("9007199254740993.0100"),
                    "first",
                    b"\x00\xff",
                    "2026-11-05T12:00:00.123456",
                ),
                (2, Decimal("0.2500"), "second", b"second", None),
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
        assert isinstance(columns[4]["type"], Text)
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


def test_packed_page_checkpoint_reopen_preserves_variable_width_rows(tmp_path):
    path = tmp_path / "packed.hawdb"
    url = URL.create("hawdb", database=str(path))
    records = Table(
        "records",
        MetaData(),
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("body", Text),
        Column("payload", LargeBinary),
    )
    expected = [
        (1, "narrow", b"\x00\xff"),
        (2, "wide" * 10_000, bytes(range(256)) * 400),
        (3, None, None),
    ]
    engine = create_engine(url)
    try:
        records.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                records.insert(),
                [
                    {"id": identifier, "body": body, "payload": payload}
                    for identifier, body, payload in expected
                ],
            )
    finally:
        engine.dispose()

    for rewrite in (True, False):
        # Checkpoint through the native maintenance API between SQLAlchemy handles.
        with hawdb.open(str(path)) as database:
            database.execute("CHECKPOINT")
        assert_row_page_format(path, b"SKRPGM02")
        reopened = create_engine(url)
        try:
            with reopened.begin() as connection:
                assert (
                    connection.execute(select(records).order_by(records.c.id)).all()
                    == expected
                )
                if rewrite:
                    expected[1] = (2, "updated" * 6_000, b"\xff\x00" * 40_000)
                    connection.execute(
                        records.update()
                        .where(records.c.id == 2)
                        .values(body=expected[1][1], payload=expected[1][2])
                    )
                    connection.execute(records.delete().where(records.c.id == 1))
                    expected = expected[1:]
        finally:
            reopened.dispose()

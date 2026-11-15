"""Create checkpointed databases in an isolated release-specific process."""

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from importlib.metadata import version

import hawdb


def main():
    assert version("hawdb") == sys.argv[3]
    database = hawdb.open(sys.argv[1])
    database.execute_sql(
        "CREATE TABLE accounts (id BIGINT PRIMARY KEY, amount NUMERIC(38,4), "
        "label TEXT UNIQUE, payload BYTEA, legacy_at TEXT)"
    )
    database.execute_sql("CREATE INDEX accounts_amount_idx ON accounts (amount)")
    for row in (
        [
            1,
            Decimal("9007199254740993.0100"),
            "first",
            b"\x00\xff",
            "2026-11-05T12:00:00.123456",
        ],
        [2, Decimal("0.2500"), "second", b"second", None],
    ):
        database.execute_sql(
            "INSERT INTO accounts (id, amount, label, payload, legacy_at) VALUES ($1, $2, $3, $4, $5)",
            row,
        )
    database.execute_sql("CREATE TABLE events (message TEXT)")
    database.execute_sql("INSERT INTO events (message) VALUES ($1)", ["same"])
    has_search = tuple(map(int, sys.argv[3].split("."))) >= (0, 31, 0)
    if has_search:
        database.execute_sql(
            "CREATE TABLE documents (id BIGINT PRIMARY KEY, body TEXT, "
            "created_at TIMESTAMPTZ)"
        )
        database.execute_sql("CREATE SEARCH INDEX documents_text ON documents (body)")
        database.execute_sql(
            "INSERT INTO documents VALUES ($1, $2, $3)",
            [1, "initial", datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)],
        )
    # A WAL-only fixture does not establish row-page format compatibility.
    database.execute("CHECKPOINT")
    database.execute_sql("INSERT INTO events (message) VALUES ($1)", ["same"])
    if has_search:
        database.execute_sql(
            "UPDATE documents SET body = $1 WHERE id = $2", ["database engine", 1]
        )
    if sys.argv[2] == "process_exit":
        # Preserve the checkpoint and committed WAL tail without handle cleanup.
        os._exit(0)
    database.close()


if __name__ == "__main__":
    main()

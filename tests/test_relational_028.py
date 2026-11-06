import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    bindparam,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError


@pytest.mark.parametrize("descending", [False, True])
def test_composite_primary_key_range_seek_with_rebinding_and_rollback(
    tmp_path, descending
):
    engine = create_engine(URL.create("hawdb", database=str(tmp_path / "range.hawdb")))
    metadata = MetaData()
    orders = Table(
        "orders",
        metadata,
        Column("tenant_id", Integer, primary_key=True, autoincrement=False),
        Column("warehouse_id", Integer, primary_key=True, autoincrement=False),
        Column("order_id", Integer, primary_key=True, autoincrement=False),
    )
    order = orders.c.order_id.desc() if descending else orders.c.order_id
    statement = (
        select(orders.c.order_id)
        .where(
            orders.c.tenant_id == bindparam("tenant"),
            orders.c.warehouse_id == bindparam("warehouse"),
            orders.c.order_id > bindparam("lower"),
            orders.c.order_id <= bindparam("upper"),
        )
        .order_by(order)
        .limit(2)
        .offset(1)
    )
    first = {"tenant": 1, "warehouse": 1, "lower": 2, "upper": 6}
    first_expected = [5, 4] if descending else [4, 5]
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                orders.insert(),
                [
                    {
                        "tenant_id": tenant,
                        "warehouse_id": warehouse,
                        "order_id": identifier,
                    }
                    for tenant in (1, 2)
                    for warehouse in (1, 2)
                    for identifier in range(1, 21)
                ],
            )
        with engine.connect() as connection:
            compiled = statement.compile(dialect=engine.dialect)
            values = {**compiled.params, **first}
            plan = (
                connection.exec_driver_sql(
                    f"EXPLAIN {compiled}",
                    tuple(values[name] for name in compiled.positiontup),
                )
                .mappings()
                .all()
            )
            assert any("TableRangeScanExec" in row["id"] for row in plan)
            assert any(
                "primary_key_range" in (row["access object"] or "") for row in plan
            )
            if not descending:
                assert all(
                    "SortExec" not in row["id"] and "TopNExec" not in row["id"]
                    for row in plan
                )

            for parameters, expected in (
                (first, first_expected),
                (
                    {"tenant": 2, "warehouse": 2, "lower": 17, "upper": 20},
                    [19, 18] if descending else [19, 20],
                ),
                ({"tenant": 1, "warehouse": 1, "lower": None, "upper": 6}, []),
                ({"tenant": 1, "warehouse": 1, "lower": 6, "upper": 2}, []),
                (first, first_expected),
            ):
                assert connection.scalars(statement, parameters).all() == expected
            connection.rollback()

            connection.execute(
                orders.delete().where(
                    orders.c.tenant_id == 1,
                    orders.c.warehouse_id == 1,
                    orders.c.order_id == 4,
                )
            )
            assert connection.scalars(statement, first).all() == (
                [5, 3] if descending else [5, 6]
            )
            connection.rollback()
            assert connection.scalars(statement, first).all() == first_expected
    finally:
        engine.dispose()


def test_batch_load_scans_and_aggregates_after_multiple_commits(tmp_path):
    engine = create_engine(URL.create("hawdb", database=str(tmp_path / "batch.hawdb")))
    metadata = MetaData()
    records = Table(
        "records",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
        Column("value", Integer),
    )
    count = 20_000
    total = count * (count + 1) // 2
    try:
        metadata.create_all(engine)
        with engine.connect() as connection:
            for start in range(1, count + 1, 4_000):
                connection.execute(
                    records.insert(),
                    [
                        {"id": identifier, "value": identifier}
                        for identifier in range(start, start + 4_000)
                    ],
                )
                connection.commit()
            assert connection.scalar(select(func.count()).select_from(records)) == count
            assert connection.scalar(select(func.sum(records.c.value))) == total
            assert connection.scalars(
                select(records.c.id)
                .where(records.c.value >= 0)
                .order_by(records.c.id)
                .limit(3)
            ).all() == [1, 2, 3]
            assert connection.scalars(
                select(records.c.id).order_by(records.c.value.desc()).limit(2)
            ).all() == [count, count - 1]

            connection.execute(
                records.update()
                .where(records.c.id <= 1_000)
                .values(value=records.c.value + 1)
            )
            connection.execute(records.delete().where(records.c.id > count - 100))
            connection.commit()
            expected_total = total + 1_000 - sum(range(count - 99, count + 1))
            assert (
                connection.scalar(select(func.count()).select_from(records))
                == count - 100
            )
            assert (
                connection.scalar(select(func.sum(records.c.value))) == expected_total
            )
    finally:
        engine.dispose()

    reopened = create_engine(engine.url)
    try:
        with reopened.connect() as connection:
            assert (
                connection.scalar(select(func.count()).select_from(records))
                == count - 100
            )
            assert (
                connection.scalar(select(func.sum(records.c.value))) == expected_total
            )
    finally:
        reopened.dispose()


def test_statement_row_budget_refuses_scan_but_allows_point_read(tmp_path):
    url = URL.create("hawdb", database=str(tmp_path / "budget.hawdb"))
    engine = create_engine(url)
    metadata = MetaData()
    records = Table(
        "records",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                records.insert(), [{"id": identifier} for identifier in (1, 2, 3)]
            )
    finally:
        engine.dispose()

    limited = create_engine(url, connect_args={"max_read_result_rows": 1})
    try:
        with limited.connect() as connection:
            with pytest.raises(DBAPIError, match="max_intermediate_rows 1"):
                connection.execute(select(records.c.id))
            assert connection.scalar(select(records.c.id).where(records.c.id == 3)) == 3
    finally:
        limited.dispose()

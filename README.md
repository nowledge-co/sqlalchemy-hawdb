# SQLAlchemy HawDB

`sqlalchemy-hawdb` provides a SQLAlchemy 2.x dialect for the embedded HawDB
database. It uses HawDB's own PEP 249 driver and does not add a wire protocol or
duplicate its DBAPI implementation.

The dialect reuses SQLAlchemy's PostgreSQL type compiler and identifier
preparer, overriding HawDB's scalar type mappings and bounded numeric defaults.
Connection handling, transactions, and catalog queries use the embedded HawDB
driver and its supported SQL subset.

## Requirements

- Python 3.11 or newer
- HawDB Python binding 0.28.x (CI tests 0.28.0)
- SQLAlchemy 2.x (CI tests both 2.0.x and 2.1.x)

Install the dialect and its HawDB dependency from PyPI:

```sh
python -m pip install sqlalchemy-hawdb
```

To build HawDB 0.28.0 from source, build and install its wheel from the upstream
release tag first:

```sh
git clone --branch v0.28.0 https://github.com/nowledge-co/hawdb.git
cd hawdb/bindings/python
python -m pip install maturin==1.15.0
maturin build --release --out dist
python -m pip install dist/*.whl
```

To install this checkout instead:

```sh
python -m pip install /path/to/sqlalchemy_hawdb
```

## Connect

Use an absolute database directory path with four slashes:

```python
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

engine = create_engine("hawdb:////var/lib/my-app/knowledge")
metadata = MetaData()
records = Table(
    "records",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("body", String(500), nullable=False),
)

metadata.create_all(engine)
with engine.begin() as connection:
    connection.execute(records.insert(), {"id": 1, "body": "Hello HawDB"})
```

HawDB creates the database directory itself; its parent directory must already
exist.

Use `hawdb:///:memory:` for an in-memory database. Like SQLAlchemy's SQLite
dialect, the in-memory engine keeps one database per thread. Add
`?read_only=true` to open an existing database read-only.

HawDB connections are thread-affine and hold their database path until closed.
For file databases the dialect uses SQLAlchemy's `NullPool`: each SQLAlchemy
connection closes its HawDB handle when returned. Do not keep multiple
connections to the same database path open at once or pass a SQLAlchemy
connection between threads. In-memory connections are kept per thread and must
also be used serially within that thread.

### Explicit namespace durability anchor

HawDB 0.26 accepts an optional `namespace_durability_anchor` for persistent
databases. Forward it through SQLAlchemy's standard `connect_args`:

```python
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

engine = create_engine(
    URL.create("hawdb", database=str(database_path)),
    connect_args={"namespace_durability_anchor": durable_ancestor},
)
```

`database_path` is the database directory. `durable_ancestor` must be an existing
ancestor whose own name and ancestry the host guarantees already durable and
stable while the database is open. A Windows host can obtain the user's
OS-provisioned LocalAppData path through the known-folder API. Pass the same
anchor on read-only reopens. The default remains `None`; the dialect does not
infer an anchor from directory existence or a permission error. See the
[upstream host contract](https://github.com/nowledge-co/hawdb/blob/v0.28.0/bindings/python/README.md).

## Supported behavior

- SQLAlchemy Core and ORM statements using HawDB's relational SQL subset
- Positional `$1`, `$2`, ... binding through HawDB's native DBAPI driver
- Explicit SQLAlchemy transactions mapped to HawDB commit and rollback
- Table, column, primary key, unique constraint, and index reflection from
  HawDB's `information_schema` and `pg_catalog` views
- The default `public` schema
- HawDB-native scalar DDL types: integer types map to `BIGINT`, floating types
  to `DOUBLE PRECISION`, strings to `TEXT`, and binary values to `BYTEA`;
  SQLAlchemy string lengths are not enforced by HawDB
- Native `Numeric`, `NUMERIC`, and `DECIMAL` columns with exact Python
  `decimal.Decimal` binding and precision/scale reflection
- SQLAlchemy Core tables without primary keys, including duplicate rows;
  HawDB's hidden row handles are not SQL columns or generated primary keys,
  and SQLAlchemy ORM mappings still require an application-visible primary key

### Exact numeric values

Use `Numeric(precision, scale)` and Python `decimal.Decimal` for exact values.
For example, `Numeric(38, 4)` preserves `Decimal("9007199254740993.0100")`
without converting it to a Python float. Reflected numeric columns retain
their declared precision and scale. `Numeric(asdecimal=False)` explicitly
converts results to Python floats; floating-point columns remain approximate.

HawDB supports precision from 1 through 38 and scale from 0 through precision.
Omitted precision defaults to 38 and omitted scale to 0, so bare `Numeric()`
has `NUMERIC(38, 0)` semantics and `Numeric(scale=4)` uses `NUMERIC(38, 4)`.
Column assignment rounds to the declared scale with ties away from zero and
rejects precision overflow. Decimal/`DOUBLE PRECISION` arithmetic and comparisons
require an explicit `CAST`; this bounded numeric contract does not provide PostgreSQL's
arbitrary-precision numeric semantics.

### HawDB 0.28 compatibility

HawDB 0.28 retains the Python DB-API, value conversions, and connection contract
from 0.27. CI checks published PyPI wheels separately from wheels built from the
release tag.

Queries that fix an equality prefix of a composite primary key and optionally
bound the next key column can now use a primary-key range seek. The dialect's
existing bound parameters, `order_by`, `limit`, and `offset` work with this
engine optimization. Ascending key order can avoid a sort; descending queries
still sort. Always specify `order_by` when application behavior depends on row
order, since a new access path can change unordered results.

Relational scans no longer have a fixed 16,384-entry cap on merged
uncheckpointed rows. They use the statement row budget instead. An explicit
`max_read_result_rows` supplied through `connect_args` still bounds SQL reads.
The integration tests cover a 20,000-row batch load, aggregation, mutations,
reopen, and budget refusal followed by a point read. They do not control
checkpoint timing; engine-level tests own the deterministic overlay regression.

New variable-length paths, relationship type disjunctions, scalar functions,
and comprehensions belong to Cypher. Per-query DiskANN beam/candidate settings
belong to the Rust vector search API. These do not add relational SQL syntax
or Python connection options. The embedded API renaming in this release is a
Rust source migration; the dialect does not use the renamed contracts. See the
[0.28 release notes](https://github.com/nowledge-co/hawdb/releases/tag/v0.28.0).

Since 0.26, HawDB uses PEG for its supported SQL grammar and retains bounded,
schema-neutral templates for repeated parameterized INSERT, UPDATE, and DELETE
statements. SQLAlchemy's compiled-statement cache and HawDB's syntax-template
cache remain separate; values are bound for every execution. UPDATE arithmetic
works with typed DOUBLE and Decimal parameters, with the explicit-cast rules
above. These engine changes do not imply support for the entire PostgreSQL
grammar.

CI creates persistent databases in isolated HawDB 0.25.0, 0.26.0, and 0.27.0
interpreters, then checks reads, indexes, Decimal scale, duplicate keyless rows,
new writes, and reopen using 0.28. Both normal close and abrupt process exit
after committed writes are covered. This is a focused default-configuration
upgrade fixture, not a migration facility for older formats.

### Upgrading from 0.24

HawDB 0.25 changes its relational durable storage formats. Older development
database formats are rejected and must be recreated; this dialect provides no
automatic migration. Preserve any data needed from an older database before
recreating it. See the upstream
[compatibility notes](https://github.com/nowledge-co/hawdb/pull/1474).

`Numeric` now creates native `NUMERIC` columns instead of `DOUBLE PRECISION`.
Existing floating-point values do not regain precision by changing their
SQLAlchemy type declaration.

### Limitations

HawDB does not report affected row counts, so DBAPI `rowcount` is `-1` for
statements without a result set. Date and time types compile to `TEXT`; their
values are encoded and decoded by SQLAlchemy's standard processors.
Auto-generated identity columns, sequences, schema alteration, savepoints, and
concurrent connections to one database path are not supported by this initial
dialect.

## Development

Install the project, the HawDB dependency, and the test tools:

```sh
python -m pip install -e '.[test]'
python -m pytest
```

GitHub Actions builds the source distribution and wheel, checks their metadata,
builds HawDB v0.28.0 from its upstream release tag, and tests the installed
dialect wheel on Python 3.11-3.13 with SQLAlchemy 2.0.x and 2.1.x for every push
and pull request. An additional Python 3.11 / SQLAlchemy 2.1 job runs the same
tests with HawDB's published PyPI wheel instead of the source-built wheel.
All jobs run on Linux.

Each test job also installs HawDB 0.25.0 and 0.27.0 from PyPI and HawDB 0.26.0
from its release tag in separate interpreters for cross-version data fixtures. Version
0.26.0 was not published to PyPI. To include those fixtures locally, set
`HAWDB_PREVIOUS_PYTHON` to an interpreter containing exactly `hawdb==0.25.0` and
`HAWDB_026_PYTHON` to one containing exactly `hawdb==0.26.0`, and
`HAWDB_027_PYTHON` to one containing exactly `hawdb==0.27.0`. Upgrade tests for
an interpreter are skipped when its environment variable is not configured.

These are the project's targeted compatibility tests. SQLAlchemy's full
`sqlalchemy.testing.suite` additionally requires HawDB-specific capability
requirements and database provisioning/cleanup before it can serve as a
compliance gate.

## Publishing

`.github/workflows/pypi.yaml` publishes the tested source distribution and wheel
when a GitHub Release is published. The release tag must match the version in
`pyproject.toml`, for example `v0.28.0`. A manual workflow dispatch runs the same
build and tests without uploading to PyPI.

Configure a PyPI Trusted Publisher for the `sqlalchemy-hawdb` project with:

- Owner: `nowledge-co`
- Repository: `sqlalchemy-hawdb`
- Workflow: `pypi.yaml`
- Environment: `pypi`

For the first release, configure a pending publisher in your PyPI account.
The publish job uses GitHub's `pypi` environment and OIDC; no PyPI API token
secret is required.

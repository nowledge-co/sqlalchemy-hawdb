# SQLAlchemy HawDB

`sqlalchemy_hawdb` provides a SQLAlchemy 2.x dialect for the embedded HawDB
database. It uses HawDB's own PEP 249 driver and does not add a wire protocol or
duplicate its DBAPI implementation.

## Requirements

- Python 3.11 or newer
- HawDB Python binding 0.22.x (CI tests 0.22.0)
- SQLAlchemy 2.x

Install the dialect and its HawDB dependency from PyPI:

```sh
python -m pip install sqlalchemy_hawdb
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

## Supported behavior

- SQLAlchemy Core and ORM statements using HawDB's relational SQL subset
- Positional `$1`, `$2`, ... binding through HawDB's native DBAPI driver
- Explicit SQLAlchemy transactions mapped to HawDB commit and rollback
- Table, column, primary key, unique constraint, and index reflection from
  HawDB's `information_schema` and `pg_catalog` views
- The default `public` schema
- HawDB-native scalar DDL types: integer types map to `BIGINT`, floating and
  numeric types to `DOUBLE PRECISION`, strings to `TEXT`, and binary values to
  `BYTEA`; SQLAlchemy string lengths and numeric precision are not enforced by
  HawDB

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
and tests the installed wheel against `hawdb==0.22.0` from PyPI on Python
3.11-3.13 for every push and pull request. Missing HawDB wheels fail CI rather
than skipping integration tests or falling back to a source build.

## Publishing

`.github/workflows/pypi.yaml` publishes the tested source distribution and wheel
when a GitHub Release is published. The release tag must match the version in
`pyproject.toml`, for example `v0.22.0`. A manual workflow dispatch runs the same
build and tests without uploading to PyPI.

Configure a PyPI Trusted Publisher for the `sqlalchemy_hawdb` project with:

- Owner: `nowledge-co`
- Repository: `sqlalchemy_hawdb`
- Workflow: `pypi.yaml`
- Environment: `pypi`

For the first release, configure a pending publisher in your PyPI account.
The publish job uses GitHub's `pypi` environment and OIDC; no PyPI API token
secret is required.

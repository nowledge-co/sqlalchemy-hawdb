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
- HawDB Python binding 0.31.x (CI tests 0.31.0)
- SQLAlchemy 2.x (CI tests both 2.0.x and 2.1.x)

Install the dialect and its HawDB dependency from PyPI:

```sh
python -m pip install sqlalchemy-hawdb
```

HawDB 0.30 introduces row-page format v2. Existing databases containing v1
relational checkpoints cannot be opened directly; see [Upgrading to 0.30](#upgrading-to-030).

To build HawDB 0.31.0 from source, build and install its wheel from the upstream
release tag first:

```sh
git clone --branch v0.31.0 https://github.com/nowledge-co/hawdb.git
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
[upstream host contract](https://github.com/nowledge-co/hawdb/blob/v0.31.0/bindings/python/README.md).

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
- Native `DateTime` / `TIMESTAMP` columns, Python `datetime.datetime` values,
  timestamp precision/timezone reflection, and clock column defaults
- Single-statement DML row counts, ORM version checks, and typed relational
  `IntegrityError` / `DataError` exceptions through HawDB's DB-API
- BM25 search-index DDL and reflection through `Index(..., hawdb_using="bm25")`
- SQLAlchemy Core tables without primary keys, including duplicate rows;
  HawDB's hidden row handles are not SQL columns or generated primary keys,
  and SQLAlchemy ORM mappings still require an application-visible primary key

### Affected rows and errors

HawDB 0.31 reports matched rows for ordinary RowPage-table INSERT, UPDATE, and
DELETE statements, including zero matches. The dialect enables SQLAlchemy's
single-statement row-count checks, including ORM optimistic version checks.
Inside a transaction a count describes staged work; rollback still discards
it. `executemany`, DDL, and strict append-table writes still report `-1`, so
`supports_sane_multi_rowcount` remains false. Strict append tables are not a
supported target for ORM version checks. DML RETURNING support is not enabled
by this change.

Relational constraint violations (such as a duplicate primary key or NOT NULL
violation) become SQLAlchemy `IntegrityError`; wrong-type/schema refusals
become `DataError`. The driver retains `kind="constraint"` / `"schema"` and
`retryable=False` on the original error, accessible as `error.orig`. Storage
corruption and unsupported formats retain their separate driver categories;
the dialect does not classify errors by matching message text. A failed single
statement leaves the transaction usable; a failed `executemany` aborts its
whole transaction and requires rollback, as before.

### SQL BM25 search indexes

Use the normal SQLAlchemy index lifecycle for an engine-maintained index on
one TEXT column:

```python
from sqlalchemy import Column, Index, Integer, MetaData, Table, Text, text

documents = Table(
    "documents", MetaData(),
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("body", Text),
)
search_index = Index(
    "documents_text", documents.c.body,
    hawdb_using="bm25", hawdb_analyzer="cjk_ngram",
)
documents.metadata.create_all(engine)

query = text(
    "SELECT d.id, score FROM text_search(:index, :query, top_k => :top_k) AS d "
    "ORDER BY score DESC, d.id"
)
with engine.connect() as connection:
    rows = connection.execute(
        query, {"index": "documents_text", "query": "database", "top_k": 20}
    ).all()

search_index.drop(engine)
```

`hawdb_analyzer` is optional; `cjk_ngram` is the only supported analyzer.
`hawdb_using="btree"` or no method keeps ordinary index DDL. Search indexes
cannot be unique, contain expressions, or index multiple columns. Reflection
through `Inspector.get_indexes()` preserves the source column and the
`hawdb_using` / `hawdb_analyzer` options, so recreating or dropping a reflected
index uses SEARCH INDEX DDL. Search indexes are omitted from table/view lists.

`text_search` accepts bound arguments and requires `top_k` from 1 to 10,000.
It returns source rows with a virtual DOUBLE PRECISION `score`. Filters apply
before the top-k cut, and the index sees staged writes in the transaction.
Postings and source rows commit or roll back together. Joins, aggregates,
DISTINCT, and locking clauses are unsupported for this table function. See the
[upstream SQL search contract](https://github.com/nowledge-co/hawdb/blob/v0.31.0/docs/SQL_SEARCH_INDEXES.md).

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
rejects precision overflow. Decimal columns and bound Decimal parameters still
require an explicit `CAST` when mixed with `DOUBLE PRECISION`. Since 0.29,
numeric literals beside DOUBLE operands are coerced to DOUBLE, for example
`balance + 12.5`; literals beside Decimal operands stay exact. This bounded
numeric contract does not provide PostgreSQL's arbitrary-precision semantics.

### Native timestamps and defaults

`DateTime()` and SQLAlchemy `TIMESTAMP()` now create native `TIMESTAMP WITHOUT
TIME ZONE` columns. `DateTime(timezone=True)` creates `TIMESTAMP WITH TIME
ZONE` (HawDB's `TIMESTAMPTZ`). Naive Python datetimes bind to TIMESTAMP; aware
datetimes bind to TIMESTAMPTZ and return normalized to UTC. Cursors preserve
microseconds and NULLs without converting timestamp values through text.

Use `sqlalchemy.dialects.postgresql.TIMESTAMP(precision=3, timezone=True)`
when an explicit precision is needed. Precision is 0 through 6, default 6;
reflection preserves both precision and timezone. Assignment rounds with
halfway cases away from 2000-01-01. Python-unrepresentable years retain the
driver's lossless tagged dict instead of being truncated into a datetime.

Clock defaults use the ordinary SQLAlchemy `server_default` interface:

```python
from sqlalchemy import Column, DateTime, func

created_at = Column("created_at", DateTime(timezone=True), server_default=func.now())
local_at = Column("local_at", DateTime(), server_default=func.localtimestamp())
```

`func.current_timestamp()` and `func.now()` return the transaction's start time
as an aware UTC value. `func.localtimestamp()` returns its naive UTC wall-clock
equivalent. The clock stays fixed within a transaction and is evaluated again
in later transactions. Default values are persisted when the row is written.

HawDB has no session timezone. TIMESTAMP/TIMESTAMPTZ comparisons require an
explicit CAST when their kinds differ. Native DATE/TIME, interval arithmetic,
`AT TIME ZONE`, and `EXTRACT` remain unsupported. See the
[timestamp contract](https://github.com/nowledge-co/hawdb/blob/v0.31.0/docs/specs/POSTGRES_SQL_PGQ_SPEC.md#sql-timestamps).

### Upgrading to 0.31

The release introduces engine-wide persistent format version 2. HawDB 0.30
predates that version, so the release notes report the format comparison as
unknown. Row-page v2 magic alone does not establish compatibility of every
database codec. CI includes a focused 0.30 checkpoint-plus-WAL-tail fixture;
that is not a promise for every old graph/schema/search configuration.

The new logical dump, verification, and format-version APIs are available in
Rust and the C ABI; HawDB 0.31's Python binding does not expose them. The
dialect adds no automatic migration. Preserve the original database and use a
reader that supports its exact format before exporting or rebuilding. A new
reader cannot export a format it refuses. See the
[logical dump contract](https://github.com/nowledge-co/hawdb/blob/v0.31.0/docs/LOGICAL_DUMP.md).

### Upgrading to 0.30

HawDB 0.30 stores relational row pages at their exact encoded length in
row-page format v2 (`SKRPGM02`), replacing v1's fixed 1 MiB slots. Databases
containing v1 (`SKRPGM01`) checkpoints, including those created by 0.25-0.29,
are explicitly refused with a database-recreation message. A successful
metadata open does not establish compatibility: branch data admission can fail
on the first statement.

SQLAlchemy exposes this as `DBAPIError`; its `orig` is a HawDB `StorageError`
with `kind == "unsupported_storage_format"`. The dialect preserves that error
and does not delete, repair, or recreate the database automatically. This is
an unsupported storage format, rather than a damaged-data classification.

Preserve the original database and use its matching older HawDB reader to
export any data needed before upgrading. Recreate the schema and import the
exported data into a separate 0.30 database. This package provides no automatic
format migration or downgrade path. Do not use the earlier WAL/reopen fixtures
as evidence that checkpointed v1 databases can be upgraded in place. See the
[upstream format contract](https://github.com/nowledge-co/hawdb/blob/v0.30.0/docs/RELATIONAL_ROW_PAGE_COMPACTION.md#development-format).

### Timestamp changes since 0.29

Starting with dialect 0.29, `DateTime` DDL uses native TIMESTAMP instead of
TEXT. Existing TEXT columns keep their type and string results, including ISO timestamp text;
upgrading the dependency or changing a Python declaration does not convert
stored columns automatically. Use an explicit application migration when
conversion is needed.

Timestamp values/types and clock defaults introduced additive on-disk tags in
HawDB 0.29. Older binaries reject databases that contain these tags. This
timestamp boundary does not relax 0.30's independent row-page v2 requirement.

### HawDB 0.31 compatibility

The dialect continues to reuse PostgreSQL's type compiler and identifier
preparer. Native timestamp/Decimal conversion and error classification stay
in HawDB's DB-API, and the engine owns BM25 scoring and index maintenance.
Existing pooling and transaction ownership remain unchanged. CI checks
published PyPI wheels separately from release-source wheels.

The new `max_read_intermediate_rows` setting separates relational scan work
from result rows in Rust and the C ABI, but the 0.31 Python binding does not
accept it. Do not pass it through `connect_args`. The existing
`max_read_result_rows` still controls the inherited scan ceiling in Python;
`LIMIT` does not authorize more scan work. No new dialect URL option is added.

Cypher string/label functions, graph property defaults, FFI transactions and
backup/restore, and search projection APIs are outside the SQLAlchemy SQL
surface. See the
[0.31 release notes](https://github.com/nowledge-co/hawdb/releases/tag/v0.31.0).

Queries that fix an equality prefix of a composite primary key and optionally
bound the next key column can use a primary-key range seek since 0.28. The
dialect's existing bound parameters, `order_by`, `limit`, and `offset` work with this
engine optimization. Ascending key order can avoid a sort; descending queries
still sort. Always specify `order_by` when application behavior depends on row
order, since a new access path can change unordered results.

Relational scans no longer have a fixed 16,384-entry cap on merged
uncheckpointed rows. They use the statement row budget instead. An explicit
`max_read_result_rows` supplied through `connect_args` still bounds SQL reads.
The integration tests cover a 20,000-row batch load, aggregation, mutations,
reopen, and budget refusal followed by a point read. They do not control
checkpoint timing; engine-level tests own the deterministic overlay regression.

Since 0.26, HawDB uses PEG for its supported SQL grammar and retains bounded,
schema-neutral templates for repeated parameterized INSERT, UPDATE, and DELETE
statements. SQLAlchemy's compiled-statement cache and HawDB's syntax-template
cache remain separate; values are bound for every execution. UPDATE arithmetic
works with typed DOUBLE and Decimal parameters, with the explicit-cast rules
above. These engine changes do not imply support for the entire PostgreSQL
grammar.

CI creates real v1 checkpoints in isolated HawDB 0.25.0-0.29.0 interpreters,
then verifies that read-only and writable opens refuse them with the typed
unsupported-format error and leave their storage files unchanged. Separate
0.30 and 0.31 v2 fixtures check reads, indexes, Decimal scale, duplicate keyless rows,
legacy timestamp-looking TEXT, new writes, and reopen. Both normal close and
process exit after an explicit checkpoint plus committed WAL tail are covered.
A variable-width checkpoint test covers narrow rows, rows larger than the 32 KiB writer target,
NULLs, updates, and deletes through SQLAlchemy.

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

Generic `Date` and `Time` retain TEXT DDL;
native SQL DATE/TIME and Python date/time parameters are unsupported. Encode
such application values explicitly when using TEXT columns.
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
builds HawDB v0.31.0 from its upstream release tag, and tests the installed
dialect wheel on Python 3.11-3.13 with SQLAlchemy 2.0.x and 2.1.x for every push
and pull request. An additional Python 3.11 / SQLAlchemy 2.1 job runs the same
tests with HawDB's published PyPI wheel instead of the source-built wheel.
All jobs run on Linux.

Each test job also installs HawDB 0.25.0, 0.27.0, 0.28.0, 0.29.0, and 0.30.0 from PyPI
and HawDB 0.26.0 from its release tag in separate interpreters for v1 refusal
fixtures. Version 0.26.0 was not published to PyPI. To include those fixtures locally, set
`HAWDB_PREVIOUS_PYTHON` to an interpreter containing exactly `hawdb==0.25.0` and
`HAWDB_026_PYTHON` to one containing exactly `hawdb==0.26.0`, and
`HAWDB_027_PYTHON` to one containing exactly `hawdb==0.27.0`, and
`HAWDB_028_PYTHON` to one containing exactly `hawdb==0.28.0`, and
`HAWDB_029_PYTHON` to one containing exactly `hawdb==0.29.0`, and
`HAWDB_030_PYTHON` to one containing exactly `hawdb==0.30.0`. Cross-version tests for
an interpreter are skipped when its environment variable is not configured.

These are the project's targeted compatibility tests. SQLAlchemy's full
`sqlalchemy.testing.suite` additionally requires HawDB-specific capability
requirements and database provisioning/cleanup before it can serve as a
compliance gate.

## Publishing

`.github/workflows/pypi.yaml` publishes the tested source distribution and wheel
when a GitHub Release is published. The release tag must match the version in
`pyproject.toml`, for example `v0.31.0`. A manual workflow dispatch runs the same
build and tests without uploading to PyPI.

Configure a PyPI Trusted Publisher for the `sqlalchemy-hawdb` project with:

- Owner: `nowledge-co`
- Repository: `sqlalchemy-hawdb`
- Workflow: `pypi.yaml`
- Environment: `pypi`

For the first release, configure a pending publisher in your PyPI account.
The publish job uses GitHub's `pypi` environment and OIDC; no PyPI API token
secret is required.

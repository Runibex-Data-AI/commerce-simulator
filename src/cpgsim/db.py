"""Database setup: connection, DSN resolution, schema installation, roles, reset.

Two roles (SPEC, loading contract):

* ``sim_writer`` owns every schema and is the only role the generator uses.
* ``bi_reader`` has ``USAGE`` + ``SELECT`` on the six business schemas (shop,
  wms, erp, mkt, feeds, shelf), including tables created later (default
  privileges), and nothing on ``sim`` or ``truth``.

Roles are cluster-wide, so who creates them depends on the setup:

* **Docker**: the Postgres container's initdb script
  (``docker/postgres/initdb/10-cpgsim.sh``) creates both roles with their
  passwords and grants ``sim_writer`` ``CREATE`` on the database. The generator
  then connects as ``sim_writer`` and ``init_db`` creates the schemas (owned by
  ``sim_writer``) and the grants.
* **Manual**: an admin runs ``sql/roles.sql`` with psql, then runs ``sim init``
  as ``sim_writer``.

``init_db`` also works when run by a superuser: it creates missing roles,
grants ``CREATE`` on the database to the writer and switches to the writer role
(``SET ROLE``) while creating objects, so ownership is always ``sim_writer``.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

log = logging.getLogger("cpgsim.db")

#: Schema files under ``<sql dir>/schemas/<name>.sql``, applied in this order.
SCHEMA_FILES: tuple[str, ...] = ("sim", "truth", "shop", "wms", "erp", "mkt", "feeds", "shelf")
#: Schemas readable by BI tools.
BUSINESS_SCHEMAS: tuple[str, ...] = ("shop", "wms", "erp", "mkt", "feeds", "shelf")
#: Simulator internals and ground truth; never granted to the reader role.
INTERNAL_SCHEMAS: tuple[str, ...] = ("sim", "truth")
#: Schemas whose tables ``reset_data`` truncates.
DATA_SCHEMAS: tuple[str, ...] = BUSINESS_SCHEMAS + INTERNAL_SCHEMAS
#: Tables that survive ``reset_data`` (operator settings, not simulated data).
KEEP_ON_RESET: frozenset[str] = frozenset({"sim.settings"})

WRITER_ROLE = "sim_writer"
READER_ROLE = "bi_reader"

ENV_DSN = "CPGSIM_DSN"
ENV_SQL_DIR = "CPGSIM_SQL_DIR"

SEED_KEY = "seed"
# Advisory lock serialising `sim scenario enable/disable` with the engine's override refresh.
SCENARIO_LOCK = 0x6370_6773_696D  # "cpgsim"
# Session-level advisory lock held by the engine (one ``Simulation``) for its
# whole lifetime: one writer per database. ``sim reset`` refuses while it is held.
WRITER_LOCK = 0x6370_6773_696D_57  # "cpgsimW"


class DsnError(RuntimeError):
    """No database connection string could be resolved."""


class InitError(RuntimeError):
    """The database cannot be initialised with the current role or files."""


class WriterLockHeld(RuntimeError):
    """Another session (a running generator) holds :data:`WRITER_LOCK`."""


# -- locations ---------------------------------------------------------------


def _repo_root() -> Path:
    # src/cpgsim/db.py -> repository root (only meaningful in a source checkout)
    return Path(__file__).resolve().parents[2]


def find_sql_dir(env: Mapping[str, str] | None = None) -> Path:
    """Directory holding ``schemas/*.sql`` and ``roles.sql``.

    ``CPGSIM_SQL_DIR`` wins when set (the Docker image sets it to ``/app/sql``);
    otherwise ``sql/`` in the source checkout this module was loaded from.
    """
    env = os.environ if env is None else env
    explicit = env.get(ENV_SQL_DIR)
    if explicit:
        p = Path(explicit)
        if not (p / "schemas").is_dir():
            raise InitError(f"{ENV_SQL_DIR}={explicit} has no schemas/ directory")
        return p
    p = _repo_root() / "sql"
    if (p / "schemas").is_dir():
        return p
    raise InitError(
        f"cannot find the sql/ directory; set {ENV_SQL_DIR} (the Docker image uses /app/sql)"
    )


def schema_file(name: str, sql_dir: Path | None = None) -> Path:
    return (sql_dir or find_sql_dir()) / "schemas" / f"{name}.sql"


# -- connecting ----------------------------------------------------------------


def resolve_dsn(cli_dsn: str | None, cfg: Any = None, env: Mapping[str, str] | None = None) -> str:
    """CLI ``--dsn`` > env ``CPGSIM_DSN`` > ``cfg.database.dsn`` > error."""
    env = os.environ if env is None else env
    for candidate in (
        cli_dsn,
        env.get(ENV_DSN),
        getattr(getattr(cfg, "database", None), "dsn", None),
    ):
        if candidate:
            return candidate
    raise DsnError(
        f"no database configured: pass --dsn, set {ENV_DSN}, or set database.dsn in an overlay"
    )


def connect(dsn: str, **kwargs: Any) -> psycopg.Connection:
    """Open a psycopg 3 connection with explicit transactions (autocommit off).

    The session time zone is pinned to UTC (``-c TimeZone=UTC``, appended to
    any ``options`` from the DSN or the caller unless they set ``TimeZone``
    themselves), so ``timestamptz`` values come back in UTC whatever the
    server, database or role default is. The engine also normalises what it
    reads; this is defence in depth.
    """
    kwargs.setdefault("application_name", "cpgsim")
    kwargs["options"] = session_options(dsn, kwargs.get("options"))
    return psycopg.connect(dsn, autocommit=False, **kwargs)


def session_options(dsn: str, extra: str | None = None) -> str:
    """libpq ``options`` for :func:`connect`: the DSN's and ``extra`` plus ``TimeZone=UTC``."""
    try:
        base = conninfo_to_dict(dsn).get("options") or ""
    except psycopg.ProgrammingError:
        base = ""
    opts = " ".join(o for o in (str(base).strip(), (extra or "").strip()) if o)
    if "timezone" not in opts.lower():
        opts = f"{opts} -c TimeZone=UTC".strip()
    return opts


def wait_for_db(
    dsn: str,
    attempts: int = 60,
    delay: float = 2.0,
    max_delay: float = 10.0,
) -> psycopg.Connection:
    """Connect, retrying with capped backoff while the server comes up."""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            conn = connect(dsn, connect_timeout=10)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return conn
        except psycopg.OperationalError as e:
            last = e
            log.info("database not ready (attempt %d/%d): %s", i, attempts, str(e).strip())
            time.sleep(min(delay * (1.5 ** (i - 1)), max_delay))
    raise InitError(f"database not reachable after {attempts} attempts: {last}")


# -- settings ------------------------------------------------------------------


def get_setting(conn: psycopg.Connection, key: str) -> str | None:
    """Value from ``sim.settings`` or None (also when the table does not exist yet).

    Runs in its own transaction and ends it.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM sim.settings WHERE key = %s", (key,))
            row = cur.fetchone()
        conn.commit()
        return None if row is None else row[0]
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName,
            psycopg.errors.InsufficientPrivilege):
        conn.rollback()
        return None


def set_setting(cur: psycopg.Cursor, key: str, value: str | None) -> None:
    """Upsert (or delete, for None) a setting; the caller commits."""
    if value is None:
        cur.execute("DELETE FROM sim.settings WHERE key = %s", (key,))
    else:
        cur.execute(
            "INSERT INTO sim.settings (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


def persisted_seed(conn: psycopg.Connection) -> int | None:
    """Seed set by ``sim reset --seed N``; overrides ``cfg.seed`` in the CLI."""
    v = get_setting(conn, SEED_KEY)
    return None if v is None else int(v)


# -- init ----------------------------------------------------------------------


def _role_info(cur: psycopg.Cursor) -> tuple[str, bool, bool]:
    cur.execute(
        "SELECT current_user, rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user"
    )
    me, is_super, createrole = cur.fetchone()  # type: ignore[misc]
    return me, bool(is_super), bool(createrole)


def _role_exists(cur: psycopg.Cursor, role: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return cur.fetchone() is not None


def _ensure_roles(
    cur: psycopg.Cursor,
    writer_role: str,
    reader_role: str,
    reader_password: str | None,
    is_super: bool,
    createrole: bool,
) -> None:
    for role in (writer_role, reader_role):
        if _role_exists(cur, role):
            continue
        if not (is_super or createrole):
            raise InitError(
                f"role {role!r} does not exist and the current role cannot create it;"
                " have an admin run sql/roles.sql first"
            )
        # LOGIN without a password: an admin sets it (sql/roles.sql or ALTER ROLE).
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        log.info("created role %s", role)
    if reader_password:
        try:
            cur.execute("SAVEPOINT cpgsim_pw")
            cur.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(reader_role), sql.Literal(reader_password)
                )
            )
            cur.execute("RELEASE SAVEPOINT cpgsim_pw")
        except psycopg.errors.InsufficientPrivilege:
            cur.execute("ROLLBACK TO SAVEPOINT cpgsim_pw")
            log.warning("not allowed to set the password of %s; leaving it unchanged", reader_role)


def _become_writer(cur: psycopg.Cursor, me: str, writer_role: str, is_super: bool) -> None:
    """Make objects created from here on belong to the writer role."""
    cur.execute("SELECT current_database()")
    db = cur.fetchone()[0]  # type: ignore[index]
    if is_super:
        cur.execute(
            sql.SQL("GRANT CREATE, CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(db), sql.Identifier(writer_role)
            )
        )
        # Schemas created earlier by someone else move to the writer.
        cur.execute(
            "SELECT nspname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner"
            " WHERE nspname = ANY(%s) AND rolname <> %s",
            (list(SCHEMA_FILES), writer_role),
        )
        for (nsp,) in cur.fetchall():
            cur.execute(
                sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                    sql.Identifier(nsp), sql.Identifier(writer_role)
                )
            )
        # ... and so do tables and views (e.g. schema files applied by hand as
        # an admin). Indexes and owned sequences follow their table.
        cur.execute(
            "SELECT n.nspname, c.relname, c.relkind FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
            " AND pg_get_userbyid(c.relowner) <> %s",
            (list(SCHEMA_FILES), writer_role),
        )
        kinds = {"r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW", "f": "FOREIGN TABLE"}
        for nsp, rel, kind in cur.fetchall():
            cur.execute(
                sql.SQL("ALTER {} {} OWNER TO {}").format(
                    sql.SQL(kinds[kind]), sql.Identifier(nsp, rel), sql.Identifier(writer_role)
                )
            )
    if me != writer_role:
        cur.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (writer_role,))
        if not cur.fetchone()[0]:  # type: ignore[index]
            raise InitError(f"connect as {writer_role} (or a superuser) to run init")
        cur.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(writer_role)))
    # Creating schemas needs CREATE on the database, unless they all exist already.
    cur.execute(
        "SELECT has_database_privilege(current_database(), 'CREATE'),"
        " (SELECT count(*) FROM pg_namespace WHERE nspname = ANY(%s))",
        (list(SCHEMA_FILES),),
    )
    can_create, existing = cur.fetchone()  # type: ignore[misc]
    if not can_create and existing < len(SCHEMA_FILES):
        raise InitError(
            f"{writer_role} lacks CREATE on database {db};"
            f" run as admin: GRANT CREATE ON DATABASE {db} TO {writer_role}"
        )


def _grant(cur: psycopg.Cursor, writer_role: str, reader_role: str) -> None:
    w, r = sql.Identifier(writer_role), sql.Identifier(reader_role)
    for name in INTERNAL_SCHEMAS:
        s = sql.Identifier(name)
        for target in (sql.SQL("PUBLIC"), r):
            cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(s, target))
            cur.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(s, target))
            cur.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(s, target))
    for name in BUSINESS_SCHEMAS:
        s = sql.Identifier(name)
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(s, r))
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
            ).format(w, s, r)
        )


def init_db(
    conn: psycopg.Connection,
    reader_password: str | None = None,
    *,
    writer_role: str = WRITER_ROLE,
    reader_role: str = READER_ROLE,
    schemas: Sequence[str] | None = None,
    sql_dir: Path | None = None,
) -> None:
    """Create roles (if allowed), schemas, tables, indexes and grants. Idempotent.

    ``schemas`` limits which schema files are applied (default: all of
    ``SCHEMA_FILES``, each of which must exist). All eight schemas are created
    and granted regardless, so a missing file never leaves a grant out.
    Runs in one transaction and commits.
    """
    names = list(SCHEMA_FILES if schemas is None else schemas)
    sql_dir = sql_dir or find_sql_dir()
    files = [schema_file(n, sql_dir) for n in names]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise InitError(f"missing schema files: {', '.join(missing)}")
    try:
        with conn.cursor() as cur:
            me, is_super, createrole = _role_info(cur)
            _ensure_roles(cur, writer_role, reader_role, reader_password, is_super, createrole)
            _become_writer(cur, me, writer_role, is_super)
            for name in SCHEMA_FILES:
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)))
            for f in files:
                log.info("applying %s", f.name)
                cur.execute(f.read_text())  # type: ignore[call-overload]  # multi-statement script
            _grant(cur, writer_role, reader_role)
            cur.execute("RESET ROLE")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# -- reset ---------------------------------------------------------------------


def data_tables(conn: psycopg.Connection) -> list[str]:
    """Schema-qualified tables that ``reset_data`` truncates."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s)"
            " ORDER BY schemaname, tablename",
            (list(DATA_SCHEMAS),),
        )
        rows = cur.fetchall()
    return [f"{s}.{t}" for s, t in rows if f"{s}.{t}" not in KEEP_ON_RESET]


def writer_lock_holders(conn: psycopg.Connection) -> list[int]:
    """Backend pids holding :data:`WRITER_LOCK` (other than this session)."""
    key = WRITER_LOCK & 0xFFFF_FFFF_FFFF_FFFF
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND classid = %s AND objid = %s AND objsubid = 1 AND pid <> pg_backend_pid()"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
            " ORDER BY pid",
            ((key >> 32) & 0xFFFF_FFFF, key & 0xFFFF_FFFF),
        )
        return [r[0] for r in cur.fetchall()]


def acquire_writer_lock(conn: psycopg.Connection, wait: float = 0.0, poll: float = 1.0) -> None:
    """Take the session-level writer lock, waiting up to ``wait`` seconds.

    Session-level advisory locks survive commits and rollbacks and are released
    when the session ends (process exit, crash, lost connection). Raises
    :class:`WriterLockHeld` when another session keeps it.
    """
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (WRITER_LOCK,))
            ok = bool(cur.fetchone()[0])  # type: ignore[index]
        conn.commit()
        if ok:
            return
        if time.monotonic() >= deadline:
            holders = writer_lock_holders(conn)
            conn.commit()
            who = f" (backend pid {', '.join(map(str, holders))})" if holders else ""
            raise WriterLockHeld(
                f"another generator is running against this database{who};"
                " only one engine may write at a time (stop it first, e.g. `docker compose stop generator`)"
            )
        time.sleep(min(poll, max(0.05, deadline - time.monotonic())))


def release_writer_lock(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (WRITER_LOCK,))
    conn.commit()


def _reset_lock(conn: psycopg.Connection, force: bool, wait: float) -> None:
    """Take WRITER_LOCK for the reset transaction, or refuse (see :func:`reset_data`)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (WRITER_LOCK,))
        if cur.fetchone()[0]:  # type: ignore[index]
            return
    holders = writer_lock_holders(conn)
    if not force:
        who = f" (backend pid {', '.join(map(str, holders))})" if holders else ""
        raise WriterLockHeld(
            f"a generator is running against this database{who}; stop it first"
            " (`docker compose stop generator`, then `docker compose run --rm generator reset --yes`)"
            " or pass --force to terminate its connection"
        )
    with conn.cursor() as cur:
        for pid in holders:
            try:
                cur.execute("SAVEPOINT cpgsim_term")
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
                cur.execute("RELEASE SAVEPOINT cpgsim_term")
                log.warning("terminated the generator session (backend pid %s)", pid)
            except psycopg.errors.InsufficientPrivilege:
                cur.execute("ROLLBACK TO SAVEPOINT cpgsim_term")
                log.warning("not allowed to terminate backend pid %s", pid)
        deadline = time.monotonic() + wait
        while True:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (WRITER_LOCK,))
            if cur.fetchone()[0]:  # type: ignore[index]
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    # Still held: go ahead. The engine fences every commit on sim.clock, so a
    # surviving writer fails at its next commit instead of writing into the
    # emptied database.
    log.warning("writer lock still held; resetting anyway (--force)")


def reset_data(
    conn: psycopg.Connection,
    seed: int | None = None,
    clear_seed: bool = False,
    force: bool = False,
    force_wait: float = 10.0,
) -> list[str]:
    """Empty every simulated table (business, sim, truth), keeping the structure.

    The clock, queue, entity state, run log and scenario overrides go too, so the
    next backfill starts a fresh simulation. ``sim.settings`` is kept; ``seed``
    stores a new seed there (``clear_seed`` removes it, so the config seed
    applies again). Returns the truncated tables. Commits.

    Refuses with :class:`WriterLockHeld` while a generator holds
    :data:`WRITER_LOCK` (a reset under a running engine would leave a hybrid of
    two timelines). ``force`` terminates the generator's session first and
    resets even if the lock cannot be taken.
    """
    try:
        _reset_lock(conn, force, force_wait)
        tables = data_tables(conn)
        with conn.cursor() as cur:
            if tables:
                idents = [sql.Identifier(*t.split(".", 1)) for t in tables]
                cur.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY").format(sql.SQL(", ").join(idents))
                )
            if seed is not None:
                set_setting(cur, SEED_KEY, str(int(seed)))
            elif clear_seed:
                set_setting(cur, SEED_KEY, None)
        conn.commit()
        return tables
    except BaseException:
        conn.rollback()
        raise


# -- maintenance ---------------------------------------------------------------

#: Simulator state rewritten on every commit (entity upserts and deletes, queue
#: inserts and deletes). A backfill turns these over far faster than autovacuum
#: (one-minute naptime, cost-limited) reclaims the dead rows, so the engine
#: vacuums them itself (``Simulation`` and ``after_backfill``).
STATE_TABLES: tuple[str, ...] = ("sim.entity_state", "sim.event_queue")


def _outside_transaction(conn: psycopg.Connection, statement: sql.Composable) -> None:
    """Run ``statement`` in autocommit mode (VACUUM cannot run in a transaction).

    The connection must be idle (no open transaction); its autocommit setting
    is restored afterwards.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("maintenance needs an idle connection (commit or roll back first)")
    prev = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute(statement)
    finally:
        conn.autocommit = prev


def _idents(tables: Sequence[str]) -> sql.Composable:
    return sql.SQL(", ").join(sql.Identifier(*t.split(".", 1)) for t in tables)


def vacuum(
    conn: psycopg.Connection,
    tables: Sequence[str] = STATE_TABLES,
    analyze: bool = False,
    skip_locked: bool = False,
) -> None:
    """``VACUUM`` (with ``ANALYZE`` if asked) the given schema-qualified tables.

    ``skip_locked`` skips a table autovacuum is working on right now (Postgres
    logs a WARNING) instead of waiting until autovacuum is cancelled.
    """
    if not tables:
        return
    opts = [o for o, on in (("ANALYZE", analyze), ("SKIP_LOCKED", skip_locked)) if on]
    verb = sql.SQL(f"VACUUM ({', '.join(opts)}) " if opts else "VACUUM ")
    _outside_transaction(conn, verb + _idents(tables))


def owned_tables(conn: psycopg.Connection, schemas: Sequence[str]) -> list[str]:
    """Tables in ``schemas`` the current role may vacuum/analyze (owner or superuser)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s)"
            " AND pg_has_role(tableowner, 'USAGE') ORDER BY schemaname, tablename",
            (list(schemas),),
        )
        rows = cur.fetchall()
    conn.commit()
    return [f"{s}.{t}" for s, t in rows]


def analyze(conn: psycopg.Connection, schemas: Sequence[str] = BUSINESS_SCHEMAS + ("truth",)) -> int:
    """``ANALYZE`` every table of ``schemas`` (fresh planner statistics). Returns the table count."""
    tables = owned_tables(conn, schemas)
    if tables:
        _outside_transaction(conn, sql.SQL("ANALYZE ") + _idents(tables))
    return len(tables)

"""CLI, configuration and database setup (cpgsim.cli, cpgsim.db).

Unit tests run everywhere. Tests marked ``db`` need ``CPGSIM_TEST_DSN`` pointing
at a database on a server where that user may create databases and roles
(e.g. the ``postgres`` superuser of a dev container). Each test gets its own
scratch database and suffixed role names, both dropped afterwards; nothing
else on the server is touched.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from cpgsim import cli, db
from cpgsim.config import DEFAULT_CONFIG, ScenarioCfg, is_reserved_domain, load_config

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


# -- argument parsing ------------------------------------------------------------


def parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


def test_globals_before_and_after_command():
    a = parse("--dsn", "A", "--config", "c.yaml", "status")
    assert (a.dsn, a.config, a.overlay, a.verbose) == ("A", "c.yaml", None, 0)
    b = parse("status", "--dsn", "B", "-v", "--overlay", "o.yaml")
    assert (b.dsn, b.config, b.overlay, b.verbose) == ("B", None, "o.yaml", 1)
    c = parse("--dsn", "A", "scenario", "list", "--dsn", "C")
    assert c.dsn == "C"
    assert c.func is cli.cmd_scenario_list


def test_command_arguments():
    a = parse("reset", "--seed", "7", "--yes")
    assert (a.seed, a.yes, a.clear_seed, a.func) == (7, True, False, cli.cmd_reset)
    b = parse("backfill", "--months", "3", "--start", "2025-01-01", "--until", "2025-02-01T12:00")
    assert (b.months, b.start, b.until) == (3, "2025-01-01", "2025-02-01T12:00")
    assert parse("run", "--max-ticks", "2").max_ticks == 2
    e = parse("scenario", "enable", "carrier_decay", "--param", "region=DE-BW", "--param", "x=2")
    assert (e.scenario_cmd, e.id, e.param) == ("enable", "carrier_decay", ["region=DE-BW", "x=2"])
    assert parse("scenario", "disable", "carrier_decay").func is cli.cmd_scenario_switch
    assert parse("truth", "carrier_decay").scenario_id == "carrier_decay"
    assert parse("start").db_attempts == 60
    with pytest.raises(SystemExit):
        parse()  # a command is required
    with pytest.raises(SystemExit):
        parse("scenario")  # an action is required


# -- dsn, paths, config ----------------------------------------------------------


def test_resolve_dsn_precedence():
    cfg = load_config(DEFAULT_CONFIG, database={"dsn": "from-cfg"})
    assert db.resolve_dsn("from-cli", cfg, env={"CPGSIM_DSN": "from-env"}) == "from-cli"
    assert db.resolve_dsn(None, cfg, env={"CPGSIM_DSN": "from-env"}) == "from-env"
    assert db.resolve_dsn(None, cfg, env={"CPGSIM_DSN": ""}) == "from-cfg"
    with pytest.raises(db.DsnError):
        db.resolve_dsn(None, load_config(DEFAULT_CONFIG), env={})


def test_find_sql_dir(tmp_path):
    assert db.find_sql_dir(env={}) == REPO / "sql"
    (tmp_path / "schemas").mkdir()
    assert db.find_sql_dir(env={"CPGSIM_SQL_DIR": str(tmp_path)}) == tmp_path
    with pytest.raises(db.InitError):
        db.find_sql_dir(env={"CPGSIM_SQL_DIR": str(tmp_path / "nope")})


def test_init_db_refuses_missing_schema_files(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "sim.sql").write_text("SELECT 1;")
    with pytest.raises(db.InitError, match="missing schema files"):
        db.init_db(None, sql_dir=tmp_path)  # type: ignore[arg-type]  # fails before using conn


def test_config_paths_precedence(tmp_path):
    ns = argparse.Namespace(config=None, overlay=None)
    assert cli.config_paths(ns, env={}) == (DEFAULT_CONFIG, None)
    env = {"CPGSIM_CONFIG": "/e/c.yaml", "CPGSIM_OVERLAY": "/e/o.yaml"}
    assert cli.config_paths(ns, env=env) == (Path("/e/c.yaml"), Path("/e/o.yaml"))
    ns2 = argparse.Namespace(config="/a/c.yaml", overlay="/a/o.yaml")
    assert cli.config_paths(ns2, env=env) == (Path("/a/c.yaml"), Path("/a/o.yaml"))
    assert cli.config_paths(ns, env={"CPGSIM_OVERLAY": ""})[1] is None
    assert cli.scenario_dir(ns, env={}) == REPO / "config" / "scenarios"


def test_load_cfg_with_overlay_and_seed_override(tmp_path, monkeypatch):
    monkeypatch.delenv("CPGSIM_CONFIG", raising=False)
    ov = tmp_path / "private.yaml"
    ov.write_text(
        "seed: 99\nchannels:\n  web:\n    base_orders_per_day: 120\n"
        "scenarios_append:\n  - id: carrier_decay\n    carrier: Nordpost\n    region: DE-HH\n"
    )
    cfg = cli.load_cfg(argparse.Namespace(config=None, overlay=str(ov)), env={})
    assert cfg.seed == 99
    assert cfg.channels.web.base_orders_per_day == 120
    assert cfg.channels.web.free_shipping_eur == 29.0  # merged, not replaced
    assert [s.id for s in cfg.scenarios] == ["carrier_decay", "promo_margin_trap", "influencer_cohort_churn",
                                             "retailer_overstock", "hero_oos_substitution", "carrier_decay"]
    assert cli.apply_seed_override(cfg, None) is cfg
    assert cli.apply_seed_override(cfg, 99) is cfg
    over = cli.apply_seed_override(cfg, 4242)
    assert over.seed == 4242 and cfg.seed == 99
    assert over.fingerprint() != cfg.fingerprint()
    with pytest.raises(cli.CliError):
        cli.load_cfg(argparse.Namespace(config=str(tmp_path / "missing.yaml"), overlay=None), env={})
    with pytest.raises(cli.CliError):
        cli.load_cfg(argparse.Namespace(config=None, overlay=str(tmp_path / "missing.yaml")), env={})


def test_empty_overlay_placeholder_is_a_no_op():
    base = load_config(DEFAULT_CONFIG)
    same = load_config(DEFAULT_CONFIG, REPO / "docker" / "empty-overlay.yaml")
    assert same.fingerprint() == base.fingerprint()


# -- shipped config files ----------------------------------------------------------


def test_default_config_values():
    cfg = load_config(DEFAULT_CONFIG)
    assert (cfg.seed, cfg.history_months, cfg.tick_seconds) == (20261114, 24, 30)
    assert cfg.company.name == "Northvale Consumer Care GmbH"
    assert cfg.erp["naming"] == "sap"
    assert set(cfg.channels.retailers()) == {"drogerie_kranz", "mylo", "shopnova", "belle"}
    assert cfg.channels.shopnova.enabled and cfg.channels.belle.enabled
    sc = {s.id: s for s in cfg.scenarios}
    cd = sc["carrier_decay"]
    assert (cd.start, cd.end) == (date(2026, 8, 3), None)
    assert cd.params() == {
        "carrier": "Swiftbox", "region": "DE-BY", "delay_multiplier": 2.5, "exception_multiplier": 3.0,
    }
    pm = sc["promo_margin_trap"]
    assert (pm.start, pm.end) == (date(2026, 3, 2), date(2026, 3, 29))
    assert pm.params() == {
        "brand": "Velmora", "discount_pct": 0.25, "free_shipping": True,
        "deal_seeker_share": 1.0, "deal_seeker_return_rate": 0.15, "cherry_pick_share": 0.8,
    }
    assert cfg.chaos.duplicate_customer_rate == 0.02
    assert cfg.chaos.sellout_restatement_rate == 0.10
    assert cfg.chaos.dst_truth is True
    # The emails used by default are on reserved/example domains only.
    for domains in cfg.customers.email_domains.values():
        assert all(is_reserved_domain(d) for d in domains)


def test_generic_scenario_definitions():
    defs = cli.scenario_definitions(REPO / "config" / "scenarios")
    assert {"carrier_decay", "promo_margin_trap"} <= set(defs)
    for d in defs.values():
        raw = yaml.safe_load(d.path.read_text())
        assert "start" not in raw and "end" not in raw and "start" not in d.params
        assert d.description
    assert defs["carrier_decay"].params["delay_multiplier"] == 2.5
    assert defs["promo_margin_trap"].params["free_shipping"] is True


# -- scenario helpers --------------------------------------------------------------


def test_parse_params():
    p = cli.parse_params(["a=2.5", "b=true", "c=DE-BY", "d=3", "e=", "f=2026-08-03", "g=x=y", "h=[1,2]"])
    assert p == {"a": 2.5, "b": True, "c": "DE-BY", "d": 3, "e": "", "f": "2026-08-03", "g": "x=y", "h": "[1,2]"}
    assert cli.parse_params(None) == {}
    for bad in ("novalue", "=3"):
        with pytest.raises(cli.CliError):
            cli.parse_params([bad])


def test_scenario_params_merge():
    cfg = load_config(DEFAULT_CONFIG)
    ns = argparse.Namespace(config=None, overlay=None, param=["region=DE-BW", "delay_multiplier=4"])
    p = cli._scenario_params(ns, cfg, "carrier_decay")
    assert p == {"carrier": "Swiftbox", "region": "DE-BW", "delay_multiplier": 4, "exception_multiplier": 3.0}
    with pytest.raises(cli.CliError, match="unknown scenario"):
        cli._scenario_params(argparse.Namespace(config=None, overlay=None, param=None), cfg, "nope")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=BERLIN).astimezone(UTC)


def test_scenario_windows_from_config_and_overrides():
    scen = [
        ScenarioCfg(id="promo_margin_trap", start=date(2026, 3, 2), end=date(2026, 3, 29), brand="Velmora"),
        ScenarioCfg(id="carrier_decay", start=date(2026, 8, 3), carrier="Swiftbox"),
        ScenarioCfg(id="off", start=date(2026, 1, 1), enabled=False),
    ]
    ws = cli.scenario_windows(scen, [], BERLIN)
    assert [w.scenario_id for w in ws] == ["promo_margin_trap", "carrier_decay"]
    promo = ws[0]
    assert promo.contains(_t("2026-03-02T00:00")) and promo.contains(_t("2026-03-29T23:59"))
    assert not promo.contains(_t("2026-03-01T23:59")) and not promo.contains(_t("2026-03-30T00:00"))
    assert promo.params == {"brand": "Velmora"}
    act = lambda ws, t: sorted(w.scenario_id for w in cli.active_scenarios(ws, _t(t)))  # noqa: E731
    assert act(ws, "2026-09-01T12:00") == ["carrier_decay"]

    overrides = [
        (1, "promo_margin_trap", "enable", _t("2026-10-01T09:00"), {"discount_pct": 0.3}),
        (2, "carrier_decay", "disable", _t("2026-10-05T00:00"), {}),
        (3, "promo_margin_trap", "disable", _t("2026-10-10T00:00"), {}),
        (4, "carrier_decay", "enable", _t("2026-10-20T00:00"), {"region": "DE-BW"}),
    ]
    ws = cli.scenario_windows(scen, overrides, BERLIN)
    assert act(ws, "2026-10-01T08:59") == ["carrier_decay"]
    assert act(ws, "2026-10-01T09:00") == ["carrier_decay", "promo_margin_trap"]
    assert act(ws, "2026-10-06T00:00") == ["promo_margin_trap"]
    assert act(ws, "2026-10-15T00:00") == []
    assert act(ws, "2026-11-01T00:00") == ["carrier_decay"]
    cli_win = [w for w in ws if w.source == "cli" and w.scenario_id == "carrier_decay"][0]
    assert cli_win.params == {"region": "DE-BW"} and cli_win.end is None
    # a disable before a config window does not cancel that window
    ws = cli.scenario_windows(scen, [(1, "carrier_decay", "disable", _t("2026-07-01T00:00"), {})], BERLIN)
    assert act(ws, "2026-09-01T00:00") == ["carrier_decay"]


def test_scenario_disable_closes_latest_open_window_only():
    """Same rule as ScenarioManager: disable closes the most recently started open window."""
    scen = [ScenarioCfg(id="carrier_decay", start=date(2026, 8, 3), carrier="Swiftbox")]
    overrides = [
        (1, "carrier_decay", "enable", _t("2026-09-01T00:00"), {"region": "DE-BW"}),
        (2, "carrier_decay", "disable", _t("2026-09-10T00:00"), {}),
    ]
    ws = cli.scenario_windows(scen, overrides, BERLIN)
    cfg_win = next(w for w in ws if w.source == "config")
    cli_win = next(w for w in ws if w.source == "cli")
    assert cli_win.end == _t("2026-09-10T00:00") and cfg_win.end is None
    assert len(cli.active_scenarios(ws, _t("2026-09-15T00:00"))) == 1


# -- progress ----------------------------------------------------------------------


class _FakeSim:
    class world:  # noqa: N801
        mode = "backfill"


def test_progress_throttles_and_counts_across_passes():
    now = [0.0]
    out = io.StringIO()
    sim = _FakeSim()
    p = cli.Progress(sim, BERLIN, out=out, every=5.0, clock=lambda: now[0])
    t0 = _t("2025-01-01T00:00")
    for i in range(1, 11):  # one commit per wall second
        now[0] = float(i)
        p(t0 + timedelta(days=i), i * 100)
    lines = out.getvalue().splitlines()
    assert len(lines) == 2  # t=1 and t=6
    assert "events 100 " in lines[0] and "2025-01-02" in lines[0]
    assert "events 600 " in lines[1] and "100 ev/s" in lines[1]
    now[0] = 20.0
    p(t0 + timedelta(days=20), 50)  # a new pass starts counting from zero again
    assert p.events == 1050
    p.done()
    assert "1,050 events" in out.getvalue().splitlines()[-1]
    sim.world.mode = "live"
    try:
        out.truncate(0), out.seek(0)
        lp = cli.Progress(sim, BERLIN, out=out, live_every=60.0, clock=lambda: now[0])
        lp(t0, 1)
        now[0] += 30
        lp(t0, 2)
        assert len(out.getvalue().splitlines()) == 1 and "ticks 1" in out.getvalue()
    finally:
        sim.world.mode = "backfill"


def test_main_without_dsn_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.delenv("CPGSIM_DSN", raising=False)
    monkeypatch.delenv("CPGSIM_CONFIG", raising=False)
    monkeypatch.delenv("CPGSIM_OVERLAY", raising=False)
    assert cli.main(["status"]) == 1
    assert "no database configured" in capsys.readouterr().err


def test_scenario_list_without_db(monkeypatch, capsys):
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY"):
        monkeypatch.delenv(k, raising=False)
    assert cli.main(["scenario", "list"]) == 0
    out = capsys.readouterr().out
    assert "carrier_decay" in out and "promo_margin_trap" in out and "unknown" in out


# -- database ----------------------------------------------------------------------

TEST_DSN = os.environ.get("CPGSIM_TEST_DSN")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="CPGSIM_TEST_DSN not set")


def _existing_schemas() -> list[str]:
    sql_dir = db.find_sql_dir()
    return [n for n in db.SCHEMA_FILES if db.schema_file(n, sql_dir).is_file()]


class Scratch:
    def __init__(self, dsn: str, writer: str, reader: str) -> None:
        self.dsn, self.writer, self.reader = dsn, writer, reader

    def init(self, conn) -> None:
        db.init_db(conn, writer_role=self.writer, reader_role=self.reader, schemas=_existing_schemas())


@pytest.fixture
def scratch():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    suffix = uuid.uuid4().hex[:8]
    dbname = f"cpgsim_clitest_{suffix}"
    writer, reader = f"sim_writer_t{suffix}", f"bi_reader_t{suffix}"
    admin = psycopg.connect(TEST_DSN, autocommit=True)
    ok = admin.execute(
        "SELECT rolsuper OR (rolcreatedb AND rolcreaterole) FROM pg_roles WHERE rolname = current_user"
    ).fetchone()[0]
    if not ok:
        admin.close()
        pytest.skip("CPGSIM_TEST_DSN user cannot create databases and roles")
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    params = conninfo_to_dict(TEST_DSN)
    params["dbname"] = dbname
    try:
        yield Scratch(make_conninfo(**params), writer, reader)
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        for role in (writer, reader):  # only the suffixed roles this test created
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


def _owner_rows(conn, writer):
    schemas = conn.execute(
        "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = ANY(%s)",
        (list(db.SCHEMA_FILES),),
    ).fetchall()
    tables = conn.execute(
        "SELECT schemaname || '.' || tablename, tableowner FROM pg_tables WHERE schemaname = ANY(%s)",
        (list(db.SCHEMA_FILES),),
    ).fetchall()
    return schemas, tables


@needs_db
@pytest.mark.db
def test_init_db_idempotent_and_owned_by_writer(scratch):
    with db.connect(scratch.dsn) as conn:
        # objects an admin created by hand beforehand are handed to the writer
        conn.execute("CREATE SCHEMA shop")
        conn.execute("CREATE TABLE shop.cli_preexisting (id serial PRIMARY KEY)")
        conn.commit()
        scratch.init(conn)
        schemas1, tables1 = _owner_rows(conn, scratch.writer)
        conn.commit()
        scratch.init(conn)  # second run: no error, nothing changes
        schemas2, tables2 = _owner_rows(conn, scratch.writer)
        conn.commit()
    assert sorted(schemas1) == sorted(schemas2)
    assert sorted(tables1) == sorted(tables2)
    assert {s for s, _ in schemas1} == set(db.SCHEMA_FILES)
    assert all(owner == scratch.writer for _, owner in schemas1)
    assert all(owner == scratch.writer for _, owner in tables1)
    names = {t for t, _ in tables1}
    assert {"sim.clock", "sim.event_queue", "sim.settings", "sim.scenario_overrides"} <= names


@needs_db
@pytest.mark.db
def test_init_db_as_writer_role(scratch):
    """The non-superuser path (docker / manual setup): roles exist, writer runs init."""
    import psycopg
    from psycopg import sql

    with db.connect(scratch.dsn) as conn:
        scratch.init(conn)  # as admin: creates roles + grants CREATE to the writer
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(scratch.writer)))
        assert conn.execute("SELECT current_user").fetchone()[0] == scratch.writer
        scratch.init(conn)  # as the writer itself
        # the writer cannot create roles
        conn.execute("RESET ROLE")
        conn.commit()
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(scratch.writer)))
        with pytest.raises(db.InitError, match="does not exist"):
            db.init_db(conn, writer_role=scratch.writer, reader_role=f"{scratch.reader}_missing",
                       schemas=_existing_schemas())
        # init_db rolled back, which also undid the transaction-scoped SET ROLE
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(scratch.writer)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE ROLE cpgsim_should_not_exist")
        conn.rollback()


@needs_db
@pytest.mark.db
def test_reader_grants(scratch):
    import psycopg
    from psycopg import sql

    w, r = sql.Identifier(scratch.writer), sql.Identifier(scratch.reader)
    with db.connect(scratch.dsn) as conn:
        scratch.init(conn)
        # a table created after init by the writer is readable thanks to default privileges
        conn.execute(sql.SQL("SET ROLE {}").format(w))
        conn.execute("CREATE TABLE shop.cli_probe (id int PRIMARY KEY)")
        conn.execute("INSERT INTO shop.cli_probe VALUES (1)")
        conn.execute("INSERT INTO sim.settings VALUES ('probe', 'x')")
        conn.execute("RESET ROLE")
        conn.commit()

        priv = conn.execute(
            "SELECT s, has_schema_privilege(%s, s, 'USAGE') FROM unnest(%s::text[]) s",
            (scratch.reader, list(db.SCHEMA_FILES)),
        ).fetchall()
        assert {s for s, ok in priv if ok} == set(db.BUSINESS_SCHEMAS)

        conn.execute(sql.SQL("SET ROLE {}").format(r))
        assert conn.execute("SELECT id FROM shop.cli_probe").fetchall() == [(1,)]
        conn.commit()
        for stmt in (
            "SELECT * FROM sim.clock",
            "SELECT * FROM sim.settings",
            "SELECT * FROM truth.scenario_windows",
            "INSERT INTO shop.cli_probe VALUES (2)",
            "CREATE TABLE shop.nope (id int)",
        ):
            conn.execute(sql.SQL("SET ROLE {}").format(r))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
            conn.rollback()
        # every existing table in the business schemas is readable
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s)",
            (list(db.BUSINESS_SCHEMAS),),
        ).fetchall()
        for s, t in tables:
            assert conn.execute(
                "SELECT has_table_privilege(%s, format('%%I.%%I', %s::text, %s::text), 'SELECT')",
                (scratch.reader, s, t),
            ).fetchone()[0], f"{s}.{t}"


@needs_db
@pytest.mark.db
def test_reset_data_and_seed_setting(scratch):
    with db.connect(scratch.dsn) as conn:
        scratch.init(conn)
        conn.execute(
            "INSERT INTO sim.clock VALUES (1, now(), now(), 1, 'fp', now(), now())"
        )
        conn.execute("INSERT INTO sim.event_queue (key, due, priority, kind) VALUES ('k', now(), 1, 'x')")
        conn.execute(
            "INSERT INTO sim.scenario_overrides (scenario_id, action, at) VALUES ('carrier_decay', 'enable', now())"
        )
        conn.commit()
        assert db.persisted_seed(conn) is None
        tables = db.reset_data(conn, seed=42)
        assert "sim.clock" in tables and "sim.settings" not in tables
        for t in ("sim.clock", "sim.event_queue", "sim.scenario_overrides"):
            assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
        assert db.persisted_seed(conn) == 42
        db.reset_data(conn)  # keeps the stored seed
        assert db.persisted_seed(conn) == 42
        # serial restarts after reset
        oid = conn.execute(
            "INSERT INTO sim.scenario_overrides (scenario_id, action, at) VALUES ('x', 'enable', now()) RETURNING id"
        ).fetchone()[0]
        conn.commit()
        assert oid == 1
        db.reset_data(conn, clear_seed=True)
        assert db.persisted_seed(conn) is None


@needs_db
@pytest.mark.db
def test_persisted_seed_before_init(scratch):
    with db.connect(scratch.dsn) as conn:
        assert db.persisted_seed(conn) is None  # no sim schema yet: no error


@needs_db
@pytest.mark.db
def test_cli_scenarios_status_truth(scratch, monkeypatch, capsys):
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY"):
        monkeypatch.delenv(k, raising=False)
    with db.connect(scratch.dsn) as conn:
        scratch.init(conn)
    base = ["--dsn", scratch.dsn]

    # enable before any simulation exists: at = wall now
    before = datetime.now(UTC)
    assert cli.main(base + ["scenario", "enable", "promo_margin_trap", "--param", "discount_pct=0.4"]) == 0
    assert cli.main(base + ["scenario", "enable", "nope"]) == 1
    with db.connect(scratch.dsn) as conn:
        rows = conn.execute("SELECT scenario_id, action, at, params FROM sim.scenario_overrides").fetchall()
        assert len(rows) == 1
        sid, action, at, params = rows[0]
        assert (sid, action) == ("promo_margin_trap", "enable")
        assert at >= before - timedelta(seconds=1)
        assert params == {
            "brand": "Velmora", "discount_pct": 0.4, "free_shipping": True,
            "deal_seeker_share": 1.0, "deal_seeker_return_rate": 0.15, "cherry_pick_share": 0.8,
        }
        # a simulation clock ahead of the switch; a seed override
        clock = datetime.now(UTC) + timedelta(hours=1)
        conn.execute(
            "INSERT INTO sim.clock VALUES (1, %s, %s, 20261114, 'fp', now(), now())",
            (clock, clock - timedelta(days=700)),
        )
        conn.execute(
            "INSERT INTO sim.run_log (wall_time, mode, sim_from, sim_to, events, rows, queue_depth, duration_ms)"
            " VALUES (now() - interval '10 minutes', 'live', %s, %s, 5, %s, 3, 12),"
            " (now() - interval '5 minutes', 'live', %s, %s, 7, %s, 4, 15),"
            " (now() - interval '3 hours', 'live', %s, %s, 9, %s, 4, 15)",
            (clock, clock, json.dumps({"shop": 10, "erp": 4}),
             clock, clock, json.dumps({"shop": 5, "wms": 2}),
             clock, clock, json.dumps({"shop": 1000})),
        )
        with conn.cursor() as cur:
            db.set_setting(cur, db.SEED_KEY, "777")
        conn.commit()
    capsys.readouterr()

    assert cli.main(base + ["scenario", "list"]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("promo_margin_trap"))
    assert "ACTIVE" in line and "cli" in line and "0.4" in line
    cd = next(l for l in out.splitlines() if l.startswith("carrier_decay"))
    assert "ACTIVE" in cd  # config window since 2026-08-03, open-ended

    assert cli.main(base + ["status"]) == 0
    out = capsys.readouterr().out
    assert "queue depth     0" in out
    assert "shop 15" in out and "erp 4" in out and "wms 2" in out and "total 21" in out
    assert "seed            20261114" in out and "DIFFERS" in out  # 'fp' is not a real fingerprint
    assert "promo_margin_trap" in out and "carrier_decay" in out
    assert "last run" in out

    # disable: at = max(database clock, sim clock + 1 us) = clock + 1 us: strictly after
    # everything the engine has committed, so its window end can never be skipped
    assert cli.main(base + ["scenario", "disable", "promo_margin_trap"]) == 0
    with db.connect(scratch.dsn) as conn:
        at = conn.execute(
            "SELECT at FROM sim.scenario_overrides WHERE action = 'disable'"
        ).fetchone()[0]
        assert at == clock + timedelta(microseconds=1)
        # one microsecond later the scenario is off
        conn.execute("UPDATE sim.clock SET sim_time = %s", (at,))
        conn.commit()
    capsys.readouterr()
    cli.main(base + ["scenario", "list"])
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("promo_margin_trap"))
    assert "inactive" in line

    # truth
    assert cli.main(base + ["truth", "carrier_decay"]) == 1
    with db.connect(scratch.dsn) as conn:
        t = datetime(2026, 8, 3, tzinfo=UTC)
        conn.execute(
            "INSERT INTO truth.scenario_windows VALUES"
            " ('carrier_decay:config:0', 'carrier_decay', %s, %s, NULL, 'config', %s, %s)",
            (json.dumps({"carrier": "Swiftbox"}), t, t, t),
        )
        for i in range(15):
            conn.execute(
                "INSERT INTO truth.planted_effects (effect_id, scenario_id, window_id, entity_type, entity_id,"
                " system_refs, effect, magnitude, detail, effective_at, created_at, updated_at)"
                " VALUES (%s, 'carrier_decay', 'carrier_decay:config:0', %s, %s, %s, %s, %s, '{}', %s, %s, %s)",
                (f"e{i:02d}", "delivery", str(i), json.dumps({"wms.shipments": 500000 + i}),
                 "delay_multiplier" if i % 3 else "exception", 2.5 if i % 3 else None,
                 t + timedelta(hours=i), t, t),
            )
        conn.commit()
    assert cli.main(base + ["truth", "carrier_decay"]) == 0
    out = capsys.readouterr().out
    assert "carrier_decay:config:0" in out
    assert "delay_multiplier" in out and "exception" in out
    assert "500000" in out  # system_refs in the sample rows
    summary = next(l for l in out.splitlines() if l.startswith("delivery") and "delay_multiplier" in l)
    assert " 10 " in summary and "2.500" in summary
    assert sum(1 for l in out.splitlines() if "wms.shipments" in l) == 10


@needs_db
@pytest.mark.db
def test_cli_init_and_reset_commands(scratch, monkeypatch, capsys):
    """``sim init`` applies every schema file, so it needs them all."""
    missing = sorted(set(db.SCHEMA_FILES) - set(_existing_schemas()))
    if missing:
        pytest.skip(f"schema files not written yet: {missing}")
    # the CLI uses the default role names; only run where they already exist
    import psycopg

    with psycopg.connect(scratch.dsn) as conn:
        n = conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname IN ('sim_writer', 'bi_reader')"
        ).fetchone()[0]
    if n < 2:
        pytest.skip("roles sim_writer/bi_reader do not exist on this server (cluster-global; not created by tests)")
    monkeypatch.delenv("CPGSIM_READER_PASSWORD", raising=False)
    assert cli.main(["--dsn", scratch.dsn, "init"]) == 0
    assert cli.main(["--dsn", scratch.dsn, "reset", "--yes", "--seed", "5"]) == 0
    with db.connect(scratch.dsn) as conn:
        assert db.persisted_seed(conn) == 5


# -- command plumbing with a fake engine ----------------------------------------


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecSim:
    """Stands in for cpgsim.engine.runner.Simulation."""

    def __init__(self, cfg, conn, fresh=True):
        self.cfg, self.conn, self.fresh = cfg, conn, fresh
        self.calls: list[tuple] = []

        class W:
            mode = "backfill"

        self.world = W()

    def open(self, **kw):
        self.calls.append(("open", kw))
        return self.fresh

    def backfill(self, until=None, progress=None):
        self.calls.append(("backfill", until))
        progress(datetime(2025, 1, 1, tzinfo=UTC), 10)

    def after_backfill(self):
        self.calls.append(("after_backfill",))

    def run_live(self, max_ticks=None, progress=None):
        self.calls.append(("run_live", max_ticks))

    def stop(self, *_a):
        self.calls.append(("stop",))


@pytest.fixture
def fake_engine(monkeypatch):
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY", "CPGSIM_READER_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    made: list[_RecSim] = []
    state = {"clock": None, "seed": None, "init": 0}

    def new_sim(cfg, conn):
        s = _RecSim(cfg, conn)
        made.append(s)
        return s

    monkeypatch.setattr(cli, "_new_simulation", new_sim)
    monkeypatch.setattr(cli.db, "connect", lambda dsn, **kw: _Conn())
    monkeypatch.setattr(cli.db, "wait_for_db", lambda dsn, attempts=60: _Conn())
    monkeypatch.setattr(cli.db, "persisted_seed", lambda conn: state["seed"])
    monkeypatch.setattr(cli.db, "init_db", lambda conn, reader_password=None: state.__setitem__("init", state["init"] + 1))
    monkeypatch.setattr(cli, "_read_clock", lambda conn: state["clock"])
    return made, state


def test_backfill_command(fake_engine, capsys):
    made, state = fake_engine
    state["seed"] = 9
    rc = cli.main(["--dsn", "x", "backfill", "--months", "3", "--start", "2025-01-01", "--until", "2025-02-01T12:00"])
    assert rc == 0
    sim = made[0]
    assert sim.cfg.seed == 9  # sim.settings seed overrides the config
    assert sim.calls[0] == ("open", {"history_start": date(2025, 1, 1), "months": 3})
    assert sim.calls[1] == ("backfill", datetime(2025, 2, 1, 11, 0, tzinfo=UTC))  # CET -> UTC
    assert sim.calls[2] == ("after_backfill",)  # fresh statistics for BI
    out = capsys.readouterr().out
    assert "backfill  sim 2025-01-01" in out and "backfill done: 10 events" in out
    assert cli.main(["--dsn", "x", "backfill", "--start", "01.01.2025"]) == 1


def test_run_command(fake_engine):
    made, _ = fake_engine
    assert cli.main(["--dsn", "x", "run", "--max-ticks", "3"]) == 0
    assert [c[0] for c in made[0].calls] == ["open", "run_live"]
    assert made[0].calls[1] == ("run_live", 3)


def test_start_command_backfills_only_when_new(fake_engine):
    made, state = fake_engine
    assert cli.main(["--dsn", "x", "start"]) == 0
    assert state["init"] == 1
    lock = {"lock_wait": cli.START_LOCK_WAIT_SECONDS}  # waits for a previous container's session
    assert made[0].calls == [
        ("open", {"months": 24, **lock}), ("backfill", None), ("after_backfill",), ("run_live", None),
    ]
    state["clock"] = (datetime(2026, 1, 1, tzinfo=UTC),)
    assert cli.main(["--dsn", "x", "start"]) == 0
    assert state["init"] == 2
    assert made[1].calls == [("open", {"months": 24, **lock}), ("run_live", None)]


def test_reset_requires_confirmation_without_tty(monkeypatch, capsys):
    for k in ("CPGSIM_DSN", "CPGSIM_CONFIG", "CPGSIM_OVERLAY"):
        monkeypatch.delenv(k, raising=False)

    class Info:
        dbname, host = "d", "h"

    class C(_Conn):
        info = Info()

    called = []
    monkeypatch.setattr(cli.db, "connect", lambda dsn, **kw: C())
    monkeypatch.setattr(cli.db, "reset_data", lambda *a, **k: called.append(k) or [])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["--dsn", "x", "reset"]) == 1
    assert "pass --yes" in capsys.readouterr().err and not called
    assert cli.main(["--dsn", "x", "reset", "--yes", "--seed", "3"]) == 0
    assert called == [{"seed": 3, "clear_seed": False, "force": False}]


def test_scenario_windows_end_self_limited_windows():
    """`sim scenario list` shows the end the engine applies (start + duration_weeks)."""
    scen = [ScenarioCfg(id="hero_oos_substitution", start=date(2026, 4, 13))]
    (w,) = cli.scenario_windows(scen, [], BERLIN)
    assert w.end == datetime(2026, 5, 4, tzinfo=BERLIN).astimezone(timezone.utc)
    (w,) = cli.scenario_windows([], [(1, "retailer_overstock", "enable", _t("2026-01-05T10:00"), {})], BERLIN)
    # 13 weeks in local wall-clock time (one hour less in UTC across the spring DST change)
    assert w.end.astimezone(BERLIN).replace(tzinfo=None) - w.start.astimezone(BERLIN).replace(tzinfo=None) == timedelta(
        weeks=13)

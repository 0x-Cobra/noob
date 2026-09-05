"""SQLite persistence: ticks, legs, fills, equity snapshots, events, key/value state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .types import Fill, Leg, LegStatus, Side, Tick

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
  ts TEXT NOT NULL, symbol TEXT NOT NULL, mid REAL, sell_exec REAL, buy_exec REAL,
  ref REAL, ref_ts TEXT, market_open INTEGER, extended_open INTEGER, ref_stale INTEGER,
  sell_premium REAL, buy_discount REAL, liquidity TEXT);
CREATE INDEX IF NOT EXISTS ix_ticks_ts ON ticks(ts);
CREATE INDEX IF NOT EXISTS ix_ticks_sym_ts ON ticks(symbol, ts);

CREATE TABLE IF NOT EXISTS legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
  entry_px REAL NOT NULL, entry_ref REAL NOT NULL, entry_ts TEXT NOT NULL, force_close_at TEXT NOT NULL,
  status TEXT NOT NULL, exit_px REAL, exit_ts TEXT, exit_reason TEXT, fees_usd REAL NOT NULL DEFAULT 0,
  pnl_usd REAL, mode TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_legs_status ON legs(status);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  qty REAL NOT NULL, px REAL NOT NULL, fees_usd REAL NOT NULL, tx_hash TEXT, mode TEXT NOT NULL,
  leg_id INTEGER, reason TEXT);

CREATE TABLE IF NOT EXISTS equity (
  ts TEXT NOT NULL, mode TEXT NOT NULL, realized_cum REAL NOT NULL, unrealized REAL NOT NULL,
  strategy_equity REAL NOT NULL, quote_usd REAL NOT NULL, inventory_usd REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, msg TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat() if ts else None


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)   # autocommit; explicit txns below
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # --- kv -----------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE key=?", (key,))

    # --- ticks --------------------------------------------------------------------------
    def add_tick(self, t: Tick) -> None:
        p, r = t.pool, t.ref
        self.conn.execute(
            "INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(t.ts), t.symbol, p.mid if p else None, p.sell_exec if p else None, p.buy_exec if p else None,
             r.price if r else None, _iso(r.ts) if r else None, int(t.market_open), int(t.extended_open),
             int(t.ref_stale), t.sell_premium, t.buy_discount, str(p.liquidity) if p else None))

    # --- legs ---------------------------------------------------------------------------
    def add_leg(self, leg: Leg) -> Leg:
        cur = self.conn.execute(
            "INSERT INTO legs(symbol,side,qty,entry_px,entry_ref,entry_ts,force_close_at,status,fees_usd,mode) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (leg.symbol, leg.side.value, leg.qty, leg.entry_px, leg.entry_ref, _iso(leg.entry_ts),
             _iso(leg.force_close_at), leg.status.value, leg.fees_usd, leg.mode))
        leg.id = cur.lastrowid
        return leg

    def close_leg(self, leg: Leg) -> None:
        self.conn.execute(
            "UPDATE legs SET status=?, exit_px=?, exit_ts=?, exit_reason=?, fees_usd=?, pnl_usd=? WHERE id=?",
            (LegStatus.CLOSED.value, leg.exit_px, _iso(leg.exit_ts), leg.exit_reason, leg.fees_usd, leg.pnl_usd, leg.id))

    def _row_to_leg(self, r: sqlite3.Row) -> Leg:
        return Leg(id=r["id"], symbol=r["symbol"], side=Side(r["side"]), qty=r["qty"], entry_px=r["entry_px"],
                   entry_ref=r["entry_ref"], entry_ts=_parse(r["entry_ts"]), force_close_at=_parse(r["force_close_at"]),
                   status=LegStatus(r["status"]), exit_px=r["exit_px"], exit_ts=_parse(r["exit_ts"]),
                   exit_reason=r["exit_reason"], fees_usd=r["fees_usd"], pnl_usd=r["pnl_usd"], mode=r["mode"])

    def open_legs(self, mode: str | None = None) -> list[Leg]:
        q = "SELECT * FROM legs WHERE status='open'"
        args: tuple = ()
        if mode:
            q += " AND mode=?"
            args = (mode,)
        return [self._row_to_leg(r) for r in self.conn.execute(q + " ORDER BY id", args)]

    def closed_legs(self, mode: str, since: datetime | None = None) -> list[Leg]:
        q = "SELECT * FROM legs WHERE status='closed' AND mode=?"
        args: list = [mode]
        if since:
            q += " AND exit_ts>=?"
            args.append(_iso(since))
        return [self._row_to_leg(r) for r in self.conn.execute(q + " ORDER BY exit_ts", args)]

    # --- fills / equity / events --------------------------------------------------------
    def add_fill(self, f: Fill, mode: str) -> None:
        self.conn.execute(
            "INSERT INTO fills(ts,symbol,side,qty,px,fees_usd,tx_hash,mode,leg_id,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_iso(f.ts), f.order.symbol, f.order.side.value, f.qty_base, f.px, f.fees_usd, f.tx_hash, mode,
             f.order.leg_id, f.order.reason))

    def add_equity(self, ts: datetime, mode: str, realized_cum: float, unrealized: float, quote_usd: float,
                   inventory_usd: float) -> None:
        self.conn.execute("INSERT INTO equity VALUES (?,?,?,?,?,?,?)",
                          (_iso(ts), mode, realized_cum, unrealized, realized_cum + unrealized, quote_usd, inventory_usd))

    def equity_series(self, mode: str, since: datetime | None = None) -> list[tuple[datetime, float]]:
        q = "SELECT ts, strategy_equity FROM equity WHERE mode=?"
        args: list = [mode]
        if since:
            q += " AND ts>=?"
            args.append(_iso(since))
        return [(_parse(r["ts"]), r["strategy_equity"]) for r in self.conn.execute(q + " ORDER BY ts", args)]

    def add_event(self, ts: datetime, level: str, kind: str, msg: str) -> None:
        self.conn.execute("INSERT INTO events(ts,level,kind,msg) VALUES (?,?,?,?)", (_iso(ts), level, kind, msg))

    def count_events(self, kind: str, since: datetime | None = None) -> int:
        q = "SELECT COUNT(*) c FROM events WHERE kind=?"
        args: list = [kind]
        if since:
            q += " AND ts>=?"
            args.append(_iso(since))
        return int(self.conn.execute(q, args).fetchone()["c"])

    def tick_stats(self, since: datetime | None = None) -> dict:
        q = ("SELECT COUNT(*) n, SUM(CASE WHEN ref IS NOT NULL AND ref_stale=0 THEN 1 ELSE 0 END) ok, "
             "SUM(CASE WHEN market_open=1 THEN 1 ELSE 0 END) ext, "
             "SUM(CASE WHEN market_open=1 AND ref IS NOT NULL AND ref_stale=0 THEN 1 ELSE 0 END) ext_ok, "
             "MIN(ts) first_ts, MAX(ts) last_ts FROM ticks")
        args: list = []
        if since:
            q += " WHERE ts>=?"
            args.append(_iso(since))
        r = self.conn.execute(q, args).fetchone()
        return {"n": r["n"] or 0, "ok": r["ok"] or 0, "ext": r["ext"] or 0, "ext_ok": r["ext_ok"] or 0,
                "first_ts": _parse(r["first_ts"]), "last_ts": _parse(r["last_ts"])}

    def recent_ticks(self, symbol: str, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM ticks WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit)))

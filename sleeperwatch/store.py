"""SQLite storage.

Two layers:
  raw_responses  every response body, gzipped, exactly as Amtrak sent it
  observations   one row per (train, accommodation, fare family) per check

Keeping the raw bodies means that if the parser is improved later (say, to
pick up a room-type field it was missing) `reparse` can rebuild the whole
observation history without re-scraping anything.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .amtrak import TrainOption, adhoc_fares, is_sleeper, parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS raw_responses (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id),
    observed_at TEXT NOT NULL,
    watch       TEXT NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    passengers  INTEGER NOT NULL,
    body_gz     BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY,
    response_id   INTEGER REFERENCES raw_responses(id) ON DELETE CASCADE,
    observed_at   TEXT NOT NULL,
    watch         TEXT NOT NULL,
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    travel_date   TEXT NOT NULL,
    passengers    INTEGER NOT NULL,
    train_number  TEXT NOT NULL,
    train_name    TEXT,
    depart        TEXT,
    arrive        TEXT,
    travel_class  TEXT,
    accommodation TEXT,
    fare_family   TEXT,
    family_code   TEXT,
    price         REAL NOT NULL,
    seats_left    INTEGER,
    days_out      REAL,         -- days between observation and departure
    rail          REAL,         -- rail-fare part of price
    room_charge   REAL          -- accommodation part of price
);
-- Room types Amtrak lists as available but doesn't price in the search
-- (it prices only the cheapest room, normally a Roomette).
CREATE TABLE IF NOT EXISTS room_availability (
    id            INTEGER PRIMARY KEY,
    response_id   INTEGER REFERENCES raw_responses(id) ON DELETE CASCADE,
    observed_at   TEXT NOT NULL,
    watch         TEXT NOT NULL,
    travel_date   TEXT NOT NULL,
    train_number  TEXT NOT NULL,
    room          TEXT NOT NULL,
    available     INTEGER,
    days_out      REAL
);
CREATE INDEX IF NOT EXISTS obs_lookup
    ON observations (watch, travel_date, train_number, accommodation, family_code, observed_at);
CREATE TABLE IF NOT EXISTS failures (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id),
    observed_at TEXT NOT NULL,
    watch       TEXT,
    travel_date TEXT,
    error       TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in db.execute("PRAGMA table_info(observations)")}
    for col, typ in (("rail", "REAL"), ("room_charge", "REAL")):
        if col not in have:
            db.execute(f"ALTER TABLE observations ADD COLUMN {col} {typ}")
    db.commit()


def start_run(db: sqlite3.Connection, note: str = "") -> int:
    cur = db.execute("INSERT INTO runs (started_at, note) VALUES (?, ?)", (now_iso(), note))
    db.commit()
    return cur.lastrowid


def finish_run(db: sqlite3.Connection, run_id: int) -> None:
    db.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (now_iso(), run_id))
    db.commit()


def record_failure(db, run_id: int, watch: str, travel_date: str, error: str) -> None:
    db.execute(
        "INSERT INTO failures (run_id, observed_at, watch, travel_date, error) VALUES (?,?,?,?,?)",
        (run_id, now_iso(), watch, travel_date, error))
    db.commit()


def _days_out(observed_at: str, depart: str) -> float | None:
    """Days from observation to departure. Departure times are station-local
    and observation times are UTC, so this is accurate to a few hours, which
    is plenty for a booking-curve chart."""
    try:
        obs = datetime.fromisoformat(observed_at).replace(tzinfo=None)
        dep = datetime.fromisoformat(depart[:19])
        return round((dep - obs).total_seconds() / 86400, 3)
    except (TypeError, ValueError):
        return None


def save_response(db, run_id: int, watch: dict, travel_date: str, raw: dict,
                  observed_at: str | None = None) -> int:
    observed_at = observed_at or now_iso()
    cur = db.execute(
        """INSERT INTO raw_responses
           (run_id, observed_at, watch, origin, destination, travel_date, passengers, body_gz)
           VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, observed_at, watch["name"], watch["from"], watch["to"], travel_date,
         watch["passengers"], gzip.compress(json.dumps(raw).encode())))
    response_id = cur.lastrowid
    n = _insert_observations(db, response_id, observed_at, watch, travel_date, raw)
    db.commit()
    return n


def _insert_observations(db, response_id, observed_at, watch, travel_date, raw: dict) -> int:
    """Rows for one stored search: its own fares, plus room prices fetched
    separately for rooms the search left unpriced (Bedrooms), stored under
    ADHOC_KEY in the same raw body."""
    options: list[TrainOption] = parse(raw)
    extra = adhoc_fares(raw)
    from .config import keep_fare, keep_train  # avoid import cycle

    rows, avail_rows = [], []
    for opt in options:
        if not keep_train(watch, opt):
            continue
        priced_here = {f.accommodation for f in opt.fares}
        for f in opt.fares + [x for x in extra.get(opt.train_number, []) if x.accommodation not in priced_here]:
            if not keep_fare(watch, f):
                continue
            rows.append((
                response_id, observed_at, watch["name"], watch["from"], watch["to"], travel_date,
                watch["passengers"], opt.train_number, opt.train_name, opt.depart, opt.arrive,
                f.travel_class, f.accommodation, f.fare_family, f.family_code, f.price,
                f.seats_left, _days_out(observed_at, opt.depart), f.rail, f.accommodation_charge,
            ))
        if watch.get("only_sleepers", True) or any(is_sleeper(f) for f in opt.fares):
            for room in opt.unpriced_rooms:
                if watch.get("accommodations") and not any(
                        a.lower() in room.name.lower() for a in watch["accommodations"]):
                    continue
                avail_rows.append((response_id, observed_at, watch["name"], travel_date,
                                   opt.train_number, room.name, room.available,
                                   _days_out(observed_at, opt.depart)))
    db.executemany(
        """INSERT INTO observations
           (response_id, observed_at, watch, origin, destination, travel_date, passengers,
            train_number, train_name, depart, arrive, travel_class, accommodation,
            fare_family, family_code, price, seats_left, days_out, rail, room_charge)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    db.executemany(
        """INSERT INTO room_availability
           (response_id, observed_at, watch, travel_date, train_number, room, available, days_out)
           VALUES (?,?,?,?,?,?,?,?)""", avail_rows)
    return len(rows)


def reparse(db, watches_by_name: dict[str, dict]) -> int:
    """Rebuild every observation from the stored raw responses, using the
    current parser and the current watchlist filters."""
    db.execute("DELETE FROM observations")
    db.execute("DELETE FROM room_availability")
    total = 0
    for r in db.execute("SELECT * FROM raw_responses ORDER BY id").fetchall():
        watch = dict(watches_by_name.get(r["watch"]) or {})
        watch.update(name=r["watch"], **{"from": r["origin"], "to": r["destination"]},
                     passengers=r["passengers"])
        watch.setdefault("only_sleepers", True)
        raw = json.loads(gzip.decompress(r["body_gz"]))
        total += _insert_observations(db, r["id"], r["observed_at"], watch, r["travel_date"], raw)
    db.commit()
    return total

"""SQLite storage.

Main tables:
  raw_responses  every response body, gzipped, exactly as Amtrak sent it
  observations   one row per (train, accommodation, fare family) per check
  trains         catalog of direct trains seen in any search: number -> name

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
-- Every direct train seen in any search (fetch or discover), so train
-- numbers in the watchlist can be matched to the names Amtrak displays.
CREATE TABLE IF NOT EXISTS trains (
    train_number  TEXT NOT NULL,
    train_name    TEXT,
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    departs       TEXT,          -- HH:MM local, as last seen
    has_sleeper   INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    PRIMARY KEY (train_number, origin, destination)
);
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


def record_trains(db, raw: dict, observed_at: str | None = None) -> int:
    """Add or refresh the catalog entry for every direct train in a search
    response. Returns how many trains were recorded."""
    observed_at = observed_at or now_iso()
    rows = []
    for o in parse(raw):
        if not o.is_direct_train or not o.origin or not o.destination:
            continue
        sleeper = any(is_sleeper(f) for f in o.fares) or bool(o.unpriced_rooms)
        rows.append((o.train_number, o.train_name, o.origin, o.destination,
                     o.depart[11:16] or None, int(sleeper), observed_at, observed_at))
    db.executemany(
        """INSERT INTO trains (train_number, train_name, origin, destination, departs,
                               has_sleeper, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT (train_number, origin, destination) DO UPDATE SET
             train_name  = excluded.train_name,
             departs     = CASE WHEN excluded.last_seen >= trains.last_seen
                                THEN excluded.departs ELSE trains.departs END,
             has_sleeper = MAX(trains.has_sleeper, excluded.has_sleeper),
             first_seen  = MIN(trains.first_seen, excluded.first_seen),
             last_seen   = MAX(trains.last_seen, excluded.last_seen)""", rows)
    return len(rows)


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
    record_trains(db, raw, observed_at)
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


def latest_search(db, origin: str, destination: str, travel_date: str, passengers: int):
    """Newest stored search for this station pair, date and party size, from
    any watch (watches with the same key share one search). None if never searched."""
    return db.execute(
        """SELECT id, observed_at, watch, body_gz FROM raw_responses
           WHERE origin=? AND destination=? AND travel_date=? AND passengers=?
           ORDER BY observed_at DESC, id DESC LIMIT 1""",
        (origin, destination, travel_date, passengers)).fetchone()


def watch_has_search(db, watch: str, origin: str, destination: str, travel_date: str,
                     passengers: int, since: str) -> bool:
    """Whether this watch already stored a search for the key at or after `since`."""
    return db.execute(
        """SELECT 1 FROM raw_responses
           WHERE watch=? AND origin=? AND destination=? AND travel_date=? AND passengers=?
             AND observed_at >= ? LIMIT 1""",
        (watch, origin, destination, travel_date, passengers, since)).fetchone() is not None


def load_body(row) -> dict:
    return json.loads(gzip.decompress(row["body_gz"]))


def hours_since(observed_at: str) -> float:
    then = datetime.fromisoformat(observed_at)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


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
        record_trains(db, raw, r["observed_at"])
    db.commit()
    return total

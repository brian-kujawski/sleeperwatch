"""Watchlist loading and filtering. See watchlist.example.toml for the format."""

from __future__ import annotations

import tomllib
from datetime import date, timedelta
from pathlib import Path

from .amtrak import Fare, TrainOption, is_sleeper

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

DEFAULTS = {
    "passengers": 1,
    "only_sleepers": True,   # drop coach/business fares
    "trains": [],            # train numbers to keep; empty = all trains on the pair
    "accommodations": [],    # e.g. ["Roomette"]; substring match; empty = all
    "fare_families": [],     # e.g. ["VLU", "FLX"]; empty = all
}


def load(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"No watchlist at {path}. Run `sleeperwatch init` to create one, "
                         "or pass --watchlist PATH.")
    with path.open("rb") as fh:  # tomllib wants bytes; avoids platform text encodings
        cfg = tomllib.load(fh)
    defaults = {**DEFAULTS, **cfg.get("defaults", {})}
    watches = []
    for raw in cfg.get("watch", []):
        w = {**defaults, **raw}
        for key in ("name", "from", "to"):
            if not w.get(key):
                raise ValueError(f"watch {raw!r} is missing {key!r}")
        w["from"], w["to"] = w["from"].upper(), w["to"].upper()
        w["trains"] = [str(t) for t in w.get("trains", [])]
        w["dates"] = expand_dates(w)
        watches.append(w)
    names = [w["name"] for w in watches]
    if len(names) != len(set(names)):
        raise ValueError("watch names must be unique")
    return watches


def expand_dates(w: dict) -> list[str]:
    """Union of explicit `dates` and a `date_range` (optionally limited to
    `weekdays`). Past dates are dropped."""
    out = {str(d) for d in w.get("dates", [])}
    rng = w.get("date_range")
    if rng:
        start, end = date.fromisoformat(str(rng["start"])), date.fromisoformat(str(rng["end"]))
        days = {WEEKDAYS[d.lower()[:3]] for d in rng.get("weekdays", [])} or set(range(7))
        d = start
        while d <= end:
            if d.weekday() in days:
                out.add(d.isoformat())
            d += timedelta(days=1)
    today = date.today().isoformat()
    return sorted(d for d in out if d >= today)


def keep_train(w: dict, opt: TrainOption) -> bool:
    if not w.get("trains"):
        return True
    numbers = set(opt.train_number.split("+"))
    return bool(numbers & set(w["trains"]))


def keep_fare(w: dict, f: Fare) -> bool:
    if w.get("only_sleepers", True) and not is_sleeper(f):
        return False
    if w.get("accommodations"):
        text = f"{f.accommodation} {f.travel_class}".lower()
        if not any(a.lower() in text for a in w["accommodations"]):
            return False
    if w.get("fare_families") and f.family_code not in w["fare_families"]:
        return False
    return True

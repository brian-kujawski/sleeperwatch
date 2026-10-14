"""Offline tests: parsing, filtering, storage, reparse, report and plot,
with the network stubbed out. Run:  python -m tests.test_offline

Two fixtures: a small SYNTHETIC one shaped like the real response (easy to
vary prices across simulated checks), and a REAL capture of NYP->CHI from
`discover` in tests/fixtures/ that pins the parser to Amtrak's actual shape.
"""

import io
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os
import sys

from sleeperwatch import amtrak, cli, config, paths, store


def accom(cls, fam, price, inv, room=None):
    # Mirrors the real shape: product name and rbdType live on reservableProduct.
    product = {"availableInventory": inv, "name": room or f"{cls} Seat", "rbdType": cls}
    return {
        "travelClass": cls, "fareFamily": fam,
        "accommodationFare": {"dollarsAmount": {"total": f"{price:.2f}"}},
        "travelLegAccommodations": [{"reservableProduct": product}],
    }


def fixture(date, roomette=480.0, bedroom=1210.0):
    def opt(num, name, dep, arr, accoms, cancelled=False):
        return {
            "isCancelled": cancelled,
            "travelLegs": [{"travelService": {"number": num, "name": name}}],
            "origin": {"schedule": {"departureDateTime": f"{date}T{dep}"}},
            "destination": {"schedule": {"arrivalDateTime": arr}},
            "elapsedSeconds": 70000,
            "reservableAccommodations": accoms,
        }
    return {"data": {"journeySolutionOption": {"journeyLegs": [{"journeyLegOptions": [
        opt("49", "Lake Shore Limited", "15:40:00", "next-day", [
            accom("Coach", "VLU", 92, 30),
            accom("Sleeper", "VLU", roomette, 3, "Roomette"),
            accom("Sleeper", "FLX", roomette * 1.2, 3, "Roomette"),
            accom("Sleeper", "VLU", bedroom, 1, "Bedroom"),
        ]),
        opt("63", "Maple Leaf", "07:15:00", "x", [accom("Coach", "VLU", 70, 40)]),
        opt("49", "Lake Shore Limited", "23:00:00", "x", [], cancelled=True),
    ]}]}}}


def main():
    d = (datetime.now() + timedelta(days=60)).date().isoformat()

    # Parsing
    opts = amtrak.parse(fixture(d))
    assert len(opts) == 2, opts
    lsl = [o for o in opts if o.train_number == "49"][0]
    rooms = [f for f in lsl.fares if amtrak.is_sleeper(f)]
    assert {f.accommodation for f in rooms} == {"Roomette", "Bedroom"}
    assert min(f.seats_left for f in rooms) == 1
    assert amtrak.parse({}) == [] and amtrak.parse({"data": {}}) == []

    # Payload sanity
    pl = amtrak.build_payload("NYP", "CHI", d, 2)
    leg = pl["journeyRequest"]["journeyLegRequests"][0]
    assert leg["origin"]["code"] == "NYP" and len(leg["passengers"]) == 2
    assert pl["journeyRequest"]["singleAdultFare"] is False

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wl = tmp / "watchlist.toml"
        wl.write_text(f'''
[defaults]
passengers = 2
[[watch]]
name = "lsl"
from = "nyp"
to = "chi"
trains = ["49"]
dates = ["{d}", "2020-01-01"]
[[watch]]
name = "lsl-roomette-value"
from = "NYP"
to = "CHI"
trains = [49]
accommodations = ["roomette"]
fare_families = ["VLU"]
dates = ["{d}"]
[[watch]]
name = "range"
from = "CHI"
to = "SEA"
date_range = {{ start = "{d}", end = "{(datetime.fromisoformat(d) + timedelta(days=13)).date()}", weekdays = ["fri"] }}
''')
        watches = config.load(wl)
        assert watches[0]["dates"] == [d], "past dates dropped"
        assert watches[0]["from"] == "NYP"
        assert len(watches[2]["dates"]) == 2 and all(
            datetime.fromisoformat(x).weekday() == 4 for x in watches[2]["dates"])

        db = store.connect(tmp / "data" / "fares.db")
        run = store.start_run(db)
        # Simulate four checks over three weeks with prices moving.
        base = datetime.now(timezone.utc) - timedelta(days=21)
        prices = [(480, 1210), (480, 1210), (560, 1350), (520, 1490)]
        for i, (rm, br) in enumerate(prices):
            at = (base + timedelta(days=7 * i)).isoformat(timespec="seconds")
            n_all = store.save_response(db, run, watches[0], d, fixture(d, rm, br), observed_at=at)
            n_one = store.save_response(db, run, watches[1], d, fixture(d, rm, br), observed_at=at)
            assert n_all == 3, n_all      # 3 room fares, coach and Maple Leaf dropped
            assert n_one == 1, n_one      # only Roomette Value
        store.finish_run(db, run)

        total = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert total == 16
        days = [r[0] for r in db.execute("SELECT days_out FROM observations WHERE watch='lsl' ORDER BY observed_at")]
        assert days[0] > days[-1] > 0

        # Reparse must reproduce the same rows from raw bodies
        assert store.reparse(db, {w["name"]: w for w in watches}) == 16
        db.close()  # Windows can't delete the temp dir while the DB is open

        args = ["--data-dir", str(tmp / "data"), "--watchlist", str(wl)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(args + ["report"])
            cli.main(args + ["fetch", "--dry-run"])
            cli.main(args + ["export", "--out", str(tmp / "obs.csv")])
        out = buf.getvalue()
        print(out)
        assert "$480" in out and "$560" in out and "Roomette" in out

        try:
            import matplotlib  # noqa
            with redirect_stdout(io.StringIO()):
                cli.main(args + ["plot"])
            charts = list((tmp / "data" / "charts").glob("*.png"))
            assert len(charts) == 2, charts
        except ImportError:
            print("(matplotlib not installed; skipped plot)")

        # init writes the packaged example; paths reports locations
        new_wl = tmp / "cfg" / "nested" / "watchlist.toml"
        with redirect_stdout(io.StringIO()):
            cli.main(["--data-dir", str(tmp / "d2"), "--watchlist", str(new_wl), "init"])
            cli.main(["--data-dir", str(tmp / "d2"), "--watchlist", str(new_wl), "paths"])
        assert new_wl.exists() and "[[watch]]" in new_wl.read_text(encoding="utf-8")
        config.load(new_wl)  # the shipped example must parse

    test_paths()
    test_real_capture()
    print("all offline tests passed")


def test_real_capture():
    """Parser against a live NYP->CHI response captured 2026-10-14."""
    import json
    fx = Path(__file__).parent / "fixtures" / "nyp-chi-2026-11-14.json"
    cap = json.loads(fx.read_text(encoding="utf-8"))
    # The site's own request is exactly what build_payload() sends.
    assert amtrak.compare_payload(cap["request_body"]) == []
    opts = {o.train_number: o for o in amtrak.parse(cap["response"])}
    assert set(opts) == {"2205+40", "43+40", "195+40", "49"}, set(opts)

    lsl = opts["49"]
    assert lsl.train_name == "Lake Shore Limited"
    room = [f for f in lsl.fares if amtrak.is_sleeper(f)]
    assert len(room) == 1, room
    r = room[0]
    assert (r.travel_class, r.accommodation, r.family_code) == ("Sleeper", "Roomette", "NA")
    assert (r.price, r.rail, r.accommodation_charge, r.seats_left) == (826.0, 218.0, 608.0, 5)
    assert [(u.name, u.available) for u in lsl.unpriced_rooms] == [("Bedroom", 3)]
    assert {f.accommodation for f in lsl.fares if not amtrak.is_sleeper(f)} == {"Coach Seat"}

    # Connecting trip: the room is on the 2nd train; name must come from that leg,
    # not the Acela First Class seat on the first.
    acela = [f for f in opts["2205+40"].fares if amtrak.is_sleeper(f)][0]
    assert acela.accommodation == "Roomette" and acela.seats_left == 18, acela

    # Watch filtering keeps only the Lake Shore sleeper and records the Bedroom count.
    w = {"name": "t", "from": "NYP", "to": "CHI", "passengers": 1, "trains": ["49"],
         "only_sleepers": True, "accommodations": [], "fare_families": []}
    with tempfile.TemporaryDirectory() as tmp:
        db = store.connect(Path(tmp) / "f.db")
        run = store.start_run(db)
        assert store.save_response(db, run, w, "2026-11-14", cap["response"]) == 1
        row = db.execute("SELECT accommodation, price, rail, room_charge FROM observations").fetchone()
        assert tuple(row) == ("Roomette", 826.0, 218.0, 608.0), tuple(row)
        avail = db.execute("SELECT train_number, room, available FROM room_availability").fetchall()
        assert [tuple(a) for a in avail] == [("49", "Bedroom", 3)], [tuple(a) for a in avail]
        db.close()


def test_paths():
    assert paths.safe_filename('a:b/c\\d*e?"f<g>|h') == "a_b_c_d_e_f_g_h"
    assert paths.safe_filename("  ...  ") == "unnamed"
    old = os.environ.get("SLEEPERWATCH_DATA")
    os.environ["SLEEPERWATCH_DATA"] = "~/somewhere"
    try:
        assert paths.data_dir() == Path.home() / "somewhere"
    finally:
        if old is None:
            os.environ.pop("SLEEPERWATCH_DATA")
        else:
            os.environ["SLEEPERWATCH_DATA"] = old
    d = paths.data_dir()
    assert d.name == "sleeperwatch" and d.is_absolute()
    if sys.platform == "win32":
        assert "AppData" in str(d)
    elif sys.platform == "darwin":
        assert "Application Support" in str(d)
    assert paths.watchlist_path("x.toml") == Path("x.toml")


if __name__ == "__main__":
    main()

"""Offline tests: parsing, filtering, storage, reparse, report and plot,
with the network stubbed out. Run:  python -m tests.test_offline

The fixture is SYNTHETIC -- shaped like the publicly documented
journey-solution-option response, not captured from Amtrak. Once you have a
real capture from `discover`, drop it in tests/ and add a test against it.
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
    product = {"availableInventory": inv}
    if room:
        product["accommodationName"] = room
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
            accom("Private Rooms", "VLU", roomette, 3, "Roomette"),
            accom("Private Rooms", "FLX", roomette * 1.2, 3, "Roomette"),
            accom("Private Rooms", "VLU", bedroom, 1, "Bedroom"),
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
    print("all offline tests passed")


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

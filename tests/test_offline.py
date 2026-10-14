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
    test_room_pricing()
    test_direct_only()
    test_train_catalog()
    test_dashboard()
    test_refresh_window()
    print("all offline tests passed")


def test_direct_only():
    """Default watches keep only single-train itineraries."""
    import json
    cap = json.loads((Path(__file__).parent / "fixtures" / "nyp-chi-2026-11-14.json").read_text(encoding="utf-8"))
    opts = amtrak.parse(cap["response"])
    w = {"trains": []}                                   # direct_only defaults to on
    assert [o.train_number for o in opts if config.keep_train(w, o)] == ["49"]
    assert len([o for o in opts if config.keep_train({"direct_only": False}, o)]) == 4
    # A bus segment disqualifies even a single-segment itinerary.
    bus = json.loads(json.dumps(cap["response"]).replace('"type": "TRAIN"', '"type": "BUS"'))
    assert [o for o in amtrak.parse(bus) if config.keep_train(w, o)] == []


def test_train_catalog():
    """Direct trains seen in searches are catalogued number -> name."""
    import json, shutil
    fx = Path(__file__).parent / "fixtures" / "nyp-chi-2026-11-14.json"
    cap = json.loads(fx.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = store.connect(tmp / "fares.db")
        assert store.record_trains(db, cap["response"], "2026-10-14T15:00:00+00:00") == 1
        assert store.record_trains(db, cap["response"], "2026-10-15T15:00:00+00:00") == 1
        db.commit()
        rows = [tuple(r) for r in db.execute(
            "SELECT train_number, train_name, origin, destination, departs, has_sleeper, first_seen, last_seen FROM trains")]
        assert rows == [("49", "Lake Shore Limited", "NYP", "CHI", "15:40", 1,
                         "2026-10-14T15:00:00+00:00", "2026-10-15T15:00:00+00:00")], rows
        db.close()

        # `trains --scan-captures` imports past discover captures
        (tmp / "data" / "captures").mkdir(parents=True)
        shutil.copy(fx, tmp / "data" / "captures" / "capture-20261014-110804.json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--data-dir", str(tmp / "data"), "--watchlist", str(tmp / "none.toml"),
                      "trains", "--scan-captures"])
        out = buf.getvalue()
        assert "Lake Shore Limited" in out and "NYP->CHI" in out and "15:40" in out, out


def test_dashboard():
    """Departures are grouped per route/party/room/train/date; overlapping
    rows at one check collapse to the cheapest; the page embeds safely."""
    from sleeperwatch import dashboard
    with tempfile.TemporaryDirectory() as tmp:
        db = store.connect(Path(tmp) / "f.db")
        rows = []
        for watch in ("a", "b"):                      # two watches sharing one search
            for at, price in (("2026-10-01T10:00:00+00:00", 900.0), ("2026-10-08T10:00:00+00:00", 850.0)):
                for fam, bump in (("NA", 0), ("FLX", 40)):
                    rows.append((at, watch, "NYP", "CHI", "2026-12-09", 2, "51", "Card</script>inal",
                                 "2026-12-09T06:45:00", "Sleeper", "Roomette", fam, price + bump, 60.0))
        db.executemany("""INSERT INTO observations (observed_at, watch, origin, destination, travel_date,
            passengers, train_number, train_name, depart, travel_class, accommodation, family_code, price, days_out)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        db.commit()
        data = dashboard.collect(db)
        db.close()
    assert len(data["departures"]) == 1
    d = data["departures"][0]
    assert (d["route"], d["pax"], d["room"], d["train"], d["date"], d["depart"]) == \
        ("NYP→CHI", 2, "Roomette", "51", "2026-12-09", "06:45")
    assert [o[2] for o in d["obs"]] == [900.0, 850.0], d["obs"]
    html = dashboard.render(data)
    assert html.count("</script>") == 1 and "Card<\\/script>inal" in html


def test_refresh_window():
    """Recently searched trip-dates are skipped; new dates are searched; a
    watch that hasn't seen a recent shared search reuses it; --force overrides."""
    import json
    from datetime import datetime, timedelta, timezone
    cap = json.loads((Path(__file__).parent / "fixtures" / "nyp-chi-2026-11-14.json").read_text(encoding="utf-8"))
    raw = cap["response"]
    this_morning = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    last_week = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(timespec="seconds")
    base = {"passengers": 2, "only_sleepers": True, "direct_only": True, "accommodations": [],
            "fare_families": [], "price_rooms": True, "refresh_window": 48}
    wb = {**base, "name": "cardinal-wb", "from": "NYP", "to": "CHI", "trains": ["51"]}
    eb = {**base, "name": "cardinal-eb", "from": "CHI", "to": "NYP", "trains": ["50"]}
    lsl = {**base, "name": "lsl-wb", "from": "NYP", "to": "CHI", "trains": ["49"]}
    with tempfile.TemporaryDirectory() as tmp:
        db = store.connect(Path(tmp) / "f.db")
        run = store.start_run(db)
        store.save_response(db, run, wb, "2026-12-09", raw, observed_at=this_morning)
        store.save_response(db, run, eb, "2026-12-10", raw, observed_at=this_morning)
        store.save_response(db, run, wb, "2026-12-16", raw, observed_at=last_week)
        jobs = [(wb, "2026-12-09"), (wb, "2026-12-16"), (wb, "2027-01-06"), (eb, "2026-12-10"), (lsl, "2026-12-09")]
        got = {(w["name"], d): a for w, d, a, *_ in cli._plan(db, jobs, force=False)}
        assert got == {
            ("cardinal-wb", "2026-12-09"): "skip",     # searched this morning
            ("cardinal-wb", "2026-12-16"): "search",   # last searched 6 days ago > 48h
            ("cardinal-wb", "2027-01-06"): "search",   # newly added date
            ("cardinal-eb", "2026-12-10"): "skip",     # untouched watch, fresh
            ("lsl-wb", "2026-12-09"): "reuse",         # same NYP-CHI search, new to this watch
        }, got
        assert {a for *_, a, _l, _g in cli._plan(db, jobs, force=True)} == {"search"}
        assert {a for *_, a, _l, _g in cli._plan(db, [(dict(wb, refresh_window=0), "2026-12-09")], force=False)} == {"search"}
        db.close()

        # End to end: a fetch where everything is skip/reuse never opens a browser.
        wl = Path(tmp) / "w.toml"
        wl.write_text("""
[defaults]
passengers = 2
refresh_window = 48
[[watch]]
name = "cardinal-wb"
from = "NYP"
to = "CHI"
trains = ["51"]
dates = ["2026-12-09"]
[[watch]]
name = "lsl-wb"
from = "NYP"
to = "CHI"
trains = ["49"]
dates = ["2026-12-09"]
""")
        import shutil
        (Path(tmp) / "data").mkdir()
        shutil.copy(Path(tmp) / "f.db", Path(tmp) / "data" / "fares.db")
        def no_browser(*a, **k):
            raise AssertionError("browser should not be opened")
        orig, amtrak.BrowserSession = amtrak.BrowserSession, no_browser
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["--data-dir", str(Path(tmp) / "data"), "--watchlist", str(wl), "fetch"])
        finally:
            amtrak.BrowserSession = orig
        out = buf.getvalue()
        assert "0 Amtrak search(es), 1 reused from recent searches, 1 skipped as fresh" in out, out
        assert "reuse lsl-wb 2026-12-09: 1 fare row(s)" in out, out


def test_room_pricing():
    """Bedroom pricing: request builder pinned to the site's own request,
    price extraction checked on Amtrak's real priced-accommodation shape."""
    import json
    fx = Path(__file__).parent / "fixtures"
    cap = json.loads((fx / "nyp-chi-2026-11-14-bedroom.json").read_text(encoding="utf-8"))
    reqs = amtrak.adhoc_requests(cap["response"], cap["request_body"])
    # Connecting itineraries are skipped; the Lake Shore's Bedroom is priced.
    assert [(r.train_number, r.room) for r in reqs] == [("49", "Bedroom")], reqs
    built = json.dumps(reqs[0].request).replace("92e76cfe_2026-11-14T15:40:00", "aac70922_2026-11-14T15:40:00")
    assert json.loads(built) == cap["site_adhoc_request"], "adhoc request no longer matches the site's"

    # The cart request carries a real priced sleeper selection; the extractor must read it.
    cart = json.loads((fx / "cart-roomette.json").read_text(encoding="utf-8"))
    fares = amtrak.fares_in(cart)
    assert [(f.accommodation, f.price, f.rail, f.accommodation_charge, f.seats_left) for f in fares] \
        == [("Roomette", 930.0, 218.0, 712.0, 7)], fares

    # Stored adhoc responses become observation rows (here shaped like the cart's).
    bedroom_resp = json.loads(json.dumps(cart).replace('"Roomette"', '"Bedroom"').replace('"930.00"', '"1489.00"'))
    raw = dict(cap["response"])
    raw[amtrak.ADHOC_KEY] = [{"train_number": "49", "room": "Bedroom", "status": 200, "response": bedroom_resp}]
    w = {"name": "t", "from": "NYP", "to": "CHI", "passengers": 1, "trains": ["49"],
         "only_sleepers": True, "accommodations": [], "fare_families": []}
    with tempfile.TemporaryDirectory() as tmp:
        db = store.connect(Path(tmp) / "f.db")
        run = store.start_run(db)
        assert store.save_response(db, run, w, "2026-11-14", raw) == 2
        got = {r[0]: r[1] for r in db.execute("SELECT accommodation, price FROM observations")}
        assert got == {"Roomette": 930.0, "Bedroom": 1489.0}, got
        assert store.reparse(db, {"t": w}) == 2   # survives a rebuild from the raw body
        db.close()


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

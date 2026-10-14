"""Command line entry point:  sleeperwatch <command>  (or python -m sleeperwatch)

  init      create a watchlist from the example
  paths     show where the watchlist and data live
  fetch     check every watched trip once and record the fares
  discover  open a browser, search by hand, capture the raw request/response
  report    latest / lowest / highest price seen per train, room and fare
  export    dump all observations to CSV
  plot      price-vs-days-before-departure charts (PNG, needs matplotlib)
  reparse   rebuild observations from stored raw responses
"""

from __future__ import annotations

import argparse
import csv
import random
import shutil
import sys
import time
from contextlib import ExitStack, closing
from importlib import resources
from pathlib import Path

from . import amtrak, config, paths, store


def _paths(args):
    data = Path(args.data_dir).expanduser() if args.data_dir else paths.data_dir()
    return {
        "data": data,
        "db": data / "fares.db",
        "profile": data / "browser-profile",
        "captures": data / "captures",
        "charts": data / "charts",
    }


def _select(watches, names):
    if not names:
        return watches
    picked = [w for w in watches if w["name"] in names]
    missing = set(names) - {w["name"] for w in picked}
    if missing:
        sys.exit(f"unknown watch name(s): {', '.join(sorted(missing))}")
    return picked


# ---------------------------------------------------------------------------


def cmd_fetch(args):
    p = _paths(args)
    watches = _select(config.load(args.watchlist), args.watch)
    jobs = [(w, d) for w in watches for d in w["dates"]]
    if not jobs:
        print("Nothing to check (no future dates in the watchlist).")
        return
    print(f"{len(jobs)} trip-date(s) across {len(watches)} watch(es)")
    if args.dry_run:
        for w, d in jobs:
            print(f"  {w['name']}: {w['from']}->{w['to']} {d} x{w['passengers']}")
        return

    db = args.open_db()
    run_id = store.start_run(db, note=" ".join(sys.argv[1:]))
    # Several watches can share one search (same pair/date/party, different
    # trains). Ask Amtrak once and reuse the answer.
    cache: dict[tuple, dict] = {}
    consecutive_failures = 0
    saved = 0
    try:
        with amtrak.BrowserSession(p["profile"], headless=args.headless, channel=args.channel,
                                   executable=args.browser) as s:
            print("Warming up browser session...")
            s.warm_up()
            for w, d in jobs:
                key = (w["from"], w["to"], d, w["passengers"])
                label = f"{w['name']} {d}"
                try:
                    if key not in cache:
                        cache[key] = s.search_raw(*key)
                        time.sleep(random.uniform(2.0, 4.5))  # be polite
                    n = store.save_response(db, run_id, w, d, cache[key])
                    saved += n
                    consecutive_failures = 0
                    print(f"  ok    {label}: {n} fare row(s)" + ("" if n else "  <- no matching sleeper fares (sold out, wrong train number, or filters too narrow)"))
                except Exception as e:
                    consecutive_failures += 1
                    store.record_failure(db, run_id, w["name"], d, str(e))
                    print(f"  FAIL  {label}: {e}")
                    if consecutive_failures >= 3:
                        print("Three failures in a row; stopping so we don't hammer a blocked session.")
                        break
    finally:
        store.finish_run(db, run_id)
    print(f"Done. {saved} observation row(s) saved to {p['db']}")


def cmd_discover(args):
    p = _paths(args)
    amtrak.discover(p["profile"], p["captures"], channel=args.channel, executable=args.browser)


REPORT_SQL = """
WITH ranked AS (
  SELECT *, ROW_NUMBER() OVER (
           PARTITION BY watch, travel_date, train_number, accommodation, family_code
           ORDER BY observed_at DESC) AS rn
  FROM observations
)
SELECT r.watch, r.travel_date, r.train_number, r.accommodation, r.travel_class,
       r.fare_family, r.price AS latest, r.seats_left, r.observed_at AS latest_at,
       agg.lo, agg.hi, agg.n
FROM ranked r
JOIN (SELECT watch, travel_date, train_number, accommodation, family_code,
             MIN(price) lo, MAX(price) hi, COUNT(*) n
      FROM observations
      GROUP BY 1,2,3,4,5) agg
  USING (watch, travel_date, train_number, accommodation, family_code)
WHERE r.rn = 1 {where}
ORDER BY r.watch, r.travel_date, r.train_number, r.accommodation, r.price
"""


def cmd_report(args):
    db = args.open_db()
    where, params = "", []
    if args.watch:
        where = f"AND r.watch IN ({','.join('?' * len(args.watch))})"
        params = args.watch
    rows = db.execute(REPORT_SQL.format(where=where), params).fetchall()
    if not rows:
        print("No observations yet. Run `fetch` first.")
        return
    header = ("watch", "date", "train", "room", "fare", "latest", "low", "high", "left", "checks", "last checked (UTC)")
    table = [header]
    for r in rows:
        table.append((
            r["watch"], r["travel_date"], r["train_number"],
            r["accommodation"] or r["travel_class"], r["fare_family"],
            f"${r['latest']:,.0f}", f"${r['lo']:,.0f}", f"${r['hi']:,.0f}",
            "" if r["seats_left"] is None else str(r["seats_left"]), str(r["n"]),
            r["latest_at"][:16].replace("T", " "),
        ))
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for i, row in enumerate(table):
        print("  ".join(c.ljust(widths[j]) for j, c in enumerate(row)))
        if i == 0:
            print("  ".join("-" * w for w in widths))

    fails = db.execute(
        "SELECT watch, travel_date, error, observed_at FROM failures ORDER BY id DESC LIMIT 5").fetchall()
    if fails:
        print("\nRecent failures:")
        for f in fails:
            print(f"  {f['observed_at'][:16]}  {f['watch']} {f['travel_date']}: {f['error']}")


def cmd_export(args):
    db = args.open_db()
    rows = db.execute("SELECT * FROM observations ORDER BY watch, travel_date, observed_at").fetchall()
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if rows:
            w.writerow(rows[0].keys())
        w.writerows(tuple(r) for r in rows)
    print(f"Wrote {len(rows)} row(s) to {out}")


# Categorical order from a CVD-validated palette (fixed order, never cycled).
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


def cmd_plot(args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("plot needs matplotlib:  pip install matplotlib")

    p = _paths(args)
    db = args.open_db()
    p["charts"].mkdir(parents=True, exist_ok=True)
    where, params = "", []
    if args.watch:
        where = f"WHERE watch IN ({','.join('?' * len(args.watch))})"
        params = args.watch
    groups = db.execute(
        f"SELECT DISTINCT watch, travel_date, train_number FROM observations {where} ORDER BY 1,2,3",
        params).fetchall()
    if not groups:
        print("No observations to plot.")
        return

    for g in groups:
        series = db.execute(
            """SELECT COALESCE(NULLIF(accommodation,''), travel_class) || ' · ' || fare_family AS label,
                      days_out, price
               FROM observations
               WHERE watch=? AND travel_date=? AND train_number=? AND days_out IS NOT NULL
               ORDER BY label, observed_at""",
            (g["watch"], g["travel_date"], g["train_number"])).fetchall()
        by_label: dict[str, list[tuple[float, float]]] = {}
        for r in series:
            by_label.setdefault(r["label"], []).append((r["days_out"], r["price"]))
        if not by_label:
            continue
        # Cheapest fares first so color slots are stable for the same room set.
        labels = sorted(by_label, key=lambda k: min(p for _, p in by_label[k]))[:len(SERIES_COLORS)]

        fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
        fig.patch.set_facecolor("#fcfcfb")
        ax.set_facecolor("#fcfcfb")
        for i, label in enumerate(labels):
            pts = by_label[label]
            xs, ys = [x for x, _ in pts], [y for _, y in pts]
            color = SERIES_COLORS[i]
            ax.step(xs, ys, where="post", color=color, linewidth=2, label=label)
            ax.plot(xs, ys, "o", color=color, markersize=4, markeredgecolor="#fcfcfb", markeredgewidth=1)
            ax.annotate(f"${ys[-1]:,.0f}", (xs[-1], ys[-1]), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=8, color="#52514e")
        ax.invert_xaxis()  # time flows left to right toward departure
        ax.set_xlabel("Days before departure", color="#52514e")
        ax.set_ylabel("Price (USD, whole party)", color="#52514e")
        ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))
        ax.grid(axis="y", color="#e6e5e0", linewidth=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c9c8c2")
        ax.tick_params(colors="#52514e", labelsize=8)
        ax.set_title(f"{g['watch']} · train {g['train_number']} · departs {g['travel_date']}",
                     loc="left", fontsize=11, color="#0b0b0b")
        if len(labels) > 1:
            ax.legend(frameon=False, fontsize=8, loc="upper left")
        fig.tight_layout()
        name = paths.safe_filename(f"{g['watch']}_{g['travel_date']}_{g['train_number'].replace('+', '-')}") + ".png"
        fig.savefig(p["charts"] / name, facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"  {p['charts'] / name}")


def cmd_reparse(args):
    db = args.open_db()
    watches = {w["name"]: w for w in config.load(args.watchlist)}
    n = store.reparse(db, watches)
    print(f"Rebuilt {n} observation row(s) from stored responses.")


def cmd_init(args):
    dest = args.watchlist
    if dest.exists() and not args.force:
        print(f"Watchlist already exists: {dest}  (use --force to overwrite)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    example = resources.files("sleeperwatch").joinpath("watchlist.example.toml")
    with resources.as_file(example) as src:
        shutil.copyfile(src, dest)
    print(f"Created {dest}\nEdit it to list your routes, then run `sleeperwatch fetch --dry-run`.")


def cmd_paths(args):
    p = _paths(args)
    print(f"watchlist  {args.watchlist}{'' if args.watchlist.exists() else '  (missing; run `sleeperwatch init`)'}")
    print(f"database   {p['db']}")
    print(f"browser    {p['profile']}")
    print(f"captures   {p['captures']}")
    print(f"charts     {p['charts']}")


# ---------------------------------------------------------------------------

CHANNEL_HELP = ("which installed browser to prefer: chrome (default; falls back to Chromium or Edge "
                "if Chrome isn't installed), msedge, or chromium (Playwright's bundled browser, "
                "launched in automation mode; Amtrak blocks it more often)")
BROWSER_HELP = "path to a specific Chrome/Chromium/Edge executable to use"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sleeperwatch", description="Track Amtrak sleeper fares over time.")
    ap.add_argument("--data-dir", help="where the DB, browser profile and charts live "
                                       "(default: your OS's app-data folder; see `sleeperwatch paths`)")
    ap.add_argument("--watchlist", help="watchlist TOML (default: ./watchlist.toml if present, "
                                        "else your OS's config folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="create a watchlist from the example")
    i.add_argument("--force", action="store_true", help="overwrite an existing watchlist")
    i.set_defaults(func=cmd_init)

    sub.add_parser("paths", help="show where files live").set_defaults(func=cmd_paths)

    f = sub.add_parser("fetch", help="check watched trips once and record fares")
    f.add_argument("--watch", nargs="*", help="only these watch names")
    f.add_argument("--headless", action="store_true", help="hide the browser (more likely to be blocked)")
    f.add_argument("--channel", default="chrome", choices=["chrome", "msedge", "chromium"], help=CHANNEL_HELP)
    f.add_argument("--browser", metavar="PATH", help=BROWSER_HELP)
    f.add_argument("--dry-run", action="store_true", help="list what would be checked")
    f.set_defaults(func=cmd_fetch)

    d = sub.add_parser("discover", help="capture a hand-made search for inspection")
    d.add_argument("--channel", default="chrome", choices=["chrome", "msedge", "chromium"], help=CHANNEL_HELP)
    d.add_argument("--browser", metavar="PATH", help=BROWSER_HELP)
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("report", help="latest/low/high per train, room and fare")
    r.add_argument("--watch", nargs="*")
    r.set_defaults(func=cmd_report)

    e = sub.add_parser("export", help="write observations to CSV")
    e.add_argument("--out", default="observations.csv")
    e.set_defaults(func=cmd_export)

    pl = sub.add_parser("plot", help="price vs days-before-departure charts")
    pl.add_argument("--watch", nargs="*")
    pl.set_defaults(func=cmd_plot)

    rp = sub.add_parser("reparse", help="rebuild observations from raw responses")
    rp.set_defaults(func=cmd_reparse)

    args = ap.parse_args(argv)
    args.watchlist = paths.watchlist_path(args.watchlist)
    # Every connection is closed on exit: Windows won't let a still-open
    # SQLite file be moved or deleted.
    with ExitStack() as stack:
        args.open_db = lambda: stack.enter_context(closing(store.connect(_paths(args)["db"])))
        try:
            args.func(args)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130


if __name__ == "__main__":
    main()

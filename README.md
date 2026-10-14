# sleeperwatch

Records Amtrak sleeper fares (roomettes, bedrooms) for a short list of trains
and dates, so you can see how prices move as departure approaches. Run it
by hand whenever you like; every run adds a data point.

Works on Linux, macOS and Windows (Python 3.11+).

## Install

Linux / macOS:

```sh
git clone https://github.com/brian-kujawski/sleeperwatch
cd sleeperwatch
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[plot]"
python -m playwright install chromium
```

Windows (PowerShell):

```powershell
git clone https://github.com/brian-kujawski/sleeperwatch
cd sleeperwatch
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[plot]"
python -m playwright install chromium
```

Use `pip install -e .` instead if you don't want charts (it skips matplotlib).
`pipx install ".[plot]"` also works if you'd rather not manage a venv.

**Browser.** By default sleeperwatch drives your installed Google Chrome,
which gets past Amtrak's bot check more reliably than Playwright's bundled
Chromium. If Chrome isn't installed it falls back to Chromium automatically.
You can also pick one with `--channel chrome|msedge|chromium`.

**Linux notes.**
- If the bundled Chromium complains about missing libraries, run
  `python -m playwright install-deps chromium` (uses your package manager,
  needs sudo).
- A visible browser window needs a desktop session (X11 or Wayland). Over
  SSH or on a headless box, use `xvfb-run -a sleeperwatch fetch`, or try
  `--headless` (blocked more often).

## Setup

```sh
sleeperwatch init     # creates your watchlist from the example
sleeperwatch paths    # shows where the watchlist and data live
```

Files live in your OS's usual places, not in the git checkout:

|             | watchlist                                 | data (DB, browser profile, charts)        |
|-------------|-------------------------------------------|-------------------------------------------|
| Linux       | `~/.config/sleeperwatch/` (`$XDG_CONFIG_HOME`) | `~/.local/share/sleeperwatch/` (`$XDG_DATA_HOME`) |
| macOS       | `~/Library/Application Support/sleeperwatch/` | same                                  |
| Windows     | `%APPDATA%\sleeperwatch\`                 | `%LOCALAPPDATA%\sleeperwatch\`            |

You can override these with `--watchlist` / `--data-dir`, or with the
`SLEEPERWATCH_WATCHLIST` / `SLEEPERWATCH_DATA` environment variables. A
`watchlist.toml` in the current directory also takes priority (and is
git-ignored).

## First run: check the shape of the data

The request format is known, but the exact field Amtrak uses for room type
(Roomette vs Bedroom) has not been checked against a live response. Do this once:

```sh
sleeperwatch discover
```

A browser window opens on Amtrak's search page. Search for one of your
sleeper routes by hand. Each search is saved to the `captures` folder, and
the terminal prints the trains it parsed plus one raw room-fare entry. If
the room names show up (e.g. `Roomette Value $480`), you're set. If they're
blank, find the field holding the room name in the sample entry, add it to
`_accommodation_name()` in `sleeperwatch/amtrak.py`, and run
`sleeperwatch reparse` to fix any rows already stored.

## Day-to-day

1. Edit the watchlist: routes, train numbers, room types, dates or date ranges.
2. `sleeperwatch fetch --dry-run`: lists what would be checked.
3. `sleeperwatch fetch`: checks everything once. A browser window opens and closes.
4. `sleeperwatch report`: latest, lowest and highest price per train, room and fare.
5. `sleeperwatch plot`: one PNG per train and date, showing price against days before departure.
6. `sleeperwatch export --out fares.csv`: writes everything to CSV for a spreadsheet.

`--watch NAME ...` narrows `fetch`, `report` and `plot` to specific watches.

## What gets stored (SQLite, `fares.db`)

- `observations`: one row per train × room type × fare family per check,
  with the whole-party price, seats left in that bucket if reported, and
  `days_out`.
- `raw_responses`: every response body, gzipped. If the parser improves
  later, `reparse` rebuilds `observations` from these without re-scraping.
- `runs`, `failures`: when it ran and what went wrong.

## How it works and when it breaks

Amtrak has no public fare API. The booking page calls an internal endpoint,
`/dotcom/journey-solution-option`. The site is protected by Akamai bot
detection, which blocks plain HTTP clients. So sleeperwatch opens a real
browser, loads amtrak.com to clear the bot check, and runs each search from
inside that page. A persistent browser profile keeps the cookies between
runs.

If every search fails, Amtrak probably changed the request. Run
`discover`, search by hand, and compare the saved `request_body` with
`build_payload()` in `amtrak.py`.

## Notes on the numbers

- Room prices cover the whole party: rail fare for each passenger plus
  the room charge. Set `passengers` in the watchlist to match how you'd book.
- Amtrak sells in fare buckets. Prices mostly step up as buckets sell out;
  drops usually mean a cancellation or a sale.
- Amtrak's terms don't permit automated access. Keep runs occasional and
  the watchlist short. The script pauses a few seconds between searches
  and stops after three failures in a row.

## Tests

```sh
python -m tests.test_offline
```

These run offline against a synthetic response, and CI runs them on Linux,
macOS and Windows. Once `discover` has captured a real response, add it as
a test fixture.

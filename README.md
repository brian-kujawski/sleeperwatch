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

**Browser.** sleeperwatch uses a browser you already have installed: Google
Chrome by default, falling back to Chromium, then Edge. It starts that browser
normally and attaches to it, rather than letting Playwright launch it in
automation mode, because Amtrak's bot detection returns 403 to
automation-launched browsers even when a person is clicking. Use
`--channel msedge` to prefer Edge, or `--browser /path/to/chrome` to pick a
specific executable. `--channel chromium` forces Playwright's bundled
Chromium, which gets blocked more often. Installing Google Chrome
is the most reliable option.

**Linux notes.**
- Snap or Flatpak Chromium works. Sandboxed browsers can't write to
  hidden folders, so their profile lives under `~/snap/...` or `~/.var/app/...`
  instead, and sleeperwatch says so when it starts.
- If Playwright's bundled Chromium complains about missing libraries, run
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

A browser window opens on Amtrak's home page. Search for one of your
sleeper routes by hand. For each search, sleeperwatch records the request
Amtrak's page sent and checks its shape against what `fetch` sends. It then
replays the request from inside the page, the same way `fetch` does,
because the page navigates away before the original response can be read.
The result goes in the `captures` folder, and the terminal prints the trains
it parsed plus one raw room-fare entry. If
the room names show up (e.g. `Roomette Value $480`), you're set. If they're
blank, find the field holding the room name in the sample entry, add it to
`_accommodation_name()` in `sleeperwatch/amtrak.py`, and run
`sleeperwatch reparse` to fix any rows already stored.

### How Amtrak prices sleepers (from live captures)

A search returns one sleeper fare per train: travel class `Sleeper`, fare
family `NA` (shown as "Standard"), priced for the cheapest room available,
normally a Roomette. The price is split into rail fare and room charge.
Other room types such as Bedrooms appear only with a count of rooms left and
a $0 price.

When you pick a Bedroom on the site, it asks for that room's price
separately, at `/dotcom/journey-solution-option/adhoc-fare`. That request
refers back to the search it follows: a cache ID and the train's IDs, which
change on every search. `fetch` sends the same request for each unpriced
room on your watched trains. That's one extra request per room type, per
train, per date; turn it off with `price_rooms = false` on a watch. Room
counts are stored in `room_availability` as well. This currently works for
direct (single-train) trips only. The site's request for a connecting trip
hasn't been captured yet.

`sleeperwatch discover --probe-rooms` runs that same Bedroom pricing after
each search you make by hand, so you can check it against the live site.
discover also saves any other Amtrak API call the site makes (cart and so on)
as an `other-*.json` file in the captures folder.

## Day-to-day

1. Edit the watchlist: for each watch, a label of your choosing (`name`), the
   stations, dates or a date range, and how many travelers. By default only
   single-train trips are kept: connections and anything with a bus segment
   are skipped (`direct_only = true`). That makes `trains` optional; use it
   only to narrow to particular train numbers.
2. `sleeperwatch fetch --dry-run`: lists what would be checked.
3. `sleeperwatch fetch`: checks everything once. A browser window opens and closes.
4. `sleeperwatch report`: latest, lowest and highest price per train, room and fare.
   `sleeperwatch dashboard` builds an interactive page from the database and
   opens it in your browser: the cheapest departure, price by travel date with
   one line per train (Room → Any shows every room type: Roomettes solid,
   Bedrooms dashed), every departure ranked by average price, and each
   departure's price history when you select it. It's a single offline HTML
   file in the data folder; re-run the command after a `fetch` to refresh it.
5. `sleeperwatch plot`: one PNG per train and date, showing price against days before departure.
6. `sleeperwatch export --out fares.csv`: writes everything to CSV for a spreadsheet.

**Which train number is which?** Every search, from `fetch` or `discover`,
records each direct train it sees in a catalog: number, the name Amtrak
displays, route and departure time. `sleeperwatch trains` lists the trains
with sleepers, and `--all` includes the rest. Add `--scan-captures` once to
import trains from earlier `discover` runs. For example:

```
train  name                route     departs  sleeper  first seen  last seen
49     Lake Shore Limited  NYP->CHI  15:40    yes      2026-10-14  2026-10-14
```

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
detection, which blocks plain HTTP clients and browsers launched in
automation mode. So sleeperwatch starts your installed browser normally on a
private profile, attaches to it, loads amtrak.com to clear the bot check,
and runs each search from inside that page. The profile keeps the cookies
between runs.

**After a 403, reset the profile.** Once Akamai flags a session, it
remembers through cookies in the browser profile, and every later run stays
blocked even if nothing else is wrong. Run `sleeperwatch discover --reset-profile`
(or `fetch --reset-profile`) to start clean.

If 403s continue after a reset, first check that a search
works in your everyday browser on the same network. If it fails there too,
the block is on your IP or Amtrak is having trouble, not sleeperwatch.

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

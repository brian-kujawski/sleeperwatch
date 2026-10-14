"""Talks to Amtrak's booking backend from inside a real browser.

Amtrak has no public fare API. The booking page posts to an internal JSON
endpoint, /dotcom/journey-solution-option, and the response carries every
train for the day with all of its fares, including sleeper rooms.

The site sits behind Akamai Bot Manager, so a plain HTTP client gets
tarpitted. Instead we open a Playwright browser with a *persistent profile*
(cookies survive between your ad hoc runs), load amtrak.com so the bot
sensor clears the session, and then issue each search with fetch() from
inside that page.

If Amtrak changes the request shape, run `sleeperwatch discover`,
do a search by hand in the window that opens, and compare the captured
request with build_payload() below.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable

BASE = "https://www.amtrak.com"
ENDPOINT = "/dotcom/journey-solution-option"
SEARCH_PAGE = f"{BASE}/tickets/departure.html"

FARE_FAMILIES = {
    "SAL": "Sale",
    "SVR": "Saver",
    "VLU": "Value",
    "FLX": "Flexible",
    "PRE": "Premium",
    "COR": "Corporate",
    "NA": "Standard",
}

# Keys we try, in order, to find a human name for an accommodation (e.g.
# "Roomette", "Bedroom"). The response shape for sleepers is not publicly
# documented, so the parser looks in several plausible places and the
# `discover` command dumps the real structure so this list can be tightened.
_NAME_KEYS = (
    "accommodationName", "accommodationType", "roomType", "displayName",
    "name", "description", "productName", "type", "code",
)


class BlockedError(RuntimeError):
    """Amtrak refused the request (bot challenge, rate limit or API error)."""


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def trace_id() -> str:
    # The SPA sends a random hex trace id with every call.
    return "%032x%d0" % (random.getrandbits(128), int(time.time() * 1000))


def build_payload(origin: str, destination: str, date: str, passengers: int = 1) -> dict:
    """One-way search body. `date` is YYYY-MM-DD."""
    return {
        "journeyRequest": {
            "fare": {"pricingUnit": "DOLLARS"},
            "type": "OW",
            "journeyLegRequests": [{
                "origin": {
                    "code": origin,
                    "schedule": {"departureDateTime": f"{date}T00:00:00"},
                },
                "destination": {"code": destination},
                "passengers": [
                    {"id": f"P{i + 1}", "type": "F", "initialType": "adult"}
                    for i in range(passengers)
                ],
            }],
            "customer": {"tierStatus": "MEMBER"},
            "isPassRider": False,
            "isCorporateTraveller": False,
            "tripTags": True,
            "singleAdultFare": passengers == 1,
            "cascadesWSDOTFilter": False,
            "xDelay": "60",
        },
        "initialJourneyLegOnly": False,
        "reservableAccomodationOptions": "ALL",  # sic; includes rooms
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass
class Fare:
    travel_class: str          # Coach / Business / Private Rooms / ...
    accommodation: str         # Roomette / Bedroom / ... ("" if unknown)
    fare_family: str           # Value / Flexible / ...
    family_code: str
    price: float               # total for the party, USD
    seats_left: int | None     # inventory left in this bucket, if reported


@dataclass
class TrainOption:
    train_number: str
    train_name: str
    depart: str                # ISO local datetime
    arrive: str
    duration_seconds: int
    fares: list[Fare] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _first_str(obj: Any, keys=_NAME_KEYS) -> str:
    if not isinstance(obj, dict):
        return ""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _accommodation_name(accom: dict) -> str:
    """Best-effort room/seat type name for one reservable accommodation."""
    direct = _first_str(accom, keys=(
        "accommodationName", "accommodationType", "roomType", "displayName",
        "accommodationDescription",
    ))
    if direct:
        return direct
    for tla in accom.get("travelLegAccommodations") or []:
        product = tla.get("reservableProduct") or {}
        for candidate in (product, product.get("accommodation"), tla.get("accommodation"), tla):
            name = _first_str(candidate)
            if name:
                return name
    return ""


def parse(raw: dict) -> list[TrainOption]:
    """Flatten the endpoint's response into train options with fares."""
    data = raw.get("data") or {}
    solution = data.get("journeySolutionOption") or {}
    legs = solution.get("journeyLegs") or []
    if not legs:
        return []

    out: list[TrainOption] = []
    for opt in legs[0].get("journeyLegOptions") or []:
        if opt.get("isCancelled"):
            continue
        travel_legs = opt.get("travelLegs") or []
        if not travel_legs:
            continue
        # Multi-segment itineraries list several services; join their numbers.
        services = [(tl.get("travelService") or {}) for tl in travel_legs]
        number = "+".join(str(s.get("number") or "?") for s in services)
        name = " / ".join(dict.fromkeys(s.get("name") or "Amtrak" for s in services))

        fares: list[Fare] = []
        for accom in opt.get("reservableAccommodations") or []:
            total = (((accom.get("accommodationFare") or {}).get("dollarsAmount") or {}).get("total"))
            try:
                price = float(total)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            seats = None
            for tla in accom.get("travelLegAccommodations") or []:
                inv = (tla.get("reservableProduct") or {}).get("availableInventory")
                if isinstance(inv, int):
                    seats = inv if seats is None else min(seats, inv)
            code = accom.get("fareFamily") or "NA"
            fares.append(Fare(
                travel_class=accom.get("travelClass") or "Unknown",
                accommodation=_accommodation_name(accom),
                fare_family=FARE_FAMILIES.get(code, code),
                family_code=code,
                price=price,
                seats_left=seats,
            ))

        out.append(TrainOption(
            train_number=number,
            train_name=name,
            depart=((opt.get("origin") or {}).get("schedule") or {}).get("departureDateTime", ""),
            arrive=((opt.get("destination") or {}).get("schedule") or {}).get("arrivalDateTime", ""),
            duration_seconds=int(opt.get("elapsedSeconds") or 0),
            fares=fares,
        ))
    out.sort(key=lambda o: o.depart)
    return out


def is_sleeper(fare: Fare) -> bool:
    text = f"{fare.travel_class} {fare.accommodation}".lower()
    return any(w in text for w in ("room", "bedroom", "sleeper", "private"))


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------


_FETCH_JS = """
async ({endpoint, payload, traceId, timeoutMs}) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
        const r = await fetch(endpoint, {
            method: 'POST', signal: ctl.signal,
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
                'x-amtrak-trace-id': traceId,
            },
            body: JSON.stringify(payload),
        });
        return {status: r.status, body: await r.text()};
    } catch (e) {
        return {status: 0, body: '', error: String(e)};
    } finally {
        clearTimeout(timer);
    }
}"""


class BrowserSession:
    """A browser page on amtrak.com that has cleared the bot check.

    Uses a persistent profile directory so clearance cookies carry over
    between runs. `channel="chrome"` uses your installed Google Chrome,
    which bot detection tends to like better than bundled Chromium.
    """

    def __init__(self, profile_dir: Path, headless: bool = False,
                 channel: str | None = "chrome", log: Callable[[str], None] = print):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        # "chromium" means Playwright's bundled browser (no channel).
        self.channel = None if channel in (None, "", "chromium") else channel
        self.log = log
        self._pw = None
        self._ctx = None
        self.page = None

    def __enter__(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SystemExit("Playwright is not installed. Run:  pip install playwright && "
                             "python -m playwright install chromium")
        from .paths import has_display

        if not self.headless and not has_display():
            raise SystemExit(
                "No display found (no DISPLAY or WAYLAND_DISPLAY), so a browser window can't open.\n"
                "Either run under a virtual display:   xvfb-run -a sleeperwatch fetch\n"
                "or try headless mode (blocked more often):  sleeperwatch fetch --headless")

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(channel=self.channel, **kwargs)
        except Exception as e:  # Chrome not installed -> bundled Chromium
            if self.channel:
                self.log(f"  could not launch channel {self.channel!r} ({e.__class__.__name__}); using bundled Chromium")
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
            else:
                raise
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(60_000)
        return self

    def __exit__(self, *exc) -> None:
        for closer in (lambda: self._ctx and self._ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    # -- bot clearance ------------------------------------------------------

    def clearance(self) -> str:
        """Akamai's verdict from its _abck cookie: cleared / not-cleared / ..."""
        try:
            for c in self._ctx.cookies(BASE):
                if c["name"] == "_abck":
                    if "~0~" in c["value"]:
                        return "cleared"
                    if "~-1~" in c["value"]:
                        return "not-cleared"
                    return "unknown"
        except Exception:
            pass
        return "absent"

    def warm_up(self) -> None:
        page = self.page
        page.goto(f"{BASE}/home.html", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        for x, y in ((420, 330), (760, 470), (980, 300), (600, 600)):
            try:
                page.mouse.move(x, y, steps=10)
                page.wait_for_timeout(200)
            except Exception:
                pass
        try:
            page.mouse.wheel(0, 450)
            page.wait_for_timeout(500)
            page.mouse.wheel(0, -250)
        except Exception:
            pass
        page.goto(SEARCH_PAGE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        for i in range(6):
            state = self.clearance()
            self.log(f"  bot clearance: {state}")
            if state == "cleared":
                return
            page.wait_for_timeout(2500)
            try:
                page.mouse.move(500 + i * 35, 420, steps=8)
            except Exception:
                pass
        self.log("  continuing without confirmed clearance")

    # -- search -------------------------------------------------------------

    def search_raw(self, origin: str, destination: str, date: str,
                   passengers: int = 1, attempts: int = 3) -> dict:
        """Return the raw JSON response for one origin/destination/date."""
        last = "no attempt"
        for attempt in range(attempts):
            res = self.page.evaluate(_FETCH_JS, {
                "endpoint": ENDPOINT,
                "payload": build_payload(origin, destination, date, passengers),
                "traceId": trace_id(),
                "timeoutMs": 45_000,
            })
            if res["status"] == 200:
                body = json.loads(res["body"])
                errors = body.get("errors") or []
                if errors:
                    msg = errors[0].get("sysMessage") or errors[0].get("message") or "endpoint error"
                    raise BlockedError(msg)
                return body
            last = res.get("error") or f"HTTP {res['status']}"
            self.log(f"    attempt {attempt + 1}/{attempts} failed: {last} (clearance: {self.clearance()})")
            if attempt + 1 < attempts:
                self.page.wait_for_timeout(4000)
                try:
                    self.warm_up()
                except Exception:
                    pass
        raise BlockedError(f"search failed: {last}")


def discover(profile_dir: Path, out_dir: Path, channel: str | None = "chrome") -> None:
    """Open a visible browser, let the user search by hand, and save every
    journey-solution-option request/response so the payload and the sleeper
    response structure can be inspected."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with BrowserSession(profile_dir, headless=False, channel=channel) as s:
        count = {"n": 0}

        def on_response(resp):
            if ENDPOINT not in resp.url:
                return
            count["n"] += 1
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                req_body = resp.request.post_data_json
            except Exception:
                req_body = resp.request.post_data
            try:
                body = resp.json()
            except Exception:
                body = {"_text": resp.text()[:5000]}
            path = out_dir / f"capture-{stamp}-{count['n']}.json"
            path.write_text(json.dumps({
                "url": resp.url, "status": resp.status,
                "request_headers": resp.request.headers,
                "request_body": req_body, "response": body,
            }, indent=2), encoding="utf-8")
            print(f"  saved {path} (HTTP {resp.status})")
            if isinstance(body, dict):
                summarize_accommodations(body)

        s.page.on("response", on_response)
        s.page.goto(SEARCH_PAGE, wait_until="domcontentloaded")
        print("\nSearch for a sleeper route in the browser window.")
        print("Every fare search is captured. Close the browser window when done.\n")
        try:
            s.page.wait_for_event("close", timeout=0)
        except KeyboardInterrupt:
            print("\nStopped.")
        except Exception:
            pass


def summarize_accommodations(body: dict) -> None:
    """Print what the parser sees, plus the raw keys of one room fare, so the
    accommodation-name lookup can be checked against reality."""
    options = parse(body)
    print(f"  parsed {len(options)} trains")
    for o in options:
        rooms = [f for f in o.fares if is_sleeper(f)]
        if rooms:
            print(f"   #{o.train_number} {o.depart}: " + ", ".join(
                f"{f.accommodation or f.travel_class} {f.fare_family} ${f.price:.0f}" for f in rooms))
    legs = (((body.get("data") or {}).get("journeySolutionOption") or {}).get("journeyLegs") or [])
    for opt in (legs[0].get("journeyLegOptions") if legs else []) or []:
        for accom in opt.get("reservableAccommodations") or []:
            if "room" in str(accom.get("travelClass", "")).lower() or "private" in str(accom.get("travelClass", "")).lower():
                print("  sample room accommodation (truncated):")
                print("   " + json.dumps(accom, indent=1)[:2500].replace("\n", "\n   "))
                return

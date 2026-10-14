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
HOME_PAGE = f"{BASE}/home.html"            # has the search form
RESULTS_PAGE = f"{BASE}/tickets/departure.html"  # "Select Your Trip"; empty without a search

FARE_FAMILIES = {
    "SAL": "Sale",
    "SVR": "Saver",
    "VLU": "Value",
    "FLX": "Flexible",
    "PRE": "Premium",
    "COR": "Corporate",
    "NA": "Standard",
}



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
    travel_class: str          # Coach / Business / First / Sleeper
    accommodation: str         # product name: Coach Seat / Roomette / Bedroom / ...
    fare_family: str           # Value / Flexible / Standard / ...
    family_code: str           # VLU / FLX / NA / ...
    price: float               # total for the party, USD (rail + accommodation)
    seats_left: int | None     # units of this product left (rooms, for sleepers)
    rail: float | None = None           # rail-fare part of price
    accommodation_charge: float | None = None  # room charge part of price


@dataclass
class RoomAvailability:
    """A room type Amtrak lists as available but doesn't price in this
    search (e.g. Bedrooms alongside the priced Roomette)."""
    name: str
    available: int | None


@dataclass
class TrainOption:
    train_number: str
    train_name: str
    depart: str                # ISO local datetime
    arrive: str
    duration_seconds: int
    fares: list[Fare] = field(default_factory=list)
    unpriced_rooms: list[RoomAvailability] = field(default_factory=list)
    segments: int = 1          # vehicles in the itinerary; >1 means a connection
    all_rail: bool = True      # False if any segment isn't a train (e.g. a Thruway bus)

    @property
    def is_direct_train(self) -> bool:
        return self.segments == 1 and self.all_rail

    def to_dict(self) -> dict:
        return asdict(self)


def _money(d: dict | None, key: str) -> float | None:
    try:
        return float(((d or {}).get("dollarsAmount") or {}).get(key))
    except (TypeError, ValueError):
        return None


def _main_product(accom: dict) -> dict:
    """The product that gives this fare its class. On a connecting trip each
    leg has its own product (e.g. an Acela First Class Seat, then a Floridian
    Roomette); the one whose rbdType matches the fare's travelClass is the
    one that matters."""
    products = [(tla.get("reservableProduct") or {}) for tla in accom.get("travelLegAccommodations") or []]
    products = [p for p in products if p]
    cls = (accom.get("travelClass") or "").lower()
    for prod in products:
        if (prod.get("rbdType") or "").lower() == cls:
            return prod
    return products[0] if products else {}


def parse(raw: dict) -> list[TrainOption]:
    """Flatten the endpoint's response into train options with fares.

    Response shape (confirmed against a live capture, 2026-10-14):
    data.journeySolutionOption.journeyLegs[0].journeyLegOptions[] -> one per
    itinerary, with travelLegs[] (one per train) and reservableAccommodations[]
    (one per class x fare family). A sleeper fare is travelClass "Sleeper",
    fareFamily "NA", priced for the cheapest room (normally a Roomette); other
    room types appear only under additionalProducts with a $0 fare.
    """
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
        unpriced: dict[str, RoomAvailability] = {}
        for accom in opt.get("reservableAccommodations") or []:
            fare = accom.get("accommodationFare")
            price = _money(fare, "total")
            if price is None or price <= 0:
                continue
            prod = _main_product(accom)
            inv = prod.get("availableInventory")
            code = accom.get("fareFamily") or "NA"
            fares.append(Fare(
                travel_class=accom.get("travelClass") or "Unknown",
                accommodation=(prod.get("name") or "").strip(),
                fare_family=FARE_FAMILIES.get(code, code),
                family_code=code,
                price=price,
                seats_left=inv if isinstance(inv, int) else None,
                rail=_money(fare, "rail"),
                accommodation_charge=_money(fare, "accommodation"),
            ))
            for tla in accom.get("travelLegAccommodations") or []:
                for extra in tla.get("additionalProducts") or []:
                    if not _money(extra.get("additionalProductFare"), "total"):
                        nm = (extra.get("name") or "").strip()
                        avail = extra.get("availableInventory")
                        if nm and nm not in unpriced:
                            unpriced[nm] = RoomAvailability(nm, avail if isinstance(avail, int) else None)

        priced_names = {f.accommodation for f in fares}
        out.append(TrainOption(
            train_number=number,
            train_name=name,
            depart=((opt.get("origin") or {}).get("schedule") or {}).get("departureDateTime", ""),
            arrive=((opt.get("destination") or {}).get("schedule") or {}).get("arrivalDateTime", ""),
            duration_seconds=int(opt.get("elapsedSeconds") or 0),
            fares=fares,
            unpriced_rooms=[r for n, r in unpriced.items() if n not in priced_names],
            segments=len(travel_legs),
            # Amtrak marks each segment's vehicle type ("TRAIN" in captures);
            # buses use another value. Missing type is assumed to be a train.
            all_rail=all((sv.get("type") or "TRAIN").upper() == "TRAIN" for sv in services),
        ))
    out.sort(key=lambda o: o.depart)
    return out


# ---------------------------------------------------------------------------
# Pricing rooms the search leaves unpriced (Bedrooms)
# ---------------------------------------------------------------------------
#
# The search prices only the cheapest room. When you pick a Bedroom on the
# site, it POSTs to ENDPOINT + "/adhoc-fare" with a body built from the
# search response: its cacheId, the itinerary's logicalId/schedule, the
# per-search travel-leg ids, and the room's product code (rbd). Shape below
# copied from a live capture (2026-10-14); the leg ids change every search,
# so the body must be built from the search response it follows.

ADHOC_ENDPOINT = ENDPOINT + "/adhoc-fare"
ADHOC_KEY = "_sleeperwatch_adhoc"   # where fetch stores room-price responses in a raw search body


@dataclass
class UnpricedRoom:
    train_number: str
    room: str
    request: dict


def adhoc_requests(raw: dict, search_payload: dict) -> list[UnpricedRoom]:
    """One adhoc-fare request per (single-train itinerary, unpriced room).

    Connecting itineraries are skipped: the site's request for those hasn't
    been captured, so its shape is unknown."""
    data = raw.get("data") or {}
    cache_id = (data.get("journeyRequest") or {}).get("cacheId")
    legs = ((data.get("journeySolutionOption") or {}).get("journeyLegs") or [])
    if not cache_id or not legs:
        return []
    try:
        req_passengers = search_payload["journeyRequest"]["journeyLegRequests"][0]["passengers"]
    except (KeyError, IndexError, TypeError):
        req_passengers = [{"id": "P1", "initialType": "adult"}]
    passengers = [{"id": p.get("id", f"P{i + 1}"), "ageGroup": p.get("initialType", "adult"),
                   "initialType": "F", "type": "F"} for i, p in enumerate(req_passengers)]

    out: list[UnpricedRoom] = []
    for opt in legs[0].get("journeyLegOptions") or []:
        travel_legs = opt.get("travelLegs") or []
        if opt.get("isCancelled") or len(travel_legs) != 1:
            continue
        tl = travel_legs[0]
        number = str((tl.get("travelService") or {}).get("number") or "?")
        seen: set[str] = set()
        for accom in opt.get("reservableAccommodations") or []:
            for tla in accom.get("travelLegAccommodations") or []:
                for extra in tla.get("additionalProducts") or []:
                    name = (extra.get("name") or "").strip()
                    if not name or name in seen or _money(extra.get("additionalProductFare"), "total"):
                        continue
                    seen.add(name)

                    def place(p):
                        p = p or {}
                        return {"schedule": dict(p.get("schedule") or {}),
                                "code": p.get("code"), "name": p.get("name")}
                    body = {
                        "cacheId": cache_id,
                        "fare": {"pricingUnit": "DOLLARS"},
                        "journeyLegOption": {
                            "adhocPricingType": "mixedAccommodation",
                            "destination": place(opt.get("destination")),
                            "logicalId": opt.get("logicalId"),
                            "origin": place(opt.get("origin")),
                            "reservableAccommodations": [{
                                "accommodationFare": {"pricingUnit": "DOLLARS"},
                                "category": accom.get("category") or "RG",
                                "travelLegAccommodations": [{
                                    "passengers": passengers,
                                    "reservableProduct": {
                                        "count": 1,
                                        "rbd": extra.get("rbd"),
                                        "name": name,
                                        "rbdType": extra.get("rbdType") or accom.get("travelClass"),
                                        "subClass": accom.get("fareFamily") or "NA",
                                    },
                                    "travelLegId": tla.get("travelLegId") or tl.get("id"),
                                }],
                            }],
                            "travelLegs": [{"id": tl.get("id"), "travelService": {"number": number}}],
                        },
                    }
                    # The site sends destination.schedule with only arrivalDateTime
                    # and origin.schedule with only departureDateTime.
                    body["journeyLegOption"]["destination"]["schedule"] = {
                        "arrivalDateTime": (opt.get("destination") or {}).get("schedule", {}).get("arrivalDateTime")}
                    body["journeyLegOption"]["origin"]["schedule"] = {
                        "departureDateTime": (opt.get("origin") or {}).get("schedule", {}).get("departureDateTime")}
                    out.append(UnpricedRoom(number, name, body))
    return out


def fares_in(obj: Any) -> list[Fare]:
    """Every priced accommodation anywhere in a response.

    Used for adhoc-fare responses, whose shape hasn't been seen yet; it looks
    for the same accommodationFare / travelLegAccommodations structure the
    search response uses, wherever it appears."""
    found: list[Fare] = []

    def walk(o):
        if isinstance(o, dict):
            if "accommodationFare" in o and "travelLegAccommodations" in o:
                price = _money(o.get("accommodationFare"), "total")
                if price and price > 0:
                    prod = _main_product(o)
                    if not prod:
                        for tla in o.get("travelLegAccommodations") or []:
                            prod = tla.get("selectedProduct") or {}
                            if prod:
                                break
                    inv = prod.get("availableInventory")
                    code = o.get("fareFamily") or "NA"
                    found.append(Fare(
                        travel_class=o.get("travelClass") or prod.get("rbdType") or "Unknown",
                        accommodation=(prod.get("name") or "").strip(),
                        fare_family=FARE_FAMILIES.get(code, code), family_code=code, price=price,
                        seats_left=inv if isinstance(inv, int) else None,
                        rail=_money(o.get("accommodationFare"), "rail"),
                        accommodation_charge=_money(o.get("accommodationFare"), "accommodation"),
                    ))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found


def adhoc_fares(raw: dict) -> dict[str, list[Fare]]:
    """Train number -> room fares from the adhoc responses fetch stored in a
    raw search body (only the room that was asked about is kept)."""
    out: dict[str, list[Fare]] = {}
    for entry in raw.get(ADHOC_KEY) or []:
        wanted = (entry.get("room") or "").lower()
        fares = [f for f in fares_in(entry.get("response")) if f.accommodation.lower() == wanted]
        if fares:
            out.setdefault(str(entry.get("train_number")), []).extend(fares[:1])
    return out


def is_sleeper(fare: Fare) -> bool:
    text = f"{fare.travel_class} {fare.accommodation}".lower()
    return any(w in text for w in ("sleeper", "roomette", "bedroom"))


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------


_FETCH_JS = """
async ({endpoint, payload, traceId, timeoutMs, extraHeaders}) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
        // Headers is case-insensitive and set() replaces, so a header passed
        // in extraHeaders (e.g. 'content-type') overrides the default instead
        // of being sent twice. Amtrak rejects a doubled Content-Type.
        // These defaults mirror what Amtrak's own page sends, including its
        // literal 'csrf-token: undefined'.
        const headers = new Headers({
            'content-type': 'application/json',
            'accept': 'application/json, text/plain, */*',
            'csrf-token': 'undefined',
        });
        for (const [k, v] of Object.entries(extraHeaders || {})) headers.set(k, v);
        headers.set('x-amtrak-trace-id', traceId);
        const r = await fetch(endpoint, {
            method: 'POST', signal: ctl.signal, headers,
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

    Default ("attach") mode: start your installed Chrome/Chromium/Edge as an
    ordinary process with only a remote-debugging port added, then attach
    to it. A browser *launched by* Playwright carries automation markers
    (--enable-automation, a test-build fingerprint) that Akamai rejects
    outright, even when a human is the one clicking. An ordinary browser
    that Playwright merely attaches to looks far more like a person.

    Fallback ("playwright") mode, used with --channel chromium or when no
    installed browser is found: Playwright launches its bundled Chromium.
    Expect more 403s that way.

    Either way a persistent profile directory keeps cookies between runs.
    """

    def __init__(self, profile_dir: Path, headless: bool = False,
                 channel: str | None = "chrome", executable: str | None = None,
                 reset_profile: bool = False, log: Callable[[str], None] = print):
        self.profile_dir = Path(profile_dir)
        self.reset_profile = reset_profile
        self.headless = headless
        self.channel = channel or "chrome"
        self.executable = executable
        self.log = log
        self._pw = None
        self._browser = None
        self._proc = None
        self._ctx = None
        self.page = None

    def __enter__(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SystemExit("Playwright is not installed. Run:  pip install playwright && "
                             "python -m playwright install chromium")
        from . import browsers
        from .paths import has_display

        if not self.headless and not has_display():
            raise SystemExit(
                "No display found (no DISPLAY or WAYLAND_DISPLAY), so a browser window can't open.\n"
                "Either run under a virtual display:   xvfb-run -a sleeperwatch fetch\n"
                "or try headless mode (blocked more often):  sleeperwatch fetch --headless")

        self._pw = sync_playwright().start()
        exe = None
        if self.channel != "chromium":
            exe = self.executable or browsers.find_browser(self.channel)
            if not exe:
                self.log(f"  no installed {self.channel} browser found "
                         f"(looked for: {', '.join(browsers.candidate_names(self.channel))}).")
                self.log("  falling back to Playwright's bundled Chromium, which Amtrak blocks more often. "
                         "Install Google Chrome, or point at a browser with --browser PATH.")
        if exe:
            self._attach(exe, browsers)
        else:
            self._launch_bundled()
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(60_000)
        return self

    def _attach(self, exe: str, browsers) -> None:
        profile = browsers.profile_dir_for(exe, self.profile_dir)
        if profile != self.profile_dir:
            self.log(f"  sandboxed browser detected; using profile at {profile}")
        self._maybe_reset(profile)
        self.log(f"  starting {exe}")
        self._proc, endpoint = browsers.start_with_debug_port(exe, profile, headless=self.headless)
        try:
            self._browser = self._pw.chromium.connect_over_cdp(endpoint)
        except Exception:
            browsers.stop(self._proc)
            raise
        self._ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()

    def _maybe_reset(self, profile: Path) -> None:
        """Delete the browser profile. Akamai's cookies remember a session it
        has flagged, so after a block every later run is blocked too until
        the profile is cleared."""
        if self.reset_profile and profile.exists():
            import shutil
            shutil.rmtree(profile)
            self.log(f"  cleared browser profile {profile}")

    def _launch_bundled(self) -> None:
        self._maybe_reset(self.profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    def alive(self) -> bool:
        """False once the user has closed the window (or the browser died)."""
        if self._proc is not None and self._proc.poll() is not None:
            return False
        try:
            return not self.page.is_closed()
        except Exception:
            return False

    def __exit__(self, *exc) -> None:
        from . import browsers

        closers = []
        if self._browser is not None:
            closers.append(self._browser.close)       # disconnects from the attached browser
        elif self._ctx is not None:
            closers.append(self._ctx.close)
        if self._proc is not None:
            closers.append(lambda: browsers.stop(self._proc))
        if self._pw is not None:
            closers.append(self._pw.stop)
        for closer in closers:
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
        page.goto(HOME_PAGE, wait_until="domcontentloaded")
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
        page.goto(RESULTS_PAGE, wait_until="domcontentloaded")
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

    def post_raw(self, payload: dict, extra_headers: dict | None = None,
                 endpoint: str = ENDPOINT) -> dict:
        """POST a payload to an Amtrak endpoint from inside the page.
        Returns {status, body, error?}; body is the response text."""
        return self.page.evaluate(_FETCH_JS, {
            "endpoint": endpoint,
            "payload": payload,
            "traceId": trace_id(),
            "timeoutMs": 45_000,
            "extraHeaders": extra_headers or {},
        })

    def search_raw(self, origin: str, destination: str, date: str,
                   passengers: int = 1, attempts: int = 3) -> dict:
        """Return the raw JSON response for one origin/destination/date."""
        last = "no attempt"
        payload = build_payload(origin, destination, date, passengers)
        for attempt in range(attempts):
            res = self.post_raw(payload)
            if res["status"] == 200:
                body = json.loads(res["body"])
                errors = body.get("errors") or []
                if errors:
                    msg = errors[0].get("sysMessage") or errors[0].get("message") or "endpoint error"
                    raise BlockedError(msg)
                return body
            last = res.get("error") or f"HTTP {res['status']}"
            if res["status"] == 403:
                last += " (bot check; if this persists, retry with --reset-profile)"
            self.log(f"    attempt {attempt + 1}/{attempts} failed: {last} (clearance: {self.clearance()})")
            if attempt + 1 < attempts:
                self.page.wait_for_timeout(4000)
                try:
                    self.warm_up()
                except Exception:
                    pass
        raise BlockedError(f"search failed: {last}")

    def price_rooms(self, raw: dict, payload: dict, want=lambda train, room: True,
                    gap_ms: int = 2000) -> list[dict]:
        """Ask Amtrak for the price of each room the search left unpriced
        (Bedrooms), for the trains/rooms `want` accepts. Returns entries to
        store under ADHOC_KEY in the raw search body. Failures are recorded
        in the entry rather than raised, so one bad room doesn't lose the
        whole search."""
        entries = []
        for r in adhoc_requests(raw, payload):
            if not want(r.train_number, r.room):
                continue
            self.page.wait_for_timeout(gap_ms)  # be polite between requests
            res = self.post_raw(r.request, endpoint=ADHOC_ENDPOINT)
            entry = {"train_number": r.train_number, "room": r.room, "status": res["status"]}
            try:
                entry["response"] = json.loads(res["body"])
            except (ValueError, TypeError):
                entry["response"] = None
                entry["error"] = res.get("error") or (res.get("body") or "")[:300]
            if isinstance(entry["response"], dict) and entry["response"].get("errors"):
                entry["error"] = entry["response"]["errors"][0].get("sysMessage")
            entries.append(entry)
        return entries


# Request headers the browser manages itself; never copied into a replay.
_BROWSER_HEADERS = {
    "accept-encoding", "connection", "content-length", "cookie", "host", "origin",
    "referer", "user-agent", "x-amtrak-trace-id",
}


def _replay_headers(headers: dict) -> dict:
    return {k: v for k, v in (headers or {}).items()
            if k.lower() not in _BROWSER_HEADERS and not k.lower().startswith(("sec-", ":"))}


def _key_paths(obj, prefix="") -> set[str]:
    """Structural keys of a JSON value (list items collapsed to [])."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            out.add(path)
            out |= _key_paths(v, path)
    elif isinstance(obj, list):
        for v in obj:
            out |= _key_paths(v, prefix + "[]")
    return out


def compare_payload(captured: dict) -> list[str]:
    """Differences in *shape* between the site's own request and ours."""
    try:
        leg = captured["journeyRequest"]["journeyLegRequests"][0]
        ours = build_payload(
            leg["origin"]["code"], leg["destination"]["code"],
            leg["origin"]["schedule"]["departureDateTime"][:10], len(leg.get("passengers") or [1]))
    except (KeyError, IndexError, TypeError):
        return ["could not read origin/destination/date from the captured request"]
    theirs_k, ours_k = _key_paths(captured), _key_paths(ours)
    return ([f"site sends, we don't: {k}" for k in sorted(theirs_k - ours_k)] +
            [f"we send, site doesn't: {k}" for k in sorted(ours_k - theirs_k)])


def priced_rooms(raw: dict) -> dict[str, list[str]]:
    """Train number -> priced sleeper products, e.g. {'49': ['Roomette $826']}."""
    out: dict[str, list[str]] = {}
    for o in parse(raw):
        rooms = [f"{f.accommodation} {f.fare_family} ${f.price:.0f}" for f in o.fares if is_sleeper(f)]
        if rooms:
            out[o.train_number] = rooms
    return out


def _path(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).path.rstrip("/")


def discover(profile_dir: Path, out_dir: Path, channel: str | None = "chrome",
             executable: str | None = None, reset_profile: bool = False,
             probe_rooms: bool = False) -> None:
    """Open a visible browser, let the user search by hand, and save what
    Amtrak's fare endpoints were asked and answered.

    Amtrak's page fires its requests and then navigates or re-renders, which
    makes the browser drop response bodies before they can be read. So each
    request (which survives) is recorded as sent and then replayed from
    inside the page to its own endpoint, the same way `fetch` works.

    With probe_rooms, each search is followed by sleeperwatch's own
    adhoc-fare requests for the rooms the search left unpriced (Bedrooms),
    exactly as `fetch` will send them.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    fare_paths = {ENDPOINT, ADHOC_ENDPOINT}
    with BrowserSession(profile_dir, headless=False, channel=channel, executable=executable,
                        reset_profile=reset_profile) as s:
        pending: list[dict] = []
        other_calls: list[dict] = []
        # Our own replays hit the same endpoints; remember them so they
        # aren't captured and replayed again in an endless loop.
        own_replays: list[str] = []

        def canon(body) -> str:
            return json.dumps(body, sort_keys=True) if isinstance(body, (dict, list)) else str(body)

        def request_body(resp):
            try:
                return resp.request.post_data_json
            except Exception:
                return resp.request.post_data

        def on_response(resp):
            # Keep this handler cheap and failure-proof; real work happens in
            # the main loop once the page has settled. Deliberately no
            # resp.body(): after Amtrak's navigation it errors or hangs.
            if resp.request.method != "POST" or "/dotcom/" not in resp.url:
                return
            body = request_body(resp)
            if _path(resp.url) not in fare_paths:
                # Some other Amtrak API call (cart, ancillaries, ...). Record
                # the request so we can learn what it does.
                other_calls.append({"url": resp.url, "status": resp.status,
                                    "request_headers": dict(resp.request.headers), "request_body": body})
                return
            key = canon(body)
            if key in own_replays:
                own_replays.remove(key)
                return
            pending.append({"url": resp.url, "endpoint": _path(resp.url), "status": resp.status,
                            "request_headers": dict(resp.request.headers), "request_body": body,
                            "response": None, "response_source": None})

        def replay(item: dict) -> None:
            try:
                s.page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            own_replays.append(canon(item["request_body"]))
            res = s.post_raw(item["request_body"], _replay_headers(item["request_headers"]),
                             endpoint=item["endpoint"])
            item["replay_status"] = res["status"]
            print(f"  replayed from inside the page: HTTP {res['status']}"
                  + (f" ({res.get('error')})" if res.get("error") else ""))
            try:
                item["response"] = json.loads(res["body"])
                item["response_source"] = "replayed"
            except (ValueError, TypeError):
                item["response"] = {"_text": (res.get("body") or "")[:5000]}

        def report_errors(body) -> bool:
            errs = body.get("errors") if isinstance(body, dict) else None
            for err in errs or []:
                print(f"  Amtrak returned an error: {err.get('sysMessage') or err.get('businessMessage') or err}")
            return bool(errs)

        def save(item: dict, prefix: str) -> Path:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = out_dir / f"{prefix}-{stamp}.json"
            n = 1
            while path.exists():
                n += 1
                path = out_dir / f"{prefix}-{stamp}-{n}.json"
            path.write_text(json.dumps(item, indent=2), encoding="utf-8")
            print(f"  saved {path}")
            return path

        def process_search(item: dict) -> None:
            print(f"\n  site's search: HTTP {item['status']}")
            if item["status"] == 403:
                print("  -> blocked by Amtrak's bot detection. Close the window and rerun with "
                      "`sleeperwatch discover --reset-profile`.")
            req = item["request_body"]
            if isinstance(req, dict):
                diffs = compare_payload(req)
                print("  request shape matches build_payload()" if not diffs
                      else "  request shape differs from build_payload():\n    " + "\n    ".join(diffs))
            if isinstance(req, dict) and item["status"] == 200:
                replay(item)
            body = item["response"]
            if probe_rooms and isinstance(req, dict) and isinstance(body, dict) and "data" in body:
                print("  pricing rooms the search left unpriced:")
                for r in adhoc_requests(body, req):   # don't recapture our own requests
                    own_replays.append(canon(r.request))
                entries = s.price_rooms(body, req)
                for e in entries:
                    fares = [f for f in fares_in(e.get("response")) if f.accommodation.lower() == e["room"].lower()]
                    if fares:
                        f = fares[0]
                        print(f"    #{e['train_number']} {e['room']}: ${f.price:.0f} "
                              f"(rail {f.rail or 0:.0f} + room {f.accommodation_charge or 0:.0f}, "
                              f"{f.seats_left} left)  HTTP {e['status']}")
                    else:
                        print(f"    #{e['train_number']} {e['room']}: no price found (HTTP {e['status']})"
                              + (f": {e['error']}" if e.get("error") else
                                 "; response saved, shape not recognised yet"))
                if not entries:
                    print("    (none: no single-train itinerary listed an unpriced room)")
                body[ADHOC_KEY] = entries
            save(item, "capture")
            if not report_errors(body) and isinstance(body, dict) and "data" in body:
                summarize_accommodations(body)

        def process_adhoc(item: dict) -> None:
            req = item["request_body"] if isinstance(item["request_body"], dict) else {}
            prod = (((req.get("journeyLegOption") or {}).get("reservableAccommodations") or [{}])[0]
                    .get("travelLegAccommodations") or [{}])[0].get("reservableProduct") or {}
            print(f"\n  site priced a room ({prod.get('name') or '?'}): HTTP {item['status']}")
            if req and item["status"] == 200:
                replay(item)
            save(item, "adhoc")
            if not report_errors(item["response"]):
                for f in fares_in(item["response"]):
                    print(f"    {f.travel_class} {f.accommodation} ${f.price:.0f} "
                          f"(rail {f.rail or 0:.0f} + room {f.accommodation_charge or 0:.0f}, {f.seats_left} left)")

        # Listen on the whole browser, not one tab, in case results open elsewhere.
        s._ctx.on("response", on_response)
        s.page.goto(HOME_PAGE, wait_until="domcontentloaded")
        print("\nUse the search form on the Amtrak home page to search a sleeper route")
        print("(one-way, the number of travelers you'd book for).")
        print("Every fare search is captured. Close the browser window when done.\n")
        # Keep Playwright's event loop turning (so responses get captured)
        # until the window is closed.
        try:
            while s.alive():
                s.page.wait_for_timeout(500)
                while pending:
                    item = pending.pop(0)
                    try:
                        (process_adhoc if item["endpoint"] == ADHOC_ENDPOINT else process_search)(item)
                    except Exception as e:
                        print(f"  could not process a capture: {e}")
                while other_calls:
                    call = other_calls.pop(0)
                    print(f"\n  other Amtrak API call: {_path(call['url'])} (HTTP {call['status']})")
                    save(call, "other")
        except KeyboardInterrupt:
            print("\nStopped.")
        except Exception as e:
            # Closing the window surfaces as a "target closed" error; that's
            # the normal way to finish, so only report anything else.
            if "closed" not in str(e).lower():
                print(f"  (discover stopped: {e.__class__.__name__}: {str(e).splitlines()[0][:150]})")


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

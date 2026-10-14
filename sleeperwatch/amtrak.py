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

    def post_raw(self, payload: dict, extra_headers: dict | None = None) -> dict:
        """POST a payload to the fare endpoint from inside the page.
        Returns {status, body, error?}; body is the response text."""
        return self.page.evaluate(_FETCH_JS, {
            "endpoint": ENDPOINT,
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


def discover(profile_dir: Path, out_dir: Path, channel: str | None = "chrome",
             executable: str | None = None, reset_profile: bool = False) -> None:
    """Open a visible browser, let the user search by hand, and save each
    journey-solution-option request with its response.

    Amtrak's page fires the search from the home page and then navigates to
    the results page, which makes the browser drop the response body before
    it can be read. So the request (which survives) is recorded as sent and
    then replayed from inside the page, the same way `fetch` works, to get
    the response. That also checks the fetch path against the live site.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with BrowserSession(profile_dir, headless=False, channel=channel, executable=executable,
                        reset_profile=reset_profile) as s:
        pending: list[dict] = []
        # Our own replays hit the same endpoint; remember them so they
        # aren't captured and replayed again in an endless loop.
        own_replays: list[str] = []

        def canon(body) -> str:
            return json.dumps(body, sort_keys=True) if isinstance(body, (dict, list)) else str(body)

        def on_response(resp):
            # Keep this handler cheap and failure-proof; real work happens in
            # the main loop once the page has settled.
            if ENDPOINT not in resp.url or resp.request.method != "POST":
                return
            item = {"url": resp.url, "status": resp.status,
                    "request_headers": dict(resp.request.headers), "request_body": None,
                    "response": None, "response_source": None}
            try:
                item["request_body"] = resp.request.post_data_json
            except Exception:
                item["request_body"] = resp.request.post_data
            key = canon(item["request_body"])
            if key in own_replays:
                own_replays.remove(key)
                return
            # Deliberately not reading resp.body() here: Amtrak navigates away
            # right after this response, and reading the body then either
            # errors or hangs forever. The main loop replays the request.
            pending.append(item)

        def process(item: dict) -> None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
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
                try:
                    s.page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                own_replays.append(canon(req))
                res = s.post_raw(req, _replay_headers(item["request_headers"]))
                item["replay_status"] = res["status"]
                print(f"  replayed from inside the page: HTTP {res['status']}"
                      + (f" ({res.get('error')})" if res.get("error") else ""))
                try:
                    item["response"] = json.loads(res["body"])
                    item["response_source"] = "replayed"
                except (ValueError, TypeError):
                    item["response"] = {"_text": (res.get("body") or "")[:5000]}
            path = out_dir / f"capture-{stamp}.json"
            n = 1
            while path.exists():
                n += 1
                path = out_dir / f"capture-{stamp}-{n}.json"
            path.write_text(json.dumps(item, indent=2), encoding="utf-8")
            print(f"  saved {path}")
            resp_body = item["response"]
            if isinstance(resp_body, dict):
                for err in resp_body.get("errors") or []:
                    print(f"  Amtrak returned an error: {err.get('sysMessage') or err.get('businessMessage') or err}")
                if "data" in resp_body:
                    summarize_accommodations(resp_body)

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
                    try:
                        process(pending.pop(0))
                    except Exception as e:
                        print(f"  could not process a capture: {e}")
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

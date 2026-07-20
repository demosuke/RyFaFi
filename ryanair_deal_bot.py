"""
Ryanair Deal Bot v2
--------------------
Polls Ryanair's public fare-finder endpoints every 20 minutes for flights
from a given airport - both one-way deals AND round-trip deals (there +
back) - and sends a Telegram message with a booking link when it finds a
fare at or below your thresholds that it hasn't already alerted on.

SETUP - same as before:
1. pip install "python-telegram-bot[job-queue]" requests
2. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars (or edit below).
3. Test first:
     python ryanair_deal_bot.py --debug          (one-way fares)
     python ryanair_deal_bot.py --debug-return    (round-trip fares)
   The round-trip endpoint hasn't been verified live - check the printed
   JSON actually has "outbound" and "inbound" keys before trusting it.
4. Run for real: python ryanair_deal_bot.py

TELEGRAM COMMANDS
/start                          - show current settings
/setmax 20                      - one-way deal threshold (EUR)
/setmaxreturn 40                - round-trip deal threshold (EUR, total)
/setdates 2026-08-01 2026-09-30 - date range to search
/settrip 2 10                   - min/max nights away for round trips
/settime morning lunch evening  - which times of day count (space separated,
                                   any combo of: morning, lunch, evening)
/status                         - show current settings + last check time
/check                          - force an immediate check
"""

import os
import sys
import json
import asyncio
import logging
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ryanair_deal_bot")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")
KIWI_API_KEY = os.environ.get("KIWI_API_KEY", "")  # optional - enables LOT search via Kiwi's Tequila API
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")  # optional - enables the "Find stay" hotel search feature
CHECK_INTERVAL_MINUTES = 20

STATE_FILE = Path(__file__).parent / "deal_bot_state.json"

ONEWAY_ENDPOINT = "https://services-api.ryanair.com/farfnd/v4/oneWayFares"
ROUNDTRIP_ENDPOINT = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"
KIWI_ENDPOINT = "https://api.tequila.kiwi.com/v2/search"
SERPAPI_ENDPOINT = "https://serpapi.com/search"

VALID_TIME_BUCKETS = ["morning", "lunch", "evening"]

DEFAULT_STATE = {
    "departure_airport": "WMI",
    "arrival_airport": None,  # None = anywhere
    "track_oneway": True,
    "track_roundtrip": True,
    "max_price": 20.0,
    "round_trip_max_price": 40.0,
    "date_from": date.today().isoformat(),
    "date_to": (date.today() + timedelta(days=180)).isoformat(),
    "dates_custom": False,  # if False, date range auto-rolls to "today -> +6 months" every check
    "min_nights": 2,
    "max_nights": 10,
    "time_buckets": ["morning", "lunch", "evening"],  # all enabled by default
    "lot_departure_airport": "WAW",  # LOT flies from Chopin, not Modlin
    "lot_max_price": 100.0,
    "wizzair_departure_airport": "WMI",  # Wizzair Poland is based at Modlin, like Ryanair
    "wizzair_max_price": 30.0,
    "drop_threshold": 3.0,  # re-alert if a previously-seen flight drops by at least this many EUR
    "pln_rate": None,       # cached EUR->PLN rate
    "pln_rate_updated": None,
    "seen_deals": {},        # Ryanair one-way: {dest-date: lowest price seen}
    "seen_round_trips": {},  # Ryanair round-trip: {dest-outdate-indate: lowest price seen}
    "seen_lot": {},          # LOT deals via Kiwi: {dest-date: lowest price seen}
    "seen_wizzair": {},      # Wizzair deals via Kiwi: {dest-date: lowest price seen}
    "route_low_oneway": {},       # {destination city: lowest one-way price ever seen}
    "route_low_roundtrip": {},    # {destination city: lowest round-trip price ever seen}
    "city_coords": {},            # {city name: {"lat":.., "lon":..}} cache for weather lookups
    "deals_since_digest": 0,
    "last_digest": None,          # ISO date of the last weekly digest sent
    "last_check": None,
}


def current_date_range(state):
    """Returns (date_from, date_to) to search. Auto-rolls to today -> +6 months
    unless the user has explicitly set a custom range via /setdates."""
    if state.get("dates_custom"):
        return state["date_from"], state["date_to"]
    today = date.today()
    return today.isoformat(), (today + timedelta(days=180)).isoformat()


def check_and_update_seen(seen_dict, key, price, drop_threshold):
    """Returns (should_alert, is_price_drop). Updates seen_dict in place with the
    lowest price seen for this key so future small wobbles don't re-trigger."""
    prior = seen_dict.get(key)
    if prior is None:
        seen_dict[key] = price
        return True, False
    if price <= prior - drop_threshold:
        seen_dict[key] = price
        return True, True
    if price < prior:
        seen_dict[key] = price  # quietly lower the baseline, not a big enough drop to alert
    return False, False


def eur_to_pln(state):
    """Returns a EUR->PLN rate, refreshing from a free no-key API at most every 6 hours."""
    now = datetime.now()
    if state.get("pln_rate") and state.get("pln_rate_updated"):
        try:
            last = datetime.fromisoformat(state["pln_rate_updated"])
            if now - last < timedelta(hours=6):
                return state["pln_rate"]
        except ValueError:
            pass
    try:
        resp = requests.get("https://api.frankfurter.app/latest", params={"from": "EUR", "to": "PLN"}, timeout=10)
        resp.raise_for_status()
        rate = resp.json()["rates"]["PLN"]
        state["pln_rate"] = rate
        state["pln_rate_updated"] = now.isoformat()
        return rate
    except Exception as e:
        log.warning(f"PLN rate fetch failed, using cached/fallback: {e}")
        return state.get("pln_rate") or 4.3  # rough fallback if API and cache both unavailable


def price_line(amount, currency, pln_rate):
    pln = amount * pln_rate
    return f"{amount:.2f} {currency} (~{pln:.0f} PLN)"


def check_record(route_low_dict, city, price):
    """Returns True and updates route_low_dict if this is the lowest price
    ever seen for this destination (regardless of exact date)."""
    prior = route_low_dict.get(city)
    if prior is None or price < prior:
        route_low_dict[city] = price
        return True
    return False


def fetch_weather(city, target_date, state):
    """Returns a short weather blurb for a city on a given date (YYYY-MM-DD), or
    None if the date is too far out for a forecast (~16 days) or the lookup fails.
    Geocoding results are cached in state['city_coords'] to avoid repeat calls."""
    try:
        days_out = (datetime.strptime(target_date, "%Y-%m-%d").date() - date.today()).days
        if days_out < 0 or days_out > 15:
            return None  # outside free forecast range

        coords = state["city_coords"].get(city)
        if not coords:
            geo_resp = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1}, timeout=10,
            )
            geo_resp.raise_for_status()
            results = geo_resp.json().get("results")
            if not results:
                return None
            coords = {"lat": results[0]["latitude"], "lon": results[0]["longitude"]}
            state["city_coords"][city] = coords

        wx_resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": coords["lat"], "longitude": coords["lon"],
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto", "start_date": target_date, "end_date": target_date,
            },
            timeout=10,
        )
        wx_resp.raise_for_status()
        daily = wx_resp.json().get("daily", {})
        if not daily.get("time"):
            return None
        tmax = daily["temperature_2m_max"][0]
        tmin = daily["temperature_2m_min"][0]
        rain = daily["precipitation_probability_max"][0]
        return f"🌤 {tmin:.0f}-{tmax:.0f}°C, {rain:.0f}% rain chance"
    except Exception as e:
        log.warning(f"Weather fetch failed for {city}/{target_date}: {e}")
        return None


def load_state():
    if STATE_FILE.exists():
        try:
            state = {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text())}
            for key in ("seen_deals", "seen_round_trips", "seen_lot", "seen_wizzair"):
                if isinstance(state.get(key), list):
                    # old format was a list of keys with no price - migrate with unknown baseline
                    # (0 means "never triggers a drop alert until a real new low is seen")
                    state[key] = {k: 0 for k in state[key]}
            return state
        except Exception as e:
            log.warning(f"Could not read state file, using defaults: {e}")
    return dict(DEFAULT_STATE)


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def time_bucket(dt: datetime) -> str:
    if dt.hour < 12:
        return "morning"
    elif dt.hour < 17:
        return "lunch"
    else:
        return "evening"


def booking_link(from_code, to_code, out_date, in_date=None):
    """Best-effort Ryanair booking search link. Verify it lands correctly - the
    exact query params Ryanair expects can change without notice."""
    base = "https://www.ryanair.com/pl/en/trip/flights/select"
    params = (
        f"?adults=1&teens=0&children=0&infants=0"
        f"&dateOut={out_date}&dateIn={in_date or ''}"
        f"&isConnectedFlight=false&discount=0&promoCode="
        f"&isReturn={'true' if in_date else 'false'}"
        f"&originIata={from_code}&destinationIata={to_code}"
    )
    return base + params


# ---------------------------------------------------------------------------
# ONE-WAY FARES
# ---------------------------------------------------------------------------
def fetch_oneway_fares(departure_airport, arrival_airport, date_from, date_to, max_price, time_buckets, debug=False):
    params = {
        "departureAirportIataCode": departure_airport,
        "outboundDepartureDateFrom": date_from,
        "outboundDepartureDateTo": date_to,
        "priceValueTo": max_price,
        "currency": "EUR",
        "market": "en-gb",
        "language": "en",
        "limit": 200,
        "offset": 0,
    }
    if arrival_airport:
        params["arrivalAirportIataCode"] = arrival_airport
    resp = requests.get(ONEWAY_ENDPOINT, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if debug:
        print(json.dumps(data, indent=2)[:3000])
        print("... (truncated if longer) ...")

    deals = []
    for item in data.get("fares", []):
        try:
            outbound = item["outbound"]
            price = outbound["price"]["value"]
            dep_dt = datetime.fromisoformat(outbound["departureDate"])
            if price > max_price:
                continue
            if time_bucket(dep_dt) not in time_buckets:
                continue
            deals.append({
                "price": price,
                "currency": outbound["price"]["currencyCode"],
                "date": outbound["departureDate"],
                "from": outbound["departureAirport"]["name"],
                "to": outbound["arrivalAirport"]["name"],
                "to_code": outbound["arrivalAirport"]["iataCode"],
            })
        except (KeyError, TypeError):
            continue
    return deals


def oneway_key(d):
    return f"{d['to_code']}-{d['date']}"


# ---------------------------------------------------------------------------
# ROUND-TRIP FARES
# ---------------------------------------------------------------------------
def fetch_roundtrip_fares(departure_airport, arrival_airport, date_from, date_to, min_nights, max_nights,
                           max_price, time_buckets, debug=False):
    params = {
        "departureAirportIataCode": departure_airport,
        "outboundDepartureDateFrom": date_from,
        "outboundDepartureDateTo": date_to,
        "inboundDepartureDateFrom": date_from,
        "inboundDepartureDateTo": date_to,
        "durationFrom": min_nights,
        "durationTo": max_nights,
        "priceValueTo": max_price,
        "currency": "EUR",
        "market": "en-gb",
        "language": "en",
        "adultPaxCount": 1,
        "searchMode": "ALL",
        "limit": 16,
        "offset": 0,
    }
    if arrival_airport:
        params["arrivalAirportIataCode"] = arrival_airport
    resp = requests.get(ROUNDTRIP_ENDPOINT, params=params, timeout=20)
    if debug and not resp.ok:
        print(f"HTTP {resp.status_code} - response body:")
        print(resp.text[:2000])
    resp.raise_for_status()
    data = resp.json()

    if debug:
        print(json.dumps(data, indent=2)[:3000])
        print("... (truncated if longer) ...")

    deals = []
    for item in data.get("fares", []):
        try:
            outbound = item["outbound"]
            inbound = item["inbound"]
            total_price = item["summary"]["price"]["value"]
            dep_dt = datetime.fromisoformat(outbound["departureDate"])
            if total_price > max_price:
                continue
            if time_bucket(dep_dt) not in time_buckets:
                continue
            deals.append({
                "price": total_price,
                "currency": item["summary"]["price"]["currencyCode"],
                "out_date": outbound["departureDate"],
                "in_date": inbound["departureDate"],
                "from": outbound["departureAirport"]["name"],
                "to": outbound["arrivalAirport"]["name"],
                "to_code": outbound["arrivalAirport"]["iataCode"],
            })
        except (KeyError, TypeError):
            continue
    return deals


def roundtrip_key(d):
    return f"{d['to_code']}-{d['out_date']}-{d['in_date']}"


# ---------------------------------------------------------------------------
# LOT / WIZZAIR FARES (via Kiwi Tequila API - requires KIWI_API_KEY)
# ---------------------------------------------------------------------------
def fetch_kiwi_fares(airline_code, departure_airport, arrival_airport, date_from, date_to,
                      max_price, time_buckets, debug=False):
    """Searches one-way fares for a specific airline via Kiwi's Tequila API.
    Requires KIWI_API_KEY to be set. Dates must be DD/MM/YYYY for this API."""
    if not KIWI_API_KEY:
        return []

    def to_kiwi_date(iso_date):
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")

    params = {
        "fly_from": departure_airport,
        "fly_to": arrival_airport or "europe",  # Kiwi needs a region if no specific destination
        "date_from": to_kiwi_date(date_from),
        "date_to": to_kiwi_date(date_to),
        "select_airlines": airline_code,
        "curr": "EUR",
        "price_to": max_price,
        "limit": 50,
        "one_for_city": 0,
    }
    headers = {"apikey": KIWI_API_KEY}
    resp = requests.get(KIWI_ENDPOINT, params=params, headers=headers, timeout=20)
    if debug and not resp.ok:
        print(f"HTTP {resp.status_code} - response body:")
        print(resp.text[:2000])
    resp.raise_for_status()
    data = resp.json()

    if debug:
        print(json.dumps(data, indent=2)[:3000])
        print("... (truncated if longer) ...")

    deals = []
    for item in data.get("data", []):
        try:
            price = item["price"]
            if price > max_price:
                continue
            dep_dt = datetime.fromisoformat(item["local_departure"].replace("Z", "+00:00")).replace(tzinfo=None)
            if time_bucket(dep_dt) not in time_buckets:
                continue
            deals.append({
                "price": price,
                "currency": "EUR",
                "date": item["local_departure"],
                "from": item.get("cityFrom", departure_airport),
                "to": item.get("cityTo", item.get("flyTo", "?")),
                "to_code": item.get("flyTo", "?"),
                "link": item.get("deep_link", ""),
            })
        except (KeyError, TypeError):
            continue
    return deals


def kiwi_key(d):
    return f"{d['to_code']}-{d['date'][:10]}"


# ---------------------------------------------------------------------------
# HOTEL SEARCH (via SerpApi's google_hotels engine - requires SERPAPI_KEY)
# ---------------------------------------------------------------------------
def fetch_hotels(city_query, checkin_date, checkout_date, adults, children, extra_terms="",
                  property_pref="both", bedrooms=None, children_ages=None, debug=False):
    """Searches hotels and/or apartments (Google's vacation_rentals mode is a
    separate search from regular hotels) for a city/date range via SerpApi.
    property_pref: 'both', 'hotel', or 'apartment'. bedrooms and children_ages only
    apply to the apartment (vacation_rentals) search, per SerpApi's own scoping.
    Returns up to 5 highly-rated results, cheapest first."""
    if not SERPAPI_KEY:
        return []

    def parse_price(rate_str):
        if not rate_str:
            return None
        digits = "".join(c for c in str(rate_str) if c.isdigit() or c == ".")
        try:
            return float(digits) if digits else None
        except ValueError:
            return None

    def run_search(vacation_rentals):
        q = f"{city_query} hotels"
        if extra_terms:
            q += f" {extra_terms}"
        params = {
            "engine": "google_hotels",
            "q": q,
            "check_in_date": checkin_date,
            "check_out_date": checkout_date,
            "adults": adults,
            "children": children,
            "currency": "EUR",
            "gl": "pl",
            "hl": "en",
            "api_key": SERPAPI_KEY,
        }
        if children and children_ages:
            params["children_ages"] = ",".join(str(a) for a in children_ages)
        if vacation_rentals:
            params["vacation_rentals"] = "true"
            if bedrooms:
                params["bedrooms"] = bedrooms
        resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=25)
        if debug and not resp.ok:
            print(f"HTTP {resp.status_code} - response body:")
            print(resp.text[:2000])
        resp.raise_for_status()
        data = resp.json()
        if debug:
            print(f"--- vacation_rentals={vacation_rentals} ---")
            print(json.dumps(data, indent=2)[:3000])
            print("... (truncated if longer) ...")

        out = []
        for item in data.get("properties", []):
            try:
                name = item.get("name", "Unknown")
                rate = item.get("rate_per_night", {}).get("lowest") or item.get("total_rate", {}).get("lowest")
                rating = item.get("overall_rating")
                link = item.get("link", "")
                kind = "apartment" if vacation_rentals else "hotel"
                out.append({"name": name, "rate": rate, "price": parse_price(rate),
                            "rating": rating, "link": link, "kind": kind})
            except (KeyError, TypeError, AttributeError):
                continue
        return out

    results = []
    if property_pref in ("both", "hotel"):
        results += run_search(vacation_rentals=False)
    if property_pref in ("both", "apartment"):
        results += run_search(vacation_rentals=True)

    # prefer highly-rated (4.5+/5) results, then show cheapest first within that pool
    highly_rated = [r for r in results if isinstance(r["rating"], (int, float)) and r["rating"] >= 4.5]
    pool = highly_rated if highly_rated else results
    pool.sort(key=lambda r: r["price"] if r["price"] is not None else float("inf"))
    return pool[:5]


async def check_for_deals(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    alerts = []  # list of {"text": str, "city": str, "checkin": str, "checkout": str}
    dep = state["departure_airport"]
    arr = state["arrival_airport"]
    date_from, date_to = current_date_range(state)
    drop_threshold = state["drop_threshold"]
    pln_rate = eur_to_pln(state)  # also refreshes state["pln_rate"] in place if stale

    if state["track_oneway"]:
        try:
            oneway = fetch_oneway_fares(
                dep, arr, date_from, date_to,
                state["max_price"], state["time_buckets"],
            )
            for d in oneway:
                should_alert, is_drop = check_and_update_seen(state["seen_deals"], oneway_key(d), d["price"], drop_threshold)
                if not should_alert:
                    continue
                link = booking_link(dep, d["to_code"], d["date"][:10])
                is_record = check_record(state["route_low_oneway"], d["to"], d["price"])
                tag = "📉 *Price drop -" if is_drop else "✈️ *Ryanair one-way"
                if is_record:
                    tag += " 🔥 lowest ever"
                checkin = d["date"][:10]
                checkout = (datetime.fromisoformat(d["date"]) + timedelta(days=3)).date().isoformat()
                weather = fetch_weather(d["to"], checkin, state)
                weather_line = f"\n{weather}" if weather else ""
                alerts.append({
                    "text": (
                        f"{tag}:* {d['from']} → {d['to']} on {d['date'][:10]}: "
                        f"*{price_line(d['price'], d['currency'], pln_rate)}*{weather_line}\n[Book →]({link})"
                    ),
                    "city": d["to"], "checkin": checkin, "checkout": checkout,
                })
                state["deals_since_digest"] += 1
        except Exception as e:
            log.error(f"One-way fetch failed: {e}")

    if state["track_roundtrip"]:
        try:
            roundtrip = fetch_roundtrip_fares(
                dep, arr, date_from, date_to,
                state["min_nights"], state["max_nights"],
                state["round_trip_max_price"], state["time_buckets"],
            )
            for d in roundtrip:
                should_alert, is_drop = check_and_update_seen(
                    state["seen_round_trips"], roundtrip_key(d), d["price"], drop_threshold
                )
                if not should_alert:
                    continue
                link = booking_link(dep, d["to_code"], d["out_date"][:10], d["in_date"][:10])
                is_record = check_record(state["route_low_roundtrip"], d["to"], d["price"])
                tag = "📉 *Price drop - round trip" if is_drop else "🔁 *Ryanair round trip"
                if is_record:
                    tag += " 🔥 lowest ever"
                weather = fetch_weather(d["to"], d["out_date"][:10], state)
                weather_line = f"\n{weather}" if weather else ""
                alerts.append({
                    "text": (
                        f"{tag}:* {d['from']} → {d['to']}\n"
                        f"Out {d['out_date'][:10]} / Back {d['in_date'][:10]}: "
                        f"*{price_line(d['price'], d['currency'], pln_rate)}* total{weather_line}\n[Book →]({link})"
                    ),
                    "city": d["to"], "checkin": d["out_date"][:10], "checkout": d["in_date"][:10],
                })
                state["deals_since_digest"] += 1
        except Exception as e:
            log.error(f"Round-trip fetch failed: {e}")

    if KIWI_API_KEY:
        try:
            lot_deals = fetch_kiwi_fares(
                "LO", state["lot_departure_airport"], arr, date_from, date_to,
                state["lot_max_price"], state["time_buckets"],
            )
            for d in lot_deals:
                should_alert, is_drop = check_and_update_seen(state["seen_lot"], kiwi_key(d), d["price"], drop_threshold)
                if not should_alert:
                    continue
                link = d["link"] or "https://www.lot.com/"
                is_record = check_record(state["route_low_oneway"], d["to"], d["price"])
                tag = "📉 *Price drop - LOT" if is_drop else "🛫 *LOT one-way"
                if is_record:
                    tag += " 🔥 lowest ever"
                checkin = d["date"][:10]
                checkout = (datetime.fromisoformat(d["date"][:10]) + timedelta(days=3)).date().isoformat()
                weather = fetch_weather(d["to"], checkin, state)
                weather_line = f"\n{weather}" if weather else ""
                alerts.append({
                    "text": (
                        f"{tag}:* {d['from']} → {d['to']} on {d['date'][:10]}: "
                        f"*{price_line(d['price'], d['currency'], pln_rate)}*{weather_line}\n[Book →]({link})"
                    ),
                    "city": d["to"], "checkin": checkin, "checkout": checkout,
                })
                state["deals_since_digest"] += 1
        except Exception as e:
            log.error(f"LOT fetch failed: {e}")

        try:
            wizz_deals = fetch_kiwi_fares(
                "W6", state["wizzair_departure_airport"], arr, date_from, date_to,
                state["wizzair_max_price"], state["time_buckets"],
            )
            for d in wizz_deals:
                should_alert, is_drop = check_and_update_seen(state["seen_wizzair"], kiwi_key(d), d["price"], drop_threshold)
                if not should_alert:
                    continue
                link = d["link"] or "https://wizzair.com/"
                is_record = check_record(state["route_low_oneway"], d["to"], d["price"])
                tag = "📉 *Price drop - Wizzair" if is_drop else "🟣 *Wizzair one-way"
                if is_record:
                    tag += " 🔥 lowest ever"
                checkin = d["date"][:10]
                checkout = (datetime.fromisoformat(d["date"][:10]) + timedelta(days=3)).date().isoformat()
                weather = fetch_weather(d["to"], checkin, state)
                weather_line = f"\n{weather}" if weather else ""
                alerts.append({
                    "text": (
                        f"{tag}:* {d['from']} → {d['to']} on {d['date'][:10]}: "
                        f"*{price_line(d['price'], d['currency'], pln_rate)}*{weather_line}\n[Book →]({link})"
                    ),
                    "city": d["to"], "checkin": checkin, "checkout": checkout,
                })
                state["deals_since_digest"] += 1
        except Exception as e:
            log.error(f"Wizzair fetch failed: {e}")

    if alerts:
        FLOOD_CAP = 25  # avoid spamming 100+ individual messages on a big first run
        individual = alerts[:FLOOD_CAP]
        overflow = alerts[FLOOD_CAP:]

        for a in individual:
            keyboard = None
            if SERPAPI_KEY:
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🏨 Find stay",
                    callback_data=f"findstay:{a['city']}:{a['checkin']}:{a['checkout']}"
                )]])
            await context.bot.send_message(
                chat_id=CHAT_ID, text=a["text"], parse_mode="Markdown",
                disable_web_page_preview=True, reply_markup=keyboard,
            )

        if overflow:
            header = f"...and {len(overflow)} more:\n\n"
            batch = header
            batches = []
            for a in overflow:
                if len(batch) + len(a["text"]) + 2 > 3500:
                    batches.append(batch)
                    batch = ""
                batch += a["text"] + "\n\n"
            if batch:
                batches.append(batch)
            for b in batches:
                await context.bot.send_message(
                    chat_id=CHAT_ID, text=b, parse_mode="Markdown", disable_web_page_preview=True
                )

        log.info(f"Sent {len(alerts)} new deal(s) ({len(individual)} individual, {len(overflow)} batched)")
    else:
        log.info("No new deals this check")

    state["seen_deals"] = dict(list(state["seen_deals"].items())[-500:])
    state["seen_round_trips"] = dict(list(state["seen_round_trips"].items())[-500:])
    state["seen_lot"] = dict(list(state["seen_lot"].items())[-500:])
    state["seen_wizzair"] = dict(list(state["seen_wizzair"].items())[-500:])
    state["last_check"] = datetime.now().isoformat(timespec="minutes")

    await maybe_send_weekly_digest(context, state)
    save_state(state)


async def maybe_send_weekly_digest(context, state):
    """Sends a recap message if 7+ days have passed since the last one."""
    today = date.today()
    last = state.get("last_digest")
    if last:
        try:
            if (today - datetime.strptime(last, "%Y-%m-%d").date()).days < 7:
                return
        except ValueError:
            pass  # bad stored value, fall through and send

    dep = state["departure_airport"]
    arr = state["arrival_airport"]
    date_from, date_to = current_date_range(state)
    pln_rate = eur_to_pln(state)
    lines = [f"📬 *Weekly recap* - {state['deals_since_digest']} new deal(s) sent this week.\n"]

    try:
        oneway = fetch_oneway_fares(dep, arr, date_from, date_to, state["max_price"], state["time_buckets"])
        oneway.sort(key=lambda d: d["price"])
        if oneway:
            lines.append("*Cheapest one-way right now:*")
            for d in oneway[:3]:
                lines.append(f"✈️ {d['to']} - {price_line(d['price'], d['currency'], pln_rate)}")
    except Exception as e:
        log.error(f"Digest one-way fetch failed: {e}")

    try:
        roundtrip = fetch_roundtrip_fares(
            dep, arr, date_from, date_to, state["min_nights"], state["max_nights"],
            state["round_trip_max_price"], state["time_buckets"],
        )
        roundtrip.sort(key=lambda d: d["price"])
        if roundtrip:
            lines.append("\n*Cheapest round trip right now:*")
            for d in roundtrip[:3]:
                lines.append(f"🔁 {d['to']} - {price_line(d['price'], d['currency'], pln_rate)}")
    except Exception as e:
        log.error(f"Digest round-trip fetch failed: {e}")

    await context.bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode="Markdown")
    state["deals_since_digest"] = 0
    state["last_digest"] = today.isoformat()


# ---------------------------------------------------------------------------
# BUTTON MENUS
# ---------------------------------------------------------------------------
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📊 Status", "🔍 Check now"],
        ["💶 One-way price", "🔁 Round-trip price"],
        ["🔀 Trip types", "🌙 Trip length"],
        ["🕐 Times of day", "🏆 Top deals"],
        ["🏠 Browse a city", "❓ Help"],
    ],
    resize_keyboard=True,
)

ONEWAY_PRICE_OPTIONS = [10, 15, 20, 25, 30, 40, 50]
ROUNDTRIP_PRICE_OPTIONS = [20, 30, 40, 50, 60, 80, 100]
TIME_LABELS = {"morning": "🌅 Morning", "lunch": "🌞 Lunch", "evening": "🌆 Evening"}


def price_keyboard(options, prefix):
    row_size = 4
    rows = []
    for i in range(0, len(options), row_size):
        rows.append([
            InlineKeyboardButton(f"{p} EUR", callback_data=f"{prefix}:{p}")
            for p in options[i:i + row_size]
        ])
    return InlineKeyboardMarkup(rows)


def time_keyboard(selected):
    row = []
    for bucket, label in TIME_LABELS.items():
        mark = "✅ " if bucket in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"toggletime:{bucket}"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("Done ✔️", callback_data="timedone")]])


def trip_type_keyboard(track_oneway, track_roundtrip):
    oneway_mark = "✅ " if track_oneway else ""
    roundtrip_mark = "✅ " if track_roundtrip else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{oneway_mark}✈️ One-way", callback_data="toggletrip:oneway")],
        [InlineKeyboardButton(f"{roundtrip_mark}🔁 Round-trip", callback_data="toggletrip:roundtrip")],
        [InlineKeyboardButton("Done ✔️", callback_data="tripdone")],
    ])


def trip_length_keyboard(min_n, max_n):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖", callback_data="tl:min-"),
         InlineKeyboardButton(f"Min nights: {min_n}", callback_data="tl:noop"),
         InlineKeyboardButton("➕", callback_data="tl:min+")],
        [InlineKeyboardButton("➖", callback_data="tl:max-"),
         InlineKeyboardButton(f"Max nights: {max_n}", callback_data="tl:noop"),
         InlineKeyboardButton("➕", callback_data="tl:max+")],
        [InlineKeyboardButton("Done ✔️", callback_data="tl:done")],
    ])


def guest_count_keyboard(adults, kids):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖", callback_data="hf:a-"),
         InlineKeyboardButton(f"Adults: {adults}", callback_data="hf:noop"),
         InlineKeyboardButton("➕", callback_data="hf:a+")],
        [InlineKeyboardButton("➖", callback_data="hf:k-"),
         InlineKeyboardButton(f"Kids: {kids}", callback_data="hf:noop"),
         InlineKeyboardButton("➕", callback_data="hf:k+")],
        [InlineKeyboardButton("Next ▶️", callback_data="hf:guestnext")],
    ])


def kid_age_keyboard(age):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖", callback_data="hf:age-"),
         InlineKeyboardButton(f"Age: {age}", callback_data="hf:noop"),
         InlineKeyboardButton("➕", callback_data="hf:age+")],
        [InlineKeyboardButton("Next ▶️", callback_data="hf:agenext")],
    ])


def property_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏨 Hotel", callback_data="hf:prop:h")],
        [InlineKeyboardButton("🏠 Apartment", callback_data="hf:prop:a")],
        [InlineKeyboardButton("🏘 Both", callback_data="hf:prop:b")],
    ])


def rooms_keyboard():
    row = [InlineKeyboardButton(str(r), callback_data=f"hf:rooms:{r}") for r in range(1, 5)]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("Any", callback_data="hf:rooms:0")]])


def dog_keyboard(dog):
    mark = "✅ " if dog else ""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{mark}🐕 Traveling with a dog", callback_data="hf:dog")],
        [InlineKeyboardButton("Search 🔍", callback_data="hf:search")],
    ])


# ---------------------------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ryanair Deal Bot is running. Use the buttons below, or /help for the full command list.",
        reply_markup=MAIN_MENU,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Buttons* (bottom of chat):\n"
        "📊 Status · 🔍 Check now · 🏆 Top deals · 🏠 Browse a city · "
        "💶 One-way price · 🔁 Round-trip price · 🔀 Trip types · 🌙 Trip length · 🕐 Times of day\n\n"
        "Every deal alert also has a *🏨 Find stay* button - tap it and walk through: "
        "adults/kids count (with kid ages), hotel vs apartment, rooms, then dog y/n. "
        "It then searches highly-rated stays for those dates.\n\n"
        "*Typed commands:*\n"
        "/setdeparture WMI - change Ryanair/Wizzair departure airport\n"
        "/setarrival BLQ - only alert for one destination\n"
        "/setarrival any - go back to searching anywhere\n"
        "/setdates 2026-08-01 2026-09-30 - fix a specific date range\n"
        "/cleardates - go back to auto (always rolls 6 months ahead)\n"
        "/settrip 2 10 - min/max nights away (or use the 🌙 Trip length button)\n"
        "/setmax 20 - Ryanair one-way price threshold\n"
        "/setmaxreturn 40 - Ryanair round-trip price threshold\n"
        "/setlotmax 100 - LOT price threshold\n"
        "/setwizzairmax 30 - Wizzair price threshold\n"
        "/setdropthreshold 3 - re-alert when a seen flight drops by at least this much\n"
        "/settime morning lunch evening - which times of day count\n"
        "/status - show current settings\n"
        "/check - check for deals right now\n"
        "/top - see the cheapest options right now (not just new ones)\n"
        "/chatid - show this chat's ID\n"
        "/clear 20 - delete the last 20 messages (needs group admin rights to delete others')\n\n"
        "By default dates auto-roll from today to 6 months out - no need to touch them "
        "unless you want a specific window. Prices are shown in EUR with an approximate PLN "
        "conversion alongside. Deals departing within ~2 weeks get a quick weather line. "
        "A 🔥 tag means it's the lowest price ever seen for that destination. "
        "You'll also get a weekly recap every 7 days even if nothing new came up.",
        parse_mode="Markdown",
    )



async def cmd_setmax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setmax 20")
        return
    try:
        new_max = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Not a number. Usage: /setmax 20")
        return
    state = load_state()
    state["max_price"] = new_max
    save_state(state)
    await update.message.reply_text(f"One-way max price set to {new_max} EUR.")


async def cmd_setmaxreturn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setmaxreturn 40")
        return
    try:
        new_max = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Not a number. Usage: /setmaxreturn 40")
        return
    state = load_state()
    state["round_trip_max_price"] = new_max
    save_state(state)
    await update.message.reply_text(f"Round-trip max price set to {new_max} EUR.")


async def cmd_setlotmax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setlotmax 100")
        return
    try:
        new_max = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Not a number. Usage: /setlotmax 100")
        return
    if not KIWI_API_KEY:
        await update.message.reply_text("Saved, but LOT search needs KIWI_API_KEY set to actually run.")
    state = load_state()
    state["lot_max_price"] = new_max
    save_state(state)
    await update.message.reply_text(f"LOT max price set to {new_max} EUR.")


async def cmd_setwizzairmax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setwizzairmax 30")
        return
    try:
        new_max = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Not a number. Usage: /setwizzairmax 30")
        return
    if not KIWI_API_KEY:
        await update.message.reply_text("Saved, but Wizzair search needs KIWI_API_KEY set to actually run.")
    state = load_state()
    state["wizzair_max_price"] = new_max
    save_state(state)
    await update.message.reply_text(f"Wizzair max price set to {new_max} EUR.")


async def cmd_setdropthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setdropthreshold 3  (re-alert when a seen flight drops by at least this many EUR)")
        return
    try:
        new_threshold = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Not a number. Usage: /setdropthreshold 3")
        return
    state = load_state()
    state["drop_threshold"] = new_threshold
    save_state(state)
    await update.message.reply_text(f"Will re-alert when a flight drops by at least {new_threshold} EUR from what you were last told.")


async def cmd_setdeparture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setdeparture WMI  (IATA airport code)")
        return
    code = context.args[0].strip().upper()
    if not code.isalpha() or len(code) != 3:
        await update.message.reply_text("That doesn't look like a 3-letter IATA code, e.g. WMI, WAW, KRK.")
        return
    state = load_state()
    state["departure_airport"] = code
    save_state(state)
    await update.message.reply_text(f"Departure airport set to {code}.")


async def cmd_setarrival(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setarrival BLQ (a specific destination) or /setarrival any")
        return
    value = context.args[0].strip().upper()
    state = load_state()
    if value == "ANY":
        state["arrival_airport"] = None
        save_state(state)
        await update.message.reply_text("Arrival set to anywhere.")
        return
    if not value.isalpha() or len(value) != 3:
        await update.message.reply_text("That doesn't look like a 3-letter IATA code, e.g. BLQ, STN, BUD.")
        return
    state["arrival_airport"] = value
    save_state(state)
    await update.message.reply_text(f"Arrival airport set to {value}.")


async def cmd_setdates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setdates 2026-08-01 2026-09-30")
        return
    date_from, date_to = context.args
    try:
        datetime.strptime(date_from, "%Y-%m-%d")
        datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Use YYYY-MM-DD format for both dates.")
        return
    state = load_state()
    state["date_from"] = date_from
    state["date_to"] = date_to
    state["dates_custom"] = True
    save_state(state)
    await update.message.reply_text(f"Date range set to {date_from} → {date_to}.")


async def cmd_cleardates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    state["dates_custom"] = False
    save_state(state)
    date_from, date_to = current_date_range(state)
    await update.message.reply_text(
        f"Back to auto date range: {date_from} → {date_to} (always rolls 6 months ahead)."
    )


async def cmd_settrip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /settrip 2 10  (min and max nights away)")
        return
    try:
        min_n, max_n = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("Both values must be whole numbers of nights.")
        return
    state = load_state()
    state["min_nights"] = min_n
    state["max_nights"] = max_n
    save_state(state)
    await update.message.reply_text(f"Round-trip length set to {min_n}-{max_n} nights.")


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /settime morning lunch evening (pick any combination)\n"
            "morning = before 12:00, lunch = 12:00-17:00, evening = after 17:00"
        )
        return
    buckets = [b.lower() for b in context.args]
    invalid = [b for b in buckets if b not in VALID_TIME_BUCKETS]
    if invalid:
        await update.message.reply_text(f"Unknown time(s): {', '.join(invalid)}. Use: morning, lunch, evening.")
        return
    state = load_state()
    state["time_buckets"] = buckets
    save_state(state)
    await update.message.reply_text(f"Alert times set to: {', '.join(buckets)}.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    date_from, date_to = current_date_range(state)
    date_mode = "fixed" if state.get("dates_custom") else "auto-rolling"
    kiwi_line = "" if KIWI_API_KEY else "\n⚠️ KIWI_API_KEY not set - LOT/Wizzair checks are skipped"
    serpapi_line = "" if SERPAPI_KEY else "\n⚠️ SERPAPI_KEY not set - hotel search is disabled"
    trip_parts = []
    if state["track_oneway"]:
        trip_parts.append("one-way")
    if state["track_roundtrip"]:
        trip_parts.append("round-trip")
    trip_types = " + ".join(trip_parts) if trip_parts else "none (tracking disabled)"
    await update.message.reply_text(
        f"Ryanair from: {state['departure_airport']}\n"
        f"To: {state['arrival_airport'] or 'anywhere'}\n"
        f"Tracking: {trip_types}\n"
        f"Ryanair one-way max: {state['max_price']} EUR\n"
        f"Ryanair round-trip max: {state['round_trip_max_price']} EUR\n"
        f"LOT from: {state['lot_departure_airport']} · max {state['lot_max_price']} EUR\n"
        f"Wizzair from: {state['wizzair_departure_airport']} · max {state['wizzair_max_price']} EUR\n"
        f"Dates: {date_from} to {date_to} ({date_mode})\n"
        f"Trip length: {state['min_nights']}-{state['max_nights']} nights\n"
        f"Times: {', '.join(state['time_buckets'])}\n"
        f"Price drop re-alert: -{state['drop_threshold']} EUR\n"
        f"Weekly digest: last sent {state['last_digest'] or 'never'} ({state['deals_since_digest']} deal(s) since)\n"
        f"Last check: {state['last_check'] or 'never'}\n"
        f"Deals alerted: {len(state['seen_deals']) + len(state['seen_round_trips']) + len(state['seen_lot']) + len(state['seen_wizzair'])}"
        f"{kiwi_line}{serpapi_line}"
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"This chat's ID is: {update.effective_chat.id}")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /clear 20  (deletes the last 20 messages, including this command)")
        return
    n = min(int(context.args[0]), 200)  # hard cap so one command can't try to nuke thousands
    current_id = update.message.message_id
    deleted = 0
    for msg_id in range(current_id, current_id - n - 1, -1):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            deleted += 1
        except Exception:
            pass  # message may not exist, be too old, or the bot may lack delete rights for it
    note = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Deleted {deleted}/{n} message(s). To clear messages from other people too, "
             f"make the bot a group admin with 'Delete messages' permission - otherwise it "
             f"can only delete its own.",
    )
    # clean up this confirmation itself after a few seconds so it doesn't linger
    await asyncio.sleep(5)
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=note.message_id)
    except Exception:
        pass


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if context.user_data.get("awaiting_city_search"):
        context.user_data["awaiting_city_search"] = False
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 3:
            await update.message.reply_text(
                "Didn't quite catch that - format is: City, YYYY-MM-DD, YYYY-MM-DD\n"
                "e.g. Rome, 2026-08-10, 2026-08-14"
            )
            return
        city, checkin, checkout = parts
        try:
            datetime.strptime(checkin, "%Y-%m-%d")
            datetime.strptime(checkout, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("Dates must be YYYY-MM-DD. Try again.")
            return
        context.user_data["hotel_flow"] = {
            "city": city, "checkin": checkin, "checkout": checkout,
            "adults": 2, "kids": 0, "kid_ages": [], "current_kid_idx": 0,
            "prop": "b", "rooms": 0, "dog": False,
        }
        await update.message.reply_text(
            f"Who's going to {city}?", reply_markup=guest_count_keyboard(2, 0)
        )
        return

    if text == "📊 Status":
        await cmd_status(update, context)
    elif text == "🔍 Check now":
        await cmd_check(update, context)
    elif text == "🏆 Top deals":
        await cmd_top(update, context)
    elif text == "🏠 Browse a city":
        if not SERPAPI_KEY:
            await update.message.reply_text("Hotel search isn't set up yet (missing SERPAPI_KEY).")
            return
        context.user_data["awaiting_city_search"] = True
        await update.message.reply_text(
            "Type: City, check-in date, check-out date\n"
            "e.g. Rome, 2026-08-10, 2026-08-14"
        )
    elif text == "💶 One-way price":
        await update.message.reply_text(
            "Pick a one-way max price:", reply_markup=price_keyboard(ONEWAY_PRICE_OPTIONS, "setmax")
        )
    elif text == "🔁 Round-trip price":
        await update.message.reply_text(
            "Pick a round-trip max price:", reply_markup=price_keyboard(ROUNDTRIP_PRICE_OPTIONS, "setmaxreturn")
        )
    elif text == "🕐 Times of day":
        state = load_state()
        await update.message.reply_text(
            "Tap to toggle which times count, then Done:",
            reply_markup=time_keyboard(state["time_buckets"]),
        )
    elif text == "🔀 Trip types":
        state = load_state()
        await update.message.reply_text(
            "Tap to toggle which trip types to track, then Done:",
            reply_markup=trip_type_keyboard(state["track_oneway"], state["track_roundtrip"]),
        )
    elif text == "🌙 Trip length":
        state = load_state()
        await update.message.reply_text(
            "Round-trip length range (nights away):",
            reply_markup=trip_length_keyboard(state["min_nights"], state["max_nights"]),
        )
    elif text == "❓ Help":
        await cmd_help(update, context)


async def handle_callback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    state = load_state()

    if data.startswith("setmax:"):
        price = float(data.split(":")[1])
        state["max_price"] = price
        save_state(state)
        await query.answer(f"One-way max set to {price} EUR")
        await query.edit_message_text(f"One-way max price set to {price} EUR ✅")

    elif data.startswith("setmaxreturn:"):
        price = float(data.split(":")[1])
        state["round_trip_max_price"] = price
        save_state(state)
        await query.answer(f"Round-trip max set to {price} EUR")
        await query.edit_message_text(f"Round-trip max price set to {price} EUR ✅")

    elif data.startswith("toggletime:"):
        bucket = data.split(":")[1]
        buckets = state["time_buckets"]
        if bucket in buckets:
            buckets.remove(bucket)
        else:
            buckets.append(bucket)
        state["time_buckets"] = buckets
        save_state(state)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=time_keyboard(buckets))

    elif data == "timedone":
        await query.answer("Saved")
        label = ", ".join(state["time_buckets"]) if state["time_buckets"] else "none selected"
        await query.edit_message_text(f"Alert times set to: {label} ✅")

    elif data.startswith("toggletrip:"):
        kind = data.split(":")[1]
        key = "track_oneway" if kind == "oneway" else "track_roundtrip"
        state[key] = not state[key]
        save_state(state)
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=trip_type_keyboard(state["track_oneway"], state["track_roundtrip"])
        )

    elif data == "tripdone":
        await query.answer("Saved")
        parts = []
        if state["track_oneway"]:
            parts.append("one-way")
        if state["track_roundtrip"]:
            parts.append("round-trip")
        label = " + ".join(parts) if parts else "nothing (tracking disabled)"
        await query.edit_message_text(f"Now tracking: {label} ✅")

    elif data == "tl:noop":
        await query.answer()

    elif data in ("tl:min-", "tl:min+", "tl:max-", "tl:max+"):
        min_n, max_n = state["min_nights"], state["max_nights"]
        if data == "tl:min+":
            min_n = min(min_n + 1, 60)
            max_n = max(max_n, min_n)  # keep max >= min
        elif data == "tl:min-":
            min_n = max(min_n - 1, 1)
        elif data == "tl:max+":
            max_n = min(max_n + 1, 90)
        elif data == "tl:max-":
            max_n = max(max_n - 1, 1)
            min_n = min(min_n, max_n)  # keep min <= max
        state["min_nights"], state["max_nights"] = min_n, max_n
        save_state(state)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=trip_length_keyboard(min_n, max_n))

    elif data == "tl:done":
        await query.answer("Saved")
        await query.edit_message_text(f"Round-trip length set to {state['min_nights']}-{state['max_nights']} nights ✅")

    elif data.startswith("findstay:"):
        _, city, checkin, checkout = data.split(":")
        await query.answer()
        if not SERPAPI_KEY:
            await context.bot.send_message(chat_id=query.message.chat_id, text="Hotel search isn't set up yet (missing SERPAPI_KEY).")
            return
        context.user_data["hotel_flow"] = {
            "city": city, "checkin": checkin, "checkout": checkout,
            "adults": 2, "kids": 0, "kid_ages": [], "current_kid_idx": 0,
            "prop": "b", "rooms": 0, "dog": False,
        }
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Who's going to {city}?",
            reply_markup=guest_count_keyboard(2, 0),
        )

    elif data == "hf:noop":
        await query.answer()

    elif data in ("hf:a+", "hf:a-", "hf:k+", "hf:k-"):
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        if data == "hf:a+":
            flow["adults"] = min(flow["adults"] + 1, 8)
        elif data == "hf:a-":
            flow["adults"] = max(flow["adults"] - 1, 1)
        elif data == "hf:k+":
            flow["kids"] = min(flow["kids"] + 1, 6)
        elif data == "hf:k-":
            flow["kids"] = max(flow["kids"] - 1, 0)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=guest_count_keyboard(flow["adults"], flow["kids"]))

    elif data == "hf:guestnext":
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        await query.answer()
        if flow["kids"] > 0:
            flow["kid_ages"] = [8] * flow["kids"]
            flow["current_kid_idx"] = 0
            await query.edit_message_text(f"{flow['adults']} adults, {flow['kids']} kids.")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Age of kid #1 of {flow['kids']}?",
                reply_markup=kid_age_keyboard(8),
            )
        else:
            await query.edit_message_text(f"{flow['adults']} adults, no kids.")
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="Hotel or apartment?", reply_markup=property_type_keyboard()
            )

    elif data in ("hf:age+", "hf:age-"):
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        idx = flow["current_kid_idx"]
        if data == "hf:age+":
            flow["kid_ages"][idx] = min(flow["kid_ages"][idx] + 1, 17)
        else:
            flow["kid_ages"][idx] = max(flow["kid_ages"][idx] - 1, 0)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=kid_age_keyboard(flow["kid_ages"][idx]))

    elif data == "hf:agenext":
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        await query.answer()
        idx = flow["current_kid_idx"]
        await query.edit_message_text(f"Kid #{idx + 1} age: {flow['kid_ages'][idx]}.")
        flow["current_kid_idx"] += 1
        if flow["current_kid_idx"] < flow["kids"]:
            next_idx = flow["current_kid_idx"]
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Age of kid #{next_idx + 1} of {flow['kids']}?",
                reply_markup=kid_age_keyboard(flow["kid_ages"][next_idx]),
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="Hotel or apartment?", reply_markup=property_type_keyboard()
            )

    elif data.startswith("hf:prop:"):
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        prop = data.split(":")[2]
        flow["prop"] = prop
        await query.answer()
        label = {"h": "Hotel", "a": "Apartment", "b": "Both"}[prop]
        await query.edit_message_text(f"Looking for: {label}.")
        await context.bot.send_message(
            chat_id=query.message.chat_id, text="How many rooms?", reply_markup=rooms_keyboard()
        )

    elif data.startswith("hf:rooms:"):
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        rooms = int(data.split(":")[2])
        flow["rooms"] = rooms
        await query.answer()
        await query.edit_message_text(f"Rooms: {rooms if rooms else 'Any'}.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Traveling with a dog? Tap to toggle, then Search:",
            reply_markup=dog_keyboard(False),
        )

    elif data == "hf:dog":
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        flow["dog"] = not flow["dog"]
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=dog_keyboard(flow["dog"]))

    elif data == "hf:search":
        flow = context.user_data.get("hotel_flow")
        if not flow:
            await query.answer("Session expired - tap Find stay again.", show_alert=True)
            return
        await query.answer("Searching...")
        extras = "pet friendly" if flow["dog"] else ""
        property_pref = {"b": "both", "h": "hotel", "a": "apartment"}[flow["prop"]]
        try:
            hotels = fetch_hotels(
                flow["city"], flow["checkin"], flow["checkout"], flow["adults"], flow["kids"],
                extras, property_pref, bedrooms=flow["rooms"] or None,
                children_ages=flow["kid_ages"] or None,
            )
        except Exception as e:
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"Hotel search failed: {e}")
            return
        if not hotels:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"No highly-rated stays found in {flow['city']} for those dates.",
            )
            return
        lines = []
        for h in hotels:
            rate_str = f"{h['rate']} EUR/night" if h["rate"] else "price n/a"
            rating_str = f"⭐ {h['rating']}" if h["rating"] else "no rating"
            kind_icon = "🏠" if h.get("kind") == "apartment" else "🏨"
            lines.append(f"{kind_icon} *{h['name']}*\n{rate_str} · {rating_str}\n{h['link']}")
        guests_str = f"{flow['adults']} adults" + (f", {flow['kids']} kids" if flow["kids"] else "")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"🏨🏠 *Top stays in {flow['city']}* ({flow['checkin']} → {flow['checkout']}, {guests_str}):\n\n" + "\n\n".join(lines),
            parse_mode="Markdown", disable_web_page_preview=True,
        )
        context.user_data.pop("hotel_flow", None)



async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Checking now...")
    await check_for_deals(context)
    await update.message.reply_text("Done - see above if anything new was found.")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pulling the current cheapest options...")
    state = load_state()
    dep = state["departure_airport"]
    arr = state["arrival_airport"]
    date_from, date_to = current_date_range(state)
    pln_rate = eur_to_pln(state)
    save_state(state)  # persist any refreshed PLN rate
    lines = []

    if state["track_oneway"]:
        try:
            oneway = fetch_oneway_fares(dep, arr, date_from, date_to, state["max_price"], state["time_buckets"])
            oneway.sort(key=lambda d: d["price"])
            for d in oneway[:5]:
                link = booking_link(dep, d["to_code"], d["date"][:10])
                lines.append(
                    f"✈️ {d['from']} → {d['to']} on {d['date'][:10]}: "
                    f"*{price_line(d['price'], d['currency'], pln_rate)}*\n[Book →]({link})"
                )
        except Exception as e:
            log.error(f"Top one-way fetch failed: {e}")

    if state["track_roundtrip"]:
        try:
            roundtrip = fetch_roundtrip_fares(
                dep, arr, date_from, date_to, state["min_nights"], state["max_nights"],
                state["round_trip_max_price"], state["time_buckets"],
            )
            roundtrip.sort(key=lambda d: d["price"])
            for d in roundtrip[:5]:
                link = booking_link(dep, d["to_code"], d["out_date"][:10], d["in_date"][:10])
                lines.append(
                    f"🔁 {d['from']} → {d['to']} · Out {d['out_date'][:10]} / Back {d['in_date'][:10]}: "
                    f"*{price_line(d['price'], d['currency'], pln_rate)}* total\n[Book →]({link})"
                )
        except Exception as e:
            log.error(f"Top round-trip fetch failed: {e}")

    if not lines:
        await update.message.reply_text("Nothing found right now within your current filters.")
        return

    header = "🏆 *Cheapest right now:*\n\n"
    await update.message.reply_text(header + "\n\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_debug_oneway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden - not listed in /help. Dumps raw one-way fetch results for troubleshooting."""
    state = load_state()
    date_from, date_to = current_date_range(state)
    try:
        deals = fetch_oneway_fares(state["departure_airport"], state["arrival_airport"],
                                    date_from, date_to,
                                    state["max_price"], state["time_buckets"])
        sample = "\n".join(str(d) for d in deals[:5])
        await update.message.reply_text(f"Found {len(deals)} one-way deal(s). Sample:\n{sample or '(none)'}")
    except Exception as e:
        await update.message.reply_text(f"Fetch failed: {e}")


async def cmd_debug_roundtrip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden - not listed in /help. Dumps raw round-trip fetch results for troubleshooting."""
    state = load_state()
    date_from, date_to = current_date_range(state)
    try:
        deals = fetch_roundtrip_fares(state["departure_airport"], state["arrival_airport"],
                                       date_from, date_to,
                                       state["min_nights"], state["max_nights"],
                                       state["round_trip_max_price"], state["time_buckets"])
        sample = "\n".join(str(d) for d in deals[:5])
        await update.message.reply_text(f"Found {len(deals)} round-trip deal(s). Sample:\n{sample or '(none)'}")
    except Exception as e:
        await update.message.reply_text(f"Fetch failed: {e}")


async def cmd_debug_lot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden - not listed in /help. Dumps raw LOT fetch results for troubleshooting."""
    state = load_state()
    date_from, date_to = current_date_range(state)
    if not KIWI_API_KEY:
        await update.message.reply_text("KIWI_API_KEY isn't set, so LOT search can't run.")
        return
    try:
        deals = fetch_kiwi_fares("LO", state["lot_departure_airport"], state["arrival_airport"],
                                  date_from, date_to, state["lot_max_price"], state["time_buckets"])
        sample = "\n".join(str(d) for d in deals[:5])
        await update.message.reply_text(f"Found {len(deals)} LOT deal(s). Sample:\n{sample or '(none)'}")
    except Exception as e:
        await update.message.reply_text(f"Fetch failed: {e}")


async def cmd_debug_wizzair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden - not listed in /help. Dumps raw Wizzair fetch results for troubleshooting."""
    state = load_state()
    date_from, date_to = current_date_range(state)
    if not KIWI_API_KEY:
        await update.message.reply_text("KIWI_API_KEY isn't set, so Wizzair search can't run.")
        return
    try:
        deals = fetch_kiwi_fares("W6", state["wizzair_departure_airport"], state["arrival_airport"],
                                  date_from, date_to, state["wizzair_max_price"], state["time_buckets"])
        sample = "\n".join(str(d) for d in deals[:5])
        await update.message.reply_text(f"Found {len(deals)} Wizzair deal(s). Sample:\n{sample or '(none)'}")
    except Exception as e:
        await update.message.reply_text(f"Fetch failed: {e}")


async def cmd_debug_hotel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden - not listed in /help. Usage: /rawhotel City YYYY-MM-DD YYYY-MM-DD"""
    if not SERPAPI_KEY:
        await update.message.reply_text("SERPAPI_KEY isn't set, so hotel search can't run.")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /rawhotel City YYYY-MM-DD YYYY-MM-DD")
        return
    city = context.args[0]
    checkin, checkout = context.args[1], context.args[2]
    try:
        hotels = fetch_hotels(city, checkin, checkout, 2, 0, debug=True)
        sample = "\n".join(str(h) for h in hotels)
        await update.message.reply_text(f"Found {len(hotels)} result(s). Sample:\n{sample or '(none)'}")
    except Exception as e:
        await update.message.reply_text(f"Fetch failed: {e}")


async def cmd_debug_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden - not listed in /help. Usage: /rawweather City YYYY-MM-DD"""
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /rawweather City YYYY-MM-DD (must be within ~15 days of today)")
        return
    city, target_date = context.args[0], context.args[1]
    state = load_state()
    result = fetch_weather(city, target_date, state)
    save_state(state)  # persist any newly cached geocoding
    await update.message.reply_text(result or "No forecast available (date outside ~16-day range, or lookup failed).")


async def cmd_debug_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden - not listed in /help. Dumps state file contents, summarizing the
    large seen-deal dicts as counts instead of full listings to avoid Telegram's
    message length limit."""
    state = load_state()
    summary = dict(state)
    for key in ("seen_deals", "seen_round_trips", "seen_lot", "seen_wizzair"):
        if key in summary:
            summary[key] = f"<{len(summary[key])} entries - omitted for length>"
    text = json.dumps(summary, indent=2)
    if len(text) > 3500:
        text = text[:3500] + "\n... (truncated)"
    await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")


def main():
    if "--debug" in sys.argv:
        state = load_state()
        date_from, date_to = current_date_range(state)
        deals = fetch_oneway_fares(state["departure_airport"], state["arrival_airport"],
                                    date_from, date_to,
                                    state["max_price"], state["time_buckets"], debug=True)
        print(f"\nParsed {len(deals)} one-way deal(s):")
        for d in deals:
            print(d)
        return

    if "--debug-return" in sys.argv:
        state = load_state()
        date_from, date_to = current_date_range(state)
        deals = fetch_roundtrip_fares(state["departure_airport"], state["arrival_airport"],
                                       date_from, date_to,
                                       state["min_nights"], state["max_nights"],
                                       state["round_trip_max_price"], state["time_buckets"], debug=True)
        print(f"\nParsed {len(deals)} round-trip deal(s):")
        for d in deals:
            print(d)
        return

    if "--debug-lot" in sys.argv:
        state = load_state()
        date_from, date_to = current_date_range(state)
        deals = fetch_kiwi_fares("LO", state["lot_departure_airport"], state["arrival_airport"],
                                  date_from, date_to, state["lot_max_price"], state["time_buckets"], debug=True)
        print(f"\nParsed {len(deals)} LOT deal(s):")
        for d in deals:
            print(d)
        return

    if "--debug-wizzair" in sys.argv:
        state = load_state()
        date_from, date_to = current_date_range(state)
        deals = fetch_kiwi_fares("W6", state["wizzair_departure_airport"], state["arrival_airport"],
                                  date_from, date_to, state["wizzair_max_price"], state["time_buckets"], debug=True)
        print(f"\nParsed {len(deals)} Wizzair deal(s):")
        for d in deals:
            print(d)
        return

    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE" or CHAT_ID == "PUT_YOUR_CHAT_ID_HERE":
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setdeparture", cmd_setdeparture))
    app.add_handler(CommandHandler("setarrival", cmd_setarrival))
    app.add_handler(CommandHandler("setmax", cmd_setmax))
    app.add_handler(CommandHandler("setmaxreturn", cmd_setmaxreturn))
    app.add_handler(CommandHandler("setlotmax", cmd_setlotmax))
    app.add_handler(CommandHandler("setwizzairmax", cmd_setwizzairmax))
    app.add_handler(CommandHandler("setdates", cmd_setdates))
    app.add_handler(CommandHandler("cleardates", cmd_cleardates))
    app.add_handler(CommandHandler("settrip", cmd_settrip))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("setdropthreshold", cmd_setdropthreshold))
    app.add_handler(CommandHandler("rawoneway", cmd_debug_oneway))
    app.add_handler(CommandHandler("rawroundtrip", cmd_debug_roundtrip))
    app.add_handler(CommandHandler("rawlot", cmd_debug_lot))
    app.add_handler(CommandHandler("rawwizzair", cmd_debug_wizzair))
    app.add_handler(CommandHandler("rawhotel", cmd_debug_hotel))
    app.add_handler(CommandHandler("rawweather", cmd_debug_weather))
    app.add_handler(CommandHandler("rawstate", cmd_debug_state))
    app.add_handler(CallbackQueryHandler(handle_callback_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_button))

    app.job_queue.run_repeating(check_for_deals, interval=CHECK_INTERVAL_MINUTES * 60, first=10)

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

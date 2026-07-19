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
DEPARTURE_AIRPORT = "WMI"  # Warsaw Modlin
CHECK_INTERVAL_MINUTES = 20

STATE_FILE = Path(__file__).parent / "deal_bot_state.json"

ONEWAY_ENDPOINT = "https://services-api.ryanair.com/farfnd/v4/oneWayFares"
ROUNDTRIP_ENDPOINT = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"

VALID_TIME_BUCKETS = ["morning", "lunch", "evening"]

DEFAULT_STATE = {
    "max_price": 20.0,
    "round_trip_max_price": 40.0,
    "date_from": date.today().isoformat(),
    "date_to": (date.today() + timedelta(days=90)).isoformat(),
    "min_nights": 2,
    "max_nights": 10,
    "time_buckets": ["morning", "lunch", "evening"],  # all enabled by default
    "seen_deals": [],        # one-way, keyed by dest-date-price
    "seen_round_trips": [],  # round-trip, keyed by dest-outdate-indate-price
    "last_check": None,
}


def load_state():
    if STATE_FILE.exists():
        try:
            return {**DEFAULT_STATE, **json.loads(STATE_FILE.read_text())}
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


def booking_link(to_code, out_date, in_date=None):
    """Best-effort Ryanair booking search link. Verify it lands correctly - the
    exact query params Ryanair expects can change without notice."""
    base = "https://www.ryanair.com/pl/en/trip/flights/select"
    params = (
        f"?adults=1&teens=0&children=0&infants=0"
        f"&dateOut={out_date}&dateIn={in_date or ''}"
        f"&isConnectedFlight=false&discount=0&promoCode="
        f"&isReturn={'true' if in_date else 'false'}"
        f"&originIata={DEPARTURE_AIRPORT}&destinationIata={to_code}"
    )
    return base + params


# ---------------------------------------------------------------------------
# ONE-WAY FARES
# ---------------------------------------------------------------------------
def fetch_oneway_fares(departure_airport, date_from, date_to, max_price, time_buckets, debug=False):
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
    return f"{d['to_code']}-{d['date']}-{d['price']}"


# ---------------------------------------------------------------------------
# ROUND-TRIP FARES
# ---------------------------------------------------------------------------
def fetch_roundtrip_fares(departure_airport, date_from, date_to, min_nights, max_nights,
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
    return f"{d['to_code']}-{d['out_date']}-{d['in_date']}-{d['price']}"


# ---------------------------------------------------------------------------
# CHECK JOB
# ---------------------------------------------------------------------------
async def check_for_deals(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    lines = []

    try:
        oneway = fetch_oneway_fares(
            DEPARTURE_AIRPORT, state["date_from"], state["date_to"],
            state["max_price"], state["time_buckets"],
        )
        new_oneway = [d for d in oneway if oneway_key(d) not in state["seen_deals"]]
        for d in new_oneway:
            link = booking_link(d["to_code"], d["date"][:10])
            lines.append(
                f"✈️ *One-way:* {d['from']} → {d['to']} on {d['date'][:10]}: "
                f"*{d['price']:.2f} {d['currency']}*\n[Book →]({link})"
            )
            state["seen_deals"].append(oneway_key(d))
    except Exception as e:
        log.error(f"One-way fetch failed: {e}")

    try:
        roundtrip = fetch_roundtrip_fares(
            DEPARTURE_AIRPORT, state["date_from"], state["date_to"],
            state["min_nights"], state["max_nights"],
            state["round_trip_max_price"], state["time_buckets"],
        )
        new_roundtrip = [d for d in roundtrip if roundtrip_key(d) not in state["seen_round_trips"]]
        for d in new_roundtrip:
            link = booking_link(d["to_code"], d["out_date"][:10], d["in_date"][:10])
            lines.append(
                f"🔁 *Round trip:* {d['from']} → {d['to']}\n"
                f"Out {d['out_date'][:10]} / Back {d['in_date'][:10]}: "
                f"*{d['price']:.2f} {d['currency']}* total\n[Book →]({link})"
            )
            state["seen_round_trips"].append(roundtrip_key(d))
    except Exception as e:
        log.error(f"Round-trip fetch failed: {e}")

    if lines:
        header = "🎉 *New Ryanair deals!*\n\n"
        batch = header
        batches = []
        for line in lines:
            if len(batch) + len(line) + 2 > 3500:
                batches.append(batch)
                batch = ""
            batch += line + "\n\n"
        if batch:
            batches.append(batch)

        for b in batches:
            await context.bot.send_message(
                chat_id=CHAT_ID, text=b, parse_mode="Markdown", disable_web_page_preview=True
            )
        log.info(f"Sent {len(lines)} new deal(s) across {len(batches)} message(s)")
    else:
        log.info("No new deals this check")

    state["seen_deals"] = state["seen_deals"][-500:]
    state["seen_round_trips"] = state["seen_round_trips"][-500:]
    state["last_check"] = datetime.now().isoformat(timespec="minutes")
    save_state(state)


# ---------------------------------------------------------------------------
# BUTTON MENUS
# ---------------------------------------------------------------------------
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📊 Status", "🔍 Check now"],
        ["💶 One-way price", "🔁 Round-trip price"],
        ["🕐 Times of day", "❓ Help"],
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


# ---------------------------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    await update.message.reply_text(
        f"Ryanair Deal Bot is running.\n"
        f"From: {DEPARTURE_AIRPORT}\n"
        f"One-way max: {state['max_price']} EUR\n"
        f"Round-trip max: {state['round_trip_max_price']} EUR\n"
        f"Dates: {state['date_from']} to {state['date_to']}\n"
        f"Trip length: {state['min_nights']}-{state['max_nights']} nights\n"
        f"Times: {', '.join(state['time_buckets'])}\n"
        f"Checking every {CHECK_INTERVAL_MINUTES} minutes.\n\n"
        f"Use the buttons below, or these commands for dates/trip length:\n"
        f"/setdates 2026-08-01 2026-09-30\n"
        f"/settrip 2 10",
        reply_markup=MAIN_MENU,
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
    save_state(state)
    await update.message.reply_text(f"Date range set to {date_from} → {date_to}.")


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
    await update.message.reply_text(
        f"One-way max: {state['max_price']} EUR\n"
        f"Round-trip max: {state['round_trip_max_price']} EUR\n"
        f"Dates: {state['date_from']} to {state['date_to']}\n"
        f"Trip length: {state['min_nights']}-{state['max_nights']} nights\n"
        f"Times: {', '.join(state['time_buckets'])}\n"
        f"Last check: {state['last_check'] or 'never'}\n"
        f"One-way deals alerted: {len(state['seen_deals'])}\n"
        f"Round-trip deals alerted: {len(state['seen_round_trips'])}"
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"This chat's ID is: {update.effective_chat.id}")


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Status":
        await cmd_status(update, context)
    elif text == "🔍 Check now":
        await cmd_check(update, context)
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
    elif text == "❓ Help":
        await cmd_start(update, context)


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


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Checking now...")
    await check_for_deals(context)
    await update.message.reply_text("Done - see above if anything new was found.")


def main():
    if "--debug" in sys.argv:
        state = load_state()
        deals = fetch_oneway_fares(DEPARTURE_AIRPORT, state["date_from"], state["date_to"],
                                    state["max_price"], state["time_buckets"], debug=True)
        print(f"\nParsed {len(deals)} one-way deal(s):")
        for d in deals:
            print(d)
        return

    if "--debug-return" in sys.argv:
        state = load_state()
        deals = fetch_roundtrip_fares(DEPARTURE_AIRPORT, state["date_from"], state["date_to"],
                                       state["min_nights"], state["max_nights"],
                                       state["round_trip_max_price"], state["time_buckets"], debug=True)
        print(f"\nParsed {len(deals)} round-trip deal(s):")
        for d in deals:
            print(d)
        return

    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE" or CHAT_ID == "PUT_YOUR_CHAT_ID_HERE":
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setmax", cmd_setmax))
    app.add_handler(CommandHandler("setmaxreturn", cmd_setmaxreturn))
    app.add_handler(CommandHandler("setdates", cmd_setdates))
    app.add_handler(CommandHandler("settrip", cmd_settrip))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CallbackQueryHandler(handle_callback_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_button))

    app.job_queue.run_repeating(check_for_deals, interval=CHECK_INTERVAL_MINUTES * 60, first=10)

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

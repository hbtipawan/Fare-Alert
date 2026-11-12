#!/usr/bin/env python3
"""
Flight Fare Alert -> Telegram
-----------------------------
Runs on a schedule (GitHub Actions). Each run:
  1. Reads new Telegram commands you sent the bot (/add, /list, /remove ...)
  2. Checks live Google Flights fares for every active watch
  3. Sends a Telegram alert when the cheapest fare is at or below your limit
  4. Saves routes + state back to the repo

Env vars:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather          (required)
  TELEGRAM_CHAT_ID     your chat id (only this chat is obeyed) (required)
  CHECK_EVERY_MIN      minutes between fare checks (default 120)
  REALERT_HOURS        repeat alert for same fare after N hours (default 24)
  DRY_RUN=1            print instead of sending to Telegram
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from fast_flights import FlightQuery, Passengers, ShoppingOptions, create_query, get_flights

ROOT = Path(__file__).resolve().parent
ROUTES_FILE = ROOT / "routes.json"
STATE_FILE = ROOT / "state.json"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CHECK_EVERY_MIN = int(os.environ.get("CHECK_EVERY_MIN", "120"))
REALERT_HOURS = float(os.environ.get("REALERT_HOURS", "24"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FORCE_CHECK = os.environ.get("FORCE_CHECK") == "1"

MAX_DATES_PER_WATCH = 31          # keep Google requests reasonable
PAUSE_BETWEEN_SEARCHES = 2.0      # seconds, be polite to Google
IST = timezone(timedelta(hours=5, minutes=30))
IATA = re.compile(r"^[A-Z]{3}$")


# ----------------------------------------------------------------- storage
def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception:
        print(f"WARN: could not read {path.name}, starting fresh", file=sys.stderr)
        return default


def save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def rupees(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = int(round(n))
    s = str(n)
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "₹" + ",".join(parts) + "," + tail


# ---------------------------------------------------------------- telegram
def tg(method: str, **params):
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        print(f"Telegram {method} failed: {data}", file=sys.stderr)
    return data


def send(text: str) -> None:
    if DRY_RUN or not TOKEN:
        print("---- TELEGRAM ----\n" + text + "\n------------------")
        return
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        tg("sendMessage", chat_id=CHAT_ID, text=chunk, parse_mode="HTML",
           disable_web_page_preview=True)


# ------------------------------------------------------------ date parsing
def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Bad date '{s}'. Use 2026-12-20 or 20-12-2026")


def parse_dates(token: str) -> tuple[date, date]:
    """'2026-12-20' | '2026-12-18:2026-12-24' | '2026-12' (whole month)."""
    if re.fullmatch(r"\d{4}-\d{2}", token):
        y, m = map(int, token.split("-"))
        start = date(y, m, 1)
        end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
        return start, end
    for sep in (":", "..", "to"):
        if sep in token:
            a, b = token.split(sep, 1)
            start, end = parse_date(a), parse_date(b)
            if end < start:
                start, end = end, start
            return start, end
    d = parse_date(token)
    return d, d


def watch_dates(w: dict) -> list[date]:
    start = max(date.fromisoformat(w["start"]), datetime.now(IST).date())
    end = date.fromisoformat(w["end"])
    out, d = [], start
    while d <= end and len(out) < MAX_DATES_PER_WATCH:
        out.append(d)
        d += timedelta(days=1)
    return out


def describe(w: dict) -> str:
    when = w["start"] if w["start"] == w["end"] else f'{w["start"]} → {w["end"]}'
    extra = []
    if w.get("nonstop"):
        extra.append("non-stop")
    if w.get("adults", 1) > 1:
        extra.append(f'{w["adults"]} adults')
    if w.get("paused"):
        extra.append("⏸ paused")
    tail = f' ({", ".join(extra)})' if extra else ""
    return f'#{w["id"]} <b>{w["from"]}→{w["to"]}</b> {when} · limit {rupees(w["limit"])}{tail}'


# ---------------------------------------------------------------- commands
HELP = """<b>✈️ Fare Alert Bot</b>

<b>Add a watch</b> (alert when fare ≤ limit, per person, one-way):
<code>/add AMD DEL 2026-12-20 4500</code>
<code>/add BOM BLR 2026-12-18:2026-12-24 3500</code>  (date range)
<code>/add DEL GOI 2027-01 4000</code>  (whole month)
Add <code>nonstop</code> and/or <code>2pax</code> at the end if needed:
<code>/add BDQ DEL 2026-12-20 5000 nonstop 2pax</code>

<b>Manage</b>
/list – all watches with latest fare
/remove 3 – delete watch #3
/limit 3 4000 – change limit of #3
/pause 3 · /resume 3
/check – force a fare check on next run
/help

Use 3-letter airport codes (AMD Ahmedabad, BDQ Vadodara, STV Surat, BOM Mumbai, DEL Delhi, BLR Bengaluru, MAA Chennai, HYD Hyderabad, CCU Kolkata, GOI/GOX Goa, LKO Lucknow, GOP Gorakhpur, VNS Varanasi).
Return trip = add a second watch the other way.
Commands are processed on the next scheduled run (≈ every 30 min)."""


def handle_command(text: str, routes: dict, state: dict) -> str:
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]
    watches = routes["watches"]

    def find(idx: str) -> dict:
        for w in watches:
            if str(w["id"]) == idx.lstrip("#"):
                return w
        raise ValueError(f"No watch #{idx}. Send /list")

    if cmd in ("/start", "/help"):
        return HELP

    if cmd == "/add":
        if len(args) < 4:
            raise ValueError("Format: /add FROM TO DATE LIMIT  e.g. /add AMD DEL 2026-12-20 4500")
        frm, to = args[0].upper(), args[1].upper()
        if not (IATA.match(frm) and IATA.match(to)) or frm == to:
            raise ValueError("Use two different 3-letter airport codes, e.g. AMD DEL")
        start, end = parse_dates(args[2])
        if end < datetime.now(IST).date():
            raise ValueError("That date is already past")
        limit = int(re.sub(r"[^\d]", "", args[3]) or 0)
        if limit <= 0:
            raise ValueError("Limit must be a rupee amount, e.g. 4500")
        flags = [a.lower() for a in args[4:]]
        adults = 1
        for f in flags:
            m = re.fullmatch(r"(\d)(pax|adults?|a)", f)
            if m:
                adults = max(1, min(9, int(m.group(1))))
        n_days = (end - start).days + 1
        w = {
            "id": routes["next_id"], "from": frm, "to": to,
            "start": start.isoformat(), "end": end.isoformat(),
            "limit": limit, "nonstop": "nonstop" in flags or "direct" in flags,
            "adults": adults, "paused": False,
            "created": now_utc().isoformat(timespec="seconds"),
        }
        watches.append(w)
        routes["next_id"] += 1
        state["force_check"] = True
        note = ""
        if n_days > MAX_DATES_PER_WATCH:
            note = f"\n⚠️ Range is {n_days} days; only the nearest {MAX_DATES_PER_WATCH} days are checked each run."
        return f"✅ Added {describe(w)}\nFirst fare check on the next run.{note}"

    if cmd == "/list":
        if not watches:
            return "No watches yet. Example:\n<code>/add AMD DEL 2026-12-20 4500</code>"
        lines = ["<b>Your watches</b>"]
        for w in watches:
            s = state["watches"].get(str(w["id"]), {})
            last = s.get("last_price")
            best = s.get("best_price")
            info = ""
            if last:
                info = f'\n   now {rupees(last)} on {s.get("last_date")}' \
                       f' · lowest seen {rupees(best)}'
            lines.append(describe(w) + info)
        return "\n".join(lines)

    if cmd in ("/remove", "/delete", "/del"):
        if not args:
            raise ValueError("Format: /remove 3")
        w = find(args[0])
        watches.remove(w)
        state["watches"].pop(str(w["id"]), None)
        return f"🗑 Removed {describe(w)}"

    if cmd == "/limit":
        if len(args) < 2:
            raise ValueError("Format: /limit 3 4000")
        w = find(args[0])
        w["limit"] = int(re.sub(r"[^\d]", "", args[1]))
        state["watches"].setdefault(str(w["id"]), {}).pop("last_alert_price", None)
        state["force_check"] = True
        return f"✏️ Updated {describe(w)}"

    if cmd in ("/pause", "/resume"):
        if not args:
            raise ValueError(f"Format: {cmd} 3")
        w = find(args[0])
        w["paused"] = cmd == "/pause"
        return f'{"⏸" if w["paused"] else "▶️"} {describe(w)}'

    if cmd == "/check":
        state["force_check"] = True
        return "🔄 Fare check queued for this run."

    raise ValueError("Unknown command. Send /help")


def process_telegram(routes: dict, state: dict) -> None:
    if not TOKEN:
        return
    data = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=0,
              allowed_updates=["message"])
    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        if chat != CHAT_ID:
            print(f"Ignoring command from unknown chat {chat}")
            continue
        try:
            reply = handle_command(text, routes, state)
        except Exception as e:  # user error -> tell them
            reply = f"❌ {e}"
        send(reply)


# ------------------------------------------------------------ fare search
def cheapest_on(frm: str, to: str, d: date, adults: int, nonstop: bool):
    q = create_query(
        flights=[FlightQuery(date=d.isoformat(), from_airport=frm, to_airport=to,
                             max_stops=0 if nonstop else None)],
        trip="one-way", seat="economy",
        passengers=Passengers(adults=adults), currency="INR", language="en-US",
    )
    res = get_flights(q, shopping=ShoppingOptions())
    options = [f for f in res if getattr(f, "price", 0) and f.price > 0]
    if nonstop:
        options = [f for f in options if len(f.flights) == 1]
    if not options:
        return None
    best = min(options, key=lambda f: f.price)
    leg0, legN = best.flights[0], best.flights[-1]

    def hhmm(t):
        t = list(t) + [0, 0]
        return f"{t[0]:02d}:{t[1]:02d}"

    return {
        "price": int(best.price),            # total for all adults
        "date": d.isoformat(),
        "airline": ", ".join(dict.fromkeys(best.airlines)) or best.type,
        "flight": " / ".join(f"{s.airline_code}{s.flight_number}" for s in best.flights if s.flight_number),
        "dep": hhmm(leg0.departure.time), "arr": hhmm(legN.arrival.time),
        "stops": len(best.flights) - 1,
        "duration": sum(s.duration for s in best.flights),
        "options": len(options),
    }


def gflights_link(frm, to, d, adults=1):
    q = f"Flights from {frm} to {to} on {d} one way {adults} adult economy"
    return "https://www.google.com/travel/flights?q=" + quote(q) + "&curr=INR&hl=en-IN"


def check_watch(w: dict, s: dict) -> str | None:
    """Search all dates of a watch. Returns alert text or None."""
    dates = watch_dates(w)
    if not dates:
        return None
    results, errors = [], 0
    for d in dates:
        try:
            r = cheapest_on(w["from"], w["to"], d, w.get("adults", 1), w.get("nonstop", False))
            if r:
                results.append(r)
        except Exception as e:
            errors += 1
            print(f"  {w['from']}-{w['to']} {d}: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(PAUSE_BETWEEN_SEARCHES)

    s["last_check"] = now_utc().isoformat(timespec="seconds")
    if not results:
        s["fail_streak"] = s.get("fail_streak", 0) + 1
        print(f"  #{w['id']}: no prices ({errors} errors)")
        return None
    s["fail_streak"] = 0

    adults = w.get("adults", 1)
    best = min(results, key=lambda r: r["price"])
    per_person = best["price"] / adults
    prev = s.get("last_price")
    s.update(last_price=round(per_person), last_date=best["date"])
    if not s.get("best_price") or per_person < s["best_price"]:
        s["best_price"] = round(per_person)
        s["best_when"] = s["last_check"]
    hist = s.setdefault("history", [])
    hist.append([s["last_check"][:16], round(per_person), best["date"]])
    del hist[:-60]
    print(f"  #{w['id']} {w['from']}-{w['to']}: {rupees(per_person)} on {best['date']} "
          f"(limit {rupees(w['limit'])}, {len(results)}/{len(dates)} dates ok)")

    if per_person > w["limit"]:
        s.pop("last_alert_price", None)       # re-arm: next dip alerts again
        return None

    # Below limit: alert if first time, a new lower price, or REALERT_HOURS passed
    last_alert = s.get("last_alert_price")
    last_alert_at = s.get("last_alert_at")
    hours_since = 1e9
    if last_alert_at:
        hours_since = (now_utc() - datetime.fromisoformat(last_alert_at)).total_seconds() / 3600
    lower = last_alert is None or per_person <= last_alert - max(50, 0.01 * last_alert)
    if not (lower or hours_since >= REALERT_HOURS):
        return None
    s["last_alert_price"] = round(per_person)
    s["last_alert_at"] = s["last_check"]

    trend = ""
    if prev and round(per_person) != prev:
        diff = per_person - prev
        trend = f" ({'▼' if diff < 0 else '▲'} {rupees(abs(diff))} vs last check)"
    stops = "non-stop" if best["stops"] == 0 else f'{best["stops"]} stop'
    dur = f'{best["duration"] // 60}h {best["duration"] % 60:02d}m'
    pax = f" per person (total {rupees(best['price'])} for {adults})" if adults > 1 else ""
    others = sorted(results, key=lambda r: r["price"])[1:4]
    alt = ""
    if others and len(dates) > 1:
        alt = "\nOther cheap dates: " + ", ".join(
            f'{r["date"][5:]} {rupees(r["price"] / adults)}' for r in others)
    d_fmt = datetime.fromisoformat(best["date"]).strftime("%a %d %b %Y")
    return (
        f"🔔 <b>FARE ALERT  {w['from']} → {w['to']}</b>\n"
        f"<b>{rupees(per_person)}</b>{pax} ≤ your limit {rupees(w['limit'])}{trend}\n"
        f"📅 {d_fmt}\n"
        f"✈️ {best['airline']} {best['flight']} · {best['dep']}→{best['arr']} · {stops} · {dur}"
        f"{alt}\n"
        f"Lowest seen so far: {rupees(s['best_price'])}\n"
        f'<a href="{gflights_link(w["from"], w["to"], best["date"], adults)}">Open in Google Flights</a>'
        f"  ·  watch #{w['id']}"
    )


# ------------------------------------------------------------------- main
def main() -> int:
    if not DRY_RUN and (not TOKEN or not CHAT_ID):
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or DRY_RUN=1)", file=sys.stderr)
        return 1
    routes = load(ROUTES_FILE, {"next_id": 1, "watches": []})
    routes.setdefault("next_id", 1)
    routes.setdefault("watches", [])
    state = load(STATE_FILE, {})
    state.setdefault("watches", {})

    process_telegram(routes, state)

    # auto-remove watches whose dates are over
    today = datetime.now(IST).date()
    for w in list(routes["watches"]):
        if date.fromisoformat(w["end"]) < today:
            routes["watches"].remove(w)
            state["watches"].pop(str(w["id"]), None)
            send(f"🏁 Travel date passed, removed {describe(w)}")

    last = state.get("last_full_check")
    due = FORCE_CHECK or state.pop("force_check", False) or not last or \
        (now_utc() - datetime.fromisoformat(last)) >= timedelta(minutes=CHECK_EVERY_MIN - 5)

    if due:
        print(f"Checking fares at {datetime.now(IST):%Y-%m-%d %H:%M} IST")
        total_fail = 0
        active = [w for w in routes["watches"] if not w.get("paused")]
        for w in active:
            s = state["watches"].setdefault(str(w["id"]), {})
            try:
                alert = check_watch(w, s)
                if alert:
                    send(alert)
            except Exception:
                traceback.print_exc()
            if s.get("fail_streak", 0) >= 1:
                total_fail += 1
        state["last_full_check"] = now_utc().isoformat(timespec="seconds")
        # warn once if every watch failed 3 runs in a row (Google blocking / layout change)
        if active and all(state["watches"].get(str(w["id"]), {}).get("fail_streak", 0) >= 3 for w in active):
            if not state.get("warned_failing"):
                send("⚠️ Fare checks have failed 3 runs in a row. Google may be blocking the "
                     "GitHub server or changed its page. Check the Actions log.")
                state["warned_failing"] = True
        else:
            state["warned_failing"] = False
    else:
        print("Fare check not due yet; only processed commands.")

    save(ROUTES_FILE, routes)
    save(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

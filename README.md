# ✈️ Flight Fare Alert → Telegram

Watches live Google Flights fares (in ₹) for any routes you choose and sends a Telegram message when the cheapest fare goes **at or below your limit**. It runs free on GitHub Actions, so your PC can stay off. You control it entirely from Telegram.

## Setup (about 10 minutes)

### 1. Create your Telegram bot
1. In Telegram, open **@BotFather** → send `/newbot` → pick a name, e.g. `Pawan Fare Bot`.
2. Copy the **bot token** it gives you (looks like `7712345678:AAH...`).
3. Open your new bot and send it `hi`.
4. In a browser, open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`. Find `"chat":{"id":123456789` — that number is your **chat id**.

### 2. Put the code on GitHub
1. Create a new repo, e.g. `fare-alert` (see the note on minutes below).
2. Upload all files from this folder, **including the `.github/workflows` folder**.
3. Go to repo **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat id
4. Go to **Settings → Actions → General → Workflow permissions** → choose **Read and write** → Save.
5. Go to the **Actions** tab → *Flight fare alert* → **Run workflow** once to test.

### 3. Use it from Telegram
```
/add AMD DEL 2026-12-20 4500                  one date
/add BOM BLR 2026-12-18:2026-12-24 3500       date range (checks every day, alerts on the cheapest)
/add DEL GOI 2027-01 4000                     whole month
/add BDQ DEL 2026-12-20 5000 nonstop 2pax     non-stop only, 2 passengers (limit is per person)
/list            watches + latest and lowest fare seen
/limit 3 4000    change the limit on watch #3
/pause 3  /resume 3  /remove 3
/check           force a fare check on the next run
/help
```
Each watch is one-way. For a return trip, add a second watch in the other direction.

## How it behaves
| Setting | Default | Where to change |
|---|---|---|
| Reads your Telegram commands | every 30 min | `cron` in `.github/workflows/fare-alert.yml` |
| Checks fares | every 2 h | `CHECK_EVERY_MIN` in the workflow |
| Alerts | when the fare first drops ≤ limit, each time it falls further, and a daily reminder while it stays below | `REALERT_HOURS` |
| Past travel dates | removed automatically | — |
| Max dates per watch | nearest 31 days | `MAX_DATES_PER_WATCH` in `fare_bot.py` |

If the fare goes back above your limit, the alert re-arms, so the next dip alerts you again. If checks fail 3 runs in a row, the bot warns you on Telegram.

## GitHub minutes
- **Public repo**: Actions minutes are free and unlimited. Your routes and dates will be visible to anyone; your bot token stays secret.
- **Private repo**: you get 2,000 free minutes a month. Running every 30 min uses about that much, so change the cron to `"0 * * * *"` (hourly).
- GitHub's scheduled runs can start 5–15 min late at busy times. That's normal.

## Limits to know
- Fares come from Google Flights through the open-source `faster-flights` library. This isn't an official API: if Google changes its site, update the version in `requirements.txt` (`pip install -U faster-flights`).
- The fare shown is Google's cheapest economy fare at check time. The airline or OTA price can differ slightly (convenience fee, baggage).

## Test locally (optional)
```
pip install -r requirements.txt
DRY_RUN=1 FORCE_CHECK=1 python fare_bot.py     # prints alerts instead of sending
```

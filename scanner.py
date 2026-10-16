
import json
import os
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

API = "https://api-contract.weex.com"
STATE_FILE = Path("alerts_state.json")

THRESHOLD = float(os.getenv("PUMP_THRESHOLD", "80"))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=35) as response:
        return json.load(response)


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram configuration missing")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.load(response)

    return result.get("ok", False)


def main():
    print("Starting WEEX Pump Short Scanner")

    url = API + "/capi/v3/market/ticker/24hr"
    tickers = get_json(url)

    if not isinstance(tickers, list) or not tickers:
        raise RuntimeError("WEEX returned no ticker list")

    print(f"Received {len(tickers)} tickers")

    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {}

    active = set()
    pumps = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if not symbol.endswith("USDT"):
            continue

        try:
            change = float(ticker["priceChangePercent"]) * 100
            price = float(ticker["lastPrice"])
        except (ValueError, TypeError, KeyError):
            continue

        if change >= THRESHOLD:
            active.add(symbol)
            pumps.append((symbol, change, price))

    pumps.sort(key=lambda x: x[1], reverse=True)

    print(f"Pumps above {THRESHOLD}%: {len(pumps)}")

    for symbol, change, price in pumps:
        print(f"{symbol}: +{change:.2f}% | {price}")

        if symbol in state:
            continue

        message = (
            "🚨 WEEX PUMP ALERT\n\n"
            f"Coin: {symbol}\n"
            f"24h change: +{change:.2f}%\n"
            f"Price: {price}\n\n"
            "Check manually before trading."
        )

        if send_telegram(message):
            state[symbol] = datetime.now(
                timezone.utc
            ).isoformat()
            print(f"Alert sent: {symbol}")

    state = {
        symbol: timestamp
        for symbol, timestamp in state.items()
        if symbol in active
    }

    STATE_FILE.write_text(
        json.dumps(state, indent=2)
    )

    print("Scan completed")


if __name__ == "__main__":
    main()

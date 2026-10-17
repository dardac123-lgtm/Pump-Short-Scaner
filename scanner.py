import json
import os
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

API = "https://api-contract.weex.com"
STATE_FILE = Path("alerts_state.json")
HISTORY_FILE = Path("alerts_history.json")
CONFIG_FILE = Path("scanner_config.json")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=35) as response:
        return json.load(response)


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_threshold():
    # Netlify saves the threshold here. If the file is missing, retain the
    # existing GitHub Actions environment setting as a fallback.
    config = read_json(CONFIG_FILE, {})
    if not isinstance(config, dict):
        raise RuntimeError("Invalid scanner_config.json")
    threshold = float(config.get("threshold", os.getenv("PUMP_THRESHOLD", "80")))
    if not (0 < threshold <= 100000):
        raise ValueError("Threshold must be greater than 0")
    print(f"Active threshold from configuration: {threshold}%")
    return threshold


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram configuration missing")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": message}).encode()
    request = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    return result.get("ok", False)


def main():
    print("Starting WEEX Pump Short Scanner")
    threshold = get_threshold()
    tickers = get_json(API + "/capi/v3/market/ticker/24hr")

    if not isinstance(tickers, list) or not tickers:
        raise RuntimeError("WEEX returned no ticker list")

    print(f"Received {len(tickers)} tickers")

    state = read_json(STATE_FILE, {})
    history = read_json(HISTORY_FILE, [])
    if not isinstance(state, dict):
        raise RuntimeError("Invalid alerts_state.json")
    if not isinstance(history, list):
        raise RuntimeError("Invalid alerts_history.json")

    active = set()
    pumps = []
    now = datetime.now(timezone.utc).isoformat()

    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        try:
            change = float(ticker["priceChangePercent"]) * 100
            price = float(ticker["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue

        if change >= threshold:
            active.add(symbol)
            pumps.append((symbol, change, price))

    pumps.sort(key=lambda x: x[1], reverse=True)
    print(f"Pumps above {threshold}%: {len(pumps)}")

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
        event = {
            "symbol": symbol,
            "detected_at": now,
            "change_percent": round(change, 2),
            "price": price,
            "threshold": threshold,
            "telegram_sent": False,
        }
        history.append(event)
        write_json(HISTORY_FILE, history)

        if send_telegram(message):
            event["telegram_sent"] = True
            state[symbol] = now
            print(f"Alert sent: {symbol}")
        else:
            print(f"Telegram not sent: {symbol}")
        write_json(HISTORY_FILE, history)

    state = {symbol: timestamp for symbol, timestamp in state.items() if symbol in active}
    write_json(STATE_FILE, state)
    write_json(HISTORY_FILE, history)
    print(f"History records: {len(history)}")
    print("Scan completed")


if __name__ == "__main__":
    main()

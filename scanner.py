"""PUMP-SHORT WEEX 24h: one Telegram alert per continuous pump episode.

In addition to alerts, save bounded public WEEX Open Interest snapshots to
`oi_history.json` so Netlify can compare OI even when the website is closed.
Funding and OI do NOT modify the alert criteria.
"""

import json
import math
import os
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api-contract.weex.com"
STATE_FILE = Path("alerts_state.json")
HISTORY_FILE = Path("alerts_history.json")
CONFIG_FILE = Path("scanner_config.json")
OI_FILE = Path("oi_history.json")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

OI_TRACKED_LIMIT = 15
OI_KEEP_HOURS = 30
OI_MAX_SAMPLES_PER_SYMBOL = 360
OI_MAX_TOTAL_SAMPLES = 5000
OI_MIN_SAMPLE_INTERVAL_SECONDS = 240


def get_json(url, timeout=35):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_number(raw):
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def get_threshold():
    config = read_json(CONFIG_FILE, {})
    if not isinstance(config, dict):
        raise RuntimeError("Invalid scanner_config.json")
    threshold = safe_number(config.get("threshold", os.getenv("PUMP_THRESHOLD", "80")))
    if threshold is None or not 1 <= threshold <= 1000:
        raise ValueError("Threshold must be between 1 and 1000%")
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


def fetch_open_interest(symbol):
    """WEEX returns openInterest in contracts, not USDT."""
    try:
        query = urllib.parse.urlencode({"symbol": symbol})
        data = get_json(API + "/capi/v3/market/openInterest?" + query, timeout=9)
        if not isinstance(data, dict):
            return symbol, None
        number = safe_number(data.get("openInterest"))
        return symbol, number if number is not None and number > 0 else None
    except Exception as exc:
        print(f"OI unavailable for {symbol}: {type(exc).__name__}")
        return symbol, None


def store_oi_snapshots(current_pumps, now):
    """Best effort. Failure here must never prevent Telegram alerts."""
    data = read_json(OI_FILE, {})
    if not isinstance(data, dict):
        print("Invalid OI history, starting new measurement store")
        data = {}
    cutoff = now - timedelta(hours=OI_KEEP_HOURS)
    now_stamp = now.isoformat()

    cleaned = {}
    for symbol, samples in data.items():
        if not isinstance(symbol, str) or not isinstance(samples, list):
            continue
        valid = []
        for item in samples[-500:]:
            if not isinstance(item, dict):
                continue
            try:
                t = datetime.fromisoformat(str(item["t"]).replace("Z", "+00:00"))
                oi = safe_number(item.get("oi"))
                price = safe_number(item.get("price"))
                if t.tzinfo is None or not cutoff <= t.astimezone(timezone.utc) <= now:
                    continue
                if oi is None or price is None or oi <= 0 or price <= 0:
                    continue
                valid.append({"t": t.astimezone(timezone.utc).isoformat(), "oi": oi, "price": price})
            except (KeyError, TypeError, ValueError):
                continue
        if valid:
            valid.sort(key=lambda p: p["t"])
            cleaned[symbol] = valid[-OI_MAX_SAMPLES_PER_SYMBOL:]

    selected = current_pumps[:OI_TRACKED_LIMIT]
    fetched = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = [pool.submit(fetch_open_interest, symbol) for symbol, _, _ in selected]
        for job in as_completed(jobs):
            symbol, amount = job.result()
            fetched[symbol] = amount

    added = 0
    for symbol, _, price in selected:
        oi = fetched.get(symbol)
        if oi is None:
            continue
        samples = cleaned.setdefault(symbol, [])
        if samples:
            previous = datetime.fromisoformat(samples[-1]["t"])
            if (now - previous).total_seconds() < OI_MIN_SAMPLE_INTERVAL_SECONDS:
                continue
        samples.append({"t": now_stamp, "oi": oi, "price": price})
        cleaned[symbol] = samples[-OI_MAX_SAMPLES_PER_SYMBOL:]
        added += 1

    # Bound the GitHub Contents API payload without discarding today's history
    # of a continuously active pump unnecessarily.
    all_rows = [(point["t"], symbol, point) for symbol, rows in cleaned.items() for point in rows]
    if len(all_rows) > OI_MAX_TOTAL_SAMPLES:
        all_rows.sort(key=lambda row: row[0], reverse=True)
        keep = set((symbol, point["t"]) for _, symbol, point in all_rows[:OI_MAX_TOTAL_SAMPLES])
        cleaned = {
            symbol: [point for point in rows if (symbol, point["t"]) in keep]
            for symbol, rows in cleaned.items()
        }
        cleaned = {symbol: rows for symbol, rows in cleaned.items() if rows}

    write_json(OI_FILE, cleaned)
    print(f"OI snapshots saved: {added}; tracked coins: {len(cleaned)}")


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
    now = datetime.now(timezone.utc)
    now_stamp = now.isoformat()
    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue
        symbol = ticker.get("symbol", "")
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            continue
        change_ratio = safe_number(ticker.get("priceChangePercent"))
        price = safe_number(ticker.get("lastPrice"))
        if change_ratio is None or price is None or price <= 0:
            continue
        change = change_ratio * 100  # WEEX supplies a fraction: 0.8 means +80%.
        if change >= threshold:
            active.add(symbol)
            pumps.append((symbol, change, price))
    pumps.sort(key=lambda x: x[1], reverse=True)
    print(f"Pumps above {threshold}%: {len(pumps)}")

    # Preserve the original alert logic and message exactly.
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
            "detected_at": now_stamp,
            "change_percent": round(change, 2),
            "price": price,
            "threshold": threshold,
            "telegram_sent": False,
        }
        history.append(event)
        write_json(HISTORY_FILE, history)
        try:
            sent = send_telegram(message)
        except Exception as exc:
            print(f"Telegram send failed for {symbol}: {type(exc).__name__}")
            sent = False
        if sent:
            event["telegram_sent"] = True
            state[symbol] = now_stamp
            print(f"Alert sent: {symbol}")
        else:
            print(f"Telegram not sent: {symbol}")
        write_json(HISTORY_FILE, history)

    state = {symbol: timestamp for symbol, timestamp in state.items() if symbol in active}
    write_json(STATE_FILE, state)
    write_json(HISTORY_FILE, history)

    # Historical OI is auxiliary. A temporary WEEX API error must not fail alerts.
    try:
        store_oi_snapshots(pumps, now)
    except Exception as exc:
        print(f"OI collection skipped: {type(exc).__name__}: {exc}")

    print(f"History records: {len(history)}")
    print("Scan completed")


if __name__ == "__main__":
    main()

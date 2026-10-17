"""PUMP-SHORT WEEX: one Telegram alert per continuous threshold episode.

Funding and OI are supplementary information. They never block an alert.
"""

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api-contract.weex.com"
STATE_FILE = Path("alerts_state.json")
HISTORY_FILE = Path("alerts_history.json")
CONFIG_FILE = Path("scanner_config.json")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


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


def get_threshold():
    config = read_json(CONFIG_FILE, {})
    if not isinstance(config, dict):
        raise RuntimeError("Invalid scanner_config.json")
    threshold = float(config.get("threshold", os.getenv("PUMP_THRESHOLD", "80")))
    if not math.isfinite(threshold) or not (0 < threshold <= 100000):
        raise ValueError("Threshold must be a positive finite number")
    print(f"Active threshold from configuration: {threshold}%")
    return threshold


def safe_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def fetch_funding_by_symbol():
    """One bulk request when at least one *new* alert is needed."""
    try:
        values = get_json(API + "/capi/v3/market/premiumIndex", timeout=8)
        if not isinstance(values, list):
            print("Funding: unexpected WEEX response, continuing without funding")
            return {}
        return {item["symbol"]: item for item in values if isinstance(item, dict) and isinstance(item.get("symbol"), str)}
    except Exception as exc:
        print(f"Funding temporarily unavailable: {type(exc).__name__}")
        return {}


def fetch_open_interest(symbol):
    """WEEX openInterest is contracts, NOT USDT and NOT 24h OI change."""
    try:
        query = urllib.parse.urlencode({"symbol": symbol})
        value = get_json(API + "/capi/v3/market/openInterest?" + query, timeout=8)
        if isinstance(value, dict):
            return safe_number(value.get("openInterest"))
    except Exception as exc:
        print(f"Open interest unavailable for {symbol}: {type(exc).__name__}")
    return None


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
        if not isinstance(ticker, dict):
            continue
        symbol = ticker.get("symbol", "")
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            continue
        change_ratio = safe_number(ticker.get("priceChangePercent"))
        price = safe_number(ticker.get("lastPrice"))
        if change_ratio is None or price is None or price <= 0:
            continue
        change = change_ratio * 100  # WEEX priceChangePercent uses fraction, e.g. 0.8 == 80%.
        if change >= threshold:
            active.add(symbol)
            pumps.append((symbol, change, price))

    pumps.sort(key=lambda row: row[1], reverse=True)
    print(f"Pumps above {threshold}%: {len(pumps)}")

    new_pumps = [entry for entry in pumps if entry[0] not in state]
    funding_by_symbol = fetch_funding_by_symbol() if new_pumps else {}

    for symbol, change, price in pumps:
        print(f"{symbol}: +{change:.2f}% | {price}")
        if symbol in state:
            continue

        funding = funding_by_symbol.get(symbol, {})
        last_funding = safe_number(funding.get("lastFundingRate"))
        forecast_funding = safe_number(funding.get("forecastFundingRate"))
        oi_contracts = fetch_open_interest(symbol)

        funding_text = f"{forecast_funding * 100:+.4f}%" if forecast_funding is not None else "brak danych"
        oi_text = f"{oi_contracts:,.2f} kontraktow" if oi_contracts is not None else "brak danych"
        message = (
            "🚨 WEEX PUMP ALERT\n\n"
            f"Coin: {symbol}\n"
            f"24h change: +{change:.2f}%\n"
            f"Price: {price} USDT\n"
            f"Funding (prognoza): {funding_text}\n"
            f"Open Interest: {oi_text}\n\n"
            "Funding i OI sa danymi pomocniczymi, nie sygnalem SHORT."
        )

        event = {
            "symbol": symbol,
            "detected_at": now,
            "change_percent": round(change, 2),
            "price": price,
            "threshold": threshold,
            "telegram_sent": False,
        }
        if last_funding is not None:
            event["last_funding_percent"] = round(last_funding * 100, 6)
        if forecast_funding is not None:
            event["forecast_funding_percent"] = round(forecast_funding * 100, 6)
        if oi_contracts is not None:
            event["open_interest_contracts"] = oi_contracts

        history.append(event)
        write_json(HISTORY_FILE, history)
        try:
            delivered = send_telegram(message)
        except Exception as exc:
            print(f"Telegram send failed for {symbol}: {type(exc).__name__}")
            delivered = False

        if delivered:
            event["telegram_sent"] = True
            state[symbol] = now
            print(f"Alert sent: {symbol}")
        else:
            print(f"Telegram not sent: {symbol}")
        write_json(HISTORY_FILE, history)

    # Preserve established semantics: one alert while symbol stays above the threshold.
    state = {symbol: timestamp for symbol, timestamp in state.items() if symbol in active}
    write_json(STATE_FILE, state)
    write_json(HISTORY_FILE, history)
    print(f"History records: {len(history)}")
    print("Scan completed")


if __name__ == "__main__":
    main()

"""PUMP SHORT: Bybit USDT perpetual pump alerts, GitHub Actions edition.
No trading actions. Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = 'https://api.bybit.com'
THRESHOLD = float(os.getenv('PUMP_THRESHOLD', '80'))
STATE = Path('alerts_state.json')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')


def get_json(url, params=None):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'PumpShortScanner/2.0', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=35) as resp:
        data = json.load(resp)
    if data.get('retCode') != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retCode')} {data.get('retMsg')}")
    return data['result']


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret')
    body = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': message, 'disable_web_page_preview': 'true'}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', data=body)
    with urllib.request.urlopen(req, timeout=25) as resp:
        result = json.load(resp)
    if not result.get('ok'):
        raise RuntimeError('Telegram rejected the message')


def main():
    if THRESHOLD <= 0:
        raise ValueError('PUMP_THRESHOLD must be positive')
    print('Fetching Bybit linear perpetual tickers...', flush=True)
    tickers = get_json(API + '/v5/market/tickers', {'category': 'linear'})['list']
    print('Tickers received:', len(tickers), flush=True)
    try:
        state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    now = int(time.time())
    current = set()
    candidates = []
    for t in tickers:
        sym = t.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
        # Bybit 24h price change is a rolling 24-hour percentage, not a candle-close signal.
        try:
            rise = float(t['price24hPcnt']) * 100
            price = float(t['lastPrice'])
        except (KeyError, ValueError, TypeError):
            continue
        if rise < THRESHOLD or price <= 0:
            continue
        current.add(sym)
        candidates.append((sym, rise, price, t))
    print('Pumps above threshold:', len(candidates), flush=True)
    new_count = 0
    for sym, rise, price, ticker in sorted(candidates, key=lambda x: -x[1]):
        if sym in state:
            print('Already alerted:', sym)
            continue
        funding = ticker.get('fundingRate', '?')
        try:
            funding = f'{float(funding)*100:+.4f}%'
        except (TypeError, ValueError):
            funding = 'brak danych'
        msg = (f'🚨 PUMP SHORT | {sym}\n'
               f'Bybit USDT perpetual\n'
               f'Wzrost 24h: +{rise:.1f}%\n'
               f'Cena: {price:.10g} USDT\n'
               f'Funding: {funding}\n'
               f'Wykryto UTC: {datetime.now(timezone.utc):%Y-%m-%d %H:%M}\n'
               f'Obserwacja, nie sygnał wejścia.')
        send_telegram(msg)
        state[sym] = now
        new_count += 1
        print('ALERT SENT:', sym, flush=True)
    # Re-arm only when the symbol drops below threshold; no duplicate alert while above.
    for sym in list(state):
        if sym not in current:
            del state[sym]
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('New Telegram alerts:', new_count, '| State entries:', len(state), flush=True)


if __name__ == '__main__':
    main()

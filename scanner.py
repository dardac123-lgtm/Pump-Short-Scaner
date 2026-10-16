import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://fapi.binance.com'
STATE = Path('alerts_state.json')
THRESHOLD = float(os.getenv('PUMP_THRESHOLD', '80'))
HOURS = int(os.getenv('PUMP_HOURS', '24'))
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')


def get(url, params=None):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'pump-short-scanner/1.0'})
    with urllib.request.urlopen(req, timeout=35) as response:
        return json.load(response)


def send(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError('Brakuje TELEGRAM_BOT_TOKEN lub TELEGRAM_CHAT_ID w GitHub Secrets')
    return get(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', {
        'chat_id': CHAT_ID, 'text': message, 'disable_web_page_preview': 'true'
    })


def main():
    if not 1 <= HOURS <= 72 or not 10 <= THRESHOLD <= 1000:
        raise ValueError('Nieprawidlowy prog lub okres')
    info = get(BASE + '/fapi/v1/exchangeInfo')
    symbols = {s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT'}
    tickers = get(BASE + '/fapi/v1/ticker/24hr')
    # Wstepny filtr oparty na 24h. Dla 24h sprawdzamy wszystkie potencjalne pompy.
    candidates = [t for t in tickers if t['symbol'] in symbols and float(t['priceChangePercent']) >= THRESHOLD * 0.85]
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    found = []
    for t in candidates:
        sym = t['symbol']
        try:
            candles = get(BASE + '/fapi/v1/klines', {'symbol': sym, 'interval': '1h', 'limit': HOURS + 2})
            # Przyblizenie: otwarcie swiecy godzinowej sprzed HOURS godzin.
            base = float(candles[-(HOURS + 1)][1])
            price = float(t['lastPrice'])
            growth = (price / base - 1) * 100 if base > 0 else 0
            if growth < THRESHOLD:
                continue
            found.append((sym, growth))
            last = state.get(sym)
            # Jeden alarm na symbol w ciagu 24h; nastepny dzien mozliwy kolejny alarm.
            if last and (now.timestamp() - float(last)) < 86400:
                continue
            message = (f'🚨 PUMP-SHORT: {sym}\n'
                       f'Wzrost: +{growth:.1f}% w {HOURS}h\n'
                       f'Cena: {price:.10g} USDT\n'
                       f'Obrot 24h: {float(t["quoteVolume"]):,.0f} USDT\n'
                       f'Czas UTC: {now:%Y-%m-%d %H:%M}\n'
                       f'Wykres: https://www.tradingview.com/chart/?symbol=BINANCE:{sym}.P\n'
                       'To alarm o pompie, nie sygnal wejscia SHORT.')
            send(message)
            state[sym] = now.timestamp()
            print('ALERT', sym, growth)
        except Exception as exc:
            print('ERROR', sym, type(exc).__name__, str(exc)[:150])
    # Ograniczamy wielkosc stanu.
    state = {k: v for k, v in state.items() if now.timestamp() - float(v) < 7 * 86400}
    STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
    print(f'{today}: contracts={len(symbols)}, candidates={len(candidates)}, pumps={len(found)}')


if __name__ == '__main__':
    main()

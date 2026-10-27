"""PUMP-SHORT: cicha, wstepna ocena rynku, BEZ alarmow i transakcji.

Uruchamiana *po* scanner.py przez Github Actions. Wstepna obserwacja startuje
ponizej progu glownych alarmow. Nie zmienia alerts_state.json,
alerts_history.json, scanner_config.json ani sposobu wysylania Telegramow.

Ograniczenie: odswiezanie ma czestotliwosc faktycznych uruchomien Actions,
ktore GitHub moze opozniac. To NIE jest niezawodny sygnal 1-minutowy.
"""

import json
import math
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scanner import API, OI_FILE, get_json, get_threshold, read_json, safe_number, store_oi_snapshots

RESULT_FILE = Path('pump_analysis.json')
MAX_COINS = 15
MAX_CANDLE_AGE_MIN = 12


def pct(a, b):
    return 100 * (a / b - 1) if a is not None and b is not None and b > 0 else None


def rsi14(closes):
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
    gains = sum(max(0, d) for d in deltas) / 14
    losses = sum(max(0, -d) for d in deltas) / 14
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100 - 100 / (1 + gains / losses)


def buy_share(candles):
    volume = sum(x['volume'] for x in candles)
    return 100 * sum(x['buy'] for x in candles) / volume if volume > 0 else None


def load_candles(symbol, now):
    """Tylko ZAMKNIETE 1m LAST; do 70 minut, 1 zapytanie na symbol."""
    end_ms = int((now - timedelta(minutes=2)).timestamp() * 1000)
    begin_ms = end_ms - 70 * 60000
    query = urllib.parse.urlencode({
        'symbol': symbol, 'interval': '1m', 'startTime': begin_ms,
        'endTime': end_ms, 'limit': 100, 'priceType': 'LAST'
    })
    result = get_json(API + '/capi/v3/market/historyKlines?' + query, timeout=16)
    if not isinstance(result, list):
        raise ValueError('API WEEX: nieprawidlowy format swiec')
    unique = {}
    for r in result:
        if not isinstance(r, list) or len(r) < 11:
            continue
        try:
            t = int(r[0])
            o, h, l, c = (float(v) for v in r[1:5])
            volume = float(r[7])
            buy = float(r[10])
            values = (o, h, l, c, volume, buy)
            if not all(map(math.isfinite, values)):
                continue
            if not begin_ms <= t <= end_ms or min(o, h, l, c) <= 0:
                continue
            if l > min(o, c) or h < max(o, c) or volume < 0 or not 0 <= buy <= volume * 1.001:
                continue
            unique[t] = {'t': t, 'open': o, 'high': h, 'low': l, 'close': c,
                         'volume': volume, 'buy': buy}
        except (TypeError, ValueError, OverflowError):
            continue
    candles = [unique[k] for k in sorted(unique)]
    if len(candles) < 31:
        raise ValueError(f'Za malo poprawnych swiec 1m ({len(candles)})')
    # Gdy brak ciaglych swiec, nie udajemy ze liczby reprezentuja 5 czy 15 minut.
    last = candles[-31:]
    if any(last[i]['t'] - last[i-1]['t'] != 60000 for i in range(1, len(last))):
        raise ValueError('Luki w ostatnich 30 minutach swiec 1m')
    age = (now.timestamp() * 1000 - (last[-1]['t'] + 60000)) / 60000
    if age > MAX_CANDLE_AGE_MIN or age < 0:
        raise ValueError(f'Nieaktualne swiece 1m: {age:.1f} min')
    return candles


def oi_changes(symbol, now):
    """Nie imputuje brakujacych pomiarow OI ani nie udaje minutowej historii."""
    history = read_json(OI_FILE, {})
    samples = history.get(symbol, []) if isinstance(history, dict) else []
    good = []
    for x in samples:
        try:
            ts = datetime.fromisoformat(x['t'].replace('Z', '+00:00'))
            oi = safe_number(x.get('oi'))
            if ts.tzinfo and oi and oi > 0 and ts <= now:
                good.append((ts.astimezone(timezone.utc), oi))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    good.sort()
    if not good or (now - good[-1][0]) > timedelta(minutes=15):
        return {'value_contracts': None, 'change_15m_percent': None, 'change_1h_percent': None}
    current = good[-1][1]
    result = {'value_contracts': current}
    for minutes, key, tolerance in ((15, 'change_15m_percent', 7),
                                    (60, 'change_1h_percent', 22)):
        target = good[-1][0] - timedelta(minutes=minutes)
        previous = min(good[:-1], key=lambda x: abs((x[0]-target).total_seconds()), default=None)
        if previous is None or abs((previous[0]-target).total_seconds()) > tolerance*60:
            result[key] = None
        else:
            result[key] = round(pct(current, previous[1]), 2)
    return result


def assess(candles):
    """Reguly przejrzyste, EKSPERYMENTALNE, nieprognoza skutecznosci."""
    last = candles[-1]
    close = last['close']
    closes = [x['close'] for x in candles]
    change5 = pct(close, closes[-6])
    change15 = pct(close, closes[-16])
    prev5 = pct(closes[-6], closes[-11])
    buy5 = buy_share(candles[-5:])
    buy_prev = buy_share(candles[-10:-5])
    buy_drop = buy_prev - buy5 if buy5 is not None and buy_prev is not None else None
    local_max = max(x['high'] for x in candles[-30:])
    from_max = pct(close, local_max)
    wick = (last['high']-max(last['open'], close))/(last['high']-last['low']) if last['high'] > last['low'] else 0
    wick_up_percent = pct(last['high'], max(last['open'], close))
    old_volume = sum(x['volume'] for x in candles[-10:-5])
    volume_ratio = sum(x['volume'] for x in candles[-5:]) / old_volume if old_volume > 0 else None

    flags = []
    if buy_drop is not None and buy_drop >= 10:
        flags.append('malejaca_przewaga_agresywnych_zakupow')
    if prev5 is not None and prev5 > 1 and prev5 - change5 >= 4:
        flags.append('hamowanie_5m')
    if wick >= .5 and wick_up_percent is not None and wick_up_percent >= 2:
        flags.append('dlugi_gorny_knot')
    if (volume_ratio is not None and volume_ratio >= 1.3 and prev5 is not None
            and prev5 > 2 and change5 <= prev5 * .4 and buy5 is not None and buy5 >= 50):
        flags.append('mozliwa_absorpcja')

    near_high = from_max >= -5
    if len(flags) >= 2 and near_high:
        state = 'MOZLIWY_PULLBACK'
    elif flags:
        state = 'POMPA_SLABNIE'
    else:
        state = 'POMPA_TRWA'

    # Nie raportujemy "niskiego" ryzyka na podstawie szczatkowych danych.
    if (change15 is not None and change15 >= 7 and buy5 is not None and buy5 >= 60) or change5 >= 5:
        continuation_risk = 'PODWYZSZONE'
    elif change15 is not None and change15 >= 3:
        continuation_risk = 'OBECNE'
    else:
        continuation_risk = 'NIEUSTALONE'
    return {
        'state': state, 'continuation_risk': continuation_risk,
        'signals': flags, 'near_30m_high': near_high,
        'change_5m_percent': round(change5, 2),
        'change_15m_percent': round(change15, 2),
        'taker_buy_last_5m_percent': round(buy5, 2) if buy5 is not None else None,
        'taker_buy_prev_5m_percent': round(buy_prev, 2) if buy_prev is not None else None,
        'rsi_14_1m': round(rsi14(closes), 1) if rsi14(closes) is not None else None,
        'from_30m_high_percent': round(from_max, 2),
        'last_closed_candle_utc': datetime.fromtimestamp(last['t']/1000, tz=timezone.utc).isoformat(),
    }


def main():
    now = datetime.now(timezone.utc)
    alarm_threshold = get_threshold()
    # Dla progu alarmu 40% obserwacja od 30%; przy nizszym progu odpowiednio nizej.
    watch_threshold = min(30.0, alarm_threshold * .75)
    tickers = get_json(API + '/capi/v3/market/ticker/24hr', timeout=30)
    if not isinstance(tickers, list):
        raise RuntimeError('WEEX nie zwrocil listy tickerow')
    watches = []
    for x in tickers:
        if not isinstance(x, dict):
            continue
        s = x.get('symbol')
        ch = safe_number(x.get('priceChangePercent'))
        price = safe_number(x.get('lastPrice'))
        if (isinstance(s, str) and s.endswith('USDT') and ch is not None
                and price is not None and price > 0 and ch * 100 >= watch_threshold):
            watches.append((s, ch*100, price))
    watches.sort(key=lambda x: x[1], reverse=True)
    watches = watches[:MAX_COINS]
    print(f'Cicha obserwacja od {watch_threshold:.1f}% (24h): {len(watches)} coinow')

    # Wybrane coiny < progu alarmu zaczynamy rejestrowac PRZED alarmem.
    try:
        store_oi_snapshots(watches, now)
    except Exception as exc:
        print('OI wstepnej obserwacji niedostepne:', type(exc).__name__, str(exc)[:160])

    entries = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(load_candles, symbol, now): (symbol, change, price)
                 for symbol, change, price in watches}
        for future in as_completed(tasks):
            symbol, change, price = tasks[future]
            item = {'symbol': symbol, 'change_24h_percent': round(change, 2),
                    'ticker_price': price, 'observed_at_utc': now.isoformat()}
            try:
                item.update(assess(future.result()))
                item['oi'] = oi_changes(symbol, now)
                item['data_status'] = 'OK'
            except Exception as exc:
                item['data_status'] = 'BRAK_DANYCH'
                item['state'] = 'NIEUSTALONE'
                item['continuation_risk'] = 'NIEUSTALONE'
                item['problem'] = str(exc)[:160]
            entries.append(item)
    entries.sort(key=lambda x: x['change_24h_percent'], reverse=True)
    previous_result = read_json(RESULT_FILE, {})
    scan_gap_minutes = None
    if isinstance(previous_result, dict) and previous_result.get('updated_at_utc'):
        try:
            prev = datetime.fromisoformat(previous_result['updated_at_utc'].replace('Z', '+00:00'))
            if prev.tzinfo and prev < now:
                scan_gap_minutes = round((now - prev).total_seconds() / 60, 1)
        except (TypeError, ValueError, AttributeError):
            pass
    result = {
        'version': 1, 'updated_at_utc': now.isoformat(),
        'minutes_since_previous_analysis': scan_gap_minutes,
        'watch_threshold_24h_percent': round(watch_threshold, 2),
        'alarm_threshold_24h_percent': alarm_threshold,
        'note': 'Wstepne wskazniki; brak niezawodnej czestotliwosci 1m, brak gwarancji kierunku.',
        'coins': entries,
    }
    RESULT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('Analiza zapisana:', len(entries), 'coinow,',
          sum(x['data_status']=='OK' for x in entries), 'z poprawnymi swiecami')


if __name__ == '__main__':
    main()

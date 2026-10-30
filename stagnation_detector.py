"""PUMP-SHORT: niezalezny eksperyment wykrywania wystrzalow po stagnacji.

Wersja obserwacyjna: NIE wysyla Telegrama, NIE zmienia scanner.py, alertow
ani konfiguracji. Wynik zapisuje wyłącznie do stagnation_scan.json.

Dane: WEEX 1h, historyczne zamkniete swiece. 72h spokoju PRZED ostatnimi
24h, nastepnie szukamy przebicia poziomu z konca stagnacji.
Ograniczenia: kandydaci sa wybierani po zmianie 24h >= 10%; moze to
przeoczyc wystrzal, ktory juz wypadl z ruchomego okna 24h. Wymaga
urzadzen WEEX historii 1h i wystarczajacego wolumenu bazowego.
"""
import json
import math
import statistics
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scanner import API, get_json, safe_number

OUT = Path('stagnation_scan.json')
CANDIDATE_24H_MIN_PCT = 10.0
MAX_CANDIDATES = 30
CALM_HOURS = 72
BREAKOUT_HOURS = 24
CALM_RANGE_MAX_PCT = 25.0
CALM_MAX_HOURLY_RISE_PCT = 12.0
VOLUME_SPIKE_MIN_RATIO = 3.0
EARLY_RISE_PCT = 15.0
ALARM_RISE_PCT = 40.0  # tylko klasyfikacja, NIE alarm Telegram


def ratio_pct(new, old):
    return (new / old - 1) * 100 if old is not None and old > 0 else None


def load_hourly(symbol, now):
    # Konserwatywnie odcinamy najnowsza, potencjalnie niedomknieta swiece.
    end_ms = int(now.timestamp() // 3600) * 3_600_000 - 3_600_000
    start_ms = end_ms - 99 * 3_600_000
    q = urllib.parse.urlencode({'symbol': symbol, 'interval': '1h',
        'startTime': start_ms, 'endTime': end_ms, 'limit': 100,
        'priceType': 'LAST'})
    data = get_json(API + '/capi/v3/market/historyKlines?' + q, timeout=20)
    if not isinstance(data, list):
        raise ValueError('Nieprawidlowe swiece WEEX')
    unique = {}
    for r in data:
        if not isinstance(r, list) or len(r) < 8:
            continue
        try:
            t = int(r[0])
            o, h, l, c = map(float, r[1:5])
            volume = float(r[7])
            if not all(math.isfinite(v) for v in (o, h, l, c, volume)):
                continue
            if not (start_ms <= t <= end_ms) or min(o,h,l,c) <= 0 or volume < 0:
                continue
            if h < max(o,c) or l > min(o,c):
                continue
            # Swieca musi byc zamknieta.
            if t + 3_600_000 > int(now.timestamp() * 1000):
                continue
            unique[t] = {'t': t, 'high': h, 'low': l, 'close': c, 'volume': volume}
        except (ValueError, TypeError, OverflowError):
            continue
    result = [unique[t] for t in sorted(unique)]
    if len(result) < CALM_HOURS + BREAKOUT_HOURS:
        raise ValueError(f'Za malo godzin historii: {len(result)}')
    result = result[-(CALM_HOURS + BREAKOUT_HOURS):]
    if any(result[i]['t'] - result[i-1]['t'] != 3_600_000 for i in range(1, len(result))):
        raise ValueError('Luki w historii swiec godzinowych')
    # Ostatnia godzina nie moze byc zbyt stara.
    if (now.timestamp() * 1000 - (result[-1]['t'] + 3_600_000)) > 2.5 * 3_600_000:
        raise ValueError('Nieaktualne swiece godzinowe')
    return result


def evaluate(candles):
    if len(candles) != CALM_HOURS + BREAKOUT_HOURS:
        raise ValueError('Niepelne okno 72h + 24h')
    calm, recent = candles[:CALM_HOURS], candles[CALM_HOURS:]
    base_low = min(x['low'] for x in calm)
    base_high = max(x['high'] for x in calm)
    base_close = calm[-1]['close']
    calm_range = ratio_pct(base_high, base_low)
    max_calm_rise = max(ratio_pct(calm[i]['close'], calm[i-1]['close']) for i in range(1, len(calm)))
    is_calm = calm_range <= CALM_RANGE_MAX_PCT and max_calm_rise <= CALM_MAX_HOURLY_RISE_PCT
    volumes = [x['volume'] for x in calm if x['volume'] > 0]
    base_vol = statistics.median(volumes) if volumes else 0
    max_recent_volume = max(x['volume'] for x in recent)
    vol_ratio = max_recent_volume / base_vol if base_vol else None
    peak = max(x['high'] for x in recent)
    peak_rise = ratio_pct(peak, base_close)
    current_rise = ratio_pct(recent[-1]['close'], base_close)
    # Szczyt musial wybic ponad cala baze; ogranicza mylenie z odbiciem w konsolidacji.
    broke_base = peak > base_high
    confirmed_vol = vol_ratio is not None and vol_ratio >= VOLUME_SPIKE_MIN_RATIO
    if not is_calm:
        status = 'BRAK_STAGNACJI'
    elif not broke_base or peak_rise < EARLY_RISE_PCT:
        status = 'STAGNACJA_BEZ_WYSTRZALU'
    elif not confirmed_vol:
        status = 'WYBICIE_BEZ_POTWIERDZENIA_WOLUMENU'
    elif peak_rise >= ALARM_RISE_PCT:
        status = 'WYSTRZAL_40_OD_BAZY'
    else:
        status = 'WCZESNY_WYSTRZAL'
    return {
        'status': status, 'calm_72h': is_calm,
        'calm_range_percent': round(calm_range, 2),
        'calm_max_hourly_rise_percent': round(max_calm_rise, 2),
        'base_price': base_close,
        'base_high': base_high,
        'rise_from_base_at_peak_percent': round(peak_rise, 2),
        'rise_from_base_now_percent': round(current_rise, 2),
        'peak_price_last_24h': peak,
        'max_hour_volume_vs_calm_median': round(vol_ratio, 2) if vol_ratio is not None else None,
        'breakout_above_base': broke_base,
        'last_closed_candle_utc': datetime.fromtimestamp(recent[-1]['t']/1000, tz=timezone.utc).isoformat(),
    }


def main():
    now = datetime.now(timezone.utc)
    tickers = get_json(API + '/capi/v3/market/ticker/24hr', timeout=35)
    if not isinstance(tickers, list):
        raise RuntimeError('Brak tickerow WEEX')
    candidates = []
    for t in tickers:
        if not isinstance(t, dict):
            continue
        symbol = t.get('symbol')
        change = safe_number(t.get('priceChangePercent'))
        price = safe_number(t.get('lastPrice'))
        if (isinstance(symbol, str) and symbol.endswith('USDT') and change is not None
                and price is not None and price > 0 and change * 100 >= CANDIDATE_24H_MIN_PCT):
            candidates.append((symbol, change * 100, price))
    candidates.sort(key=lambda x: x[1], reverse=True)
    selected = candidates[:MAX_CANDIDATES]
    entries = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(load_hourly, sym, now): (sym, ch, price)
                   for sym, ch, price in selected}
        for future in as_completed(futures):
            sym, ch, price = futures[future]
            item = {'symbol': sym, 'change_24h_percent': round(ch, 2), 'ticker_price': price}
            try:
                item.update(evaluate(future.result()))
                item['data_status'] = 'OK'
            except Exception as exc:
                item.update({'status': 'BRAK_DANYCH', 'data_status': 'BRAK_DANYCH',
                             'problem': str(exc)[:160]})
            entries.append(item)
    entries.sort(key=lambda x: x['change_24h_percent'], reverse=True)
    result = {
        'version': 1, 'updated_at_utc': now.isoformat(),
        'mode': 'OBSERWACJA_BEZ_ALERTOW',
        'rules': {'calm_hours': CALM_HOURS, 'recent_hours': BREAKOUT_HOURS,
                  'calm_range_max_percent': CALM_RANGE_MAX_PCT,
                  'candidate_24h_min_percent': CANDIDATE_24H_MIN_PCT,
                  'early_rise_from_base_percent': EARLY_RISE_PCT,
                  'strong_rise_from_base_percent': ALARM_RISE_PCT,
                  'hour_volume_ratio_min': VOLUME_SPIKE_MIN_RATIO},
        'limitations': 'Zalezy od 24h preselekcji i harmonogramu GitHuba. To nie jest ciagle sledzenie epizodow ani sygnal inwestycyjny.',
        'total_candidates': len(candidates), 'checked': len(selected),
        'coins': entries
    }
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('Stagnation scan:', len(entries), 'checked; statuses:',
          {s: sum(e['status'] == s for e in entries) for s in sorted(set(e['status'] for e in entries))})


if __name__ == '__main__':
    main()

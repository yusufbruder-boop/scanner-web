"""
IBKR Bridge — läuft LOKAL auf deinem PC
Pollt IBKR Client Portal API (localhost:5001) alle 60s
Sendet Daten an Hermes Scanner auf Railway

Starten: python ibkr_bridge.py
Voraussetzung: IBKR Client Portal Gateway muss laufen (localhost:5001)
"""

import requests
import time
import json
import urllib3
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Konfiguration ─────────────────────────────────────────────────────────────
IBKR_BASE   = 'https://localhost:5001/v1/api'
RAILWAY_URL = 'https://scanner-web-production-7a52.up.railway.app'
BRIDGE_TOKEN = 'hermes-ibkr-2026'
INTERVAL    = 60  # Sekunden zwischen Updates

HEADERS_IBKR = {'Content-Type': 'application/json'}
HEADERS_RLW  = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {BRIDGE_TOKEN}',
}

# ── IBKR Scanner-Typen ────────────────────────────────────────────────────────
SCANNER_CONFIGS = [
    {
        'key': 'most_active',
        'label': 'Most Active',
        'body': {
            'instrument': 'STK',
            'location': 'STK.US.MAJOR',
            'type': 'MOST_ACTIVE',
            'filter': [{'code': 'volumeRate', 'value': 1000000}],
        },
    },
    {
        'key': 'hot_options',
        'label': 'Hot By Options Volume',
        'body': {
            'instrument': 'STK',
            'location': 'STK.US.MAJOR',
            'type': 'HOT_BY_OPT_VOLUME',
            'filter': [],
        },
    },
    {
        'key': 'top_gainers',
        'label': 'Top Gainers',
        'body': {
            'instrument': 'STK',
            'location': 'STK.US.MAJOR',
            'type': 'TOP_PERC_GAIN',
            'filter': [{'code': 'priceAbove', 'value': 5}],
        },
    },
    {
        'key': 'top_losers',
        'label': 'Top Losers',
        'body': {
            'instrument': 'STK',
            'location': 'STK.US.MAJOR',
            'type': 'TOP_PERC_LOSE',
            'filter': [{'code': 'priceAbove', 'value': 5}],
        },
    },
]


def ibkr_get(path):
    try:
        r = requests.get(f'{IBKR_BASE}{path}', headers=HEADERS_IBKR,
                         verify=False, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f'  IBKR GET {path} Fehler: {e}')
    return None


def ibkr_scanner(body):
    try:
        r = requests.post(f'{IBKR_BASE}/iserver/scanner/run',
                          headers=HEADERS_IBKR, json=body,
                          verify=False, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f'  Scanner Fehler: {e}')
    return None


def get_price_snapshot(conid):
    data = ibkr_get(f'/iserver/marketdata/snapshot?conids={conid}&fields=31,83,84,85,86,7295,7296')
    if data and isinstance(data, list) and len(data) > 0:
        d = data[0]
        return {
            'price': float(str(d.get('31', 0)).replace(',', '') or 0),
            'chg':   float(str(d.get('83', '0%')).replace('%','').replace(',','') or 0),
            'vol':   int(str(d.get('7295', 0)).replace(',','') or 0),
            'bid':   float(str(d.get('84', 0)).replace(',','') or 0),
            'ask':   float(str(d.get('85', 0)).replace(',','') or 0),
        }
    return {}


def parse_scanner_result(raw, key):
    results = []
    contracts = raw.get('contracts', raw.get('Contract', []))
    if isinstance(contracts, dict):
        contracts = [contracts]
    for c in contracts[:15]:
        sym = c.get('symbol') or c.get('Symbol', '')
        conid = c.get('con_id') or c.get('conId', '')
        if not sym:
            continue
        entry = {
            'sym':      sym,
            'conid':    conid,
            'category': key,
            'rank':     c.get('rank', len(results) + 1),
            'reason':   c.get('secType', 'STK'),
        }
        # Preis-Snapshot (optional, nicht critical)
        if conid:
            snap = get_price_snapshot(conid)
            entry.update(snap)
        results.append(entry)
    return results


def fetch_all_scanners():
    payload = {
        'most_active':  [],
        'hot_options':  [],
        'top_gainers':  [],
        'top_losers':   [],
        'connected':    False,
        'ts':           datetime.now().isoformat(),
    }

    # Auth-Check
    auth = ibkr_get('/iserver/auth/status')
    if not auth or not auth.get('authenticated'):
        # Session tickle
        requests.post(f'{IBKR_BASE}/tickle', headers=HEADERS_IBKR, verify=False, timeout=5)
        time.sleep(2)
        auth = ibkr_get('/iserver/auth/status')

    if not auth or not auth.get('authenticated'):
        print(f'  [IBKR] Nicht authentifiziert — Gateway läuft?')
        return None

    payload['connected'] = True
    print(f'  [IBKR] Verbunden ✓  ({datetime.now().strftime("%H:%M:%S")})')

    for cfg in SCANNER_CONFIGS:
        print(f'  Scanne: {cfg["label"]}...')
        raw = ibkr_scanner(cfg['body'])
        if raw:
            items = parse_scanner_result(raw, cfg['key'])
            payload[cfg['key']] = items
            print(f'    → {len(items)} Treffer')
        else:
            print(f'    → keine Daten')
        time.sleep(1)

    return payload


def push_to_railway(payload):
    try:
        r = requests.post(
            f'{RAILWAY_URL}/ibkr/push',
            headers=HEADERS_RLW,
            json=payload,
            timeout=15,
        )
        if r.status_code == 200:
            resp = r.json()
            print(f'  [Railway] OK — {resp.get("signals", 0)} Signale, {resp.get("confirmed", 0)} IBKR+Polygon bestätigt')
        else:
            print(f'  [Railway] Fehler {r.status_code}: {r.text[:120]}')
    except Exception as e:
        print(f'  [Railway] Verbindungsfehler: {e}')


def main():
    print('=' * 60)
    print('IBKR Bridge — Hermes Scanner Datenquelle')
    print(f'IBKR Gateway: {IBKR_BASE}')
    print(f'Railway:      {RAILWAY_URL}')
    print(f'Intervall:    {INTERVAL}s')
    print('=' * 60)
    print()

    while True:
        ts = datetime.now().strftime('%H:%M:%S')
        print(f'[{ts}] Starte IBKR Scan...')

        payload = fetch_all_scanners()
        if payload:
            syms = [s['sym'] for s in payload.get('most_active', [])[:5]]
            print(f'  Most Active Top-5: {", ".join(syms) if syms else "—"}')
            push_to_railway(payload)
        else:
            print(f'  [IBKR] Kein Gateway erreichbar — nächster Versuch in {INTERVAL}s')

        print()
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()

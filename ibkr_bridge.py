"""
IBKR Bridge — läuft LOKAL auf deinem PC
Zeigt was der Markt kauft/verkauft: Most Active, Top Gainers, Hot Options
Sendet alle 60s an Hermes auf Railway → sichtbar auf Handy

Starten: python ibkr_bridge.py
Voraussetzung: IBKR Client Portal Gateway laufen (localhost:5001)
Download: https://www.interactivebrokers.com/en/trading/ibkr-apis.php
"""

import requests, time, urllib3
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

IBKR_BASE    = 'https://localhost:5001/v1/api'
RAILWAY_URL  = 'https://scanner-web-production-7a52.up.railway.app'
BRIDGE_TOKEN = 'hermes-ibkr-2026'
INTERVAL     = 60

HDR_IBKR = {'Content-Type': 'application/json'}
HDR_RLW  = {'Content-Type': 'application/json',
             'Authorization': f'Bearer {BRIDGE_TOKEN}'}

# Was kauft/verkauft der Markt — 4 Scanner
SCANNERS = [
    {'key': 'most_active',  'label': 'Most Active',
     'body': {'instrument': 'STK', 'location': 'STK.US.MAJOR',
              'type': 'MOST_ACTIVE',
              'filter': [{'code': 'volumeRate', 'value': 1000000}]}},
    {'key': 'hot_options',  'label': 'Hot Options Volume',
     'body': {'instrument': 'STK', 'location': 'STK.US.MAJOR',
              'type': 'HOT_BY_OPT_VOLUME', 'filter': []}},
    {'key': 'top_gainers',  'label': 'Top Gainers',
     'body': {'instrument': 'STK', 'location': 'STK.US.MAJOR',
              'type': 'TOP_PERC_GAIN',
              'filter': [{'code': 'priceAbove', 'value': 2}]}},
    {'key': 'top_losers',   'label': 'Top Losers',
     'body': {'instrument': 'STK', 'location': 'STK.US.MAJOR',
              'type': 'TOP_PERC_LOSE',
              'filter': [{'code': 'priceAbove', 'value': 2}]}},
]


def get(path):
    try:
        r = requests.get(f'{IBKR_BASE}{path}', headers=HDR_IBKR, verify=False, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f'  GET {path}: {e}')
        return None


def post(path, body=None):
    try:
        r = requests.post(f'{IBKR_BASE}{path}', headers=HDR_IBKR,
                          json=body or {}, verify=False, timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f'  POST {path}: {e}')
        return None


def price_snapshot(conids):
    """Preis + Change% für Liste von ConIDs."""
    if not conids:
        return {}
    cids = ','.join(str(c) for c in conids[:20])
    data = get(f'/iserver/marketdata/snapshot?conids={cids}&fields=31,83,7295,55')
    result = {}
    if data and isinstance(data, list):
        for d in data:
            cid = str(d.get('conid', ''))
            price = float(str(d.get('31', 0)).replace(',', '') or 0)
            chg   = float(str(d.get('83', '0%')).replace('%', '').replace(',', '') or 0)
            vol   = int(str(d.get('7295', 0)).replace(',', '') or 0)
            sym   = str(d.get('55', cid))
            result[cid] = {'sym': sym, 'price': price, 'chg': chg, 'vol': vol}
    return result


def run_scanner(body):
    raw = post('/iserver/scanner/run', body)
    if not raw:
        return []
    contracts = raw.get('contracts', raw.get('Contract', []))
    if isinstance(contracts, dict):
        contracts = [contracts]
    results = []
    for c in contracts[:20]:
        sym   = c.get('symbol') or c.get('Symbol', '')
        conid = c.get('con_id') or c.get('conId', 0)
        if sym:
            results.append({'sym': sym, 'conid': conid,
                            'rank': c.get('rank', len(results)+1)})
    return results


def ensure_auth():
    auth = get('/iserver/auth/status')
    if not auth or not auth.get('authenticated'):
        post('/tickle')
        time.sleep(2)
        auth = get('/iserver/auth/status')
    return bool(auth and auth.get('authenticated'))


def fetch_market_data():
    payload = {
        'most_active': [], 'hot_options': [],
        'top_gainers': [], 'top_losers':  [],
        'prices':      {},
        'connected':   True,
        'ts':          datetime.now().isoformat(),
    }

    for cfg in SCANNERS:
        print(f'  [{cfg["label"]}]...')
        items = run_scanner(cfg['body'])
        payload[cfg['key']] = items
        print(f'    {len(items)} Treffer: {", ".join(i["sym"] for i in items[:5])}')
        time.sleep(0.8)

    # Preise für alle gefundenen Stocks
    all_conids = []
    for key in ['most_active', 'hot_options', 'top_gainers', 'top_losers']:
        for item in payload[key]:
            if item.get('conid') and item['conid'] not in all_conids:
                all_conids.append(item['conid'])

    if all_conids:
        print(f'  Lade {len(all_conids)} Preise...')
        payload['prices'] = price_snapshot(all_conids[:20])
        # Preise in die Items einmergen
        for key in ['most_active', 'hot_options', 'top_gainers', 'top_losers']:
            for item in payload[key]:
                snap = payload['prices'].get(str(item.get('conid', '')), {})
                item['price'] = snap.get('price', 0)
                item['chg']   = snap.get('chg', 0)
                item['vol']   = snap.get('vol', 0)

    return payload


def push_to_railway(payload):
    try:
        r = requests.post(f'{RAILWAY_URL}/ibkr/push',
                          headers=HDR_RLW, json=payload, timeout=15)
        if r.status_code == 200:
            print(f'  [Railway] OK')
        else:
            print(f'  [Railway] Fehler {r.status_code}: {r.text[:100]}')
    except Exception as e:
        print(f'  [Railway] {e}')


def main():
    print('=' * 55)
    print('IBKR Bridge — Marktdaten fuer Hermes')
    print(f'Gateway: {IBKR_BASE}')
    print(f'Railway: {RAILWAY_URL}')
    print('=' * 55)

    while True:
        print(f'\n[{datetime.now().strftime("%H:%M:%S")}] Starte...')

        if not ensure_auth():
            print('  NICHT verbunden. Gateway laeuft? Browser-Login gemacht?')
            print(f'  Oeffne: https://localhost:5001')
            time.sleep(INTERVAL)
            continue

        print('  Verbunden mit IBKR Gateway')
        payload = fetch_market_data()
        push_to_railway(payload)
        print(f'  Naechster Abruf in {INTERVAL}s')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()

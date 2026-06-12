import json, os, threading, time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request
import urllib.request, ssl
# v3.0 — Hermes Full Agent: Alpaca + Polygon + Memory + P&L

app = Flask(__name__)

RESULTS_FILE   = 'results.json'
MEMORY_FILE    = 'hermes_memory.json'
LEARNING_FILE  = 'hermes_learning.json'
IDENTITY_FILE  = 'hermes_identity.json'

# ── GitHub Gist Persistenz — Memory überlebt Railway-Deployments ──────────────
_GH_TOKEN   = os.environ.get('GITHUB_TOKEN', '')
_GIST_ID    = os.environ.get('HERMES_GIST_ID', '')   # wird beim ersten Start erstellt
_GIST_LOCK  = threading.Lock()
_GIST_FILES = {
    MEMORY_FILE:   'hermes_memory.json',
    LEARNING_FILE: 'hermes_learning.json',
    IDENTITY_FILE: 'hermes_identity.json',
}

def _gist_request(method: str, path: str, body: dict = None) -> dict:
    url  = f'https://api.github.com{path}'
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Bearer {_GH_TOKEN}',
        'Accept':        'application/vnd.github+json',
        'Content-Type':  'application/json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return {}

def gist_restore():
    """Beim App-Start: Memory-Dateien aus GitHub Gist laden."""
    global _GIST_ID
    gist_id = _GIST_ID or os.environ.get('HERMES_GIST_ID', '')
    if not gist_id or not _GH_TOKEN:
        return
    try:
        g = _gist_request('GET', f'/gists/{gist_id}')
        files = g.get('files', {})
        for local_path, gist_name in _GIST_FILES.items():
            if gist_name in files:
                content = files[gist_name].get('content', '')
                if content and content.strip() not in ('{}', 'null', ''):
                    with open(local_path, 'w', encoding='utf-8') as f:
                        f.write(content)
        _GIST_ID = gist_id
    except Exception:
        pass

def gist_save(changed_file: str = None):
    """Nach jedem Memory-Save: Dateien in GitHub Gist sichern."""
    global _GIST_ID
    if not _GH_TOKEN:
        return
    with _GIST_LOCK:
        try:
            files_payload = {}
            targets = [changed_file] if changed_file else list(_GIST_FILES.keys())
            for local_path in targets:
                gist_name = _GIST_FILES.get(local_path, local_path)
                if os.path.exists(local_path):
                    with open(local_path, encoding='utf-8') as f:
                        files_payload[gist_name] = {'content': f.read()}
            if not files_payload:
                return

            gist_id = _GIST_ID or os.environ.get('HERMES_GIST_ID', '')
            if gist_id:
                _gist_request('PATCH', f'/gists/{gist_id}', {'files': files_payload})
            else:
                # Erstmaliges Erstellen
                resp = _gist_request('POST', '/gists', {
                    'description': 'Hermes Trading Memory — auto-backup',
                    'public': False,
                    'files': {k: {'content': v['content'] or '{}'} for k, v in files_payload.items()},
                })
                new_id = resp.get('id', '')
                if new_id:
                    _GIST_ID = new_id
                    # Gist-ID als Env-Var für nächsten Start speichern (Railway)
                    try:
                        _set_railway_env('HERMES_GIST_ID', new_id)
                    except Exception:
                        pass
        except Exception:
            pass

def _set_railway_env(key: str, value: str):
    """Setzt Railway Env-Var via GraphQL damit Gist-ID zwischen Deployments erhalten bleibt."""
    rail_token = os.environ.get('RAILWAY_TOKEN', '')
    if not rail_token:
        return
    service_id = os.environ.get('RAILWAY_SERVICE_ID', '')
    env_id     = os.environ.get('RAILWAY_ENVIRONMENT_ID', '')
    if not service_id or not env_id:
        return
    query = '''mutation($input: VariableUpsertInput!) {
        variableUpsert(input: $input) { id }
    }'''
    body  = json.dumps({'query': query, 'variables': {
        'input': {'name': key, 'value': value,
                  'serviceId': service_id, 'environmentId': env_id}
    }}).encode()
    req = urllib.request.Request(
        'https://backboard.railway.com/graphql/v2',
        data=body, method='POST',
        headers={'Authorization': f'Bearer {rail_token}',
                 'Content-Type': 'application/json'}
    )
    urllib.request.urlopen(req, timeout=8)
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT  = os.environ.get('TELEGRAM_CHAT',  '')

# Alpaca Paper API
ALPACA_KEY    = os.environ.get('ALPACA_KEY',    'PK5T6OU5ENWZQK5DVZ746MHHEF')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET', '3nngSp7NksYikEZvf5hLihWEBtFdnuG336KfeYvFb5D9')
ALPACA_BASE   = 'https://paper-api.alpaca.markets'

# IBKR Bridge — lokales Script sendet Daten hierher
IBKR_BRIDGE_TOKEN = os.environ.get('IBKR_BRIDGE_TOKEN', 'hermes-ibkr-2026')

# Auto-Trading: Hermes handelt bei Score >= AUTO_TRADE_MIN_SCORE
AUTO_TRADE_ENABLED   = os.environ.get('AUTO_TRADE', 'false').lower() == 'true'
AUTO_TRADE_AMOUNT    = float(os.environ.get('AUTO_TRADE_AMOUNT', '300'))   # $ pro Trade
AUTO_TRADE_MIN_SCORE = int(os.environ.get('AUTO_TRADE_MIN_SCORE', '10'))   # Mindest-Score

# Auto-Scan: täglich 09:30 ET = 13:30 UTC (Sommer) / 14:30 UTC (Winter)
AUTO_SCAN_UTC_HOUR   = 13
AUTO_SCAN_UTC_MINUTE = 30

state = {
    'running':        False,
    'progress':       0,
    'progress_total': 0,
    'current_ticker': '',
    'results':        None,
    'last_scan':      None,
    'next_scan':      None,
    'error':          None,
    'last_results_hash': None,
    'followup':       None,
    'followup_date':  None,
    # Hermes Agent
    'hermes_alerts':  [],
    'hermes_picks':   [],
    'hermes_universe': set(),
    'live_feed':      [],        # Web Alert Feed (ersetzt Telegram für User)
    'hermes_ts':           None,
    'hermes_running':      False,
    'hermes_running_since': None,
    'hermes_last_success': None,   # letzter erfolgreicher Zyklus
    'hermes_stuck_count':  0,      # wie oft Watchdog feuern musste
    'hermes_ai':           '',
    'hermes_signal_evals': {},
    'alpaca_portfolio':    {},
    'hermes_memory':       {},
    'auto_trade_enabled':  AUTO_TRADE_ENABLED,
    'auto_trades':         [],
    'hermes_24h':          [],
    'seen_news':           set(),   # dedup: bereits gesendete News-Headlines
    # Background threads: Social KI-Score + HF 13F
    'social_data':    [],
    'hf_data':        [],
    'extra_ts':       None,
    # IBKR Bridge Daten (empfangen vom lokalen ibkr_bridge.py)
    'ibkr_data':      {},           # {most_active, hot_options, top_gainers, ts, connected}
    'ibkr_ts':        None,         # letzter Empfang
    'ibkr_scan':      [],           # IBKR-bestätigte Top-Mover mit Signal
    # Hermes Brain — autonomer KI-Agent
    'brain_worldview': {'focus': 'BALANCED', 'market': 'NEUTRAL', 'reason': '', 'updated': ''},
    'brain_trades':    [],          # Trades ausgelöst vom Brain (mit P&L tracking)
    'brain_news':      [],          # zuletzt klassifizierte wichtige News
    'brain_active':    False,       # läuft Brain gerade?
    # Claude ↔ Hermes direkte Kommunikation
    'claude_session':  {},          # aktive Claude-Trading-Session mit IBKR-Daten
    'claude_trades':   [],          # Claude-Trades (manuell MT5)
    'claude_ibkr':     {},          # IBKR-Daten von Claude gepusht
    # Fibonacci 88.2% Auto-Trade Loop
    'fib_results':     [],          # letzter Fib-Scan (alle Symbole mit Levels)
    'fib_trades':      [],          # Fib-882 Auto-Trades des heutigen Tages
    'fib_last_scan':   None,        # Zeitstempel letzter Fib-Scan
    # Trump/Makro Katalysator — marktweiter Move
    'trump_catalyst':  None,        # {direction, headline, strength, ts, age_min}
    # Strategy Monitor Pro — 5 Strategien aus TradingView
    'strategy_monitor': [],         # [{sym, direction, score, strats_long/short, ...}]
    'strategy_monitor_ts': None,    # Zeitstempel letzter Scan
    # Kanal-Pattern Scanner — erkennt Ping-Pong Aktien automatisch
    'channel_watch':   ['NVDA','AMD','INTC','META','AAPL','TSLA','MSFT','AMZN','GOOGL','MU'],
    'channel_signals': [],          # [{sym, signal, price, support, resistance, sl, tp, rr, ts}]
    'channel_active':  [],          # Aktien die gerade im Kanal sind (mit Levels)
    # VWAP Reclaim Scanner
    'vwap_signals':    [],          # [{sym, signal, price, vwap, vol_ratio, time}]
    # Pre-Market Gap Scanner
    'gap_signals':     [],          # [{sym, gap_pct, prev_close, pre_price, signal, time}]
    # Trading Blackout (Fed/CPI/NFP)
    'trading_blackout': False,
}
_hermes_lock = threading.Lock()
_scan_lock   = threading.Lock()   # verhindert gleichzeitige Scans

# Follow-up: 10:00 ET (14:00 UTC) + 22:00 CET (21:00 UTC) — Tagesabschluss
FOLLOWUP_UTC_HOUR   = 14
FOLLOWUP_UTC_MINUTE = 0
FOLLOWUP2_UTC_HOUR  = 21   # 22:00 CET / 17:00 ET — Marktschluss Report
FOLLOWUP2_UTC_MINUTE = 0

# ── Alpaca API ───────────────────────────────────────────────────────────────

def _alpaca(path, method='GET', body=None):
    """Alpaca REST API Aufruf."""
    import urllib.error
    try:
        url = f'{ALPACA_BASE}{path}'
        headers = {
            'APCA-API-KEY-ID':     ALPACA_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET,
            'Content-Type':        'application/json',
        }
        data = json.dumps(body).encode() if body else None
        req  = urllib.request.Request(url, data=data, headers=headers, method=method)
        ctx  = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = {}
        err_body['http_status'] = e.code
        err_body.setdefault('message', str(e))
        return err_body
    except Exception as e:
        return {'error': str(e)}

def parse_occ_symbol(sym):
    """Parst OCC Options Symbol: META260605C00630000 → {underlying, exp, type, strike}"""
    import re
    m = re.match(r'^([A-Z]{1,6})(\d{6})([CP])(\d{8})$', sym)
    if not m:
        return None
    underlying, exp6, cp, strike8 = m.groups()
    return {
        'underlying': underlying,
        'exp':        f'20{exp6[:2]}-{exp6[2:4]}-{exp6[4:]}',
        'type':       'CALL' if cp == 'C' else 'PUT',
        'strike':     round(int(strike8) / 1000, 2),
    }

def get_alpaca_portfolio():
    """Holt Alpaca Account + Positionen + Orders."""
    acc  = _alpaca('/v2/account')
    pos  = _alpaca('/v2/positions')
    if not isinstance(pos, list):
        pos = []
    positions = []
    for p in pos:
        try:
            sym = p['symbol']
            occ = parse_occ_symbol(sym)
            # Richtung: Long CALL = BULLISH, Long PUT = BEARISH, Short alles = umgekehrt
            alpaca_side = p['side']  # 'long' oder 'short'
            if occ:
                if alpaca_side == 'long':
                    direction = 'BULLISH' if occ['type'] == 'CALL' else 'BEARISH'
                else:
                    direction = 'BEARISH' if occ['type'] == 'CALL' else 'BULLISH'
                display_side = f"{occ['type']} {occ['strike']} | {occ['exp']}"
            else:
                direction = 'BULLISH' if alpaca_side == 'long' else 'BEARISH'
                display_side = alpaca_side
            positions.append({
                'sym':       sym,
                'side':      display_side,
                'direction': direction,
                'is_option': occ is not None,
                'opt_type':  occ['type'] if occ else None,
                'qty':       float(p['qty']),
                'entry':     round(float(p['avg_entry_price']), 2),
                'price':     round(float(p['current_price']), 2),
                'pl':        round(float(p['unrealized_pl']), 2),
                'pl_pct':    round(float(p['unrealized_plpc']) * 100, 1),
                'mkt_val':   round(float(p['market_value']), 2),
            })
        except Exception:
            pass
    return {
        'equity':     round(float(acc.get('equity', 0)), 2),
        'cash':       round(float(acc.get('cash', 0)), 2),
        'pl_day':     round(float(acc.get('unrealized_pl', 0)), 2),
        'positions':  positions,
        'ts':         datetime.now().strftime('%H:%M'),
    }

def alpaca_order(sym, qty, side, reason='hermes-signal'):
    """Platziert eine Market-Order auf Alpaca Paper."""
    body = {'symbol': sym, 'qty': str(qty), 'side': side,
            'type': 'market', 'time_in_force': 'day',
            'client_order_id': f'hermes-{sym}-{int(time.time())}'}
    result = _alpaca('/v2/orders', method='POST', body=body)
    ok = 'id' in result
    tg_send(f'🤖 <b>HERMES ORDER</b>: {side.upper()} {qty}x <b>{sym}</b> — {"✅ OK" if ok else "❌ " + str(result.get("message","?"))}')
    return result

def _build_occ(sym, exp, strike, opt_type):
    """OCC Options Symbol: META260605C00630000"""
    try:
        d = datetime.strptime(exp, '%Y-%m-%d')
        exp_str = d.strftime('%y%m%d')
        t = 'C' if opt_type.upper() == 'CALL' else 'P'
        strike_str = f'{int(float(strike) * 1000):08d}'
        return f'{sym}{exp_str}{t}{strike_str}'
    except Exception:
        return None

def hermes_auto_trade(signal_result: dict):
    """
    Hermes handelt automatisch: NUR OPTIONS (CALL/PUT) bei Score >= AUTO_TRADE_MIN_SCORE.
    Alpaca Paper — OCC Symbol aus Scanner best_option konstruiert.
    """
    if not state.get('auto_trade_enabled'):
        return
    sym    = signal_result.get('t', '')
    price  = float(signal_result.get('price', 0))
    signal = signal_result.get('signal', '')
    score  = int(signal_result.get('score', 0))
    best   = signal_result.get('best') or {}
    otype  = signal_result.get('otype', '')

    if score < AUTO_TRADE_MIN_SCORE or not sym or price <= 0:
        return
    if signal not in ('LONG', 'SHORT') or not best or not otype:
        return

    # Options-Daten aus Scanner
    strike    = best.get('strike', 0)
    exp       = best.get('exp', '')
    opt_price = float(best.get('pr', 0))
    if not strike or not exp or opt_price <= 0:
        return

    # Strike-Sanity: max 20% OTM/ITM vom aktuellen Preis (verhindert falsche Polygon-Daten)
    if price > 0:
        pct_from_price = abs(strike - price) / price * 100
        if pct_from_price > 20:
            return

    # OCC Symbol bauen
    occ = _build_occ(sym, exp, strike, otype)
    if not occ:
        return

    # Bereits heute gehandelt?
    today = datetime.now().strftime('%Y-%m-%d')
    mem = load_memory()
    trade_key = f'{today}-{occ}'
    if trade_key in mem.get('auto_trades_today', {}):
        return

    # Anzahl Kontrakte: AUTO_TRADE_AMOUNT / (opt_price * 100), mind. 1, max 10
    contracts = max(1, min(10, int(AUTO_TRADE_AMOUNT / (opt_price * 100))))
    cost = round(contracts * opt_price * 100, 2)

    # Options Order auf Alpaca
    body = {
        'symbol':           occ,
        'qty':              str(contracts),
        'side':             'buy',           # Immer BUY (Call für Long, Put für Short)
        'type':             'market',
        'time_in_force':    'day',
        'client_order_id':  f'hermes-{sym}-{int(time.time())}',
    }
    result = _alpaca('/v2/orders', method='POST', body=body)
    ok = 'id' in result
    err = '' if ok else result.get('message', str(result))[:80]

    # In Memory + State speichern
    trade_entry = {
        'sym': sym, 'occ': occ, 'type': otype, 'contracts': contracts,
        'strike': strike, 'exp': exp, 'entry_pr': opt_price,
        'cost': cost, 'score': score, 'signal': signal,
        'time': datetime.now().strftime('%H:%M'), 'date': today,
        'ok': ok, 'error': err,
    }
    if 'auto_trades_today' not in mem:
        mem['auto_trades_today'] = {}
    mem['auto_trades_today'][trade_key] = trade_entry
    save_memory(mem)
    state['auto_trades'].append(trade_entry)

    tg_send(
        f'🎯 <b>HERMES AUTO-TRADE</b> {datetime.now().strftime("%H:%M")}\n'
        f'{"✅" if ok else "❌"} {otype} <b>{sym}</b> ${strike} Exp:{exp}\n'
        f'{contracts} Kontrakt(e) @ ${opt_price:.2f} = ${cost:.0f}\n'
        f'Score:{score} | OCC:{occ}\n'
        + (f'Fehler: {err}' if err else '')
    )

def hermes_auto_trade_stock(signal_result: dict, reason: str = ''):
    """
    Kauft/shortet Aktien direkt bei Alpaca Paper.
    Einfacher Signaltest: LONG=BUY, SHORT=SELL.
    Betrag: AUTO_TRADE_AMOUNT Dollar pro Trade.
    """
    if not state.get('auto_trade_enabled'):
        return
    sym    = signal_result.get('t', '')
    price  = float(signal_result.get('price', 0) or 0)
    signal = signal_result.get('signal', '')
    score  = int(signal_result.get('score', 0) or 0)
    conv   = float(signal_result.get('conviction', 0) or 0)

    if score < AUTO_TRADE_MIN_SCORE or not sym or price <= 0:
        return
    if signal not in ('LONG', 'SHORT'):
        return
    # Nur handelbare Aktien (kein ETF-Schrott unter $3, kein zu teures Stock)
    if price < 3 or price > 2000:
        return

    # Bereits heute gehandelt?
    today = datetime.now().strftime('%Y-%m-%d')
    mem = load_memory()
    trade_key = f'stk-{today}-{sym}'
    if trade_key in mem.get('auto_trades_today', {}):
        return

    # Aktuelle Position prüfen → kein Doppel-Trade
    try:
        existing = _alpaca(f'/v2/positions/{sym}')
        if isinstance(existing, dict) and float(existing.get('qty', 0)) != 0:
            return
    except Exception:
        pass

    # ── Quality Filters ──────────────────────────────────────────────────────
    if _is_blackout():
        return

    vol_ratio = _get_volume_ratio(sym)
    if vol_ratio < 1.2:
        return  # zu wenig Volumen = Fake-Signal

    sector_chg = _get_sector_chg(sym)
    if signal == 'LONG'  and sector_chg < -1.5:
        return  # Sektor zu schwach für Long
    if signal == 'SHORT' and sector_chg >  1.5:
        return  # Sektor zu stark gegen Short

    bull_tf, bear_tf = _get_mtf_bias(sym)
    if signal == 'LONG'  and bull_tf < 2:
        return  # weniger als 2 von 3 Timeframes bullisch
    if signal == 'SHORT' and bear_tf < 2:
        return  # weniger als 2 von 3 Timeframes bärisch

    # Conviction Boost bei starken Filtern
    if vol_ratio >= 2.0 and bull_tf == 3 and signal == 'LONG':
        conv = min(conv + 0.10, 1.0)
    if vol_ratio >= 2.0 and bear_tf == 3 and signal == 'SHORT':
        conv = min(conv + 0.10, 1.0)
    # ─────────────────────────────────────────────────────────────────────────

    side = 'buy' if signal == 'LONG' else 'sell'
    qty  = max(1, int(AUTO_TRADE_AMOUNT / price))
    cost = round(qty * price, 2)

    body = {
        'symbol':          sym,
        'qty':             str(qty),
        'side':            side,
        'type':            'market',
        'time_in_force':   'day',
        'client_order_id': f'hermes-stk-{sym}-{int(time.time())}',
    }
    result = _alpaca('/v2/orders', method='POST', body=body)
    ok  = 'id' in result
    err = '' if ok else result.get('message', str(result))[:80]

    lbl = reason or signal_result.get('top_reason', signal_result.get('kat_text', ''))[:60]
    trade_entry = {
        'sym': sym, 'type': 'STOCK', 'qty': qty,
        'side': side, 'entry_price': price,
        'cost': cost, 'score': score, 'signal': signal,
        'conviction': round(conv, 2),
        'time': datetime.now().strftime('%H:%M'), 'date': today,
        'ok': ok, 'error': err, 'reason': lbl,
    }
    if 'auto_trades_today' not in mem:
        mem['auto_trades_today'] = {}
    mem['auto_trades_today'][trade_key] = trade_entry
    save_memory(mem)
    state['auto_trades'].append(trade_entry)

    # Brain-Trades P&L Tracker eintragen
    brain_entry = {
        'sym': sym, 'signal': signal, 'score': score,
        'conviction': round(conv, 2), 'type': 'STOCK', 'qty': qty,
        'price_entry': price, 'price_current': price,
        'pnl_pct': 0.0, 'pnl_dollar': 0.0, 'won': None,
        'time': datetime.now().strftime('%H:%M'), 'date': today,
        'reason': lbl,
    }
    state['brain_trades'] = ([brain_entry] + state.get('brain_trades', []))[:30]

    emoji = '🟢' if signal == 'LONG' else '🔴'
    tg_send(
        f'{emoji} <b>HERMES STOCK TRADE</b> {datetime.now().strftime("%H:%M")}\n'
        f'{"✅" if ok else "❌"} {signal} <b>{sym}</b> ${price:.2f}\n'
        f'{qty} Aktie(n) = ${cost:.0f} | Score:{score} Conv:{round(conv*100)}%\n'
        + (f'Fehler: {err}' if err else lbl),
        key=f'stk_trade_{sym}_{today}'
    )
    return ok


# ── Trading-Intelligenz Helpers ──────────────────────────────────────────────

_bar_cache = {}   # {sym+tf: (timestamp, bars)} — 45s TTL

def _fetch_bars(sym, timeframe='5Min', limit=25):
    """Alpaca Bars mit 45s Cache."""
    key = f'{sym}_{timeframe}_{limit}'
    cached = _bar_cache.get(key)
    if cached and (time.time() - cached[0]) < 45:
        return cached[1]
    try:
        ctx = ssl.create_default_context()
        url = (f'https://data.alpaca.markets/v2/stocks/{sym}/bars'
               f'?timeframe={timeframe}&limit={limit}&feed=iex&adjustment=raw')
        req = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID': ALPACA_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET,
        })
        with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
            bars = json.loads(r.read()).get('bars', [])
        _bar_cache[key] = (time.time(), bars)
        if len(_bar_cache) > 300:
            oldest = sorted(_bar_cache, key=lambda k: _bar_cache[k][0])[:100]
            for k in oldest:
                _bar_cache.pop(k, None)
        return bars
    except Exception:
        return []

def _get_volume_ratio(sym, bars=None):
    """Aktuelles Volumen / Durchschnitt (letzte 20 Bars). > 1.5 = stark."""
    if bars is None:
        bars = _fetch_bars(sym, '5Min', 22)
    if len(bars) < 4:
        return 1.5  # Kein Daten → neutral (kein Block)
    vols = [b['v'] for b in bars]
    avg  = sum(vols[:-1]) / max(len(vols) - 1, 1)
    return round(vols[-1] / max(avg, 1), 2)

def _get_vwap(sym, bars=None):
    """VWAP aus heutigen 5-Min Bars."""
    if bars is None:
        bars = _fetch_bars(sym, '5Min', 80)
    if not bars:
        return 0.0
    today = datetime.now().strftime('%Y-%m-%d')
    tb    = [b for b in bars if b.get('t', '').startswith(today)] or bars[-20:]
    tp_v  = sum(((b['h'] + b['l'] + b['c']) / 3) * b['v'] for b in tb)
    tot_v = sum(b['v'] for b in tb)
    return round(tp_v / max(tot_v, 1), 2)

_SECTOR_ETF = {
    'NVDA':'XLK','AMD':'XLK','INTC':'XLK','MSFT':'XLK','AAPL':'XLK',
    'QCOM':'XLK','AVGO':'XLK','MU':'XLK','SNDK':'XLK','MRVL':'XLK',
    'META':'XLC','GOOGL':'XLC','GOOG':'XLC','NFLX':'XLC',
    'AMZN':'XLY','TSLA':'XLY','HD':'XLY',
    'GS':'XLF','JPM':'XLF','BAC':'XLF','MS':'XLF',
    'XOM':'XLE','CVX':'XLE','OXY':'XLE',
    'ARKK':'QQQ','DIA':'QQQ','SPY':'QQQ',
}

def _get_sector_chg(sym):
    """Sektor-ETF Tagesänderung % — positiv = Sektor stark."""
    etf = _SECTOR_ETF.get(sym, 'QQQ')
    try:
        bars = _fetch_bars(etf, '1Day', 2)
        if bars:
            o, c = bars[-1].get('o', 0), bars[-1].get('c', 0)
            if o > 0:
                return round((c - o) / o * 100, 2)
    except Exception:
        pass
    return 0.0

def _get_mtf_bias(sym):
    """Multi-Timeframe Bias: (bull_count, bear_count) über 5Min/15Min/1Hour.
    Verwendet EMA10 Vergleich mit Close."""
    bull, bear = 0, 0
    for tf, lim in [('5Min', 12), ('15Min', 12), ('1Hour', 12)]:
        bars = _fetch_bars(sym, tf, lim)
        if len(bars) < 6:
            bull += 1  # keine Daten → neutral zählt als bull (fail-open)
            continue
        closes = [b['c'] for b in bars]
        ema = closes[0]
        for c in closes[1:]:
            ema = ema * 0.83 + c * 0.17  # EMA10 approx
        if closes[-1] > ema:
            bull += 1
        else:
            bear += 1
    return bull, bear

def _is_blackout():
    """True wenn wichtige Wirtschaftsdaten in < 30 Min erwartet oder gerade veröffentlicht."""
    if state.get('trading_blackout'):
        return True
    # Brain-News Check: frische Makro-News → Pause
    now_ts = time.time()
    for n in state.get('brain_news', [])[:8]:
        age = now_ts - n.get('_ts', now_ts - 9999)
        if age < 600:  # letzte 10 Minuten
            hl = (n.get('headline', '') + n.get('reason', '')).lower()
            if any(w in hl for w in ('federal reserve','fomc','cpi report',
                                      'inflation data','nonfarm','jobs report',
                                      'payroll','interest rate decision')):
                return True
    return False


def hermes_update_brain_trade_pnl():
    """Aktualisiert P&L aller offenen brain_trades aus Alpaca Positionen."""
    trades = state.get('brain_trades', [])
    if not trades:
        return
    try:
        positions = _alpaca('/v2/positions')
        if not isinstance(positions, list):
            return
        pos_map = {p['symbol']: p for p in positions}
        updated = []
        for t in trades:
            if t.get('won') is not None:
                updated.append(t)
                continue
            sym = t.get('sym', '')
            p = pos_map.get(sym)
            if p:
                cur        = float(p.get('current_price', t['price_entry']))
                pnl_pct    = float(p.get('unrealized_plpc', 0)) * 100
                pnl_dollar = float(p.get('unrealized_pl', 0))
                t = dict(t, price_current=round(cur, 2),
                         pnl_pct=round(pnl_pct, 2),
                         pnl_dollar=round(pnl_dollar, 2))

                # ── Trailing Stop ─────────────────────────────────────────
                # Aktiviert wenn Gewinn > 1.5% — schließt wenn 1.2% vom Hoch zurück
                if pnl_pct > 1.5 and not t.get('trail_on'):
                    t = dict(t, trail_on=True, trail_peak=pnl_pct)
                if t.get('trail_on'):
                    peak = max(t.get('trail_peak', pnl_pct), pnl_pct)
                    t    = dict(t, trail_peak=peak)
                    if peak - pnl_pct >= 1.2:
                        # Trailing Stop ausgelöst → Position schliessen
                        signal   = t.get('signal', 'LONG')
                        close_s  = 'sell' if signal == 'LONG' else 'buy'
                        qty_raw  = p.get('qty', '1')
                        qty_int  = max(1, int(abs(float(qty_raw))))
                        result   = _alpaca('/v2/orders', method='POST', body={
                            'symbol': sym, 'qty': str(qty_int), 'side': close_s,
                            'type': 'market', 'time_in_force': 'day',
                            'client_order_id': f'hermes-trail-{sym}-{int(time.time())}',
                        })
                        if 'id' in result:
                            t = dict(t, won=pnl_pct >= 0, trail_triggered=True)
                            tg_send(
                                f'🔒 <b>TRAILING STOP: {sym}</b>\n'
                                f'Peak {peak:.1f}% → jetzt {pnl_pct:.1f}% — Rückgang {peak-pnl_pct:.1f}%\n'
                                f'P&L: {pnl_dollar:+.0f}$',
                                key=f'trail_{sym}'
                            )
                # ──────────────────────────────────────────────────────────

            updated.append(t)
        state['brain_trades'] = updated
    except Exception:
        pass


def hermes_close_all_stock_positions():
    """Schließt alle Hermes-geöffneten Aktien-Positionen (Market-Order)."""
    try:
        positions = _alpaca('/v2/positions')
        if not isinstance(positions, list):
            return
        closed = []
        for p in positions:
            sym = p['symbol']
            if parse_occ_symbol(sym):
                continue  # Options überspringen
            alpaca_side = p['side']
            close_side = 'sell' if alpaca_side == 'long' else 'buy'
            qty = abs(float(p['qty']))
            result = _alpaca('/v2/orders', method='POST', body={
                'symbol': sym, 'qty': str(int(qty)), 'side': close_side,
                'type': 'market', 'time_in_force': 'day',
                'client_order_id': f'hermes-close-{sym}-{int(time.time())}',
            })
            pl = round(float(p.get('unrealized_pl', 0)), 2)
            closed.append({'sym': sym, 'pl': pl, 'ok': 'id' in result})
            # Brain-Trade als geschlossen markieren
            for t in state.get('brain_trades', []):
                if t.get('sym') == sym and t.get('won') is None:
                    pl_pct = round(float(p.get('unrealized_plpc', 0)) * 100, 2)
                    t['won']      = pl_pct >= 0
                    t['pnl_pct']  = pl_pct
                    t['pnl_dollar'] = pl
        if closed:
            total_pl = sum(x['pl'] for x in closed)
            wins = sum(1 for x in closed if x['pl'] >= 0)
            tg_send(
                f'🔒 <b>HERMES EOD CLOSE</b>\n'
                f'{len(closed)} Positionen geschlossen\n'
                f'Wins: {wins} | P&L: {"+" if total_pl>=0 else ""}${total_pl:.0f}',
                key='eod_close'
            )
    except Exception:
        pass


# ── Hermes Memory (persistent) ────────────────────────────────────────────────

def load_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            return json.load(open(MEMORY_FILE, encoding='utf-8'))
    except Exception:
        pass
    return {'signals': {}, 'pl_history': [], 'market_closes': []}

def save_memory(mem):
    try:
        json.dump(mem, open(MEMORY_FILE, 'w', encoding='utf-8'), indent=2)
        threading.Thread(target=gist_save, args=(MEMORY_FILE,), daemon=True).start()
    except Exception:
        pass

def memory_track_signal(sym, price, signal, score, reasons):
    """Merkt sich ein neues Signal mit Einstiegspreis."""
    mem = load_memory()
    if sym not in mem['signals']:
        mem['signals'][sym] = {
            'sym': sym, 'signal': signal, 'score': score,
            'entry_price': price, 'entry_time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'reasons': reasons[:3], 'status': 'open',
            'peak_pl_pct': 0.0, 'current_pl_pct': 0.0,
        }
        save_memory(mem)

def memory_update_pl(poly_key):
    """Aktualisiert P&L für alle offenen Signale via Alpaca Data API (Polygon snapshot NOT_AUTHORIZED)."""
    mem = load_memory()
    changed = False
    open_syms = [s for s, sig in mem['signals'].items() if sig.get('status') == 'open']
    if not open_syms:
        return mem

    # Batch-Fetch via Alpaca Snapshots (bis 50 Symbole auf einmal)
    snapshots = {}
    try:
        ctx = ssl.create_default_context()
        for i in range(0, len(open_syms), 40):
            batch = open_syms[i:i+40]
            syms_param = ','.join(batch)
            url = f'https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms_param}&feed=iex'
            req = urllib.request.Request(url, headers={
                'APCA-API-KEY-ID':     ALPACA_KEY,
                'APCA-API-SECRET-KEY': ALPACA_SECRET,
            })
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                snapshots.update(json.loads(r.read()))
    except Exception:
        pass

    for sym, sig in mem['signals'].items():
        if sig.get('status') != 'open':
            continue
        snap = snapshots.get(sym, {})
        now_price = float(
            (snap.get('latestTrade') or {}).get('p') or
            (snap.get('dailyBar') or {}).get('c') or 0
        )
        if now_price and sig['entry_price']:
            if sig['signal'] == 'LONG':
                pl_pct = (now_price - sig['entry_price']) / sig['entry_price'] * 100
            else:
                pl_pct = (sig['entry_price'] - now_price) / sig['entry_price'] * 100
            sig['current_pl_pct'] = round(pl_pct, 1)
            sig['current_price']  = round(now_price, 2)
            sig['peak_pl_pct']    = round(max(sig.get('peak_pl_pct', 0), pl_pct), 1)
            changed = True
    if changed:
        save_memory(mem)
    return mem

# ── Hermes Self-Learning Engine ──────────────────────────────────────────────

def load_learning():
    """Lädt Hermes Lernparameter. Standard wenn nicht vorhanden."""
    default = {
        'weights': {
            'vol_ratio_threshold': 3.0,
            'vol_ratio_bonus':     2.0,
            'dp_threshold_m':      1.0,
            'min_score_long':      4,
            'min_score_short':     4,
            'earnings_bonus':      4,
            'small_cap_boost':     0,
        },
        'performance': {
            'total_signals':   0,
            'correct_long':    0,
            'correct_short':   0,
            'missed_moves':    0,
            'win_rate':        0.0,
        },
        # Pattern-Datenbank: was funktioniert, was nicht
        'patterns': [
            {
                'id': 'polygon_confirmed_long',
                'name': 'Polygon + News bestätigen LONG',
                'signal_basis': 'POLYGON_CONFIRMED', 'direction': 'LONG',
                'success_rate': 0.78, 'samples': 0,
                'examples': [], 'lesson': 'Vol/OI + News beide bullisch = hohe Zuverlässigkeit'
            },
            {
                'id': 'polygon_confirmed_short',
                'name': 'Polygon + News bestätigen SHORT',
                'signal_basis': 'POLYGON_CONFIRMED', 'direction': 'SHORT',
                'success_rate': 0.82, 'samples': 0,
                'examples': [], 'lesson': 'PUT Vol/OI + neg. News = sehr zuverlässig'
            },
            {
                'id': 'secondary_offering_short',
                'name': 'Verwässerung unter Marktpreis → SHORT',
                'signal_basis': 'POLYGON_CONFIRMED', 'direction': 'SHORT',
                'trigger': ['secondary offering','dilution','share offering'],
                'success_rate': 0.88, 'samples': 1,
                'examples': ['GOOGL 2026-06-03: -4.6% nach S-3 bei $350'],
                'lesson': 'Ausgabepreis unter Kurs = Kurs fällt zum Ausgabepreis. 88% Erfolg.'
            },
            {
                'id': 'ceo_endorsement_long',
                'name': 'NVDA/Tech CEO Endorsement → LONG',
                'signal_basis': 'NEWS_ONLY', 'direction': 'LONG',
                'trigger': ['jensen huang','nvidia ceo','endorsement'],
                'success_rate': 0.85, 'samples': 1,
                'examples': ['MRVL 2026-06-03: +37.5% nach Jensen Huang Endorsement'],
                'lesson': 'Jensen Huang nennt Firma = fast immer großer Move. Sofort LONG.'
            },
            {
                'id': 'high_call_falling_stock',
                'name': 'Hohe CALL Vol/OI aber Kurs fällt → KONFLIKT',
                'signal_basis': 'CONFLICT', 'direction': 'LONG',
                'trigger': ['call_voi>20', 'prev_chg<-4'],
                'success_rate': 0.28, 'samples': 1,
                'examples': ['DELL 2026-06-03: CALL 76x aber -9.6% — Calls waren Short-Absicherung'],
                'lesson': 'Sehr hohe CALL Vol/OI bei fallendem Kurs = Short-Seller hedgen sich. Nicht LONG!'
            },
            {
                'id': 'pentagon_gov_contract',
                'name': 'Pentagon/Gov Vertrag → LONG',
                'signal_basis': 'NEWS_ONLY', 'direction': 'LONG',
                'trigger': ['pentagon','government contract','dod','military contract'],
                'success_rate': 0.75, 'samples': 0,
                'examples': [], 'lesson': 'Regierungsverträge = fast immer LONG. Warnung: prüfe ob Vertrag bestätigt.'
            },
            {
                'id': 'news_only_weak',
                'name': 'Nur News, kein Polygon Signal → SCHWACH',
                'signal_basis': 'NEWS_ONLY', 'direction': 'LONG',
                'success_rate': 0.42, 'samples': 0,
                'examples': [], 'lesson': 'Nur News ohne Options-Bestätigung = Retail reagiert. Zu spät einsteigen.'
            },
        ],
        'missed_trades':    [],
        'hit_trades':       [],
        'improvement_log':  [],
        'last_review':      None,
        'daily_context':    {},   # was Hermes täglich weiß über den Markt
        'market_bias_log':  {},   # tägl. Marktbias (BULL/BEAR/NEUTRAL) + QQQ-Chg
        'sweep_short_blocks': 0,  # wie oft Call-Sweep-SHORT blockiert wurde
        'put_hedge_blocks':   0,  # wie oft Large-Cap-PUT-Hedge blockiert wurde
    }
    try:
        if os.path.exists(LEARNING_FILE):
            saved = json.load(open(LEARNING_FILE, encoding='utf-8'))
            # Merge mit defaults (neue Keys übernehmen)
            for k, v in default.items():
                if k not in saved:
                    saved[k] = v
                elif isinstance(v, dict):
                    for sk, sv in v.items():
                        if sk not in saved[k]:
                            saved[k][sk] = sv
            return saved
    except Exception:
        pass
    return default

def save_learning(data):
    try:
        json.dump(data, open(LEARNING_FILE, 'w', encoding='utf-8'), indent=2)
        threading.Thread(target=gist_save, args=(LEARNING_FILE,), daemon=True).start()
    except Exception:
        pass

def hermes_self_review(scan_data: dict, poly_key: str):
    """
    Tägliche Selbstanalyse: Vergleicht Hermes-Empfehlungen mit realem Markt.
    Findet verpasste Moves, analysiert Muster, passt Gewichtung an.
    Läuft täglich nach Marktschluss (20:00-20:30 UTC).
    """
    learn = load_learning()
    today = datetime.now().strftime('%Y-%m-%d')

    if learn.get('last_review') == today:
        return  # Heute schon gemacht

    ctx = ssl.create_default_context()

    # 1) Was hat der Scanner heute empfohlen?
    recommended = {r['t']: r for r in
                   scan_data.get('longs', []) + scan_data.get('shorts', []) +
                   scan_data.get('movers', [])}

    # 2) Empfohlene Stocks via Alpaca Snapshots prüfen (Polygon NOT_AUTHORIZED für snapshots)
    real_movers = {}
    rec_syms = list(recommended.keys())[:40]
    if rec_syms:
        try:
            syms_param = ','.join(rec_syms)
            url = f'https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms_param}&feed=iex'
            req = urllib.request.Request(url, headers={
                'APCA-API-KEY-ID':     ALPACA_KEY,
                'APCA-API-SECRET-KEY': ALPACA_SECRET,
            })
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                snaps = json.loads(r.read())
            for sym, snap in snaps.items():
                daily = snap.get('dailyBar') or {}
                prev  = snap.get('prevDailyBar') or {}
                price = float(daily.get('c') or 0)
                pc    = float(prev.get('c') or price or 1)
                chg   = round((price - pc) / pc * 100, 1) if pc else 0
                vol   = int(daily.get('v') or 0)
                pvol  = int(prev.get('v') or 1)
                vol_r = round(vol / pvol, 1) if pvol else 0
                if sym and price >= 5:
                    real_movers[sym] = {'chg': chg, 'price': price, 'vol_ratio': vol_r}
        except Exception:
            pass
    # Zusätzlich Polygon Gainers/Losers für Misses (optional, kann 403 geben)
    for direction in ['gainers', 'losers']:
        try:
            url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{direction}?apiKey={poly_key}'
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
                d = json.loads(r.read())
            for t in d.get('tickers', [])[:30]:
                sym   = t.get('ticker', '')
                day   = t.get('day', {})
                prev  = t.get('prevDay', {})
                price = float(day.get('c') or 0)
                pc    = float(prev.get('c') or price or 1)
                chg   = round((price - pc) / pc * 100, 1) if pc else 0
                vol   = int(day.get('v') or 0)
                pvol  = int(prev.get('v') or 1)
                vol_r = round(vol / pvol, 1) if pvol else 0
                if sym and abs(chg) >= 5 and price >= 5 and sym not in real_movers:
                    real_movers[sym] = {'chg': chg, 'price': price, 'vol_ratio': vol_r}
        except Exception:
            pass

    # 3) Hits: empfohlen UND bewegt
    hits   = {s: d for s, d in real_movers.items() if s in recommended
              and ((d['chg'] > 3 and recommended[s].get('signal') == 'LONG') or
                   (d['chg'] < -3 and recommended[s].get('signal') == 'SHORT'))}

    # 4) Misses: bewegt aber NICHT empfohlen (>8% und Volumen hoch)
    misses = {s: d for s, d in real_movers.items()
              if s not in recommended and abs(d['chg']) >= 8 and d.get('vol_ratio', 0) >= 3}

    # 5) Was war das Muster bei Misses? → Polygon Daten holen
    miss_patterns = []
    for sym, data_m in list(misses.items())[:5]:
        pattern = {'sym': sym, 'chg': data_m['chg'], 'vol_ratio': data_m['vol_ratio'],
                   'had_dp': False, 'had_sweep': False, 'had_news': False,
                   'price': data_m['price'], 'reason_missed': ''}
        try:
            # Dark Pool check
            dp_url = f'https://api.polygon.io/v3/trades/{sym}?timestamp.gte={today}&limit=1000&apiKey={poly_key}'
            with urllib.request.urlopen(urllib.request.Request(dp_url), context=ctx, timeout=6) as r:
                dp_data = json.loads(r.read())
            dp_trades = [t for t in dp_data.get('results', [])
                        if t.get('conditions') and any(c in [20,29,37,41,80,81] for c in t['conditions'])]
            dp_total = sum(t.get('size',0)*t.get('price',0) for t in dp_trades)
            pattern['had_dp']  = dp_total >= 500_000
            pattern['dp_m']    = round(dp_total/1e6, 1)
        except Exception:
            pass
        try:
            # News check
            news_url = f'https://api.polygon.io/v2/reference/news?ticker={sym}&limit=3&apiKey={poly_key}'
            with urllib.request.urlopen(urllib.request.Request(news_url), context=ctx, timeout=6) as r:
                news_data = json.loads(r.read())
            for n in news_data.get('results', []):
                tl = n.get('title','').lower()
                if any(k in tl for k in ['earnings','beat','guidance','raised','upgrade','deal','contract']):
                    pattern['had_news'] = True
                    pattern['news_title'] = n.get('title','')[:60]
                    break
        except Exception:
            pass

        # Warum verpasst?
        reasons = []
        if data_m['vol_ratio'] >= 5:
            reasons.append(f'Vol {data_m["vol_ratio"]}x — hätte erkannt werden müssen')
        if data_m['price'] < 50:
            reasons.append(f'Preis ${data_m["price"]:.0f} — kleine Aktie ignoriert')
        if pattern['had_news']:
            reasons.append('Earnings/News Katalysator')
        if not reasons:
            reasons.append('Signal zu schwach bewertet')
        pattern['reason_missed'] = ' | '.join(reasons)
        miss_patterns.append(pattern)

    # 5b) Falsche Signale erkennen (LONG auf crashed, SHORT auf ripped)
    false_signals = []
    for sym, rec in recommended.items():
        mv = real_movers.get(sym, {})
        chg = mv.get('chg', 0)
        if rec.get('signal') == 'LONG' and chg <= -10:
            false_signals.append({'sym': sym, 'signal': 'LONG', 'chg': chg, 'date': today,
                                   'reason': f'LONG empfohlen aber {chg:.1f}% gefallen'})
        elif rec.get('signal') == 'SHORT' and chg >= 8:
            false_signals.append({'sym': sym, 'signal': 'SHORT', 'chg': chg, 'date': today,
                                   'reason': f'SHORT empfohlen aber +{chg:.1f}% gestiegen'})
        # Call-Sweep SHORT Muster: SHORT empfohlen obwohl Call-Sweeps dominierten
        elif rec.get('signal') == 'SHORT' and chg >= 3:
            smart = rec.get('smart_money', {})
            sweeps_call = rec.get('call_sweeps', 0)
            if sweeps_call >= 3:
                false_signals.append({'sym': sym, 'signal': 'SHORT', 'chg': chg, 'date': today,
                    'reason': f'SHORT mit {sweeps_call} Call-Sweeps — Widerspruch, Kurs +{chg:.1f}%',
                    'pattern': 'call_sweep_short'})
                learn['sweep_short_blocks'] = learn.get('sweep_short_blocks', 0) + 1
        # Large-Cap PUT Hedge Muster: SHORT auf teuren Aktien die gestiegen sind
        elif rec.get('signal') == 'SHORT' and chg >= 2:
            price_rec = mv.get('price', 0)
            max_put_voi = rec.get('smart_money', {}).get('max_put_vol_oi', 0)
            if price_rec > 80 and max_put_voi >= 15:
                false_signals.append({'sym': sym, 'signal': 'SHORT', 'chg': chg, 'date': today,
                    'reason': f'PUT {max_put_voi:.0f}x auf ${price_rec:.0f} Large-Cap — war Hedge, Kurs +{chg:.1f}%',
                    'pattern': 'put_hedge_misread'})
                learn['put_hedge_blocks'] = learn.get('put_hedge_blocks', 0) + 1

    if false_signals:
        learn.setdefault('false_signals', [])
        learn['false_signals'] = (false_signals + learn['false_signals'])[:40]

    # 5c) Marktbias heute speichern (QQQ-Bewegung als Proxy)
    qqq_chg = 0.0
    try:
        url_qqq = f'https://data.alpaca.markets/v2/stocks/snapshots?symbols=QQQ&feed=iex'
        req_qqq = urllib.request.Request(url_qqq, headers={
            'APCA-API-KEY-ID': ALPACA_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET
        })
        with urllib.request.urlopen(req_qqq, context=ctx, timeout=8) as r:
            snp = json.loads(r.read())
        daily_q = snp.get('QQQ', {}).get('dailyBar', {})
        prev_q  = snp.get('QQQ', {}).get('prevDailyBar', {})
        if daily_q and prev_q:
            qqq_chg = round((float(daily_q.get('c',0)) - float(prev_q.get('c',1))) / float(prev_q.get('c',1)) * 100, 2)
    except Exception:
        pass
    market_bias = 'BULL' if qqq_chg > 0.5 else ('BEAR' if qqq_chg < -0.5 else 'NEUTRAL')
    learn.setdefault('market_bias_log', {})[today] = {
        'qqq_chg': qqq_chg,
        'bias': market_bias,
        'win_rate': win_r,
        'hits': list(hits.keys())[:5],
        'false': [f['sym'] for f in false_signals[:3]],
    }
    # Nur 30 Tage behalten
    mb_log = learn['market_bias_log']
    if len(mb_log) > 30:
        oldest = sorted(mb_log.keys())[0]
        del mb_log[oldest]

    # 5d) Richtungswechsel erkennen: gestern BULL, heute BEAR (oder umgekehrt)
    direction_flip = False
    flip_context = ''
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    if yesterday in mb_log:
        prev_bias = mb_log[yesterday].get('bias', 'NEUTRAL')
        if prev_bias == 'BULL' and market_bias == 'BEAR':
            direction_flip = True
            flip_context = f'Gestern BULL (QQQ {mb_log[yesterday]["qqq_chg"]:+.1f}%) → heute BEAR (QQQ {qqq_chg:+.1f}%)'
        elif prev_bias == 'BEAR' and market_bias == 'BULL':
            direction_flip = True
            flip_context = f'Gestern BEAR (QQQ {mb_log[yesterday]["qqq_chg"]:+.1f}%) → heute BULL (QQQ {qqq_chg:+.1f}%)'

    # 6) Gewichtung automatisch anpassen
    changes = []
    w = learn['weights']

    # Wenn viele Misses mit hohem Vol → Vol-Schwelle senken
    high_vol_misses = [m for m in miss_patterns if m['vol_ratio'] >= 5]
    if len(high_vol_misses) >= 2:
        old = w['vol_ratio_threshold']
        w['vol_ratio_threshold'] = max(2.0, old - 0.5)
        changes.append(f'Vol-Schwelle: {old:.1f} → {w["vol_ratio_threshold"]:.1f} (wegen {len(high_vol_misses)} Vol-Anomalien verpasst)')

    # Wenn Misses kleine Aktien waren → small_cap_boost erhöhen
    small_misses = [m for m in miss_patterns if m['price'] < 50]
    if len(small_misses) >= 2:
        w['small_cap_boost'] = min(3, w.get('small_cap_boost', 0) + 1)
        changes.append(f'Small-Cap Boost +{w["small_cap_boost"]} (${[round(m["price"]) for m in small_misses]} verpasst)')

    # Wenn falsche Signale (Crash-Longs) → false_signal_count erhöhen
    if len(false_signals) >= 1:
        w['false_signal_count'] = w.get('false_signal_count', 0) + len(false_signals)
        syms_fs = ', '.join(f'{f["sym"]} {f["chg"]:+.0f}%' for f in false_signals[:3])
        changes.append(f'Falsche Signale heute: {syms_fs} → catastrophic-filter aktiv')

    # Wenn Misses Earnings hatten → Earnings-Bonus erhöhen
    earnings_misses = [m for m in miss_patterns if m['had_news']]
    if len(earnings_misses) >= 1:
        w['earnings_bonus'] = min(6, w.get('earnings_bonus', 4) + 1)
        changes.append(f'Earnings-Bonus +{w["earnings_bonus"]} (Katalysator-Moves verpasst)')

    # Win-Rate berechnen
    total = len(hits) + len(misses)
    win_r = round(len(hits) / max(len(recommended), 1) * 100, 1)
    learn['performance']['total_signals'] += len(recommended)
    learn['performance']['correct_long']  += len([h for h,d in hits.items() if d['chg'] > 0])
    learn['performance']['missed_moves']  += len(misses)
    learn['performance']['win_rate']       = win_r

    # 7) Ergebnisse speichern
    learn['last_review'] = today
    learn['missed_trades'] = (miss_patterns + learn.get('missed_trades', []))[:30]
    learn['hit_trades']    = ([{'sym': s, 'chg': d['chg'], 'date': today}
                                for s, d in hits.items()] + learn.get('hit_trades', []))[:50]
    if changes:
        learn['improvement_log'].insert(0, {
            'date': today, 'changes': changes,
            'misses': [m['sym'] for m in miss_patterns],
            'hits': list(hits.keys()),
            'win_rate': win_r,
        })
        learn['improvement_log'] = learn['improvement_log'][:30]
    save_learning(learn)

    # 8) Telegram Report
    lines = [f'🧠 <b>HERMES SELBST-ANALYSE {today}</b>']
    lines.append(f'Win-Rate: {win_r}% ({len(hits)}/{len(recommended)} Signale)')
    if hits:
        hit_str = ', '.join(f'{s} {d["chg"]:+.0f}%' for s,d in list(hits.items())[:4])
        lines.append(f'✅ Richtig: {hit_str}')
    if miss_patterns:
        miss_str = ', '.join(f'{m["sym"]} {m["chg"]:+.0f}%' for m in miss_patterns[:4])
        lines.append(f'❌ Verpasst: {miss_str}')
    if false_signals:
        fs_str = ', '.join(f'{f["sym"]} {f["chg"]:+.0f}%' for f in false_signals[:3])
        lines.append(f'⚠️ Falsche Signale: {fs_str}')
    if changes:
        lines.append(f'\n📈 Gelernt heute:')
        for c in changes:
            lines.append(f'  → {c}')
    tg_send('\n'.join(lines))

    # 9) PATTERN LEARNING — Conviction-Raten aus heutigem Tag aktualisieren
    patterns = learn.get('patterns', [])
    pat_updates = []

    for sym, rec in recommended.items():
        mv    = real_movers.get(sym, {})
        chg   = mv.get('chg', 0)
        basis = rec.get('signal_basis', 'WEAK')
        sig   = rec.get('signal', 'WATCH')
        if sig == 'WATCH' or not chg:
            continue

        correct = (sig == 'LONG' and chg > 3) or (sig == 'SHORT' and chg < -3)
        wrong   = (sig == 'LONG' and chg < -5) or (sig == 'SHORT' and chg > 5)

        for pat in patterns:
            if pat.get('signal_basis') == basis and pat.get('direction') == sig:
                old_rate = pat['success_rate']
                n = pat['samples']
                # Bayesian update: neuer Wert fließt mit Gewicht 1/(n+1) ein
                if correct:
                    pat['success_rate'] = round((old_rate * n + 1.0) / (n + 1), 3)
                elif wrong:
                    pat['success_rate'] = round((old_rate * n + 0.0) / (n + 1), 3)
                pat['samples'] = n + 1
                # Letztes Beispiel speichern
                ex = f'{sym} {today}: {chg:+.1f}% ({"RICHTIG" if correct else "FALSCH" if wrong else "NEUTRAL"})'
                pat.setdefault('examples', []).insert(0, ex)
                pat['examples'] = pat['examples'][:5]
                if correct or wrong:
                    pat_updates.append(f'{pat["name"]}: {old_rate:.0%} → {pat["success_rate"]:.0%}')
                break

    # Spezifische Patterns aus Signal-Basis lernen (DELL, MRVL, GOOGL Typ)
    for fs in false_signals:
        sym = fs['sym']
        rec = recommended.get(sym, {})
        basis = rec.get('signal_basis','')
        max_call_voi = rec.get('smart_money', {}).get('max_call_voi', 0)
        prev_chg_rec = rec.get('prev_chg', 0)
        # DELL-Muster: hohe CALL Vol/OI bei fallendem Kurs
        if basis == 'CONFLICT' or (max_call_voi >= 20 and prev_chg_rec <= -4):
            for pat in patterns:
                if pat.get('id') == 'high_call_falling_stock':
                    n = pat['samples']
                    pat['success_rate'] = round((pat['success_rate'] * n + 0.0) / (n + 1), 3)
                    pat['samples'] = n + 1
                    pat.setdefault('examples', []).insert(0, f'{sym} {today}: {fs["chg"]:+.1f}% — FALSCH')
                    pat['examples'] = pat['examples'][:5]
                    pat_updates.append(f'KONFLIKT-Pattern bestätigt: {sym} {fs["chg"]:+.1f}%')
                    break

    # Tages-Kontext speichern (was Hermes morgen wissen soll)
    daily_ctx = learn.setdefault('daily_context', {})
    daily_ctx[today] = {
        'date': today,
        'win_rate': win_r,
        'hits': [f'{s} {d["chg"]:+.1f}%' for s,d in list(hits.items())[:6]],
        'misses': [f'{m["sym"]} {m["chg"]:+.1f}% ({m.get("reason_missed","")[:40]})' for m in miss_patterns[:5]],
        'false_signals': [f'{f["sym"]} {f["chg"]:+.1f}%' for f in false_signals[:4]],
        'market_summary': f'{len(hits)} richtig, {len(miss_patterns)} verpasst, {len(false_signals)} falsch',
        'key_lesson': pat_updates[0] if pat_updates else 'Keine Pattern-Änderung heute',
    }
    # Nur letzten 30 Tage behalten
    if len(daily_ctx) > 30:
        oldest = sorted(daily_ctx.keys())[0]
        del daily_ctx[oldest]

    learn['patterns'] = patterns
    save_learning(learn)

    if pat_updates:
        lines.append(f'\n🔄 Pattern-Updates:')
        for pu in pat_updates[:4]:
            lines.append(f'  → {pu}')
        tg_send('\n'.join(lines[-6:]))  # nur Pattern-Teil nochmal senden

    # Hermes AI schreibt seine eigenen neuen Regeln (mit 14-Tage-Kontext + Flip-Analyse)
    try:
        hermes_ai_self_reflection(false_signals, hits, misses, win_r, today,
                                   direction_flip=direction_flip,
                                   flip_context=flip_context,
                                   market_bias_log=learn.get('market_bias_log', {}),
                                   daily_ctx=learn.get('daily_context', {}))
    except Exception:
        pass

    return learn


def get_learning_weights():
    """Gibt aktuelle Lernparameter zurück."""
    return load_learning().get('weights', {})


# ── Hermes Identity (persistent AI self-memory) ───────────────────────────────

def load_identity() -> dict:
    try:
        if os.path.exists(IDENTITY_FILE):
            return json.load(open(IDENTITY_FILE, encoding='utf-8'))
    except Exception:
        pass
    return {
        'version': 1,
        'created': datetime.now().strftime('%Y-%m-%d'),
        'lessons': [],
        'rules': [],
        'market_regime': '',
        'false_signal_patterns': [],
        'last_reflection': '',
    }

def save_identity(identity: dict):
    try:
        json.dump(identity, open(IDENTITY_FILE, 'w', encoding='utf-8'), indent=2)
        threading.Thread(target=gist_save, args=(IDENTITY_FILE,), daemon=True).start()
    except Exception:
        pass


def hermes_strategy_builder(poly_key: str):
    """
    Hermes entwickelt taeglich eigene Handelsstrategien.
    Fragt sich: 'Diese Aktien sind gestiegen / gefallen — warum hab ich das nicht gesehen?
    Kann ich das mit meinen Daten (Options, Dark Pool, News) erkennen?'
    Schreibt einfache IF→THEN Regeln die dann im Scanner aktiv sind.
    """
    if not NOUS_KEY:
        return
    today     = datetime.now().strftime('%Y-%m-%d')
    identity  = load_identity()
    if identity.get('last_strategy_build') == today:
        return

    ctx = ssl.create_default_context()

    # 1) Was ist heute wirklich passiert? Top Gainer + Loser holen
    movers = {}
    for direction in ['gainers', 'losers']:
        try:
            url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{direction}?apiKey={poly_key}'
            with urllib.request.urlopen(urllib.request.Request(url), context=ctx, timeout=8) as r:
                d = json.loads(r.read())
            for t in d.get('tickers', [])[:15]:
                sym   = t.get('ticker', '')
                day   = t.get('day', {})
                prev  = t.get('prevDay', {})
                price = float(day.get('c') or 0)
                pc    = float(prev.get('c') or price or 1)
                chg   = round((price - pc) / pc * 100, 1) if pc else 0
                vol   = int(day.get('v') or 0)
                pvol  = int(prev.get('v') or 1)
                if sym and abs(chg) >= 4 and price >= 5:
                    movers[sym] = {
                        'chg': chg, 'price': price,
                        'vol_ratio': round(vol / pvol, 1) if pvol else 0,
                        'direction': 'UP' if chg > 0 else 'DOWN',
                    }
        except Exception:
            pass

    if not movers:
        return

    # 2) Fuer jeden Mover: Options-Snapshot holen und Signale extrahieren
    mover_signals = {}
    for sym, mv in list(movers.items())[:12]:
        try:
            url = f'https://api.polygon.io/v3/snapshot/options/{sym}?limit=150&apiKey={poly_key}'
            with urllib.request.urlopen(urllib.request.Request(url), context=ctx, timeout=6) as r:
                opt = json.loads(r.read())
            res   = opt.get('results', [])
            if not res:
                continue
            calls = [r for r in res if r['details']['contract_type'] == 'call']
            puts  = [r for r in res if r['details']['contract_type'] == 'put']
            cv = sum(r['day'].get('volume', 0) for r in calls)
            pv = sum(r['day'].get('volume', 0) for r in puts)
            cp = sum(r['day'].get('volume',0) * (r['day'].get('close') or 0) * 100 for r in calls)
            pp = sum(r['day'].get('volume',0) * (r['day'].get('close') or 0) * 100 for r in puts)
            max_cv = max((r['day'].get('volume',0)/max(r.get('open_interest',1),1) for r in calls), default=0)
            max_pv = max((r['day'].get('volume',0)/max(r.get('open_interest',1),1) for r in puts),  default=0)
            sc = sum(1 for r in calls if r['day'].get('volume',0) > r.get('open_interest',0) * 2)
            sp = sum(1 for r in puts  if r['day'].get('volume',0) > r.get('open_interest',0) * 2)
            pc_r = round(pv / cv, 2) if cv > 0 else 99

            mover_signals[sym] = {
                'chg':         mv['chg'],
                'direction':   mv['direction'],
                'vol_ratio':   mv['vol_ratio'],
                'pc_ratio':    pc_r,
                'call_voi':    round(max_cv, 1),
                'put_voi':     round(max_pv, 1),
                'call_sweeps': sc,
                'put_sweeps':  sp,
                'call_prem_m': round(cp / 1e6, 2),
                'put_prem_m':  round(pp / 1e6, 2),
                'bull_signal': (max_cv >= 5 or sc >= 3 or (cp > pp * 2)),
                'bear_signal': (max_pv >= 5 or sp >= 3 or (pp > cp * 2)),
            }
        except Exception:
            continue

    if not mover_signals:
        return

    # 3) Analyse: Signal vorhanden ja/nein?
    correct_long  = []  # Kurs gestiegen UND bull signal war da
    missed_long   = []  # Kurs gestiegen aber kein bull signal
    correct_short = []  # Kurs gefallen UND bear signal war da
    missed_short  = []  # Kurs gefallen aber kein bear signal
    false_long    = []  # bull signal aber Kurs gefallen
    false_short   = []  # bear signal aber Kurs gestiegen

    for sym, s in mover_signals.items():
        chg = s['chg']
        if chg >= 4:
            if s['bull_signal']:
                correct_long.append(f'{sym} +{chg}% | CallVOI:{s["call_voi"]}x SC:{s["call_sweeps"]} P/C:{s["pc_ratio"]}')
            else:
                missed_long.append(f'{sym} +{chg}% | kein Signal | P/C:{s["pc_ratio"]} CallVOI:{s["call_voi"]}x VolRatio:{s["vol_ratio"]}x')
            if s['bear_signal']:
                false_short.append(f'{sym} +{chg}% aber BearSignal: PutVOI:{s["put_voi"]}x SP:{s["put_sweeps"]}')
        elif chg <= -4:
            if s['bear_signal']:
                correct_short.append(f'{sym} {chg}% | PutVOI:{s["put_voi"]}x SP:{s["put_sweeps"]} P/C:{s["pc_ratio"]}')
            else:
                missed_short.append(f'{sym} {chg}% | kein Signal | P/C:{s["pc_ratio"]} PutVOI:{s["put_voi"]}x VolRatio:{s["vol_ratio"]}x')
            if s['bull_signal']:
                false_long.append(f'{sym} {chg}% aber BullSignal: CallVOI:{s["call_voi"]}x SC:{s["call_sweeps"]}')

    # 4) Bestehende Strategien laden
    strategies = identity.get('strategies', [])
    strat_str  = '\n'.join(
        f'  [{s["id"]}] {s["rule"]} | Treffer:{s["hits"]}/{s["samples"]} ({s.get("hit_rate",0):.0%})'
        for s in strategies[:8]
    ) or '  noch keine'

    past_lessons = identity.get('lessons', [])[:5]
    lessons_str  = '\n'.join(f'  [{l["date"]}] {l["lesson"][:80]}' for l in past_lessons) or '  keine'

    # 5) KI fragt sich: Warum hab ich das nicht gesehen?
    prompt = f"""Du bist Hermes, ein AI Trading-Agent der heute ({today}) folgendes beobachtet hat:

=== MARKTBEWEGUNGEN HEUTE ===
Richtig erkannt LONG ({len(correct_long)}):
{chr(10).join(correct_long[:5]) or '  keine'}

Richtig erkannt SHORT ({len(correct_short)}):
{chr(10).join(correct_short[:5]) or '  keine'}

VERPASST — haette LONG sein sollen ({len(missed_long)}):
{chr(10).join(missed_long[:6]) or '  keine'}

VERPASST — haette SHORT sein sollen ({len(missed_short)}):
{chr(10).join(missed_short[:6]) or '  keine'}

Falsch — BullSignal aber gefallen ({len(false_long)}):
{chr(10).join(false_long[:4]) or '  keine'}

Falsch — BearSignal aber gestiegen ({len(false_short)}):
{chr(10).join(false_short[:4]) or '  keine'}

=== MEINE BESTEHENDEN STRATEGIEN ===
{strat_str}

=== MEINE LEKTIONEN ===
{lessons_str}

=== DEINE AUFGABE ===
Analysiere die VERPASSTEN Moves. Frage dich:
- Welche Kombination von Signalen (P/C Ratio, CallVOI, Sweeps, VolRatio) war bei den Gewinnern?
- Warum hab ich die SHORT-Kandidaten nicht gesehen? Was haette ich pruefen muessen?
- Entwickle 2-3 einfache IF→THEN Regeln die ich beim NAECHSTEN Scan anwenden kann
- Erklaere ob deine bestehenden Strategien noch funktionieren oder angepasst werden muessen

Antworte NUR mit validem JSON (keine Backticks, kein anderer Text):
{{
  "new_strategies": [
    {{
      "id": "kurzer_eindeutiger_name",
      "rule": "WENN [konkrete Bedingung z.B. P/C < 0.4 UND CallVOI > 8x UND VolRatio > 3x] DANN LONG",
      "why": "Erklaerung warum diese Regel funktioniert",
      "confidence": 0.7,
      "applies_to": "alle | tech | small_cap | earnings | risk_off"
    }}
  ],
  "update_strategies": [
    {{"id": "bestehende_strategie_id", "new_hit_rate": 0.75, "note": "warum angepasst"}}
  ],
  "remove_strategies": ["id_der_nicht_mehr_gilt"],
  "key_insight": "1-2 Saetze: was war heute das wichtigste Muster",
  "missed_reason": "Warum hab ich die verpassten Moves nicht erkannt — fehlt mir ein Signal?"
}}"""

    resp = _nous_call(
        prompt,
        system='Du bist Hermes AI Trading Stratege. Entwickle einfache, testbare IF-THEN Handelsregeln aus echten Marktdaten. JSON only.',
        max_tokens=800, temperature=0.4
    )
    if not resp:
        identity['last_strategy_build'] = today
        save_identity(identity)
        return

    try:
        import re as _re
        m = _re.search(r'\{[\s\S]*\}', resp)
        if not m:
            return
        data = json.loads(m.group())

        # Neue Strategien eintragen
        existing_ids = {s['id'] for s in strategies}
        for ns in data.get('new_strategies', []):
            sid = ns.get('id', '').strip()
            if not sid or sid in existing_ids:
                continue
            strategies.append({
                'id':         sid,
                'rule':       ns.get('rule', '')[:150],
                'why':        ns.get('why', '')[:100],
                'confidence': float(ns.get('confidence', 0.6)),
                'applies_to': ns.get('applies_to', 'alle'),
                'created':    today,
                'hits':       0,
                'samples':    0,
                'hit_rate':   float(ns.get('confidence', 0.6)),
            })

        # Bestehende Strategien aktualisieren
        strat_map = {s['id']: s for s in strategies}
        for us in data.get('update_strategies', []):
            sid = us.get('id', '')
            if sid in strat_map:
                strat_map[sid]['hit_rate'] = float(us.get('new_hit_rate', strat_map[sid]['hit_rate']))
                strat_map[sid].setdefault('notes', []).insert(0, f'{today}: {us.get("note","")[:60]}')

        # Alte Strategien entfernen
        remove = set(data.get('remove_strategies', []))
        strategies = [s for s in strategies if s['id'] not in remove][-20:]
        identity['strategies'] = strategies

        # Key Insight als Lektion speichern
        insight = data.get('key_insight', '').strip()
        missed  = data.get('missed_reason', '').strip()
        if insight:
            identity.setdefault('lessons', []).insert(0, {
                'date':    today,
                'lesson':  insight,
                'pattern': 'strategy_builder',
                'missed':  missed,
            })
            identity['lessons'] = identity['lessons'][:30]

        identity['last_strategy_build'] = today
        save_identity(identity)

        # Telegram Report
        new_strats = data.get('new_strategies', [])
        lines = [f'<b>HERMES STRATEGIE-UPDATE {today}</b>']
        if insight:
            lines.append(insight)
        if missed:
            lines.append(f'Verpasst weil: {missed[:100]}')
        if new_strats:
            lines.append(f'\nNeue Regeln ({len(new_strats)}):')
            for ns in new_strats[:3]:
                lines.append(f'  → {ns.get("rule","")[:80]}')
        lines.append(f'Aktive Strategien: {len(strategies)}')
        tg_send('\n'.join(lines))

    except Exception:
        pass

    identity['last_strategy_build'] = today
    save_identity(identity)


def hermes_ai_self_reflection(false_signals: list, hits: dict, misses: dict,
                               win_rate: float, today: str,
                               direction_flip: bool = False,
                               flip_context: str = '',
                               market_bias_log: dict = None,
                               daily_ctx: dict = None) -> str:
    """
    Hermes AI analysiert seine eigenen Fehler mit 14-Tage-Gedächtnis.
    Bei Richtungswechsel fragt Hermes: 'Was habe ich verpasst?'
    Schreibt neue Regeln in hermes_identity.json.
    """
    import re as _re
    if not NOUS_KEY:
        return ''
    identity = load_identity()
    if identity.get('last_reflection') == today:
        return ''

    past_lessons = identity.get('lessons', [])[:8]
    past_rules   = identity.get('rules', [])[:12]

    false_str   = '\n'.join(f'  - {f["sym"]}: {f["signal"]} empfohlen aber {f["chg"]:+.1f}% ({f["reason"]})'
                            for f in false_signals[:6]) or '  keine'
    hit_str     = ', '.join(f'{s} {d["chg"]:+.1f}%' for s, d in list(hits.items())[:5]) or 'keine'
    miss_str    = ', '.join(f'{s} {d["chg"]:+.1f}%' for s, d in list(misses.items())[:5]) or 'keine'
    lessons_str = '\n'.join(f'  [{l["date"]}] {l["lesson"][:90]}'
                            for l in past_lessons) or '  noch keine'
    rules_str   = '\n'.join(f'  - {r}' for r in past_rules) or '  noch keine'

    # 14-Tage-Kontext aufbauen
    bias_log = market_bias_log or {}
    ctx_14   = daily_ctx or {}
    last_14_dates = sorted(bias_log.keys())[-14:]
    history_lines = []
    for d in last_14_dates:
        b  = bias_log.get(d, {})
        dc = ctx_14.get(d, {})
        wr = b.get('win_rate', dc.get('win_rate', '?'))
        qqq = b.get('qqq_chg', 0)
        bias = b.get('bias', '?')
        hits_d = ', '.join(b.get('hits', dc.get('hits', [])) or [])[:60]
        false_d = ', '.join(b.get('false', dc.get('false_signals', [])) or [])[:40]
        history_lines.append(
            f'  {d}: QQQ{qqq:+.1f}% [{bias}] WinRate:{wr}%'
            + (f' | Richtig:{hits_d}' if hits_d else '')
            + (f' | FALSCH:{false_d}' if false_d else '')
        )
    history_str = '\n'.join(history_lines) or '  kein Verlauf'

    # Spezifische Frage bei Richtungswechsel
    flip_section = ''
    if direction_flip and flip_context:
        flip_section = f"""
=== RICHTUNGSWECHSEL ERKANNT ===
{flip_context}
Frage an dich selbst: Was habe ich gestern verpasst?
Welche Warnsignale hat der Markt gegeben, die ich ignoriert oder falsch bewertet habe?
War das Geopolitik, Earnings, Makro, oder ein technisches Muster?
"""

    prompt = f"""Du bist Hermes, ein AI Trading-Agent der sich täglich selbst verbessert.
Du hast Zugriff auf 14 Tage deines eigenen Gedächtnisses und analysierst Muster ueber mehrere Tage.

=== HEUTE ({today}) ===
Win-Rate: {win_rate:.1f}%
Richtige Signale: {hit_str}
Verpasste Moves: {miss_str}
Falsche Signale (empfohlen aber falsch):
{false_str}
{flip_section}
=== 14-TAGE VERLAUF ===
{history_str}

=== MEINE REGELN (aktuell) ===
{rules_str}

=== MEINE LEKTIONEN (aus Fehlern) ===
{lessons_str}

Aufgabe:
1. Erkenne Muster ueber mehrere Tage (nicht nur heute)
2. Bei Richtungswechsel: was habe ich STRUKTURELL verpasst?
3. Schreibe konkrete, umsetzbare neue Regeln
4. Erklaere in 2-3 Saetzen deine wichtigste Erkenntnis

Antworte NUR mit diesem JSON:
{{
  "new_rules": ["max 3 Regeln, sehr konkret: z.B. 'Keine SHORTs wenn >= 5 Call-Sweeps erkannt'"],
  "new_lesson": "2-3 Saetze: was heute + im Verlauf der letzten Tage gelernt",
  "pattern": "Muster-Name hinter den Fehlern (z.B. 'Geopolitik-Flip', 'Hedge-Misread', 'Momentum-Blindspot')",
  "what_i_missed": "Konkret: welche Signale habe ich bei Richtungswechsel ignoriert",
  "remove_rules": ["veraltete Regeln entfernen oder leer lassen"],
  "weight_adjustments": {{"min_score_long": 0, "min_score_short": 0}}
}}"""

    response = _nous_call(
        prompt,
        system='Du bist Hermes AI Trading Agent mit 14-Tage-Gedaechtnis. Analysiere Muster ueber mehrere Tage. Schreibe deine eigenen Handelsregeln. Antworte AUSSCHLIESSLICH mit validem JSON ohne Backticks.',
        max_tokens=700, temperature=0.3
    )
    if not response:
        return ''

    try:
        match = _re.search(r'\{[\s\S]*\}', response)
        if not match:
            return ''
        data = json.loads(match.group())

        # Neue Regeln eintragen (Duplikate überspringen)
        existing = set(identity.get('rules', []))
        for rule in data.get('new_rules', []):
            if rule and rule.strip() and rule.strip() not in existing:
                identity.setdefault('rules', []).append(rule.strip())

        # Veraltete Regeln entfernen
        remove = set(data.get('remove_rules', []))
        identity['rules'] = [r for r in identity.get('rules', []) if r not in remove][-15:]

        # Neue Lektion + "Was habe ich verpasst?"
        lesson      = data.get('new_lesson', '').strip()
        what_missed = data.get('what_i_missed', '').strip()
        pattern     = data.get('pattern', '')
        if lesson:
            identity.setdefault('lessons', []).insert(0, {
                'date':         today,
                'lesson':       lesson,
                'pattern':      pattern,
                'what_missed':  what_missed,
                'win_rate':     win_rate,
                'false_count':  len(false_signals),
                'direction_flip': direction_flip,
            })
            identity['lessons'] = identity['lessons'][:30]

        # Gewichtungen aus KI-Vorschlag anwenden
        w_adj = data.get('weight_adjustments', {})
        if w_adj:
            learn = load_learning()
            for k, v in w_adj.items():
                if k in learn.get('weights', {}) and isinstance(v, (int, float)) and v != 0:
                    old_v = learn['weights'][k]
                    learn['weights'][k] = max(1, old_v + int(v))
            save_learning(learn)

        identity['last_reflection'] = today
        save_identity(identity)

        # Telegram Report
        lines = [f'<b>HERMES LERNT — {today}</b>']
        if lesson:
            lines.append(lesson)
        if what_missed and direction_flip:
            lines.append(f'Was verpasst: {what_missed[:120]}')
        if pattern:
            lines.append(f'Muster: {pattern}')
        new_rules = data.get('new_rules', [])
        if new_rules:
            lines.append('Neue Regeln: ' + ' | '.join(new_rules[:2]))
        tg_send('\n'.join(lines))
        return lesson
    except Exception:
        return ''


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _classify_alert(msg):
    """Klassifiziert Alert-Typ für Icon und Farbe im Web Feed."""
    m = msg.lower()
    if any(x in m for x in ['rotation','sektor','geld fliesst']): return 'rotation', '#8080ff', '🔄'
    if any(x in m for x in ['earnings','earning','sell-the-news','drop.*%']): return 'earnings', '#ffa040', '📊'
    if any(x in m for x in ['extreme','mover','signal','long','short','pick']): return 'signal', '#4dff91', '🎯'
    if any(x in m for x in ['watchdog','stuck','neustart','fehler','error']): return 'system', '#ff4d4d', '⚙️'
    if any(x in m for x in ['selbst','learning','gelernt','pattern','win-rate']): return 'learning', '#ffd700', '🧠'
    if any(x in m for x in ['intelligence','24h','gainer','loser']): return 'intel', '#60a5fa', '🔍'
    if any(x in m for x in ['marktschluss','close','analyse']): return 'close', '#c084fc', '📈'
    return 'info', '#94a3b8', '💬'

def feed_push(msg, alert_type=None):
    """Fügt Alert zum Web Live-Feed hinzu."""
    import re
    clean = re.sub(r'<[^>]+>', '', msg).strip()[:300]
    if not clean:
        return
    atype, color, icon = _classify_alert(msg) if not alert_type else (alert_type, '#94a3b8', '💬')
    entry = {
        'ts':    datetime.now().strftime('%H:%M'),
        'msg':   clean,
        'type':  atype,
        'color': color,
        'icon':  icon,
    }
    feed = state.get('live_feed', [])
    feed.insert(0, entry)
    state['live_feed'] = feed[:80]  # max 80 Einträge

def tg_send(msg):
    feed_push(msg)   # immer auch in Web Feed
    try:
        ctx = ssl.create_default_context()
        url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
        data = json.dumps({'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'HTML'}).encode()
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, context=ctx, timeout=8)
    except:
        pass

def load_results():
    try:
        if os.path.exists(RESULTS_FILE):
            with open(RESULTS_FILE) as f:
                return json.load(f)
    except:
        pass
    return None

def save_results(data):
    try:
        with open(RESULTS_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def results_hash(data):
    return data.get('time', '') if data else ''

def progress_cb(i, total, ticker):
    state['progress']       = i
    state['progress_total'] = total
    state['current_ticker'] = ticker

def next_scan_time():
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=AUTO_SCAN_UTC_HOUR, minute=AUTO_SCAN_UTC_MINUTE,
                            second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.strftime('%Y-%m-%d %H:%M UTC')

# ── Scan-Thread ───────────────────────────────────────────────────────────────

def run_scan_thread(trigger='manual'):
    if not _scan_lock.acquire(blocking=False):
        return   # bereits ein Scan aktiv — sicher beenden
    from scanner import run_scan
    state['running'] = True
    state['error']   = None
    try:
        results = run_scan(progress_cb=progress_cb)
        state['results']           = results
        state['last_scan']         = results['time']
        state['last_results_hash'] = results_hash(results)
        save_results(results)

        lines = [f'<b>OPTIONS SCANNER {results["time"]}</b>  [{trigger}]']
        lines.append(f'Gescannt: {results["scanned"]}/{results["total"]} Aktien\n')

        lines.append('<b>TOP LONG:</b>')
        for r in results['longs'][:5]:
            b = r.get('best') or {}
            lines.append(f'  UP {r["t"]}  ${r["price"]}  P/C:{r["pc"]}  Score:{r["score"]}')
            if b:
                lines.append(f'     CALL ${b.get("strike")} @ ${b.get("pr")}  Exp:{b.get("exp")}')

        lines.append('\n<b>TOP SHORT:</b>')
        for r in results['shorts'][:5]:
            b = r.get('best') or {}
            lines.append(f'  DN {r["t"]}  ${r["price"]}  Score:{r["score"]}  Drop:{r["drop_high"]}%')
            if b:
                lines.append(f'     PUT ${b.get("strike")} @ ${b.get("pr")}  Exp:{b.get("exp")}')

        if results['movers']:
            lines.append('\n<b>NEXT MOVER:</b>')
            for r in results['movers'][:3]:
                b = r.get('best') or {}
                lines.append(f'  ** {r["t"]}  ${r["price"]}  P/C:{r["pc"]}  Trend:{r["trend"]}%')
                if b:
                    lines.append(f'     CALL ${b.get("strike")} @ ${b.get("pr")}  {r.get("kat_text","")[:40]}')

        tg_send('\n'.join(lines))

        # Social KI-Score + HF 13F im Hintergrund laden (blockiert nicht)
        threading.Thread(target=enrich_background, args=(results,), daemon=True).start()

    except Exception as e:
        state['error'] = str(e)
        tg_send(f'Scanner Fehler: {e}')
    finally:
        state['running']        = False
        state['progress']       = 0
        state['current_ticker'] = ''
        state['next_scan']      = next_scan_time()
        _scan_lock.release()

def enrich_background(scan_results: dict):
    """
    Läuft nach dem Scan im Hintergrund:
    - Social KI-Score berechnen (Reddit/Stocktwits + Scan-Daten)
    - Hedge Fund 13F laden (SEC EDGAR)
    - Influencer-Feeds (Leopold etc.)
    Dauert 30-60s — blockiert NICHT den Haupt-Scan.
    """
    try:
        from scanner import (get_cached_social, get_cached_influencers,
                             get_alpaca_news, POS_KEYS, NEG_KEYS)
        scan_map = {r['t']: r for r in
                    scan_results.get('longs', []) +
                    scan_results.get('shorts', []) +
                    scan_results.get('watch', [])}

        # ── Social KI-Score + Preis + Heute% + Grund ────────────────────────
        import urllib.request as _ur2, ssl as _ssl2, json as _json2
        _ctx2 = _ssl2.create_default_context()
        POLYGON_API = os.environ.get('POLYGON_API_KEY', '')

        # Crypto komplett raus aus Social
        _CRYPTO = {'BTC','ETH','SOL','BTC.X','ETH.X','DOGE','XRP','MSTR',
                   'SHIB','ADA','AVAX','MATIC','LTC','BCH','LINK','UNI',
                   'ATOM','XLM','ALGO','VET','HBAR','SAND','MANA','CRO'}

        # Alpaca Keys für Live-Preise
        _ALPA_KEY = os.environ.get('ALPACA_KEY', 'PK5T6OU5ENWZQK5DVZ746MHHEF')
        _ALPA_SEC = os.environ.get('ALPACA_SECRET', '3nngSp7NksYikEZvf5hLihWEBtFdnuG336KfeYvFb5D9')

        def _poly_live(sym):
            """Live-Preis + heute% via Alpaca (zuverlässig), 7T% via Polygon Aggregates."""
            if sym in _CRYPTO:
                return 0.0, 0.0, 0.0
            price = today_chg = trend_7d = 0.0
            POLY2 = os.environ.get('POLYGON_API_KEY', '')

            # 1) Alpaca Snapshot → Live-Preis + heute% (kostenlos, kein Auth-Problem)
            try:
                alp_url = f'https://data.alpaca.markets/v2/stocks/{sym}/snapshot'
                alp_req = _ur2.Request(alp_url, headers={
                    'APCA-API-KEY-ID':     _ALPA_KEY,
                    'APCA-API-SECRET-KEY': _ALPA_SEC,
                })
                with _ur2.urlopen(alp_req, context=_ctx2, timeout=8) as r:
                    ad = _json2.loads(r.read())
                lq   = ad.get('latestTrade', {}) or ad.get('latestQuote', {})
                dbar = ad.get('dailyBar', {})
                pbar = ad.get('prevDailyBar', {})
                price = float(lq.get('p') or dbar.get('c') or 0)
                prev_c = float(pbar.get('c') or price or 1)
                if price and prev_c:
                    today_chg = round((price - prev_c) / prev_c * 100, 1)
            except Exception:
                pass

            # 2) Polygon Aggregates → 7T Trend + Fallback-Preis
            try:
                from_d = (datetime.now() - timedelta(days=12)).strftime('%Y-%m-%d')
                to_d   = datetime.now().strftime('%Y-%m-%d')
                agg_url = f'https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/{from_d}/{to_d}?adjusted=true&sort=asc&limit=12&apiKey={POLY2}'
                with _ur2.urlopen(_ur2.Request(agg_url), context=_ctx2, timeout=8) as r2:
                    pg = _json2.loads(r2.read())
                bars = [b['c'] for b in pg.get('results', []) if b.get('c')]
                if len(bars) >= 2:
                    if not price:
                        price     = round(bars[-1], 2)
                    trend_7d  = round((bars[-1] - bars[0]) / bars[0] * 100, 1)
                    if today_chg == 0.0:
                        today_chg = round((bars[-1] - bars[-2]) / bars[-2] * 100, 1)
            except Exception:
                pass
            return round(price, 2), today_chg, trend_7d

        def _poly_news_reason(sym):
            """Holt neueste Polygon-News-Headline als Trend-Grund."""
            if not POLYGON_API:
                return ''
            try:
                url = f'https://api.polygon.io/v2/reference/news?ticker={sym}&limit=1&apiKey={POLYGON_API}'
                req = _ur2.Request(url)
                with _ur2.urlopen(req, context=_ctx2, timeout=6) as r:
                    d = _json2.loads(r.read())
                items = d.get('results', [])
                if items:
                    return items[0].get('title', '')[:70]
            except Exception:
                pass
            return ''

        social_raw, social_scores_map = get_cached_social()
        # Crypto komplett rausfiltern
        social_filtered = [s for s in social_raw if s not in _CRYPTO and not s.endswith('.X')]
        social_data = []
        for sym in social_filtered[:12]:
            src_sc = social_scores_map.get(sym, 0)
            ki = min(30, int(src_sc / 3))
            price = today_chg = trend_7d = 0.0
            pc_ratio = 1.0
            news_kat = kat_text = reason = ''
            best_opt = signal = None

            if sym in scan_map:
                r = scan_map[sym]
                price    = r['price']
                trend_7d = r['trend']
                prev_chg = r.get('prev_chg', 0)
                today_chg = prev_chg          # Vortag-% als Näherung für "heute"
                pc_ratio  = r['pc']
                news_kat  = r['katalysator']
                kat_text  = r.get('kat_text', '')
                best_opt  = r.get('best')
                signal    = r['signal']
                reason    = kat_text          # Aus Scan-Daten direkt
                if r['pc'] < 0.3:  ki += 20
                elif r['pc'] < 0.5: ki += 10
                if trend_7d > 10: ki += 20
                elif trend_7d > 5: ki += 10
                elif trend_7d < -5: ki -= 10
                if news_kat == 'POSITIV': ki += 20
                if news_kat == 'NEGATIV': ki -= 10
                dp_t = (r.get('dp') or {}).get('dp_total', 0) or 0
                if dp_t >= 1_000_000: ki += 10
            else:
                # Ticker nicht im Scan → Yahoo + Polygon News
                price, today_chg, trend_7d = _poly_live(sym)
                if today_chg > 3:  ki += 15
                elif today_chg > 1: ki += 8
                elif today_chg < -3: ki -= 5
                if trend_7d > 10: ki += 15
                elif trend_7d > 5: ki += 8
                reason = _poly_news_reason(sym)
                if reason:
                    ki += 10
                    news_kat = 'POSITIV'

            social_data.append({
                'sym':      sym,
                'price':    price,
                'today_chg': round(today_chg, 1),
                'trend_7d': round(trend_7d, 1),
                'pc':       round(pc_ratio, 3),
                'ki_score': max(0, min(99, ki)),
                'mentions': src_sc,
                'news_kat': news_kat,
                'reason':   reason[:70] if reason else (kat_text[:70] if kat_text else ''),
                'best':     best_opt,
                'signal':   signal or '─',
            })
        social_data.sort(key=lambda x: -x['ki_score'])

        # ── Hedge Fund 13F (SEC EDGAR — nur Filing-Datum + bekannte Holdings) ────
        import urllib.request as _ur2, ssl as _ssl2
        _ctx2 = _ssl2.create_default_context()
        _HDR2 = {'User-Agent': 'scanner/3.0 yusufbruder@gmail.com', 'Accept': 'application/json'}

        HF_CIK = {
            "Pershing Square (Ackman)": "0001336528",
            "Duquesne (Druckenmiller)": "0001536411",
            "Tiger Global":             "0001167483",
            "Coatue Management":        "0001336119",
            "Appaloosa (Tepper)":       "0001418814",
        }
        # Bekannte aktuelle Positionen (Q1 2026, aus öffentlichen Quellen)
        HF_KNOWN = {
            "Pershing Square (Ackman)": [
                {'sym':'GOOGL','action':'GEHALTEN','val_m':2300,'date':'2026-05-15'},
                {'sym':'HHH',  'action':'AUFGESTOCKT','val_m':850,'date':'2026-05-15'},
                {'sym':'HILTON','action':'GEHALTEN','val_m':700,'date':'2026-05-15'},
            ],
            "Duquesne (Druckenmiller)": [
                {'sym':'NVDA','action':'NEU GEKAUFT','val_m':620,'date':'2026-05-15'},
                {'sym':'TSM', 'action':'AUFGESTOCKT','val_m':310,'date':'2026-05-15'},
                {'sym':'MSFT','action':'GEHALTEN','val_m':280,'date':'2026-05-15'},
            ],
            "Tiger Global": [
                {'sym':'META','action':'AUFGESTOCKT','val_m':950,'date':'2026-05-15'},
                {'sym':'MSFT','action':'GEHALTEN','val_m':600,'date':'2026-05-15'},
                {'sym':'AMZN','action':'GEHALTEN','val_m':540,'date':'2026-05-15'},
            ],
            "Coatue Management": [
                {'sym':'NVDA','action':'GEHALTEN','val_m':1200,'date':'2026-05-15'},
                {'sym':'AAPL','action':'AUFGESTOCKT','val_m':800,'date':'2026-05-15'},
                {'sym':'PLTR','action':'NEU GEKAUFT','val_m':320,'date':'2026-05-15'},
            ],
            "Appaloosa (Tepper)": [
                {'sym':'GOOGL','action':'AUFGESTOCKT','val_m':450,'date':'2026-05-15'},
                {'sym':'AMZN','action':'GEHALTEN','val_m':380,'date':'2026-05-15'},
                {'sym':'BABA','action':'REDUZIERT','val_m':200,'date':'2026-05-15'},
            ],
        }

        def _hf_filing_date(cik):
            try:
                pad = cik.lstrip('0').zfill(10)
                req = _ur2.Request(f'https://data.sec.gov/submissions/CIK{pad}.json', headers=_HDR2)
                with _ur2.urlopen(req, context=_ctx2, timeout=8) as r:
                    d = json.loads(r.read())
                fls = d.get('filings', {}).get('recent', {})
                forms, dates = fls.get('form', []), fls.get('filingDate', [])
                for i, frm in enumerate(forms[:20]):
                    if '13F' in frm:
                        return dates[i] if i < len(dates) else '2026-05-15'
            except Exception:
                pass
            return '2026-05-15'

        def _yahoo_price_change(sym, filing_date):
            """Aktueller Kurs + % seit Filing-Datum via Polygon."""
            POLY2 = os.environ.get('POLYGON_API_KEY', '')
            try:
                # Aktueller Kurs
                url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{sym}?apiKey={POLY2}'
                req = _ur2.Request(url)
                with _ur2.urlopen(req, context=_ctx2, timeout=8) as r:
                    d = json.loads(r.read())
                td = d.get('ticker', {})
                price_now = float((td.get('day') or {}).get('c') or (td.get('prevDay') or {}).get('c') or 0)
                # Kurs am Filing-Datum via Aggregates
                from_d = filing_date
                to_d   = (datetime.strptime(filing_date, '%Y-%m-%d') + timedelta(days=5)).strftime('%Y-%m-%d')
                agg_url = f'https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/{from_d}/{to_d}?adjusted=true&sort=asc&limit=5&apiKey={POLY2}'
                req2 = _ur2.Request(agg_url)
                with _ur2.urlopen(req2, context=_ctx2, timeout=8) as r2:
                    d2 = json.loads(r2.read())
                bars = d2.get('results', [])
                price_then = float(bars[0]['c']) if bars else 0.0
                since = round((price_now - price_then) / price_then * 100, 1) if price_then > 0 else 0.0
                return round(price_now, 2), round(price_then, 2), since
            except Exception:
                return 0.0, 0.0, 0.0

        hf_data = []
        for nm, cik in HF_CIK.items():
            try:
                filing_date = _hf_filing_date(cik)
                known = HF_KNOWN.get(nm, [])
                holdings = []
                for h in known:
                    sym = h['sym']
                    price_now, price_then, since = _yahoo_price_change(sym, h['date'])
                    holdings.append({
                        'sym':        sym,
                        'action':     h['action'],
                        'val_m':      h['val_m'],
                        'date':       h['date'],
                        'price_now':  price_now,
                        'price_then': price_then,
                        'since_pct':  since,
                    })
                hf_data.append({
                    'manager': nm,
                    'date':    filing_date,
                    'form':    '13F-HR',
                    'url':     f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F',
                    'holdings': holdings,
                })
            except Exception:
                pass
            time.sleep(0.5)

        # ── Situational Awareness LP (L. Aschenbrenner) — 13F Q1 2026 (filed 18.05.2026) ──
        # LONG: KI-Infrastruktur ("Electricity is the new oil")
        # PUT: Semiconductors als Hedge/Short ($7.7 Mrd Notional)
        leo_longs = [
            ('NBIS',  2600, 'NBIS — 38% des Portfolios ($2.6 Mrd) — Kernposition'),
            ('KEEL',   450, 'KEEL (ehem. BITF) — 19.88M Aktien — AI Datacenter'),
            ('CLSK',   380, 'CleanSpark — 12.28M Aktien — Bitcoin/AI Mining'),
            ('RIOT',   320, 'Riot Platforms — 11.50M Aktien'),
            ('BTDR',   180, 'Bitdeer — 3.44M Aktien — AI Compute'),
            ('IREN',   150, 'IREN — erhöht — AI Datacenter'),
            ('APLD',   120, 'Applied Digital — erhöht'),
        ]
        leo_puts = [
            ('SMH',  2040, 'SMH PUT $2.04 Mrd — Semiconductor Hedge'),
            ('NVDA', 1570, 'NVDA PUT $1.57 Mrd'),
            ('ORCL', 1070, 'ORCL PUT $1.07 Mrd'),
            ('AVGO', 1010, 'AVGO PUT $1.01 Mrd'),
            ('AMD',   969, 'AMD PUT $969 Mio'),
        ]
        leo_holdings = []
        for sym, val_m, reason in leo_longs:
            try:
                price_now, price_then, since = _yahoo_price_change(sym, '2026-01-01')
                if price_now > 0:
                    leo_holdings.append({
                        'sym': sym, 'action': 'KAUFT', 'val_m': val_m,
                        'date': '2026-Q1', 'reason': reason,
                        'price_now': price_now, 'price_then': price_then, 'since_pct': since,
                    })
            except Exception:
                pass
        for sym, val_m, reason in leo_puts:
            try:
                price_now, price_then, since = _yahoo_price_change(sym, '2026-01-01')
                if price_now > 0:
                    leo_holdings.append({
                        'sym': sym, 'action': 'PUT (SHORT)', 'val_m': val_m,
                        'date': '2026-Q1', 'reason': reason,
                        'price_now': price_now, 'price_then': price_then, 'since_pct': since,
                    })
            except Exception:
                pass
        if leo_holdings:
            hf_data.append({
                'manager': 'Situational Awareness LP (L. Aschenbrenner)',
                'date':    '2026-Q1 (13F 18.05.2026)',
                'form':    '13F',
                'url':     'https://trendspider.com/blog/leopold-aschenbrenner-situational-awareness-lp-13f-may-18-2026/',
                'holdings': leo_holdings,
            })

        # ── Influencer ────────────────────────────────────────────────────────
        influencers = get_cached_influencers()

        state['social_data'] = social_data
        state['hf_data']     = hf_data
        state['extra_ts']    = datetime.now().strftime('%H:%M')

        # Results mit extra Daten updaten + Hash ändern → Frontend bemerkt neue Daten
        if state['results']:
            merged = dict(state['results'])
            merged['social_data'] = social_data
            merged['hf_data']     = hf_data
            merged['influencers'] = influencers
            state['results'] = merged
            save_results(merged)
            state['last_results_hash'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    except Exception as e:
        print(f'[enrich] Fehler: {e}')


def run_followup():
    """Prüft um 10:00 ET ob die Signale von heute ihr Ziel erreicht haben."""
    today = datetime.now().strftime('%Y-%m-%d')
    if state['followup_date'] == today:
        return  # Heute schon gemacht
    data = state['results'] or load_results()
    if not data or data.get('today') != today:
        return  # Kein heutiger Scan

    # Polygon Snapshot — aktueller Kurs direkt aus Polygon (kein yfinance)
    POLY = os.environ.get('POLYGON_API_KEY', '')
    _ctx_fu = ssl.create_default_context()

    def _poly_current_price(sym):
        try:
            url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{sym}?apiKey={POLY}'
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=_ctx_fu, timeout=8) as r:
                d = json.loads(r.read())
            day = d.get('ticker', {}).get('day', {})
            prev = d.get('ticker', {}).get('prevDay', {})
            price = float(day.get('c') or prev.get('c') or 0)
            return price
        except Exception:
            return 0.0

    longs  = data.get('longs', [])[:5]
    shorts = data.get('shorts', [])[:5]
    followup_results = {'longs': [], 'shorts': [], 'time': datetime.now().strftime('%H:%M')}

    INVEST = 100   # $100 Basis-Investment

    def _calc_pnl(chg_pct, won, mult_str, best, signal):
        """Berechnet P&L bei $100 Investition in die empfohlene Option."""
        try:
            opt_pr = float((best or {}).get('pr') or 0)
            if opt_pr <= 0:
                return 0, 0
            # Multiplier aus Scan (z.B. "5x" → 5)
            mult = float(mult_str.replace('x','')) if mult_str and mult_str != 'None' else 3.0
            if won:
                # Ziel erreicht → Gewinn = mult * Investment
                profit = round(INVEST * mult, 0)
                total  = INVEST + profit
            else:
                # Ziel nicht erreicht → Option verliert proportional zum Stock-Move
                # Grobe Schätzung: Option bewegt sich 3x der Aktie
                opt_chg = chg_pct * 3
                opt_chg = max(-90, min(opt_chg, 200))  # realistisch begrenzen
                profit = round(INVEST * opt_chg / 100, 0)
                total  = INVEST + profit
            return round(profit, 0), round(total, 0)
        except Exception:
            return 0, INVEST

    for r in longs:
        try:
            current = _poly_current_price(r['t'])
            if current > 0:
                entry   = r['price']
                ziel    = r.get('ziel') or (entry * 1.02)
                chg_pct = (current - entry) / entry * 100
                won     = current >= ziel
                profit, total = _calc_pnl(chg_pct, won, r.get('mult'), r.get('best'), 'LONG')
                followup_results['longs'].append({'t': r['t'], 'signal': 'LONG', 'entry': entry,
                    'current': round(current, 2), 'ziel': round(ziel, 2),
                    'chg_pct': round(chg_pct, 1), 'won': bool(won),
                    'invest': INVEST, 'profit': profit, 'total': total})
        except Exception:
            pass

    for r in shorts:
        try:
            current = _poly_current_price(r['t'])
            if current > 0:
                entry   = r['price']
                ziel    = r.get('ziel') or (entry * 0.98)
                chg_pct = (current - entry) / entry * 100
                won     = current <= ziel
                profit, total = _calc_pnl(-chg_pct, won, r.get('mult'), r.get('best'), 'SHORT')
                followup_results['shorts'].append({'t': r['t'], 'signal': 'SHORT', 'entry': entry,
                    'current': round(current, 2), 'ziel': round(ziel, 2),
                    'chg_pct': round(chg_pct, 1), 'won': bool(won),
                    'invest': INVEST, 'profit': profit, 'total': total})
        except Exception:
            pass

    state['followup']      = followup_results
    state['followup_date'] = today

    # Telegram Report
    label = state.get('followup_label', '10:00')
    lines = [f'<b>📊 {label} SIGNAL REPORT — {today}</b>\n']
    all_res = followup_results['longs'] + followup_results['shorts']
    winners = sum(1 for r in all_res if r['won'])
    losers  = len(all_res) - winners
    lines.append(f'{"✅" if winners > losers else "❌"} Gewinner: {winners} | Verlierer: {losers}\n')
    for r in all_res:
        icon = '✅' if r['won'] else '❌'
        lines.append(f'{icon} <b>{r["t"]}</b> {r["signal"]}: {r["chg_pct"]:+.1f}% '
                     f'(Entry: ${r["entry"]} → ${r["current"]} | Ziel: ${r["ziel"]})')
    tg_send('\n'.join(lines))

# ── Auto-Scheduler: Scan 09:30 ET + Report 10:00 ET + Report 22:00 CET ──────

def auto_scheduler():
    while True:
        now = datetime.now(timezone.utc)
        targets = []
        for h, m, label in [
            (AUTO_SCAN_UTC_HOUR, AUTO_SCAN_UTC_MINUTE, 'scan'),
            (FOLLOWUP_UTC_HOUR,  FOLLOWUP_UTC_MINUTE,  'followup_10'),
            (FOLLOWUP2_UTC_HOUR, FOLLOWUP2_UTC_MINUTE, 'followup_22'),
        ]:
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
            targets.append((t, label))
        next_event, next_label = min(targets, key=lambda x: x[0])
        wait_sec = (next_event - now).total_seconds()
        state['next_scan'] = next_scan_time()
        time.sleep(max(wait_sec, 1))
        now2 = datetime.now(timezone.utc)
        if abs((now2 - next_event).total_seconds()) < 120:
            if next_label == 'scan' and not state['running']:
                threading.Thread(target=run_scan_thread, kwargs={'trigger': 'auto'}, daemon=True).start()
            elif next_label in ('followup_10', 'followup_22'):
                state['followup_label'] = '10:00 ET' if next_label == 'followup_10' else '22:00 CET'
                threading.Thread(target=run_followup, daemon=True).start()

# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Options Scanner</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0e1a; color: #e0e6f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }
.header { background: linear-gradient(135deg, #1a2540 0%, #0d1628 100%); padding: 12px 16px 0; border-bottom: 1px solid #1e3a5f; position: sticky; top: 0; z-index: 100; }
.header-top { display:flex; align-items:center; justify-content:space-between; padding-bottom:8px; }
.header h1 { font-size: 17px; color: #4db8ff; letter-spacing: 1px; display:inline; }
.live-dot { display:inline-block; width:8px; height:8px; background:#4dff91; border-radius:50%; margin-left:8px; animation: pulse 2s infinite; vertical-align:middle; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.header-info { font-size: 11px; color: #6b8cad; display:flex; gap:12px; flex-wrap:wrap; padding-bottom:8px; }
/* Tabs */
.tabs { display:flex; border-top:1px solid #1e3a5f; }
.tab-btn { flex:1; padding:9px 0; font-size:12px; font-weight:700; letter-spacing:1px; border:none; background:transparent; cursor:pointer; color:#4a6a8a; border-bottom:2px solid transparent; transition:all 0.2s; }
.tab-btn.active { color:#4db8ff; border-bottom-color:#4db8ff; background:rgba(77,184,255,0.06); }
.tab-btn.intel.active { color:#b070ff; border-bottom-color:#b070ff; background:rgba(176,112,255,0.06); }
.tab-btn.fib         { color:#4a6a8a; }
.tab-btn.fib.active  { color:#4dffaa; border-bottom-color:#4dffaa; background:rgba(77,255,170,0.06); }
.fib-card { background:#0d1f17; border:1px solid #1a3a25; border-radius:8px; padding:10px 12px; margin-bottom:8px; }
.fib-card.at-level  { border-color:#4dff88; box-shadow:0 0 8px rgba(77,255,136,0.15); }
.fib-card.near-key  { border-color:#ffd700; }
.fib-card.near-882  { border-color:#ff6b35; box-shadow:0 0 8px rgba(255,107,53,0.2); }
.fib-sym  { font-size:15px; font-weight:700; color:#e0e0e0; }
.fib-price{ font-size:13px; color:#4db8ff; font-weight:600; }
.fib-level-badge { display:inline-block; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:700; margin-right:4px; }
.badge-882 { background:#ff6b35; color:#fff; }
.badge-618 { background:#ffd700; color:#000; }
.badge-786 { background:#ff9500; color:#000; }
.badge-500 { background:#4db8ff; color:#000; }
.badge-382 { background:#2a5a8a; color:#fff; }
.badge-236 { background:#1a3a5a; color:#fff; }
.fib-bar  { height:6px; background:#0a1a12; border-radius:3px; margin:8px 0 4px; position:relative; overflow:visible; }
.fib-bar-fill { height:100%; border-radius:3px; background:linear-gradient(90deg,#1a4a2a,#4dff88); }
.fib-marker { position:absolute; top:-4px; width:2px; height:14px; background:#ffd700; }
.fib-section-title { color:#4a6a8a; font-size:10px; letter-spacing:1px; text-transform:uppercase; padding:8px 8px 4px; }
.tab-pane { display:none; }
.tab-pane.active { display:block; }
.scan-btn { display: block; width: calc(100% - 32px); margin: 14px 16px 0; padding: 13px; background: linear-gradient(135deg, #1a6b3c, #0d4a28); color: #4dff91; font-size: 15px; font-weight: bold; border: 1px solid #2d9e57; border-radius: 10px; cursor: pointer; text-align: center; letter-spacing: 1px; }
.scan-btn:disabled { background: #1a2540; color: #4a6a8a; border-color: #2a3a5a; cursor:default; }
.refresh-bar { display:flex; align-items:center; gap:8px; margin: 8px 16px 0; font-size:11px; color:#4a6a8a; }
.refresh-dot { width:6px; height:6px; background:#4a6a8a; border-radius:50%; flex-shrink:0; }
.refresh-dot.active { background:#4dff91; animation: pulse 1.5s infinite; }
.progress-wrap { margin: 10px 16px 0; }
.progress-bar { height: 6px; background: #1e3a5f; border-radius: 3px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #4db8ff, #4dff91); transition: width 0.5s; }
.progress-text { font-size: 11px; color: #6b8cad; margin-top: 5px; text-align: center; }
.section { margin: 8px 0; }
.section-title { background: #111827; padding: 10px 16px; font-size: 11px; font-weight: bold; letter-spacing: 2px; color: #6b8cad; border-top: 1px solid #1e3a5f; border-bottom: 1px solid #1e3a5f; }
.section-title.long  { color: #4dff91; border-left: 3px solid #4dff91; }
.section-title.short { color: #ff4d6b; border-left: 3px solid #ff4d6b; }
.section-title.mover { color: #ffd700; border-left: 3px solid #ffd700; }
.section-title.news  { color: #4db8ff; border-left: 3px solid #4db8ff; }
.card { background: #111827; border: 1px solid #1e3a5f; margin: 8px; border-radius: 10px; overflow: hidden; transition: border-color 0.3s; }
.card:active { border-color: #4db8ff; }
.card-header { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; }
.ticker { font-size: 17px; font-weight: bold; color: #fff; }
.price  { font-size: 17px; font-weight: bold; }
.signal-long  { color: #4dff91; }
.signal-short { color: #ff4d6b; }
.signal-mover { color: #ffd700; }
.badge { font-size: 10px; font-weight: bold; padding: 3px 8px; border-radius: 10px; }
.badge-long  { background: #0d3a1f; color: #4dff91; border: 1px solid #2d9e57; }
.badge-short { background: #3a0d1a; color: #ff4d6b; border: 1px solid #9e2d40; }
.badge-mover { background: #3a2d00; color: #ffd700; border: 1px solid #9e8000; }
.badge-kat   { background: #1a2b3a; color: #4db8ff; border: 1px solid #2d6b9e; }
.card-body { padding: 6px 14px 12px; }
.row { display: flex; gap: 16px; margin: 4px 0; flex-wrap: wrap; }
.stat { display: flex; flex-direction: column; }
.stat-label { font-size: 10px; color: #4a6a8a; text-transform: uppercase; letter-spacing: 0.5px; }
.stat-value { font-size: 13px; font-weight: bold; color: #c0d4e8; margin-top: 1px; }
.option-box { background: #0d1628; border: 1px solid #1e3a5f; border-radius: 8px; padding: 8px 10px; margin-top: 8px; }
.option-main { font-size: 14px; font-weight: bold; }
.option-detail { font-size: 11px; color: #6b8cad; margin-top: 3px; }
.hebel { font-size: 18px; font-weight: bold; color: #ffd700; }
.kat-text { font-size: 11px; color: #4db8ff; margin-top: 6px; padding: 5px 8px; background: #0a1929; border-radius: 5px; border-left: 2px solid #1e5a8f; }
.news-card { background: #111827; border: 1px solid #1e3a5f; margin: 8px; border-radius: 10px; padding: 12px 14px; }
.news-ticker { font-size: 11px; font-weight: bold; color: #4db8ff; margin-bottom: 4px; }
.news-title  { font-size: 13px; color: #c0d4e8; line-height: 1.4; }
.news-kat    { display: inline-block; margin-top: 6px; font-size: 10px; padding: 2px 8px; border-radius: 8px; }
.news-pos    { background: #0d3a1f; color: #4dff91; }
.news-neg    { background: #3a0d1a; color: #ff4d6b; }
.watch-row   { padding: 6px 16px; border-bottom: 1px solid #1e2a3a; display:flex; justify-content:space-between; align-items:center; }
.empty       { text-align: center; color: #4a6a8a; padding: 24px; font-size: 13px; line-height:1.6; }
.pct-pos { color: #4dff91; }
.pct-neg { color: #ff4d6b; }
.zdte { color: #ff9900; font-size: 10px; font-weight: bold; }
.new-flash { animation: flash 2s ease-out; }
@keyframes flash { 0% { background: #1a4a2a; } 100% { background: #111827; } }
</style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <div><h1>OPTIONS SCANNER</h1><span class="live-dot" id="liveDot"></span></div>
    <div style="display:flex;gap:6px;align-items:center">
      <div id="hermes-badge" style="background:#0a2a1a;border:1px solid #2d9e57;border-radius:20px;padding:3px 10px;display:flex;align-items:center;gap:5px;font-size:11px;font-weight:700;color:#4dff91;cursor:default" title="Hermes Agent Status">
        <span style="width:6px;height:6px;background:#4dff91;border-radius:50%;display:inline-block;animation:pulse 2s infinite"></span>
        <span id="hermes-status-text">HERMES</span>
      </div>
      <div id="autotrade-btn" onclick="toggleAutoTrade()" style="background:#0a1a2a;border:1px solid #1e3a5f;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:700;color:#4a6a8a;cursor:pointer" title="Auto-Trade ein/ausschalten">
        🤖 AUTO AUS
      </div>
      <div onclick="closeAllTrades()" style="background:#1a0a0a;border:1px solid #5f1e1e;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:700;color:#8a4a4a;cursor:pointer;margin-left:4px" title="Alle offenen Positionen schließen">
        🔒 CLOSE ALL
      </div>
    </div>
  </div>
  <div class="header-info" style="padding:0 0 6px">
    <span id="lastScanInfo">Lade...</span>
    <span id="nextScanInfo"></span>
  </div>
  <div class="tabs">
    <button class="tab-btn active"      id="tab1Btn" onclick="showTab(1)">📊 SCANNER</button>
    <button class="tab-btn intel"       id="tab2Btn" onclick="showTab(2)">🔍 INTEL</button>
    <button class="tab-btn fib"         id="tab3Btn" onclick="showTab(3)">📐 FIB</button>
  </div>
</div>

<div id="tab1">
<button class="scan-btn" id="scanBtn" onclick="startScan()">SCAN STARTEN</button>

<div class="refresh-bar">
  <span class="refresh-dot" id="refreshDot"></span>
  <span id="refreshInfo">Prüfe alle 60s auf neue Ergebnisse</span>
</div>

<div class="progress-wrap" id="progressWrap" style="display:none">
  <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  <div class="progress-text" id="progressText">Initialisiere...</div>
</div>

<div id="content">
  <div class="empty">Klicke <b>SCAN STARTEN</b> oder warte auf den täglichen Auto-Scan (09:30 ET).</div>
</div>
</div><!-- end tab1 -->

<div id="tab2" style="display:none">
  <div id="intel-content">
    <div class="empty" style="padding:20px 16px">
      Intel-Daten werden nach dem Scan geladen...<br>
      <span style="color:#4a6a8a;font-size:12px">Reddit/Stocktwits KI-Score, Hedge Fund 13F, Leopold Aschenbrenner</span>
    </div>
  </div>
</div>

<div id="tab3" style="display:none">
  <div style="padding:12px 8px 6px">
    <button onclick="loadFibScan()" style="background:linear-gradient(135deg,#1a3a2a,#0d2a1a);border:1px solid #2a6a3a;color:#4dff88;padding:9px 22px;border-radius:6px;font-size:12px;font-weight:700;letter-spacing:1px;cursor:pointer;width:100%">
      FIBONACCI SCAN STARTEN
    </button>
  </div>
  <div id="fib-status" style="text-align:center;color:#4a6a8a;font-size:11px;padding:4px 8px"></div>
  <div id="fib-content">
    <div class="empty" style="padding:20px 16px">
      Fibonacci Scanner — 88.2% / 61.8% / 78.6% Level-Erkennung<br>
      <span style="color:#4a6a8a;font-size:12px">SPY, QQQ, Gold, Silver, Stocks — alle Instrumente auf einen Blick</span>
    </div>
  </div>
</div>

<script>
let lastHash = null;
let refreshInterval = null;
let refreshCountdown = 60;

function pct(v) {
  if (v == null || isNaN(v)) return '<span style="color:#4a6a8a">—</span>';
  let cls = v >= 0 ? 'pct-pos' : 'pct-neg';
  return '<span class="' + cls + '">' + (v >= 0 ? '+' : '') + v.toFixed(1) + '%</span>';
}

function optionBox(b, otype, mult, today) {
  if (!b) return '';
  let color = otype === 'CALL' ? '#4dff91' : '#ff4d6b';
  let zdte  = b.exp === today ? '<span class="zdte"> [HEUTE]</span>' : '';
  let m     = mult ? '<span class="hebel">' + mult + '</span>' : '';
  return '<div class="option-box">'
    + '<div class="option-main" style="color:' + color + '">'
    + otype + ' $' + b.strike + ' (' + (b.pct >= 0 ? '+' : '') + b.pct + '%) @ <b>$' + b.pr + '</b>  ' + m
    + '</div>'
    + '<div class="option-detail">Exp: ' + b.exp + zdte
    + ' &nbsp;|&nbsp; Vol: ' + b.vol.toLocaleString()
    + ' &nbsp;|&nbsp; OI: ' + b.oi.toLocaleString() + '</div>'
    + '</div>';
}

function renderCard(r, cls, isNew) {
  let b = r.best;
  // Signal-Farbe immer nach echtem Signal, nicht nach Sektion
  let realSig = r.signal || (cls === 'long' ? 'LONG' : cls === 'short' ? 'SHORT' : 'MOVER');
  let sigColor = realSig === 'LONG' ? 'signal-long' : (realSig === 'SHORT' ? 'signal-short' : 'signal-mover');
  let badge    = realSig === 'LONG' ? 'badge-long'  : (realSig === 'SHORT' ? 'badge-short'  : 'badge-mover');
  let ibkrLiveBadge = r.ibkr_live ? ' <span style="font-size:9px;background:#001a33;border:1px solid #00e5ff66;color:#00e5ff;padding:1px 5px;border-radius:3px">🏦 IBKR LIVE</span>' : '';
  let katStr = r.kat_strength || 'NORMAL';
  let katBadge = '';
  if (r.katalysator && r.katalysator !== 'KEIN') {
    if (r.katalysator === 'POSITIV') {
      if (katStr === 'EXTREME') katBadge = '<span class="badge" style="background:#3a0a00;color:#ff6020;border:1px solid #ff4000;font-weight:bold">🔥 EXTREME CATALYST</span>';
      else if (katStr === 'HIGH') katBadge = '<span class="badge" style="background:#1a0a30;color:#c080ff;border:1px solid #8040cc;font-weight:bold">🏛️ HIGH-IMPACT</span>';
      else katBadge = '<span class="badge badge-kat">POSITIV NEWS</span>';
    } else {
      if (katStr === 'HIGH' || katStr === 'EXTREME') katBadge = '<span class="badge" style="background:#2a0000;color:#ff4040;border:1px solid #aa0000;font-weight:bold">⚠️ HIGH-IMPACT SHORT</span>';
      else katBadge = '<span class="badge badge-kat" style="background:#2a0a0a;color:#ff8080">NEGATIV NEWS</span>';
    }
  }
  // Conviction + Signal-Basis Badge
  let basis = r.signal_basis || '';
  let conv  = r.conviction ? Math.round(r.conviction * 100) : 0;
  let convBadge = '';
  if (basis === 'POLYGON_CONFIRMED') {
    convBadge = '<span class="badge" style="background:#0a2a0a;color:#00ff88;border:1px solid #00cc66;font-weight:bold">✅ POLYGON+NEWS ' + conv + '%</span>';
  } else if (basis === 'POLYGON_ONLY') {
    convBadge = '<span class="badge" style="background:#0a1a2a;color:#40a0ff;border:1px solid #2060aa">📊 SMART MONEY ' + conv + '%</span>';
  } else if (basis === 'NEWS_ONLY') {
    convBadge = '<span class="badge" style="background:#1a1a0a;color:#c0c040;border:1px solid #808020">📰 NEWS ' + conv + '%</span>';
  } else if (basis === 'CONFLICT') {
    convBadge = '<span class="badge" style="background:#2a1a00;color:#ff8000;border:1px solid #aa5000;font-weight:bold">⚡ KONFLIKT — nicht traden</span>';
  }
  // Earnings Wahrscheinlichkeit Badge
  let earnBadge = '';
  if (r.earnings) {
    let ea = r.earnings;
    if (ea.verdict === 'SELL-THE-NEWS') {
      earnBadge = '<span class="badge" style="background:#2a0a00;color:#ff6040;border:1px solid #aa3000;font-weight:bold">'
        + 'EARNINGS ' + ea.date + ' | DROP ' + ea.sell_prob + '% | Run-Up ' + ea.run5 + '%</span>';
    } else if (ea.verdict === 'BEAT-ERWARTUNG') {
      earnBadge = '<span class="badge" style="background:#0a2a0a;color:#40ff80;border:1px solid #00aa40;font-weight:bold">'
        + 'EARNINGS ' + ea.date + ' | BEAT ' + ea.beat_prob + '%</span>';
    } else {
      earnBadge = '<span class="badge" style="background:#1a1a00;color:#ffd700;border:1px solid #aa9000">'
        + 'EARNINGS ' + ea.date + ' | Offen ' + ea.sell_prob + '% DROP / ' + ea.beat_prob + '% BEAT</span>';
    }
  }
  let conflictBadge = r.conflict
    ? '<span class="badge" style="background:#3a2000;color:#ffa500;border:1px solid #a06000">⚠ PULLBACK</span>' : '';
  let socialBadge = r.is_social || r.social_score > 20
    ? '<span class="badge" style="background:#1a1a3a;color:#b070ff;border:1px solid #6040aa">🔥 REDDIT/X</span>' : '';
  let dpM = r.dp && r.dp.dp_total ? (r.dp.dp_total / 1e6).toFixed(1) : 0;
  let dpBadge = dpM >= 1
    ? '<span class="badge" style="background:#1a1200;color:#ffa040;border:1px solid #a06000">🏦 Dark Pool $' + dpM + 'M</span>' : '';
  let swBadge = r.sweep && r.sweep.sweeps_call >= 2
    ? '<span class="badge" style="background:#0a1f0a;color:#80ff80;border:1px solid #208020">⚡ ' + r.sweep.sweeps_call + ' Sweeps</span>' : '';
  let opt = b ? optionBox(b, r.otype || (cls === 'short' ? 'PUT' : 'CALL'), r.mult, r.today) : '';
  // News mit Link
  let kat = '';
  if (r.kat_text) {
    kat = r.kat_url
      ? '<div class="kat-text"><a href="' + r.kat_url + '" target="_blank" rel="noopener" style="color:#60a5fa;text-decoration:underline;text-decoration-color:#1e3a5f">' + r.kat_text + ' ↗</a></div>'
      : '<div class="kat-text" style="color:#94a3b8">' + r.kat_text + '</div>';
  }
  let flash = isNew ? ' new-flash' : '';

  // Hermes AI Signal-Bewertung wenn vorhanden
  let _evals = (typeof lastData !== 'undefined' && lastData && lastData.hermes_signal_evals) ? lastData.hermes_signal_evals : {};
  let aiEval = _evals[r.t] || '';
  let aiBox = aiEval
    ? '<div style="margin:0 14px 10px;background:#060e1a;border:1px solid #00e5ff33;border-radius:8px;padding:8px 10px">'
      + '<div style="font-size:9px;font-weight:bold;color:#00e5ff;letter-spacing:2px;margin-bottom:4px">🤖 HERMES BEWERTUNG</div>'
      + '<div style="font-size:11px;color:#94c8e0;line-height:1.5;white-space:pre-wrap">' + aiEval + '</div>'
      + '</div>'
    : '';

  return '<div class="card' + flash + '">'
    + '<div class="card-header">'
    +   '<div><span class="ticker">' + r.t + '</span>'
    +   '<span class="price ' + sigColor + '" style="margin-left:10px">$' + r.price + '</span>' + ibkrLiveBadge + '</div>'
    +   '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + earnBadge + convBadge + katBadge + conflictBadge + socialBadge + dpBadge + swBadge
    +   '<span class="badge ' + badge + '">' + realSig + (r.score > 0 ? ' ' + r.score : '') + '</span></div>'
    + '</div>'
    + '<div class="card-body">'
    +   '<div class="row">'
    +     '<div class="stat"><span class="stat-label">Trend 10T</span><span class="stat-value">' + pct(r.trend) + '</span></div>'
    +     '<div class="stat"><span class="stat-label">Vortag</span><span class="stat-value">'    + pct(r.prev_chg) + '</span></div>'
    +     '<div class="stat"><span class="stat-label">P/C</span><span class="stat-value">'       + (r.pc != null ? r.pc : '—') + '</span></div>'
    +     '<div class="stat"><span class="stat-label">Hoch-Abst.</span><span class="stat-value ' + (r.drop_high < -5 ? 'pct-neg' : '') + '">' + (r.drop_high != null ? r.drop_high + '%' : '—') + '</span></div>'
    +   '</div>'
    + (function() {
        let sm = r.smart_money || {};
        let smHtml = '';
        let em = sm.expected_move || 0;
        let maxVoi = Math.max(sm.max_call_voi||0, sm.max_put_voi||0);
        let cp = sm.call_premium || 0;
        let pp = sm.put_premium || 0;
        if (em > 0 || maxVoi >= 3 || cp > 500000 || pp > 500000) {
          smHtml += '<div style="display:flex;flex-wrap:wrap;gap:5px;padding:5px 14px 2px">';
          if (em >= 5) smHtml += '<span style="font-size:10px;background:#1a1a0a;border:1px solid #ffd70066;color:#ffd700;padding:2px 7px;border-radius:8px">⚡ Erwartete Bewegung ±' + em + '%</span>';
          if (sm.max_call_voi >= 3) smHtml += '<span style="font-size:10px;background:#0a2a0a;border:1px solid #4dff9166;color:#4dff91;padding:2px 7px;border-radius:8px">🐋 Call Vol/OI ' + sm.max_call_voi + 'x</span>';
          if (sm.max_put_voi >= 3)  smHtml += '<span style="font-size:10px;background:#2a0a0a;border:1px solid #ff4d6b66;color:#ff4d6b;padding:2px 7px;border-radius:8px">🐋 Put Vol/OI ' + sm.max_put_voi + 'x</span>';
          if (cp > 1000000) smHtml += '<span style="font-size:10px;background:#0a1a2a;border:1px solid #4db8ff66;color:#4db8ff;padding:2px 7px;border-radius:8px">💰 Call Flow $' + (cp/1e6).toFixed(1) + 'M</span>';
          if (pp > 1000000) smHtml += '<span style="font-size:10px;background:#1a0a2a;border:1px solid #b070ff66;color:#b070ff;padding:2px 7px;border-radius:8px">💰 Put Flow $' + (pp/1e6).toFixed(1) + 'M</span>';
          smHtml += '</div>';
          // Größte Anomalie
          let top = (sm.anomalies||[])[0];
          if (top && top.ratio >= 5) {
            smHtml += '<div style="margin:3px 14px 0;padding:4px 8px;background:#0a0e1a;border-left:3px solid #ffd700;font-size:10px;color:#ffd700">'
              + '🔥 ' + top.type + ' $' + top.strike + ' Vol/OI: ' + top.ratio + 'x (' + top.vol.toLocaleString() + ' vol, OI:' + top.oi.toLocaleString() + ') @ $' + top.pr
              + '</div>';
          }
        }
        return smHtml;
      })()
    +   opt + kat
    + '</div>'
    + aiBox
    + '</div>';
}

function renderResults(data, isNew) {
  let html = '';

  // ── LIVE ALERT FEED (ersetzt Telegram) ───────────────────────────────────
  let feed = (data.live_feed || []).slice(0, 12);
  if (feed.length > 0) {
    html += '<div style="margin:8px;background:#080812;border:1px solid #2030aa44;border-radius:10px;overflow:hidden">'
      + '<div style="padding:8px 14px;background:linear-gradient(90deg,#0a0a20,#101030);border-bottom:1px solid #2030aa44;display:flex;justify-content:space-between;align-items:center">'
      + '<span style="font-size:10px;font-weight:bold;color:#8080ff;letter-spacing:2px">⚡ LIVE ALERTS</span>'
      + '<span style="font-size:9px;color:#4a5a8a">' + feed.length + ' Einträge</span>'
      + '</div>';
    feed.forEach(f => {
      let bg = f.type === 'signal'   ? '#0a1a0a' :
               f.type === 'earnings' ? '#1a0f00' :
               f.type === 'rotation' ? '#0a0a1e' :
               f.type === 'learning' ? '#1a1500' :
               f.type === 'system'   ? '#1a0808' : '#0a0a0a';
      html += '<div style="padding:6px 14px;border-top:1px solid #1a1a2a;background:' + bg + ';display:flex;gap:8px;align-items:flex-start">'
        + '<span style="font-size:14px;flex-shrink:0">' + (f.icon||'💬') + '</span>'
        + '<div style="flex:1">'
        + '<span style="font-size:11px;color:' + (f.color||'#94a3b8') + '">' + f.msg + '</span>'
        + '</div>'
        + '<span style="font-size:9px;color:#4a5a8a;flex-shrink:0">' + f.ts + '</span>'
        + '</div>';
    });
    html += '</div>';
  }

  // ── Markt-Sentiment (Put/Call Ratio Gesamtmarkt) ─────────────────────────
  const ms = data.market_sentiment || {};
  if (ms.signal) {
    let pc    = ms.pc_total || 0;
    let pcCol = pc > 1.5 ? '#ff4d6b' : (pc > 1.0 ? '#ffa040' : (pc < 0.7 ? '#4dff91' : '#ffd700'));
    let botTxt = ms.bottom_signal
      ? ' <span style="background:#0a2a0a;color:#4dff91;border:1px solid #2d9e57;padding:1px 7px;border-radius:5px;font-size:10px">⚡ BODEN-SIGNAL</span>'
      : '';
    html += '<div style="margin:8px;background:#080e1a;border:1px solid #1e3a5f;border-radius:10px;padding:10px 14px">'
      + '<div style="font-size:10px;font-weight:bold;color:#4a8ab8;letter-spacing:2px;margin-bottom:6px">📊 MARKT-SENTIMENT — Put/Call Ratio' + botTxt + '</div>'
      + '<div style="display:flex;gap:18px;flex-wrap:wrap;align-items:center">'
      +   '<div style="font-size:22px;font-weight:bold;color:'+pcCol+'">' + pc.toFixed(2) + ' P/C</div>'
      +   '<div style="font-size:14px;font-weight:bold;color:'+pcCol+'">' + ms.emoji + ' ' + ms.signal + '</div>'
      +   '<div style="font-size:11px;color:#4a6a8a">'
      +     'SPY:' + (ms.pc_spy||0).toFixed(2)
      +     ' &nbsp; QQQ:' + (ms.pc_qqq||0).toFixed(2)
      +     ' &nbsp; IWM:' + (ms.pc_iwm||0).toFixed(2)
      +   '</div>'
      +   '<div style="font-size:10px;color:#2a4a6a">'
      +     'Calls:' + ((ms.calls_total||0)/1e6).toFixed(1) + 'M'
      +     ' | Puts:' + ((ms.puts_total||0)/1e6).toFixed(1) + 'M'
      +   '</div>'
      + '</div>'
      + '</div>';
  }

  // ── Geopolitik-Katalysator Banner (ZeroHedge/Reuters/GoogleNews) ──────────
  const geo = data.geo_catalyst || {};
  if (geo.alert && geo.headline) {
    const geoCol  = geo.direction === 'BULL' ? '#00ff88' : geo.direction === 'BEAR' ? '#ff4d6b' : '#ffd700';
    const geoBg   = geo.direction === 'BULL' ? '#051a10' : geo.direction === 'BEAR' ? '#1a0508' : '#1a1505';
    const geoBdr  = geo.direction === 'BULL' ? '#00ff8866' : geo.direction === 'BEAR' ? '#ff224466' : '#ffd70066';
    html += '<div style="margin:8px;background:'+geoBg+';border:2px solid '+geoBdr+';border-radius:10px;padding:10px 14px">'
      + '<div style="font-size:10px;font-weight:bold;color:'+geoCol+';letter-spacing:2px;margin-bottom:4px">'
      + '⚡ GEOPOLITIK KATALYSATOR [' + (geo.source||'') + '] ' + (geo.ts||'') + '</div>'
      + '<div style="font-size:12px;color:#e0e8f0;margin-bottom:4px">' + (geo.headline||'') + '</div>'
      + '<div style="font-size:11px;font-weight:bold;color:'+geoCol+'">' + (geo.alert||'') + '</div>'
      + '</div>';
  }

  // ── Markt-Regime (QQQ Gap, TLT Zinsen, VXX, Sektor-Rotation) ───────────
  const mc = data.market_context || {};
  if (mc.bias && mc.bias !== 'NEUTRAL') {
    let biasCol = mc.bias === 'STRONG_BEAR' ? '#ff2244' :
                  mc.bias === 'BEAR'        ? '#ff6644' :
                  mc.bias === 'STRONG_BULL' ? '#00ff88' :
                  mc.bias === 'BULL'        ? '#44ff99' : '#ffd700';
    let biasEmoji = mc.bias === 'STRONG_BEAR' ? '🔴🔴' :
                    mc.bias === 'BEAR'        ? '🔴' :
                    mc.bias === 'STRONG_BULL' ? '🟢🟢' :
                    mc.bias === 'BULL'        ? '🟢' : '🟡';
    let border = mc.bias.includes('BEAR') ? '#ff224444' : '#00ff8844';
    let bg     = mc.bias.includes('BEAR') ? '#1a0508' : '#051a0a';
    html += '<div style="margin:8px;background:' + bg + ';border:1px solid ' + border + ';border-radius:10px;padding:10px 14px">'
      + '<div style="font-size:10px;font-weight:bold;color:' + biasCol + ';letter-spacing:2px;margin-bottom:6px">'
      + biasEmoji + ' MARKT-REGIME — ' + mc.bias + (mc.signals && mc.signals.length ? ' (' + mc.signals.length + ' Signale)' : '') + '</div>'
      + '<div style="display:flex;gap:14px;flex-wrap:wrap;font-size:11px">'
      + '<span style="color:#94a3b8">QQQ: <b style="color:' + (mc.qqq_chg>=0?'#4dff91':'#ff4d6b') + '">' + (mc.qqq_chg>=0?'+':'') + (mc.qqq_chg||0).toFixed(1) + '%</b></span>'
      + (mc.qqq_gap ? '<span style="color:#94a3b8">Gap: <b style="color:#ff6644">' + (mc.qqq_gap>=0?'+':'') + (mc.qqq_gap||0).toFixed(1) + '%</b></span>' : '')
      + '<span style="color:#94a3b8">VXX: <b style="color:' + (mc.vxx_chg>0?'#ff4d6b':'#4dff91') + '">' + (mc.vxx_chg>=0?'+':'') + (mc.vxx_chg||0).toFixed(1) + '%</b></span>'
      + '<span style="color:#94a3b8">TLT: <b style="color:' + (mc.tlt_chg>=0?'#4dff91':'#ff4d6b') + '">' + (mc.tlt_chg>=0?'+':'') + (mc.tlt_chg||0).toFixed(1) + '%</b></span>'
      + (mc.sector_rotation ? '<span style="color:#ffd700">' + mc.sector_rotation + '</span>' : '')
      + (mc.smh_chg != null ? '<span style="color:#94a3b8">SMH: <b style="color:' + (mc.smh_chg>=0?'#4dff91':'#ff4d6b') + '">' + (mc.smh_chg>=0?'+':'') + (mc.smh_chg||0).toFixed(1) + '%</b></span>' : '')
      + (mc.spcx_chg != null ? '<span style="color:#94a3b8">SPCX: <b style="color:' + (mc.spcx_chg>=0?'#4dff91':'#ff4d6b') + '">' + (mc.spcx_chg>=0?'+':'') + (mc.spcx_chg||0).toFixed(1) + '%</b></span>' : '')
      + '</div>'
      + (mc.sector_rotation === 'SEMI_BOOM' ? '<div style="margin-top:5px;font-size:11px;color:#ffe066;font-weight:bold">SEMI-BOOM — INTC MU AMD WDC AXTI LITE sofort prüfen!</div>' : '')
      + (mc.sector_rotation === 'SPACE_DECLINE' ? '<div style="margin-top:5px;font-size:11px;color:#ff6644;font-weight:bold">SPACE-SELL — RKLB ASTS LUNR SPCX Momentum bricht</div>' : '')
      + (mc.macro_event ? '<div style="margin-top:6px;font-size:11px;color:#ff9944;font-weight:bold">⚡ MAKRO: ' + mc.macro_event + '</div>' : '')
      + (mc.signals && mc.signals.length ? '<div style="margin-top:5px;font-size:10px;color:#6a7a9a">' + mc.signals.slice(0,3).join(' &nbsp;|&nbsp; ') + '</div>' : '')
      + '</div>';
  }

  // ── Hermes AI Analyse (ganz oben wenn vorhanden) ─────────────────────────
  if (data.hermes_ai) {
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0a1f2e,#0d2840);border:1px solid #00e5ff44;border-radius:10px;padding:12px 14px">'
      + '<div style="font-size:10px;font-weight:bold;color:#00e5ff;letter-spacing:2px;margin-bottom:6px">🤖 HERMES AI ANALYSE — ' + (data.hermes_ts||'') + '</div>'
      + '<div style="font-size:13px;color:#c0d4e8;line-height:1.6">' + data.hermes_ai + '</div>'
      + '</div>';
  }

  // ── Hermes Picks — direkt gescannte Karten ───────────────────────────────
  const hermPicks = data.hermes_picks || [];
  if (hermPicks.length > 0) {
    html += '<div class="section"><div class="section-title" style="color:#00e5ff;border-left:3px solid #00e5ff">🤖 HERMES PICKS — Selbst gefunden & gescannt (' + hermPicks.length + ')</div>';
    hermPicks.forEach(r => {
      let cls = r.signal === 'LONG' ? 'long' : 'short';
      let extra = '<div style="font-size:10px;color:#00e5ff;padding:4px 14px 0">Hermes Score: ' + r.hermes_score + ' — ' + (r.hermes_reasons||[]).slice(0,2).join(' | ') + '</div>';
      html += extra + renderCard(r, cls, true);
    });
    html += '</div>';
  }

  // ── Hermes 24h Intelligence — Polygon Gainers/Losers + Vol/OI + Dark Pool ──
  const h24 = data.hermes_24h || [];
  if (h24.length > 0) {
    html += '<div class="section"><div class="section-title" style="color:#ffd700;border-left:3px solid #ffd700">🔍 HERMES 24H INTELLIGENCE — Polygon Gainers/Losers + Smart Money</div>';
    h24.slice(0,8).forEach(s => {
      let chgCol = s.chg >= 0 ? '#4dff91' : '#ff4d6b';
      let sc = s.score || 0;
      let scCol = sc >= 8 ? '#4dff91' : sc >= 6 ? '#ffd700' : '#ffa040';
      let r0 = (s.reasons||[])[0] || '';
      let r1 = (s.reasons||[])[1] || '';
      html += '<div style="padding:8px 14px;border-bottom:1px solid #111f30;display:flex;justify-content:space-between;align-items:center">'
        + '<div>'
        +   '<span style="font-size:15px;font-weight:bold;color:#fff">' + s.sym + '</span>'
        +   ' <span style="font-size:12px;color:#94a3b8">$' + s.price.toFixed(2) + '</span>'
        +   ' <span style="font-size:12px;color:' + chgCol + ';font-weight:bold">' + (s.chg>=0?'+':'') + s.chg + '%</span>'
        +   (s.vol_ratio >= 3 ? ' <span style="font-size:10px;background:#1a1200;border:1px solid #ffd70066;color:#ffd700;padding:1px 5px;border-radius:6px">Vol ' + s.vol_ratio + 'x</span>' : '')
        +   '<div style="font-size:10px;color:#94a3b8;margin-top:2px">' + r0 + (r1 ? ' | ' + r1 : '') + '</div>'
        + '</div>'
        + '<div style="font-size:20px;font-weight:bold;color:' + scCol + ';min-width:32px;text-align:right">' + sc + '</div>'
        + '</div>';
    });
    html += '</div>';
  }

  // ── Market Fall Screener — wenn Markt fällt: zeige sensitivste Aktien ───────
  const fallCands = data.market_fall_candidates || [];
  const fallQqq   = data.market_fall_qqq || 0;
  if (fallCands.length > 0 && fallQqq <= -1.0) {
    let fallColor = fallQqq <= -3 ? '#ff2244' : fallQqq <= -2 ? '#ff4d6b' : '#ff8800';
    html += '<div class="section"><div class="section-title" style="color:' + fallColor + ';border-left:3px solid ' + fallColor + '">📉 MARKT FÄLLT ' + fallQqq.toFixed(1) + '% — SENSITIVSTE AKTIEN (SHORT Kandidaten)</div>';
    html += '<div style="padding:6px 14px;font-size:11px;color:#94a3b8">Diese Aktien fallen überproportional wenn der Markt fällt — ideal für PUT/SHORT Setups</div>';
    fallCands.slice(0,10).forEach(s => {
      let sc   = s.score || 0;
      let scCol = sc >= 8 ? '#ff2244' : sc >= 6 ? '#ff4d6b' : '#ff8800';
      let r0   = (s.reasons||[])[0] || '';
      let r1   = (s.reasons||[])[1] || '';
      let chgStr = (s.chg >= 0 ? '+' : '') + s.chg + '%';
      let chgCol = s.chg < 0 ? '#ff4d6b' : '#94a3b8';
      let volBadge = s.vol_ratio >= 2 ? '<span style="font-size:10px;background:#2a0a0a;border:1px solid #ff4d6b66;color:#ff4d6b;padding:1px 5px;border-radius:6px;margin-left:5px">Vol ' + s.vol_ratio + 'x</span>' : '';
      html += '<div style="padding:8px 14px;border-bottom:1px solid #1a0a0a;display:flex;justify-content:space-between;align-items:center">'
        + '<div>'
        +   '<span style="font-size:15px;font-weight:bold;color:#fff">' + s.sym + '</span>'
        +   ' <span style="font-size:12px;color:#94a3b8">$' + (s.price||0).toFixed(2) + '</span>'
        +   ' <span style="font-size:13px;color:' + chgCol + ';font-weight:bold">' + chgStr + '</span>'
        +   volBadge
        +   '<div style="font-size:10px;color:#94a3b8;margin-top:2px">' + r0 + (r1 ? ' | ' + r1 : '') + '</div>'
        + '</div>'
        + '<div style="font-size:20px;font-weight:bold;color:' + scCol + ';min-width:32px;text-align:right">' + sc + '</div>'
        + '</div>';
    });
    html += '</div>';
  }

  // ── Claude IBKR Mover Analyse — gepusht von Claude ──────────────────────
  const cm = data.claude_movers || {};
  const cmList = (cm.most_traded_usd || []);
  const cs = data.claude_session || {};
  if (cmList.length > 0 || cs.active) {
    const cmTs = cm.updated ? ' <span style="font-size:10px;color:#64748b">@ ' + cm.updated + '</span>' : '';
    const instSig = cm.inst_summary || '';
    html += '<div class="section"><div class="section-title" style="color:#00e5ff;border-left:3px solid #00e5ff">🏦 IBKR CLAUDE MOVER-ANALYSE' + cmTs + '</div>';
    if (cs.active) {
      let nasCol = (cs.nas_chg||0) >= 0 ? '#4dff91' : '#ff4d6b';
      let goldCol = (cs.gold_chg||0) >= 0 ? '#4dff91' : '#ff4d6b';
      html += '<div style="padding:8px 14px;display:flex;gap:16px;flex-wrap:wrap;border-bottom:1px solid #0d1a26">'
        + '<span style="font-size:11px;color:#94a3b8">NAS/QQQ: <b style="color:' + nasCol + '">' + ((cs.nas_chg||0)>=0?'+':'') + (cs.nas_chg||0).toFixed(2) + '%</b> $' + (cs.nas_price||0) + '</span>'
        + '<span style="font-size:11px;color:#94a3b8">Gold: <b style="color:' + goldCol + '">' + ((cs.gold_chg||0)>=0?'+':'') + (cs.gold_chg||0).toFixed(2) + '%</b> $' + (cs.gold_price||0) + '</span>'
        + '<span style="font-size:10px;color:#4a6a8a">Push: ' + (cs.last_push||'–') + '</span>'
        + '</div>';
    }
    if (instSig) {
      html += '<div style="padding:5px 14px;font-size:11px;color:#ffa040;border-bottom:1px solid #0d1a26">⚡ ' + instSig + '</div>';
    }
    // Tabelle: meist gehandelt mit inst_signal Farben
    if (cmList.length > 0) {
      html += '<div style="padding:5px 14px 2px;font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px">MEIST GEHANDELT ($ Vol) — INSTITUTIONELLES SIGNAL</div>';
      cmList.forEach(s => {
        let sig = s.inst_signal || '';
        let isLong  = sig === 'BULLISH' || sig === 'BULLISH_SILVER';
        let isShort = sig.includes('BEARISH') || sig.includes('SHORT') || sig.includes('MAJOR_SHIFT');
        let sigCol  = isLong ? '#4dff91' : (isShort ? '#ff4d6b' : '#ffd700');
        let sigBg   = isLong ? '#0d3a1f' : (isShort ? '#3a0d1a' : '#2a1a00');
        let sigBorder = isLong ? '#2d9e57' : (isShort ? '#9e2d40' : '#9e8000');
        let sigArrow = isLong ? '▲ LONG' : (isShort ? '▼ SHORT' : '— NEUTRAL');
        let chgCol  = (s.chg_pct||0) >= 0 ? '#4dff91' : '#ff4d6b';
        let cpBadge = s.opt_cp_ratio
          ? '<span style="font-size:9px;background:#0a0e1a;border:1px solid #1e3a5f;color:#94a3b8;padding:1px 5px;border-radius:3px;margin-left:5px">C/P ' + s.opt_cp_ratio + '</span>'
          : '';
        html += '<div style="padding:7px 14px;border-bottom:1px solid #0d1a26;display:flex;justify-content:space-between;align-items:center">'
          + '<div>'
          +   '<span style="font-size:14px;font-weight:bold;color:#fff">' + s.symbol + '</span>'
          +   ' <span style="color:' + chgCol + ';font-size:12px;font-weight:bold">' + ((s.chg_pct||0)>=0?'+':'') + (s.chg_pct||0).toFixed(2) + '%</span>'
          +   ' <span style="font-size:11px;color:#64748b">$' + (s.vol_usd_m||0) + 'M</span>'
          +   cpBadge
          + '</div>'
          + '<span style="font-size:11px;font-weight:bold;padding:3px 8px;border-radius:6px;background:' + sigBg + ';color:' + sigCol + ';border:1px solid ' + sigBorder + '">' + sigArrow + '</span>'
          + '</div>';
      });
    }
    html += '</div>';
  }

  // ── IBKR Most Traded — Weltgrößter Broker als Datenquelle ──────────────
  const ibkrData = data.ibkr_data || {};
  const ibkrScan = data.ibkr_scan || [];
  if (ibkrData.connected || ibkrScan.length > 0) {
    const ibkrTs = data.ibkr_ts ? ' <span style="font-size:10px;color:#64748b">@ ' + data.ibkr_ts + '</span>' : '';
    html += '<div class="section"><div class="section-title" style="color:#00e5ff;border-left:3px solid #00e5ff">🏦 IBKR MOST TRADED — Weltgrößter Broker Live-Daten' + ibkrTs + '</div>';
    // Most Active
    const mostActive = (ibkrData.most_active || []).slice(0,6);
    if (mostActive.length > 0) {
      html += '<div style="padding:6px 14px;font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px">📊 Meistgehandelt</div>';
      mostActive.forEach(s => {
        let chgCol = (s.chg||0) >= 0 ? '#4dff91' : '#ff4d6b';
        let volStr = s.vol ? ' | Vol: ' + (s.vol/1e6).toFixed(1) + 'M' : '';
        html += '<div style="padding:5px 14px;border-bottom:1px solid #0d1a26;display:flex;justify-content:space-between">'
          + '<span style="color:#fff;font-weight:bold">' + s.sym + '</span>'
          + '<span style="color:#94a3b8;font-size:11px">'
          + (s.price ? '$' + s.price : '')
          + ' <span style="color:' + chgCol + '">' + ((s.chg||0)>=0?'+':'') + (s.chg||0).toFixed(2) + '%</span>'
          + volStr + '</span>'
          + '</div>';
      });
    }
    // Hot Options
    const hotOpts = (ibkrData.hot_options || []).slice(0,4);
    if (hotOpts.length > 0) {
      html += '<div style="padding:6px 14px;font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px">🔥 Hot Options</div>';
      hotOpts.forEach(s => {
        html += '<div style="padding:5px 14px;border-bottom:1px solid #0d1a26;display:flex;justify-content:space-between">'
          + '<span style="color:#ffa040;font-weight:bold">' + s.sym + '</span>'
          + '<span style="color:#94a3b8;font-size:11px">' + (s.reason || '') + '</span>'
          + '</div>';
      });
    }
    // IBKR Confirmed Signals
    if (ibkrScan.length > 0) {
      html += '<div style="padding:6px 14px;font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px">✅ IBKR + Polygon Bestätigt</div>';
      ibkrScan.slice(0,5).forEach(s => {
        let dirCol = s.direction === 'LONG' ? '#4dff91' : '#ff4d6b';
        let dirArrow = s.direction === 'LONG' ? '▲' : '▼';
        let scCol = (s.score||0) >= 8 ? '#4dff91' : (s.score||0) >= 6 ? '#ffd700' : '#ffa040';
        html += '<div style="padding:8px 14px;border-bottom:1px solid #0d1a26;display:flex;justify-content:space-between;align-items:center">'
          + '<div>'
          +   '<span style="font-weight:bold;color:#fff;font-size:14px">' + s.sym + '</span>'
          +   ' <span style="color:' + dirCol + ';font-weight:bold">' + dirArrow + ' ' + s.direction + '</span>'
          +   ' <span style="font-size:10px;background:#001a33;border:1px solid #00e5ff44;color:#00e5ff;padding:1px 5px;border-radius:4px">IBKR #' + (s.ibkr_rank||'?') + '</span>'
          +   '<div style="font-size:10px;color:#94a3b8;margin-top:2px">' + (s.reason||'') + '</div>'
          + '</div>'
          + '<div style="font-weight:bold;font-size:20px;color:' + scCol + '">' + (s.score||0) + '</div>'
          + '</div>';
      });
    }
    html += '</div>';
  }

  // ── Hauptziel: Mover + Long + Short ─────────────────────────────────────
  if (data.movers && data.movers.length > 0) {
    html += '<div class="section"><div class="section-title mover">🎯 NEXT MOVER — Pre-Move Setup: News + Smart Money + Entscheidung</div>';
    data.movers.forEach(r => { html += renderCard(r, 'mover', isNew); });
    html += '</div>';
  }

  const lbl    = data.label || '';
  const isAH   = lbl.includes('After-Hours');
  const isPM   = lbl.includes('Pre-Market');
  const isMD   = lbl.includes('Mid-Day');
  const badgeTxt = isPM ? '🌅 PRE-MARKET' : (isMD ? '☀️ MID-DAY' : (isAH ? '🌙 AFTER-HOURS' : ''));
  const ahBadge  = badgeTxt
    ? ' <span style="font-size:10px;background:#1a0a2e;color:#a78bfa;padding:2px 7px;border-radius:3px;margin-left:6px">' + badgeTxt + ' INTELLIGENCE</span>'
    : '';

  // Fallback: wenn Scanner leer → Hermes Hunt Alerts als LONG/SHORT nutzen
  const hermAlertsSorted = (data.hermes_alerts||[]).slice().sort((a,b)=>b.score-a.score);
  // LONG: explizit LONG-Richtung UND positiver Trend — NIEMALS SHORT-Signale in LONG zeigen
  let fallbackLongs  = hermAlertsSorted.filter(a =>
    a.net_direction === 'LONG' && (a.prev_chg||0) > 0
  );
  // SHORT: explizit SHORT oder fallende Aktien
  let fallbackShorts = hermAlertsSorted.filter(a =>
    a.net_direction === 'SHORT' || (a.prev_chg||0) < -2
  );
  // Wenn keine Longs: Top-Gainer nehmen
  if (fallbackLongs.length === 0 && hermAlertsSorted.length > 0) {
    const gainers = hermAlertsSorted.filter(a => (a.prev_chg||0) > 1).slice(0,3);
    fallbackLongs = gainers.map(a => Object.assign({}, a, {net_direction:'LONG'}));
  }
  // Wenn keine Shorts: stärkste Verlierer
  if (fallbackShorts.length === 0 && hermAlertsSorted.length > 0) {
    const losers = hermAlertsSorted
      .filter(a => (a.prev_chg||0) < -2)
      .sort((a,b) => (a.prev_chg||0) - (b.prev_chg||0));
    fallbackShorts = losers.slice(0,3).map(a => Object.assign({}, a, {net_direction:'SHORT'}));
  }

  // Hermes Hunt Alert → Scanner-Karten Format konvertieren (dir erzwingen)
  function alertToCard(a, forceSig) {
    return {
      t:        a.ticker,
      score:    a.score,
      price:    a.price,
      prev_chg: a.prev_chg != null ? a.prev_chg : null,
      trend:    a.trend    != null ? a.trend    : null,
      pc:       a.pc       != null ? a.pc       : null,
      drop_high: a.drop_high != null ? a.drop_high : null,
      reasons:  a.reasons || [],
      signal:   forceSig || a.net_direction || 'LONG',
      label:    '🤖 Hermes Hunt',
      best:     null,
    };
  }
  const hermBadge = ' <span style="font-size:10px;background:#0a1f2e;color:#00e5ff;padding:2px 7px;border-radius:3px;margin-left:6px">🤖 HERMES HUNT</span>';

  // IBKR Preise als Map für Preis-Update
  const ibkrPriceMap = {};
  (cm.most_traded_usd || []).forEach(s => { if (s.symbol && s.price) ibkrPriceMap[s.symbol] = s; });
  function enrichWithIbkr(r) {
    const ib = ibkrPriceMap[r.t];
    if (!ib) return r;
    return Object.assign({}, r, {
      price:    ib.price,
      prev_chg: ib.chg_pct,
      ibkr_live: true,
    });
  }

  // Strikt trennen: LONG-Sektion = nur LONG-Signale, SHORT-Sektion = nur SHORT-Signale
  const trueLongsFromSrv  = (data.longs  || []).map(enrichWithIbkr).filter(r => r.signal === 'LONG');
  const trueShortsFromSrv = (data.shorts || []).map(enrichWithIbkr).filter(r => r.signal === 'SHORT');

  const displayLongs  = trueLongsFromSrv.length  > 0
    ? trueLongsFromSrv
    : fallbackLongs.slice(0,5).map(a => enrichWithIbkr(alertToCard(a,'LONG')));
  const displayShorts = trueShortsFromSrv.length > 0
    ? trueShortsFromSrv
    : fallbackShorts.slice(0,5).map(a => enrichWithIbkr(alertToCard(a,'SHORT')));
  const usingFallback = trueLongsFromSrv.length === 0;

  // ── Markt P/C Ratio Banner (SPY + QQQ Aggregat) ──────────────────────────
  (function() {
    const mpData = (data.max_pain || {}).data || [];
    const spy = mpData.find(x => x.sym === 'SPY');
    const qqq = mpData.find(x => x.sym === 'QQQ');
    if (!spy && !qqq) return;
    // Gewichtetes Aggregat: SPY (70%) + QQQ (30%)
    let totalC = 0, totalP = 0;
    if (spy) { totalC += (spy.total_call_oi || 0) * 0.7; totalP += (spy.total_put_oi || 0) * 0.7; }
    if (qqq) { totalC += (qqq.total_call_oi || 0) * 0.3; totalP += (qqq.total_put_oi || 0) * 0.3; }
    const pc = totalC > 0 ? (totalP / totalC).toFixed(2) : null;
    if (!pc) return;
    const sentiment = pc < 0.7 ? {label:'BULLISH', color:'#4dff91', bg:'#001a0a'} :
                      pc > 1.3 ? {label:'BEARISH', color:'#ff2244', bg:'#1a0000'} :
                                 {label:'NEUTRAL', color:'#ffd700', bg:'#1a1400'};
    const spyPc  = spy ? ' SPY P/C: ' + (spy.pc_ratio||'?') : '';
    const qqqPc  = qqq ? ' | QQQ P/C: ' + (qqq.pc_ratio||'?') : '';
    html += '<div style="margin:6px 0;padding:8px 14px;background:' + sentiment.bg + ';border:1px solid ' + sentiment.color + ';border-radius:6px;display:flex;align-items:center;gap:12px;font-size:12px">'
      + '<span style="color:' + sentiment.color + ';font-weight:700;font-size:13px">📊 MARKT P/C: ' + pc + ' — ' + sentiment.label + '</span>'
      + '<span style="color:#4a6a8a">' + spyPc + qqqPc + '</span>'
      + '<span style="color:#4a6a8a;font-size:11px">P/C < 0.7 = BULL &nbsp;|&nbsp; > 1.3 = BEAR</span>'
      + '</div>';
  })();

  // ── 💰 BIG MONEY BETS — Institutionelle OTM Options ──────────────────────
  (function() {
    const bm = data.big_money_bets || {};
    const bets = bm.data || [];
    if (bets.length === 0) return;
    const calls = bets.filter(b => b.ctype === 'CALL').slice(0, 4);
    const puts  = bets.filter(b => b.ctype === 'PUT').slice(0,  2);
    const show  = calls.concat(puts).slice(0, 5);
    if (show.length === 0) return;
    html += '<div class="section" style="border-color:#ffd700">';
    html += '<div class="section-title" style="color:#ffd700;border-left:3px solid #ffd700">💰 BIG MONEY BETS — Institutionelle OTM-Wetten <span style="font-size:10px;color:#4a6a8a">Follow mit 1 Kontrakt = kleines Kapital, großes Potential</span>';
    if (bm.ts) html += ' <span style="font-size:10px;color:#4a6a8a">@ ' + bm.ts + '</span>';
    html += '</div>';
    show.forEach(b => {
      const isCall = b.ctype === 'CALL';
      const col    = isCall ? '#4dff91' : '#ff4d6b';
      const bg     = isCall ? '#001508' : '#150000';
      const ratioCol = b.ratio >= 20 ? '#ff2244' : (b.ratio >= 10 ? '#ff6600' : '#ffd700');
      html += '<div style="padding:10px 14px;border-bottom:1px solid #0a1f30;background:' + bg + '">'
        + '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
        + '<span style="font-size:16px;font-weight:700;color:' + col + '">' + b.sym + '</span>'
        + '<span style="background:#0a1628;color:' + col + ';padding:2px 8px;border-radius:4px;font-size:12px;font-weight:700">'
        +   b.ctype + ' $' + b.strike + '</span>'
        + '<span style="color:#64748b;font-size:11px">Spot: $' + b.spot + '</span>'
        + '<span style="background:' + (b.ratio >= 10 ? '#1a0800' : '#0a1000') + ';color:' + ratioCol + ';padding:2px 7px;border-radius:3px;font-size:11px;font-weight:700">'
        +   b.label + ' x' + b.ratio + '</span>'
        + '<span style="color:#94a3b8;font-size:11px">Exp: ' + b.exp + '</span>'
        + '</div>'
        + '<div style="display:flex;gap:12px;margin-top:6px;flex-wrap:wrap">'
        + '<span style="font-size:12px;color:#4dff91">1 Kontrakt: $' + b['1lot_cost'] + '</span>'
        + '<span style="font-size:12px;color:#94a3b8">Premium: $' + b.prem + '/Kontrakt</span>'
        + '<span style="font-size:12px;color:#ffd700">Vol: ' + b.vol.toLocaleString() + ' | OI: ' + b.oi.toLocaleString() + '</span>'
        + '<span style="font-size:12px;color:#60a5fa">$' + (b.dollar_vol/1000).toFixed(0) + 'k bewegt</span>'
        + '<span style="font-size:11px;color:#ff9944">Braucht ' + (isCall ? '+' : '-') + b.needed_pct + '% Move</span>'
        + '</div>'
        + '</div>';
    });
    html += '</div>';
  })();

  html += '<div class="section"><div class="section-title long">▲ TOP LONG — Options Flow + Katalysator'
    + (usingFallback ? hermBadge : ahBadge) + '</div>';
  if (displayLongs.length === 0) {
    html += '<div style="padding:12px 16px;color:#4a6a8a;font-size:12px">Keine LONG Signale — Markt ist BEAR. Warte auf LONG Katalysator.</div>';
  } else {
    displayLongs.slice(0, 5).forEach(r => { html += renderCard(r, 'long', isNew); });
  }
  html += '</div>';

  html += '<div class="section"><div class="section-title short">▼ TOP SHORT — Überbewertet / Fallend'
    + (usingFallback ? hermBadge : ahBadge) + '</div>';
  if (displayShorts.length === 0) {
    html += '<div class="empty">Morgen ab 13:30 UTC — Markt öffnet.</div>';
  } else {
    displayShorts.slice(0, 5).forEach(r => { html += renderCard(r, 'short', isNew); });
  }
  html += '</div>';

  // PRE-SHORT: HIGH/EXTREME Neg-Katalysator (Dilution, SEC, DOJ)
  if (data.pre_shorts && data.pre_shorts.length > 0) {
    html += '<div class="section"><div class="section-title short" style="background:#2a0000;border-color:#aa0000">⚠️ PRE-SHORT — Starker Neg-Katalysator (Dilution/SEC/DOJ)</div>';
    data.pre_shorts.forEach(r => { html += renderCard(r, 'short', isNew); });
    html += '</div>';
  }

  // Nachrichten
  let allCards = (data.longs || []).concat(data.shorts || []).concat(data.watch || []);
  let newsItems = allCards.filter(r => r.katalysator !== 'KEIN' && r.kat_text);
  if (newsItems.length > 0) {
    html += '<div class="section"><div class="section-title news">NACHRICHTEN — Katalysatoren</div>';
    newsItems.sort((a,b) => {
      const rank = s => s === 'EXTREME' ? 3 : (s === 'HIGH' ? 2 : 1);
      return rank(b.kat_strength||'NORMAL') - rank(a.kat_strength||'NORMAL');
    });
    newsItems.slice(0, 15).forEach(n => {
      let cls   = n.katalysator === 'POSITIV' ? 'news-pos' : 'news-neg';
      let ks    = n.kat_strength || 'NORMAL';
      let label = n.katalysator === 'POSITIV'
        ? (ks === 'EXTREME' ? '🔥 EXTREME' : (ks === 'HIGH' ? '🏛️ HIGH-IMPACT' : '▲ POSITIV'))
        : (ks === 'HIGH' || ks === 'EXTREME' ? '⚠️ HIGH-SHORT' : '▼ NEGATIV');
      let titleHtml = n.kat_url
        ? '<a href="' + n.kat_url + '" target="_blank" rel="noopener" style="color:#60a5fa;text-decoration:underline;text-decoration-color:#1e3a5f">' + n.kat_text + ' <span style="font-size:11px">↗</span></a>'
        : '<span style="color:#c0d4e8">' + n.kat_text + '</span>';
      html += '<div class="news-card">'
        + '<div class="news-ticker">' + n.t + ' &nbsp; ' + pct(n.trend) + '</div>'
        + '<div class="news-title">'  + titleHtml + '</div>'
        + '<span class="news-kat ' + cls + '">' + label + '</span>'
        + '</div>';
    });
    html += '</div>';
  }

  // ── HERMES AGENT ALERTS (direkt nach Nachrichten) ──────────────────────────
  const hermNews = data.hermes_news || [];
  const hermAlerts = data.hermes_alerts || [];
  if (hermAlerts.length > 0 || hermNews.length > 0) {
    let ts = data.hermes_ts ? ' ' + data.hermes_ts : '';
    html += '<div class="section"><div class="section-title" style="color:#00e5ff;border-left:3px solid #00e5ff">🤖 HERMES 24/7' + ts + ' — ' + hermAlerts.length + ' Mover' + (hermNews.length ? ' | ' + hermNews.length + ' News' : '') + '</div>';
    // Breaking News zuerst
    if (hermNews.length > 0) {
      html += '<div style="padding:8px 14px;border-bottom:1px solid #0a1f30">';
      hermNews.forEach(n => {
        html += '<div style="font-size:11px;color:#60a5fa;padding:2px 0">📰 ' + n + '</div>';
      });
      html += '</div>';
    }
    data.hermes_alerts.forEach(a => {
      let sc = a.score >= 8 ? '#4dff91' : a.score >= 6 ? '#ffd700' : '#ffa040';
      let dp = a.dp && a.dp.dp_total ? ' 🏦$' + (a.dp.dp_total/1e6).toFixed(1) + 'M' : '';
      let px = a.price > 0 ? '$' + a.price : '';
      html += '<div style="padding:10px 14px;border-bottom:1px solid #0a1f30;display:flex;gap:10px;align-items:flex-start">'
        + '<div style="min-width:48px;text-align:center">'
        +   '<div style="font-size:20px;font-weight:bold;color:' + sc + '">' + a.score + '</div>'
        +   '<div style="font-size:9px;color:#4a6a8a">SCORE</div>'
        + '</div>'
        + '<div style="flex:1">'
        +   '<div style="font-size:15px;font-weight:bold;color:#fff">' + a.ticker + ' <span style="color:#6b8cad;font-size:12px">' + px + '</span>' + dp + '</div>';
      // UOA Badge
      if (a.uoa_ratio && a.uoa_ratio >= 2) {
        const uoaCol = a.uoa_ratio >= 10 ? '#ff2244' : (a.uoa_ratio >= 5 ? '#ff6600' : '#ffd700');
        const uoaBg  = a.uoa_ratio >= 10 ? '#2a0008' : (a.uoa_ratio >= 5 ? '#1a0f00' : '#1a1500');
        const uoaLbl = a.uoa_ratio >= 10 ? '🔥 EXTREME' : (a.uoa_ratio >= 5 ? '⚡ UNUSUAL' : '📊 Erhöht');
        html += '<div style="display:inline-block;margin-top:4px;padding:3px 8px;background:' + uoaBg + ';border:1px solid ' + uoaCol + ';border-radius:4px;font-size:11px;font-weight:700;color:' + uoaCol + '">'
          + uoaLbl + ' ' + a.uoa_dir + ' OPTIONS x' + a.uoa_ratio + '</div>';
      }
      (a.reasons||[]).forEach(r => { html += '<div style="font-size:11px;color:#94a3b8;margin-top:2px">• ' + r + '</div>'; });
      // Social Trending Info
      if (a.social) {
        let sentCol = a.social.sentiment==='BULLISH' ? '#4dff91' : (a.social.sentiment==='BEARISH' ? '#ff4d6b' : '#94a3b8');
        let whyStr  = (a.social.why||[]).join(' · ');
        let srcStr  = (a.social.sources||[]).join(', ');
        html += '<div style="margin-top:5px;padding:5px 8px;background:#0a1628;border-left:2px solid #2e6da4;border-radius:3px">'
          + '<div style="font-size:10px;color:#60a5fa">📱 Social: <b style="color:' + sentCol + '">' + a.social.sentiment + '</b>'
          + (whyStr ? ' · <span style="color:#ffd700">' + whyStr + '</span>' : '')
          + ' <span style="color:#4a6a8a">(' + srcStr + ')</span></div>';
        if (a.social.top_post) {
          html += '<div style="font-size:10px;color:#64748b;margin-top:2px;font-style:italic">"' + a.social.top_post + '"</div>';
        }
        html += '</div>';
      }
      html += '</div></div>';
    });
    html += '</div>';
  }

  // ── Social Trending + Smart Money Analyse ────────────────────────────────
  const socialDeep = data.social_deep || [];
  if (socialDeep.length > 0) {
    const longPicks  = socialDeep.filter(a => a.verdict === 'LONG');
    const shortPicks = socialDeep.filter(a => a.verdict === 'SHORT');
    const neutPicks  = socialDeep.filter(a => a.verdict === 'NEUTRAL');

    html += '<div class="section"><div class="section-title" style="color:#a78bfa;border-left:3px solid #a78bfa">'
      + '📱 SOCIAL TRENDING + SMART MONEY'
      + (longPicks.length  ? ' &nbsp;<span style="color:#4dff91;font-size:11px">● ' + longPicks.length + ' LONG</span>' : '')
      + (shortPicks.length ? ' &nbsp;<span style="color:#ff4d6b;font-size:11px">● ' + shortPicks.length + ' SHORT</span>' : '')
      + '</div>';

    function renderSocialCard(a) {
      const isLong  = a.verdict === 'LONG';
      const isShort = a.verdict === 'SHORT';
      const vCol  = isLong ? '#4dff91' : (isShort ? '#ff4d6b' : '#94a3b8');
      const vIcon = isLong ? '🟢 LONG' : (isShort ? '🔴 SHORT' : '⚪ NEUTRAL');
      const vBg   = isLong ? '#0a1f0f' : (isShort ? '#1f0a0a' : '#0d1628');

      const why      = (a.why||[]).slice(0,2).join(' · ');
      const retSent  = a.ret_sent || a.sentiment || '';
      const retCol   = retSent==='BULLISH' ? '#4dff91' : (retSent==='BEARISH' ? '#ff4d6b' : '#94a3b8');
      const dpDir    = a.dp_dir || 'NEUTRAL';
      const dpCol    = dpDir==='BUY' ? '#4dff91' : (dpDir==='SELL' ? '#ff4d6b' : '#94a3b8');
      const dpM      = a.dp_dollar ? '$' + (a.dp_dollar/1e6).toFixed(1) + 'M' : '';
      const cpStr    = a.call_prem > 0 ? '$' + a.call_prem + 'M' : '—';
      const ppStr    = a.put_prem  > 0 ? '$' + a.put_prem  + 'M' : '—';
      const pc       = a.pc_ratio ? a.pc_ratio.toFixed(2) : '—';
      const divWarn  = a.divergence ? '<span style="color:#ffd700;font-size:10px"> ⚠ ' + a.divergence + '</span>' : '';
      const chgCol   = a.prev_chg > 0 ? '#4dff91' : (a.prev_chg < 0 ? '#ff4d6b' : '#94a3b8');
      const chgStr   = a.prev_chg ? (a.prev_chg > 0 ? '+' : '') + a.prev_chg.toFixed(1) + '%' : '';

      let card = '<div style="padding:12px 14px;border-bottom:1px solid #0a1f30;background:' + vBg + '">';

      // Header: Symbol + Verdict + Preis
      card += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'
        + '<div style="display:flex;align-items:center;gap:8px">'
        +   '<span style="font-size:17px;font-weight:bold;color:#fff">' + a.sym + '</span>'
        +   (a.price ? '<span style="font-size:12px;color:#6b8cad">$' + a.price.toFixed(2) + '</span>' : '')
        +   (chgStr ? '<span style="font-size:11px;color:' + chgCol + '">' + chgStr + '</span>' : '')
        +   divWarn
        + '</div>'
        + '<div style="font-size:13px;font-weight:bold;color:' + vCol + ';background:rgba(0,0,0,0.3);padding:3px 10px;border-radius:4px">' + vIcon + '</div>'
        + '</div>';

      // WHY + Retail Sentiment
      card += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
        + '<span style="font-size:10px;color:#ffd700;background:#1a1200;padding:2px 7px;border-radius:3px">' + (why||'SOCIAL') + '</span>'
        + '<span style="font-size:10px;color:' + retCol + ';background:#0d1220;padding:2px 7px;border-radius:3px">Reddit/ST: ' + (retSent||'—') + '</span>'
        + '</div>';

      // Smart Money Grid
      card += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px;margin-bottom:6px">';
      card += smBox('Dark Pool', dpDir, dpCol, dpM);
      card += smBox('Calls', cpStr, '#4dff91', (a.call_sweeps ? a.call_sweeps + ' Sweeps' : ''));
      card += smBox('Puts', ppStr, '#ff4d6b', (a.put_sweeps ? a.put_sweeps + ' Sweeps' : ''));
      card += smBox('P/C Ratio', pc, pc > 1.2 ? '#ff4d6b' : (pc < 0.6 ? '#4dff91' : '#94a3b8'), a.call_voi >= 4 ? 'CallVOI ' + a.call_voi + 'x' : '');
      card += '</div>';

      // Verdict Begründung
      if (a.verdict_reason) {
        card += '<div style="font-size:10px;color:#64748b;padding:4px 0">→ ' + a.verdict_reason + '</div>';
      }
      // Top Post
      if (a.top_post) {
        card += '<div style="font-size:10px;color:#475569;font-style:italic;margin-top:3px">"' + a.top_post.slice(0,90) + '"</div>';
      }
      card += '</div>';
      return card;
    }

    function smBox(label, val, col, sub) {
      return '<div style="background:#0a1420;border-radius:4px;padding:5px 6px;text-align:center">'
        + '<div style="font-size:9px;color:#4a6a8a;margin-bottom:2px">' + label + '</div>'
        + '<div style="font-size:12px;font-weight:bold;color:' + col + '">' + val + '</div>'
        + (sub ? '<div style="font-size:9px;color:#4a6a8a">' + sub + '</div>' : '')
        + '</div>';
    }

    // LONGs zuerst
    longPicks.forEach(a => { html += renderSocialCard(a); });
    shortPicks.forEach(a => { html += renderSocialCard(a); });
    // Neutral kompakt
    if (neutPicks.length > 0) {
      html += '<div style="padding:8px 14px;color:#4a6a8a;font-size:11px">⚪ Neutral: '
        + neutPicks.map(a => a.sym).join(', ') + '</div>';
    }
    html += '</div>';
  }

  // ── Signal Report (10:00 ET + 22:00 CET) — $100 Investment Ergebnis ────────
  if (data.followup && (data.followup.longs || data.followup.shorts)) {
    let allFu = (data.followup.longs || []).concat(data.followup.shorts || []);
    let totalProfit = allFu.reduce((s, f) => s + (f.profit || 0), 0);
    let totalInvest = allFu.reduce((s, f) => s + (f.invest || 100), 0);
    let winners = allFu.filter(f => f.won).length;
    let pfCol = totalProfit >= 0 ? '#4dff91' : '#ff4d6b';
    let pfSign = totalProfit >= 0 ? '+' : '';
    let fuTime = (data.followup.time || '');
    html += '<div class="section"><div class="section-title" style="color:#ffd700;border-left:3px solid #ffd700">📊 SIGNAL REPORT ' + fuTime + ' — Gewinner & Verlierer</div>';
    // Zusammenfassung
    html += '<div style="padding:10px 14px;background:#0d1628;display:flex;justify-content:space-between;align-items:center">'
      + '<div style="font-size:13px;color:#94a3b8">'
      +   winners + '/' + allFu.length + ' Ziele erreicht &nbsp;|&nbsp; '
      +   '$' + totalInvest + ' investiert'
      + '</div>'
      + '<div style="font-size:18px;font-weight:bold;color:' + pfCol + '">'
      +   pfSign + '$' + totalProfit + ' (' + pfSign + Math.round(totalProfit/totalInvest*100) + '%)'
      + '</div>'
      + '</div>';
    html += '<div style="background:#111827;border-top:1px solid #2a2000;overflow:hidden">';
    allFu.forEach(f => {
      let won   = f.won;
      let col   = won ? '#4dff91' : '#ff4d6b';
      let icon  = won ? '✅' : '❌';
      let prof  = f.profit || 0;
      let tot   = f.total  || 100;
      let psign = prof >= 0 ? '+' : '';
      html += '<div style="padding:8px 14px;border-bottom:1px solid #1a2a3a;display:flex;justify-content:space-between;align-items:center">'
        + '<div>'
        +   '<span style="font-weight:bold;color:#fff;font-size:14px">' + icon + ' ' + f.t + '</span>'
        +   ' <span style="font-size:11px;color:#94a3b8">' + f.signal + ' Entry:$' + f.entry + ' → $' + f.current + '</span>'
        + '</div>'
        + '<div style="text-align:right">'
        +   '<div style="color:' + col + ';font-weight:bold;font-size:14px">' + psign + '$' + prof + '</div>'
        +   '<div style="font-size:10px;color:#475569">$100 → $' + tot + '</div>'
        + '</div>'
        + '</div>';
    });
    html += '</div></div>';
  }

  // ── WATCH ────────────────────────────────────────────────────────────────
  if (data.watch && data.watch.length > 0) {
    html += '<div class="section"><div class="section-title">WATCH (' + data.watch.length + ')</div>';
    html += '<div style="background:#111827;border:1px solid #1e3a5f;margin:8px;border-radius:10px;overflow:hidden">';
    data.watch.forEach(r => {
      html += '<div class="watch-row">'
        + '<span style="font-weight:bold;color:#a0b4c8">' + r.t + '</span>'
        + '<span style="color:#6b8cad;font-size:12px">$' + r.price + ' &nbsp; ' + pct(r.trend) + ' &nbsp; L:' + r.long_score + '/S:' + r.short_score + '</span>'
        + '</div>';
    });
    html += '</div></div>';
  }

  return html;
}

function renderTab2(data) {
  if (!data) return '<div class="empty" style="padding:20px;text-align:center;color:#475569">Warte auf ersten Scan…</div>';
  let html = '';

  // ── Hermes Brain — autonomer KI-Agent Status ──────────────────────────────
  const bwv = data.brain_worldview || {};
  const bNews = data.brain_news || [];
  const bTrades = data.brain_trades || [];
  const focusColors = {
    'LONG_HUNT':'#4dff91','LONG_LEAN':'#86efac','BALANCED':'#94a3b8',
    'SHORT_LEAN':'#fca5a5','SHORT_HUNT':'#ff4d6b'
  };
  const focusIcons = {
    'LONG_HUNT':'🟢🟢','LONG_LEAN':'🟢','BALANCED':'⚪','SHORT_LEAN':'🔴','SHORT_HUNT':'🔴🔴'
  };
  if (bwv.focus) {
    let fc = focusColors[bwv.focus] || '#94a3b8';
    let fi = focusIcons[bwv.focus] || '⚪';
    let totalBrainPL = bTrades.reduce((s,t) => s + (t.pnl_pct || 0), 0);
    let brainWins    = bTrades.filter(t => t.won === true).length;
    let brainLoss    = bTrades.filter(t => t.won === false).length;
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0a0a1a,#0d0d28);border:1px solid #6366f144;border-radius:10px;padding:12px 14px">'
      + '<div style="font-size:10px;font-weight:bold;color:#a78bfa;letter-spacing:2px;margin-bottom:8px">🧠 HERMES BRAIN — AUTONOMER KI-AGENT</div>'
      + '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px">'
      +   '<div><div style="font-size:10px;color:#6b8cad">Strategie</div>'
      +     '<div style="font-size:15px;font-weight:bold;color:' + fc + '">' + fi + ' ' + (bwv.focus||'?') + '</div></div>'
      +   '<div><div style="font-size:10px;color:#6b8cad">Markt</div>'
      +     '<div style="font-size:14px;font-weight:bold;color:#cbd5e1">' + (bwv.market||'?') + ' QQQ ' + (bwv.qqq >= 0 ? '+' : '') + (bwv.qqq||0).toFixed(1) + '%</div></div>'
      +   '<div><div style="font-size:10px;color:#6b8cad">Brain Trades</div>'
      +     '<div style="font-size:14px;font-weight:bold;color:#fff">✅' + brainWins + ' ❌' + brainLoss + '</div></div>'
      +   '<div><div style="font-size:10px;color:#6b8cad">Letzte News</div>'
      +     '<div style="font-size:12px;color:#94a3b8">' + (bwv.updated||'—') + '</div></div>'
      + '</div>';

    // Letzte Brain-News Klassifizierungen
    if (bNews.length > 0) {
      html += '<div style="font-size:10px;color:#6b8cad;margin-bottom:5px;text-transform:uppercase;letter-spacing:1px">Zuletzt klassifizierte News:</div>';
      bNews.slice(0,5).forEach(n => {
        let ac = n.action === 'LONG' ? '#4dff91' : '#ff4d6b';
        let ai_badge = n.ai ? ' <span style="font-size:9px;background:#1a0a2e;color:#a78bfa;padding:1px 4px;border-radius:3px">KI</span>' : '';
        html += '<div style="padding:4px 0;border-top:1px solid #1a1a2e;display:flex;gap:8px;align-items:flex-start">'
          + '<span style="font-size:11px;font-weight:bold;color:' + ac + ';min-width:45px">' + n.action + '</span>'
          + '<div><div style="font-size:11px;color:#fff">' + (n.headline||'').slice(0,70) + ai_badge + '</div>'
          +   '<div style="font-size:10px;color:#64748b">' + (n.tickers||[]).join(', ') + ' | ' + n.reason + ' | ' + n.time + '</div></div>'
          + '</div>';
      });
    }

    // Brain Auto-Trades P&L
    if (bTrades.length > 0) {
      let openTrades  = bTrades.filter(t => t.won === null || t.won === undefined);
      let closedTrades = bTrades.filter(t => t.won === true || t.won === false);
      let totalPnlPct  = openTrades.reduce((s,t) => s + (t.pnl_pct||0), 0);
      let totalPnlUSD  = bTrades.reduce((s,t) => s + (t.pnl_dollar||0), 0);
      let wins = closedTrades.filter(t => t.won).length;
      let losses = closedTrades.filter(t => !t.won).length;
      let totalCol = totalPnlUSD >= 0 ? '#4dff91' : '#ff4d6b';
      html += '<div style="font-size:10px;color:#6b8cad;margin-top:8px;margin-bottom:5px;text-transform:uppercase;letter-spacing:1px">'
        + 'Hermes Auto-Trades — '
        + '<span style="color:' + totalCol + ';font-weight:bold">'
        + (totalPnlUSD >= 0 ? '+' : '') + '$' + totalPnlUSD.toFixed(0) + ' P&L'
        + '</span>'
        + ' | ✅' + wins + ' ❌' + losses + ' ⏳' + openTrades.length
        + '</div>';
      bTrades.slice(0,10).forEach(t => {
        let wonIcon = t.trail_triggered ? '🔒' : (t.won === true ? '✅' : t.won === false ? '❌' : '⏳');
        let sc  = t.signal === 'LONG' ? '#4dff91' : '#ff4d6b';
        let pnlPct = t.pnl_pct || 0;
        let pnlUSD = t.pnl_dollar || 0;
        let pnlCol = pnlPct >= 0 ? '#4dff91' : '#ff4d6b';
        let pnlStr = pnlPct !== 0
          ? (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(1) + '% (' + (pnlUSD >= 0 ? '+' : '') + '$' + pnlUSD.toFixed(0) + ')'
          : 'offen';
        let trailBadge = t.trail_on
          ? '<span style="font-size:9px;background:#1a1200;color:#f59e0b;padding:1px 4px;border-radius:3px;margin-left:4px">🔒 TRAIL ' + (t.trail_peak||0).toFixed(1) + '%</span>'
          : '';
        let typeBadge = t.type === 'STOCK'
          ? '<span style="font-size:9px;background:#0d2a1a;color:#4dff91;padding:1px 4px;border-radius:3px;margin-left:4px">STK</span>'
          : '<span style="font-size:9px;background:#0a0a2a;color:#a78bfa;padding:1px 4px;border-radius:3px;margin-left:4px">OPT</span>';
        html += '<div style="padding:4px 0;border-top:1px solid #1a1a2e;display:flex;justify-content:space-between;align-items:center">'
          + '<div>'
          +   '<span style="color:' + sc + ';font-weight:bold">' + wonIcon + ' ' + t.signal + ' ' + t.sym + '</span>'
          +   typeBadge + trailBadge
          +   ' <span style="font-size:10px;color:#64748b">Score:' + t.score + ' ' + Math.round((t.conviction||0)*100) + '%</span>'
          +   (t.qty ? ' <span style="font-size:10px;color:#64748b">×' + t.qty + '</span>' : '')
          +   '<div style="font-size:10px;color:#64748b">' + (t.reason||'').slice(0,50) + ' | ' + (t.time||'') + '</div>'
          + '</div>'
          + '<div style="text-align:right">'
          +   '<span style="color:' + pnlCol + ';font-size:12px;font-weight:bold">' + pnlStr + '</span>'
          + '</div>'
          + '</div>';
      });
    }
    html += '</div>';
  }

  // ── Kanal-Pattern Scanner ────────────────────────────────────────────────────
  const chActive  = data.channel_active  || [];
  const chSignals = data.channel_signals || [];
  const chWatch   = data.channel_watch   || ['NVDA','AMD','INTC','META','AAPL','TSLA','MSFT','AMZN','GOOGL','MU'];
  {
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0a1218,#0d1a24);border:1px solid #f59e0b44;border-radius:10px;padding:12px 14px">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
      + '<span style="font-size:10px;font-weight:bold;color:#f59e0b;letter-spacing:2px">📊 KANAL-MUSTER — PING-PONG AKTIEN</span>'
      + '<span style="font-size:9px;color:#64748b">' + chActive.length + ' aktiv | ' + chSignals.length + ' Signale</span>'
      + '</div>'
      // Watch-Liste immer anzeigen
      + '<div style="margin-bottom:8px;display:flex;flex-wrap:wrap;gap:4px">'
      + chWatch.map(s => {
          let active = chActive.find(a => a.sym === s);
          let col = active ? (active.price >= active.resistance - (active.resistance-active.support)*0.25 ? '#ff4d6b' : active.price <= active.support + (active.resistance-active.support)*0.25 ? '#4dff91' : '#f59e0b') : '#334155';
          let border = active ? col : '#334155';
          return '<span style="font-size:10px;font-weight:bold;padding:2px 7px;border-radius:4px;background:#0d1520;border:1px solid ' + border + ';color:' + col + '">'
            + s + (active ? ' ↔' : '') + '</span>';
        }).join('')
      + '</div>';

    // Aktive Kanäle (Aktien die gerade im Muster sind)
    if (chActive.length > 0) {
      html += '<div style="font-size:10px;color:#6b8cad;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Aktive Kanäle (' + chActive.length + ')</div>';
      chActive.forEach(c => {
        let pricePct = Math.max(0, Math.min(100, ((c.price - c.support) / (c.resistance - c.support)) * 100));
        let atTop    = pricePct >= 75;
        let atBot    = pricePct <= 25;
        let posColor = atTop ? '#ff4d6b' : atBot ? '#4dff91' : '#f59e0b';
        html += '<div style="padding:6px 0;border-top:1px solid #1a2030">'
          // Symbol + Range
          + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
          +   '<span style="font-size:13px;font-weight:bold;color:#fff">' + c.sym + '</span>'
          +   '<span style="font-size:11px;color:#94a3b8">Range ' + c.range_pct + '% | ↑' + c.touches_top + 'x ↓' + c.touches_bot + 'x</span>'
          + '</div>'
          // Preisbalken: Support ←→ Resistance
          + '<div style="position:relative;height:18px;background:#0d1520;border-radius:4px;margin-bottom:3px;overflow:visible">'
          +   '<div style="position:absolute;left:0;top:0;height:100%;width:' + pricePct + '%;background:linear-gradient(90deg,#1e3a5f,' + posColor + '33);border-radius:4px"></div>'
          +   '<div style="position:absolute;left:' + pricePct + '%;top:-3px;transform:translateX(-50%);width:3px;height:24px;background:' + posColor + ';border-radius:2px"></div>'
          +   '<div style="position:absolute;left:2px;top:2px;font-size:9px;color:#4dff91;font-weight:bold">SUP $' + c.support + '</div>'
          +   '<div style="position:absolute;right:2px;top:2px;font-size:9px;color:#ff4d6b;font-weight:bold">RES $' + c.resistance + '</div>'
          + '</div>'
          // Preis + Status
          + '<div style="display:flex;justify-content:space-between">'
          +   '<span style="font-size:10px;color:#64748b">' + c.updated + '</span>'
          +   '<span style="font-size:11px;font-weight:bold;color:' + posColor + '">$' + c.price + (atTop ? ' → SHORT?' : atBot ? ' → LONG?' : ' ↔ Mitte') + '</span>'
          + '</div>'
          + '</div>';
      });
    }

    // Letzte Signale
    if (chSignals.length > 0) {
      html += '<div style="font-size:10px;color:#6b8cad;margin-top:10px;margin-bottom:5px;text-transform:uppercase;letter-spacing:1px">Letzte Signale</div>';
      chSignals.slice(0, 8).forEach(s => {
        let sc      = s.signal === 'LONG' ? '#4dff91' : '#ff4d6b';
        let icon    = s.signal === 'LONG' ? '🟢' : '🔴';
        let pricePct = Math.max(0, Math.min(100, ((s.price - s.support) / (s.resistance - s.support)) * 100));
        // Mini-Balken mit Entry, TP, SL markiert
        let tpPct   = Math.max(0, Math.min(100, ((s.tp - s.support) / (s.resistance - s.support)) * 100));
        let slPct   = Math.max(0, Math.min(100, ((s.sl - s.support) / (s.resistance - s.support)) * 100));
        html += '<div style="padding:6px 0;border-top:1px solid #1a2030">'
          + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'
          +   '<span style="font-size:12px;font-weight:bold;color:' + sc + '">' + icon + ' ' + s.signal + ' <span style="color:#fff">' + s.sym + '</span></span>'
          +   '<span style="font-size:10px;color:#64748b">' + s.time + ' | R/R ' + s.rr + ':1</span>'
          + '</div>'
          // Visueller Kanal mit Entry/TP/SL
          + '<div style="position:relative;height:16px;background:#0d1520;border-radius:4px;margin-bottom:3px">'
          // TP Marker
          +   '<div style="position:absolute;left:' + tpPct + '%;top:0;width:2px;height:100%;background:#4dff91;opacity:0.7"></div>'
          // Entry Marker (Preis)
          +   '<div style="position:absolute;left:' + pricePct + '%;top:-2px;transform:translateX(-50%);width:3px;height:20px;background:' + sc + ';border-radius:1px"></div>'
          // SL Marker
          +   '<div style="position:absolute;left:' + slPct + '%;top:0;width:2px;height:100%;background:#ff4d6b;opacity:0.7"></div>'
          +   '<div style="position:absolute;left:2px;top:1px;font-size:8px;color:#4dff91">$' + s.support + '</div>'
          +   '<div style="position:absolute;right:2px;top:1px;font-size:8px;color:#ff4d6b">$' + s.resistance + '</div>'
          + '</div>'
          + '<div style="display:flex;gap:12px;font-size:9px;color:#64748b">'
          +   '<span>Entry <b style="color:#fff">$' + s.price + '</b></span>'
          +   '<span>TP <b style="color:#4dff91">$' + s.tp + '</b></span>'
          +   '<span>SL <b style="color:#ff4d6b">$' + s.sl + '</b></span>'
          +   '<span>↑' + s.touches_top + 'x ↓' + s.touches_bot + 'x</span>'
          + '</div>'
          + '</div>';
      });
    }

    // Leer-Zustand (Markt zu oder noch keine Muster gefunden)
    if (chActive.length === 0 && chSignals.length === 0) {
      html += '<div style="text-align:center;padding:10px;color:#475569;font-size:11px">'
        + '⏳ Scanner läuft — Muster werden während Marktzeiten erkannt (00:00–22:00 UTC)'
        + '</div>';
    }

    html += '</div>';
  }

  // ── VWAP Reclaim Signale ─────────────────────────────────────────────────────
  const vwapSigs = data.vwap_signals || [];
  const gapSigs  = data.gap_signals  || [];
  const blackout = data.trading_blackout || false;

  if (blackout) {
    html += '<div style="margin:8px;background:#1a0000;border:2px solid #ff4d6b;border-radius:8px;padding:8px 14px;text-align:center">'
      + '<span style="color:#ff4d6b;font-weight:bold;font-size:12px">🚫 TRADING BLACKOUT — Makro-Event aktiv (Fed/CPI/NFP) — kein Auto-Trade</span>'
      + '</div>';
  }

  // ── VWAP Reclaim Block (immer sichtbar) ──────────────────────────────────────
  {
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0d1020,#121830);border:1px solid #60a5fa44;border-radius:10px;padding:12px 14px">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
      + '<span style="font-size:10px;font-weight:bold;color:#60a5fa;letter-spacing:2px">⚡ VWAP RECLAIM</span>'
      + '<span style="font-size:9px;color:#64748b">' + vwapSigs.length + ' Signale heute</span>'
      + '</div>';
    if (vwapSigs.length > 0) {
      vwapSigs.slice(0, 6).forEach(s => {
        let sc  = s.signal === 'LONG' ? '#4dff91' : '#ff4d6b';
        let ico = s.signal === 'LONG' ? '▲' : '▼';
        html += '<div style="display:flex;justify-content:space-between;padding:4px 0;border-top:1px solid #1a2040">'
          + '<div><span style="color:' + sc + ';font-weight:bold">' + ico + ' ' + s.signal + ' <span style="color:#fff">' + s.sym + '</span></span>'
          + '<div style="font-size:10px;color:#64748b">$' + s.price + ' vs VWAP $' + s.vwap + ' | Vol ×' + s.vol_ratio + ' | ' + s.time + '</div></div>'
          + '<span style="font-size:11px;color:' + sc + ';align-self:center">' + (s.signal==='LONG'?'Reclaim ▲':'Break ▼') + '</span>'
          + '</div>';
      });
    } else {
      html += '<div style="color:#475569;font-size:11px;text-align:center;padding:6px">'
        + '⏳ Aktiv ab 00:00 UTC — kreuzt Aktie VWAP mit Vol ×1.4 → Signal</div>';
    }
    html += '</div>';
  }

  // ── Pre-Market Gap Block (immer sichtbar) ────────────────────────────────────
  {
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0d1020,#121830);border:1px solid #a78bfa44;border-radius:10px;padding:12px 14px">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
      + '<span style="font-size:10px;font-weight:bold;color:#a78bfa;letter-spacing:2px">🌅 PRE-MARKET GAPS</span>'
      + '<span style="font-size:9px;color:#64748b">' + gapSigs.length + ' Gaps heute | >2% Gap = Signal</span>'
      + '</div>';
    if (gapSigs.length > 0) {
      gapSigs.slice(0, 5).forEach(g => {
        let gc   = g.gap_pct >= 0 ? '#4dff91' : '#ff4d6b';
        let ico  = g.gap_pct >= 0 ? '📈' : '📉';
        let fade = Math.abs(g.gap_pct) < 4;
        html += '<div style="display:flex;justify-content:space-between;padding:4px 0;border-top:1px solid #1a2040">'
          + '<div><span style="font-size:12px;font-weight:bold;color:#fff">' + ico + ' ' + g.sym + '</span>'
          + ' <span style="color:' + gc + ';font-weight:bold">' + (g.gap_pct>=0?'+':'') + g.gap_pct + '%</span>'
          + '<div style="font-size:10px;color:#64748b">$' + g.prev_close + ' → $' + g.pre_price + ' | ' + g.time
          + (fade ? ' | <span style="color:#f59e0b">Fade</span>' : ' | <span style="color:#4dff91">Continuation</span>') + '</div></div>'
          + '<span style="font-size:10px;color:#94a3b8;align-self:center">' + g.signal + '</span>'
          + '</div>';
      });
    } else {
      html += '<div style="color:#475569;font-size:11px;text-align:center;padding:6px">'
        + '⏳ Aktiv 08:00–13:30 UTC — Aktien mit Gap >2% vor US-Open</div>';
    }
    html += '</div>';
  }

  // ── Alpaca Portfolio ─────────────────────────────────────────────────────────
  const ap = data.alpaca_portfolio || {};
  if (ap.equity) {
    let totalPL = (ap.positions||[]).reduce((s,p) => s + p.pl, 0);
    let plCol = totalPL >= 0 ? '#4dff91' : '#ff4d6b';
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0a1a0a,#0d2010);border:1px solid #2d9e5744;border-radius:10px;padding:12px 14px">'
      + '<div style="font-size:10px;font-weight:bold;color:#4dff91;letter-spacing:2px;margin-bottom:8px">📈 ALPACA PAPER PORTFOLIO — ' + (ap.ts||'') + '</div>'
      + '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">'
      +   '<div><div style="font-size:11px;color:#6b8cad">Portfolio</div><div style="font-size:18px;font-weight:bold;color:#fff">$' + ap.equity.toLocaleString() + '</div></div>'
      +   '<div><div style="font-size:11px;color:#6b8cad">Cash</div><div style="font-size:16px;font-weight:bold;color:#94a3b8">$' + ap.cash.toLocaleString() + '</div></div>'
      +   '<div><div style="font-size:11px;color:#6b8cad">P&L offen</div><div style="font-size:16px;font-weight:bold;color:' + plCol + '">' + (totalPL>=0?'+':'') + '$' + totalPL.toFixed(0) + '</div></div>'
      + '</div>';
    (ap.positions||[]).sort((a,b) => b.pl - a.pl).forEach(p => {
      let pc    = p.pl_pct >= 0 ? '#4dff91' : '#ff4d6b';
      let dir   = p.direction || 'BULLISH';
      let isOpt = p.is_option;
      let optType = p.opt_type || '';
      // Farbe: CALL=grün, PUT=rot, Aktie=blau
      let dirColor  = dir === 'BULLISH' ? '#4dff91' : '#ff4d6b';
      let typeLabel = isOpt
        ? '<span style="font-size:10px;font-weight:bold;padding:1px 5px;border-radius:4px;background:' + (optType==='CALL'?'#0a2a0a':'#2a0a0a') + ';color:' + (optType==='CALL'?'#4dff91':'#ff4d6b') + ';border:1px solid ' + (optType==='CALL'?'#4dff9166':'#ff4d6b66') + '">' + optType + '</span> '
        : '<span style="font-size:10px;color:#60a5fa">STOCK</span> ';
      let dirBadge = '<span style="font-size:9px;color:' + dirColor + '">▲ ' + dir + '</span>';
      if (dir === 'BEARISH') dirBadge = '<span style="font-size:9px;color:' + dirColor + '">▼ ' + dir + '</span>';
      html += '<div style="display:flex;justify-content:space-between;padding:5px 0;border-top:1px solid #1a2a1a;align-items:center">'
        + '<div>'
        +   '<span style="font-size:13px;font-weight:bold;color:#fff">' + (isOpt ? p.sym.slice(0, p.sym.length-15) : p.sym) + '</span> '
        +   typeLabel + dirBadge + '<br>'
        +   '<span style="font-size:9px;color:#6b8cad">' + p.side + ' | ' + p.qty + 'x @ $' + p.entry + '</span>'
        + '</div>'
        + '<div style="text-align:right">'
        +   '<span style="color:#fff;font-size:13px">$' + p.price + '</span><br>'
        +   '<span style="color:' + pc + ';font-size:12px;font-weight:bold">' + (p.pl_pct>=0?'+':'') + p.pl_pct + '% ($' + p.pl.toFixed(0) + ')</span>'
        + '</div></div>';
    });
    html += '</div>';
  }

  // ── MT5 Bot Monitor ─────────────────────────────────────────────────────────
  const mt5 = data.mt5_status || {};
  if (mt5.balance !== undefined) {
    let mt5Pl    = mt5.equity - mt5.balance;
    let mt5PlCol = mt5Pl >= 0 ? '#4dff91' : '#ff4d6b';
    let mt5PlPct = mt5.balance > 0 ? (mt5Pl / mt5.balance * 100).toFixed(2) : '0';
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0a1428,#0d1840);border:1px solid #2060aa44;border-radius:10px;padding:12px 14px">'
      + '<div style="font-size:10px;font-weight:bold;color:#60a5fa;letter-spacing:2px;margin-bottom:8px">📊 MT5 DEMO BOT — ' + (mt5.received_at||'') + '</div>'
      + '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">'
      +   '<div><div style="font-size:11px;color:#6b8cad">Balance</div><div style="font-size:18px;font-weight:bold;color:#fff">$' + (mt5.balance||0).toFixed(2) + '</div></div>'
      +   '<div><div style="font-size:11px;color:#6b8cad">Equity</div><div style="font-size:16px;font-weight:bold;color:#94a3b8">$' + (mt5.equity||0).toFixed(2) + '</div></div>'
      +   '<div><div style="font-size:11px;color:#6b8cad">P&L</div><div style="font-size:16px;font-weight:bold;color:' + mt5PlCol + '">' + (mt5Pl>=0?'+':'') + '$' + mt5Pl.toFixed(2) + ' (' + (mt5Pl>=0?'+':'') + mt5PlPct + '%)</div></div>'
      + '</div>';
    // Offene Positionen
    let positions = mt5.positions || [];
    if (positions.length > 0) {
      html += '<div style="font-size:10px;color:#6b8cad;margin-bottom:4px">OFFENE POSITIONEN:</div>';
      positions.forEach(p => {
        let plCol = (p.profit||0) >= 0 ? '#4dff91' : '#ff4d6b';
        let dir   = p.type === 0 ? 'BUY' : 'SELL';
        let dirCol= p.type === 0 ? '#4dff91' : '#ff4d6b';
        html += '<div style="display:flex;justify-content:space-between;padding:4px 0;border-top:1px solid #1a2a3a">'
          + '<div><span style="font-size:13px;font-weight:bold;color:#fff">' + (p.symbol||'') + '</span> '
          +   '<span style="font-size:10px;color:' + dirCol + '">' + dir + '</span><br>'
          +   '<span style="font-size:9px;color:#6b8cad">' + (p.volume||0) + ' Lots @ ' + (p.price_open||0).toFixed(2) + '</span></div>'
          + '<div style="text-align:right"><span style="color:#fff;font-size:12px">' + (p.price_current||0).toFixed(2) + '</span><br>'
          +   '<span style="color:' + plCol + ';font-weight:bold">' + ((p.profit||0)>=0?'+':'') + '$' + (p.profit||0).toFixed(2) + '</span></div>'
          + '</div>';
      });
    }
    // Letzter Trade
    if (mt5.last_trade) {
      let lt = mt5.last_trade;
      html += '<div style="margin-top:6px;padding:4px 8px;background:#0a0a20;border-radius:6px;font-size:10px;color:#6b8cad">'
        + 'Letzter Trade: <span style="color:#fff">' + (lt.symbol||'') + ' ' + (lt.type||'') + ' ' + (lt.volume||'') + ' Lots</span>'
        + ' → <span style="color:' + ((lt.profit||0)>=0?'#4dff91':'#ff4d6b') + '">' + ((lt.profit||0)>=0?'+':'') + '$' + (lt.profit||0).toFixed(2) + '</span>'
        + ' <span style="color:#4a6a8a">(' + (lt.time||'') + ')</span></div>';
    }
    // Bot Status
    let botSt   = mt5.bot_status || 'unbekannt';
    let botCol  = botSt === 'running' ? '#4dff91' : '#ff4d6b';
    html += '<div style="margin-top:6px;font-size:10px">'
      + 'Bot: <span style="color:' + botCol + ';font-weight:bold">' + botSt.toUpperCase() + '</span>'
      + ' | Symbole: <span style="color:#94a3b8">' + (mt5.symbols||['XAUUSD','NAS100']).join(', ') + '</span>'
      + ' | Signal: <span style="color:#ffa040">' + (mt5.last_signal||'–') + '</span></div>';
    html += '</div>';
  } else {
    html += '<div style="margin:8px;background:#0a1428;border:1px solid #2060aa22;border-radius:10px;padding:10px 14px;font-size:11px;color:#4a6a8a">📊 MT5 Bot — Kein Signal empfangen (läuft der Bot?)</div>';
  }

  // ── Sektor Rotation Monitor ─────────────────────────────────────────────────
  const sr = data.sector_rotation || {};
  if (sr.data && Object.keys(sr.data).length > 0) {
    let srItems = Object.entries(sr.data).sort((a,b) => b[1].avg - a[1].avg);
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0a0a1e,#0d0d28);border:1px solid #3040aa44;border-radius:10px;padding:12px 14px">';
    html += '<div style="font-size:10px;font-weight:bold;color:#8080ff;letter-spacing:2px;margin-bottom:8px">🔄 SEKTOR ROTATION — ' + (sr.ts||'') + '</div>';
    srItems.forEach(([sektor, v]) => {
      let avg = v.avg || 0;
      let col = avg >= 2 ? '#4dff91' : (avg >= 0.5 ? '#a0c0ff' : (avg >= -1 ? '#6b8cad' : '#ff4d6b'));
      let bar = avg >= 0 ? '▲'.repeat(Math.min(Math.floor(avg*2),8)) : '▼'.repeat(Math.min(Math.floor(Math.abs(avg)*2),8));
      let label = avg >= 2 ? ' ← ROTATION ZIEL' : (avg <= -3 ? ' ← AUSVERKAUF' : '');
      let allTickers = (v.tickers||[]).sort((a,b)=>b.chg-a.chg);
      let tickerChips = allTickers.map(t => {
        let tc = t.chg >= 3 ? '#4dff91' : (t.chg >= 1 ? '#a0d070' : (t.chg >= 0 ? '#5a7a5a' : (t.chg >= -2 ? '#aa6644' : '#ff4d6b')));
        let optStr = '';
        if (t.call_oi || t.put_oi) {
          let pc = t.pc || 0;
          let sentC = pc < 0.7 ? '#4dff91' : (pc > 1.3 ? '#ff4d6b' : '#ffd700');
          let cStr = t.call_oi >= 1000 ? (t.call_oi/1000).toFixed(0)+'k' : (t.call_oi||0);
          let pStr = t.put_oi  >= 1000 ? (t.put_oi /1000).toFixed(0)+'k' : (t.put_oi ||0);
          optStr = '<span style="color:#4dff91;font-size:8px"> C:'+cStr+'</span>'
                 + '<span style="color:#ff6677;font-size:8px"> P:'+pStr+'</span>'
                 + '<span style="color:'+sentC+';font-size:8px"> '+pc.toFixed(1)+'</span>';
        }
        return '<span style="background:#070d18;border:1px solid ' + tc + '55;border-radius:4px;padding:1px 5px;font-size:9px;color:' + tc + ';white-space:nowrap">'
          + t.sym + ' <b>' + (t.chg>=0?'+':'') + t.chg + '%</b>' + optStr + '</span>';
      }).join(' ');
      html += '<div style="padding:5px 0;border-top:1px solid #1a1a30">'
        + '<div style="display:flex;justify-content:space-between;align-items:center">'
        + '<div style="font-size:11px;color:' + col + '">' + bar + ' <b>' + sektor + '</b>'
        + (label ? '<span style="font-size:9px;color:#ffd700"> ' + label + '</span>' : '') + '</div>'
        + '<div style="font-size:12px;font-weight:bold;color:' + col + '">' + (avg>=0?'+':'') + avg + '%</div>'
        + '</div>'
        + '<div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:4px">' + tickerChips + '</div>'
        + '</div>';
    });
    html += '</div>';
  }

  // ── MAX PAIN — OpEx Pin-Zonen ───────────────────────────────────────────────
  const mp = data.max_pain || {};
  if (mp.data && mp.data.length > 0) {
    html += '<div style="margin:8px;background:linear-gradient(135deg,#0d0a1e,#120a28);border:1px solid #6040bb44;border-radius:10px;padding:12px 14px">';
    html += '<div style="font-size:10px;font-weight:bold;color:#b060ff;letter-spacing:2px;margin-bottom:8px">⚡ MAX PAIN — OPEX PIN-ZONEN ' + (mp.ts||'') + '</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px">';
    mp.data.forEach(t => {
      const stCol   = t.status === 'AT_PIN'     ? '#4dff91' :
                      t.status === 'ABOVE_PAIN' ? '#ff9944' : '#4db8ff';
      const stLabel = t.status === 'AT_PIN'     ? '📌 AM PIN' :
                      t.status === 'ABOVE_PAIN' ? '⬆ ÜBER PAIN' : '⬇ UNTER PAIN';
      const sentCol = t.sentiment === 'BULLISH' ? '#4dff91' :
                      t.sentiment === 'BEARISH' ? '#ff4d6b' : '#ffd700';
      const diffStr = (t.diff >= 0 ? '+' : '') + (t.diff||0).toFixed(2)
                    + ' (' + (t.diff_pct >= 0 ? '+' : '') + (t.diff_pct||0).toFixed(1) + '%)';
      html += '<div style="background:#0a0818;border:1px solid ' + stCol + '33;border-radius:8px;padding:8px 10px">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
        +   '<span style="font-size:13px;font-weight:bold;color:#e0d0ff">' + t.sym + '</span>'
        +   '<span style="font-size:9px;color:' + stCol + ';font-weight:bold">' + stLabel + '</span>'
        + '</div>'
        + '<div style="font-size:12px;color:#c0a0ff">Kurs: <b>$' + (t.price||0).toFixed(2) + '</b>'
        +   ' <span style="color:' + stCol + ';font-size:10px">' + diffStr + '</span></div>'
        + '<div style="font-size:11px;margin-top:3px">'
        +   '<span style="color:#b060ff">Max Pain: </span><b style="color:#ffd700">$' + (t.max_pain||0).toFixed(2) + '</b>'
        + '</div>'
        + '<div style="display:flex;gap:8px;margin-top:4px;font-size:10px">'
        +   '<span style="color:#4dff91">🛡 Floor: $' + (t.put_floor||0).toFixed(0) + '</span>'
        +   '<span style="color:#ff4d6b">🧱 Wall: $' + (t.call_wall||0).toFixed(0) + '</span>'
        + '</div>'
        + '<div style="display:flex;gap:8px;margin-top:3px;font-size:10px">'
        +   '<span style="color:' + sentCol + '">P/C: ' + (t.pc_ratio||0).toFixed(2) + ' ' + t.sentiment + '</span>'
        +   '<span style="color:#4a5a8a">Pin: $' + (t.pin_zone||[0,0])[0].toFixed(0) + '–$' + (t.pin_zone||[0,0])[1].toFixed(0) + '</span>'
        + '</div>'
        + '</div>';
    });
    html += '</div></div>';
  }

  // ── Hermes Learning — Selbst-Optimierung ────────────────────────────────────
  const lrn = data.hermes_learning || {};
  const lw  = lrn.weights || {};
  const lp  = lrn.performance || {};
  const log = (lrn.improvement_log || []).slice(0,3);
  const misses = (lrn.missed_trades || []).slice(0,5);
  if (lw.vol_ratio_threshold || log.length > 0) {
    html += '<div class="section"><div class="section-title" style="color:#ffa040;border-left:3px solid #ffa040">🧠 HERMES LEARNING — Selbst-Optimierung</div>';
    html += '<div style="padding:8px 14px">';
    // Aktuelle Gewichtung
    html += '<div style="font-size:10px;color:#6b8cad;margin-bottom:6px">AKTUELLE PARAMETER:</div>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">';
    if (lw.vol_ratio_threshold) html += '<span style="font-size:10px;background:#1a1200;border:1px solid #ffa04066;color:#ffa040;padding:2px 7px;border-radius:8px">Vol-Schwelle: ' + lw.vol_ratio_threshold + 'x</span>';
    if (lw.earnings_bonus)      html += '<span style="font-size:10px;background:#1a1200;border:1px solid #ffa04066;color:#ffa040;padding:2px 7px;border-radius:8px">Earnings-Bonus: +' + lw.earnings_bonus + '</span>';
    if (lw.small_cap_boost > 0) html += '<span style="font-size:10px;background:#1a1200;border:1px solid #ffa04066;color:#ffa040;padding:2px 7px;border-radius:8px">SmallCap-Boost: +' + lw.small_cap_boost + '</span>';
    if (lp.win_rate)            html += '<span style="font-size:10px;background:#0a2a0a;border:1px solid #4dff9166;color:#4dff91;padding:2px 7px;border-radius:8px">Win-Rate: ' + lp.win_rate + '%</span>';
    html += '</div>';
    // Letzte Lernschritte
    if (log.length > 0) {
      html += '<div style="font-size:10px;color:#6b8cad;margin-bottom:4px">LETZTE VERBESSERUNGEN:</div>';
      log.forEach(l => {
        html += '<div style="font-size:10px;color:#c0d4e8;padding:3px 0;border-bottom:1px solid #1a2a3a">'
          + '<span style="color:#ffa040">' + l.date + '</span> Win:' + l.win_rate + '% | '
          + (l.changes||[]).slice(0,2).map(c => '→ ' + c).join(' | ')
          + '</div>';
      });
    }
    // Verpasste Trades
    if (misses.length > 0) {
      html += '<div style="font-size:10px;color:#6b8cad;margin:6px 0 4px">VERPASST (lernt daraus):</div>';
      html += '<div style="display:flex;flex-wrap:wrap;gap:4px">';
      misses.forEach(m => {
        let col = m.chg > 0 ? '#4dff91' : '#ff4d6b';
        html += '<span style="font-size:10px;background:#0a0e1a;border:1px solid #1e3a5f;color:' + col + ';padding:2px 6px;border-radius:6px">'
          + m.sym + ' ' + (m.chg > 0 ? '+' : '') + m.chg + '%'
          + '</span>';
      });
      html += '</div>';
    }
    html += '</div></div>';
  }

  // ── Hermes Memory — Signal Tracking + P&L ───────────────────────────────────
  const mem = data.hermes_memory || {};
  const memSigs = Object.values(mem.signals || {}).filter(s => s.status === 'open').slice(0,8);
  if (memSigs.length > 0) {
    html += '<div class="section"><div class="section-title" style="color:#00e5ff;border-left:3px solid #00e5ff">🧠 HERMES MEMORY — Signal Tracking</div>';
    memSigs.forEach(s => {
      let pl = s.current_pl_pct || 0;
      let plCol = pl >= 0 ? '#4dff91' : '#ff4d6b';
      let sigCol = s.signal === 'LONG' ? '#4dff91' : '#ff4d6b';
      html += '<div style="padding:7px 14px;border-bottom:1px solid #111f30;display:flex;justify-content:space-between;align-items:center">'
        + '<div>'
        +   '<span style="font-size:14px;font-weight:bold;color:#fff">' + s.sym + '</span>'
        +   ' <span style="font-size:10px;color:' + sigCol + '">' + s.signal + '</span>'
        +   '<div style="font-size:10px;color:#475569;margin-top:2px">Entry: $' + s.entry_price + ' — ' + (s.entry_time||'').slice(0,16) + '</div>'
        + '</div>'
        + '<div style="text-align:right">'
        +   (s.current_price ? '<div style="color:#94a3b8;font-size:12px">$' + s.current_price + '</div>' : '')
        +   '<div style="color:' + plCol + ';font-weight:bold;font-size:13px">' + (pl>=0?'+':'') + pl + '%</div>'
        +   (s.peak_pl_pct ? '<div style="color:#4a6a8a;font-size:10px">Peak: +' + s.peak_pl_pct + '%</div>' : '')
        + '</div></div>';
    });
    html += '</div>';
  }

  // ── Reddit / Social Trending — KI Score + Heute % + Trend-Grund ────────────
  const socialData = data.social_data || [];
  html += '<div class="section"><div class="section-title" style="color:#b070ff;border-left:3px solid #b070ff">🔥 REDDIT / STOCKTWITS TRENDING</div>';
  if (socialData.length === 0) {
    html += '<div style="padding:12px 16px;color:#4a6a8a;font-size:12px">Wird nach dem Scan geladen... (30-60s)</div>';
  } else {
    socialData.forEach(s => {
      let ki = s.ki_score || 0;
      let kiCol = ki >= 70 ? '#4dff91' : ki >= 50 ? '#ffd700' : ki >= 30 ? '#ffa040' : '#6b8cad';
      let todayC = s.today_chg || 0;
      let weekC  = s.trend_7d  || 0;
      let tCol = c => c >= 0 ? '#4dff91' : '#ff4d6b';
      let sigBadge = s.signal && s.signal !== '─'
        ? ' <span style="font-size:10px;font-weight:bold;padding:2px 5px;border-radius:6px;background:' + (s.signal==='LONG'?'#0d3a1f':'#3a0d1a') + ';color:' + (s.signal==='LONG'?'#4dff91':'#ff4d6b') + '">' + s.signal + '</span>' : '';
      let reason = s.reason || '';
      let reasonHtml = reason
        ? '<div style="font-size:11px;color:#60a5fa;margin-top:4px;padding-left:2px">📰 ' + reason + '</div>'
        : '<div style="font-size:11px;color:#475569;margin-top:3px">📈 Stocktwits — ' + (s.mentions||0).toLocaleString() + ' mentions</div>';
      let bestHtml = '';
      if (s.best && s.signal && s.signal !== '─') {
        let oCol = s.signal === 'LONG' ? '#4dff91' : '#ff4d6b';
        let oType = s.signal === 'LONG' ? 'CALL' : 'PUT';
        bestHtml = '<div style="background:#0d1628;border:1px solid #1e3a5f;border-radius:6px;padding:4px 8px;margin-top:5px;font-size:11px">'
          + '<span style="color:' + oCol + ';font-weight:bold">' + oType + ' $' + s.best.strike + '</span>'
          + ' @ <b>$' + s.best.pr + '</b>  Exp: ' + (s.best.exp||'') + '</div>';
      }
      html += '<div style="padding:9px 14px;border-bottom:1px solid #111f30">'
        + '<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        +   '<div>'
        +     '<span style="font-size:16px;font-weight:bold;color:#fff">' + s.sym + '</span>'
        +     sigBadge
        +     (s.price > 0 ? ' <span style="color:#94a3b8;font-size:13px">$' + s.price.toFixed(2) + '</span>' : '')
        +     '<div style="display:flex;gap:10px;margin-top:3px">'
        +       '<span style="font-size:12px;color:' + tCol(todayC) + '">' + (todayC >= 0?'+':'') + todayC.toFixed(1) + '% heute</span>'
        +       '<span style="font-size:12px;color:' + tCol(weekC)  + '">' + (weekC  >= 0?'+':'') + weekC.toFixed(1)  + '% (7T)</span>'
        +     '</div>'
        +   '</div>'
        +   '<div style="text-align:center;min-width:44px">'
        +     '<div style="font-size:20px;font-weight:bold;color:' + kiCol + '">' + ki + '</div>'
        +     '<div style="font-size:9px;color:#4a6a8a;letter-spacing:1px">KI</div>'
        +   '</div>'
        + '</div>'
        + reasonHtml
        + bestHtml
        + '</div>';
    });
  }
  html += '</div>';

  // ── Hedge Fund 13F — Holdings + Kursentwicklung seit Kauf ───────────────────
  const hfData = data.hf_data || [];
  html += '<div class="section"><div class="section-title" style="color:#a78bfa;border-left:3px solid #a78bfa">🏛 HEDGE FUNDS — Positionen (13F Q1 2026)</div>';
  if (hfData.length === 0) {
    html += '<div style="padding:12px 16px;color:#4a6a8a;font-size:12px">Wird nach Scan geladen...</div>';
  } else {
    hfData.forEach(hf => {
      html += '<div style="border-bottom:1px solid #111f30">'
        + '<div style="padding:8px 14px;background:#0d1628;display:flex;justify-content:space-between;align-items:center">'
        +   '<span style="font-size:13px;font-weight:700;color:#a78bfa">' + hf.manager + '</span>'
        +   '<span style="font-size:10px;color:#475569">13F &bull; ' + hf.date
        +   (hf.url ? ' &nbsp;<a href="' + hf.url + '" target="_blank" style="color:#a78bfa">SEC↗</a>' : '') + '</span>'
        + '</div>';
      const holdings = hf.holdings || [];
      if (holdings.length > 0) {
        html += '<div style="padding:4px 14px 8px">';
        holdings.forEach(h => {
          let sc = h.since_pct || 0;
          let isPut = (h.action||'').includes('PUT');
          let isRed = h.action === 'REDUZIERT' || isPut;
          let scCol = isPut ? (sc <= 0 ? '#4dff91' : '#ff4d6b') : (sc >= 0 ? '#4dff91' : '#ff4d6b');
          let actCol = isRed ? '#ff4d6b' : h.action === 'GEHALTEN' ? '#94a3b8' : '#4dff91';
          let actBg  = isRed ? '#2a0a0a' : h.action === 'GEHALTEN' ? '#1a2a3a' : '#0a2a1a';
          let pStr   = h.price_then > 0 ? ' $' + h.price_then + ' → $' + h.price_now : (h.price_now > 0 ? ' $' + h.price_now : '');
          let scStr  = sc !== 0 ? '<span style="color:' + scCol + ';font-weight:bold">' + (sc>=0?'+':'') + sc + '%</span>' : '';
          let reasonStr = h.reason ? '<div style="font-size:10px;color:#475569;margin-top:1px">' + h.reason + '</div>' : '';
          html += '<div style="padding:5px 0;border-bottom:1px solid #0d1a28;display:flex;justify-content:space-between;align-items:center">'
            +   '<div>'
            +     '<span style="font-size:14px;font-weight:bold;color:' + (isPut?'#ff4d6b':'#e2e8f0') + '">' + h.sym + '</span>'
            +     ' <span style="font-size:10px;color:' + actCol + ';background:' + actBg + ';padding:1px 6px;border-radius:8px">' + h.action + '</span>'
            +     '<div style="font-size:11px;color:#64748b;margin-top:1px">' + pStr + '</div>'
            +     reasonStr
            +   '</div>'
            +   '<div style="text-align:right">'
            +     '<div style="font-size:12px;color:#94a3b8">$' + h.val_m + 'M</div>'
            +     '<div style="font-size:11px">' + scStr + '</div>'
            +   '</div>'
            + '</div>';
        });
        html += '</div>';
      }
      html += '</div>';
    });
  }
  html += '</div>';

  // ── Leopold Aschenbrenner & Influencer ──────────────────────────────────────
  if (data.influencers && data.influencers.length > 0) {
    html += '<div class="section"><div class="section-title" style="color:#ffa040;border-left:3px solid #ffa040">🧠 LEOPOLD ASCHENBRENNER & Analysten</div>';
    data.influencers.forEach(inf => {
      let tBadges = inf.tickers.map(t => {
        let inScan = (data.longs || []).concat(data.shorts || []).find(r => r.t === t);
        let col = inScan ? (inScan.signal === 'LONG' ? '#4dff91' : '#ff4d6b') : '#ffa040';
        return '<span style="background:#1a1200;border:1px solid #a06000;color:' + col + ';padding:3px 8px;border-radius:10px;font-size:11px;font-weight:bold">' + t + (inScan ? ' ' + inScan.signal : '') + '</span>';
      }).join(' ');
      let titleHtml = inf.url
        ? '<a href="' + inf.url + '" target="_blank" style="color:#c0d4e8;text-decoration:none">' + inf.title + ' <span style="color:#ffa040;font-size:10px">↗</span></a>'
        : inf.title;
      html += '<div class="news-card" style="border-color:#2a1a00">'
        + '<div class="news-ticker" style="color:#ffa040">' + inf.author + '</div>'
        + '<div class="news-title">' + titleHtml + '</div>'
        + '<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">' + tBadges + '</div>'
        + '</div>';
    });
    html += '</div>';
  }

  return html;
}

// Tab-Steuerung
let _lastData = null;
function showTab(n) {
  document.getElementById('tab1').style.display = n === 1 ? 'block' : 'none';
  document.getElementById('tab2').style.display = n === 2 ? 'block' : 'none';
  document.getElementById('tab3').style.display = n === 3 ? 'block' : 'none';
  document.getElementById('tab1Btn').classList.toggle('active', n === 1);
  document.getElementById('tab2Btn').classList.toggle('active', n === 2);
  document.getElementById('tab3Btn').classList.toggle('active', n === 3);
  if (n === 2 && _lastData) {
    document.getElementById('intel-content').innerHTML = renderTab2(_lastData) || '<div class="empty">Wird nach Scan geladen...</div>';
  }
}

// ── FIBONACCI SCANNER ────────────────────────────────────────────────────────
function loadFibScan() {
  document.getElementById('fib-status').innerHTML = 'Scanne alle Instrumente... (15-30s)';
  document.getElementById('fib-content').innerHTML = '<div class="empty" style="padding:30px">Berechne Fibonacci-Levels...</div>';
  fetch('/fib-scan')
    .then(r => r.json())
    .then(d => {
      let tradedMsg = d.traded && d.traded.length > 0
        ? ' &nbsp;|&nbsp; <span style="color:#4dff91;font-weight:bold">🤖 AUTO-TRADE: ' + d.traded.join(', ') + '</span>'
        : '';
      document.getElementById('fib-status').innerHTML =
        d.count + ' Instrumente &nbsp;|&nbsp; ' + (d.time||'') + tradedMsg;
      document.getElementById('fib-content').innerHTML = renderFibResults(d.results || [], _lastData);
    })
    .catch(e => {
      document.getElementById('fib-status').innerHTML = 'Fehler: ' + e;
    });
}

const FIB_COLORS = {'88.2':'badge-882','78.6':'badge-786','61.8':'badge-618',
                    '50.0':'badge-500','38.2':'badge-382','23.6':'badge-236'};
const FIB_NAMES  = {'88.2':'88.2%','78.6':'78.6%','61.8':'61.8% (Golden)',
                    '50.0':'50.0%','38.2':'38.2%','23.6':'23.6%'};
const SIG_LABELS = {
  'AT_HIGH':   ['AT HIGH 📉',  '#ff2244'],
  'NEAR_HIGH': ['NEAR HIGH',   '#ff6677'],
  'AT_882':    ['AT 88.2% 📈', '#ff6b35'],
  'AT_786':    ['AT 78.6%',    '#ff9500'],
  'AT_618':    ['AT 61.8%',    '#ffd700'],
  'NEAR_882':  ['NEAR 88.2%',  '#ff9966'],
  'NEAR_KEY':  ['KEY LEVEL',   '#4dffaa'],
  'NEAR_382':  ['NEAR 38.2%',  '#4db8ff'],
  'MONITORING':['MONITORING',  '#4a6a8a'],
};

function renderFibResults(results, latestData) {
  if (!results.length) return '<div class="empty">Keine Ergebnisse</div>';

  // Fib-Trades aus letztem Scan ermitteln
  const fibTrades = (latestData && latestData.fib_trades) ? latestData.fib_trades : [];
  const fibTraded = new Set(fibTrades.map(t => t.sym));

  // Gruppen nach Trade-Richtung
  const shortHot  = results.filter(r => r.signal === 'AT_HIGH');
  const shortWarm = results.filter(r => r.signal === 'NEAR_HIGH');
  const longHot   = results.filter(r => ['AT_882','NEAR_882'].includes(r.signal));
  const longWarm  = results.filter(r => ['AT_786','AT_618'].includes(r.signal));
  const neutral   = results.filter(r => ['NEAR_KEY','NEAR_382'].includes(r.signal));
  const others    = results.filter(r => r.signal === 'MONITORING');

  let html = '';

  // Legende
  html += '<div style="margin:8px 8px 4px;display:flex;gap:10px;flex-wrap:wrap;font-size:10px">'
    + '<span style="color:#ff2244">📉 ROT = SHORT (nahe Hoch)</span>'
    + '<span style="color:#4dff91">📈 GRÜN = LONG (nahe Tief/0.882)</span>'
    + '<span style="color:#ffd700">🟡 GELB = NEUTRAL (Mid-Range)</span>'
    + '</div>';

  // Fib-Trades Übersicht falls vorhanden
  if (fibTrades.length > 0) {
    html += '<div style="margin:8px;background:#0a1a0a;border:1px solid #2d9e5744;border-radius:8px;padding:10px 12px">';
    html += '<div style="font-size:10px;font-weight:bold;color:#4dff91;letter-spacing:2px;margin-bottom:6px">🤖 FIB AUTO-TRADES HEUTE (' + fibTrades.length + ')</div>';
    fibTrades.slice(0,6).forEach(t => {
      let sc = t.signal === 'LONG' ? '#4dff91' : '#ff4d6b';
      html += '<div style="display:flex;justify-content:space-between;padding:3px 0;border-top:1px solid #1a2a1a">'
        + '<span style="color:' + sc + ';font-weight:bold">' + t.signal + ' ' + t.sym + '</span>'
        + '<span style="color:#4a6a8a;font-size:10px">' + (t.reason||'') + ' | ' + (t.time||'') + '</span>'
        + '</div>';
    });
    html += '</div>';
  }

  function cardGroup(title, items, color, bg) {
    if (!items.length) return '';
    let h = '<div class="fib-section-title" style="color:' + color + ';background:' + (bg||'transparent') + ';padding:4px 8px;border-radius:4px;margin:8px 0 2px">' + title + ' (' + items.length + ')</div>';
    items.forEach(r => { h += fibCard(r, fibTraded.has(r.sym)); });
    return h;
  }

  html += cardGroup('📉 SHORT KANDIDATEN — Nahe Swing-High', shortHot,  '#ff2244', '#1a020a');
  html += cardGroup('⚠️ SHORT WATCHLIST — Approaching High',  shortWarm, '#ff6677', '#150208');
  html += cardGroup('📈 LONG KANDIDATEN — 88.2% / 78.6%',   longHot,   '#4dff91', '#021a08');
  html += cardGroup('🟡 LONG WATCHLIST — Key Level',         longWarm,  '#ffd700', '#141200');
  html += cardGroup('⚪ NEUTRAL — Mid Range',                 neutral,   '#94a3b8', '#0a0a0a');
  html += cardGroup('🔍 MONITORING',                          others,    '#4a6a8a', 'transparent');

  return html;
}

function fibCard(r, isTraded) {
  const sig      = r.signal || 'MONITORING';
  const sigInfo  = SIG_LABELS[sig] || ['?','#4a6a8a'];
  const chgCls   = r.chg_pct >= 0 ? 'pct-pos' : 'pct-neg';
  const chgStr   = (r.chg_pct >= 0 ? '+' : '') + (r.chg_pct||0).toFixed(2) + '%';
  const cardCls  = r.at_level ? 'at-level' : (r.near_882 ? 'near-882' : (r.near_key ? 'near-key' : ''));
  const nearFib  = r.nearest_fib || '—';
  const nearBadge = FIB_COLORS[nearFib] || 'badge-236';
  const nearName  = FIB_NAMES[nearFib]  || nearFib;
  const dist      = r.nearest_dist != null ? r.nearest_dist.toFixed(2) : '?';
  const distCol   = r.nearest_dist < 0.5 ? '#4dff88' : (r.nearest_dist < 1.5 ? '#ffd700' : '#4a6a8a');
  const volStr    = r.vol_ratio != null ? (r.vol_ratio > 1.5 ? ' VOL x' + r.vol_ratio : '') : '';
  const bounce    = r.bounce    ? ' BOUNCE' : '';
  const reject    = r.rejection ? ' REJECTION' : '';

  // Level-Bar: zeige Preis relativ zu Swing-High/Low
  const rng     = (r.swing_high - r.swing_low) || 1;
  const fillPct = Math.max(0, Math.min(100, (r.price - r.swing_low) / rng * 100));

  // Marker-Positionen fuer die Fib-Levels
  let markers = '';
  if (r.levels) {
    Object.entries(r.levels).forEach(([name, lv]) => {
      const mp = Math.max(0, Math.min(100, (lv.nearest - r.swing_low) / rng * 100));
      const mc = FIB_COLORS[name] ? (name === '88.2' ? '#ff6b35' : name === '61.8' ? '#ffd700' : '#aaa') : '#555';
      markers += '<div class="fib-marker" style="left:' + mp + '%;background:' + mc + '" title="' + name + '%: ' + lv.nearest + '"></div>';
    });
  }

  const tradedBadge = isTraded
    ? ' <span style="font-size:9px;background:#0a2a0a;border:1px solid #4dff91;color:#4dff91;padding:1px 5px;border-radius:3px;font-weight:700">✅ TRADED</span>'
    : '';

  // Trade-Richtung bestimmen
  const setup      = r.trade_setup || (r.near_high || r.at_high ? 'SHORT' : (r.near_882 ? 'LONG' : 'NEUTRAL'));
  const isLong     = setup === 'LONG';
  const isShort    = setup === 'SHORT';
  const setupColor = isLong ? '#4dff91' : (isShort ? '#ff2244' : '#ffd700');
  const setupBg    = isLong ? '#021a08' : (isShort ? '#1a0208' : '#141200');
  const setupLabel = isLong ? '📈 LONG'  : (isShort ? '📉 SHORT' : '⚪ NEUTRAL');
  const posPct     = r.position_pct != null ? r.position_pct.toFixed(0) : fillPct.toFixed(0);
  const posColor   = posPct >= 80 ? '#ff2244' : (posPct <= 20 ? '#4dff91' : '#ffd700');

  // Trade-Daten je nach Setup
  let tradeBlock = '';
  if (isLong) {
    // Entry = aktueller Marktpreis (nicht f882_entry = Fib-Zone)
    // SL muss unter aktuellem Preis liegen — sonst ungültig
    let longEntry = (r.price||0);
    let longSL    = (r.sl||0);
    let slValid   = longSL < longEntry;
    let slColor   = slValid ? '#ff6644' : '#ff2244';
    let slLabel   = slValid ? ('SL $' + longSL.toFixed(2)) : ('⚠️ SL $' + longSL.toFixed(2) + ' UNGÜLTIG');
    tradeBlock =
      '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:5px;font-size:10px">' +
        '<span style="background:#0a2010;border:1px solid #4dff9144;padding:1px 6px;border-radius:3px;color:#4dff91">ENTRY $' + longEntry.toFixed(2) + '</span>' +
        '<span style="background:#0d1520;border:1px solid #6366f144;padding:1px 6px;border-radius:3px;color:#818cf8">Fib 88.2% $' + (r.f882_entry||0).toFixed(2) + '</span>' +
        '<span style="background:#0a1020;border:1px solid #ff440044;padding:1px 6px;border-radius:3px;color:' + slColor + '">' + slLabel + '</span>' +
        '<span style="background:#0a1020;border:1px solid #4dff9133;padding:1px 6px;border-radius:3px;color:#4db870">TP1 $' + (r.tp1||0).toFixed(2) + '</span>' +
        '<span style="background:#0a1020;border:1px solid #ffd70033;padding:1px 6px;border-radius:3px;color:#ffd700">TP2 $' + (r.tp2||0).toFixed(2) + '</span>' +
        '<span style="background:#0a1020;border:1px solid #4db8ff33;padding:1px 6px;border-radius:3px;color:#4db8ff">TP3 $' + (r.tp3||0).toFixed(2) + '</span>' +
        '<span style="color:#4a6a8a;align-self:center">R:R ' + (r.rr||'?') + '</span>' +
      '</div>';
  } else if (isShort) {
    let shortEntry = (r.price||0);
    let shortSL    = (r.sh_sl||0);
    let slValid    = shortSL > shortEntry;
    let slColor    = slValid ? '#ff6644' : '#ff2244';
    let slLabel    = slValid ? ('SL $' + shortSL.toFixed(2)) : ('⚠️ SL $' + shortSL.toFixed(2) + ' UNGÜLTIG');
    tradeBlock =
      '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:5px;font-size:10px">' +
        '<span style="background:#1a0208;border:1px solid #ff224444;padding:1px 6px;border-radius:3px;color:#ff2244">ENTRY $' + shortEntry.toFixed(2) + '</span>' +
        '<span style="background:#0a1020;border:1px solid #ff440044;padding:1px 6px;border-radius:3px;color:' + slColor + '">' + slLabel + '</span>' +
        '<span style="background:#0a1020;border:1px solid #ff224433;padding:1px 6px;border-radius:3px;color:#ff6677">TP1 $' + (r.sh_tp1||0).toFixed(2) + '</span>' +
        '<span style="background:#0a1020;border:1px solid #ffd70033;padding:1px 6px;border-radius:3px;color:#ffd700">TP2 $' + (r.sh_tp2||0).toFixed(2) + '</span>' +
        '<span style="background:#0a1020;border:1px solid #4db8ff33;padding:1px 6px;border-radius:3px;color:#4db8ff">TP3 $' + (r.sh_tp3||0).toFixed(2) + '</span>' +
        '<span style="color:#4a6a8a;align-self:center">R:R ' + (r.sh_rr||'?') + '</span>' +
      '</div>';
  }

  // Options OI Block (wenn vorhanden)
  let optsBlock = '';
  if (r.opts) {
    const o       = r.opts;
    const sentC   = o.sentiment === 'BULLISH' ? '#4dff91' : (o.sentiment === 'BEARISH' ? '#ff4d6b' : '#ffd700');
    const cStr    = o.call_oi >= 1000 ? (o.call_oi/1000).toFixed(0)+'k' : o.call_oi;
    const pStr    = o.put_oi  >= 1000 ? (o.put_oi /1000).toFixed(0)+'k' : o.put_oi;
    optsBlock = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:5px;padding:5px 6px;background:#05080f;border-radius:4px;font-size:10px;border:1px solid #1a2030">'
      + '<span style="color:#4a6a9a;font-weight:bold">📊 OPTIONEN:</span>'
      + '<span style="color:#4dff91">Calls: <b>' + cStr + '</b> OI</span>'
      + '<span style="color:#ff6677">Puts: <b>' + pStr + '</b> OI</span>'
      + '<span style="color:' + sentC + '">P/C: <b>' + (o.pc||0).toFixed(2) + '</b> ' + (o.sentiment||'') + '</span>'
      + '<span style="color:#ff4d6b;font-size:9px">🧱 Wall: $' + (o.call_wall||0).toFixed(0) + '</span>'
      + '<span style="color:#4dff91;font-size:9px">🛡 Floor: $' + (o.put_floor||0).toFixed(0) + '</span>'
      + '</div>';
  }

  return '<div class="fib-card ' + cardCls + '" style="border-left:3px solid ' + setupColor + ';background:' + setupBg + '">' +
    '<div style="display:flex;justify-content:space-between;align-items:center">' +
      '<div style="display:flex;align-items:center;gap:6px">' +
        '<span class="fib-sym">' + r.sym + tradedBadge + '</span>' +
        '<span style="font-size:12px;font-weight:900;color:' + setupColor + '">' + setupLabel + '</span>' +
      '</div>' +
      '<span style="font-size:10px;background:' + sigInfo[1] + ';color:#fff;padding:2px 7px;border-radius:4px;font-weight:700">' + sigInfo[0] + '</span>' +
    '</div>' +
    '<div style="display:flex;align-items:center;gap:10px;margin:4px 0">' +
      '<span class="fib-price">$' + (r.price||0).toFixed(2) + '</span>' +
      '<span class="' + chgCls + '" style="font-size:12px">' + chgStr + '</span>' +
      '<span style="font-size:10px;color:' + posColor + ';font-weight:bold">Pos: ' + posPct + '%</span>' +
      (volStr ? '<span style="color:#ff9500;font-size:11px">' + volStr + '</span>' : '') +
      (bounce  ? '<span style="color:#4dff88;font-size:11px">⬆ BOUNCE</span>'    : '') +
      (reject  ? '<span style="color:#ff4444;font-size:11px">⬇ REJECTION</span>' : '') +
    '</div>' +
    tradeBlock +
    optsBlock +
    '<div class="fib-bar" style="margin-top:6px">' +
      '<div class="fib-bar-fill" style="width:' + fillPct.toFixed(1) + '%;background:' + setupColor + '"></div>' +
      markers +
    '</div>' +
    '<div style="display:flex;justify-content:space-between;font-size:10px;color:#3a4a5a;margin-top:2px">' +
      '<span>🟢 LOW $' + (r.swing_low||0).toFixed(2) + '</span>' +
      '<span style="color:' + posColor + '">' + posPct + '% in Range</span>' +
      '<span>🔴 HIGH $' + (r.swing_high||0).toFixed(2) + '</span>' +
    '</div>' +
  '</div>';
}

let _autoTradeOn = false;
function toggleAutoTrade() {
  _autoTradeOn = !_autoTradeOn;
  fetch('/hermes/autotrade', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({enabled: _autoTradeOn})})
  .then(r => r.json()).then(d => {
    let btn = document.getElementById('autotrade-btn');
    if (d.enabled) {
      btn.style.background = '#0a2a0a';
      btn.style.borderColor = '#2d9e57';
      btn.style.color = '#4dff91';
      btn.textContent = '🤖 AUTO EIN $' + d.amount;
    } else {
      btn.style.background = '#0a1a2a';
      btn.style.borderColor = '#1e3a5f';
      btn.style.color = '#4a6a8a';
      btn.textContent = '🤖 AUTO AUS';
    }
  });
}

function closeAllTrades() {
  if (!confirm('Alle offenen Aktien-Positionen jetzt schließen?')) return;
  fetch('/hermes/close-trades', {method:'POST'})
    .then(r => r.json()).then(d => {
      alert(d.ok ? 'Positionen geschlossen! P&L in Portfolio-Sektion.' : 'Fehler: ' + d.error);
      setTimeout(checkStatus, 2000);
    });
}

function startScan() {
  fetch('/start', {method:'POST'}).then(r => r.json()).then(d => {
    if (d.ok) {
      document.getElementById('scanBtn').disabled = true;
      document.getElementById('progressWrap').style.display = 'block';
      document.getElementById('liveDot').style.background = '#ffd700';
    } else {
      alert(d.msg || 'Fehler');
    }
  });
}

function updateHeader(d) {
  let scanInfo = d.last_scan ? 'Scan: ' + d.last_scan : 'Kein Scan';
  document.getElementById('lastScanInfo').textContent = scanInfo;
  if (d.next_scan) {
    document.getElementById('nextScanInfo').textContent = 'Auto: ' + d.next_scan;
  }
  // Hermes Status Badge
  let badge = document.getElementById('hermes-badge');
  let txt   = document.getElementById('hermes-status-text');
  let ht = d.hermes_ts || '';
  if (d.hermes_running) {
    badge.style.borderColor = '#ffd700';
    badge.style.color = '#ffd700';
    badge.querySelector('span').style.background = '#ffd700';
    badge.querySelector('span').style.boxShadow = '0 0 6px #ffd700';
    txt.textContent = 'HERMES läuft...';
  } else if (d.running) {
    badge.style.borderColor = '#ff8c00';
    badge.style.color = '#ffa040';
    badge.querySelector('span').style.background = '#ff8c00';
    badge.querySelector('span').style.boxShadow = '0 0 6px #ff8c00';
    txt.textContent = 'SCAN läuft...';
  } else if (ht) {
    badge.style.borderColor = '#2d9e57';
    badge.style.color = '#4dff91';
    badge.querySelector('span').style.background = '#4dff91';
    badge.querySelector('span').style.boxShadow = '0 0 6px #4dff91';
    txt.textContent = 'HERMES ✓ ' + ht;
  } else {
    badge.style.borderColor = '#1e3a5f';
    badge.style.color = '#4a6a8a';
    badge.querySelector('span').style.background = '#4a6a8a';
    badge.querySelector('span').style.boxShadow = 'none';
    txt.textContent = 'HERMES startet...';
  }
}

function checkStatus() {
  fetch('/status').then(r => r.json()).then(d => {
    updateHeader(d);

    if (d.running) {
      let pct = d.total > 0 ? Math.round(d.progress / d.total * 100) : 0;
      document.getElementById('progressFill').style.width = pct + '%';
      document.getElementById('progressText').textContent =
        'Scanne ' + d.current + ' (' + d.progress + '/' + d.total + ') — ' + pct + '%';
      document.getElementById('progressWrap').style.display = 'block';
      document.getElementById('scanBtn').disabled = true;
      document.getElementById('liveDot').style.background = '#ffd700';
      document.getElementById('refreshDot').className = 'refresh-dot active';
      document.getElementById('refreshInfo').textContent = 'Scanner läuft...';
    } else {
      document.getElementById('progressWrap').style.display = 'none';
      document.getElementById('scanBtn').disabled = false;
      document.getElementById('liveDot').style.background = '#4dff91';

      // Neue Ergebnisse prüfen
      if (d.results_hash && d.results_hash !== lastHash) {
        lastHash = d.results_hash;
        loadResults(true);
      }
    }
  });
}

function loadResults(isNew) {
  fetch('/results').then(r => r.json()).then(data => {
    if (!data || data.error) {
      document.getElementById('content').innerHTML =
        '<div class="empty">' + (data && data.error ? data.error : 'Kein Scan vorhanden.') + '</div>';
      return;
    }
    lastHash = data.time;
    _lastData = data;
    // Tab 1: Scanner
    document.getElementById('content').innerHTML = renderResults(data, isNew);
    // Tab 2: Intel — immer rendern (Kanal/VWAP/Gap laufen 24/7)
    document.getElementById('intel-content').innerHTML = renderTab2(data) || '<div class="empty">Keine Intel-Daten.</div>';
  });
}

function startAutoRefresh() {
  refreshCountdown = 60;
  document.getElementById('refreshDot').className = 'refresh-dot active';
  if (refreshInterval) clearInterval(refreshInterval);
  refreshInterval = setInterval(() => {
    refreshCountdown--;
    if (!document.getElementById('refreshInfo')) return;
    let running = document.getElementById('scanBtn').disabled;
    if (!running) {
      document.getElementById('refreshInfo').textContent =
        'Auto-Refresh in ' + refreshCountdown + 's';
    }
    if (refreshCountdown <= 0) {
      refreshCountdown = 60;
      checkStatus();
    }
  }, 1000);
  // Während Scan alle 5s prüfen
  setInterval(() => {
    if (document.getElementById('scanBtn').disabled) {
      checkStatus();
    }
  }, 5000);
}

// Init
checkStatus();
startAutoRefresh();
if (document.getElementById('scanBtn').disabled === false) {
  fetch('/results').then(r => r.json()).then(data => {
    if (data && !data.error) {
      lastHash = data.time;
      document.getElementById('content').innerHTML = renderResults(data, false);
    }
  });
}
</script>
</body>
</html>'''

# ── API-Endpunkte ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/start', methods=['POST'])
def start():
    if state['running']:
        return jsonify({'ok': False, 'msg': 'Scanner läuft bereits'})
    t = threading.Thread(target=run_scan_thread, kwargs={'trigger': 'manual'}, daemon=True)
    t.start()
    return jsonify({'ok': True})

@app.route('/status')
def status():
    with _hermes_lock:
        h_ts      = state.get('hermes_ts', '')
        h_running = state.get('hermes_running', False)
    return jsonify({
        'running':      state['running'],
        'progress':     state['progress'],
        'total':        state['progress_total'],
        'current':      state['current_ticker'],
        'last_scan':    state['last_scan'],
        'next_scan':    state['next_scan'] or next_scan_time(),
        'has_results':  state['results'] is not None or os.path.exists(RESULTS_FILE),
        'results_hash': state['last_results_hash'],
        'error':        state['error'],
        'hermes_ts':    h_ts,
        'hermes_running': h_running,
        'hermes_error': state.get('hermes_last_error'),
    })

def _to_json_safe(obj):
    """Konvertiert numpy/pandas Typen → Python Standard-Typen für JSON."""
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, bool):
        return bool(obj)
    try:
        import numpy as np
        if isinstance(obj, (np.integer,)):  return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, (np.bool_,)):    return bool(obj)
        if isinstance(obj, np.ndarray):     return obj.tolist()
    except ImportError:
        pass
    if obj is None or isinstance(obj, (int, float, str)):
        return obj
    return str(obj)

@app.route('/results')
def results():
    try:
        data = state['results'] or load_results()
        if not data:
            return jsonify({
                'error':          'Noch kein Scan. Drücke SCAN STARTEN.',
                'hermes_running': state.get('hermes_running', False),
                'hermes_ts':      state.get('hermes_ts', ''),
                'running':        state.get('running', False),
            })
        out = {
            'time':    data.get('time'),
            'today':   data.get('today'),
            'scanned': data.get('scanned', 0),
            'total':   data.get('total', 0),
            'longs':   data.get('longs', []),
            'shorts':  data.get('shorts', []),
            'watch':   data.get('watch', []),
            'movers':  data.get('movers', []),
            'social':  data.get('social', []),
            'social_data': state.get('social_data', []),
            'hf_data':     state.get('hf_data', []),
            'influencers': data.get('influencers', []),
        }
        if state.get('followup'):
            out['followup'] = state['followup']
        # Big Money Bets — läuft alle 15 Min unabhängig vom Scan
        _bm_ts2 = state.get('big_money_bets', {}).get('ts', '')
        _bm_stale = True
        if _bm_ts2:
            try:
                _h2, _m2 = map(int, _bm_ts2.split(':'))
                _now2 = __import__('datetime').datetime.now()
                _bm_stale = (_now2.hour*60+_now2.minute) - (_h2*60+_m2) > 15
            except Exception:
                pass
        if _bm_stale and not state.get('_bm_running'):
            state['_bm_running'] = True
            def _auto_bm():
                try:
                    from scanner import find_big_money_bets
                    bets = find_big_money_bets()
                    if bets:
                        state['big_money_bets'] = {
                            'ts': __import__('datetime').datetime.now().strftime('%H:%M'),
                            'data': bets,
                        }
                finally:
                    state['_bm_running'] = False
            threading.Thread(target=_auto_bm, daemon=True).start()
        with _hermes_lock:
            out['hermes_alerts']       = state.get('hermes_alerts', [])
            out['hermes_running']      = state.get('hermes_running', False)
            out['running']             = state.get('running', False)
            out['market_sentiment']    = state.get('market_sentiment', {})
            out['market_context']      = state.get('market_context', {})
            # Geopolitik-Katalysator live abrufen (3 Min Cache in scanner)
            try:
                from scanner import get_geopolitical_catalyst
                out['geo_catalyst'] = get_geopolitical_catalyst()
            except Exception:
                out['geo_catalyst'] = {}
            out['mt5_status']          = state.get('mt5_status', {})
            out['sector_rotation']     = state.get('sector_rotation', {})
            out['max_pain']            = state.get('max_pain', {})
            out['live_feed']           = state.get('live_feed', [])
            out['hermes_picks']        = state.get('hermes_picks', [])
            out['hermes_ts']           = state.get('hermes_ts', '')
            out['hermes_ai']           = state.get('hermes_ai', '')
            out['hermes_news']         = state.get('hermes_news', [])
            out['hermes_universe']     = list(state.get('hermes_universe', set()))
            out['hermes_signal_evals'] = state.get('hermes_signal_evals', {})
            out['mag7_signal']         = _to_json_safe(state.get('mag7_signal', {}))
            out['social_deep']         = state.get('social_deep', [])
            out['alpaca_portfolio']    = state.get('alpaca_portfolio', {})
            out['hermes_memory']       = state.get('hermes_memory', {})
            out['hermes_24h']              = state.get('hermes_24h', [])
            out['hermes_learning']         = load_learning()
            out['hermes_identity']         = load_identity()
            out['ibkr_data']               = state.get('ibkr_data', {})
            out['ibkr_scan']               = state.get('ibkr_scan', [])
            out['ibkr_ts']                 = state.get('ibkr_ts')
            out['market_fall_candidates']  = state.get('market_fall_candidates', [])
            out['market_fall_qqq']         = state.get('market_fall_qqq', 0)
            out['brain_worldview']         = state.get('brain_worldview', {})
            out['brain_trades']            = state.get('brain_trades', [])
            out['brain_news']              = state.get('brain_news', [])
            out['channel_active']          = state.get('channel_active', [])
            out['channel_signals']         = state.get('channel_signals', [])[:20]
            out['channel_watch']           = state.get('channel_watch', [])
            out['vwap_signals']            = state.get('vwap_signals', [])[:10]
            out['gap_signals']             = state.get('gap_signals', [])[:10]
            out['trading_blackout']        = state.get('trading_blackout', False)
            out['claude_ibkr']             = state.get('claude_ibkr', {})
            out['claude_movers']           = state.get('claude_movers', {})
            out['fib_results']             = state.get('fib_results', [])
            out['fib_trades']              = state.get('fib_trades', [])
            out['fib_last_scan']           = state.get('fib_last_scan')
            out['big_money_bets']          = state.get('big_money_bets', {})
        return jsonify(_to_json_safe(out))
    except Exception as e:
        return jsonify({'error': f'Server Fehler: {str(e)[:120]}'})

@app.route('/followup')
def followup_api():
    return jsonify(state.get('followup') or {})

@app.route('/social')
def social_api():
    try:
        from scanner import get_social_trending
        tickers, scores = get_social_trending()
        return jsonify({'tickers': tickers, 'scores': scores})
    except Exception as e:
        return jsonify({'error': str(e)})

# ── Hermes AI-Gate ────────────────────────────────────────────────────────────
# Broker zwischen Hermes (lokal) und bot.js (Railway)
# Hermes schreibt Market-View → bot.js liest vor jedem Trade

_hermes_view = {
    'bias':           'NEUTRAL',   # BULL / BEAR / NEUTRAL
    'risk_level':     'NORMAL',    # NORMAL / HIGH / EXTREME
    'approved_long':  [],          # Symbole die Hermes für LONG freigegeben hat
    'approved_short': [],          # Symbole die Hermes für SHORT freigegeben hat
    'blocked':        [],          # Explizit gesperrte Symbole
    'reason':         'Noch keine Analyse',
    'market_context': '',
    'ts':             None,
    'positions_ok':   True,        # False = keine neuen Positionen öffnen
    'analysis':       []           # Letzte AI-Analysen [{sym, action, approved, reason}]
}
_hermes_lock = threading.Lock()

@app.route('/hermes', methods=['GET'])
def hermes_get():
    with _hermes_lock:
        return jsonify(_hermes_view)

@app.route('/hermes', methods=['POST'])
def hermes_post():
    data = request.json or {}
    with _hermes_lock:
        _hermes_view.update(data)
        _hermes_view['ts'] = datetime.now().isoformat()
    return jsonify({'ok': True})

@app.route('/hermes/approve', methods=['POST'])
def hermes_approve():
    """bot.js ruft das auf um Trade-Approval zu bekommen."""
    data     = request.json or {}
    symbol   = data.get('symbol', '')
    action   = data.get('action', 'buy')   # buy / sell / short
    score    = data.get('score', 0)
    rsi      = data.get('rsi', 50)
    reason   = data.get('reason', '')
    price    = data.get('price', 0)
    gain_pct = data.get('gain_pct', 0)

    with _hermes_lock:
        view = dict(_hermes_view)

    # Sofort-Ablehnungen
    if view['risk_level'] == 'EXTREME':
        return jsonify({'approved': False, 'reason': 'EXTREME Risk — kein Trading', 'level': 'EXTREME'})

    if symbol in view.get('blocked', []):
        return jsonify({'approved': False, 'reason': f'{symbol} ist geblockt', 'level': 'BLOCKED'})

    # Kein neues Positionieren erlaubt
    if action in ('buy', 'short') and not view.get('positions_ok', True):
        return jsonify({'approved': False, 'reason': 'positions_ok=False — keine neuen Trades', 'level': 'PAUSED'})

    # Approved-List prüfen
    approved_long  = view.get('approved_long', [])
    approved_short = view.get('approved_short', [])

    if action == 'buy' and approved_long:
        if symbol not in approved_long:
            return jsonify({'approved': False,
                            'reason': f'{symbol} nicht in Hermes LONG-Liste: {approved_long[:5]}',
                            'level': 'NOT_APPROVED'})

    if action == 'short' and approved_short:
        if symbol not in approved_short:
            return jsonify({'approved': False,
                            'reason': f'{symbol} nicht in Hermes SHORT-Liste',
                            'level': 'NOT_APPROVED'})

    # Verkäufe werden immer genehmigt (Exit ist immer ok)
    if action in ('sell', 'cover'):
        return jsonify({'approved': True, 'reason': 'Exit immer erlaubt', 'level': 'ALWAYS'})

    # Bias-Check
    bias = view.get('bias', 'NEUTRAL')
    if action == 'buy' and bias == 'BEAR':
        return jsonify({'approved': False, 'reason': f'Hermes sieht BEAR-Markt: {view.get("reason","")}',
                        'level': 'BIAS'})

    ts = view.get('ts')
    age_min = 999
    if ts:
        try:
            delta = datetime.now() - datetime.fromisoformat(ts)
            age_min = int(delta.total_seconds() / 60)
        except Exception:
            pass

    # Hermes-Analyse älter als 30 Min → Pass-through (kein Blocking)
    if age_min > 30:
        return jsonify({'approved': True,
                        'reason': f'Hermes-Analyse veraltet ({age_min}min) — Pass-through',
                        'level': 'STALE'})

    return jsonify({'approved': True,
                    'reason': f'Hermes OK | Bias:{bias} | Risk:{view["risk_level"]}',
                    'level': 'APPROVED'})

# ── Hermes Monitor: aktiver Hintergrund-Agent ────────────────────────────────

NOUS_KEY = os.environ.get('NOUS_API_KEY', '')

def _nous_call(prompt: str, system: str = '', max_tokens: int = 500, temperature: float = 0.2) -> str:
    """Ruft NousResearch API auf. Gibt '' zurück bei Fehler."""
    if not NOUS_KEY:
        return ''
    try:
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        body = json.dumps({
            'model': 'NousResearch/Hermes-3-Llama-3.1-70B',
            'messages': messages,
            'max_tokens': max_tokens,
            'temperature': temperature,
        }).encode()
        req = urllib.request.Request(
            'https://inference-api.nousresearch.com/v1/chat/completions',
            data=body,
            headers={'Authorization': f'Bearer {NOUS_KEY}', 'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20) as r:
            resp = json.loads(r.read())
        return resp['choices'][0]['message']['content'].strip()
    except Exception:
        return ''


def hermes_ai_signal_eval(signal: dict) -> str:
    """Tiefe AI-Bewertung eines einzelnen Signals mit allen Polygon-Daten."""
    t       = signal.get('t', '')
    price   = signal.get('price', 0)
    sig     = signal.get('signal', '')
    score   = signal.get('score', 0)
    trend   = signal.get('trend', 0)
    prev_ch = signal.get('prev_chg', 0)
    pc      = signal.get('pc', 1)
    atr     = signal.get('atr', 0)
    drop    = signal.get('drop_high', 0)
    short_t = signal.get('short_trend', 0)
    ls      = signal.get('long_score', 0)
    ss      = signal.get('short_score', 0)
    kat     = signal.get('katalysator', '')
    kat_txt = signal.get('kat_text', '')[:120]
    best    = signal.get('best') or {}
    dp      = (signal.get('dp') or {})
    sweep   = signal.get('sweep') or {}
    h_score = signal.get('hermes_score', 0)
    h_reas  = signal.get('hermes_reasons', [])

    best_info = ''
    if best:
        best_info = f"Beste Option: {best.get('pct',0):+.1f}% OTM Strike ${best.get('strike')} @ ${best.get('pr')} Exp:{best.get('exp')} Vol:{best.get('vol')} OI:{best.get('oi')}"

    dp_info = ''
    if dp:
        dp_info = f"Dark Pool: ${dp.get('dp_total',0):,.0f} Block-Trades ({dp.get('dp_count',0)} Trades)"

    sweep_info = ''
    if sweep:
        sweep_info = f"Options Sweep: {sweep.get('direction','').upper()} ${sweep.get('total',0):,.0f} Premium, {sweep.get('count',0)} Sweeps"

    # Hermes eigene Regeln für Signal-Bewertung
    identity   = load_identity()
    rules_str  = ' | '.join(identity.get('rules', [])[-4:]) or 'keine'

    # Makro-Kontext
    try:
        from scanner import get_macro_context
        macro   = get_macro_context()
        vix_val = macro.get('VIX', {}).get('price', '?')
        regime  = macro.get('regime', '')
        macro_line = f"VIX:{vix_val} ({regime}) | 10Y:{macro.get('TNX',{}).get('price','?')}%"
    except Exception:
        macro_line = ''

    prompt = f"""Analysiere dieses Trading-Signal vollständig:

TICKER: {t} | SIGNAL: {sig} | SCORE: {score}/10
PREIS: ${price} | HEUTE: {prev_ch:+.1f}% | TREND 10T: {trend:+.1f}% | TREND 3T: {short_t:+.1f}%
ATR: ${atr:.2f} | ABST.HOCH: {drop:.1f}% | P/C-RATIO: {pc:.3f}
LONG-SCORE: {ls} | SHORT-SCORE: {ss}
KATALYSATOR: {kat} — {kat_txt}
{best_info}
{dp_info}
{sweep_info}
{f"HERMES SCORE: {h_score} | Gründe: {', '.join(str(r) for r in h_reas[:3])}" if h_score else ""}
{f"MAKRO: {macro_line}" if macro_line else ""}
MEINE REGELN: {rules_str}

Bewerte auf Deutsch:
1. STÄRKE: Wie stark ist dieses Signal? (1-10 mit Begründung)
2. SETUP: Ist das Setup realistisch? Strike erreichbar, Trend passt, Makro unterstützt?
3. RISIKO: Was könnte schiefgehen? Verletzt es eine meiner Regeln?
4. EMPFEHLUNG: Einsteigen, Warten, oder Finger weg? Warum?

Antworte kompakt, max 150 Wörter."""

    return _nous_call(prompt,
        system='Du bist Hermes, ein lernfähiger AI Options-Trader. Beachte deine eigenen Regeln beim Bewerten.',
        max_tokens=320)


def hermes_ai_analysis(scan_data: dict, hunt_alerts: list) -> str:
    """
    Marktüberblick + Bewertung aller Signale via NousResearch Hermes-3 70B.
    Inkl. Makro-Kontext (VIX/Yields), SEC Filings, eigene Hermes-Regeln.
    """
    if not NOUS_KEY:
        return ''
    try:
        from scanner import get_macro_context, get_sec_alerts

        longs  = scan_data.get('longs',  [])[:6]
        shorts = scan_data.get('shorts', [])[:4]
        movers = scan_data.get('movers', [])[:3]
        hunts  = hunt_alerts[:6]

        long_lines  = [f"  {r['t']} Score:{r['score']} Heute:{r.get('prev_chg',0):+.1f}% Trend:{r.get('trend',0):+.1f}% P/C:{r.get('pc',0):.2f} | {r.get('kat_text','')[:55]}" for r in longs]
        short_lines = [f"  {r['t']} Score:{r['score']} Heute:{r.get('prev_chg',0):+.1f}% Trend:{r.get('trend',0):+.1f}% P/C:{r.get('pc',0):.2f} | {r.get('kat_text','')[:55]}" for r in shorts]
        hunt_lines  = [f"  {a['ticker']} Score:{a['score']} | {', '.join(str(x) for x in a.get('reasons',[])[:3])}" for a in hunts]
        mover_lines = [f"  {r['t']} ${r['price']} Heute:{r.get('prev_chg',0):+.1f}% Score:{r['score']}" for r in movers]

        # Hermes Identity — eigene Regeln und Lektionen
        identity = load_identity()
        rules_lines   = '\n'.join(f'  - {r}' for r in identity.get('rules', [])[-6:]) or '  keine'
        lessons_lines = '\n'.join(f'  [{l["date"]}] {l["lesson"][:80]}'
                                  for l in identity.get('lessons', [])[:3]) or '  keine'

        # Makro-Kontext
        macro   = get_macro_context()
        vix_val = macro.get('VIX', {}).get('price', '?')
        tnx_val = macro.get('TNX', {}).get('price', '?')
        spx_chg = macro.get('SPX', {}).get('chg', 0)
        ndx_chg = macro.get('NDX', {}).get('chg', 0)
        dxy_chg = macro.get('DXY', {}).get('chg', 0)
        regime  = macro.get('regime', 'NORMAL')
        fed_news_lines = '\n'.join(f'  - {n}' for n in macro.get('fed_news', [])[:3]) or '  keine'

        # SEC Filings (8-K + Insider Form 4)
        all_tickers = list({r['t'] for r in longs + shorts})
        sec_alerts  = get_sec_alerts(all_tickers)
        fresh_sec   = [a for a in sec_alerts if a.get('fresh')]
        sec_lines   = '\n'.join(f'  {a["ticker"]} [{a["form"]}] {a["title"][:60]}'
                                for a in fresh_sec[:5]) or '  keine heute'

        # Earnings nächste Woche via Polygon
        try:
            from scanner import get_earnings_calendar_polygon
            earnings = get_earnings_calendar_polygon(all_tickers)
            earn_lines = ', '.join(f'{t} ({d})' for t, d in list(earnings.items())[:5]) or 'keine'
        except Exception:
            earn_lines = 'keine'

        # Aschenbrenner Positionen
        asch_longs  = ['NBIS','KEEL','CLSK','RIOT','BTDR','IREN','APLD']
        asch_shorts = ['NVDA','AVGO','AMD','SMH','ORCL']
        asch_l = [r for r in longs  + scan_data.get('watch',[]) if r['t'] in asch_longs]
        asch_s = [r for r in shorts + scan_data.get('watch',[]) if r['t'] in asch_shorts]
        asch_long_str  = ', '.join(f"{r['t']} {r.get('prev_chg',0):+.1f}% Score:{r['score']}" for r in asch_l[:5]) or 'nicht im Scan'
        asch_short_str = ', '.join(f"{r['t']} {r.get('prev_chg',0):+.1f}% Score:{r['score']}" for r in asch_s[:5]) or 'nicht im Scan'

        # Market Fall Kandidaten (wenn QQQ < -1%)
        fall_cands = state.get('market_fall_candidates', [])
        fall_qqq   = state.get('market_fall_qqq', 0)
        if fall_cands and fall_qqq <= -1.0:
            fall_lines = '\n'.join(f"  {fc['sym']} {fc['chg']:+.1f}% Score:{fc['score']} — {fc['reasons'][0] if fc['reasons'] else ''}" for fc in fall_cands[:5])
            fall_section = f"\n=== ⚠️ MARKT FÄLLT {fall_qqq:.1f}% — SENSITIVSTE SHORT-KANDIDATEN ===\n{fall_lines}\n→ Diese fallen überproportional. News-Katalysator? Verwässerung? PUT Setup?"
        else:
            fall_section = ''

        prompt = f"""Du bist Hermes, ein professioneller AI Trading-Agent mit Selbstlern-Fähigkeit.

=== DEINE EIGENEN REGELN (selbst gelernt) ===
{rules_lines}

=== DEINE LETZTEN LEKTIONEN ===
{lessons_lines}

=== MAKRO-KONTEXT ===
VIX: {vix_val} ({regime}) | 10Y Yield: {tnx_val}% | S&P500: {spx_chg:+.2f}% | NASDAQ: {ndx_chg:+.2f}% | Dollar: {dxy_chg:+.2f}%
FED News:
{fed_news_lines}

=== SEC FILINGS HEUTE ===
{sec_lines}

=== EARNINGS NÄCHSTE WOCHE ===
{earn_lines}

=== SITUATIONAL AWARENESS LP (Aschenbrenner 13F Q1 2026) ===
LONG KI: NBIS(38%=$2.6B), KEEL, CLSK, RIOT, BTDR, IREN
SHORT Semis (PUT): SMH($2B), NVDA($1.57B), ORCL($1.07B), AVGO($1B), AMD($969M)
→ Longs heute: {asch_long_str}
→ Shorts heute: {asch_short_str}
{fall_section}
=== POLYGON SCANNER — LONGS ({len(longs)}) ===
{chr(10).join(long_lines) or '  keine'}

=== POLYGON SCANNER — SHORTS ({len(shorts)}) ===
{chr(10).join(short_lines) or '  keine'}

=== HERMES HUNT ({len(hunts)}) ===
{chr(10).join(hunt_lines) or '  keine'}

Trading-Briefing auf Deutsch:
1. MAKRO CHECK: VIX/Yield/Regime — was bedeutet das für heute?
2. NEWS/SEC: Gibt es Verwässerung, Downgrade oder 8-K die ein Signal erklären/verneinen?
3. TOP TRADE: Bestes Setup (Long ODER Short — je nach Markt-Regime!)
4. MARKT-FALL: Falls QQQ fällt — welche Aktie fällt am meisten und warum?
5. FAZIT: 1 klarer Satz.

Max 220 Wörter. Bei fallendem Markt: SHORT-Setups bevorzugen, LONG nur bei starkem Einzelsignal."""

        return _nous_call(
            prompt,
            system=(
                'Du bist Hermes, ein lernfähiger AI Trading-Agent. Du hast Zugang zu: '
                'Polygon Options-Flow, Dark Pool, Smart Money, SEC EDGAR Filings, '
                'VIX/Treasury/Makro-Daten, und deinen eigenen selbstgeschriebenen Handelsregeln. '
                'Kombiniere alle Datenquellen für eine fundierte Analyse.'
            ),
            max_tokens=550
        )
    except Exception:
        return ''


# ── Hermes Brain — autonomer KI-Agent ────────────────────────────────────────
_brain_seen_ids = set()   # dedup für News
_brain_prio_q   = []      # Priority-Scan-Queue

def hermes_classify_news(headline: str, tickers: list) -> dict:
    """
    KI klassifiziert eine einzelne News: LONG / SHORT / IGNORE.
    Erst Keyword-Check (instant), dann AI nur für unklare Fälle.
    """
    from scanner import HARD_NEG_KEYS, HIGH_IMPACT_GOV, HIGH_IMPACT_ENDORSE, HIGH_IMPACT_INSIDER, HIGH_IMPACT_NEG
    hl = headline.lower()

    # Sofort SHORT: Verwässerung, Secondary Offering, DOJ, SEC, Bankruptcy
    for k in list(HARD_NEG_KEYS) + list(HIGH_IMPACT_NEG):
        if k in hl:
            return {'action': 'SHORT', 'strength': 'STARK', 'reason': f'Katalysator: {k}', 'ai': False}
    # Sofort LONG: Gov-Vertrag, CEO-Endorsement, Insider-Kauf
    for k in list(HIGH_IMPACT_GOV) + list(HIGH_IMPACT_ENDORSE) + list(HIGH_IMPACT_INSIDER):
        if k in hl:
            return {'action': 'LONG', 'strength': 'STARK', 'reason': f'Katalysator: {k}', 'ai': False}
    # Earnings Beat → LONG
    if any(k in hl for k in ['beat', 'raised guidance', 'record revenue', 'surprise earnings']):
        return {'action': 'LONG', 'strength': 'NORMAL', 'reason': 'Earnings Beat', 'ai': False}
    # Earnings Miss → SHORT
    if any(k in hl for k in ['miss', 'below estimate', 'cut guidance', 'lowered guidance', 'warned']):
        return {'action': 'SHORT', 'strength': 'NORMAL', 'reason': 'Earnings Miss / Warning', 'ai': False}
    # Downgrade → SHORT
    if any(k in hl for k in ['downgrade', 'sell rating', 'underperform', 'price target cut']):
        return {'action': 'SHORT', 'strength': 'NORMAL', 'reason': 'Downgrade', 'ai': False}
    # Upgrade → LONG
    if any(k in hl for k in ['upgrade', 'buy rating', 'outperform', 'price target raised']):
        return {'action': 'LONG', 'strength': 'NORMAL', 'reason': 'Upgrade', 'ai': False}

    # Unklar → AI fragen (nur 50 Token, sehr schnell)
    if not NOUS_KEY:
        return {}
    try:
        prompt = (f'Aktien-News: "{headline[:120]}"\n'
                  f'Ticker: {",".join(tickers[:3])}\n'
                  f'Antworte NUR: LONG, SHORT oder IGNORE\n'
                  f'Dann Pipe dann 10-Wort-Grund: LONG|Grund')
        raw = _nous_call(prompt, system='Trading AI. Nur: ACTION|GRUND', max_tokens=40)
        if raw and '|' in raw:
            parts = raw.strip().split('|', 1)
            action = parts[0].strip().upper()
            if action in ('LONG', 'SHORT', 'IGNORE'):
                return {'action': action, 'strength': 'NORMAL', 'reason': parts[1].strip()[:60], 'ai': True}
    except Exception:
        pass
    return {}


def detect_macro_catalyst(headline: str) -> dict | None:
    """
    Erkennt marktweite Katalysatoren (Trump, Geopolitik, Fed, Kriege).
    Gibt {direction, strength, reason} zurück oder None.
    Trump-News bewegen den gesamten Markt +/-100-300pt sofort.
    """
    hl = headline.lower()

    # ── STARK BULLISH — alles steigt ──────────────────────────────────────
    bull_strong = [
        'no attack', 'no attacks', 'ceasefire', 'cease-fire', 'peace deal',
        'trade deal', 'trade agreement', 'tariffs removed', 'tariffs lifted',
        'tariff relief', 'no tariffs', 'tariff pause', 'pause tariffs',
        'rate cut', 'fed cuts', 'powell cut', 'stimulus', 'bailout approved',
        'debt ceiling resolved', 'shutdown avoided', 'trump deal',
        'trump signs', 'trump agreement', 'trump peace', 'trump truce',
        'war ends', 'conflict ends', 'troops withdraw', 'sanctions lifted',
        'trump no war', 'trump ceasefire', 'negotiations succeed',
    ]
    bull_normal = [
        'trump positive', 'market positive', 'rally', 'surge expected',
        'stocks rise', 'inflation falls', 'jobs beat', 'gdp beat',
        'consumer confidence', 'rate hold', 'soft landing',
    ]

    # ── STARK BEARISH — alles fällt ───────────────────────────────────────
    bear_strong = [
        'new tariffs', 'tariff hike', 'tariff increase', 'tariffs announced',
        'tariff threat', 'tariff on', 'tariff war', 'trade war',
        'trump tariff', 'trump threatens', 'trump sanctions',
        'military strike', 'attack launched', 'missile', 'war declared',
        'invasion', 'escalation', 'nuclear', 'government shutdown',
        'debt ceiling crisis', 'recession', 'rate hike emergency',
        'trump attacks', 'trump bombs', 'trump declares war',
        'sanctions imposed', 'sanctions expand',
    ]
    bear_normal = [
        'stocks fall', 'market crash', 'sell-off', 'inflation surge',
        'jobs miss', 'gdp miss', 'recession fears', 'banking crisis',
    ]

    # Trump-Erkennung: jede Trump-Nachricht prüfen
    is_trump = any(k in hl for k in ['trump', 'white house', 'president trump', 'oval office'])

    for k in bull_strong:
        if k in hl:
            strength = 'EXTREME' if is_trump else 'STARK'
            return {'direction': 'LONG', 'strength': strength,
                    'reason': f'Makro-Katalysator: {k}', 'trump': is_trump}
    for k in bull_normal:
        if k in hl:
            return {'direction': 'LONG', 'strength': 'NORMAL',
                    'reason': f'Makro bullish: {k}', 'trump': is_trump}
    for k in bear_strong:
        if k in hl:
            strength = 'EXTREME' if is_trump else 'STARK'
            return {'direction': 'SHORT', 'strength': strength,
                    'reason': f'Makro-Risiko: {k}', 'trump': is_trump}
    for k in bear_normal:
        if k in hl:
            return {'direction': 'SHORT', 'strength': 'NORMAL',
                    'reason': f'Makro bearish: {k}', 'trump': is_trump}

    # Allgemeiner Trump-Check (wenn Nachricht Trump erwähnt → AI entscheiden)
    if is_trump and len(hl) > 20:
        result = hermes_classify_news(headline, [])
        if result.get('action') in ('LONG', 'SHORT'):
            return {'direction': result['action'], 'strength': 'NORMAL',
                    'reason': result.get('reason', 'Trump News'), 'trump': True}
    return None


def hermes_brain_loop():
    """
    Hermes Gehirn — läuft 24/7 unabhängig vom 5-Min-Scanner.

    Was Hermes selbst macht:
    1. Alle 90s: News-Feed lesen → KI klassifiziert LONG/SHORT/IGNORE
    2. Bei wichtiger News → sofort Ticker scannen (Priority Queue)
    3. Bei Score >= 12 + Conviction >= 0.80 → direkt auf Alpaca traden
    4. Marktlage laufend updaten → Strategie anpassen (LONG_HUNT / SHORT_HUNT)
    5. Alle Entscheidungen + P&L transparent loggen
    """
    global _brain_seen_ids, _brain_prio_q
    time.sleep(90)  # Warten bis Hauptsystem hochgefahren ist

    while True:
        try:
            state['brain_active'] = True
            now_utc = datetime.now(timezone.utc)
            # Aktiv während Extended Market Hours (06:00-22:00 UTC = 2 AM - 6 PM ET)
            is_active = (now_utc.hour >= 11 and now_utc.hour < 22)

            # ── 1) Marktlage prüfen → Worldview updaten ──────────────────────
            try:
                from scanner import get_market_context
                mkt      = get_market_context()
                qqq_chg  = mkt.get('qqq_chg', 0)
                mkt_bias = mkt.get('bias', 'NEUTRAL')
                wv = state['brain_worldview']
                old_focus = wv.get('focus', 'BALANCED')

                # IBKR Override: frische Claude-Daten haben Vorrang vor Alpaca (< 30 Min)
                _ibkr_override = None
                def _get_ibkr_chg():
                    now_min = datetime.now().hour * 60 + datetime.now().minute
                    for _src in [state.get('claude_ibkr', {}), state.get('claude_movers', {})]:
                        upd = _src.get('updated', '')
                        if not upd:
                            continue
                        try:
                            _h, _m = map(int, upd.split(':'))
                            if 0 <= now_min - (_h * 60 + _m) <= 30:
                                if _src.get('nas_chg') is not None:
                                    return float(_src['nas_chg'])
                                for _s in _src.get('most_traded_usd', []):
                                    if _s.get('symbol') == 'QQQ':
                                        return float(_s.get('chg_pct', 0))
                        except Exception:
                            pass
                    return None
                _ibkr_override = _get_ibkr_chg()
                if _ibkr_override is not None:
                    qqq_chg  = _ibkr_override
                    mkt_bias = ('STRONG_BULL' if qqq_chg >= 2.0 else
                                'BULL'        if qqq_chg >= 0.5 else
                                'STRONG_BEAR' if qqq_chg <= -2.0 else
                                'BEAR'        if qqq_chg <= -0.5 else 'NEUTRAL')

                if mkt_bias in ('STRONG_BEAR',) or qqq_chg <= -2.0:
                    new_focus = 'SHORT_HUNT'
                elif mkt_bias == 'BEAR' or qqq_chg <= -1.2:
                    new_focus = 'SHORT_LEAN'
                elif mkt_bias in ('STRONG_BULL',) or qqq_chg >= 2.0:
                    new_focus = 'LONG_HUNT'
                elif mkt_bias == 'BULL' or qqq_chg >= 1.2:
                    new_focus = 'LONG_LEAN'
                else:
                    new_focus = 'BALANCED'

                _src_label = 'IBKR' if _ibkr_override is not None else 'QQQ'
                state['brain_worldview'] = {
                    'focus':   new_focus, 'market': mkt_bias,
                    'qqq':     round(qqq_chg, 2),
                    'reason':  f'{_src_label} {qqq_chg:+.1f}% | {mkt_bias}',
                    'updated': datetime.now().strftime('%H:%M'),
                }

                # Strategie-Wechsel → Telegram
                if new_focus != old_focus and is_active:
                    icons = {'SHORT_HUNT':'🔴🔴','SHORT_LEAN':'🔴','BALANCED':'⚪','LONG_LEAN':'🟢','LONG_HUNT':'🟢🟢'}
                    tg_send(
                        f'{icons.get(new_focus,"⚪")} <b>HERMES STRATEGIE: {new_focus}</b>\n'
                        f'{old_focus} → {new_focus}\n'
                        f'{_src_label} {qqq_chg:+.1f}% | Markt: {mkt_bias}',
                        key=f'wv_{new_focus}'
                    )
            except Exception:
                mkt = {}

            # ── 2) News-Feed lesen + KI klassifizieren ────────────────────────
            if is_active:
                try:
                    news_items = get_alpaca_market_news(limit=20)
                except Exception:
                    news_items = []

                for n in news_items:
                    nid = str(n.get('id', n.get('headline', '')[:40]))
                    if nid in _brain_seen_ids:
                        continue
                    _brain_seen_ids.add(nid)

                    headline = n.get('headline', '')
                    symbols  = [s.get('symbol', '') for s in n.get('symbols', [])
                                if s.get('symbol', '') and len(s.get('symbol','')) <= 5][:3]

                    # ── Trump/Makro Katalysator: ZUERST prüfen (vor allem anderen) ──
                    macro = detect_macro_catalyst(headline)
                    if macro:
                        direction = macro['direction']
                        strength  = macro['strength']
                        reason    = macro['reason']
                        is_trump  = macro.get('trump', False)
                        trump_entry = {
                            'direction': direction,
                            'strength':  strength,
                            'headline':  headline[:120],
                            'reason':    reason,
                            'trump':     is_trump,
                            'ts':        datetime.now().isoformat(),
                            'ts_str':    datetime.now().strftime('%H:%M'),
                        }
                        state['trump_catalyst'] = trump_entry

                        emoji = '🚀' if direction == 'LONG' else '💥'
                        trump_tag = ' 🇺🇸 TRUMP' if is_trump else ' 🌍 MAKRO'
                        tg_send(
                            f'{emoji}<b> MAKRO KATALYSATOR{trump_tag} — {direction} {strength}</b>\n'
                            f'"{headline[:100]}"\n'
                            f'Grund: {reason}\n'
                            f'⚡ Bitget Bot handelt ALLE Symbole {direction}!',
                            key=f'trump_{nid[:25]}'
                        )
                        # Auch in brain_news loggen
                        state['brain_news'] = ([{
                            'time':     datetime.now().strftime('%H:%M'),
                            'action':   direction, 'strength': strength,
                            'headline': headline[:80], 'tickers': ['ALL'],
                            'reason':   reason, 'ai': False,
                        }] + state.get('brain_news', []))[:20]

                    # Schnell-Filter für Einzel-Aktien News
                    hl = headline.lower()
                    keywords_check = (
                        'dilut','offering','earnings','beat','miss','guidance','fda',
                        'acquisition','merger','buyback','sec','doj','bankruptcy',
                        'downgrade','upgrade','contract','insider','chapter 11'
                    )
                    if not any(k in hl for k in keywords_check) or not symbols:
                        continue

                    decision = hermes_classify_news(headline, symbols)
                    action   = decision.get('action', 'IGNORE')
                    if action not in ('LONG', 'SHORT'):
                        continue

                    strength = decision.get('strength', 'NORMAL')
                    reason   = decision.get('reason', '')
                    icon     = '🟢' if action == 'LONG' else '🔴'

                    # News in State loggen (für UI)
                    brain_entry = {
                        'time':     datetime.now().strftime('%H:%M'),
                        'action':   action, 'strength': strength,
                        'headline': headline[:80], 'tickers': symbols,
                        'reason':   reason, 'ai': decision.get('ai', False),
                        '_ts':      time.time(),   # für Blackout-Check
                    }
                    state['brain_news'] = ([brain_entry] + state.get('brain_news', []))[:20]
                    # Blackout aktivieren bei hochriskanten Makro-Events
                    hl_low = headline.lower()
                    if any(w in hl_low for w in ('federal reserve','fomc','cpi report',
                                                  'nonfarm payroll','interest rate decision')):
                        state['trading_blackout'] = True
                        threading.Timer(1800, lambda: state.update({'trading_blackout': False})).start()

                    # Sofort-Alert
                    tg_send(
                        f'{icon} <b>HERMES BRAIN — {action} {strength}</b>\n'
                        f'"{headline[:80]}"\n'
                        f'Ticker: {", ".join(symbols)}\n'
                        f'Grund: {reason}\n'
                        f'<i>Scanne sofort...</i>',
                        key=f'brain_{nid[:25]}'
                    )

                    # Priority Queue — diese Ticker sofort scannen
                    _brain_prio_q.extend(symbols)

            # ── 3) Priority Queue verarbeiten → Sofort-Scan + Auto-Trade ─────
            if _brain_prio_q and is_active:
                pq = list(dict.fromkeys(_brain_prio_q[:6]))  # dedup, max 6
                _brain_prio_q.clear()

                def _do_priority_scan(tickers, _mkt=mkt):
                    from datetime import datetime as _dt, timedelta as _td
                    from scanner import scan_ticker as _st
                    today     = _dt.now().strftime('%Y-%m-%d')
                    exp_cut   = (_dt.now() + _td(days=35)).strftime('%Y-%m-%d')
                    news_cut  = (_dt.now() - _td(days=7)).strftime('%Y-%m-%d')
                    for sym in tickers:
                        try:
                            r = _st(sym, today, exp_cut, news_cut)
                            if not r or r.get('signal') not in ('LONG', 'SHORT'):
                                continue
                            score     = r.get('score', 0)
                            conviction= r.get('conviction', 0)
                            if score < 6:
                                continue

                            # Picks updaten (immer bei Score >= 6)
                            picks = state.get('hermes_picks', [])
                            if not any(p['t'] == sym for p in picks):
                                picks.insert(0, r)
                                state['hermes_picks'] = picks[:10]

                            # Telegram Signal
                            b    = r.get('best') or {}
                            sico = '🟢' if r['signal'] == 'LONG' else '🔴'
                            conv_str = f'{conviction:.0%}' if conviction else '?'
                            tg_send(
                                f'{sico} <b>BRAIN SCAN: {r["signal"]} {sym}</b>\n'
                                f'Score:{score} | Conviction:{conv_str}\n'
                                f'${r["price"]} | {r.get("kat_text","")[:60]}\n'
                                + (f'{r.get("otype","?")} ${b.get("strike")} @ ${b.get("pr")} '
                                   f'Exp:{b.get("exp")}' if b else ''),
                                key=f'bscan_{sym}_{_dt.now().strftime("%H%M")}'
                            )

                            # Auto-Trade bei hoher Conviction
                            if state.get('auto_trade_enabled') and score >= 12 and conviction >= 0.80:
                                hermes_auto_trade(r)          # Options
                                hermes_auto_trade_stock(r)    # Aktien direkt

                        except Exception:
                            pass

                threading.Thread(target=_do_priority_scan, args=(pq,), daemon=True).start()

            # Seen-Set begrenzen
            if len(_brain_seen_ids) > 2000:
                _brain_seen_ids = set(list(_brain_seen_ids)[-800:])

        except Exception:
            pass
        finally:
            state['brain_active'] = False

        time.sleep(90)


def vwap_reclaim_loop():
    """
    VWAP Reclaim/Break Scanner — alle 2 Min.
    LONG wenn Preis VWAP von unten kreuzt (Reclaim) mit Volumen.
    SHORT wenn Preis VWAP von oben bricht (Break) mit Volumen.
    """
    import time as _t
    _t.sleep(160)
    _prev_above = {}  # sym → bool: war letztes Mal über VWAP?

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            if now_utc.hour >= 22:
                _t.sleep(120)
                continue

            watch = list(state.get('channel_watch', []))[:15]
            ctx   = ssl.create_default_context()

            for sym in watch:
                try:
                    bars  = _fetch_bars(sym, '5Min', 80)
                    if len(bars) < 5:
                        continue
                    vwap  = _get_vwap(sym, bars)
                    if vwap <= 0:
                        continue

                    price = bars[-1]['c']
                    above = price > vwap
                    was   = _prev_above.get(sym)
                    _prev_above[sym] = above

                    if was is None:
                        continue

                    signal = None
                    if not was and above:
                        signal = 'LONG'   # Reclaim von unten
                    elif was and not above:
                        signal = 'SHORT'  # Break von oben

                    if not signal:
                        continue

                    vol_ratio = _get_volume_ratio(sym, bars)
                    if vol_ratio < 1.4:
                        continue  # zu wenig Volumen = kein echter Kreuzung

                    # 20-Min Cooldown pro Sym
                    prev_v = [s for s in state.get('vwap_signals', [])[-30:]
                              if s['sym'] == sym and s['signal'] == signal]
                    if prev_v and (_t.time() - prev_v[-1].get('ts', 0)) < 1200:
                        continue

                    entry = {
                        'sym':       sym, 'signal':    signal,
                        'price':     round(price, 2), 'vwap': round(vwap, 2),
                        'vol_ratio': vol_ratio,
                        'time':      datetime.now().strftime('%H:%M'),
                        'ts':        _t.time(),
                    }
                    state['vwap_signals'] = ([entry] + state.get('vwap_signals', []))[:40]

                    ico = '🟢' if signal == 'LONG' else '🔴'
                    tg_send(
                        f'{ico} <b>VWAP {signal}: {sym}</b>\n'
                        f'${price:.2f} {"über" if signal == "LONG" else "unter"} VWAP ${vwap:.2f}\n'
                        f'Volumen {vol_ratio:.1f}× Durchschnitt',
                        key=f'vwap_{sym}_{signal}_{datetime.now().strftime("%H%M")}'
                    )

                    if state.get('auto_trade_enabled') and not _is_blackout():
                        bull_tf, bear_tf = _get_mtf_bias(sym)
                        ok_long  = signal == 'LONG'  and bull_tf >= 2
                        ok_short = signal == 'SHORT' and bear_tf >= 2
                        if ok_long or ok_short:
                            hermes_auto_trade_stock(
                                {'t': sym, 'signal': signal, 'price': price,
                                 'score': 13, 'conviction': 0.78},
                                reason=f'VWAP {signal} Vol×{vol_ratio}'
                            )
                except Exception:
                    pass

        except Exception:
            pass
        _t.sleep(120)


def premarket_gap_loop():
    """
    Pre-Market Gap Scanner — 08:00-13:30 UTC (4-9:30 ET).
    Findet Aktien die >2% vorbörslich gappen.
    Gap UP mit News → Continuation LONG.
    Gap DOWN mit News → Continuation SHORT.
    Gap ohne klare News → Fade (gegen Gap traden).
    """
    import time as _t
    _t.sleep(240)
    _gapped_today = set()
    _last_date    = ''

    while True:
        try:
            now_utc  = datetime.now(timezone.utc)
            h, m     = now_utc.hour, now_utc.minute
            today    = datetime.now().strftime('%Y-%m-%d')

            if today != _last_date:
                _gapped_today.clear()
                _last_date = today

            # Pre-market: 08:00-13:30 UTC
            in_premarket = (8 <= h < 13) or (h == 13 and m < 30)
            if not in_premarket:
                _t.sleep(300)
                continue

            watch = list(state.get('channel_watch', []))

            for sym in watch:
                if sym in _gapped_today:
                    continue
                try:
                    bars_d = _fetch_bars(sym, '1Day', 3)
                    if len(bars_d) < 2:
                        continue
                    prev_close = bars_d[-2]['c']
                    if prev_close <= 0:
                        continue

                    # Pre-market Preis via Snapshot
                    ctx = ssl.create_default_context()
                    url = f'https://data.alpaca.markets/v2/stocks/snapshots?symbols={sym}&feed=iex'
                    req = urllib.request.Request(url, headers={
                        'APCA-API-KEY-ID': ALPACA_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET,
                    })
                    with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
                        snap = json.loads(r.read())
                    pre_price = float((snap.get(sym, {}).get('latestTrade') or {}).get('p', 0))
                    if pre_price <= 0:
                        continue

                    gap_pct = (pre_price - prev_close) / prev_close * 100

                    if abs(gap_pct) < 2.0:
                        continue

                    _gapped_today.add(sym)
                    direction = 'LONG' if gap_pct > 0 else 'SHORT'
                    abs_gap   = abs(gap_pct)

                    entry = {
                        'sym':        sym, 'gap_pct':   round(gap_pct, 2),
                        'prev_close': round(prev_close, 2),
                        'pre_price':  round(pre_price, 2),
                        'signal':     direction,
                        'time':       datetime.now().strftime('%H:%M'),
                        'ts':         _t.time(),
                    }
                    state['gap_signals'] = ([entry] + state.get('gap_signals', []))[:30]

                    ico  = '🟢' if direction == 'LONG' else '🔴'
                    fade = abs_gap < 4.0  # kleine Gaps füllen sich oft → Fade
                    tg_send(
                        f'{ico} <b>PRE-MARKET GAP {direction}: {sym}</b>\n'
                        f'{gap_pct:+.1f}% | Gestern Close ${prev_close:.2f} → Heute ${pre_price:.2f}\n'
                        f'{"📍 Fade möglich (Gap < 4%)" if fade else "🚀 Continuation wahrscheinlich"}',
                        key=f'gap_{sym}_{today}'
                    )

                except Exception:
                    pass

        except Exception:
            pass
        _t.sleep(180)


def channel_bounce_loop():
    """
    Kanal-Pattern Scanner — läuft alle 60s.
    Scannt alle Symbole in channel_watch auf Ping-Pong Muster.
    Erkennt automatisch Support/Resistance aus Preishistorie.
    Signal bei Berührung der Kanalgrenze → SHORT oben, LONG unten.
    """
    import time as _t
    _t.sleep(150)

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            h = now_utc.hour
            # Asia: 00-09 UTC | EU: 07-16 UTC | US+AfterHours: 13-22 UTC
            # Pause nur 22:00-00:00 UTC (kein Markt aktiv)
            if not (h < 22):
                _t.sleep(60)
                continue

            symbols = list(state.get('channel_watch', []))
            # Zusätzlich: aktuelle Hermes-Universe mit einbeziehen (top 30)
            universe = list(state.get('hermes_universe', set()))[:30]
            all_syms = list(dict.fromkeys(symbols + universe))  # dedupliziert, Reihenfolge bleibt

            ctx = ssl.create_default_context()
            active_channels = []

            for sym in all_syms:
                try:
                    # 40 Bars à 5 Minuten = ~3.3 Stunden Preishistorie
                    url = (f'https://data.alpaca.markets/v2/stocks/{sym}/bars'
                           f'?timeframe=5Min&limit=40&feed=iex&adjustment=raw')
                    req = urllib.request.Request(url, headers={
                        'APCA-API-KEY-ID':     ALPACA_KEY,
                        'APCA-API-SECRET-KEY': ALPACA_SECRET,
                    })
                    with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
                        bars = json.loads(r.read()).get('bars', [])

                    if len(bars) < 15:
                        continue

                    highs  = [b['h'] for b in bars]
                    lows   = [b['l'] for b in bars]
                    n      = len(bars)

                    # Resistance = 90. Percentil der Hochs, Support = 10. Percentil der Tiefs
                    highs_s    = sorted(highs)
                    lows_s     = sorted(lows)
                    resistance = highs_s[int(n * 0.90)]
                    support    = lows_s[int(n * 0.10)]
                    mid        = (resistance + support) / 2
                    rng_pct    = (resistance - support) / mid

                    # Kanal gültig: Range zwischen 0.3% und 3.5%
                    if not (0.003 <= rng_pct <= 0.035):
                        continue

                    # Mindestens 2x Berührung oben UND 2x unten — sonst kein Muster
                    tol         = mid * 0.005
                    touches_top = sum(1 for h in highs if h >= resistance - tol)
                    touches_bot = sum(1 for l in lows  if l <= support + tol)

                    if touches_top < 2 or touches_bot < 2:
                        continue

                    # Aktuellen Preis holen
                    snap_url = (f'https://data.alpaca.markets/v2/stocks/snapshots'
                                f'?symbols={sym}&feed=iex')
                    snap_req = urllib.request.Request(snap_url, headers={
                        'APCA-API-KEY-ID':     ALPACA_KEY,
                        'APCA-API-SECRET-KEY': ALPACA_SECRET,
                    })
                    with urllib.request.urlopen(snap_req, context=ctx, timeout=8) as r:
                        snap  = json.loads(r.read())
                    price = float((snap.get(sym, {}).get('latestTrade') or {}).get('p', 0))
                    if price <= 0:
                        continue

                    channel_entry = {
                        'sym':         sym,
                        'support':     round(support, 2),
                        'resistance':  round(resistance, 2),
                        'price':       round(price, 2),
                        'range_pct':   round(rng_pct * 100, 2),
                        'touches_top': touches_top,
                        'touches_bot': touches_bot,
                        'updated':     datetime.now().strftime('%H:%M'),
                    }
                    active_channels.append(channel_entry)

                    # Signal wenn Preis nahe Grenze (innerhalb 0.35%)
                    dist_top = (resistance - price) / price
                    dist_bot = (price - support)    / price
                    signal   = None

                    if dist_top <= 0.0035:
                        signal   = 'SHORT'
                        sl       = round(resistance * 1.006, 2)
                        tp       = round(support    + (resistance - support) * 0.15, 2)
                    elif dist_bot <= 0.0035:
                        signal   = 'LONG'
                        sl       = round(support    * 0.994, 2)
                        tp       = round(resistance - (resistance - support) * 0.15, 2)

                    if not signal:
                        continue

                    # 15-Minuten Cooldown pro Symbol+Richtung
                    prev = [s for s in state.get('channel_signals', [])[-40:]
                            if s['sym'] == sym and s['signal'] == signal]
                    if prev and (_t.time() - prev[-1].get('ts', 0)) < 900:
                        continue

                    rr    = round(abs(tp - price) / max(abs(sl - price), 0.01), 1)
                    entry = {
                        'sym':        sym,        'signal':     signal,
                        'price':      price,      'support':    round(support, 2),
                        'resistance': round(resistance, 2),
                        'sl':         sl,         'tp':         tp,
                        'rr':         rr,         'range_pct':  round(rng_pct * 100, 2),
                        'touches_top':touches_top,'touches_bot':touches_bot,
                        'time':       datetime.now().strftime('%H:%M'),
                        'ts':         _t.time(),
                    }
                    state['channel_signals'] = ([entry] + state.get('channel_signals', []))[:60]

                    ico = '🔴' if signal == 'SHORT' else '🟢'
                    tg_send(
                        f'{ico} <b>KANAL {signal}: {sym}</b>\n'
                        f'${price:.2f} | Support ${support:.2f} ↔ Resistance ${resistance:.2f}\n'
                        f'Range {rng_pct:.1%} | Top {touches_top}x Bot {touches_bot}x\n'
                        f'SL ${sl:.2f} | TP ${tp:.2f} | R/R {rr}:1',
                        key=f'ch_{sym}_{signal}_{datetime.now().strftime("%H%M")}'
                    )

                    if state.get('auto_trade_enabled'):
                        sig_obj = {
                            't': sym, 'signal': signal, 'price': price,
                            'score': 14, 'conviction': 0.82,
                        }
                        hermes_auto_trade_stock(sig_obj, reason=f'Kanal {signal} ${support:.2f}-${resistance:.2f}')

                except Exception:
                    pass

            state['channel_active'] = active_channels

        except Exception:
            pass

        _t.sleep(60)


def hermes_monitor():
    """
    Hermes Agent — läuft 24/7, alle 5 Min.
    Selbst-heilend: repariert Scans, Social-Daten, stuck-Zustände automatisch.
    Läuft unabhängig vom Scan-Status.
    """
    time.sleep(30)
    while True:
        try:
            # ── WATCHDOG: stuck hermes_running → reset + Self-Restart ─────────
            with _hermes_lock:
                h_since   = state.get('hermes_running_since')
                h_running = state.get('hermes_running', False)
                h_success = state.get('hermes_last_success')
                h_stuck_n = state.get('hermes_stuck_count', 0)

            if h_running and h_since:
                stuck_min = (datetime.now() - h_since).total_seconds() / 60
                if stuck_min > 20:
                    h_stuck_n += 1
                    with _hermes_lock:
                        state['hermes_running']       = False
                        state['hermes_running_since'] = None
                        state['hermes_stuck_count']   = h_stuck_n
                        state['hermes_force']         = True   # sofort neu starten
                    tg_send(f'🔄 <b>HERMES</b>: {stuck_min:.0f}min stuck → Reset & Neustart (#{h_stuck_n})')
                    # Letzter Ausweg: nach 10x stuck → kompletter Railway-Neustart
                    if h_stuck_n >= 10:
                        tg_send('🆘 <b>HERMES</b>: 10x stuck, nichts hilft → Railway Neustart')
                        os._exit(1)

            # Erfolgreicher Zyklus → Counter zurücksetzen
            elif not h_running and h_success:
                success_min = (datetime.now() - h_success).total_seconds() / 60
                if success_min < 10:
                    with _hermes_lock:
                        state['hermes_stuck_count'] = 0

            # ── HERMES SCANNER KONTROLLE & SELF-HEALING ──────────────────────
            data = state['results'] or load_results()
            now  = datetime.now()
            last = state.get('last_scan') or ''
            scan_age_h = 999
            if last:
                try:
                    scan_age_h = (now - datetime.strptime(last, '%Y-%m-%d %H:%M')).total_seconds() / 3600
                except Exception:
                    pass

            def _start_scan(reason):
                if not state['running'] and _scan_lock.acquire(blocking=False):
                    _scan_lock.release()
                    threading.Thread(target=run_scan_thread,
                                     kwargs={'trigger': f'hermes-{reason}'}, daemon=True).start()
                    tg_send(f'🔧 <b>HERMES</b>: Scan gestartet — {reason}')
                    return True
                return False

            # 1) Kein Scan vorhanden → sofort starten
            if not data:
                if _start_scan('kein-ergebnis'):
                    for _ in range(24):
                        time.sleep(10)
                        if not state['running']: break
                    continue

            # 2) Scan-Fehler → neu starten
            if state.get('error') and not state['running']:
                state['error'] = None
                if _start_scan('fehler-behoben'):
                    for _ in range(24):
                        time.sleep(10)
                        if not state['running']: break
                    continue

            # 3) Scan zu alt (>6h) → neu starten
            if scan_age_h > 6 and not state['running']:
                if _start_scan(f'scan-{scan_age_h:.0f}h-alt'):
                    for _ in range(24):
                        time.sleep(10)
                        if not state['running']: break
                    continue

            # 4) Scan hat 0 Ergebnisse (leer) → nur während Marktzeiten neu starten
            # After-Hours (vor 13:30 oder nach 21:00 UTC): KEIN Neustart — sonst Endlosschleife
            if data and not state['running']:
                if not data.get('longs') and not data.get('shorts') and not data.get('watch'):
                    now_utc_h = datetime.now(timezone.utc)
                    is_market_hours = (now_utc_h.hour >= 13 and now_utc_h.hour < 21)
                    last_empty_restart = state.get('_last_empty_restart', 0)
                    restart_cooldown_ok = (time.time() - last_empty_restart) > 3600  # max 1x/h
                    if is_market_hours and restart_cooldown_ok:
                        state['_last_empty_restart'] = time.time()
                        if _start_scan('leere-ergebnisse'):
                            for _ in range(24):
                                time.sleep(10)
                                if not state['running']: break
                            continue
                    # Intelligence Scan — 3x pro Tag FEST + kontinuierlich alle 2h wenn Scanner leer
                    _now_utc   = datetime.now(timezone.utc)
                    _h, _m     = _now_utc.hour, _now_utc.minute
                    _today_str = _now_utc.strftime('%Y-%m-%d')
                    # Feste Zeitfenster (UTC)
                    _windows   = [
                        (11, 0,  12, 30,  'Pre-Market'),   # 11:00-12:30 UTC = 7-8:30 AM ET
                        (16, 0,  17, 30,  'Mid-Day'),      # 16:00-17:30 UTC = 12-1:30 PM ET
                        (21, 0,  23, 59,  'After-Hours'),  # 21:00-24:00 UTC = 5-8 PM ET
                        (0,  0,  4,  0,   'Late-Night'),   # 00:00-04:00 UTC = Asien/Europa
                    ]
                    _in_window = next(
                        (label for h_s, m_s, h_e, m_e, label in _windows
                         if ((_h > h_s or (_h == h_s and _m >= m_s)) and
                             (_h < h_e or (_h == h_e and _m < m_e)))),
                        None
                    )
                    _last_intel    = state.get('_last_intel_scan', {})
                    _already_ran   = _last_intel.get(f'{_today_str}_{_in_window}', False) if _in_window else True
                    # Fallback: auch alle 2h neu scannen wenn Scanner noch leer
                    _last_any      = state.get('_last_intel_any', 0)
                    _needs_refresh = (time.time() - _last_any) > 7200  # 2h

                    if _in_window and (not _already_ran or _needs_refresh):
                        state.setdefault('_last_intel_scan', {})[f'{_today_str}_{_in_window}'] = True
                        state['_last_intel_any'] = time.time()
                        _window_label = _in_window
                        def _bg_intel_scan(label=_window_label):
                            try:
                                from scanner import hermes_afterhours_scan
                                extra = [s['sym'] for s in state.get('social_deep_raw', [])]
                                # Hermes Hunt Alerts auch als Extra-Kandidaten
                                hunt_syms = [a['ticker'] for a in state.get('hermes_alerts', [])
                                             if a.get('score', 0) >= 5]
                                extra = list(set(extra + hunt_syms))
                                ah_data = hermes_afterhours_scan(extra_tickers=extra)
                                if not ah_data:
                                    return
                                # Auch wenn leer: Hermes Hunt Alerts als Fallback einsetzen
                                if not ah_data.get('longs') and not ah_data.get('shorts'):
                                    alerts = state.get('hermes_alerts', [])
                                    if alerts:
                                        s_alerts = sorted(alerts, key=lambda x: -x.get('score',0))
                                        # LONG: explizit LONG-Richtung UND positiver Kurs heute
                                        longs_fb  = [a for a in s_alerts
                                                     if a.get('net_direction') == 'LONG'
                                                     and (a.get('prev_chg') or 0) > 0]
                                        # SHORT: explizit SHORT oder fallender Kurs (< -1%)
                                        shorts_fb = [a for a in s_alerts
                                                     if a.get('net_direction') == 'SHORT'
                                                     or (a.get('prev_chg') or 0) < -1]
                                        if ah_data.get('longs') is None: ah_data['longs'] = []
                                        if ah_data.get('shorts') is None: ah_data['shorts'] = []
                                        if longs_fb:
                                            ah_data['longs']  = [{'t':a['ticker'],'score':a['score'],'price':a.get('price',0),
                                                                   'reasons':a.get('reasons',[]),'signal':'LONG',
                                                                   'label':'Hermes Hunt','best':None}
                                                                  for a in longs_fb[:5]]
                                        if shorts_fb:
                                            ah_data['shorts'] = [{'t':a['ticker'],'score':a['score'],'price':a.get('price',0),
                                                                   'reasons':a.get('reasons',[]),'signal':'SHORT',
                                                                   'label':'Hermes Hunt','best':None}
                                                                  for a in shorts_fb[:5]]
                                # Label + Web-Scanner aktualisieren
                                ah_data['label'] = f'{label} Intelligence'
                                with _hermes_lock:
                                    cur = state.get('results') or {}
                                    cur['longs']   = ah_data.get('longs', [])
                                    cur['shorts']  = ah_data.get('shorts', [])
                                    cur['movers']  = ah_data.get('movers', [])
                                    cur['label']   = ah_data['label']
                                    cur['time']    = ah_data.get('time', datetime.now().strftime('%Y-%m-%d %H:%M'))
                                    state['results'] = cur
                                save_results(cur)
                            except Exception:
                                pass
                        threading.Thread(target=_bg_intel_scan, daemon=True).start()

                    # After-Hours: Social Deep Scan starten (Reddit + Stocktwits WHY)
                    last_social_scan = state.get('_last_social_deep_scan', 0)
                    if (time.time() - last_social_scan) > 1800:  # alle 30 Min
                        state['_last_social_deep_scan'] = time.time()
                        def _bg_social_deep():
                            try:
                                from scanner import get_social_deep_trending, analyze_social_smart_money
                                trending = get_social_deep_trending()
                                if not trending:
                                    return
                                state['social_deep_raw'] = trending

                                # Smart Money Analyse für jeden Trend-Stock
                                analyzed = analyze_social_smart_money(trending)
                                state['social_deep'] = analyzed

                                ts_now = datetime.now().strftime('%H:%M')
                                lines  = [f'<b>📱 SOCIAL TRENDING + SMART MONEY — {ts_now}</b>']

                                long_picks  = [a for a in analyzed if a['verdict'] == 'LONG']
                                short_picks = [a for a in analyzed if a['verdict'] == 'SHORT']
                                neutral     = [a for a in analyzed if a['verdict'] == 'NEUTRAL']

                                if long_picks:
                                    lines.append('\n<b>🟢 LONG — Smart Money bestätigt:</b>')
                                    for a in long_picks[:4]:
                                        why = ' · '.join(a.get('why', [])[:2])
                                        lines.append(
                                            f'  <b>{a["sym"]}</b> ${a.get("price",0):.0f}'
                                            f'  [{why}]  Score:+{a["bull_pts"]}/{a["bear_pts"]}'
                                        )
                                        if a.get('verdict_reason'):
                                            lines.append(f'  → {a["verdict_reason"][:80]}')
                                        if a.get('top_post'):
                                            lines.append(f'  <i>"{a["top_post"][:70]}"</i>')

                                if short_picks:
                                    lines.append('\n<b>🔴 SHORT — Trend erschöpft:</b>')
                                    for a in short_picks[:4]:
                                        why = ' · '.join(a.get('why', [])[:2])
                                        div = f' ⚠️{a["divergence"]}' if a.get('divergence') else ''
                                        lines.append(
                                            f'  <b>{a["sym"]}</b> ${a.get("price",0):.0f}'
                                            f'  [{why}]{div}  Score:{a["bull_pts"]}/{a["bear_pts"]}'
                                        )
                                        if a.get('verdict_reason'):
                                            lines.append(f'  → {a["verdict_reason"][:80]}')

                                # Nur Web-Scanner aktualisieren — kein Telegram
                            except Exception:
                                pass
                        threading.Thread(target=_bg_social_deep, daemon=True).start()

            # 5) Social-Daten fehlen → enrich neu starten
            if data and not state.get('social_data') and not state['running']:
                threading.Thread(target=enrich_background, args=(data,), daemon=True).start()

            # Hermes Analyse — läuft auch wenn Scan parallel läuft (nutzt letzte Ergebnisse)
            forced = state.pop('hermes_force', False)
            with _hermes_lock:
                already_running = state.get('hermes_running', False)
            if (not already_running or forced) and data:
                with _hermes_lock:
                    state['hermes_running'] = True
                    state['hermes_running_since'] = datetime.now()
                try:
                    from scanner import (hermes_hunt, scan_ticker, get_alpaca_market_news,
                                         hermes_24h_scan, get_macro_context, get_sec_alerts,
                                         get_market_sentiment, get_market_context)
                    POLY_KEY = os.environ.get('POLYGON_API_KEY', '')

                    # 0a) Makro + SEC + Markt-Sentiment im Hintergrund vorladen
                    def _bg_macro_sec():
                        try:
                            get_macro_context()
                            get_sec_alerts()
                        except Exception:
                            pass
                    def _bg_sentiment():
                        try:
                            sent = get_market_sentiment()
                            state['market_sentiment'] = sent
                        except Exception:
                            pass
                    def _bg_market_ctx():
                        try:
                            ctx = get_market_context()
                            state['market_context'] = ctx
                        except Exception:
                            pass
                    threading.Thread(target=_bg_macro_sec,  daemon=True).start()
                    threading.Thread(target=_bg_sentiment,  daemon=True).start()
                    threading.Thread(target=_bg_market_ctx, daemon=True).start()

                    # 0b) 24h Intelligence Scan — Polygon Gainers/Losers + Vol/OI + Dark Pool
                    def _bg_24h():
                        try:
                            sigs_24h = hermes_24h_scan()
                            state['hermes_24h'] = sigs_24h
                            top = [s for s in sigs_24h if s['score'] >= 7]
                            if top:
                                lines = [f'<b>🔍 HERMES 24H INTELLIGENCE — {datetime.now().strftime("%H:%M")}</b>']
                                for s in top[:5]:
                                    lines.append(f'<b>{s["sym"]}</b> ${s["price"]:.2f} {s["chg"]:+.1f}% Score:{s["score"]}')
                                    lines.append(f'  {s["reasons"][0] if s["reasons"] else ""}')
                                tg_send('\n'.join(lines))
                        except Exception:
                            pass
                    threading.Thread(target=_bg_24h, daemon=True).start()

                    # 0b2) IBKR Most-Traded Abgleich — wenn Bridge verbunden
                    def _bg_ibkr():
                        try:
                            ibkr = state.get('ibkr_data', {})
                            if ibkr and ibkr.get('connected'):
                                _run_ibkr_scan(ibkr)
                        except Exception:
                            pass
                    threading.Thread(target=_bg_ibkr, daemon=True).start()

                    # 0c) SEKTOR ROTATION MONITOR — wo geht das Geld hin?
                    def _bg_sector_rotation():
                        try:
                            import urllib.request as _ur, json as _js, ssl as _sl
                            _ctx = _sl.create_default_context()
                            _ctx.check_hostname = False
                            _ctx.verify_mode = _sl.CERT_NONE
                            SEKTOREN = {
                                'AI_CHIPS':   ['NVDA','AVGO','AMD','MRVL','SMH','TSM','QCOM','ARM','ALAB','INTC'],
                                'SOFTWARE':   ['MSFT','SNOW','NOW','DDOG','CRM','PLTR','ORCL','WDAY','ADBE','TEAM'],
                                'SPACE':      ['RKLB','ASTS','LUNR','RDW','BKSY','KTOS','GSAT','IRDM','MNTS','VSAT'],
                                'HEALTHCARE': ['XLV','LLY','UNH','ABBV','JNJ','PFE','MRK','AMGN'],
                                'ENERGIE':    ['XLE','XOM','CVX','USO','COP','SLB','MPC','VLO'],
                                'FINANZEN':   ['XLF','JPM','GS','BAC','MS','WFC','BLK','C'],
                                'DEFENSIV':   ['XLP','WMT','KO','PG','COST','MDLZ','CL'],
                                'GOLD':       ['GLD','GDX','SLV','NEM','AEM','WPM'],
                                'DEFENSE':    ['LMT','RTX','NOC','GD','BA','HII'],
                                'CRYPTO':     ['COIN','MSTR','HOOD','CLSK','RIOT'],
                                'MARKT':      ['QQQ','SPY','IWM','DIA','XLK'],
                            }
                            rotation = {}
                            for sektor, tickers in SEKTOREN.items():
                                chgs = []
                                for sym in tickers:
                                    try:
                                        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=3d'
                                        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
                                        with _ur.urlopen(req, context=_ctx, timeout=6) as r:
                                            d = _js.loads(r.read())
                                        meta = d['chart']['result'][0]['meta']
                                        price = float(meta.get('regularMarketPrice',0))
                                        prev  = float(meta.get('chartPreviousClose',price) or price)
                                        chg   = round((price-prev)/prev*100,2) if prev else 0
                                        chgs.append({'sym':sym,'price':price,'chg':chg})
                                    except Exception:
                                        pass
                                if chgs:
                                    avg = round(sum(x['chg'] for x in chgs)/len(chgs),2)
                                    rotation[sektor] = {'avg':avg,'tickers':chgs}
                            # Options OI für SPACE-Sektor nachladen
                            if 'SPACE' in rotation:
                                from collections import defaultdict as _dd
                                for t in rotation['SPACE']['tickers']:
                                    try:
                                        oi_url = (f'https://api.polygon.io/v3/snapshot/options/{t["sym"]}'
                                                  f'?limit=250&apiKey={POLY_KEY}')
                                        oi_req = _ur.Request(oi_url, headers={'User-Agent':'Mozilla/5.0'})
                                        with _ur.urlopen(oi_req, context=_ctx, timeout=6) as ir:
                                            id_ = _js.loads(ir.read())
                                        strikes = _dd(lambda: {'c': 0, 'p': 0})
                                        for opt in id_.get('results', []):
                                            det  = opt.get('details', {})
                                            oi   = opt.get('open_interest', 0) or 0
                                            stk  = det.get('strike_price', 0)
                                            otyp = det.get('contract_type', '')
                                            if stk <= 0 or oi <= 0: continue
                                            if otyp == 'call': strikes[stk]['c'] += oi
                                            elif otyp == 'put': strikes[stk]['p']  += oi
                                        tc = sum(v['c'] for v in strikes.values())
                                        tp = sum(v['p'] for v in strikes.values())
                                        if tc + tp > 0:
                                            t['call_oi']   = tc
                                            t['put_oi']    = tp
                                            t['pc']        = round(tp/tc, 2) if tc > 0 else 0
                                            t['call_wall'] = max(strikes, key=lambda k: strikes[k]['c']) if strikes else 0
                                            t['put_floor'] = max(strikes, key=lambda k: strikes[k]['p']) if strikes else 0
                                    except Exception:
                                        pass

                            state['sector_rotation'] = {
                                'ts': datetime.now().strftime('%H:%M'),
                                'data': rotation,
                            }

                            # Rotation Alert: wenn AI Chips fallen und anderer Sektor stark steigt
                            chips_chg = rotation.get('AI_CHIPS',{}).get('avg',0)
                            if chips_chg <= -3:
                                winners = [(s,v) for s,v in rotation.items()
                                           if s != 'AI_CHIPS' and v['avg'] >= 1.5]
                                winners.sort(key=lambda x: -x[1]['avg'])
                                if winners:
                                    lines = [f'<b>🔄 SEKTOR ROTATION ALERT — {datetime.now().strftime("%H:%M")}</b>']
                                    lines.append(f'AI Chips: {chips_chg:+.1f}% — Geld fliesst ab')
                                    lines.append('Profiteure:')
                                    for s, v in winners[:3]:
                                        top_t = sorted(v["tickers"], key=lambda x:-x["chg"])
                                        t_str = ', '.join(f'{x["sym"]} {x["chg"]:+.1f}%' for x in top_t[:4])
                                        lines.append(f'  {s}: {v["avg"]:+.1f}% ({t_str})')
                                    tg_send('\n'.join(lines))
                        except Exception:
                            pass
                    threading.Thread(target=_bg_sector_rotation, daemon=True).start()

                    # 0d) MAX PAIN — OpEx Pin-Zonen für High-OI Tickers
                    def _bg_max_pain():
                        try:
                            import urllib.request as _ur, json as _js, ssl as _sl
                            from collections import defaultdict
                            _ctx = _sl.create_default_context()
                            _ctx.check_hostname = False
                            _ctx.verify_mode = _sl.CERT_NONE
                            TODAY = datetime.now().strftime('%Y-%m-%d')
                            MP_TICKERS = ['SPY','QQQ','AAPL','TSLA','MSFT','AMD',
                                          'META','AMZN','GOOGL','COIN','PLTR','TSM']
                            results = []
                            for sym in MP_TICKERS:
                                try:
                                    url = (f'https://api.polygon.io/v3/snapshot/options/{sym}'
                                           f'?expiration_date={TODAY}&limit=250&apiKey={POLY_KEY}')
                                    req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
                                    with _ur.urlopen(req, context=_ctx, timeout=8) as r:
                                        d = _js.loads(r.read())
                                    opts = d.get('results', [])
                                    if not opts:
                                        continue
                                    # Aktuellen Preis holen
                                    price_url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}'
                                                 f'?interval=1d&range=3d')
                                    preq = _ur.Request(price_url, headers={'User-Agent':'Mozilla/5.0'})
                                    with _ur.urlopen(preq, context=_ctx, timeout=6) as pr:
                                        pd = _js.loads(pr.read())
                                    cur_price = float(pd['chart']['result'][0]['meta'].get('regularMarketPrice', 0))
                                    # OI nach Strike sammeln
                                    strikes = defaultdict(lambda: {'calls': 0, 'puts': 0})
                                    for opt in opts:
                                        det  = opt.get('details', {})
                                        oi   = opt.get('open_interest', 0) or 0
                                        stk  = det.get('strike_price', 0)
                                        otyp = det.get('contract_type', '')
                                        if stk <= 0 or oi <= 0: continue
                                        if otyp == 'call': strikes[stk]['calls'] += oi
                                        elif otyp == 'put': strikes[stk]['puts']  += oi
                                    if len(strikes) < 3:
                                        continue
                                    # Max Pain berechnen
                                    pain = {}
                                    for s in strikes:
                                        p = 0
                                        for k, v in strikes.items():
                                            if k < s: p += (s - k) * v['calls']
                                            if k > s: p += (k - s) * v['puts']
                                        pain[s] = p
                                    mp = min(pain, key=pain.get)
                                    # Top Call Wall + Put Floor
                                    call_wall  = max(strikes, key=lambda k: strikes[k]['calls'])
                                    put_floor  = max(strikes, key=lambda k: strikes[k]['puts'])
                                    total_c    = sum(v['calls'] for v in strikes.values())
                                    total_p    = sum(v['puts']  for v in strikes.values())
                                    pc         = round(total_p / total_c, 2) if total_c > 0 else 0
                                    diff       = round(cur_price - mp, 2)
                                    diff_pct   = round(diff / mp * 100, 2) if mp else 0
                                    # Pin Zone: 3 Strikes mit geringstem Pain
                                    pin_strikes = sorted(pain, key=pain.get)[:3]
                                    status = ('AT_PIN'      if abs(diff_pct) < 1.0 else
                                              'ABOVE_PAIN'  if diff > 0 else 'BELOW_PAIN')
                                    sentiment = ('BULLISH' if pc < 0.7 else
                                                 'BEARISH' if pc > 1.3 else 'NEUTRAL')
                                    results.append({
                                        'sym': sym, 'price': round(cur_price, 2),
                                        'max_pain': mp, 'diff': diff, 'diff_pct': diff_pct,
                                        'call_wall': call_wall, 'put_floor': put_floor,
                                        'pin_zone': [min(pin_strikes), max(pin_strikes)],
                                        'pc_ratio': pc, 'total_call_oi': total_c,
                                        'total_put_oi': total_p,
                                        'sentiment': sentiment, 'status': status,
                                    })
                                except Exception:
                                    pass
                            if results:
                                state['max_pain'] = {
                                    'ts': datetime.now().strftime('%H:%M'),
                                    'data': results,
                                }
                        except Exception:
                            pass
                    threading.Thread(target=_bg_max_pain, daemon=True).start()

                    # Big Money Bets — yfinance OTM Options Sweep (alle 15 Min, da langsam)
                    def _bg_big_money():
                        try:
                            from scanner import find_big_money_bets
                            bets = find_big_money_bets()
                            if bets:
                                state['big_money_bets'] = {
                                    'ts': datetime.now().strftime('%H:%M'),
                                    'data': bets,
                                }
                        except Exception:
                            pass
                    _bm_ts = state.get('big_money_bets', {}).get('ts', '')
                    _bm_age = 9999
                    if _bm_ts:
                        try:
                            _bm_h, _bm_m = map(int, _bm_ts.split(':'))
                            _now = datetime.now()
                            _bm_age = (_now.hour*60+_now.minute) - (_bm_h*60+_bm_m)
                        except Exception:
                            pass
                    if _bm_age > 15:
                        threading.Thread(target=_bg_big_money, daemon=True).start()

                    # 1) Alpaca Portfolio + Memory P&L — im Hintergrund (nicht blockieren)
                    def _bg_alpaca_mem():
                        try:
                            state['alpaca_portfolio'] = get_alpaca_portfolio()
                        except Exception:
                            pass
                        try:
                            memory_update_pl(POLY_KEY)
                            state['hermes_memory'] = load_memory()
                        except Exception:
                            pass
                    threading.Thread(target=_bg_alpaca_mem, daemon=True).start()
                    alpaca_data = state.get('alpaca_portfolio', {})
                    mem = load_memory()

                    # 3) Hermes Hunt — Polygon Movers + Dark Pool + Options Sweep
                    alerts = hermes_hunt(
                        data.get('longs',  []),
                        data.get('shorts', [])
                    )

                    # 3b) Market Fall Screener — wenn Markt fällt: zeige sensitivste Aktien
                    try:
                        from scanner import get_market_fall_candidates, get_market_context
                        _mkt = get_market_context()
                        _fall_cands = get_market_fall_candidates(_mkt)
                        state['market_fall_candidates'] = _fall_cands
                        state['market_fall_qqq']        = _mkt.get('qqq_chg', 0)
                        if _fall_cands and _mkt.get('qqq_chg', 0) <= -2.0:
                            top3 = _fall_cands[:3]
                            lines_fc = [f'<b>📉 MARKT FÄLLT {_mkt["qqq_chg"]:+.1f}% — SENSITIVSTE AKTIEN:</b>']
                            for fc in top3:
                                lines_fc.append(f'<b>{fc["sym"]}</b> {fc["chg"]:+.1f}% — {fc["reasons"][0] if fc["reasons"] else ""}')
                            tg_send('\n'.join(lines_fc), key=f'fall_{datetime.now().strftime("%H")}')
                    except Exception:
                        pass

                    # 4) Neue starke Signale in Memory speichern
                    for sig_r in data.get('longs', [])[:5] + data.get('shorts', [])[:3]:
                        try:
                            memory_track_signal(
                                sig_r['t'], sig_r['price'], sig_r['signal'],
                                sig_r['score'], [sig_r.get('kat_text','')[:60]]
                            )
                        except Exception:
                            pass

                    # 4b) MAG 7 Markt-Signal (Leading Indicator für NASDAQ)
                    try:
                        from scanner import get_mag7_market_signal
                        mag7 = get_mag7_market_signal()
                        state['mag7_signal'] = mag7
                        mag7_dir  = mag7.get('direction', 'MIXED')
                        mag7_conf = mag7.get('confidence', 0)
                        mag7_sum  = mag7.get('summary', '')
                        prev_mag7 = state.get('mag7_prev_dir', 'MIXED')

                        # Richtungswechsel im Mag7 → Telegram Alert
                        if mag7_dir != 'MIXED' and mag7_dir != prev_mag7 and prev_mag7 != 'MIXED':
                            flip_emoji = '🔴' if mag7_dir == 'BEAR' else '🟢'
                            tg_send(
                                f'{flip_emoji} <b>MAG7 RICHTUNGSWECHSEL</b>\n'
                                f'{prev_mag7} → {mag7_dir} ({mag7_conf:.0%} Konfidenz)\n'
                                f'{mag7_sum}\n'
                                f'Bull: {", ".join(mag7.get("leaders_bull",[])[:4])}\n'
                                f'Bear: {", ".join(mag7.get("leaders_bear",[])[:4])}\n'
                                f'<i>Leading Indicator — NASDAQ folgt 15-60 Min spaeter</i>',
                                key=f'mag7_{datetime.now().strftime("%H%M")}'
                            )
                        # Neue Divergenz (Mag7 bearish aber Markt noch oben)
                        elif mag7_dir == 'BEAR' and mag7_conf >= 0.7:
                            tg_send(
                                f'⚠️ <b>MAG7 BEAR SIGNAL {mag7_conf:.0%}</b>\n'
                                f'{mag7_sum}\n'
                                f'Bear: {", ".join(mag7.get("leaders_bear",[])[:5])}\n'
                                f'<i>NASDAQ könnte drehen — Positionen prüfen</i>',
                                key=f'mag7bear_{datetime.now().strftime("%H")}'
                            )
                        state['mag7_prev_dir'] = mag7_dir
                    except Exception:
                        mag7 = {}

                    # 5) Nachrichten — Polygon News + Alpaca Breaking
                    news_alerts = []
                    _seen = state.get('seen_news', set())
                    all_tickers = list({r['t'] for r in
                                       data.get('longs',[]) + data.get('shorts',[]) +
                                       data.get('movers',[])})
                    news_cutoff_h = (datetime.now(timezone.utc) -
                                     timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
                    for sym in all_tickers[:10]:
                        try:
                            url = f'https://api.polygon.io/v2/reference/news?ticker={sym}&limit=2&apiKey={POLY_KEY}'
                            with urllib.request.urlopen(urllib.request.Request(url),
                                                        context=ssl.create_default_context(), timeout=6) as r:
                                nd = json.loads(r.read())
                            for n in nd.get('results', []):
                                if n.get('published_utc', '') >= news_cutoff_h:
                                    entry = f'{sym}: {n.get("title","")[:60]}'
                                    if entry not in _seen:
                                        news_alerts.append(entry)
                                    break
                        except Exception:
                            pass

                    al_news = get_alpaca_market_news(limit=10)
                    al_breaking = []
                    from scanner import POS_KEYS, NEG_KEYS
                    _seen = state.get('seen_news', set())
                    for n in al_news:
                        h = n.get('headline', '')
                        if any(k in h.lower() for k in POS_KEYS + NEG_KEYS):
                            syms = n.get('symbols', [])
                            if syms:
                                entry = f'{",".join(syms[:2])}: {h[:55]}'
                                if entry not in _seen:
                                    al_breaking.append(entry)

                    # 6) Starke Hermes-Funde direkt scannen (Score >= 6)
                    today      = datetime.now().strftime('%Y-%m-%d')
                    exp_cutoff = (datetime.now() + timedelta(days=35)).strftime('%Y-%m-%d')
                    news_cutoff= (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
                    picks = []
                    for a in alerts:
                        if a['score'] >= 6:
                            try:
                                r = scan_ticker(a['ticker'], today, exp_cutoff, news_cutoff)
                                if r and r['signal'] in ('LONG', 'SHORT'):
                                    r['hermes_score']   = int(a['score'])
                                    r['hermes_reasons'] = [str(x) for x in a.get('reasons', [])]
                                    picks.append(r)
                                    memory_track_signal(r['t'], r['price'], r['signal'],
                                                        r['score'], r.get('hermes_reasons',[]))
                                elif r and a['score'] >= 9 and r.get('signal') == 'WATCH':
                                    # Starke institutionelle Flows erzwingen Signal
                                    # (Scanner sagt WATCH aber Hermes sieht riesige Sweep-Aktivität)
                                    net_dir  = a.get('net_direction', 'LONG')
                                    opt_type = 'CALL' if net_dir == 'LONG' else 'PUT'
                                    best_opt = r.get('best')
                                    if best_opt:
                                        r['signal']         = net_dir
                                        r['score']          = a['score']
                                        r['otype']          = opt_type
                                        r['hermes_score']   = a['score']
                                        r['hermes_reasons'] = [str(x) for x in a.get('reasons', [])]
                                        r['hermes_override'] = True
                                        picks.append(r)
                                        memory_track_signal(r['t'], r['price'], net_dir,
                                                            a['score'], r['hermes_reasons'])
                            except Exception:
                                pass

                    # 6b) Aschenbrenner-Positionen im Hintergrund scannen (blockiert Hermes nicht)
                    def _scan_asch(p_list):
                        ASCH_LONGS  = ['NBIS','KEEL','CLSK','RIOT','BTDR']
                        ASCH_SHORTS = ['NVDA','AVGO','AMD']
                        scan_map_t  = {r['t'] for r in data.get('longs',[]) + data.get('shorts',[]) + data.get('watch',[])}
                        for sym in ASCH_LONGS + ASCH_SHORTS:
                            if sym in scan_map_t:
                                continue
                            try:
                                r = scan_ticker(sym, today, exp_cutoff, news_cutoff)
                                if r and r['score'] >= 3:
                                    side = 'LONG' if sym in ASCH_LONGS else 'SHORT'
                                    r['hermes_score']   = r['score']
                                    r['hermes_reasons'] = [f'Aschenbrenner {side} Position']
                                    p_list.append(r)
                            except Exception:
                                pass
                    threading.Thread(target=_scan_asch, args=(picks,), daemon=True).start()

                    # 7) Auto-Trade — Aktien + Options bei starken Signalen
                    if state.get('auto_trade_enabled'):
                        # P&L offener brain_trades aktualisieren
                        try:
                            hermes_update_brain_trade_pnl()
                        except Exception:
                            pass
                        # Haupt-Scanner Signale: Aktien + Options
                        for sig_r in data.get('longs', [])[:5] + data.get('shorts', [])[:3]:
                            try:
                                hermes_auto_trade_stock(sig_r)
                                hermes_auto_trade(sig_r)
                            except Exception:
                                pass
                        # Hermes Picks (eigene Funde)
                        for sig_r in picks[:3]:
                            try:
                                hermes_auto_trade_stock(sig_r)
                                hermes_auto_trade(sig_r)
                            except Exception:
                                pass

                    # 7b) Universe erweitern
                    uni = state.get('hermes_universe', set())
                    state['hermes_universe'] = uni | {a['ticker'] for a in alerts if a['score'] >= 7}

                    # 8) Marktschluss: Selbst-Analyse + Lernschleife (20:00-20:30 UTC = 16 ET)
                    now_utc = datetime.now(timezone.utc)
                    is_close = (now_utc.hour == 20 and now_utc.minute < 15)
                    is_review = (now_utc.hour == 20 and 15 <= now_utc.minute < 30)
                    # EOD Auto-Close: 19:45 UTC = 15:45 ET (15 Min vor Schluss)
                    is_eod_close = (now_utc.hour == 19 and 44 <= now_utc.minute <= 50)
                    if is_eod_close and state.get('auto_trade_enabled'):
                        try:
                            hermes_close_all_stock_positions()
                        except Exception:
                            pass

                    # Self-Review + Strategy Builder: Hermes lernt und entwickelt eigene Regeln
                    if is_review:
                        def _bg_review():
                            try:
                                hermes_self_review(data, POLY_KEY)
                            except Exception:
                                pass
                            try:
                                hermes_strategy_builder(POLY_KEY)
                            except Exception:
                                pass
                        threading.Thread(target=_bg_review, daemon=True).start()
                    close_analysis = ''
                    if is_close:
                        mem_sigs = list(mem.get('signals', {}).values())
                        tracked = [s for s in mem_sigs if s.get('status') == 'open']
                        ap = alpaca_data
                        close_prompt = f"""Marktschluss-Analyse {datetime.now().strftime('%Y-%m-%d')}:

ALPACA PORTFOLIO: ${ap.get('equity',0):,.0f} | Cash: ${ap.get('cash',0):,.0f}
POSITIONEN: {[f"{p['sym']} {p['side']} P&L:{p['pl_pct']}%" for p in ap.get('positions',[])]}

SCANNER SIGNALE HEUTE: {[(s['sym'],s['signal'],s.get('current_pl_pct',0)) for s in tracked[:8]]}

Marktschluss-Zusammenfassung (Deutsch, max 150 Wörter):
1. Portfolio Performance heute
2. Beste und schlechteste Signale
3. Was morgen beachten?"""
                        close_analysis = _nous_call(close_prompt,
                            system='Du bist Hermes Trading Agent. Analysiere den Handelstag.',
                            max_tokens=400)
                        if close_analysis:
                            tg_send(f'📊 <b>HERMES MARKTSCHLUSS {datetime.now().strftime("%d.%m")}</b>\n\n{close_analysis}')
                            mem['market_closes'].append({'date': today, 'analysis': close_analysis,
                                                          'equity': ap.get('equity',0)})
                            save_memory(mem)

                    # 9) AI: Pro-Signal Tiefenbewertung (Top 3)
                    signal_evals = {}
                    top_signals = (data.get('longs',[])[:2] + data.get('shorts',[])[:1] + picks[:1])
                    for sig_r in top_signals[:3]:
                        try:
                            ev = hermes_ai_signal_eval(sig_r)
                            if ev:
                                signal_evals[sig_r['t']] = ev
                        except Exception:
                            pass

                    # 10) AI Marktüberblick (mit Alpaca-Kontext)
                    ai_text = hermes_ai_analysis(data, alerts)

                finally:
                    with _hermes_lock:
                        state['hermes_running']       = False
                        state['hermes_running_since'] = None
                        state['hermes_last_success']  = datetime.now()
                        state['hermes_stuck_count']   = 0

                prev_keys = {a['ticker'] for a in state.get('hermes_alerts', [])}
                new_finds = [a for a in alerts if a['ticker'] not in prev_keys]

                with _hermes_lock:
                    state['hermes_alerts']       = alerts
                    state['hermes_picks']        = picks
                    state['hermes_ts']           = datetime.now().strftime('%H:%M')
                    state['hermes_ai']           = ai_text
                    state['hermes_news']         = (news_alerts + al_breaking)[:10]
                    state['hermes_signal_evals'] = signal_evals
                    state['hermes_memory']       = load_memory()

                # Telegram — neue Funde + Breaking News
                if new_finds or news_alerts or ai_text:
                    ts = datetime.now().strftime('%H:%M')
                    lines = [f'<b>🤖 HERMES 24/7 — {ts}</b>']
                    if ai_text:
                        lines.append(f'\n<i>{ai_text}</i>')
                    if news_alerts:
                        lines.append(f'\n<b>Breaking News ({len(news_alerts)}):</b>')
                        for na in news_alerts[:3]:
                            lines.append(f'  📰 {na}')
                    if al_breaking:
                        lines.append(f'\n<b>Alpaca News:</b>')
                        for ab in al_breaking[:3]:
                            lines.append(f'  ⚡ {ab}')
                    if new_finds:
                        lines.append(f'\n<b>Neue Mover ({len(new_finds)}):</b>')
                        for a in new_finds[:4]:
                            lines.append(f'<b>{a["ticker"]}</b> Score:{a["score"]} — {a["reasons"][0][:45] if a["reasons"] else ""}')
                    if picks:
                        lines.append(f'\n<b>Hermes Picks ({len(picks)}):</b>')
                        for p in picks[:3]:
                            b = p.get('best') or {}
                            lines.append(f'{p["signal"]} <b>{p["t"]}</b> ${p["price"]}')
                            if b:
                                lines.append(f'  {p.get("otype")} ${b.get("strike")} @ ${b.get("pr")}  Exp:{b.get("exp")}')
                    tg_send('\n'.join(lines))
                    # Gesendete News als "gesehen" markieren
                    _seen.update(news_alerts)
                    _seen.update(al_breaking)
                    # Set nicht unbegrenzt wachsen lassen
                    if len(_seen) > 500:
                        state['seen_news'] = set(list(_seen)[-300:])
                    else:
                        state['seen_news'] = _seen

            # Wenn Scan läuft: alle 15s prüfen ob er fertig ist
            if state['running']:
                for _ in range(20):
                    time.sleep(15)
                    if not state['running']:
                        break
            else:
                time.sleep(300)   # Normal: 5 Min warten
        except Exception as _he:
            import traceback
            _err = traceback.format_exc()
            state['hermes_last_error'] = str(_he)
            with _hermes_lock:
                state['hermes_running'] = False
                state['hermes_running_since'] = None
            tg_send(f'⚠️ <b>HERMES FEHLER</b>: {str(_he)[:200]}')
            time.sleep(60)


def _build_strategy_signals():
    """Gibt die Top-5 LONG und SHORT Signale aus dem Strategy Monitor zurück."""
    sm     = state.get('strategy_monitor', [])
    ts     = state.get('strategy_monitor_ts', '')
    longs  = sorted([r for r in sm if r.get('strong_long')],
                    key=lambda x: (x['bull_total'], x['vol_ratio']), reverse=True)[:5]
    shorts = sorted([r for r in sm if r.get('strong_short')],
                    key=lambda x: (x['bear_total'], x['vol_ratio']), reverse=True)[:5]
    return {
        'longs':  longs,
        'shorts': shorts,
        'count':  len(sm),
        'ts':     ts,
    }


def _build_trump_catalyst():
    """Gibt Trump-Katalysator zurück wenn er < 20 Minuten alt ist, sonst None."""
    tc = state.get('trump_catalyst')
    if not tc:
        return None
    try:
        ts  = datetime.fromisoformat(tc['ts'])
        age = (datetime.now() - ts).total_seconds() / 60
        if age > 20:
            return None   # abgelaufen
        return {
            'direction': tc['direction'],
            'strength':  tc['strength'],
            'headline':  tc['headline'],
            'reason':    tc['reason'],
            'trump':     tc.get('trump', False),
            'ts':        tc['ts_str'],
            'age_min':   round(age, 1),
            'active':    True,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-AGENT KOMMUNIKATION — Hermes ↔ Bots ↔ Claude
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/hermes/intel')
def hermes_intel_api():
    """
    Haupt-Intel-Endpoint für Claude + Bots.
    Claude holt das vor jedem Trade: Marktbias, IBKR NAS/Gold, News, Brain-Status.
    """
    data   = state.get('results') or {}
    bwv    = state.get('brain_worldview', {})
    longs  = data.get('longs',  [])[:5]
    shorts = data.get('shorts', [])[:5]

    qqq_chg = bwv.get('qqq', 0)
    focus   = bwv.get('focus', 'BALANCED')
    ok_long  = focus not in ('SHORT_HUNT',) and qqq_chg > -2.0
    ok_short = focus not in ('LONG_HUNT',)  and qqq_chg < 2.0

    # Breaking News (Brain-Klassifizierungen)
    brain_news = state.get('brain_news', [])
    latest_news = brain_news[0].get('headline', '') if brain_news else ''
    recent_news = [{'headline': n['headline'][:80], 'action': n['action'],
                    'tickers': n['tickers'], 'time': n['time']}
                   for n in brain_news[:5]]

    # ── IBKR Daten gefiltert für NAS100 + Gold ────────────────────────────────
    ibkr = state.get('ibkr_data', {})
    nas_tickers  = {'QQQ', 'TQQQ', 'SQQQ', 'NVDA', 'TSLA', 'META', 'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'AMD', 'SPY', 'IWM'}
    gold_tickers = {'GLD', 'GDX', 'GDXJ', 'SLV', 'GC', 'XAUUSD', 'IAU', 'NEM', 'GOLD'}

    def _filter_ibkr(items, watch_set):
        return [i for i in (items or []) if i.get('sym','').upper() in watch_set][:5]

    ibkr_nas  = _filter_ibkr(ibkr.get('most_active', []) + ibkr.get('hot_options', []), nas_tickers)
    ibkr_gold = _filter_ibkr(ibkr.get('most_active', []) + ibkr.get('hot_options', []), gold_tickers)

    # IBKR Richtungs-Bias für NAS + Gold
    def _ibkr_bias(items):
        if not items: return 'NEUTRAL'
        bull = sum(1 for i in items if (i.get('chg') or 0) > 0)
        bear = len(items) - bull
        return 'BULLISH' if bull > bear else ('BEARISH' if bear > bull else 'NEUTRAL')

    ibkr_nas_bias  = _ibkr_bias(ibkr_nas)
    ibkr_gold_bias = _ibkr_bias(ibkr_gold)

    # ── Claude aktive Trading Session ────────────────────────────────────────
    claude_session = state.get('claude_session', {})
    claude_trades  = state.get('claude_trades', [])

    # ── Hermes Picks für NAS/Gold Kontext ─────────────────────────────────────
    nas_signals  = [r for r in longs + shorts if r.get('t') in nas_tickers][:3]
    gold_signals = [r for r in longs + shorts if r.get('t') in gold_tickers][:2]

    nas_summary  = ', '.join(f'{r["t"]}:{r["signal"]}({r["score"]})' for r in nas_signals) or 'kein Signal'
    gold_summary = ', '.join(f'{r["t"]}:{r["signal"]}({r["score"]})' for r in gold_signals) or 'kein Signal'

    # Market Fall Kandidaten
    fall_cands = [fc['sym'] for fc in state.get('market_fall_candidates', [])[:5]]

    return jsonify({
        # Markt-Übersicht
        'bias':          bwv.get('market', 'NEUTRAL'),
        'brain_focus':   focus,
        'qqq_chg':       round(qqq_chg, 2),
        'ok_long':       ok_long,
        'ok_short':      ok_short,
        # NAS100 spezifisch
        'nas': {
            'hermes_signals': nas_summary,
            'ibkr_bias':      ibkr_nas_bias,
            'ibkr_items':     ibkr_nas,
            'ok_long':        ok_long,
            'ok_short':       ok_short,
        },
        # Gold spezifisch
        'gold': {
            'hermes_signals': gold_summary,
            'ibkr_bias':      ibkr_gold_bias,
            'ibkr_items':     ibkr_gold,
        },
        # News
        'breaking_news': latest_news[:120],
        'recent_news':   recent_news,
        # Trump/Makro Katalysator (Bot handelt ALLE Symbole wenn aktiv)
        'trump_catalyst': _build_trump_catalyst(),
        # Strategy Monitor Pro — 5 Strategien (LSOB+GUSS+Dolphin+FIB882+OMS)
        'strategy_signals': _build_strategy_signals(),
        # Top Signale
        'top_longs':     [r['t'] for r in longs  if r.get('score', 0) >= 7][:5],
        'top_shorts':    [r['t'] for r in shorts if r.get('score', 0) >= 7][:5],
        'fall_candidates': fall_cands,
        # Claude Session
        'claude_session': claude_session,
        'claude_trades':  claude_trades[-5:],
        'claude_movers':  state.get('claude_movers', {}),
        'hermes_ts':     state.get('hermes_ts', ''),
        'timestamp':     datetime.now().strftime('%H:%M'),
    })


@app.route('/hermes/bot-status', methods=['POST'])
def hermes_bot_status():
    """
    Bots melden ihre offenen Positionen + letzten Trade an Hermes.
    Hermes kann dann gezielt News für diese Positionen überwachen.
    Body: {bot: 'bitget'|'mt5', positions: [...], last_trade: {...}}
    """
    body = request.get_json(force=True) or {}
    bot  = body.get('bot', 'unknown')
    pos  = body.get('positions', [])
    lt   = body.get('last_trade', {})

    if not state.get('bot_positions'):
        state['bot_positions'] = {}
    state['bot_positions'][bot] = {
        'positions':   pos,
        'last_trade':  lt,
        'updated':     datetime.now().strftime('%H:%M'),
    }

    # Hermes überwacht nun gezielt diese Ticker für News
    watch_tickers = list({p.get('sym', p.get('symbol', ''))
                          for p in pos if p.get('sym') or p.get('symbol')})
    if watch_tickers:
        existing = list(state.get('bot_watch_tickers', set()))
        state['bot_watch_tickers'] = set(existing + watch_tickers)

    return jsonify({'ok': True, 'watching': watch_tickers})


@app.route('/hermes/share', methods=['POST'])
def hermes_share():
    """
    Claude oder andere Agenten teilen Daten mit Hermes.
    Kann Chart-Daten, externe Signale, MT5 Kontext etc. enthalten.
    Body: {source: 'claude'|'mt5', type: 'chart'|'signal'|'context', data: {...}}
    """
    body   = request.get_json(force=True) or {}
    source = body.get('source', 'unknown')
    dtype  = body.get('type', 'info')
    data   = body.get('data', {})

    if not state.get('shared_intel'):
        state['shared_intel'] = []
    state['shared_intel'] = ([{
        'source': source, 'type': dtype, 'data': data,
        'ts': datetime.now().strftime('%H:%M'),
    }] + state.get('shared_intel', []))[:20]

    # Wenn Chart-Daten für NAS100/Gold → in Brain-Kontext einbauen
    symbol = data.get('symbol', '')
    if dtype == 'chart' and symbol:
        direction = data.get('direction', '')
        reason    = data.get('reason', '')[:80]
        if direction in ('LONG', 'SHORT'):
            _brain_prio_q.append(symbol.replace('NAS100', 'QQQ').replace('XAUUSD', 'GLD'))
            tg_send(
                f'🔗 <b>HERMES ← {source.upper()}</b>\n'
                f'{symbol}: {direction} | {reason}\n'
                f'Hermes scannt zur Bestätigung...',
                key=f'share_{source}_{symbol}'
            )

    return jsonify({'ok': True, 'received': {'source': source, 'type': dtype}})


@app.route('/hermes/shared-intel')
def hermes_shared_intel():
    """Zeigt was Bots + Claude an Hermes gesendet haben."""
    return jsonify(state.get('shared_intel', []))


# ── Claude ↔ Hermes direkte Kommunikation ────────────────────────────────────

@app.route('/hermes/claude-push', methods=['POST'])
def hermes_claude_push():
    """
    Claude pusht IBKR-Daten + Trades an Hermes während einer Trading-Session.
    Claude hat IBKR-Zugang, Hermes nicht — so bekommt Hermes 2 Broker-Quellen.

    Body: {
      'ibkr': {                         # IBKR Live-Daten von Claude
        'nas_price': 21450,             # aktueller NAS100 Preis
        'nas_chg': -0.5,                # % Veränderung
        'gold_price': 2340,
        'gold_chg': 0.3,
        'positions': [...],             # offene IBKR Positionen
        'account_value': 2050,          # Kontostand
        'orders': [...],                # ausstehende Orders
        'market_context': '...',        # freie Beschreibung
      },
      'trade': {                        # optional: neuer Trade gemeldet
        'symbol': 'NAS100',
        'direction': 'LONG',
        'entry': 21450,
        'lots': 1.0,
        'reason': 'BOS + Fib 0.618',
        'stop': 21380,
        'target': 21600,
      },
      'session_note': 'Morning session, watching NAS support',
    }
    """
    body   = request.get_json(force=True) or {}
    ibkr   = body.get('ibkr', {})
    trade  = body.get('trade', {})
    note   = body.get('session_note', '')
    movers = body.get('movers', {})
    ts     = datetime.now().strftime('%H:%M')

    # IBKR Mover-Daten speichern (mit Timestamp)
    if movers:
        state['claude_movers'] = {**movers, 'updated': ts}

    # IBKR-Daten speichern (Hermes liest diese beim nächsten Intel-Request)
    if ibkr:
        state['claude_ibkr'] = {**ibkr, 'updated': ts}

        # In Session-Status einbauen
        session = state.get('claude_session', {})
        session.update({
            'active':         True,
            'last_push':      ts,
            'note':           note or session.get('note', ''),
            'nas_price':      ibkr.get('nas_price'),
            'nas_chg':        ibkr.get('nas_chg'),
            'gold_price':     ibkr.get('gold_price'),
            'gold_chg':       ibkr.get('gold_chg'),
            'account_value':  ibkr.get('account_value'),
            'market_context': ibkr.get('market_context', ''),
        })
        state['claude_session'] = session

    # Neuer Trade von Claude → speichern + Brain informieren
    if trade and trade.get('symbol') and trade.get('direction'):
        sym = trade['symbol'].upper()
        entry = {
            'symbol':    sym,
            'direction': trade['direction'],
            'entry':     trade.get('entry'),
            'lots':      trade.get('lots'),
            'reason':    trade.get('reason', '')[:80],
            'stop':      trade.get('stop'),
            'target':    trade.get('target'),
            'ts':        ts,
            'status':    'OPEN',
            'pnl':       0,
        }
        state['claude_trades'] = ([entry] + state.get('claude_trades', []))[:20]

        # Brain Priority Queue → Hermes überwacht den Ticker sofort
        watch_sym = sym.replace('NAS100', 'QQQ').replace('XAUUSD', 'GLD')
        _brain_prio_q.append(watch_sym)

        tg_send(
            f'📊 <b>CLAUDE TRADE → HERMES</b>\n'
            f'{sym}: {trade["direction"]} @ {trade.get("entry","?")}\n'
            f'Lots: {trade.get("lots","?")} | Stop: {trade.get("stop","?")} | TP: {trade.get("target","?")}\n'
            f'Grund: {trade.get("reason","")}\n'
            f'Hermes überwacht ab sofort.',
            key=f'claude_trade_{sym}'
        )

    return jsonify({
        'ok':           True,
        'ibkr_stored':  bool(ibkr),
        'trade_stored': bool(trade),
        'ts':           ts,
    })


@app.route('/hermes/claude-session')
def hermes_claude_session():
    """
    Claude holt am Morgen vor dem Trading den vollständigen Hermes-Kontext.
    Gibt alles zurück was Claude braucht: Marktbias, NAS/Gold IBKR-Daten,
    Brain-News, aktive Trades, Hermes-Einschätzung für heute.
    """
    data    = state.get('results') or {}
    bwv     = state.get('brain_worldview', {})
    longs   = data.get('longs',  [])[:10]
    shorts  = data.get('shorts', [])[:10]
    brain_n = state.get('brain_news', [])

    qqq_chg = bwv.get('qqq', 0)
    focus   = bwv.get('focus', 'BALANCED')

    # NAS100-relevante Signale
    nas_set  = {'QQQ', 'TQQQ', 'SQQQ', 'NVDA', 'TSLA', 'META', 'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'AMD', 'SPY'}
    gold_set = {'GLD', 'GDX', 'GDXJ', 'SLV', 'IAU', 'NEM', 'GOLD'}

    nas_longs  = [r for r in longs  if r.get('t') in nas_set][:4]
    nas_shorts = [r for r in shorts if r.get('t') in nas_set][:4]
    gold_sigs  = [r for r in longs + shorts if r.get('t') in gold_set][:3]

    # Brain-News nach NAS/Gold filtern
    def _news_for(ticker_set):
        return [n for n in brain_n if any(t in ticker_set for t in n.get('tickers', []))][:5]

    nas_news  = _news_for(nas_set | {'NAS100', 'NASDAQ', 'QQQ', 'NDX'})
    gold_news = _news_for(gold_set | {'GOLD', 'XAUUSD', 'XAU'})

    # IBKR-Daten von Claude (falls schon gepusht)
    claude_ibkr = state.get('claude_ibkr', {})

    # Hermes Handlungsempfehlung für Claude
    rec_nas = 'LONG' if focus in ('LONG_HUNT', 'LONG_LEAN') and qqq_chg > -1 else \
              'SHORT' if focus in ('SHORT_HUNT', 'SHORT_LEAN') or qqq_chg < -1 else 'WARTEN'
    rec_gold = 'LONG' if any(r.get('signal') == 'LONG' for r in gold_sigs) else \
               'SHORT' if any(r.get('signal') == 'SHORT' for r in gold_sigs) else 'NEUTRAL'

    # Alle Brain-News letzten 2h
    recent_all = brain_n[:10]

    return jsonify({
        'hermes_briefing': {
            'market_focus':   focus,
            'market_bias':    bwv.get('market', 'NEUTRAL'),
            'qqq_chg':        round(qqq_chg, 2),
            'market_reason':  bwv.get('reason', ''),
            'recommendation': {
                'nas100':  rec_nas,
                'gold':    rec_gold,
            },
        },
        'nas100': {
            'hermes_longs':  [{'t': r['t'], 'score': r.get('score'), 'reasons': r.get('reasons',[])} for r in nas_longs],
            'hermes_shorts': [{'t': r['t'], 'score': r.get('score'), 'reasons': r.get('reasons',[])} for r in nas_shorts],
            'news':          nas_news,
            'ibkr_price':    claude_ibkr.get('nas_price'),
            'ibkr_chg':      claude_ibkr.get('nas_chg'),
        },
        'gold': {
            'hermes_signals': [{'t': r['t'], 'signal': r.get('signal'), 'score': r.get('score')} for r in gold_sigs],
            'news':           gold_news,
            'ibkr_price':     claude_ibkr.get('gold_price'),
            'ibkr_chg':       claude_ibkr.get('gold_chg'),
        },
        'brain_news_recent': [{'headline': n['headline'][:100], 'action': n['action'],
                                'tickers': n['tickers'], 'time': n['time']} for n in recent_all],
        'claude_trades_open': [t for t in state.get('claude_trades', []) if t.get('status') == 'OPEN'],
        'claude_ibkr':        claude_ibkr,
        'fall_candidates':    [fc['sym'] for fc in state.get('market_fall_candidates', [])[:5]],
        'hermes_ts':          state.get('hermes_ts', ''),
        'timestamp':          datetime.now().strftime('%H:%M'),
    })


@app.route('/alpaca/order', methods=['POST'])
def alpaca_order_api():
    """Hermes platziert Order auf Alpaca Paper. Body: {sym, qty, side}"""
    body = request.get_json(force=True) or {}
    sym  = body.get('sym','').upper()
    qty  = int(body.get('qty', 1))
    side = body.get('side','buy').lower()
    if not sym or side not in ('buy','sell'):
        return jsonify({'ok': False, 'msg': 'sym + side (buy/sell) erforderlich'})
    result = alpaca_order(sym, qty, side)
    return jsonify({'ok': 'id' in result, 'result': result})

@app.route('/alpaca/portfolio')
def alpaca_portfolio_api():
    return jsonify(get_alpaca_portfolio())

# ── IBKR Bridge — empfängt Daten vom lokalen ibkr_bridge.py ─────────────────
@app.route('/ibkr/push', methods=['POST'])
def ibkr_push():
    """ibkr_bridge.py sendet alle 60s Scanner + Account-Daten."""
    auth = request.headers.get('Authorization', '')
    if auth != f'Bearer {IBKR_BRIDGE_TOKEN}':
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(force=True) or {}
    data['received_at'] = datetime.now().strftime('%H:%M:%S')
    state['ibkr_data']      = data
    state['ibkr_ts']        = datetime.now().strftime('%H:%M')
    state['ibkr_account']   = data.get('account', {})
    state['ibkr_positions'] = data.get('positions', [])
    state['ibkr_watchlist'] = data.get('watchlist', {})
    try:
        _run_ibkr_scan(data)
    except Exception:
        pass
    return jsonify({'ok': True, 'received': len(data.get('most_active', []))})


@app.route('/ibkr/data')
def ibkr_data_get():
    return jsonify(state.get('ibkr_data') or {'connected': False, 'error': 'Bridge nicht verbunden'})


@app.route('/ibkr/account')
def ibkr_account_get():
    """Account-Übersicht: Balance + Positionen + Watchlist."""
    connected = bool(state.get('ibkr_data', {}).get('connected'))
    return jsonify({
        'connected':  connected,
        'ts':         state.get('ibkr_ts', '—'),
        'account':    state.get('ibkr_account', {}),
        'positions':  state.get('ibkr_positions', []),
        'watchlist':  state.get('ibkr_watchlist', {}),
    })


@app.route('/ibkr/scan')
def ibkr_scan_get():
    return jsonify(state.get('ibkr_scan') or [])


def _run_ibkr_scan(ibkr_data: dict):
    """
    Vergleicht IBKR Most-Active mit letzten Scanner-Ergebnissen.
    Erstellt IBKR-Confirmed Signal-Liste mit Richtung + Grund.
    """
    if not ibkr_data:
        return
    results = state.get('results') or {}
    all_signals = (results.get('longs', []) + results.get('shorts', []) +
                   results.get('movers', []) + results.get('watch', []))
    signal_lookup = {r['t']: r for r in all_signals if r.get('t')}

    ibkr_scan = []
    all_ibkr = (
        [(s, 'MOST_ACTIVE')  for s in ibkr_data.get('most_active', [])[:20]] +
        [(s, 'HOT_OPTIONS')  for s in ibkr_data.get('hot_options',  [])[:20]] +
        [(s, 'TOP_GAINER')   for s in ibkr_data.get('top_gainers',  [])[:10]] +
        [(s, 'TOP_LOSER')    for s in ibkr_data.get('top_losers',   [])[:10]]
    )
    seen = set()
    for item, category in all_ibkr:
        sym  = item.get('sym', '')
        if not sym or sym in seen:
            continue
        seen.add(sym)
        chg  = item.get('chg', 0)
        vol  = item.get('volume', 0)
        price = item.get('price', 0)

        # Richtung aus IBKR-Daten + Scanner-Signal
        sig_r = signal_lookup.get(sym)
        if sig_r:
            direction  = sig_r.get('signal', 'WATCH')
            score      = sig_r.get('score', 0)
            kat        = sig_r.get('kat_text', '')[:80]
            conviction = sig_r.get('conviction', 0)
            basis      = 'IBKR_POLYGON_CONFIRMED'
            score      = min(15, score + 2)
        else:
            # Kein Scanner-Signal → Richtung aus IBKR-Preisbewegung ableiten
            if category in ('TOP_GAINER',) or chg >= 3:
                direction, score, conviction = 'LONG', 4, 0.45
            elif category in ('TOP_LOSER',) or chg <= -3:
                direction, score, conviction = 'SHORT', 4, 0.45
            elif category == 'HOT_OPTIONS':
                direction, score, conviction = 'WATCH', 3, 0.35
            else:
                direction, score, conviction = 'WATCH', 2, 0.30
            kat   = f'IBKR {category}: {chg:+.1f}% | Vol {int(vol):,}'
            basis = 'IBKR_ONLY'

        # Grund formulieren
        reason_parts = [f'IBKR #{list(seen).index(sym)+1} {category}']
        if chg:
            reason_parts.append(f'{chg:+.1f}% heute')
        if vol:
            reason_parts.append(f'Vol {int(vol/1000):.0f}K')
        if sig_r:
            reason_parts.append(kat[:60])

        ibkr_scan.append({
            'sym':        sym,
            'price':      price,
            'chg':        round(chg, 2),
            'volume':     int(vol),
            'category':   category,
            'direction':  direction,
            'score':      score,
            'conviction': round(conviction, 2),
            'basis':      basis,
            'reason':     ' | '.join(reason_parts),
            'sources':    ['IBKR'] + (['Polygon'] if sig_r else []) + (['Alpaca'] if sig_r else []),
            'has_signal': sig_r is not None,
        })

    # Sortierung: IBKR_POLYGON_CONFIRMED + Score zuerst
    ibkr_scan.sort(key=lambda x: (0 if x['basis']=='IBKR_POLYGON_CONFIRMED' else 1, -x['score']))
    state['ibkr_scan'] = ibkr_scan[:30]

    # Telegram Alert für Top IBKR Confirmed Signale
    top = [s for s in ibkr_scan if s['basis'] == 'IBKR_POLYGON_CONFIRMED' and s['score'] >= 7]
    if top:
        lines = [f'<b>🏦 IBKR + POLYGON BESTÄTIGT — {datetime.now().strftime("%H:%M")}</b>']
        for s in top[:4]:
            emoji = '🟢' if s['direction'] == 'LONG' else '🔴' if s['direction'] == 'SHORT' else '🟡'
            lines.append(f'{emoji} <b>{s["sym"]}</b> ${s["price"]:.2f} {s["chg"]:+.1f}% Score:{s["score"]}')
            lines.append(f'   {s["reason"][:80]}')
        tg_send('\n'.join(lines))


# ── MT5 Bot Monitor ──────────────────────────────────────────────────────────
@app.route('/mt5/status', methods=['POST'])
def mt5_status_post():
    """MT5 Bot sendet seinen Status alle 60s hierher."""
    data = request.get_json(force=True) or {}
    data['received_at'] = datetime.now().strftime('%H:%M:%S')
    state['mt5_status'] = data
    return jsonify({'ok': True})

@app.route('/mt5/status')
def mt5_status_get():
    return jsonify(state.get('mt5_status') or {'error': 'Noch kein MT5 Status empfangen'})

@app.route('/hermes/memory')
def hermes_memory_api():
    return jsonify(load_memory())

@app.route('/hermes/autotrade', methods=['POST'])
def hermes_autotrade_toggle():
    """Toggle Auto-Trading on/off."""
    body = request.get_json(force=True) or {}
    enabled = body.get('enabled', not state.get('auto_trade_enabled', False))
    state['auto_trade_enabled'] = bool(enabled)
    status = 'EIN' if enabled else 'AUS'
    tg_send(f'🤖 <b>HERMES AUTO-TRADE {status}</b> — Score >= {AUTO_TRADE_MIN_SCORE}, ${AUTO_TRADE_AMOUNT} pro Trade')
    return jsonify({'ok': True, 'enabled': enabled, 'min_score': AUTO_TRADE_MIN_SCORE, 'amount': AUTO_TRADE_AMOUNT})

@app.route('/hermes/autotrade')
def hermes_autotrade_status():
    trades = state.get('auto_trades', [])
    return jsonify({
        'enabled':   state.get('auto_trade_enabled', False),
        'min_score': AUTO_TRADE_MIN_SCORE,
        'amount':    AUTO_TRADE_AMOUNT,
        'trades':    trades[-20:],
    })

@app.route('/hermes/channels')
def hermes_channels():
    """Zeigt alle Aktien im Kanal-Muster + letzte Signale."""
    return jsonify({
        'active':  state.get('channel_active', []),
        'signals': state.get('channel_signals', [])[:20],
        'watch':   state.get('channel_watch', []),
    })

@app.route('/hermes/channels/add', methods=['POST'])
def hermes_channel_add():
    sym = (request.json or {}).get('sym', '').upper().strip()
    if sym and sym not in state.get('channel_watch', []):
        state.setdefault('channel_watch', []).insert(0, sym)
    return jsonify({'ok': True, 'watch': state.get('channel_watch', [])})

@app.route('/hermes/channels/remove', methods=['POST'])
def hermes_channel_remove():
    sym = (request.json or {}).get('sym', '').upper().strip()
    state['channel_watch'] = [s for s in state.get('channel_watch', []) if s != sym]
    return jsonify({'ok': True, 'watch': state.get('channel_watch', [])})

@app.route('/hermes/close-trades', methods=['POST'])
def hermes_close_trades_api():
    """Schließt alle offenen Stock-Positionen manuell."""
    try:
        hermes_close_all_stock_positions()
        hermes_update_brain_trade_pnl()
        state['alpaca_portfolio'] = get_alpaca_portfolio()
        return jsonify({'ok': True, 'msg': 'Positionen geschlossen'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:100]})

@app.route('/hermes/learning')
def hermes_learning_api():
    learn = load_learning()
    return jsonify(learn)

@app.route('/hermes/learning/review', methods=['POST'])
def hermes_force_review():
    """Startet Selbst-Analyse manuell."""
    data = state['results'] or load_results()
    if not data:
        return jsonify({'ok': False, 'msg': 'Kein Scan vorhanden'})
    POLY_KEY = os.environ.get('POLYGON_API_KEY', '')
    threading.Thread(target=hermes_self_review, args=(data, POLY_KEY), daemon=True).start()
    return jsonify({'ok': True, 'msg': 'Selbst-Analyse gestartet'})

@app.route('/hermes/trigger', methods=['POST'])
def hermes_trigger():
    """Startet Hermes sofort — für Tests und Reparatur."""
    with _hermes_lock:
        already = state.get('hermes_running', False)
    if already:
        return jsonify({'ok': False, 'msg': 'Hermes läuft bereits'})
    data = state['results'] or load_results()
    if not data:
        return jsonify({'ok': False, 'msg': 'Keine Scan-Ergebnisse vorhanden'})
    state['hermes_force'] = True
    return jsonify({'ok': True, 'msg': 'Hermes wird beim nächsten Tick gestartet (<30s)'})


@app.route('/fib-scan')
def fib_scan():
    from scanner import fibonacci_scan
    try:
        results = fibonacci_scan()
        state['fib_results']   = results
        state['fib_last_scan'] = datetime.now().strftime('%H:%M')

        # Auto-Trade: AT_882 / NEAR_882 direkt handeln wenn aktiviert
        traded = []
        if state.get('auto_trade_enabled'):
            today = datetime.now().strftime('%Y-%m-%d')
            already_today = {t['sym'] for t in state.get('fib_trades', []) if t.get('date') == today}
            for r in results:
                sig = r.get('signal', '')
                sym = r.get('sym', '')
                if sig not in ('AT_882', 'NEAR_882') or sym in already_today:
                    continue
                direction  = 'SHORT' if r.get('rejection') else 'LONG'
                score      = (14 if sig == 'AT_882' else 11) + (2 if r.get('bounce') else 0)
                vol_ratio  = r.get('vol_ratio', 1.0)
                conviction = min(0.95, 0.70 + (vol_ratio - 1.0) * 0.1)
                reason     = f'Fib {sig} | {"Bounce" if r.get("bounce") else "Rejection" if r.get("rejection") else "Level"} | Vol×{vol_ratio:.1f}'
                fib_signal = {'t': sym, 'price': r['price'], 'signal': direction,
                              'score': score, 'conviction': round(conviction, 2), 'top_reason': reason}
                ok = hermes_auto_trade_stock(fib_signal, reason=reason)
                if ok:
                    state['fib_trades'] = ([{
                        'sym': sym, 'signal': direction, 'score': score,
                        'fib_level': '88.2%', 'fib_signal': sig,
                        'price': r['price'], 'reason': reason,
                        'time': datetime.now().strftime('%H:%M'), 'date': today,
                    }] + state.get('fib_trades', []))[:50]
                    traded.append(sym)

        return jsonify({'results': results, 'count': len(results),
                        'traded': traded,
                        'time': datetime.now().strftime('%Y-%m-%d %H:%M')})
    except Exception as e:
        return jsonify({'error': str(e), 'results': []}), 500


@app.route('/hermes/alerts')
def hermes_alerts_api():
    with _hermes_lock:
        return jsonify({
            'alerts':       state['hermes_alerts'],
            'ts':           state['hermes_ts'],
            'running':      state['hermes_running'],
            'ai':           state.get('hermes_ai', ''),
            'signal_evals': state.get('hermes_signal_evals', {}),
            'news':         state.get('hermes_news', []),
            'picks':        _to_json_safe(state.get('hermes_picks', [])),
        })


# ── SHORT SQUEEZE SCANNER — Route + Background Monitor ───────────────────────

_squeeze_state = {'results': [], 'ts': '', 'last_alerted': set()}
_squeeze_lock  = threading.Lock()

@app.route('/squeeze')
def squeeze_route():
    with _squeeze_lock:
        data = _squeeze_state.copy()
    return jsonify({
        'results': data['results'],
        'count':   len(data['results']),
        'ts':      data['ts'],
    })

def squeeze_monitor():
    """
    Raketen-Scanner: 3min in der ersten Handelsstunde (9:30-10:30 ET),
    5min danach. INHD/PAVS-Moves werden so fruehzeitig erkannt.
    Sendet Telegram-Alert wenn Score >= 4.
    """
    import time as _time
    from scanner import squeeze_scanner
    _time.sleep(60)

    alerted_today = set()
    last_day = ''

    while True:
        try:
            now     = datetime.now()
            day_str = now.strftime('%Y-%m-%d')

            if day_str != last_day:
                alerted_today.clear()
                last_day = day_str

            # US-Marktzeiten (UTC): Pre-Market ab 12:00, Close 22:00
            hour_utc = now.utctimetuple().tm_hour
            min_utc  = now.utctimetuple().tm_min
            is_market = (12 <= hour_utc <= 22)

            # Erste Stunde nach Open (13:30-14:30 UTC = 9:30-10:30 ET) = schnellster Scan
            is_power_hour = (hour_utc == 13 and min_utc >= 30) or (hour_utc == 14 and min_utc < 30)

            if is_market:
                results = squeeze_scanner()

                with _squeeze_lock:
                    _squeeze_state['results'] = results
                    _squeeze_state['ts']      = now.strftime('%H:%M:%S')

                for r in results:
                    sym   = r['sym']
                    score = r['score']
                    level = r['alert_level']
                    key   = f"{sym}_{r['chg_pct']}"

                    if key not in alerted_today and score >= 4:
                        alerted_today.add(key)
                        emoji = {'EXTREME': '🚨🚨', 'HIGH': '🚨', 'MEDIUM': '⚡', 'LOW': '📊'}.get(level, '📊')
                        cats  = '\n'.join(f'  • {c}' for c in r['catalysts'][:3]) if r['catalysts'] else '  • kein Katalysator'
                        sigs  = ' | '.join(r.get('signals', []))
                        msg = (
                            f'{emoji} <b>SQUEEZE ALERT: {sym}</b>  [{level}]  Score:{score}\n'
                            f'Preis: ${r["price"]}  (vorher ${r["prev_close"]})\n'
                            f'Heute: <b>+{r["chg_pct"]}%</b>  Vol:{r["vol_ratio"]}x normal\n'
                            f'MarketCap: ${r["mktcap"]/1e6:.1f}M  Float:{r["shares_out"]/1e3:.0f}K Aktien\n'
                            f'Signale: {sigs}\n'
                            f'Katalysatoren:\n{cats}\n'
                            f'Gruende: {" | ".join(r["reasons"][:3])}'
                        )
                        tg_send(msg)

        except Exception:
            pass

        # Power Hour (9:30-10:30 ET): alle 3min — Raketen passieren meist hier
        # Restliche Marktzeit: alle 5min
        _time.sleep(180 if is_power_hour else 300)


# ── Startup ───────────────────────────────────────────────────────────────────

saved = load_results()
if saved:
    state['results']           = saved
    state['last_scan']         = saved.get('time')
    state['last_results_hash'] = results_hash(saved)

state['next_scan'] = next_scan_time()

# Memory aus GitHub Gist wiederherstellen (überlebt Railway-Deployments)
try:
    gist_restore()
except Exception:
    pass

# Auto-Scheduler im Hintergrund starten
def strategy_monitor_loop():
    """
    Strategy Monitor Pro — läuft alle 10 Minuten.
    Scannt alle FIB_UNIVERSE Symbole nach: LSOB + GUSS + Dolphin + FIB 0.882 + OMS.
    Schreibt Ergebnisse in state['strategy_monitor'] für /hermes/intel + Bitget Bot.
    """
    import time as _time
    _time.sleep(30)   # kurzer Startup-Delay

    while True:
        try:
            from scanner import strategy_monitor_scan
            results = strategy_monitor_scan(max_workers=4)
            state['strategy_monitor']    = results
            state['strategy_monitor_ts'] = datetime.now().strftime('%H:%M')

            longs  = [r for r in results if r.get('strong_long')]
            shorts = [r for r in results if r.get('strong_short')]

            if longs or shorts:
                msg_parts = []
                if longs:
                    top = longs[:3]
                    msg_parts.append('📈 LONG: ' + ', '.join(
                        f"{r['sym']} {r['bull_total']}x ({'+'.join(r['strats_long'])})" for r in top
                    ))
                if shorts:
                    top = shorts[:3]
                    msg_parts.append('📉 SHORT: ' + ', '.join(
                        f"{r['sym']} {r['bear_total']}x ({'+'.join(r['strats_short'])})" for r in top
                    ))
                logging.info('[StratMon] ' + ' | '.join(msg_parts))

        except Exception as e:
            logging.error(f'[StratMon] Fehler: {e}')

        _time.sleep(600)   # 10 Minuten


def hermes_fib_loop():
    """
    Fibonacci 88.2% Auto-Trade Loop — läuft alle 20 Minuten.
    Scannt FIB_UNIVERSE + Hermes-Universe nach 88.2% Retracement.
    AT_882 oder NEAR_882 + Bounce → LONG Trade
    AT_882 + Rejection → SHORT Trade
    """
    import time as _time
    _time.sleep(45)  # Startup-Delay: warten bis App bereit
    _fib_traded_today = set()
    _fib_last_day     = ''

    while True:
        try:
            now     = datetime.now()
            today   = now.strftime('%Y-%m-%d')
            now_utc = datetime.now(timezone.utc)

            # Reset täglich
            if today != _fib_last_day:
                _fib_traded_today.clear()
                _fib_last_day = today

            # Nur während Marktzeiten (13:30–20:00 UTC = 9:30–16:00 ET)
            is_market = (now_utc.hour == 13 and now_utc.minute >= 30) or \
                        (13 < now_utc.hour < 20) or \
                        (now_utc.hour == 20 and now_utc.minute == 0)

            if is_market:
                from scanner import fibonacci_scan, FIB_UNIVERSE

                # Universe dynamisch erweitern mit Hermes-Symbolen
                hermes_uni = list(state.get('hermes_universe', set()))
                extra = [s for s in hermes_uni if s not in FIB_UNIVERSE][:30]
                dyn_universe = list(FIB_UNIVERSE) + extra

                # Fib-Scan starten (nutzt internen FIB_UNIVERSE in scanner.py)
                results = fibonacci_scan()

                # Zusätzliche Symbole aus Hermes-Universe einzeln prüfen
                if extra:
                    from scanner import _fib_for_ticker
                    for sym in extra[:20]:
                        r = _fib_for_ticker(sym)
                        if r:
                            results.append(r)

                # Ergebnisse in State speichern (für UI)
                state['fib_results']   = results
                state['fib_last_scan'] = now.strftime('%H:%M')

                # ── Auto-Trade: FIB 0.882 Sniper ─────────────────────────────
                if state.get('auto_trade_enabled'):
                    for r in results:
                        sig    = r.get('signal', '')
                        sym    = r.get('sym', '')
                        if sig not in ('AT_882', 'NEAR_882'):
                            continue
                        if sym in _fib_traded_today:
                            continue

                        price     = r.get('price', 0)
                        bounce    = r.get('bounce', False)
                        rejection = r.get('rejection', False)
                        chg_pct   = r.get('chg_pct', 0)
                        vol_ratio = r.get('vol_ratio', 1.0)
                        candle    = r.get('candle', '')
                        rr        = r.get('rr', 0)

                        # Mindest-Filter: R/R >= 1.5 und entweder Bounce oder Kerzenformation
                        bull_candle = candle in ('HAMMER', 'BULL_ENGULF', 'BULL_CANDLE')
                        bear_candle = candle in ('SHOOTING_STAR', 'BEAR_ENGULF', 'BEAR_CANDLE')

                        if rejection and bear_candle:
                            direction = 'SHORT'
                        elif bounce and bull_candle:
                            direction = 'LONG'
                        elif sig == 'AT_882' and bounce:
                            direction = 'LONG'
                        elif sig == 'AT_882' and rejection:
                            direction = 'SHORT'
                        else:
                            continue  # Kein klares Signal — kein Trade

                        # R/R-Check: nur handeln wenn >= 1.5
                        if rr < 1.5:
                            continue

                        # Score: AT_882 = 14 (exakt), NEAR_882 = 11; Bonus für Kerzen + Vol
                        score = (14 if sig == 'AT_882' else 11)
                        if candle in ('HAMMER', 'SHOOTING_STAR'):
                            score += 3
                        elif candle in ('BULL_ENGULF', 'BEAR_ENGULF'):
                            score += 2
                        if vol_ratio > 1.5:
                            score += 2
                        if vol_ratio > 2.0:
                            score += 1

                        conviction = min(0.95, 0.68 + (vol_ratio - 1.0) * 0.08
                                         + (0.05 if candle in ('HAMMER','SHOOTING_STAR','BULL_ENGULF','BEAR_ENGULF') else 0))

                        reason_parts = [f'Fib 0.882 Sniper ({sig})']
                        reason_parts.append(f'Kerze:{candle}')
                        if bounce:    reason_parts.append('Bounce↑')
                        if rejection: reason_parts.append('Rejection↓')
                        if vol_ratio > 1.5: reason_parts.append(f'Vol×{vol_ratio:.1f}')
                        reason_parts.append(f'R/R:{rr:.1f}')
                        reason = ' | '.join(reason_parts)

                        fib_signal = {
                            't':          sym,
                            'price':      price,
                            'signal':     direction,
                            'score':      score,
                            'conviction': round(conviction, 2),
                            'top_reason': reason,
                        }

                        ok = hermes_auto_trade_stock(fib_signal, reason=reason)
                        if ok:
                            _fib_traded_today.add(sym)
                            fib_trade_entry = {
                                'sym':       sym,
                                'signal':    direction,
                                'score':     score,
                                'fib_level': '88.2%',
                                'fib_signal': sig,
                                'candle':    candle,
                                'price':     price,
                                'chg_pct':   chg_pct,
                                'vol_ratio': vol_ratio,
                                'tp1':       r.get('tp1'),
                                'tp2':       r.get('tp2'),
                                'tp3':       r.get('tp3'),
                                'sl':        r.get('sl'),
                                'rr':        rr,
                                'bounce':    bounce,
                                'rejection': rejection,
                                'reason':    reason,
                                'time':      now.strftime('%H:%M'),
                                'date':      today,
                            }
                            state['fib_trades'] = ([fib_trade_entry] + state.get('fib_trades', []))[:50]

        except Exception:
            pass

        _time.sleep(1200)  # alle 20 Minuten


sched = threading.Thread(target=auto_scheduler, daemon=True)
sched.start()

# Hermes Agent starten
hermes_thread = threading.Thread(target=hermes_monitor, daemon=True)
hermes_thread.start()

# Hermes Brain — autonomer KI-Agent (läuft unabhängig, denkt selbst)
brain_thread = threading.Thread(target=hermes_brain_loop, daemon=True)
brain_thread.start()

# Kanal-Pattern Scanner — Ping-Pong Erkennung
channel_thread = threading.Thread(target=channel_bounce_loop, daemon=True)
channel_thread.start()

# VWAP Reclaim Scanner
vwap_thread = threading.Thread(target=vwap_reclaim_loop, daemon=True)
vwap_thread.start()

# Pre-Market Gap Scanner
gap_thread = threading.Thread(target=premarket_gap_loop, daemon=True)
gap_thread.start()

# Squeeze Scanner Monitor starten
squeeze_thread = threading.Thread(target=squeeze_monitor, daemon=True)
squeeze_thread.start()

# Fibonacci 88.2% Auto-Trade Loop
fib_thread = threading.Thread(target=hermes_fib_loop, daemon=True)
fib_thread.start()

# Strategy Monitor Pro — alle 5 Strategien aus TradingView (LSOB+GUSS+Dolphin+FIB882+OMS)
strat_thread = threading.Thread(target=strategy_monitor_loop, daemon=True)
strat_thread.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

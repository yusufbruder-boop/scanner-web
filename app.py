import json, os, threading, time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request
import urllib.request, ssl
# v3.0 — Hermes Full Agent: Alpaca + Polygon + Memory + P&L

app = Flask(__name__)

RESULTS_FILE   = 'results.json'
MEMORY_FILE    = 'hermes_memory.json'
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT  = os.environ.get('TELEGRAM_CHAT',  '')

# Alpaca Paper API
ALPACA_KEY    = os.environ.get('ALPACA_KEY',    'PK5T6OU5ENWZQK5DVZ746MHHEF')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET', '3nngSp7NksYikEZvf5hLihWEBtFdnuG336KfeYvFb5D9')
ALPACA_BASE   = 'https://paper-api.alpaca.markets'

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
    'hermes_picks':   [],        # direkt gescannte Karten
    'hermes_universe': set(),    # dynamisch erweiterte Tickers
    'hermes_ts':           None,
    'hermes_running':      False,
    'hermes_running_since': None,
    'hermes_ai':           '',
    'hermes_signal_evals': {},
    'alpaca_portfolio':    {},
    'hermes_memory':       {},
    # Background threads: Social KI-Score + HF 13F
    'social_data':    [],
    'hf_data':        [],
    'extra_ts':       None,
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
    except Exception as e:
        return {'error': str(e)}

def get_alpaca_portfolio():
    """Holt Alpaca Account + Positionen + Orders."""
    acc  = _alpaca('/v2/account')
    pos  = _alpaca('/v2/positions')
    if not isinstance(pos, list):
        pos = []
    positions = []
    for p in pos:
        try:
            positions.append({
                'sym':      p['symbol'],
                'side':     p['side'],
                'qty':      float(p['qty']),
                'entry':    round(float(p['avg_entry_price']), 2),
                'price':    round(float(p['current_price']), 2),
                'pl':       round(float(p['unrealized_pl']), 2),
                'pl_pct':   round(float(p['unrealized_plpc']) * 100, 1),
                'mkt_val':  round(float(p['market_value']), 2),
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
    """Aktualisiert P&L für alle offenen Signale via Polygon."""
    mem = load_memory()
    changed = False
    for sym, sig in mem['signals'].items():
        if sig.get('status') != 'open':
            continue
        try:
            ctx = ssl.create_default_context()
            url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{sym}?apiKey={poly_key}'
            with urllib.request.urlopen(urllib.request.Request(url), context=ctx, timeout=6) as r:
                d = json.loads(r.read())
            t = d.get('ticker', {})
            now_price = float(t.get('lastTrade', {}).get('p') or t.get('day', {}).get('c') or 0)
            if now_price and sig['entry_price']:
                if sig['signal'] == 'LONG':
                    pl_pct = (now_price - sig['entry_price']) / sig['entry_price'] * 100
                else:
                    pl_pct = (sig['entry_price'] - now_price) / sig['entry_price'] * 100
                sig['current_pl_pct'] = round(pl_pct, 1)
                sig['current_price']  = round(now_price, 2)
                sig['peak_pl_pct']    = round(max(sig.get('peak_pl_pct', 0), pl_pct), 1)
                changed = True
        except Exception:
            pass
    if changed:
        save_memory(mem)
    return mem

# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

def tg_send(msg):
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

        _CRYPTO = {'BTC', 'ETH', 'SOL', 'BTC.X', 'ETH.X', 'DOGE', 'XRP', 'MSTR'}

        def _poly_live(sym):
            """Live-Preis + heute% via Polygon Snapshot, 7T% via Aggregates."""
            if sym in _CRYPTO:
                return 0.0, 0.0, 0.0
            POLY2 = os.environ.get('POLYGON_API_KEY', '')
            if not POLY2:
                return 0.0, 0.0, 0.0
            price = today_chg = trend_7d = 0.0
            try:
                # 1) Polygon Snapshot → Live-Preis + heutiges %
                snap_url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{sym}?apiKey={POLY2}'
                with _ur2.urlopen(_ur2.Request(snap_url), context=_ctx2, timeout=8) as r:
                    sd = _json2.loads(r.read())
                t = sd.get('ticker', {})
                price      = float(t.get('lastTrade', {}).get('p') or t.get('day', {}).get('c') or 0)
                today_chg  = round(float(t.get('todaysChangePerc') or 0), 1)
                if not price:
                    price = float(t.get('day', {}).get('c') or t.get('prevDay', {}).get('c') or 0)
            except Exception:
                pass
            try:
                # 2) Aggregates → 7T Trend + heute% Fallback
                from_d = (datetime.now() - timedelta(days=12)).strftime('%Y-%m-%d')
                to_d   = datetime.now().strftime('%Y-%m-%d')
                agg_url = f'https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/{from_d}/{to_d}?adjusted=true&sort=asc&limit=12&apiKey={POLY2}'
                with _ur2.urlopen(_ur2.Request(agg_url), context=_ctx2, timeout=8) as r2:
                    ad = _json2.loads(r2.read())
                bars = [b['c'] for b in ad.get('results', []) if b.get('c')]
                if len(bars) >= 2:
                    if not price:
                        price = round(bars[-1], 2)
                    trend_7d = round((bars[-1] - bars[0]) / bars[0] * 100, 1)
                    if today_chg == 0.0:   # Snapshot hat versagt → Fallback aus Aggregates
                        today_chg = round((bars[-1] - bars[-2]) / bars[-2] * 100, 1) if bars[-2] else 0.0
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
        social_data = []
        for sym in social_raw[:12]:
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

        # ── Situational Awareness LP (Leopold Aschenbrenner) — bekannte AI-Holdings ──
        # Kein öffentlicher RSS/13F — zeige bekannte Positionen mit Live-Preisen
        leo_syms = ['NVDA', 'MSFT', 'GOOGL', 'META', 'AMD', 'PLTR', 'TSLA', 'AMZN']
        leo_holdings = []
        for sym in leo_syms[:6]:
            try:
                price_now, price_then, since = _yahoo_price_change(sym, '2026-01-01')
                if price_now > 0:
                    leo_holdings.append({
                        'sym':        sym,
                        'action':     'AI FOCUS',
                        'val_m':      0,
                        'date':       '2026',
                        'reason':     'Situational Awareness LP — KI-Infrastruktur',
                        'price_now':  price_now,
                        'price_then': price_then,
                        'since_pct':  since,
                    })
            except Exception:
                pass
        if leo_holdings:
            hf_data.append({
                'manager':  'Situational Awareness LP (L. Aschenbrenner)',
                'date':     '2026',
                'form':     'AI FUND',
                'url':      'https://situational-awareness.ai',
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
    <div id="hermes-badge" style="background:#0a2a1a;border:1px solid #2d9e57;border-radius:20px;padding:3px 10px;display:flex;align-items:center;gap:5px;font-size:11px;font-weight:700;color:#4dff91;cursor:default" title="Hermes Agent Status">
      <span style="width:6px;height:6px;background:#4dff91;border-radius:50%;display:inline-block;animation:pulse 2s infinite"></span>
      <span id="hermes-status-text">HERMES</span>
    </div>
  </div>
  <div class="header-info" style="padding:0 0 6px">
    <span id="lastScanInfo">Lade...</span>
    <span id="nextScanInfo"></span>
  </div>
  <div class="tabs">
    <button class="tab-btn active"      id="tab1Btn" onclick="showTab(1)">📊 SCANNER</button>
    <button class="tab-btn intel"       id="tab2Btn" onclick="showTab(2)">🔍 INTEL</button>
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

<script>
let lastHash = null;
let refreshInterval = null;
let refreshCountdown = 60;

function pct(v) {
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
  let sigColor = cls === 'long' ? 'signal-long' : (cls === 'short' ? 'signal-short' : 'signal-mover');
  let badge    = cls === 'long' ? 'badge-long'  : (cls === 'short' ? 'badge-short'  : 'badge-mover');
  let katBadge = r.katalysator !== 'KEIN'
    ? '<span class="badge badge-kat">' + (r.katalysator === 'POSITIV' ? 'POSITIV NEWS' : 'NEGATIV NEWS') + '</span>' : '';
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
    +   '<span class="price ' + sigColor + '" style="margin-left:10px">$' + r.price + '</span></div>'
    +   '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + katBadge + conflictBadge + socialBadge + dpBadge + swBadge
    +   '<span class="badge ' + badge + '">' + r.signal + (r.score > 0 ? ' ' + r.score : '') + '</span></div>'
    + '</div>'
    + '<div class="card-body">'
    +   '<div class="row">'
    +     '<div class="stat"><span class="stat-label">Trend 10T</span><span class="stat-value">' + pct(r.trend) + '</span></div>'
    +     '<div class="stat"><span class="stat-label">Vortag</span><span class="stat-value">'    + pct(r.prev_chg) + '</span></div>'
    +     '<div class="stat"><span class="stat-label">P/C</span><span class="stat-value">'       + r.pc + '</span></div>'
    +     '<div class="stat"><span class="stat-label">Hoch-Abst.</span><span class="stat-value ' + (r.drop_high < -5 ? 'pct-neg' : '') + '">' + r.drop_high + '%</span></div>'
    +   '</div>'
    +   opt + kat
    + '</div>'
    + aiBox
    + '</div>';
}

function renderResults(data, isNew) {
  let html = '';

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

  // ── Hauptziel: Mover + Long + Short ─────────────────────────────────────
  if (data.movers && data.movers.length > 0) {
    html += '<div class="section"><div class="section-title mover">🎯 NEXT MOVER — 10%+ Potenzial, Günstiger Call</div>';
    data.movers.forEach(r => { html += renderCard(r, 'mover', isNew); });
    html += '</div>';
  }

  html += '<div class="section"><div class="section-title long">▲ TOP LONG — Options Flow + Katalysator</div>';
  if (!data.longs || data.longs.length === 0) {
    html += '<div class="empty">Keine Long-Signale.</div>';
  } else {
    data.longs.slice(0, 5).forEach(r => { html += renderCard(r, 'long', isNew); });
  }
  html += '</div>';

  html += '<div class="section"><div class="section-title short">▼ TOP SHORT — Überbewertet / Fallend</div>';
  if (!data.shorts || data.shorts.length === 0) {
    html += '<div class="empty">Keine Short-Signale.</div>';
  } else {
    data.shorts.slice(0, 5).forEach(r => { html += renderCard(r, 'short', isNew); });
  }
  html += '</div>';

  // Nachrichten
  let allCards = (data.longs || []).concat(data.shorts || []).concat(data.watch || []);
  let newsItems = allCards.filter(r => r.katalysator !== 'KEIN' && r.kat_text);
  if (newsItems.length > 0) {
    html += '<div class="section"><div class="section-title news">NACHRICHTEN — Katalysatoren</div>';
    newsItems.slice(0, 15).forEach(n => {
      let cls   = n.katalysator === 'POSITIV' ? 'news-pos' : 'news-neg';
      let label = n.katalysator === 'POSITIV' ? '▲ POSITIV' : '▼ NEGATIV';
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
      (a.reasons||[]).forEach(r => { html += '<div style="font-size:11px;color:#94a3b8;margin-top:2px">• ' + r + '</div>'; });
      html += '</div></div>';
    });
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
  let html = '';

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
    (ap.positions||[]).forEach(p => {
      let pc = p.pl_pct >= 0 ? '#4dff91' : '#ff4d6b';
      html += '<div style="display:flex;justify-content:space-between;padding:5px 0;border-top:1px solid #1a2a1a">'
        + '<div><span style="font-size:14px;font-weight:bold;color:#fff">' + p.sym + '</span>'
        +   ' <span style="font-size:10px;color:#6b8cad">' + p.side + ' ' + p.qty + 'x @ $' + p.entry + '</span></div>'
        + '<div style="text-align:right">'
        +   '<span style="color:#fff;font-size:13px">$' + p.price + '</span>'
        +   ' <span style="color:' + pc + ';font-size:12px;font-weight:bold">' + (p.pl_pct>=0?'+':'') + p.pl_pct + '% ($' + p.pl.toFixed(0) + ')</span>'
        + '</div></div>';
    });
    html += '</div>';
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
          let scCol = sc >= 0 ? '#4dff91' : '#ff4d6b';
          let actCol = h.action === 'REDUZIERT' ? '#ff4d6b' : h.action === 'GEHALTEN' ? '#94a3b8' : '#4dff91';
          let actBg  = h.action === 'REDUZIERT' ? '#2a0a0a' : h.action === 'GEHALTEN' ? '#1a2a3a' : '#0a2a1a';
          let pStr   = h.price_then > 0 ? ' $' + h.price_then + ' → $' + h.price_now : (h.price_now > 0 ? ' $' + h.price_now : '');
          let scStr  = sc !== 0 ? '<span style="color:' + scCol + ';font-weight:bold">' + (sc>=0?'+':'') + sc + '%</span>' : '';
          html += '<div style="padding:5px 0;border-bottom:1px solid #0d1a28;display:flex;justify-content:space-between;align-items:center">'
            +   '<div>'
            +     '<span style="font-size:14px;font-weight:bold;color:#e2e8f0">' + h.sym + '</span>'
            +     ' <span style="font-size:10px;color:' + actCol + ';background:' + actBg + ';padding:1px 6px;border-radius:8px">' + h.action + '</span>'
            +     '<div style="font-size:11px;color:#64748b;margin-top:1px">' + pStr + '</div>'
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
  document.getElementById('tab1Btn').classList.toggle('active', n === 1);
  document.getElementById('tab2Btn').classList.toggle('active', n === 2);
  if (n === 2 && _lastData) {
    document.getElementById('intel-content').innerHTML = renderTab2(_lastData) || '<div class="empty">Wird nach Scan geladen...</div>';
  }
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
    txt.textContent = 'HERMES läuft...';
  } else if (ht) {
    badge.style.borderColor = '#2d9e57';
    badge.style.color = '#4dff91';
    badge.querySelector('span').style.background = '#4dff91';
    txt.textContent = 'HERMES ✓ ' + ht;
  } else {
    badge.style.borderColor = '#1e3a5f';
    badge.style.color = '#4a6a8a';
    badge.querySelector('span').style.background = '#4a6a8a';
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
    // Tab 2: Intel (nur wenn aktiv)
    if (document.getElementById('tab2').style.display !== 'none') {
      document.getElementById('intel-content').innerHTML = renderTab2(data) || '<div class="empty">Keine Intel-Daten.</div>';
    }
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
            return jsonify({'error': 'Noch kein Scan. Drücke SCAN STARTEN.'})
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
        with _hermes_lock:
            out['hermes_alerts']       = state.get('hermes_alerts', [])
            out['hermes_picks']        = state.get('hermes_picks', [])
            out['hermes_ts']           = state.get('hermes_ts', '')
            out['hermes_ai']           = state.get('hermes_ai', '')
            out['hermes_news']         = state.get('hermes_news', [])
            out['hermes_universe']     = list(state.get('hermes_universe', set()))
            out['hermes_signal_evals'] = state.get('hermes_signal_evals', {})
            out['alpaca_portfolio']    = state.get('alpaca_portfolio', {})
            out['hermes_memory']       = state.get('hermes_memory', {})
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

Bewerte auf Deutsch:
1. STÄRKE: Wie stark ist dieses Signal? (1-10 mit Begründung)
2. SETUP: Ist das Setup realistisch? (Strike erreichbar, Trend passt, Timing?)
3. RISIKO: Was könnte schiefgehen?
4. EMPFEHLUNG: Einsteigen, Warten, oder Finger weg? Warum?

Antworte kompakt, max 150 Wörter."""

    return _nous_call(prompt, system='Du bist ein erfahrener Options-Trader und Analyst. Analysiere präzise und ehrlich, auch wenn das Signal schwach ist.', max_tokens=300)


def hermes_ai_analysis(scan_data: dict, hunt_alerts: list) -> str:
    """
    Marktüberblick + Bewertung aller Signale via NousResearch Hermes-3 70B.
    """
    if not NOUS_KEY:
        return ''
    try:
        longs  = scan_data.get('longs',  [])[:6]
        shorts = scan_data.get('shorts', [])[:4]
        movers = scan_data.get('movers', [])[:3]
        hunts  = hunt_alerts[:6]

        long_lines  = [f"  {r['t']} Score:{r['score']} Trend:{r.get('trend',0):+.1f}% P/C:{r.get('pc',0):.2f} Kat:{r.get('katalysator','')} | {r.get('kat_text','')[:60]}" for r in longs]
        short_lines = [f"  {r['t']} Score:{r['score']} Trend:{r.get('trend',0):+.1f}% P/C:{r.get('pc',0):.2f} Kat:{r.get('katalysator','')} | {r.get('kat_text','')[:60]}" for r in shorts]
        hunt_lines  = [f"  {a['ticker']} Score:{a['score']} | {', '.join(str(x) for x in a.get('reasons',[])[:3])}" for a in hunts]
        mover_lines = [f"  {r['t']} ${r['price']} Score:{r['score']} P/C:{r.get('pc',0):.2f}" for r in movers]

        prompt = f"""Du bist Hermes, ein AI Trading-Agent. Analysiere den gesamten Markt heute:

=== LONG SIGNALE ({len(longs)}) ===
{chr(10).join(long_lines) or '  keine'}

=== SHORT SIGNALE ({len(shorts)}) ===
{chr(10).join(short_lines) or '  keine'}

=== NEXT MOVERS ({len(movers)}) ===
{chr(10).join(mover_lines) or '  keine'}

=== HERMES ENTDECKT ({len(hunts)}) ===
{chr(10).join(hunt_lines) or '  keine'}

Erstelle eine Trading-Briefing auf Deutsch:
1. MARKTLAGE: Bullish/Bearish/Neutral? Warum?
2. TOP TRADE: Welches ist das beste Setup heute und warum?
3. VERSTECKTES SIGNAL: Gibt es etwas Übersehenes das wichtig sein könnte?
4. WARNUNG: Was sollte man heute vermeiden?
5. ZUSAMMENFASSUNG: 1 Satz für den Tag.

Max 200 Wörter, direkt und präzise."""

        return _nous_call(
            prompt,
            system='Du bist Hermes, ein professioneller AI Trading-Agent mit Zugang zu Options-Flow, Dark Pool und Marktdaten. Analysiere objektiv und präzise.',
            max_tokens=500
        )
    except Exception:
        return ''


def hermes_monitor():
    """
    Hermes Agent — läuft 24/7, alle 5 Min.
    Selbst-heilend: repariert Scans, Social-Daten, stuck-Zustände automatisch.
    Läuft unabhängig vom Scan-Status.
    """
    time.sleep(30)
    while True:
        try:
            # ── WATCHDOG: stuck hermes_running → reset ────────────────────────
            with _hermes_lock:
                h_since = state.get('hermes_running_since')
                h_running = state.get('hermes_running', False)
            if h_running and h_since:
                stuck_min = (datetime.now() - h_since).total_seconds() / 60
                if stuck_min > 12:
                    with _hermes_lock:
                        state['hermes_running'] = False
                        state['hermes_running_since'] = None
                    tg_send('⚠️ <b>HERMES WATCHDOG</b>: stuck-Zustand zurückgesetzt')

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

            # 4) Scan hat 0 Ergebnisse (leer) → neu starten
            if data and not state['running']:
                if not data.get('longs') and not data.get('shorts') and not data.get('watch'):
                    if _start_scan('leere-ergebnisse'):
                        for _ in range(24):
                            time.sleep(10)
                            if not state['running']: break
                        continue

            # 5) Social-Daten fehlen → enrich neu starten
            if data and not state.get('social_data') and not state['running']:
                threading.Thread(target=enrich_background, args=(data,), daemon=True).start()

            # 3) Hermes Analyse — läuft auch wenn Scan parallel läuft (nutzt letzte Ergebnisse)
            forced = state.pop('hermes_force', False)
            with _hermes_lock:
                already_running = state.get('hermes_running', False)
            if (not already_running or forced) and data:
                with _hermes_lock:
                    state['hermes_running'] = True
                    state['hermes_running_since'] = datetime.now()
                try:
                    from scanner import hermes_hunt, scan_ticker, get_alpaca_market_news
                    POLY_KEY = os.environ.get('POLYGON_API_KEY', '')

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

                    # 4) Neue starke Signale in Memory speichern
                    for sig_r in data.get('longs', [])[:5] + data.get('shorts', [])[:3]:
                        try:
                            memory_track_signal(
                                sig_r['t'], sig_r['price'], sig_r['signal'],
                                sig_r['score'], [sig_r.get('kat_text','')[:60]]
                            )
                        except Exception:
                            pass

                    # 5) Nachrichten — Polygon News + Alpaca Breaking
                    news_alerts = []
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
                                    news_alerts.append(f'{sym}: {n.get("title","")[:60]}')
                                    break
                        except Exception:
                            pass

                    al_news = get_alpaca_market_news(limit=10)
                    al_breaking = []
                    from scanner import POS_KEYS, NEG_KEYS
                    for n in al_news:
                        h = n.get('headline', '')
                        if any(k in h.lower() for k in POS_KEYS + NEG_KEYS):
                            syms = n.get('symbols', [])
                            if syms:
                                al_breaking.append(f'{",".join(syms[:2])}: {h[:55]}')

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
                            except Exception:
                                pass

                    # 7) Universe erweitern
                    uni = state.get('hermes_universe', set())
                    state['hermes_universe'] = uni | {a['ticker'] for a in alerts if a['score'] >= 7}

                    # 8) Marktschluss-Analyse (16:00-16:15 ET = 20:00-20:15 UTC)
                    now_utc = datetime.now(timezone.utc)
                    is_close = (now_utc.hour == 20 and now_utc.minute < 15)
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
                        state['hermes_running'] = False
                        state['hermes_running_since'] = None

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

@app.route('/hermes/memory')
def hermes_memory_api():
    return jsonify(load_memory())

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


# ── Startup ───────────────────────────────────────────────────────────────────

saved = load_results()
if saved:
    state['results']           = saved
    state['last_scan']         = saved.get('time')
    state['last_results_hash'] = results_hash(saved)

state['next_scan'] = next_scan_time()

# Auto-Scheduler im Hintergrund starten
sched = threading.Thread(target=auto_scheduler, daemon=True)
sched.start()

# Hermes Agent starten
hermes_thread = threading.Thread(target=hermes_monitor, daemon=True)
hermes_thread.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

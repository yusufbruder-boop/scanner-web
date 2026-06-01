import urllib.request, json, ssl, time, threading, os, re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

ctx = ssl.create_default_context()
API           = os.environ.get('POLYGON_API_KEY', '')
ALPACA_KEY    = os.environ.get('ALPACA_API_KEY',    '')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY', '')
NOUS_KEY      = os.environ.get('NOUS_API_KEY',      '')

# ── Social Trending (Reddit WSB + Stocktwits) ────────────────────────────────
_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')
_SKIP_WORDS = {'THE','AND','FOR','ARE','YOU','NOT','BUT','HAS','WAS','ALL','CAN',
               'GET','ITS','TOO','NEW','BUY','PUT','CALL','CEO','IPO','SEC','ETF',
               'LOL','WSB','DD','YOLO','ATH','ATL','IMO','IMO','TBH','GBH'}

def get_social_trending():
    """Holt trending Tickers von Reddit WSB, r/stocks, Stocktwits, Yahoo Trending."""
    tickers = {}
    sources = {}

    # 1) Reddit WSB
    for sub, weight in [('wallstreetbets', 1.0), ('stocks', 0.5), ('options', 0.6)]:
        try:
            url = f'https://www.reddit.com/r/{sub}/hot.json?limit=30'
            req = urllib.request.Request(url, headers={'User-Agent': 'scanner/3.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                data = json.loads(r.read())
            for post in data.get('data', {}).get('children', []):
                d     = post.get('data', {})
                text  = (d.get('title', '') + ' ' + d.get('selftext', ''))[:500]
                score = d.get('score', 0)
                ratio = d.get('upvote_ratio', 0.5)
                for m in _TICKER_RE.findall(text):
                    if m not in _SKIP_WORDS and len(m) >= 2:
                        pts = int(score * ratio / 100 * weight)
                        tickers[m] = tickers.get(m, 0) + pts
                        sources.setdefault(m, set()).add(f'r/{sub}')
        except Exception:
            pass

    # 2) Stocktwits trending
    try:
        url2 = 'https://api.stocktwits.com/api/2/trending/symbols.json'
        req2 = urllib.request.Request(url2, headers={'User-Agent': 'scanner/3.0'})
        with urllib.request.urlopen(req2, context=ctx, timeout=8) as r:
            d2 = json.loads(r.read())
        for i, sym_data in enumerate(d2.get('symbols', [])[:15]):
            sym = sym_data.get('symbol', '')
            if sym and sym not in _SKIP_WORDS:
                tickers[sym] = tickers.get(sym, 0) + max(1, 15-i) * 4
                sources.setdefault(sym, set()).add('Stocktwits')
    except Exception:
        pass

    # 3) Stocktwits Stream
    try:
        url3 = 'https://api.stocktwits.com/api/2/streams/trending.json?filter=all'
        req3 = urllib.request.Request(url3, headers={'User-Agent': 'scanner/3.0'})
        with urllib.request.urlopen(req3, context=ctx, timeout=8) as r:
            d3 = json.loads(r.read())
        for msg in d3.get('messages', [])[:30]:
            sym = msg.get('symbols', [{}])[0].get('symbol', '') if msg.get('symbols') else ''
            if sym and sym not in _SKIP_WORDS:
                tickers[sym] = tickers.get(sym, 0) + 10
                sources.setdefault(sym, set()).add('Stocktwits-Stream')
    except Exception:
        pass

    # 4) Yahoo Finance Trending
    try:
        url4 = 'https://query1.finance.yahoo.com/v1/finance/trending/US?count=20'
        req4 = urllib.request.Request(url4, headers={'User-Agent': 'scanner/3.0'})
        with urllib.request.urlopen(req4, context=ctx, timeout=8) as r:
            d4 = json.loads(r.read())
        quotes = d4.get('finance', {}).get('result', [{}])[0].get('quotes', [])
        for i, q in enumerate(quotes[:20]):
            sym = q.get('symbol', '').split('.')[0].split('-')[0]
            if sym and sym not in _SKIP_WORDS and len(sym) <= 5:
                tickers[sym] = tickers.get(sym, 0) + max(1, 20-i) * 3
                sources.setdefault(sym, set()).add('Yahoo-Trending')
    except Exception:
        pass

    sorted_tickers = sorted(tickers.items(), key=lambda x: -x[1])
    top = [t for t, s in sorted_tickers if s >= 8][:15]
    return top, {t: s for t, s in sorted_tickers if t in top}

# Cached social data
_social_cache = {'tickers': [], 'scores': {}, 'ts': 0}
_social_lock  = threading.Lock()

def get_cached_social():
    with _social_lock:
        if time.time() - _social_cache['ts'] > 1800:  # 30 Min Cache
            tickers, scores = get_social_trending()
            _social_cache.update({'tickers': tickers, 'scores': scores, 'ts': time.time()})
        return _social_cache['tickers'], _social_cache['scores']

# ── Influencer / Smart Money ─────────────────────────────────────────────────
_INFLUENCER_FEEDS = [
    ('https://situationalawareness.substack.com/feed', 'Leopold Aschenbrenner'),
    ('https://www.astralcodexten.com/feed',            'Scott Alexander (ACX)'),
    ('https://www.noahpinion.blog/feed',               'Noah Smith'),
]
_influencer_cache: list = []
_influencer_ts: float   = 0.0
_influencer_lock = threading.Lock()

def get_influencer_signals() -> list:
    """RSS-Feeds von Smart Money / AI-Influencern → Ticker-Erwähnungen extrahieren."""
    import xml.etree.ElementTree as ET, re as _re, html as _html
    PAT  = _re.compile(r'\b\$?([A-Z]{2,5})\b')
    SKIP = {'I','A','THE','FOR','AND','BUT','NOT','ARE','YOU','HAS','ITS','WAS',
            'ALL','AI','US','UN','EU','UK','OR','AT','IT','IS','BE','AS','BY',
            'CEO','IPO','FED','GDP','EUR','USD','ETF','SEC','NYSE'}
    results = []
    for feed_url, author in _INFLUENCER_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={'User-Agent': 'scanner/3.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                raw = r.read()
            root = ET.fromstring(raw)
            ns   = {'atom': 'http://www.w3.org/2005/Atom'}
            items = root.findall('.//item') or root.findall('.//atom:entry', ns)
            for item in items[:8]:
                title_el = item.find('title') or item.find('atom:title', ns)
                desc_el  = item.find('description') or item.find('atom:summary', ns) or item.find('atom:content', ns)
                link_el  = item.find('link') or item.find('atom:link', ns)
                title = _html.unescape(title_el.text or '') if title_el is not None else ''
                desc  = _html.unescape(desc_el.text  or '') if desc_el  is not None else ''
                link  = link_el.get('href', link_el.text or '') if link_el is not None else ''
                if not isinstance(link, str):
                    link = ''
                text = (title + ' ' + desc[:300])
                tickers = [m for m in PAT.findall(text) if m not in SKIP and 2 <= len(m) <= 5]
                if tickers:
                    results.append({
                        'author': author,
                        'title':  title[:80],
                        'tickers': list(dict.fromkeys(tickers))[:6],
                        'url':    link,
                    })
        except Exception:
            pass
    return results

def get_cached_influencers() -> list:
    global _influencer_cache, _influencer_ts
    with _influencer_lock:
        if time.time() - _influencer_ts > 3600:  # 1h Cache
            _influencer_cache = get_influencer_signals()
            _influencer_ts    = time.time()
    return _influencer_cache

UNIVERSE = [
    'NVDA','AMD','META','AAPL','MSFT','AMZN','GOOGL','TSLA','NFLX',
    'MU','INTC','AVGO','QCOM','MRVL','SMCI','ARM','AMAT',
    'PLTR','CRWD','NET','DDOG','SOUN',
    'IONQ','RGTI','IREN','WULF','DELL','HPE',
    'GS','JPM','BAC','ASTS','LUNR','RKLB',
    'GLD','SLV','USO','AAL','DAL',
    'MSTR','COIN','HOOD','SOFI','ORCL','NOW',
    'XOM','CVX','LMT','RTX',
    # Erweiterung: häufige Mover mit Katalysatoren
    'SNOW','PANW','UBER','LYFT','RIVN','LCID','NIO','F','GM',
    'SHOP','SQ','PYPL','ROKU','SPOT','PINS','SNAP',
    'ENPH','SEDG','FSLR','NEE','HIMS','CELH','WOLF',
]

POS_KEYS = ['contract','government','deal','partnership','upgrade','raised','beat',
            'record','billion','trump','invest','breakthrough','ai','quantum','launch',
            'buyback','dividend','acquisition','target','infrastructure','pivot',
            'revenue','earnings','profit','surge','soar','stake','award','fda',
            'approval','patent','merger','spin','ipo','buyout','license','guidance']
NEG_KEYS = ['lawsuit','downgrade','miss','cut','investigation','fraud',
            'recall','ban','warning','below','probe','short seller','loss',
            'decline','disappoint','weak','concern','risk','violation','delay',
            'bankruptcy','default','dilut','offering','withdrew']

# ── Dark Pool / Block Trade Detection (Polygon Trades API) ───────────────────
def get_darkpool_signal(ticker: str, today: str) -> dict:
    """
    Sucht große Block-Trades und Dark Pool Prints via Polygon /v3/trades.
    Dark pool conditions: 37=Large Block, 41=OTC/Dark Pool, 20, 29, 80, 81.
    """
    try:
        start = f'{today}T13:30:00Z'
        url   = (f'https://api.polygon.io/v3/trades/{ticker}'
                 f'?timestamp.gte={start}&order=desc&limit=250&apiKey={API}')
        data   = poly_fetch(url)
        trades = data.get('results', [])
        if not trades:
            return {}
        DP_CONDS = {20, 29, 37, 41, 80, 81}
        large_all = []
        dark_prints = []
        for t in trades:
            size   = t.get('size', 0) or 0
            price  = t.get('price', 0.0) or 0.0
            dollar = size * price
            conds  = set(t.get('conditions', []) or [])
            is_dp  = bool(conds & DP_CONDS)
            if dollar >= 500_000:
                large_all.append(dollar)
                if is_dp or dollar >= 2_000_000:
                    dark_prints.append(dollar)
        if not large_all:
            return {}
        return {
            'count':    len(large_all),
            'dp_count': len(dark_prints),
            'total':    int(sum(large_all)),
            'dp_total': int(sum(dark_prints)),
            'largest':  int(max(large_all)),
        }
    except Exception:
        return {}


# ── Options Sweep Detection ───────────────────────────────────────────────────
def get_options_sweep(contracts: list) -> dict:
    """
    Erkennt Sweep Orders: Vol > 2x OI = frische institutionelle Käufe.
    Gibt Sweep-Anzahl und größten Block-Dollar zurück.
    """
    sweeps_call = sweeps_put = 0
    top_call_dollar = top_put_dollar = 0.0
    for c in contracts:
        ctype  = c['details']['contract_type']
        vol    = c['day'].get('volume', 0) or 0
        oi     = max(c.get('open_interest', 0) or 1, 1)
        pr     = c['day'].get('close') or c['day'].get('open') or 0
        dollar = vol * pr * 100
        if vol > oi * 2 and vol > 100:
            if ctype == 'call': sweeps_call += 1
            else:               sweeps_put  += 1
        if ctype == 'call' and dollar > top_call_dollar: top_call_dollar = dollar
        if ctype == 'put'  and dollar > top_put_dollar:  top_put_dollar  = dollar
    return {
        'sweeps_call':     sweeps_call,
        'sweeps_put':      sweeps_put,
        'top_call_dollar': int(top_call_dollar),
        'top_put_dollar':  int(top_put_dollar),
    }


# ── Alpaca News API (kostenlos) ───────────────────────────────────────────────
def get_alpaca_news(tickers: list, limit: int = 5) -> list:
    """Alpaca Market News — kostenlos mit Paper-Account-Keys."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        return []
    try:
        syms = ','.join(tickers[:10])
        url  = f'https://data.alpaca.markets/v1beta1/news?symbols={syms}&limit={limit}&sort=desc'
        req  = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID':     ALPACA_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET,
            'User-Agent':          'scanner/3.0',
        })
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return json.loads(r.read()).get('news', [])
    except Exception:
        return []


# ── Yahoo Finance Top Movers (kostenlos) ────────────────────────────────────
def get_market_movers() -> list:
    """Holt aktuelle Top-Mover von Yahoo Finance — Aktien die sich heute stark bewegen."""
    movers = []
    urls = [
        ('https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers&count=20', 'Gainer'),
        ('https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_losers&count=20',  'Loser'),
        ('https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=most_actives&count=20','Active'),
    ]
    for url, label in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
                d = json.loads(r.read())
            quotes = d.get('finance', {}).get('result', [{}])[0].get('quotes', [])
            for q in quotes[:10]:
                sym = q.get('symbol', '').split('.')[0]
                chg = q.get('regularMarketChangePercent', 0) or 0
                price = q.get('regularMarketPrice', 0) or 0
                vol   = q.get('regularMarketVolume', 0) or 0
                if sym and 2 <= len(sym) <= 5 and abs(chg) >= 3:
                    movers.append({'sym': sym, 'chg': round(chg, 1), 'price': round(price, 2),
                                   'vol': vol, 'label': label})
        except Exception:
            pass
    return movers


# ── Alpaca Marktdaten (echte News + Preis) ───────────────────────────────────
def get_alpaca_market_news(limit: int = 20) -> list:
    """Alpaca allgemeine Marktnews — ohne Ticker-Filter, breites Signal."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        return []
    try:
        url = f'https://data.alpaca.markets/v1beta1/news?limit={limit}&sort=desc'
        req = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID':     ALPACA_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET,
            'User-Agent':          'scanner/3.0',
        })
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return json.loads(r.read()).get('news', [])
    except Exception:
        return []


def get_alpaca_snapshot(tickers: list) -> dict:
    """Alpaca Snapshot: aktueller Preis + Tagesvolumen für mehrere Ticker."""
    if not (ALPACA_KEY and ALPACA_SECRET):
        return {}
    try:
        syms = ','.join(tickers[:30])
        url  = f'https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}'
        req  = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID':     ALPACA_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET,
        })
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return {}


# ── Hermes Hunt: übersehene 10%+ Mover suchen ────────────────────────────────
def hermes_hunt(current_longs: list, current_shorts: list) -> list:
    """
    Hermes Agent — volles Polygon + Alpaca + Yahoo:
    Sucht 10%+ Mover die der Haupt-Scanner übersehen hat.
    Quellen: Dark Pool, Options Sweep, Polygon News, Alpaca News, Yahoo Movers.
    """
    in_scan = {r['t'] for r in current_longs + current_shorts}
    today   = datetime.now().strftime('%Y-%m-%d')

    # Kandidaten: UNIVERSE + Social + Yahoo Movers
    candidates = [t for t in UNIVERSE if t not in in_scan]
    try:
        social_t, _ = get_cached_social()
        for t in social_t:
            if t not in in_scan and t not in candidates and 2 <= len(t) <= 5:
                candidates.append(t)
    except Exception:
        pass

    # Yahoo Movers: Aktien die sich heute bereits stark bewegen
    movers_today = get_market_movers()
    for m in movers_today:
        t = m['sym']
        if t not in in_scan and t not in candidates:
            candidates.append(t)

    # Alpaca Markt-News: Ticker aus News extrahieren
    try:
        _pat = re.compile(r'\b([A-Z]{2,5})\b')
        _skip = {'THE','AND','FOR','ARE','NOT','BUT','HAS','CEO','IPO','FED','GDP',
                 'ETF','USD','EUR','AI','US','UK','EU','OR','AT','IT','IS'}
        al_news = get_alpaca_market_news(limit=15)
        for n in al_news:
            headline = n.get('headline', '')
            for sym in n.get('symbols', []):
                if sym and sym not in in_scan and sym not in candidates and 2 <= len(sym) <= 5:
                    candidates.append(sym)
            for m in _pat.findall(headline):
                if m not in _skip and m not in in_scan and m not in candidates and 2 <= len(m) <= 5:
                    candidates.append(m)
    except Exception:
        pass

    # Alpaca Snapshot für alle Kandidaten (Batch)
    al_snap = get_alpaca_snapshot(candidates[:40])

    alerts = []

    def check_one(ticker):
        score   = 0
        reasons = []
        dp_info = {}

        # 0) Yahoo Mover Check — bereits heute +5%?
        mover = next((m for m in movers_today if m['sym'] == ticker), None)
        if mover:
            if abs(mover['chg']) >= 8:
                score += 4
                reasons.append(f'Yahoo {mover["label"]}: {mover["chg"]:+.1f}% heute')
            elif abs(mover['chg']) >= 5:
                score += 2
                reasons.append(f'Yahoo {mover["label"]}: {mover["chg"]:+.1f}% heute')

        # 1) Dark Pool Print (Polygon /v3/trades)
        dp = get_darkpool_signal(ticker, today)
        if dp.get('dp_total', 0) >= 500_000:
            dp_info = dp
            m_val = dp['dp_total'] / 1_000_000
            score += 5 if m_val >= 10 else (4 if m_val >= 5 else (3 if m_val >= 1 else 1))
            reasons.append(f'Dark Pool ${m_val:.1f}M ({dp["dp_count"]} Prints)')

        # 2) Options Sweep (Polygon Snapshot)
        price_from_opt = 0
        try:
            opt = poly_fetch(f'https://api.polygon.io/v3/snapshot/options/{ticker}?limit=100&apiKey={API}')
            res = opt.get('results', [])
            if res:
                price_from_opt = res[0].get('underlying_asset', {}).get('price', 0)
                calls  = [r for r in res if r['details']['contract_type'] == 'call']
                cv     = sum(r['day'].get('volume', 0) for r in calls)
                oi_tot = sum(max(r.get('open_interest', 0) or 1, 1) for r in calls)
                sw     = get_options_sweep(res)
                if oi_tot and cv > oi_tot * 2 and cv > 200:
                    score += 3
                    reasons.append(f'Call Sweep Vol:{cv:,} vs OI:{oi_tot:,}')
                if sw['sweeps_call'] >= 2:
                    score += 2
                    reasons.append(f'{sw["sweeps_call"]} Call-Sweeps erkannt')
                if sw['top_call_dollar'] >= 500_000:
                    score += 1
                    reasons.append(f'Block Call ${sw["top_call_dollar"]/1e6:.1f}M')
        except Exception:
            pass

        # 3) Alpaca Snapshot: ungewöhnliches Volumen?
        if ticker in al_snap:
            snap = al_snap[ticker]
            dbar = snap.get('dailyBar', {})
            vol  = dbar.get('v', 0) or 0
            prev = snap.get('prevDailyBar', {})
            pvol = prev.get('v', 0) or 1
            if pvol and vol / pvol > 3:
                score += 2
                reasons.append(f'Alpaca Vol {vol/1e6:.1f}M vs Vortag {pvol/1e6:.1f}M ({vol/pvol:.1f}x)')

        # 4) Polygon News (letzte 4h)
        try:
            nd  = poly_fetch(f'https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=5&apiKey={API}')
            cut = (datetime.now() - timedelta(hours=4)).strftime('%Y-%m-%dT%H:%M:%SZ')
            for n in nd.get('results', []):
                if n.get('published_utc', '') < cut:
                    continue
                tl   = n.get('title', '').lower()
                sent = next((i.get('sentiment','') for i in n.get('insights',[])
                             if i.get('ticker') == ticker), '')
                if any(k in tl for k in POS_KEYS) and sent != 'negative':
                    score += 3
                    reasons.append(f'Polygon News: {n.get("title","")[:55]}')
                    break
                if any(k in tl for k in NEG_KEYS) or sent == 'negative':
                    score += 2
                    reasons.append(f'BEAR News: {n.get("title","")[:50]}')
                    break
        except Exception:
            pass

        # 5) Alpaca News für diesen Ticker
        for n in get_alpaca_news([ticker], limit=3):
            h  = n.get('headline', '')
            hl = h.lower()
            if any(k in hl for k in POS_KEYS):
                score += 2
                reasons.append(f'Alpaca: {h[:50]}')
                break

        if score >= 4 and reasons:
            price = price_from_opt or (mover['price'] if mover else 0)
            return {
                'ticker':  ticker,
                'score':   score,
                'reasons': reasons[:4],
                'dp':      dp_info,
                'price':   round(price, 2),
                'ts':      datetime.now().strftime('%H:%M'),
            }
        return None

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(check_one, t) for t in candidates[:40]]
        for fut in as_completed(futs, timeout=90):
            try:
                r = fut.result()
                if r:
                    alerts.append(r)
            except Exception:
                pass

    alerts.sort(key=lambda x: -x['score'])
    return alerts[:10]


# Paid Polygon plan ($79) — kein Rate-Limit nötig
def poly_fetch(url, retries=2):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, context=ctx, timeout=12) as r:
                data = json.loads(r.read())
            if data.get('status') == 'ERROR':
                raise Exception(data.get('error', 'API Error'))
            return data
        except Exception as e:
            if '429' in str(e) or 'Too Many' in str(e):
                time.sleep(5)
            if attempt >= retries:
                raise

def best_option(contracts, is_call, price, today, exp_cutoff):
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    candidates = []
    for c in contracts:
        exp = c['details']['expiration_date']
        if exp > exp_cutoff:
            continue
        strike = c['details']['strike_price']
        pct = ((strike - price) / price * 100) if is_call else ((price - strike) / price * 100)
        pr  = c['day'].get('close') or c['day'].get('open') or 0
        vol = c['day'].get('volume', 0)
        oi  = c.get('open_interest', 0)
        if 0.3 <= pct <= 12 and pr > 0:
            not_0dte = 1 if exp >= tomorrow else 0
            candidates.append({
                'strike': strike, 'pct': round(pct, 1), 'pr': pr,
                'vol': vol, 'oi': oi, 'exp': exp,
                'total': vol * pr * 100, 'not_0dte': not_0dte
            })
    candidates.sort(key=lambda x: (-x['not_0dte'], x['pr']))
    return next((c for c in candidates if c['vol'] > 30), candidates[0] if candidates else None)

def scan_ticker(ticker, today, exp_cutoff, news_cutoff):
    try:
        opt = poly_fetch(f'https://api.polygon.io/v3/snapshot/options/{ticker}?limit=250&apiKey={API}')
        res = opt.get('results', [])
        if not res:
            return None
        price = res[0].get('underlying_asset', {}).get('price', 0)
        if price < 2:
            return None

        calls = [r for r in res if r['details']['contract_type'] == 'call']
        puts  = [r for r in res if r['details']['contract_type'] == 'put']
        cv = sum(r['day'].get('volume', 0) for r in calls)
        pv = sum(r['day'].get('volume', 0) for r in puts)
        cp = sum(r['day'].get('volume', 0) * (r['day'].get('close') or 0) * 100 for r in calls)
        pp = sum(r['day'].get('volume', 0) * (r['day'].get('close') or 0) * 100 for r in puts)
        if cv + pv < 100:
            return None
        pc = pv / cv if cv else 99

        # Options Sweep Detection
        sweep = get_options_sweep(res)

        # Polygon Aggregates — immer aktuelle Tages-OHLCV Daten (kein yfinance)
        from_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        to_date   = datetime.now().strftime('%Y-%m-%d')
        agg = poly_fetch(
            f'https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day'
            f'/{from_date}/{to_date}?adjusted=true&sort=asc&limit=25&apiKey={API}'
        )
        bars = agg.get('results', [])
        if not bars or len(bars) < 3:
            return None
        closes = [float(b['c']) for b in bars]
        highs  = [float(b['h']) for b in bars]
        lows   = [float(b['l']) for b in bars]
        atr = max(highs[-i] - lows[-i] for i in range(1, 4))
        c10 = closes[-10:] if len(closes) >= 10 else closes
        trend_pct   = ((c10[-1] - c10[0]) / c10[0]) * 100
        c3 = closes[-3:]
        short_trend = ((c3[-1] - c3[0]) / c3[0]) * 100
        prev_chg    = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0
        period_high = max(highs[-10:]) if len(highs) >= 10 else max(highs)
        drop_from_high = ((closes[-1] - period_high) / period_high) * 100

        # Dark Pool parallel (Thread)
        dp_result = [{}]
        def _fetch_dp():
            dp_result[0] = get_darkpool_signal(ticker, today)
        dp_thread = threading.Thread(target=_fetch_dp, daemon=True)
        dp_thread.start()

        # Polygon News
        news_data = poly_fetch(f'https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=8&apiKey={API}')
        katalysator = 'KEIN'
        kat_text = kat_url = ''
        al_news_text = ''
        for n in news_data.get('results', []):
            if n.get('published_utc', '')[:10] < news_cutoff:
                continue
            title_l = n.get('title', '').lower()
            sent = next((i.get('sentiment', '') for i in n.get('insights', []) if i.get('ticker') == ticker), '')
            has_pos = any(k in title_l for k in POS_KEYS)
            has_neg = any(k in title_l for k in NEG_KEYS)
            if has_pos and sent in ('positive', 'neutral', ''):
                katalysator = 'POSITIV'
                kat_text = n.get('title', '')[:70]
                kat_url  = n.get('article_url', '')
                break
            elif has_neg or sent == 'negative':
                if katalysator != 'POSITIV':
                    katalysator = 'NEGATIV'
                    kat_text = n.get('title', '')[:70]
                    kat_url  = n.get('article_url', '')

        # Alpaca News als zweite Quelle (nur wenn Polygon nichts hat)
        if katalysator == 'KEIN':
            for an in get_alpaca_news([ticker], limit=3):
                h = an.get('headline', '')
                hl = h.lower()
                if any(k in hl for k in POS_KEYS):
                    katalysator = 'POSITIV'
                    kat_text = h[:70]
                    kat_url  = an.get('url', '')
                    al_news_text = '[Alpaca] '
                    break
                elif any(k in hl for k in NEG_KEYS):
                    katalysator = 'NEGATIV'
                    kat_text = h[:70]
                    kat_url  = an.get('url', '')
                    al_news_text = '[Alpaca] '

        # Social momentum
        _, social_scores = get_cached_social()
        social_score = social_scores.get(ticker, 0)
        social_boost = min(2, social_score // 50)

        # Dark Pool warten
        dp_thread.join(timeout=8)
        dp = dp_result[0]
        dp_dollar = dp.get('dp_total', 0)
        dp_score  = (3 if dp_dollar >= 5_000_000 else
                     2 if dp_dollar >= 1_000_000 else
                     1 if dp_dollar >= 500_000   else 0)

        long_score = short_score = 0

        # P/C Ratio
        if pc < 0.3:   long_score  += 3
        elif pc < 0.5: long_score  += 1
        if pc > 0.8:   short_score += 3
        elif pc > 0.6: short_score += 1

        # Trend
        if trend_pct < -7:   short_score += 4
        elif trend_pct < -4: short_score += 2
        elif trend_pct < -2: short_score += 1
        if 5 < trend_pct < 25:                              long_score  += 2
        if trend_pct >= 25 and short_trend > 0:             long_score  += 2
        if trend_pct >= 25 and short_trend < -2:            short_score += 2

        # Abstand vom Hoch
        if drop_from_high < -8:   short_score += 4
        elif drop_from_high < -5: short_score += 2
        elif drop_from_high < -3: short_score += 1

        # Kurzfrist-Trend
        if short_trend < -3:   short_score += 2
        elif short_trend < -1: short_score += 1
        if short_trend > 3:    long_score  += 1

        # News-Katalysator (+4 — wichtigstes Signal für 10%+ Moves)
        if katalysator == 'POSITIV': long_score  += 4
        if katalysator == 'NEGATIV': short_score += 4

        # Dollar Flow
        if cp > pp * 3: long_score  += 1
        if pp > cp * 2: short_score += 1

        # Options Sweep (+2 für institutionelle Käufe)
        if sweep.get('sweeps_call', 0) >= 2: long_score  += 2
        if sweep.get('sweeps_put',  0) >= 2: short_score += 2

        # Dark Pool (+dp_score für große Block-Trades)
        long_score  += dp_score

        # Social Hype
        if social_boost > 0 and trend_pct > 0:
            long_score += social_boost

        bc = best_option(calls, True,  price, today, exp_cutoff)
        bp = best_option(puts,  False, price, today, exp_cutoff)

        tie_goes_short = drop_from_high < -8 and short_score >= 4
        if short_score >= 4 and (short_score > long_score or tie_goes_short) and bp:
            signal, score, best, otype = 'SHORT', short_score, bp, 'PUT'
        elif long_score >= 4 and long_score > short_score and bc:
            signal, score, best, otype = 'LONG',  long_score,  bc, 'CALL'
        else:
            signal, score, best, otype = 'WATCH', 0, bc or bp, None

        conflict = signal == 'SHORT' and trend_pct > 15 and short_trend > -5

        ziel = mult = None
        if best and signal != 'WATCH':
            ziel = (price + atr) if signal == 'LONG' else (price - atr)
            gain = max(0, ziel - best['strike']) if signal == 'LONG' else max(0, best['strike'] - ziel)
            mult = f"{gain / best['pr']:.0f}x" if best['pr'] > 0 and gain > 0 else None

        return {
            't': ticker, 'price': round(price, 2), 'signal': signal, 'score': score,
            'pc': round(pc, 3), 'cp': round(cp), 'pp': round(pp),
            'trend': round(trend_pct, 1), 'prev_chg': round(prev_chg, 1),
            'drop_high': round(drop_from_high, 1), 'short_trend': round(short_trend, 1),
            'long_score': long_score, 'short_score': short_score,
            'katalysator': katalysator, 'kat_text': (al_news_text + kat_text),
            'best': best, 'otype': otype, 'ziel': ziel, 'mult': mult,
            'atr': round(atr, 2), 'today': today, 'conflict': conflict,
            'kat_url': kat_url, 'social_score': social_score,
            'dp': dp, 'sweep': sweep,
        }
    except Exception:
        return None


def run_scan(progress_cb=None):
    """
    Haupt-Scan: NUR Polygon Options Daten.
    Social-Scoring, HF, Influencer laufen in eigenen Background-Threads (app.py).
    """
    today      = datetime.now().strftime('%Y-%m-%d')
    exp_cutoff = (datetime.now() + timedelta(days=35)).strftime('%Y-%m-%d')
    news_cutoff= (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')

    # Social Trending für Universe-Erweiterung + Score-Boost (aus Cache — kein Fetch)
    social_tickers, _ = get_cached_social()
    extra_social = [t for t in social_tickers if t not in UNIVERSE]
    universe = list(UNIVERSE) + extra_social

    results = []
    total   = len(universe)
    done    = [0]
    lock    = threading.Lock()

    def scan_one(ticker):
        r = scan_ticker(ticker, today, exp_cutoff, news_cutoff)
        with lock:
            done[0] += 1
            if progress_cb:
                progress_cb(done[0], total, ticker)
        return r

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(scan_one, t) for t in universe]
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    longs  = sorted([r for r in results if r['signal'] == 'LONG'],  key=lambda x: -x['score'])
    shorts = sorted([r for r in results if r['signal'] == 'SHORT'], key=lambda x: -x['score'])
    watch  = [r for r in results if r['signal'] == 'WATCH']

    # NEXT MOVER: Kleines Cap + Katalysator + billiger Call (Hauptziel: 10%+ Mover)
    movers = sorted(
        [r for r in watch + longs
         if r['price'] < 200 and r['pc'] < 0.45
         and r['katalysator'] == 'POSITIV' and r.get('best')
         and r['best']['pr'] < 1.0],
        key=lambda x: (x['pc'], -x['long_score'])
    )[:5]

    for r in results:
        r['is_social'] = r['t'] in social_tickers

    return {
        'longs':    longs[:10],
        'shorts':   shorts[:10],
        'watch':    watch,
        'movers':   movers,
        'social':   social_tickers[:10],
        'scanned':  len(results),
        'total':    len(universe),
        'time':     datetime.now().strftime('%Y-%m-%d %H:%M'),
        'today':    today,
    }

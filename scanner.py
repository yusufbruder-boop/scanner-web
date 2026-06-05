import urllib.request, json, ssl, time, threading, os, re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

ctx = ssl.create_default_context()
API           = os.environ.get('POLYGON_API_KEY', '')
ALPACA_KEY    = os.environ.get('ALPACA_API_KEY',    '')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY', '')
NOUS_KEY      = os.environ.get('NOUS_API_KEY',      '')

_LEARNING_FILE = 'hermes_learning.json'
_patterns_cache = {'data': None, 'ts': 0}

def _load_patterns():
    """Lädt Pattern-Datenbank aus hermes_learning.json (gecacht 5 Min)."""
    global _patterns_cache
    if time.time() - _patterns_cache['ts'] < 300 and _patterns_cache['data']:
        return _patterns_cache['data']
    try:
        if os.path.exists(_LEARNING_FILE):
            d = json.load(open(_LEARNING_FILE, encoding='utf-8'))
            _patterns_cache = {'data': d, 'ts': time.time()}
            return d
    except Exception:
        pass
    return {'patterns': []}

# ── Social Trending (Reddit WSB + Stocktwits) ────────────────────────────────
_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')
_SKIP_WORDS = {'THE','AND','FOR','ARE','YOU','NOT','BUT','HAS','WAS','ALL','CAN',
               'GET','ITS','TOO','NEW','BUY','PUT','CALL','CEO','IPO','SEC','ETF',
               'LOL','WSB','DD','YOLO','ATH','ATL','IMO','IMO','TBH','GBH'}

def get_social_deep_trending() -> list:
    """
    Holt trending Tickers + erklaert WARUM sie trending sind.
    Quellen: Reddit WSB/stocks/options, Stocktwits, Yahoo.
    Gibt Liste zurueck: [{sym, score, sources, why, sentiment, top_posts}]
    """
    tickers  = {}   # sym → score
    sources  = {}   # sym → set of sources
    posts    = {}   # sym → list of top post titles
    st_sent  = {}   # sym → 'bullish'/'bearish'/'neutral'

    # ── 1) Reddit: Titel + Selftext lesen, WHY extrahieren ───────────────────
    for sub, weight in [('wallstreetbets', 1.2), ('stocks', 0.7),
                        ('options', 0.8), ('investing', 0.5)]:
        try:
            url = f'https://www.reddit.com/r/{sub}/hot.json?limit=40'
            req = urllib.request.Request(url, headers={'User-Agent': 'HermesScanner/3.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                data = json.loads(r.read())
            for post in data.get('data', {}).get('children', []):
                d      = post.get('data', {})
                title  = d.get('title', '')
                body   = d.get('selftext', '')[:300]
                score  = d.get('score', 0)
                ratio  = d.get('upvote_ratio', 0.5)
                comms  = d.get('num_comments', 0)
                flair  = d.get('link_flair_text', '') or ''
                text   = title + ' ' + body

                for m in _TICKER_RE.findall(text):
                    if m in _SKIP_WORDS or len(m) < 2:
                        continue
                    pts = int((score * ratio / 100 + comms * 0.2) * weight)
                    tickers[m] = tickers.get(m, 0) + pts
                    sources.setdefault(m, set()).add(f'r/{sub}')
                    # Post-Titel als WHY speichern (top 3 pro Ticker)
                    if title and len(posts.get(m, [])) < 3:
                        posts.setdefault(m, []).append({
                            'title': title[:100],
                            'score': score,
                            'sub':   sub,
                            'flair': flair,
                        })
        except Exception:
            pass

    # ── 2) Stocktwits: Symbol + Sentiment (bullish/bearish) ──────────────────
    try:
        url2 = 'https://api.stocktwits.com/api/2/trending/symbols.json'
        with urllib.request.urlopen(
                urllib.request.Request(url2, headers={'User-Agent': 'HermesScanner/3.0'}),
                context=ctx, timeout=8) as r:
            d2 = json.loads(r.read())
        for i, sym_data in enumerate(d2.get('symbols', [])[:20]):
            sym = sym_data.get('symbol', '')
            if not sym or sym in _SKIP_WORDS:
                continue
            tickers[sym] = tickers.get(sym, 0) + max(1, 20 - i) * 5
            sources.setdefault(sym, set()).add('Stocktwits')
    except Exception:
        pass

    # Stocktwits Stream mit Sentiment
    try:
        url3 = 'https://api.stocktwits.com/api/2/streams/trending.json?filter=all'
        with urllib.request.urlopen(
                urllib.request.Request(url3, headers={'User-Agent': 'HermesScanner/3.0'}),
                context=ctx, timeout=8) as r:
            d3 = json.loads(r.read())
        for msg in d3.get('messages', [])[:40]:
            sym_list = msg.get('symbols', [])
            if not sym_list:
                continue
            sym  = sym_list[0].get('symbol', '')
            sent = (msg.get('entities', {}).get('sentiment', {}) or {}).get('basic', '')
            body = msg.get('body', '')[:100]
            if sym and sym not in _SKIP_WORDS:
                tickers[sym] = tickers.get(sym, 0) + 12
                sources.setdefault(sym, set()).add('Stocktwits-Stream')
                if sent in ('Bullish', 'Bearish'):
                    cur = st_sent.get(sym, {})
                    cur[sent] = cur.get(sent, 0) + 1
                    st_sent[sym] = cur
                if body and len(posts.get(sym, [])) < 3:
                    posts.setdefault(sym, []).append({
                        'title': body,
                        'score': 0,
                        'sub': 'Stocktwits',
                        'flair': sent or '',
                    })
    except Exception:
        pass

    # ── 3) Yahoo Trending ─────────────────────────────────────────────────────
    try:
        url4 = 'https://query1.finance.yahoo.com/v1/finance/trending/US?count=25'
        with urllib.request.urlopen(
                urllib.request.Request(url4, headers={'User-Agent': 'Mozilla/5.0'}),
                context=ctx, timeout=8) as r:
            d4 = json.loads(r.read())
        quotes = d4.get('finance', {}).get('result', [{}])[0].get('quotes', [])
        for i, q in enumerate(quotes[:25]):
            sym = q.get('symbol', '').split('.')[0].split('-')[0]
            if sym and sym not in _SKIP_WORDS and len(sym) <= 5:
                tickers[sym] = tickers.get(sym, 0) + max(1, 25 - i) * 3
                sources.setdefault(sym, set()).add('Yahoo')
    except Exception:
        pass

    # ── 4) WHY-Analyse: Warum ist der Ticker trending? ───────────────────────
    WHY_PATTERNS = [
        # Earnings
        (['earnings', 'eps', 'beat', 'miss', 'guidance', 'revenue', 'quarter', 'q1','q2','q3','q4'],
         'EARNINGS'),
        # Analyst
        (['upgrade', 'downgrade', 'price target', 'pt ', 'overweight', 'buy rating', 'analyst'],
         'ANALYST'),
        # News/Deal
        (['acquisition', 'merger', 'deal', 'partnership', 'contract', 'buyout', 'acquired'],
         'M&A/DEAL'),
        # Short Squeeze
        (['short squeeze', 'short interest', 'gamma squeeze', 'squeeze', 'yolo', 'puts', 'calls expiring'],
         'SHORT-SQUEEZE'),
        # Technical
        (['breakout', 'all-time high', 'ath', 'support', 'resistance', 'moving average', 'rsi'],
         'TECHNICAL'),
        # FDA/Gov
        (['fda', 'approval', 'trial', 'phase', 'drug', 'clinical'],
         'FDA/BIOTECH'),
        # AI/Tech hype
        (['ai ', 'artificial intelligence', 'nvidia', 'chip', 'data center', 'llm', 'chatgpt'],
         'AI-HYPE'),
        # Macro
        (['fed', 'interest rate', 'inflation', 'recession', 'tariff', 'china', 'war', 'oil'],
         'MAKRO'),
    ]

    result = []
    sorted_t = sorted(tickers.items(), key=lambda x: -x[1])

    for sym, score in sorted_t[:20]:
        if score < 5:
            continue
        sym_posts = posts.get(sym, [])
        all_text  = ' '.join(p['title'].lower() for p in sym_posts)

        # WHY bestimmen
        why_tags = []
        for keywords, tag in WHY_PATTERNS:
            if any(k in all_text for k in keywords):
                why_tags.append(tag)

        # Sentiment aus Stocktwits
        sent_data = st_sent.get(sym, {})
        bull = sent_data.get('Bullish', 0)
        bear = sent_data.get('Bearish', 0)
        if bull > bear * 1.5:
            sentiment = 'BULLISH'
        elif bear > bull * 1.5:
            sentiment = 'BEARISH'
        elif bull or bear:
            sentiment = 'MIXED'
        else:
            sentiment = 'NEUTRAL'

        # Bester Post-Titel als Kurzgründung
        top_title = ''
        if sym_posts:
            best = max(sym_posts, key=lambda p: p.get('score', 0))
            top_title = best['title']

        result.append({
            'sym':       sym,
            'score':     score,
            'sources':   list(sources.get(sym, set())),
            'why':       why_tags if why_tags else ['MENTIONS'],
            'sentiment': sentiment,
            'top_post':  top_title,
            'post_count': len(sym_posts),
        })

    return result


def analyze_social_smart_money(trending_list: list) -> list:
    """
    Fuer jeden Trending-Stock: Smart Money Check + KI-Urteil.
    - Dark Pool Richtung (BUY/SELL/NEUTRAL)
    - Options Flow: Call vs Put Premium, Vol/OI, Sweeps
    - Divergenz: Retail bullish aber Smart Money verkauft?
    - KI-Urteil: LONG (Trend intakt) / SHORT (Trend erschoepft) / NEUTRAL
    """
    today = datetime.now().strftime('%Y-%m-%d')
    results = []

    def _check_sm(item):
        sym      = item['sym']
        why      = item.get('why', [])
        ret_sent = item.get('sentiment', 'NEUTRAL')  # Retail-Sentiment
        top_post = item.get('top_post', '')

        sm_data = {
            'sym':         sym,
            'why':         why,
            'ret_sent':    ret_sent,
            'top_post':    top_post,
            'sources':     item.get('sources', []),
            'social_score':item.get('score', 0),
            # Smart Money Felder
            'dp_dir':      'NEUTRAL',
            'dp_dollar':   0,
            'call_prem':   0,
            'put_prem':    0,
            'pc_ratio':    1.0,
            'call_voi':    0.0,
            'put_voi':     0.0,
            'call_sweeps': 0,
            'put_sweeps':  0,
            'price':       0,
            'prev_chg':    0,
            # Divergenz
            'divergence':  '',
            # Endergebnis
            'verdict':     'NEUTRAL',
            'verdict_score': 0,
            'verdict_reason': '',
        }

        # 1) Options Snapshot
        try:
            opt = poly_fetch(f'https://api.polygon.io/v3/snapshot/options/{sym}?limit=200&apiKey={API}')
            res = opt.get('results', [])
            if res:
                sm_data['price'] = res[0].get('underlying_asset', {}).get('price', 0)
                calls = [r for r in res if r['details']['contract_type'] == 'call']
                puts  = [r for r in res if r['details']['contract_type'] == 'put']
                cv = sum(r['day'].get('volume', 0) for r in calls)
                pv = sum(r['day'].get('volume', 0) for r in puts)
                cp = sum(r['day'].get('volume',0)*(r['day'].get('close') or 0)*100 for r in calls)
                pp = sum(r['day'].get('volume',0)*(r['day'].get('close') or 0)*100 for r in puts)
                mc = max((r['day'].get('volume',0)/max(r.get('open_interest',1),1) for r in calls), default=0)
                mp = max((r['day'].get('volume',0)/max(r.get('open_interest',1),1) for r in puts),  default=0)
                sw = get_options_sweep(res)
                sm_data.update({
                    'call_prem':   round(cp / 1e6, 2),
                    'put_prem':    round(pp / 1e6, 2),
                    'pc_ratio':    round(pv / max(cv, 1), 3),
                    'call_voi':    round(mc, 1),
                    'put_voi':     round(mp, 1),
                    'call_sweeps': sw.get('sweeps_call', 0),
                    'put_sweeps':  sw.get('sweeps_put', 0),
                })
        except Exception:
            pass

        # 2) Dark Pool Richtung
        try:
            dp = get_darkpool_signal(sym, today)
            sm_data['dp_dir']    = dp.get('direction', 'NEUTRAL')
            sm_data['dp_dollar'] = dp.get('dp_total', 0)
        except Exception:
            pass

        # 3) Kursveraenderung (Alpaca Snapshot)
        try:
            snap = get_alpaca_snapshot([sym]).get(sym, {})
            d_bar  = snap.get('dailyBar', {})
            p_bar  = snap.get('prevDailyBar', {})
            price  = float(d_bar.get('c') or 0)
            pprice = float(p_bar.get('c') or price or 1)
            if price and pprice:
                sm_data['prev_chg'] = round((price - pprice) / pprice * 100, 2)
                if not sm_data['price']:
                    sm_data['price'] = price
        except Exception:
            pass

        # 4) Divergenz erkennen: Retail vs Smart Money
        cp_val = sm_data['call_prem']
        pp_val = sm_data['put_prem']
        dp_dir = sm_data['dp_dir']
        chg    = sm_data['prev_chg']
        pc     = sm_data['pc_ratio']

        if ret_sent == 'BULLISH' and pp_val > cp_val * 1.5:
            sm_data['divergence'] = 'BEAR_DIV'   # Retail bullish aber Smart Money kauft Puts
        elif ret_sent == 'BEARISH' and cp_val > pp_val * 1.5:
            sm_data['divergence'] = 'BULL_DIV'   # Retail bearish aber Smart Money kauft Calls
        elif ret_sent == 'BULLISH' and dp_dir == 'SELL':
            sm_data['divergence'] = 'DP_SELL'    # Retail bullish aber Dark Pool verkauft
        elif ret_sent == 'BEARISH' and dp_dir == 'BUY':
            sm_data['divergence'] = 'DP_BUY'     # Retail bearish aber Dark Pool kauft

        # 5) Scoring & Urteil
        bull = bear = 0
        reasons = []

        # Call/Put Flow
        if cp_val > pp_val * 1.5:
            bull += 3; reasons.append(f'Call-Premium ${cp_val:.1f}M dominiert')
        elif pp_val > cp_val * 1.5:
            bear += 3; reasons.append(f'Put-Premium ${pp_val:.1f}M dominiert')

        # Vol/OI
        if sm_data['call_voi'] >= 8:
            bull += 2; reasons.append(f'Call VOI {sm_data["call_voi"]:.0f}x')
        elif sm_data['call_voi'] >= 4:
            bull += 1
        if sm_data['put_voi'] >= 8:
            bear += 2; reasons.append(f'Put VOI {sm_data["put_voi"]:.0f}x')
        elif sm_data['put_voi'] >= 4:
            bear += 1

        # Sweeps
        if sm_data['call_sweeps'] >= 3:
            bull += 2; reasons.append(f'{sm_data["call_sweeps"]} Call-Sweeps')
        if sm_data['put_sweeps'] >= 3:
            bear += 2; reasons.append(f'{sm_data["put_sweeps"]} Put-Sweeps')

        # Dark Pool
        if dp_dir == 'BUY' and sm_data['dp_dollar'] >= 1_000_000:
            bull += 3; reasons.append(f'Dark Pool KAUF ${sm_data["dp_dollar"]/1e6:.1f}M')
        elif dp_dir == 'SELL' and sm_data['dp_dollar'] >= 1_000_000:
            bear += 3; reasons.append(f'Dark Pool VERKAUF ${sm_data["dp_dollar"]/1e6:.1f}M')

        # Retail Sentiment leicht gewichten
        if ret_sent == 'BULLISH':
            bull += 1
        elif ret_sent == 'BEARISH':
            bear += 1

        # WHY-Katalysatoren
        if 'EARNINGS' in why:
            bull += 1; bear += 1   # neutral — kann beide Richtungen
        if 'SHORT-SQUEEZE' in why:
            bull += 2; reasons.append('Short-Squeeze Signal')

        # Divergenz: gegen Retail-Trend = stärkeres Signal
        div = sm_data['divergence']
        if div == 'BEAR_DIV':
            bear += 2; reasons.append('Divergenz: Retail Bull aber Smart Money SHORT')
        elif div == 'BULL_DIV':
            bull += 2; reasons.append('Divergenz: Retail Bear aber Smart Money LONG')
        elif div == 'DP_SELL':
            bear += 1; reasons.append('Dark Pool verkauft trotz Retail-Euphorie')
        elif div == 'DP_BUY':
            bull += 1; reasons.append('Dark Pool kauft trotz Retail-Panik')

        # Trend-Erschoepfung: Kurs bereits stark gestiegen + Put-Absicherung
        if chg > 5 and pc > 1.0:
            bear += 1; reasons.append(f'Kurs +{chg:.1f}% aber P/C={pc:.2f} — Absicherung läuft')
        elif chg < -5 and pc < 0.6:
            bull += 1; reasons.append(f'Kurs {chg:.1f}% aber Call-Käufer aktiv — Bounce?')

        # Urteil
        net = bull - bear
        if net >= 3:
            verdict = 'LONG'
        elif net <= -3:
            verdict = 'SHORT'
        else:
            verdict = 'NEUTRAL'

        sm_data['verdict']        = verdict
        sm_data['verdict_score']  = net
        sm_data['verdict_reason'] = ' | '.join(reasons[:3])
        sm_data['bull_pts']       = bull
        sm_data['bear_pts']       = bear
        return sm_data

    # Parallel für Top 10 Trending
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_check_sm, item): item for item in trending_list[:10]}
        for f in as_completed(futs):
            try:
                r = f.result()
                if r:
                    results.append(r)
            except Exception:
                pass

    # Sortieren: SHORT/LONG zuerst (stärkstes Signal vorne)
    results.sort(key=lambda x: abs(x.get('verdict_score', 0)), reverse=True)
    return results


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
    ('https://www.astralcodexten.com/feed', 'Scott Alexander (ACX)'),
    ('https://www.noahpinion.blog/feed',    'Noah Smith'),
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
    # Finanzwerte + Rotation
    'GS','JPM','BAC','C','WFC','MS','V','MA','AXP','BX','KRE','XLF',
    # Defensive + Rotation
    'XLV','XLP','XLU','XLE','XLI',
    'ASTS','LUNR','RKLB',
    'GLD','SLV','USO','AAL','DAL',
    'MSTR','COIN','HOOD','SOFI','ORCL','NOW',
    'XOM','CVX','LMT','RTX',
    # Erweiterung: häufige Mover mit Katalysatoren
    'SNOW','PANW','UBER','LYFT','RIVN','LCID','NIO','F','GM',
    'SHOP','SQ','PYPL','ROKU','SPOT','PINS','SNAP',
    'ENPH','SEDG','FSLR','NEE','HIMS','CELH','WOLF',
    # Situational Awareness LP (Aschenbrenner) — 13F Q1 2026
    # LONG: KI-Infrastruktur + Mining
    'NBIS','KEEL','CLSK','RIOT','BTDR','APLD','CRWV','BE',
    # SHORT (PUT): Semiconductors
    'SMH',
]

POS_KEYS = ['contract','government','deal','partnership','upgrade','raised','beat',
            'record','billion','trump','invest','breakthrough','ai','quantum','launch',
            'buyback','dividend','acquisition','target','infrastructure','pivot',
            'revenue','earnings','profit','surge','soar','stake','award','fda',
            'approval','patent','merger','spin','ipo','buyout','license','guidance',
            'custom silicon','custom asic','inference','hyperscaler','data center',
            'ai chip','ai revenue','compute','semiconductor design']
NEG_KEYS = ['lawsuit','downgrade','miss','cut','investigation','fraud',
            'recall','ban','warning','below','probe','short seller','loss',
            'decline','disappoint','weak','concern','risk','violation','delay',
            'bankruptcy','default','dilut','offering','withdrew',
            'secondary','share issuance','equity raise','stock offering','new shares']
# Dilution/Secondary — überschreibt immer positive News
HARD_NEG_KEYS = ['dilut','secondary offering','share offering','equity offering',
                 'stock offering','new shares','share issuance','equity raise']

# ── HIGH-IMPACT News-Katalysatoren (Score +5) ────────────────────────────────
# Regierungs- und Militärverträge
HIGH_IMPACT_GOV = ['pentagon','department of defense','dod contract','military contract',
                   'government contract','federal contract','awarded contract',
                   'defense contract','national security','air force','navy contract',
                   'army contract','nato','space force','doge contract','white house deal']
# CEO/Analyst Endorsement + Next Big Thing
HIGH_IMPACT_ENDORSE = ['next trillion','next billion dollar','ceo predicts','nvidia ceo',
                       'jensen huang','elon musk','tim cook','satya nadella',
                       'next $1 billion','next 1 trillion','names as next',
                       'will be next','biggest winner','top pick 2025','top pick 2026',
                       'best ai play','number one ai','dominant ai']
# Insider-Kauf (stark bullish)
HIGH_IMPACT_INSIDER = ['trump bought','trump buys','trump purchased','congressman bought',
                       'senator bought','insider purchased','ceo bought shares',
                       'ceo purchased','executive bought','board member bought',
                       'trump administration buys','white house purchased']

# ── EXTREME-IMPACT: Insider-Kauf + Gov-Vertrag (Score +7) ────────────────────
EXTREME_COMBO_GOV   = HIGH_IMPACT_GOV
EXTREME_COMBO_BUY   = HIGH_IMPACT_INSIDER

# ── HIGH-IMPACT Short Katalysatoren (Score +5) ────────────────────────────────
HIGH_IMPACT_NEG = ['sec charges','doj investigation','class action','criminal charges',
                   'going concern','chapter 11','chapter 7','fda rejection',
                   'clinical failure','trial failed','missed revenue','massive loss',
                   'accounting fraud','restatement','deregistered','delisted',
                   'short seller report']

# ── Macro Context (VIX, Yields, Indices, Fed) ────────────────────────────────
_macro_cache = {'data': {}, 'ts': 0}
_macro_lock  = threading.Lock()

def get_macro_context() -> dict:
    """VIX, 10Y Yield, S&P, NASDAQ, Dollar via Yahoo Finance. 30 Min Cache."""
    with _macro_lock:
        if time.time() - _macro_cache['ts'] < 1800:
            return _macro_cache['data']
    result = {}
    symbols = {'VIX': '^VIX', 'TNX': '^TNX', 'SPX': '^GSPC', 'NDX': '^NDX', 'DXY': 'DX-Y.NYB'}
    for name, sym in symbols.items():
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d'
            req = urllib.request.Request(url, headers={'User-Agent': 'hermes/3.0'})
            with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
                d = json.loads(r.read())
            meta = d['chart']['result'][0]['meta']
            price = float(meta.get('regularMarketPrice', 0))
            prev  = float(meta.get('chartPreviousClose', price) or price)
            chg   = round((price - prev) / prev * 100, 2) if prev else 0
            result[name] = {'price': round(price, 2), 'chg': chg}
        except Exception:
            pass
    vix = result.get('VIX', {}).get('price', 20)
    result['regime'] = ('LOW_VOL_BULL' if vix < 15 else
                        'NORMAL'       if vix < 20 else
                        'ELEVATED_RISK' if vix < 30 else 'HIGH_FEAR')
    # Federal Reserve + Economic news via Fed RSS
    try:
        import xml.etree.ElementTree as ET
        req = urllib.request.Request('https://www.federalreserve.gov/feeds/press_all.xml',
                                     headers={'User-Agent': 'hermes/3.0'})
        with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
            root = ET.fromstring(r.read())
        fed_news = []
        for item in root.findall('.//item')[:5]:
            title = item.findtext('title', '')
            if title:
                fed_news.append(title[:80])
        result['fed_news'] = fed_news
    except Exception:
        result['fed_news'] = []
    with _macro_lock:
        _macro_cache['data'] = result
        _macro_cache['ts']   = time.time()
    return result


# ── SEC EDGAR Alerts (8-K + Form 4 Insider) ──────────────────────────────────
_sec_cache = {'data': [], 'ts': 0}
_sec_lock  = threading.Lock()

def get_sec_alerts(tickers: list = None) -> list:
    """Aktuelle SEC 8-K + Form 4 für Universe-Stocks via EDGAR EFTS. 1h Cache."""
    with _sec_lock:
        if time.time() - _sec_cache['ts'] < 3600:
            return _sec_cache['data']
    today   = datetime.now().strftime('%Y-%m-%d')
    week_ago = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    alerts  = []
    watch   = (tickers or UNIVERSE)[:25]
    for ticker in watch:
        for form, label in [('8-K', 'Material Event'), ('4', 'Insider Trade')]:
            try:
                url = (f'https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22'
                       f'&dateRange=custom&startdt={week_ago}&enddt={today}&forms={form}')
                req = urllib.request.Request(url,
                    headers={'User-Agent': 'hermes-scanner/3.0 yusufbruder@gmail.com'})
                with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
                    d = json.loads(r.read())
                for hit in d.get('hits', {}).get('hits', [])[:2]:
                    src = hit.get('_source', {})
                    filing_date = src.get('file_date', today)
                    entity = src.get('entity_name', ticker)[:40]
                    form_desc = src.get('form_type', form)
                    alerts.append({
                        'ticker': ticker, 'form': form_desc, 'label': label,
                        'title': f'{entity} — {form_desc}',
                        'date':  filing_date,
                        'fresh': filing_date >= today,
                    })
            except Exception:
                pass
    with _sec_lock:
        _sec_cache['data'] = alerts
        _sec_cache['ts']   = time.time()
    return alerts


# ── Wirtschaftskalender via Polygon ──────────────────────────────────────────
def get_earnings_calendar_polygon(tickers: list) -> dict:
    """Earnings-Termine für Ticker via Polygon Reference. Gibt {ticker: date} zurück."""
    result = {}
    today = datetime.now().strftime('%Y-%m-%d')
    cutoff = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
    for t in tickers[:20]:
        try:
            url = f'https://api.polygon.io/vX/reference/financials?ticker={t}&limit=1&apiKey={API}'
            d = poly_fetch(url)
            for r in d.get('results', []):
                ed = r.get('end_date', '')
                if today <= ed <= cutoff:
                    result[t] = ed
        except Exception:
            pass
    return result


# ── Dark Pool / Block Trade Detection (Polygon Trades API) ───────────────────
def get_market_sentiment() -> dict:
    """
    Gesamtmarkt-Sentiment via Put/Call Ratio (SPY + QQQ + IWM).
    Ruft Calls und Puts separat ab um Polygon-Limit zu umgehen.
    Gibt dict zurück: pc_total, pc_spy, pc_qqq, pc_iwm, signal, score, emoji
    """
    syms = ['SPY', 'QQQ', 'IWM']
    total_c = total_p = 0
    per_sym = {}

    def _fetch_vol(sym, ctype):
        url = f'https://api.polygon.io/v3/snapshot/options/{sym}?limit=250&contract_type={ctype}&apiKey={API}'
        d = poly_fetch(url)
        return sum(r['day'].get('volume', 0) for r in d.get('results', []))

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        tasks = [(s, t) for s in syms for t in ['call', 'put']]
        vols = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_fetch_vol, s, t): (s, t) for s, t in tasks}
            for fut, key in as_completed(futs, timeout=20):
                try:
                    vols[key] = fut.result()
                except Exception:
                    vols[key] = 0

        for sym in syms:
            cv = vols.get((sym, 'call'), 0)
            pv = vols.get((sym, 'put'),  0)
            total_c += cv
            total_p += pv
            per_sym[sym] = {'calls': cv, 'puts': pv, 'pc': round(pv / cv, 2) if cv else 0}
    except Exception:
        pass

    pc = round(total_p / total_c, 2) if total_c else 0

    if   pc > 2.0: signal, emoji, score = 'EXTREME FEAR',     '🔴🔴', 10
    elif pc > 1.5: signal, emoji, score = 'STARK BEARISH',    '🔴',    8
    elif pc > 1.2: signal, emoji, score = 'BEARISH',          '🟠',    6
    elif pc > 0.9: signal, emoji, score = 'LEICHT BEARISH',   '🟡',    4
    elif pc > 0.7: signal, emoji, score = 'NEUTRAL',          '⚪',    2
    elif pc > 0.5: signal, emoji, score = 'LEICHT BULLISH',   '🟢',    1
    else:          signal, emoji, score = 'BULLISH',          '🟢🟢',  0

    # Boden-Wahrscheinlichkeit: hohe P/C + QQQ Dip-Käufer = Reversal möglich
    qqq_pc = per_sym.get('QQQ', {}).get('pc', 0)
    spy_pc  = per_sym.get('SPY', {}).get('pc', 0)
    bottom_signal = (pc > 1.3 and qqq_pc < spy_pc * 0.8)  # QQQ wird stärker gekauft als SPY

    return {
        'pc_total':      pc,
        'pc_spy':        per_sym.get('SPY', {}).get('pc', 0),
        'pc_qqq':        per_sym.get('QQQ', {}).get('pc', 0),
        'pc_iwm':        per_sym.get('IWM', {}).get('pc', 0),
        'calls_total':   total_c,
        'puts_total':    total_p,
        'signal':        signal,
        'emoji':         emoji,
        'score':         score,
        'bottom_signal': bottom_signal,
        'per_sym':       per_sym,
    }


def get_darkpool_signal(ticker: str, today: str) -> dict:
    """
    Dark Pool Direction: erkennt ob institutionelle KAUFEN oder VERKAUFEN.
    Methode: Vergleich Trade-Preis mit Rolling-VWAP der Session.
    - Großer Trade UEBER VWAP = Akkumulation (bullish)
    - Großer Trade UNTER VWAP = Distribution (bearish)
    Dark pool conditions: 37=Large Block, 41=OTC/Dark Pool, 20, 29, 80, 81.
    """
    try:
        start = f'{today}T13:30:00Z'
        url   = (f'https://api.polygon.io/v3/trades/{ticker}'
                 f'?timestamp.gte={start}&order=asc&limit=500&apiKey={API}')
        data   = poly_fetch(url)
        trades = data.get('results', [])
        if not trades:
            return {}

        # VWAP berechnen aus allen Trades (Preis × Größe / Gesamtgröße)
        total_vol = sum(t.get('size', 0) or 0 for t in trades)
        total_pv  = sum((t.get('size', 0) or 0) * (t.get('price', 0) or 0) for t in trades)
        vwap = total_pv / total_vol if total_vol > 0 else 0

        DP_CONDS = {20, 29, 37, 41, 80, 81}
        large_all = []
        dark_prints = []
        buy_dollar = sell_dollar = 0   # Richtungs-Tracking

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
                # Richtung: über VWAP = institutioneller Kauf, unter VWAP = Verkauf
                if vwap > 0:
                    if price >= vwap * 1.001:   # mindestens 0.1% über VWAP
                        buy_dollar  += dollar
                    elif price <= vwap * 0.999:
                        sell_dollar += dollar
                    else:                        # neutral (nahe VWAP)
                        buy_dollar  += dollar * 0.5
                        sell_dollar += dollar * 0.5

        if not large_all:
            return {}

        total_dir = buy_dollar + sell_dollar
        buy_pct   = round(buy_dollar  / total_dir * 100) if total_dir > 0 else 50
        sell_pct  = round(sell_dollar / total_dir * 100) if total_dir > 0 else 50
        # Richtung: klares Signal ab 60% in eine Richtung
        if buy_pct >= 65:
            direction = 'BUY'
        elif sell_pct >= 65:
            direction = 'SELL'
        else:
            direction = 'NEUTRAL'

        return {
            'count':     len(large_all),
            'dp_count':  len(dark_prints),
            'total':     int(sum(large_all)),
            'dp_total':  int(sum(dark_prints)),
            'largest':   int(max(large_all)),
            'vwap':      round(vwap, 2),
            'buy_pct':   buy_pct,
            'sell_pct':  sell_pct,
            'direction': direction,   # NEU: BUY / SELL / NEUTRAL
        }
    except Exception:
        return {}


# ── MAG 7 Markt-Signal (führender Indikator für NASDAQ) ──────────────────────
MAG7 = ['AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'NVDA', 'TSLA']

_mag7_cache = {'data': None, 'ts': 0}

def get_mag7_market_signal() -> dict:
    """
    Aggregiertes Options-Flow Signal der Magnificent 7.
    Logik: 4+ von 7 bullish = NASDAQ steigt (Leading Indicator ~15-60 Min voraus).
    4+ von 7 bearish = NASDAQ fällt oder dreht.
    Divergenz: Index steigt aber Mag7 bearish = Distribution, Umkehr kommt.
    """
    global _mag7_cache
    if time.time() - _mag7_cache['ts'] < 300 and _mag7_cache['data']:
        return _mag7_cache['data']

    today = datetime.now().strftime('%Y-%m-%d')
    results = {}

    def _check_one(sym):
        try:
            opt = poly_fetch(f'https://api.polygon.io/v3/snapshot/options/{sym}?limit=200&apiKey={API}')
            res = opt.get('results', [])
            if not res:
                return None
            price = res[0].get('underlying_asset', {}).get('price', 0)
            calls = [r for r in res if r['details']['contract_type'] == 'call']
            puts  = [r for r in res if r['details']['contract_type'] == 'put']

            cv = sum(r['day'].get('volume', 0) for r in calls)
            pv = sum(r['day'].get('volume', 0) for r in puts)
            # Call Premium vs Put Premium (Smart Money bevorzugt Prämie)
            cp = sum(r['day'].get('volume', 0) * (r['day'].get('close') or 0) * 100 for r in calls)
            pp = sum(r['day'].get('volume', 0) * (r['day'].get('close') or 0) * 100 for r in puts)

            # Sweep Detektion
            sweep = get_options_sweep(res)
            sc = sweep.get('sweeps_call', 0)
            sp = sweep.get('sweeps_put', 0)

            # Ungewöhnliche Vol/OI
            max_call_voi = max(
                (r['day'].get('volume',0) / max(r.get('open_interest',1),1) for r in calls), default=0)
            max_put_voi  = max(
                (r['day'].get('volume',0) / max(r.get('open_interest',1),1) for r in puts),  default=0)

            # Richtung bestimmen (mehrere Faktoren)
            bull_pts = bear_pts = 0
            reasons_b = []
            reasons_s = []

            pc = pv / cv if cv > 0 else 1
            if pc < 0.4:
                bull_pts += 2; reasons_b.append(f'P/C={pc:.2f}')
            elif pc < 0.6:
                bull_pts += 1
            elif pc > 1.2:
                bear_pts += 2; reasons_s.append(f'P/C={pc:.2f}')
            elif pc > 0.9:
                bear_pts += 1

            if cp > pp * 1.5:
                bull_pts += 2; reasons_b.append(f'CallPrem${cp/1e6:.1f}M')
            elif pp > cp * 1.5:
                bear_pts += 2; reasons_s.append(f'PutPrem${pp/1e6:.1f}M')

            if sc >= 3:
                bull_pts += 2; reasons_b.append(f'{sc}CallSweep')
            elif sc >= 1:
                bull_pts += 1
            if sp >= 3:
                bear_pts += 2; reasons_s.append(f'{sp}PutSweep')
            elif sp >= 1:
                bear_pts += 1

            if max_call_voi >= 8:
                bull_pts += 2; reasons_b.append(f'CallVOI{max_call_voi:.0f}x')
            elif max_call_voi >= 4:
                bull_pts += 1
            if max_put_voi >= 8:
                bear_pts += 2; reasons_s.append(f'PutVOI{max_put_voi:.0f}x')
            elif max_put_voi >= 4:
                bear_pts += 1

            if bull_pts >= bear_pts + 2:
                flow = 'BULL'
            elif bear_pts >= bull_pts + 2:
                flow = 'BEAR'
            else:
                flow = 'NEUTRAL'

            return {
                'sym': sym, 'price': price, 'flow': flow,
                'bull_pts': bull_pts, 'bear_pts': bear_pts,
                'pc': round(pc, 2), 'sc': sc, 'sp': sp,
                'reasons_bull': reasons_b[:3], 'reasons_bear': reasons_s[:3],
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_check_one, sym): sym for sym in MAG7}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results[r['sym']] = r

    if not results:
        return {}

    bull_count = sum(1 for r in results.values() if r['flow'] == 'BULL')
    bear_count = sum(1 for r in results.values() if r['flow'] == 'BEAR')
    neut_count = len(results) - bull_count - bear_count

    # Markt-Signal: 4+ von 7 = klares Signal
    if bull_count >= 4:
        market_dir = 'BULL'
        conf = round(bull_count / len(results), 2)
    elif bear_count >= 4:
        market_dir = 'BEAR'
        conf = round(bear_count / len(results), 2)
    else:
        market_dir = 'MIXED'
        conf = 0.0

    # Stärkste Signale (die die Richtung führen)
    leaders_bull = [s for s, r in results.items() if r['flow'] == 'BULL']
    leaders_bear = [s for s, r in results.items() if r['flow'] == 'BEAR']

    out = {
        'direction':    market_dir,
        'confidence':   conf,
        'bull_count':   bull_count,
        'bear_count':   bear_count,
        'neutral_count':neut_count,
        'checked':      len(results),
        'leaders_bull': leaders_bull,
        'leaders_bear': leaders_bear,
        'details':      results,
        'ts':           datetime.now().strftime('%H:%M'),
        'summary': (f'Mag7: {bull_count}BULL/{bear_count}BEAR/{neut_count}MIX'
                    + (f' → {market_dir} {conf:.0%}' if market_dir != 'MIXED' else '')),
    }
    _mag7_cache = {'data': out, 'ts': time.time()}
    return out


def detect_flow_divergence(prev_chg: float, cv: int, pv: int,
                            sc: int, sp: int, dp_dir: str) -> dict:
    """
    Erkennt Divergenz zwischen Preis und Options-Flow.
    Preis steigt aber PUT-Flow dominiert = Hidden Distribution (BEAR kommt).
    Preis fällt aber CALL-Flow dominiert = Smart Accumulation (BULL kommt).
    Das ist der wichtigste Leading Indicator — tritt 15-60 Min vor Preisumkehr auf.
    """
    if cv + pv == 0:
        return {'type': 'NONE', 'strength': 0, 'msg': ''}

    pc = pv / max(cv, 1)

    # Preis STEIGT aber Institutionelle kaufen PUTS
    if prev_chg > 0.3 and pc > 1.5:
        strength = min(round(pc * prev_chg / 2, 1), 5)
        dp_note  = ' + Dark Pool SELL' if dp_dir == 'SELL' else ''
        return {
            'type':     'BEAR_DIV',
            'strength': strength,
            'msg':      f'WARNUNG: Preis +{prev_chg:.1f}% aber P/C={pc:.2f} — Distribution{dp_note}',
        }

    # Preis FÄLLT aber Institutionelle kaufen CALLS
    if prev_chg < -0.3 and pc < 0.5:
        strength = min(round((1/pc) * abs(prev_chg) / 2, 1), 5)
        dp_note  = ' + Dark Pool BUY' if dp_dir == 'BUY' else ''
        return {
            'type':     'BULL_DIV',
            'strength': strength,
            'msg':      f'REVERSAL: Preis {prev_chg:.1f}% aber P/C={pc:.2f} — Akkumulation{dp_note}',
        }

    # Dark Pool contra Preis (schwächeres Signal)
    if prev_chg > 0.5 and dp_dir == 'SELL':
        return {'type': 'BEAR_DIV_SOFT', 'strength': 1,
                'msg': f'Dark Pool SELL bei Preis +{prev_chg:.1f}%'}
    if prev_chg < -0.5 and dp_dir == 'BUY':
        return {'type': 'BULL_DIV_SOFT', 'strength': 1,
                'msg': f'Dark Pool BUY bei Preis {prev_chg:.1f}%'}

    return {'type': 'NONE', 'strength': 0, 'msg': ''}


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


# ── Polygon Top Movers (Gainers/Losers — 24/7) ───────────────────────────────
def get_market_movers() -> list:
    """Polygon Gainers + Losers — direkt aus Polygon-Abo, läuft 24/7."""
    movers = []
    for direction, label in [('gainers', 'Gainer'), ('losers', 'Loser')]:
        try:
            url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{direction}?apiKey={API}'
            data = poly_fetch(url)
            for t in data.get('tickers', [])[:15]:
                sym  = t.get('ticker', '')
                day  = t.get('day', {})
                prev = t.get('prevDay', {})
                price = float(day.get('c') or 0)
                pc    = float(prev.get('c') or price)
                chg   = round((price - pc) / pc * 100, 1) if pc else 0
                vol   = int(day.get('v') or 0)
                if sym and 2 <= len(sym) <= 5 and abs(chg) >= 2:
                    movers.append({'sym': sym, 'chg': chg, 'price': round(price, 2),
                                   'vol': vol, 'label': label})
        except Exception:
            pass
    return movers


def hermes_24h_scan() -> list:
    """
    Hermes 24h Intelligence — filtert alle Polygon-Daten der letzten 24h:
    1. Gainers/Losers mit starkem Volumen (HPE-Typ Anomalie)
    2. Tickers mit Vol/OI > 3x auf Options (institutionelle Positionierung)
    3. Dark Pool > $1M in letzten 24h
    4. Polygon News mit starkem Sentiment
    5. Earnings in nächsten 7 Tagen (binäre Events)
    Gibt eine sortierte Liste potenzieller Next-Mover zurück.
    """
    today     = datetime.now().strftime('%Y-%m-%d')
    from_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    signals   = []

    # 1) Polygon Gainers + Losers (Top-Mover 24h)
    candidates = {}
    for direction in ['gainers', 'losers']:
        try:
            url = f'https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{direction}?apiKey={API}'
            d   = poly_fetch(url)
            for t in d.get('tickers', [])[:50]:
                sym   = t.get('ticker', '')
                day   = t.get('day', {})
                prev  = t.get('prevDay', {})
                price = float(day.get('c') or 0)
                pc    = float(prev.get('c') or price or 1)
                chg   = round((price - pc) / pc * 100, 1) if pc else 0
                vol   = int(day.get('v') or 0)
                pvol  = int(prev.get('v') or 1)
                vol_ratio = round(vol / pvol, 1) if pvol else 0
                if sym and 2 <= len(sym) <= 6 and price >= 5:
                    candidates[sym] = {
                        'sym': sym, 'price': price, 'chg': chg,
                        'vol': vol, 'vol_ratio': vol_ratio,
                        'direction': direction, 'score': 0, 'reasons': []
                    }
        except Exception:
            pass

    # 2) Für jeden Kandidaten: Options Vol/OI + Dark Pool + News checken
    def _check_24h(sym, info):
        score   = 0
        reasons = list(info.get('reasons', []))
        price   = info['price']
        chg     = info['chg']
        vol_r   = info['vol_ratio']

        # Volumen-Anomalie (HPE hatte 8x normales Volumen)
        if vol_r >= 8:
            score += 6
            reasons.append(f'Volumen {vol_r:.0f}x normal — EXTREMES Signal')
        elif vol_r >= 5:
            score += 4
            reasons.append(f'Volumen {vol_r:.0f}x normal')
        elif vol_r >= 3:
            score += 2
            reasons.append(f'Volumen {vol_r:.0f}x normal')

        # Kursveränderung
        if abs(chg) >= 15:
            score += 4
            reasons.append(f'{chg:+.1f}% heute — Mega-Move')
        elif abs(chg) >= 8:
            score += 3
            reasons.append(f'{chg:+.1f}% heute')
        elif abs(chg) >= 5:
            score += 2
            reasons.append(f'{chg:+.1f}% heute')

        # Options Vol/OI Anomalie
        try:
            opt = poly_fetch(f'https://api.polygon.io/v3/snapshot/options/{sym}?limit=100&apiKey={API}')
            res = opt.get('results', [])
            if res:
                calls = [r for r in res if r['details']['contract_type'] == 'call']
                puts  = [r for r in res if r['details']['contract_type'] == 'put']
                max_call_voi = max((r['day'].get('volume',0) / max(r.get('open_interest',1),1)
                                   for r in calls if r['day'].get('volume',0) > 100), default=0)
                max_put_voi  = max((r['day'].get('volume',0) / max(r.get('open_interest',1),1)
                                   for r in puts  if r['day'].get('volume',0) > 100), default=0)
                if max_call_voi >= 5:
                    score += 4
                    reasons.append(f'CALL Vol/OI {max_call_voi:.0f}x — Smart Money')
                elif max_call_voi >= 3:
                    score += 2
                    reasons.append(f'CALL Vol/OI {max_call_voi:.0f}x')
                if max_put_voi >= 5:
                    score += 4
                    reasons.append(f'PUT Vol/OI {max_put_voi:.0f}x — Smart Money SHORT')
                elif max_put_voi >= 3:
                    score += 2
                    reasons.append(f'PUT Vol/OI {max_put_voi:.0f}x')
        except Exception:
            pass

        # Dark Pool letzte 24h
        try:
            dp = get_darkpool_signal(sym, today)
            dp_m = dp.get('dp_total', 0) / 1e6
            if dp_m >= 10:
                score += 5
                reasons.append(f'Dark Pool ${dp_m:.0f}M — große Blocks')
            elif dp_m >= 3:
                score += 3
                reasons.append(f'Dark Pool ${dp_m:.1f}M')
            elif dp_m >= 1:
                score += 1
                reasons.append(f'Dark Pool ${dp_m:.1f}M')
        except Exception:
            pass

        # Polygon News letzte 24h
        try:
            nd = poly_fetch(f'https://api.polygon.io/v2/reference/news?ticker={sym}&limit=3&apiKey={API}')
            cutoff = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
            for n in nd.get('results', []):
                if n.get('published_utc', '') >= cutoff:
                    title = n.get('title', '')
                    tl = title.lower()
                    if any(k in tl for k in POS_KEYS):
                        score += 3
                        reasons.append(f'News: {title[:55]}')
                        break
                    elif any(k in tl for k in ['earnings', 'beat', 'guidance', 'raised']):
                        score += 4
                        reasons.append(f'EARNINGS: {title[:55]}')
                        break
        except Exception:
            pass

        info['score']   = score
        info['reasons'] = reasons[:4]
        return info if score >= 4 else None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_check_24h, sym, info): sym for sym, info in candidates.items()}
        for fut in as_completed(futs, timeout=45):
            try:
                r = fut.result()
                if r:
                    signals.append(r)
            except Exception:
                pass

    signals.sort(key=lambda x: -x['score'])
    return signals[:15]


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

    # Kandidaten: UNIVERSE + Social Deep + Yahoo Movers
    candidates = [t for t in UNIVERSE if t not in in_scan]

    # Social Deep Trending: Reddit + Stocktwits trending Stocks hinzufügen
    _social_deep_map = {}  # sym → {why, sentiment, top_post}
    try:
        social_deep = get_social_deep_trending()
        for s in social_deep:
            t = s['sym']
            _social_deep_map[t] = s
            if t not in in_scan and t not in candidates and 2 <= len(t) <= 5:
                candidates.append(t)
    except Exception:
        # Fallback: altes System
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

    # Alpaca Snapshot für alle Kandidaten (Batch) — max 20 um API-Rate-Limit zu vermeiden
    al_snap = get_alpaca_snapshot(candidates[:20])

    alerts = []

    def check_one(ticker):
        score   = 0
        reasons = []
        dp_info = {}

        # 0) Yahoo Mover Check — bereits heute +/-5%?
        mover = next((m for m in movers_today if m['sym'] == ticker), None)
        if mover:
            chg_abs = abs(mover['chg'])
            if chg_abs >= 10:
                score += 6
                reasons.append(f'{mover["chg"]:+.1f}% heute — EXTREMER MOVE')
            elif chg_abs >= 7:
                score += 5
                reasons.append(f'{mover["chg"]:+.1f}% heute — STARKER MOVE')
            elif chg_abs >= 5:
                score += 3
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
        call_sweeps_n = 0
        put_sweeps_n  = 0
        pc_ratio      = None
        try:
            opt = poly_fetch(f'https://api.polygon.io/v3/snapshot/options/{ticker}?limit=100&apiKey={API}')
            res = opt.get('results', [])
            if res:
                price_from_opt = res[0].get('underlying_asset', {}).get('price', 0)
                calls  = [r for r in res if r['details']['contract_type'] == 'call']
                puts   = [r for r in res if r['details']['contract_type'] == 'put']
                cv     = sum(r['day'].get('volume', 0) for r in calls)
                pv     = sum(r['day'].get('volume', 0) for r in puts)
                oi_tot = sum(max(r.get('open_interest', 0) or 1, 1) for r in calls)
                if cv > 0:
                    pc_ratio = round(pv / cv, 2)
                sw     = get_options_sweep(res)
                call_sweeps_n = sw.get('sweeps_call', 0)
                put_sweeps_n  = sw.get('sweeps_put', 0)
                if oi_tot and cv > oi_tot * 2 and cv > 200:
                    score += 3
                    reasons.append(f'Call Sweep Vol:{cv:,} vs OI:{oi_tot:,}')
                if call_sweeps_n >= 2:
                    score += 2
                    reasons.append(f'{call_sweeps_n} Call-Sweeps erkannt')
                if put_sweeps_n >= 2:
                    score += 1
                    reasons.append(f'{put_sweeps_n} Put-Sweeps erkannt')
                if sw['top_call_dollar'] >= 500_000:
                    score += 1
                    reasons.append(f'Block Call ${sw["top_call_dollar"]/1e6:.1f}M')
        except Exception:
            pass

        # 3) Alpaca Snapshot: Volumen + Preise
        prev_chg      = None
        trend_10d     = None
        prev_close_al = None
        live_price_al = None
        if ticker in al_snap:
            snap = al_snap[ticker]
            dbar = snap.get('dailyBar', {})
            vol  = dbar.get('v', 0) or 0
            prev = snap.get('prevDailyBar', {})
            pvol = prev.get('v', 0) or 1
            if pvol and vol / pvol > 3:
                score += 2
                reasons.append(f'Alpaca Vol {vol/1e6:.1f}M vs Vortag {pvol/1e6:.1f}M ({vol/pvol:.1f}x)')
            # Live-Preis: latestTrade > dailyBar.c > 0
            live_price_al = float(snap.get('latestTrade', {}).get('p', 0) or dbar.get('c', 0) or 0)
            pc = float(prev.get('c') or 0)
            if pc > 0:
                prev_close_al = pc
            if live_price_al and pc:
                prev_chg = round((live_price_al - pc) / pc * 100, 2)
        # 3b) Polygon 30-Tage Bars: Trend + drop_high + prev_chg Fallback
        try:
            from_d = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            to_d   = datetime.now().strftime('%Y-%m-%d')
            bars_d = poly_fetch(f'https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{from_d}/{to_d}?adjusted=true&limit=25&apiKey={API}')
            bars   = bars_d.get('results', [])
            curr_p = float(price_from_opt or live_price_al or 0)
            if len(bars) >= 2:
                trend_10d = round((bars[-1]['c'] - bars[0]['c']) / bars[0]['c'] * 100, 2)
                if prev_chg is None and len(bars) >= 2:
                    prev_c = float(bars[-2]['c'] or 0)
                    use_p  = curr_p or float(bars[-1]['c'] or 0)
                    if prev_c and use_p:
                        prev_chg = round((use_p - prev_c) / prev_c * 100, 2)
                highs  = [float(b.get('h', b['c'])) for b in bars[-10:]]
                p_high = max(highs) if highs else 0
                use_p2 = curr_p or float(bars[-1]['c'] or 0)
                drop_high = round((use_p2 - p_high) / p_high * 100, 2) if p_high and use_p2 else None
            else:
                drop_high = None
        except Exception:
            drop_high = None
        # prev_chg letzter Fallback: Options-Preis vs Alpaca prevDailyBar
        if prev_chg is None and price_from_opt > 0 and prev_close_al:
            prev_chg = round((price_from_opt - prev_close_al) / prev_close_al * 100, 2)

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

        # 6) Social Deep Trending Signal
        social_info = _social_deep_map.get(ticker)
        if social_info:
            sc_score  = social_info.get('score', 0)
            why_tags  = social_info.get('why', [])
            sentiment = social_info.get('sentiment', 'NEUTRAL')
            top_post  = social_info.get('top_post', '')
            sources   = social_info.get('sources', [])

            # Score-Bonus basierend auf Sentiment + Kategorie
            if sentiment == 'BULLISH':
                score += 3
            elif sentiment == 'BEARISH':
                score += 2
            elif sc_score >= 50:
                score += 1

            # Kategorie-Bonus: Earnings/Squeeze sind stärkere Signale
            if 'EARNINGS' in why_tags:
                score += 2
            if 'SHORT-SQUEEZE' in why_tags:
                score += 2
            if 'M&A/DEAL' in why_tags:
                score += 2

            # Reason aufbauen
            why_str  = ' | '.join(why_tags[:2])
            src_str  = ', '.join(sources[:2])
            sent_str = f'{sentiment}' if sentiment != 'NEUTRAL' else ''
            reason_parts = [f'Social [{why_str}]']
            if sent_str:
                reason_parts.append(sent_str)
            reason_parts.append(f'({src_str})')
            reasons.append(' '.join(reason_parts))
            if top_post:
                reasons.append(f'"{top_post[:60]}"')

        if score >= 4 and reasons:
            price     = price_from_opt or (mover['price'] if mover else 0)
            chg_today = mover['chg'] if mover else (prev_chg or 0)
            # Richtung: extremer Fall überschreibt Sweeps
            # >7% runter = Trend ist SHORT, egal ob Dip-Käufer Calls kaufen
            if chg_today <= -7:
                net_dir = 'SHORT'
            elif chg_today >= 7:
                net_dir = 'LONG'
            elif call_sweeps_n > 0 or put_sweeps_n > 0:
                net_dir = 'LONG' if call_sweeps_n >= put_sweeps_n else 'SHORT'
            elif chg_today <= -4:
                net_dir = 'SHORT'
            elif chg_today >= 4:
                net_dir = 'LONG'
            else:
                net_dir = 'LONG'
            out = {
                'ticker':        ticker,
                'score':         score,
                'reasons':       reasons[:5],
                'dp':            dp_info,
                'price':         round(price, 2),
                'ts':            datetime.now().strftime('%H:%M'),
                'call_sweeps':   call_sweeps_n,
                'put_sweeps':    put_sweeps_n,
                'net_direction': net_dir,
                'prev_chg':      prev_chg,
                'trend':         trend_10d,
                'pc':            pc_ratio,
                'drop_high':     drop_high,
            }
            if social_info:
                out['social'] = {
                    'why':       social_info.get('why', []),
                    'sentiment': social_info.get('sentiment', ''),
                    'top_post':  social_info.get('top_post', '')[:80],
                    'sources':   social_info.get('sources', []),
                }
            return out
        return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(check_one, t) for t in candidates[:20]]
        for fut in as_completed(futs, timeout=60):
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

def get_earnings_soon(ticker):
    """Prüft ob Earnings in den nächsten 10 Tagen. Gibt (datum, tage) zurück oder None."""
    try:
        url = f'https://api.polygon.io/vX/reference/financials?ticker={ticker}&limit=1&apiKey={API}'
        d = poly_fetch(url)
        for r in d.get('results', []):
            fd = r.get('fiscal_period_description', '')
            # Polygon hat kein direktes earnings date — nutze SEC filing date als Näherung
            ed = r.get('end_date', '')
            if ed:
                days = (datetime.strptime(ed, '%Y-%m-%d') - datetime.now()).days
                if -5 <= days <= 14:
                    return ed, days
    except Exception:
        pass
    return None, None

def get_smart_money_signals(options, price, today):
    """
    Analysiert Options auf Smart Money Positionierung:
    - Vol/OI Anomalien (> 3x = jemand weiß etwas)
    - Expected Move (ATM Straddle Preis)
    - Sweep Cluster (mehrere große Sweeps gleiche Richtung)
    - OI Konzentration (auf welchen Strike sammelt sich OI?)
    """
    calls = [r for r in options if r['details']['contract_type'] == 'call']
    puts  = [r for r in options if r['details']['contract_type'] == 'put']
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    anomalies = []  # (ticker, type, vol, oi, ratio, strike, exp, premium)
    call_premium = put_premium = 0
    max_call_vol_oi = max_put_vol_oi = 0

    for contracts, ctype in [(calls, 'CALL'), (puts, 'PUT')]:
        for c in contracts:
            exp = c['details']['expiration_date']
            if exp <= today:
                continue
            vol = c['day'].get('volume', 0) or 0
            oi  = c.get('open_interest', 0) or 1
            pr  = c['day'].get('close') or c['day'].get('open') or 0
            ratio = vol / oi
            premium = vol * pr * 100
            strike = c['details']['strike_price']
            if ctype == 'CALL':
                call_premium += premium
                if ratio > max_call_vol_oi:
                    max_call_vol_oi = ratio
            else:
                put_premium += premium
                if ratio > max_put_vol_oi:
                    max_put_vol_oi = ratio
            # Anomalie: Vol/OI > 3x und echtes Volumen
            if ratio >= 3.0 and vol >= 200 and pr >= 0.05:
                anomalies.append({
                    'type': ctype, 'vol': vol, 'oi': oi,
                    'ratio': round(ratio, 1), 'strike': strike,
                    'exp': exp, 'pr': round(pr, 2), 'premium': round(premium),
                })

    # Expected Move: ATM Straddle (nächste Expiry)
    expected_move_pct = 0
    try:
        next_expiries = sorted({c['details']['expiration_date']
                                for c in options if c['details']['expiration_date'] > today})[:2]
        for exp in next_expiries:
            exp_calls = [c for c in calls if c['details']['expiration_date'] == exp]
            exp_puts  = [c for c in puts  if c['details']['expiration_date'] == exp]
            # ATM = Strike am nächsten zum aktuellen Preis
            atm_call = min(exp_calls, key=lambda x: abs(x['details']['strike_price'] - price), default=None)
            atm_put  = min(exp_puts,  key=lambda x: abs(x['details']['strike_price'] - price), default=None)
            if atm_call and atm_put:
                c_pr = atm_call['day'].get('close') or 0
                p_pr = atm_put['day'].get('close') or 0
                straddle = c_pr + p_pr
                if straddle > 0 and price > 0:
                    expected_move_pct = round(straddle / price * 100, 1)
                    break
    except Exception:
        pass

    # Größte Anomalien nach Ratio sortieren
    anomalies.sort(key=lambda x: -x['ratio'])

    return {
        'anomalies':       anomalies[:5],
        'max_call_vol_oi': round(max_call_vol_oi, 1),
        'max_put_vol_oi':  round(max_put_vol_oi, 1),
        'call_premium':    round(call_premium),
        'put_premium':     round(put_premium),
        'expected_move':   expected_move_pct,
        'bull_flow':       call_premium > put_premium * 1.5,
        'bear_flow':       put_premium  > call_premium * 1.5,
    }

def best_option(contracts, is_call, price, today, exp_cutoff, atr=5.0):
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    candidates = []
    for c in contracts:
        exp = c['details']['expiration_date']
        if exp > exp_cutoff or exp <= today:
            continue
        strike = c['details']['strike_price']
        pct = ((strike - price) / price * 100) if is_call else ((price - strike) / price * 100)
        pr  = c['day'].get('close') or c['day'].get('open') or 0
        vol = c['day'].get('volume', 0)
        oi  = c.get('open_interest', 0)
        if pr < 0.05 or vol < 50:
            continue
        # Erreichbarkeit: OTM darf nicht größer als ~ATR × Tage × Faktor sein
        try:
            days_left = max(1, (datetime.strptime(exp, '%Y-%m-%d') - datetime.now()).days)
        except Exception:
            days_left = 7
        max_otm = min(12.0, (atr / price * 100) * (days_left ** 0.5) * 1.8)
        if not (0.5 <= pct <= max_otm):
            continue
        # Vol/OI Ratio — echtes Interesse erforderlich
        vol_oi = vol / oi if oi > 0 else 0
        if oi > 500 and vol_oi < 0.15:   # Altbestand ohne frisches Interesse
            continue
        candidates.append({
            'strike': strike, 'pct': round(pct, 1), 'pr': pr,
            'vol': vol, 'oi': oi, 'exp': exp,
            'total': vol * pr * 100,
            'vol_oi': round(vol_oi, 2),
            'days': days_left,
        })
    # Sortierung: meistes Volumen zuerst (nicht billigster Preis)
    candidates.sort(key=lambda x: -x['vol'])
    return candidates[0] if candidates else None

def hermes_afterhours_scan(extra_tickers: list = None) -> dict:
    """
    After-Hours Intelligence Scan — laeuft wenn Markt geschlossen ist.
    Analysiert den KOMPLETTEN Tages-Flow (Options + Dark Pool) und
    gibt immer LONG und SHORT Kandidaten zurueck.

    Logik:
    - Schaut den vollen Tages-Flow: welche Stocks hatten die groesste
      institutionelle Aktivitaet HEUTE?
    - LONG: hohe Call-Premium + Dark Pool BUY + bullische News
    - SHORT: hohe Put-Premium + Dark Pool SELL + bearische News / Ueberdehnung
    - Niedrigere Score-Schwelle (3 statt 4) — voller Datensatz nach Close
    """
    today      = datetime.now().strftime('%Y-%m-%d')
    exp_cutoff = (datetime.now() + timedelta(days=45)).strftime('%Y-%m-%d')
    news_cut   = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Universe: Hauptkandidaten + Social Trending + Extra
    universe_ah = list(UNIVERSE[:40])
    if extra_tickers:
        for t in extra_tickers:
            if t not in universe_ah and 2 <= len(t) <= 5:
                universe_ah.append(t)

    # Social Trending Stocks auch scannen
    try:
        social_deep = get_social_deep_trending()
        for s in social_deep[:10]:
            t = s['sym']
            if t not in universe_ah and 2 <= len(t) <= 5:
                universe_ah.append(t)
    except Exception:
        pass

    longs  = []
    shorts = []

    def _scan_one(ticker):
        score_l = score_s = 0
        rl = []  # long reasons
        rs = []  # short reasons
        price = 0
        best_call = best_put = None

        # 1) Options Flow (kompletter Tages-Flow)
        try:
            opt = poly_fetch(f'https://api.polygon.io/v3/snapshot/options/{ticker}?limit=200&apiKey={API}')
            res = opt.get('results', [])
            if not res:
                return None
            price = res[0].get('underlying_asset', {}).get('price', 0)
            if price < 3:
                return None

            calls = [r for r in res if r['details']['contract_type'] == 'call']
            puts  = [r for r in res if r['details']['contract_type'] == 'put']
            cv = sum(r['day'].get('volume', 0) for r in calls)
            pv = sum(r['day'].get('volume', 0) for r in puts)
            cp = sum(r['day'].get('volume',0)*(r['day'].get('close') or 0)*100 for r in calls)
            pp = sum(r['day'].get('volume',0)*(r['day'].get('close') or 0)*100 for r in puts)
            pc = pv / max(cv, 1)

            mc = max((r['day'].get('volume',0)/max(r.get('open_interest',1),1)
                      for r in calls if r['day'].get('volume',0) > 50), default=0)
            mp = max((r['day'].get('volume',0)/max(r.get('open_interest',1),1)
                      for r in puts  if r['day'].get('volume',0) > 50), default=0)

            sw = get_options_sweep(res)
            sc = sw.get('sweeps_call', 0)
            sp = sw.get('sweeps_put', 0)

            # Call-Flow (LONG Signal)
            if cp >= 5_000_000:
                score_l += 4; rl.append(f'Call-Premium ${cp/1e6:.1f}M')
            elif cp >= 2_000_000:
                score_l += 3; rl.append(f'Call-Premium ${cp/1e6:.1f}M')
            elif cp >= 1_000_000:
                score_l += 2; rl.append(f'Call-Premium ${cp/1e6:.1f}M')

            if mc >= 10:
                score_l += 3; rl.append(f'Call VOI {mc:.0f}x (ungewöhnlich)')
            elif mc >= 5:
                score_l += 2; rl.append(f'Call VOI {mc:.0f}x')

            if sc >= 5:
                score_l += 3; rl.append(f'{sc} Call-Sweeps (koordiniert)')
            elif sc >= 2:
                score_l += 2; rl.append(f'{sc} Call-Sweeps')

            if pc < 0.4:
                score_l += 2; rl.append(f'P/C {pc:.2f} — stark bullisch')
            elif pc < 0.6:
                score_l += 1

            # Put-Flow (SHORT Signal)
            if pp >= 5_000_000:
                score_s += 4; rs.append(f'Put-Premium ${pp/1e6:.1f}M')
            elif pp >= 2_000_000:
                score_s += 3; rs.append(f'Put-Premium ${pp/1e6:.1f}M')
            elif pp >= 1_000_000:
                score_s += 2; rs.append(f'Put-Premium ${pp/1e6:.1f}M')

            if mp >= 10:
                score_s += 3; rs.append(f'Put VOI {mp:.0f}x (ungewöhnlich)')
            elif mp >= 5:
                score_s += 2; rs.append(f'Put VOI {mp:.0f}x')

            if sp >= 5:
                score_s += 3; rs.append(f'{sp} Put-Sweeps')
            elif sp >= 2:
                score_s += 2; rs.append(f'{sp} Put-Sweeps')

            if pc > 1.5:
                score_s += 2; rs.append(f'P/C {pc:.2f} — stark bearisch')
            elif pc > 1.0:
                score_s += 1

            # Beste Option für morgen finden (AH: niedrigere Anforderungen)
            best_call = best_option(calls, True,  price, today, exp_cutoff, price * 0.02)
            best_put  = best_option(puts,  False, price, today, exp_cutoff, price * 0.02)

        except Exception:
            return None

        # 2) Dark Pool Richtung
        try:
            dp = get_darkpool_signal(ticker, today)
            dp_dir = dp.get('direction', 'NEUTRAL')
            dp_m   = dp.get('dp_total', 0) / 1e6
            if dp_dir == 'BUY' and dp_m >= 0.5:
                score_l += 3 if dp_m >= 5 else (2 if dp_m >= 1 else 1)
                rl.append(f'Dark Pool KAUF ${dp_m:.1f}M')
            elif dp_dir == 'SELL' and dp_m >= 0.5:
                score_s += 3 if dp_m >= 5 else (2 if dp_m >= 1 else 1)
                rs.append(f'Dark Pool VERKAUF ${dp_m:.1f}M')
        except Exception:
            pass

        # 3) News (letzte 24h)
        try:
            nd = poly_fetch(f'https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=5&apiKey={API}')
            for n in nd.get('results', []):
                if n.get('published_utc', '') < news_cut:
                    continue
                tl   = n.get('title', '').lower()
                sent = next((i.get('sentiment','') for i in n.get('insights',[])
                             if i.get('ticker') == ticker), '')
                if any(k in tl for k in POS_KEYS) and sent != 'negative':
                    score_l += 2; rl.append(f'News: {n.get("title","")[:50]}'); break
                if any(k in tl for k in NEG_KEYS) or sent == 'negative':
                    score_s += 2; rs.append(f'Neg.News: {n.get("title","")[:45]}'); break
        except Exception:
            pass

        # 4) Ergebnis bauen (Schwelle: 3 statt 4)
        result = None
        if score_l >= 3 and score_l > score_s and best_call:
            result = {
                't': ticker, 'signal': 'LONG', 'score': score_l,
                'price': round(price, 2),
                'reasons': rl[:4],
                'best': best_call,
                'otype': 'CALL',
                'label': 'AH-Setup',   # After-Hours Label
            }
        elif score_s >= 3 and score_s > score_l and best_put:
            result = {
                't': ticker, 'signal': 'SHORT', 'score': score_s,
                'price': round(price, 2),
                'reasons': rs[:4],
                'best': best_put,
                'otype': 'PUT',
                'label': 'AH-Setup',
            }
        elif score_l >= 3 and not best_call:
            # Kein best_option aber Signal stark — trotzdem zeigen (ohne Options-Details)
            result = {
                't': ticker, 'signal': 'LONG', 'score': score_l,
                'price': round(price, 2),
                'reasons': rl[:4],
                'best': None,
                'otype': 'CALL',
                'label': 'AH-Flow',
            }
        elif score_s >= 3 and not best_put:
            result = {
                't': ticker, 'signal': 'SHORT', 'score': score_s,
                'price': round(price, 2),
                'reasons': rs[:4],
                'best': None,
                'otype': 'PUT',
                'label': 'AH-Flow',
            }
        return result

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(_scan_one, t) for t in universe_ah[:35]]
        for f in as_completed(futs):
            try:
                r = f.result()
                if not r:
                    continue
                if r['signal'] == 'LONG':
                    longs.append(r)
                else:
                    shorts.append(r)
            except Exception:
                pass

    # Nach Score sortieren
    longs.sort(key=lambda x: -x['score'])
    shorts.sort(key=lambda x: -x['score'])

    return {
        'longs':   longs[:8],
        'shorts':  shorts[:8],
        'time':    datetime.now().strftime('%Y-%m-%d %H:%M'),
        'label':   'After-Hours Intelligence',
        'movers':  [],
        'watch':   [],
        'scanned': len(universe_ah[:35]),
        'today':   today,
    }


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

        # Smart Money Positionierung — Vol/OI Anomalien + Expected Move
        sm = get_smart_money_signals(res, price, today)

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
        prev_close  = closes[-1]  # letzter daily bar = gestern's Close
        prev_chg    = ((price - prev_close) / prev_close * 100) if prev_close else 0  # live vs gestern
        period_high = max(highs[-10:]) if len(highs) >= 10 else max(highs)
        drop_from_high = ((closes[-1] - period_high) / period_high) * 100

        # ── EARNINGS ANALYSE — Wahrscheinlichkeit steigen/fallen ─────────────
        earnings_date, earnings_days = get_earnings_soon(ticker)
        earnings_analysis = None
        c5 = closes[-5:] if len(closes) >= 5 else closes
        run5 = ((c5[-1]-c5[0])/c5[0]*100) if len(c5)>=2 else 0  # 5-Tage Run-Up
        avg_vol5 = sum(b['v'] for b in bars[-5:])/5 if len(bars)>=5 else 0
        last_vol  = bars[-1]['v'] if bars else 0
        vol_spike = last_vol/avg_vol5 if avg_vol5 > 0 else 1

        # Earnings in 0-3 Tagen → Wahrscheinlichkeitsanalyse
        if earnings_days is not None and -1 <= earnings_days <= 3:
            # Sell-the-News Wahrscheinlichkeit
            sell_prob  = 50.0  # Basis
            beat_prob  = 50.0

            # +1) Pre-Earnings Run-Up > 8% = klassisches "Sell the News"
            if run5 >= 15:
                sell_prob += 25
                beat_prob -= 25
                runup_str = f'+{run5:.1f}% in 5T = Sell-the-News sehr wahrscheinlich'
            elif run5 >= 8:
                sell_prob += 15
                beat_prob -= 15
                runup_str = f'+{run5:.1f}% in 5T = Sell-the-News wahrscheinlich'
            else:
                runup_str = f'+{run5:.1f}% in 5T = normales Niveau'

            # +2) Volumen-Spike auf dem Weg hoch = Distribution (Smart Money exit)
            if vol_spike >= 2.5 and run5 > 5:
                sell_prob += 10
                beat_prob -= 10

            # +3) PUT Vol/OI bei nahen Strikes = Absicherung gegen Drop
            near_puts = [r for r in res if
                         r.get('details',{}).get('contract_type') == 'put' and
                         abs(r['details'].get('strike_price',0) - price) / price <= 0.12 and
                         (r.get('day',{}).get('volume',0) or 0) > 50]
            put_near_voi = max(
                ((r.get('day',{}).get('volume',0) or 0) / max(r.get('open_interest',1),1))
                for r in near_puts) if near_puts else 0
            if put_near_voi >= 5:
                sell_prob += 15
                beat_prob -= 15
            elif put_near_voi >= 3:
                sell_prob += 8
                beat_prob -= 8

            # +4) Call Premium stark > Put Premium = Markt erwartet Beat
            cp_ratio = sm.get('call_premium',0) / max(sm.get('put_premium',1),1)
            if cp_ratio >= 4:
                beat_prob += 10
                sell_prob -= 10
            elif cp_ratio <= 1.5:
                sell_prob += 8
                beat_prob -= 8

            # Clampen 5-95%
            sell_prob = min(95, max(5, sell_prob))
            beat_prob = min(95, max(5, beat_prob))

            earnings_analysis = {
                'date':        earnings_date,
                'days':        earnings_days,
                'run5':        round(run5, 1),
                'vol_spike':   round(vol_spike, 1),
                'put_near_voi':round(put_near_voi, 1),
                'sell_prob':   round(sell_prob, 0),
                'beat_prob':   round(beat_prob, 0),
                'runup_str':   runup_str,
                'verdict':     ('SELL-THE-NEWS' if sell_prob >= 65 else
                                'BEAT-ERWARTUNG' if beat_prob >= 65 else 'UNENTSCHIEDEN'),
            }

        # Dark Pool parallel (Thread) — jetzt mit Richtungserkennung
        dp_result = [{}]
        def _fetch_dp():
            dp_result[0] = get_darkpool_signal(ticker, today)
        dp_thread = threading.Thread(target=_fetch_dp, daemon=True)
        dp_thread.start()

        # Polygon News — mit HIGH/EXTREME Impact Erkennung
        news_data = poly_fetch(f'https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=10&apiKey={API}')
        katalysator = 'KEIN'
        katalysator_strength = 'NORMAL'  # NORMAL / HIGH / EXTREME
        kat_text = kat_url = ''
        al_news_text = ''
        for n in news_data.get('results', []):
            if n.get('published_utc', '')[:10] < news_cutoff:
                continue
            title_l = n.get('title', '').lower()
            sent = next((i.get('sentiment', '') for i in n.get('insights', []) if i.get('ticker') == ticker), '')
            has_hard_neg  = any(k in title_l for k in HARD_NEG_KEYS)
            has_high_neg  = any(k in title_l for k in HIGH_IMPACT_NEG)
            has_gov       = any(k in title_l for k in HIGH_IMPACT_GOV)
            has_endorse   = any(k in title_l for k in HIGH_IMPACT_ENDORSE)
            has_insider   = any(k in title_l for k in HIGH_IMPACT_INSIDER)
            has_pos       = any(k in title_l for k in POS_KEYS)
            has_neg       = any(k in title_l for k in NEG_KEYS)

            # HARD NEG überschreibt alles (Dilution, Secondary Offering)
            if has_hard_neg:
                katalysator = 'NEGATIV'
                katalysator_strength = 'HIGH'
                kat_text = n.get('title', '')[:70]
                kat_url  = n.get('article_url', '')
                break

            # EXTREME: Insider-Kauf + Gov-Vertrag gleichzeitig
            if has_insider and has_gov and katalysator != 'NEGATIV':
                katalysator = 'POSITIV'
                katalysator_strength = 'EXTREME'
                kat_text = '🔥 EXTREME: ' + n.get('title', '')[:57]
                kat_url  = n.get('article_url', '')
                continue  # weiter suchen ob HARD_NEG folgt

            # HIGH: Gov-Vertrag oder CEO-Endorsement oder Insider-Kauf
            if (has_gov or has_endorse or has_insider) and katalysator != 'NEGATIV':
                katalysator = 'POSITIV'
                if katalysator_strength != 'EXTREME':
                    katalysator_strength = 'HIGH'
                if has_gov:
                    kat_text = '🏛️ GOV: ' + n.get('title', '')[:62]
                elif has_endorse:
                    kat_text = '⭐ ENDORSE: ' + n.get('title', '')[:58]
                else:
                    kat_text = '💰 INSIDER: ' + n.get('title', '')[:58]
                kat_url = n.get('article_url', '')
                continue

            # HIGH NEG: SEC/DOJ/Criminal
            if has_high_neg:
                katalysator = 'NEGATIV'
                katalysator_strength = 'HIGH'
                kat_text = '⚠️ ' + n.get('title', '')[:67]
                kat_url  = n.get('article_url', '')
                break

            # NORMAL positiv
            if has_pos and sent in ('positive', 'neutral', '') and not has_neg:
                if katalysator == 'KEIN':
                    katalysator = 'POSITIV'
                    kat_text = n.get('title', '')[:70]
                    kat_url  = n.get('article_url', '')
            elif has_neg or sent == 'negative':
                if katalysator not in ('POSITIV',):
                    katalysator = 'NEGATIV'
                    kat_text = n.get('title', '')[:70]
                    kat_url  = n.get('article_url', '')

        # Alpaca News als zweite Quelle (nur wenn Polygon nichts hat)
        if katalysator == 'KEIN':
            for an in get_alpaca_news([ticker], limit=5):
                h = an.get('headline', '')
                hl = h.lower()
                has_gov_al     = any(k in hl for k in HIGH_IMPACT_GOV)
                has_endorse_al = any(k in hl for k in HIGH_IMPACT_ENDORSE)
                has_insider_al = any(k in hl for k in HIGH_IMPACT_INSIDER)
                has_hard_neg_al= any(k in hl for k in HARD_NEG_KEYS)
                if has_hard_neg_al:
                    katalysator = 'NEGATIV'
                    katalysator_strength = 'HIGH'
                    kat_text = h[:70]; kat_url = an.get('url', '')
                    al_news_text = '[Alpaca] '
                    break
                elif has_insider_al and has_gov_al:
                    katalysator = 'POSITIV'; katalysator_strength = 'EXTREME'
                    kat_text = '🔥 EXTREME: ' + h[:57]; kat_url = an.get('url', '')
                    al_news_text = '[Alpaca] '
                    break
                elif has_gov_al or has_endorse_al or has_insider_al:
                    katalysator = 'POSITIV'; katalysator_strength = 'HIGH'
                    prefix = '🏛️ GOV: ' if has_gov_al else ('⭐ ENDORSE: ' if has_endorse_al else '💰 INSIDER: ')
                    kat_text = prefix + h[:62]; kat_url = an.get('url', '')
                    al_news_text = '[Alpaca] '
                    break
                elif any(k in hl for k in POS_KEYS):
                    katalysator = 'POSITIV'
                    kat_text = h[:70]; kat_url = an.get('url', '')
                    al_news_text = '[Alpaca] '
                    break
                elif any(k in hl for k in NEG_KEYS):
                    katalysator = 'NEGATIV'
                    kat_text = h[:70]; kat_url = an.get('url', '')
                    al_news_text = '[Alpaca] '

        # Social momentum
        _, social_scores = get_cached_social()
        social_score = social_scores.get(ticker, 0)
        social_boost = min(2, social_score // 50)

        # Dark Pool warten
        dp_thread.join(timeout=8)
        dp = dp_result[0]
        dp_dollar = dp.get('dp_total', 0)
        dp_dir    = dp.get('direction', 'NEUTRAL')  # NEU: BUY / SELL / NEUTRAL
        dp_score  = (3 if dp_dollar >= 5_000_000 else
                     2 if dp_dollar >= 1_000_000 else
                     1 if dp_dollar >= 500_000   else 0)

        # Flow-Divergenz erkennen (Preis vs Options-Flow vs Dark Pool)
        _pc_div   = pv / max(cv, 1)
        divergence = detect_flow_divergence(prev_chg, cv, pv, sc, sp, dp_dir)

        long_score = short_score = 0
        reasons_long = []
        reasons_short = []

        # Gelernte Gewichtungen + Regeln laden (Hermes Self-Learning 14-Tage)
        try:
            import json as _lj, os as _lo
            _lf = 'hermes_learning.json'
            _ld = _lj.load(open(_lf)) if _lo.path.exists(_lf) else {}
            _lw = _ld.get('weights', {})
        except Exception:
            _ld, _lw = {}, {}
        _vol_thresh    = float(_lw.get('vol_ratio_threshold', 3.0))
        _earnings_bon  = int(_lw.get('earnings_bonus', 4))
        _smallcap_bon  = int(_lw.get('small_cap_boost', 0))

        # Marktbias der letzten 3 Tage laden — konservativere Schwellen bei schlechtem Trend
        try:
            _bias_log = _ld.get('market_bias_log', {})
            _recent_days = sorted(_bias_log.keys())[-3:]
            _recent_wr   = [_bias_log[d].get('win_rate', 50) for d in _recent_days if _bias_log[d].get('win_rate')]
            _avg_wr_3d   = sum(_recent_wr) / len(_recent_wr) if _recent_wr else 50
            # Wenn Win-Rate < 40% über 3 Tage → Score-Schwellen erhöhen
            _score_adj = 2 if _avg_wr_3d < 40 else (1 if _avg_wr_3d < 50 else 0)
        except Exception:
            _score_adj = 0

        # Gelernte Regeln aus identity.json laden und als Filter anwenden
        try:
            _id_f = 'hermes_identity.json'
            _id_d = _lj.load(open(_id_f)) if _lo.path.exists(_id_f) else {}
            _ai_rules = _id_d.get('rules', [])
        except Exception:
            _ai_rules = []

        # Regel-Parser: KI-Regeln aus identity.json in Flags umwandeln
        _block_short_on_call_sweep = any('call-sweep' in r.lower() or 'call sweep' in r.lower() for r in _ai_rules)
        _block_put_hedge_largecap  = any('put' in r.lower() and 'large' in r.lower() for r in _ai_rules)
        _require_news_confirm      = any('news' in r.lower() and 'bestätig' in r.lower() for r in _ai_rules)

        # Aktive Strategien aus identity.json laden und auf diesen Ticker anwenden
        _strategies = _id_d.get('strategies', [])
        _strat_long_bonus  = 0
        _strat_short_bonus = 0
        _strat_reasons_l   = []
        _strat_reasons_s   = []
        for strat in _strategies:
            if strat.get('hit_rate', 0) < 0.55:
                continue   # Nur Strategien mit >55% Trefferquote anwenden
            rule = strat.get('rule', '').lower()
            sid  = strat.get('id', '')
            applies = strat.get('applies_to', 'alle')
            # Einfacher Regel-Matcher: prüft ob Bedingungen erfüllt
            match = False
            bonus = int(round(strat.get('confidence', 0.6) * 3))   # max +3 Punkte
            # LONG-Strategien
            if 'long' in rule:
                conds = []
                if 'p/c' in rule and 'call_voi' in rule:
                    conds.append(pc < 0.6 and max_call_voi >= 5)
                if 'sweeps' in rule or 'sweep' in rule:
                    conds.append(sc >= 3)
                if 'volratio' in rule or 'vol_ratio' in rule:
                    conds.append(vol_spike >= float(rule.split('volratio')[1].split()[0].replace('>','').replace('x','').strip()) if 'volratio' in rule else False)
                if 'darkpool' in rule.replace(' ', '') or 'dark pool' in rule:
                    conds.append(dp_dir == 'BUY')
                if conds and all(conds):
                    _strat_long_bonus += bonus
                    _strat_reasons_l.append(f'Strategie [{sid}]: {strat.get("rule","")[:50]}')
                    strat['samples'] = strat.get('samples', 0) + 1
            # SHORT-Strategien
            elif 'short' in rule:
                conds = []
                if 'p/c' in rule:
                    try:
                        thr = float([w for w in rule.split() if w.replace('.','').isdigit()][0])
                        conds.append(pc > thr)
                    except Exception:
                        conds.append(pc > 1.2)
                if 'sweeps' in rule or 'sweep' in rule:
                    conds.append(sp >= 3)
                if 'darkpool' in rule.replace(' ', '') or 'dark pool' in rule:
                    conds.append(dp_dir == 'SELL')
                if conds and all(conds):
                    _strat_short_bonus += bonus
                    _strat_reasons_s.append(f'Strategie [{sid}]: {strat.get("rule","")[:50]}')
                    strat['samples'] = strat.get('samples', 0) + 1

        # SmallCap Boost (gelernt: kleine Aktien nicht ignorieren)
        if price < 50 and _smallcap_bon > 0:
            long_score  += _smallcap_bon
            short_score += _smallcap_bon

        # Earnings-Erkennung mit Wahrscheinlichkeitsanalyse
        has_earnings = any(k in (kat_text + al_news_text).lower()
                          for k in ['earnings','beat','guidance','raised','revenue','results'])
        if has_earnings:
            long_score  += _earnings_bon
            short_score += _earnings_bon
            reasons_long.append(f'EARNINGS/KATALYSATOR (+{_earnings_bon} gelernt)')

        # Earnings in 0-3 Tagen → Wahrscheinlichkeit in Score einbauen
        if earnings_analysis:
            ea = earnings_analysis
            if ea['verdict'] == 'SELL-THE-NEWS':
                short_score += 5
                reasons_short.append(
                    f'EARNINGS {ea["date"]} | {ea["run5"]:+.0f}% Run-Up | '
                    f'DROP {ea["sell_prob"]:.0f}% wahrscheinlich')
                # Long Score senken wenn Sell-the-News wahrscheinlich
                long_score = max(0, long_score - 3)
            elif ea['verdict'] == 'BEAT-ERWARTUNG':
                long_score += 4
                reasons_long.append(
                    f'EARNINGS {ea["date"]} | Beat {ea["beat_prob"]:.0f}% wahrscheinlich | '
                    f'Call-Premium dominiert')

        # ── TIER 1: Smart Money Signale (höchste Priorität) ──────────────────

        # Vol/OI Anomalie mit gelernter Schwelle
        max_call_voi = sm['max_call_vol_oi']
        max_put_voi  = sm['max_put_vol_oi']
        if max_call_voi >= 10:
            long_score += 6
            reasons_long.append(f'Vol/OI CALL {max_call_voi:.0f}x — massive Positionierung')
        elif max_call_voi >= 5:
            long_score += 4
            reasons_long.append(f'Vol/OI CALL {max_call_voi:.0f}x — ungewöhnlich')
        elif max_call_voi >= 3:
            long_score += 2
            reasons_long.append(f'Vol/OI CALL {max_call_voi:.0f}x')
        if max_put_voi >= 10:
            short_score += 6
            reasons_short.append(f'Vol/OI PUT {max_put_voi:.0f}x — massive Positionierung')
        elif max_put_voi >= 5:
            short_score += 4
            reasons_short.append(f'Vol/OI PUT {max_put_voi:.0f}x — ungewöhnlich')
        elif max_put_voi >= 3:
            short_score += 2
            reasons_short.append(f'Vol/OI PUT {max_put_voi:.0f}x')

        # Dark Pool — jetzt mit RICHTUNG
        if dp_dollar >= 1_000_000:
            dp_m = dp_dollar / 1e6
            if dp_dir == 'BUY':
                pts = 5 if dp_dollar >= 10_000_000 else (4 if dp_dollar >= 5_000_000 else 2)
                long_score  += pts
                reasons_long.append(f'Dark Pool ${dp_m:.1f}M KAUF ({dp.get("buy_pct",50)}% über VWAP)')
            elif dp_dir == 'SELL':
                pts = 5 if dp_dollar >= 10_000_000 else (4 if dp_dollar >= 5_000_000 else 2)
                short_score += pts
                reasons_short.append(f'Dark Pool ${dp_m:.1f}M VERKAUF ({dp.get("sell_pct",50)}% unter VWAP)')
            else:
                # Neutral: nur Volumen zählt (kein Richtungs-Bonus)
                pts = 3 if dp_dollar >= 10_000_000 else (2 if dp_dollar >= 5_000_000 else 1)
                long_score  += pts
                reasons_long.append(f'Dark Pool ${dp_m:.1f}M neutral')

        # Flow-Divergenz Score (Leading Indicator!)
        div_type = divergence.get('type', 'NONE')
        div_str  = divergence.get('strength', 0)
        if div_type == 'BEAR_DIV':
            short_score += min(int(div_str) + 2, 5)
            reasons_short.append(f'DIVERGENZ: {divergence["msg"][:60]}')
            # Wenn Divergenz: Long-Score reduzieren
            long_score = max(0, long_score - 2)
        elif div_type == 'BULL_DIV':
            long_score  += min(int(div_str) + 2, 5)
            reasons_long.append(f'DIVERGENZ: {divergence["msg"][:60]}')
            short_score = max(0, short_score - 2)
        elif div_type in ('BEAR_DIV_SOFT', 'BULL_DIV_SOFT'):
            if 'BEAR' in div_type:
                short_score += 1
                reasons_short.append(divergence['msg'][:50])
            else:
                long_score  += 1
                reasons_long.append(divergence['msg'][:50])

        # Options Sweep Cluster (koordiniertes Smart Money)
        sc = sweep.get('sweeps_call', 0)
        sp = sweep.get('sweeps_put', 0)
        if sc >= 5:
            long_score += 4
            reasons_long.append(f'{sc} Call-Sweeps — koordiniert')
        elif sc >= 2:
            long_score += 2
            reasons_long.append(f'{sc} Call-Sweeps')
        if sp >= 5:
            short_score += 4
            reasons_short.append(f'{sp} Put-Sweeps — koordiniert')
        elif sp >= 2:
            short_score += 2
            reasons_short.append(f'{sp} Put-Sweeps')

        # Premium Flow (wohin fließt das Geld)
        if sm['bull_flow']:
            long_score += 2
            reasons_long.append(f'Call Premium ${sm["call_premium"]/1e6:.1f}M dominiert')
        if sm['bear_flow']:
            short_score += 2
            reasons_short.append(f'Put Premium ${sm["put_premium"]/1e6:.1f}M dominiert')

        # Eigene Strategien anwenden (aus Strategy Builder)
        if _strat_long_bonus > 0:
            long_score  += _strat_long_bonus
            reasons_long.extend(_strat_reasons_l)
        if _strat_short_bonus > 0:
            short_score += _strat_short_bonus
            reasons_short.extend(_strat_reasons_s)

        # ── TIER 2: Katalysatoren (binäre Events) ───────────────────────────

        # News-Katalysator (Stärke: NORMAL=+3, HIGH=+5, EXTREME=+7)
        if katalysator == 'POSITIV':
            kat_pts = 7 if katalysator_strength == 'EXTREME' else (5 if katalysator_strength == 'HIGH' else 3)
            long_score += kat_pts
            label = '🔥 EXTREME' if katalysator_strength == 'EXTREME' else ('🏛️ HIGH-IMPACT' if katalysator_strength == 'HIGH' else 'News')
            reasons_long.append(f'{label}: {kat_text[:50]}')
        if katalysator == 'NEGATIV':
            kat_pts = 7 if katalysator_strength == 'EXTREME' else (5 if katalysator_strength == 'HIGH' else 3)
            short_score += kat_pts
            label = '⚠️ HIGH-IMPACT' if katalysator_strength in ('HIGH','EXTREME') else 'Neg. News'
            reasons_short.append(f'{label}: {kat_text[:50]}')

        # Expected Move (hoch = Markt erwartet großen Swing)
        em = sm['expected_move']
        if em >= 10:
            long_score  += 2
            short_score += 2  # Binary Event — beide Richtungen möglich
        elif em >= 5:
            long_score  += 1
            short_score += 1

        # ── TIER 3: Technische Bestätigung ──────────────────────────────────

        # Trend (jetzt nur Bestätigung, nicht mehr Hauptsignal)
        if trend_pct < -7:   short_score += 2
        elif trend_pct < -3: short_score += 1
        if trend_pct > 7:    long_score  += 2
        elif trend_pct > 3:  long_score  += 1

        # P/C Ratio
        if pc < 0.3:   long_score  += 2
        elif pc < 0.5: long_score  += 1
        if pc > 0.8:   short_score += 2
        elif pc > 0.6: short_score += 1

        # Abstand vom Hoch
        if drop_from_high < -10: short_score += 2
        elif drop_from_high < -5: short_score += 1

        # Social
        if social_boost > 0 and trend_pct > 0:
            long_score += social_boost

        bc = best_option(calls, True,  price, today, exp_cutoff, atr)
        bp = best_option(puts,  False, price, today, exp_cutoff, atr)

        # Dynamische Schwellen: bei schlechter Win-Rate der letzten 3 Tage strenger werden
        _min_long  = int(_lw.get('min_score_long',  4)) + _score_adj
        _min_short = int(_lw.get('min_score_short', 4)) + _score_adj

        tie_goes_short = drop_from_high < -8 and short_score >= _min_short
        if short_score >= _min_short and (short_score > long_score or tie_goes_short) and bp:
            signal, score, best, otype = 'SHORT', short_score, bp, 'PUT'
        elif long_score >= _min_long and long_score > short_score and bc:
            signal, score, best, otype = 'LONG',  long_score,  bc, 'CALL'
        else:
            signal, score, best, otype = 'WATCH', 0, bc or bp, None

        # Catastrophic event filter: Tagesbewegung bereits extrem → kein Signal mehr
        # Verhindert LONG auf Stocks die heute -15%+ gecrasht sind (FDA, Earnings fail etc.)
        if signal == 'LONG' and prev_chg <= -15:
            signal, score, best, otype = 'WATCH', 0, bc or bp, None
        # Verhindert SHORT auf Stocks die heute schon +10%+ gestiegen sind (falsche Richtung)
        elif signal == 'SHORT' and prev_chg >= 10:
            signal, score, best, otype = 'WATCH', 0, bc or bp, None

        # Call-Sweep Widerspruch: viele Call-Sweeps = bullisches Signal
        # SHORT bei >= 5 Call-Sweeps nur erlaubt wenn NEGATIV-Katalysator oder Crash
        # Threshold: KI kann den Wert durch Lernen auf 3 senken
        _sweep_block_thresh = 3 if _block_short_on_call_sweep else 5
        if signal == 'SHORT' and sc >= _sweep_block_thresh and katalysator != 'NEGATIV' and prev_chg > -5:
            signal, score, best, otype = 'WATCH', 0, bc or bp, None

        # Large-Cap PUT Hedge Filter: bei teuren Aktien (>$80) ist hohes PUT-Volumen
        # oft institutionelle Absicherung bestehender Longs, kein Direktional-Short
        # KI kann diesen Filter durch Lernen verschärfen
        _put_hedge_price = 60 if _block_put_hedge_largecap else 80
        if signal == 'SHORT' and max_put_voi >= 20 and price > _put_hedge_price and prev_chg > -3:
            short_score = max(0, short_score - 4)
            if short_score < _min_short or short_score <= long_score:
                signal, score, best, otype = 'WATCH', 0, bc or bp, None

        conflict = signal == 'SHORT' and trend_pct > 15 and short_trend > -5

        # ── CONVICTION SYSTEM — Polygon first, News bestätigt ────────────────
        # Polygon-Signal: was sagen Options + Dark Pool?
        poly_long  = max_call_voi >= 5 or (dp_dollar >= 3_000_000 and prev_chg > 0)
        poly_short = max_put_voi  >= 5 or (dp_dollar >= 3_000_000 and prev_chg < 0)
        news_long  = katalysator == 'POSITIV'
        news_short = katalysator == 'NEGATIV'

        # KONFLIKT ERKENNUNG (DELL-Fall: CALL 76x aber Kurs fällt stark)
        # Hohe Call Vol/OI bei fallendem Kurs = Calls sind Short-Absicherung, kein echter Kauf
        hedge_calls = max_call_voi >= 20 and prev_chg <= -4
        hedge_puts  = max_put_voi  >= 20 and prev_chg >= 4
        if hedge_calls:
            long_score  = max(0, long_score - 6)
            reasons_long.append(f'WARNUNG: CALL {max_call_voi:.0f}x aber Kurs {prev_chg:+.1f}% — Short-Absicherung')
            poly_long = False
        if hedge_puts:
            short_score = max(0, short_score - 6)
            reasons_short.append(f'WARNUNG: PUT {max_put_voi:.0f}x aber Kurs {prev_chg:+.1f}% — Long-Absicherung')
            poly_short = False

        # Signal-Basis bestimmen
        if signal == 'LONG':
            if poly_long and news_long:
                signal_basis = 'POLYGON_CONFIRMED'  # beide einig → höchste Conviction
                conviction   = 0.85
            elif poly_long and not news_long:
                signal_basis = 'POLYGON_ONLY'       # Smart Money weiß was, News fehlt noch
                conviction   = 0.65
            elif news_long and not poly_long:
                signal_basis = 'NEWS_ONLY'           # Retail reagiert, zu spät
                conviction   = 0.40
            elif hedge_calls:
                signal_basis = 'CONFLICT'
                conviction   = 0.25
            else:
                signal_basis = 'WEAK'
                conviction   = 0.30
        elif signal == 'SHORT':
            if poly_short and news_short:
                signal_basis = 'POLYGON_CONFIRMED'
                conviction   = 0.88
            elif poly_short and not news_short:
                signal_basis = 'POLYGON_ONLY'
                conviction   = 0.68
            elif news_short and not poly_short:
                signal_basis = 'NEWS_ONLY'
                conviction   = 0.42
            elif hedge_puts:
                signal_basis = 'CONFLICT'
                conviction   = 0.25
            else:
                signal_basis = 'WEAK'
                conviction   = 0.30
        else:
            signal_basis = 'WATCH'
            conviction   = 0.0

        # Conviction aus gelernten Patterns anwenden
        try:
            patterns = _load_patterns()
            for pat in patterns.get('patterns', []):
                if pat.get('signal_basis') == signal_basis and pat.get('direction') == signal:
                    hist_rate = pat.get('success_rate', conviction)
                    conviction = round((conviction + hist_rate) / 2, 2)
                    break
        except Exception:
            pass

        ziel = mult = None
        if best and signal != 'WATCH':
            ziel = (price + atr) if signal == 'LONG' else (price - atr)
            gain = max(0, ziel - best['strike']) if signal == 'LONG' else max(0, best['strike'] - ziel)
            mult = f"{gain / best['pr']:.0f}x" if best['pr'] > 0 and gain > 0 else None

        # Haupt-Grund für das Signal
        kat_text_full = al_news_text + kat_text
        if signal == 'LONG' and reasons_long:
            kat_text_full = reasons_long[0] + (' | ' + kat_text[:40] if kat_text else '')
        elif signal == 'SHORT' and reasons_short:
            kat_text_full = reasons_short[0] + (' | ' + kat_text[:40] if kat_text else '')

        return {
            't': ticker, 'price': round(price, 2), 'signal': signal, 'score': score,
            'pc': round(pc, 3), 'cp': round(cp), 'pp': round(pp),
            'trend': round(trend_pct, 1), 'prev_chg': round(prev_chg, 1),
            'drop_high': round(drop_from_high, 1), 'short_trend': round(short_trend, 1),
            'long_score': long_score, 'short_score': short_score,
            'katalysator': katalysator, 'kat_strength': katalysator_strength, 'kat_text': kat_text_full,
            'signal_basis': signal_basis, 'conviction': conviction,
            'earnings': earnings_analysis,
            'best': best, 'otype': otype, 'ziel': ziel, 'mult': mult,
            'atr': round(atr, 2), 'today': today, 'conflict': conflict,
            'kat_url': kat_url, 'social_score': social_score,
            'dp': dp, 'sweep': sweep,
            'smart_money': {
                'anomalies':     sm['anomalies'][:3],
                'expected_move': sm['expected_move'],
                'call_premium':  sm['call_premium'],
                'put_premium':   sm['put_premium'],
                'max_call_voi':  sm['max_call_vol_oi'],
                'max_put_voi':   sm['max_put_vol_oi'],
            },
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
    news_cutoff= (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

    # Social Trending für Universe-Erweiterung + Score-Boost (aus Cache — kein Fetch)
    social_tickers, _ = get_cached_social()
    extra_social = [t for t in social_tickers if t not in UNIVERSE][:8]  # max 8 extra
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

    # PRE-MOVER: HIGH/EXTREME Katalysator → sofort auf Next-Mover Liste
    # Auch wenn Options noch keine Anomalie zeigen — News-Setup ist genug
    pre_movers_high = [
        r for r in results
        if r.get('kat_strength') in ('HIGH', 'EXTREME')
        and r['katalysator'] == 'POSITIV'
        and r['signal'] != 'SHORT'
    ]

    # NEXT MOVER: Kleines Cap + Katalysator + billiger Call (Hauptziel: 10%+ Mover)
    movers_classic = [
        r for r in watch + longs
        if r['price'] < 200 and r['pc'] < 0.45
        and r['katalysator'] == 'POSITIV' and r.get('best')
        and r['best']['pr'] < 1.0
    ]

    # Kombiniert: HIGH/EXTREME zuerst, dann classic, dedup
    seen_movers = set()
    movers_combined = []
    for r in sorted(pre_movers_high, key=lambda x: (
            0 if x.get('kat_strength') == 'EXTREME' else 1, -x['long_score'])):
        if r['t'] not in seen_movers:
            seen_movers.add(r['t'])
            movers_combined.append(r)
    for r in sorted(movers_classic, key=lambda x: (x['pc'], -x['long_score'])):
        if r['t'] not in seen_movers:
            seen_movers.add(r['t'])
            movers_combined.append(r)
    movers = movers_combined[:8]

    # PRE-SHORT: HIGH/EXTREME negative Katalysator (Dilution, SEC, DOJ)
    pre_shorts_high = sorted(
        [r for r in results
         if r.get('kat_strength') in ('HIGH', 'EXTREME')
         and r['katalysator'] == 'NEGATIV'],
        key=lambda x: -x['short_score']
    )[:5]

    for r in results:
        r['is_social'] = r['t'] in social_tickers

    return {
        'longs':         longs[:10],
        'shorts':        shorts[:10],
        'watch':         watch,
        'movers':        movers,
        'pre_shorts':    pre_shorts_high,
        'social':        social_tickers[:10],
        'scanned':       len(results),
        'total':         len(universe),
        'time':          datetime.now().strftime('%Y-%m-%d %H:%M'),
        'today':         today,
    }

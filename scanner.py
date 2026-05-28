import urllib.request, json, ssl, time, threading, os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf

ctx = ssl.create_default_context()
API = os.environ.get('POLYGON_API_KEY', 'xwPphlChrHFlmFPxSkod8WwMdwALipY5')

UNIVERSE = [
    'NVDA','AMD','META','AAPL','MSFT','AMZN','GOOGL','TSLA','NFLX',
    'MU','INTC','AVGO','QCOM','MRVL','SMCI','ARM','AMAT',
    'PLTR','CRWD','NET','DDOG','SOUN',
    'IONQ','RGTI','IREN','WULF','DELL','HPE',
    'GS','JPM','BAC','ASTS','LUNR','RKLB',
    'GLD','SLV','USO','AAL','DAL',
]

POS_KEYS = ['contract','government','deal','partnership','upgrade','raised','beat',
            'record','billion','trump','invest','breakthrough','ai','quantum','launch',
            'buyback','dividend','acquisition','target','infrastructure','pivot',
            'revenue','earnings','profit','surge','soar','stake','award']
NEG_KEYS = ['lawsuit','downgrade','miss','cut','investigation','fraud',
            'recall','ban','warning','below','probe','short seller','loss',
            'decline','disappoint','weak','concern','risk']

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

        df = yf.download(ticker, period='20d', interval='1d', progress=False, auto_adjust=True)
        if df is None or len(df) < 3:
            return None
        closes = list(df['Close'].values.flatten().astype(float))
        highs  = list(df['High'].values.flatten().astype(float))
        lows_a = df['Low'].values.flatten().astype(float)
        atr = max(highs[-i] - float(lows_a[-i]) for i in range(1, 4))
        c10 = closes[-10:] if len(closes) >= 10 else closes
        trend_pct   = ((c10[-1] - c10[0]) / c10[0]) * 100
        c3 = closes[-3:]
        short_trend = ((c3[-1] - c3[0]) / c3[0]) * 100
        prev_chg    = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0
        period_high = max(highs[-10:]) if len(highs) >= 10 else max(highs)
        drop_from_high = ((closes[-1] - period_high) / period_high) * 100

        news_data = poly_fetch(f'https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=5&apiKey={API}')
        katalysator = 'KEIN'
        kat_text = ''
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
                break
            elif has_neg or sent == 'negative':
                if katalysator != 'POSITIV':
                    katalysator = 'NEGATIV'
                    kat_text = n.get('title', '')[:70]

        long_score = short_score = 0
        if pc < 0.3:    long_score  += 3
        elif pc < 0.5:  long_score  += 1
        if pc > 0.8:    short_score += 3
        elif pc > 0.6:  short_score += 1
        if trend_pct < -7:    short_score += 4
        elif trend_pct < -4:  short_score += 2
        elif trend_pct < -2:  short_score += 1
        if 5 < trend_pct < 25: long_score  += 2
        if trend_pct >= 25:    short_score += 3
        if drop_from_high < -8:    short_score += 4
        elif drop_from_high < -5:  short_score += 2
        elif drop_from_high < -3:  short_score += 1
        if short_trend < -3:   short_score += 2
        elif short_trend < -1: short_score += 1
        if short_trend > 3:    long_score  += 1
        if katalysator == 'POSITIV': long_score  += 4
        if katalysator == 'NEGATIV': short_score += 4
        if cp > pp * 3: long_score  += 1
        if pp > cp * 2: short_score += 1

        bc = best_option(calls, True,  price, today, exp_cutoff)
        bp = best_option(puts,  False, price, today, exp_cutoff)

        tie_goes_short = drop_from_high < -8 and short_score >= 4

        if short_score >= 4 and (short_score > long_score or tie_goes_short) and bp:
            signal, score, best, otype = 'SHORT', short_score, bp, 'PUT'
        elif long_score >= 4 and long_score > short_score and bc:
            signal, score, best, otype = 'LONG',  long_score,  bc, 'CALL'
        else:
            signal, score, best, otype = 'WATCH', 0, bc or bp, None

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
            'katalysator': katalysator, 'kat_text': kat_text,
            'best': best, 'otype': otype, 'ziel': ziel, 'mult': mult,
            'atr': round(atr, 2), 'today': today
        }
    except Exception as e:
        return None


def run_scan(progress_cb=None):
    today      = datetime.now().strftime('%Y-%m-%d')
    exp_cutoff = (datetime.now() + timedelta(days=35)).strftime('%Y-%m-%d')
    news_cutoff= (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')

    results = []
    total = len(UNIVERSE)
    done  = [0]
    lock  = threading.Lock()

    def scan_one(args):
        i, ticker = args
        r = scan_ticker(ticker, today, exp_cutoff, news_cutoff)
        with lock:
            done[0] += 1
            if progress_cb:
                progress_cb(done[0], total, ticker)
        return r

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(scan_one, (i, t)): t for i, t in enumerate(UNIVERSE)}
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)

    longs  = sorted([r for r in results if r['signal'] == 'LONG'],  key=lambda x: x['score'], reverse=True)
    shorts = sorted([r for r in results if r['signal'] == 'SHORT'], key=lambda x: x['score'], reverse=True)
    watch  = [r for r in results if r['signal'] == 'WATCH']

    # NEXT MOVER: Kleines Cap + Katalysator + billiger Call
    movers = []
    for r in watch + longs:
        if r['price'] < 200 and r['pc'] < 0.45 and r['katalysator'] == 'POSITIV' and r.get('best'):
            if r['best']['pr'] < 1.0:
                movers.append(r)
    movers = sorted(movers, key=lambda x: (x['pc'], -x['long_score']))[:5]

    return {
        'longs': longs[:10],
        'shorts': shorts[:10],
        'watch': watch,
        'movers': movers,
        'scanned': len(results),
        'total': total,
        'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'today': today
    }

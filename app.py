import json, os, threading, time
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request
import urllib.request, ssl

app = Flask(__name__)

RESULTS_FILE  = 'results.json'
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TG_CHAT  = os.environ.get('TELEGRAM_CHAT',  '')

# Auto-Scan: täglich 09:30 ET = 13:30 UTC (Sommer) / 14:30 UTC (Winter)
AUTO_SCAN_UTC_HOUR   = 13
AUTO_SCAN_UTC_MINUTE = 30

state = {
    'running':        False,
    'progress':       0,
    'progress_total': 39,
    'current_ticker': '',
    'results':        None,
    'last_scan':      None,
    'next_scan':      None,
    'error':          None,
    'last_results_hash': None,
    'followup':       None,
    'followup_date':  None,
}

# Follow-up: 10:00 ET = 14:00 UTC täglich
FOLLOWUP_UTC_HOUR   = 14
FOLLOWUP_UTC_MINUTE = 0

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
    except Exception as e:
        state['error'] = str(e)
        tg_send(f'Scanner Fehler: {e}')
    finally:
        state['running']        = False
        state['progress']       = 0
        state['current_ticker'] = ''
        state['next_scan']      = next_scan_time()

def run_followup():
    """Prüft um 10:00 ET ob die Signale von heute ihr Ziel erreicht haben."""
    today = datetime.now().strftime('%Y-%m-%d')
    if state['followup_date'] == today:
        return  # Heute schon gemacht
    data = state['results'] or load_results()
    if not data or data.get('today') != today:
        return  # Kein heutiger Scan

    import yfinance as yf
    longs  = data.get('longs', [])[:5]
    shorts = data.get('shorts', [])[:5]
    followup_results = {'longs': [], 'shorts': [], 'time': datetime.now().strftime('%H:%M')}

    for r in longs:
        try:
            df = yf.download(r['t'], period='1d', interval='5m', progress=False, auto_adjust=True)
            if df is not None and len(df) > 0:
                current = float(df['Close'].iloc[-1])
                entry   = r['price']
                ziel    = r.get('ziel') or (entry * 1.02)
                chg_pct = (current - entry) / entry * 100
                won     = current >= ziel
                followup_results['longs'].append({'t': r['t'], 'signal': 'LONG', 'entry': entry,
                    'current': round(current, 2), 'ziel': round(ziel, 2),
                    'chg_pct': round(chg_pct, 1), 'won': won})
        except Exception:
            pass

    for r in shorts:
        try:
            df = yf.download(r['t'], period='1d', interval='5m', progress=False, auto_adjust=True)
            if df is not None and len(df) > 0:
                current = float(df['Close'].iloc[-1])
                entry   = r['price']
                ziel    = r.get('ziel') or (entry * 0.98)
                chg_pct = (current - entry) / entry * 100
                won     = current <= ziel
                followup_results['shorts'].append({'t': r['t'], 'signal': 'SHORT', 'entry': entry,
                    'current': round(current, 2), 'ziel': round(ziel, 2),
                    'chg_pct': round(chg_pct, 1), 'won': won})
        except Exception:
            pass

    state['followup']      = followup_results
    state['followup_date'] = today

    # Telegram Report
    lines = [f'<b>📊 10:00 SIGNAL CHECK — {today}</b>\n']
    all_res = followup_results['longs'] + followup_results['shorts']
    winners = sum(1 for r in all_res if r['won'])
    losers  = len(all_res) - winners
    lines.append(f'✅ Gewinner: {winners} | ❌ Verlierer: {losers}\n')
    for r in all_res:
        icon = '✅' if r['won'] else '❌'
        lines.append(f'{icon} <b>{r["t"]}</b> {r["signal"]}: {r["chg_pct"]:+.1f}% '
                     f'(Entry: ${r["entry"]} → Jetzt: ${r["current"]} | Ziel: ${r["ziel"]})')
    tg_send('\n'.join(lines))

# ── Auto-Scheduler: jeden Tag 09:30 ET ───────────────────────────────────────

def auto_scheduler():
    while True:
        now = datetime.now(timezone.utc)
        # Nächsten Scan-Zeitpunkt berechnen
        target_scan = now.replace(hour=AUTO_SCAN_UTC_HOUR, minute=AUTO_SCAN_UTC_MINUTE,
                                  second=0, microsecond=0)
        if target_scan <= now:
            target_scan += timedelta(days=1)
        # Nächsten Follow-up Zeitpunkt
        target_fu = now.replace(hour=FOLLOWUP_UTC_HOUR, minute=FOLLOWUP_UTC_MINUTE,
                                second=0, microsecond=0)
        if target_fu <= now:
            target_fu += timedelta(days=1)
        # Das frühere Event abwarten
        next_event = min(target_scan, target_fu)
        wait_sec = (next_event - now).total_seconds()
        state['next_scan'] = next_scan_time()
        time.sleep(max(wait_sec, 1))
        now2 = datetime.now(timezone.utc)
        # Scan starten?
        if not state['running'] and abs((now2 - target_scan).total_seconds()) < 120:
            t = threading.Thread(target=run_scan_thread, kwargs={'trigger': 'auto'}, daemon=True)
            t.start()
        # Follow-up starten?
        if abs((now2 - target_fu).total_seconds()) < 120:
            tf = threading.Thread(target=run_followup, daemon=True)
            tf.start()

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
.header { background: linear-gradient(135deg, #1a2540 0%, #0d1628 100%); padding: 14px 16px 10px; border-bottom: 1px solid #1e3a5f; position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 18px; color: #4db8ff; letter-spacing: 1px; display:inline; }
.live-dot { display:inline-block; width:8px; height:8px; background:#4dff91; border-radius:50%; margin-left:8px; animation: pulse 2s infinite; vertical-align:middle; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.header-info { font-size: 11px; color: #6b8cad; margin-top: 5px; display:flex; gap:12px; flex-wrap:wrap; }
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
  <h1>OPTIONS SCANNER</h1><span class="live-dot" id="liveDot"></span>
  <div class="header-info">
    <span id="lastScanInfo">Lade...</span>
    <span id="nextScanInfo"></span>
  </div>
</div>

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
  <div class="empty">Klicke <b>SCAN STARTEN</b> oder warte auf den täglichen Auto-Scan.<br><br>
  Der Scanner startet automatisch täglich um <b>09:30 Uhr ET</b>.<br>
  Ergebnisse erscheinen automatisch auf dieser Seite.</div>
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
  let opt = b ? optionBox(b, r.otype || (cls === 'short' ? 'PUT' : 'CALL'), r.mult, r.today) : '';
  // News mit Link
  let kat = '';
  if (r.kat_text) {
    kat = r.kat_url
      ? '<div class="kat-text"><a href="' + r.kat_url + '" target="_blank" style="color:#4db8ff;text-decoration:none">' + r.kat_text + ' ↗</a></div>'
      : '<div class="kat-text">' + r.kat_text + '</div>';
  }
  let flash = isNew ? ' new-flash' : '';

  return '<div class="card' + flash + '">'
    + '<div class="card-header">'
    +   '<div><span class="ticker">' + r.t + '</span>'
    +   '<span class="price ' + sigColor + '" style="margin-left:10px">$' + r.price + '</span></div>'
    +   '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + katBadge + conflictBadge + socialBadge
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
    + '</div></div>';
}

function renderResults(data, isNew) {
  let html = '';

  if (data.movers && data.movers.length > 0) {
    html += '<div class="section"><div class="section-title mover">NEXT MOVER — Kleines Cap + Katalysator + Billiger Call</div>';
    data.movers.forEach(r => { html += renderCard(r, 'mover', isNew); });
    html += '</div>';
  }

  html += '<div class="section"><div class="section-title long">TOP LONG — Katalysator + P/C tief</div>';
  if (!data.longs || data.longs.length === 0) {
    html += '<div class="empty">Keine Long-Signale.</div>';
  } else {
    data.longs.slice(0, 5).forEach(r => { html += renderCard(r, 'long', isNew); });
  }
  html += '</div>';

  html += '<div class="section"><div class="section-title short">TOP SHORT — Überbewertet / Fallend</div>';
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
        ? '<a href="' + n.kat_url + '" target="_blank" style="color:#c0d4e8;text-decoration:none">' + n.kat_text + ' <span style="color:#4db8ff;font-size:10px">↗ LINK</span></a>'
        : n.kat_text;
      html += '<div class="news-card">'
        + '<div class="news-ticker">' + n.t + ' &nbsp; ' + pct(n.trend) + '</div>'
        + '<div class="news-title">'  + titleHtml + '</div>'
        + '<span class="news-kat ' + cls + '">' + label + '</span>'
        + '</div>';
    });
    html += '</div>';
  }

  // Social Trending
  if (data.social && data.social.length > 0) {
    html += '<div class="section"><div class="section-title" style="color:#b070ff;border-left:3px solid #b070ff">🔥 REDDIT/X TRENDING (' + data.social.length + ')</div>';
    html += '<div style="background:#111827;border:1px solid #2a1a4a;margin:8px;border-radius:10px;padding:10px 14px;display:flex;flex-wrap:wrap;gap:8px">';
    data.social.forEach(t => {
      let inScan = (data.longs || []).concat(data.shorts || []).find(r => r.t === t);
      let badge  = inScan ? (inScan.signal === 'LONG' ? ' style="background:#0d3a1f;color:#4dff91"' : ' style="background:#3a0d1a;color:#ff4d6b"') : '';
      let sig    = inScan ? ' ' + inScan.signal : '';
      html += '<span style="background:#1a1a3a;border:1px solid #4030aa;color:#b070ff;padding:4px 10px;border-radius:12px;font-size:12px;font-weight:bold"' + badge + '>' + t + sig + '</span>';
    });
    html += '</div></div>';
  }

  // 10:00 Follow-up
  if (data.followup && (data.followup.longs || data.followup.shorts)) {
    html += '<div class="section"><div class="section-title" style="color:#ffd700;border-left:3px solid #ffd700">📊 10:00 SIGNAL CHECK — Gewinner & Verlierer</div>';
    html += '<div style="background:#111827;border:1px solid #2a2000;margin:8px;border-radius:10px;overflow:hidden">';
    let allFu = (data.followup.longs || []).concat(data.followup.shorts || []);
    allFu.forEach(f => {
      let won = f.won;
      let color = won ? '#4dff91' : '#ff4d6b';
      let icon  = won ? '✅' : '❌';
      html += '<div style="padding:8px 14px;border-bottom:1px solid #1e2a3a;display:flex;justify-content:space-between">'
        + '<span style="font-weight:bold;color:#fff">' + icon + ' ' + f.t + ' ' + f.signal + '</span>'
        + '<span style="color:' + color + ';font-size:13px">' + (f.chg_pct >= 0 ? '+' : '') + f.chg_pct.toFixed(1) + '% (Ziel: ' + (f.won ? 'Erreicht' : 'Nicht erreicht') + ')</span>'
        + '</div>';
    });
    html += '</div></div>';
  }

  // Watch
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
  let scanInfo = d.last_scan ? 'Letzter Scan: ' + d.last_scan : 'Noch kein Scan';
  document.getElementById('lastScanInfo').textContent = scanInfo;
  if (d.next_scan) {
    document.getElementById('nextScanInfo').textContent = 'Auto-Scan: ' + d.next_scan;
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
    document.getElementById('content').innerHTML = renderResults(data, isNew);
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
    })

@app.route('/results')
def results():
    data = state['results'] or load_results()
    if not data:
        return jsonify({'error': 'Noch kein Scan. Drücke SCAN STARTEN.'})
    # Follow-up aus Cache anhängen
    if state.get('followup'):
        data = dict(data)
        data['followup'] = state['followup']
    return jsonify(data)

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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

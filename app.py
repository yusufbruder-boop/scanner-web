import json, os, threading, time
from datetime import datetime
from flask import Flask, jsonify, render_template_string
import urllib.request, ssl

app = Flask(__name__)

# State
state = {
    'running': False,
    'progress': 0,
    'progress_total': 39,
    'current_ticker': '',
    'results': None,
    'last_scan': None,
    'error': None,
}

RESULTS_FILE = 'results.json'
TG_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8298160314:AAGa4X8RyMU6h8dRwTbmyHU53w_hIPgonGA')
TG_CHAT  = os.environ.get('TELEGRAM_CHAT',  '5872959107')

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

def progress_cb(i, total, ticker):
    state['progress'] = i
    state['progress_total'] = total
    state['current_ticker'] = ticker

def run_scan_thread():
    from scanner import run_scan
    state['running'] = True
    state['error'] = None
    try:
        results = run_scan(progress_cb=progress_cb)
        state['results'] = results
        state['last_scan'] = results['time']
        save_results(results)

        # Telegram Zusammenfassung
        lines = [f'<b>OPTIONS SCANNER {results["time"]}</b>']
        lines.append(f'Gescannt: {results["scanned"]}/{results["total"]} Aktien\n')

        lines.append('<b>TOP LONG:</b>')
        for r in results['longs'][:5]:
            b = r.get('best') or {}
            mult = r.get('mult') or '?'
            lines.append(f'  UP {r["t"]}  ${r["price"]}  P/C:{r["pc"]}  Score:{r["score"]}')
            if b:
                lines.append(f'     CALL ${b.get("strike")} @ ${b.get("pr")}  Exp:{b.get("exp")}  {mult}')

        lines.append('\n<b>TOP SHORT:</b>')
        for r in results['shorts'][:5]:
            b = r.get('best') or {}
            mult = r.get('mult') or '?'
            lines.append(f'  DN {r["t"]}  ${r["price"]}  Score:{r["score"]}  Drop:{r["drop_high"]}%')
            if b:
                lines.append(f'     PUT ${b.get("strike")} @ ${b.get("pr")}  Exp:{b.get("exp")}  {mult}')

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
        state['running'] = False
        state['progress'] = 0
        state['current_ticker'] = ''

# HTML Template
HTML = '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Options Scanner</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0e1a; color: #e0e6f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }
.header { background: linear-gradient(135deg, #1a2540 0%, #0d1628 100%); padding: 16px; border-bottom: 1px solid #1e3a5f; position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 18px; color: #4db8ff; letter-spacing: 1px; }
.header .sub { font-size: 11px; color: #6b8cad; margin-top: 3px; }
.scan-btn { display: block; width: calc(100% - 32px); margin: 16px; padding: 14px; background: linear-gradient(135deg, #1a6b3c, #0d4a28); color: #4dff91; font-size: 16px; font-weight: bold; border: 1px solid #2d9e57; border-radius: 10px; cursor: pointer; text-align: center; letter-spacing: 1px; }
.scan-btn:disabled { background: #1a2540; color: #4a6a8a; border-color: #2a3a5a; }
.progress-wrap { margin: 0 16px 16px; }
.progress-bar { height: 6px; background: #1e3a5f; border-radius: 3px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #4db8ff, #4dff91); transition: width 0.5s; }
.progress-text { font-size: 11px; color: #6b8cad; margin-top: 5px; text-align: center; }
.section { margin: 8px 0; }
.section-title { background: #111827; padding: 10px 16px; font-size: 12px; font-weight: bold; letter-spacing: 2px; color: #6b8cad; border-top: 1px solid #1e3a5f; border-bottom: 1px solid #1e3a5f; }
.section-title.long  { color: #4dff91; border-left: 3px solid #4dff91; }
.section-title.short { color: #ff4d6b; border-left: 3px solid #ff4d6b; }
.section-title.mover { color: #ffd700; border-left: 3px solid #ffd700; }
.section-title.news  { color: #4db8ff; border-left: 3px solid #4db8ff; }
.card { background: #111827; border: 1px solid #1e3a5f; margin: 8px; border-radius: 10px; overflow: hidden; }
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
.empty       { text-align: center; color: #4a6a8a; padding: 20px; font-size: 13px; }
.last-scan   { font-size: 11px; color: #4a6a8a; text-align: center; padding: 8px; }
.pct-pos { color: #4dff91; }
.pct-neg { color: #ff4d6b; }
.zdte { color: #ff9900; font-size: 10px; font-weight: bold; }
</style>
</head>
<body>
<div class="header">
  <h1>OPTIONS SCANNER</h1>
  <div class="sub" id="last-scan">Lade...</div>
</div>

<button class="scan-btn" id="scanBtn" onclick="startScan()">SCAN STARTEN</button>
<div class="progress-wrap" id="progressWrap" style="display:none">
  <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  <div class="progress-text" id="progressText">Initialisiere...</div>
</div>

<div id="content">
  <div class="empty">Klicke SCAN STARTEN um den Scanner zu starten.<br>Dauert ~15 Minuten.</div>
</div>

<script>
let polling = null;

function startScan() {
  fetch('/start', {method:'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        document.getElementById('scanBtn').disabled = true;
        document.getElementById('progressWrap').style.display = 'block';
        startPolling();
      } else {
        alert(d.msg || 'Fehler');
      }
    });
}

function startPolling() {
  if (polling) clearInterval(polling);
  polling = setInterval(checkStatus, 5000);
}

function checkStatus() {
  fetch('/status').then(r => r.json()).then(d => {
    if (d.running) {
      let pct = d.total > 0 ? Math.round(d.progress / d.total * 100) : 0;
      document.getElementById('progressFill').style.width = pct + '%';
      document.getElementById('progressText').textContent =
        `Scanne ${d.current} (${d.progress}/${d.total}) — ${pct}%`;
    } else {
      clearInterval(polling);
      document.getElementById('scanBtn').disabled = false;
      document.getElementById('progressWrap').style.display = 'none';
      loadResults();
    }
  });
}

function pct(v) {
  let cls = v >= 0 ? 'pct-pos' : 'pct-neg';
  let s = v >= 0 ? '+' : '';
  return `<span class="${cls}">${s}${v.toFixed(1)}%</span>`;
}

function optionBox(b, otype, mult, today) {
  if (!b) return '';
  let color = otype === 'CALL' ? '#4dff91' : '#ff4d6b';
  let zdte = b.exp === today ? '<span class="zdte"> [HEUTE]</span>' : '';
  let m = mult ? `<span class="hebel">${mult}</span>` : '';
  return `
    <div class="option-box">
      <div class="option-main" style="color:${color}">
        ${otype} $${b.strike} (${b.pct > 0 ? '+' : ''}${b.pct}%)  @ <b>$${b.pr}</b>  ${m}
      </div>
      <div class="option-detail">
        Exp: ${b.exp}${zdte} &nbsp;|&nbsp; Vol: ${b.vol.toLocaleString()} &nbsp;|&nbsp; OI: ${b.oi.toLocaleString()}
      </div>
    </div>`;
}

function renderCard(r, cls) {
  let b = r.best;
  let sigColor = cls === 'long' ? 'signal-long' : (cls === 'short' ? 'signal-short' : 'signal-mover');
  let badge = cls === 'long' ? 'badge-long' : (cls === 'short' ? 'badge-short' : 'badge-mover');
  let sigLabel = r.signal;
  let katBadge = r.katalysator !== 'KEIN' ?
    `<span class="badge badge-kat">${r.katalysator === 'POSITIV' ? 'POSITIV NEWS' : 'NEGATIV NEWS'}</span>` : '';
  let opt = b ? optionBox(b, r.otype || (cls==='short'?'PUT':'CALL'), r.mult, r.today) : '';
  let kat = r.kat_text ? `<div class="kat-text">${r.kat_text}</div>` : '';

  return `
  <div class="card">
    <div class="card-header">
      <div>
        <span class="ticker">${r.t}</span>
        <span class="price ${sigColor}" style="margin-left:10px">$${r.price}</span>
      </div>
      <div style="display:flex;gap:6px;align-items:center">
        ${katBadge}
        <span class="badge ${badge}">${sigLabel} ${r.score > 0 ? r.score : ''}</span>
      </div>
    </div>
    <div class="card-body">
      <div class="row">
        <div class="stat"><span class="stat-label">Trend 10T</span><span class="stat-value">${pct(r.trend)}</span></div>
        <div class="stat"><span class="stat-label">Vortag</span><span class="stat-value">${pct(r.prev_chg)}</span></div>
        <div class="stat"><span class="stat-label">P/C Ratio</span><span class="stat-value">${r.pc}</span></div>
        <div class="stat"><span class="stat-label">Hoch-Abst.</span><span class="stat-value ${r.drop_high < -5 ? 'pct-neg' : ''}">${r.drop_high}%</span></div>
      </div>
      ${opt}
      ${kat}
    </div>
  </div>`;
}

function loadResults() {
  fetch('/results').then(r => r.json()).then(data => {
    if (!data || data.error) {
      document.getElementById('content').innerHTML =
        `<div class="empty">${data && data.error ? data.error : 'Kein Scan vorhanden. Starte den Scanner.'}</div>`;
      return;
    }

    document.getElementById('last-scan').textContent = 'Letzter Scan: ' + data.time +
      ' | ' + data.scanned + '/' + data.total + ' Aktien';

    let html = '';

    // NEXT MOVER
    if (data.movers && data.movers.length > 0) {
      html += '<div class="section"><div class="section-title mover">NEXT MOVER — Kleines Cap + Katalysator + Billiger Call</div>';
      data.movers.forEach(r => { html += renderCard(r, 'mover'); });
      html += '</div>';
    }

    // TOP LONG
    html += '<div class="section"><div class="section-title long">TOP LONG — Katalysator + P/C tief</div>';
    if (data.longs.length === 0) {
      html += '<div class="empty">Keine Long-Signale heute.</div>';
    } else {
      data.longs.slice(0, 5).forEach(r => { html += renderCard(r, 'long'); });
    }
    html += '</div>';

    // TOP SHORT
    html += '<div class="section"><div class="section-title short">TOP SHORT — Überbewertet / Fallend</div>';
    if (data.shorts.length === 0) {
      html += '<div class="empty">Keine Short-Signale heute.</div>';
    } else {
      data.shorts.slice(0, 5).forEach(r => { html += renderCard(r, 'short'); });
    }
    html += '</div>';

    // NEWS aus allen gescannten Aktien
    let news_items = [];
    let all = (data.longs || []).concat(data.shorts || []).concat(data.watch || []);
    all.forEach(r => {
      if (r.katalysator !== 'KEIN' && r.kat_text) {
        news_items.push({t: r.t, text: r.kat_text, kat: r.katalysator, trend: r.trend});
      }
    });
    if (news_items.length > 0) {
      html += '<div class="section"><div class="section-title news">NACHRICHTEN — Katalysatoren</div>';
      news_items.slice(0, 15).forEach(n => {
        let cls = n.kat === 'POSITIV' ? 'news-pos' : 'news-neg';
        let label = n.kat === 'POSITIV' ? 'POSITIV' : 'NEGATIV';
        html += `<div class="news-card">
          <div class="news-ticker">${n.t} &nbsp; ${pct(n.trend)}</div>
          <div class="news-title">${n.text}</div>
          <span class="news-kat ${cls}">${label}</span>
        </div>`;
      });
      html += '</div>';
    }

    // WATCH Liste
    if (data.watch && data.watch.length > 0) {
      html += '<div class="section"><div class="section-title">WATCH — Kein klares Signal</div>';
      let watchHtml = data.watch.map(r =>
        `<div style="padding:6px 16px;border-bottom:1px solid #1e2a3a;display:flex;justify-content:space-between;align-items:center">
          <span style="font-weight:bold;color:#a0b4c8">${r.t}</span>
          <span style="color:#6b8cad;font-size:12px">$${r.price} &nbsp; ${pct(r.trend)} &nbsp; L:${r.long_score}/S:${r.short_score}</span>
        </div>`).join('');
      html += `<div style="background:#111827;border:1px solid #1e3a5f;margin:8px;border-radius:10px;overflow:hidden">${watchHtml}</div>`;
      html += '</div>';
    }

    html += `<div class="last-scan">Letzter Scan: ${data.time}</div>`;
    document.getElementById('content').innerHTML = html;
  });
}

// Status prüfen beim Laden
fetch('/status').then(r => r.json()).then(d => {
  if (d.running) {
    document.getElementById('scanBtn').disabled = true;
    document.getElementById('progressWrap').style.display = 'block';
    startPolling();
  } else if (d.has_results) {
    loadResults();
  }
});
</script>
</body>
</html>'''

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/start', methods=['POST'])
def start():
    if state['running']:
        return jsonify({'ok': False, 'msg': 'Scanner läuft bereits'})
    t = threading.Thread(target=run_scan_thread, daemon=True)
    t.start()
    return jsonify({'ok': True})

@app.route('/status')
def status():
    return jsonify({
        'running': state['running'],
        'progress': state['progress'],
        'total': state['progress_total'],
        'current': state['current_ticker'],
        'last_scan': state['last_scan'],
        'has_results': state['results'] is not None or os.path.exists(RESULTS_FILE),
        'error': state['error'],
    })

@app.route('/results')
def results():
    data = state['results'] or load_results()
    if not data:
        return jsonify({'error': 'Noch kein Scan. Starte mit SCAN STARTEN.'})
    return jsonify(data)

# Beim Start: letzte Ergebnisse laden
if __name__ == '__main__':
    saved = load_results()
    if saved:
        state['results'] = saved
        state['last_scan'] = saved.get('time')
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

"""
Baltic Exchange Airfreight Rate Index Crawler (v6)
- Fix: Embed JSON in <script type="application/json"> to avoid syntax errors
- Multi-CDN fallback
- Native HTML range slider (no external dep)
- Categorized insights + CSV/XLSX download
"""
import re
import json
import os
import sys
import tempfile
import sqlite3
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library is not installed. Run: pip install requests")
    sys.exit(1)

INFOGRAM_ID = "SuAWaj6s2iFahJI4zPJw"
PARENT_URL = "https://www.aircargonews.net/data-hub/industry-data/baltic-exchange-airfreight-rate-index/"
EMBED_URL = "https://e.infogram.com/_/" + INFOGRAM_ID + "?src=embed"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": PARENT_URL,
}

# SQLite DB location. Set TAC_DB_PATH to override it.
# Example (Windows): set TAC_DB_PATH=C:\data\tac_index.db
# Example (macOS/Linux): export TAC_DB_PATH=/data/tac_index.db
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tac_index.db")
DB_PATH = os.environ.get("TAC_DB_PATH", DEFAULT_DB_PATH)

# Local dashboard web server
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 9001
DASHBOARD_ROUTE = "/TAC_Index"


def fetch_html():
    print("[1] Fetching Infogram embed...")
    r = requests.get(EMBED_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    print("    OK HTTP " + str(r.status_code) + ", size=" + str(len(r.text)))
    return r.text


def extract_data(html):
    print("[2] Extracting infographicData...")
    m = re.search(r"window\.infographicData\s*=\s*(\{.+?\});", html, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    idx = html.find("window.infographicData")
    if idx < 0:
        raise RuntimeError("infographicData not found")
    eq = html.find("=", idx)
    start = html.find("{", eq)
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:i+1])
    raise RuntimeError("JSON parse failed")


def parse_chart(data):
    print("[3] Parsing chart...")
    entities = data.get("elements", {}).get("content", {}).get("content", {}).get("entities", {})
    for eid, ent in entities.items():
        if ent.get("type") != "CHART":
            continue
        cd = ent.get("props", {}).get("chartData", {})
        sheets = cd.get("data", [])
        if not sheets or not sheets[0]:
            continue
        sheet = sheets[0]
        rows = []
        for row in sheet:
            if not isinstance(row, list):
                continue
            vals = [c.get("value", "") if isinstance(c, dict) else c for c in row]
            rows.append(vals)
        return rows
    raise RuntimeError("No CHART entity found")


def build_dataset(rows):
    if not rows or len(rows) < 2:
        raise RuntimeError("Empty dataset")
    header = rows[0]
    body = rows[1:]
    labels = [str(r[0]) for r in body]
    series = []
    for col_idx in range(1, len(header)):
        vals = []
        for r in body:
            if col_idx < len(r):
                txt = str(r[col_idx]).replace(",", "").strip()
                try:
                    vals.append(float(txt))
                except Exception:
                    vals.append(None)
            else:
                vals.append(None)
        series.append({"name": str(header[col_idx]), "values": vals})
    return {"labels": labels, "series": series}


def save_dataset_to_db(dataset, db_path=DB_PATH):
    """Save one crawl result to SQLite and return (run_id, saved_row_count)."""
    print("[4] Saving dataset to SQLite...")

    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    extracted_at = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                extracted_at TEXT NOT NULL,
                source_url   TEXT NOT NULL,
                infogram_id  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS airfreight_rates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id     INTEGER NOT NULL,
                label      TEXT NOT NULL,
                route      TEXT NOT NULL,
                value      REAL,
                unit       TEXT NOT NULL DEFAULT 'USD/kg',
                sort_order INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE CASCADE,
                UNIQUE (run_id, label, route)
            );

            CREATE INDEX IF NOT EXISTS idx_rates_route_label
                ON airfreight_rates(route, label);
            CREATE INDEX IF NOT EXISTS idx_rates_run_id
                ON airfreight_rates(run_id);
            """
        )

        with conn:
            cur = conn.execute(
                """
                INSERT INTO crawl_runs (extracted_at, source_url, infogram_id)
                VALUES (?, ?, ?)
                """,
                (extracted_at, PARENT_URL, INFOGRAM_ID),
            )
            run_id = cur.lastrowid

            records = []
            labels = dataset.get("labels", [])
            for series in dataset.get("series", []):
                route = series.get("name", "")
                values = series.get("values", [])
                for idx, label in enumerate(labels):
                    value = values[idx] if idx < len(values) else None
                    records.append((run_id, str(label), str(route), value, "USD/kg", idx))

            conn.executemany(
                """
                INSERT INTO airfreight_rates
                    (run_id, label, route, value, unit, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                records,
            )

        print("    DB: " + os.path.abspath(db_path))
        print("    run_id=" + str(run_id) + ", rows=" + str(len(records)))
        return run_id, len(records)
    finally:
        conn.close()


ROUTE_KO = {
    "Frankfurt-North America": "프랑크푸르트-북미",
    "Hong Kong-Europe": "홍콩-유럽",
    "Hong Kong-North America": "홍콩-북미",
}


def build_html_page(dataset):
    print("[5] Building dashboard...")
    cfg = {
        "labels": dataset["labels"],
        "series": dataset["series"],
        "routeKo": ROUTE_KO,
        "extractedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sourceUrl": PARENT_URL,
    }
    # CRITICAL: ensure_ascii=True so any unicode is \uXXXX escaped (no special chars)
    # And we embed it inside <script type="application/json"> so JS parser never touches it.
    cfg_json = json.dumps(cfg, ensure_ascii=True)
    # Also sanitize </script> just in case
    cfg_json_safe = cfg_json.replace("</", "<\\/")

    extracted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build HTML using string concatenation (no f-string with braces issues)
    html_parts = []
    html_parts.append('<!DOCTYPE html>\n')
    html_parts.append('<html lang="ko">\n')
    html_parts.append('<head>\n')
    html_parts.append('<meta charset="UTF-8">\n')
    html_parts.append('<title>TAC Index - Baltic Exchange Airfreight Index (BAI)</title>\n')
    html_parts.append('<style>\n')
    html_parts.append(CSS)
    html_parts.append('</style>\n')
    html_parts.append('</head>\n')
    html_parts.append('<body>\n')

    html_parts.append('<div class="header">\n')
    html_parts.append('<h1 id="page-title">TAC Index - Baltic Exchange Airfreight Index (BAI)</h1>\n')
    html_parts.append('<div class="lang-toggle">\n')
    html_parts.append('<button class="lang-btn active" data-lang="ko">한국어</button>\n')
    html_parts.append('<button class="lang-btn" data-lang="en">English</button>\n')
    html_parts.append('</div>\n')
    html_parts.append('</div>\n')

    html_parts.append('<p class="meta">\n')
    html_parts.append('<span id="meta-source">출처</span>: ')
    html_parts.append('<a href="' + PARENT_URL + '" target="_blank">Air Cargo News</a> | ')
    html_parts.append('<span id="meta-extracted">추출 시각</span>: ' + extracted_at + ' | ')
    html_parts.append('<span id="meta-unit">단위: USD/kg</span>\n')
    html_parts.append('</p>\n')

    html_parts.append('<div id="lib-warning" class="lib-warning"></div>\n')

    html_parts.append('<div class="controls">\n')
    html_parts.append('<div class="control-row">\n')
    html_parts.append('<label class="control-label" id="label-region">권역 선택</label>\n')
    html_parts.append('<div class="region-chips" id="region-chips"></div>\n')
    html_parts.append('</div>\n')
    html_parts.append('<div class="control-row">\n')
    html_parts.append('<label class="control-label" id="label-period">기간 선택</label>\n')
    html_parts.append('<div class="range-slider" id="rangeSlider">\n')
    html_parts.append('<div class="track"></div>\n')
    html_parts.append('<div class="fill" id="rangeFill"></div>\n')
    html_parts.append('<input type="range" id="rangeMin" min="0" max="100" value="0">\n')
    html_parts.append('<input type="range" id="rangeMax" min="0" max="100" value="100">\n')
    html_parts.append('</div>\n')
    html_parts.append('<div class="slider-labels"><strong id="slider-from">-</strong><strong id="slider-to">-</strong></div>\n')
    html_parts.append('</div>\n')
    html_parts.append('</div>\n')

    html_parts.append('<div class="chart-card">\n')
    html_parts.append('<div class="chart-toolbar">\n')
    html_parts.append('<button class="download-btn" id="btn-csv"><span>↓</span><span id="btn-csv-text">CSV 다운로드</span></button>\n')
    html_parts.append('<button class="download-btn xlsx" id="btn-xlsx"><span>↓</span><span id="btn-xlsx-text">XLSX 다운로드</span></button>\n')
    html_parts.append('</div>\n')
    html_parts.append('<div class="chart-container"><canvas id="mainChart"></canvas></div>\n')
    html_parts.append('</div>\n')

    html_parts.append('<div class="insights">\n')
    html_parts.append('<h3 id="insights-title">권역별 시사점</h3>\n')
    html_parts.append('<div id="insights-list"></div>\n')
    html_parts.append('</div>\n')

    # CRITICAL: Embed JSON in a separate script tag - this NEVER causes JS syntax errors
    html_parts.append('<script type="application/json" id="dashboard-data">\n')
    html_parts.append(cfg_json_safe)
    html_parts.append('\n</script>\n')

    html_parts.append('<script>\n')
    html_parts.append(JS_CODE)
    html_parts.append('\n</script>\n')

    html_parts.append('</body>\n')
    html_parts.append('</html>\n')

    return ''.join(html_parts)


CSS = """
* { box-sizing: border-box; }
body {
  font-family: 'Segoe UI', sans-serif;
  max-width: 1280px; margin: 30px auto; padding: 24px;
  background: #f0f3f7; color: #2c3e50;
}
.header {
  display: flex; justify-content: space-between; align-items: center;
  border-bottom: 3px solid #3498db; padding-bottom: 16px; margin-bottom: 24px;
  flex-wrap: wrap; gap: 12px;
}
h1 { margin: 0; color: #2c3e50; font-size: 24px; }
.lang-toggle {
  display: flex; gap: 4px; background: white; padding: 4px;
  border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.lang-btn {
  padding: 6px 14px; border: none; background: transparent;
  cursor: pointer; border-radius: 6px; font-weight: 600; color: #7f8c8d;
}
.lang-btn.active { background: #3498db; color: white; }
.meta { color: #7f8c8d; font-size: 12px; margin-bottom: 20px; }
.meta a { color: #3498db; text-decoration: none; }
.controls {
  background: white; padding: 24px; border-radius: 12px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06); margin-bottom: 24px;
}
.control-row { margin-bottom: 20px; }
.control-row:last-child { margin-bottom: 0; }
.control-label {
  display: block; font-weight: 600; margin-bottom: 10px;
  color: #34495e; font-size: 14px;
}
.region-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  padding: 8px 16px; border: 2px solid #bdc3c7; background: white;
  border-radius: 24px; cursor: pointer; font-size: 13px;
  user-select: none; font-weight: 500;
}
.chip:hover { border-color: #3498db; }
.chip.active { background: #3498db; color: white; border-color: #3498db; }

.range-slider {
  position: relative; height: 44px; padding: 0 12px;
}
.range-slider .track {
  position: absolute; left: 12px; right: 12px; top: 50%;
  height: 6px; background: #ecf0f1; border-radius: 3px; transform: translateY(-50%);
}
.range-slider .fill {
  position: absolute; height: 6px; background: #3498db; border-radius: 3px;
  top: 50%; transform: translateY(-50%);
}
.range-slider input[type=range] {
  position: absolute; left: 0; right: 0; top: 0;
  width: 100%; height: 44px; background: transparent;
  -webkit-appearance: none; appearance: none; pointer-events: none;
  margin: 0;
}
.range-slider input[type=range]::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none;
  width: 22px; height: 22px; border-radius: 50%;
  background: white; border: 2px solid #3498db;
  box-shadow: 0 2px 6px rgba(0,0,0,0.2);
  cursor: pointer; pointer-events: auto;
}
.range-slider input[type=range]::-moz-range-thumb {
  width: 22px; height: 22px; border-radius: 50%;
  background: white; border: 2px solid #3498db;
  box-shadow: 0 2px 6px rgba(0,0,0,0.2);
  cursor: pointer; pointer-events: auto;
}
.slider-labels {
  display: flex; justify-content: space-between;
  margin-top: 12px; font-size: 13px; color: #7f8c8d; padding: 0 12px;
}
.slider-labels strong { color: #2c3e50; font-size: 14px; }

.chart-card {
  background: white; padding: 28px; border-radius: 12px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06); margin-bottom: 24px;
}
.chart-toolbar {
  display: flex; justify-content: flex-end; gap: 8px; margin-bottom: 12px;
}
.download-btn {
  padding: 8px 16px; border: 1px solid #3498db; background: white;
  color: #3498db; border-radius: 6px; cursor: pointer; font-size: 13px;
  font-weight: 600; display: inline-flex; align-items: center; gap: 6px;
}
.download-btn:hover { background: #3498db; color: white; }
.download-btn.xlsx { border-color: #27ae60; color: #27ae60; }
.download-btn.xlsx:hover { background: #27ae60; color: white; }
.download-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.chart-container {
  position: relative; width: 100%; height: 480px;
}

.insights {
  background: white; padding: 24px; border-radius: 12px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06); margin-bottom: 24px;
}
.insights h3 { margin-top: 0; color: #2c3e50; }
.insight-card {
  border-left: 4px solid #3498db;
  background: #f8f9fb;
  border-radius: 0 10px 10px 0;
  padding: 18px 20px;
  margin-bottom: 14px;
}
.insight-card:last-child { margin-bottom: 0; }
.insight-route {
  font-weight: 700; font-size: 16px; color: #2c3e50;
  margin-bottom: 12px; padding-bottom: 8px;
  border-bottom: 1px solid #e0e6ed;
}
.insight-grid {
  display: grid; grid-template-columns: 1fr;
  gap: 6px; font-size: 14px; line-height: 1.55;
}
.insight-line { display: flex; gap: 8px; }
.insight-line .ico { flex-shrink: 0; width: 22px; }
.insight-line .number { color: #e74c3c; font-weight: 700; }
.insight-line .up { color: #e74c3c; font-weight: 700; }
.insight-line .down { color: #2980b9; font-weight: 700; }

.lib-warning {
  background: #fff3cd; border: 1px solid #ffc107; color: #856404;
  padding: 14px 18px; border-radius: 8px; margin-bottom: 20px;
  font-size: 14px; display: none;
}
.lib-warning.show { display: block; }

@media (max-width: 700px) {
  h1 { font-size: 18px; }
  .chart-container { height: 360px; }
}
"""


JS_CODE = r"""
// ====== Load CFG from embedded JSON tag (NEVER causes syntax errors) ======
var CFG;
try {
  CFG = JSON.parse(document.getElementById('dashboard-data').textContent);
} catch (err) {
  document.body.innerHTML = '<h1 style="color:red">Data load failed: ' + err.message + '</h1>';
  throw err;
}

// ====== Multi-CDN fallback ======
var CDN_LIST = {
  chart: [
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
    "https://unpkg.com/chart.js@4.4.1/dist/chart.umd.js",
    "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
  ],
  xlsx: [
    "https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js",
    "https://unpkg.com/xlsx@0.18.5/dist/xlsx.full.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"
  ]
};

function loadScript(urls, checkGlobal, onDone) {
  var i = 0;
  function tryNext() {
    if (i >= urls.length) { onDone(false); return; }
    var url = urls[i++];
    var s = document.createElement('script');
    s.src = url;
    s.async = false;
    s.onload = function() {
      if (window[checkGlobal]) onDone(true);
      else tryNext();
    };
    s.onerror = function() { tryNext(); };
    document.head.appendChild(s);
  }
  tryNext();
}

var LIBS = { chart: false, xlsx: false };

function showWarning(msg) {
  var w = document.getElementById('lib-warning');
  w.textContent = msg;
  w.classList.add('show');
}

// ====== App ======
var I18N = {
  ko: {
    title: "TAC Index - Baltic Exchange Airfreight Index (BAI)",
    source: "출처", extracted: "추출 시각", unit: "단위: USD/kg",
    region: "권역 선택", period: "기간 선택",
    insightsTitle: "권역별 시사점",
    csv: "CSV 다운로드", xlsx: "XLSX 다운로드",
    routeName: function(en) { return CFG.routeKo[en] || en; },
    dateCol: "날짜",
    labels: { period: "기간", change: "변화", avg: "구간 평균",
              maxmin: "최고/최저", range: "변동폭" }
  },
  en: {
    title: "TAC Index - Baltic Exchange Airfreight Index (BAI)",
    source: "Source", extracted: "Extracted", unit: "Unit: USD/kg",
    region: "Select Region", period: "Select Period",
    insightsTitle: "Regional Insights",
    csv: "Download CSV", xlsx: "Download XLSX",
    routeName: function(en) { return en; },
    dateCol: "Date",
    labels: { period: "Period", change: "Change", avg: "Average",
              maxmin: "High / Low", range: "Range" }
  }
};

var lang = 'ko';
var selectedRegions = {};
CFG.series.forEach(function(s) { selectedRegions[s.name] = true; });
var N = CFG.labels.length;
var rangeIdx = [0, N - 1];
var chart = null;

function isSelected(name) { return selectedRegions[name] === true; }
function selectedCount() {
  var c = 0;
  for (var k in selectedRegions) if (selectedRegions[k]) c++;
  return c;
}

// ====== Slider ======
function setupSlider() {
  var minInput = document.getElementById('rangeMin');
  var maxInput = document.getElementById('rangeMax');
  var fillEl = document.getElementById('rangeFill');
  minInput.min = 0; minInput.max = N - 1; minInput.value = 0;
  maxInput.min = 0; maxInput.max = N - 1; maxInput.value = N - 1;

  function updateFill() {
    var a = parseInt(minInput.value, 10);
    var b = parseInt(maxInput.value, 10);
    var lo = Math.min(a, b), hi = Math.max(a, b);
    var total = N - 1;
    var leftPct = total > 0 ? (lo / total) * 100 : 0;
    var rightPct = total > 0 ? (hi / total) * 100 : 100;
    fillEl.style.left = leftPct + '%';
    fillEl.style.width = (rightPct - leftPct) + '%';
    rangeIdx = [lo, hi];
    document.getElementById('slider-from').textContent = CFG.labels[lo];
    document.getElementById('slider-to').textContent = CFG.labels[hi];
  }

  function onChange(e) {
    var a = parseInt(minInput.value, 10);
    var b = parseInt(maxInput.value, 10);
    if (a > b) {
      if (e.target === minInput) minInput.value = b;
      else maxInput.value = a;
    }
    updateFill();
    render();
  }
  minInput.addEventListener('input', onChange);
  maxInput.addEventListener('input', onChange);
  updateFill();
}

// ====== Chips ======
function buildChips() {
  var wrap = document.getElementById('region-chips');
  wrap.innerHTML = '';
  CFG.series.forEach(function(s) {
    var chip = document.createElement('div');
    chip.className = 'chip' + (isSelected(s.name) ? ' active' : '');
    chip.textContent = I18N[lang].routeName(s.name);
    chip.onclick = function() {
      if (isSelected(s.name)) {
        if (selectedCount() > 1) selectedRegions[s.name] = false;
      } else {
        selectedRegions[s.name] = true;
      }
      buildChips();
      render();
    };
    wrap.appendChild(chip);
  });
}

// ====== Chart ======
var palette = ['#3498db','#e74c3c','#2ecc71','#f39c12','#9b59b6','#1abc9c'];

function render() {
  if (!LIBS.chart || typeof Chart === 'undefined') {
    buildInsights();
    return;
  }
  var s = rangeIdx[0], e = rangeIdx[1];
  var labels = CFG.labels.slice(s, e + 1);
  var datasets = [];
  var colorIdx = 0;
  CFG.series.forEach(function(ser) {
    if (!isSelected(ser.name)) return;
    var color = palette[colorIdx++ % palette.length];
    datasets.push({
      label: I18N[lang].routeName(ser.name),
      data: ser.values.slice(s, e + 1),
      borderColor: color,
      backgroundColor: color + '22',
      tension: 0.25,
      pointRadius: 2,
      pointHoverRadius: 6,
      borderWidth: 2.5,
      spanGaps: true
    });
  });

  if (chart) {
    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.update();
  } else {
    var ctx = document.getElementById('mainChart').getContext('2d');
    chart = new Chart(ctx, {
      type: 'line',
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { position: 'top' },
          tooltip: {
            callbacks: {
              label: function(c) {
                return c.dataset.label + ': ' +
                  (c.parsed.y != null ? c.parsed.y.toFixed(2) : 'N/A') + ' USD/kg';
              }
            }
          }
        },
        scales: {
          x: { ticks: { maxTicksLimit: 18 } },
          y: { title: { display: true, text: 'USD / kg' }, beginAtZero: false }
        }
      }
    });
  }
  buildInsights();
}

// ====== Insights ======
function buildInsights() {
  var s = rangeIdx[0], e = rangeIdx[1];
  var list = document.getElementById('insights-list');
  list.innerHTML = '';
  var t = I18N[lang];

  CFG.series.forEach(function(ser) {
    if (!isSelected(ser.name)) return;
    var slice = [];
    for (var i = s; i <= e; i++) {
      if (ser.values[i] != null) slice.push(ser.values[i]);
    }
    if (slice.length < 2) return;

    var first = slice[0];
    var last = slice[slice.length - 1];
    var max = Math.max.apply(null, slice);
    var min = Math.min.apply(null, slice);
    var sum = 0;
    for (var j = 0; j < slice.length; j++) sum += slice[j];
    var avg = sum / slice.length;
    var change = ((last - first) / first) * 100;
    var fromLbl = CFG.labels[s];
    var toLbl = CFG.labels[e];
    var routeDisplay = t.routeName(ser.name);
    var trendCls = change >= 0 ? 'up' : 'down';
    var changeStr = (change >= 0 ? '+' : '') + change.toFixed(1) + '%';
    var months = slice.length;

    var lines;
    if (lang === 'ko') {
      var trend = change >= 0 ? '상승' : '하락';
      lines = [
        { ico: '[1]', txt: '<b>' + t.labels.period + '</b>: ' + fromLbl + ' ~ ' + toLbl +
          ' (총 <span class="number">' + months + '</span>개월)임.' },
        { ico: '[2]', txt: '<b>' + t.labels.change + '</b>: <span class="number">$' + first.toFixed(2) +
          '</span> -> <span class="number">$' + last.toFixed(2) + '</span> ' +
          '<span class="' + trendCls + '">' + trend + ' (' + changeStr + ')</span>임.' },
        { ico: '[3]', txt: '<b>' + t.labels.avg + '</b>: <span class="number">$' + avg.toFixed(2) + '</span>임.' },
        { ico: '[4]', txt: '<b>' + t.labels.maxmin + '</b>: 최고 <span class="number">$' + max.toFixed(2) +
          '</span> / 최저 <span class="number">$' + min.toFixed(2) + '</span>임.' },
        { ico: '[5]', txt: '<b>' + t.labels.range + '</b>: <span class="number">$' +
          (max - min).toFixed(2) + '</span> 수준임.' }
      ];
    } else {
      var trendEn = change >= 0 ? 'increase' : 'decrease';
      lines = [
        { ico: '[1]', txt: '<b>' + t.labels.period + '</b>: ' + fromLbl + ' ~ ' + toLbl +
          ' (<span class="number">' + months + '</span> months total).' },
        { ico: '[2]', txt: '<b>' + t.labels.change + '</b>: <span class="number">$' + first.toFixed(2) +
          '</span> -> <span class="number">$' + last.toFixed(2) + '</span> ' +
          '<span class="' + trendCls + '">' + trendEn + ' (' + changeStr + ')</span>.' },
        { ico: '[3]', txt: '<b>' + t.labels.avg + '</b>: <span class="number">$' + avg.toFixed(2) + '</span>.' },
        { ico: '[4]', txt: '<b>' + t.labels.maxmin + '</b>: high <span class="number">$' + max.toFixed(2) +
          '</span> / low <span class="number">$' + min.toFixed(2) + '</span>.' },
        { ico: '[5]', txt: '<b>' + t.labels.range + '</b>: <span class="number">$' +
          (max - min).toFixed(2) + '</span>.' }
      ];
    }

    var card = document.createElement('div');
    card.className = 'insight-card';
    var html = '<div class="insight-route">' + routeDisplay + '</div><div class="insight-grid">';
    lines.forEach(function(ln) {
      html += '<div class="insight-line"><div class="ico">' + ln.ico + '</div><div>' + ln.txt + '</div></div>';
    });
    html += '</div>';
    card.innerHTML = html;
    list.appendChild(card);
  });
}

// ====== Download ======
function getTable() {
  var s = rangeIdx[0], e = rangeIdx[1];
  var t = I18N[lang];
  var header = [t.dateCol];
  var active = CFG.series.filter(function(ser){ return isSelected(ser.name); });
  active.forEach(function(ser){ header.push(t.routeName(ser.name)); });
  var body = [];
  for (var i = s; i <= e; i++) {
    var row = [CFG.labels[i]];
    active.forEach(function(ser) {
      var v = ser.values[i];
      row.push(v == null ? '' : v);
    });
    body.push(row);
  }
  return { header: header, body: body };
}

function fileBaseName() {
  var f = CFG.labels[rangeIdx[0]].replace(/[^A-Za-z0-9_\-]/g, '');
  var t = CFG.labels[rangeIdx[1]].replace(/[^A-Za-z0-9_\-]/g, '');
  return 'baltic_airfreight_' + f + '_to_' + t;
}

function triggerDownload(blob, filename) {
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(function() {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 100);
}

function downloadCSV() {
  var tbl = getTable();
  var lines = [];
  lines.push(tbl.header.join(','));
  tbl.body.forEach(function(row) {
    lines.push(row.map(function(v) {
      var s = String(v);
      if (s.indexOf(',') >= 0 || s.indexOf('"') >= 0 || s.indexOf('\n') >= 0) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }).join(','));
  });
  var csv = '\ufeff' + lines.join('\r\n');
  var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  triggerDownload(blob, fileBaseName() + '.csv');
}

function downloadXLSX() {
  if (typeof XLSX === 'undefined') {
    alert('XLSX library not available. Use CSV instead.');
    return;
  }
  var tbl = getTable();
  var aoa = [tbl.header].concat(tbl.body);
  var ws = XLSX.utils.aoa_to_sheet(aoa);
  var wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Baltic Index');
  XLSX.writeFile(wb, fileBaseName() + '.xlsx');
}

// ====== Language ======
function applyLang() {
  var t = I18N[lang];
  document.getElementById('page-title').textContent = t.title;
  document.getElementById('meta-source').textContent = t.source;
  document.getElementById('meta-extracted').textContent = t.extracted;
  document.getElementById('meta-unit').textContent = t.unit;
  document.getElementById('label-region').textContent = t.region;
  document.getElementById('label-period').textContent = t.period;
  document.getElementById('insights-title').textContent = t.insightsTitle;
  document.getElementById('btn-csv-text').textContent = t.csv;
  document.getElementById('btn-xlsx-text').textContent = t.xlsx;
  document.documentElement.lang = lang;
  buildChips();
  render();
}

// ====== Init ======
function initApp() {
  setupSlider();

  var btns = document.querySelectorAll('.lang-btn');
  for (var i = 0; i < btns.length; i++) {
    btns[i].onclick = (function(btn) {
      return function() {
        for (var j = 0; j < btns.length; j++) btns[j].classList.remove('active');
        btn.classList.add('active');
        lang = btn.getAttribute('data-lang');
        applyLang();
      };
    })(btns[i]);
  }

  document.getElementById('btn-csv').onclick = downloadCSV;
  document.getElementById('btn-xlsx').onclick = downloadXLSX;

  applyLang();

  window.addEventListener('resize', function() {
    if (chart) chart.resize();
  });
}

// ====== Bootstrap ======
loadScript(CDN_LIST.chart, 'Chart', function(ok) {
  LIBS.chart = ok;
  if (!ok) {
    showWarning('차트 라이브러리(Chart.js)를 CDN에서 불러오지 못했습니다. 인터넷 연결 또는 회사 방화벽을 확인해 주세요. (시사점은 표시됩니다)');
  }
  loadScript(CDN_LIST.xlsx, 'XLSX', function(ok2) {
    LIBS.xlsx = ok2;
    if (!ok2) {
      document.getElementById('btn-xlsx').disabled = true;
    }
    try {
      initApp();
    } catch (err) {
      showWarning('초기화 오류: ' + err.message);
      console.error(err);
    }
  });
});
"""


class QuietHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Serve the generated dashboard through the fixed /TAC_Index route."""

    def __init__(self, *args, directory=None, dashboard_filename=None, **kwargs):
        self.dashboard_filename = dashboard_filename
        super().__init__(*args, directory=directory, **kwargs)

    def _map_dashboard_route(self):
        request_path = self.path.split("?", 1)[0].split("#", 1)[0]
        if request_path in (DASHBOARD_ROUTE, DASHBOARD_ROUTE + "/"):
            self.path = "/" + self.dashboard_filename

    def do_GET(self):
        self._map_dashboard_route()
        super().do_GET()

    def do_HEAD(self):
        self._map_dashboard_route()
        super().do_HEAD()

    def log_message(self, format, *args):
        pass


def serve_dashboard(directory, filename):
    """Serve the dashboard at http://127.0.0.1:9001/TAC_Index."""
    handler = partial(
        QuietHTTPRequestHandler,
        directory=directory,
        dashboard_filename=filename,
    )
    server = ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), handler)
    url = "http://" + SERVER_HOST + ":" + str(SERVER_PORT) + DASHBOARD_ROUTE

    print("[6] Starting dashboard server...")
    print("    " + url)
    webbrowser.open(url)
    print("    Press Ctrl+C to stop the server.")
    print("")
    print("DONE!")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


def main():
    try:
        html = fetch_html()
        data = extract_data(html)
        rows = parse_chart(data)
        dataset = build_dataset(rows)
        save_dataset_to_db(dataset)
        page_html = build_html_page(dataset)

        tmp_dir = tempfile.gettempdir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(tmp_dir, "baltic_dashboard_" + ts + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page_html)

        serve_dashboard(tmp_dir, os.path.basename(path))
    except requests.exceptions.RequestException as e:
        print("\nNETWORK ERROR: " + str(e))
        input("Press Enter to exit...")
    except Exception as e:
        print("\nERROR: " + str(e))
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")


if __name__ == "__main__":
    main()

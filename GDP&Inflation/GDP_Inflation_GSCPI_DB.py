# -*- coding: utf-8 -*-
import sys
import subprocess
import os
import sqlite3
import json
from io import StringIO

# 1. 프로그램 시작 시 필수 라이브러리 검사 및 자동 설치 기능 (requests 패키지 등 미설치 문제 해결)
REQUIRED_PACKAGES = {
    'requests': 'requests',
    'flask': 'flask',
    'pandas': 'pandas',
    'plotly': 'plotly',
    'xlrd': 'xlrd',
    'openpyxl': 'openpyxl'
}

print("[시스템] 필수 라이브러리 검사 중...")
for module_name, pip_name in REQUIRED_PACKAGES.items():
    try:
        __import__(module_name)
    except ImportError:
        print(f"[알림] '{pip_name}' 라이브러리가 없습니다. 자동으로 설치를 시작합니다...")
        try:
            # 외부 환경 제어 에러 우회를 위해 --break-system-packages 옵션 포함
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--break-system-packages"])
            print(f"[완료] '{pip_name}' 설치 성공!")
        except Exception as e:
            print(f"[오류] '{pip_name}' 설치 중 문제가 발생했습니다: {e}")
            sys.exit(1)

# 라이브러리가 모두 확인된 후 나머지 모듈 로드
import warnings
warnings.filterwarnings("ignore")

from flask import Flask, render_template_string, request, Response
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from io import BytesIO
import urllib.parse
import socket
import threading
import time
import webbrowser
from datetime import datetime

app = Flask(__name__)

# SQLite cache configuration
# The DB is created automatically next to this Python file on the first run.
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'gdp_inflation_gscpi_cache.db'
)
DB_PATH = os.environ.get('ECONOMIC_DATA_DB_PATH', DEFAULT_DB_PATH)
try:
    CACHE_TTL_HOURS = float(os.environ.get('CACHE_TTL_HOURS', '24'))
except ValueError:
    CACHE_TTL_HOURS = 24.0


def init_cache_db():
    """Create the SQLite cache DB/table if they do not exist."""
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataframe_cache (
                cache_key  TEXT PRIMARY KEY,
                cached_at  REAL NOT NULL,
                row_count  INTEGER NOT NULL,
                payload    TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_dataframe_cache(cache_key, df):
    """Serialize a DataFrame into SQLite using pandas table JSON."""
    payload = df.to_json(orient='table', date_format='iso', force_ascii=False)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute(
            """
            INSERT INTO dataframe_cache (cache_key, cached_at, row_count, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                cached_at = excluded.cached_at,
                row_count = excluded.row_count,
                payload = excluded.payload
            """,
            (cache_key, time.time(), int(len(df)), payload),
        )
        conn.commit()


def load_dataframe_cache(cache_key, allow_expired=False):
    """Load a cached DataFrame; return None when missing or expired."""
    init_cache_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute('PRAGMA busy_timeout=30000')
        row = conn.execute(
            'SELECT cached_at, payload FROM dataframe_cache WHERE cache_key = ?',
            (cache_key,),
        ).fetchone()

    if row is None:
        return None

    cached_at, payload = row
    age_hours = max(0.0, (time.time() - float(cached_at)) / 3600.0)
    if not allow_expired and CACHE_TTL_HOURS >= 0 and age_hours > CACHE_TTL_HOURS:
        return None

    try:
        df = pd.read_json(StringIO(payload), orient='table')
        df.attrs['cache_age_hours'] = age_hours
        return df
    except Exception as exc:
        print(f'[캐시 경고] {cache_key} 읽기 실패: {exc}')
        return None


def get_cached_or_fetch(cache_key, fetch_function):
    """Use fresh SQLite data first; fetch only when cache is absent/expired."""
    cached = load_dataframe_cache(cache_key)
    if cached is not None:
        print(f'[DB 캐시] {cache_key}: {len(cached):,}행 로드 '
              f'({cached.attrs.get("cache_age_hours", 0):.1f}시간 경과)')
        return cached

    print(f'[네트워크] {cache_key}: 캐시 없음 또는 만료, 데이터 조회 중...')
    try:
        df = fetch_function()
        save_dataframe_cache(cache_key, df)
        print(f'[DB 저장] {cache_key}: {len(df):,}행 저장 완료')
        return df
    except Exception as exc:
        # If refresh fails, an expired cache is still more useful than no data.
        stale = load_dataframe_cache(cache_key, allow_expired=True)
        if stale is not None:
            print(f'[캐시 대체] 네트워크 오류로 기존 DB 데이터를 사용합니다: {exc}')
            return stale
        raise


WB_GDP_INDICATOR = "NY.GDP.MKTP.KD.ZG"
WB_INFLATION_INDICATOR = "FP.CPI.TOTL.ZG"
WB_GDP_API_URL = f"https://api.worldbank.org/v2/country/all/indicator/{WB_GDP_INDICATOR}?format=json&per_page=25000"
WB_INFLATION_API_URL = f"https://api.worldbank.org/v2/country/all/indicator/{WB_INFLATION_INDICATOR}?format=json&per_page=25000"
COUNTRY_META_URL = "https://api.worldbank.org/v2/country?format=json&per_page=400"
GSCPI_XLS_URL = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xls"
# Initial selection: G7 countries plus South Korea
DEFAULT_COUNTRIES = [
    'Canada',
    'France',
    'Germany',
    'Italy',
    'Japan',
    'United Kingdom',
    'United States',
    'Korea, Rep.',
]

def safe_get_json(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def get_country_meta():
    def fetch():
        data = safe_get_json(COUNTRY_META_URL)
        rows = data[1] if isinstance(data, list) and len(data) > 1 else []
        out = []
        for x in rows:
            region = (x.get('region') or {}).get('value') if isinstance(x.get('region'), dict) else None
            name = x.get('name')
            code = x.get('id')
            if code and name and region and region != 'Aggregates':
                out.append({'code': code, 'country': name, 'region': region})
        return (pd.DataFrame(out)
                .drop_duplicates(subset=['code'])
                .sort_values(['region', 'country']))

    return get_cached_or_fetch('worldbank_country_meta', fetch)


def get_worldbank_indicator(url, value_name):
    def fetch():
        data = safe_get_json(url)
        rows = data[1] if isinstance(data, list) and len(data) > 1 else []
        out = []
        for x in rows:
            country = (x.get('country') or {}).get('value') if isinstance(x.get('country'), dict) else None
            code = (x.get('countryiso3code') or '').strip()
            year = x.get('date')
            value = x.get('value')
            if country and code and year and value is not None:
                try:
                    out.append({
                        'code': code,
                        'country': country,
                        'year': int(year),
                        value_name: float(value),
                    })
                except Exception:
                    pass
        return pd.DataFrame(out)

    if 'NY.GDP.MKTP.KD.ZG' in url:
        source_key = 'worldbank_gdp'
    elif 'FP.CPI.TOTL.ZG' in url:
        source_key = 'worldbank_inflation'
    else:
        source_key = 'worldbank_' + value_name
    return get_cached_or_fetch(source_key + '_' + value_name, fetch)


def get_gscpi_data():
    def fetch():
        resp = requests.get(GSCPI_XLS_URL, timeout=60)
        resp.raise_for_status()
        content = resp.content
        xls = pd.ExcelFile(BytesIO(content), engine='xlrd')
        sheet = None
        for s in xls.sheet_names:
            s_norm = str(s).strip().lower()
            if 'monthly' in s_norm and 'data' in s_norm:
                sheet = s
                break
        if sheet is None:
            sheet = xls.sheet_names[-1]
        df = pd.read_excel(BytesIO(content), sheet_name=sheet, engine='xlrd')
        cols = {str(c).strip().lower(): c for c in df.columns}
        date_col = cols.get('date', df.columns[0])
        value_col = cols.get('gscpi', df.columns[1])
        out = df[[date_col, value_col]].copy()
        out.columns = ['date', 'gscpi']
        out['date'] = pd.to_datetime(out['date'], errors='coerce')
        out['gscpi'] = pd.to_numeric(out['gscpi'], errors='coerce')
        out['year'] = out['date'].dt.year
        return out.dropna(subset=['date', 'gscpi']).sort_values('date')

    out = get_cached_or_fetch('newyorkfed_gscpi', fetch)
    # Keep datetime behavior identical after a DB round trip.
    if not out.empty:
        out['date'] = pd.to_datetime(out['date'], errors='coerce')
        out['year'] = pd.to_numeric(out['year'], errors='coerce').astype('Int64')
    return out


def build_line(df, x, y, color=None, title=''):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'
    fig = px.line(df, x=x, y=y, color=color, markers=True, title=(title if title else None))
    fig.update_layout(template='plotly_white', height=460, margin=dict(l=20, r=20, t=(60 if title else 20), b=20), legend_title_text='')
    return fig.to_html(full_html=False, include_plotlyjs='cdn')


def build_bar(df, x, y, title=''):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'
    fig = px.bar(df, x=x, y=y, text_auto='.2f', title=(title if title else None), color=y, color_continuous_scale='Blues')
    fig.update_layout(template='plotly_white', height=420, margin=dict(l=20, r=20, t=(60 if title else 20), b=20), coloraxis_showscale=False)
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_macro_compare_chart(gdp_df, inf_df):
    if gdp_df.empty and inf_df.empty:
        return '<div class="empty">데이터 없음</div>'
    fig = go.Figure()
    if not gdp_df.empty:
        g = gdp_df.groupby('year', as_index=False)['value'].mean()
        fig.add_trace(go.Scatter(x=g['year'], y=g['value'], mode='lines+markers', name='GDP 평균', line=dict(color='#1d4ed8', width=3)))
    if not inf_df.empty:
        i = inf_df.groupby('year', as_index=False)['value'].mean()
        fig.add_trace(go.Scatter(x=i['year'], y=i['value'], mode='lines+markers', name='Inflation 평균', yaxis='y2', line=dict(color='#b91c1c', width=3)))
    fig.update_layout(template='plotly_white', height=460, title='선택 국가 기준 GDP / Inflation 평균 비교', margin=dict(l=20, r=20, t=60, b=20), legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1), yaxis=dict(title='GDP 성장률'), yaxis2=dict(title='Inflation', overlaying='y', side='right'))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_exec_insights_macro(df, metric_name):
    if df.empty:
        return ['관련 데이터 부재: 정량적 시사점 도출이 제한적']
    latest = df.sort_values(['country', 'year']).groupby('country', as_index=False).tail(1)
    avg_val = latest['value'].mean()
    max_row = latest.loc[latest['value'].idxmax()]
    min_row = latest.loc[latest['value'].idxmin()]
    spread = latest['value'].max() - latest['value'].min()
    if df['year'].nunique() >= 2:
        yearly_avg = df.groupby('year', as_index=False)['value'].mean().sort_values('year')
        latest_avg = yearly_avg['value'].iloc[-1]
        prev_avg = yearly_avg['value'].iloc[-2]
        diff = latest_avg - prev_avg
        if diff > 0:
            trend_text = f'전년 대비 평균 {metric_name}: {diff:.2f}p 상승'
        elif diff < 0:
            trend_text = f'전년 대비 평균 {metric_name}: {abs(diff):.2f}p 하락'
        else:
            trend_text = f'전년 대비 평균 {metric_name}: 유사한 수준 유지'
    else:
        trend_text = f'{metric_name} 추세 판단: 비교 가능한 연도 수 부족'
    if avg_val >= 0:
        level_text = f'최신 평균 {metric_name}: {avg_val:.2f} (플러스 영역)'
    else:
        level_text = f'최신 평균 {metric_name}: {avg_val:.2f} (마이너스 영역)'
    return [
        level_text,
        f'최고 국가: {max_row["country"]} ({max_row["value"]:.2f})',
        f'최저 국가: {min_row["country"]} ({min_row["value"]:.2f})',
        f'국가 간 격차: {spread:.2f}p',
        trend_text,
    ]


def build_exec_insights_gscpi(df):
    if df.empty:
        return ['관련 데이터 부재: 공급망 시사점 도출 제한']
    latest = df.sort_values('date').iloc[-1]
    last12 = df.sort_values('date').tail(12)
    avg12 = last12['gscpi'].mean()
    max12 = last12['gscpi'].max()
    min12 = last12['gscpi'].min()
    amp12 = max12 - min12
    if len(df) >= 13:
        prev = df.sort_values('date').iloc[-13]['gscpi']
        diff = latest['gscpi'] - prev
        if diff > 0:
            trend = f'1년 전 대비 GSCPI: {diff:.2f}p 상승'
        elif diff < 0:
            trend = f'1년 전 대비 GSCPI: {abs(diff):.2f}p 하락'
        else:
            trend = '1년 전 대비 GSCPI: 유사한 수준 유지'
    else:
        trend = '1년 비교 판단: 데이터 부족'
    if latest['gscpi'] > avg12:
        level = '최근 수준 평가: 최근 12개월 평균 상회'
    elif latest['gscpi'] < avg12:
        level = '최근 수준 평가: 최근 12개월 평균 하회'
    else:
        level = '최근 수준 평가: 최근 12개월 평균과 유사'
    return [
        f'최신 시점: {latest["date"].strftime("%Y-%m")}',
        f'최신 GSCPI: {latest["gscpi"]:.2f}',
        f'최근 12개월 평균: {avg12:.2f}',
        f'최근 12개월 범위: {min12:.2f} ~ {max12:.2f}',
        f'최근 12개월 변동폭: {amp12:.2f}p',
        level,
        trend,
    ]


# 2. 누락되었던 데이터 다운로드 처리용 헬퍼 함수 구현
def prepare_download_df(dataset, region, years, countries, meta):
    if dataset == 'gdp':
        df = get_worldbank_indicator(WB_GDP_API_URL, 'value').merge(meta, on=['code', 'country'], how='left')
    elif dataset == 'inflation':
        df = get_worldbank_indicator(WB_INFLATION_API_URL, 'value').merge(meta, on=['code', 'country'], how='left')
    elif dataset == 'gscpi':
        df = get_gscpi_data()
        if not df.empty:
            df = df[df['year'] >= df['year'].max() - years + 1]
        return df
    else: # macro_compare
        gdp = get_worldbank_indicator(WB_GDP_API_URL, 'gdp_growth').merge(meta, on=['code', 'country'], how='left')
        inf = get_worldbank_indicator(WB_INFLATION_API_URL, 'inflation').merge(meta, on=['code', 'country'], how='left')
        if region != '전체':
            gdp = gdp[gdp['region'] == region]
            inf = inf[inf['region'] == region]
        if countries:
            gdp = gdp[gdp['country'].isin(countries)]
            inf = inf[inf['country'].isin(countries)]
        res = pd.merge(gdp, inf, on=['code', 'country', 'year', 'region'], how='outer')
        if not res.empty:
            res = res[res['year'] >= res['year'].max() - years + 1]
        return res.sort_values(['country', 'year'])

    if region != '전체':
        df = df[df['region'] == region]
    if countries:
        df = df[df['country'].isin(countries)]
    if not df.empty:
        df = df[df['year'] >= df['year'].max() - years + 1]
    return df.sort_values(['country', 'year'])


def make_download_response(df, filename, kind='csv'):
    if kind == 'excel':
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
        output.seek(0)
        return Response(output.getvalue(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': f'attachment; filename={filename}'})
    csv_data = df.to_csv(index=False, encoding='utf-8-sig')
    return Response(csv_data, mimetype='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename={filename}'})


HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>GDP, Inflation & GSCPI</title><style>:root {--navy:#12355b; --blue:#1d4ed8; --teal:#0f766e; --red:#b91c1c; --bg:#f3f6fb; --card:#ffffff; --line:#d9e2ec; --text:#1f2937; --muted:#6b7280;}* { box-sizing:border-box; }body { font-family: 'Segoe UI', Arial, sans-serif; margin:0; background:var(--bg); color:var(--text); } .wrap { max-width:1520px; margin:0 auto; padding:24px; } .hero { background:linear-gradient(135deg, var(--navy), var(--teal)); color:#fff; padding:28px; border-radius:18px; margin-bottom:20px; } .hero h1 { margin:0 0 10px 0; font-size:30px; } .meta { font-size:13px; opacity:.95; line-height:1.7; } .tabs { display:flex; gap:10px; margin-bottom:18px; } .tab { display:inline-block; padding:11px 18px; border-radius:12px; text-decoration:none; background:#dbe4f0; color:#16324f; font-weight:700; } .tab.active { background:var(--navy); color:#fff; } .panel { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:18px; box-shadow:0 4px 14px rgba(18,53,91,.06); margin-bottom:18px; width:100%; } .section-title { margin:0 0 14px 0; font-size:18px; color:var(--navy); } .grid { display:grid; grid-template-columns:repeat(4, 1fr); gap:16px; } .row2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; } .kpi { background:linear-gradient(180deg, #fff, #f8fbff); border:1px solid var(--line); border-left:6px solid var(--blue); padding:16px; border-radius:14px; } .kpi .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.4px; } .kpi .value { margin-top:8px; font-size:24px; font-weight:800; color:var(--navy); } label { display:block; font-size:13px; color:var(--muted); margin-bottom:6px; font-weight:600; } select { width:100%; padding:11px; border:1px solid var(--line); border-radius:10px; background:#fff; } button { padding:10px 14px; border:none; background:var(--navy); color:#fff; border-radius:10px; cursor:pointer; text-decoration:none; display:inline-block; font-weight:600; } .select-tools { display:flex; gap:7px; margin:0 0 7px 0; flex-wrap:wrap; } .select-tools button { padding:6px 10px; font-size:12px; background:#475569; } .select-tools button:first-child { background:var(--teal); } .muted { color:var(--muted); font-size:13px; } .btn-row { display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap; } .icon-btn { display:inline-flex; align-items:center; justify-content:center; width:30px; height:30px; border-radius:8px; text-decoration:none; color:#fff; font-size:14px; font-weight:700; } .icon-csv { background:#2563eb; } .icon-xlsx { background:#0f766e; } ul.exec { margin:0; padding-left:18px; } ul.exec li { margin:10px 0; line-height:1.6; } .subnote { margin-top:6px; color:var(--muted); font-size:12px; } .empty { padding:40px; text-align:center; color:var(--muted); }</style></head><body><div class="wrap"><div class="hero"><h1>GDP, Inflation & GSCPI</h1><div class="meta"><div><b>출처</b> | GDP, Inflation: World Bank Open Data / GSCPI: Federal Reserve Bank of New York</div><div><b>조회 시각</b> | {{ fetched_at }}</div></div></div><div class="tabs"><a class="tab {% if tab == 'macro' %}active{% endif %}\" href="/?tab=macro&region={{ selected_region_enc }}&years={{ selected_years }}{% for c in selected_countries %}&countries={{ c|urlencode }}{% endfor %}">GDP + Inflation</a><a class="tab {% if tab == 'gscpi' %}active{% endif %}" href="/?tab=gscpi&years={{ selected_years }}">GSCPI</a></div>{% if tab == 'macro' %}<div class="panel"><h3 class="section-title">공통 필터</h3><form method="get" id="macro-filter-form"><input type="hidden" name=\"tab\" value="macro"><input type="hidden" name="auto_select_region" id="auto-select-region" value="0"><div class="grid"><div><label>지역 선택</label><select name="region" onchange="selectRegionCountriesAndSubmit(this.form)">{% for r in regions %}<option value="{{ r }}" {% if r == selected_region %}selected{% endif %}>{{ r }}</option>{% endfor %}</select></div><div><label>기간 선택</label><select name="years" onchange="this.form.submit()"><option value="5" {% if selected_years == 5 %}selected{% endif %}>최근 5개년</option><option value="10" {% if selected_years == 10 %}selected{% endif %}>최근 10개년</option><option value="20" {% if selected_years == 20 %}selected{% endif %}>최근 20개년</option></select></div><div style="grid-column:span 2;"><label>국가 선택</label><div class="select-tools"><button type="button" onclick="setAllCountries(true)">권역 전체 선택</button><button type="button" onclick="setAllCountries(false)">선택 해제</button></div><select name="countries" id="country-select" multiple size="8">{% for c in country_options %}<option value="{{ c }}" {% if c in selected_countries %}selected{% endif %}>{{ c }}</option>{% endfor %}</select></div></div><div style="margin-top:12px;"><button type="submit">조회</button></div><div class="subnote">최초 화면에서는 G7과 한국이 기본 선택됩니다. 권역을 변경하면 해당 권역의 모든 국가가 자동 선택되며, 버튼 또는 Ctrl/Shift로 선택을 조정할 수 있습니다.</div></form></div><div class="grid"><div class="kpi"><div class="label">Selected Countries</div><div class="value">{{ selected_countries|length }}</div></div><div class="kpi"><div class="label">GDP Latest Year</div><div class="value">{{ latest_gdp_year }}</div></div><div class="kpi"><div class="label">Inflation Latest Year</div><div class="value">{{ latest_inf_year }}</div></div><div class="kpi"><div class="label">Selected Region</div><div class="value">{{ selected_region }}</div></div></div><div class="row2"><div class="panel"><h3 class="section-title">GDP 성장률 추이</h3><div class="btn-row"><a class="icon-btn icon-csv" href="{{ gdp_csv_url }}" title="CSV 다운로드">C</a><a class="icon-btn icon-xlsx" href="{{ gdp_excel_url }}" title="Excel 다운로드">X</a></div>{{ gdp_line|safe }}</div><div class="panel"><h3 class="section-title">Inflation 추이</h3><div class="btn-row"><a class="icon-btn icon-csv" href="{{ inf_csv_url }}" title="CSV 다운로드">C</a><a class="icon-btn icon-xlsx" href="{{ inf_excel_url }}" title="Excel 다운로드">X</a></div>{{ inf_line|safe }}</div></div><div class="panel"><h3 class="section-title">GDP / Inflation 평균 비교</h3><div class="btn-row"><a class="icon-btn icon-csv" href="{{ macro_compare_csv_url }}" title="CSV 다운로드">C</a><a class="icon-btn icon-xlsx" href="{{ macro_compare_excel_url }}" title="Excel 다운로드">X</a></div>{{ macro_compare_chart|safe }}</div><div class="row2"><div class="panel"><h3 class="section-title">국가별 최신 GDP 성장률 비교</h3>{{ gdp_bar|safe }}</div><div class="panel"><h3 class="section-title">국가별 최신 Inflation 비교</h3>{{ inf_bar|safe }}</div></div><div class="row2"><div class="panel"><h3 class="section-title">GDP 시사점</h3><ul class="exec">{% for x in gdp_insights %}<li>{{ x }}</li>{% endfor %}</ul></div><div class="panel"><h3 class="section-title">Inflation 시사점</h3><ul class="exec">{% for x in inf_insights %}<li>{{ x }}</li>{% endfor %}</ul></div></div>{% endif %}{% if tab == 'gscpi' %}<div class="panel"><h3 class="section-title">GSCPI 필터</h3><form method="get"><input type="hidden" name="tab" value="gscpi"><div class="grid"><div><label>기간 선택</label><select name="years" onchange="this.form.submit()"><option value="5" {% if selected_years == 5 %}selected{% endif %}>최근 5개년</option><option value="10" {% if selected_years == 10 %}selected{% endif %}>최근 10개년</option><option value="20" {% if selected_years == 20 %}selected{% endif %}>최근 20개년</option></select></div></div><div style="margin-top:12px;"><button type="submit">조회</button></div></form></div><div class="grid"><div class="kpi"><div class="label">Latest Point</div><div class="value">{{ latest_gscpi_label }}</div></div><div class="kpi"><div class="label">Avg 12M</div><div class="value">{{ gscpi_avg12 }}</div></div><div class="kpi"><div class="label">Max 12M</div><div class="value">{{ gscpi_max12 }}</div></div><div class="kpi"><div class="label">Min 12M</div><div class="value">{{ gscpi_min12 }}</div></div></div><div class="row2"><div class="panel"><h3 class="section-title">GSCPI 추이</h3><div class="btn-row"><a class="icon-btn icon-csv" href="{{ gscpi_csv_url }}" title="CSV 다운로드">C</a><a class="icon-btn icon-xlsx" href="{{ gscpi_excel_url }}" title="Excel 다운로드">X</a></div>{{ gscpi_line|safe }}</div><div class="panel"><h3 class="section-title">최근 12개월 GSCPI 비교</h3>{{ gscpi_bar|safe }}</div></div><div class="panel"><h3 class="section-title">GSCPI 시사점</h3><ul class="exec">{% for x in gscpi_insights %}<li>{{ x }}</li>{% endfor %}</ul></div>{% endif %}</div><script>
function setAllCountries(selected) {
    var countrySelect = document.getElementById('country-select');
    if (!countrySelect) return;
    for (var i = 0; i < countrySelect.options.length; i++) {
        countrySelect.options[i].selected = selected;
    }
}
function selectRegionCountriesAndSubmit(form) {
    var flag = document.getElementById('auto-select-region');
    if (flag) flag.value = '1';
    // Do not send countries from the previous region.
    setAllCountries(false);
    form.submit();
}
</script></body></html>"""


@app.template_filter('urlencode')
def urlencode_filter(s):
    return urllib.parse.quote(str(s))


@app.route('/download_data')
def download_data():
    dataset = request.args.get('dataset', 'gdp')
    region = request.args.get('region', '전체')
    years = int(request.args.get('years', '10'))
    countries = request.args.getlist('countries')
    fmt = request.args.get('format', 'csv')
    meta = get_country_meta()
    df = prepare_download_df(dataset, region, years, countries, meta)
    if fmt == 'excel':
        return make_download_response(df, f'{dataset}_data.xlsx', 'excel')
    return make_download_response(df, f'{dataset}_data.csv', 'csv')


@app.route('/')
def index():
    tab = request.args.get('tab', 'macro')
    selected_region = request.args.get('region', '전체')
    try:
        selected_years = int(request.args.get('years', '10'))
    except Exception:
        selected_years = 10
    selected_countries = request.args.getlist('countries')
    auto_select_region = request.args.get('auto_select_region', '0') == '1'
    fetched_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    meta = get_country_meta()
    regions = ['전체'] + sorted(meta['region'].dropna().unique().tolist())
    filtered_meta = meta if selected_region == '전체' else meta[meta['region'] == selected_region]
    country_options = filtered_meta['country'].dropna().unique().tolist()

    if tab == 'macro':
        if auto_select_region:
            # A region change selects every country belonging to that region.
            selected_countries = list(country_options)
        elif not selected_countries:
            selected_countries = [c for c in DEFAULT_COUNTRIES if c in country_options]
            if not selected_countries:
                selected_countries = country_options[:3]
        else:
            # Keep only countries that actually belong to the selected region.
            valid_countries = set(country_options)
            selected_countries = [c for c in selected_countries if c in valid_countries]

    countries_query = ''.join([f'&countries={urllib.parse.quote(c)}' for c in selected_countries])
    context = {
        'tab': tab,
        'regions': regions,
        'selected_region': selected_region,
        'selected_region_enc': urllib.parse.quote(selected_region),
        'selected_years': selected_years,
        'country_options': country_options,
        'selected_countries': selected_countries,
        'fetched_at': fetched_at,
    }

    if tab == 'macro':
        gdp = get_worldbank_indicator(WB_GDP_API_URL, 'value').merge(meta, on=['code', 'country'], how='left')
        inf = get_worldbank_indicator(WB_INFLATION_API_URL, 'value').merge(meta, on=['code', 'country'], how='left')
        if selected_region != '전체':
            gdp = gdp[gdp['region'] == selected_region]
            inf = inf[inf['region'] == selected_region]
        gdp = gdp[gdp['country'].isin(selected_countries)]
        inf = inf[inf['country'].isin(selected_countries)]
        if not gdp.empty:
            gdp = gdp[gdp['year'] >= gdp['year'].max() - selected_years + 1]
        if not inf.empty:
            inf = inf[inf['year'] >= inf['year'].max() - selected_years + 1]
        
        # 3. 누락되었던 바 차트용 데이터 바인딩 로직 구현
        gdp_latest = gdp.sort_values(['country', 'year']).groupby('country', as_index=False).tail(1) if not gdp.empty else pd.DataFrame()
        inf_latest = inf.sort_values(['country', 'year']).groupby('country', as_index=False).tail(1) if not inf.empty else pd.DataFrame()
        
        base_dl = f'/download_data?region={urllib.parse.quote(selected_region)}&years={selected_years}{countries_query}'
        context.update({
            'latest_gdp_year': str(int(gdp['year'].max())) if not gdp.empty else '-',
            'latest_inf_year': str(int(inf['year'].max())) if not inf.empty else '-',
            'gdp_line': build_line(gdp.sort_values(['country', 'year']), 'year', 'value', 'country', ''),
            'inf_line': build_line(inf.sort_values(['country', 'year']), 'year', 'value', 'country', ''),
            'gdp_bar': build_bar(gdp_latest, 'country', 'value', '') if not gdp_latest.empty else '<div class="empty">데이터 없음</div>',
            'inf_bar': build_bar(inf_latest, 'country', 'value', '') if not inf_latest.empty else '<div class="empty">데이터 없음</div>',
            'macro_compare_chart': build_macro_compare_chart(gdp, inf),
            'gdp_insights': build_exec_insights_macro(gdp, 'GDP 성장률'),
            'inf_insights': build_exec_insights_macro(inf, 'Inflation'),
            'gdp_csv_url': base_dl + '&dataset=gdp&format=csv',
            'gdp_excel_url': base_dl + '&dataset=gdp&format=excel',
            'inf_csv_url': base_dl + '&dataset=inflation&format=csv',
            'inf_excel_url': base_dl + '&dataset=inflation&format=excel',
            'macro_compare_csv_url': base_dl + '&dataset=macro_compare&format=csv',
            'macro_compare_excel_url': base_dl + '&dataset=macro_compare&format=excel',
        })
    else:
        gscpi = get_gscpi_data()
        if not gscpi.empty:
            gscpi = gscpi[gscpi['year'] >= gscpi['year'].max() - selected_years + 1]
        latest = gscpi.tail(12).copy() if not gscpi.empty else pd.DataFrame()
        if not latest.empty:
            latest['label'] = latest['date'].dt.strftime('%Y-%m')
        last12 = gscpi.tail(12) if not gscpi.empty else pd.DataFrame()
        context.update({
            'latest_gscpi_label': latest['label'].iloc[-1] if not latest.empty else '-',
            'gscpi_avg12': f"{last12['gscpi'].mean():.2f}" if not last12.empty else '-',
            'gscpi_max12': f"{last12['gscpi'].max():.2f}" if not last12.empty else '-',
            'gscpi_min12': f"{last12['gscpi'].min():.2f}" if not last12.empty else '-',
            'gscpi_line': build_line(gscpi, 'date', 'gscpi', None, ''),
            'gscpi_bar': build_bar(latest, 'label', 'gscpi', '') if not latest.empty else '<div class="empty">데이터 없음</div>',
            'gscpi_insights': build_exec_insights_gscpi(gscpi),
            'gscpi_csv_url': f'/download_data?dataset=gscpi&years={selected_years}&format=csv',
            'gscpi_excel_url': f'/download_data?dataset=gscpi&years={selected_years}&format=excel',
        })

    return render_template_string(HTML, **context)


if __name__ == '__main__':
    init_cache_db()
    print(f'[DB] SQLite 캐시: {os.path.abspath(DB_PATH)}')
    print(f'[DB] 캐시 유효시간: {CACHE_TTL_HOURS:g}시간')
    def is_port_available(host, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return True
            except OSError:
                return False

    def find_available_port(host='127.0.0.1', start_port=8000, end_port=8100):
        """8000부터 순서대로 확인하여 사용 가능한 첫 번째 포트를 반환합니다."""
        for candidate in range(start_port, end_port + 1):
            if is_port_available(host, candidate):
                return candidate
        raise RuntimeError(
            f'{start_port}~{end_port} 범위에서 사용 가능한 포트를 찾지 못했습니다.'
        )

    def wait_and_open_browser(url, port, timeout=20):
        start = time.time()
        while time.time() - start < timeout:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    webbrowser.open_new(url)
                    print(f'[INFO] Browser opened: {url}')
                    return
            except Exception:
                time.sleep(0.4)
        print(f'[WARN] Browser auto-open failed or server readiness timed out. Open manually: {url}')

    host = '127.0.0.1'
    port = find_available_port(host, start_port=8000, end_port=8100)
    local_url = f'http://127.0.0.1:{port}'
    print(f'[INFO] Starting Flask server on {host}:{port}')
    print(f'[INFO] Browser will open automatically: {local_url}')
    threading.Thread(target=wait_and_open_browser, args=(local_url, port), daemon=True).start()
    app.run(host=host, port=port, debug=False, use_reloader=False)
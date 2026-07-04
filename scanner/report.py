"""scanner/report.py -- HTML report generation."""
import json
import re
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Optional

from scanner.config import (
    log, TODAY, RUN_TS, REPORTS_DIR, SOLD, _SUPPRESSED_UUIDS,
    ACTIVE, PRICE_DROP, NEEDS_BACKFILL_FIELDS, _SERVER_PORT,
)
from scanner.detect import _price_valid, needs_backfill, detect_azure_gray
from scanner.score import (
    compute_market_baseline, deal_score, er_confidence_tier,
    deal_stars, days_on_market, market_delta, price_drop_summary,
)

def generate_report(state: dict, summary: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    out_path = REPORTS_DIR / f"{TODAY}.html"

    all_listings = state["listings"]
    active = [v for v in all_listings.values()
              if v.get("status") not in (SOLD, "filtered")
              and v["id"] not in _SUPPRESSED_UUIDS
              and not v.get("dismissed")]

    # Live market baseline -- anchors deal_score stars to the current comparable set
    mkt_baseline, mkt_provisional, mkt_er_floor, mkt_label = compute_market_baseline(active)

    # Use first_seen == TODAY for new-listing detection
    new_ids = {
        v["id"] for v in all_listings.values()
        if v.get("first_seen", "")[:10] == TODAY
        and v.get("status") not in (SOLD, "filtered")
        and not v.get("dismissed")
    }
    price_drop_ids = set(summary.get("price_drops", []))

    n_active = len(active)
    n_new    = len(new_ids)
    n_drops  = len(price_drop_ids)

    def _rank_key(v):
        """
        Sort key: ER-confidence tier first, then descending deal score,
        then descending days on market (older = more motivated seller),
        then ascending price.
        """
        days, _ = days_on_market(v)
        return (
            er_confidence_tier(v, mkt_er_floor),
            -(deal_score(v, mkt_baseline, mkt_er_floor) or 0),
            -days,
            v.get("price") or 999_999,
        )

    def _no_data(v):
        raw_title = (v.get("title") or "").strip()
        return (not raw_title or raw_title.lower() == "www.cars.com") and not v.get("price")

    azure_listings = sorted(
        [v for v in active if v.get("azure_gray") and not v.get("dismissed") and not _no_data(v)],
        key=_rank_key,
    )
    # Split main listings into confirmed-ER (tier 0) and unconfirmed (tier 1+2)
    non_azure = [v for v in active if not v.get("azure_gray") and not _no_data(v)]
    er_confirmed_listings  = sorted(
        [v for v in non_azure if er_confidence_tier(v, mkt_er_floor) == 0],
        key=_rank_key,
    )

    def _is_stale_at_shell(v: dict) -> bool:
        """AT listing with no VIN that has been around 2+ days -- will never self-resolve."""
        if v.get("source") != "autotrader":
            return False
        if v.get("vin"):
            return False
        if v.get("extended_range_confirmed") is not None:
            return False
        first = v.get("first_seen", "")
        try:
            from datetime import date as _date
            return (_date.today() - _date.fromisoformat(first[:10])).days >= 2
        except (ValueError, TypeError):
            return False

    _all_unconf = [v for v in non_azure if er_confidence_tier(v, mkt_er_floor) > 0]
    _stale_shells = [v for v in _all_unconf if _is_stale_at_shell(v)]
    unconfirmed_listings = sorted(
        [v for v in _all_unconf if not _is_stale_at_shell(v)],
        key=_rank_key,
    )
    n_azure      = len(azure_listings)
    n_er_conf    = len(er_confirmed_listings)
    n_unconf     = len(unconfirmed_listings)
    n_stale_at   = len(_stale_shells)

    pending_vdp_count = sum(1 for v in active if _no_data(v))

    prices     = [v["price"] for v in active if _price_valid(v.get("price"))]
    miles_vals = [v["miles"] for v in active if v.get("miles")]
    avg_price  = f"${mean(prices):,.0f}" if prices else "—"
    med_miles  = f"{int(median(miles_vals)):,} mi" if miles_vals else "—"

    seller_counts: dict = {}
    for v in active:
        st = v.get("seller_type", "Dealer")
        seller_counts[st] = seller_counts.get(st, 0) + 1

    clean_prices = [v["price"] for v in active
                    if _price_valid(v.get("price")) and v.get("history_flag") == "✅ Clean"]
    lowest_clean = f"${min(clean_prices):,}" if clean_prices else "—"

    sold_this_run = [
        v for v in all_listings.values()
        if v.get("status") == SOLD and v.get("last_seen") == TODAY
    ]

    pending_str = (
        f" &nbsp;·&nbsp; <span style='color:#556'>{pending_vdp_count} pending data</span>"
        if pending_vdp_count else ""
    )

    baseline_note = (
        f"* provisional (under 8 ER comps) -- stars use ${mkt_baseline:,} fallback"
        if mkt_provisional
        else f"Stars vs. ${mkt_baseline:,} median adj. price of {mkt_label} ER-confirmed Lariat comps"
    )

    # ── build HTML ─────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lightning Scan — {TODAY}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;
     background:#0d0d12;color:#dde0ea;font-size:14px;line-height:1.5}}
.wrap{{max-width:1600px;margin:0 auto;padding:24px 20px}}
h1{{font-size:21px;color:#b0ccff;margin-bottom:4px}}
.run-meta{{color:#778;font-size:13px;margin-bottom:22px}}

.alert{{border-radius:7px;padding:13px 17px;margin-bottom:14px}}
.alert-azure{{background:#0e2038;border-left:4px solid #4a8fd4}}
.alert-drop {{background:#251800;border-left:4px solid #d97a20}}
.alert strong{{display:block;font-size:14px;margin-bottom:5px}}

.section-hdr{{margin:22px 0 6px;font-size:12px;text-transform:uppercase;
              letter-spacing:.07em;color:#4a8fd4;font-weight:600}}
.section-hdr.unconf-hdr{{color:#8a9ab0}}

table{{width:100%;border-collapse:collapse;margin-top:0}}
th{{background:#141420;color:#778;text-transform:uppercase;font-size:11px;
    letter-spacing:.06em;padding:8px 10px;text-align:left;white-space:nowrap;
    cursor:pointer;user-select:none}}
th:hover{{color:#aab;background:#181828}}
th.sort-asc::after{{content:" ▲";font-size:9px}}
th.sort-desc::after{{content:" ▼";font-size:9px}}
td{{padding:7px 9px;border-bottom:1px solid #1a1a26;vertical-align:middle;font-size:13px}}
tr:hover td{{background:#13131e}}
tr.r-azure td{{background:#0a1828}}
tr.r-azure:hover td{{background:#0c1e33}}
tr.r-new td{{border-left:3px solid #4caf50}}
tr.r-drop td{{border-left:3px solid #ff9800}}
tr.r-unconf td{{opacity:0.75}}
tr.r-likelysr td{{opacity:0.45;font-style:italic}}

.stars{{font-size:13px;letter-spacing:1px}}
.s5{{color:#f2c94c}}.s4{{color:#f2c94c}}.s3{{color:#8a9ab0}}
.s2{{color:#8a9ab0}}.s1{{color:#445}}

tr.r-plat td{{background:#1c1500;border-left:3px solid #c8a800}}
tr.r-plat:hover td{{background:#261d00}}
tr.r-dim td{{opacity:0.35;font-style:italic}}

.drop-price{{color:#d97a20}}
.was{{color:#554;text-decoration:line-through;font-size:12px}}

.delta-under{{color:#6fcf97;font-weight:600;font-size:12px}}
.delta-over{{color:#eb5757;font-size:12px}}
.delta-flat{{color:#778;font-size:12px}}

.dom-hot{{color:#f2c94c;font-weight:600}}
.dom-warm{{color:#8a9ab0}}
.dom-cold{{color:#445}}

.drop-badge{{color:#d97a20;font-size:11px;white-space:nowrap}}

.vin-cell{{font-family:monospace;font-size:11px;color:#8a9ab0}}
.btn-vin{{background:none;border:1px solid #334;border-radius:3px;color:#667;
          cursor:pointer;font-size:10px;padding:1px 4px;margin-left:4px;
          vertical-align:middle}}
.btn-vin:hover{{border-color:#556;color:#aab}}

a{{color:#6ab0f5;text-decoration:none}}
a:hover{{text-decoration:underline}}

.er-y{{color:#6fcf97;font-weight:700}}
.er-n{{color:#eb5757;font-weight:700}}
.er-u{{color:#f2c94c;font-weight:700}}
.er-src{{color:#556;font-size:10px;display:block;margin-top:1px}}

.market{{background:#141420;border-radius:8px;padding:18px 20px;margin-top:28px;
         display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px}}
.market-title{{grid-column:1/-1;color:#556;font-size:11px;text-transform:uppercase;
               letter-spacing:.07em;margin-bottom:2px}}
.stat-lbl{{color:#556;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
.stat-val{{font-size:20px;font-weight:600;color:#b8ccec;margin-top:3px}}
.seller-mix{{grid-column:1/-1;color:#8a9ab0;font-size:13px;margin-top:4px}}

.sold-section{{margin-top:26px;color:#445;font-size:13px}}
.sold-section h3{{color:#556;margin-bottom:8px;font-size:13px;
                 text-transform:uppercase;letter-spacing:.05em}}
.footer-note{{margin-top:20px;color:#445;font-size:12px}}
.baseline-note{{font-size:11px;color:#556;margin-top:5px;padding-left:2px}}

/* interactive server buttons */
.btn-dismiss{{
  background:transparent;border:1px solid #334;color:#556;
  border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;
  white-space:nowrap;transition:opacity .15s,color .15s,border-color .15s
}}
.btn-dismiss:hover{{color:#e05;border-color:#e05}}
.btn-confirm{{
  background:transparent;border:none;cursor:pointer;
  font-size:14px;font-weight:700;padding:0 3px;
  transition:opacity .15s,background .15s;line-height:1;
  border-radius:3px
}}
.btn-confirm:hover{{background:rgba(255,255,255,0.08)}}
.btn-confirm:disabled{{cursor:default;opacity:.5}}
.btn-confirm.er-u{{
  border:1px solid #4a4a20;
  padding:0 5px;
  font-size:12px;
  border-radius:4px
}}
.btn-confirm.er-u:hover{{border-color:#f2c94c;background:rgba(242,201,76,0.08)}}
.btn-hist{{
  background:none;border:1px solid #334;border-radius:4px;
  color:inherit;cursor:pointer;font-size:13px;padding:2px 6px;
  white-space:nowrap;
}}
.btn-hist:hover{{border-color:#778;background:rgba(255,255,255,0.05)}}
.hist-picker{{
  position:absolute;z-index:100;background:#1e1e2e;border:1px solid #445;
  border-radius:6px;padding:4px;display:flex;flex-direction:column;gap:2px;
  box-shadow:0 4px 16px rgba(0,0,0,0.5);min-width:130px;
}}
.hist-picker button{{
  background:none;border:none;color:#dde0ea;cursor:pointer;
  font-size:13px;padding:5px 10px;text-align:left;border-radius:4px;
}}
.hist-picker button:hover{{background:rgba(255,255,255,0.08)}}
tr.row-dismissed td{{opacity:.25;pointer-events:none}}
tr.row-dismissed .btn-dismiss{{pointer-events:auto;opacity:.6}}
</style>
<script>
const SERVER = 'http://localhost:{_SERVER_PORT}';
const TIMEOUT = 3000;

async function pingServer() {{
  try {{
    const r = await fetch(SERVER + '/ping', {{method:'GET', cache:'no-store'}});
    if (r.ok) {{
      document.getElementById('srv-status').style.background = '#6fcf97';
      document.getElementById('srv-status').title = 'Server online';
      return;
    }}
  }} catch(e) {{}}
  document.getElementById('srv-status').style.background = '#eb5757';
  document.getElementById('srv-status').title = 'Server offline -- restart with: python3 scanner.py --report';
}}
pingServer();
setInterval(pingServer, 10000);

async function apiPost(path) {{
  try {{
    const ctrl = new AbortController();
    const tid  = setTimeout(() => ctrl.abort(), TIMEOUT);
    const r = await fetch(SERVER + path, {{
      method: 'POST',
      signal: ctrl.signal
    }});
    clearTimeout(tid);
    if (!r.ok) return false;
    const j = await r.json();
    return j.ok === true;
  }} catch(e) {{
    return false;
  }}
}}

function showRowError(btn, msg) {{
  const tr  = btn.closest('tr');
  let err   = tr.querySelector('.row-err');
  if (!err) {{
    err = document.createElement('span');
    err.className = 'row-err';
    err.style.cssText = 'color:#e05;font-size:11px;margin-left:6px';
    btn.parentNode.appendChild(err);
  }}
  err.textContent = msg;
  clearTimeout(err._tid);
  err._tid = setTimeout(() => {{ err.textContent = ''; }}, 4000);
}}

async function dismissRow(uid, btn) {{
  btn.disabled = true;
  btn.textContent = '...';
  const ok = await apiPost('/dismiss?uuid=' + uid);
  if (!ok) {{
    btn.disabled = false;
    btn.textContent = 'Dismiss';
    showRowError(btn, 'server offline');
    return;
  }}
  const tr = btn.closest('tr');
  tr.classList.add('row-dismissed');
  btn.textContent = 'dismissed';
}}

async function toggleER(uid, btn, current) {{
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '...';
  const newVal = (current === 'true') ? 'false' : 'true';
  const ok = await apiPost('/set-er?uuid=' + uid + '&value=' + newVal);
  btn.disabled = false;
  if (!ok) {{
    btn.textContent = prev;
    showRowError(btn, 'server offline');
    return;
  }}
  if (newVal === 'true') {{
    btn.textContent = 'ok'; btn.className = 'btn-confirm er-y';
    btn.title = 'ER confirmed -- click to toggle';
  }} else {{
    btn.textContent = 'no'; btn.className = 'btn-confirm er-n';
    btn.title = 'Not ER -- click to toggle';
  }}
  btn.setAttribute('data-current', newVal);
}}

async function toggle511A(uid, btn, current) {{
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '...';
  const newVal = (current === 'true') ? 'false' : 'true';
  const ok = await apiPost('/set-511a?uuid=' + uid + '&value=' + newVal);
  btn.disabled = false;
  if (!ok) {{
    btn.textContent = prev;
    showRowError(btn, 'server offline');
    return;
  }}
  if (newVal === 'true') {{
    btn.textContent = 'ok'; btn.className = 'btn-confirm er-y';
    btn.title = '511A confirmed -- click to toggle';
  }} else {{
    btn.textContent = 'no'; btn.className = 'btn-confirm er-n';
    btn.title = 'Not 511A -- click to toggle';
  }}
  btn.setAttribute('data-current', newVal);
}}

function openHistPicker(uid, btn) {{
  const existing = document.getElementById('hist-picker');
  if (existing) {{ existing.remove(); if (existing._uid === uid) return; }}

  const options = [
    ['clean',    'Clean'],
    ['accident', 'Accident'],
    ['buyback',  'Buyback'],
    ['unknown',  'Unknown'],
  ];

  const picker = document.createElement('div');
  picker.className = 'hist-picker';
  picker.id = 'hist-picker';
  picker._uid = uid;

  options.forEach(([val, label]) => {{
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => setHistory(uid, val, label, btn, picker);
    picker.appendChild(b);
  }});

  // Position below the button
  const rect = btn.getBoundingClientRect();
  picker.style.top  = (rect.bottom + window.scrollY + 4) + 'px';
  picker.style.left = (rect.left  + window.scrollX)     + 'px';
  picker.style.position = 'absolute';
  document.body.appendChild(picker);

  // Close on outside click
  setTimeout(() => {{
    document.addEventListener('click', function _close(e) {{
      if (!picker.contains(e.target) && e.target !== btn) {{
        picker.remove();
        document.removeEventListener('click', _close);
      }}
    }});
  }}, 0);
}}

async function setHistory(uid, value, label, btn, picker) {{
  picker.remove();
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '...';
  const ok = await apiPost('/set-history?uuid=' + uid + '&value=' + value);
  btn.disabled = false;
  if (!ok) {{
    btn.textContent = prev;
    showRowError(btn, 'server offline');
    return;
  }}
  btn.textContent = label;
}}

function copyVin(vin, btn) {{
  navigator.clipboard.writeText(vin).then(() => {{
    const prev = btn.textContent;
    btn.textContent = 'copied';
    setTimeout(() => {{ btn.textContent = prev; }}, 1500);
  }}).catch(() => {{
    btn.textContent = 'err';
    setTimeout(() => {{ btn.textContent = 'copy'; }}, 1500);
  }});
}}

/* ── client-side table sort ──────────────────────────────────────────── */
/* Each sortable <th> carries data-col (0-based column index) and
   data-type ("num" or "str"). Clicking cycles asc/desc. */
document.addEventListener('DOMContentLoaded', () => {{
  document.querySelectorAll('th[data-col]').forEach(th => {{
    th.addEventListener('click', () => sortByTh(th));
  }});
}});

let _sortState = {{}};  /* tableId -> {{col, asc}} */

function sortByTh(th) {{
  const table = th.closest('table');
  const tid   = table.id || (table.id = 'tbl' + Math.random().toString(36).slice(2));
  const col   = parseInt(th.dataset.col);
  const type  = th.dataset.type || 'str';
  const prev  = _sortState[tid];
  const asc   = (prev && prev.col === col) ? !prev.asc : true;
  _sortState[tid] = {{col, asc}};

  /* update header arrows */
  th.closest('thead').querySelectorAll('th').forEach(h => {{
    h.classList.remove('sort-asc', 'sort-desc');
  }});
  th.classList.add(asc ? 'sort-asc' : 'sort-desc');

  const tbody = table.querySelector('tbody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {{
    const av = a.children[col]?.dataset.sort ?? a.children[col]?.textContent ?? '';
    const bv = b.children[col]?.dataset.sort ?? b.children[col]?.textContent ?? '';
    let cmp;
    if (type === 'num') {{
      cmp = (parseFloat(av) || 0) - (parseFloat(bv) || 0);
    }} else {{
      cmp = av.localeCompare(bv, undefined, {{numeric: true}});
    }}
    return asc ? cmp : -cmp;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
</script>
</head>
<body>
<div class="wrap">
<h1>Lightning Scanner — 2023-24 F-150 Lightning Lariat 511A ER</h1>
<p class="run-meta">
  Run: {RUN_TS} &nbsp;·&nbsp;
  <strong style="color:#c0d0e8">{n_active} active</strong>
  (<strong style="color:#6fcf97">{n_er_conf} confirmed ER</strong> /
  <strong style="color:#f2c94c">{n_unconf} unconfirmed</strong>) &nbsp;·&nbsp;
  <strong style="color:#4caf50">{n_new} new</strong> &nbsp;·&nbsp;
  <strong style="color:#ff9800">{n_drops} drop{'s' if n_drops != 1 else ''}</strong> &nbsp;·&nbsp;
  <strong style="color:#4a8fd4">{n_azure} Azure Gray</strong>{pending_str}{"" if not n_stale_at else f" &nbsp;·&nbsp; <span style='color:#445;font-size:12px' title='AutoTrader listings seen 2+ days with no VIN — will never self-resolve; kept in state for sold-marking'>{n_stale_at} AT shells hidden</span>"}
  <span id="srv-status" style="display:inline-block;width:8px;height:8px;border-radius:50%;
    background:#556;margin-left:6px;vertical-align:middle" title="Server status unknown"></span>
</p>
"""

    # ── alert: price drops ─────────────────────────────────────────────
    if price_drop_ids:
        html += '<div class="alert alert-drop">\n'
        html += '  <strong>Price Drop Alert (&gt;$100)</strong>\n'
        for uid in price_drop_ids:
            v = all_listings.get(uid, {})
            hist = v.get("price_history", [])
            if len(hist) >= 2:
                old_p = hist[-2]["price"]
                new_p = v.get("price", hist[-1]["price"])
                drop  = old_p - new_p
                if drop >= 100:
                    html += (
                        f'  <a href="{v["link"]}" target="_blank">'
                        f'{v.get("title","")[:55]}</a> '
                        f'dropped <strong>${drop:,}</strong> '
                        f'(${old_p:,} -> ${new_p:,})<br>\n'
                    )
        html += '</div>\n'

    # ── shared table header ────────────────────────────────────────────
    # Columns (0-indexed for JS sort):
    # 0:Deal 1:Days 2:Delta 3:Title 4:Location 5:Miles 6:Price 7:Drops 8:VIN 9:ER 10:511A 11:Hist 12:Color 13:Link 14:Dismiss
    table_header = """<table id="tbl-main">
<thead>
<tr>
  <th data-col="0" data-type="num" title="Deal score (1-5 stars vs market median)">Deal</th>
  <th data-col="1" data-type="num" title="Days on market (true listing age if known, else days we have seen it)">Days</th>
  <th data-col="2" data-type="num" title="Dollars below (positive) or above (negative) market median, adjusted for mileage">Delta</th>
  <th data-col="3" data-type="str">Year / Title</th>
  <th data-col="4" data-type="str">Location</th>
  <th data-col="5" data-type="num">Miles</th>
  <th data-col="6" data-type="num">Price</th>
  <th data-col="7" data-type="str" title="Price drop count and most recent drop amount">Drops</th>
  <th data-col="8" data-type="str" title="VIN (click to copy for independent history check)">VIN</th>
  <th data-col="9"  data-type="str" title="Extended Range confirmed? Source shown on hover">ER</th>
  <th data-col="10" data-type="str">511A</th>
  <th data-col="11" data-type="str">History</th>
  <th data-col="12" data-type="str">Color</th>
  <th data-col="13" data-type="str">Link</th>
  <th></th>
</tr>
</thead>
<tbody>
"""

    def _render_rows(listing_group: list) -> str:
        out = ""
        for v in listing_group:
            uid      = v["id"]
            is_azure = v.get("azure_gray", False)
            is_new   = uid in new_ids
            is_drop  = uid in price_drop_ids or v.get("status") == "price_drop"
            is_plat_pass = (
                re.search(r"\bPlatinum\b", v.get("title", ""), re.I)
                and re.search(r"\b2023\b", v.get("title", ""))
            )
            tc = v.get("timeout_count", 0)
            is_dim = (
                tc >= 3
                and not v.get("price")
                and not v.get("location")
                and not v.get("color")
            )
            er_tier = er_confidence_tier(v, mkt_er_floor)

            row_cls = ""
            if is_azure:      row_cls += " r-azure"
            if is_plat_pass:  row_cls += " r-plat"
            if is_dim:        row_cls += " r-dim"
            if is_new:        row_cls += " r-new"
            elif is_drop:     row_cls += " r-drop"
            if er_tier == 2:  row_cls += " r-likelysr"
            elif er_tier == 1: row_cls += " r-unconf"
            if v.get("dismissed"): row_cls += " row-dismissed"

            # ── Deal score ─────────────────────────────────────────────
            score = deal_score(v, mkt_baseline, mkt_er_floor)
            score_num = score or 0
            if score is None:
                deal_cell = '<td data-sort="0">—</td>'
            else:
                deal_cell = (
                    f'<td data-sort="{score_num}">'
                    f'<span class="stars s{score}">{deal_stars(score)}</span>'
                    f'</td>'
                )

            # ── Days on market ─────────────────────────────────────────
            days, true_dom = days_on_market(v)
            if true_dom:
                dom_label = f"{days}d"
                dom_title = f"title=\"{days} days on market (Visor true listing age)\""
                dom_cls   = "dom-hot" if days >= 45 else ("dom-warm" if days >= 14 else "dom-cold")
            else:
                dom_label = f"~{days}d" if days > 0 else "—"
                dom_title = f"title=\"seen for ~{days} days (our first-seen date; true age may be longer)\""
                dom_cls   = "dom-hot" if days >= 45 else ("dom-warm" if days >= 14 else "dom-cold")
            days_cell = (
                f'<td data-sort="{days}" {dom_title}>'
                f'<span class="{dom_cls}">{dom_label}</span>'
                f'</td>'
            )

            # ── Delta ($ vs market) ────────────────────────────────────
            delta = market_delta(v, mkt_baseline)
            if delta is None:
                delta_cell = '<td data-sort="0">—</td>'
            elif delta >= 1_000:
                # Under market: positive is good (savings vs. median)
                delta_cell = (
                    f'<td data-sort="{delta}" title="${delta:,} below market median -- opens negotiation here">'
                    f'<span class="delta-under">+${delta:,}</span></td>'
                )
            elif delta <= -1_000:
                # Over market: negative means paying a premium
                over = -delta
                delta_cell = (
                    f'<td data-sort="{delta}" title="${over:,} above market median">'
                    f'<span class="delta-over">-${over:,}</span></td>'
                )
            else:
                delta_cell = (
                    f'<td data-sort="{delta}">'
                    f'<span class="delta-flat">~mkt</span></td>'
                )

            # ── Year / title ───────────────────────────────────────────
            year_m = re.search(r"\b(20\d\d)\b", v.get("title", ""))
            year   = year_m.group(1) if year_m else "—"
            title_short = re.sub(r"^\d{4}\s+", "", v.get("title", "")).strip()[:48]
            title_html = f"<strong>{year}</strong> {title_short}"
            if is_plat_pass:
                title_html = '<span style="color:#c8a800;font-weight:700;font-size:10px">PLAT</span> ' + title_html
            title_cell = f'<td data-sort="{year} {title_short}">{title_html}</td>'

            # ── Location ───────────────────────────────────────────────
            location = v.get("location") or "—"
            if (v.get("source") == "autotrader" and location != "—" and len(location) > 60):
                _loc_segs = re.split(
                    r'(?:No Accidents|Excellent|Good Deal|Great Deal|Fair Deal|'
                    r'EV Battery|See payment|Dealer Fees|Electric|Hybrid|'
                    r'\d+[Kk]\s*mi\b|[\d,]{{4,}})',
                    location, flags=re.I,
                )
                _loc_clean = _loc_segs[-1].strip()[:60] if _loc_segs else ""
                location = _loc_clean if _loc_clean else location[:50] + "..."
            if v.get("source") == "ebay":
                loc_display = location if location != "—" else "United States"
                location = f"eBay · {loc_display}"
            loc_extras = ""
            if tc >= 2:
                loc_extras += (
                    f' <span style="color:#445;font-size:10px;font-style:italic">'
                    f'timeout x{tc}</span>'
                )
            if (v.get("source") == "autotrader" and v.get("vdp_fail_count", 0) >= 1):
                loc_extras += (
                    ' <span style="color:#445;font-size:10px;font-style:italic"'
                    ' title="AutoTrader VDP blocked -- data from search page only">'
                    '(AT)</span>'
                )
            location_cell = f'<td data-sort="{location}">{location}{loc_extras}</td>'

            # ── Miles ──────────────────────────────────────────────────
            miles = v.get("miles")
            miles_cell = (
                f'<td data-sort="{miles or 0}">{miles:,}</td>' if miles
                else '<td data-sort="0">—</td>'
            )

            # ── Price ──────────────────────────────────────────────────
            price = v.get("price")
            if is_drop:
                hist_ph = v.get("price_history", [])
                if len(hist_ph) >= 2:
                    old_p = hist_ph[-2]["price"]
                    price_html = (
                        f'<span class="drop-price">${price:,} ↓</span> '
                        f'<span class="was">${old_p:,}</span>'
                    )
                else:
                    price_html = f"${price:,}" if price else "—"
            else:
                price_html = f"${price:,}" if price else "—"
            price_cell = (
                f'<td data-sort="{price or 0}">{price_html}</td>'
            )

            # ── Price-drop summary ─────────────────────────────────────
            drop_summary = price_drop_summary(v)
            drop_cell = (
                f'<td data-sort="{drop_summary}">'
                f'<span class="drop-badge">{drop_summary}</span></td>'
                if drop_summary
                else '<td data-sort="">—</td>'
            )

            # ── VIN ────────────────────────────────────────────────────
            vin = v.get("vin") or ""
            if vin:
                vin_cell = (
                    f'<td data-sort="{vin}" class="vin-cell">'
                    f'{vin[:8]}...'
                    f'<button class="btn-vin" onclick="copyVin(\'{vin}\',this)"'
                    f' title="Copy full VIN: {vin}">copy</button>'
                    f'</td>'
                )
            else:
                vin_cell = '<td data-sort="">—</td>'

            # ── ER column (toggle + source tooltip) ───────────────────
            er      = v.get("extended_range_confirmed")
            er_note = v.get("er_note") or ""
            er_src_html = (
                f'<span class="er-src" title="{er_note}">{er_note[:28]}</span>'
                if er_note else ""
            )
            if er is True:
                er_cell = (
                    f'<td data-sort="1"><button class="btn-confirm er-y" data-current="true"'
                    f' onclick="toggleER(\'{uid}\',this,this.dataset.current)"'
                    f' title="ER confirmed ({er_note}) -- click to toggle">ok</button>'
                    f'{er_src_html}</td>'
                )
            elif er is False:
                er_cell = (
                    f'<td data-sort="0"><button class="btn-confirm er-n" data-current="false"'
                    f' onclick="toggleER(\'{uid}\',this,this.dataset.current)"'
                    f' title="Not ER ({er_note}) -- click to toggle">no</button>'
                    f'{er_src_html}</td>'
                )
            else:
                er_cell = (
                    f'<td data-sort=""><button class="btn-confirm er-u" data-current="null"'
                    f' onclick="toggleER(\'{uid}\',this,this.dataset.current)"'
                    f' title="ER unknown -- click to confirm">?</button>'
                    f'{er_src_html}</td>'
                )

            # ── 511A column ────────────────────────────────────────────
            eq = v.get("equipment_511a_confirmed")
            equip_note = v.get("equip_note") or ""
            if eq is True:
                eq_cell = (
                    f'<td data-sort="1"><button class="btn-confirm er-y" data-current="true"'
                    f' onclick="toggle511A(\'{uid}\',this,this.dataset.current)"'
                    f' title="511A confirmed ({equip_note}) -- click to toggle">ok</button></td>'
                )
            elif eq is False:
                eq_cell = (
                    f'<td data-sort="0"><button class="btn-confirm er-n" data-current="false"'
                    f' onclick="toggle511A(\'{uid}\',this,this.dataset.current)"'
                    f' title="Not 511A -- click to toggle">no</button></td>'
                )
            else:
                eq_cell = (
                    f'<td data-sort=""><button class="btn-confirm er-u" data-current="null"'
                    f' onclick="toggle511A(\'{uid}\',this,this.dataset.current)"'
                    f' title="511A unknown -- click to confirm">?</button></td>'
                )

            # ── History picker ─────────────────────────────────────────
            hist_flag = v.get("history_flag", "Unknown")
            hist_note_str = v.get("history_note", "")
            hist_note_html = (
                f' <span style="color:#556;font-size:10px">({hist_note_str})</span>'
                if hist_note_str else ""
            )
            hist_cell = (
                f'<td data-sort="{hist_flag}">'
                f'<button class="btn-hist"'
                f' onclick="openHistPicker(\'{uid}\',this)"'
                f' title="Click to set history status">'
                f'{hist_flag}</button>{hist_note_html}</td>'
            )

            # ── Color ──────────────────────────────────────────────────
            color = v.get("color") or "—"
            color_pfx = "blue " if is_azure else ""
            if is_new:
                color_pfx = "new " + color_pfx
            color_cell = f'<td data-sort="{color}">{color_pfx}{color}</td>'

            # ── Link ───────────────────────────────────────────────────
            link = v.get("link", "")
            link_cell = (
                f'<td><a href="{link}" target="_blank">View</a></td>'
                if link else '<td>—</td>'
            )

            # ── Dismiss ────────────────────────────────────────────────
            dismiss_label = "dismissed" if v.get("dismissed") else "Dismiss"
            dismiss_cell = (
                f'<td><button class="btn-dismiss"'
                f' onclick="dismissRow(\'{uid}\',this)"'
                f' title="Hide this listing">{dismiss_label}</button></td>'
            )

            out += (
                f'<tr class="{row_cls.strip()}" data-uid="{uid}">'
                + deal_cell
                + days_cell
                + delta_cell
                + title_cell
                + location_cell
                + miles_cell
                + price_cell
                + drop_cell
                + vin_cell
                + er_cell
                + eq_cell
                + hist_cell
                + color_cell
                + link_cell
                + dismiss_cell
                + "</tr>\n"
            )
        return out

    # ── Azure Gray priority section ────────────────────────────────────
    if azure_listings:
        html += '<p class="section-hdr">Priority — Azure Gray</p>\n'
        html += table_header
        html += _render_rows(azure_listings)
        html += "</tbody></table>\n"

    # ── Confirmed ER section ───────────────────────────────────────────
    html += f'<p class="section-hdr">Confirmed ER ({n_er_conf})</p>\n'
    html += table_header
    html += _render_rows(er_confirmed_listings)
    html += "</tbody></table>\n"

    # ── Unconfirmed section ────────────────────────────────────────────
    _stale_note = (
        f" · {n_stale_at} stale AT shells hidden"
        if n_stale_at else ""
    )
    html += (
        f'<p class="section-hdr unconf-hdr">'
        f'Unconfirmed ({n_unconf}{_stale_note}) '
        f'<span style="font-size:10px;color:#445;text-transform:none;letter-spacing:0">'
        f'-- no VIN; ER status unknown. Dimmer rows are priced below the ER floor (likely SR).'
        f'</span></p>\n'
    )
    html += table_header
    html += _render_rows(unconfirmed_listings)
    html += "</tbody></table>\n"

    # ── market snapshot ────────────────────────────────────────────────
    seller_mix = " · ".join(
        f"{cnt} {label.lower()}" for label, cnt in sorted(seller_counts.items()) if cnt
    )
    html += f"""
<div class="market">
  <div class="market-title">Market Snapshot</div>
  <div><div class="stat-lbl">Active</div><div class="stat-val">{n_active}</div></div>
  <div><div class="stat-lbl">ER Confirmed</div><div class="stat-val">{n_er_conf}</div></div>
  <div><div class="stat-lbl">Avg Price</div><div class="stat-val">{avg_price}</div></div>
  <div><div class="stat-lbl">Median Miles</div><div class="stat-val">{med_miles}</div></div>
  <div><div class="stat-lbl">Lowest Clean</div><div class="stat-val">{lowest_clean}</div></div>
  <div><div class="stat-lbl">Deal Baseline</div><div class="stat-val">${mkt_baseline:,}{"*" if mkt_provisional else ""}</div></div>
  <div class="seller-mix">{seller_mix}</div>
</div>
<p class="baseline-note">{baseline_note}</p>
"""

    if sold_this_run:
        html += '<div class="sold-section"><h3>Sold / Gone This Run</h3>\n'
        for v in sold_this_run:
            p = v.get("price")
            price_str = f" — ${p:,}" if _price_valid(p) else ""
            html += f"  <div>• {v.get('title','')} — {v.get('location','')}{price_str}</div>\n"
        html += "</div>\n"

    _src_label_map = {
        "cars.com":    "cars.com",
        "ebay":        "eBay Motors",
        "carvana":     "Carvana",
        "cargurus":    "CarGurus",
        "carmax":      "CarMax",
        "autotrader":  "AutoTrader",
        "edmunds":     "Edmunds",
        "vroom":       "Vroom",
    }
    _active_srcs: set = set()
    for _e in state.get("listings", {}).values():
        _s = _e.get("source", "")
        if _s:
            _active_srcs.add(_s)
    _footer_parts = [_src_label_map.get(s, s) for s in sorted(_active_srcs)
                     if _src_label_map.get(s, s)]
    _footer_sources = ", ".join(_footer_parts) if _footer_parts else "—"
    html += f'<p class="footer-note">Sources: {_footer_sources}.</p>\n'

    html += "\n</div>\n</body>\n</html>\n"

    out_path.write_text(html, encoding="utf-8")
    log.info("Report written → %s", out_path)
    return out_path


# ═══════════════════════════════════════════════════════════════════════════
# Dismissal
# ═══════════════════════════════════════════════════════════════════════════


"""Local live dashboard (Flask): levels, indicators, setup checks, positions, P&L."""
from __future__ import annotations

from flask import Flask, jsonify

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Camarilla + RSI bot</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0d1117; color:#e6edf3; font:14px/1.45 system-ui,Segoe UI,Arial; margin:18px; }
  h1 { font-size:18px; margin:0 0 4px; }
  .sub { color:#8b949e; margin-bottom:14px; }
  .pnl-pos { color:#3fb950; } .pnl-neg { color:#f85149; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(430px,1fr)); gap:14px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:12px 14px; }
  .card h2 { font-size:15px; margin:0 0 6px; display:flex; justify-content:space-between; }
  .lv { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
  .lv span { background:#21262d; border-radius:5px; padding:2px 7px; font-size:12px; color:#8b949e; }
  .lv b { color:#e6edf3; font-weight:600; }
  .chk { margin:2px 0; font-size:12.5px; }
  .ok { color:#3fb950; } .no { color:#f85149; } .blk { color:#d29922; }
  .pos { background:#1f6feb22; border:1px solid #1f6feb55; border-radius:6px;
         padding:6px 8px; margin-top:8px; font-size:13px; }
  .muted { color:#8b949e; }
  table.risk { font-size:12.5px; border-collapse:collapse; margin-top:8px; }
  table.risk td { padding:1px 10px 1px 0; color:#8b949e; }
  table.risk td b { color:#e6edf3; }
</style></head>
<body>
  <h1>Camarilla + RSI Intraday Bot <span id="mode" class="muted"></span></h1>
  <div class="sub">
    Day P&L: <b id="pnl">—</b> &nbsp;•&nbsp; realized <span id="rl">—</span>
    &nbsp;•&nbsp; trades <span id="tr">—</span>
    &nbsp;•&nbsp; <span id="halt"></span>
    <span class="muted" id="ts" style="float:right"></span>
  </div>
  <div class="grid" id="grid"></div>
<script>
const fmt = n => n==null ? "—" : Number(n).toLocaleString("en-IN");
async function refresh(){
  try{
    const r = await fetch("/api/state"); const s = await r.json();
    const g = s.global || {};
    document.getElementById("mode").textContent =
        " — " + (g.mode||"?").toUpperCase() + (g.live_orders ? " (LIVE ORDERS)" : " (simulated fills)");
    const pnl = g.day_pnl ?? 0;
    const el = document.getElementById("pnl");
    el.textContent = "₹" + fmt(pnl);
    el.className = pnl >= 0 ? "pnl-pos" : "pnl-neg";
    document.getElementById("rl").textContent = "₹" + fmt(g.realized_pnl ?? 0);
    const rk = g.risk || {};
    document.getElementById("tr").textContent = (rk.trades_today??0) + "/" + (rk.max_trades??"—");
    document.getElementById("halt").innerHTML = rk.halted
        ? '<span class="no">HALTED: '+(rk.halt_reason||"")+'</span>'
        : (g.squared_off ? '<span class="blk">squared off (EOD)</span>' : '<span class="ok">running</span>');
    document.getElementById("ts").textContent = g.ts || "";
    const grid = document.getElementById("grid"); grid.innerHTML = "";
    for (const [sym, d] of Object.entries(s.instruments || {})){
      const lv = d.levels || {}; const m = d.meta || {};
      let html = `<h2><span>${sym} <span class="muted">${d.tradingsymbol||""}</span></span>
        <span>${fmt(d.ltp)} <span class="${(d.pct_change??0)>=0?'ok':'no'}">${d.pct_change??""}%</span></span></h2>`;
      html += `<div class="lv">
        <span>H5 <b>${fmt(lv.h5)}</b></span><span>H4 <b>${fmt(lv.h4)}</b></span>
        <span>H3 <b>${fmt(lv.h3)}</b></span><span>P <b>${fmt(lv.pivot)}</b></span>
        <span>L3 <b>${fmt(lv.l3)}</b></span><span>L4 <b>${fmt(lv.l4)}</b></span>
        <span>ORH <b>${fmt(d.orh)}</b>${d.or_final?"":" *"}</span><span>ORL <b>${fmt(d.orl)}</b></span></div>`;
      html += `<div class="lv">
        <span>RSI <b>${m.rsi??"—"}</b></span><span>ATR <b>${m.atr??"—"}</b></span>
        <span>VWAP <b>${fmt(m.vwap)}</b></span>
        <span>vol <b>${fmt(m.vol)}</b> / avg ${fmt(m.vol_sma)}</span></div>`;
      const blocks = [];
      if (d.blocked_day) blocks.push("NO-TRADE DAY: " + (d.block_reasons||[]).join("; "));
      if (d.rsi_band_blocked) blocks.push("RSI pinned 40–55 (first hour)");
      if (d.choppy) blocks.push("choppy H3–L3");
      if (blocks.length) html += `<div class="chk blk">⚠ ${blocks.join(" • ")}</div>`;
      for (const [setup, checks] of Object.entries(d.checks || {})){
        html += `<div class="chk muted"><u>${setup}</u></div>`;
        for (const c of checks)
          html += `<div class="chk ${c.ok?"ok":"no"}">${c.ok?"✔":"✘"} ${c.name}` +
                  (c.detail?` <span class="muted">(${c.detail})</span>`:"") + `</div>`;
      }
      if (d.position){
        const p = d.position;
        html += `<div class="pos">POSITION ${p.setup} — ${p.lots} lots (${p.qty}) @ ${fmt(p.entry)}
          | SL ${fmt(p.stop)} | T1 ${fmt(p.target1)}${p.t1_done?" ✔":""}
          | hedge ${p.hedge||"—"} | uP&L <b class="${p.upnl>=0?'pnl-pos':'pnl-neg'}">₹${fmt(p.upnl)}</b></div>`;
      }
      const card = document.createElement("div"); card.className = "card";
      card.innerHTML = html; grid.appendChild(card);
    }
  }catch(e){ /* engine still booting */ }
}
refresh(); setInterval(refresh, 3000);
</script></body></html>"""


def create_app(state, cfg: dict) -> Flask:
    app = Flask("camarilla_bot")

    @app.route("/")
    def index():
        return _PAGE

    @app.route("/api/state")
    def api_state():
        return jsonify(state.snapshot())

    return app

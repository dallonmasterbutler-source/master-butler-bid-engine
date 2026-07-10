"""
MASTER BUTLER — BID REVIEW: UNIFIED COMMAND CENTER (Dallon's Stitch
design, Jul 10: 'picture in the background with the address and price,
site specs on the right, the button on the bottom... revamp the entire
site with this style').

Ported from Stitch screen 2c6e406c (project 3268214625124165543) to
PLAIN CSS — no Tailwind CDN, no frameworks (Martha's-machine rule).
Tokens verbatim from the design:
  base #05140f · surface #082d22 · container #0b3d2e · high #112921
  gold #c9a227 · glass rgba(17,41,33,.7)+blur · gold-glow text shadow
  Hanken Grotesk headlines · Inter body · tabular numerals

DOCTRINE KEPT: the gold button reads 'Approve — creates DRAFT quote'
(nothing ever sends); hero photo is the CUSTOMER'S real imagery
(street view → aerial → calm green), never stock.

Served at /newbid/<stamp> (parallel preview — the office's pages are
untouched until the evening flip).
"""

import html as _html
import re
import urllib.parse


def esc(s):
    return _html.escape(str(s if s is not None else ""))


STYLE = """
*{box-sizing:border-box;margin:0}
:root{--base:#05140f;--surface:#082d22;--cont:#0b3d2e;--high:#112921;
 --gold:#c9a227;--goldink:#e8c56a;--ink:#e2e8f0;--mut:#a3adab;
 --line:rgba(201,162,39,.18);--glass:rgba(17,41,33,.7);
 --warn:#d97706;--err:#b91c1c;--ok:#2d6a4f}
body{background:var(--base);color:var(--ink);
 font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
 font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--goldink)}
.hg,h1,h2,h3{font-family:'Hanken Grotesk','Inter',sans-serif}
.tab{font-variant-numeric:tabular-nums}
.topnav{display:flex;align-items:center;gap:26px;padding:14px 30px;
 background:var(--surface);border-bottom:1px solid var(--line);
 position:sticky;top:0;z-index:40}
.topnav .brand{font-weight:800;font-size:16px;letter-spacing:.2px;
 color:#fff;text-decoration:none;display:flex;align-items:center;gap:9px}
.topnav .brand span{color:var(--gold)}
.topnav a.t{color:var(--mut);text-decoration:none;font-size:13px;
 font-weight:600;padding:6px 12px;border-radius:999px}
.topnav a.t.on{background:var(--gold);color:#0b3d2e;font-weight:800}
.wrap{max-width:1280px;margin:0 auto;padding:26px 30px 130px}
.pagehead{display:flex;justify-content:space-between;align-items:flex-end;
 flex-wrap:wrap;gap:12px;margin-bottom:20px}
.pagehead h1{font-size:26px;font-weight:800;color:#fff;letter-spacing:-.3px}
.pagehead .sub{color:var(--mut);font-size:13px;margin-top:3px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:10.5px;font-weight:800;letter-spacing:.6px;
 text-transform:uppercase;padding:6px 12px;border-radius:999px;
 border:1px solid var(--line);color:var(--goldink);background:var(--high)}
.chip.warn{border-color:rgba(217,119,6,.5);color:#fbbf24}
.chip.alarm{border-color:rgba(220,60,60,.6);color:#fca5a5}
.grid{display:grid;grid-template-columns:1fr 340px;gap:22px;
 align-items:start}
@media(max-width:960px){.grid{grid-template-columns:1fr}}
.hero{position:relative;border-radius:16px;overflow:hidden;
 border:1px solid var(--line);min-height:340px;background:
 linear-gradient(160deg,#0b3d2e,#082d22)}
.hero img{position:absolute;inset:0;width:100%;height:100%;
 object-fit:cover}
.hero .shade{position:absolute;inset:0;background:
 linear-gradient(to top,rgba(5,20,15,.92) 0%,rgba(5,20,15,.25) 45%,
 rgba(5,20,15,.15) 100%)}
.hero .sched{position:absolute;top:16px;right:16px;background:
 rgba(5,20,15,.75);backdrop-filter:blur(8px);border:1px solid var(--line);
 border-radius:10px;padding:8px 14px;font-size:11px;font-weight:700;
 letter-spacing:.5px;text-transform:uppercase;color:var(--goldink)}
.hero .foot{position:absolute;left:22px;right:22px;bottom:18px}
.hero .lbl{font-size:10px;font-weight:800;letter-spacing:2px;
 text-transform:uppercase;color:rgba(255,255,255,.75)}
.hero .price{font-family:'Hanken Grotesk',sans-serif;font-size:52px;
 font-weight:800;color:var(--gold);line-height:1.05;
 text-shadow:0 0 14px rgba(201,162,39,.45)}
.hero .was{font-size:22px;color:rgba(255,255,255,.45);
 text-decoration:line-through;font-weight:600;margin-left:12px}
.hero .conf{font-size:12px;color:rgba(255,255,255,.75);margin-top:4px}
.rail{display:flex;flex-direction:column;gap:14px}
.card{background:var(--glass);backdrop-filter:blur(12px);
 border:1px solid var(--line);border-radius:16px;overflow:hidden}
.card .hd{display:flex;align-items:center;gap:10px;padding:15px 20px;
 border-bottom:1px solid var(--line)}
.card .hd h3{font-size:13px;font-weight:800;letter-spacing:1.2px;
 text-transform:uppercase;color:#fff}
.card .hd .ico{color:var(--gold);font-size:15px}
.card .hd .right{margin-left:auto;font-size:10px;color:var(--mut);
 letter-spacing:1px;text-transform:uppercase;font-weight:700}
.card .bd{padding:16px 20px}
.spec{display:flex;justify-content:space-between;align-items:center;
 padding:9px 0;border-bottom:1px solid rgba(201,162,39,.08);font-size:13px}
.spec:last-child{border-bottom:none}
.spec .k{color:var(--mut)}
.spec .v{font-weight:800;color:var(--goldink)}
.specnote{margin-top:12px;background:rgba(201,162,39,.06);
 border:1px solid var(--line);border-radius:10px;padding:10px 13px;
 font-size:12px;color:var(--mut);font-style:italic}
.stack{display:flex;flex-direction:column;gap:18px;margin-top:22px}
.histrow{display:flex;justify-content:space-between;align-items:center;
 background:rgba(255,255,255,.03);border:1px solid rgba(201,162,39,.08);
 border-radius:10px;padding:11px 15px;margin-bottom:8px}
.histrow b{font-size:14px}
.histrow .pill{font-size:10.5px;background:var(--high);
 border:1px solid var(--line);border-radius:999px;padding:3px 10px;
 color:var(--mut);margin-left:6px}
.wline{background:rgba(217,119,6,.08);border:1px solid rgba(217,119,6,.3);
 border-radius:10px;padding:11px 14px;font-size:13px;margin-bottom:8px;
 color:#fcd34d}
.wline.info{background:rgba(255,255,255,.03);
 border-color:rgba(201,162,39,.15);color:var(--mut)}
.mults{display:flex;gap:10px;flex-wrap:wrap}
.mult{background:var(--high);border:1px solid var(--line);
 border-radius:12px;padding:12px 16px;min-width:104px;text-align:center}
.mult .k{font-size:9.5px;font-weight:800;letter-spacing:1px;
 text-transform:uppercase;color:var(--mut)}
.mult .v{font-family:'Hanken Grotesk',sans-serif;font-size:17px;
 font-weight:800;margin-top:3px;color:#fff}
.mult .v.gold{color:var(--goldink)}
.mult .s{font-size:10px;color:var(--mut)}
table.bid{width:100%;border-collapse:collapse;font-size:13.5px}
table.bid th{font-size:10px;font-weight:800;letter-spacing:1.2px;
 text-transform:uppercase;color:var(--mut);text-align:left;
 padding:10px 20px;background:rgba(255,255,255,.04)}
table.bid td{padding:13px 20px;border-top:1px solid rgba(201,162,39,.08)}
table.bid td.r,table.bid th.r{text-align:right}
table.bid .svc{font-weight:700;color:#fff}
table.bid .note{font-size:11.5px;color:var(--mut);margin-top:2px}
.bidtotal{display:flex;justify-content:space-between;align-items:center;
 padding:15px 20px;border-top:1px solid var(--line);
 background:rgba(255,255,255,.03)}
.bidtotal .k{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;
 color:var(--mut);font-weight:800}
.bidtotal .v{font-family:'Hanken Grotesk',sans-serif;font-size:22px;
 font-weight:800;color:var(--gold)}
.shots{display:flex;gap:10px;flex-wrap:wrap}
.shots img{height:120px;border-radius:10px;border:1px solid var(--line)}
.btn{display:inline-flex;align-items:center;gap:8px;border-radius:999px;
 padding:10px 20px;font-size:13px;font-weight:800;cursor:pointer;
 border:1px solid var(--line);background:var(--high);color:var(--ink);
 text-decoration:none}
.btn.gold{background:var(--gold);color:#0b3d2e;border-color:var(--gold)}
.btn.ghostred{color:#fca5a5;border-color:rgba(220,60,60,.4);
 background:transparent}
.bubble{background:rgba(255,255,255,.04);border:1px solid
 rgba(201,162,39,.1);border-radius:12px;padding:11px 15px;
 font-size:13px;margin-bottom:9px}
.bubble .who{font-size:10px;font-weight:800;letter-spacing:1px;
 text-transform:uppercase;color:var(--goldink);margin-bottom:3px}
textarea,input[type=text]{width:100%;background:rgba(0,0,0,.35);
 border:1px solid var(--line);border-radius:10px;color:var(--ink);
 padding:11px 14px;font:inherit}
.notechips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.actionbar{position:fixed;left:0;right:0;bottom:0;z-index:50;
 background:rgba(8,45,34,.92);backdrop-filter:blur(14px);
 border-top:1px solid var(--line)}
.actionbar .in{max-width:1280px;margin:0 auto;display:flex;
 align-items:center;gap:14px;padding:13px 30px}
.actionbar .total{margin-left:auto;text-align:right}
.actionbar .total .k{font-size:9.5px;letter-spacing:2px;
 text-transform:uppercase;color:var(--mut);font-weight:800}
.actionbar .total .v{font-family:'Hanken Grotesk',sans-serif;
 font-size:24px;font-weight:800;color:#fff}
.previewtag{position:fixed;top:70px;right:16px;z-index:60;
 background:var(--gold);color:#0b3d2e;font-size:10.5px;font-weight:800;
 letter-spacing:1px;padding:6px 12px;border-radius:999px;
 text-transform:uppercase}
"""


def _spec_row(k, v):
    if v in (None, "", "None"):
        return ""
    return (f"<div class='spec'><span class='k'>{esc(k)}</span>"
            f"<span class='v tab'>{esc(v)}</span></div>")


def render(rec, stamp, hero_url=None, hist=None, must_know="",
           photo_urls=None, links=None):
    """Record dict -> full standalone page (bytes-ready str)."""
    d = rec.get("draft") or {}
    bid = d.get("bid") or {}
    lines = bid.get("services") or []
    notes = bid.get("notes") or []
    pi = d.get("prop_info") or {}
    total = d.get("total") or sum((s.get("price") or 0) for s in lines)
    conf = bid.get("confidence")
    # voicemail records wear the CALLER's name (caller-ID), never the
    # copycall envelope
    name = ((rec.get("caller_id") or {}).get("name")
            or (rec.get("from") or "").split("<")[0].strip()
            or "Customer")
    name = re.sub(r"^☎\s*Voicemail from\s*", "☎ ", name)
    addr = rec.get("address") or ""
    status = rec.get("customer_status") or ""
    back = f"/newbid/{stamp}"

    # edited lines carry the engine's original → the crossed-out price
    orig = sum((s.get("orig_price") or s.get("price") or 0) for s in lines)
    was = (f"<span class='was tab'>${orig:,.0f}</span>"
           if orig and abs(orig - (total or 0)) >= 1 else "")

    chips = ""
    if status:
        chips += f"<span class='chip'>{esc(status)}</span>"
    if rec.get("sched_pref"):
        chips += f"<span class='chip'>📅 {esc(rec['sched_pref'])[:34]}</span>"
    oq = rec.get("open_quote_ctx") or {}
    if oq.get("number"):
        chips += (f"<span class='chip warn'>📋 quote #{esc(oq['number'])} "
                  f"· {esc(oq.get('status') or '')}</span>")
    if rec.get("dns_match"):
        chips += "<span class='chip alarm'>⛔ DO NOT SERVICE</span>"

    hero_img = (f"<img src='{esc(hero_url)}' alt=''>" if hero_url else "")
    sched = (f"<div class='sched'>📅 {esc(rec['sched_pref'])[:40]}</div>"
             if rec.get("sched_pref") else "")

    # ── site specifications rail ──
    aer = pi.get("aerial_surfaces") or {}
    specs = "".join([
        _spec_row("Total Area", f"{pi['sqft']:,} sqft" if pi.get("sqft") else None),
        _spec_row("Stories", pi.get("stories") and f"{pi['stories']} story"),
        _spec_row("Pitch of Roof", pi.get("pitch")),
        _spec_row("Roof Type", pi.get("roof_material")),
        _spec_row("Debris Level", pi.get("debris_read")),
        _spec_row("Basement", pi.get("basement_sqft")
                  and f"{pi['basement_sqft']:,} sqft"),
        _spec_row("Garage", pi.get("garage_sqft")
                  and f"{pi['garage_sqft']:,} sqft"),
        _spec_row("Driveway (aerial)", aer.get("driveway")
                  and f"~{aer['driveway']:,} sqft"),
        _spec_row("Sidewalk (aerial)", aer.get("sidewalk")
                  and f"~{aer['sidewalk']:,} sqft"),
    ])
    alert = rec.get("office_alert") or ""
    specnote = (f"<div class='specnote'>{esc(alert[:220])}</div>"
                if alert else "")

    # ── history ──
    hist_html = ""
    for svc, entries in sorted((hist or {}).items()):
        dated = sorted((e for e in entries if e and e[0]), reverse=True)[:3]
        if not dated:
            continue
        latest = dated[0]
        pills = "".join(f"<span class='pill tab'>{esc(e[0][:7])} · "
                        f"${e[1]:,.0f}</span>" for e in dated[1:])
        hist_html += (f"<div class='histrow'><b>{esc(svc.title())}</b>"
                      f"<span><span class='v tab' style='font-weight:800;"
                      f"color:var(--goldink)'>${latest[1]:,.0f}</span>"
                      f"<span class='pill'>{esc(latest[0][:7])}</span>"
                      f"{pills}</span></div>")
    if not hist_html:
        hist_html = "<div class='wline info'>First visit — no history " \
                    "at this home yet.</div>"

    # ── warnings ──
    warn_html = ""
    for n in notes:
        n = str(n)
        cls = "wline" if (n.startswith("⚠") or "REVIEW" in n
                          or "NOTE:" in n or "🚩" in n) else "wline info"
        warn_html += f"<div class='{cls}'>{esc(n[:300])}</div>"
    if not warn_html:
        warn_html = "<div class='wline info'>No warnings — clean run.</div>"

    # ── how it was priced ──
    def mult(k, v, s="", gold=False):
        if v in (None, "", "None"):
            return ""
        return (f"<div class='mult'><div class='k'>{esc(k)}</div>"
                f"<div class='v{' gold' if gold else ''} tab'>{esc(v)}</div>"
                f"<div class='s'>{esc(s)}</div></div>")
    mults = "".join([
        mult("House size", pi.get("sqft") and f"{pi['sqft']:,}", "sqft"),
        mult("Stories", pi.get("stories") and f"{pi['stories']}-story"),
        mult("Roof pitch", pi.get("pitch"), gold=(pi.get("pitch")
             not in ("mild", None))),
        mult("Debris", pi.get("debris_read") or "standard"),
        mult("Roof", pi.get("roof_material") or "standard"),
    ])

    # ── bid table ──
    rows = ""
    for s in lines:
        price = s.get("price") or 0
        edited = (f"<div class='note'>office edited — system said "
                  f"${s['orig_price']:,.0f}</div>"
                  if s.get("orig_price") not in (None, price) else "")
        rows += (f"<tr><td><div class='svc'>{esc(s.get('name'))}</div>"
                 f"{edited}</td>"
                 f"<td class='r tab'>${price:,.2f}</td></tr>")
    conf_txt = f"confidence {conf}%" if conf is not None else ""

    # ── photos ──
    shots = "".join(f"<a href='{esc(u)}' target='_blank'>"
                    f"<img src='{esc(u)}' loading='lazy'></a>"
                    for u in (photo_urls or [])[:6])
    link_btns = "".join(f"<a class='btn' target='_blank' rel='noopener' "
                        f"href='{esc(u)}'>{esc(t)}</a>"
                        for t, u in (links or []))

    # ── conversation ──
    msg = rec.get("newest_message") or ""
    convo = (f"<div class='bubble'><div class='who'>{esc(name)} · latest"
             f"</div>{esc(msg[:600])}</div>") if msg else \
        "<div class='wline info'>No messages logged.</div>"

    # ── must know ──
    mk = esc(must_know or "")

    q = urllib.parse.quote
    page = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bid Review — {esc(name)}</title>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;700;800&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>{STYLE}</style></head><body>
<div class="previewtag">Preview — new design</div>
<nav class="topnav">
 <a class="brand" href="/">🎩 Master <span>Butler</span></a>
 <a class="t on" href="/">Bids</a>
 <a class="t" href="/scoreboard">Scoreboard</a>
 <a class="t" href="/winback">Win-back</a>
 <a class="t" href="/customers">Customers</a>
</nav>
<div class="wrap">
 <div class="pagehead">
  <div><h1>Review: {esc(name)}</h1>
   <div class="sub">Property: {esc(addr) or '— address not on file —'}</div></div>
  <div class="chips">{chips}</div>
 </div>
 <div class="grid">
  <div>
   <div class="hero">{hero_img}<div class="shade"></div>{sched}
    <div class="foot"><div class="lbl">Estimated total</div>
     <span class="price tab">${(total or 0):,.0f}</span>{was}
     <div class="conf">{esc(conf_txt)}</div></div>
   </div>
   <div class="stack">
    <div class="card"><div class="hd"><span class="ico">🕘</span>
      <h3>History at this home</h3><span class="right">past visits</span></div>
     <div class="bd">{hist_html}</div></div>
    <div class="card"><div class="hd"><span class="ico">⚠️</span>
      <h3>Warnings</h3></div><div class="bd">{warn_html}</div></div>
    <div class="card"><div class="hd"><span class="ico">🧮</span>
      <h3>How it was priced</h3>
      <span class="right">size · multipliers</span></div>
     <div class="bd"><div class="mults">{mults}</div></div></div>
    <div class="card"><div class="hd"><span class="ico">📋</span>
      <h3>Detailed bid breakdown</h3>
      <span class="right">{len(lines)} items</span></div>
     <table class="bid"><tr><th>Service</th><th class="r">Subtotal</th></tr>
      {rows}</table>
     <div class="bidtotal"><span class="k">Calculated subtotal</span>
      <span class="v tab">${(total or 0):,.2f}</span></div></div>
    <div class="card"><div class="hd"><span class="ico">📸</span>
      <h3>Photos &amp; flyover</h3></div>
     <div class="bd"><div class="shots">{shots or
        "<span class='wline info' style='display:block'>no photos yet</span>"}
      </div><div style="display:flex;gap:9px;margin-top:12px;flex-wrap:wrap">
      {link_btns}</div></div></div>
    <div class="card"><div class="hd"><span class="ico">💬</span>
      <h3>Conversation &amp; reply</h3>
      <span class="right">quick responses · ✨ draft</span></div>
     <div class="bd">{convo}
      <textarea rows="3" placeholder="Reply — sending stays locked until Dallon flips it on"></textarea>
      <div style="display:flex;gap:9px;margin-top:10px">
       <button class="btn" type="button">✨ Draft a reply for me</button>
      </div></div></div>
    <div class="card"><div class="hd"><span class="ico">📝</span>
      <h3>Internal field notes</h3><span class="right">office-only</span></div>
     <div class="bd">
      <form method="POST" action="/must_know">
       <input type="hidden" name="stamp" value="{esc(stamp)}">
       <input type="hidden" name="address" value="{esc(addr)}">
       <input type="hidden" name="back" value="{esc(back)}">
       <input type="text" name="text" value="{mk}"
        placeholder="Must Know for this home (gate code, dog…)">
       <div class="notechips">
        <button class="btn" style="padding:7px 14px">Save note</button>
       </div></form></div></div>
   </div>
  </div>
  <div class="rail">
   <div class="card"><div class="hd"><span class="ico">ℹ️</span>
     <h3>Site specifications</h3></div>
    <div class="bd">{specs or "<div class='wline info'>no property data"
     "</div>"}{specnote}</div></div>
  </div>
 </div>
</div>
<div class="actionbar"><div class="in">
 <form method="POST" action="/hold" style="display:inline">
  <input type="hidden" name="stamp" value="{esc(stamp)}">
  <input type="hidden" name="customer" value="{esc(rec.get('from') or '')}">
  <input type="hidden" name="hold_reason" value="office_hold">
  <input type="hidden" name="back" value="{esc(back)}">
  <button class="btn ghostred">⏸ Park it</button></form>
 <form method="POST" action="/flag_review" style="display:inline">
  <input type="hidden" name="stamp" value="{esc(stamp)}">
  <input type="hidden" name="customer" value="{esc(rec.get('from') or '')}">
  <input type="hidden" name="back" value="{esc(back)}">
  <button class="btn">🚩 Escalate</button></form>
 <div class="total"><div class="k">Final bid total</div>
  <div class="v tab">${(total or 0):,.2f}</div></div>
 <form method="POST" action="/review" style="display:inline">
  <input type="hidden" name="stamp" value="{esc(stamp)}">
  <input type="hidden" name="customer" value="{esc(rec.get('from') or '')}">
  <input type="hidden" name="back" value="/?c={q(_bid_key(rec))}">
  <button class="btn gold" name="action" value="approve">
   ✓ Approve — creates DRAFT quote →</button></form>
</div></div>
</body></html>"""
    return page


def _bid_key(rec):
    m = re.search(r"<([^>]+)>", rec.get("from") or "")
    return (m.group(1).lower() if m else "")

"""Stage 9: render the web dashboard.

Generates a single self-contained HTML page (docs/index.html) from the state
files, plus a live-fetch mode that re-reads state JSON from the repo so the
deployed page on GitHub Pages stays current without a rebuild.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import config, jobs, state

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clip Factory - Dashboard</title>
<style>
:root{
  --bg:#0b0f17;--card:#141b26;--card2:#1a2331;--line:#243044;
  --txt:#e6edf6;--muted:#8b9bb4;--acc:#5eead4;--blue:#60a5fa;
  --ok:#34d399;--warn:#fbbf24;--err:#f87171;--run:#a78bfa;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:22px}
header{display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;margin-bottom:20px}
h1{font-size:20px;margin:0;letter-spacing:.3px}
h1 span{color:var(--acc)}
.sub{color:var(--muted);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;background:var(--card);
  border:1px solid var(--line);border-radius:999px;padding:5px 11px;font-size:12px;color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}
.dot.off{background:var(--muted)}.dot.bad{background:var(--err)}.dot.run{background:var(--run)}
button{background:var(--card2);border:1px solid var(--line);color:var(--txt);
  border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--acc);color:var(--acc)}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 16px}
.kpi .lab{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.8px}
.kpi .val{font-size:26px;font-weight:650;margin-top:6px}
.kpi .sub{font-size:11px;margin-top:3px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:17px;margin-bottom:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:11px;
  text-transform:uppercase;letter-spacing:.7px;padding:7px 9px;border-bottom:1px solid var(--line)}
td{padding:9px;border-bottom:1px solid rgba(36,48,68,.55);vertical-align:top}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600}
.t-ok{background:rgba(52,211,153,.14);color:var(--ok)}
.t-run{background:rgba(167,139,250,.16);color:var(--run)}
.t-warn{background:rgba(251,191,36,.14);color:var(--warn)}
.t-err{background:rgba(248,113,113,.15);color:var(--err)}
.t-mute{background:rgba(139,155,180,.14);color:var(--muted)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--muted)}
.hi{color:var(--acc);font-weight:600}
.flow{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch}
.stage{flex:1 1 130px;background:var(--card2);border:1px solid var(--line);
  border-radius:10px;padding:11px 12px;min-width:120px}
.stage .n{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.stage .s{font-size:14px;font-weight:600;margin-top:5px}
.stage .m{font-size:11px;color:var(--muted);margin-top:4px}
.stage.ok{border-left:3px solid var(--ok)}.stage.run{border-left:3px solid var(--run)}
.stage.err{border-left:3px solid var(--err)}.stage.idle{border-left:3px solid var(--line)}
.acct{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.acct .row{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:13px 14px}
.acct .row h3{margin:0 0 9px;font-size:13px}
.acct .kv{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;color:var(--muted)}
.acct .kv b{color:var(--txt);font-weight:500}
.bar{height:7px;background:var(--card2);border-radius:4px;overflow:hidden;margin-top:9px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--blue))}
.empty{color:var(--muted);font-size:13px;padding:14px 0;text-align:center}
.errbox{background:rgba(248,113,113,.07);border:1px solid rgba(248,113,113,.3);
  border-radius:8px;padding:10px 12px;margin-bottom:8px;font-size:12.5px}
.errbox .m{color:var(--err);font-weight:600}
.errbox .d{color:var(--muted);margin-top:3px}
.logline{display:flex;gap:10px;padding:5px 0;border-bottom:1px solid rgba(36,48,68,.5);font-size:12.5px}
.logline:last-child{border:none}
.logline .t{color:var(--muted);font-family:ui-monospace,monospace;font-size:11px;white-space:nowrap}
.two{display:grid;grid-template-columns:1.4fr 1fr;gap:16px}
@media(max-width:860px){.two{grid-template-columns:1fr}}
.spark{display:flex;align-items:flex-end;gap:3px;height:74px;margin-top:8px}
.spark div{flex:1;background:linear-gradient(180deg,var(--acc),rgba(94,234,212,.2));
  border-radius:3px 3px 0 0;min-height:3px;position:relative}
.foot{color:var(--muted);font-size:11.5px;text-align:center;padding:14px 0 4px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Clip <span>Factory</span></h1>
      <div class="sub" id="stamp">loading...</div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap" id="health"></div>
    <button onclick="load(true)">Refresh</button>
  </header>

  <div class="grid kpis" id="kpis"></div>

  <section>
    <h2>Pipeline</h2>
    <div class="flow" id="flow"></div>
  </section>

  <section>
    <h2>Accounts &amp; Connections</h2>
    <div class="acct" id="accounts"></div>
  </section>

  <div class="two">
    <section>
      <h2>Ongoing work</h2>
      <div id="ongoing"></div>
    </section>
    <section>
      <h2>Recent errors</h2>
      <div id="errors"></div>
    </section>
  </div>

  <section>
    <h2>Today's queue</h2>
    <div id="queue"></div>
  </section>

  <div class="two">
    <section>
      <h2>Published &amp; performance</h2>
      <div id="perf"></div>
    </section>
    <section>
      <h2>Activity log</h2>
      <div id="log"></div>
    </section>
  </div>

  <div class="foot" id="foot"></div>
</div>

<script>
const EMBED = __DATA__;
const STAT = ["state/jobs.json","state/queue/__DATE__.json","state/rendered/__DATE__.json",
              "state/posted.json","state/performance.json","state/log.json","state/accounts.json",
              "state/storage.json"];
const BASE = __RAWBASE__;

async function fetchState(){
  if(!BASE) return EMBED;
  const out = {...EMBED};
  await Promise.all(STAT.map(async (p,i)=>{
    try{
      const r = await fetch(BASE+"/"+p+"?t="+Date.now(), {cache:"no-store"});
      if(r.ok){
        const j = await r.json();
        if(i===0) out.jobs=j;
        if(i===1) out.queue=j;
        if(i===2) out.rendered=j;
        if(i===3) out.posted=j;
        if(i===4) out.performance=j;
        if(i===5) out.log=j;
        if(i===6) out.accounts=j;
        if(i===7) out.storage=j;
      }
    }catch(e){}
  }));
  return out;
}

const esc = s => String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = n => (n==null?0:n).toLocaleString();
const ago = t => { if(!t) return "-"; const d=(Date.now()-new Date(t).getTime())/1000;
  if(d<60) return Math.round(d)+"s ago"; if(d<3600) return Math.round(d/60)+"m ago";
  if(d<86400) return Math.round(d/3600)+"h ago"; return Math.round(d/86400)+"d ago"; };
const dur = s => s==null?"-":(s<60?Math.round(s)+"s":Math.round(s/60)+"m "+Math.round(s%60)+"s");

function tag(status){
  const m = {ok:["t-ok","OK"],running:["t-run","RUNNING"],partial:["t-warn","PARTIAL"],
             failed:["t-err","FAILED"],published:["t-ok","PUBLISHED"],
             qa_passed:["t-ok","QA PASS"],rendered:["t-mute","RENDERED"],
             uploaded:["t-mute","UPLOADED"],selected:["t-mute","SELECTED"],
             skipped:["t-warn","SKIPPED"],
             transcribed:["t-mute","TRANSCRIBED"],discovered:["t-mute","DISCOVERED"]}[status]
            || ["t-mute", String(status||"").toUpperCase()];
  return `<span class="tag ${m[0]}">${m[1]}</span>`;
}

function render(d){
  const runs = (d.jobs&&d.jobs.runs)||[];
  const running = runs.filter(r=>r.status==="running");
  const done = runs.filter(r=>r.status!=="running");
  const last = s => done.filter(r=>r.stage===s)[0] || null;
  const q = (d.queue&&d.queue.clips)||[];
  const r = (d.rendered&&d.rendered.clips)||[];
  const posts = (d.posted&&d.posted.posts)||[];
  const perf = (d.performance&&d.performance.clips)||[];
  const log = ((d.log&&d.log.entries)||[]).slice().reverse();
  const errors = log.filter(e=>e.level==="error");
  const acct = d.accounts||{};

  document.getElementById("stamp").textContent =
    "updated " + (d.jobs&&d.jobs.updated ? ago(d.jobs.updated) : "n/a");

  // health pills
  const health = [
    ["API keys", acct._llm_ok!==false && ((acct.llm&&acct.llm.groq)||(acct.llm&&acct.llm.gemini))],
    ["Storage", acct.storage_backend&&acct.storage_backend.configured],
    ["TikTok", acct.tiktok&&acct.tiktok.configured],
    ["Instagram", acct.instagram&&acct.instagram.configured]
  ];
  document.getElementById("health").innerHTML = health.map(([n,ok])=>
    `<span class="pill"><span class="dot ${ok?"":"bad"}"></span>${n}</span>`).join("");

  // KPIs
  const views = perf.reduce((a,c)=>a+Object.values(c.platforms||{})
      .reduce((b,p)=>b+(p.views||p.plays||0),0),0);
  const todayQ = q.length, needRender = q.filter(i=>i.stage==="selected").length;
  const kpis = [
    ["Clips today", todayQ, `${needRender} awaiting render`],
    ["Rendered", r.length, `${r.filter(c=>c.stage==="qa_passed").length} awaiting upload`],
    ["Published", posts.length, "across all platforms"],
    ["Total views", num(views), "from platform APIs"],
    ["In progress", running.length, running.length?"stages running":"idle"],
    ["Errors", errors.length, errors.length?"see log":"none"]
  ];
  document.getElementById("kpis").innerHTML = kpis.map(([l,v,s])=>
    `<div class="kpi"><div class="lab">${l}</div><div class="val">${v}</div><div class="sub sub">${s}</div></div>`).join("");

  // flow
  const order = [["discover","Discover"],["transcribe","Transcribe"],["select","Select"],
    ["render","Render"],["qa","QA"],["upload","Upload"],["publish","Publish"],
    ["analytics","Stats"],["cleanup","Cleanup"]];
  document.getElementById("flow").innerHTML = order.map(([key,label])=>{
    const isRun = running.some(r=>r.stage===key);
    const l = last(key);
    let cls = "idle", s = "waiting", m = "no runs yet";
    if(isRun){ cls="run"; s="running"; m="in progress..."; }
    else if(l){ cls = l.status==="ok"?"ok":(l.status==="failed"?"err":"run");
      s = l.status; m = `${l.items==null?"":l.items+" items"} · ${ago(l.started)}`; }
    return `<div class="stage ${cls}"><div class="n">${label}</div><div class="s">${s}</div><div class="m">${esc(m)}</div></div>`;
  }).join("");

  // accounts
  const cards = [];
  const ig = acct.instagram||{}, fb = acct.facebook||{}, tt = acct.tiktok||{};
  cards.push(card("Instagram Reels", ig.configured, [
    ["App", ig.configured?"connected":"not configured"],
    ["Business ID", ig.user_id?"set":"missing"],
    ["Posts today", posts.filter(p=>p.platform==="instagram").length]
  ]));
  cards.push(card("Facebook Reels", fb.configured, [
    ["Page token", fb.configured?"connected":"not configured"],
    ["Page ID", fb.enabled?"set":"disabled"],
    ["Posts today", posts.filter(p=>p.platform==="facebook").length]
  ]));
  cards.push(card("TikTok", tt.configured, [
    ["OAuth", tt.configured?"authorised":"not authorised"],
    ["Last refresh", tt.last_refresh?ago(tt.last_refresh):"-"],
    ["Mode", (d.accounts&&d.accounts.tiktok&&d.accounts.tiktok.draft_mode)?"draft":"direct post"],
    ["Posts today", posts.filter(p=>p.platform==="tiktok").length]
  ]));
  const st = acct.storage_backend||{};
  cards.push(card("Storage - "+(st.configured?(st.backend||"ready"):"not configured"), !!(st.configured), [
    ["Bucket / folder", st.bucket||"-"],
    ["Public URLs", st.public?"ready":"-"],
    ["Clips stored", r.filter(c=>c.r2_url).length],
    ["Local work", (d.storage&&d.storage.work_mb!=null)?d.storage.work_mb+" MB":"-"],
    ["Freed last run", (d.storage&&d.storage.freed_mb!=null)?d.storage.freed_mb+" MB":"-"],
    ["Remote pruned", (d.storage&&d.storage.remote_deleted!=null)?(d.storage.remote_deleted+" ("+d.storage.remote_days+"d)"):"-"]
  ]));
  const llm = acct.llm||{};
  cards.push(card("LLM / Transcription", !!(llm.groq||llm.gemini), [
    ["Groq", llm.groq?"key set":"-"],
    ["Gemini", llm.gemini?"key set":"-"]
  ]));
  cards.push(card("Campaigns", true, [
    ["Enabled", (d.queue&&d.queue.campaigns||[]).length],
    ["Sources today", todayQ]
  ]));
  document.getElementById("accounts").innerHTML = cards.join("");

  // ongoing
  const o = document.getElementById("ongoing");
  if(!running.length){ o.innerHTML = `<div class="empty">Nothing running right now.<br>Next scheduled run drives the flow above.</div>`; }
  else o.innerHTML = `<table><thead><tr><th>Stage</th><th>Started</th><th>Running</th><th>Detail</th></tr></thead><tbody>` +
    running.map(x=>`<tr><td>${tag("running")} <b>${esc(x.stage)}</b></td>
      <td class="mono">${ago(x.started)}</td><td class="mono">${dur((Date.now()-new Date(x.started).getTime())/1000)}</td>
      <td class="mono">${esc(JSON.stringify(x.meta||{})).slice(0,80)}</td></tr>`).join("") + "</tbody></table>";

  // errors
  const eb = document.getElementById("errors");
  if(!errors.length) eb.innerHTML = `<div class="empty">No errors. Nice.</div>`;
  else eb.innerHTML = errors.slice(0,8).map(e=>
    `<div class="errbox"><div class="m">${esc(e.stage)} — ${esc(e.message)}</div>
     <div class="d mono">${esc(e.t)}${e.reason?" · "+esc(e.reason):""}</div></div>`).join("");

  // queue table
  const qb = document.getElementById("queue");
  if(!q.length) qb.innerHTML = `<div class="empty">No sources discovered yet.</div>`;
  else qb.innerHTML = `<table><thead><tr><th>Source</th><th>Campaign</th><th>Window</th><th>Stage</th><th>Clips</th><th>Duration</th></tr></thead><tbody>` +
    q.map(i=>`<tr><td class="mono" style="max-width:340px;overflow:hidden;text-overflow:ellipsis" title="${esc(i.source_title||i.source_url)}">${esc(i.source_title||i.source_url)}</td>
      <td>${esc(i.campaign||"-")}</td>
      <td class="mono">${esc(i.source_section||"auto")}${i.auto_section?" *":""}</td>
      <td>${tag(i.stage)}</td>
      <td>${(i.clips||[]).length}</td><td class="mono">${i.duration?Math.round(i.duration)+"s":"-"}</td></tr>`).join("") + "</tbody></table>";

  // performance
  const pb = document.getElementById("perf");
  const rows = perf.map(c=>{
    const pl = Object.entries(c.platforms||{});
    const tv = pl.reduce((a,[,p])=>a+(p.views||p.plays||0),0);
    const tl = pl.reduce((a,[,p])=>a+(p.likes||0),0);
    return {id:c.clip_id, camp:c.campaign, plats:pl.map(([k])=>k).join(", "), tv, tl};
  }).sort((a,b)=>b.tv-a.tv);
  let html = rows.length ? `<table><thead><tr><th>Clip</th><th>Platforms</th><th>Views</th><th>Likes</th></tr></thead><tbody>` +
    rows.slice(0,10).map(x=>`<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.plats)}</td>
      <td class="hi">${num(x.tv)}</td><td>${num(x.tl)}</td></tr>`).join("") + "</tbody></table>"
    : `<div class="empty">Stats appear once posts are published and analytics runs.</div>`;
  const daily = (d.performance&&d.performance.daily)||{};
  const days = Object.keys(daily).sort().slice(-14);
  if(days.length){
    const max = Math.max(...days.map(k=>daily[k].views||0),1);
    html += `<div class="sub" style="margin-top:16px">Views last ${days.length} day(s)</div><div class="spark">` +
      days.map(k=>`<div style="height:${Math.max(4,Math.round((daily[k].views||0)/max*100))}%" title="${k}: ${num(daily[k].views)} views"></div>`).join("") + "</div>";
  }
  pb.innerHTML = html;

  // log
  const lb = document.getElementById("log");
  lb.innerHTML = log.length ? log.slice(0,18).map(e=>
    `<div class="logline"><span class="t">${esc((e.t||"").slice(11,19))}</span>
     <span class="tag ${e.level==="error"?"t-err":e.level==="warn"?"t-warn":"t-mute"}">${esc(e.level)}</span>
     <span>${esc(e.stage)} — ${esc(e.message)}</span></div>`).join("")
    : `<div class="empty">No activity recorded yet.</div>`;

  document.getElementById("foot").textContent =
    `state source: ${BASE?"live from repository":"embedded snapshot"} · renders from queue/rendered/posted/performance/jobs/log`;
}

function card(name, ok, rows){
  return `<div class="row"><h3>${esc(name)} <span class="tag ${ok?"t-ok":"t-err"}">${ok?"OK":"OFF"}</span></h3>` +
    rows.map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("") + "</div>";
}

async function load(force){
  try{ render(await fetchState()); }
  catch(e){ document.getElementById("stamp").textContent = "failed to load state: "+e; }
}
load();
setInterval(load, 60000);
</script>
</body>
</html>
"""


def collect() -> dict:
    jobs_data = state.read_json(config.STATE / "jobs.json", {"runs": []})
    return {
        "generated": state.now(),
        "jobs": jobs_data,
        "queue": state.load_queue(),
        "rendered": state.load_rendered(),
        "posted": state.load_posted(),
        "performance": state.load_performance(),
        "log": state.load_log(),
        "accounts": _accounts_snapshot(),
        "storage": state.read_json(config.STATE / "storage.json", {}),
    }


def _accounts_snapshot() -> dict:
    try:
        from . import tokens

        snap = tokens.account_health()
    except Exception:
        snap = {}
    # Storage backend (S3/MinIO, Nextcloud, or R2) - whichever is configured.
    try:
        from . import storage

        snap["storage_backend"] = storage.status()
    except Exception as exc:
        snap["storage_backend"] = {"backend": "unknown", "configured": False,
                                   "error": str(exc)[:200]}
    campaigns = config.campaigns()
    snap["campaigns"] = [c.get("id") for c in campaigns.get("campaigns", []) if c.get("enabled")]
    # Persist for the live-fetch mode on GitHub Pages
    state.write_json(config.STATE / "accounts.json", snap)
    return snap


def run(out_path: pathlib.Path | None = None, raw_base: str | None = None) -> pathlib.Path:
    # Collect BEFORE starting our own job, so the snapshot never shows the
    # dashboard itself as permanently "running".
    data = collect()
    date = data["queue"].get("date", state.today())
    job = jobs.start("dashboard")

    # Allow live updates from Pages by pointing at raw.githubusercontent.com
    if raw_base is None:
        repo = config.env("GITHUB_REPOSITORY", "")
        branch = config.env("GITHUB_REF_NAME", "main")
        raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}" if repo else ""

    html = (
        PAGE
        .replace("__DATA__", json.dumps(data, ensure_ascii=False))
        .replace("__DATE__", date)
        .replace("__RAWBASE__", json.dumps(raw_base))
    )

    out = out_path or (config.DOCS / "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    # Also keep a machine-readable snapshot for external monitoring
    state.write_json(config.DOCS / "state.json", data)

    jobs.finish(job, status="ok", items=1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the dashboard")
    parser.add_argument("--out", type=pathlib.Path, default=None)
    parser.add_argument("--raw-base", default=None,
                        help="base URL for live state fetch (defaults to raw.githubusercontent)")
    args = parser.parse_args(argv)
    path = run(out_path=args.out, raw_base=args.raw_base)
    print(f"dashboard written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

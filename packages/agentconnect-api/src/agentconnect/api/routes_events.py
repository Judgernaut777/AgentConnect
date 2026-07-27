"""The ecosystem event bus HTTP surface (docs/EVENT_BUS.md).

`GET /events` replays/polls the canonical, append-only `event_log`; `GET
/events/stream` is its live SSE tail; `GET /observe/tree` is the live
manager -> worker -> subagent hierarchy (§8); `GET /observe` is the
self-contained operator HTML page that renders it. All three JSON routes are
operator-plane (`list_events`/`observe_tree` in `OPERATOR_ACTIONS`,
sessions.py): the ledger this reads is fleet-wide, not scoped to one
manager/reviewer token's task binding — the same posture as
`list_sessions`/`audit_task`. `GET /observe` itself is PUBLIC (see
`authz.PUBLIC_ROUTES`) because the constant HTML string it returns names no
task and carries no ledger content — every data call its inline JS makes
carries the caller's own bearer token to the real, protected routes above.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from agentconnect.core.errors import InvalidRequest
from agentconnect.core.observability.model import EventType
from agentconnect.core.service import AgentConnectService

from .routes_tasks import service

router = APIRouter(tags=["events"])

#: The full wire vocabulary, for `?type=` validation. Built once from the
#: canonical enum (docs/EVENT_BUS.md §4) rather than duplicated here.
_VALID_TYPES = frozenset(t.value for t in EventType)

#: How many consecutive empty polls between SSE keepalive comments.
_KEEPALIVE_EVERY = 15


def _parse_types(raw: Optional[str]) -> Optional[list[str]]:
    """`?type=a,b` -> `["a", "b"]`, or `None` when omitted. An unknown type
    name is a 400 (`InvalidRequest`), not a silently-empty filter — a typo in
    a consumer's filter should fail loudly, not just return nothing forever."""
    if not raw:
        return None
    types = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = sorted(set(types) - _VALID_TYPES)
    if unknown:
        raise InvalidRequest(f"unknown event type(s): {', '.join(unknown)}")
    return types


@router.get("/events")
def list_events(
    request: Request, since: int = 0, limit: int = 100,
    type: Optional[str] = None, task_id: Optional[str] = None,  # noqa: A002
    outcome: Optional[str] = None,
) -> dict[str, Any]:
    """Replay-from-`since` / poll surface. `since` is EXCLUSIVE (`seq >
    since`) — a consumer resumes with the last `seq` it saw, never with
    arithmetic on it: `seq` is monotonic but not necessarily dense."""
    svc = service(request)
    types = _parse_types(type)
    events = svc.list_bus_events(
        since=since, limit=limit, types=types, task_id=task_id, outcome=outcome,
    )
    return {"events": events, "latest_seq": svc.latest_bus_seq()}


def sse_lines(
    svc: AgentConnectService, cursor: int, types: Optional[list[str]], interval: float,
    *, should_stop: Optional[Callable[[], bool]] = None,
) -> Iterator[str]:
    """The generator `GET /events/stream` serves — factored out as a plain,
    directly-testable function (no FastAPI/ASGI/httpx transport involved) so
    its framing, resume, and keepalive behaviour can be unit-tested without an
    open-ended live HTTP connection.

    Production usage passes `should_stop=None`: the loop only ever ends via
    `GeneratorExit` when the client disconnects (Starlette closes the
    generator, which unwinds this `while True` cleanly — no special handling
    needed). A test passes a `should_stop` predicate (e.g. "stop after N
    frames") to get a bounded, deterministic run instead.
    """
    yield "retry: 3000\n\n"
    idle = 0
    while should_stop is None or not should_stop():
        rows = svc.list_bus_events(since=cursor, limit=200, types=types)
        if rows:
            idle = 0
            for row in rows:
                cursor = row["seq"]
                yield (
                    f"id: {row['seq']}\n"
                    f"event: {row['type']}\n"
                    f"data: {json.dumps(row, default=str)}\n\n"
                )
        else:
            idle += 1
            if idle >= _KEEPALIVE_EVERY:
                idle = 0
                yield ": keepalive\n\n"
        time.sleep(interval)


@router.get("/events/stream")
def stream_events(
    request: Request, since: Optional[int] = None, type: Optional[str] = None,  # noqa: A002
) -> StreamingResponse:
    """Live SSE tail. `since` (query) takes priority over a `Last-Event-ID`
    resume header, which takes priority over "start from the current tail"
    (a fresh live-only subscriber). Auth is the ordinary app-level bearer
    token — an `EventSource` cannot set that header, so this surface is for
    programmatic consumers (`curl -N -H "Authorization: Bearer …" …`); the
    HTML operator page (a later addition) fetch-polls `/events` instead.
    """
    svc = service(request)
    types = _parse_types(type)
    if since is not None:
        cursor = since
    else:
        last_event_id = request.headers.get("last-event-id")
        try:
            cursor = int(last_event_id) if last_event_id else svc.latest_bus_seq()
        except ValueError:
            cursor = svc.latest_bus_seq()
    #: Injectable for tests (`app.state.sse_poll_interval` /
    #: `app.state.sse_should_stop`); production defaults are a real 1-second
    #: tail cadence and an unbounded stream (ends only on client disconnect).
    #: The stop seam exists because an in-process TestClient cannot safely
    #: half-close an infinite streaming response — a bounded run is the only
    #: deterministic way to drive this route end-to-end in a test.
    interval = getattr(request.app.state, "sse_poll_interval", 1.0)
    should_stop = getattr(request.app.state, "sse_should_stop", None)

    return StreamingResponse(
        sse_lines(svc, cursor, types, interval, should_stop=should_stop),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/observe/tree")
def observe_tree(
    request: Request, task_id: Optional[str] = None, include_terminal: int = 0,
) -> dict[str, Any]:
    """The live manager -> worker -> subagent hierarchy (docs/EVENT_BUS.md §8),
    privacy-redacted per node at serialization time. `include_terminal=1`
    widens the default (open work only, all tasks) to also include finished
    tasks — a 404 is only possible when `task_id` names a task that does not
    exist at all, not one that is merely terminal."""
    svc = service(request)
    return svc.observe_tree(task_id=task_id, include_terminal=bool(include_terminal))


#: The self-contained operator page (docs/EVENT_BUS.md §8). Zero external
#: assets — no CDN script, no web font, no remote image — inline CSS/JS only,
#: the same constraint `queue_web._PAGE` (agentconnect-router) already follows.
#: Every prefix in `_TREE_REFRESH_PREFIXES` below is duplicated in the page's
#: own JS (a browser cannot import a Python module), so a change to one must
#: be mirrored in the other — call out in review, not enforced by a shared
#: constant, because the page ships as a literal string.
_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentConnect · Observability</title>
<style>
 :root{--bg:#fff;--fg:#111;--muted:#666;--border:#ddd;--head:#f3f4f6;--code:#f3f4f6;
   --accent:#2563eb;--ok:#16a34a;--bad:#dc2626;--warn:#d97706}
 @media (prefers-color-scheme: dark){
   :root{--bg:#0f1115;--fg:#e5e7eb;--muted:#9ca3af;--border:#2a2e37;--head:#1a1d24;
     --code:#1a1d24;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}
 }
 :root[data-theme="dark"]{--bg:#0f1115;--fg:#e5e7eb;--muted:#9ca3af;--border:#2a2e37;
   --head:#1a1d24;--code:#1a1d24;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}
 :root[data-theme="light"]{--bg:#fff;--fg:#111;--muted:#666;--border:#ddd;--head:#f3f4f6;
   --code:#f3f4f6;--accent:#2563eb;--ok:#16a34a;--bad:#dc2626;--warn:#d97706}
 *{box-sizing:border-box} body{font:14px/1.5 system-ui,sans-serif;margin:0;padding:0;
   background:var(--bg);color:var(--fg)}
 header{display:flex;align-items:center;gap:.75rem;padding:.6rem 1rem;border-bottom:1px solid var(--border);
   flex-wrap:wrap}
 header h1{font-size:1rem;margin:0;white-space:nowrap}
 input{font:inherit;padding:.3rem .5rem;border:1px solid var(--border);border-radius:6px;
   background:var(--bg);color:var(--fg)}
 .dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--muted);display:inline-block}
 .dot.ok{background:var(--ok)} .dot.bad{background:var(--bad)}
 .muted{color:var(--muted)} .spacer{flex:1}
 main{display:flex;gap:0;height:calc(100vh - 3rem)}
 #tree{flex:1 1 55%;overflow:auto;padding:.75rem 1rem;border-right:1px solid var(--border)}
 #side{flex:1 1 45%;display:flex;flex-direction:column;min-width:0}
 #detail{flex:1 1 auto;overflow:auto;padding:.75rem 1rem}
 #ticker{flex:0 0 auto;max-height:30%;overflow:auto;border-top:1px solid var(--border);
   padding:.5rem 1rem;font-size:.8rem}
 details{margin:.15rem 0} summary{cursor:pointer;padding:.15rem .3rem;border-radius:4px;
   list-style:none} summary::-webkit-details-marker{display:none}
 summary:hover{background:var(--head)} summary.sel{outline:1px solid var(--accent)}
 .badge{display:inline-block;padding:.05rem .4rem;border-radius:8px;font-size:.75rem;
   margin-left:.35rem;border:1px solid var(--border)}
 .b-queued,.b-prepared,.b-open{color:var(--muted)}
 .b-running,.b-in_progress,.b-working,.b-claimed{color:var(--accent);border-color:var(--accent)}
 .b-succeeded,.b-completed,.b-done{color:var(--ok);border-color:var(--ok)}
 .b-failed,.b-cancelled,.b-rejected{color:var(--bad);border-color:var(--bad)}
 .b-needs_approval,.b-blocked{color:var(--warn);border-color:var(--warn)}
 .kind{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.03em}
 pre{white-space:pre-wrap;word-break:break-word;background:var(--code);padding:.6rem;
   border-radius:6px;font-size:.8rem}
 table{border-collapse:collapse;width:100%;font-size:.85rem} td{padding:.2rem .4rem;
   vertical-align:top} td.k{color:var(--muted);white-space:nowrap;width:9rem}
 .ev{border-bottom:1px dotted var(--border);padding:.15rem 0}
 code{background:var(--code);padding:.05rem .3rem;border-radius:4px}
</style></head><body>
<header>
 <h1>AgentConnect Observability</h1>
 <span class="dot" id="dot"></span>
 <span class="muted" id="seq">seq —</span>
 <span class="spacer"></span>
 <label class="muted">token <input id="tok" size="28"></label>
</header>
<main>
 <div id="tree"><p class="muted">Loading…</p></div>
 <div id="side">
  <div id="detail"><p class="muted">Click a node to see its detail.</p></div>
  <div id="ticker"><p class="muted">Events will appear here.</p></div>
 </div>
</main>
<script>
(function(){
"use strict";
var tokEl=document.getElementById('tok');
tokEl.value=localStorage.getItem('actok')||'';
tokEl.oninput=function(){localStorage.setItem('actok', tokEl.value);};
var dot=document.getElementById('dot'), seqEl=document.getElementById('seq');
var treeEl=document.getElementById('tree'), detailEl=document.getElementById('detail');
var tickerEl=document.getElementById('ticker');
var nodesById={}, lastSeq=0, ticks=[], selected=null, pollMs=2000, fails=0;
//: Event-type prefixes that mean "the tree may have changed, refetch it".
//: Kept in sync with the docstring above `_PAGE` in routes_events.py.
var REFRESH_PREFIXES=['task.','subtask.','worker.','session.','review.','state.'];

function esc(s){return (s===null||s===undefined?'':s+'').replace(/[&<>]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function H(){var h={}; if(tokEl.value) h['Authorization']='Bearer '+tokEl.value; return h;}
function fmtT(s){if(s===null||s===undefined) return '—'; var n=Math.round(s);
  if(n<60) return n+'s'; if(n<3600) return Math.floor(n/60)+'m'+(n%60)+'s';
  return Math.floor(n/3600)+'h'+Math.floor((n%3600)/60)+'m';}

async function api(path){
  var r=await fetch(path, {headers:H()});
  if(r.status===401||r.status===403){throw new Error('auth:'+r.status);}
  if(!r.ok){throw new Error('http:'+r.status);}
  return r.json();
}

function nodeLine(n){
  var kids=(n.children||[]).map(nodeLine).join('');
  var badge='<span class="badge b-'+esc(n.state)+'">'+esc(n.state)+'</span>';
  var bits=[esc(n.kind)];
  if(n.model) bits.push(esc(n.model));
  bits.push(fmtT(n.elapsed_s));
  nodesById[n.kind+':'+n.id]=n;
  var summary='<summary data-key="'+esc(n.kind+':'+n.id)+'">'+
    '<span class="kind">'+esc(n.kind)+'</span> '+esc(n.title||n.id)+badge+
    ' <span class="muted">'+bits.slice(1).join(' · ')+'</span></summary>';
  return '<details open>'+summary+(kids?('<div style="margin-left:1.1rem">'+kids+'</div>'):'')+'</details>';
}

function renderDetail(n){
  if(!n){detailEl.innerHTML='<p class="muted">Click a node to see its detail.</p>';return;}
  var rows=[
    ['kind', n.kind], ['id', n.id], ['state', n.state], ['privacy_tier', n.privacy_tier],
    ['actor', n.actor], ['model', n.model], ['current_tool', n.current_tool],
    ['tokens', n.tokens?('in '+n.tokens.input+' / out '+n.tokens.output):null],
    ['cost_usd', n.cost_usd], ['elapsed_s', fmtT(n.elapsed_s)],
    ['started_at', n.started_at], ['delegation_id', n.delegation_id],
    ['parent_delegation_id', n.parent_delegation_id],
  ];
  if(n.lease){rows.push(['lease.holder', n.lease.holder], ['lease.expires_at', n.lease.expires_at],
    ['lease.fence', n.lease.fence]);}
  var html='<table>'+rows.filter(function(r){return r[1]!==null && r[1]!==undefined;})
    .map(function(r){return '<tr><td class="k">'+esc(r[0])+'</td><td>'+esc(r[1])+'</td></tr>';}).join('')+
    '</table>';
  if(n.prompt){html+='<p class="muted">prompt</p><pre>'+esc(n.prompt)+'</pre>';}
  if(n.artifacts && n.artifacts.length){
    html+='<p class="muted">artifacts</p><table>'+n.artifacts.map(function(a){
      return '<tr><td class="k">'+esc(a.id)+'</td><td>'+esc(a.name)+'</td></tr>';}).join('')+'</table>';
  }
  detailEl.innerHTML=html;
}

treeEl.addEventListener('click', function(ev){
  var s=ev.target.closest('summary'); if(!s) return;
  var prev=treeEl.querySelector('summary.sel'); if(prev) prev.classList.remove('sel');
  s.classList.add('sel');
  selected=s.getAttribute('data-key');
  renderDetail(nodesById[selected]);
});

function walkCount(n){return 1+(n.children||[]).reduce(function(a,c){return a+walkCount(c);},0);}

async function refreshTree(){
  var data=await api('/observe/tree');
  nodesById={};
  treeEl.innerHTML = data.roots.length ?
    data.roots.map(nodeLine).join('') : '<p class="muted">No open tasks.</p>';
  if(selected && nodesById[selected]){
    var s=treeEl.querySelector('summary[data-key="'+selected.replace(/"/g,'')+'"]');
    if(s) s.classList.add('sel');
    renderDetail(nodesById[selected]);
  }
  if(typeof data.latest_seq==='number' && data.latest_seq>lastSeq) lastSeq=data.latest_seq;
  seqEl.textContent='seq '+lastSeq;
}

function tick(type, ts){
  ticks.unshift({type:type, ts:ts});
  ticks=ticks.slice(0,50);
  tickerEl.innerHTML=ticks.map(function(t){
    return '<div class="ev"><code>'+esc(t.type)+'</code> <span class="muted">'+
      esc(new Date(t.ts*1000).toLocaleTimeString())+'</span></div>';}).join('');
}

async function poll(){
  try{
    var data=await api('/events?since='+lastSeq+'&limit=200');
    var refresh=false;
    (data.events||[]).forEach(function(e){
      tick(e.type, e.ts);
      if(REFRESH_PREFIXES.some(function(p){return e.type.indexOf(p)===0;})) refresh=true;
    });
    if(typeof data.latest_seq==='number') lastSeq=Math.max(lastSeq, data.latest_seq);
    seqEl.textContent='seq '+lastSeq;
    dot.className='dot ok'; fails=0; pollMs=2000;
    if(refresh || !nodesById || Object.keys(nodesById).length===0) await refreshTree();
  }catch(e){
    fails++;
    dot.className='dot bad';
    var msg=String(e.message||e);
    if(msg.indexOf('auth:')===0){
      seqEl.textContent='unauthorized — set a token?';
    }
    pollMs=Math.min(10000, 2000*Math.pow(2, Math.min(fails,3)));
  }
  setTimeout(poll, pollMs);
}

refreshTree().catch(function(){});
poll();
})();
</script>
</body></html>"""


@router.get("/observe", response_class=HTMLResponse)
def observe_page() -> str:
    """The self-contained live view (docs/EVENT_BUS.md §8). Public route (see
    `authz.PUBLIC_ROUTES`): the page itself carries no ledger data, only inline
    JS that fetch-polls the protected `/events` and `/observe/tree` routes
    using whatever bearer token the operator types into the page. Not an
    `EventSource`/SSE page on purpose — a browser `EventSource` cannot set an
    `Authorization` header, so this page fetch-polls instead; `GET
    /events/stream` remains available for programmatic consumers that can set
    headers (`curl -N -H "Authorization: Bearer …" …`)."""
    return _PAGE

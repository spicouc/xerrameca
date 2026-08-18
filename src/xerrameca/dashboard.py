from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .node.events import EventStore
from .node.identity import load_node_state
from .node.supervisor import LocalSupervisor


DASHBOARD_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Xerrameca Dashboard</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
header{padding:1rem 1.25rem;border-bottom:1px solid #333;position:sticky;top:0;background:#111}
main{padding:1rem 1.25rem;max-width:1100px;margin:auto}
.card{border:1px solid #333;border-radius:12px;padding:1rem;margin:.75rem 0;background:#181818}
.row{display:flex;gap:1rem;flex-wrap:wrap}.metric{min-width:150px}
small,.muted{color:#aaa}.warn{color:#ffcf66}.ok{color:#8de28d}
pre{white-space:pre-wrap;word-break:break-word;background:#101010;padding:.75rem;border-radius:8px;overflow:auto}
button{background:#242424;color:#eee;border:1px solid #444;border-radius:8px;padding:.45rem .7rem}
</style>
</head>
<body>
<header><strong>Xerrameca Dashboard</strong> <span id=\"node\" class=\"muted\"></span></header>
<main>
<div class=\"row\"><div class=\"card metric\">Conversations<br><strong id=\"count\">0</strong></div><div class=\"card metric\">Warnings<br><strong id=\"warnings\">0</strong></div></div>
<div id=\"conversations\"></div>
</main>
<script>
async function refresh(){
 const s=await fetch('/api/summary').then(r=>r.json());
 document.getElementById('node').textContent=s.node.display_name+' · '+s.node.node_id;
 document.getElementById('count').textContent=s.conversations.length;
 document.getElementById('warnings').textContent=s.warning_count;
 const root=document.getElementById('conversations'); root.innerHTML='';
 for(const item of s.conversations){
   const c=item.conversation, m=item.metrics, f=item.findings;
   const div=document.createElement('div'); div.className='card';
   const last=(c.messages||[]).slice(-1)[0];
   div.innerHTML='<strong>'+esc(c.name||'Xerrameca')+'</strong> <span class=\"muted\">'+esc(c.id)+'</span><br>'+
    '<span class=\"'+(c.status==='active'?'ok':'muted')+'\">'+esc(c.status)+'</span> · round '+c.current_round+'/'+c.max_rounds+
    ' · epoch '+c.coordinator_epoch+'<br><small>'+esc(c.objective||'')+'</small><br>'+
    (last?'<p>'+esc(last.author_node_id||last.author_id||'agent')+': '+esc(last.content||'')+'</p>':'')+
    (f.length?'<p class=\"warn\">'+f.map(x=>esc(x.code)).join(' · ')+'</p>':'')+
    '<small>latency p50 '+m.response_latency.p50_seconds+'s · p95 '+m.response_latency.p95_seconds+'s · events '+m.event_count+'</small>'+
    '<p><button onclick=\"openConversation(\''+c.id+'\')\">Timeline</button></p><div id=\"d-'+c.id+'\"></div>';
   root.appendChild(div);
 }
}
function esc(v){return String(v??'').replace(/[&<>\"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[m]));}
async function openConversation(id){const d=await fetch('/api/conversations/'+encodeURIComponent(id)).then(r=>r.json());document.getElementById('d-'+id).innerHTML='<pre>'+esc(JSON.stringify(d.events,null,2))+'</pre>';}
refresh(); setInterval(refresh,5000);
</script>
</body></html>"""


def create_dashboard_app(state_dir: str | Path) -> FastAPI:
    """Create an optional read-only dashboard over one node's durable state.

    The dashboard does not participate in federation and owns no runtime state.
    It is intended to bind to loopback unless placed behind an authenticated
    reverse proxy.
    """

    state = load_node_state(state_dir)
    supervisor = LocalSupervisor(state.state_dir)
    store = EventStore(state.state_dir)
    app = FastAPI(title="Xerrameca Dashboard", version="1")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "xerrameca-dashboard", "node_id": state.node_id}

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        conversations = supervisor.inspect_all()
        warning_count = sum(
            1
            for item in conversations
            for finding in item["findings"]
            if finding["severity"] == "warning"
        )
        return {
            "node": state.public_dict(),
            "warning_count": warning_count,
            "conversations": conversations,
        }

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str) -> dict[str, Any]:
        inspected = supervisor.inspect(conversation_id)
        inspected["events"] = [
            event.to_dict() for event in store.list_events(conversation_id)
        ]
        return inspected

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    return app

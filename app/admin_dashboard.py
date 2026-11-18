import hmac
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.db import connection

router = APIRouter()
_basic = HTTPBasic(auto_error=False)

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#101827"><title>EPUB Import Monitor | FiqhBooks</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#111c2e;--panel2:#16243a;--line:#26364e;--muted:#91a2bb;--text:#edf3fb;--blue:#72a7ff;--green:#48d6a0;--amber:#f4bd62;--red:#ff7d87}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 15% -15%,#1d3557 0,transparent 42%),var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1280px;margin:0 auto;padding:34px 24px 60px}.top{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:28px}.eyebrow{color:var(--blue);font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.top h1{font-size:28px;letter-spacing:-.04em;margin:5px 0}.sub{color:var(--muted)}button,input{font:inherit}.btn{border:1px solid var(--line);background:#1a2a42;color:var(--text);border-radius:9px;padding:9px 14px;cursor:pointer;font-weight:650}.btn:hover{border-color:#5b83bb;background:#203653}.btn:disabled{opacity:.45;cursor:not-allowed}.btn.primary{background:#285da8;border-color:#3976ca}.btn.danger{color:#ffabb1;border-color:#713d4b;background:#38202d}.stats{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:18px}.stat,.panel{background:linear-gradient(145deg,#142137,#101a2a);border:1px solid var(--line);border-radius:14px}.stat{padding:17px}.stat span{display:block;color:var(--muted);font-size:12px;font-weight:650}.stat strong{display:block;font-size:27px;letter-spacing:-.04em;margin-top:6px}.stat.failed strong{color:var(--red)}.stat.imported strong{color:var(--green)}.panel{padding:18px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.toolbar h2{font-size:16px;margin:0}.tools{display:flex;gap:9px;align-items:center}.search{width:min(300px,45vw);background:#0b1423;border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 12px;outline:none}.search:focus{border-color:var(--blue)}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:820px}th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:750;padding:11px 10px;border-bottom:1px solid var(--line)}td{padding:13px 10px;border-bottom:1px solid #202f45;vertical-align:top}tr:last-child td{border-bottom:0}.filename{font-weight:700;max-width:310px;overflow-wrap:anywhere}.meta{color:var(--muted);font-size:12px;margin-top:3px}.pill{display:inline-flex;border-radius:99px;padding:3px 9px;font-size:11px;font-weight:800;text-transform:capitalize;background:#25354b;color:#c6d3e4}.pill.imported{background:#153c35;color:#73e2ba}.pill.failed{background:#482631;color:#ff9ca5}.pill.processing{background:#47391e;color:#ffd184}.pill.queued{background:#1c3555;color:#9bc4ff}.error{color:#ff9ca5;max-width:260px;overflow-wrap:anywhere;font-size:12px}.audit{color:#aebed2;font-size:12px;max-width:260px;overflow-wrap:anywhere}.empty{text-align:center;color:var(--muted);padding:34px}.foot{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:12px;margin-top:14px}.notice{color:var(--muted);font-size:12px}.toast{position:fixed;right:22px;bottom:22px;background:#1b3049;border:1px solid #365577;border-radius:10px;padding:12px 16px;display:none;max-width:min(420px,90vw);box-shadow:0 10px 40px #0008}.toast.show{display:block}@media(max-width:760px){main{padding:24px 14px}.top{align-items:flex-start}.top h1{font-size:23px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat:last-child{grid-column:span 2}.toolbar{align-items:flex-start;flex-direction:column}.tools{width:100%}.search{width:100%;flex:1}.foot{flex-direction:column}}
</style></head>
<body><main>
<header class="top"><div><div class="eyebrow">FiqhBooks · Library Operations</div><h1>EPUB Import Monitor</h1><div class="sub">Track Telegram imports, extraction audits and failed jobs.</div></div><button class="btn primary" id="refresh">↻ &nbsp;Refresh</button></header>
<section class="stats">
<div class="stat"><span>Total imports</span><strong id="total">—</strong></div><div class="stat"><span>Queued</span><strong id="queued">—</strong></div><div class="stat"><span>Processing</span><strong id="processing">—</strong></div><div class="stat imported"><span>Imported</span><strong id="imported">—</strong></div><div class="stat failed"><span>Failed</span><strong id="failed">—</strong></div>
</section>
<section class="panel"><div class="toolbar"><div><h2>Recent EPUB files</h2><div class="notice" id="updated">Loading import records…</div></div><div class="tools"><input class="search" id="filter" placeholder="Filter by file name or status" aria-label="Filter imports"><button class="btn" id="reload">Reload</button></div></div>
<div class="tablewrap"><table><thead><tr><th>File</th><th>Status</th><th>Attempts</th><th>Extraction audit</th><th>Last update</th><th>Action</th></tr></thead><tbody id="rows"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody></table></div><div class="foot"><span id="count">—</span><span>Automatic queue worker processes up to 4 jobs per poller run.</span></div></section>
</main><div class="toast" id="toast" role="status"></div>
<script>
const $=id=>document.getElementById(id);let imports=[];
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const date=s=>s?new Date(s).toLocaleString():"—";
function toast(s){$("toast").textContent=s;$("toast").classList.add("show");setTimeout(()=>$("toast").classList.remove("show"),3200)}
function auditText(a){if(!a||typeof a!=="object")return "—";const keys=["linear_spine_items","total_spine_items","extracted_chars","indexed_passages","passages","audit_status"];const bits=keys.filter(k=>a[k]!==undefined).map(k=>k.replaceAll("_"," ")+": "+a[k]);return bits.length?bits.join(" · "):JSON.stringify(a)}
function render(){const q=$("filter").value.trim().toLowerCase();const shown=imports.filter(x=>(x.file_name+" "+x.status).toLowerCase().includes(q));$("rows").innerHTML=shown.length?shown.map(x=>'<tr><td><div class="filename">'+esc(x.file_name)+'</div><div class="meta">'+(x.file_size?((x.file_size/1048576).toFixed(2)+' MB'):'Size unavailable')+' · Import #'+x.id+(x.book_id?' · Book #'+x.book_id:'')+'</div>'+(x.error?'<div class="error" style="margin-top:6px">'+esc(x.error)+'</div>':'')+'</td><td><span class="pill '+esc(x.status)+'">'+esc(x.status)+'</span></td><td>'+esc(x.attempt_count??0)+'</td><td><div class="audit">'+esc(auditText(x.audit))+'</div></td><td><div>'+esc(date(x.updated_at))+'</div><div class="meta">Added '+esc(date(x.created_at))+'</div></td><td>'+(x.status==="failed"?'<button class="btn danger" data-retry="'+esc(x.id)+'">Retry import</button>':'<span class="meta">—</span>')+'</td></tr>').join(""):'<tr><td colspan="6" class="empty">No matching imports.</td></tr>';$("count").textContent="Showing "+shown.length+" of "+imports.length+" imports"}
async function load(){ $("refresh").disabled=true;$("reload").disabled=true;try{const r=await fetch("/admin/api/imports?limit=100",{cache:"no-store"});if(!r.ok)throw new Error(r.status===401?"Authentication required":("Could not load imports (HTTP "+r.status+")"));const d=await r.json();imports=d.recent_imports||[];for(const k of ["total","queued","processing","imported","failed"])$(k).textContent=d.summary[k]??0;$("updated").textContent="Updated "+new Date().toLocaleTimeString()+" · "+(d.summary.total_attempts??0)+" total processing attempts";render()}catch(e){$("rows").innerHTML='<tr><td colspan="6" class="empty">'+esc(e.message)+'</td></tr>';$("updated").textContent="Dashboard data unavailable"}finally{$("refresh").disabled=false;$("reload").disabled=false}}
async function retry(id,btn){if(!confirm("Queue this failed EPUB for another import attempt?"))return;btn.disabled=true;try{const r=await fetch("/admin/api/retry/"+encodeURIComponent(id),{method:"POST",headers:{"X-Admin-Action":"retry"}});const d=await r.json();if(!r.ok)throw new Error(d.detail||"Retry failed");toast("Import #"+id+" queued for retry.");await load()}catch(e){toast(e.message);btn.disabled=false}}
$("refresh").addEventListener("click",load);$("reload").addEventListener("click",load);$("filter").addEventListener("input",render);$("rows").addEventListener("click",e=>{const b=e.target.closest("[data-retry]");if(b)retry(b.dataset.retry,b)});load();setInterval(load,30000);
</script></body></html>"""


def _admin_auth(credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)]) -> str:
    expected = os.getenv("FIQHBOOKS_ADMIN_PASSWORD") or os.getenv("MCP_BEARER_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard authentication is not configured",
        )
    supplied_user = credentials.username if credentials else ""
    supplied_password = credentials.password if credentials else ""
    valid = (
        hmac.compare_digest(supplied_user.encode(), b"admin")
        and hmac.compare_digest(supplied_password.encode(), expected.encode())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dashboard credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return supplied_user


@router.get("/imports", response_class=HTMLResponse)
def import_dashboard(_: Annotated[str, Depends(_admin_auth)]):
    return HTMLResponse(
        DASHBOARD_HTML,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/imports")
def dashboard_import_status(
    _: Annotated[str, Depends(_admin_auth)],
    limit: int = 100,
):
    limit = max(1, min(int(limit), 100))
    with connection() as conn:
        summary = conn.execute(
            """SELECT count(*)::int AS total,
                      count(*) FILTER (WHERE status='queued')::int AS queued,
                      count(*) FILTER (WHERE status='processing')::int AS processing,
                      count(*) FILTER (WHERE status='imported')::int AS imported,
                      count(*) FILTER (WHERE status='failed')::int AS failed,
                      coalesce(sum(attempt_count),0)::int AS total_attempts
               FROM telegram_imports"""
        ).fetchone()
        rows = conn.execute(
            """SELECT id,file_name,file_size,status,attempt_count,error,
                      book_id,edition_id,audit,created_at,updated_at
               FROM telegram_imports
               ORDER BY created_at DESC,id DESC
               LIMIT %s""",
            (limit,),
        ).fetchall()
    return {"ok": True, "summary": dict(summary), "recent_imports": [dict(row) for row in rows]}


@router.post("/api/retry/{import_id}")
def dashboard_retry_import(
    import_id: int,
    _: Annotated[str, Depends(_admin_auth)],
    x_admin_action: Annotated[str | None, Header()] = None,
):
    # A non-simple custom header prevents cross-site HTML forms from triggering retries.
    if x_admin_action != "retry":
        raise HTTPException(status_code=403, detail="Missing admin action header")
    with connection() as conn:
        row = conn.execute(
            """UPDATE telegram_imports
               SET status='queued',error=NULL,processing_started_at=NULL,updated_at=now()
               WHERE id=%s AND status='failed' AND file_id IS NOT NULL
               RETURNING id,file_name,status,attempt_count""",
            (import_id,),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=409,
            detail="Import not found, not failed, or missing its Telegram file reference",
        )
    return {"ok": True, "message": "Import queued for retry", "import": dict(row)}


def attach_admin_dashboard(app) -> None:
    app.include_router(router, prefix="/admin", tags=["Admin dashboard"])

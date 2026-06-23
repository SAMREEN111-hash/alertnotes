"""
AlertNotes - Main Application
FastAPI app handling:
  - Grafana/Alertmanager webhook receiver
  - Resolution form (web UI)
  - REST API for querying history
  - Web dashboard
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import database as db
from .enrichment import enrich_context, summarize_context
from .slack import post_history_on_fire, send_resolution_prompt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AlertNotes",
    description="Operational memory for your alerts",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    db.init_db()
    logger.info("AlertNotes started. DB initialized.")


# ─── Webhook: Grafana Alertmanager ─────────────────────────────────────────

class AlertmanagerPayload(BaseModel):
    alerts: list
    commonLabels: dict = {}
    commonAnnotations: dict = {}


@app.post("/webhook/alertmanager")
async def alertmanager_webhook(payload: AlertmanagerPayload):
    """
    Receives Grafana Alertmanager webhook payloads.
    On firing: looks up history and posts to Slack.
    On resolving: captures context and sends resolution prompt.
    """
    results = []

    for alert in payload.alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        status = alert.get("status", "firing")

        alert_name = labels.get("alertname", "UnknownAlert")
        service = labels.get("service") or labels.get("job") or labels.get("app", "")
        environment = labels.get("env") or labels.get("environment", "production")
        severity = labels.get("severity", "warning")

        fingerprint = db.compute_fingerprint(alert_name, service, environment, labels)
        alert_id = db.upsert_alert(fingerprint, alert_name, service, environment, severity, labels)

        starts_at = alert.get("startsAt", datetime.utcnow().isoformat())
        ends_at = alert.get("endsAt")

        if status == "firing":
            resolutions = db.get_resolutions(fingerprint, limit=3)
            await post_history_on_fire(
                alert_name=alert_name,
                service=service,
                environment=environment,
                severity=severity,
                resolutions=resolutions,
                fingerprint=fingerprint,
            )
            results.append({"alert": alert_name, "action": "history_posted", "fingerprint": fingerprint})

        elif status == "resolved" and ends_at:
            # Auto-pull context from Slack and GitHub
            context = await enrich_context(alert_name, service, starts_at, ends_at)
            context_summary = summarize_context(context)

            # Store as pending resolution awaiting engineer input
            token = db.create_pending(
                fingerprint=fingerprint,
                alert_name=alert_name,
                service=service,
                fired_at=starts_at,
                payload={"alert_id": alert_id, "resolved_at": ends_at, "auto_context": context},
            )

            await send_resolution_prompt(
                alert_name=alert_name,
                service=service,
                fired_at=starts_at,
                token=token,
                auto_context_summary=context_summary,
            )
            results.append({"alert": alert_name, "action": "resolution_prompt_sent", "token": token})

    return {"processed": len(results), "results": results}


# ─── Resolution Form ────────────────────────────────────────────────────────

@app.get("/resolve/{token}", response_class=HTMLResponse)
async def resolution_form(token: str, skip: bool = False):
    """Web form for documenting alert resolutions."""
    with db.get_conn() as conn:
        pending = conn.execute(
            "SELECT * FROM pending_resolutions WHERE token = ?", (token,)
        ).fetchone()

    if not pending:
        return HTMLResponse("<h2>This resolution link has expired or already been used.</h2>", status_code=404)

    pending = dict(pending)
    payload = json.loads(pending["payload"])
    auto_context = payload.get("auto_context", {})
    context_summary = summarize_context(auto_context)

    if skip:
        return HTMLResponse(_render_skip_page(pending["alert_name"]))

    fired_dt = datetime.fromisoformat(pending["fired_at"])
    return HTMLResponse(_render_resolution_form(pending, context_summary, fired_dt))


@app.post("/resolve/{token}")
async def submit_resolution(
    token: str,
    cause: str = Form(...),
    fix: str = Form(...),
    resolved_by: str = Form(""),
):
    """Saves the resolution and removes the pending record."""
    with db.get_conn() as conn:
        pending = conn.execute(
            "SELECT * FROM pending_resolutions WHERE token = ?", (token,)
        ).fetchone()

    if not pending:
        raise HTTPException(status_code=404, detail="Resolution link expired or already used")

    pending = dict(pending)
    payload = json.loads(pending["payload"])
    auto_context = payload.get("auto_context", {})
    commits = auto_context.get("commits", []) + auto_context.get("pre_fire_commits", [])

    alert_id = payload["alert_id"]
    resolved_at = payload["resolved_at"]

    db.save_resolution(
        fingerprint=pending["fingerprint"],
        alert_id=alert_id,
        fired_at=pending["fired_at"],
        resolved_at=resolved_at,
        cause=cause.strip(),
        fix=fix.strip(),
        resolved_by=resolved_by.strip() or "anonymous",
        auto_context=auto_context,
        commits=commits,
    )

    with db.get_conn() as conn:
        conn.execute("DELETE FROM pending_resolutions WHERE token = ?", (token,))

    return HTMLResponse(_render_success_page(pending["alert_name"]))


# ─── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/alerts")
async def list_alerts(search: Optional[str] = None, limit: int = 50):
    return db.get_alert_history(limit=limit, search=search)


@app.get("/api/alert/{fingerprint}/history")
async def alert_history(fingerprint: str, limit: int = 10):
    resolutions = db.get_resolutions(fingerprint, limit=limit)
    if not resolutions:
        raise HTTPException(status_code=404, detail="No resolutions found for this alert")
    return resolutions


@app.get("/api/stats")
async def stats():
    return db.get_stats()


@app.post("/api/resolution/{resolution_id}/helpful")
async def mark_helpful(resolution_id: int, helpful: bool = True):
    db.mark_helpful(resolution_id, helpful)
    return {"status": "ok"}


# ─── Web Dashboard ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    alerts = db.get_alert_history(limit=50)
    stats = db.get_stats()
    return HTMLResponse(_render_dashboard(alerts, stats))


@app.get("/alert/{fingerprint}", response_class=HTMLResponse)
async def alert_detail(fingerprint: str):
    resolutions = db.get_resolutions(fingerprint, limit=20)
    if not resolutions:
        alerts = db.get_alert_history(limit=100)
        alert = next((a for a in alerts if a["fingerprint"] == fingerprint), None)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        resolutions = []
        alert_name = alert["alert_name"]
        service = alert["service"]
    else:
        alert_name = resolutions[0]["alert_name"]
        service = resolutions[0]["service"]

    return HTMLResponse(_render_alert_detail(fingerprint, alert_name, service, resolutions))


# ─── HTML Rendering ─────────────────────────────────────────────────────────

def _base_style():
    return """
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
      * { box-sizing: border-box; margin: 0; padding: 0; }
      body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
             background: #0f1117; color: #e2e8f0; min-height: 100vh; }
      .nav { background: #1a1d27; border-bottom: 1px solid #2d3148;
             padding: 0 2rem; display: flex; align-items: center;
             height: 56px; gap: 1rem; }
      .nav-brand { font-size: 1.1rem; font-weight: 600; color: #7c85f5;
                   text-decoration: none; display: flex; align-items: center; gap: 8px; }
      .nav-brand span { font-size: 1.3rem; }
      .nav a { color: #94a3b8; text-decoration: none; font-size: 0.9rem; }
      .nav a:hover { color: #e2e8f0; }
      .container { max-width: 1100px; margin: 0 auto; padding: 2rem; }
      .card { background: #1a1d27; border: 1px solid #2d3148;
              border-radius: 10px; padding: 1.5rem; }
      .badge { display: inline-flex; align-items: center; padding: 2px 8px;
               border-radius: 4px; font-size: 0.75rem; font-weight: 500; }
      .badge-red { background: #3b1a1a; color: #f87171; }
      .badge-yellow { background: #332b00; color: #fbbf24; }
      .badge-blue { background: #1a2540; color: #60a5fa; }
      .badge-green { background: #0f2a1a; color: #34d399; }
      .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px;
             border-radius: 6px; font-size: 0.9rem; font-weight: 500;
             cursor: pointer; text-decoration: none; border: none; }
      .btn-primary { background: #7c85f5; color: white; }
      .btn-primary:hover { background: #6470f3; }
      .btn-ghost { background: transparent; color: #94a3b8;
                  border: 1px solid #2d3148; }
      .btn-ghost:hover { background: #2d3148; color: #e2e8f0; }
      input, textarea, select {
        background: #0f1117; border: 1px solid #2d3148; color: #e2e8f0;
        border-radius: 6px; padding: 10px 12px; font-size: 0.95rem;
        font-family: inherit; width: 100%;
      }
      input:focus, textarea:focus { outline: none; border-color: #7c85f5; }
      label { font-size: 0.85rem; color: #94a3b8; display: block; margin-bottom: 6px; }
      .field { margin-bottom: 1.2rem; }
      h1 { font-size: 1.5rem; font-weight: 600; }
      h2 { font-size: 1.2rem; font-weight: 600; }
      h3 { font-size: 1rem; font-weight: 600; }
      .muted { color: #64748b; font-size: 0.875rem; }
      .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
      .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; }
      .stat-card { background: #1a1d27; border: 1px solid #2d3148;
                   border-radius: 10px; padding: 1.25rem; }
      .stat-value { font-size: 2rem; font-weight: 700; color: #7c85f5; }
      .stat-label { font-size: 0.8rem; color: #64748b; margin-top: 4px; }
      table { width: 100%; border-collapse: collapse; }
      th { text-align: left; padding: 10px 12px; font-size: 0.8rem;
           color: #64748b; border-bottom: 1px solid #2d3148; font-weight: 500; }
      td { padding: 12px; border-bottom: 1px solid #1e2235; font-size: 0.9rem; }
      tr:hover td { background: #1e2235; }
      a { color: #7c85f5; text-decoration: none; }
      a:hover { text-decoration: underline; }
      .timeline-item { border-left: 2px solid #2d3148; padding-left: 1.5rem;
                       padding-bottom: 1.5rem; position: relative; }
      .timeline-item::before { content: ''; position: absolute; left: -5px; top: 4px;
        width: 8px; height: 8px; border-radius: 50%; background: #7c85f5; }
      .context-box { background: #0f1117; border: 1px solid #2d3148;
                     border-radius: 6px; padding: 1rem; font-size: 0.85rem;
                     color: #94a3b8; white-space: pre-wrap; font-family: monospace; }
      @media (max-width: 768px) {
        .grid-4 { grid-template-columns: repeat(2, 1fr); }
        .grid-2 { grid-template-columns: 1fr; }
        .container { padding: 1rem; }
      }
    </style>
    """


def _nav(active: str = ""):
    return f"""
    <nav class="nav">
      <a class="nav-brand" href="/"><span>🔔</span> AlertNotes</a>
      <a href="/">Dashboard</a>
      <a href="/api/stats">Stats API</a>
      <a href="/api/alerts">Alerts API</a>
    </nav>
    """


def _render_dashboard(alerts: list, stats: dict) -> str:
    rows = ""
    for a in alerts:
        sev_class = "badge-red" if a.get("severity") == "critical" else "badge-yellow"
        last_seen = a.get("last_seen", "")[:10]
        res_count = a.get("resolution_count", 0)
        rows += f"""
        <tr>
          <td><a href="/alert/{a['fingerprint']}">{a['alert_name']}</a></td>
          <td>{a.get('service') or '—'}</td>
          <td><span class="badge {sev_class}">{a.get('severity', 'warning')}</span></td>
          <td>{a.get('environment', 'production')}</td>
          <td>{a.get('fire_count', 1)}</td>
          <td>{'✅ ' + str(res_count) if res_count else '<span class="muted">none</span>'}</td>
          <td class="muted">{last_seen}</td>
        </tr>
        """

    top_alerts = ""
    for a in stats.get("top_recurring_alerts", []):
        top_alerts += f"<li>{a['alert_name']} ({a.get('service','?')}) — <strong>{a['fire_count']}x</strong></li>"

    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>{_base_style()}<title>AlertNotes</title></head>
    <body>
    {_nav()}
    <div class="container">
      <div style="margin: 2rem 0 1.5rem; display: flex; align-items: center; justify-content: space-between;">
        <div>
          <h1>Alert History</h1>
          <p class="muted" style="margin-top: 4px;">Operational memory for your production alerts</p>
        </div>
      </div>

      <div class="grid-4" style="margin-bottom: 2rem;">
        <div class="stat-card">
          <div class="stat-value">{stats['total_alerts']}</div>
          <div class="stat-label">Unique Alerts</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{stats['total_resolutions']}</div>
          <div class="stat-label">Documented Resolutions</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{stats['avg_resolution_mins']}m</div>
          <div class="stat-label">Avg Resolution Time</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{stats['helpful_count']}</div>
          <div class="stat-label">Times History Helped</div>
        </div>
      </div>

      <div class="card">
        <table>
          <thead>
            <tr>
              <th>Alert</th><th>Service</th><th>Severity</th>
              <th>Environment</th><th>Fires</th><th>Documented</th><th>Last Seen</th>
            </tr>
          </thead>
          <tbody>{rows if rows else '<tr><td colspan="7" style="text-align:center;padding:2rem;color:#64748b;">No alerts yet. Configure the webhook to start capturing.</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    </body></html>
    """


def _render_alert_detail(fingerprint: str, alert_name: str, service: str, resolutions: list) -> str:
    timeline = ""
    for res in resolutions:
        fired_dt = datetime.fromisoformat(res["fired_at"])
        date_str = fired_dt.strftime("%B %d, %Y at %H:%M UTC")
        duration_min = round((res.get("duration_secs") or 0) / 60)
        commits = json.loads(res.get("commits") or "[]")

        commit_html = ""
        if commits:
            commit_html = "<div style='margin-top:8px'>"
            for c in commits[:3]:
                commit_html += f"<div class='muted' style='font-family:monospace;font-size:0.8rem'><code>{c['sha']}</code> {c['message'][:80]} — {c['author']}</div>"
            commit_html += "</div>"

        helpful_id = res["id"]
        timeline += f"""
        <div class="timeline-item">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <span style="font-weight:600">{date_str}</span>
            <span class="muted">· {duration_min} min · {res.get('resolved_by','unknown')}</span>
          </div>
          <div class="card" style="margin-bottom:8px">
            <div style="margin-bottom:12px">
              <label>Cause</label>
              <p>{res.get('cause') or '<em style="color:#64748b">Not documented</em>'}</p>
            </div>
            <div>
              <label>Fix</label>
              <p>{res.get('fix') or '<em style="color:#64748b">Not documented</em>'}</p>
            </div>
            {commit_html}
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="muted" style="font-size:0.8rem">Was this helpful?</span>
            <button class="btn btn-ghost" style="padding:4px 10px;font-size:0.8rem"
              onclick="fetch('/api/resolution/{helpful_id}/helpful?helpful=true',{{method:'POST'}});this.textContent='👍 Yes';this.disabled=true">👍 Yes</button>
            <button class="btn btn-ghost" style="padding:4px 10px;font-size:0.8rem"
              onclick="fetch('/api/resolution/{helpful_id}/helpful?helpful=false',{{method:'POST'}});this.textContent='👎 No';this.disabled=true">👎 No</button>
          </div>
        </div>
        """

    if not timeline:
        timeline = "<p class='muted'>No documented resolutions yet for this alert.</p>"

    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>{_base_style()}<title>{alert_name} — AlertNotes</title></head>
    <body>
    {_nav()}
    <div class="container">
      <div style="margin: 2rem 0 1.5rem;">
        <a href="/" class="muted" style="font-size:0.85rem">← Back to dashboard</a>
        <h1 style="margin-top:8px">{alert_name}</h1>
        <p class="muted" style="margin-top:4px">{service or 'unknown service'} · {len(resolutions)} documented resolution(s)</p>
      </div>
      <div style="margin-top: 1.5rem;">
        {timeline}
      </div>
    </div>
    </body></html>
    """


def _render_resolution_form(pending: dict, context_summary: str, fired_dt: datetime) -> str:
    date_str = fired_dt.strftime("%B %d, %Y at %H:%M UTC")
    context_html = ""
    if context_summary:
        context_html = f"""
        <div class="field">
          <label>🤖 Auto-detected context (from Slack & Git)</label>
          <div class="context-box">{context_summary}</div>
        </div>
        """

    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>{_base_style()}<title>Document Resolution — AlertNotes</title></head>
    <body>
    {_nav()}
    <div class="container" style="max-width: 680px;">
      <div style="margin: 2rem 0 1.5rem;">
        <h1>Document This Resolution</h1>
        <p class="muted" style="margin-top:4px">
          <strong style="color:#e2e8f0">{pending['alert_name']}</strong>
          · {pending.get('service') or 'unknown service'}
          · fired {date_str}
        </p>
      </div>
      <div class="card">
        <form method="POST">
          {context_html}
          <div class="field">
            <label>What caused this alert? *</label>
            <textarea name="cause" rows="3" required
              placeholder="e.g. OOMKill during image processing spike; memory limit was too low for batch workload"></textarea>
          </div>
          <div class="field">
            <label>What fixed it? *</label>
            <textarea name="fix" rows="3" required
              placeholder="e.g. Increased memory limit from 512MB to 2GB in values.yaml and redeployed"></textarea>
          </div>
          <div class="field">
            <label>Your name or Slack handle (optional)</label>
            <input type="text" name="resolved_by" placeholder="e.g. @rahul or Rahul K.">
          </div>
          <div style="display:flex;gap:12px;margin-top:1.5rem">
            <button type="submit" class="btn btn-primary">Save Resolution</button>
            <a href="?skip=true" class="btn btn-ghost">Skip for now</a>
          </div>
        </form>
      </div>
      <p class="muted" style="margin-top:1rem;font-size:0.8rem">
        This takes ~30 seconds and saves the next engineer 30+ minutes.
      </p>
    </div>
    </body></html>
    """


def _render_success_page(alert_name: str) -> str:
    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>{_base_style()}<title>Saved — AlertNotes</title></head>
    <body>
    {_nav()}
    <div class="container" style="max-width:580px;text-align:center;padding-top:4rem">
      <div style="font-size:3rem;margin-bottom:1rem">✅</div>
      <h1>Resolution saved</h1>
      <p class="muted" style="margin-top:8px">
        The next engineer who sees <strong style="color:#e2e8f0">{alert_name}</strong>
        will know exactly what caused it and how you fixed it.
      </p>
      <a href="/" class="btn btn-primary" style="margin-top:1.5rem">View Dashboard</a>
    </div>
    </body></html>
    """


def _render_skip_page(alert_name: str) -> str:
    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>{_base_style()}<title>Skipped — AlertNotes</title></head>
    <body>
    {_nav()}
    <div class="container" style="max-width:580px;text-align:center;padding-top:4rem">
      <div style="font-size:3rem;margin-bottom:1rem">⏭️</div>
      <h1>Skipped</h1>
      <p class="muted" style="margin-top:8px">
        No worries. If you remember later, you can add a note from the dashboard.
      </p>
      <a href="/" class="btn btn-ghost" style="margin-top:1.5rem">Back to Dashboard</a>
    </div>
    </body></html>
    """
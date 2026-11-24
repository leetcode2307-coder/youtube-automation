import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from app.assemble import assemble_video
from app.config import settings
from app.emailer import create_approval_record, send_approval_email
from app.logging_config import logger
from app.models import ApprovalRecord, ProductionPackage, SlotType
from app.pipeline import run_content_pipeline
from app.storage import storage

app = FastAPI(title="AI Engineering YouTube Shorts Approval & Control Portal", version="1.0.0")


def verify_token(run_id: str, raw_token: str) -> ApprovalRecord:
    """Validate that the token matches the stored hash, is unexpired, and unconsumed."""
    token_hash = hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
    record = storage.get_approval_by_token_hash(token_hash)

    if not record or record.run_id != run_id:
        logger.warning("Invalid approval token attempt for run %s", run_id)
        raise HTTPException(status_code=403, detail="Invalid approval token or run ID mismatch.")

    if record.consumed_at_utc is not None:
        logger.warning("Token replay attempt for run %s (already consumed at %s)", run_id, record.consumed_at_utc)
        raise HTTPException(
            status_code=409,
            detail=f"This approval link has already been used on {record.consumed_at_utc} (Decision: {record.decision})."
        )

    now_utc = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(record.expires_at_utc)
    if now_utc > expires_at:
        logger.warning("Expired token attempt for run %s (expired at %s)", run_id, record.expires_at_utc)
        raise HTTPException(status_code=410, detail="This approval token has expired. Please request a new review link.")

    return record


@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# =====================================================================
# UI Interface - Web Control Center Dashboard
# =====================================================================

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_view():
    """Render the main operator UI dashboard."""
    packages = storage.list_packages()
    history = storage.get_topic_history()

    ledger = {}
    if settings.UPLOAD_LEDGER_FILE.exists():
        try:
            with open(settings.UPLOAD_LEDGER_FILE, "r", encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception:
            ledger = {}

    status_badge_colors = {
        "scheduled": "background:#e0e7ff; color:#3730a3;",
        "generating": "background:#fef3c7; color:#92400e;",
        "generated": "background:#dbeafe; color:#1e40af;",
        "awaiting_flow_export": "background:#fed7aa; color:#9a3412;",
        "assembling": "background:#fef08a; color:#854d0e;",
        "awaiting_approval": "background:#e9d5ff; color:#6b21a8;",
        "approved": "background:#bbf7d0; color:#166534;",
        "rejected": "background:#fecaca; color:#991b1b;",
        "uploaded_private": "background:#86efac; color:#14532d;",
        "failed": "background:#fca5a5; color:#7f1d1d;",
    }

    rows_html = ""
    for p in reversed(packages):
        badge_style = status_badge_colors.get(p.status, "background:#e5e7eb; color:#374151;")
        slot_badge = "☀️ Morning" if p.slot == "morning" else "🌙 Evening"
        slot_style = "background:#fef3c7; color:#b45309;" if p.slot == "morning" else "background:#e0e7ff; color:#4338ca;"

        # Check if video file exists
        video_exists = (settings.FINAL_OUTPUT_DIR / p.run_id / "final_short.mp4").exists()
        watch_btn = f"<a href='/video/{p.run_id}' target='_blank' class='btn-link'>▶ Watch MP4</a>" if video_exists else "<span style='color:#9ca3af;'>No video yet</span>"

        # Action buttons
        actions = f"""
        <div style='display:flex; gap:6px; flex-wrap:wrap;'>
          <button onclick="viewBrief('{p.run_id}')" class='action-btn btn-brief'>📄 Flow Brief</button>
          <button onclick="assembleRun('{p.run_id}')" class='action-btn btn-assemble'>🎬 Assemble</button>
          <button onclick="notifyRun('{p.run_id}')" class='action-btn btn-notify'>✉️ Get Review Link</button>
        </div>
        """

        rows_html += f"""
        <tr>
          <td style='padding:12px; font-weight:600; font-family:monospace;'>{p.run_id}</td>
          <td style='padding:12px;'><span style='padding:4px 8px; border-radius:12px; font-size:12px; font-weight:600; {slot_style}'>{slot_badge}</span></td>
          <td style='padding:12px;'>
            <div style='font-weight:600; color:#111827;'>{p.topic}</div>
            <div style='font-size:13px; color:#4b5563; margin-top:2px;'>{p.metadata.title}</div>
          </td>
          <td style='padding:12px; text-align:center;'><b>{p.total_duration_seconds}s</b><br><span style='font-size:11px; color:#6b7280;'>{len(p.scenes)} scenes</span></td>
          <td style='padding:12px; text-align:center;'><span style='padding:4px 10px; border-radius:16px; font-size:12px; font-weight:600; {badge_style}'>{p.status.upper()}</span></td>
          <td style='padding:12px;'>{watch_btn}</td>
          <td style='padding:12px;'>{actions}</td>
        </tr>
        """

    if not rows_html:
        rows_html = "<tr><td colspan='7' style='padding:24px; text-align:center; color:#6b7280;'>No runs created yet. Click 'Generate Morning Run' or 'Generate Evening Run' above!</td></tr>"

    history_html = "".join(
        f"<li><b>{h.get('topic')}</b> <span style='color:#6b7280; font-size:12px;'>({h.get('run_id')} &bull; {h.get('recorded_at_utc', '')[:10]})</span></li>"
        for h in reversed(history[-15:])
    ) or "<li>No topic history yet.</li>"

    ledger_html = "".join(
        f"<li><b>Run:</b> {run_id} &rarr; <b>YouTube ID:</b> <a href='https://studio.youtube.com' target='_blank' style='color:#2563eb;'>{rec.get('youtube_video_id')}</a> (Privacy: {rec.get('privacy_status')})</li>"
        for run_id, rec in ledger.items()
    ) or "<li>No uploaded videos yet. Videos upload privately after approval.</li>"

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>AI Engineering Shorts - Control Portal</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        :root {{
          --primary: #4f46e5;
          --primary-hover: #4338ca;
          --bg: #f9fafb;
          --card: #ffffff;
          --text: #111827;
          --muted: #6b7280;
          --border: #e5e7eb;
        }}
        body {{
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
          background: var(--bg);
          color: var(--text);
          margin: 0;
          padding: 24px;
        }}
        .container {{
          max-width: 1200px;
          margin: 0 auto;
        }}
        .header-card {{
          background: linear-gradient(135deg, #1e1b4b 0%, #312e81 100%);
          color: #fff;
          padding: 24px 32px;
          border-radius: 16px;
          box-shadow: 0 4px 20px rgba(0,0,0,0.12);
          display: flex;
          justify-content: space-between;
          align-items: center;
          flex-wrap: wrap;
          gap: 16px;
        }}
        .header-title h1 {{
          margin: 0 0 6px 0;
          font-size: 24px;
          letter-spacing: -0.5px;
        }}
        .header-title p {{
          margin: 0;
          color: #c7d2fe;
          font-size: 14px;
        }}
        .pill-group {{
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
        }}
        .pill {{
          background: rgba(255,255,255,0.12);
          border: 1px solid rgba(255,255,255,0.2);
          padding: 6px 12px;
          border-radius: 20px;
          font-size: 12px;
          color: #e0e7ff;
        }}
        .toolbar {{
          background: var(--card);
          padding: 16px 24px;
          border-radius: 12px;
          border: 1px solid var(--border);
          margin: 20px 0;
          display: flex;
          justify-content: space-between;
          align-items: center;
          flex-wrap: wrap;
          gap: 12px;
        }}
        .btn {{
          background: var(--primary);
          color: #fff;
          border: none;
          padding: 10px 18px;
          border-radius: 8px;
          font-weight: 600;
          font-size: 14px;
          cursor: pointer;
          transition: all 0.15s ease;
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }}
        .btn:hover {{ background: var(--primary-hover); transform: translateY(-1px); }}
        .btn-morning {{ background: #d97706; }}
        .btn-morning:hover {{ background: #b45309; }}
        .btn-secondary {{ background: #4b5563; }}
        .btn-secondary:hover {{ background: #374151; }}
        .action-btn {{
          padding: 5px 10px;
          border-radius: 6px;
          font-size: 12px;
          font-weight: 600;
          cursor: pointer;
          border: 1px solid var(--border);
          background: #f3f4f6;
          color: #374151;
        }}
        .action-btn:hover {{ background: #e5e7eb; }}
        .btn-brief {{ color: #1e40af; background: #eff6ff; border-color: #bfdbfe; }}
        .btn-assemble {{ color: #854d0e; background: #fefce8; border-color: #fef08a; }}
        .btn-notify {{ color: #6b21a8; background: #faf5ff; border-color: #e9d5ff; }}
        .btn-link {{
          color: #2563eb;
          text-decoration: none;
          font-weight: 600;
          font-size: 13px;
        }}
        .btn-link:hover {{ text-decoration: underline; }}
        .card {{
          background: var(--card);
          border-radius: 12px;
          border: 1px solid var(--border);
          overflow: hidden;
          margin-bottom: 24px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }}
        .card-header {{
          padding: 16px 20px;
          border-bottom: 1px solid var(--border);
          font-size: 16px;
          font-weight: 700;
          background: #fcfcfd;
        }}
        table {{
          width: 100%;
          border-collapse: collapse;
          font-size: 14px;
        }}
        th {{
          background: #f9fafb;
          padding: 12px;
          text-align: left;
          font-size: 12px;
          text-transform: uppercase;
          color: #6b7280;
          border-bottom: 1px solid var(--border);
        }}
        tr:not(:last-child) td {{
          border-bottom: 1px solid var(--border);
        }}
        .grid-2 {{
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 20px;
        }}
        @media(max-width: 800px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
        ul {{ margin: 0; padding-left: 20px; line-height: 1.6; font-size: 14px; }}
        /* Modal */
        .modal {{
          display: none;
          position: fixed;
          top: 0; left: 0; width: 100%; height: 100%;
          background: rgba(0,0,0,0.6);
          align-items: center;
          justify-content: center;
          z-index: 1000;
        }}
        .modal-content {{
          background: #fff;
          max-width: 800px;
          width: 90%;
          max-height: 85vh;
          border-radius: 12px;
          overflow: hidden;
          display: flex;
          flex-direction: column;
        }}
        .modal-header {{
          padding: 16px 24px;
          background: #1e1b4b;
          color: #fff;
          display: flex;
          justify-content: space-between;
          align-items: center;
        }}
        .modal-body {{
          padding: 20px 24px;
          overflow-y: auto;
          white-space: pre-wrap;
          font-family: monospace;
          font-size: 13px;
          background: #f8fafc;
          line-height: 1.5;
        }}
        .close-btn {{
          background: transparent;
          border: none;
          color: #fff;
          font-size: 20px;
          cursor: pointer;
        }}
        #toast {{
          display: none;
          position: fixed;
          bottom: 24px;
          right: 24px;
          background: #111827;
          color: #fff;
          padding: 14px 20px;
          border-radius: 8px;
          font-size: 14px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.2);
          z-index: 2000;
        }}
      </style>
    </head>
    <body>
      <div class="container">
        <!-- Header -->
        <div class="header-card">
          <div class="header-title">
            <h1>🎬 AI Engineering Shorts - Control Portal</h1>
            <p>Automated YouTube Shorts Production with Human-in-the-Loop Approval</p>
          </div>
          <div class="pill-group">
            <span class="pill">⚡ Groq: {settings.GROQ_MODEL}</span>
            <span class="pill">🕒 Timezone: {settings.TIMEZONE}</span>
            <span class="pill">📅 07:00 & 16:30 IST</span>
          </div>
        </div>

        <!-- Action Toolbar -->
        <div class="toolbar">
          <div style="display:flex; gap:10px; flex-wrap:wrap;">
            <button onclick="triggerGenerate('morning')" class="btn btn-morning">
              ☀️ Generate Morning Short (07:00 IST)
            </button>
            <button onclick="triggerGenerate('evening')" class="btn">
              🌙 Generate Evening Short (16:30 IST)
            </button>
          </div>
          <button onclick="location.reload()" class="btn btn-secondary">
            🔄 Refresh Status
          </button>
        </div>

        <!-- Production Runs Table -->
        <div class="card">
          <div class="card-header">
            Recent Production Runs ({len(packages)} total)
          </div>
          <div style="overflow-x:auto;">
            <table>
              <thead>
                <tr>
                  <th>Run ID</th>
                  <th>Slot</th>
                  <th>Topic & SEO Title</th>
                  <th style="text-align:center;">Duration</th>
                  <th style="text-align:center;">Status</th>
                  <th>Media</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows_html}
              </tbody>
            </table>
          </div>
        </div>

        <!-- History & Ledger Grid -->
        <div class="grid-2">
          <div class="card">
            <div class="card-header">🛡️ Topic History (Anti-Duplication)</div>
            <div style="padding:16px 20px; max-height:240px; overflow-y:auto;">
              <ul>
                {history_html}
              </ul>
            </div>
          </div>

          <div class="card">
            <div class="card-header">📤 YouTube Publishing Ledger</div>
            <div style="padding:16px 20px; max-height:240px; overflow-y:auto;">
              <ul>
                {ledger_html}
              </ul>
            </div>
          </div>
        </div>
      </div>

      <!-- Flow Brief Modal -->
      <div id="briefModal" class="modal">
        <div class="modal-content">
          <div class="modal-header">
            <h3 style="margin:0;" id="modalTitle">Google Flow Generation Brief</h3>
            <button onclick="closeModal()" class="close-btn">&times;</button>
          </div>
          <div id="modalBody" class="modal-body"></div>
          <div style="padding:12px 24px; background:#f1f5f9; display:flex; justify-content:flex-end;">
            <button onclick="copyBrief()" class="btn">📋 Copy All Prompts</button>
          </div>
        </div>
      </div>

      <div id="toast"></div>

      <script>
        function showToast(msg, isError = false) {{
          const t = document.getElementById("toast");
          t.innerText = msg;
          t.style.background = isError ? "#b91c1c" : "#15803d";
          t.style.display = "block";
          setTimeout(() => {{ t.style.display = "none"; }}, 4000);
        }}

        async function triggerGenerate(slot) {{
          showToast("AI Agent generating " + slot + " Short package with Groq... Please wait ~15-25s.");
          try {{
            const res = await fetch("/api/generate?slot=" + slot, {{ method: "POST" }});
            const data = await res.json();
            if (res.ok) {{
              showToast("Package created: " + data.run_id + "! Reloading...");
              setTimeout(() => location.reload(), 1500);
            }} else {{
              showToast("Generation failed: " + (data.detail || "Error"), true);
            }}
          }} catch (e) {{
            showToast("Network error: " + e, true);
          }}
        }}

        async function assembleRun(runId) {{
          showToast("Assembling video for " + runId + " with FFmpeg...");
          try {{
            const res = await fetch("/api/assemble/" + runId, {{ method: "POST" }});
            const data = await res.json();
            if (res.ok) {{
              showToast("Video assembled successfully! Status updated.");
              setTimeout(() => location.reload(), 1500);
            }} else {{
              showToast("Assembly failed: " + (data.detail || "Check clips"), true);
            }}
          }} catch (e) {{
            showToast("Assembly error: " + e, true);
          }}
        }}

        async function notifyRun(runId) {{
          showToast("Generating approval link...");
          try {{
            const res = await fetch("/api/notify/" + runId, {{ method: "POST" }});
            const data = await res.json();
            if (res.ok) {{
              window.open(data.review_url, "_blank");
              showToast("Review portal opened in new tab!");
              setTimeout(() => location.reload(), 1000);
            }} else {{
              showToast("Notification failed: " + (data.detail || "Error"), true);
            }}
          }} catch (e) {{
            showToast("Error: " + e, true);
          }}
        }}

        async function viewBrief(runId) {{
          try {{
            const res = await fetch("/api/brief/" + runId);
            const text = await res.text();
            document.getElementById("modalTitle").innerText = "Flow Brief: " + runId;
            document.getElementById("modalBody").innerText = text;
            document.getElementById("briefModal").style.display = "flex";
          }} catch (e) {{
            showToast("Failed to load brief: " + e, true);
          }}
        }}

        function closeModal() {{
          document.getElementById("briefModal").style.display = "none";
        }}

        function copyBrief() {{
          const text = document.getElementById("modalBody").innerText;
          navigator.clipboard.writeText(text);
          showToast("Prompts copied to clipboard!");
        }}
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# =====================================================================
# UI API Endpoints for Dashboard Control
# =====================================================================

@app.post("/api/generate")
def api_generate_run(slot: SlotType = Query("morning")):
    """Trigger content pipeline via UI."""
    try:
        pkg = run_content_pipeline(slot=slot)
        return {
            "status": "success",
            "run_id": pkg.run_id,
            "topic": pkg.topic,
            "title": pkg.metadata.title,
            "duration": pkg.total_duration_seconds
        }
    except Exception as e:
        logger.error("UI generation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/assemble/{run_id}")
def api_assemble_run(run_id: str):
    """Trigger video assembly via UI."""
    try:
        mp4_path = assemble_video(run_id)
        return {"status": "success", "run_id": run_id, "video_path": str(mp4_path)}
    except Exception as e:
        logger.error("UI assembly failed for %s: %s", run_id, e)
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/notify/{run_id}")
def api_notify_run(run_id: str):
    """Generate review link and send email notification via UI."""
    pkg = storage.load_package(run_id)
    if not pkg:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")

    raw_token, record = create_approval_record(run_id)
    send_approval_email(pkg, raw_token)
    review_url = f"{settings.APPROVAL_BASE_URL.rstrip('/')}/review/{run_id}?token={raw_token}"

    return {
        "status": "success",
        "run_id": run_id,
        "review_url": review_url,
        "expires_at": record.expires_at_utc
    }


@app.get("/api/brief/{run_id}", response_class=PlainTextResponse)
def api_get_brief(run_id: str):
    """Retrieve raw FLOW_PROMPTS.txt content."""
    brief_file = settings.FLOW_EXPORTS_DIR / run_id / "FLOW_PROMPTS.txt"
    if not brief_file.exists():
        raise HTTPException(status_code=404, detail=f"Flow brief not found for {run_id}")
    return brief_file.read_text(encoding="utf-8")


@app.get("/api/status")
def api_get_status():
    """Retrieve full system status as JSON."""
    packages = storage.list_packages()
    history = storage.get_topic_history()
    return {
        "total_packages": len(packages),
        "recent_packages": [p.model_dump() for p in packages[-5:]],
        "history_count": len(history),
        "groq_model": settings.GROQ_MODEL,
        "timezone": settings.TIMEZONE,
    }


# =====================================================================
# Video Streaming & Review Gate Endpoints
# =====================================================================

@app.post("/ready/{run_id}")
def mark_run_ready(run_id: str):
    """Notify system that assembly is complete and send approval email."""
    package = storage.load_package(run_id)
    if not package:
        raise HTTPException(status_code=404, detail=f"Package {run_id} not found.")

    final_mp4 = settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4"
    if not final_mp4.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Final assembled MP4 not found at {final_mp4}. Run assembly first."
        )

    raw_token, record = create_approval_record(run_id)
    email_sent = send_approval_email(package, raw_token)
    storage.update_package_status(run_id, "awaiting_approval")

    review_url = f"{settings.APPROVAL_BASE_URL.rstrip('/')}/review/{run_id}?token={raw_token}"
    return {
        "status": "awaiting_approval",
        "run_id": run_id,
        "email_sent": email_sent,
        "review_url": review_url
    }


@app.get("/video/{run_id}")
def stream_video(run_id: str):
    """Serve the assembled MP4 video file for browser preview."""
    final_mp4 = settings.FINAL_OUTPUT_DIR / run_id / "final_short.mp4"
    if not final_mp4.exists():
        raise HTTPException(status_code=404, detail="Video file not found.")
    return FileResponse(final_mp4, media_type="video/mp4")


@app.get("/review/{run_id}", response_class=HTMLResponse)
def review_page(run_id: str, token: str = Query(..., description="Approval secret token")):
    """
    Render human-friendly video review page.
    NOTE: GET requests NEVER trigger approval or upload!
    """
    record = verify_token(run_id, token)
    package = storage.load_package(run_id)
    if not package:
        raise HTTPException(status_code=404, detail="Package not found.")

    scenes_rows = "".join(
        f"<tr>"
        f"<td style='padding:10px;border-bottom:1px solid #e0e0e0;'><b>Scene {s.scene_number}</b> ({s.duration_seconds}s)</td>"
        f"<td style='padding:10px;border-bottom:1px solid #e0e0e0;'>"
        f"<b>Overlay:</b> {s.on_screen_text}<br>"
        f"<b>Narration:</b> {s.narration}<br>"
        f"<span style='color:#666;font-size:12px;'><b>Flow Prompt:</b> {s.visual_prompt[:120]}...</span>"
        f"</td>"
        f"</tr>"
        for s in package.scenes
    )

    tags_html = " ".join(f"<span style='background:#e8f0fe;color:#1967d2;padding:4px 8px;border-radius:12px;font-size:12px;display:inline-block;margin:2px;'>{t}</span>" for t in package.metadata.tags)
    hashtags_html = " ".join(f"<span style='color:#1a73e8;font-weight:bold;'>{h}</span>" for h in package.metadata.hashtags)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <title>Review: {package.metadata.title}</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#f0f2f5; margin:0; padding:24px; }}
        .container {{ max-width: 900px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); overflow: hidden; }}
        .header {{ background: #1a237e; color: #fff; padding: 20px 28px; display:flex; justify-content:space-between; align-items:center; }}
        .body-grid {{ display: grid; grid-template-columns: 340px 1fr; gap: 24px; padding: 28px; }}
        video {{ width: 100%; border-radius: 8px; background: #000; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }}
        .btn {{ border:none; border-radius:6px; padding:12px 24px; font-size:16px; font-weight:bold; cursor:pointer; }}
        .btn-approve {{ background: #2e7d32; color: #fff; width: 100%; margin-bottom: 12px; }}
        .btn-approve:hover {{ background: #1b5e20; }}
        .btn-reject {{ background: #c62828; color: #fff; width: 100%; }}
        .btn-reject:hover {{ background: #b71c1c; }}
        .meta-card {{ background: #f8f9fa; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
        @media(max-width: 768px) {{ .body-grid {{ grid-template-columns: 1fr; }} }}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <div>
            <h2 style="margin:0; font-size:20px;">AI Engineering Shorts Review Gate</h2>
            <div style="font-size:13px; opacity:0.85;">Run ID: {package.run_id} &bull; Slot: {package.slot.upper()}</div>
          </div>
          <span style="background:#e8eaf6; color:#1a237e; font-weight:bold; padding:6px 12px; border-radius:16px; font-size:13px;">Status: Awaiting Approval</span>
        </div>

        <div class="body-grid">
          <div>
            <!-- Video Player (Portrait 9:16) -->
            <video controls autoplay muted loop>
              <source src="/video/{package.run_id}" type="video/mp4">
              Your browser does not support the video tag.
            </video>
            <div style="margin-top:12px; font-size:13px; color:#555; text-align:center;">
              Duration: <b>{package.total_duration_seconds} seconds</b> &bull; Resolution: <b>1080x1920</b>
            </div>

            <div style="margin-top: 24px;">
              <!-- Form 1: Approve (Explicit POST) -->
              <form method="POST" action="/approve/{package.run_id}">
                <input type="hidden" name="token" value="{token}">
                <button type="submit" class="btn btn-approve">
                  &check; Approve & Upload Privately
                </button>
              </form>

              <!-- Form 2: Reject (Explicit POST) -->
              <form method="POST" action="/reject/{package.run_id}" style="margin-top:10px;">
                <input type="hidden" name="token" value="{token}">
                <input type="text" name="comments" placeholder="Optional rejection feedback..." style="width:calc(100% - 16px); padding:8px; margin-bottom:8px; border:1px solid #ccc; border-radius:4px; font-size:13px;">
                <button type="submit" class="btn btn-reject">
                  &cross; Reject & Cancel Upload
                </button>
              </form>
            </div>
          </div>

          <div>
            <h2 style="margin-top:0; color:#111;">{package.metadata.title}</h2>
            <div class="meta-card">
              <b>Topic:</b> {package.topic}<br>
              <b>Hook (0-3s):</b> <i>"{package.hook}"</i><br>
              <b>Synthetic Media Disclosure:</b> {package.metadata.synthetic_media_disclosure}<br>
              <div style="margin-top:8px;">{hashtags_html}</div>
            </div>

            <div class="meta-card">
              <b>Description:</b>
              <p style="white-space:pre-wrap; margin:8px 0 0 0; font-size:14px; color:#333;">{package.metadata.description}</p>
            </div>

            <div class="meta-card">
              <b>Tags:</b><br>
              <div style="margin-top:6px;">{tags_html}</div>
            </div>

            <h3>Scene Plan & Narration:</h3>
            <table>
              <tbody>
                {scenes_rows}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/approve/{run_id}", response_class=HTMLResponse)
def approve_run(run_id: str, token: str = Form(...)):
    """Authorize video upload with single-use token consumption."""
    record = verify_token(run_id, token)

    # 1. Consume token atomically
    now_utc = datetime.now(timezone.utc).isoformat()
    record.consumed_at_utc = now_utc
    record.decision = "approved"
    storage.save_approval_record(record)

    # 2. Update package status
    storage.update_package_status(run_id, "approved")

    # 3. Trigger private YouTube upload
    upload_result_msg = ""
    try:
        from app.youtube import upload_video_to_youtube
        upload_record = upload_video_to_youtube(run_id)
        upload_result_msg = f"Successfully uploaded as Private! YouTube Video ID: <code>{upload_record.youtube_video_id}</code>"
    except Exception as e:
        logger.error("YouTube upload invocation failed for %s: %s", run_id, e)
        upload_result_msg = f"Approval recorded, but YouTube upload failed or credentials needed: {str(e)}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <title>Video Approved</title>
      <style>
        body {{ font-family: sans-serif; background: #f0fdf4; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }}
        .card {{ background:#fff; padding:32px; border-radius:12px; box-shadow:0 4px 12px rgba(0,0,0,0.1); max-width:500px; text-align:center; }}
        h1 {{ color:#15803d; }}
        a {{ color: #2563eb; text-decoration: none; font-weight: bold; }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>&check; Video Approved!</h1>
        <p>Run ID: <b>{run_id}</b></p>
        <p>{upload_result_msg}</p>
        <p style="font-size:13px; color:#666;">Approval token consumed at {now_utc}. Replays are safely disabled.</p>
        <div style="margin-top:20px;">
          <a href="/">&larr; Return to Dashboard</a>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/reject/{run_id}", response_class=HTMLResponse)
def reject_run(run_id: str, token: str = Form(...), comments: Optional[str] = Form(None)):
    """Reject video, cancel upload, and consume token."""
    record = verify_token(run_id, token)

    now_utc = datetime.now(timezone.utc).isoformat()
    record.consumed_at_utc = now_utc
    record.decision = "rejected"
    record.review_comments = comments
    storage.save_approval_record(record)

    storage.update_package_status(run_id, "rejected", f"Rejected by approver: {comments or 'No comment'}")

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <title>Video Rejected</title>
      <style>
        body {{ font-family: sans-serif; background: #fef2f2; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }}
        .card {{ background:#fff; padding:32px; border-radius:12px; box-shadow:0 4px 12px rgba(0,0,0,0.1); max-width:500px; text-align:center; }}
        h1 {{ color:#b91c1c; }}
        a {{ color: #2563eb; text-decoration: none; font-weight: bold; }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>&cross; Video Rejected</h1>
        <p>Run ID: <b>{run_id}</b> has been closed without uploading to YouTube.</p>
        <p><i>Feedback: {comments or 'None provided'}</i></p>
        <div style="margin-top:20px;">
          <a href="/">&larr; Return to Dashboard</a>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

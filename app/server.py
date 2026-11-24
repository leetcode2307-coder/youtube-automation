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

app = FastAPI(title="AI Engineering YouTube Shorts Studio & Review Portal", version="1.0.0")


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
# Modern Dashboard UI - Operator Studio & Control Center
# =====================================================================

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_view():
    """Render the ultra-modern, intuitive operator UI dashboard."""
    packages = storage.list_packages()
    history = storage.get_topic_history()

    ledger = {}
    if settings.UPLOAD_LEDGER_FILE.exists():
        try:
            with open(settings.UPLOAD_LEDGER_FILE, "r", encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception:
            ledger = {}

    status_meta = {
        "scheduled": {"label": "SCHEDULED", "color": "#6366f1", "bg": "rgba(99, 102, 241, 0.15)", "step": 1},
        "generating": {"label": "GENERATING AI SCRIPT", "color": "#f59e0b", "bg": "rgba(245, 158, 11, 0.15)", "step": 1},
        "generated": {"label": "PROMPTS GENERATED", "color": "#3b82f6", "bg": "rgba(59, 130, 246, 0.15)", "step": 1},
        "awaiting_flow_export": {"label": "AWAITING GOOGLE FLOW EXPORT", "color": "#f97316", "bg": "rgba(249, 115, 22, 0.15)", "step": 2},
        "assembling": {"label": "ASSEMBLING FFMPEG VIDEO", "color": "#eab308", "bg": "rgba(234, 179, 8, 0.15)", "step": 3},
        "awaiting_approval": {"label": "AWAITING HUMAN APPROVAL", "color": "#a855f7", "bg": "rgba(168, 85, 247, 0.15)", "step": 4},
        "approved": {"label": "APPROVED BY OPERATOR", "color": "#10b981", "bg": "rgba(16, 185, 129, 0.15)", "step": 5},
        "rejected": {"label": "REJECTED (UPLOAD CANCELLED)", "color": "#ef4444", "bg": "rgba(239, 68, 68, 0.15)", "step": 4},
        "uploaded_private": {"label": "UPLOADED (PRIVATE TO YOUTUBE)", "color": "#10b981", "bg": "rgba(16, 185, 129, 0.25)", "step": 5},
        "failed": {"label": "FAILED (ACTION REQUIRED)", "color": "#f87171", "bg": "rgba(248, 113, 113, 0.15)", "step": 1},
    }

    cards_html = ""
    for p in reversed(packages):
        meta = status_meta.get(p.status, {"label": p.status.upper(), "color": "#94a3b8", "bg": "rgba(148, 163, 184, 0.15)", "step": 1})
        slot_label = "☀️ Morning Slot (07:00 IST)" if p.slot == "morning" else "🌙 Evening Slot (16:30 IST)"
        slot_bg = "background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3);" if p.slot == "morning" else "background: rgba(99, 102, 241, 0.15); color: #a5b4fc; border: 1px solid rgba(99, 102, 241, 0.3);"

        # Stepper active classes
        current_step = meta["step"]
        def get_step_class(s):
            if p.status == "rejected":
                return "step-failed" if s == 4 else "step-done" if s < 4 else "step-pending"
            if s < current_step or p.status == "uploaded_private":
                return "step-done"
            elif s == current_step:
                return "step-active"
            else:
                return "step-pending"

        # Check if video exists
        video_exists = (settings.FINAL_OUTPUT_DIR / p.run_id / "final_short.mp4").exists()
        watch_btn = f"""<a href="/video/{p.run_id}" target="_blank" class="btn-action btn-watch">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
            Watch MP4 (1080x1920)
        </a>""" if video_exists else ""

        # Scene pills
        scenes_preview = "".join(
            f"<span class='scene-chip' title='{s.narration}'>Scene {s.scene_number} ({s.duration_seconds}s)</span>"
            for s in p.scenes
        )

        cards_html += f"""
        <div class="run-card">
          <!-- Card Header -->
          <div class="run-card-header">
            <div class="run-card-meta">
              <span class="slot-badge" style="{slot_bg}">{slot_label}</span>
              <span class="run-id-badge">{p.run_id}</span>
              <span class="status-pill" style="background:{meta['bg']}; color:{meta['color']}; border: 1px solid {meta['color']}40;">
                <span class="status-dot" style="background:{meta['color']};"></span>
                {meta['label']}
              </span>
            </div>
            <div class="run-time-meta">
              Target Duration: <b>{p.total_duration_seconds}s</b> &bull; {len(p.scenes)} Scenes
            </div>
          </div>

          <!-- Stepper Progress Bar -->
          <div class="stepper-container">
            <div class="stepper-item {get_step_class(1)}">
              <div class="step-circle">1</div>
              <div class="step-label">AI Script & Prompts</div>
            </div>
            <div class="step-line { 'line-active' if current_step >= 2 else '' }"></div>
            <div class="stepper-item {get_step_class(2)}">
              <div class="step-circle">2</div>
              <div class="step-label">Google Flow Clips</div>
            </div>
            <div class="step-line { 'line-active' if current_step >= 3 else '' }"></div>
            <div class="stepper-item {get_step_class(3)}">
              <div class="step-circle">3</div>
              <div class="step-label">FFmpeg Assembly</div>
            </div>
            <div class="step-line { 'line-active' if current_step >= 4 else '' }"></div>
            <div class="stepper-item {get_step_class(4)}">
              <div class="step-circle">4</div>
              <div class="step-label">Human Review Gate</div>
            </div>
            <div class="step-line { 'line-active' if current_step >= 5 else '' }"></div>
            <div class="stepper-item {get_step_class(5)}">
              <div class="step-circle">5</div>
              <div class="step-label">YouTube Private Upload</div>
            </div>
          </div>

          <!-- Card Body -->
          <div class="run-card-body">
            <h3 class="run-title">{p.metadata.title}</h3>
            <div class="run-topic"><b>Topic:</b> {p.topic}</div>
            <div class="run-hook"><b>Hook (0-3s):</b> <i>"{p.hook}"</i></div>

            <div class="scenes-chips-container">
              {scenes_preview}
            </div>
          </div>

          <!-- Card Footer Actions -->
          <div class="run-card-footer">
            <div class="action-buttons-group">
              <button onclick="viewBrief('{p.run_id}')" class="btn-action btn-prompts">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                Google Flow Prompts Brief
              </button>

              <button onclick="assembleRun('{p.run_id}')" class="btn-action btn-assemble">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                Assemble Video (FFmpeg)
              </button>

              <button onclick="notifyRun('{p.run_id}')" class="btn-action btn-review">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                Open Review Portal
              </button>

              {watch_btn}
            </div>
          </div>
        </div>
        """

    if not cards_html:
        cards_html = """
        <div class="empty-state">
          <div class="empty-icon">🎬</div>
          <h3>No production runs yet</h3>
          <p>Generate your first daily short using the action buttons above or wait for APScheduler at 07:00 AM IST.</p>
        </div>
        """

    history_items = "".join(
        f"""<div class="history-item">
          <div class="history-topic">{h.get('topic')}</div>
          <div class="history-meta">Run: <code>{h.get('run_id')}</code> &bull; Recorded: {h.get('recorded_at_utc', '')[:19].replace('T', ' ')} UTC</div>
        </div>"""
        for h in reversed(history[-20:])
    ) or "<div class='empty-text'>No topic history recorded yet. Anti-duplication store is clean.</div>"

    ledger_items = "".join(
        f"""<div class="ledger-item">
          <div class="ledger-header">
            <span class="ledger-run">Run: {run_id}</span>
            <span class="privacy-badge">🔒 {rec.get('privacy_status', 'private').upper()}</span>
          </div>
          <div class="ledger-meta">
            YouTube Video ID: <a href="https://studio.youtube.com" target="_blank" class="yt-link">{rec.get('youtube_video_id')}</a>
            &bull; SHA-256: <code>{rec.get('video_sha256', '')[:16]}...</code>
          </div>
        </div>"""
        for run_id, rec in ledger.items()
    ) or "<div class='empty-text'>No videos uploaded yet. Videos upload privately after you approve them in the review portal.</div>"

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>AI Engineering Shorts Studio</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <link rel="preconnect" href="https://fonts.googleapis.com">
      <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
      <style>
        :root {{
          --bg: #090d16;
          --card: #111726;
          --card-hover: #161e31;
          --border: #1e293b;
          --border-light: #334155;
          --text: #f8fafc;
          --text-muted: #94a3b8;
          --primary: #6366f1;
          --primary-hover: #4f46e5;
          --accent: #8b5cf6;
          --success: #10b981;
          --warning: #f59e0b;
          --danger: #ef4444;
          --font-sans: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
          --font-mono: 'JetBrains Mono', monospace;
        }}

        * {{ box-sizing: border-box; }}
        body {{
          font-family: var(--font-sans);
          background-color: var(--bg);
          color: var(--text);
          margin: 0;
          padding: 24px;
          min-height: 100vh;
          background-image: 
            radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.12) 0px, transparent 50%),
            radial-gradient(at 100% 0%, rgba(139, 92, 246, 0.08) 0px, transparent 50%);
        }}

        .container {{
          max-width: 1280px;
          margin: 0 auto;
        }}

        /* Top Hero Header */
        .hero {{
          background: linear-gradient(135deg, rgba(30, 27, 75, 0.9) 0%, rgba(17, 24, 39, 0.95) 100%);
          border: 1px solid rgba(99, 102, 241, 0.3);
          border-radius: 20px;
          padding: 28px 36px;
          margin-bottom: 28px;
          box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5);
          display: flex;
          justify-content: space-between;
          align-items: center;
          flex-wrap: wrap;
          gap: 20px;
          backdrop-filter: blur(12px);
        }}

        .hero-left h1 {{
          font-size: 26px;
          font-weight: 800;
          letter-spacing: -0.5px;
          margin: 0 0 8px 0;
          background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%);
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          display: flex;
          align-items: center;
          gap: 10px;
        }}

        .hero-left p {{
          color: var(--text-muted);
          margin: 0;
          font-size: 14px;
        }}

        .hero-stats {{
          display: flex;
          gap: 12px;
          flex-wrap: wrap;
        }}

        .stat-badge {{
          background: rgba(255, 255, 255, 0.05);
          border: 1px solid rgba(255, 255, 255, 0.1);
          padding: 8px 14px;
          border-radius: 12px;
          font-size: 13px;
          color: #e2e8f0;
          display: flex;
          align-items: center;
          gap: 8px;
        }}

        /* Action Toolbar */
        .toolbar {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: 16px;
          padding: 16px 24px;
          margin-bottom: 28px;
          flex-wrap: wrap;
          gap: 16px;
        }}

        .btn-cta {{
          border: none;
          padding: 12px 22px;
          border-radius: 10px;
          font-size: 14px;
          font-weight: 700;
          cursor: pointer;
          transition: all 0.2s ease;
          display: inline-flex;
          align-items: center;
          gap: 8px;
          font-family: var(--font-sans);
        }}

        .btn-morning {{
          background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
          color: #fff;
          box-shadow: 0 4px 14px rgba(245, 158, 11, 0.3);
        }}
        .btn-morning:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(245, 158, 11, 0.4); }}

        .btn-evening {{
          background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
          color: #fff;
          box-shadow: 0 4px 14px rgba(99, 102, 241, 0.3);
        }}
        .btn-evening:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(99, 102, 241, 0.4); }}

        .btn-refresh {{
          background: rgba(255, 255, 255, 0.05);
          border: 1px solid var(--border-light);
          color: var(--text);
        }}
        .btn-refresh:hover {{ background: rgba(255, 255, 255, 0.1); }}

        /* Tabs */
        .nav-tabs {{
          display: flex;
          gap: 8px;
          border-bottom: 1px solid var(--border);
          margin-bottom: 24px;
          padding-bottom: 4px;
        }}

        .tab-btn {{
          background: transparent;
          border: none;
          color: var(--text-muted);
          font-size: 14px;
          font-weight: 600;
          padding: 10px 18px;
          cursor: pointer;
          border-radius: 8px;
          transition: all 0.15s ease;
        }}

        .tab-btn.active {{
          color: #fff;
          background: rgba(99, 102, 241, 0.15);
          border-bottom: 2px solid var(--primary);
        }}

        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        /* Run Cards */
        .run-card {{
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: 18px;
          padding: 24px;
          margin-bottom: 22px;
          transition: border-color 0.2s ease, transform 0.2s ease;
          box-shadow: 0 4px 20px -4px rgba(0, 0, 0, 0.3);
        }}

        .run-card:hover {{
          border-color: rgba(99, 102, 241, 0.4);
        }}

        .run-card-header {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          flex-wrap: wrap;
          gap: 12px;
          margin-bottom: 16px;
        }}

        .run-card-meta {{
          display: flex;
          align-items: center;
          gap: 10px;
          flex-wrap: wrap;
        }}

        .slot-badge {{
          font-size: 12px;
          font-weight: 700;
          padding: 5px 12px;
          border-radius: 20px;
        }}

        .run-id-badge {{
          font-family: var(--font-mono);
          font-size: 12px;
          background: rgba(255, 255, 255, 0.05);
          border: 1px solid var(--border);
          padding: 4px 10px;
          border-radius: 8px;
          color: #94a3b8;
        }}

        .status-pill {{
          font-size: 11px;
          font-weight: 800;
          letter-spacing: 0.5px;
          padding: 5px 12px;
          border-radius: 20px;
          display: inline-flex;
          align-items: center;
          gap: 6px;
        }}

        .status-dot {{
          width: 7px;
          height: 7px;
          border-radius: 50%;
          display: inline-block;
        }}

        .run-time-meta {{
          font-size: 13px;
          color: var(--text-muted);
        }}

        /* Stepper */
        .stepper-container {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          margin: 18px 0;
          background: rgba(0, 0, 0, 0.2);
          padding: 14px 20px;
          border-radius: 12px;
          border: 1px solid rgba(255, 255, 255, 0.04);
          overflow-x: auto;
        }}

        .stepper-item {{
          display: flex;
          align-items: center;
          gap: 8px;
          white-space: nowrap;
        }}

        .step-circle {{
          width: 24px;
          height: 24px;
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
          font-size: 12px;
          font-weight: 700;
        }}

        .step-label {{
          font-size: 12px;
          font-weight: 600;
        }}

        .step-done .step-circle {{ background: #10b981; color: #fff; }}
        .step-done .step-label {{ color: #10b981; }}

        .step-active .step-circle {{ background: var(--primary); color: #fff; box-shadow: 0 0 10px var(--primary); }}
        .step-active .step-label {{ color: #fff; }}

        .step-pending .step-circle {{ background: #1e293b; color: #64748b; }}
        .step-pending .step-label {{ color: #64748b; }}

        .step-failed .step-circle {{ background: #ef4444; color: #fff; }}
        .step-failed .step-label {{ color: #ef4444; }}

        .step-line {{
          flex: 1;
          height: 2px;
          background: #1e293b;
          margin: 0 10px;
          min-width: 20px;
        }}
        .step-line.line-active {{ background: #10b981; }}

        .run-title {{
          font-size: 18px;
          font-weight: 700;
          margin: 0 0 8px 0;
          color: #fff;
        }}

        .run-topic {{
          font-size: 14px;
          color: #cbd5e1;
          margin-bottom: 6px;
        }}

        .run-hook {{
          font-size: 13px;
          color: var(--text-muted);
          margin-bottom: 14px;
        }}

        .scenes-chips-container {{
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          margin-top: 10px;
        }}

        .scene-chip {{
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid var(--border);
          padding: 4px 10px;
          border-radius: 6px;
          font-size: 12px;
          color: #94a3b8;
          font-family: var(--font-mono);
        }}

        .run-card-footer {{
          margin-top: 20px;
          padding-top: 18px;
          border-top: 1px solid var(--border);
          display: flex;
          justify-content: flex-end;
          align-items: center;
        }}

        .action-buttons-group {{
          display: flex;
          gap: 10px;
          flex-wrap: wrap;
        }}

        .btn-action {{
          border: none;
          padding: 9px 15px;
          border-radius: 8px;
          font-size: 13px;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.15s ease;
          display: inline-flex;
          align-items: center;
          gap: 6px;
          text-decoration: none;
          font-family: var(--font-sans);
        }}

        .btn-prompts {{
          background: rgba(59, 130, 246, 0.15);
          color: #60a5fa;
          border: 1px solid rgba(59, 130, 246, 0.3);
        }}
        .btn-prompts:hover {{ background: rgba(59, 130, 246, 0.25); }}

        .btn-assemble {{
          background: rgba(234, 179, 8, 0.15);
          color: #facc15;
          border: 1px solid rgba(234, 179, 8, 0.3);
        }}
        .btn-assemble:hover {{ background: rgba(234, 179, 8, 0.25); }}

        .btn-review {{
          background: rgba(168, 85, 247, 0.15);
          color: #c084fc;
          border: 1px solid rgba(168, 85, 247, 0.3);
        }}
        .btn-review:hover {{ background: rgba(168, 85, 247, 0.25); }}

        .btn-watch {{
          background: rgba(16, 185, 129, 0.15);
          color: #34d399;
          border: 1px solid rgba(16, 185, 129, 0.3);
        }}
        .btn-watch:hover {{ background: rgba(16, 185, 129, 0.25); }}

        /* Instructions & History Grid */
        .grid-2 {{
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 22px;
          margin-top: 10px;
        }}
        @media(max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}

        .box-panel {{
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: 16px;
          padding: 22px;
        }}

        .box-panel h3 {{
          margin: 0 0 16px 0;
          font-size: 16px;
          font-weight: 700;
          color: #f8fafc;
          display: flex;
          align-items: center;
          gap: 8px;
        }}

        .history-list, .ledger-list {{
          display: flex;
          flex-direction: column;
          gap: 10px;
          max-height: 480px;
          overflow-y: auto;
          padding-right: 6px;
        }}

        .history-item, .ledger-item {{
          background: rgba(255, 255, 255, 0.02);
          border: 1px solid var(--border);
          border-radius: 10px;
          padding: 12px 16px;
        }}

        .history-topic {{ font-weight: 600; font-size: 14px; color: #e2e8f0; margin-bottom: 4px; }}
        .history-meta, .ledger-meta {{ font-size: 12px; color: var(--text-muted); }}

        .ledger-header {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 6px;
        }}
        .ledger-run {{ font-family: var(--font-mono); font-size: 12px; font-weight: 600; color: #cbd5e1; }}
        .privacy-badge {{
          font-size: 11px;
          font-weight: 700;
          background: rgba(16, 185, 129, 0.15);
          color: #34d399;
          padding: 3px 8px;
          border-radius: 6px;
        }}
        .yt-link {{ color: #60a5fa; text-decoration: none; font-weight: 600; }}
        .yt-link:hover {{ text-decoration: underline; }}

        /* Modal */
        .modal-overlay {{
          display: none;
          position: fixed;
          top: 0; left: 0; right: 0; bottom: 0;
          background: rgba(0, 0, 0, 0.75);
          z-index: 1000;
          align-items: center;
          justify-content: center;
          padding: 20px;
          backdrop-filter: blur(8px);
        }}

        .modal-card {{
          background: #111827;
          border: 1px solid var(--border-light);
          border-radius: 18px;
          max-width: 840px;
          width: 100%;
          max-height: 85vh;
          overflow: hidden;
          display: flex;
          flex-direction: column;
          box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6);
        }}

        .modal-header {{
          padding: 18px 24px;
          background: rgba(30, 27, 75, 0.6);
          border-bottom: 1px solid var(--border);
          display: flex;
          justify-content: space-between;
          align-items: center;
        }}

        .modal-title {{ font-size: 16px; font-weight: 700; color: #fff; margin: 0; }}
        .modal-close {{ background: transparent; border: none; color: #94a3b8; font-size: 22px; cursor: pointer; }}
        .modal-close:hover {{ color: #fff; }}

        .modal-body {{
          padding: 20px 24px;
          overflow-y: auto;
          font-family: var(--font-mono);
          font-size: 13px;
          line-height: 1.6;
          color: #cbd5e1;
          white-space: pre-wrap;
          background: rgba(0, 0, 0, 0.2);
        }}

        .modal-footer {{
          padding: 16px 24px;
          background: #111827;
          border-top: 1px solid var(--border);
          display: flex;
          justify-content: space-between;
          align-items: center;
        }}

        /* Toast notifications */
        .toast {{
          display: none;
          position: fixed;
          bottom: 24px;
          right: 24px;
          background: #1e1b4b;
          border: 1px solid rgba(99, 102, 241, 0.4);
          color: #fff;
          padding: 14px 22px;
          border-radius: 12px;
          font-size: 14px;
          font-weight: 600;
          box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
          z-index: 2000;
        }}
      </style>
    </head>
    <body>
      <div class="container">
        <!-- Hero Header -->
        <div class="hero">
          <div class="hero-left">
            <h1>🎬 AI Engineering Shorts Studio</h1>
            <p>Automated YouTube Shorts Production Engine &bull; Dual Daily Runs with Human Approval</p>
          </div>
          <div class="hero-stats">
            <div class="stat-badge">
              <span style="color:#6366f1;">⚡ Model:</span> <b>{settings.GROQ_MODEL}</b>
            </div>
            <div class="stat-badge">
              <span style="color:#10b981;">🕒 Timezone:</span> <b>{settings.TIMEZONE}</b>
            </div>
            <div class="stat-badge">
              <span style="color:#f59e0b;">📅 Schedules:</span> <b>07:00 & 16:30 IST</b>
            </div>
          </div>
        </div>

        <!-- Action Toolbar -->
        <div class="toolbar">
          <div style="display:flex; gap:12px; flex-wrap:wrap;">
            <button onclick="triggerGenerate('morning')" class="btn-cta btn-morning">
              ☀️ Generate Morning Short (07:00 IST)
            </button>
            <button onclick="triggerGenerate('evening')" class="btn-cta btn-evening">
              🌙 Generate Evening Short (16:30 IST)
            </button>
          </div>
          <button onclick="location.reload()" class="btn-cta btn-refresh">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
            Refresh Pipeline
          </button>
        </div>

        <!-- Navigation Tabs -->
        <div class="nav-tabs">
          <button class="tab-btn active" onclick="switchTab('tab-pipeline')">🚀 Production Pipeline ({len(packages)})</button>
          <button class="tab-btn" onclick="switchTab('tab-flow-guide')">🎨 Google Flow Handoff Guide</button>
          <button class="tab-btn" onclick="switchTab('tab-history')">🛡️ Topic History ({len(history)})</button>
          <button class="tab-btn" onclick="switchTab('tab-ledger')">📤 YouTube Publishing Ledger ({len(ledger)})</button>
        </div>

        <!-- Tab 1: Pipeline Runs -->
        <div id="tab-pipeline" class="tab-content active">
          {cards_html}
        </div>

        <!-- Tab 2: Google Flow Guide -->
        <div id="tab-flow-guide" class="tab-content">
          <div class="box-panel" style="line-height: 1.7;">
            <h3>🎨 Google Flow Visual Generation Guide</h3>
            <p style="color:var(--text-muted); font-size:14px;">
              Google Flow generation operates under your <b>Google AI Pro</b> subscription through an agreed manual handoff boundary. Follow these 4 steps for each production run:
            </p>
            <ol style="color:#e2e8f0; font-size:14px; padding-left:20px;">
              <li>Click <b>Google Flow Prompts Brief</b> on the active run above (or open <code>data/flow_exports/&lt;run_id&gt;/FLOW_PROMPTS.txt</code>).</li>
              <li>Open <b>Google Flow</b> in your browser and set the format to <b>Vertical (9:16)</b>.</li>
              <li>For each scene (1 to 5), paste the generated visual prompt and set the matching duration (e.g. 8s).</li>
              <li>Export each video clip and save into your project folder:
                <br><code>data/flow_exports/&lt;run_id&gt;/scene_01.mp4</code> ... <code>scene_05.mp4</code>
              </li>
              <li>Click <b>Assemble Video (FFmpeg)</b> on the card above. FFmpeg will stitch the clips, standardize to 1080x1920, and add the neural voiceover!</li>
            </ol>
          </div>
        </div>

        <!-- Tab 3: Topic History -->
        <div id="tab-history" class="tab-content">
          <div class="box-panel">
            <h3>🛡️ Persistent Topic History (Anti-Duplication)</h3>
            <p style="color:var(--text-muted); font-size:13px; margin-bottom:16px;">
              The system queries this history before every run to guarantee that no topic angle is duplicated across morning or evening slots.
            </p>
            <div class="history-list">
              {history_items}
            </div>
          </div>
        </div>

        <!-- Tab 4: YouTube Ledger -->
        <div id="tab-ledger" class="tab-content">
          <div class="box-panel">
            <h3>📤 YouTube Upload Ledger (Idempotency Guard)</h3>
            <p style="color:var(--text-muted); font-size:13px; margin-bottom:16px;">
              Approved videos uploaded to your channel are tracked here with SHA-256 hashes to prevent accidental duplicate uploads.
            </p>
            <div class="ledger-list">
              {ledger_items}
            </div>
          </div>
        </div>
      </div>

      <!-- Flow Brief Modal -->
      <div id="briefModal" class="modal-overlay">
        <div class="modal-card">
          <div class="modal-header">
            <h3 class="modal-title" id="modalTitle">Google Flow Generation Brief</h3>
            <button onclick="closeModal()" class="modal-close">&times;</button>
          </div>
          <div id="modalBody" class="modal-body"></div>
          <div class="modal-footer">
            <span style="font-size:12px; color:var(--text-muted);">Paste these prompts into Google Flow in 9:16 vertical mode</span>
            <button onclick="copyBrief()" class="btn-cta btn-evening">📋 Copy All Prompts</button>
          </div>
        </div>
      </div>

      <!-- Toast Notification -->
      <div id="toast" class="toast"></div>

      <script>
        function showToast(msg, isError = false) {{
          const t = document.getElementById("toast");
          t.innerText = msg;
          t.style.borderColor = isError ? "rgba(239, 68, 68, 0.8)" : "rgba(16, 185, 129, 0.8)";
          t.style.background = isError ? "#450a0a" : "#064e3b";
          t.style.display = "block";
          setTimeout(() => {{ t.style.display = "none"; }}, 4500);
        }}

        function switchTab(tabId) {{
          document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
          document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
          document.getElementById(tabId).classList.add('active');
          event.target.classList.add('active');
        }}

        async function triggerGenerate(slot) {{
          showToast("AI Agent generating " + slot.toUpperCase() + " Short package with Groq... Please wait ~15-25s.");
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
              showToast("Video assembled successfully! Status updated to awaiting approval.");
              setTimeout(() => location.reload(), 1500);
            }} else {{
              showToast("Assembly failed: " + (data.detail || "Check scene clips"), true);
            }}
          }} catch (e) {{
            showToast("Assembly error: " + e, true);
          }}
        }}

        async function notifyRun(runId) {{
          showToast("Generating secure one-time approval link...");
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
          showToast("Prompts copied to clipboard! Paste into Google Flow.");
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
# Video Streaming & Redesigned Review Gate Endpoints
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

    scenes_cards = "".join(
        f"""<div class="scene-review-card">
          <div class="scene-review-header">
            <span class="scene-num">Scene {s.scene_number}</span>
            <span class="scene-dur">{s.duration_seconds} seconds</span>
            <span class="scene-overlay"><b>Overlay:</b> "{s.on_screen_text}"</span>
          </div>
          <div class="scene-voice"><b>Voiceover:</b> "{s.narration}"</div>
          <div class="scene-prompt"><b>Flow Visual Prompt:</b> {s.visual_prompt}</div>
        </div>"""
        for s in package.scenes
    )

    tags_html = " ".join(f"<span class='tag-chip'>{t}</span>" for t in package.metadata.tags)
    hashtags_html = " ".join(f"<span class='hashtag-chip'>{h}</span>" for h in package.metadata.hashtags)

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Review & Authorize: {package.metadata.title}</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <link rel="preconnect" href="https://fonts.googleapis.com">
      <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
      <style>
        :root {{
          --bg: #090d16;
          --card: #111726;
          --border: #1e293b;
          --border-light: #334155;
          --text: #f8fafc;
          --text-muted: #94a3b8;
          --primary: #6366f1;
          --success: #10b981;
          --danger: #ef4444;
          --font-sans: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
          --font-mono: 'JetBrains Mono', monospace;
        }}

        * {{ box-sizing: border-box; }}
        body {{
          font-family: var(--font-sans);
          background-color: var(--bg);
          color: var(--text);
          margin: 0;
          padding: 24px;
          min-height: 100vh;
          background-image: radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.1) 0px, transparent 50%);
        }}

        .container {{
          max-width: 1100px;
          margin: 0 auto;
        }}

        .review-header {{
          background: linear-gradient(135deg, rgba(30, 27, 75, 0.9) 0%, rgba(17, 24, 39, 0.95) 100%);
          border: 1px solid rgba(99, 102, 241, 0.3);
          border-radius: 16px;
          padding: 20px 28px;
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 24px;
        }}

        .review-header h2 {{
          margin: 0 0 4px 0;
          font-size: 20px;
          font-weight: 800;
          color: #fff;
        }}

        .review-grid {{
          display: grid;
          grid-template-columns: 360px 1fr;
          gap: 28px;
        }}
        @media(max-width: 860px) {{ .review-grid {{ grid-template-columns: 1fr; }} }}

        /* 9:16 Video Mockup Container */
        .video-box {{
          background: #000;
          border-radius: 20px;
          overflow: hidden;
          box-shadow: 0 10px 40px rgba(0, 0, 0, 0.7);
          border: 2px solid var(--border-light);
          position: sticky;
          top: 24px;
        }}

        video {{
          width: 100%;
          display: block;
          aspect-ratio: 9/16;
          background: #000;
        }}

        .video-meta {{
          padding: 14px;
          text-align: center;
          font-size: 13px;
          color: var(--text-muted);
          background: #0d121f;
          border-top: 1px solid var(--border);
        }}

        /* Action Buttons Box */
        .decision-panel {{
          margin-top: 18px;
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: 14px;
          padding: 18px;
        }}

        .btn-approve {{
          background: linear-gradient(135deg, #10b981 0%, #059669 100%);
          color: #fff;
          width: 100%;
          border: none;
          padding: 14px 20px;
          border-radius: 10px;
          font-size: 15px;
          font-weight: 700;
          cursor: pointer;
          box-shadow: 0 4px 14px rgba(16, 185, 129, 0.3);
          transition: transform 0.15s ease;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
        }}
        .btn-approve:hover {{ transform: translateY(-2px); }}

        .btn-reject {{
          background: rgba(239, 68, 68, 0.15);
          color: #f87171;
          border: 1px solid rgba(239, 68, 68, 0.3);
          width: 100%;
          border: none;
          padding: 12px 18px;
          border-radius: 10px;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          margin-top: 10px;
        }}
        .btn-reject:hover {{ background: rgba(239, 68, 68, 0.25); }}

        /* Content Panel */
        .content-panel {{
          display: flex;
          flex-direction: column;
          gap: 18px;
        }}

        .detail-card {{
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: 14px;
          padding: 20px;
        }}

        .detail-card h3 {{
          margin: 0 0 12px 0;
          font-size: 16px;
          font-weight: 700;
          color: #fff;
        }}

        .tag-chip {{
          background: rgba(99, 102, 241, 0.15);
          border: 1px solid rgba(99, 102, 241, 0.3);
          color: #a5b4fc;
          font-size: 12px;
          padding: 4px 10px;
          border-radius: 14px;
          display: inline-block;
          margin: 3px;
        }}

        .hashtag-chip {{
          color: #60a5fa;
          font-weight: 700;
          font-size: 13px;
          margin-right: 8px;
        }}

        .scene-review-card {{
          background: rgba(255, 255, 255, 0.02);
          border: 1px solid var(--border);
          border-radius: 10px;
          padding: 14px;
          margin-bottom: 12px;
        }}

        .scene-review-header {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 8px;
          flex-wrap: wrap;
          gap: 8px;
        }}

        .scene-num {{ font-weight: 700; color: #a5b4fc; font-size: 13px; }}
        .scene-dur {{ font-size: 12px; color: var(--text-muted); font-family: var(--font-mono); }}
        .scene-overlay {{ font-size: 13px; color: #f1f5f9; }}
        .scene-voice {{ font-size: 13px; color: #cbd5e1; margin-bottom: 6px; line-height: 1.5; }}
        .scene-prompt {{ font-size: 12px; color: #64748b; font-family: var(--font-mono); line-height: 1.4; }}
      </style>
    </head>
    <body>
      <div class="container">
        <!-- Review Header -->
        <div class="review-header">
          <div>
            <h2>🛡️ Short Review & Approval Gate</h2>
            <div style="font-size:13px; color:var(--text-muted);">Run ID: <code>{package.run_id}</code> &bull; Slot: {package.slot.upper()}</div>
          </div>
          <span style="background:rgba(168, 85, 247, 0.2); color:#c084fc; border:1px solid rgba(168, 85, 247, 0.4); padding:6px 14px; border-radius:20px; font-size:12px; font-weight:700;">
            Awaiting Human Decision
          </span>
        </div>

        <div class="review-grid">
          <!-- Left Column: Video Player & Approval Action -->
          <div>
            <div class="video-box">
              <video controls autoplay muted loop>
                <source src="/video/{package.run_id}" type="video/mp4">
                Your browser does not support video preview.
              </video>
              <div class="video-meta">
                📐 <b>1080x1920 (9:16)</b> &bull; ⏱️ <b>{package.total_duration_seconds}s</b> &bull; H.264 / AAC
              </div>
            </div>

            <div class="decision-panel">
              <!-- Form 1: Approve (Explicit POST) -->
              <form method="POST" action="/approve/{package.run_id}">
                <input type="hidden" name="token" value="{token}">
                <button type="submit" class="btn-approve">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                  Approve & Upload Privately
                </button>
              </form>

              <!-- Form 2: Reject (Explicit POST) -->
              <form method="POST" action="/reject/{package.run_id}" style="margin-top:10px;">
                <input type="hidden" name="token" value="{token}">
                <input type="text" name="comments" placeholder="Optional rejection feedback..." style="width:100%; padding:10px; margin-bottom:8px; border:1px solid var(--border); border-radius:8px; background:#0b0f19; color:#fff; font-size:13px;">
                <button type="submit" class="btn-reject">
                  &cross; Reject & Cancel Upload
                </button>
              </form>
            </div>
          </div>

          <!-- Right Column: Metadata & Breakdown -->
          <div class="content-panel">
            <div class="detail-card">
              <h1 style="font-size:22px; margin:0 0 10px 0; color:#fff;">{package.metadata.title}</h1>
              <div style="font-size:14px; color:#cbd5e1; margin-bottom:8px;"><b>Topic:</b> {package.topic}</div>
              <div style="font-size:14px; color:#94a3b8; margin-bottom:12px;"><b>Opening Hook:</b> <i>"{package.hook}"</i></div>
              <div>{hashtags_html}</div>
            </div>

            <div class="detail-card">
              <h3>YouTube SEO Description</h3>
              <p style="white-space:pre-wrap; margin:0; font-size:13px; color:#cbd5e1; line-height:1.6;">{package.metadata.description}</p>
            </div>

            <div class="detail-card">
              <h3>YouTube Keyword Tags</h3>
              <div>{tags_html}</div>
            </div>

            <div class="detail-card">
              <h3>Scene Breakdown ({len(package.scenes)} scenes)</h3>
              {scenes_cards}
            </div>
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
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Video Approved</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{ font-family: -apple-system, sans-serif; background: #090d16; color: #fff; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }}
        .card {{ background:#111726; padding:40px; border-radius:20px; border:1px solid #1e293b; max-width:520px; text-align:center; box-shadow:0 20px 40px rgba(0,0,0,0.5); }}
        h1 {{ color:#10b981; font-size:26px; margin-top:0; }}
        p {{ color:#cbd5e1; line-height:1.6; }}
        a {{ color: #6366f1; text-decoration: none; font-weight: bold; }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>&check; Short Approved!</h1>
        <p>Run ID: <b>{run_id}</b></p>
        <p style="background:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.3); padding:12px; border-radius:10px; color:#34d399;">{upload_result_msg}</p>
        <p style="font-size:12px; color:#64748b;">Single-use token consumed at {now_utc}. Replay attacks are permanently blocked.</p>
        <div style="margin-top:24px;">
          <a href="/">&larr; Return to Studio Dashboard</a>
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
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Video Rejected</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{ font-family: -apple-system, sans-serif; background: #090d16; color: #fff; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }}
        .card {{ background:#111726; padding:40px; border-radius:20px; border:1px solid #1e293b; max-width:520px; text-align:center; box-shadow:0 20px 40px rgba(0,0,0,0.5); }}
        h1 {{ color:#ef4444; font-size:26px; margin-top:0; }}
        p {{ color:#cbd5e1; line-height:1.6; }}
        a {{ color: #6366f1; text-decoration: none; font-weight: bold; }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>&cross; Video Rejected</h1>
        <p>Run ID: <b>{run_id}</b> has been closed without uploading to YouTube.</p>
        <p><i>Feedback: {comments or 'None provided'}</i></p>
        <div style="margin-top:24px;">
          <a href="/">&larr; Return to Studio Dashboard</a>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

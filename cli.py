import argparse
import os
import sys
from pathlib import Path

import uvicorn
from app.assemble import assemble_video
from app.config import settings
from app.logging_config import logger
from app.pipeline import run_content_pipeline
from app.scheduler import start_scheduler_forever
from app.server import app as fastapi_app
from app.storage import storage
from app.tts import generate_speech_sync
from app.youtube import authorize_youtube_interactive


def cmd_generate(args):
    """Generate script and Google Flow brief."""
    print(f"\n[1/2] Generating AI Engineering Short package for {args.slot.upper()} slot...")
    pkg = run_content_pipeline(slot=args.slot, force_mock=args.mock)
    print(f"\nSUCCESS! Package Created:")
    print(f"  Run ID:        {pkg.run_id}")
    print(f"  Topic:         {pkg.topic}")
    print(f"  Target Length: {pkg.total_duration_seconds} seconds")
    print(f"  Title:         {pkg.metadata.title}")
    print(f"  Scenes:        {len(pkg.scenes)}")
    print(f"  Brief Path:    data/flow_exports/{pkg.run_id}/FLOW_PROMPTS.txt")
    print("\nNext step: Open Google Flow, export scene clips to data/flow_exports/{pkg.run_id}/, then run:")
    print(f"  python cli.py assemble --run-id {pkg.run_id}")


def cmd_assemble(args):
    """Assemble exported Google Flow clips with FFmpeg."""
    print(f"\n[2/3] Assembling video for Run ID: {args.run_id}...")
    try:
        final_mp4 = assemble_video(args.run_id)
        print(f"\nSUCCESS! Video Assembled & Validated:")
        print(f"  Final Output: {final_mp4}")
        print(f"  Size:         {final_mp4.stat().st_size:,} bytes")
        print("\nNext step: To preview and send approval email, run:")
        print(f"  python cli.py notify --run-id {args.run_id}")
    except Exception as e:
        print(f"\nERROR: Assembly failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_notify(args):
    """Generate approval token and send review email."""
    print(f"\n[3/3] Sending approval request for Run ID: {args.run_id}...")
    from app.emailer import create_approval_record, send_approval_email
    pkg = storage.load_package(args.run_id)
    if not pkg:
        print(f"ERROR: Run ID {args.run_id} not found in packages.", file=sys.stderr)
        sys.exit(1)

    final_mp4 = settings.FINAL_OUTPUT_DIR / args.run_id / "final_short.mp4"
    if not final_mp4.exists():
        print(f"ERROR: Final video not found at {final_mp4}. Run 'assemble' first.", file=sys.stderr)
        sys.exit(1)

    raw_token, record = create_approval_record(args.run_id)
    send_approval_email(pkg, raw_token)
    storage.update_package_status(args.run_id, "awaiting_approval")
    review_url = f"{settings.APPROVAL_BASE_URL.rstrip('/')}/review/{args.run_id}?token={raw_token}"
    print(f"\nSUCCESS! Review Link Generated:")
    print(f"  Review URL: {review_url}")
    print(f"  Recipient:  {settings.APPROVAL_RECIPIENT_EMAIL or 'Console Dry-Run'}")
    print("\nMake sure the approval server is running:")
    print("  python cli.py serve")


def cmd_serve(args):
    """Run FastAPI approval web service."""
    host = args.host
    port = args.port
    print(f"\nStarting Approval Portal at http://{host}:{port}...")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


def cmd_youtube_auth(args):
    """Authorize YouTube Data API v3 OAuth interactively to generate token.json."""
    print("\nStarting YouTube OAuth browser authorization...")
    try:
        token_path = authorize_youtube_interactive()
        print(f"\nSUCCESS! YouTube OAuth token saved to: {token_path}")
        print("For Render deployment, add this file as a Secret File or set YOUTUBE_TOKEN_JSON.")
    except Exception as e:
        print(f"\nERROR: YouTube OAuth failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_schedule(args):
    """Start APScheduler daily automated runs."""
    print(f"\nStarting APScheduler service in {settings.TIMEZONE}...")
    print(f"  Morning slot: 07:00 AM IST")
    print(f"  Evening slot: 04:30 PM (16:30) IST")
    start_scheduler_forever()


def cmd_status(args):
    """Print system status, recent packages, and topic history."""
    print("=" * 65)
    print("AI Engineering Shorts Automation - System Status")
    print("=" * 65)
    packages = storage.list_packages()
    print(f"\nTotal Production Packages: {len(packages)}")
    for p in packages[-5:]:
        print(f"  - [{p.status.upper()}] {p.run_id} | {p.topic} ({p.total_duration_seconds}s)")

    history = storage.get_topic_history()
    print(f"\nCovered Topics in History: {len(history)}")
    for h in history[-5:]:
        print(f"  - {h.get('topic')} ({h.get('run_id')})")

    upload_file = settings.UPLOAD_LEDGER_FILE
    if upload_file.exists():
        import json
        with open(upload_file, "r", encoding="utf-8") as f:
            ledger = json.load(f)
        print(f"\nUploaded Videos Ledger: {len(ledger)}")
        for run_id, u in ledger.items():
            print(f"  - {run_id} -> YouTube ID: {u.get('youtube_video_id')} (Private)")
    print("=" * 65)


def cmd_test_tts(args):
    """Test Edge-TTS voice generation."""
    print("\nTesting Edge-TTS speech generation...")
    test_out = Path("data/final/test_tts.mp3")
    test_srt = Path("data/final/test_tts.srt")
    generate_speech_sync(
        "Welcome to the AI Engineering channel. Today we are breaking down Model Context Protocol architectures.",
        test_out,
        test_srt
    )
    print(f"SUCCESS: Generated {test_out} ({test_out.stat().st_size} bytes) and {test_srt}")


def main():
    parser = argparse.ArgumentParser(
        description="AI Engineering YouTube Shorts Automation CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # generate
    p_gen = subparsers.add_parser("generate", help="Run content generation pipeline")
    p_gen.add_argument("--slot", choices=["morning", "evening"], default="morning", help="Schedule slot")
    p_gen.add_argument("--mock", action="store_true", help="Force mock LLM for testing without API key")
    p_gen.set_defaults(func=cmd_generate)

    # assemble
    p_asm = subparsers.add_parser("assemble", help="Assemble video clips with FFmpeg")
    p_asm.add_argument("--run-id", required=True, help="Run ID to assemble")
    p_asm.set_defaults(func=cmd_assemble)

    # notify
    p_not = subparsers.add_parser("notify", help="Send approval email and generate token")
    p_not.add_argument("--run-id", required=True, help="Run ID to notify")
    p_not.set_defaults(func=cmd_notify)

    # serve
    p_srv = subparsers.add_parser("serve", help="Run FastAPI approval web server")
    p_srv.add_argument("--host", default=settings.SERVER_HOST, help="Host to bind")
    p_srv.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", settings.SERVER_PORT)),
        help="Port to bind",
    )
    p_srv.set_defaults(func=cmd_serve)

    # youtube-auth
    p_yt = subparsers.add_parser("youtube-auth", help="Authorize YouTube OAuth interactively and save token.json")
    p_yt.set_defaults(func=cmd_youtube_auth)

    # schedule
    p_sch = subparsers.add_parser("schedule", help="Start APScheduler daily background service")
    p_sch.set_defaults(func=cmd_schedule)

    # status
    p_sts = subparsers.add_parser("status", help="Show system status and recent runs")
    p_sts.set_defaults(func=cmd_status)

    # test-tts
    p_tts = subparsers.add_parser("test-tts", help="Test Edge-TTS voice generation")
    p_tts.set_defaults(func=cmd_test_tts)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

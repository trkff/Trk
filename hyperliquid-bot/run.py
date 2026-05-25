"""
Entry point — starts both the dashboard and the bot.
Dashboard runs on localhost:8080.
Bot runs in a background thread (started via dashboard controls).
"""

import sys
import os

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot import db
from bot.logger import setup_logger, get_logger
from dashboard.app import create_app

log = get_logger("run")


def main():
    # Initialize database
    db.init_db()
    db.migrate_db()

    # Setup logging
    cfg = db.get_all_config()
    debug = cfg.get("debug_logging", "false").lower() == "true"
    setup_logger("bot", debug=debug)

    log.info("=" * 50)
    log.info("RazorHL — Hyperliquid Scalping Bot")
    log.info("=" * 50)

    if not db.is_configured():
        log.info("Bot not configured yet. Open http://localhost:8080 to set up credentials.")
    else:
        log.info("Configuration found. Use the dashboard to start the bot.")

    # Create and run Flask app
    app, socketio = create_app()
    log.info("Dashboard available at http://localhost:8080")

    # Auto-restart bot if it was running before pm2 restart
    if db.is_configured() and db.get_config("bot_status") in ("running", "paused"):
        log.info("Bot was running before restart — auto-starting...")
        from main import start_bot
        start_bot()

    socketio.run(app, host="0.0.0.0", port=8080, debug=False, use_reloader=False,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()

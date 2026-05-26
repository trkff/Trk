"""
Dashboard web server — Flask + SocketIO.
Serves the trading bot dashboard on localhost:8080.
Auto-refreshes data every 5 seconds via SocketIO.
"""

import json
import threading
import time

from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO

from bot import db
from bot.logger import get_logger, set_debug

log = get_logger("dashboard")


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "hl-bot-dashboard-secret"
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    # ── Pages ───────────────────────────────────────────────────────

    @app.after_request
    def no_cache(response):
        if "text/html" in response.content_type:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.before_request
    def check_configured():
        # API calls always pass through — only redirect HTML page requests
        if request.endpoint and not request.endpoint.startswith("api_") and request.endpoint not in (
            "config_page", "static", "backtest_page", "scanner_page", "strategies_page", "ativos_page", "analise_page",
        ):
            if not db.is_configured():
                return redirect(url_for("config_page"))

    @app.route("/")
    def overview():
        return render_template("overview.html", page="overview")

    @app.route("/trades")
    def trades_page():
        return render_template("trades.html", page="trades")

    @app.route("/signals")
    def signals_page():
        return render_template("signals.html", page="signals")

    @app.route("/logs")
    def logs_page():
        return render_template("logs.html", page="logs")

    @app.route("/config")
    def config_page():
        return render_template("config.html", page="config")

    @app.route("/backtest")
    def backtest_page():
        return render_template("backtest.html", page="backtest")

    @app.route("/scanner")
    def scanner_page():
        return render_template("scanner.html", page="scanner")

    @app.route("/strategies")
    def strategies_page():
        return render_template("strategies.html", page="strategies")

    @app.route("/ativos")
    def ativos_page():
        return render_template("ativos.html", page="ativos")

    @app.route("/analise")
    def analise_page():
        return render_template("analise.html", page="analise")

    # ── API endpoints ───────────────────────────────────────────────

    @app.route("/api/overview")
    def api_overview():
        from main import get_asset_live_status
        cfg = db.get_all_config()
        stats = db.get_trade_stats()
        open_trades = db.get_open_trades()
        daily_pnl = db.get_daily_pnl()
        total_pnl = db.get_total_pnl()

        return jsonify({
            "bot_status": cfg.get("bot_status", "stopped"),
            "use_testnet": cfg.get("use_testnet", "true"),
            "daily_pnl": round(daily_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(stats["win_rate"], 1),
            "today_count": stats["today_count"],
            "total_closed": stats["total_closed"],
            "open_trades": open_trades,
            "asset_status": get_asset_live_status(),
            "strategy_stats": db.get_strategy_stats(),
        })

    @app.route("/api/trades")
    def api_trades():
        asset = request.args.get("asset")
        side = request.args.get("side")
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        strategy = request.args.get("strategy")
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        trades = db.get_trades(limit, offset, asset, side, date_from, date_to, strategy=strategy)
        return jsonify(trades)

    @app.route("/api/trades/cumulative-pnl")
    def api_cumulative_pnl():
        data = db.get_cumulative_pnl()
        return jsonify(data)

    @app.route("/api/trades/pnl-distribution")
    def api_pnl_distribution():
        data = db.get_pnl_distribution()
        return jsonify(data)

    @app.route("/api/strategy-stats")
    def api_strategy_stats():
        days = request.args.get("days", type=int)
        return jsonify(db.get_strategy_stats(days=days))

    @app.route("/api/signals")
    def api_signals():
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        strategy = request.args.get("strategy")
        signals = db.get_signals(limit, offset, strategy_name=strategy)
        return jsonify(signals)

    @app.route("/api/strategies", methods=["GET"])
    def api_strategies():
        from bot.strategies.manager import get_all_strategy_metadata
        return jsonify(get_all_strategy_metadata())

    @app.route("/api/strategies/<name>", methods=["POST"])
    def api_save_strategy(name):
        from bot.strategies.manager import STRATEGY_MAP
        if name not in STRATEGY_MAP:
            return jsonify({"error": f"Unknown strategy: {name}"}), 404
        data = request.get_json() or {}
        enabled = bool(data.get("enabled", False))
        params = data.get("params", {})
        db.set_strategy_config(name, enabled, params)
        log.info(f"Strategy '{name}' updated: enabled={enabled}")
        return jsonify({"ok": True})

    @app.route("/api/strategies/applied", methods=["GET"])
    def api_strategies_applied():
        """Lista estratégias para a aba Estratégias:
        - Todas com scanner_metrics (aplicadas via Scanner)
        - PLUS todas que estão enabled=true (mesmo sem scanner_metrics — configuração legada)
        """
        import json as _json
        from bot.strategies.manager import STRATEGY_MAP
        all_cfg = db.get_all_config()

        metrics_by_name: dict[str, dict] = {}
        for key, val in all_cfg.items():
            if key.startswith("strategy.") and key.endswith(".scanner_metrics"):
                inst_name = key[len("strategy."):-len(".scanner_metrics")]
                try:
                    m = _json.loads(val)
                except _json.JSONDecodeError:
                    continue
                if m.get("archived"):
                    continue
                metrics_by_name[inst_name] = m

        candidates = set(metrics_by_name.keys())
        for key, val in all_cfg.items():
            if key.startswith("strategy.") and key.endswith(".enabled") and val == "true":
                inst_name = key[len("strategy."):-len(".enabled")]
                candidates.add(inst_name)

        result = []
        for inst_name in candidates:
            if inst_name not in STRATEGY_MAP:
                continue
            scfg = db.get_strategy_config(inst_name)
            strategy = STRATEGY_MAP[inst_name]
            result.append({
                "name":         inst_name,
                "display_name": strategy.DISPLAY_NAME,
                "enabled":      scfg["enabled"],
                "params":       {**strategy.DEFAULT_PARAMS, **scfg["params"]},
                "metrics":      metrics_by_name.get(inst_name, {}),
            })
        result.sort(key=lambda x: (
            0 if x["metrics"] else 1,                           # com métricas primeiro
            -1 * (x["metrics"].get("applied_at", "") != ""),    # depois por data
            x["name"],
        ))
        return jsonify(result)

    @app.route("/api/analise")
    def api_analise():
        """Cruza scanner metrics com performance ao vivo para análise de padrões.
        Retorna lista de strategies aplicadas (com scanner_metrics) com:
        - scanner: trades, wr, pf, roi, tpd, max_dd
        - live: closed_total, wins, win_rate, pnl, pnl_per_trade, avg_slippage_pct
        """
        import json as _json
        from bot.strategies.manager import STRATEGY_MAP
        all_cfg = db.get_all_config()
        stats_by_name = {s["strategy"]: s for s in db.get_strategy_stats()}

        result = []
        for key, val in all_cfg.items():
            if not (key.startswith("strategy.") and key.endswith(".scanner_metrics")):
                continue
            inst_name = key[len("strategy."):-len(".scanner_metrics")]
            if inst_name not in STRATEGY_MAP:
                continue
            try:
                m = _json.loads(val)
            except _json.JSONDecodeError:
                continue
            stats = stats_by_name.get(inst_name, {})
            closed = stats.get("closed_total", 0) or 0
            pnl = stats.get("pnl", 0.0) or 0.0
            pnl_per_trade = (pnl / closed) if closed > 0 else None
            scfg = db.get_strategy_config(inst_name)
            result.append({
                "name": inst_name,
                "display_name": STRATEGY_MAP[inst_name].DISPLAY_NAME,
                "enabled": scfg["enabled"],
                "archived": bool(m.get("archived", False)),
                "asset": m.get("asset"),
                "tag": m.get("tag"),
                "timeframe": m.get("timeframe") or "5m",
                "scanner": {
                    "trades":  m.get("trades"),
                    "wr":      m.get("wr"),
                    "pf":      m.get("pf"),
                    "roi":     m.get("roi"),
                    "tpd":     m.get("tpd"),
                    "max_dd":  m.get("max_dd"),
                },
                "live": {
                    "closed_total":      closed,
                    "wins":              stats.get("wins", 0),
                    "win_rate":          stats.get("win_rate", 0),
                    "pnl":               pnl,
                    "pnl_per_trade":     pnl_per_trade,
                    "avg_slippage_pct":  stats.get("avg_slippage_pct"),
                    "open_count":        stats.get("open_count", 0),
                },
            })
        return jsonify(result)

    @app.route("/api/strategies/applied/<name>", methods=["DELETE"])
    def api_strategy_applied_delete(name):
        """Soft-delete: marca scanner_metrics como archived=true e desativa.
        A aba Estratégias filtra arquivadas; a aba Análise continua incluindo (para
        preservar histórico de scanner_metrics × performance live)."""
        import json as _json
        db.set_config(f"strategy.{name}.enabled", "false")
        raw = db.get_config(f"strategy.{name}.scanner_metrics")
        if raw:
            try:
                m = _json.loads(raw)
            except _json.JSONDecodeError:
                m = {}
            m["archived"] = True
            db.set_config(f"strategy.{name}.scanner_metrics", _json.dumps(m))
        log.info(f"Strategy '{name}' arquivada (some da aba Estratégias; permanece em Análise)")
        return jsonify({"ok": True})

    @app.route("/api/backtest/run", methods=["POST"])
    def api_backtest_run():
        from bot.backtest.engine import start_backtest_job
        data = request.get_json() or {}
        job_id = start_backtest_job(
            data.get("strategy", "mean_reversion"),
            data.get("asset", "BTC").upper(),
            int(data.get("days", 90)),
            float(data.get("trade_size_usd", 1000.0)),
            float(data.get("fee_rate", 0.0009)),
        )
        return jsonify({"job_id": job_id})

    @app.route("/api/backtest/status/<job_id>")
    def api_backtest_status(job_id):
        from bot.backtest.engine import get_job
        job = get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    # ── Ativos API (download de candles novos) ─────────────────────

    # In-memory job registry for downloads: {job_id: {asset, status, message, result, started_at}}
    _ativos_jobs: dict = {}
    _ativos_jobs_lock = threading.Lock()

    @app.route("/api/ativos")
    def api_ativos_list():
        from bot.backtest.csv_loader import (
            list_lighter_perp_markets, get_csv_status, SUPPORTED_DOWNLOAD_INTERVALS,
        )
        interval = (request.args.get("interval") or "5m").strip()
        if interval not in SUPPORTED_DOWNLOAD_INTERVALS:
            return jsonify({"error": f"intervalo inválido: {interval}"}), 400
        markets = list_lighter_perp_markets()
        out = []
        for m in markets:
            status = get_csv_status(m["symbol"], interval)
            out.append({**m, **status, "interval": interval})
        out.sort(key=lambda x: (
            0 if x["has_csv"] and x["rows"] > 0 else 1,
            -x["rows"],
            x["symbol"],
        ))
        return jsonify(out)

    @app.route("/api/ativos/intervals")
    def api_ativos_intervals():
        from bot.backtest.csv_loader import SUPPORTED_DOWNLOAD_INTERVALS
        return jsonify(SUPPORTED_DOWNLOAD_INTERVALS)

    @app.route("/api/ativos/download", methods=["POST"])
    def api_ativos_download():
        from bot.backtest.csv_loader import download_full_history, SUPPORTED_DOWNLOAD_INTERVALS
        import uuid
        data = request.get_json() or {}
        asset = (data.get("asset") or "").upper().strip()
        interval = (data.get("interval") or "5m").strip()
        if not asset:
            return jsonify({"error": "asset required"}), 400
        if interval not in SUPPORTED_DOWNLOAD_INTERVALS:
            return jsonify({"error": f"intervalo inválido: {interval}"}), 400

        job_key = f"{asset}|{interval}"
        with _ativos_jobs_lock:
            for jid, j in _ativos_jobs.items():
                if j.get("key") == job_key and j["status"] == "running":
                    return jsonify({"job_id": jid, "existing": True})

        job_id = uuid.uuid4().hex
        _ativos_jobs[job_id] = {
            "asset": asset, "interval": interval, "key": job_key,
            "status": "running", "message": "iniciando...", "result": None,
            "started_at": time.time(),
        }

        def _progress(msg: str):
            with _ativos_jobs_lock:
                if job_id in _ativos_jobs:
                    _ativos_jobs[job_id]["message"] = msg

        def _runner():
            try:
                result = download_full_history(asset, interval=interval, progress_cb=_progress)
                with _ativos_jobs_lock:
                    _ativos_jobs[job_id]["status"] = "done" if result.get("ok") else "error"
                    _ativos_jobs[job_id]["message"] = (
                        f"{result.get('rows', 0)} candles salvos (+{result.get('added', 0)} novos)"
                        if result.get("ok") else result.get("error", "erro")
                    )
                    _ativos_jobs[job_id]["result"] = result
            except Exception as e:
                log.warning(f"[ativos] download {asset} {interval} falhou: {e}")
                with _ativos_jobs_lock:
                    _ativos_jobs[job_id]["status"] = "error"
                    _ativos_jobs[job_id]["message"] = str(e)

        threading.Thread(target=_runner, daemon=True).start()
        return jsonify({"job_id": job_id})

    @app.route("/api/ativos/download/<job_id>")
    def api_ativos_download_status(job_id):
        with _ativos_jobs_lock:
            job = _ativos_jobs.get(job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            return jsonify(job)

    # ── Scanner API ──────────────────────────────────────────────────

    @app.route("/api/scanner/assets")
    def api_scanner_assets():
        from bot.backtest.scanner import get_available_assets
        tf = request.args.get("timeframe", "5m")
        return jsonify(get_available_assets(timeframe=tf))

    @app.route("/api/scanner/run", methods=["POST"])
    def api_scanner_run():
        from bot.backtest.scanner import start_scan_job
        data = request.get_json() or {}
        job_id = start_scan_job(
            data.get("asset", "BTC").upper(),
            int(data.get("days", 90)),
            data.get("strategies") or None,
            timeframe=data.get("timeframe", "5m"),
        )
        return jsonify({"job_id": job_id})

    @app.route("/api/scanner/status/<job_id>")
    def api_scanner_status(job_id):
        from bot.backtest.scanner import get_scan_job
        job = get_scan_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    @app.route("/api/scanner/apply", methods=["POST"])
    def api_scanner_apply():
        from bot.backtest.scanner import apply_result
        data = request.get_json() or {}
        result = apply_result(
            data.get("asset", "").upper(),
            data.get("strategy", ""),
            data.get("params", {}),
            tag=data.get("tag"),
            timeframe=data.get("timeframe", "5m"),
        )
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/api/logs")
    def api_logs():
        level = request.args.get("level")
        limit = int(request.args.get("limit", 200))
        logs = db.get_logs(limit, level)
        return jsonify(logs)

    @app.route("/api/config", methods=["GET"])
    def api_get_config():
        return jsonify(db.get_all_config())

    @app.route("/api/config", methods=["POST"])
    def api_save_config():
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data"}), 400
        db.set_configs(data)

        # Toggle debug if changed
        if "debug_logging" in data:
            set_debug(data["debug_logging"].lower() == "true")

        return jsonify({"ok": True})

    @app.route("/api/bot/start", methods=["POST"])
    def api_bot_start():
        from main import start_bot
        db.set_config("bot_status", "running")
        start_bot()
        return jsonify({"ok": True, "status": "running"})

    @app.route("/api/bot/pause", methods=["POST"])
    def api_bot_pause():
        from main import pause_bot
        pause_bot()
        return jsonify({"ok": True, "status": "paused"})

    @app.route("/api/bot/stop", methods=["POST"])
    def api_bot_stop():
        from main import stop_bot
        stop_bot()
        return jsonify({"ok": True, "status": "stopped"})

    # ── SocketIO — push updates every 5 seconds ────────────────────

    def background_pusher():
        from main import get_asset_live_status
        while True:
            try:
                cfg = db.get_all_config()
                stats = db.get_trade_stats()
                open_trades = db.get_open_trades()
                daily_pnl = db.get_daily_pnl()
                total_pnl = db.get_total_pnl()

                socketio.emit("overview_update", {
                    "bot_status": cfg.get("bot_status", "stopped"),
                    "daily_pnl": round(daily_pnl, 2),
                    "total_pnl": round(total_pnl, 2),
                    "win_rate": round(stats["win_rate"], 1),
                    "today_count": stats["today_count"],
                    "open_trades": open_trades,
                    "asset_status": get_asset_live_status(),
                    "strategy_stats": db.get_strategy_stats(),
                })
            except Exception as e:
                log.debug(f"background_pusher error: {e}")
            time.sleep(5)

    @socketio.on("connect")
    def on_connect():
        log.debug("Dashboard client connected")

    socketio.start_background_task(background_pusher)

    return app, socketio


# Allow running standalone
if __name__ == "__main__":
    db.init_db()
    app, socketio = create_app()
    log.info("Dashboard starting on http://localhost:8080")
    socketio.run(app, host="0.0.0.0", port=8080, debug=False)

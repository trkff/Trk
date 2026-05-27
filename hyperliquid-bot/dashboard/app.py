"""
Dashboard web server — Flask + SocketIO.
Serves the trading bot dashboard on localhost:8080.
Auto-refreshes data every 5 seconds via SocketIO.
"""

import json
import queue
import threading
import time

from flask import Flask, render_template, request, jsonify, redirect, url_for, g, session
from flask_socketio import SocketIO

from bot import db
from bot.logger import get_logger, set_debug

log = get_logger("dashboard")


def create_app():
    app = Flask(__name__)
    # Persist a random session secret in the DB so Flask cookies survive restarts.
    secret = db.get_config("flask.secret_key")
    if not secret:
        import secrets as _secrets
        secret = _secrets.token_hex(32)
        db.set_config("flask.secret_key", secret)
    app.config["SECRET_KEY"] = secret
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
    def _set_active_profile():
        """Resolve the active profile for the request from the Flask session.

        Falls back to the first profile in the DB when the session is missing
        or points at a deleted profile.
        """
        pid = session.get("active_profile_id")
        if pid is None or db.get_profile(pid) is None:
            profiles = db.list_profiles()
            pid = profiles[0]["id"] if profiles else 1
            session["active_profile_id"] = pid
        g.profile_id = pid

    @app.before_request
    def check_configured():
        # API calls always pass through — only redirect HTML page requests
        if request.endpoint and not request.endpoint.startswith("api_") and request.endpoint not in (
            "config_page", "static", "backtest_page", "scanner_page", "scanner_v2_page", "strategies_page", "ativos_page", "analise_page",
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

    @app.route("/scanner_v2")
    def scanner_v2_page():
        return render_template("scanner_v2.html", page="scanner_v2")

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
        stats = db.get_trade_stats(profile_id=g.profile_id)
        open_trades = db.get_open_trades(profile_id=g.profile_id)
        daily_pnl = db.get_daily_pnl(profile_id=g.profile_id)
        total_pnl = db.get_total_pnl(profile_id=g.profile_id)

        return jsonify({
            "bot_status": db.get_profile_config(g.profile_id, "bot_status") or "stopped",
            "use_testnet": cfg.get("use_testnet", "true"),
            "daily_pnl": round(daily_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(stats["win_rate"], 1),
            "today_count": stats["today_count"],
            "total_closed": stats["total_closed"],
            "open_trades": open_trades,
            "asset_status": get_asset_live_status(),
            "strategy_stats": db.get_strategy_stats(profile_id=g.profile_id),
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
        trades = db.get_trades(limit, offset, asset, side, date_from, date_to,
                               strategy=strategy, profile_id=g.profile_id)
        return jsonify(trades)

    @app.route("/api/trades/cumulative-pnl")
    def api_cumulative_pnl():
        data = db.get_cumulative_pnl(profile_id=g.profile_id)
        return jsonify(data)

    @app.route("/api/trades/pnl-distribution")
    def api_pnl_distribution():
        data = db.get_pnl_distribution(profile_id=g.profile_id)
        return jsonify(data)

    @app.route("/api/strategy-stats")
    def api_strategy_stats():
        days = request.args.get("days", type=int)
        return jsonify(db.get_strategy_stats(days=days, profile_id=g.profile_id))

    @app.route("/api/signals")
    def api_signals():
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        strategy = request.args.get("strategy")
        signals = db.get_signals(limit, offset, strategy_name=strategy,
                                 profile_id=g.profile_id)
        return jsonify(signals)

    @app.route("/api/strategies", methods=["GET"])
    def api_strategies():
        from bot.strategies.manager import get_all_strategy_metadata
        return jsonify(get_all_strategy_metadata(profile_id=g.profile_id))

    @app.route("/api/strategies/<name>", methods=["POST"])
    def api_save_strategy(name):
        from bot.strategies.manager import STRATEGY_MAP
        if name not in STRATEGY_MAP:
            return jsonify({"error": f"Unknown strategy: {name}"}), 404
        data = request.get_json() or {}
        enabled = bool(data.get("enabled", False))
        params = data.get("params", {})
        db.set_strategy_config(name, enabled, params, profile_id=g.profile_id)
        log.info(f"Strategy '{name}' updated: enabled={enabled}")
        return jsonify({"ok": True})

    @app.route("/api/strategies/applied", methods=["GET"])
    def api_strategies_applied():
        """Lista estratégias para a aba Estratégias (do perfil ativo):
        - Todas com scanner_metrics (aplicadas via Scanner)
        - PLUS todas que estão enabled=true (mesmo sem scanner_metrics — configuração legada)
        """
        import json as _json
        from bot.strategies.manager import STRATEGY_MAP
        all_cfg = db.get_all_config()

        prefix = f"profile.{g.profile_id}.strategy."

        metrics_by_name: dict[str, dict] = {}
        for key, val in all_cfg.items():
            if key.startswith(prefix) and key.endswith(".scanner_metrics"):
                inst_name = key[len(prefix):-len(".scanner_metrics")]
                try:
                    m = _json.loads(val)
                except _json.JSONDecodeError:
                    continue
                if m.get("archived"):
                    continue
                metrics_by_name[inst_name] = m

        candidates = set(metrics_by_name.keys())
        for key, val in all_cfg.items():
            if key.startswith(prefix) and key.endswith(".enabled") and val == "true":
                inst_name = key[len(prefix):-len(".enabled")]
                candidates.add(inst_name)

        result = []
        for inst_name in candidates:
            if inst_name not in STRATEGY_MAP:
                continue
            scfg = db.get_strategy_config(inst_name, profile_id=g.profile_id)
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
        Escopo: perfil ativo.
        """
        import json as _json
        from bot.strategies.manager import STRATEGY_MAP
        all_cfg = db.get_all_config()
        stats_by_name = {s["strategy"]: s for s in db.get_strategy_stats(profile_id=g.profile_id)}

        prefix = f"profile.{g.profile_id}.strategy."
        result = []
        for key, val in all_cfg.items():
            if not (key.startswith(prefix) and key.endswith(".scanner_metrics")):
                continue
            inst_name = key[len(prefix):-len(".scanner_metrics")]
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
            scfg = db.get_strategy_config(inst_name, profile_id=g.profile_id)
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
        """Soft-delete: marca scanner_metrics como archived=true e desativa (no perfil ativo).
        A aba Estratégias filtra arquivadas; a aba Análise continua incluindo (para
        preservar histórico de scanner_metrics × performance live)."""
        import json as _json
        db.set_profile_config(g.profile_id, f"strategy.{name}.enabled", "false")
        raw = db.get_profile_config(g.profile_id, f"strategy.{name}.scanner_metrics")
        if raw:
            try:
                m = _json.loads(raw)
            except _json.JSONDecodeError:
                m = {}
            m["archived"] = True
            db.set_profile_config(g.profile_id, f"strategy.{name}.scanner_metrics", _json.dumps(m))
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
            profile_id=g.profile_id,
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

    # In-memory job registry for downloads: {job_id: {asset, interval, status, message, result, started_at, key}}
    # status ∈ {"queued", "running", "done", "error"}
    _ativos_jobs: dict = {}
    _ativos_jobs_lock = threading.Lock()

    # Fila FIFO + worker único — garante 1 download por vez, evita rate-limit da Lighter
    _ativos_queue: "queue.Queue[str]" = queue.Queue()
    _ativos_worker_started = {"v": False}
    _ativos_worker_lock = threading.Lock()

    def _ativos_queue_position(job_id: str) -> int:
        """0 = rodando agora; 1 = próximo da fila; etc."""
        with _ativos_jobs_lock:
            queued_ids = [
                jid for jid, j in _ativos_jobs.items()
                if j.get("status") == "queued"
            ]
            # ordem de inserção é a ordem dos jobs queued (Python preserva ordem do dict)
            try:
                idx = queued_ids.index(job_id)
            except ValueError:
                return 0
            # +1 se já há um job running (ele ocupa a posição 0)
            has_running = any(j.get("status") == "running" for j in _ativos_jobs.values())
            return idx + (1 if has_running else 0) + (0 if has_running else 1)

    def _ativos_worker_loop():
        from bot.backtest.csv_loader import download_full_history
        while True:
            job_id = _ativos_queue.get()
            try:
                with _ativos_jobs_lock:
                    job = _ativos_jobs.get(job_id)
                    if not job:
                        continue
                    job["status"] = "running"
                    job["message"] = "iniciando..."
                    job["started_at"] = time.time()
                    asset = job["asset"]
                    interval = job["interval"]

                def _progress(msg: str):
                    with _ativos_jobs_lock:
                        if job_id in _ativos_jobs:
                            _ativos_jobs[job_id]["message"] = msg

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
                    log.warning(f"[ativos] worker {asset} {interval} falhou: {e}")
                    with _ativos_jobs_lock:
                        _ativos_jobs[job_id]["status"] = "error"
                        _ativos_jobs[job_id]["message"] = str(e)
            finally:
                _ativos_queue.task_done()

    def _ensure_ativos_worker():
        with _ativos_worker_lock:
            if not _ativos_worker_started["v"]:
                threading.Thread(target=_ativos_worker_loop, daemon=True, name="ativos-worker").start()
                _ativos_worker_started["v"] = True

    def _enqueue_download(asset: str, interval: str) -> tuple[str, bool]:
        """Enfileira (asset, interval). Se já tem job queued OU running para esse par, devolve o existente.
        Retorna (job_id, was_existing)."""
        import uuid
        job_key = f"{asset}|{interval}"
        with _ativos_jobs_lock:
            for jid, j in _ativos_jobs.items():
                if j.get("key") == job_key and j.get("status") in ("queued", "running"):
                    return jid, True
            job_id = uuid.uuid4().hex
            _ativos_jobs[job_id] = {
                "asset": asset, "interval": interval, "key": job_key,
                "status": "queued", "message": "na fila...", "result": None,
                "started_at": None, "enqueued_at": time.time(),
            }
        _ativos_queue.put(job_id)
        _ensure_ativos_worker()
        return job_id, False

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
        from bot.backtest.csv_loader import SUPPORTED_DOWNLOAD_INTERVALS
        data = request.get_json() or {}
        asset = (data.get("asset") or "").upper().strip()
        interval = (data.get("interval") or "5m").strip()
        if not asset:
            return jsonify({"error": "asset required"}), 400
        if interval not in SUPPORTED_DOWNLOAD_INTERVALS:
            return jsonify({"error": f"intervalo inválido: {interval}"}), 400

        job_id, existing = _enqueue_download(asset, interval)
        return jsonify({
            "job_id": job_id,
            "existing": existing,
            "queue_position": _ativos_queue_position(job_id),
        })

    @app.route("/api/ativos/download/<job_id>")
    def api_ativos_download_status(job_id):
        with _ativos_jobs_lock:
            job = _ativos_jobs.get(job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            # cópia rasa para anexar queue_position sem mexer no original
            resp = dict(job)
        resp["queue_position"] = _ativos_queue_position(job_id) if resp.get("status") == "queued" else 0
        return jsonify(resp)

    # Update-all: agrega N job_ids individuais (todos passam pela mesma fila)
    _ativos_updateall: dict = {"batch_id": None}
    _ativos_updateall_batches: dict = {}  # batch_id -> {job_ids: [...], started_at}

    @app.route("/api/ativos/update-all", methods=["POST"])
    def api_ativos_update_all():
        """Enfileira download_full_history para todos os CSVs em candles/.
        Cada um vira um job individual na fila — o worker processa sequencialmente.
        Devolve um batch_id que agrupa os job_ids."""
        from bot.backtest.csv_loader import SUPPORTED_DOWNLOAD_INTERVALS, _CANDLES_DIR
        import re, uuid

        # batch ativo? devolve o existente
        cur_batch = _ativos_updateall.get("batch_id")
        if cur_batch and cur_batch in _ativos_updateall_batches:
            batch = _ativos_updateall_batches[cur_batch]
            with _ativos_jobs_lock:
                any_active = any(
                    _ativos_jobs.get(jid, {}).get("status") in ("queued", "running")
                    for jid in batch["job_ids"]
                )
            if any_active:
                return jsonify({"batch_id": cur_batch, "existing": True, "total": len(batch["job_ids"])})

        # descobre CSVs presentes
        targets: list[tuple[str, str]] = []
        if _CANDLES_DIR.exists():
            pattern = re.compile(r"^([a-z0-9]+)_(\d+[mh])\.csv$", re.IGNORECASE)
            for f in _CANDLES_DIR.iterdir():
                if not f.is_file():
                    continue
                m = pattern.match(f.name)
                if not m:
                    continue
                asset = m.group(1).upper()
                interval = m.group(2)
                if interval in SUPPORTED_DOWNLOAD_INTERVALS:
                    targets.append((asset, interval))
        targets.sort()

        job_ids: list[str] = []
        for asset, interval in targets:
            jid, _ = _enqueue_download(asset, interval)
            job_ids.append(jid)

        batch_id = uuid.uuid4().hex
        _ativos_updateall_batches[batch_id] = {
            "job_ids": job_ids, "started_at": time.time(),
        }
        _ativos_updateall["batch_id"] = batch_id
        return jsonify({"batch_id": batch_id, "total": len(job_ids)})

    @app.route("/api/ativos/update-all/<batch_id>")
    def api_ativos_update_all_status(batch_id):
        batch = _ativos_updateall_batches.get(batch_id)
        if not batch:
            return jsonify({"error": "Batch not found"}), 404
        job_ids = batch["job_ids"]
        total = len(job_ids)
        with _ativos_jobs_lock:
            states = [_ativos_jobs.get(jid, {}) for jid in job_ids]
        done = sum(1 for s in states if s.get("status") == "done")
        errors = [
            f"{s.get('asset')} {s.get('interval')}: {s.get('message', 'erro')}"
            for s in states if s.get("status") == "error"
        ]
        running = next((s for s in states if s.get("status") == "running"), None)
        queued_n = sum(1 for s in states if s.get("status") == "queued")
        finished_n = done + len(errors)
        all_done = finished_n >= total
        if running:
            msg = f"{finished_n}/{total} — {running.get('asset')} {running.get('interval')}: {running.get('message', '...')}"
        elif all_done:
            msg = f"{total}/{total} concluído" + (f" ({len(errors)} erros)" if errors else "")
        else:
            msg = f"{finished_n}/{total} concluído — {queued_n} na fila"
        return jsonify({
            "kind": "update-all",
            "status": "done" if all_done else "running",
            "total": total,
            "current": finished_n,
            "queued": queued_n,
            "errors": errors,
            "message": msg,
            "started_at": batch["started_at"],
        })

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
            profile_id=g.profile_id,
        )
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)

    # ── Scanner v2 API (grid scan + walk-forward) ────────────────────

    @app.route("/api/scanner_v2/assets")
    def api_scanner_v2_assets():
        from bot.backtest.scanner_v2 import get_available_assets
        tf = request.args.get("timeframe", "5m")
        return jsonify(get_available_assets(timeframe=tf))

    @app.route("/api/scanner_v2/run", methods=["POST"])
    def api_scanner_v2_run():
        from bot.backtest.scanner_v2 import start_scan_v2_job
        data = request.get_json() or {}
        job_id = start_scan_v2_job(
            data.get("asset", "BTC").upper(),
            int(data.get("days", 90)),
            data.get("strategies") or None,
            timeframe=data.get("timeframe", "5m"),
            max_combos_per_family=int(data.get("max_combos_per_family", 5000)),
        )
        return jsonify({"job_id": job_id})

    @app.route("/api/scanner_v2/status/<job_id>")
    def api_scanner_v2_status(job_id):
        from bot.backtest.scanner_v2 import get_job
        job = get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    @app.route("/api/scanner_v2/wfo", methods=["POST"])
    def api_scanner_v2_wfo():
        from bot.backtest.scanner_v2 import start_wfo_job
        data = request.get_json() or {}
        job_id = start_wfo_job(
            data.get("asset", "BTC").upper(),
            total_days=int(data.get("total_days", 180)),
            n_windows=int(data.get("n_windows", 4)),
            train_ratio=float(data.get("train_ratio", 0.7)),
            strategies=data.get("strategies") or None,
            timeframe=data.get("timeframe", "5m"),
            top_n=int(data.get("top_n", 5)),
            max_combos_per_family=int(data.get("max_combos_per_family", 5000)),
        )
        return jsonify({"job_id": job_id})

    @app.route("/api/scanner_v2/replay", methods=["POST"])
    def api_scanner_v2_replay():
        from bot.backtest.scanner_v2 import start_replay_job
        data = request.get_json() or {}
        combos = data.get("combos") or []
        if not isinstance(combos, list) or not combos:
            return jsonify({"error": "combos vazio ou inválido"}), 400
        job_id = start_replay_job(
            data.get("asset", "BTC").upper(),
            combos,
            n_windows=int(data.get("n_windows", 6)),
            days=int(data.get("days", 180)),
            timeframe=data.get("timeframe", "5m"),
        )
        return jsonify({"job_id": job_id})

    @app.route("/api/scanner_v2/apply", methods=["POST"])
    def api_scanner_v2_apply():
        # Reusa o apply_result do scanner antigo: os 7 campos novos (adx/session/atr)
        # ficam preservados em scanner_metrics.scanner_params (não traduzidos para
        # params live ainda — a live engine não os consome). _METRIC_KEYS filtra
        # as métricas; o resto vai pra scanner_params.
        from bot.backtest.scanner import apply_result
        data = request.get_json() or {}
        result = apply_result(
            data.get("asset", "").upper(),
            data.get("strategy", ""),
            data.get("params", {}),
            tag=data.get("tag"),
            timeframe=data.get("timeframe", "5m"),
            profile_id=g.profile_id,
        )
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/api/logs")
    def api_logs():
        level = request.args.get("level")
        limit = int(request.args.get("limit", 200))
        show_all = request.args.get("all") == "1"
        profile_id = None if show_all else g.profile_id
        logs = db.get_logs(limit, level, profile_id=profile_id)
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
        db.set_profile_config(g.profile_id, "bot_status", "running")
        start_bot(profile_id=g.profile_id)
        return jsonify({"ok": True, "status": "running"})

    @app.route("/api/bot/pause", methods=["POST"])
    def api_bot_pause():
        from main import pause_bot
        pause_bot(profile_id=g.profile_id)
        return jsonify({"ok": True, "status": "paused"})

    @app.route("/api/bot/stop", methods=["POST"])
    def api_bot_stop():
        from main import stop_bot
        stop_bot(profile_id=g.profile_id)
        return jsonify({"ok": True, "status": "stopped"})

    # ── Profile CRUD ───────────────────────────────────────────────

    @app.route("/api/profiles", methods=["GET"])
    def api_list_profiles():
        out = []
        for p in db.list_profiles():
            d = dict(p)  # only public fields (list_profiles returns redacted columns)
            d["bot_status"] = db.get_profile_config(p["id"], "bot_status") or "stopped"
            d["is_active"] = (p["id"] == g.profile_id)
            out.append(d)
        return jsonify(out)

    @app.route("/api/profiles", methods=["POST"])
    def api_create_profile():
        body = request.get_json(silent=True) or {}
        try:
            pid = db.create_profile(
                name=(body.get("name") or "").strip(),
                exchange=body.get("exchange") or "lighter",
                credentials=body.get("credentials") or {},
            )
        except ValueError as e:
            msg = str(e)
            status = 409 if "already used" in msg else 400
            return jsonify({"error": msg}), status
        # Return only public fields
        prof = next((p for p in db.list_profiles() if p["id"] == pid), None)
        return jsonify(prof or {"id": pid}), 201

    @app.route("/api/profiles/<int:pid>", methods=["PATCH"])
    def api_patch_profile(pid):
        if db.get_profile(pid) is None:
            return jsonify({"error": "not found"}), 404
        body = request.get_json(silent=True) or {}
        try:
            db.update_profile(
                pid,
                name=body.get("name"),
                exchange=body.get("exchange"),
                credentials=body.get("credentials"),
            )
        except ValueError as e:
            msg = str(e)
            status = 409 if "already used" in msg else 400
            return jsonify({"error": msg}), status
        prof = next((p for p in db.list_profiles() if p["id"] == pid), None)
        return jsonify(prof or {"id": pid})

    @app.route("/api/profiles/<int:pid>", methods=["DELETE"])
    def api_delete_profile(pid):
        if db.get_profile(pid) is None:
            return jsonify({"error": "not found"}), 404
        profiles = db.list_profiles()
        if len(profiles) <= 1:
            return jsonify({"error": "cannot delete the last profile"}), 409
        open_rows = db.get_open_trades(profile_id=pid)
        if open_rows:
            return jsonify({
                "error": "close open positions before deleting this profile",
                "open_count": len(open_rows),
            }), 409

        # Stop the bot first if it's running — the reaper joins the worker,
        # disconnects its client and refreshes the candle manager (which will
        # rebuild around a different profile's client). Doing this synchronously
        # avoids the window where _on_candle_close_dispatch could process a
        # candle for a profile_id that we're about to delete.
        import main as bot_main
        with bot_main._bot_lock:
            t = bot_main._bot_threads.get(pid)
        if t is not None:
            try:
                bot_main.stop_bot(profile_id=pid)
            except Exception:
                log.exception("stop_bot failed for profile %s during delete", pid)
            # Wait for the reaper to finish removing the profile from the dicts.
            # 16s = stop_bot's join(15s) + a buffer for the candle-mgr refresh.
            deadline = time.time() + 16
            while time.time() < deadline:
                with bot_main._bot_lock:
                    still_present = pid in bot_main._bot_threads
                if not still_present:
                    break
                time.sleep(0.25)

        db.delete_profile(pid)
        # If we were sitting on this profile, fall back to the first remaining
        if session.get("active_profile_id") == pid:
            remaining = db.list_profiles()
            session["active_profile_id"] = remaining[0]["id"] if remaining else 1
        return "", 204

    @app.route("/api/profiles/<int:pid>/activate", methods=["POST"])
    def api_activate_profile(pid):
        if db.get_profile(pid) is None:
            return jsonify({"error": "not found"}), 404
        session["active_profile_id"] = pid
        return jsonify({"active_profile_id": pid})

    @app.route("/api/profiles/<int:pid>/bot/start", methods=["POST"])
    def api_profile_bot_start(pid):
        if db.get_profile(pid) is None:
            return jsonify({"error": "not found"}), 404
        from main import start_bot
        t = start_bot(profile_id=pid)
        if t is None:
            return jsonify({
                "error": "could not start bot — check exchange credentials",
                "status": db.get_profile_config(pid, "bot_status") or "error",
            }), 500
        return jsonify({"ok": True, "status": "running", "profile_id": pid})

    @app.route("/api/profiles/<int:pid>/bot/pause", methods=["POST"])
    def api_profile_bot_pause(pid):
        if db.get_profile(pid) is None:
            return jsonify({"error": "not found"}), 404
        from main import pause_bot
        pause_bot(profile_id=pid)
        return jsonify({"ok": True, "status": "paused", "profile_id": pid})

    @app.route("/api/profiles/<int:pid>/bot/stop", methods=["POST"])
    def api_profile_bot_stop(pid):
        if db.get_profile(pid) is None:
            return jsonify({"error": "not found"}), 404
        from main import stop_bot
        stop_bot(profile_id=pid)
        return jsonify({"ok": True, "status": "stopped", "profile_id": pid})

    # ── SocketIO — push updates every 5 seconds ────────────────────

    def background_pusher():
        """Push overview updates for every profile.

        Each profile gets its own event named overview_update.<id>; the client
        listens only to the event matching its active profile. Phase 5 may
        switch to SocketIO rooms for tighter scoping.
        """
        from main import get_asset_live_status
        while True:
            try:
                for prof in db.list_profiles():
                    pid = prof["id"]
                    stats = db.get_trade_stats(profile_id=pid)
                    open_trades = db.get_open_trades(profile_id=pid)
                    daily_pnl = db.get_daily_pnl(profile_id=pid)
                    total_pnl = db.get_total_pnl(profile_id=pid)
                    payload = {
                        "profile_id": pid,
                        "bot_status": db.get_profile_config(pid, "bot_status") or "stopped",
                        "daily_pnl": round(daily_pnl, 2),
                        "total_pnl": round(total_pnl, 2),
                        "win_rate": round(stats["win_rate"], 1),
                        "today_count": stats["today_count"],
                        "open_trades": open_trades,
                        "asset_status": get_asset_live_status(),
                        "strategy_stats": db.get_strategy_stats(profile_id=pid),
                    }
                    socketio.emit(f"overview_update.{pid}", payload)
                    # Backwards-compat: keep the legacy event for the active-profile flow
                    if pid == 1:
                        socketio.emit("overview_update", payload)
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

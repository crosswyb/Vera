"""Local workbench HTTP boundary; the retired replay/RTC routes are archived."""

import os
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from live.store import LiveError, LiveStore, require


def create_app(database=None, config=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=64 * 1024,
                      TRUSTED_HOSTS=os.getenv("ZSJH_LIVE_TRUSTED_HOSTS", "localhost,127.0.0.1,[::1]").split(","),
                      LIVE_DB=database or os.getenv("ZSJH_LIVE_DB", ".zsjh/workbench-pilot/workbench.sqlite3"),
                      LIVE_ENABLED=True)
    if config:
        app.config.update(config)
    store = LiveStore(Path(app.config["LIVE_DB"]))
    app.extensions["live_store"] = store

    @app.before_request
    def boundary():
        require(app.config["LIVE_ENABLED"], "实时试点已关闭", 503)
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            require(request.headers.get("Sec-Fetch-Site") != "cross-site", "拒绝跨站修改", 403)
            origin = request.headers.get("Origin")
            require(not origin or origin == request.host_url.rstrip("/"), "拒绝跨站修改", 403)
            require(request.is_json, "修改请求必须使用 JSON", 415)
        if request.path.startswith(("/live/", "/internal/live/")):
            token = request.headers.get("Authorization", "")
            g.member = store.authenticate(token[7:] if token.startswith("Bearer ") else "")
            if request.path.startswith("/internal/"):
                require(g.member["role"] == "agent", "只允许内部 worker")

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, LiveError):
            return jsonify(error=str(exc), **exc.details), exc.status
        if isinstance(exc, ValidationError):
            return jsonify(error="字段或动作不符合实时契约"), 400
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, (ValueError, TypeError)):
            return jsonify(error="请求格式无效"), 400
        app.logger.error("live request failed: %s", type(exc).__name__)
        return jsonify(error="处理失败，已提交的工作仍在；请重试"), 500

    @app.get("/")
    def page():
        return render_template("workbench.html")

    @app.get("/health")
    def health():
        runtime = getattr(app.extensions.get("workbench"), "runtime", None)
        if runtime is None:
            return jsonify(status="ok", mode="workbench", scheduler={"status": "not_started"}, schema_version=1)
        scheduler = runtime.health()
        return jsonify(status=scheduler["status"], mode="workbench", scheduler=scheduler, schema_version=1), 200 if scheduler["status"] == "ok" else 503

    from live.workbench_api import register_workbench
    register_workbench(app, store)
    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env.live", override=False)
    create_app().run(host=os.getenv("ZSJH_LIVE_HOST", "127.0.0.1"), port=int(os.getenv("ZSJH_LIVE_PORT", "8766")), threaded=True)

"""One local entry point; synthetic demos work without cloud configuration."""

import argparse
import json
import os
import signal
from pathlib import Path

from dotenv import load_dotenv
from werkzeug.serving import make_server

from live.local_files import private_write
from live.store import LiveStore


ROOT = Path(__file__).resolve().parents[1]


def check_provider(config=None):
    """Explicit read-only transport check: no completion, search or transcript."""
    import requests
    from live.provider_settings import from_environment
    config = config or from_environment().model
    endpoint, key = config.base_url, config.api_key
    if not config.configured:
        return {"state": "not_configured"}
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(endpoint + "/models", headers={"Authorization": "Bearer " + key} if key else {},
                             timeout=(5, 10), stream=True, allow_redirects=False) as response:
                return {"state": "connected" if response.status_code == 200 else "http_error", "http_status": response.status_code}
    except requests.RequestException as exc:
        import traceback
        causes, current = [], exc
        for _ in range(5):
            if current is None:
                break
            causes.append({"kind": type(current).__name__, "frames": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno} for frame in traceback.extract_tb(current.__traceback__)]})
            current = current.__cause__ or current.__context__
        # A models GET carries no private request body. Remove the credential
        # defensively and keep diagnostics local; never return response content.
        return {"state": "transport_error", "error_kind": type(exc).__name__, "detail": str(exc).replace(key, "[redacted]")[:500], "causes": causes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "serve", "doctor"])
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".zsjh/workbench-pilot")
    parser.add_argument("--output-root", type=Path, default=Path.home() / "Documents/ZSJH/Meetings")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.live")
    parser.add_argument("--port", type=int, default=8767, help="0 selects a free loopback port")
    parser.add_argument("--synthetic-network", action="store_true", help="Explicit finite synthetic model/search probe, using the persisted 6/10 request ceilings")
    parser.add_argument("--audio", action="store_true", help="Allow authenticated local AudioCore events; does not itself start capture")
    parser.add_argument("--desktop", action="store_true", help="Private parent-process handshake on stdout; never use as an ordinary log")
    parser.add_argument("--check-provider", action="store_true", help="With doctor only: read-only configured /models connectivity check; no model/search calls")
    args = parser.parse_args()
    if args.check_provider and args.command != "doctor":
        parser.error("--check-provider requires doctor")
    load_dotenv(args.env_file, override=False)
    args.state_dir = args.state_dir.expanduser().resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["ZSJH_LIVE_MODEL_USAGE_DB"] = str(args.state_dir / "model-usage.sqlite3")
    os.environ["ZSJH_WB_SEARCH_USAGE_DB"] = str(args.state_dir / "search-usage.sqlite3")
    os.environ.setdefault("ZSJH_LIVE_MODEL_REQUEST_LIMIT", "6")
    os.environ.setdefault("ZSJH_WB_SEARCH_REQUEST_LIMIT", "unlimited" if args.desktop else "10")
    if args.synthetic_network:
        os.environ["ZSJH_WB_MODEL_APPROVED"] = "true"
        os.environ["ZSJH_WB_SEARCH_APPROVED"] = "true"
    if args.audio:
        os.environ["ZSJH_WB_AUDIO_APPROVED"] = "true"
    if args.desktop:
        # Desktop daily use has no request ceiling; explicit env limits still win.
        os.environ.setdefault("ZSJH_WB_MODEL_REQUEST_LIMIT", "unlimited")
    credentials_path = args.state_dir / "credentials.json"
    database = args.state_dir / "workbench.sqlite3"
    if args.command == "init":
        if credentials_path.exists():
            raise SystemExit("已有工作区保留。使用现有凭据，或指定新的 --state-dir。")
        private_write(credentials_path, json.dumps(LiveStore(database).seed(title="ZSJH 独立合成试验", synthetic=True), ensure_ascii=False, indent=2))
        print("Created private workspace:", credentials_path)
        return
    if not credentials_path.exists():
        raise SystemExit("先运行 init 创建独立工作区。")
    from live_api import create_app
    app = create_app(database, {"WORKBENCH_ROOT": args.output_root,
                              "WORKBENCH_SETTINGS_PATH": args.state_dir / "providers.json"})
    wb = app.extensions["workbench"]
    if args.command == "doctor":
        import certifi
        import ssl
        ssl.create_default_context(cafile=certifi.where())
        with wb.store.transaction() as db:
            storage = wb.store._get(db, "wb_storage", "default")
        result = {"capabilities": wb.capabilities, **storage, "credentials_path": str(credentials_path)}
        result["tls_context"] = "ready"
        if args.check_provider:
            result["provider_connection"] = check_provider(wb.provider_settings.current.model)
        print(json.dumps(result, ensure_ascii=False))
        return
    from live.workbench_runtime import WorkbenchRuntime
    runtime = WorkbenchRuntime.configured(wb).start()
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    if args.desktop:
        credentials = json.loads(credentials_path.read_text())
        owner = next(m for m in credentials["members"] if m["role"] == "owner")
        agent = next(m for m in credentials["members"] if m["role"] == "agent")
        print(json.dumps({"type": "ready", "port": server.server_port, "owner_token": owner["token"], "agent_token": agent["token"]}), flush=True)
    else:
        print(f"Workbench: http://127.0.0.1:{server.server_port}/workbench", flush=True)
        print("成员访问码保存在私有 credentials.json；不会输出到日志。", flush=True)
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
        server.server_close()


if __name__ == "__main__":
    main()

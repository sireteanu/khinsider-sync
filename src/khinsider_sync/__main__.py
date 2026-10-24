"""Start the web interface:  python -m khinsider_sync   (or khinsider-sync after pip install)."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import webbrowser

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def restart_program(argv) -> int:
    """Start Khinsider Sync again with the same options (no new browser tab)."""
    env = dict(os.environ, KHINSIDER_NO_BROWSER="1")
    env["PYTHONPATH"] = PKG_PARENT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, "-m", "khinsider_sync", *argv]
    print("\n  Restarting Khinsider Sync...\n", flush=True)
    if os.name == "nt":                  # Windows has no real exec: run it and wait (same console)
        return subprocess.call(cmd, env=env)
    os.execve(sys.executable, cmd, env)  # Docker / Linux / macOS: replace this process (same PID)
    return 0


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def already_running(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return r.status in (200, 401)
    except Exception as e:  # 401 (password) also means it's us
        return getattr(e, "code", None) == 401


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="khinsider-sync",
                                 description="Browse every khinsider.com soundtrack and keep the albums you pick in sync.")
    ap.add_argument("--data", default=env("KHINSIDER_DATA", "downloads"),
                    help="where synced albums go (env KHINSIDER_DATA; can also be changed in the settings)")
    ap.add_argument("--config", default=env("KHINSIDER_CONFIG", "config"),
                    help="where the catalog and settings are kept (env KHINSIDER_CONFIG)")
    ap.add_argument("--host", default=env("KHINSIDER_HOST", "127.0.0.1"),
                    help="address to listen on; 0.0.0.0 to reach it from other devices (env KHINSIDER_HOST)")
    ap.add_argument("--port", type=int, default=int(env("KHINSIDER_PORT", "8485")), help="(env KHINSIDER_PORT)")
    ap.add_argument("--site", default=env("KHINSIDER_SITE", "https://downloads.khinsider.com/"), help=argparse.SUPPRESS)
    ap.add_argument("--password", default=env("KHINSIDER_PASSWORD"),
                    help="ask for this password in the browser (env KHINSIDER_PASSWORD)")
    ap.add_argument("--lock-data", action="store_true", default=env("KHINSIDER_DATA_LOCKED", "0") == "1",
                    help="don't allow changing the download folder in the interface (used in Docker)")
    ap.add_argument("--no-browser", action="store_true", default=env("KHINSIDER_NO_BROWSER", "0") == "1",
                    help="don't open the browser on start")
    args = ap.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        sys.exit('Missing dependencies. Install with:  pip install -e .   (or run start.bat on Windows)')
    from .engine import Engine
    from .web import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    config_dir = os.path.abspath(args.config)
    os.makedirs(config_dir, exist_ok=True)
    # a log file next to the catalog, so a window that closed too fast can still be explained
    log_file = os.path.join(config_dir, "khinsider-sync.log")
    try:
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        fh.setLevel(logging.INFO)
        logging.getLogger().addHandler(fh)
    except OSError:
        log_file = None
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("dircrawl").propagate = True

    data_dir = os.path.abspath(args.data)
    restart = {"wanted": False}
    server = None

    def on_shutdown():
        server.should_exit = True

    def on_restart():
        restart["wanted"] = True
        server.should_exit = True

    app = create_app(lambda: Engine(config_dir, data_dir, args.site, data_locked=args.lock_data),
                     password=args.password, on_restart=on_restart, on_shutdown=on_shutdown)
    shown = "localhost" if args.host in ("0.0.0.0", "127.0.0.1", "::") else args.host
    url = f"http://{shown}:{args.port}/"
    if not port_free(args.host, args.port):
        if already_running(args.port):
            print(f"\n  Khinsider Sync is already running at {url} - opening it.\n"
                  "  (Only one copy can run at a time. Use the Restart button in the page to restart it.)\n",
                  flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        logging.error("Port %d is already used by another program. Close that program, or start Khinsider Sync "
                      "on another port:  start.bat --port %d", args.port, args.port + 1)
        return 1
    print(f"\n  Khinsider Sync is running at {url}\n  catalog/settings: {config_dir}\n"
          f"  downloads:        {data_dir} (changeable in Settings{'' if not args.lock_data else ' - locked'})\n"
          f"  log file:         {log_file or '(none)'}\n"
          f"  Press Ctrl+C to stop.\n", flush=True)
    logging.info("Khinsider Sync %s starting (Python %s, %s)", __import__("khinsider_sync").__version__,
                 sys.version.split()[0], sys.platform)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    try:
        server.run()
    except Exception:
        logging.exception("Khinsider Sync stopped because of an error")
        return 1
    if restart["wanted"]:
        return restart_program(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

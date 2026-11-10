"""One-click launcher for the local web dashboard and the Master Duel HUD."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements-app.txt"
REQUIRED_MODULES = {
    "fastapi": "fastapi",
    "jinja2": "jinja2",
    "uvicorn": "uvicorn",
    "mss": "mss",
    "cv2": "opencv-python-headless",
    "numpy": "numpy",
    "pytesseract": "pytesseract",
    "PyQt6": "PyQt6",
    "psutil": "psutil",
}


def _uv_executable() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    return str(candidate) if candidate.is_file() else None


def ensure_dependencies() -> None:
    missing = [package for module, package in REQUIRED_MODULES.items()
               if importlib.util.find_spec(module) is None]
    if not missing:
        return
    print("Installing missing application packages: " + ", ".join(missing), flush=True)
    uv = _uv_executable()
    if uv:
        command = [uv, "pip", "install", "--python", sys.executable, "-r", str(REQUIREMENTS)]
    else:
        try:
            import ensurepip

            ensurepip.bootstrap(upgrade=True)
        except Exception:
            pass
        command = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    subprocess.run(command, cwd=ROOT, check=True)
    still_missing = [module for module in REQUIRED_MODULES if importlib.util.find_spec(module) is None]
    if still_missing:
        raise RuntimeError("Packages are still unavailable after installation: " + ", ".join(still_missing))


def _health(url: str, timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=timeout) as response:
            response.read()
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def start_web_server(host: str, port: int, url: str) -> tuple[subprocess.Popen[bytes] | None, bool]:
    if _health(url):
        print(f"Reusing the dashboard already running at {url}", flush=True)
        return None, False
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "web_app:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    server = subprocess.Popen(command, cwd=ROOT)
    for _ in range(100):
        if server.poll() is not None:
            raise RuntimeError(f"The web server exited early with code {server.returncode}.")
        if _health(url):
            print(f"Web dashboard ready at {url}", flush=True)
            return server, True
        time.sleep(0.2)
    server.terminate()
    server.wait(timeout=5)
    raise RuntimeError(f"The dashboard did not become healthy at {url}/api/health within 20 seconds.")


def _stop_server(server: subprocess.Popen[bytes] | None) -> None:
    if server is None or server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=8)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)


def check_installation() -> None:
    ensure_dependencies()
    from database_manager import DatabaseManager
    from mcts_search import load_deck, make_initial_state, run_mcts

    DatabaseManager(ROOT / "data" / "combo_cache.db").initialize()
    deck = load_deck(ROOT / "examples" / "vs_k9_deck.json", ROOT / "data" / "banlist.json")
    hand = list(deck.main_deck[:5])
    state = make_initial_state(deck, hand)
    result = run_mcts(deck, hand, iterations=12, rollout_depth=8, seed=7, initial_state=state)
    print(
        f"Install check passed: {len(deck.main_deck)} Main / {len(deck.extra_deck)} Extra, "
        f"{result.simulations} MCTS iterations, cache initialized at data/combo_cache.db.",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("YGO_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("YGO_WEB_PORT", "8000")))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-overlay", action="store_true")
    parser.add_argument("--check", action="store_true", help="install dependencies and run a small smoke check")
    args = parser.parse_args(argv)
    if args.check:
        check_installation()
        return 0

    ensure_dependencies()
    from database_manager import DatabaseManager

    DatabaseManager(ROOT / "data" / "combo_cache.db").initialize()
    url = f"http://{args.host}:{args.port}"
    server: subprocess.Popen[bytes] | None = None
    owns_server = False
    try:
        server, owns_server = start_web_server(args.host, args.port, url)
        if not args.no_browser:
            webbrowser.open(url)
        if args.no_overlay:
            print("Press Ctrl+C to stop the dashboard.", flush=True)
            while server is not None and server.poll() is None:
                time.sleep(0.5)
            return server.returncode if server is not None else 0
        from overlay_assistant import main as overlay_main

        return overlay_main(["--web-url", url, "--no-browser"])
    except KeyboardInterrupt:
        return 0
    finally:
        if owns_server:
            _stop_server(server)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Yu-Gi-Oh! AI startup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

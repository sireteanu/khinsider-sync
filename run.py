#!/usr/bin/env python3
"""Start the web interface without installing:  python run.py   (opens http://localhost:8485/)

Options: python run.py --help   (e.g. --data D:\\Music, --port 8485, --host 0.0.0.0)
On Windows, start.bat is the easiest way: it sets up everything Khinsider Sync needs first.
"""
import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(HERE, ".venv", "Scripts" if os.name == "nt" else "bin",
                       "python.exe" if os.name == "nt" else "python")


def pause():
    # double-clicked on Windows: keep the window open so the message can be read
    if os.name == "nt" and sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass


def run() -> int:
    # use the project's own Python (made by start.bat) when this one lacks the dependencies
    if os.path.exists(VENV_PY) and os.path.abspath(sys.executable).lower() != os.path.abspath(VENV_PY).lower():
        try:
            import fastapi, uvicorn  # noqa: F401,E401
        except ImportError:
            return subprocess.call([VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
    sys.path.insert(0, os.path.join(HERE, "src"))
    try:
        from khinsider_sync.__main__ import main
    except ImportError as e:
        print(f"Khinsider Sync can't start: {e}\n\nIts dependencies aren't installed for this Python ({sys.executable}).\n"
              "Run start.bat instead (it installs them), or:  python -m pip install -e .")
        return 1
    return main() or 0


if __name__ == "__main__":
    try:
        code = run()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (print(e.code) or 1)
    except KeyboardInterrupt:
        code = 0
    except Exception:
        traceback.print_exc()
        code = 1
    if code:
        pause()
    sys.exit(code)

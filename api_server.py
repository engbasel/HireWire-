"""
HireWire — Web Dashboard API Server
A Flask-based REST API that bridges the frontend UI with the bot engine.
"""

import os
import sys
import time
import signal
import json
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from collections import deque

from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS
from dotenv import load_dotenv, set_key, dotenv_values

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "logs" / "agent.log"
DB_FILE  = BASE_DIR / "mostaql_memory.db"

# ── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="dashboard", static_url_path="")
CORS(app)

# ── Bot Process State ──────────────────────────────────────────────────────
bot_process: subprocess.Popen | None = None
bot_lock = threading.Lock()
log_buffer: deque = deque(maxlen=500)     # Ring buffer of recent log lines
stats_cache: dict = {}
last_run_time: str = "Never"
total_cycles: int = 0
projects_found: int = 0

# ── Log tail thread ────────────────────────────────────────────────────────
def _tail_log():
    """Background thread: tail the log file and push lines into log_buffer."""
    LOG_FILE.parent.mkdir(exist_ok=True)
    LOG_FILE.touch(exist_ok=True)
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        # Skip to end initially
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                log_buffer.append(line.rstrip())
            else:
                time.sleep(0.5)

threading.Thread(target=_tail_log, daemon=True).start()


# ── Helpers ────────────────────────────────────────────────────────────────
def _get_db_stats() -> dict:
    """Read stats from SQLite without importing config (avoids env validation)."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_FILE))
        total = conn.execute("SELECT COUNT(*) FROM processed_projects").fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM processed_projects WHERE created_at > datetime('now', '-7 days')"
        ).fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM processed_projects WHERE created_at > datetime('now', '-1 day')"
        ).fetchone()[0]
        # Platform breakdown
        rows = conn.execute(
            "SELECT url, COUNT(*) as cnt FROM processed_projects GROUP BY "
            "CASE WHEN url LIKE '%mostaql%' THEN 'mostaql' "
            "     WHEN url LIKE '%nafezly%' THEN 'nafezly' "
            "     WHEN url LIKE '%peopleperhour%' THEN 'pph' "
            "     WHEN url LIKE '%guru%' THEN 'guru' "
            "     ELSE 'other' END"
        ).fetchall()
        conn.close()
        return {
            "total": total,
            "last_7_days": recent,
            "today": today,
        }
    except Exception as e:
        return {"total": 0, "last_7_days": 0, "today": 0, "error": str(e)}


def _read_env() -> dict:
    """Read .env file as dict (masked for sensitive keys)."""
    if not ENV_FILE.exists():
        return {}
    vals = dotenv_values(str(ENV_FILE))
    masked = {}
    for k, v in vals.items():
        if v and any(k in s for s in ["KEY", "TOKEN", "SECRET"]):
            masked[k] = v[:6] + "…" + v[-4:] if len(v) > 12 else "****"
        else:
            masked[k] = v
    return masked


def _is_bot_running() -> bool:
    global bot_process
    if bot_process is None:
        return False
    return bot_process.poll() is None


# ── Routes — Static ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("dashboard", "index.html")


# ── Routes — Bot Control ───────────────────────────────────────────────────
@app.route("/api/bot/status", methods=["GET"])
def bot_status():
    running = _is_bot_running()
    stats = _get_db_stats()
    return jsonify({
        "running": running,
        "pid": bot_process.pid if running else None,
        "last_run": last_run_time,
        "db_stats": stats,
        "uptime": _get_uptime(),
    })


@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    global bot_process, last_run_time
    with bot_lock:
        if _is_bot_running():
            return jsonify({"ok": False, "message": "Bot is already running."}), 400

        if not ENV_FILE.exists():
            return jsonify({"ok": False, "message": "Missing .env file. Please save your credentials first."}), 400

        # Check required env vars
        vals = dotenv_values(str(ENV_FILE))
        missing = [k for k in ["GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
                   if not vals.get(k) or "your_" in vals.get(k, "")]
        if missing:
            return jsonify({"ok": False, "message": f"Missing credentials: {', '.join(missing)}"}), 400

        python_exec = str(BASE_DIR / "venv" / "Scripts" / "python.exe")
        if not Path(python_exec).exists():
            python_exec = sys.executable  # fallback

        bot_process = subprocess.Popen(
            [python_exec, str(BASE_DIR / "main.py")],
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Pipe process stdout → log_buffer
        def _pipe():
            for line in bot_process.stdout:
                log_buffer.append(line.rstrip())
        threading.Thread(target=_pipe, daemon=True).start()

    return jsonify({"ok": True, "message": "Bot started.", "pid": bot_process.pid})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    global bot_process
    with bot_lock:
        if not _is_bot_running():
            return jsonify({"ok": False, "message": "Bot is not running."}), 400
        bot_process.terminate()
        try:
            bot_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bot_process.kill()
        bot_process = None
    return jsonify({"ok": True, "message": "Bot stopped."})


# ── Routes — Logs ──────────────────────────────────────────────────────────
@app.route("/api/logs", methods=["GET"])
def get_logs():
    since = request.args.get("since", 0, type=int)
    lines = list(log_buffer)
    return jsonify({"lines": lines[since:], "total": len(lines)})


@app.route("/api/logs/stream")
def stream_logs():
    """SSE endpoint to stream logs in real-time."""
    def generate():
        last_idx = len(log_buffer)
        while True:
            current = list(log_buffer)
            if len(current) > last_idx:
                for line in current[last_idx:]:
                    yield f"data: {json.dumps(line)}\n\n"
                last_idx = len(current)
            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Routes — Config ────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_config():
    """Return non-sensitive config from config.py defaults + .env."""
    defaults = {
        "AI_CRITERIA": "مشاريع برمجة وتطوير الويب أو تطبيقات الهواتف الذكية أو أتمتة الأعمال.",
        "INTERVAL_MINUTES": 5,
        "MIN_HIRING_RATE": 1,
        "NEW_CLIENT_DAYS": 5,
        "MAX_PROJECTS_PER_RUN": 30,
        "GEMINI_MODEL": "gemini-2.5-flash",
        "MOSTAQL_URL": "https://mostaql.com/projects?category=development&sort=latest",
        "NAFEZLY_URL": "https://nafezly.com/projects?specialize=development&page=1",
        "PPH_URL": "https://www.peopleperhour.com/freelance-jobs",
        "GURU_URL": "https://www.guru.com/d/jobs/c/programming-development/",
    }

    # Read actual values from config.py by importing it safely
    try:
        import importlib.util, types
        spec = importlib.util.spec_from_file_location("_cfg_tmp", str(BASE_DIR / "config.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for key in defaults:
            if hasattr(mod, key):
                defaults[key] = getattr(mod, key)
    except Exception:
        pass

    # Credentials masked status
    env_vals = dotenv_values(str(ENV_FILE)) if ENV_FILE.exists() else {}
    credentials = {
        "GEMINI_API_KEY": bool(env_vals.get("GEMINI_API_KEY") and "your_" not in env_vals.get("GEMINI_API_KEY", "")),
        "TELEGRAM_BOT_TOKEN": bool(env_vals.get("TELEGRAM_BOT_TOKEN") and "your_" not in env_vals.get("TELEGRAM_BOT_TOKEN", "")),
        "TELEGRAM_CHAT_ID": bool(env_vals.get("TELEGRAM_CHAT_ID") and "your_" not in env_vals.get("TELEGRAM_CHAT_ID", "")),
    }
    
    # Mask for display only
    display = {}
    for k in ["GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        v = env_vals.get(k, "")
        if v and "your_" not in v and len(v) > 10:
            display[k] = v[:6] + "••••••" + v[-4:]
        else:
            display[k] = ""

    return jsonify({"settings": defaults, "credentials": credentials, "display": display})


@app.route("/api/config/credentials", methods=["POST"])
def save_credentials():
    """Save API credentials to .env file."""
    data = request.json or {}
    ENV_FILE.touch(exist_ok=True)
    updated = []
    for key in ["GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        val = data.get(key, "").strip()
        if val:
            set_key(str(ENV_FILE), key, val)
            updated.append(key)
    if updated:
        return jsonify({"ok": True, "message": f"Saved: {', '.join(updated)}"})
    return jsonify({"ok": False, "message": "No valid credentials provided."}), 400


@app.route("/api/config/settings", methods=["POST"])
def save_settings():
    """Persist user-editable settings back to config.py."""
    data = request.json or {}
    config_path = BASE_DIR / "config.py"
    content = config_path.read_text(encoding="utf-8")

    mapping = {
        "AI_CRITERIA": (str, 'AI_CRITERIA: str = '),
        "INTERVAL_MINUTES": (int, 'INTERVAL_MINUTES: int = '),
        "MIN_HIRING_RATE": (int, 'MIN_HIRING_RATE: int = '),
        "NEW_CLIENT_DAYS": (int, 'NEW_CLIENT_DAYS: int = '),
        "MAX_PROJECTS_PER_RUN": (int, 'MAX_PROJECTS_PER_RUN: int = '),
        "GEMINI_MODEL": (str, 'GEMINI_MODEL: str = '),
    }

    import re
    for field, (typ, prefix) in mapping.items():
        if field not in data:
            continue
        val = data[field]
        try:
            val = typ(val)
        except Exception:
            continue
        if typ == str:
            new_line = f'{prefix}"{val}"'
            content = re.sub(rf'{re.escape(prefix)}"[^"]*"', new_line, content)
        else:
            new_line = f'{prefix}{val}'
            content = re.sub(rf'{re.escape(prefix)}\d+', new_line, content)

    config_path.write_text(content, encoding="utf-8")
    return jsonify({"ok": True, "message": "Settings saved. Restart the bot to apply."})


# ── Routes — Database ──────────────────────────────────────────────────────
@app.route("/api/db/recent", methods=["GET"])
def db_recent():
    """Return the 50 most recent processed projects."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, url, title, hiring_rate, created_at FROM processed_projects "
            "ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        return jsonify({"projects": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"projects": [], "error": str(e)})


@app.route("/api/db/clear", methods=["POST"])
def db_clear():
    """Clear all database entries."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("DELETE FROM processed_projects")
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "message": "Database cleared. Bot will re-evaluate all projects."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ── Uptime ─────────────────────────────────────────────────────────────────
_server_start = time.time()
def _get_uptime() -> str:
    secs = int(time.time() - _server_start)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── Entry ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n⚡ HireWire Dashboard starting on http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

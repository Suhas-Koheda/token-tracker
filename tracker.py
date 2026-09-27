#!/usr/bin/env python3
"""
Token Usage Tracker — V–A–C Sreda edition
White-cube exhibition diagram aesthetic. Black ink on paper-white field.
Dotted connectors link floating type. No cards, no shadows, no radius.

Data sources:
  - OpenCode  (~/.local/share/opencode/opencode.db)
  - VS Code   (~/.config/Code/User/globalStorage/emptyWindowChatSessions/*.jsonl)
  - Antigravity (~/.config/Antigravity IDE/logs/)
  - Codex     (~/.codex/sessions/)
"""

import http.server
import json
import os
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────
PORT = int(os.environ.get("TOKEN_TRACKER_PORT", "8765"))
OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
VSCODE_CHAT_DIR = os.path.expanduser("~/.config/Code/User/globalStorage/emptyWindowChatSessions")
VSCODE_STATE_DB = os.path.expanduser("~/.config/Code/User/globalStorage/state.vscdb")
ANTIGRAVITY_LOG_DIR = os.path.expanduser("~/.config/Antigravity IDE/logs")
CODEX_DIR = os.path.expanduser("~/.codex")
POLL_INTERVAL = 5
LOG_FILE = os.path.expanduser("~/.local/share/token-tracker/tracker.log")

# ── Data loading ────────────────────────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
_last_load = 0.0


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} {msg}\n")


def _connect_db(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        log(f"DB connect error ({path}): {e}")
        return None


def _load_opencode_sessions() -> list[dict]:
    conn = _connect_db(OPENCODE_DB)
    if not conn:
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, title, model, agent, directory,
                   tokens_input, tokens_output, tokens_reasoning,
                   tokens_cache_read, tokens_cache_write,
                   cost, time_created, time_updated
            FROM session_v2
            ORDER BY time_created DESC
            """
        ).fetchall()
    except Exception as e:
        log(f"session_v2 query error: {e}")
        conn.close()
        return []
    conn.close()

    sessions = []
    for r in rows:
        model_raw = r["model"] or ""
        try:
            model_obj = json.loads(model_raw)
            model_id = model_obj.get("id", model_raw) if isinstance(model_obj, dict) else model_raw
        except (json.JSONDecodeError, TypeError):
            model_id = model_raw

        sessions.append({
            "id": r["id"],
            "title": r["title"] or "(untitled)",
            "model": model_id,
            "agent": r["agent"] or "unknown",
            "directory": r["directory"] or "",
            "tokens_input": r["tokens_input"] or 0,
            "tokens_output": r["tokens_output"] or 0,
            "tokens_reasoning": r["tokens_reasoning"] or 0,
            "tokens_cache_read": r["tokens_cache_read"] or 0,
            "tokens_cache_write": r["tokens_cache_write"] or 0,
            "cost": r["cost"] or 0.0,
            "time_created": r["time_created"],
            "time_updated": r["time_updated"],
        })
    return sessions


def _load_opencode_messages() -> list[dict]:
    conn = _connect_db(OPENCODE_DB)
    if not conn:
        return []
    try:
        rows = conn.execute(
            """
            SELECT session_id, type, seq, data
            FROM session_message
            WHERE type = 'assistant'
            ORDER BY session_id, seq
            """
        ).fetchall()
    except Exception as e:
        log(f"session_message query error: {e}")
        conn.close()
        return []
    conn.close()

    messages = []
    for r in rows:
        try:
            data = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        tokens = data.get("tokens", {})
        cache = tokens.get("cache", {})
        model_raw = data.get("model", {})
        try:
            model_obj = json.loads(model_raw) if isinstance(model_raw, str) else model_raw
            model_id = model_obj.get("id", "unknown") if isinstance(model_obj, dict) else "unknown"
        except (json.JSONDecodeError, TypeError):
            model_id = "unknown"

        messages.append({
            "session_id": r["session_id"],
            "seq": r["seq"],
            "model": model_id,
            "tokens_input": tokens.get("input", 0),
            "tokens_output": tokens.get("output", 0),
            "tokens_reasoning": tokens.get("reasoning", 0),
            "tokens_cache_read": cache.get("read", 0),
            "tokens_cache_write": cache.get("write", 0),
            "cost": data.get("cost", 0.0),
            "time": data.get("time", 0),
        })
    return messages


def _load_vscode_sessions() -> list[dict]:
    """Read VS Code chat session JSONL files for token usage."""
    sessions = []
    if not os.path.isdir(VSCODE_CHAT_DIR):
        return sessions
    for fname in os.listdir(VSCODE_CHAT_DIR):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(VSCODE_CHAT_DIR, fname)
        try:
            with open(fpath, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("kind") == 1:  # request
                        v = obj.get("v", {})
                        # VS Code chat requests may have model info and usage
                        model = v.get("model", {})
                        model_id = model.get("id", "unknown") if isinstance(model, dict) else "unknown"
                        # Check for token usage in the request
                        usage = v.get("usage", v.get("tokenUsage", {}))
                        if usage or v.get("responseId"):
                            sessions.append({
                                "source": "vscode",
                                "file": fname,
                                "model": model_id,
                                "tokens_input": usage.get("input", usage.get("prompt_tokens", 0)),
                                "tokens_output": usage.get("output", usage.get("completion_tokens", 0)),
                                "tokens_reasoning": 0,
                                "tokens_cache_read": usage.get("cacheRead", usage.get("cached_input_tokens", 0)),
                                "tokens_cache_write": 0,
                                "cost": 0.0,
                                "time": v.get("timestamp", v.get("creationDate", 0)),
                            })
        except Exception:
            continue
    return sessions


def _load_antigravity_sessions() -> list[dict]:
    """Parse Antigravity IDE logs for usage data."""
    sessions = []
    if not os.path.isdir(ANTIGRAVITY_LOG_DIR):
        return sessions
    # Look for exthost logs that might contain API usage
    for root, _dirs, files in os.walk(ANTIGRAVITY_LOG_DIR):
        for fname in files:
            if not fname.endswith(".log"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        # Look for usage/token patterns in logs
                        if "usage" in line.lower() or "token" in line.lower():
                            # Try to parse as JSON first
                            try:
                                obj = json.loads(line)
                                usage = obj.get("usage", {})
                                if usage:
                                    sessions.append({
                                        "source": "antigravity",
                                        "file": fpath,
                                        "model": obj.get("model", "unknown"),
                                        "tokens_input": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                                        "tokens_output": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                                        "tokens_reasoning": usage.get("reasoning_tokens", 0),
                                        "tokens_cache_read": usage.get("cached_input_tokens", 0),
                                        "tokens_cache_write": 0,
                                        "cost": 0.0,
                                        "time": obj.get("timestamp", 0),
                                    })
                            except json.JSONDecodeError:
                                # Plain text log line with token info
                                pass
            except Exception:
                continue
    return sessions


def _load_codex_sessions() -> list[dict]:
    sessions = []
    if not os.path.isdir(CODEX_DIR):
        return sessions
    sessions_dir = os.path.join(CODEX_DIR, "sessions")
    if not os.path.isdir(sessions_dir):
        return sessions
    for root, _dirs, files in os.walk(sessions_dir):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        usage = obj.get("usage") or obj.get("token_usage") or {}
                        if usage:
                            sessions.append({
                                "source": "codex",
                                "file": fpath,
                                "tokens_input": usage.get("input_tokens", 0),
                                "tokens_output": usage.get("output_tokens", 0),
                                "tokens_reasoning": usage.get("reasoning_tokens", 0),
                                "tokens_cache_read": usage.get("cached_input_tokens", 0),
                                "tokens_cache_write": 0,
                                "cost": 0.0,
                                "time": obj.get("timestamp", 0),
                            })
            except Exception:
                continue
    return sessions


def _aggregate() -> dict:
    oc_sessions = _load_opencode_sessions()
    oc_messages = _load_opencode_messages()
    vscode_sessions = _load_vscode_sessions()
    antigravity_sessions = _load_antigravity_sessions()
    codex_sessions = _load_codex_sessions()

    # OpenCode totals
    total_in = sum(s["tokens_input"] for s in oc_sessions)
    total_out = sum(s["tokens_output"] for s in oc_sessions)
    total_reasoning = sum(s["tokens_reasoning"] for s in oc_sessions)
    total_cache_read = sum(s["tokens_cache_read"] for s in oc_sessions)
    total_cache_write = sum(s["tokens_cache_write"] for s in oc_sessions)
    total_cost = sum(s["cost"] for s in oc_sessions)

    # VS Code totals
    vscode_in = sum(s["tokens_input"] for s in vscode_sessions)
    vscode_out = sum(s["tokens_output"] for s in vscode_sessions)

    # Antigravity totals
    ag_in = sum(s["tokens_input"] for s in antigravity_sessions)
    ag_out = sum(s["tokens_output"] for s in antigravity_sessions)

    # Codex totals
    codex_in = sum(s["tokens_input"] for s in codex_sessions)
    codex_out = sum(s["tokens_output"] for s in codex_sessions)

    # Per-model breakdown
    model_breakdown: dict[str, dict] = {}
    for s in oc_sessions:
        m = s["model"]
        if m not in model_breakdown:
            model_breakdown[m] = {"input": 0, "output": 0, "reasoning": 0,
                                  "cache_read": 0, "cache_write": 0, "cost": 0.0, "sessions": 0}
        mb = model_breakdown[m]
        mb["input"] += s["tokens_input"]
        mb["output"] += s["tokens_output"]
        mb["reasoning"] += s["tokens_reasoning"]
        mb["cache_read"] += s["tokens_cache_read"]
        mb["cache_write"] += s["tokens_cache_write"]
        mb["cost"] += s["cost"]
        mb["sessions"] += 1

    # Per-agent breakdown
    agent_breakdown: dict[str, dict] = {}
    for s in oc_sessions:
        a = s["agent"]
        if a not in agent_breakdown:
            agent_breakdown[a] = {"input": 0, "output": 0, "reasoning": 0,
                                  "cache_read": 0, "cache_write": 0, "cost": 0.0, "sessions": 0}
        ab = agent_breakdown[a]
        ab["input"] += s["tokens_input"]
        ab["output"] += s["tokens_output"]
        ab["reasoning"] += s["tokens_reasoning"]
        ab["cache_read"] += s["tokens_cache_read"]
        ab["cache_write"] += s["tokens_cache_write"]
        ab["cost"] += s["cost"]
        ab["sessions"] += 1

    # Daily breakdown
    daily: dict[str, dict] = {}
    for s in oc_sessions:
        ts = s.get("time_created") or 0
        if ts:
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            day = "unknown"
        if day not in daily:
            daily[day] = {"input": 0, "output": 0, "reasoning": 0,
                          "cache_read": 0, "cache_write": 0, "cost": 0.0, "sessions": 0}
        d = daily[day]
        d["input"] += s["tokens_input"]
        d["output"] += s["tokens_output"]
        d["reasoning"] += s["tokens_reasoning"]
        d["cache_read"] += s["tokens_cache_read"]
        d["cache_write"] += s["tokens_cache_write"]
        d["cost"] += s["cost"]
        d["sessions"] += 1

    # Source summary
    sources = []
    if oc_sessions:
        sources.append({"name": "OpenCode", "sessions": len(oc_sessions),
                        "input": total_in, "output": total_out,
                        "reasoning": total_reasoning,
                        "cache_read": total_cache_read,
                        "cache_write": total_cache_write,
                        "cost": round(total_cost, 6)})
    if vscode_sessions:
        sources.append({"name": "VS Code", "sessions": len(vscode_sessions),
                        "input": vscode_in, "output": vscode_out,
                        "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0})
    if antigravity_sessions:
        sources.append({"name": "Antigravity", "sessions": len(antigravity_sessions),
                        "input": ag_in, "output": ag_out,
                        "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0})
    if codex_sessions:
        sources.append({"name": "Codex", "sessions": len(codex_sessions),
                        "input": codex_in, "output": codex_out,
                        "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0})

    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "working_directory": os.getcwd(),
        "home_directory": os.path.expanduser("~"),
        "opencode": {
            "sessions": oc_sessions,
            "message_count": len(oc_messages),
            "totals": {
                "input": total_in,
                "output": total_out,
                "reasoning": total_reasoning,
                "cache_read": total_cache_read,
                "cache_write": total_cache_write,
                "cost": round(total_cost, 6),
            },
            "by_model": model_breakdown,
            "by_agent": agent_breakdown,
            "by_day": daily,
        },
        "vscode": {"sessions": vscode_sessions, "count": len(vscode_sessions)},
        "antigravity": {"sessions": antigravity_sessions, "count": len(antigravity_sessions)},
        "codex": {"sessions": codex_sessions, "count": len(codex_sessions)},
        "sources": sources,
    }


def refresh_cache() -> None:
    global _last_load
    try:
        data = _aggregate()
        with _cache_lock:
            _cache.clear()
            _cache.update(data)
        _last_load = time.time()
    except Exception as e:
        log(f"refresh error: {e}")


def cache_refresh_loop() -> None:
    while True:
        if time.time() - _last_load >= POLL_INTERVAL:
            refresh_cache()
        time.sleep(1)


# ── V–A–C Sreda Dashboard ──────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Token Usage Tracker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400&family=Inter:wght@400&display=swap" rel="stylesheet">
<style>
  :root {
    --color-ink-black: #000000;
    --color-paper-white: #ffffff;
    --color-pencil-gray: #999999;
    --font-display: 'Space Grotesk', ui-sans-serif, system-ui, sans-serif;
    --font-text: 'Inter', ui-sans-serif, system-ui, sans-serif;
    --font-arial: 'Arial', ui-sans-serif, system-ui, sans-serif;
    --text-caption: 13px;
    --text-body-sm: 16px;
    --text-body: 20px;
    --text-subheading: 24px;
    --text-heading: 48px;
    --text-display: 80px;
    --spacing-5: 5px;
    --spacing-10: 10px;
    --spacing-12: 12px;
    --spacing-13: 13px;
    --spacing-20: 20px;
    --spacing-26: 26px;
    --spacing-48: 48px;
    --spacing-50: 50px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: var(--font-text);
    font-weight: 400;
    background: var(--color-paper-white);
    color: var(--color-ink-black);
    line-height: 1.2;
    padding: var(--spacing-50);
    min-height: 100vh;
  }

  /* ── Diagrammatic header ─────────────────────────────────────── */
  .hero {
    position: relative;
    margin-bottom: var(--spacing-50);
    padding-bottom: var(--spacing-48);
    border-bottom: 1px dotted var(--color-ink-black);
  }
  .hero-constellation {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-50);
    flex-wrap: wrap;
  }
  .hero-marker {
    font-family: var(--font-display);
    font-size: var(--text-display);
    line-height: 0.8;
    font-weight: 400;
    color: var(--color-ink-black);
    flex-shrink: 0;
  }
  .hero-meta {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-10);
    padding-top: var(--spacing-20);
  }
  .hero-caption {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
    line-height: 1.2;
  }
  .hero-title {
    font-family: var(--font-display);
    font-size: var(--text-heading);
    line-height: 0.8;
    font-weight: 400;
    color: var(--color-ink-black);
  }
  .hero-subtitle {
    font-family: var(--font-text);
    font-size: var(--text-body);
    color: var(--color-ink-black);
    line-height: 1.2;
    max-width: 400px;
  }
  .hero-updated {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
  }
  .hero-updated span { color: var(--color-ink-black); }

  /* ── Connector lines ──────────────────────────────────────────── */
  .connector {
    border: none;
    border-top: 1px dotted var(--color-ink-black);
    margin: var(--spacing-48) 0;
  }
  .connector-dashed {
    border: none;
    border-top: 1px dashed var(--color-ink-black);
    margin: var(--spacing-48) 0;
  }

  /* ── Section anchor labels ───────────────────────────────────── */
  .section-label {
    font-family: var(--font-text);
    font-size: var(--text-body-sm);
    font-weight: 400;
    color: var(--color-ink-black);
    margin-bottom: var(--spacing-20);
    display: flex;
    align-items: center;
    gap: var(--spacing-20);
  }
  .section-label::after {
    content: '';
    flex: 1;
    border-top: 1px dashed var(--color-ink-black);
  }

  /* ── Summary numbers (no cards, just type) ───────────────────── */
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--spacing-48) var(--spacing-50);
    margin-bottom: var(--spacing-50);
  }
  .summary-item {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-5);
  }
  .summary-label {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .summary-value {
    font-family: var(--font-display);
    font-size: var(--text-heading);
    line-height: 0.8;
    font-weight: 400;
    color: var(--color-ink-black);
  }
  .summary-value.small {
    font-size: var(--text-subheading);
  }

  /* ── Tables (borderless, ruled) ──────────────────────────────── */
  .data-table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: var(--spacing-50);
    font-size: var(--text-body-sm);
  }
  .data-table th {
    font-family: var(--font-text);
    font-weight: 400;
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
    text-align: left;
    padding: var(--spacing-10) var(--spacing-20);
    border-bottom: 1px solid var(--color-ink-black);
  }
  .data-table th.num { text-align: right; }
  .data-table td {
    padding: var(--spacing-12) var(--spacing-20);
    border-bottom: 1px dotted var(--color-pencil-gray);
    vertical-align: top;
  }
  .data-table td.num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .data-table tr:hover td {
    background: rgba(0,0,0,0.02);
  }

  /* ── Tags (outlined, no fill) ────────────────────────────────── */
  .tag {
    display: inline-block;
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-ink-black);
    border: 1px solid var(--color-ink-black);
    padding: 2px 8px;
    border-radius: 0;
    white-space: nowrap;
  }
  .tag-gray {
    color: var(--color-pencil-gray);
    border-color: var(--color-pencil-gray);
  }

  /* ── Navigation arrows ───────────────────────────────────────── */
  .nav-arrows {
    display: flex;
    gap: var(--spacing-50);
    margin-bottom: var(--spacing-50);
    flex-wrap: wrap;
  }
  .nav-item {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-5);
  }
  .nav-link {
    font-family: var(--font-text);
    font-size: var(--text-subheading);
    font-weight: 400;
    color: var(--color-ink-black);
    text-decoration: none;
    display: flex;
    align-items: center;
    gap: var(--spacing-10);
  }
  .nav-link:hover {
    text-decoration: underline;
  }
  .nav-caption {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
  }

  /* ── Source list ─────────────────────────────────────────────── */
  .source-list {
    list-style: none;
  }
  .source-item {
    display: flex;
    align-items: baseline;
    gap: var(--spacing-20);
    padding: var(--spacing-12) 0;
    border-bottom: 1px dotted var(--color-pencil-gray);
  }
  .source-name {
    font-family: var(--font-text);
    font-size: var(--text-body);
    font-weight: 400;
    color: var(--color-ink-black);
    min-width: 120px;
  }
  .source-detail {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
  }

  /* ── Footer ──────────────────────────────────────────────────── */
  .footer {
    margin-top: var(--spacing-50);
    padding-top: var(--spacing-20);
    border-top: 1px solid var(--color-ink-black);
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--spacing-20);
  }
  .footer-text {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
  }
  .footer-link {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-ink-black);
    text-decoration: none;
    border-bottom: 1px solid var(--color-ink-black);
    padding-bottom: 1px;
  }
  .footer-link:hover {
    color: var(--color-pencil-gray);
    border-color: var(--color-pencil-gray);
  }

  /* ── Empty state ─────────────────────────────────────────────── */
  .empty-state {
    font-family: var(--font-text);
    font-size: var(--text-body);
    color: var(--color-pencil-gray);
    padding: var(--spacing-48) 0;
    text-align: center;
  }

  /* ── Charts & Heatmaps ────────────────────────────────────────── */
  .chart-container {
    margin-bottom: var(--spacing-50);
  }
  .bar-chart {
    display: flex;
    align-items: flex-end;
    gap: var(--spacing-20);
    height: 180px;
    padding: var(--spacing-20) 0;
    border-bottom: 1px solid var(--color-ink-black);
  }
  .bar-group {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-10);
    height: 100%;
    justify-content: flex-end;
  }
  .bar {
    width: 100%;
    max-width: 48px;
    background: var(--color-ink-black);
    transition: height 0.3s ease;
  }
  .bar-label {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
    text-align: center;
    margin-top: var(--spacing-10);
  }
  .bar-value {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-ink-black);
    margin-bottom: var(--spacing-5);
  }

  /* Heatmap */
  .heatmap-container {
    overflow-x: auto;
    padding: var(--spacing-20) 0;
  }
  .heatmap-grid {
    display: grid;
    grid-template-columns: 60px repeat(24, 1fr);
    gap: 2px;
    min-width: 800px;
  }
  .heatmap-label {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding-right: var(--spacing-10);
  }
  .heatmap-cell {
    aspect-ratio: 1;
    border: 1px solid var(--color-paper-white);
  }
  .heatmap-header {
    display: contents;
  }
  .heatmap-header .heatmap-label {
    justify-content: center;
    padding-right: 0;
    padding-bottom: var(--spacing-5);
  }

  /* Horizontal bars */
  .hbar-chart {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-12);
  }
  .hbar-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-20);
  }
  .hbar-label {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-ink-black);
    min-width: 80px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hbar-track {
    flex: 1;
    height: 20px;
    background: var(--color-paper-white);
    border-bottom: 1px dotted var(--color-pencil-gray);
    position: relative;
  }
  .hbar-fill {
    height: 100%;
    background: var(--color-ink-black);
    transition: width 0.3s ease;
  }
  .hbar-value {
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-pencil-gray);
    min-width: 60px;
    text-align: right;
  }

  /* Stacked composition bar */
  .stacked-bar {
    display: flex;
    height: 32px;
    border: 1px solid var(--color-ink-black);
    margin: var(--spacing-20) 0;
  }
  .stacked-segment {
    height: 100%;
    transition: width 0.3s ease;
  }
  .stacked-legend {
    display: flex;
    gap: var(--spacing-20);
    flex-wrap: wrap;
    margin-bottom: var(--spacing-20);
  }
  .legend-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-5);
    font-family: var(--font-text);
    font-size: var(--text-caption);
    color: var(--color-ink-black);
  }
  .legend-swatch {
    width: 12px;
    height: 12px;
    border: 1px solid var(--color-ink-black);
  }

  /* ── Responsive ──────────────────────────────────────────────── */
  @media (max-width: 768px) {
    body { padding: var(--spacing-20); }
    .hero-marker { font-size: 48px; }
    .hero-title { font-size: 24px; }
    .summary-value { font-size: 24px; }
    .nav-arrows { gap: var(--spacing-20); }
    .data-table { font-size: var(--text-caption); }
    .data-table th, .data-table td { padding: var(--spacing-5) var(--spacing-10); }
    .bar-chart { height: 120px; }
    .heatmap-grid { grid-template-columns: 40px repeat(24, 1fr); min-width: 600px; }
  }
</style>
</head>
<body>

<!-- ── Hero ─────────────────────────────────────────────────────── -->
<header class="hero">
  <div class="hero-constellation">
    <div class="hero-marker">T</div>
    <div class="hero-meta">
      <div class="hero-caption">fig 1</div>
      <h1 class="hero-title">Token Usage Tracker</h1>
      <p class="hero-subtitle">OpenCode, VS Code, Antigravity &amp; Codex token consumption</p>
      <div class="hero-updated">Last updated: <span id="last-updated">loading...</span></div>
    </div>
  </div>
</header>

<!-- ── Summary ──────────────────────────────────────────────────── -->
<section id="summary-section">
  <div class="section-label">Summary</div>
  <div class="summary-grid" id="summary-grid"></div>
</section>

<hr class="connector">

<!-- ── Daily Bar Chart ──────────────────────────────────────────── -->
<section id="daily-chart-section">
  <div class="section-label">Daily Usage</div>
  <div class="chart-container">
    <div class="bar-chart" id="daily-chart"></div>
  </div>
</section>

<hr class="connector">

<!-- ── Token Composition ────────────────────────────────────────── -->
<section id="composition-section">
  <div class="section-label">Token Composition</div>
  <div class="stacked-legend" id="composition-legend"></div>
  <div class="stacked-bar" id="composition-bar"></div>
</section>

<hr class="connector">

<!-- ── Activity Heatmap ─────────────────────────────────────────── -->
<section id="heatmap-section">
  <div class="section-label">Activity Heatmap</div>
  <div class="heatmap-container">
    <div class="heatmap-grid" id="heatmap-grid"></div>
  </div>
</section>

<hr class="connector">

<!-- ── Model Comparison ─────────────────────────────────────────── -->
<section id="model-chart-section">
  <div class="section-label">Model Comparison</div>
  <div class="hbar-chart" id="model-chart"></div>
</section>

<hr class="connector">

<!-- ── Agent Comparison ─────────────────────────────────────────── -->
<section id="agent-chart-section">
  <div class="section-label">Agent Comparison</div>
  <div class="hbar-chart" id="agent-chart"></div>
</section>

<hr class="connector">

<!-- ── Sources ──────────────────────────────────────────────────── -->
<section id="sources-section">
  <div class="section-label">Sources</div>
  <ul class="source-list" id="source-list"></ul>
</section>

<hr class="connector">

<!-- ── Sessions ─────────────────────────────────────────────────── -->
<section id="sessions-section">
  <div class="section-label">Sessions</div>
  <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Session</th>
        <th>Path</th>
        <th>Model</th>
        <th>Agent</th>
        <th class="num">Input</th>
        <th class="num">Output</th>
        <th class="num">Reasoning</th>
        <th class="num">Cache Read</th>
        <th class="num">Cache Write</th>
        <th class="num">Cost</th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
</section>

<hr class="connector">

<!-- ── By Model ─────────────────────────────────────────────────── -->
<section id="models-section">
  <div class="section-label">By Model</div>
  <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Model</th>
        <th class="num">Sessions</th>
        <th class="num">Input</th>
        <th class="num">Output</th>
        <th class="num">Reasoning</th>
        <th class="num">Cache Read</th>
        <th class="num">Cache Write</th>
        <th class="num">Cost</th>
      </tr></thead>
      <tbody id="models-body"></tbody>
    </table>
  </div>
</section>

<hr class="connector">

<!-- ── By Agent ─────────────────────────────────────────────────── -->
<section id="agents-section">
  <div class="section-label">By Agent</div>
  <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Agent</th>
        <th class="num">Sessions</th>
        <th class="num">Input</th>
        <th class="num">Output</th>
        <th class="num">Reasoning</th>
        <th class="num">Cache Read</th>
        <th class="num">Cache Write</th>
        <th class="num">Cost</th>
      </tr></thead>
      <tbody id="agents-body"></tbody>
    </table>
  </div>
</section>

<hr class="connector">

<!-- ── Daily ────────────────────────────────────────────────────── -->
<section id="daily-section">
  <div class="section-label">Daily</div>
  <div style="overflow-x:auto">
    <table class="data-table">
      <thead><tr>
        <th>Date</th>
        <th class="num">Sessions</th>
        <th class="num">Input</th>
        <th class="num">Output</th>
        <th class="num">Reasoning</th>
        <th class="num">Cache Read</th>
        <th class="num">Cache Write</th>
        <th class="num">Cost</th>
      </tr></thead>
      <tbody id="daily-body"></tbody>
    </table>
  </div>
</section>

<hr class="connector">

<!-- ── Other Sources ───────────────────────────────────────────── -->
<section id="other-section">
  <div class="section-label">Other Sources</div>
  <div id="other-sources"></div>
</section>

<!-- ── Footer ───────────────────────────────────────────────────── -->
<footer class="footer">
  <span class="footer-text">Token Usage Tracker — background service on port 8765</span>
  <span class="footer-text" id="footer-wd"></span>
  <a href="/api/stats" class="footer-link">API</a>
</footer>

<script>
let DATA = null;

function fmt(n) {
  if (!n) return '0';
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return n.toString();
}

function fmtCost(c) {
  if (!c) return '$0.000';
  if (c < 0.01) return '$' + c.toFixed(5);
  return '$' + c.toFixed(3);
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function renderSummary(d) {
  const t = d.opencode.totals;
  const items = [
    { label: 'Input Tokens', value: fmt(t.input) },
    { label: 'Output Tokens', value: fmt(t.output) },
    { label: 'Reasoning', value: fmt(t.reasoning) },
    { label: 'Cache Read', value: fmt(t.cache_read) },
    { label: 'Cache Write', value: fmt(t.cache_write) },
    { label: 'Total Cost', value: fmtCost(t.cost) },
    { label: 'Sessions', value: d.opencode.sessions.length.toString() },
  ];
  document.getElementById('summary-grid').innerHTML = items.map(i =>
    '<div class="summary-item">' +
    '<span class="summary-label">' + i.label + '</span>' +
    '<span class="summary-value">' + i.value + '</span>' +
    '</div>'
  ).join('');
}

function renderSources(d) {
  if (!d.sources.length) {
    document.getElementById('source-list').innerHTML = '<li class="source-item"><span class="source-detail">No data sources found</span></li>';
    return;
  }
  document.getElementById('source-list').innerHTML = d.sources.map(s =>
    '<li class="source-item">' +
    '<span class="source-name">' + esc(s.name) + '</span>' +
    '<span class="source-detail">' + s.sessions + ' sessions — ' + fmt(s.input) + ' in / ' + fmt(s.output) + ' out</span>' +
    '</li>'
  ).join('');
}

function renderSessions(d) {
  const rows = d.opencode.sessions.map(s =>
    '<tr>' +
    '<td title="' + esc(s.title) + '">' + esc(s.title) + '</td>' +
    '<td style="font-size:var(--text-caption);color:var(--color-pencil-gray);word-break:break-all">' + esc(s.directory.replace(DATA.home_directory, '~')) + '</td>' +
    '<td><span class="tag">' + esc(s.model) + '</span></td>' +
    '<td><span class="tag tag-gray">' + esc(s.agent) + '</span></td>' +
    '<td class="num">' + fmt(s.tokens_input) + '</td>' +
    '<td class="num">' + fmt(s.tokens_output) + '</td>' +
    '<td class="num">' + fmt(s.tokens_reasoning) + '</td>' +
    '<td class="num">' + fmt(s.tokens_cache_read) + '</td>' +
    '<td class="num">' + fmt(s.tokens_cache_write) + '</td>' +
    '<td class="num">' + fmtCost(s.cost) + '</td>' +
    '</tr>'
  ).join('');
  document.getElementById('sessions-body').innerHTML = rows || '<tr><td colspan="10" style="text-align:center;color:var(--color-pencil-gray)">No sessions found</td></tr>';
}

function renderModels(d) {
  const entries = Object.entries(d.opencode.by_model).sort((a,b) => (b[1].input + b[1].output) - (a[1].input + a[1].output));
  const rows = entries.map(([m, v]) =>
    '<tr>' +
    '<td><span class="tag">' + esc(m) + '</span></td>' +
    '<td class="num">' + v.sessions + '</td>' +
    '<td class="num">' + fmt(v.input) + '</td>' +
    '<td class="num">' + fmt(v.output) + '</td>' +
    '<td class="num">' + fmt(v.reasoning) + '</td>' +
    '<td class="num">' + fmt(v.cache_read) + '</td>' +
    '<td class="num">' + fmt(v.cache_write) + '</td>' +
    '<td class="num">' + fmtCost(v.cost) + '</td>' +
    '</tr>'
  ).join('');
  document.getElementById('models-body').innerHTML = rows || '<tr><td colspan="8" style="text-align:center;color:var(--color-pencil-gray)">No data</td></tr>';
}

function renderAgents(d) {
  const entries = Object.entries(d.opencode.by_agent).sort((a,b) => (b[1].input + b[1].output) - (a[1].input + a[1].output));
  const rows = entries.map(([a, v]) =>
    '<tr>' +
    '<td><span class="tag tag-gray">' + esc(a) + '</span></td>' +
    '<td class="num">' + v.sessions + '</td>' +
    '<td class="num">' + fmt(v.input) + '</td>' +
    '<td class="num">' + fmt(v.output) + '</td>' +
    '<td class="num">' + fmt(v.reasoning) + '</td>' +
    '<td class="num">' + fmt(v.cache_read) + '</td>' +
    '<td class="num">' + fmt(v.cache_write) + '</td>' +
    '<td class="num">' + fmtCost(v.cost) + '</td>' +
    '</tr>'
  ).join('');
  document.getElementById('agents-body').innerHTML = rows || '<tr><td colspan="8" style="text-align:center;color:var(--color-pencil-gray)">No data</td></tr>';
}

function renderDaily(d) {
  const entries = Object.entries(d.opencode.by_day).sort((a,b) => b[0].localeCompare(a[0]));
  const rows = entries.map(([day, v]) =>
    '<tr>' +
    '<td>' + esc(day) + '</td>' +
    '<td class="num">' + v.sessions + '</td>' +
    '<td class="num">' + fmt(v.input) + '</td>' +
    '<td class="num">' + fmt(v.output) + '</td>' +
    '<td class="num">' + fmt(v.reasoning) + '</td>' +
    '<td class="num">' + fmt(v.cache_read) + '</td>' +
    '<td class="num">' + fmt(v.cache_write) + '</td>' +
    '<td class="num">' + fmtCost(v.cost) + '</td>' +
    '</tr>'
  ).join('');
  document.getElementById('daily-body').innerHTML = rows || '<tr><td colspan="8" style="text-align:center;color:var(--color-pencil-gray)">No data</td></tr>';
}

function renderOther(d) {
  const parts = [];
  if (d.vscode.count > 0) {
    parts.push('<div class="section-label" style="margin-top:var(--spacing-20)">VS Code</div>');
    parts.push('<div class="empty-state">' + d.vscode.count + ' sessions found</div>');
  }
  if (d.antigravity.count > 0) {
    parts.push('<div class="section-label" style="margin-top:var(--spacing-20)">Antigravity</div>');
    parts.push('<div class="empty-state">' + d.antigravity.count + ' sessions found</div>');
  }
  if (d.codex.count > 0) {
    parts.push('<div class="section-label" style="margin-top:var(--spacing-20)">Codex</div>');
    parts.push('<div class="empty-state">' + d.codex.count + ' sessions found</div>');
  }
  if (!parts.length) {
    parts.push('<div class="empty-state">No other sources detected. Data will appear here automatically when available.</div>');
  }
  document.getElementById('other-sources').innerHTML = parts.join('');
}

function renderDailyChart(d) {
  const entries = Object.entries(d.opencode.by_day).sort((a,b) => a[0].localeCompare(b[0]));
  if (!entries.length) {
    document.getElementById('daily-chart').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  const maxVal = Math.max(...entries.map(([,v]) => v.input + v.output));
  const bars = entries.map(([day, v]) => {
    const total = v.input + v.output;
    const height = maxVal > 0 ? (total / maxVal * 100) : 0;
    return '<div class="bar-group">' +
      '<span class="bar-value">' + fmt(total) + '</span>' +
      '<div class="bar" style="height:' + height + '%"></div>' +
      '<span class="bar-label">' + day.slice(5) + '</span>' +
      '</div>';
  }).join('');
  document.getElementById('daily-chart').innerHTML = bars;
}

function renderComposition(d) {
  const t = d.opencode.totals;
  const total = t.input + t.output + t.reasoning + t.cache_read + t.cache_write;
  if (!total) {
    document.getElementById('composition-bar').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  const segments = [
    { label: 'Input', value: t.input },
    { label: 'Output', value: t.output },
    { label: 'Reasoning', value: t.reasoning },
    { label: 'Cache Read', value: t.cache_read },
    { label: 'Cache Write', value: t.cache_write },
  ];
  const fills = ['#000000', '#333333', '#666666', '#999999', '#cccccc'];
  const bar = segments.map((s, i) =>
    '<div class="stacked-segment" style="width:' + (s.value / total * 100) + '%;background:' + fills[i] + '" title="' + s.label + ': ' + fmt(s.value) + '"></div>'
  ).join('');
  const legend = segments.map((s, i) =>
    '<span class="legend-item"><span class="legend-swatch" style="background:' + fills[i] + '"></span>' + s.label + ' ' + fmt(s.value) + '</span>'
  ).join('');
  document.getElementById('composition-bar').innerHTML = bar;
  document.getElementById('composition-legend').innerHTML = legend;
}

function renderHeatmap(d) {
  const sessions = d.opencode.sessions;
  if (!sessions.length) {
    document.getElementById('heatmap-grid').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  // Build day x hour matrix
  const days = [];
  const dayMap = {};
  sessions.forEach(s => {
    const ts = s.time_created;
    if (!ts) return;
    const dt = new Date(ts);
    const day = dt.toISOString().slice(0, 10);
    const hour = dt.getUTCHours();
    if (!dayMap[day]) {
      dayMap[day] = new Array(24).fill(0);
      days.push(day);
    }
    dayMap[day][hour] += s.tokens_input + s.tokens_output;
  });
  days.sort();
  if (!days.length) {
    document.getElementById('heatmap-grid').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  const maxVal = Math.max(...days.map(d => Math.max(...dayMap[d])));
  // Header row with hours
  let html = '<div class="heatmap-header"><span class="heatmap-label"></span>';
  for (let h = 0; h < 24; h++) {
    html += '<span class="heatmap-label">' + (h % 3 === 0 ? h : '') + '</span>';
  }
  html += '</div>';
  // Data rows
  days.forEach(day => {
    html += '<span class="heatmap-label">' + day.slice(5) + '</span>';
    for (let h = 0; h < 24; h++) {
      const val = dayMap[day][h];
      const intensity = maxVal > 0 ? val / maxVal : 0;
      const gray = Math.round(255 - intensity * 255);
      const bg = 'rgb(' + gray + ',' + gray + ',' + gray + ')';
      html += '<div class="heatmap-cell" style="background:' + bg + '" title="' + day + ' ' + h + ':00 — ' + fmt(val) + ' tokens"></div>';
    }
  });
  document.getElementById('heatmap-grid').innerHTML = html;
}

function renderModelChart(d) {
  const entries = Object.entries(d.opencode.by_model).sort((a,b) => (b[1].input + b[1].output) - (a[1].input + a[1].output));
  if (!entries.length) {
    document.getElementById('model-chart').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  const maxVal = Math.max(...entries.map(([,v]) => v.input + v.output));
  const rows = entries.map(([m, v]) => {
    const total = v.input + v.output;
    const pct = maxVal > 0 ? (total / maxVal * 100) : 0;
    return '<div class="hbar-row">' +
      '<span class="hbar-label" title="' + esc(m) + '">' + esc(m) + '</span>' +
      '<div class="hbar-track"><div class="hbar-fill" style="width:' + pct + '%"></div></div>' +
      '<span class="hbar-value">' + fmt(total) + '</span>' +
      '</div>';
  }).join('');
  document.getElementById('model-chart').innerHTML = rows;
}

function renderAgentChart(d) {
  const entries = Object.entries(d.opencode.by_agent).sort((a,b) => (b[1].input + b[1].output) - (a[1].input + a[1].output));
  if (!entries.length) {
    document.getElementById('agent-chart').innerHTML = '<div class="empty-state">No data</div>';
    return;
  }
  const maxVal = Math.max(...entries.map(([,v]) => v.input + v.output));
  const rows = entries.map(([a, v]) => {
    const total = v.input + v.output;
    const pct = maxVal > 0 ? (total / maxVal * 100) : 0;
    return '<div class="hbar-row">' +
      '<span class="hbar-label">' + esc(a) + '</span>' +
      '<div class="hbar-track"><div class="hbar-fill" style="width:' + pct + '%"></div></div>' +
      '<span class="hbar-value">' + fmt(total) + '</span>' +
      '</div>';
  }).join('');
  document.getElementById('agent-chart').innerHTML = rows;
}

function renderAll(d) {
  DATA = d;
  document.getElementById('last-updated').textContent = d.last_updated;
  document.getElementById('footer-wd').textContent = 'Working directory: ' + d.working_directory;
  renderSummary(d);
  renderSources(d);
  renderDailyChart(d);
  renderComposition(d);
  renderHeatmap(d);
  renderModelChart(d);
  renderAgentChart(d);
  renderSessions(d);
  renderModels(d);
  renderAgents(d);
  renderDaily(d);
  renderOther(d);
}

async function fetchData() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    renderAll(data);
  } catch (e) {
    console.error('Failed to fetch:', e);
    document.getElementById('last-updated').textContent = 'error: ' + e.message;
  }
}

fetchData();
setInterval(fetchData, 5000);
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/stats":
            self._serve_stats()
        elif path == "/api/sessions":
            self._serve_sessions()
        elif path == "/health":
            self._serve_health()
        else:
            self._serve_404()

    def _serve_html(self):
        body = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_stats(self):
        with _cache_lock:
            data = json.dumps(_cache).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_sessions(self):
        with _cache_lock:
            sessions = _cache.get("opencode", {}).get("sessions", [])
        data = json.dumps(sessions).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_health(self):
        body = json.dumps({"status": "ok", "uptime": time.time()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_404(self):
        body = b'{"error":"not found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    refresh_cache()
    t = threading.Thread(target=cache_refresh_loop, daemon=True)
    t.start()
    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"Server started on http://127.0.0.1:{PORT}")
    print(f"Token Usage Tracker running at http://127.0.0.1:{PORT}")
    print(f"Log file: {LOG_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Server stopped by user")
        server.shutdown()


if __name__ == "__main__":
    main()

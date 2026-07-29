#!/usr/bin/env python3
"""AI Conversation Browser — browse/search conversations from Claude Code, Codex,
Grok, OpenCode, Hermes, Cursor IDE and Cursor CLI in one local web page.

Usage:
    python3 app.py                # incremental index + serve http://localhost:8377
    python3 app.py --full         # force full reindex before serving
    python3 app.py --scan all     # dry-run scanners, print counts, no index writes

Stdlib only. File-based sources delete via move-to-Trash; OpenCode and
Hermes DB-only sessions delete via their own CLI (`opencode session delete`,
`hermes sessions delete`); Cursor IDE is read-only here (no safe delete path).
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

# ---------------------------------------------------------------- config ----

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_DB = os.environ.get("ACB_INDEX_DB", os.path.join(BASE_DIR, "index.db"))
PORT = int(os.environ.get("ACB_PORT", "8377"))
# Max chars of searchable body text indexed per conversation. Effectively
# "index everything" for real conversations — the bound only guards against a
# single pathological multi-hundred-MB session bloating the index.
BODY_CAP = int(os.environ.get("ACB_BODY_CAP", "2000000"))
# Background reindex interval while the app is running (minutes, 0 disables).
AUTO_REINDEX_MIN = float(os.environ.get("ACB_REINDEX_MIN", "5"))

CLAUDE_ROOTS = [
    os.path.join(HOME, ".claude", "projects"),
    os.path.join(HOME, ".claude-j", "projects"),
]
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
CODEX_TITLE_INDEX = os.path.join(HOME, ".codex", "session_index.jsonl")
GROK_SESSIONS = os.path.join(HOME, ".grok", "sessions")
OPENCODE_DB = os.path.join(HOME, ".local", "share", "opencode", "opencode.db")
HERMES_DB = os.path.join(HOME, ".hermes", "state.db")
HERMES_SESSIONS = os.path.join(HOME, ".hermes", "sessions")
CURSOR_VSCDB = os.path.join(
    HOME, "Library", "Application Support", "Cursor", "User", "globalStorage", "state.vscdb"
)
CURSOR_SEARCH_DB = os.path.join(
    HOME, "Library", "Application Support", "Cursor", "User", "globalStorage",
    "conversation-search.db",
)
CURSOR_WORKSPACE_STORAGE = os.path.join(
    HOME, "Library", "Application Support", "Cursor", "User", "workspaceStorage"
)
CURSOR_CHATS = os.path.join(HOME, ".cursor", "chats")
CURSOR_PROJECTS = os.path.join(HOME, ".cursor", "projects")

SOURCE_LABELS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "grok": "Grok",
    "opencode": "OpenCode",
    "hermes": "Hermes",
    "cursor-ide": "Cursor IDE",
    "cursor-cli": "Cursor CLI",
}

# --------------------------------------------------------------- helpers ----


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_s(sec):
    try:
        return iso(datetime.fromtimestamp(float(sec), tz=timezone.utc)) if sec else None
    except (ValueError, OSError, TypeError):
        return None


def iso_from_ms(ms):
    return iso_from_s(ms / 1000.0) if ms else None


def norm_iso(s):
    """Normalize an ISO-8601 string (Z / offset / naive-local) to UTC 'Z' form."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()  # naive → assume local time
        return iso(dt)
    except ValueError:
        return None


def one_line(s, n=90):
    s = re.sub(r"\s+", " ", s or "").strip()
    if not s:
        return None
    return s[: n - 1] + "…" if len(s) > n else s


def blocks_text(content):
    """Message content that may be a plain string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text"):
                if isinstance(b.get("text"), str):
                    out.append(b["text"])
        return "\n".join(out)
    return ""


def as_json(v):
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return json.loads(v)


def prefix_range(prefix):
    """Half-open key range covering all keys starting with prefix.
    cursorDiskKV LIKE 'p%' full-scans its index (~2.5s); this range form seeks."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def ro_query(db_path, sql, params=()):
    """Read-only query against a live (possibly WAL) sqlite db, with busy retry.
    Never uses immutable=1 — these dbs may be written by their apps concurrently."""
    last_err = None
    for _ in range(5):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(0.05)
    raise last_err


def file_watermark(path):
    st = os.stat(path)
    return f"{st.st_mtime}:{st.st_size}"


def decode_dashed_path(name):
    """Best-effort decode of '-Users-x-Code-foo' dir names (lossy encoding)."""
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name


class BodyAcc:
    """Accumulates searchable body text up to BODY_CAP."""

    def __init__(self):
        self.parts = []
        self.length = 0

    def add(self, text):
        if text and self.length < BODY_CAP:
            self.parts.append(text[: BODY_CAP - self.length])
            self.length += len(text)

    def text(self):
        return "\n".join(self.parts)


# ---------------------------------------------------------- source: claude ----


def list_claude_units():
    units = {}
    for root in CLAUDE_ROOTS:
        if not os.path.isdir(root):
            continue
        for proj in os.scandir(root):
            if not proj.is_dir():
                continue
            for f in os.scandir(proj.path):
                if f.is_file() and f.name.endswith(".jsonl"):
                    units[f.path] = (file_watermark(f.path), None)
    return units


def iter_claude_messages(path):
    """Yields (role, text, ts) for displayable user/assistant turns."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            if t not in ("user", "assistant") or obj.get("isSidechain"):
                continue
            msg = obj.get("message") or {}
            text = blocks_text(msg.get("content"))
            if text.strip():
                yield t, text, norm_iso(obj.get("timestamp"))


def parse_claude_unit(path, _args):
    sid = os.path.splitext(os.path.basename(path))[0]
    title = cwd = first_ts = last_ts = first_user = None
    count = 0
    body = BodyAcc()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            if t == "ai-title" and obj.get("aiTitle"):
                title = obj["aiTitle"]
            if cwd is None and obj.get("cwd"):
                cwd = obj["cwd"]
            ts = obj.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if t in ("user", "assistant") and not obj.get("isSidechain"):
                text = blocks_text((obj.get("message") or {}).get("content"))
                if text.strip():
                    count += 1
                    if first_user is None and t == "user" and not text.lstrip().startswith("<"):
                        first_user = text
                    body.add(text)
    return {
        "id": sid,
        "title": title or one_line(first_user) or sid,
        "cwd": cwd,
        "created_at": norm_iso(first_ts),
        "updated_at": norm_iso(last_ts),
        "msg_count": count,
        "size_bytes": os.path.getsize(path),
        "origin_path": path,
        "deletable": 1,
        "body": body.text(),
    }


# ----------------------------------------------------------- source: codex ----

_codex_titles = {}


def load_codex_titles():
    titles = {}
    if os.path.isfile(CODEX_TITLE_INDEX):
        with open(CODEX_TITLE_INDEX, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("id") and obj.get("thread_name"):
                    titles[obj["id"]] = obj["thread_name"]
    return titles


def list_codex_units():
    global _codex_titles
    _codex_titles = load_codex_titles()
    units = {}
    if os.path.isdir(CODEX_SESSIONS):
        for dirpath, _dirs, files in os.walk(CODEX_SESSIONS):
            for name in files:
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    path = os.path.join(dirpath, name)
                    units[path] = (file_watermark(path), None)
    return units


def iter_codex_messages(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            p = obj.get("payload") or {}
            if obj.get("type") == "response_item" and p.get("type") == "message":
                role = p.get("role")
                if role in ("user", "assistant"):
                    text = blocks_text(p.get("content"))
                    if text.strip():
                        yield role, text, norm_iso(obj.get("timestamp"))


def parse_codex_unit(path, _args):
    sid = cwd = created = last_ts = first_user = None
    count = 0
    body = BodyAcc()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("timestamp"):
                last_ts = obj["timestamp"]
            p = obj.get("payload") or {}
            if obj.get("type") == "session_meta":
                sid = p.get("id")
                cwd = p.get("cwd")
                created = p.get("timestamp") or obj.get("timestamp")
            elif obj.get("type") == "response_item" and p.get("type") == "message":
                role = p.get("role")
                if role in ("user", "assistant"):
                    text = blocks_text(p.get("content"))
                    if text.strip():
                        count += 1
                        if first_user is None and role == "user" and not text.lstrip().startswith("<"):
                            first_user = text
                        body.add(text)
    # id = filename stem, not session id: resumed sessions produce several
    # rollout files sharing one session id, and each file must stay indexed
    file_id = os.path.splitext(os.path.basename(path))[0]
    return {
        "id": file_id,
        "title": _codex_titles.get(sid) or one_line(first_user) or file_id,
        "cwd": cwd,
        "created_at": norm_iso(created),
        "updated_at": norm_iso(last_ts) or iso_from_s(os.path.getmtime(path)),
        "msg_count": count,
        "size_bytes": os.path.getsize(path),
        "origin_path": path,
        "deletable": 1,
        "body": body.text(),
    }


# ------------------------------------------------------------ source: grok ----


def list_grok_units():
    units = {}
    if os.path.isdir(GROK_SESSIONS):
        for enc in os.scandir(GROK_SESSIONS):
            if not enc.is_dir():
                continue
            for sess in os.scandir(enc.path):
                if not sess.is_dir():
                    continue
                summary = os.path.join(sess.path, "summary.json")
                if not os.path.isfile(summary):
                    continue
                hist = os.path.join(sess.path, "chat_history.jsonl")
                wm_file = hist if os.path.isfile(hist) else summary
                units[sess.path] = (file_watermark(wm_file), None)
    return units


def iter_grok_messages(sess_dir):
    hist = os.path.join(sess_dir, "chat_history.jsonl")
    if not os.path.isfile(hist):
        return
    with open(hist, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            role = obj.get("type") or obj.get("role")
            if role in ("user", "assistant"):
                text = blocks_text(obj.get("content"))
                if text.strip():
                    yield role, text, norm_iso(obj.get("timestamp"))


def parse_grok_unit(sess_dir, _args):
    with open(os.path.join(sess_dir, "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    info = summary.get("info") or {}
    sid = info.get("id") or os.path.basename(sess_dir)
    body = BodyAcc()
    first_user = None
    for role, text, _ts in iter_grok_messages(sess_dir):
        if first_user is None and role == "user" and not text.lstrip().startswith("<"):
            first_user = text
        body.add(text)
    size = sum(
        f.stat().st_size for f in os.scandir(sess_dir) if f.is_file()
    )
    return {
        "id": sid,
        "title": summary.get("generated_title")
        or one_line(summary.get("session_summary"))
        or one_line(first_user)
        or sid,
        "cwd": info.get("cwd"),
        "created_at": norm_iso(summary.get("created_at")),
        "updated_at": norm_iso(summary.get("updated_at") or summary.get("last_active_at")),
        "msg_count": summary.get("num_chat_messages"),
        "size_bytes": size,
        "origin_path": sess_dir,
        "deletable": 1,
        "body": body.text(),
    }


# -------------------------------------------------------- source: opencode ----


def list_opencode_units():
    if not os.path.isfile(OPENCODE_DB):
        return {}
    rows = ro_query(
        OPENCODE_DB,
        "SELECT id, directory, title, time_created, time_updated FROM session",
    )
    return {r[0]: (f"{r[4]}", r) for r in rows}


def parse_opencode_unit(sid, row):
    _id, directory, title, t_created, t_updated = row
    msgs = ro_query(
        OPENCODE_DB,
        "SELECT data FROM message WHERE session_id=? ORDER BY time_created",
        (sid,),
    )
    count = 0
    for (data,) in msgs:
        try:
            if as_json(data).get("role") in ("user", "assistant"):
                count += 1
        except (ValueError, AttributeError):
            continue
    body = BodyAcc()
    first_text = None
    parts = ro_query(
        OPENCODE_DB, "SELECT data FROM part WHERE session_id=? ORDER BY rowid", (sid,)
    )
    for (data,) in parts:
        try:
            d = as_json(data)
        except ValueError:
            continue
        if d.get("type") == "text" and isinstance(d.get("text"), str):
            first_text = first_text or d["text"]
            body.add(d["text"])
            if body.length >= BODY_CAP:
                break
    return {
        "id": sid,
        "title": title or one_line(first_text) or sid,
        "cwd": directory,
        "created_at": iso_from_ms(t_created),
        "updated_at": iso_from_ms(t_updated),
        "msg_count": count,
        "size_bytes": None,
        "origin_path": f"sqlite:{OPENCODE_DB}#{sid}",
        "deletable": 1,
        "body": body.text(),
    }


def load_opencode_transcript(sid):
    msgs = ro_query(
        OPENCODE_DB,
        "SELECT id, data FROM message WHERE session_id=? ORDER BY time_created",
        (sid,),
    )
    parts = ro_query(
        OPENCODE_DB,
        "SELECT message_id, data FROM part WHERE session_id=? ORDER BY rowid",
        (sid,),
    )
    parts_by_msg = {}
    for mid, data in parts:
        try:
            d = as_json(data)
        except ValueError:
            continue
        if d.get("type") == "text" and isinstance(d.get("text"), str):
            parts_by_msg.setdefault(mid, []).append(d["text"])
    out = []
    for mid, data in msgs:
        try:
            d = as_json(data)
        except ValueError:
            continue
        role = d.get("role")
        if role not in ("user", "assistant"):
            continue
        text = "\n".join(parts_by_msg.get(mid, []))
        if text.strip():
            ts = ((d.get("time") or {}).get("created"))
            out.append({"role": role, "text": text, "ts": iso_from_ms(ts)})
    return out


# ---------------------------------------------------------- source: hermes ----


def hermes_file_id(name):
    return name[len("session_"):-len(".json")]


def list_hermes_units():
    units = {}
    file_ids = set()
    if os.path.isdir(HERMES_SESSIONS):
        for f in os.scandir(HERMES_SESSIONS):
            if f.is_file() and f.name.startswith("session_") and f.name.endswith(".json"):
                units["file:" + f.path] = (file_watermark(f.path), None)
                file_ids.add(hermes_file_id(f.name))
    if os.path.isfile(HERMES_DB):
        rows = ro_query(
            HERMES_DB,
            "SELECT id, title, cwd, started_at, ended_at, message_count FROM sessions",
        )
        for r in rows:
            # dedup: prefer the trashable flat-file copy when both exist
            if str(r[0]) in file_ids:
                continue
            units["db:" + str(r[0])] = (f"{r[3]}:{r[4]}", r)
    return units


def parse_hermes_unit(unit_key, args):
    if unit_key.startswith("file:"):
        path = unit_key[5:]
        with open(path, encoding="utf-8", errors="replace") as fh:
            obj = json.load(fh)
        # filename-derived id, not the internal session_id: multiple dump files
        # can share one internal id and each file must stay indexed
        sid = hermes_file_id(os.path.basename(path))
        body = BodyAcc()
        first_user = None
        count = 0
        for m in obj.get("messages") or []:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            text = blocks_text(m.get("content"))
            if text.strip():
                count += 1
                if first_user is None and role == "user" and not text.lstrip().startswith("<"):
                    first_user = text
                body.add(text)
        return {
            "id": str(sid),
            "title": one_line(first_user) or f"Hermes {obj.get('platform') or ''} session".strip(),
            "cwd": obj.get("cwd"),
            "created_at": norm_iso(obj.get("session_start")),
            "updated_at": norm_iso(obj.get("last_updated")),
            "msg_count": count,
            "size_bytes": os.path.getsize(path),
            "origin_path": path,
            "deletable": 1,
            "body": body.text(),
        }
    sid, title, cwd, started, ended, msg_count = args
    body = BodyAcc()
    first_user = None
    rows = ro_query(
        HERMES_DB,
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY timestamp",
        (sid,),
    )
    for role, content in rows:
        if role in ("user", "assistant"):
            text = blocks_text(content) if not isinstance(content, str) else content
            if text and text.strip():
                if first_user is None and role == "user":
                    first_user = text
                body.add(text)
    return {
        "id": str(sid),
        "title": title or one_line(first_user) or str(sid),
        "cwd": cwd,
        "created_at": iso_from_s(started),
        "updated_at": iso_from_s(ended or started),
        "msg_count": msg_count,
        "size_bytes": None,
        "origin_path": f"sqlite:{HERMES_DB}#{sid}",
        "deletable": 1,
        "body": body.text(),
    }


def load_hermes_transcript(origin_path):
    if origin_path.startswith("sqlite:"):
        sid = origin_path.split("#", 1)[1]
        rows = ro_query(
            HERMES_DB,
            "SELECT role, content, timestamp FROM messages WHERE session_id=? ORDER BY timestamp",
            (sid,),
        )
        return [
            {"role": r, "text": c, "ts": iso_from_s(ts)}
            for r, c, ts in rows
            if r in ("user", "assistant") and isinstance(c, str) and c.strip()
        ]
    with open(origin_path, encoding="utf-8", errors="replace") as fh:
        obj = json.load(fh)
    out = []
    for m in obj.get("messages") or []:
        role = m.get("role")
        text = blocks_text(m.get("content"))
        if role in ("user", "assistant") and text.strip():
            out.append({"role": role, "text": text, "ts": None})
    return out


# ------------------------------------------------------ source: cursor-ide ----

_cursor_search = {}  # composerId -> (title, body) from Cursor's own search index


def load_cursor_search_index():
    """Cursor ships its own FTS index (conversation-search.db) — reuse it as the
    body-text source so we never bulk-read bubbles from the 2.1GB state.vscdb."""
    out = {}
    if not os.path.isfile(CURSOR_SEARCH_DB):
        return out
    try:
        rows = ro_query(
            CURSOR_SEARCH_DB,
            "SELECT c.id, f.title, f.body FROM conversations c "
            "JOIN conversation_fts f ON f.rowid = c.rowid",
        )
        for cid, title, fbody in rows:
            out[cid] = (title, (fbody or "")[:BODY_CAP])
    except sqlite3.Error as e:
        print(f"[cursor-ide] conversation-search.db unavailable ({e}); "
              "content search limited to titles", file=sys.stderr)
    return out


def cursor_workspace_cwd(workspace_id):
    if not workspace_id:
        return None
    ws_json = os.path.join(CURSOR_WORKSPACE_STORAGE, workspace_id, "workspace.json")
    try:
        with open(ws_json, encoding="utf-8") as fh:
            folder = json.load(fh).get("folder") or ""
        if folder.startswith("file://"):
            return unquote(urlparse(folder).path)
    except (OSError, ValueError):
        pass
    return None


def list_cursor_ide_units():
    global _cursor_search
    if not os.path.isfile(CURSOR_VSCDB):
        return {}
    _cursor_search = load_cursor_search_index()
    headers = {}
    for cid, ws, created, updated, value in ro_query(
        CURSOR_VSCDB,
        "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, value FROM composerHeaders",
    ):
        headers[cid] = (ws, created, updated, value)
    lo, hi = prefix_range("composerData:")
    data_rows = ro_query(
        CURSOR_VSCDB,
        "SELECT key, length(value) FROM cursorDiskKV WHERE key>=? AND key<?",
        (lo, hi),
    )
    units = {}
    for key, vlen in data_rows:
        cid = key.split(":", 1)[1]
        h = headers.get(cid)
        wm = f"{h[2] or h[1]}" if h else f"len:{vlen}"
        units[cid] = (wm, h)
    for cid, h in headers.items():  # headers without composerData rows
        if cid not in units:
            units[cid] = (f"{h[2] or h[1]}", h)
    return units


def parse_cursor_ide_unit(cid, header):
    title = cwd = created_ms = updated_ms = None
    if header:
        ws, created_ms, updated_ms, value = header
        try:
            hv = as_json(value)
        except (ValueError, TypeError):
            hv = {}
        title = hv.get("name")
        fs_path = ((hv.get("workspaceIdentifier") or {}).get("configPath") or {}).get("fsPath")
        if fs_path:
            cwd = os.path.dirname(fs_path) if fs_path.endswith(".code-workspace") else fs_path
        if not cwd:
            cwd = cursor_workspace_cwd(ws)
    if not title or not created_ms:
        rows = ro_query(
            CURSOR_VSCDB,
            "SELECT value FROM cursorDiskKV WHERE key=?",
            ("composerData:" + cid,),
        )
        if rows:
            try:
                dv = as_json(rows[0][0])
                title = title or dv.get("name")
                created_ms = created_ms or dv.get("createdAt")
                updated_ms = updated_ms or dv.get("lastUpdatedAt")
            except (ValueError, TypeError):
                pass
    search_title, search_body = _cursor_search.get(cid, (None, ""))
    lo, hi = prefix_range(f"bubbleId:{cid}:")
    (bubble_count,) = ro_query(
        CURSOR_VSCDB,
        "SELECT count(*) FROM cursorDiskKV WHERE key>=? AND key<?",
        (lo, hi),
    )[0]
    return {
        "id": cid,
        "title": title or search_title or "(untitled)",
        "cwd": cwd,
        "created_at": iso_from_ms(created_ms),
        "updated_at": iso_from_ms(updated_ms) or iso_from_ms(created_ms),
        "msg_count": bubble_count,
        "size_bytes": None,
        "origin_path": f"sqlite:{CURSOR_VSCDB}#{cid}",
        "deletable": 0,
        "body": search_body,
    }


def load_cursor_ide_transcript(cid):
    lo, hi = prefix_range(f"bubbleId:{cid}:")
    rows = ro_query(
        CURSOR_VSCDB,
        "SELECT key, value FROM cursorDiskKV WHERE key>=? AND key<?",
        (lo, hi),
    )
    bubbles = []
    for _key, value in rows:
        try:
            b = as_json(value)
        except (ValueError, TypeError):
            continue
        text = b.get("text") or ""
        if not text.strip():
            continue  # tool-call / context bubbles
        role = "user" if b.get("type") == 1 else "assistant"
        bubbles.append((b.get("createdAt") or "", role, text))
    bubbles.sort(key=lambda x: x[0])
    return [
        {"role": role, "text": text, "ts": norm_iso(ts) if isinstance(ts, str) else iso_from_ms(ts)}
        for ts, role, text in bubbles
    ]


# ------------------------------------------------------ source: cursor-cli ----


def list_cursor_cli_units():
    units = {}
    if os.path.isdir(CURSOR_CHATS):
        for ws in os.scandir(CURSOR_CHATS):
            if not ws.is_dir():
                continue
            for chat in os.scandir(ws.path):
                if not chat.is_dir():
                    continue
                db = os.path.join(chat.path, "store.db")
                if os.path.isfile(db):
                    units[chat.path] = (file_watermark(db), None)
    return units


def cursor_cli_meta(db):
    for key, value in ro_query(db, "SELECT key, value FROM meta"):
        try:
            raw = bytes.fromhex(value) if isinstance(value, str) else value
            m = as_json(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(m, dict) and ("agentId" in m or "name" in m):
            return m
    return {}


def iter_cursor_cli_blobs(db):
    """Yields decoded JSON message dicts (with a 'role' key), in rowid order.
    Binary blobs (protobuf manifest chain) are skipped."""
    for _rowid, data in ro_query(db, "SELECT rowid, data FROM blobs ORDER BY rowid"):
        try:
            obj = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and "role" in obj:
            yield obj


def cursor_cli_project_cwd(chat_uuid):
    if not os.path.isdir(CURSOR_PROJECTS):
        return None
    for proj in os.scandir(CURSOR_PROJECTS):
        if proj.is_dir() and os.path.isdir(
            os.path.join(proj.path, "agent-transcripts", chat_uuid)
        ):
            return decode_dashed_path(proj.name)
    return None


def parse_cursor_cli_unit(chat_dir, _args):
    db = os.path.join(chat_dir, "store.db")
    chat_uuid = os.path.basename(chat_dir)
    meta = cursor_cli_meta(db)
    body = BodyAcc()
    first_user = cwd = None
    count = 0
    for i, obj in enumerate(iter_cursor_cli_blobs(db)):
        role = obj.get("role")
        text = blocks_text(obj.get("content"))
        if cwd is None and i < 20 and text:
            m = re.search(r"Workspace Path:\s*(\S+)", text)
            if m:
                cwd = m.group(1)
        if role in ("user", "assistant") and text.strip():
            count += 1
            if first_user is None and role == "user" and not text.lstrip().startswith("<"):
                first_user = text
            body.add(text)
    if cwd is None:
        cwd = cursor_cli_project_cwd(chat_uuid)
    created = meta.get("createdAt")
    return {
        "id": chat_uuid,
        "title": meta.get("name") or one_line(first_user) or chat_uuid,
        "cwd": cwd,
        "created_at": iso_from_ms(created) if isinstance(created, (int, float)) else norm_iso(created),
        "updated_at": iso_from_s(os.path.getmtime(db)),
        "msg_count": count,
        "size_bytes": os.path.getsize(db),
        "origin_path": chat_dir,
        "deletable": 1,
        "body": body.text(),
    }


def load_cursor_cli_transcript(chat_dir):
    db = os.path.join(chat_dir, "store.db")
    out = []
    for obj in iter_cursor_cli_blobs(db):
        role = obj.get("role")
        text = blocks_text(obj.get("content"))
        if role in ("user", "assistant") and text.strip():
            out.append({"role": role, "text": text, "ts": None})
    return out


# ---------------------------------------------------------------- indexer ----

SOURCES = {
    "claude-code": (list_claude_units, parse_claude_unit),
    "codex": (list_codex_units, parse_codex_unit),
    "grok": (list_grok_units, parse_grok_unit),
    "opencode": (list_opencode_units, parse_opencode_unit),
    "hermes": (list_hermes_units, parse_hermes_unit),
    "cursor-ide": (list_cursor_ide_units, parse_cursor_ide_unit),
    "cursor-cli": (list_cursor_cli_units, parse_cursor_cli_unit),
}

HAS_FTS = True
_reindex_lock = threading.Lock()


def index_con():
    con = sqlite3.connect(INDEX_DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    global HAS_FTS
    con = index_con()
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
            source TEXT NOT NULL, id TEXT NOT NULL, unit_key TEXT NOT NULL,
            title TEXT, cwd TEXT, created_at TEXT, updated_at TEXT,
            msg_count INTEGER, size_bytes INTEGER, origin_path TEXT NOT NULL,
            deletable INTEGER NOT NULL, body TEXT,
            PRIMARY KEY (source, id))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS indexed_units (
            source TEXT NOT NULL, unit_key TEXT NOT NULL, watermark TEXT NOT NULL,
            PRIMARY KEY (source, unit_key))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS hidden (
            source TEXT NOT NULL, id TEXT NOT NULL,
            PRIMARY KEY (source, id))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS starred (
            source TEXT NOT NULL, id TEXT NOT NULL,
            PRIMARY KEY (source, id))"""
    )
    try:
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5("
            "source, id UNINDEXED, title, cwd, body)"
        )
    except sqlite3.OperationalError:
        HAS_FTS = False
        print("FTS5 unavailable — falling back to LIKE search", file=sys.stderr)
    con.commit()
    con.close()


def upsert(con, source, unit_key, watermark, rec):
    con.execute(
        "DELETE FROM conversations WHERE source=? AND id=?", (source, rec["id"])
    )
    if HAS_FTS:
        con.execute(
            "DELETE FROM conversations_fts WHERE source=? AND id=?", (source, rec["id"])
        )
    con.execute(
        "INSERT INTO conversations (source,id,unit_key,title,cwd,created_at,updated_at,"
        "msg_count,size_bytes,origin_path,deletable,body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source, rec["id"], unit_key, rec["title"], rec["cwd"], rec["created_at"],
            rec["updated_at"], rec["msg_count"], rec["size_bytes"], rec["origin_path"],
            rec["deletable"], rec["body"],
        ),
    )
    if HAS_FTS:
        con.execute(
            "INSERT INTO conversations_fts (source,id,title,cwd,body) VALUES (?,?,?,?,?)",
            (source, rec["id"], rec["title"] or "", rec["cwd"] or "", rec["body"] or ""),
        )
    con.execute(
        "INSERT OR REPLACE INTO indexed_units (source,unit_key,watermark) VALUES (?,?,?)",
        (source, unit_key, watermark),
    )


def remove_unit(con, source, unit_key):
    rows = con.execute(
        "SELECT id FROM conversations WHERE source=? AND unit_key=?", (source, unit_key)
    ).fetchall()
    for (cid,) in [tuple(r) for r in rows]:
        con.execute("DELETE FROM conversations WHERE source=? AND id=?", (source, cid))
        if HAS_FTS:
            con.execute(
                "DELETE FROM conversations_fts WHERE source=? AND id=?", (source, cid)
            )
    con.execute(
        "DELETE FROM indexed_units WHERE source=? AND unit_key=?", (source, unit_key)
    )


def reindex(full=False, only=None):
    with _reindex_lock:
        con = index_con()
        stats = {}
        for source, (list_units, parse_unit) in SOURCES.items():
            if only and source != only:
                continue
            t0 = time.time()
            try:
                units = list_units()
            except Exception as e:
                print(f"[{source}] enumeration failed: {e}", file=sys.stderr)
                stats[source] = {"error": str(e)}
                continue
            old = dict(
                con.execute(
                    "SELECT unit_key, watermark FROM indexed_units WHERE source=?",
                    (source,),
                ).fetchall()
            )
            updated = errors = 0
            for key, (wm, args) in units.items():
                if not full and old.get(key) == wm:
                    continue
                try:
                    rec = parse_unit(key, args)
                except Exception as e:
                    errors += 1
                    print(f"[{source}] parse failed for {key}: {e}", file=sys.stderr)
                    continue
                if rec:
                    upsert(con, source, key, wm, rec)
                    updated += 1
            removed = 0
            for key in set(old) - set(units):
                remove_unit(con, source, key)
                removed += 1
            con.commit()
            stats[source] = {
                "total": len(units),
                "updated": updated,
                "removed": removed,
                "errors": errors,
                "seconds": round(time.time() - t0, 2),
            }
            print(f"[{source}] {stats[source]}")
        con.close()
        return stats


# ------------------------------------------------------ transcript loading ----


def load_transcript(source, origin_path, conv_id):
    if source == "claude-code":
        return [{"role": r, "text": t, "ts": ts} for r, t, ts in iter_claude_messages(origin_path)]
    if source == "codex":
        return [{"role": r, "text": t, "ts": ts} for r, t, ts in iter_codex_messages(origin_path)]
    if source == "grok":
        return [{"role": r, "text": t, "ts": ts} for r, t, ts in iter_grok_messages(origin_path)]
    if source == "opencode":
        return load_opencode_transcript(conv_id)
    if source == "hermes":
        return load_hermes_transcript(origin_path)
    if source == "cursor-ide":
        return load_cursor_ide_transcript(conv_id)
    if source == "cursor-cli":
        return load_cursor_cli_transcript(origin_path)
    raise ValueError(f"unknown source {source}")


# ------------------------------------------------------------------ delete ----


def _uniquify(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, 1000):
        cand = f"{base} {i}{ext}"
        if not os.path.exists(cand):
            return cand
    raise RuntimeError("could not find unique trash name")


def trash(path):
    """Move a file or directory to macOS Trash. Finder first (gives 'Put Back'),
    manual ~/.Trash move as fallback."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "Finder" to delete POSIX file "{path}"'],
            check=True, capture_output=True, timeout=15,
        )
        return "finder"
    except Exception:
        dest = _uniquify(os.path.join(HOME, ".Trash", os.path.basename(path)))
        shutil.move(path, dest)
        return "fallback"


CLI_DELETE = {
    "opencode": {
        "cmd": lambda conv_id: ["opencode", "session", "delete", conv_id],
        "gone": lambda conv_id: not ro_query(
            OPENCODE_DB, "SELECT 1 FROM session WHERE id=?", (conv_id,)
        ),
    },
    "hermes": {
        "cmd": lambda conv_id: ["hermes", "sessions", "delete", conv_id, "--yes"],
        "gone": lambda conv_id: not ro_query(
            HERMES_DB, "SELECT 1 FROM sessions WHERE id=?", (conv_id,)
        ),
    },
}


def cli_delete_session(source, conv_id):
    """Delete a session via its owning app's own CLI. Some of these CLIs
    (hermes) exit 0 even when the id doesn't exist, so exit code alone isn't
    trustworthy — always verify against the source's own DB afterwards.
    Raises RuntimeError on failure."""
    spec = CLI_DELETE[source]
    cmd = spec["cmd"](conv_id)
    exe = shutil.which(cmd[0])
    if not exe:
        raise RuntimeError(f"'{cmd[0]}' not found on PATH")
    r = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "unknown error").strip())
    if not spec["gone"](conv_id):
        raise RuntimeError(
            (r.stdout or r.stderr or "delete reported success but the session still exists").strip()
        )


def delete_conversation(source, conv_id):
    con = index_con()
    row = con.execute(
        "SELECT * FROM conversations WHERE source=? AND id=?", (source, conv_id)
    ).fetchone()
    if row is None:
        con.close()
        return 404, {"error": "not found"}
    if not row["deletable"]:
        con.close()
        return 400, {"error": f"{SOURCE_LABELS.get(source, source)} conversations are "
                              "read-only here — delete inside the app itself"}
    origin = row["origin_path"]
    if origin.startswith("sqlite:"):
        if source not in CLI_DELETE:
            con.close()
            return 400, {"error": "refusing to delete a database-backed conversation"}
        try:
            cli_delete_session(source, conv_id)
            method = "cli"
        except RuntimeError as e:
            con.close()
            return 400, {"error": str(e)}
    else:
        try:
            method = trash(origin)
        except FileNotFoundError:
            method = "already-gone"
    con.execute("DELETE FROM conversations WHERE source=? AND id=?", (source, conv_id))
    if HAS_FTS:
        con.execute(
            "DELETE FROM conversations_fts WHERE source=? AND id=?", (source, conv_id)
        )
    con.execute(
        "DELETE FROM indexed_units WHERE source=? AND unit_key=?",
        (source, row["unit_key"]),
    )
    con.commit()
    con.close()
    return 200, {"ok": True, "method": method}


# ------------------------------------------------------------------ server ----

LIST_COLS = ("source,id,title,cwd,created_at,updated_at,msg_count,size_bytes,"
             "origin_path,deletable")


def fts_match_exprs(q):
    """Candidate FTS expressions, tried in order until one yields results.
    Quoted -> exact phrase. Unquoted multi-word -> words near each other first,
    then the loose all-words-anywhere match as fallback."""
    q = q.strip()
    tokens = re.findall(r"[\w']+", q)[:8]
    if not tokens:
        return []
    if len(q) >= 2 and q.startswith('"') and q.endswith('"'):
        return ['"' + " ".join(tokens) + '"']
    exprs = []
    if len(tokens) >= 2:
        exprs.append("NEAR(" + " ".join(f'"{t}"' for t in tokens) + ", 15)")
    exprs.append(" ".join(f'"{t}"*' for t in tokens))
    return exprs


def search_index(q):
    con = index_con()
    out = []
    if HAS_FTS:
        for expr in fts_match_exprs(q):
            try:
                rows = con.execute(
                    "SELECT source, id, snippet(conversations_fts, 4, '‹', '›', ' … ', 14) "
                    "FROM conversations_fts WHERE conversations_fts MATCH ? "
                    "ORDER BY rank LIMIT 800",
                    (expr,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            if rows:
                out = [{"source": r[0], "id": r[1], "snippet": r[2]} for r in rows]
                break
    if not out:  # LIKE fallback (also covers FTS syntax edge cases)
        like = f"%{q}%"
        rows = con.execute(
            "SELECT source, id FROM conversations WHERE title LIKE ? OR cwd LIKE ? "
            "OR body LIKE ? LIMIT 800",
            (like, like, like),
        ).fetchall()
        out = [{"source": r[0], "id": r[1], "snippet": None} for r in rows]
    con.close()
    return out


ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class Handler(BaseHTTPRequestHandler):
    server_version = "ConvoBrowser/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def _host_ok(self):
        """Reject non-local Host headers (DNS-rebinding protection)."""
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("["):  # [::1]:port
            host = host.split("]")[0] + "]"
        else:
            host = host.rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def do_GET(self):
        if not self._host_ok():
            self._json({"error": "forbidden"}, 403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/conversations":
                con = index_con()
                cols = ",".join("c." + c for c in LIST_COLS.split(","))
                rows = con.execute(
                    f"SELECT {cols}, "
                    "CASE WHEN h.id IS NULL THEN 0 ELSE 1 END AS hidden, "
                    "CASE WHEN s.id IS NULL THEN 0 ELSE 1 END AS starred "
                    "FROM conversations c LEFT JOIN hidden h "
                    "ON h.source=c.source AND h.id=c.id "
                    "LEFT JOIN starred s ON s.source=c.source AND s.id=c.id "
                    "ORDER BY c.updated_at DESC"
                ).fetchall()
                con.close()
                self._json({"conversations": [dict(r) for r in rows]})
            elif path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                self._json({"matches": search_index(q) if len(q) >= 2 else []})
            elif path.startswith("/api/conversations/"):
                rest = path[len("/api/conversations/"):]
                source, _, conv_id = rest.partition("/")
                source, conv_id = unquote(source), unquote(conv_id)
                con = index_con()
                row = con.execute(
                    f"SELECT {LIST_COLS} FROM conversations WHERE source=? AND id=?",
                    (source, conv_id),
                ).fetchone()
                con.close()
                if row is None:
                    self._json({"error": "not found"}, 404)
                    return
                messages = load_transcript(source, row["origin_path"], conv_id)
                self._json({"meta": dict(row), "messages": messages})
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except BrokenPipeError:
                pass

    def do_POST(self):
        if not self._host_ok():
            self._json({"error": "forbidden"}, 403)
            return
        parsed = urlparse(self.path)
        body = self._read_body()
        try:
            if parsed.path == "/api/reindex":
                stats = reindex(full=bool(body.get("full")))
                self._json({"ok": True, "stats": stats})
            elif parsed.path == "/api/hide":
                source, conv_id = body.get("source"), body.get("id")
                if not source or not conv_id:
                    self._json({"error": "source and id required"}, 400)
                    return
                con = index_con()
                if body.get("hidden", True):
                    con.execute(
                        "INSERT OR IGNORE INTO hidden (source, id) VALUES (?, ?)",
                        (source, conv_id),
                    )
                else:
                    con.execute(
                        "DELETE FROM hidden WHERE source=? AND id=?", (source, conv_id)
                    )
                con.commit()
                con.close()
                self._json({"ok": True})
            elif parsed.path == "/api/delete":
                source, conv_id = body.get("source"), body.get("id")
                if not source or not conv_id:
                    self._json({"error": "source and id required"}, 400)
                    return
                status, payload = delete_conversation(source, conv_id)
                self._json(payload, status)
            elif parsed.path == "/api/star":
                source, conv_id = body.get("source"), body.get("id")
                if not source or not conv_id:
                    self._json({"error": "source and id required"}, 400)
                    return
                con = index_con()
                if body.get("starred", True):
                    con.execute(
                        "INSERT OR IGNORE INTO starred (source, id) VALUES (?, ?)",
                        (source, conv_id),
                    )
                else:
                    con.execute(
                        "DELETE FROM starred WHERE source=? AND id=?", (source, conv_id)
                    )
                con.commit()
                con.close()
                self._json({"ok": True})
            elif parsed.path == "/api/reveal":
                source, conv_id = body.get("source"), body.get("id")
                if not source or not conv_id:
                    self._json({"error": "source and id required"}, 400)
                    return
                con = index_con()
                row = con.execute(
                    "SELECT cwd FROM conversations WHERE source=? AND id=?",
                    (source, conv_id),
                ).fetchone()
                con.close()
                if row is None or not row["cwd"]:
                    self._json({"error": "no folder known for this conversation"}, 400)
                    return
                if not os.path.isdir(row["cwd"]):
                    self._json({"error": "folder no longer exists"}, 400)
                    return
                subprocess.run(["open", row["cwd"]], check=False)
                self._json({"ok": True})
            elif parsed.path == "/api/bulk-hide":
                items = body.get("items") or []
                hidden = body.get("hidden", True)
                con = index_con()
                for it in items:
                    source, conv_id = it.get("source"), it.get("id")
                    if not source or not conv_id:
                        continue
                    if hidden:
                        con.execute(
                            "INSERT OR IGNORE INTO hidden (source, id) VALUES (?, ?)",
                            (source, conv_id),
                        )
                    else:
                        con.execute(
                            "DELETE FROM hidden WHERE source=? AND id=?", (source, conv_id)
                        )
                con.commit()
                con.close()
                self._json({"ok": True, "count": len(items)})
            elif parsed.path == "/api/bulk-delete":
                items = body.get("items") or []
                results = []
                for it in items:
                    source, conv_id = it.get("source"), it.get("id")
                    if not source or not conv_id:
                        continue
                    status, payload = delete_conversation(source, conv_id)
                    results.append({
                        "source": source, "id": conv_id,
                        "ok": status == 200, "error": payload.get("error"),
                    })
                self._json({"ok": True, "results": results})
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except BrokenPipeError:
                pass


# -------------------------------------------------------------------- main ----


def cmd_scan(source):
    targets = list(SOURCES) if source == "all" else [source]
    for src in targets:
        if src not in SOURCES:
            print(f"unknown source: {src}", file=sys.stderr)
            continue
        list_units, parse_unit = SOURCES[src]
        t0 = time.time()
        units = list_units()
        parsed = errors = 0
        samples = []
        for key, (wm, args) in units.items():
            try:
                rec = parse_unit(key, args)
            except Exception as e:
                errors += 1
                print(f"  ! {key}: {e}", file=sys.stderr)
                continue
            parsed += 1
            if len(samples) < 3:
                samples.append(rec)
        print(f"\n== {src}: {parsed} parsed, {errors} errors, "
              f"{round(time.time() - t0, 1)}s ==")
        for rec in samples:
            print(f"  {rec['updated_at']}  [{rec['msg_count']}] "
                  f"{(rec['title'] or '')[:60]!r}  cwd={rec['cwd']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", metavar="SOURCE",
                    help="dry-run a scanner (or 'all') and print results; no index writes")
    ap.add_argument("--full", action="store_true", help="force full reindex")
    ap.add_argument("--no-index", action="store_true", help="skip indexing on startup")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    if args.scan:
        cmd_scan(args.scan)
        return

    url = f"http://localhost:{args.port}"
    try:
        # bind first: if another instance is already serving, just open the browser
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"Port {args.port} already in use — opening {url} (app already running?)")
        if not args.no_browser:
            webbrowser.open(url)
        return
    init_db()
    if not args.no_index:
        print("Indexing…")
        reindex(full=args.full)
    if AUTO_REINDEX_MIN > 0:
        def auto_reindex():
            while True:
                time.sleep(AUTO_REINDEX_MIN * 60)
                try:
                    reindex()
                except Exception as e:
                    print(f"auto-reindex failed: {e}", file=sys.stderr)
        threading.Thread(target=auto_reindex, daemon=True).start()
    print(f"Serving {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

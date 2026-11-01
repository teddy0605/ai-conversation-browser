"""Tests for app.py. Stdlib only.

HOME and ACB_INDEX_DB point at a temp dir before app is imported, so every
source path and the index live there. Real conversations and the real
index.db are never read or written. Subprocess calls are mocked.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="acb-test-home-")
os.environ["HOME"] = _HOME.name
os.environ["ACB_INDEX_DB"] = os.path.join(_HOME.name, "index.db")
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402


def tearDownModule():
    _HOME.cleanup()


class _Conn:
    """Fake socket for driving app.Handler without a server."""

    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args, **kwargs):
        return self.data

    def sendall(self, data):
        self.output.extend(data)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="acb-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for name, value in {
            "INDEX_DB": os.path.join(self.tmp, "index.db"),
            "HERMES_DB": os.path.join(self.tmp, "hermes.db"),
            "HERMES_SESSIONS": os.path.join(self.tmp, "hermes-sessions"),
            "CURSOR_CHATS": os.path.join(self.tmp, "cursor-chats"),
            "GROK_SESSIONS": os.path.join(self.tmp, "grok"),
        }.items():
            p = patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)
        app.init_db()

    def add_record(self, source, conv_id, origin, **extra):
        rec = dict(id=conv_id, title="Fixture", cwd=self.tmp, created_at=None,
                   updated_at=None, msg_count=1, size_bytes=0, origin_path=origin,
                   deletable=1, body="fixture text")
        rec.update(extra)
        con = app.index_con()
        app.upsert(con, source, origin, "wm", rec)
        con.commit()
        con.close()

    def records(self, source):
        con = app.index_con()
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM conversations WHERE source=?", (source,))]
        con.close()
        return rows

    def make_hermes_db(self):
        con = sqlite3.connect(app.HERMES_DB)
        con.executescript(
            "CREATE TABLE sessions (id TEXT, title TEXT, cwd TEXT, started_at REAL, "
            "ended_at REAL, message_count INTEGER);"
            "CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, "
            "tool_calls TEXT, tool_call_id TEXT, timestamp REAL);"
        )
        return con


# 1. S1: CSRF guard on POST
class CsrfTest(Base):
    def post(self, headers):
        raw = json.dumps({"source": "claude-code", "id": "target"}).encode()
        head = "POST /api/hide HTTP/1.1\r\nHost: localhost:8377\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        head += f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
        conn = _Conn(head.encode() + raw)
        app.Handler(conn, ("127.0.0.1", 1), None)
        return int(bytes(conn.output).split(b" ")[1])

    def hidden_count(self):
        con = app.index_con()
        n = con.execute("SELECT count(*) FROM hidden").fetchone()[0]
        con.close()
        return n

    def test_rejects(self):
        cases = [
            {"Origin": "http://evil.example", "Content-Type": "application/json"},
            {"Origin": "null", "Content-Type": "application/json"},
            {"Content-Type": "text/plain"},
            {},
            {"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers), 403)
        self.assertEqual(self.hidden_count(), 0)

    def test_accepts_local_json(self):
        cases = [
            {"Content-Type": "application/json"},  # menu bar app, self-POST from main()
            {"Origin": "http://localhost:8377", "Content-Type": "application/json",
             "Sec-Fetch-Site": "same-origin"},
            {"Origin": "http://127.0.0.1:8377", "Content-Type": "application/json; charset=utf-8"},
            {"Origin": "http://[::1]:8377", "Content-Type": "application/json"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers), 200)


# 2. S2: the path goes to osascript as an argument
class TrashTest(Base):
    def test_path_not_in_script(self):
        path = os.path.join(self.tmp, 'x"\ndo shell script "echo PWNED"\n--.jsonl')
        open(path, "w").close()
        with patch.object(app.subprocess, "run") as run:
            self.assertEqual(app.trash(path), "finder")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-1], path)
        scripts = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-e"]
        self.assertTrue(scripts)
        self.assertFalse(any("PWNED" in s for s in scripts))


# 4. S4: Grok msg_count is an int or None
class GrokCountTest(Base):
    def parse(self, count):
        d = os.path.join(app.GROK_SESSIONS, "ws", "sess")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "summary.json"), "w") as fh:
            json.dump({"info": {"id": "sess"}, "num_chat_messages": count}, fh)
        return app.parse_grok_unit(d, None)

    def test_count(self):
        self.assertEqual(self.parse(7)["msg_count"], 7)
        self.assertIsNone(self.parse('<img src=x onerror="alert(1)">')["msg_count"])


# 5. C1: Hermes resume uses the internal session_id
class HermesResumeTest(Base):
    def test_file_copy_keeps_ui_id_and_stores_resume_id(self):
        os.makedirs(app.HERMES_SESSIONS)
        path = os.path.join(app.HERMES_SESSIONS, "session_dump-copy-2.json")
        with open(path, "w") as fh:
            json.dump({"session_id": "real-session-id",
                       "messages": [{"role": "user", "content": "hi"}]}, fh)
        app.reindex(only="hermes")
        con = app.index_con()
        row = con.execute(f"SELECT {app.LIST_COLS} FROM conversations WHERE source='hermes'").fetchone()
        con.close()
        self.assertEqual(row["id"], "dump-copy-2")
        self.assertEqual(row["resume_id"], "real-session-id")

    def test_old_index_gets_column_and_hermes_reparse(self):
        app.INDEX_DB = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(app.INDEX_DB)
        con.executescript(
            "CREATE TABLE conversations (source TEXT NOT NULL, id TEXT NOT NULL, "
            "unit_key TEXT NOT NULL, title TEXT, cwd TEXT, created_at TEXT, updated_at TEXT, "
            "msg_count INTEGER, size_bytes INTEGER, origin_path TEXT NOT NULL, "
            "deletable INTEGER NOT NULL, body TEXT, PRIMARY KEY (source, id));"
            "CREATE TABLE indexed_units (source TEXT NOT NULL, unit_key TEXT NOT NULL, "
            "watermark TEXT NOT NULL, PRIMARY KEY (source, unit_key));"
            "INSERT INTO indexed_units VALUES ('hermes', 'file:/x', 'w'), ('codex', '/y', 'w');"
        )
        con.close()
        app.init_db()
        con = app.index_con()
        cols = [r[1] for r in con.execute("PRAGMA table_info(conversations)")]
        units = [r[0] for r in con.execute("SELECT source FROM indexed_units")]
        con.close()
        self.assertIn("resume_id", cols)
        self.assertEqual(units, ["codex"])


# 6. C2: Hermes DB watermark follows messages, count and title
class HermesWatermarkTest(Base):
    def test_watermark_changes(self):
        con = self.make_hermes_db()
        con.execute("INSERT INTO sessions VALUES ('s1', 'Old', ?, 1, NULL, 1)", (self.tmp,))
        con.execute("INSERT INTO messages VALUES ('s1', 'user', 'oldword', NULL, NULL, 1)")
        con.commit()
        app.reindex(only="hermes")
        wm1 = app.list_hermes_units()["db:s1"][0]

        con.execute("INSERT INTO messages VALUES ('s1', 'assistant', 'newword', NULL, NULL, 2)")
        con.commit()
        wm2 = app.list_hermes_units()["db:s1"][0]
        self.assertNotEqual(wm1, wm2)

        con.execute("UPDATE sessions SET message_count=2")
        con.commit()
        wm3 = app.list_hermes_units()["db:s1"][0]
        self.assertNotEqual(wm2, wm3)

        con.execute("UPDATE sessions SET title='New'")
        con.commit()
        wm4 = app.list_hermes_units()["db:s1"][0]
        self.assertNotEqual(wm3, wm4)
        con.close()

        app.reindex(only="hermes")
        rec = self.records("hermes")[0]
        self.assertEqual((rec["title"], rec["msg_count"]), ("New", 2))
        self.assertIn("newword", rec["body"])


# 7. C3: Cursor CLI watermark includes the -wal file
class CursorWalTest(Base):
    def test_wal_write_changes_watermark(self):
        chat = os.path.join(app.CURSOR_CHATS, "ws", "chat-id")
        os.makedirs(chat)
        writer = sqlite3.connect(os.path.join(chat, "store.db"))
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executescript("CREATE TABLE meta (key TEXT, value BLOB); CREATE TABLE blobs (data BLOB);")
        writer.execute("INSERT INTO meta VALUES ('meta', ?)",
                       (json.dumps({"agentId": "chat-id", "name": "WAL"}).encode(),))
        writer.execute("INSERT INTO blobs VALUES (?)",
                       (json.dumps({"role": "user", "content": "firstword"}).encode(),))
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        app.reindex(only="cursor-cli")
        before = app.list_cursor_cli_units()[chat][0]

        writer.execute("INSERT INTO blobs VALUES (?)",
                       (json.dumps({"role": "assistant", "content": "secondword"}).encode(),))
        writer.commit()
        after = app.list_cursor_cli_units()[chat][0]
        self.assertNotEqual(before, after)

        app.reindex(only="cursor-cli")
        self.assertIn("secondword", self.records("cursor-cli")[0]["body"])


# 11. C7: deleting a Hermes file copy also deletes its DB twin
class HermesTwinDeleteTest(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(app.HERMES_SESSIONS)
        self.hdb = self.make_hermes_db()
        self.addCleanup(self.hdb.close)

    def add_file(self, sid, internal=None):
        path = os.path.join(app.HERMES_SESSIONS, f"session_{sid}.json")
        with open(path, "w") as fh:
            json.dump({"session_id": internal or sid, "messages": [{"role": "user", "content": "file copy"}]}, fh)
        return path

    def fake_cli(self, argv, **kwargs):
        self.hdb.execute("DELETE FROM sessions WHERE id=?", (argv[3],))
        self.hdb.commit()
        return subprocess.CompletedProcess(argv, 0, "", "")

    def delete(self, sid):
        trash = lambda p: os.rename(p, p + ".trashed") or "finder"  # noqa: E731
        with patch.object(app, "trash", side_effect=trash), \
                patch.object(app.shutil, "which", return_value="/bin/hermes"), \
                patch.object(app.subprocess, "run", side_effect=self.fake_cli) as run:
            status, _ = app.delete_conversation("hermes", sid)
        self.assertEqual(status, 200)
        return run

    def test_twin_deleted_and_not_resurrected(self):
        self.add_file("dup")
        self.hdb.execute("INSERT INTO sessions VALUES ('dup', 'DB copy', ?, 1, 2, 1)", (self.tmp,))
        self.hdb.commit()
        app.reindex(only="hermes")
        run = self.delete("dup")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/bin/hermes", "sessions", "delete", "dup", "--yes"])
        app.reindex(only="hermes")
        self.assertEqual(self.records("hermes"), [])

    def test_twin_found_by_internal_id(self):
        self.add_file("export-copy", internal="real-session")
        self.hdb.executemany("INSERT INTO sessions VALUES (?, 'DB', ?, 1, 2, 1)",
                             [("real-session", self.tmp), ("export-copy", self.tmp)])
        self.hdb.commit()
        app.reindex(only="hermes")
        run = self.delete("export-copy")
        self.assertEqual(run.call_args.args[0],
                         ["/bin/hermes", "sessions", "delete", "real-session", "--yes"])
        ids = [r[0] for r in self.hdb.execute("SELECT id FROM sessions")]
        self.assertEqual(ids, ["export-copy"])
        self.assertEqual([r["id"] for r in self.records("hermes")], [])  # twin row gone
        con = app.index_con()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM conversations_fts").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM indexed_units").fetchone()[0], 0)
        con.close()
        app.reindex(only="hermes")
        self.assertEqual([r["id"] for r in self.records("hermes")], ["export-copy"])

    def test_file_without_twin_skips_cli(self):
        self.add_file("solo")
        app.reindex(only="hermes")
        run = self.delete("solo")
        run.assert_not_called()


# 12. C8: the index connection waits for locks
class IndexTimeoutTest(Base):
    def test_timeout(self):
        with patch.object(app.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            app.index_con().close()
        self.assertEqual(connect.call_args.kwargs.get("timeout"), 60)


# 13. C9: NEAR hits rank first
class NearBoostTest(Base):
    def test_close_words_rank_first(self):
        far = "alpha " + "filler " * 20 + "beta " + "filler " * 20
        self.add_record("codex", "far", "/far", body=far * 3)
        self.add_record("codex", "close", "/close", body="alpha beta " + "filler " * 300)
        con = app.index_con()
        plain = [r[1] for r in con.execute(
            "SELECT source, id FROM conversations_fts WHERE conversations_fts MATCH ? ORDER BY rank",
            (app.fts_match_exprs(["alpha", "beta"], False)[0],))]
        con.close()
        self.assertEqual(plain[0], "far")  # without the boost, bm25 puts "far" first
        self.assertEqual([m["id"] for m in app.search_index("alpha beta")], ["close", "far"])


# 14. P1: Claude profile dirs are discovered
class ClaudeRootsTest(Base):
    def test_discovery(self):
        home = os.path.join(self.tmp, "home")
        for d in (".claude/projects/p", ".claude-work/projects/p",
                  ".claude-work/archived_projects/p", ".claude-empty"):
            os.makedirs(os.path.join(home, d))
        open(os.path.join(home, ".claude.json"), "w").close()
        roots = app.claude_roots(home)
        self.assertEqual(roots, [
            os.path.join(home, ".claude", "projects"),
            os.path.join(home, ".claude", "archived_projects"),
            os.path.join(home, ".claude-work", "projects"),
            os.path.join(home, ".claude-work", "archived_projects"),
        ])
        for d in (".claude/projects/p", ".claude-work/projects/p", ".claude-work/archived_projects/p"):
            open(os.path.join(home, d, "s.jsonl"), "w").close()
        with patch.object(app, "HOME", home):
            self.assertEqual(len(app.list_claude_units()), 3)

    def test_profile_created_after_start(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with patch.object(app, "HOME", home):
            self.assertEqual(app.list_claude_units(), {})
            os.makedirs(os.path.join(home, ".claude-new", "projects", "p"))
            open(os.path.join(home, ".claude-new", "projects", "p", "s.jsonl"), "w").close()
            self.assertEqual(len(app.list_claude_units()), 1)


if __name__ == "__main__":
    unittest.main()

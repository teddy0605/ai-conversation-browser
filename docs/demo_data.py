"""Build a fake HOME with invented AI tool sessions, for screenshots and demos.

Usage:
    python3 docs/demo_data.py /tmp/acb-demo-home
    HOME=/tmp/acb-demo-home ACB_INDEX_DB=/tmp/acb-demo.db ACB_PORT=8399 \
        python3 app.py --no-browser

All people, projects and prompts are made up. The files use the same formats
app.py reads, so the app shows them like real sessions.
"""

import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

USER_HOME = "/Users/alex"  # the cwd value written into sessions
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def proj(name):
    return f"{USER_HOME}/code/{name}"


# Each conversation: source, title, cwd, hours ago (last activity), turns.
# A turn is ("user", text), ("assistant", text) or ("tool", name, args, output).
CONVERSATIONS = [
    ("claude-code", "Fix flaky checkout test in the cart service", proj("acme-shop"), 2, [
        ("user", "The checkout test fails about one run in five on CI. Can you find out why?"),
        ("assistant", "I will run the test in a loop and look at what changes between runs."),
        ("tool", "Bash", {"command": "pytest tests/test_checkout.py -x --count=20 -q",
                          "description": "Run the checkout test 20 times"},
         "...............F....\nFAILED tests/test_checkout.py::test_total_with_coupon\n"
         "AssertionError: assert Decimal('41.99') == Decimal('42.00')\n1 failed, 19 passed in 3.41s"),
        ("tool", "Read", {"file_path": f"{proj('acme-shop')}/cart/pricing.py"},
         "def apply_coupon(total, coupon):\n    rate = random_rounding()\n    return total * (1 - coupon.percent / 100)"),
        ("assistant", "The coupon math uses float division, and the rounding mode comes from a "
                      "shared helper that other tests change. I will switch to `Decimal` with "
                      "`ROUND_HALF_UP` and pin the rounding in the test fixture."),
        ("tool", "Edit", {"file_path": f"{proj('acme-shop')}/cart/pricing.py",
                          "old_string": "return total * (1 - coupon.percent / 100)",
                          "new_string": "return (total * (100 - coupon.percent) / 100).quantize(CENT, ROUND_HALF_UP)"},
         "The file was updated."),
        ("tool", "Bash", {"command": "pytest tests/test_checkout.py --count=50 -q"}, "50 passed in 7.92s"),
        ("assistant", "Fixed. The test passed 50 runs in a row. The cause was float rounding in "
                      "`apply_coupon`."),
    ]),
    ("codex", "Add rate limiting to the public API", proj("acme-shop"), 20, [
        ("user", "Add a simple rate limit to /api/products: 60 requests per minute per IP."),
        ("assistant", "I will add a small token bucket middleware and a test."),
        ("tool", "shell", {"command": ["bash", "-lc", "rg -n 'app.use' server/"]},
         "server/app.js:14:app.use(express.json())\nserver/app.js:15:app.use('/api', apiRouter)"),
        ("tool", "apply_patch", "*** Begin Patch\n*** Add File: server/rateLimit.js\n+const buckets = new Map()\n*** End Patch",
         "Success. Updated the following files:\nA server/rateLimit.js"),
        ("tool", "shell", {"command": ["bash", "-lc", "npm test -- rateLimit"]},
         "PASS server/rateLimit.test.js\n  rate limit\n    ok allows 60 requests\n    ok blocks the 61st request"),
        ("assistant", "Done. `/api/products` now returns 429 after 60 requests per minute per IP."),
    ]),
    ("cursor-cli", "Migrate the product images to WebP", proj("acme-shop"), 50, [
        ("user", "Convert the PNG product images in public/img to WebP and update the templates."),
        ("tool", "run_terminal_cmd", {"command": "ls public/img | wc -l"}, "128"),
        ("assistant", "There are 128 images. I will convert them with `cwebp` at quality 82."),
        ("tool", "run_terminal_cmd", {"command": "for f in public/img/*.png; do cwebp -q 82 \"$f\" -o \"${f%.png}.webp\"; done"},
         "Saved 128 files, total size 9.4 MB -> 3.1 MB"),
        ("assistant", "Converted. The image folder went from 9.4 MB to 3.1 MB."),
    ]),
    ("opencode", "Write a README for weather-cli", proj("weather-cli"), 5, [
        ("user", "Write a short README for this tool: install, usage, config."),
        ("tool", "read", {"filePath": f"{proj('weather-cli')}/src/main.rs"},
         "fn main() {\n    let args = Args::parse();\n    let city = args.city.unwrap_or(config.default_city);"),
        ("assistant", "Here is a README with install steps, three usage examples and the config file format."),
        ("tool", "write", {"filePath": f"{proj('weather-cli')}/README.md"}, "Wrote 64 lines"),
    ]),
    ("claude-code", "Add a --units flag (metric or imperial)", proj("weather-cli"), 30, [
        ("user", "Add a --units flag. Default to metric."),
        ("tool", "Grep", {"pattern": "celsius", "path": "src"}, "src/format.rs:12:    format!(\"{:.1} celsius\", t)"),
        ("assistant", "I added `--units metric|imperial` with clap and converted the output in `format.rs`."),
        ("tool", "Bash", {"command": "cargo test"}, "running 9 tests\ntest result: ok. 9 passed; 0 failed"),
    ]),
    ("grok", "Why does the weather API return 401 after midnight?", proj("weather-cli"), 75, [
        ("user", "The weather API returns 401 for a few minutes after midnight UTC. Why?"),
        ("tool", "search_files", {"pattern": "expires_at", "path": "src/auth.rs"},
         "src/auth.rs:31: if now > token.expires_at { refresh() }"),
        ("assistant", "The token cache compares local time with a UTC expiry. Use `Utc::now()` in `auth.rs`."),
    ]),
    ("hermes", "Plan the blog migration from Jekyll to Hugo", proj("blog"), 9, [
        ("user", "Plan the move of my blog from Jekyll to Hugo. Keep the old URLs working."),
        ("tool", "terminal", {"command": "find _posts -name '*.md' | wc -l"}, "87"),
        ("assistant", "Plan: 1. Convert front matter. 2. Map `permalink` to Hugo `url`. "
                      "3. Add aliases for the 12 posts that changed slug. 4. Compare the sitemaps."),
    ]),
    ("claude-code", "Dark mode for the blog theme", proj("blog"), 100, [
        ("user", "Add a dark mode to the theme. Follow the OS setting."),
        ("tool", "Read", {"file_path": f"{proj('blog')}/assets/css/main.css"}, ":root { --bg: #fff; --fg: #222; }"),
        ("assistant", "I added a `prefers-color-scheme: dark` block that swaps the CSS variables."),
    ]),
    ("codex", "Terraform plan shows drift on the S3 bucket", proj("infra"), 26, [
        ("user", "terraform plan wants to change the logs bucket every run. Find the drift."),
        ("tool", "shell", {"command": ["bash", "-lc", "terraform plan -target=aws_s3_bucket.logs -no-color"]},
         "  ~ resource \"aws_s3_bucket\" \"logs\" {\n      ~ tags = {\n          - \"CostCenter\" = \"ops\" -> null\n        }"),
        ("assistant", "A tag policy adds `CostCenter` outside Terraform. Add it to `default_tags` or ignore it."),
    ]),
    ("opencode", "Upgrade the EKS module to v20", proj("infra"), 140, [
        ("user", "Upgrade the eks module from v19 to v20. What breaks?"),
        ("tool", "bash", {"command": "terraform init -upgrade"}, "Upgrading modules...\n- eks in .terraform/modules/eks"),
        ("assistant", "v20 moves `aws-auth` to access entries. I listed the three resources that need a `moved` block."),
    ]),
    ("cursor-cli", "Sort photos into folders by EXIF date", proj("photo-sorter"), 60, [
        ("user", "Write a script that moves photos into YYYY/MM folders by EXIF date."),
        ("assistant", "I wrote `sort.py`. It reads `DateTimeOriginal` and falls back to the file time."),
        ("tool", "run_terminal_cmd", {"command": "python3 sort.py --dry-run ~/Pictures/inbox"},
         "would move 412 files into 23 folders"),
    ]),
    ("claude-code", "Speed up the product search query", proj("acme-shop"), 170, [
        ("user", "Product search takes 2 seconds. Can we make it faster?"),
        ("tool", "Bash", {"command": "psql -c 'EXPLAIN ANALYZE SELECT * FROM products WHERE name ILIKE \\'%lamp%\\''"},
         "Seq Scan on products  (cost=0.00..18334.00 rows=96) (actual time=1912.3 ms)"),
        ("assistant", "A trigram index fixes the sequential scan. The query now takes 14 ms."),
    ]),
    ("codex", "Explain the order state machine", proj("acme-shop"), 220, [
        ("user", "Explain how an order moves from cart to shipped."),
        ("assistant", "An order goes cart -> pending_payment -> paid -> packed -> shipped. "
                      "Refunds can start from paid or packed."),
    ]),
    ("grok", "Generate test fixtures for the cart service", proj("acme-shop"), 300, [
        ("user", "Generate 20 realistic product fixtures for the cart tests."),
        ("assistant", "Here are 20 fixtures with names, prices in cents and stock counts."),
    ]),
    ("claude-code", "Set up GitHub Actions for weather-cli", proj("weather-cli"), 360, [
        ("user", "Add CI: build and test on Linux and macOS."),
        ("tool", "Write", {"file_path": f"{proj('weather-cli')}/.github/workflows/ci.yml"}, "File created."),
        ("assistant", "CI runs `cargo test` on ubuntu-latest and macos-latest."),
    ]),
    ("hermes", "Draft a post about the Hugo migration", proj("blog"), 420, [
        ("user", "Draft a short post about what I learned moving the blog to Hugo."),
        ("assistant", "Draft: build time went from 40 s to 0.8 s, and URL aliases kept old links working."),
    ]),
    ("opencode", "Rotate the database password in staging", proj("infra"), 500, [
        ("user", "Rotate the staging database password without downtime."),
        ("tool", "bash", {"command": "aws secretsmanager rotate-secret --secret-id staging/db"}, "{\n  \"VersionId\": \"example\"\n}"),
        ("assistant", "Rotation started. The app reads the new secret on the next connection pool refresh."),
    ]),
    ("cursor-cli", "Add a duplicate finder to photo-sorter", proj("photo-sorter"), 610, [
        ("user", "Find duplicate photos by content hash, not by name."),
        ("assistant", "I added `--dupes`. It groups files by SHA-256 and prints each group."),
    ]),
    ("claude-code", "Refactor the email templates", proj("acme-shop"), 700, [
        ("user", "The order emails repeat the same header and footer. Clean that up."),
        ("assistant", "I moved the header and footer into `base.html` and the four emails now extend it."),
    ]),
    ("pi", "Add retries to the image upload", proj("acme-shop"), 40, [
        ("user", "Uploads to the image bucket fail sometimes with a timeout. Add retries."),
        ("tool", "bash", {"command": "rg -n 'upload_file' cart/ media/"},
         "media/storage.py:22:    client.upload_file(path, BUCKET, key)"),
        ("assistant", "I wrapped `upload_file` in a retry with backoff: 3 tries, 1 s, 2 s, 4 s."),
        ("tool", "bash", {"command": "pytest tests/test_storage.py -q"}, "6 passed in 0.84s"),
    ]),
    ("pi", "Tidy up the Terraform variables", proj("infra"), 260, [
        ("user", "Many variables in infra/ have no description. Add them."),
        ("assistant", "I added descriptions to 31 variables and marked 4 unused ones for removal."),
    ]),
    ("codex", "Cache weather responses for 10 minutes", proj("weather-cli"), 760, [
        ("user", "Cache API responses on disk for 10 minutes."),
        ("assistant", "Responses now go to `~/.cache/weather-cli/` with the fetch time in the file name."),
    ]),
]


def ts(hours_ago, minute=0):
    return NOW - timedelta(hours=hours_ago) + timedelta(minutes=minute)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def timed_turns(hours_ago, turns):
    """Spread the turns over the minutes before the last-activity time."""
    n = len(turns)
    return [(ts(hours_ago, i - n), t) for i, t in enumerate(turns)]


def dashed(path):
    return path.replace("/", "-")


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def claude(home, title, cwd, hours, turns):
    sid = str(uuid.uuid4())
    rows = [{"type": "ai-title", "aiTitle": title}]
    for when, t in timed_turns(hours, turns):
        base = {"cwd": cwd, "timestamp": iso(when), "isSidechain": False}
        if t[0] in ("user", "assistant"):
            rows.append({**base, "type": t[0], "message": {"role": t[0], "content": t[1]}})
            continue
        call_id = "toolu_" + uuid.uuid4().hex[:20]
        rows.append({**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": call_id, "name": t[1], "input": t[2]}]}})
        rows.append({**base, "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": t[3]}]}})
    write_jsonl(os.path.join(home, ".claude", "projects", dashed(cwd), sid + ".jsonl"), rows)


def codex(home, title, cwd, hours, turns):
    sid = str(uuid.uuid4())
    start = ts(hours, -len(turns))
    rows = [{"timestamp": iso(start), "type": "session_meta",
             "payload": {"id": sid, "cwd": cwd, "timestamp": iso(start)}}]
    for when, t in timed_turns(hours, turns):
        if t[0] in ("user", "assistant"):
            kind = "input_text" if t[0] == "user" else "output_text"
            p = {"type": "message", "role": t[0], "content": [{"type": kind, "text": t[1]}]}
            rows.append({"timestamp": iso(when), "type": "response_item", "payload": p})
            continue
        call_id = "call_" + uuid.uuid4().hex[:16]
        if isinstance(t[2], str):
            call = {"type": "custom_tool_call", "name": t[1], "input": t[2], "call_id": call_id}
            out = {"type": "custom_tool_call_output", "call_id": call_id, "output": t[3]}
        else:
            call = {"type": "function_call", "name": t[1], "arguments": json.dumps(t[2]), "call_id": call_id}
            out = {"type": "function_call_output", "call_id": call_id,
                   "output": json.dumps({"output": t[3], "metadata": {"exit_code": 0}})}
        rows.append({"timestamp": iso(when), "type": "response_item", "payload": call})
        rows.append({"timestamp": iso(when), "type": "response_item", "payload": out})
    day = start.strftime("%Y/%m/%d")
    name = f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
    write_jsonl(os.path.join(home, ".codex", "sessions", day, name), rows)
    with open(os.path.join(home, ".codex", "session_index.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": sid, "thread_name": title}) + "\n")


def openai_rows(hours, turns, tool_role):
    """OpenAI-style messages: tool calls on the assistant, results as their own rows."""
    rows = []
    for when, t in timed_turns(hours, turns):
        if t[0] in ("user", "assistant"):
            rows.append({"role": t[0], "content": t[1], "timestamp": iso(when)})
            continue
        call_id = "call_" + uuid.uuid4().hex[:16]
        rows.append({"role": "assistant", "content": "", "timestamp": iso(when), "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": t[1], "arguments": json.dumps(t[2])}}]})
        rows.append({"role": tool_role, "tool_call_id": call_id, "content": t[3], "timestamp": iso(when)})
    return rows


def grok(home, title, cwd, hours, turns):
    sid = str(uuid.uuid4())
    d = os.path.join(home, ".grok", "sessions", dashed(cwd), sid)
    rows = openai_rows(hours, turns, "tool_result")
    write_jsonl(os.path.join(d, "chat_history.jsonl"), [{"type": r.pop("role"), **r} for r in rows])
    summary = {"info": {"id": sid, "cwd": cwd}, "generated_title": title,
               "created_at": rows[0]["timestamp"], "updated_at": rows[-1]["timestamp"],
               "num_chat_messages": sum(1 for t in turns if t[0] != "tool")}
    with open(os.path.join(d, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh)


def hermes(home, title, cwd, hours, turns):
    sid = NOW.strftime("%Y%m%d") + "_" + uuid.uuid4().hex[:6]
    rows = openai_rows(hours, turns, "tool")
    for r in rows:
        del r["timestamp"]
    obj = {"session_id": sid, "platform": "cli", "cwd": cwd,
           "session_start": iso(ts(hours, -len(turns))), "last_updated": iso(ts(hours)),
           "messages": rows}
    path = os.path.join(home, ".hermes", "sessions", f"session_{sid}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def opencode_db(home):
    path = os.path.join(home, ".local", "share", "opencode", "opencode.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, directory TEXT, title TEXT,"
        " time_created INTEGER, time_updated INTEGER);"
        "CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT,"
        " time_created INTEGER, data TEXT);"
        "CREATE TABLE IF NOT EXISTS part (id TEXT PRIMARY KEY, message_id TEXT,"
        " session_id TEXT, data TEXT);")
    return con


def opencode(con, title, cwd, hours, turns):
    sid = "ses_" + uuid.uuid4().hex[:20]
    timed = timed_turns(hours, turns)
    ms = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731
    con.execute("INSERT INTO session VALUES (?,?,?,?,?)",
                (sid, cwd, title, ms(timed[0][0]), ms(timed[-1][0])))
    for when, t in timed:
        mid = "msg_" + uuid.uuid4().hex[:20]
        role = "assistant" if t[0] == "tool" else t[0]
        con.execute("INSERT INTO message VALUES (?,?,?,?)",
                    (mid, sid, ms(when), json.dumps({"role": role, "time": {"created": ms(when)}})))
        if t[0] == "tool":
            part = {"type": "tool", "tool": t[1],
                    "state": {"status": "completed", "input": t[2], "output": t[3]}}
        else:
            part = {"type": "text", "text": t[1]}
        con.execute("INSERT INTO part VALUES (?,?,?,?)",
                    ("prt_" + uuid.uuid4().hex[:20], mid, sid, json.dumps(part)))


def cursor_cli(home, title, cwd, hours, turns):
    chat = str(uuid.uuid4())
    d = os.path.join(home, ".cursor", "chats", uuid.uuid4().hex, chat)
    os.makedirs(d)
    db = os.path.join(d, "store.db")
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                      "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);")
    created = ts(hours, -len(turns))
    meta = {"agentId": chat, "name": title, "createdAt": int(created.timestamp() * 1000)}
    con.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta).encode().hex(),))
    blobs = [{"role": "system", "content": f"<user_info>\nOS: darwin\nWorkspace Path: {cwd}\n</user_info>"}]
    for t in turns:
        if t[0] in ("user", "assistant"):
            blobs.append({"role": t[0], "content": [{"type": "text", "text": t[1]}]})
            continue
        call_id = "toolu_" + uuid.uuid4().hex[:20]
        blobs.append({"role": "assistant", "content": [
            {"type": "tool-call", "toolCallId": call_id, "toolName": t[1], "args": t[2]}]})
        blobs.append({"role": "tool", "content": [
            {"type": "tool-result", "toolCallId": call_id, "toolName": t[1], "result": t[3]}]})
    for b in blobs:
        con.execute("INSERT INTO blobs VALUES (?, ?)", (uuid.uuid4().hex, json.dumps(b).encode()))
    con.commit()
    con.close()
    t = ts(hours).timestamp()
    os.utime(db, (t, t))  # the app takes "updated" from the file time


def pi(home, title, cwd, hours, turns):
    sid = str(uuid.uuid4())
    start = ts(hours, -len(turns) - 1)
    rows = [{"type": "session", "version": 3, "id": sid, "timestamp": iso(start), "cwd": cwd}]
    parent = None

    def add(entry):
        nonlocal parent
        entry.update(id=uuid.uuid4().hex[:8], parentId=parent)
        parent = entry["id"]
        rows.append(entry)

    add({"type": "session_info", "timestamp": iso(start), "name": title})
    for when, t in timed_turns(hours, turns):
        if t[0] in ("user", "assistant"):
            msg = {"role": t[0], "content": [{"type": "text", "text": t[1]}]}
            add({"type": "message", "timestamp": iso(when), "message": msg})
            continue
        call_id = "call_" + uuid.uuid4().hex[:16]
        add({"type": "message", "timestamp": iso(when), "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": call_id, "name": t[1], "arguments": t[2]}]}})
        add({"type": "message", "timestamp": iso(when), "message": {
            "role": "toolResult", "toolCallId": call_id, "toolName": t[1],
            "content": [{"type": "text", "text": t[3]}]}})
    name = f"{start.strftime('%Y-%m-%dT%H-%M-%S-000Z')}_{sid}.jsonl"
    write_jsonl(os.path.join(home, ".pi", "agent", "sessions", f"-{dashed(cwd)}--", name), rows)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: demo_data.py <fake-home-dir>")
    home = os.path.abspath(sys.argv[1])
    if os.path.exists(home):
        shutil.rmtree(home)
    os.makedirs(home)
    con = opencode_db(home)
    writers = {"claude-code": claude, "codex": codex, "grok": grok, "hermes": hermes,
               "cursor-cli": cursor_cli, "pi": pi, "opencode": lambda _h, *a: opencode(con, *a)}
    for source, title, cwd, hours, turns in CONVERSATIONS:
        writers[source](home, title, cwd, hours, turns)
    con.commit()
    con.close()
    print(f"Wrote {len(CONVERSATIONS)} conversations to {home}")


if __name__ == "__main__":
    main()

# AI Conversation Browser

One local web page to browse, search and clean up conversations from **all** the AI coding
tools on your machine — because you remember *the conversation*, not *which tool* it was in.

Supported sources:

| Tool | Storage read | Delete support |
|---|---|---|
| Claude Code | `~/.claude/projects`, `~/.claude-j/projects` (JSONL) | ✓ move to Trash |
| Codex CLI | `~/.codex/sessions` (JSONL) | ✓ move to Trash |
| Grok CLI | `~/.grok/sessions` (JSON/JSONL) | ✓ move to Trash |
| OpenCode | `~/.local/share/opencode/opencode.db` (SQLite) | ✓ via `opencode session delete` |
| Hermes | `~/.hermes/sessions/*.json` + `~/.hermes/state.db` | ✓ files via Trash, db-only via `hermes sessions delete` |
| Cursor IDE | `~/Library/Application Support/Cursor/.../state.vscdb` (SQLite) | read-only → hide |
| Cursor CLI | `~/.cursor/chats` (SQLite per chat) | ✓ move to Trash |

Missing tools are skipped silently, so it runs fine on machines that only have some of them.

## Run

```bash
python3 app.py            # index new/changed sessions, serve http://localhost:8377, open browser
python3 app.py --full     # force a full reindex first
python3 app.py --scan all # dry-run the scanners, print counts, no writes
python3 app.py --port 9000 --no-browser
```

Python 3.9+ stdlib only — nothing to install. The first index takes a few seconds;
later startups only reparse sessions that changed. If the port is already in use,
it assumes the app is running and just opens the browser — so a shell alias like

```bash
alias convos='python3 /path/to/ai-conversation-browser/app.py'
```

works as both "launch" and "bring it up again".

## Features

- **Unified list** with tool badge, title, workspace folder, created/updated times, message count
- **Search**: instant fuzzy matching on titles/folders + SQLite FTS5 full-text search over
  message content, with highlighted snippets
- **Filters**: click a tool chip to solo it (⌘-click to multi-toggle), folder dropdown, sorting,
  ★ starred-only toggle
- **Keyboard navigation**: `j`/`k` or ↑/↓ to move through the list, `Enter` to open
- **Star/pin**: click the ★ on any card (or inside the transcript view) to pin a conversation;
  the header button filters to starred-only
- **Bulk select**: ☑ Select mode with big checkbox targets on every row — click a row to
  toggle it, `Shift`-click to select a range, `Ctrl`/`Cmd`-click any row (even outside select
  mode) to jump straight into it, "Select all shown" / "Clear" for the whole filtered list,
  then hide or trash-delete the selection in one shot
- **Activity heatmap**: a 35-day bar chart of conversation activity above the list, respecting
  active filters
- **Recently viewed**: 🕘 Recent dropdown remembers the last 8 conversations you opened
- **Copy to clipboard**: copy the open conversation as Markdown without downloading a file
- **Reveal in Finder**: jump straight to a conversation's working folder from the transcript view
- **Transcript view** with its own in-conversation search (`Cmd+F` inside the panel,
  `Enter`/`⇧Enter` to jump between matches)
- **Delete**: two-click confirm. File-based sources move the session file/folder to the
  macOS Trash — recoverable, never a hard delete. OpenCode and Hermes DB-only sessions
  delete via their own CLI (`opencode session delete`, `hermes sessions delete`)
- **Hide** (Cursor IDE only): removes a conversation from the app without touching the
  original — Cursor IDE's chat storage has no safe delete path, so hide is the only option
  there; the 🙈 header button shows/unhides them
- **Export**: download any conversation as a Markdown file
- **Light/dark theme**: follows the OS, 🌓 button to override (persisted)
- `⟳ Reindex` picks up new sessions without restarting; `/` focuses search; `Esc` closes
- **Auto-reindex**: rescans sources every 5 minutes while running (`ACB_REINDEX_MIN` env
  var to change, `0` to disable); the list refreshes itself when idle
- **Full content indexed**: entire transcripts are searchable (per-conversation safety
  bound of 2MB of text, `ACB_BODY_CAP` to change)

## Security

- Binds to `127.0.0.1` only — never reachable from the network
- Rejects requests with a non-localhost `Host` header (DNS-rebinding protection)
- The three app-owned databases (OpenCode, Hermes, Cursor IDE) are opened strictly
  read-only (`mode=ro`) for indexing/search — this tool never writes to them directly.
  Deleting an OpenCode or Hermes DB-only conversation instead shells out to that app's
  own CLI (`opencode session delete` / `hermes sessions delete`), so writes only ever
  happen through the owning app's own code, never through a raw connection of ours
- No external assets, no CDN, no telemetry — everything stays on your machine

## Files

- `app.py` — scanners for all sources, incremental indexer, HTTP API, delete/hide logic
- `index.html` — the whole UI (vanilla JS, self-contained)
- `index.db` — generated search index (gitignored; safe to delete, rebuilt on next run)

## Notes

- macOS-specific bits: Trash integration and the Cursor IDE path. Everything else is
  portable; Linux support would mainly mean adjusting source paths.
- The first Finder-based delete may trigger a one-time macOS Automation permission prompt.
  If denied, files are moved to `~/.Trash` directly (works, but no "Put Back" in Finder).
- Resumed Codex/Hermes sessions produce multiple files per logical session; each file is
  listed separately so nothing is hidden from search.
- Cursor IDE content search reuses Cursor's own `conversation-search.db` index; transcripts
  are read lazily from `state.vscdb` with range queries (never a full scan of the 2GB db).

## License

MIT

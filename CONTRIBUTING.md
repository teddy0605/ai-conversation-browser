# Contributing

PRs are welcome. Keep changes small and focused.

## Run

```bash
python3 app.py
```

This indexes your local AI tool history and opens http://localhost:8377.
Use `--no-browser`, `--port` or `--scan all` while you work on a scanner.

## Test

```bash
python3 -m unittest discover tests
```

Tests create temp dirs and mock subprocess calls. A new test must not read real data
under `~` or touch the real `index.db`.

## Rules

- Stdlib only. Do not add dependencies to `app.py` or `index.html`.
- Match the existing style.
- If you change a parser, say so in the PR. Old sessions need `python3 app.py --full`.

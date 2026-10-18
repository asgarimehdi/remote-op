# Remote-Op

Tkinter desktop app to manage the my-opencode GitHub Actions fleets —
rebuilt on the **RemoteHermes** pattern: pure-stdlib REST (no `gh` CLI
needed for API calls), a checkbox fleet tree grouped by repo, one
execution model (**Run checked / Stop checked**), an operation
indicator with a serialized op lock, parallel + progressive loading,
and the same light theme.

## Features

- **Repos** — any number of fleet repos (label + PAT + repo + branch +
  OpenCode server password) in the Repos dialog. A repo without a PAT
  falls back to your `gh auth login` token. Settings are saved to
  `settings.json`; an old-format single-repo `settings.json` is
  migrated automatically on first load.
- **Fleet tree** — one row per machine: workflow file, latest run,
  start time, **remaining time** out of the workflow's own
  `timeout-minutes` (under 2h left is highlighted red), and the
  OpenCode server state (● UP + uptime / ● DOWN, re-checked every
  5 minutes against `http://<machine>:4096`). Double-click a machine
  to open its web UI. Repo group rows tick/untick the whole repo.
- **Run / Stop** — tick machines and use Run checked / Stop checked
  (across repos), or Run all / Stop all for one repo (the selected
  row's repo, else the target repo). Machines already running are
  skipped on dispatch; dispatch and cancel run in parallel.
- **Add instance…** — creates one new instance in the selected repo,
  cloned from its first (lowest-numbered) machine; numbering fills
  gaps. With no machine left, the template is recovered from git
  history (the most recently deleted workflow), as before.
- **Create N from first…** — give a number (e.g. **10**) and that many
  new instances are cloned from the repo's first machine in **one
  commit**. The dialog previews the names that will be created;
  numbers that only have a leftover state dir are skipped too, so a
  deleted machine's data never silently revives.
- **Delete checked** — tick any machines (across repos) and delete
  them all: each machine's workflow file plus ALL of its data under
  `opencode/instances/<name>/`, in **one commit per repo**. The
  shared `opencode/config` is never touched. Active runs are
  cancelled first and awaited (bounded wait); a machine whose run
  will not confirm stopped is skipped, untouched.
- **Rename prefix…** — for forks sharing one Tailscale network:
  renames the prefix in one repo (workflow files, names, `WF_NAME`)
  and moves every state dir, all in one commit. h-dashboard branches
  and old Tailscale entries are NOT renamed. Active runs are
  cancelled first.
- **Connect** — opens a terminal running `opencode --server
  http://<machine>:4096` with that repo's password exported as
  `OPENCODE_SERVER_PASSWORD` (the variable the OpenCode V2 client
  reads; `OPENCODE_PASSWORD` is set alongside for legacy tooling).
  Saving the Repos dialog also persists the *target* repo's password
  with `setx` on Windows, as the old app did.
- **Delete old runs** — clears a repo's completed Actions runs from
  GitHub history (up to 500); machines and state are untouched.
- **Hide cancelled** — drops machines whose latest run ended
  cancelled from the tree.

New instances start with a clean state and create
`opencode/instances/<name>/` on their first sync.

## Requirements

- Python 3.8+
- A PAT per repo, **or** the [GitHub CLI](https://cli.github.com/)
  installed + logged in (`gh auth login`) as the fallback token
- `opencode` in `PATH` (only for Connect)

## Setup

```sh
python app.py
# then open Repos… and add your fleets
```

Or run the prebuilt Windows exe: `dist/RemoteOp.exe`. The exe reads
and writes `settings.json` next to itself.

## Build the exe

```sh
pip install pyinstaller
pyinstaller --onefile --windowed --name RemoteOp app.py
```

Close any running `RemoteOp.exe` before rebuilding (Windows locks it).

## Tests

```sh
python unit_test.py
```

Headless tests for the pure helpers, the settings migration, and the
bulk create / bulk delete / rename fleet operations (against an
in-memory fake repo — including the one-commit guarantee and the
skip-a-machine-whose-run-will-not-stop rule).

## Notes

- `settings.json` holds PATs and passwords and is git-ignored — never
  committed.
- Instance identity inside a workflow file is exactly three spots —
  the workflow `name:`, the `WF_NAME:` env, and the sync-back commit
  message (`from <name> instance`) — and cloning rewrites only those.

# Remote-Op

Minimal Tkinter desktop app to manage GitHub Actions workflows and watch
`opencode` server uptime. No console flash — all `gh` calls run hidden.

## Features

- **Settings** — repo, password, branch. Saved to `settings.json` on Save.
  Save also sets `$env:OPENCODE_SERVER_PASSWORD` (the variable the
  OpenCode V2 client reads; `$env:OPENCODE_PASSWORD` is set alongside it
  for legacy tooling) for the app session and persists both with `setx`
  so new terminals have them.
- **Workflows** — loaded from the remote repo (`gh workflow list`).
  Run one or all (`gh workflow run <name> --repo <repo> --ref <branch>`).
  A workflow that already has an active run on the branch is skipped, and
  only one trigger batch runs at a time.
- **Add instance** — creates the next free instance in the repo's
  current prefix: copies the lowest-numbered instance's workflow file,
  renames it (workflow name, `WF_NAME`, sync commit message) and commits
  it via the API as `<prefix><N>.yml`. Gaps are filled first (with
  open1/open2/open4 present, Add creates open3). The new instance starts
  with a clean state and creates `opencode/instances/<prefix><N>/` on
  its first sync. When no instance is left at all, Add recovers the most
  recently deleted workflow from git history as its template, so the
  fleet can always be rebuilt from inside the app. Add / Delete /
  Rename run one at a time (a click while one is running is logged and
  ignored) — parallel deletes used to race each other's commits.
- **Delete selected** — deletes an instance completely: its workflow
  file plus ALL of its data under `opencode/instances/openN/` (one
  commit). The shared `opencode/config` is never touched. If the
  instance has an active run it is cancelled first — its final sync
  would otherwise resurrect the deleted data.
- **Runs** — refresh list, cancel selected / cancel ALL, delete ALL.
- **Uptime** — one row per loaded workflow (`<prefix>N` → `http://<prefix>N:4096`),
  UP/DOWN + uptime counter, auto-checked every 5 minutes. URLs are clickable.
- **Rename prefix…** — for forks that share one Tailscale network with
  the original repo: renames the instance prefix in the configured repo
  (workflow files, names and `WF_NAME`, e.g. `open1` → `fork1`) and moves
  each instance's data `opencode/instances/openN/` → `forkN/` in one
  commit, so both repos can run side by side without hostname or
  h-dashboard-branch collisions. Active runs are cancelled first.
  h-dashboard branches are NOT renamed (new ones are created on the
  next runs; the old ones stay as leftovers).
  Each server has a **Connect** button that opens a terminal and runs
  `opencode --server <url>` with the password set.

## Requirements

- Python 3.8+
- [GitHub CLI](https://cli.github.com/) installed + logged in (`gh auth login`)
- `opencode` in `PATH` (only for Connect)

## Setup

```sh
cp settings.example.json settings.json
# then edit settings.json or fill the fields in the app and press Save
python app.py
```

Or run the prebuilt Windows exe: `dist/RemoteOp.exe`. The exe reads and
writes `settings.json` next to itself.

## Build the exe

```sh
pip install pyinstaller
pyinstaller --onefile --windowed --name RemoteOp app.py
```

Close any running `RemoteOp.exe` before rebuilding (Windows locks it).

## Notes

- `settings.json` holds your password and is git-ignored — never committed.
- `gh` commands use your `gh auth login` session, not the password field.

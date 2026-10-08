# Remote-Op

Minimal Tkinter desktop app to manage GitHub Actions workflows and watch
`opencode` server uptime. No console flash — all `gh` calls run hidden.

## Features

- **Settings** — repo, password, branch. Saved to `settings.json` on Save.
  Save also sets `$env:OPENCODE_PASSWORD` for the app session and persists
  it with `setx` so new terminals have it.
- **Workflows** — loaded from the remote repo (`gh workflow list`).
  Run one or all (`gh workflow run <name> --repo <repo> --ref <branch>`).
  A workflow that already has an active run on the branch is skipped, and
  only one trigger batch runs at a time.
- **Runs** — refresh list, cancel selected / cancel ALL, delete ALL.
- **Uptime** — `http://open1:4096` … `http://open4:4096` UP/DOWN + uptime
  counter, auto-checked every 5 minutes. URLs are clickable.
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

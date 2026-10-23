#!/usr/bin/env python3
"""
Remote-Op — multi-repo manager for the my-opencode runner fleets.

Rebuilt on the RemoteHermes pattern (2026-10-18): pure stdlib (urllib)
against the GitHub REST API — no `gh` CLI needed for the API calls —
a checkbox fleet tree grouped by repo, one execution model (Run
checked / Stop checked), an operation indicator with a serialized op
lock, parallel + progressive loading, and the RemoteHermes light theme.

Each configured repo holds one workflow file per OpenCode instance
(op1/open2, rey1..rey11 style): state lives per instance under
opencode/instances/<wf>/, the shared opencode/config is NEVER touched
by this app, and instances are told apart only by their WF_NAME (the
WF_NAME inside the file is authoritative — file names have varied,
e.g. op1.yml hosts open1).

Fleet operations added on the Hermes basis:

  * Delete checked — tick any machines (across repos) and delete them
    all: active runs are cancelled and awaited first, then, per repo,
    the workflow files + every instance's state go in ONE commit.
  * Create N from first machine — give a number (e.g. 10) and that
    many new instances are cloned from the repo's first (lowest-
    numbered) machine, gap-filling the numbering, in ONE commit.
    With no machine left, the template is recovered from git history,
    as before; in a brand-new raw repo (no machine ever, no history)
    the first machines are bootstrapped from the canonical workflow
    template embedded in the app, rendered for that repo.

Each repo may carry its own PAT; a repo without one falls back to the
token of the locally logged-in gh CLI account. The per-repo OpenCode
server password is used by Connect (exported as
OPENCODE_SERVER_PASSWORD, with the legacy OPENCODE_PASSWORD alongside)
and persisted with setx on Windows when the repos are saved.

Settings live in settings.json next to the program — beside the .exe
in a PyInstaller build, beside this file when run as a script (keep
it private: it holds the PATs and passwords). An old-format
settings.json (single repo/password/branch) is migrated on load.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from tkinter import messagebox, scrolledtext, simpledialog, ttk

RUN_BUDGET_MIN = 340  # fallback; the real value is parsed per workflow file


def _app_dir():
    """Folder the settings file lives in: next to the program.

    When frozen (PyInstaller), __file__ points inside the bundle — in
    a --onefile build that is the _MEIPASS temp extraction dir, which
    PyInstaller deletes when the app exits, so a settings.json written
    there silently vanishes and never appears next to the exe. Use the
    exe's own folder instead. When run as a plain script, keep the
    original rule: this file's folder.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

NAME_RE = re.compile(r"^(.*?)(\d+)$")
PREFIX_RE = re.compile(r"^[a-z]([a-z0-9-]*[a-z])?$")
TIMEOUT_RE = re.compile(r"timeout-minutes:\s*(\d+)")
ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def parse_instance_name(name):
    """'open3' -> ('open', 3); 'mehdi12' -> ('mehdi', 12); else None.

    A valid prefix starts and ends with a letter, so the trailing
    digits are unambiguously the instance number.
    """
    m = NAME_RE.match(name or "")
    if not m or not PREFIX_RE.match(m.group(1)):
        return None
    return m.group(1), int(m.group(2))


def valid_prefix(prefix):
    return bool(PREFIX_RE.match(prefix or ""))


def next_free_number(used):
    """Smallest positive integer not in `used` (fills gaps, e.g. after a delete)."""
    n = 1
    while n in used:
        n += 1
    return n


def plan_new_numbers(used, count):
    """The `count` instance numbers a bulk create will take, in order.

    Gap-filling, exactly like repeated single Adds: every number is
    the smallest free one at its turn, so [used={2,3}, count=3]
    plans [1, 4, 5].
    """
    taken = set(used)
    out = []
    for _ in range(max(0, count)):
        n = next_free_number(taken)
        taken.add(n)
        out.append(n)
    return out


def workflow_instance(text):
    """The WF_NAME value inside a workflow file's text, or None."""
    m = re.search(r"WF_NAME:\s*(\S+)", text)
    return m.group(1) if m else None


def renamed_workflow(text, new_name):
    """An existing workflow file re-pointed at `new_name`.

    The old instance name is read from the file's own WF_NAME, so any
    prefix works. Only three spots carry the identity: the workflow
    `name:`, the WF_NAME job env, and the sync-back commit message.
    Raises ValueError if a marker is missing.
    """
    old_name = workflow_instance(text)
    if not old_name:
        raise ValueError("template has no WF_NAME")
    old = re.escape(old_name)
    out, n1 = re.subn(rf"(?m)^name:\s*{old}\s*$", f"name: {new_name}", text, count=1)
    out, n2 = re.subn(rf"WF_NAME:\s*{old}", f"WF_NAME: {new_name}", out, count=1)
    out, n3 = re.subn(rf"from {old} instance", f"from {new_name} instance", out, count=1)
    if not (n1 and n2 and n3):
        raise ValueError("template is missing name:/WF_NAME:/commit-message markers")
    return out



# ---------------------------------------------------------------------------
# Canonical workflow template (embedded)
# ---------------------------------------------------------------------------
# The current live my-opencode workflow (taken from haileen5's syd1 on
# 2026-10-20, byte-identical to Shabakebehdasht's rey1 except the
# identity spots) with the account-specific spots as placeholders:
# {{WF_NAME}} the instance name, {{OWNER}} the repo owner (git identity
# + the owner's own h-dashboard repo), {{REPO}} the full repo slug
# (sync-back remote). A brand-new raw repo — no machine, no git
# history — is bootstrapped from this; every other create clones a
# real machine instead, so a fleet's own drift always wins over this
# snapshot. Keep it in step with the live fleets when their workflow
# changes materially.
TEMPLATE_WORKFLOW = r'''name: {{WF_NAME}}
on:
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 340
    env:
      REPO_ORIGIN: https://github.com/{{OWNER}}/h-dashboard.git
      WF_NAME: {{WF_NAME}}
      OPENCODE_MODELS: ${{ vars.OPENCODE_DEFAULT_MODELS }}

    steps:
      - name: 1. Checkout Repository
        uses: actions/checkout@v6
        with:
          persist-credentials: false

      - name: 2. Setup OpenSSH Server & Set Runner Password
        run: |
          sudo apt-get update
          sudo apt-get install -y openssh-server
          echo "runner:${{ secrets.RUNNER_PASSWORD }}" | sudo chpasswd
          sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
          sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
          sudo systemctl restart ssh
          sudo systemctl enable ssh

      - name: 3. Setup npm, PATH, and Environment Variables Persistently
        env:
          GH_TOKEN: ${{ secrets.MY_PAT }}
        run: |
          mkdir -p ~/.npm-global
          npm config set prefix '~/.npm-global'

          echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.bashrc
          echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> ~/.profile
          export PATH="$HOME/.npm-global/bin:$PATH"

          echo "export GH_TOKEN=$GH_TOKEN" >> ~/.bashrc
          echo "export GH_TOKEN=$GH_TOKEN" >> ~/.profile
          echo "GH_TOKEN=$GH_TOKEN" | sudo tee -a /etc/environment

          echo 'export INITIAL_PASSWORD="${{ secrets.RUNNER_PASSWORD }}"' >> ~/.bashrc
          echo 'export INITIAL_PASSWORD="${{ secrets.RUNNER_PASSWORD }}"' >> ~/.profile
          echo 'INITIAL_PASSWORD="${{ secrets.RUNNER_PASSWORD }}"' | sudo tee -a /etc/environment

          # CodeGraph CLI — installed here, the first step where npm is on PATH.
          # It indexes h-dashboard in step 10b; the /usr/local/bin symlink
          # keeps the plain `codegraph` command available in every shell.
          npm install -g @colbymchenry/codegraph
          if [ -x ~/.npm-global/bin/codegraph ]; then
            sudo ln -sf ~/.npm-global/bin/codegraph /usr/local/bin/codegraph
            echo "✅ CodeGraph installed: $(/usr/local/bin/codegraph --version)"
          else
            echo '⚠️ CodeGraph binary not found after installation'
          fi

      - name: 4. Install PHP 8.5 & Composer (Laravel + pgsql + redis)
        run: |
          sudo apt-get update
          sudo apt-get install -y lsb-release ca-certificates curl
          # PHP now comes straight from packages.sury.org (official install
          # per its README): the Launchpad mirror (ppa:ondrej/php) answered
          # 503 for over an hour on 2026-10-16 and killed every run in this
          # step, and the PPA itself announces it is merging into sury.org.
          # The keyring package installs the repo signing key.
          sudo curl -sSLo /tmp/debsuryorg-archive-keyring.deb https://packages.sury.org/debsuryorg-archive-keyring.deb
          sudo dpkg -i /tmp/debsuryorg-archive-keyring.deb
          sudo sh -c 'echo "deb [signed-by=/usr/share/keyrings/debsuryorg-archive-keyring.gpg] https://packages.sury.org/php/ $(lsb_release -sc) main" > /etc/apt/sources.list.d/php.list'
          php_ok=0
          for attempt in 1 2 3; do
            if sudo apt-get update && sudo apt-get install -y \
              php8.5 php8.5-cli php8.5-common php8.5-fpm \
              php8.5-mbstring php8.5-xml php8.5-pgsql php8.5-curl php8.5-zip php8.5-bcmath php8.5-intl php8.5-gd \
              php8.5-redis php8.5-tokenizer php8.5-fileinfo php8.5-ctype \
              unzip; then
              php_ok=1
              break
            fi
            echo "⚠️ PHP install attempt $attempt failed — retrying in 20s"
            sleep 20
          done
          if [ "$php_ok" != 1 ]; then
            echo '❌ PHP install failed after 3 attempts (packages.sury.org unreachable)'
            exit 1
          fi
          php -r "copy('https://getcomposer.org/installer', 'composer-setup.php');"
          sudo php composer-setup.php --install-dir=/usr/local/bin --filename=composer
          php -r "unlink('composer-setup.php');"

      - name: 5. Connect Tailscale as Exit Node & Show Access Info
        env:
          TAILSCALE_AUTHKEY: ${{ secrets.TAILSCALE_AUTHKEY }}
        run: |
          curl -fsSL https://tailscale.com/install.sh | sh
          sudo tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname="${{ env.WF_NAME }}" --advertise-exit-node --accept-routes --accept-dns

          # Enable IP forwarding (required for exit node to route traffic)
          echo 'net.ipv4.ip_forward = 1' | sudo tee -a /etc/sysctl.conf
          echo 'net.ipv6.conf.all.forwarding = 1' | sudo tee -a /etc/sysctl.conf
          sudo sysctl -p /etc/sysctl.conf

          TS_IP=$(sudo tailscale ip -4)
          echo "=================================================="
          echo "  >>> SSH & WEB SERVER ARE LIVE! <<<"
          echo "  Tailscale IP  : $TS_IP"
          echo "  Username      : runner"
          echo "  Password      : ${{ secrets.RUNNER_PASSWORD }}"
          echo "  ------------------------------------------------"
          echo "  💻 SSH: ssh runner@$TS_IP"
          echo "  🌐 Web : http://$TS_IP:8000  (Laravel)"
          echo "  🔀 9Router: http://$TS_IP:20128  (dashboard)"
          echo "  🤖 OpenCode: http://$TS_IP:4096  (user: opencode, pass: RUNNER_PASSWORD)"
          echo "  💾 Project: /home/runner/h-dashboard"
          echo "  🌍 Exit Node: Enabled (advertise-exit-node)"
          echo "=================================================="

      - name: 6. Authenticate gh CLI & configure git credential helper
        env:
          GH_TOKEN: ${{ secrets.MY_PAT }}
        run: |
          unset GH_TOKEN
          echo "${{ secrets.MY_PAT }}" | gh auth login --with-token
          gh auth setup-git
          gh auth status
          echo "✅ GitHub CLI authenticated as: $(gh api user -q .login)"

      - name: 7. Clone / sync h-dashboard and create branch
        env:
          GH_TOKEN: ${{ secrets.MY_PAT }}
        run: |
          cd /home/runner
          if [ ! -d h-dashboard ]; then
            git clone "$REPO_ORIGIN"
          fi
          cd h-dashboard

          # Set git identity for commits
          git config user.name "{{OWNER}}"
          git config user.email "{{OWNER}}@users.noreply.github.com"

          # Fetch latest origin/beta
          git fetch origin beta

          # Create / reset WF_NAME branch from origin/beta
          git checkout -B "${WF_NAME}" origin/beta
          echo "✅ Branch ${WF_NAME} created/reset from origin/beta"

          cp .env.example.pgsql .env

          # .hermes.md is not wanted — AGENTS.md is the authoritative instruction
          # file. Drop it from the checkout and from the index (without touching
          # beta), then exclude it so `git status` stays clean.
          if git ls-files --error-unmatch .hermes.md >/dev/null 2>&1; then
            git rm -q --cached .hermes.md
          fi
          rm -f .hermes.md
          grep -qxF '.hermes.md' .git/info/exclude 2>/dev/null \
            || echo '.hermes.md' >> .git/info/exclude
          echo '✅ .hermes.md removed from the checkout and the index'

      - name: 8. Start Docker Compose (PostgreSQL)
        run: |
          cd /home/runner/h-dashboard
          docker compose -f docker-compose-pgsql-.yml up -d
          echo '✅ Docker Compose started'

      - name: 9. Composer, NPM, Optimize, Migrate & Seed
        run: |
          cd /home/runner/h-dashboard
          composer update --no-interaction
          npm update
          # APP_KEY: step 7 re-copies the tracked .env template over .env on every
          # run, and that template ships APP_KEY blank since #953 — without a key
          # the served app throws a 500 on every web request. Generate one for
          # this run when .env has none (artisan setup passes without it, which
          # is why setup looked healthy while the pages failed).
          if ! grep -q '^APP_KEY=base64:' .env; then
            php artisan key:generate --force
            echo '🔑 APP_KEY generated for this run'
          fi
          php artisan optimize:clear
          php artisan optimize
          php artisan migrate:fresh --seed --force
          echo '✅ Database migrated and seeded'

      - name: 10. Build frontend assets
        run: |
          cd /home/runner/h-dashboard
          if ! npm run build; then
            # npm's optional-dependencies bug (npm/cli#4828): after the
            # `npm update` in step 9, the platform binding
            # @rolldown/binding-linux-x64-gnu can be missing from
            # node_modules and vite dies with "Cannot find native
            # binding" (run 37746307765 — the identical build had passed
            # 20 minutes earlier; the bug strikes nondeterministically).
            # npm's own prescribed remedy is a clean reinstall without
            # the stale lockfile; do exactly that, once, and rebuild.
            echo '⚠️ Build failed — clean npm reinstall (stale-lock optional-deps bug), then rebuild'
            rm -rf node_modules package-lock.json
            npm install
            npm run build
          fi
          echo '✅ Frontend assets built'

      - name: 10b. Index h-dashboard with CodeGraph
        run: |
          cd /home/runner/h-dashboard
          if [ -x /usr/local/bin/codegraph ]; then
            CODEGRAPH_TELEMETRY=0 codegraph init .
            CODEGRAPH_TELEMETRY=0 codegraph status .
            echo '✅ CodeGraph index built for h-dashboard'
          else
            echo '⚠️ CodeGraph not on PATH — skipping index'
          fi

      - name: 11. Install 9router globally and create symlink
        run: |
          npm install -g 9router
          if [ -f ~/.npm-global/bin/9router ]; then
            sudo ln -sf ~/.npm-global/bin/9router /usr/local/bin/9router
            echo '✅ 9router installed and symlinked to /usr/local/bin/9router'
          else
            echo '⚠️ 9router binary not found after installation'
          fi

      - name: 12. Restore 9router db backup & start hidden (tray)
        run: |
          export INITIAL_PASSWORD="${{ secrets.RUNNER_PASSWORD }}"
          # Make sure the data dir exists
          mkdir -p ~/.9router/db
          BACKUP_DIR="${GITHUB_WORKSPACE:-.}/opencode/instances/${WF_NAME}"
          # Restore THIS instance's backed-up 9router state. State is
          # per-instance under opencode/instances/<wf>/ (data,
          # cbm-cache, 9router db) so the four workflows never
          # overwrite each other; only opencode/config stays shared.
          if [ -f "$BACKUP_DIR/9router.sqlite" ]; then
            cp "$BACKUP_DIR/9router.sqlite" ~/.9router/db/data.sqlite
            echo '✅ Restored 9router data.sqlite from repo backup'
          else
            echo '⚠️ per-instance 9router.sqlite not found in repo — starting fresh'
          fi
          # Start 9router hidden in the background (tray mode) on the default port 20128.
          # --tray is the same "hide to background" option used interactively.
          nohup 9router --tray --skip-update -p 20128 > /tmp/9router.log 2>&1 &
          sleep 6
          if curl -fsS http://localhost:20128 > /dev/null 2>&1; then
            echo '✅ 9router is live on http://localhost:20128'
          else
            echo '⚠️ 9router did not come up — check /tmp/9router.log'
          fi


      - name: 13. Install OpenCode V2, restore state, configure MCPs + Superpowers plugin & start serve
        env:
          OPENCODE_SERVER_PASSWORD: ${{ secrets.RUNNER_PASSWORD }}
          OPENCODE_DISABLE_AUTOUPDATE: "true"
          GH_TOKEN: ${{ secrets.MY_PAT }}
        run: |
          # 1. Install OpenCode V2 — npm package @opencode/cli (the v1
          #    package opencode-ai stays on the 1.x line forever). Same
          #    npm-global pattern as CodeGraph/9router, plus the
          #    /usr/local/bin symlink. Any v1 install is removed first:
          #    both packages ship an `opencode` binary and the official
          #    migration guide says not to keep them side by side.
          npm uninstall -g opencode-ai >/dev/null 2>&1 || true
          npm install -g @opencode/cli
          if [ -f ~/.npm-global/bin/opencode ]; then
            sudo ln -sf ~/.npm-global/bin/opencode /usr/local/bin/opencode
            echo "✅ OpenCode installed: $(opencode --version)"
          else
            echo '⚠️ opencode binary not found after installation'
          fi

          # 1b. Install codebase-memory-mcp (single static binary from its
          #     GitHub releases): the fifth MCP server, ensured in §4. Its
          #     own OpenCode extras (skill, agents, plugin, instructions)
          #     are installed in §4b, and its index cache
          #     (~/.cache/codebase-memory-mcp) is restored in §2 and synced
          #     back to the repo in step 16.
          #     The binary MUST live in a user-owned dir
          #     ($HOME/.local/bin, no sudo): the `install -y` in §4b
          #     runs an activation transaction that refuses to
          #     proceed when the binary's install dir is root-owned
          #     ("install_dir_not_owned_by_you") — with the old
          #     root-owned /usr/local/bin copy, §4b silently wrote
          #     NOTHING for the whole lifetime of these workflows.
          mkdir -p "$HOME/.local/bin"
          if [ ! -x "$HOME/.local/bin/codebase-memory-mcp" ]; then
            CBM_TMP="$(mktemp -d)"
            curl -fsSL -o "$CBM_TMP/cbm.tar.gz" https://github.com/DeusData/codebase-memory-mcp/releases/latest/download/codebase-memory-mcp-linux-amd64.tar.gz
            tar --no-same-owner -xzf "$CBM_TMP/cbm.tar.gz" -C "$CBM_TMP"
            install -m 0755 "$CBM_TMP/codebase-memory-mcp" "$HOME/.local/bin/codebase-memory-mcp"
            rm -rf "$CBM_TMP"
            echo "✅ codebase-memory-mcp installed: $("$HOME/.local/bin/codebase-memory-mcp" --version)"
          fi

          # 2. Restore the OpenCode backup from the repo (opencode/ dir,
          #    synced back in step 16). config/ is SHARED by all four
          #    instances (opencode.json, plugins, agents and skills —
          #    skills stay shared on purpose), while data and the
          #    codebase-memory index cache are PER-INSTANCE under
          #    instances/<wf>/, so instances never clobber each other.
          OC="${GITHUB_WORKSPACE:-.}/opencode"
          OCI="$OC/instances/${WF_NAME}"
          if [ -d "$OC/config" ]; then
            mkdir -p ~/.config/opencode
            cp -r "$OC/config/." ~/.config/opencode/
            echo '✅ Restored OpenCode config from repo backup'
          fi
          if [ -d "$OCI/data" ]; then
            mkdir -p ~/.local/share/opencode
            cp -r "$OCI/data/." ~/.local/share/opencode/
            echo '✅ Restored OpenCode data (sessions/db) from the per-instance backup'
          fi
          if [ -d "$OCI/cbm-cache" ]; then
            mkdir -p ~/.cache/codebase-memory-mcp
            cp -r "$OCI/cbm-cache/." ~/.cache/codebase-memory-mcp/
            echo '✅ Restored codebase-memory-mcp index cache from the per-instance backup'
          fi

          # 3. Prepare the MCP environment. OpenCode does NOT inherit gh's
          #    login: every MCP server is a separate process that reads only
          #    its own env vars. So the GitHub token (same MY_PAT gh uses)
          #    is exported here — opencode serve below inherits it, and
          #    the config in §4 references it as
          #    {env:...}, so NO secret lands in opencode.json (which step 16
          #    backs up to this repo). Persisted to ~/.bashrc as well, for
          #    shells opened later over SSH.
          export GITHUB_PERSONAL_ACCESS_TOKEN="$GH_TOKEN"
          echo "export GITHUB_PERSONAL_ACCESS_TOKEN=${GH_TOKEN}" >> ~/.bashrc
          echo '✅ MCP environment prepared (GitHub token exported for opencode serve)'

          # 3b. The laravel-boost MCP runs `php artisan boost:mcp` inside
          #     h-dashboard; the package used to be composer-required by the
          #     old Hermes step, so ensure it here (no-op if already there).
          cd /home/runner/h-dashboard
          if php artisan list 2>/dev/null | grep -q 'boost:mcp'; then
            echo '✅ Laravel Boost present (boost:mcp available)'
          else
            composer require laravel/boost --no-interaction 2>&1 | tail -3 || true
            php artisan boost:update 2>/dev/null || true
            echo '✅ Laravel Boost installed for the laravel-boost MCP'
          fi

          # 4. Write/merge ~/.config/opencode/opencode.json. OpenCode reads
          #    MCP servers ONLY from its own config. The merge ENSURES the
          #    wanted servers (laravel-boost, github, codegraph, context7,
          #    codebase-memory-mcp), DROPS postgis for good (removed at
          #    Mehdi's request), and
          #    PRESERVES any other server already in the config — servers
          #    added at runtime are backed up by step 16 and must survive
          #    restarts, so the mcp block is merged, never rewritten whole.
          #    It also ensures the Superpowers plugin: V2 reads the
          #    `plugins` array; the V1 `plugin` key is mirrored with the
          #    same spec so a rollback to V1 would keep the plugin too.
          #    oh-my-opencode-slim is ensured in the `plugins` array
          #    ONLY: it is a dual-export plugin, and its own
          #    v2-compatibility doc warns that a legacy `plugin`-key
          #    entry gets invoked twice on v2 hosts. Its per-agent
          #    free models are configured in §4c.
          python3 - <<'PYEOF2'
          import json, os
          path = os.path.expanduser("~/.config/opencode/opencode.json")
          cfg = {}
          if os.path.exists(path):
              try:
                  cfg = json.load(open(path))
              except Exception:
                  cfg = {}
          cfg.setdefault("$schema", "https://opencode.ai/config.json")
          # Autoupdate stays OFF: the runner is ephemeral, so an
          # /update could never persist, and the update banner
          # only adds noise when a release lands mid-run.
          cfg["autoupdate"] = False
          # The default model is PER-INSTANCE and comes from the
          # opencode/default-models.txt file in this repo: one full
          # model id per line (# comments and blank lines ignored).
          # The file wins whenever it has at least one model line, so
          # models are changed by a plain commit to that file. If the
          # file is missing or empty, the OPENCODE_DEFAULT_MODELS
          # repository variable (repo Settings > Secrets and variables
          # > Actions > Variables) applies; if that is empty or unset
          # too, the built-in list below is the last resort. Each
          # workflow takes its own line by number (<prefix>1 -> line 1,
          # <prefix>2 -> line 2, ...). A shorter list wraps around
          # instead of erroring (with 2 lines: instance 3 -> line 1,
          # instance 4 -> line 2).
          _models = []
          try:
              with open(os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "opencode/default-models.txt")) as _mf:
                  _models = [ln.strip() for ln in _mf if ln.strip() and not ln.strip().startswith("#")]
          except OSError:
              _models = []
          if not _models:
              _models = [ln.strip() for ln in os.environ.get("OPENCODE_MODELS", "").splitlines() if ln.strip()]
          if not _models:
              _models = [
                  "opencode/deepseek-v4.1-flash-free",
                  "opencode/glm-5.3-flash-free",
                  "opencode/mimo-v2.6-flash-free",
                  "opencode/beam-free",
              ]
          # The instance number is the trailing digits of WF_NAME,
          # whatever the prefix is (open3, mehdi3, fork12, ...): the
          # model list is indexed by that number.
          _wf = os.environ.get("WF_NAME", "")
          _num = ""
          for _ch in reversed(_wf):
              if _ch.isdigit():
                  _num = _ch + _num
              else:
                  break
          _idx = int(_num) - 1 if _num else 0
          cfg["model"] = _models[_idx % len(_models)]
          mcp = cfg.get("mcp", {})
          mcp.pop("postgis", None)
          mcp.update({
              "laravel-boost": {
                  "type": "local",
                  "command": ["bash", "-c", "cd /home/runner/h-dashboard && exec php artisan boost:mcp"],
                  "enabled": True,
                  "timeout": 30000,
              },
              "github": {
                  "type": "local",
                  "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
                  "environment": {
                      "GITHUB_PERSONAL_ACCESS_TOKEN": "{env:GITHUB_PERSONAL_ACCESS_TOKEN}",
                  },
                  "enabled": True,
                  "timeout": 120000,
              },
              "codegraph": {
                  "type": "local",
                  "command": ["codegraph", "serve", "--mcp"],
                  "enabled": True,
                  "timeout": 30000,
              },
              "context7": {
                  "type": "remote",
                  "url": "https://mcp.context7.com/mcp",
                  "enabled": True,
                  "timeout": 30000,
              },
              "codebase-memory-mcp": {
                  "type": "local",
                  "command": [os.path.expanduser("~/.local/bin/codebase-memory-mcp")],
                  "enabled": True,
                  "timeout": 60000,
              },
          })
          cfg["mcp"] = mcp
          sp_spec = "superpowers@git+https://github.com/obra/superpowers.git"
          for pkey in ("plugins", "plugin"):
              plist = cfg.get(pkey, [])
              if sp_spec not in plist:
                  plist.append(sp_spec)
              cfg[pkey] = plist
          slim_spec = "oh-my-opencode-slim"
          plist = cfg.get("plugins", [])
          if slim_spec not in plist:
              plist.append(slim_spec)
          cfg["plugins"] = plist
          os.makedirs(os.path.dirname(path), exist_ok=True)
          with open(path, "w") as f:
              json.dump(cfg, f, indent=2)
          print("✅ opencode.json ready: default model + mcp (laravel-boost, github, codegraph, context7, codebase-memory-mcp; postgis dropped) + superpowers & oh-my-opencode-slim plugins")
          PYEOF2

          # 4b. codebase-memory-mcp's own OpenCode integration: its skill
          #     (~/.config/opencode/skills/codebase-memory), three read-only
          #     agents, the cbm-augment plugin and AGENTS.md instructions.
          #     Everything lands under ~/.config/opencode, so step 16 syncs
          #     it like the rest of the config. Two installer quirks shape
          #     this loop: (1) it aborts instantly when its binary is
          #     root-owned ("install_dir_not_owned_by_you") — hence the
          #     user-owned copy from §1b; with the old root-owned
          #     /usr/local/bin copy this step silently wrote NOTHING for
          #     the whole lifetime of these workflows, which is why the
          #     extras never reached the backup; (2) its config-write ops
          #     race each other and a random subset fails per run
          #     ("target: does not exist or cannot be inspected" — root
          #     runs mask it, mode bits do not bind root), so --force is
          #     retried until every expected file is actually on disk.
          #     The MCP entry itself is ensured by §4; this loop only
          #     chases the extras.
          CBM_EXTRAS="AGENTS.md skills/codebase-memory/SKILL.md agents/codebase-memory.md agents/codebase-memory-scout.md agents/codebase-memory-auditor.md plugins/cbm-augment.ts"
          missing=1
          for attempt in 1 2 3 4 5; do
            "$HOME/.local/bin/codebase-memory-mcp" install -y --force > /tmp/cbm-install.log 2>&1 || true
            chmod -R u+w ~/.config/opencode 2>/dev/null || true
            missing=0
            for f in $CBM_EXTRAS; do
              [ -f "$HOME/.config/opencode/$f" ] || missing=1
            done
            [ "$missing" = 0 ] && break
            echo "⚠️ codebase-memory-mcp extras incomplete after attempt $attempt — retrying"
          done
          if [ "$missing" = 0 ]; then
            echo '✅ codebase-memory-mcp OpenCode extras installed (skill, 3 agents, plugin, AGENTS.md)'
          else
            echo '⚠️ codebase-memory-mcp extras still incomplete after 5 attempts — see /tmp/cbm-install.log; the MCP server itself is ensured in §4'
          fi

          # 4c. oh-my-opencode-slim: write/merge its config
          #     (~/.config/opencode/oh-my-opencode-slim.json, synced by
          #     step 16 like the rest of the config) with a "free" preset
          #     that puts EVERY agent on a free OpenCode Zen model, plus
          #     free councillors for the Council agent. The merge
          #     preserves any other presets/settings already present, and
          #     "preset" is only defaulted to "free" when unset, so a
          #     preset later picked with /preset is not forced back.
          #     The plugin itself is registered in §4 (plugins array).
          #     2026-10-16 refresh against the live Zen free list:
          #     ling-3.1-flash-free was REMOVED from Zen (the librarian
          #     had to move), deepseek-v4.1-flash-free and
          #     glm-5.3-flash-free are new zero-retention flash models,
          #     and beam-free (1M, no training) is the designer —
          #     Zen is called from the runner, so its country list
          #     does not apply to this setup (Mehdi, 2026-10-16).
          #     2026-10-17: beam-free is Reflection Beam, the
          #     de-stealthed successor of cat-jam-free (deprecated,
          #     gone from the live Zen list); same interface.
          python3 - <<'PYEOF3'
          import json, os
          path = os.path.expanduser("~/.config/opencode/oh-my-opencode-slim.json")
          cfg = {}
          if os.path.exists(path):
              try:
                  cfg = json.load(open(path))
              except Exception:
                  cfg = {}
          free_models = {
              "orchestrator": "opencode/deepseek-v4.1-flash-free",
              "oracle": "opencode/nemotron-3.5-lightning-free",
              "explorer": "opencode/glm-5.3-flash-free",
              "fixer": "opencode/deepseek-v4.1-flash-free",
              "librarian": "opencode/glm-5.3-flash-free",
              "designer": "opencode/beam-free",
              "council": "opencode/nemotron-3.5-lightning-free",
          }
          free_preset = cfg.setdefault("presets", {}).setdefault("free", {})
          for agent, model in free_models.items():
              free_preset.setdefault(agent, {})["model"] = model
          councillors = cfg.setdefault("council", {}).setdefault("presets", {}).setdefault("default", {})
          # Migration for this refresh: the previously managed
          # councillors "mimo"/"spark" are superseded by the names
          # below; pop them so a synced config keeps no stale ones.
          for stale in ("mimo", "spark"):
              councillors.pop(stale, None)
          for cname, cmodel in (("deepseek", "opencode/deepseek-v4.1-flash-free"),
                                ("nemotron", "opencode/nemotron-3.5-lightning-free"),
                                ("glm", "opencode/glm-5.3-flash-free")):
              councillors.setdefault(cname, {})["model"] = cmodel
          cfg["council"].setdefault("default_preset", "default")
          cfg.setdefault("preset", "free")
          with open(path, "w") as f:
              json.dump(cfg, f, indent=2)
          print("✅ oh-my-opencode-slim.json ready: 'free' preset — every agent on a free OpenCode Zen model")
          PYEOF3

          # 5. Start opencode serve in the background on 0.0.0.0:4096 so it
          #    is reachable over Tailscale. HTTP Basic Auth comes from the
          #    OPENCODE_SERVER_PASSWORD step env (= RUNNER_PASSWORD secret);
          #    the username defaults to "opencode".
          cd /home/runner/h-dashboard
          nohup opencode serve --hostname 0.0.0.0 --port 4096 > /tmp/opencode.log 2>&1 &
          sleep 6
          if curl -fsS -u "opencode:${OPENCODE_SERVER_PASSWORD}" http://localhost:4096/api/info > /dev/null 2>&1; then
            echo '✅ opencode serve is live on http://localhost:4096 (basic auth: opencode / RUNNER_PASSWORD)'
          else
            echo '⚠️ opencode serve did not come up — check /tmp/opencode.log'
          fi

      - name: 14. Start Laravel Development Server
        run: |
          cd /home/runner/h-dashboard
          nohup php artisan serve --host=0.0.0.0 > /dev/null 2>&1 &
          echo '✅ Laravel server started on port 8000'

      - name: 15. Server Keep-Alive (5h 40m)
        run: |
          for i in {1..340}; do
            sleep 60
            echo "[$i/340] Server is active (SSH + Laravel :8000 + OpenCode :4096)..."
          done

      - name: 16. Sync opencode state & 9router data back to my-opencode
        if: always()
        env:
          GH_TOKEN: ${{ secrets.MY_PAT }}
        run: |
          cd "${GITHUB_WORKSPACE:-.}"
          git config user.name "{{OWNER}}"
          git config user.email "{{OWNER}}@users.noreply.github.com"
          # Authenticate with PAT for push
          git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/{{REPO}}.git"

          # 1. Copy OpenCode state back to the repo. Layout:
          #    opencode/config mirrors ~/.config/opencode and is SHARED
          #    by all four instances (skills included); everything
          #    instance-specific goes under opencode/instances/<wf>/:
          #    data mirrors ~/.local/share/opencode (sessions,
          #    opencode.db, storage), cbm-cache mirrors
          #    ~/.cache/codebase-memory-mcp, and 9router.sqlite is this
          #    instance's 9router db. Restored in steps 12 and 13.
          ODEST=opencode
          OINST="$ODEST/instances/${WF_NAME}"
          mkdir -p "$ODEST/config" "$OINST/data" "$OINST/cbm-cache"
          [ -d ~/.config/opencode ] && cp -r ~/.config/opencode/. "$ODEST/config/"
          [ -d ~/.local/share/opencode ] && cp -r ~/.local/share/opencode/. "$OINST/data/"
          [ -d ~/.cache/codebase-memory-mcp ] && cp -r ~/.cache/codebase-memory-mcp/. "$OINST/cbm-cache/"
          # Prune what must not be committed:
          #  - data/auth.json holds provider API keys in plaintext — never
          #    back credentials up to git (the free Zen model needs no key).
          #  - data/snapshot (git object store behind session revert) and
          #    data/log are bulky/regenerable; data/bin is downloaded tool
          #    binaries; config/node_modules is plugin deps (reinstalled).
          for junk in "$OINST/data/auth.json" "$OINST/data/snapshot" \
                      "$OINST/data/log" "$OINST/data/bin" \
                      "$ODEST/config/node_modules" \
                      "$OINST/cbm-cache/logs"; do
            if [ -e "$junk" ]; then
              echo "🧹 Excluding from backup: $junk"
              rm -rf "$junk"
            fi
          done
          # Guard — no nested git repos inside the backup: a stray .git
          # would be recorded as a gitlink (mode 160000) with no .gitmodules
          # and the NEXT run's checkout dies with 'No url found for
          # submodule path' (the my-sev/my-reb breakage). Strip any found.
          find "$ODEST" -name .git -type d -prune -exec rm -rf {} + 2>/dev/null || true
          find "$ODEST" -name .git -type f -delete 2>/dev/null || true
          echo '✅ Copied OpenCode state → opencode/ (shared config + per-instance dirs) for backup'

          # 2. Copy this instance's 9router database back to the repo
          if [ -f ~/.9router/db/data.sqlite ]; then
            cp ~/.9router/db/data.sqlite "$OINST/9router.sqlite"
            echo '✅ Copied 9router db → opencode/instances/${WF_NAME}/9router.sqlite for backup'
          fi

          # 3. Commit & push if anything changed. Stage FIRST, then judge
          #    from the staged diff: plain `git diff` is blind to untracked
          #    files, so checking before `git add` misses a brand-new
          #    opencode/ backup entirely (the first op2 run lost its backup
          #    exactly this way — the step reported success in under a
          #    second and pushed nothing).
          git add -A opencode/
          if git diff --cached --quiet; then
            echo 'ℹ️ No changes to commit'
          else
            git commit -m "Sync opencode state & 9router data from {{WF_NAME}} instance"
            # Pull first in case another run pushed before us
            git pull --rebase origin "${{ github.ref_name }}" || true
            git push origin "${{ github.ref_name }}" || echo '⚠️ Push failed — changes saved locally'
            echo '✅ OpenCode & 9router data synced to my-opencode'
          fi


      - name: 17. Tailscale Logout (cleanup on shutdown)
        if: always()
        run: |
          sudo tailscale logout || true
          echo "✅ Tailscale logout executed — hostname freed"
'''


def embedded_template(name, repo):
    """TEMPLATE_WORKFLOW rendered for instance `name` in repo
    `owner/name`: the owner's h-dashboard is the dashboard origin and
    the git identity, exactly as in the live fleets."""
    owner = repo.split("/")[0] if "/" in repo else repo
    return (TEMPLATE_WORKFLOW
            .replace("{{WF_NAME}}", name)
            .replace("{{OWNER}}", owner)
            .replace("{{REPO}}", repo))


def workflow_timeout(text):
    """The workflow's timeout-minutes, or the fleet fallback."""
    m = TIMEOUT_RE.search(text or "")
    return int(m.group(1)) if m else RUN_BUDGET_MIN


def remaining_minutes(run_created_iso, timeout_min, now=None):
    """Minutes of run budget left for an in-progress run."""
    if not run_created_iso:
        return None
    now = now or datetime.now(timezone.utc)
    started = datetime.fromisoformat(run_created_iso.replace("Z", "+00:00"))
    elapsed = (now - started).total_seconds() / 60.0
    return int(timeout_min - elapsed)


def fmt_remaining(mins):
    if mins is None:
        return "—"
    if mins < 0:
        return "overdue"
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}m left" if h else f"{m}m left"


# Ports that together mean "the machine is FULLY up": 9router starts
# in step 12, OpenCode serve in step 13 and Laravel in step 14 (the
# last service to boot), so all three answering means the boot has
# finished. One early port answering does NOT make the machine
# ready — that is exactly the old bug: the row went green as soon as
# the run started, minutes before the machine could be used.
READY_PORTS = (4096, 8000, 20128)


def readiness(all_ok, run_active):
    """A machine's readiness state: "ready" only when EVERY service
    answers; "starting" while its run is active but the stack is not
    fully up yet; otherwise "down"."""
    if all_ok:
        return "ready"
    return "starting" if run_active else "down"


def fmt_server(state, since, now=None):
    """Server-column text for a readiness state (None = never probed
    and no active run)."""
    if state is None:
        return "—"
    if state == "starting":
        return "◐ STARTING"
    if state == "down":
        return "● DOWN"
    if since:
        now = time.time() if now is None else now
        mins = int((now - since) // 60)
        h, m = divmod(mins, 60)
        return f"● UP {h}h{m:02d}m" if h else f"● UP {m}m"
    return "● UP"


def iso_ts(s):
    """Epoch seconds for a GitHub ISO timestamp ('...Z'), or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def ready_since(run, prev, now=None):
    """The moment a ready machine's UP duration counts from: when
    its current run actually started (GitHub's run_started_at,
    falling back to created_at). NOT the moment this app first saw
    the machine ready — stamping the probe time made every
    already-up machine show the same, wrong uptime (identical for
    the whole fleet, counting from when the app happened to look).
    Only when the run carries no timestamps at all does the
    previous/first-observation stamp survive as a fallback."""
    ts = iso_ts((run or {}).get("run_started_at")) \
        or iso_ts((run or {}).get("created_at"))
    if ts is not None:
        return ts
    if prev and prev[0] == "ready" and prev[1]:
        return prev[1]
    return time.time() if now is None else now


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def blank_repo():
    return {"label": "", "pat": "", "repo": "", "branch": "opc",
            "password": ""}


def _label_for(repo_slug):
    """Default tree label for a repo slug: its owner (haileen5/my-opencode
    and Shabakebehdasht/my-opencode meet in the tree by owner)."""
    return repo_slug.split("/")[0] if "/" in repo_slug else repo_slug


def load_settings(path=None):
    """Settings dict; an old single-repo settings.json is migrated.

    Old format: {"repo": ..., "password": ..., "branch": ...} becomes
    one entry in "repos" (label = the repo's owner). A file that
    cannot be parsed yields an empty fleet rather than a crash.
    """
    path = path or SETTINGS_PATH
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    repos = []
    for r in data.get("repos", []) if isinstance(data, dict) else []:
        e = blank_repo()
        e.update({k: r[k] for k in e if k in r})
        if e["repo"] and not e["label"]:
            e["label"] = _label_for(e["repo"])
        repos.append(e)
    if not repos and isinstance(data, dict) and data.get("repo"):
        e = blank_repo()
        e.update({"repo": data["repo"],
                  "password": data.get("password", ""),
                  "branch": data.get("branch") or data.get("ref") or "opc",
                  "label": _label_for(data["repo"])})
        repos.append(e)
    return {"repos": repos,
            "active": data.get("active", "") if isinstance(data, dict) else "",
            "hide_cancelled": bool(data.get("hide_cancelled"))
            if isinstance(data, dict) else False}


def save_settings(settings, path=None):
    with open(path or SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# GitHub REST client (one instance per repo entry)
# ---------------------------------------------------------------------------

_gh_cli_token_cache = {"t": None}


def gh_cli_token():
    """Token of the locally logged-in gh CLI account, or None.

    Used for repos with no PAT of their own. Only a successful lookup
    is cached.
    """
    if _gh_cli_token_cache["t"]:
        return _gh_cli_token_cache["t"]
    exe = shutil.which("gh")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "auth", "token"], capture_output=True,
                           text=True, timeout=15)
    except Exception:
        return None
    tok = (r.stdout or "").strip()
    if r.returncode == 0 and tok:
        _gh_cli_token_cache["t"] = tok
        return tok
    return None


class GH:
    def __init__(self, pat):
        self.pat = (pat or "").strip()

    def _token(self):
        return self.pat or gh_cli_token()

    def api(self, method, path, body=None, timeout=90):
        tok = self._token()
        if not tok:
            return 0, {"message": "No PAT set for this repo and no gh "
                                  "CLI login found — set a PAT in the "
                                  "repo, or run `gh auth login`."}
        url = "https://api.github.com/" + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Remote-Op/2.0",
        }
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"message": raw.decode("utf-8", "replace")[:200]}
        except Exception as e:  # network errors etc.
            return 0, {"message": str(e)}

    # -- repo content ------------------------------------------------------
    def list_instances(self, repo, branch):
        """{wf_name: {file, sha, text, timeout}} for the repo.

        Only workflow files whose WF_NAME is an instance name
        (<prefix><number>) count; the WF_NAME inside the file is
        authoritative, not the file name.
        """
        code, listing = self.api("GET", f"repos/{repo}/contents/.github/workflows?ref={branch}")
        out = {}
        if code != 200 or not isinstance(listing, list):
            return out
        for e in listing:
            fname = e.get("name", "")
            if not fname.endswith(".yml"):
                continue
            code2, f = self.api("GET", f"repos/{repo}/contents/.github/workflows/{fname}?ref={branch}")
            if code2 != 200 or "content" not in f:
                continue
            try:
                text = base64.b64decode(f["content"]).decode("utf-8")
            except Exception:
                continue
            wf = workflow_instance(text)
            if wf and parse_instance_name(wf):
                out[wf] = {"file": fname, "sha": f["sha"], "text": text,
                           "timeout": workflow_timeout(text)}
        return out

    def get_file(self, repo, branch, path):
        code, f = self.api("GET", f"repos/{repo}/contents/{path}?ref={branch}")
        if code != 200 or "content" not in f:
            return None, None
        return base64.b64decode(f["content"]).decode("utf-8"), f["sha"]

    def state_numbers(self, repo, branch):
        """Instance numbers that have a state dir under
        opencode/instances/ — a number whose workflow was hand-deleted
        must not be reused either (its old data would silently revive)."""
        code, listing = self.api("GET", f"repos/{repo}/contents/opencode/instances?ref={branch}")
        nums = set()
        if code == 200 and isinstance(listing, list):
            for e in listing:
                p = parse_instance_name(e.get("name", ""))
                if p:
                    nums.add(p[1])
        return nums

    def history_template(self, repo, branch):
        """When no workflow files are left, recover the most recent
        instance workflow from git history: (instance_name, text) of
        the newest commit that still had one, or None."""
        code, commits = self.api(
            "GET", f"repos/{repo}/commits?sha={branch}&path=.github/workflows&per_page=30")
        if code != 200 or not isinstance(commits, list):
            return None
        for c in commits:
            sha = c.get("sha")
            if not sha:
                continue
            code2, listing = self.api(
                "GET", f"repos/{repo}/contents/.github/workflows?ref={sha}")
            if code2 != 200 or not isinstance(listing, list):
                continue
            for e in listing:
                fname = e.get("name", "")
                if not fname.endswith(".yml"):
                    continue
                text, _ = self.get_file(repo, sha, f".github/workflows/{fname}")
                if not text:
                    continue
                name = workflow_instance(text)
                if name and parse_instance_name(name):
                    return name, text
        return None

    # -- actions -----------------------------------------------------------
    def list_runs(self, repo, per_page=100):
        code, d = self.api("GET", f"repos/{repo}/actions/runs?per_page={per_page}")
        if code != 200 or not isinstance(d, dict):
            return []
        return d.get("workflow_runs", [])

    def list_completed_runs(self, repo, limit=500):
        """Completed runs only (GitHub refuses to delete active ones),
        newest first, paged until `limit`."""
        out, page = [], 1
        while len(out) < limit:
            code, d = self.api(
                "GET", f"repos/{repo}/actions/runs?status=completed"
                       f"&per_page=100&page={page}")
            if code != 200 or not isinstance(d, dict):
                break
            batch = d.get("workflow_runs", [])
            if not batch:
                break
            out.extend(batch)
            page += 1
        return out[:limit]

    def delete_run(self, repo, run_id):
        code, _ = self.api("DELETE", f"repos/{repo}/actions/runs/{run_id}")
        return code == 204

    def dispatch(self, repo, branch, workflow_file):
        code, _ = self.api("POST", f"repos/{repo}/actions/workflows/{workflow_file}/dispatches",
                           {"ref": branch})
        return code == 204

    def cancel_run(self, repo, run_id):
        code, _ = self.api("POST", f"repos/{repo}/actions/runs/{run_id}/cancel")
        return code == 202

    # -- git trees (bulk create / delete / rename in one commit) ----------
    def ref_head(self, repo, branch):
        code, d = self.api("GET", f"repos/{repo}/git/ref/heads/{branch}")
        return d["object"]["sha"] if code == 200 else None

    def head_tree(self, repo, commit_sha):
        code, d = self.api("GET", f"repos/{repo}/git/commits/{commit_sha}")
        return d["tree"]["sha"] if code == 200 else None

    def recursive_tree(self, repo, ref):
        code, d = self.api("GET", f"repos/{repo}/git/trees/{ref}?recursive=1")
        return d.get("tree", []) if code == 200 else []

    def create_tree(self, repo, entries, base_tree=None):
        body = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        code, d = self.api("POST", f"repos/{repo}/git/trees", body)
        return d["sha"] if code in (200, 201) else None

    def create_commit(self, repo, message, tree_sha, parents):
        code, d = self.api("POST", f"repos/{repo}/git/commits",
                           {"message": message, "tree": tree_sha, "parents": parents})
        return d["sha"] if code in (200, 201) else None

    def update_ref(self, repo, branch, sha):
        code, _ = self.api("PATCH", f"repos/{repo}/git/refs/heads/{branch}", {"sha": sha})
        return code == 200

    def create_blob(self, repo, text):
        code, d = self.api("POST", f"repos/{repo}/git/blobs",
                           {"content": base64.b64encode(text.encode("utf-8")).decode(),
                            "encoding": "base64"})
        return d["sha"] if code in (200, 201) else None

    def commit_tree_change(self, repo, branch, message, build_entries, log,
                           attempts=5):
        """Apply a tree change in ONE commit, retrying when the branch
        moves underneath us. build_entries(tree_blobs) returns the
        tree entries to apply on top of the current head (an entry
        with sha=None deletes its path). Returns True when the commit
        landed (or there was nothing to change)."""
        for attempt in range(attempts):
            head = self.ref_head(repo, branch)
            if not head:
                log("could not resolve the branch head")
                return False
            base = self.head_tree(repo, head)
            blobs = [e for e in self.recursive_tree(repo, head)
                     if e.get("type") == "blob"]
            entries = build_entries(blobs)
            if entries is None:
                return False
            if not entries:
                return True
            tree_sha = self.create_tree(repo, entries, base_tree=base)
            if not tree_sha:
                log("creating the tree FAILED")
                return False
            commit = self.create_commit(repo, message, tree_sha, [head])
            if not commit:
                log("creating the commit FAILED")
                return False
            if self.update_ref(repo, branch, commit):
                return True
            log(f"branch moved during the commit (attempt "
                f"{attempt + 1}/{attempts}) — retrying…")
            time.sleep(2)
        log("the change did not land — run it again")
        return False


# ---------------------------------------------------------------------------
# Fleet operations (UI-independent; log is a callable taking a string)
# ---------------------------------------------------------------------------

def active_run_for(gh, repo, branch, workflow_file):
    """The active run dict of one workflow file on `branch`, or None."""
    for r in gh.list_runs(repo):
        if (r.get("path", "").endswith(workflow_file)
                and r.get("status") in ACTIVE_STATUSES
                and r.get("head_branch") == branch):
            return r
    return None


def cancel_and_wait(gh, repo, branch, wf, workflow_file, log):
    """Cancel the machine's active run and wait until it is really
    over — a dying run's final sync writes state back, so file/data
    operations must not race it. Bounded wait (30 x 4s polls, short
    per-poll timeout): an unconfirmed stop aborts THIS machine's
    operation instead of holding the app hostage. True when clear."""
    run = active_run_for(gh, repo, branch, workflow_file)
    if not run:
        return True
    log(f"{wf}: cancelling run {run['id']} and waiting…")
    gh.cancel_run(repo, run["id"])
    for _ in range(30):
        time.sleep(4)
        _, d = gh.api("GET", f"repos/{repo}/actions/runs/{run['id']}",
                      timeout=15)
        if d.get("status") == "completed":
            return True
    log(f"{wf}: run {run['id']} did not confirm stopped — this machine "
        f"is SKIPPED (nothing of it was removed); retry once it ended")
    return False


def create_instances(gh, cfg, count, log, mode="raw", prefix_hint=None):
    """Clone `count` new instances from the repo's FIRST machine.

    The template is the lowest-numbered instance's workflow file (or,
    with no machine left, the most recently deleted one from git
    history; or, in a brand-new raw repo where no machine ever
    existed, the canonical workflow embedded in the app —
    TEMPLATE_WORKFLOW rendered for this repo, with `prefix_hint` as
    the instance prefix, default "op"). Numbering gap-fills over
    workflow numbers ∪ state-dir numbers. All new files land in ONE
    git-tree commit. Returns the created instance names ([] when
    nothing was created).

    mode="copy" also clones the template's backed-up per-instance
    state — every blob under opencode/instances/<template>/ (data/:
    sessions & history, cbm-cache/: the codebase-memory index, and
    9router.sqlite when present) — into each new instance's state
    dir, reusing the same blob SHAs in the same single commit, so the
    new machine boots fully warm instead of raw. (Plugins, skills,
    agents and MCP config need no copying: they live in the SHARED
    opencode/config every instance already uses.) When the template
    has no backed-up state, copy degrades to raw with a log line.
    mode="raw" is the old behaviour: clean state, created by the
    instance's own first sync.
    """
    repo, branch = cfg["repo"], cfg["branch"]
    inst = gh.list_instances(repo, branch)
    embedded = False
    if inst:
        template_name = sorted(
            inst, key=lambda n: parse_instance_name(n)[1])[0]
        template_text = inst[template_name]["text"]
        log(f"template: first machine {template_name} "
            f"({inst[template_name]['file']})")
    else:
        hist = gh.history_template(repo, branch)
        if hist:
            template_name, template_text = hist
            log(f"repo is empty — using the last deleted workflow "
                f"({template_name}) from git history as the template")
        else:
            # Brand-new raw repo: no machine ever existed here, so
            # there is nothing to clone and nothing in history either.
            # Bootstrap from the canonical workflow embedded in the
            # app, rendered for THIS repo (owner/slug), instead of
            # dead-ending. The machines start raw — a new repo has no
            # backed-up state anywhere to copy.
            pfx = prefix_hint if prefix_hint and valid_prefix(
                prefix_hint) else "op"
            template_name = f"{pfx}1"
            template_text = embedded_template(template_name, repo)
            embedded = True
            log(f"repo is brand-new (no machines, none in git history) "
                f"— bootstrapping from the workflow template embedded "
                f"in Remote-Op, prefix '{pfx}' (change it later with "
                f"Rename prefix… if you want another)")
    prefix = parse_instance_name(template_name)[0]
    used = {parse_instance_name(n)[1] for n in inst}
    used |= gh.state_numbers(repo, branch)
    numbers = plan_new_numbers(used, count)
    names = [f"{prefix}{n}" for n in numbers]
    texts = {}
    for name in names:
        try:
            texts[name] = renamed_workflow(template_text, name)
        except ValueError as e:
            log(f"create aborted: {e}")
            return []
    # One blob per new file, created in parallel.
    blob_sha = {}

    def make_blob(name):
        blob_sha[name] = gh.create_blob(repo, texts[name])

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(make_blob, names))
    if any(not s for s in blob_sha.values()):
        log("create aborted: a blob upload FAILED (nothing committed)")
        return []

    # Copy mode: snapshot the template's state entries ONCE (path,
    # mode, blob sha). The commit below re-points those same blobs at
    # each new instance's dir — nothing is re-uploaded, however large
    # the state is.
    src_state = []
    if mode == "copy" and embedded:
        log("copy mode: a brand-new repo has no backed-up state to "
            "copy — creating raw (first sync creates the state)")
    elif mode == "copy":
        head = gh.ref_head(repo, branch)
        if head:
            prefix = f"opencode/instances/{template_name}/"
            src_state = [e for e in gh.recursive_tree(repo, head)
                         if e.get("type") == "blob"
                         and e["path"].startswith(prefix)]
        if src_state:
            log(f"copy mode: cloning {template_name}'s backed-up state "
                f"({len(src_state)} file(s): sessions/history, "
                f"codebase-memory index, 9router db if present) into "
                f"each new machine")
        else:
            log(f"copy mode: {template_name} has NO backed-up state "
                f"under opencode/instances/{template_name}/ — creating "
                f"raw instead (it never synced, or its state is gone)")

    def entries(_blobs):
        out = [{"path": f".github/workflows/{name}.yml", "mode": "100644",
                "type": "blob", "sha": blob_sha[name]} for name in names]
        if src_state:
            src_prefix = f"opencode/instances/{template_name}/"
            for name in names:
                for e in src_state:
                    out.append({
                        "path": f"opencode/instances/{name}/"
                                + e["path"][len(src_prefix):],
                        "mode": e["mode"], "type": "blob",
                        "sha": e["sha"]})
        return out

    msg = (f"Add {names[0]} workflow" if count == 1 else
           f"Add {count} instances from {template_name}: "
           f"{names[0]}..{names[-1]}")
    if src_state:
        msg += f" (state copied from {template_name})"
    if not gh.commit_tree_change(repo, branch, msg, entries, log):
        log("create FAILED — no instances were committed")
        return []
    for name in names:
        if src_state:
            log(f"✅ Created .github/workflows/{name}.yml + a full copy "
                f"of {template_name}'s state in opencode/instances/"
                f"{name}/ ({len(src_state)} file(s)) — {name} boots "
                f"warm, with {template_name}'s history")
        else:
            log(f"✅ Created .github/workflows/{name}.yml — {name} "
                f"starts with a clean state and creates "
                f"opencode/instances/{name}/ on its first sync")
    return names


def delete_instances(gh, cfg, names, log):
    """Delete the given machines of one repo, completely.

    Every machine's active run is cancelled and awaited first; a
    machine whose run will not confirm stopped is skipped untouched.
    The rest go in ONE git-tree commit: their workflow files plus
    ALL blobs under opencode/instances/<name>/ each. The shared
    opencode/config is never touched. Returns (deleted, skipped).
    """
    repo, branch = cfg["repo"], cfg["branch"]
    inst = gh.list_instances(repo, branch)
    cleared = []
    skipped = []
    for name in names:
        info = inst.get(name)
        if info and not cancel_and_wait(gh, repo, branch, name,
                                        info["file"], log):
            skipped.append(name)
        else:
            cleared.append(name)
    if not cleared:
        return [], skipped
    prefixes = tuple(f"opencode/instances/{n}/" for n in cleared)
    files = {inst[n]["file"] for n in cleared if n in inst}

    def entries(blobs):
        out = [{"path": f".github/workflows/{f}", "mode": "100644",
                "type": "blob", "sha": None} for f in sorted(files)]
        out += [{"path": e["path"], "mode": "100644", "type": "blob",
                 "sha": None} for e in blobs
                if e["path"].startswith(prefixes)]
        return out

    msg = (f"Remove {cleared[0]} instance (workflow + data)"
           if len(cleared) == 1 else
           f"Remove {len(cleared)} instances (workflows + data): "
           f"{', '.join(cleared)}")
    if not gh.commit_tree_change(repo, branch, msg, entries, log):
        log("delete FAILED — the commit did not land")
        return [], skipped + cleared
    for name in cleared:
        log(f"✅ {name} deleted (workflow file + "
            f"opencode/instances/{name}/)")
    log("Shared opencode/config untouched.")
    return cleared, skipped


def rename_prefix_op(gh, cfg, old_prefix, new_prefix, log):
    """Rename the instance prefix of one repo in ONE commit: new
    workflow files are added, old ones deleted, and each instance's
    state blobs move opencode/instances/<old>N/ -> <new>N/. Active
    runs are cancelled and awaited first. h-dashboard branches and
    old Tailscale entries are NOT renamed. Returns True on success."""
    repo, branch = cfg["repo"], cfg["branch"]
    inst = gh.list_instances(repo, branch)
    targets = {}
    for name in inst:
        p = parse_instance_name(name)
        if p and p[0] == old_prefix:
            targets[name] = f"{new_prefix}{p[1]}"
    if not targets:
        log(f"no {old_prefix}N instances found in {repo}")
        return False
    for old_name in targets:
        if not cancel_and_wait(gh, repo, branch, old_name,
                               inst[old_name]["file"], log):
            log(f"rename aborted: {old_name} is still running "
                f"(nothing changed)")
            return False
    blob_sha = {}

    def make_blob(item):
        old_name, new_name = item
        try:
            text = renamed_workflow(inst[old_name]["text"], new_name)
        except ValueError:
            blob_sha[old_name] = None
            return
        blob_sha[old_name] = gh.create_blob(repo, text)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(make_blob, sorted(targets.items())))
    if any(not s for s in blob_sha.values()):
        log("rename aborted: a renamed workflow could not be prepared "
            "(nothing changed)")
        return False
    moves = [(f"opencode/instances/{o}/", f"opencode/instances/{n}/")
             for o, n in sorted(targets.items())]

    def entries(blobs):
        out = []
        for old_name, new_name in sorted(targets.items()):
            out.append({"path": f".github/workflows/{new_name}.yml",
                        "mode": "100644", "type": "blob",
                        "sha": blob_sha[old_name]})
            old_file = inst[old_name]["file"]
            if old_file != f"{new_name}.yml":
                out.append({"path": f".github/workflows/{old_file}",
                            "mode": "100644", "type": "blob", "sha": None})
        for e in blobs:
            for old_dir, new_dir in moves:
                if e["path"].startswith(old_dir):
                    out.append({"path": new_dir + e["path"][len(old_dir):],
                                "mode": e["mode"], "type": "blob",
                                "sha": e["sha"]})
                    out.append({"path": e["path"], "mode": "100644",
                                "type": "blob", "sha": None})
                    break
        return out

    if not gh.commit_tree_change(
            repo, branch,
            f"Rename instance prefix {old_prefix} -> {new_prefix}",
            entries, log):
        log("rename FAILED — the commit did not land")
        return False
    log(f"✅ Prefix renamed {old_prefix} -> {new_prefix} "
        f"({len(targets)} instance(s), one commit)")
    return True


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title("Remote-Op — OpenCode fleet manager")
        root.geometry("1360x780")
        self.settings = load_settings()
        self.instances = {}   # (label, wf) -> workflow info, ALL repos
        self.runs = {}        # (label, wf) -> latest run dict
        self.uptime = {}      # (label, wf) -> (readiness state, since ts|None)
        self._loading = set()  # repo labels whose refresh is in flight
        self.checked = set()   # (label, wf) ticked in the fleet tree
        self._op_lock = threading.Lock()
        self._op_desc = None

        # ---- theme ---------------------------------------------------
        # Same light theme as RemoteHermes, on ttk's clam base (pure
        # stdlib): soft gray app background, white cards, one accent
        # for primary actions, Segoe UI text, Consolas for the log.
        BG, PANEL = "#f3f4f6", "#ffffff"
        INK, MUTED, ACCENT = "#111827", "#6b7280", "#2563eb"
        self.BG, self.MUTED = BG, MUTED
        root.configure(bg=BG)
        root.option_add("*Font", ("Segoe UI", 10))
        root.minsize(1080, 620)
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10), background=BG,
                        foreground=INK)
        style.configure("TButton", padding=(10, 5), background="#e8ebef",
                        bordercolor="#d1d5db")
        style.map("TButton",
                  background=[("active", "#d9dee6"),
                              ("disabled", "#eef0f3")])
        style.configure("Accent.TButton", background=ACCENT,
                        foreground="#ffffff", bordercolor=ACCENT)
        style.map("Accent.TButton",
                  background=[("active", "#1d4ed8"),
                              ("disabled", "#93c5fd")],
                  foreground=[("disabled", "#ffffff")])
        style.configure("Danger.TButton", foreground="#b91c1c")
        style.configure("Treeview", background=PANEL,
                        fieldbackground=PANEL, rowheight=26, borderwidth=0)
        style.configure("Treeview.Heading", background="#eef2f7",
                        font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#dbeafe")],
                  foreground=[("selected", INK)])
        style.configure("TCombobox", padding=4)

        def section(parent, text):
            tk.Label(parent, text=text, bg=BG, fg=MUTED,
                     font=("Segoe UI", 9, "bold")).pack(
                         anchor="w", padx=12, pady=(12, 4))

        top = tk.Frame(root, bg=BG)
        top.pack(fill=tk.X, padx=12, pady=(10, 2))
        tk.Label(top, text="Target repo:", bg=BG).pack(side=tk.LEFT)
        self.repo_var = tk.StringVar()
        self.repo_combo = ttk.Combobox(top, textvariable=self.repo_var,
                                       state="readonly", width=22)
        self.repo_combo.pack(side=tk.LEFT, padx=6)
        self.repo_combo.bind("<<ComboboxSelected>>", self._on_repo_switch)
        ttk.Button(top, text="Repos…", command=self._repos_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="↻ Refresh", command=self.refresh).pack(side=tk.LEFT, padx=2)
        self.status_var = tk.StringVar(value="")
        tk.Label(top, textvariable=self.status_var, bg=BG,
                 fg=MUTED).pack(side=tk.LEFT, padx=12)

        # Two panes below the header: fleet list + controls on the
        # LEFT (full height), log on the RIGHT. Draggable divider.
        body = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashwidth=4,
                              bg="#d1d5db", bd=0)
        body.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        left = tk.Frame(body, bg=BG)
        right = tk.Frame(body, bg=BG)
        body.add(left, minsize=470, stretch="always")
        body.add(right, minsize=330, stretch="always")
        root.after(80, lambda: body.sash_place(0, 830, 0))

        section(left, "MACHINES")
        bar = tk.Frame(left, bg=BG)
        bar.pack(fill=tk.X, padx=12, pady=(0, 8))
        ttk.Button(bar, text="▶ Run checked", style="Accent.TButton", command=self.run_checked).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="⏹ Stop checked", style="Danger.TButton", command=self.stop_checked).pack(side=tk.LEFT, padx=2)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(bar, text="▶▶ Run all", command=self.run_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="⏹ Stop all", command=self.cancel_all).pack(side=tk.LEFT, padx=2)

        tree_card = tk.Frame(left, bg=PANEL, highlightbackground="#e5e7eb",
                             highlightthickness=1)
        tree_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        # One tree for the whole fleet: a group row per repo (its
        # label in the tree column), its machines as child rows.
        cols = ("file", "run", "started", "remaining", "server")
        self.tree = ttk.Treeview(tree_card, columns=cols,
                                 show="tree headings", height=14)
        self.tree.heading("#0", text="Repo / Machine")
        self.tree.column("#0", width=135)
        for col, title, w in [("file", "File", 195),
                              ("run", "Run", 95), ("started", "Started", 58),
                              ("remaining", "Remaining", 82),
                              ("server", "Server", 105)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=w)
        self.tree.tag_configure("low", foreground="#c0392b")
        self.tree.tag_configure("ok", foreground="#1e8449")
        self.tree.tag_configure("starting", foreground="#d97706")
        self.tree.tag_configure("down", foreground="#c0392b")
        self.tree.tag_configure("acct", background="#e8eef5",
                                font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f8fafc")
        vsb = ttk.Scrollbar(tree_card, orient=tk.VERTICAL,
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.img_off = self._box_image(0)
        self.img_on = self._box_image(1)
        self.img_some = self._box_image(2)
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<space>", self._on_tree_space, add="+")
        self.tree.bind("<Double-1>", self._on_tree_double, add="+")

        btns = tk.Frame(left, bg=BG)
        btns.pack(fill=tk.X, padx=12, pady=(2, 2))
        self.btn_add = ttk.Button(btns, text="Add instance…", command=self.add_instance)
        self.btn_add.pack(side=tk.LEFT, padx=2)
        self.btn_bulk = ttk.Button(btns, text="Create N from first…", style="Accent.TButton", command=self.create_bulk)
        self.btn_bulk.pack(side=tk.LEFT, padx=2)
        self.btn_del = ttk.Button(btns, text="Delete checked", style="Danger.TButton", command=self.delete_checked)
        self.btn_del.pack(side=tk.LEFT, padx=2)
        ttk.Separator(btns, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        self.select_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Select all", variable=self.select_all_var,
                        command=self._toggle_select_all).pack(side=tk.LEFT, padx=2)
        self.checked_var = tk.StringVar(value="")
        tk.Label(btns, textvariable=self.checked_var, bg=BG,
                 fg=MUTED).pack(side=tk.LEFT, padx=6)

        util = tk.Frame(left, bg=BG)
        util.pack(fill=tk.X, padx=12, pady=2)
        self.btn_rename = ttk.Button(util, text="Rename prefix…", command=self.rename_prefix)
        self.btn_rename.pack(side=tk.LEFT, padx=2)
        ttk.Button(util, text="Connect", command=self.connect_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(util, text="🧹 Delete old runs", command=self.delete_old_runs).pack(side=tk.LEFT, padx=2)
        self.hide_cancelled_var = tk.BooleanVar(
            value=bool(self.settings.get("hide_cancelled")))
        ttk.Checkbutton(util, text="Hide cancelled",
                        variable=self.hide_cancelled_var,
                        command=self._toggle_hide_cancelled).pack(side=tk.LEFT, padx=6)
        self.uptime_var = tk.StringVar(value="server check: —")
        tk.Label(util, textvariable=self.uptime_var, bg=BG,
                 fg=MUTED).pack(side=tk.LEFT, padx=6)

        section(right, "LOG")
        self.op_var = tk.StringVar(value="")
        self.op_label = tk.Label(right, textvariable=self.op_var, anchor="w",
                                 bg="#eef2f7", fg=INK,
                                 font=("Segoe UI", 10, "bold"))
        self.op_label.pack(fill=tk.X, padx=12, pady=(0, 6), ipady=5)
        self.logbox = scrolledtext.ScrolledText(
            right, height=14, state="normal", bg=PANEL,
            font=("Consolas", 9), relief="flat", wrap=tk.WORD,
            highlightbackground="#e5e7eb", highlightthickness=1)
        self.logbox.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
        self.logbox.tag_configure("err", foreground="#b91c1c")
        self.logbox.tag_configure("ok", foreground="#15803d")
        self.logbox.tag_configure("warn", foreground="#b45309")
        self._sync_repo_combo()
        self._apply_active_password()
        if self.settings["repos"]:
            self.refresh()
        else:
            self.log("No repos yet — open Repos… and add one "
                     "(label, repo owner/name, branch; PAT optional — "
                     "empty uses your gh CLI login). An old-format "
                     "settings.json is migrated automatically.")
        self.root.after(3 * 1000, self._uptime_loop)

    # -- plumbing -----------------------------------------------------------
    def log(self, msg):
        self.root.after(0, self._log_now, str(msg))

    def _log_now(self, msg):
        tag = None
        if "ERROR" in msg or "FAILED" in msg or "ABORTED" in msg \
                or " aborted" in msg:
            tag = "err"
        elif msg.startswith("✅"):
            tag = "ok"
        elif msg.startswith("⏳") or "still in progress" in msg \
                or "retrying" in msg:
            tag = "warn"
        self.logbox.insert(tk.END, msg + "\n", tag)
        self.logbox.see(tk.END)

    def bg(self, fn):
        threading.Thread(target=self._guarded, args=(fn,), daemon=True).start()

    def _guarded(self, fn):
        try:
            fn()
        except Exception as e:
            self.log(f"ERROR: {e}")

    def _acquire_op(self, what):
        """Serialize Add/Create/Delete/Rename behind one lock; name
        the holder so a refusal says WHAT is still running instead of
        a bare 'wait'."""
        if not self._op_lock.acquire(blocking=False):
            self.log(f"{self._op_desc or 'another operation'} still in "
                     f"progress — wait for it")
            return False
        self._op_desc = what
        self._op_started(what)
        return True

    def _release_op(self):
        self._op_desc = None
        self._op_lock.release()

    def _op_started(self, what):
        """Persistent operation indicator (above the log): the user
        must be able to SEE that work is running — log lines alone
        scroll past. The mutating buttons lock while an op runs."""
        def ui():
            self.op_var.set(f"⏳ {what} — running…")
            self.op_label.config(fg="#92400e", bg="#fef3c7")
            for b in (self.btn_add, self.btn_bulk, self.btn_del,
                      self.btn_rename):
                b.state(["disabled"])
        self.root.after(0, ui)

    def _op_done(self, text, ok=True):
        def ui():
            self.op_var.set(("✅ " if ok else "⚠️ ") + text)
            self.op_label.config(fg="#166534" if ok else "#991b1b",
                                 bg="#dcfce7" if ok else "#fee2e2")
            for b in (self.btn_add, self.btn_bulk, self.btn_del,
                      self.btn_rename):
                b.state(["!disabled"])
        self.root.after(0, ui)

    # -- repo bookkeeping -----------------------------------------------------
    def target_repo(self):
        label = self.repo_var.get()
        for a in self.settings["repos"]:
            if a["label"] == label:
                return a
        return self.settings["repos"][0] if self.settings["repos"] else None

    def _sync_repo_combo(self):
        labels = [a["label"] for a in self.settings["repos"]]
        self.repo_combo["values"] = labels
        active = self.settings.get("active")
        if active in labels:
            self.repo_var.set(active)
        elif labels:
            self.repo_var.set(labels[0])

    def _on_repo_switch(self, _evt=None):
        self.settings["active"] = self.repo_var.get()
        try:
            save_settings(self.settings)
        except OSError as e:
            messagebox.showerror(
                "Remote-Op",
                f"Could not save settings:\n{SETTINGS_PATH}\n\n{e}")
        self._apply_active_password()
        self._fill_tree()

    def repo_by_label(self, label):
        for a in self.settings["repos"]:
            if a["label"] == label:
                return a
        return None

    def instances_of(self, label):
        return {wf: info for (lb, wf), info in self.instances.items()
                if lb == label}

    def _apply_active_password(self):
        """Export the target repo's OpenCode server password for this
        session (OPENCODE_SERVER_PASSWORD is what the V2 client reads;
        OPENCODE_PASSWORD is kept alongside for legacy tooling)."""
        cfg = self.target_repo()
        pwd = cfg["password"] if cfg else ""
        os.environ["OPENCODE_PASSWORD"] = pwd
        os.environ["OPENCODE_SERVER_PASSWORD"] = pwd

    def selected_ref(self):
        """(repo_cfg, wf) for a machine row, (repo_cfg, None) for a
        repo group row, None when nothing usable is selected."""
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.startswith("i::"):
            label, wf = iid[3:].rsplit("::", 1)
            cfg = self.repo_by_label(label)
            return (cfg, wf) if cfg else None
        if iid.startswith("a::"):
            cfg = self.repo_by_label(iid[3:])
            return (cfg, None) if cfg else None
        return None

    def scope_repo(self):
        """The repo the per-repo actions apply to: the selected row's
        repo, else the target repo (top combo)."""
        ref = self.selected_ref()
        return ref[0] if ref else self.target_repo()

    # -- refresh --------------------------------------------------------------
    def refresh(self):
        repos = list(self.settings["repos"])
        if not repos:
            return
        self.status_var.set("loading…")
        # Progressive: every repo's group row appears at once (marked
        # loading), and its machines land the moment THAT repo's fetch
        # finishes — not after the slowest repo.
        self._loading = {a["label"] for a in repos}
        self._fill_tree()

        def work():
            def fetch(cfg):
                label = cfg["label"]
                inst = runs = None
                try:
                    gh = GH(cfg["pat"])
                    inst = gh.list_instances(cfg["repo"], cfg["branch"])
                    runs = {}
                    for r in gh.list_runs(cfg["repo"]):
                        wf = None
                        for name, info in inst.items():
                            if r.get("path", "").endswith(info["file"]):
                                wf = name
                                break
                        if wf and (label, wf) not in runs:
                            runs[(label, wf)] = r
                except Exception as e:
                    self.log(f"{label}: load FAILED ({e})")

                def apply(cfg=cfg, inst=inst, runs=runs):
                    label = cfg["label"]
                    if inst is not None:
                        for k in [k for k in self.instances if k[0] == label]:
                            del self.instances[k]
                        for k in [k for k in self.runs if k[0] == label]:
                            del self.runs[k]
                        for k in [k for k in self.uptime if k[0] == label]:
                            del self.uptime[k]
                        for wf, info in inst.items():
                            self.instances[(label, wf)] = info
                        self.runs.update(runs)
                    self._loading.discard(label)
                    self._fill_tree()
                    if not self._loading and self.instances:
                        # Probe readiness the moment the fleet is
                        # loaded, not at the loop's next tick — a
                        # machine that is already up must not sit
                        # orange while the loop idles.
                        self.bg(self._check_uptime)

                self.root.after(0, apply)

            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(fetch, repos))

        self.bg(work)

    def _toggle_hide_cancelled(self):
        self.settings["hide_cancelled"] = bool(self.hide_cancelled_var.get())
        try:
            save_settings(self.settings)
        except OSError as e:
            self.log(f"could not save settings: {e}")
        self._fill_tree()

    @staticmethod
    def _box_image(kind):
        """A tiny checkbox image: 0 = empty, 1 = ticked, 2 = partial."""
        img = tk.PhotoImage(width=13, height=13)
        img.put("#ffffff", to=(0, 0, 13, 13))
        for i in range(13):
            img.put("#666666", to=(i, 0, i + 1, 1))
            img.put("#666666", to=(i, 12, i + 1, 13))
            img.put("#666666", to=(0, i, 1, i + 1))
            img.put("#666666", to=(12, i, 13, i + 1))
        if kind == 1:
            for x, y in [(2, 6), (3, 7), (4, 8), (5, 9), (5, 10), (6, 7),
                         (6, 8), (7, 5), (8, 4), (9, 3), (10, 2), (10, 3)]:
                img.put("#1a7f37", to=(x, y, x + 1, y + 1))
        elif kind == 2:
            img.put("#1a7f37", to=(3, 3, 10, 10))
        return img

    def _repo_check_state(self, label):
        wfs = [wf for (l, wf) in self.instances if l == label]
        if not wfs:
            return 0
        n = sum(1 for wf in wfs if (label, wf) in self.checked)
        return 1 if n == len(wfs) else (2 if n else 0)

    def _server_state(self, label, wf):
        """The machine's readiness state: the last probe's verdict,
        or — before any probe — "starting" when its run is already
        active (a booting machine is never shown ready on faith),
        else None (never probed, not running)."""
        st = self.uptime.get((label, wf))
        if st:
            return st[0]
        run = self.runs.get((label, wf), {})
        return "starting" if run.get("status") in ACTIVE_STATUSES else None

    def _server_text(self, label, wf):
        st = self.uptime.get((label, wf))
        return fmt_server(self._server_state(label, wf),
                          st[1] if st else None)

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.checked.intersection_update(self.instances.keys())
        total, running, hidden = 0, 0, 0
        for cfg in self.settings["repos"]:
            label = cfg["label"]
            inst = self.instances_of(label)
            parent = self.tree.insert(
                "", tk.END, iid=f"a::{label}",
                text=" " + label + (" (loading…)" if label in self._loading else ""),
                image=(self.img_off, self.img_on, self.img_some)[
                    self._repo_check_state(label)],
                open=True,
                values=(cfg["repo"], "", "", "", ""), tags=("acct",))
            for rowno, wf in enumerate(sorted(
                    inst, key=lambda n: parse_instance_name(n)[1])):
                info = inst[wf]
                run = self.runs.get((label, wf), {})
                status = run.get("status") or "—"
                if (self.settings.get("hide_cancelled")
                        and status not in ("in_progress", "queued")
                        and run.get("conclusion") == "cancelled"):
                    hidden += 1
                    continue
                shown = status if status != "completed" else (run.get("conclusion") or "completed")
                started, remaining, tag = "—", "—", ""
                if run.get("created_at"):
                    started = datetime.fromisoformat(
                        run["created_at"].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
                srv_state = self._server_state(label, wf)
                if status in ("in_progress", "queued"):
                    running += 1
                    mins = remaining_minutes(run.get("created_at"), info["timeout"])
                    remaining = fmt_remaining(mins)
                    # Green means READY, not merely running: while
                    # the stack is still booting the row stays orange
                    # (starting); only a machine whose services all
                    # answer turns green. The red low-budget warning
                    # outranks both.
                    if mins is not None and mins < 120:
                        tag = "low"
                    elif srv_state == "ready":
                        tag = "ok"
                    else:
                        tag = "starting"
                srv = self._server_text(label, wf)
                # One row colour only (ttk tags are per-row): a DOWN
                # server outranks the green/orange tints, but never
                # the red low-budget warning.
                row_tag = tag
                if srv_state == "down" and row_tag != "low":
                    row_tag = "down"
                self.tree.insert(
                    parent, tk.END, iid=f"i::{label}::{wf}",
                    text=f" {wf}",
                    image=self.img_on if (label, wf) in self.checked
                    else self.img_off,
                    values=(info["file"], shown, started, remaining, srv),
                    tags=tuple(t for t in (row_tag,
                                           "odd" if rowno % 2 else "even") if t))
                total += 1
        self.status_var.set(f"{len(self.settings['repos'])} repo(s), "
                            f"{total} machine(s), {running} running"
                            + (f", {hidden} cancelled hidden" if hidden else "")
                            + (f" — loading {len(self._loading)} repo(s)…"
                               if self._loading else ""))
        self.checked_var.set(f"{len(self.checked)} checked"
                             if self.checked else "")
        self.select_all_var.set(bool(self.instances) and
                                len(self.checked) == len(self.instances))

    # -- checkbox handling ------------------------------------------------------
    @staticmethod
    def _parse_iid(iid):
        if iid.startswith("i::"):
            label, wf = iid[3:].rsplit("::", 1)
            return ("i", label, wf)
        if iid.startswith("a::"):
            return ("a", iid[3:], None)
        return (None, None, None)

    def _toggle_instance(self, label, wf):
        key = (label, wf)
        if key in self.checked:
            self.checked.discard(key)
        else:
            self.checked.add(key)
        self._fill_tree()

    def _toggle_repo(self, label):
        wfs = [wf for (l, wf) in self.instances if l == label]
        if all((label, wf) in self.checked for wf in wfs) and wfs:
            for wf in wfs:
                self.checked.discard((label, wf))
        else:
            for wf in wfs:
                self.checked.add((label, wf))
        self._fill_tree()

    def _on_tree_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        # Only the checkbox image / first column toggles; a click
        # elsewhere just selects the row.
        col = self.tree.identify_column(event.x)
        if col != "#0":
            return
        kind, label, wf = self._parse_iid(iid)
        if kind == "i":
            self._toggle_instance(label, wf)
        elif kind == "a":
            self._toggle_repo(label)

    def _on_tree_space(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return "break"
        kind, label, wf = self._parse_iid(sel[0])
        if kind == "i":
            self._toggle_instance(label, wf)
        elif kind == "a":
            self._toggle_repo(label)
        return "break"

    def _on_tree_double(self, _event=None):
        ref = self.selected_ref()
        if ref and ref[1]:
            webbrowser.open(f"http://{ref[1]}:4096")

    def _toggle_select_all(self):
        if self.select_all_var.get():
            self.checked = set(self.instances.keys())
        else:
            self.checked = set()
        self._fill_tree()

    # -- readiness (integrated in the Server column) ---------------------------
    # A machine counts as UP only when its WHOLE stack answers
    # (READY_PORTS: 9router + OpenCode + Laravel — Laravel boots
    # last). While a machine is still starting, the loop re-checks
    # every 30s so its row flips green the moment it is truly ready;
    # a settled fleet is probed every 5 minutes.
    def _uptime_loop(self):
        self.bg(self._check_uptime)
        starting = any(
            self.runs.get(k, {}).get("status") in ACTIVE_STATUSES
            and self._server_state(*k) != "ready"
            for k in self.instances)
        if starting:
            delay = 30 * 1000
        elif not self.instances:
            delay = 5 * 1000  # fleet still loading — look again soon
        else:
            delay = 5 * 60 * 1000
        self.root.after(delay, self._uptime_loop)

    @staticmethod
    def _ping(url, timeout=6):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status < 500
        except Exception:
            return False

    def _fully_up(self, wf):
        """True only when every service of the machine answers."""
        with ThreadPoolExecutor(max_workers=len(READY_PORTS)) as ex:
            return all(ex.map(
                lambda p: self._ping(f"http://{wf}:{p}"), READY_PORTS))

    def _check_uptime(self):
        keys = list(self.instances.keys())

        def probe(key):
            label, wf = key
            run = self.runs.get(key, {})
            state = readiness(
                self._fully_up(wf),
                run.get("status") in ACTIVE_STATUSES)
            prev = self.uptime.get(key)
            since = ready_since(run, prev) if state == "ready" else None
            self.uptime[key] = (state, since)

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(probe, keys))
        self.root.after(0, self._after_uptime)

    def _after_uptime(self):
        self.uptime_var.set(
            f"server check: {time.strftime('%H:%M:%S')} "
            f"(30s while starting, else 5 min)")
        self._fill_tree()

    # -- run / cancel -----------------------------------------------------------
    def _dispatch(self, cfg, wf):
        info = self.instances.get((cfg["label"], wf))
        if not info:
            return
        run = self.runs.get((cfg["label"], wf), {})
        if run.get("status") in ("in_progress", "queued"):
            self.log(f"{cfg['label']}/{wf}: already {run['status']} — skipped")
            return
        if GH(cfg["pat"]).dispatch(cfg["repo"], cfg["branch"], info["file"]):
            self.log(f"{cfg['label']}/{wf}: dispatched")
        else:
            self.log(f"{cfg['label']}/{wf}: dispatch FAILED")

    def _dispatch_many(self, targets):
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda t: self._dispatch(*t), targets))
        self.refresh()

    def run_checked(self):
        targets = []
        for label, wf in sorted(self.checked):
            cfg = self.repo_by_label(label)
            if cfg:
                targets.append((cfg, wf))
        if not targets:
            self.log("nothing checked — tick the machines to run first")
            return

        def work():
            self._dispatch_many(targets)
        self.bg(work)

    def run_all(self):
        cfg = self.scope_repo()
        if not cfg:
            return
        targets = [(cfg, wf) for wf in self.instances_of(cfg["label"])]
        if not targets:
            self.log(f"{cfg['label']}: no machines loaded")
            return

        def work():
            self._dispatch_many(targets)
        self.bg(work)

    def _active_jobs(self, cfgs):
        """[(cfg, wf, run_id)] for every active run of the given repos."""
        jobs = []
        for cfg in cfgs:
            gh = GH(cfg["pat"])
            inst = self.instances_of(cfg["label"])
            for r in gh.list_runs(cfg["repo"]):
                if r.get("status") not in ACTIVE_STATUSES:
                    continue
                for wf, info in inst.items():
                    if r.get("path", "").endswith(info["file"]):
                        jobs.append((cfg, wf, r["id"]))
                        break
        return jobs

    def _cancel_many(self, jobs):
        def one(job):
            cfg, wf, rid = job
            if GH(cfg["pat"]).cancel_run(cfg["repo"], rid):
                self.log(f"{cfg['label']}/{wf}: run {rid} cancelled")
            else:
                self.log(f"{cfg['label']}/{wf}: cancel of run {rid} FAILED")
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(one, jobs))
        self.refresh()

    def stop_checked(self):
        cfgs = []
        seen = set()
        for label, _wf in sorted(self.checked):
            if label not in seen:
                seen.add(label)
                cfg = self.repo_by_label(label)
                if cfg:
                    cfgs.append(cfg)
        if not cfgs:
            self.log("nothing checked — tick the machines to stop first")
            return
        checked = set(self.checked)

        def work():
            jobs = [j for j in self._active_jobs(cfgs)
                    if (j[0]["label"], j[1]) in checked]
            if not jobs:
                self.log("no active runs among the checked machines")
                return
            self._cancel_many(jobs)
        self.bg(work)

    def cancel_all(self):
        cfg = self.scope_repo()
        if not cfg:
            return
        if not messagebox.askyesno(
                "Stop all",
                f"Cancel every active run in {cfg['repo']}?"):
            return

        def work():
            jobs = self._active_jobs([cfg])
            if not jobs:
                self.log(f"{cfg['label']}: no active runs")
                return
            self._cancel_many(jobs)
        self.bg(work)

    # -- connect ------------------------------------------------------------------
    def connect_selected(self):
        ref = self.selected_ref()
        if not ref or not ref[1]:
            self.log("select a machine first, then Connect")
            return
        cfg, wf = ref
        url = f"http://{wf}:4096"
        pwd = cfg["password"].replace("'", "''")
        ps_cmd = (f"$env:OPENCODE_SERVER_PASSWORD='{pwd}'; "
                  f"$env:OPENCODE_PASSWORD='{pwd}'; opencode --server {url}")
        self.log(f"opencode --server {url} "
                 f"(password via $env:OPENCODE_SERVER_PASSWORD, repo {cfg['label']})")
        try:
            if os.name == "nt":
                subprocess.Popen(
                    ["powershell.exe", "-NoExit", "-Command", ps_cmd],
                    creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                subprocess.Popen(["opencode", "--server", url],
                                 env={**os.environ,
                                      "OPENCODE_PASSWORD": cfg["password"],
                                      "OPENCODE_SERVER_PASSWORD": cfg["password"]})
        except FileNotFoundError:
            self.log("ERROR: powershell/opencode not found in PATH.")
        except Exception as e:
            self.log(f"Connect failed: {e}")

    # -- delete old runs ------------------------------------------------------------
    def delete_old_runs(self):
        cfg = self.scope_repo()
        if not cfg:
            return

        def work():
            gh = GH(cfg["pat"])
            runs = gh.list_completed_runs(cfg["repo"])
            if not runs:
                self.log(f"{cfg['label']}: no completed runs to delete")
                return
            # The confirmation needs the count first, and messagebox
            # must run on the UI thread.
            self.root.after(0, lambda: self._confirm_delete_runs(cfg, runs))
        self.bg(work)

    def _confirm_delete_runs(self, cfg, runs):
        if not messagebox.askyesno(
                "Delete old runs",
                f"Delete {len(runs)} completed Actions run(s) of "
                f"{cfg['repo']} from GitHub's history?\n\n"
                f"Machines and their state are untouched; runs still "
                f"in progress or queued are kept."):
            return

        def work():
            if not self._acquire_op(f"delete old runs of {cfg['label']}"):
                return
            outcome = [f"{cfg['label']}: old runs deleted", True]
            try:
                gh = GH(cfg["pat"])
                done = [0]

                def one(r):
                    if gh.delete_run(cfg["repo"], r["id"]):
                        done[0] += 1
                with ThreadPoolExecutor(max_workers=6) as ex:
                    list(ex.map(one, runs))
                self.log(f"{cfg['label']}: deleted {done[0]}/{len(runs)} "
                         f"completed runs")
                if done[0] < len(runs):
                    outcome[:] = [f"{cfg['label']}: run cleanup finished "
                                  f"with failures — see log", False]
            finally:
                self._release_op()
                self._op_done(outcome[0], outcome[1])
            self.refresh()
        self.bg(work)

    # -- add / create N ---------------------------------------------------------------
    def add_instance(self):
        self._create_dialog(preset_count=1)

    def create_bulk(self):
        self._create_dialog(preset_count=None)

    def _create_dialog(self, preset_count):
        """Create dialog: pick the repo, the count (preset 1 for Add),
        and — asked every time — whether each new machine is RAW
        (clean state) or a COPY of the first machine (its backed-up
        state cloned: sessions/history, codebase-memory index and
        9router db; plugins & skills are shared config and come along
        either way). When the chosen repo has no machines, a prefix
        field appears too: it names the machines when a brand-new
        repo is bootstrapped from the embedded template."""
        cfg = self.scope_repo() or self.target_repo()
        if not cfg:
            self.log("add a repo first (Repos…)")
            return
        win = tk.Toplevel(self.root)
        win.title("Add instance" if preset_count == 1
                  else "Create N from first machine")
        win.geometry(f"500x500+{self.root.winfo_rootx() + 140}"
                     f"+{self.root.winfo_rooty() + 90}")
        win.configure(bg=self.BG)
        frm = tk.Frame(win, bg=self.BG)
        frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        tk.Label(frm, text="Repo:", bg=self.BG).grid(row=0, column=0, sticky="w", pady=3)
        repo_var = tk.StringVar(value=cfg["label"])
        combo = ttk.Combobox(frm, textvariable=repo_var, state="readonly",
                             width=24, values=[a["label"] for a in self.settings["repos"]])
        combo.grid(row=0, column=1, sticky="w", pady=3)

        tk.Label(frm, text="How many machines:", bg=self.BG).grid(row=1, column=0, sticky="w", pady=3)
        count_var = tk.StringVar(value=str(preset_count or 10))
        ent = ttk.Entry(frm, textvariable=count_var, width=8)
        ent.grid(row=1, column=1, sticky="w", pady=3)
        if preset_count:
            ent.state(["disabled"])

        prefix_var = tk.StringVar(value="op")
        prefix_lbl = tk.Label(frm, text="New prefix:", bg=self.BG)
        prefix_lbl.grid(row=2, column=0, sticky="w", pady=3)
        prefix_ent = ttk.Entry(frm, textvariable=prefix_var, width=12)
        prefix_ent.grid(row=2, column=1, sticky="w", pady=3)

        mode_var = tk.StringVar(value="copy")
        tk.Label(frm, text="Start state:", bg=self.BG).grid(row=3, column=0, sticky="nw", pady=(8, 3))
        modes = tk.Frame(frm, bg=self.BG)
        modes.grid(row=3, column=1, sticky="w", pady=(8, 3))
        tk.Radiobutton(modes, bg=self.BG, activebackground=self.BG,
                       variable=mode_var, value="copy", wraplength=320,
                       justify="left",
                       text="Copy of the first machine — warm start "
                            "(history, sessions, indexes, 9router db)"
                       ).pack(anchor="w")
        tk.Radiobutton(modes, bg=self.BG, activebackground=self.BG,
                       variable=mode_var, value="raw", wraplength=320,
                       justify="left",
                       text="Raw — clean state, first sync creates it"
                       ).pack(anchor="w")
        tk.Label(frm, text="Plugins, skills, agents and MCP config live "
                           "in the shared opencode/config — every new "
                           "machine gets those either way.",
                 bg=self.BG, fg=self.MUTED, justify="left",
                 wraplength=430).grid(row=4, column=0, columnspan=2,
                                      sticky="w", pady=(2, 0))

        info_var = tk.StringVar(value="")
        tk.Label(frm, textvariable=info_var, bg=self.BG, fg=self.MUTED,
                 justify="left", wraplength=430).grid(
                     row=5, column=0, columnspan=2, sticky="w", pady=(8, 2))

        def update_info(*_a):
            c = self.repo_by_label(repo_var.get())
            if not c:
                info_var.set("")
                return
            inst = self.instances_of(c["label"])
            # The prefix field matters only when the repo has no
            # machine to clone from (history / brand-new bootstrap).
            if inst:
                prefix_lbl.grid_remove()
                prefix_ent.grid_remove()
            else:
                prefix_lbl.grid()
                prefix_ent.grid()
            try:
                n = int(count_var.get())
            except ValueError:
                info_var.set("Count must be a whole number (e.g. 10).")
                return
            if n < 1 or n > 100:
                info_var.set("Count must be between 1 and 100.")
                return
            mode_line = ("Each new machine also receives a full copy of "
                         "the first machine's backed-up state."
                         if mode_var.get() == "copy" else
                         "New machines start raw (no state copied).")
            if inst:
                first = sorted(inst, key=lambda x: parse_instance_name(x)[1])[0]
                prefix = parse_instance_name(first)[0]
                used = {parse_instance_name(x)[1] for x in inst}
                names = [f"{prefix}{k}" for k in plan_new_numbers(used, n)]
                info_var.set(
                    f"Template (first machine): {first}\n"
                    f"Will create: {', '.join(names)}\n"
                    f"{mode_line}\n"
                    f"(preview from the loaded fleet — numbers that only "
                    f"have a state dir are skipped too, so the final "
                    f"numbering is recomputed at run time)")
            else:
                pfx = prefix_var.get().strip() or "op"
                names = [f"{pfx}{k}" for k in plan_new_numbers(set(), n)]
                info_var.set(
                    "No machines loaded for this repo — at run time "
                    "the template comes from git history if a machine "
                    "was deleted here before. On a brand-new repo "
                    "(no history either) the workflow template "
                    "embedded in Remote-Op is used instead, rendered "
                    "for this repo.\n"
                    f"If it is brand-new, will create: "
                    f"{', '.join(names)}\n"
                    "Copy falls back to raw in both cases: there is "
                    "no live state to clone.")

        count_var.trace_add("write", update_info)
        mode_var.trace_add("write", update_info)
        prefix_var.trace_add("write", update_info)
        combo.bind("<<ComboboxSelected>>", update_info)
        update_info()

        btns = tk.Frame(frm, bg=self.BG)
        btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

        def go():
            try:
                n = int(count_var.get())
            except ValueError:
                messagebox.showerror("Create", "Count must be a whole number (e.g. 10).", parent=win)
                return
            if n < 1 or n > 100:
                messagebox.showerror("Create", "Count must be between 1 and 100.", parent=win)
                return
            c = self.repo_by_label(repo_var.get())
            mode = mode_var.get()
            pfx = prefix_var.get().strip()
            if pfx and not valid_prefix(pfx):
                messagebox.showerror(
                    "Create",
                    "Prefix must start and end with a letter "
                    "(lowercase letters, digits and hyphens inside), "
                    "e.g. rey, syd, open.", parent=win)
                return
            win.destroy()
            if c:
                self._start_create(c, n, mode, prefix=pfx or None)

        ttk.Button(btns, text="Create", style="Accent.TButton", command=go).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.LEFT, padx=2)

    def _start_create(self, cfg, count, mode="copy", prefix=None):
        def work():
            if not self._acquire_op(
                    f"create {count} instance(s) in {cfg['label']} "
                    f"({mode})"):
                return
            outcome = [f"{cfg['label']}: create FAILED — see log", False]
            try:
                created = create_instances(GH(cfg["pat"]), cfg, count,
                                           self.log, mode=mode,
                                           prefix_hint=prefix)
                if created:
                    outcome[:] = [f"{cfg['label']}: {len(created)} "
                                  f"instance(s) created "
                                  f"({created[0]}..{created[-1]}, {mode})",
                                  True]
                    self.checked.update((cfg["label"], n) for n in created)
            finally:
                self._release_op()
                self._op_done(outcome[0], outcome[1])
            self.refresh()
        self.bg(work)

    # -- delete checked ---------------------------------------------------------------
    def delete_checked(self):
        """Bulk delete: every ticked machine, grouped per repo."""
        by_repo = {}
        for label, wf in sorted(self.checked):
            cfg = self.repo_by_label(label)
            if cfg:
                by_repo.setdefault(label, (cfg, []))[1].append(wf)
        if not by_repo:
            self.log("nothing checked — tick the machines to delete first")
            return
        total = sum(len(v[1]) for v in by_repo.values())
        lines = "\n".join(
            f"• {label}: {', '.join(sorted(v[1], key=lambda n: parse_instance_name(n)[1]))}"
            for label, v in sorted(by_repo.items()))
        if not messagebox.askyesno(
                "Delete checked machines",
                f"Delete {total} checked machine(s) COMPLETELY?\n\n{lines}\n\n"
                f"For each machine: its workflow file + ALL its data "
                f"under opencode/instances/<name>/ (sessions, caches, "
                f"9router db) — one commit per repo.\n\n"
                f"The shared opencode/config is NOT touched.\n"
                f"Active runs are cancelled and awaited first; a "
                f"machine whose run will not stop is skipped."):
            return

        def work():
            if not self._acquire_op(f"delete {total} checked machine(s)"):
                return
            outcome = [f"delete checked: {total} machine(s) deleted", True]
            try:
                deleted_all, skipped_all = [], []
                for label, (cfg, names) in sorted(by_repo.items()):
                    self.log(f"— {label} ({cfg['repo']}): deleting "
                             f"{len(names)} machine(s)…")
                    deleted, skipped = delete_instances(
                        GH(cfg["pat"]), cfg, names, self.log)
                    deleted_all += [(label, n) for n in deleted]
                    skipped_all += [(label, n) for n in skipped]
                for key in deleted_all:
                    self.checked.discard(key)
                if skipped_all:
                    outcome[:] = [f"delete checked: {len(deleted_all)} "
                                  f"deleted, {len(skipped_all)} skipped "
                                  f"— see log", False]
                elif not deleted_all:
                    outcome[:] = ["delete checked: nothing was deleted "
                                  "— see log", False]
                else:
                    outcome[:] = [f"delete checked: {len(deleted_all)} "
                                  f"machine(s) deleted", True]
            finally:
                self._release_op()
                self._op_done(outcome[0], outcome[1])
            self.refresh()
        self.bg(work)

    # -- rename prefix ------------------------------------------------------------------
    def rename_prefix(self):
        cfg = self.scope_repo()
        if not cfg:
            return
        inst = self.instances_of(cfg["label"])
        ref = self.selected_ref()
        prefixes = []
        if ref and ref[1] and parse_instance_name(ref[1]):
            prefixes.append(parse_instance_name(ref[1])[0])
        prefixes += sorted({p[0] for p in
                            (parse_instance_name(n) for n in inst) if p})
        if not prefixes:
            messagebox.showwarning("Rename prefix",
                                   "No machines loaded for this repo — press Refresh first.")
            return
        current = prefixes[0]
        new = simpledialog.askstring(
            "Rename prefix",
            f"Repo: {cfg['repo']}\nCurrent prefix: {current}\nNew prefix:",
            parent=self.root)
        if new is None:
            return
        new = new.strip().lower()
        if not valid_prefix(new):
            messagebox.showerror(
                "Rename prefix",
                f"'{new}' is not a valid prefix.\nUse lowercase letters/digits/hyphens, "
                "starting and ending with a letter (e.g. mehdi, fork).")
            return
        if new == current:
            messagebox.showinfo("Rename prefix", "That is already the current prefix.")
            return
        if not messagebox.askyesno(
                "Rename prefix",
                f"Rename prefix '{current}' → '{new}' in {cfg['repo']}?\n\n"
                f"• workflow files, names and WF_NAME ({current}1 → {new}1, …)\n"
                f"• instance data moves: opencode/instances/{current}N/ → {new}N/\n"
                f"• machines come up as {new}1, {new}2, … on Tailscale\n"
                f"• all of it in ONE commit\n\n"
                f"NOT renamed: the h-dashboard branches (new ones are "
                f"created on the next runs; the old ones stay as "
                f"leftovers) and old Tailscale device entries.\n"
                f"Active runs will be cancelled first."):
            return

        def work():
            if not self._acquire_op(
                    f"rename prefix {current} -> {new} in {cfg['label']}"):
                return
            outcome = [f"{cfg['label']}: rename FAILED — see log", False]
            try:
                if rename_prefix_op(GH(cfg["pat"]), cfg, current, new, self.log):
                    outcome[:] = [f"{cfg['label']}: prefix renamed "
                                  f"{current} -> {new}", True]
            finally:
                self._release_op()
                self._op_done(outcome[0], outcome[1])
            self.refresh()
        self.bg(work)

    # -- repos dialog ---------------------------------------------------------------------
    def _repos_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Repos")
        win.geometry("720x400")
        lb = tk.Listbox(win, width=24)
        lb.pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=6)
        for a in self.settings["repos"]:
            lb.insert(tk.END, a["label"])

        form = tk.Frame(win)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)
        vars_ = {}
        rows = [("label", "Label", 30, False),
                ("pat", "PAT (optional — empty = gh CLI login)", 46, True),
                ("repo", "Repo (owner/name)", 30, False),
                ("branch", "Branch", 30, False),
                ("password", "OpenCode server password", 30, True)]
        for key, title, w, secret in rows:
            r = tk.Frame(form)
            r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=title, width=34, anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar()
            tk.Entry(r, textvariable=v, width=w,
                     show="•" if secret else "").pack(side=tk.LEFT)
            vars_[key] = v
        tk.Label(form, text="The server password is exported as "
                            "OPENCODE_SERVER_PASSWORD for Connect and, "
                            "on Save, persisted with setx on Windows "
                            "(target repo only). Branch is usually "
                            "'opc'.", fg="#666", wraplength=430,
                 justify="left").pack(anchor="w", pady=4)
        state = {"idx": None}

        def load_fields(idx):
            state["idx"] = idx
            a = self.settings["repos"][idx] if idx is not None else blank_repo()
            for k, v in vars_.items():
                v.set(a.get(k, ""))

        def flush():
            if state["idx"] is None:
                return
            a = self.settings["repos"][state["idx"]]
            for k, v in vars_.items():
                a[k] = v.get().strip()
            if a["repo"] and not a["label"]:
                a["label"] = _label_for(a["repo"])
            if not a["branch"]:
                a["branch"] = "opc"

        def on_select(_evt=None):
            flush()
            sel = lb.curselection()
            if sel:
                load_fields(sel[0])

        lb.bind("<<ListboxSelect>>", on_select)

        btns = tk.Frame(form)
        btns.pack(fill=tk.X, pady=8)

        def new_repo():
            flush()
            self.settings["repos"].append(blank_repo())
            lb.insert(tk.END, "")
            lb.selection_clear(0, tk.END)
            lb.selection_set(tk.END)
            load_fields(len(self.settings["repos"]) - 1)

        def del_repo():
            sel = lb.curselection()
            if not sel:
                return
            del self.settings["repos"][sel[0]]
            lb.delete(sel[0])
            state["idx"] = None
            load_fields(None)

        def save_close():
            if state["idx"] is None and any(
                    v.get().strip() for v in vars_.values()):
                # The form was filled without pressing New first —
                # adopt the typed values as a new repo instead of
                # silently discarding them on save.
                self.settings["repos"].append(blank_repo())
                lb.insert(tk.END, "")
                state["idx"] = len(self.settings["repos"]) - 1
            flush()
            self.settings["repos"] = [
                a for a in self.settings["repos"] if a.get("repo")]
            for a in self.settings["repos"]:
                if not a["label"]:
                    a["label"] = _label_for(a["repo"])
            self._sync_repo_combo()
            try:
                save_settings(self.settings)
            except OSError as e:
                messagebox.showerror(
                    "Remote-Op",
                    f"Could not save settings:\n{SETTINGS_PATH}\n\n{e}")
                return
            self._apply_active_password()
            self._persist_password_setx()
            win.destroy()
            self.refresh()

        tk.Button(btns, text="New", command=new_repo).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Delete", command=del_repo).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Save & close", command=save_close).pack(side=tk.LEFT, padx=10)
        if self.settings["repos"]:
            lb.selection_set(0)
            load_fields(0)
        else:
            # Start with a first entry already created, so the visible
            # form is bound to a real slot — otherwise whatever is
            # typed first is discarded on save.
            new_repo()

    def _persist_password_setx(self):
        """Persist the TARGET repo's server password for future
        terminals (Windows setx), as the old single-repo app did."""
        cfg = self.target_repo()
        if not cfg or os.name != "nt":
            return
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            for var in ("OPENCODE_SERVER_PASSWORD", "OPENCODE_PASSWORD"):
                subprocess.run(["setx", var, cfg["password"]],
                               capture_output=True, text=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                               startupinfo=si)
            self.log("Persisted via setx — new terminals will have "
                     "OPENCODE_SERVER_PASSWORD (target repo).")
        except Exception as e:
            self.log(f"setx failed (session-only): {e}")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

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
  * Update checked / Update all — re-render every workflow file from
    the canonical template embedded in the app (one delta commit per
    repo, only files that actually change). Takes effect on the next
    dispatch; running jobs keep the old form.
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
    prefix works. Two spots carry the identity: the workflow `name:`
    and the WF_NAME job env. A third spot — the old sync-back step's
    commit message ("from <old> instance") — is rewritten too when
    present (legacy files from before the sync-back removal); the
    template without a sync-back step has none, so it is optional.
    Raises ValueError if a required marker is missing.
    """
    old_name = workflow_instance(text)
    if not old_name:
        raise ValueError("template has no WF_NAME")
    old = re.escape(old_name)
    out, n1 = re.subn(rf"(?m)^name:\s*{old}\s*$", f"name: {new_name}", text, count=1)
    out, n2 = re.subn(rf"WF_NAME:\s*{old}", f"WF_NAME: {new_name}", out, count=1)
    out, _n3 = re.subn(rf"from {old} instance", f"from {new_name} instance", out, count=1)
    if not (n1 and n2):
        raise ValueError("template is missing name:/WF_NAME: markers")
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
# Update re-renders every live workflow file from this template;
# manual edits to a workflow file are overwritten on the next Update.
TEMPLATE_WORKFLOW = r'''# Managed by Remote-Op — re-rendered from the embedded template on Update; manual edits are overwritten
name: {{WF_NAME}}
on:
  workflow_dispatch:
    inputs:
      openrouter_account:
        description: "OpenRouter account name (must exist in the OPENROUTER_KEYS secret JSON; empty = default Zen + OpenCode account)"
        required: false
        default: ""
        type: string
      openrouter_model:
        description: "OpenRouter model id, e.g. vendor/model:free (empty = default Zen + OpenCode account)"
        required: false
        default: ""
        type: string
      nvidia_account:
        description: "NVIDIA account name (must exist in the NVIDIA_KEYS secret JSON; empty = default Zen + OpenCode account)"
        required: false
        default: ""
        type: string
      nvidia_model:
        description: "NVIDIA NIM model id, e.g. nvidia/nemotron-3-ultra-550b-a55b (empty = default Zen + OpenCode account)"
        required: false
        default: ""
        type: string
      custom_preset:
        description: "Custom preset name (must exist in the CUSTOM_KEYS secret JSON; empty = default Zen + OpenCode account)"
        required: false
        default: ""
        type: string
      custom_base_url:
        description: "Custom preset base URL, e.g. https://api.example.com (empty = default Zen + OpenCode account)"
        required: false
        default: ""
        type: string
      custom_model:
        description: "Custom preset model id, e.g. vendor/model (empty = default Zen + OpenCode account)"
        required: false
        default: ""
        type: string
      opencode_account:
        description: "OpenCode account name (must exist in the OPENCODE_KEYS secret JSON; Remote-Op sends the per-machine account automatically on a plain dispatch; empty = no Zen auth — fails fast)"
        required: false
        default: ""
        type: string
      nine_router:
        description: "Set to \"true\" to run this machine on 9router/code instead of the default Zen + OpenCode account; empty = default"
        required: false
        default: ""
        type: string


jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 340
    env:
      REPO_ORIGIN: https://github.com/{{OWNER}}/h-dashboard.git
      WF_NAME: {{WF_NAME}}
      OPENCODE_MODELS: ${{ vars.OPENCODE_DEFAULT_MODELS }}
      NINE_ROUTER_API_KEY: ${{ secrets.HERMES_CUSTOM_API_KEY }}

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

          # Sync the fork's beta from upstream (asgarimehdi/h-dashboard)
          # BEFORE branching: this fork's beta usually lags upstream, so
          # branching from origin/beta alone would boot stale code. Mirror
          # upstream/beta onto the fork, then branch from the freshly
          # synced origin/beta. If the push to the fork fails (e.g. this
          # token cannot push there), do NOT fail the boot and do NOT
          # silently keep the stale beta either: warn loudly and branch
          # from upstream/beta for this run instead.
          UPSTREAM_REPO="https://github.com/asgarimehdi/h-dashboard.git"
          BASE_REF="origin/beta"
          if [ "$REPO_ORIGIN" = "$UPSTREAM_REPO" ]; then
            echo "ℹ️ REPO_ORIGIN is upstream itself — fork beta sync skipped"
          else
            git remote get-url upstream >/dev/null 2>&1 \
              || git remote add upstream "$UPSTREAM_REPO"
            if git fetch upstream beta; then
              git checkout -B beta upstream/beta
              git remote set-url origin "$(printf '%s' "$REPO_ORIGIN" | sed "s#^https://#https://x-access-token:${GH_TOKEN}@#")"
              if git push origin beta; then
                git fetch origin beta
                echo "✅ Fork beta synced from upstream asgarimehdi/h-dashboard"
              else
                echo "⚠️⚠️ WARNING: could not push the synced beta to the fork — branching from upstream/beta for this run; the fork's beta stays stale"
                BASE_REF="upstream/beta"
              fi
            else
              echo "⚠️⚠️ WARNING: could not fetch upstream beta — branching from origin/beta (possibly stale) for this run"
            fi
          fi

          # Create / reset WF_NAME branch from the synced beta
          git checkout -B "${WF_NAME}" "$BASE_REF"
          echo "✅ Branch ${WF_NAME} created/reset from $BASE_REF"

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
          # wreq-js is installed before 9router.
          npm install -g wreq-js
          npm install -g 9router
          if [ -f ~/.npm-global/bin/9router ]; then
            sudo ln -sf ~/.npm-global/bin/9router /usr/local/bin/9router
            echo '✅ 9router installed and symlinked to /usr/local/bin/9router'
          else
            echo '⚠️ 9router binary not found after installation'
          fi

      - name: 12. Fetch 9router db from keys repo & start hidden (tray)
        env:
          GH_TOKEN: ${{ secrets.MY_PAT }}
        run: |
          export INITIAL_PASSWORD="${{ secrets.RUNNER_PASSWORD }}"
          # Make sure the data dir exists
          mkdir -p ~/.9router/db
          # The 9router db lives in the private keys repo
          # ({{OWNER}}/my-9router, file 9router.sqlite at root) — one
          # shared db for this account. It is fetched fresh on every
          # boot; nothing is read from opencode/instances/<wf>/ any
          # more, and the db is never written back (keys must not
          # land in this public fleet repo's history). Fail fast if
          # the keys repo or the file is unreachable — a silent
          # fresh start would boot a keyless router.
          KEYS_TMP="$(mktemp -d)"
          if ! git clone --depth 1 "https://x-access-token:${GH_TOKEN}@github.com/{{OWNER}}/my-9router.git" "$KEYS_TMP"; then
            echo '::error::could not clone {{OWNER}}/my-9router — check the repo exists and MY_PAT can read it'
            exit 1
          fi
          if [ -f "$KEYS_TMP/9router.sqlite" ]; then
            cp "$KEYS_TMP/9router.sqlite" ~/.9router/db/data.sqlite
            echo '✅ Restored 9router data.sqlite from keys repo'
          else
            echo '::error::9router.sqlite not found in {{OWNER}}/my-9router'
            exit 1
          fi
          rm -rf "$KEYS_TMP"
          # Start 9router hidden in the background (tray mode) on the default port 20128.
          # --tray is the same "hide to background" option used interactively.
          nohup 9router --tray --skip-update -p 20128 > /tmp/9router.log 2>&1 &
          sleep 6
          if curl -fsS http://localhost:20128 > /dev/null 2>&1; then
            echo '✅ 9router is live on http://localhost:20128'
          else
            echo '⚠️ 9router did not come up — check /tmp/9router.log'
          fi

      # 12b. Per-run OpenRouter choice (Remote-Op dispatch inputs only).
      #     Runs ONLY when a dispatch carried an account name; a plain
      #     dispatch (or any other trigger) skips it and the run keeps
      #     the default Zen + OpenCode account setup. The token is NEVER sent in the
      #     dispatch: it is read here from the OPENROUTER_KEYS secret
      #     (one JSON object, account name -> token, set by the repo
      #     owner), masked, and exported via $GITHUB_ENV so step 13
      #     and the `opencode serve` process inherit OPENROUTER_API_KEY
      #     from the environment — the built-in openrouter provider
      #     authenticates from that variable, and no key is written
      #     into opencode.json. Missing secret / bad JSON / unknown
      #     account / missing model all fail fast, with the account
      #     name only in the message — never the token or the JSON.
      - name: 12b. Resolve OpenRouter account for this run only
        if: inputs.openrouter_account != ''
        env:
          OPENROUTER_KEYS: ${{ secrets.OPENROUTER_KEYS }}
          OPENROUTER_ACCOUNT: ${{ inputs.openrouter_account }}
          OPENROUTER_MODEL: ${{ inputs.openrouter_model }}
        run: |
          python3 - <<'PYEOF_OR'
          import json, os, sys
          keys_raw = os.environ.get("OPENROUTER_KEYS", "")
          if not keys_raw:
              print("::error::OPENROUTER_KEYS secret is not set on this repo — "
                    "set it (JSON: account name -> token) or dispatch without an OpenRouter account")
              sys.exit(1)
          try:
              keys = json.loads(keys_raw)
          except Exception:
              print("::error::OPENROUTER_KEYS is not valid JSON")
              sys.exit(1)
          account = os.environ.get("OPENROUTER_ACCOUNT", "")
          model = os.environ.get("OPENROUTER_MODEL", "")
          if not model:
              print("::error::openrouter_model is empty — dispatch with both an account and a model")
              sys.exit(1)
          token = keys.get(account) if isinstance(keys, dict) else None
          if not isinstance(token, str) or not token:
              print(f"::error::OpenRouter account '{account}' not found in OPENROUTER_KEYS")
              sys.exit(1)
          print("::add-mask::" + token)
          with open(os.environ["GITHUB_ENV"], "a") as f:
              f.write("OPENROUTER_API_KEY=" + token + "\n")
              f.write("OPENROUTER_MODEL=" + model + "\n")
          print(f"OpenRouter account '{account}' resolved for this run only")
          PYEOF_OR

      # 12c. Per-run NVIDIA choice (Remote-Op dispatch inputs only).
      #     Runs ONLY when a dispatch carried an account name; a plain
      #     dispatch (or any other trigger) skips it and the run keeps
      #     the default Zen + OpenCode account setup. Same shape as step 12b: the token is
      #     NEVER sent in the dispatch — it is read here from the
      #     NVIDIA_KEYS secret (one JSON object, account name -> token,
      #     set by the repo owner), masked, and exported via
      #     $GITHUB_ENV so step 13 and the `opencode serve` process
      #     inherit NVIDIA_API_KEY from the environment. §4 writes the
      #     `nvidia` provider block (OpenAI-compatible, NVIDIA NIM
      #     endpoint) into opencode.json for this run only, reading
      #     the key from that variable — no key is written into the
      #     config. Missing secret / bad JSON / unknown account /
      #     missing model all fail fast, with the account name only
      #     in the message — never the token or the JSON.
      - name: 12c. Resolve NVIDIA account for this run only
        if: inputs.nvidia_account != ''
        env:
          NVIDIA_KEYS: ${{ secrets.NVIDIA_KEYS }}
          NVIDIA_ACCOUNT: ${{ inputs.nvidia_account }}
          NVIDIA_MODEL: ${{ inputs.nvidia_model }}
        run: |
          python3 - <<'PYEOF_NV'
          import json, os, sys
          keys_raw = os.environ.get("NVIDIA_KEYS", "")
          if not keys_raw:
              print("::error::NVIDIA_KEYS secret is not set on this repo — "
                    "set it (JSON: account name -> token) or dispatch without an NVIDIA account")
              sys.exit(1)
          try:
              keys = json.loads(keys_raw)
          except Exception:
              print("::error::NVIDIA_KEYS is not valid JSON")
              sys.exit(1)
          account = os.environ.get("NVIDIA_ACCOUNT", "")
          model = os.environ.get("NVIDIA_MODEL", "")
          if not model:
              print("::error::nvidia_model is empty — dispatch with both an account and a model")
              sys.exit(1)
          token = keys.get(account) if isinstance(keys, dict) else None
          if not isinstance(token, str) or not token:
              print(f"::error::NVIDIA account '{account}' not found in NVIDIA_KEYS")
              sys.exit(1)
          print("::add-mask::" + token)
          with open(os.environ["GITHUB_ENV"], "a") as f:
              f.write("NVIDIA_API_KEY=" + token + "\n")
              f.write("NVIDIA_MODEL=" + model + "\n")
          print(f"NVIDIA account '{account}' resolved for this run only")
          PYEOF_NV

      # 12d. Per-run Custom preset choice (Remote-Op dispatch inputs only).
      #     Runs ONLY when a dispatch carried a preset name; a plain
      #     dispatch (or any other trigger) skips it and the run keeps
      #     the default Zen + OpenCode account setup. Same shape as steps 12b/12c: the
      #     token is NEVER sent in the dispatch — it is read here from
      #     the CUSTOM_KEYS secret (one JSON object, preset name ->
      #     token, set by the repo owner), masked, and exported via
      #     $GITHUB_ENV so step 13 and the `opencode serve` process
      #     inherit CUSTOM_API_KEY from the environment. §4 writes the
      #     `custom` provider block (OpenAI-compatible, the preset's
      #     baseURL) into opencode.json for this run only, reading the
      #     key from that variable — no key is written into the
      #     config. Missing secret / bad JSON / unknown preset /
      #     missing base URL / missing model all fail fast, with the
      #     preset name only in the message — never the token or the
      #     JSON.
      - name: 12d. Resolve Custom preset for this run only
        if: inputs.custom_preset != ''
        env:
          CUSTOM_KEYS: ${{ secrets.CUSTOM_KEYS }}
          CUSTOM_PRESET: ${{ inputs.custom_preset }}
          CUSTOM_BASE_URL: ${{ inputs.custom_base_url }}
          CUSTOM_MODEL: ${{ inputs.custom_model }}
        run: |
          python3 - <<'PYEOF_CU'
          import json, os, sys
          keys_raw = os.environ.get("CUSTOM_KEYS", "")
          if not keys_raw:
              print("::error::CUSTOM_KEYS secret is not set on this repo — "
                    "set it (JSON: preset name -> token) or dispatch without a Custom preset")
              sys.exit(1)
          try:
              keys = json.loads(keys_raw)
          except Exception:
              print("::error::CUSTOM_KEYS is not valid JSON")
              sys.exit(1)
          preset = os.environ.get("CUSTOM_PRESET", "")
          base_url = os.environ.get("CUSTOM_BASE_URL", "")
          model = os.environ.get("CUSTOM_MODEL", "")
          if not base_url:
              print("::error::custom_base_url is empty — dispatch with a preset, its base URL and a model")
              sys.exit(1)
          if not model:
              print("::error::custom_model is empty — dispatch with both a preset and a model")
              sys.exit(1)
          token = keys.get(preset) if isinstance(keys, dict) else None
          if not isinstance(token, str) or not token:
              print(f"::error::Custom preset '{preset}' not found in CUSTOM_KEYS")
              sys.exit(1)
          print("::add-mask::" + token)
          with open(os.environ["GITHUB_ENV"], "a") as f:
              f.write("CUSTOM_API_KEY=" + token + "\n")
              f.write("CUSTOM_BASE_URL=" + base_url + "\n")
              f.write("CUSTOM_MODEL=" + model + "\n")
          print(f"Custom preset '{preset}' resolved for this run only")
          PYEOF_CU

      # 12e. Default OpenCode account (Remote-Op dispatch inputs only).
      #     Runs when a dispatch carried an account name — which is
      #     EVERY plain dispatch from Remote-Op: it resolves the
      #     per-machine account automatically (machine N -> account
      #     N in settings order, wrapping) and sends only the NAME.
      #     Same shape as steps 12b/12c: the token is NEVER sent in
      #     the dispatch — it is read here from the OPENCODE_KEYS
      #     secret (one JSON object, account name -> token, set by
      #     the repo owner), masked, and exported via $GITHUB_ENV
      #     so step 13 and the `opencode serve` process inherit
      #     OPENCODE_API_KEY from the environment. The built-in
      #     opencode provider authenticates from that variable, so
      #     no key is written into opencode.json and no provider
      #     block is needed — §4 just picks the per-instance Zen
      #     model. Missing secret / bad JSON / unknown account all
      #     fail fast, with the account name only in the message —
      #     never the token or the JSON. A dispatch with no account
      #     at all (e.g. a manual dispatch with no inputs) skips
      #     this step; step 13's provider guard then fails fast
      #     instead of letting the run 401 mid-run with no key.
      - name: 12e. Resolve OpenCode account for this run
        if: inputs.opencode_account != ''
        env:
          OPENCODE_KEYS: ${{ secrets.OPENCODE_KEYS }}
          OPENCODE_ACCOUNT: ${{ inputs.opencode_account }}
        run: |
          python3 - <<'PYEOF_OA'
          import json, os, sys
          keys_raw = os.environ.get("OPENCODE_KEYS", "")
          if not keys_raw:
              print("::error::OPENCODE_KEYS secret is not set on this repo — "
                    "set it (JSON: account name -> token) or dispatch with a provider choice")
              sys.exit(1)
          try:
              keys = json.loads(keys_raw)
          except Exception:
              print("::error::OPENCODE_KEYS is not valid JSON")
              sys.exit(1)
          account = os.environ.get("OPENCODE_ACCOUNT", "")
          token = keys.get(account) if isinstance(keys, dict) else None
          if not isinstance(token, str) or not token:
              print(f"::error::OpenCode account '{account}' not found in OPENCODE_KEYS")
              sys.exit(1)
          print("::add-mask::" + token)
          with open(os.environ["GITHUB_ENV"], "a") as f:
              f.write("OPENCODE_API_KEY=" + token + "\n")
          print(f"OpenCode account '{account}' resolved for this run only")
          PYEOF_OA

      - name: 13. Install OpenCode V2, restore state, configure MCPs + Superpowers plugin & start serve
        env:
          OPENCODE_SERVER_PASSWORD: ${{ secrets.RUNNER_PASSWORD }}
          OPENCODE_DISABLE_AUTOUPDATE: "true"
          GH_TOKEN: ${{ secrets.MY_PAT }}
          NINE_ROUTER: ${{ inputs.nine_router }}
        run: |
          # 0. Provider guard: every dispatch must carry a provider —
          #    Remote-Op always sends one (a plain dispatch carries
          #    the per-machine opencode_account, the 9router option
          #    carries nine_router=true, and the other providers
          #    carry their own inputs). A manual dispatch with no
          #    inputs would otherwise boot the Zen model with no key
          #    and fail mid-run with a 401; fail fast here with a
          #    clear error instead. 12b/12c/12d/12e export their
          #    signals via $GITHUB_ENV, so they are visible here.
          if [ -z "${OPENCODE_API_KEY:-}" ] \
              && [ -z "${OPENROUTER_MODEL:-}" ] \
              && [ -z "${NVIDIA_MODEL:-}" ] \
              && [ -z "${CUSTOM_MODEL:-}" ] \
              && [ -z "${NINE_ROUTER:-}" ]; then
            echo '::error::no provider selected — dispatch from Remote-Op, or pass opencode_account / nine_router / provider inputs'
            exit 1
          fi

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
          #     (~/.cache/codebase-memory-mcp) is restored in §2.
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

          # 2. Restore the OpenCode backup from the repo (opencode/ dir).
          #    config/ is SHARED by all four
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
          #    {env:...}, so NO secret lands in opencode.json.
          #    Persisted to ~/.bashrc as well, for
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
          #    PRESERVES any other server already in the config — the
          #    mcp block is merged, never rewritten whole.
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
          # Per-run OpenRouter override (step 12b exported
          # OPENROUTER_MODEL only when the dispatch carried an account
          # + model). It wins over the per-instance file/variable/
          # built-in resolution below, for this run only: the runner's
          # config is never committed back, so the next plain dispatch
          # restores the Zen model + OpenCode account. The openrouter provider reads
          # OPENROUTER_API_KEY from the environment (step 12b), so no
          # key is written into this config.
          _or_raw = os.environ.get("OPENROUTER_MODEL", "").strip()
          _or_model = ("openrouter/" + _or_raw if _or_raw and not _or_raw.startswith("openrouter/") else _or_raw)
          # Per-run NVIDIA override (step 12c exported NVIDIA_MODEL
          # only when the dispatch carried an account + model). Unlike
          # the built-in openrouter provider, NVIDIA needs an explicit
          # provider block: an OpenAI-compatible provider pointing at
          # the NVIDIA NIM endpoint, reading its key from
          # NVIDIA_API_KEY in the environment (step 12c), with the
          # chosen model registered in its `models` map (OpenCode
          # only resolves models listed there). The model id is the
          # full NIM id (e.g. nvidia/nemotron-3-ultra-550b-a55b), so
          # the OpenCode ref is nvidia/<that id>. The block is written
          # only for an NVIDIA run; a plain dispatch leaves whatever
          # provider block the restored config already had untouched
          # (and the model resolution below restores the Zen model +
          # OpenCode account).
          # When several overrides are somehow set, Custom wins,
          # then NVIDIA, then OpenRouter, then 9router — the Run
          # dialog only ever sends one provider's inputs.
          # The NIM id itself usually contains a slash (e.g.
          # nvidia/nemotron-3-ultra-550b-a55b), so the OpenCode ref
          # is nvidia/<NIM id> — three segments. A value already in
          # that triple form has its first nvidia/ stripped back to
          # the NIM id; anything else is taken as the NIM id as-is.
          _nv_id = os.environ.get("NVIDIA_MODEL", "").strip()
          if _nv_id.startswith("nvidia/nvidia/"):
              _nv_id = _nv_id[len("nvidia/"):]
          _nv_model = ("nvidia/" + _nv_id) if _nv_id else ""
          # Per-run Custom preset override (step 12d exported
          # CUSTOM_MODEL + CUSTOM_BASE_URL only when the dispatch
          # carried a preset name). Like NVIDIA it needs an explicit
          # provider block: an OpenAI-compatible provider pointing
          # at the preset's baseURL, reading its key from
          # CUSTOM_API_KEY in the environment (step 12d), with the
          # chosen model registered in its `models` map. The
          # OpenCode ref is custom/<model-id>; a value already in
          # that form has its one leading custom/ stripped back to
          # the id. The baseURL is normalized here too (a manual
          # dispatch may send it raw): trailing slashes stripped,
          # a trailing /v1/models trimmed to /v1, /v1 appended
          # unless already there or the base already ends in a
          # version segment (/v2, /v4, ...), which is kept as-is.
          # The block is written only for a
          # Custom run; a plain dispatch leaves whatever provider
          # block the restored config already had untouched (and
          # the model resolution below restores the Zen model +
          # OpenCode account).
          _cu_raw = os.environ.get("CUSTOM_MODEL", "").strip()
          _cu_base = os.environ.get("CUSTOM_BASE_URL", "").strip()
          _cu_id = _cu_raw[len("custom/"):] \
              if _cu_raw.startswith("custom/") else _cu_raw
          _cu_model = ("custom/" + _cu_id) if _cu_id else ""
          if _cu_base:
              _cu_base = _cu_base.rstrip("/")
              if _cu_base.endswith("/v1/models"):
                  _cu_base = _cu_base[:-len("/models")]
              _cu_tail = _cu_base.rsplit("/", 1)[-1]
              _cu_versioned = _cu_tail.startswith("v") \
                  and _cu_tail[1:].isdigit()
              if not _cu_base.endswith("/v1") and not _cu_versioned:
                  _cu_base = _cu_base + "/v1"
          # 9router provider: this machine's own 9router on
          # http://127.0.0.1:20128 (step 12 restores its db from the
          # private keys repo {{OWNER}}/my-9router). OpenCode talks
          # to it as an OpenAI-compatible provider; the model is the
          # `code` combo, addressed by name — 9router routes it
          # across the combo's models. The key comes from the
          # HERMES_CUSTOM_API_KEY repo secret (job env
          # NINE_ROUTER_API_KEY); its value must match an apiKey in
          # the restored db or 9router answers 401. 9router/code is
          # used ONLY when the dispatch explicitly chose it (the
          # nine_router input = "true", sent by Remote-Op's 9router
          # option); a plain dispatch uses the per-instance Zen
          # model with the OpenCode account's key (step 12e). A
          # per-run provider choice above still wins for that run
          # only. Choosing 9router with no 9router key is a
          # misconfiguration — fail fast here, not 401 mid-run.
          _nr_key = os.environ.get("NINE_ROUTER_API_KEY", "").strip()
          _nr_flag = os.environ.get("NINE_ROUTER", "").strip().lower() == "true"
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
          if _cu_model and _cu_base:
              _prov = cfg.setdefault("provider", {})
              _custom = _prov.setdefault("custom", {})
              _custom["npm"] = "@ai-sdk/openai-compatible"
              _custom["name"] = "Custom"
              _custom_opts = _custom.setdefault("options", {})
              _custom_opts["baseURL"] = _cu_base
              _custom_opts["apiKey"] = "{env:CUSTOM_API_KEY}"
              _custom.setdefault("models", {}).setdefault(
                  _cu_id, {"name": _cu_id})
              cfg["model"] = _cu_model
          elif _nv_model:
              _nim_id = _nv_id
              _prov = cfg.setdefault("provider", {})
              _nvidia = _prov.setdefault("nvidia", {})
              _nvidia["npm"] = "@ai-sdk/openai-compatible"
              _nvidia["name"] = "NVIDIA NIM"
              _nvidia_opts = _nvidia.setdefault("options", {})
              _nvidia_opts["baseURL"] = "https://integrate.api.nvidia.com/v1"
              _nvidia_opts["apiKey"] = "{env:NVIDIA_API_KEY}"
              _nvidia.setdefault("models", {}).setdefault(
                  _nim_id, {"name": _nim_id})
              cfg["model"] = _nv_model
          elif _or_model:
              cfg["model"] = _or_model
          elif _nr_flag:
              if not _nr_key:
                  print("::error::nine_router was chosen but "
                        "NINE_ROUTER_API_KEY is not set — set the "
                        "HERMES_CUSTOM_API_KEY secret or dispatch with "
                        "another provider")
                  raise SystemExit(1)
              _prov = cfg.setdefault("provider", {})
              _nr = _prov.setdefault("9router", {})
              _nr["npm"] = "@ai-sdk/openai-compatible"
              _nr["name"] = "9router"
              _nr_opts = _nr.setdefault("options", {})
              _nr_opts["baseURL"] = "http://127.0.0.1:20128/v1"
              _nr_opts["apiKey"] = "{env:NINE_ROUTER_API_KEY}"
              _nr.setdefault("models", {}).setdefault("code", {"name": "code"})
              cfg["model"] = "9router/code"
          else:
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
          #     Everything lands under ~/.config/opencode (restored in §2).
          #     Two installer quirks shape
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
          #     (~/.config/opencode/oh-my-opencode-slim.json) with a "free" preset
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
          # Provider override (same CUSTOM_MODEL / NVIDIA_MODEL /
          # OPENROUTER_MODEL / NINE_ROUTER as §4, Custom checked
          # first, then NVIDIA, then OpenRouter, then 9router):
          # EVERY slim seat — agents, council and councillors — on
          # the chosen model for this run only, and the preset
          # forced to "free" so these seats are the ones actually
          # used. The runner's slim config is never committed back,
          # so the next plain dispatch restores the seats below. A
          # plain dispatch (no per-run choice) leaves every seat on
          # its free Zen model below; only an explicit 9router
          # choice (nine_router input) puts every seat on
          # 9router/code, the same as §4's main model.
          _cu_raw = os.environ.get("CUSTOM_MODEL", "").strip()
          _nv_raw = os.environ.get("NVIDIA_MODEL", "").strip()
          _or_raw = os.environ.get("OPENROUTER_MODEL", "").strip()
          _nr_flag = os.environ.get("NINE_ROUTER", "").strip().lower() == "true"
          _override = ""
          if _cu_raw:
              _cu_id = _cu_raw[len("custom/"):] \
                  if _cu_raw.startswith("custom/") else _cu_raw
              _override = "custom/" + _cu_id
          elif _nv_raw:
              _nv_id = _nv_raw[len("nvidia/"):] \
                  if _nv_raw.startswith("nvidia/nvidia/") else _nv_raw
              _override = "nvidia/" + _nv_id
          elif _or_raw:
              _override = "openrouter/" + _or_raw if not _or_raw.startswith("openrouter/") else _or_raw
          elif _nr_flag:
              _override = "9router/code"
          if _override:
              for _seat in list(free_preset.values()):
                  if isinstance(_seat, dict):
                      _seat["model"] = _override
              for _seat in list(councillors.values()):
                  if isinstance(_seat, dict):
                      _seat["model"] = _override
              cfg["preset"] = "free"
          cfg["council"].setdefault("default_preset", "default")
          cfg.setdefault("preset", "free")
          with open(path, "w") as f:
              json.dump(cfg, f, indent=2)
          if _override:
              print(f"✅ oh-my-opencode-slim.json ready: 'free' preset — every agent on {_override}")
          else:
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

      - name: 16b. Back up 9router db to keys repo (on shutdown)
        if: always()
        env:
          GH_TOKEN: ${{ secrets.MY_PAT }}
        run: |
          # Back up the live 9router db into the private keys repo
          # ({{OWNER}}/my-9router, file 9router.sqlite) so keys/combos
          # added or changed during this run survive shutdown. Runs
          # on every shutdown (cancel/timeout included) via
          # if: always(). 9router is stopped first so SQLite
          # checkpoints its WAL into data.sqlite; the copy is
          # integrity-checked before it is pushed, and a failed
          # backup is a loud warning, not a failed run. If ara1 and
          # ara2 shut down together they race on the same file —
          # last writer wins; both booted from the same file, so
          # only usage-history rows can differ. If step 12 never
          # restored a db (fetch failed), no file exists here and
          # nothing is backed up — an empty db can never overwrite
          # the keys repo.
          pkill -f "9router --tray --skip-update" || true
          for _i in $(seq 1 10); do pgrep -f "9router --tray" >/dev/null || break; sleep 1; done
          DB_SRC="$HOME/.9router/db/data.sqlite"
          if [ ! -f "$DB_SRC" ]; then
            echo '::warning::no 9router db found — nothing to back up'
            exit 0
          fi
          # Merge any WAL content still on disk (no-op if 9router
          # already flushed it on exit) and copy to a temp file.
          if command -v sqlite3 >/dev/null 2>&1; then
            sqlite3 "$DB_SRC" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
          fi
          TMP_DIR="$(mktemp -d)"
          TMP_DB="$TMP_DIR/9router.sqlite"
          cp "$DB_SRC" "$TMP_DB" || { echo '::warning::could not copy the 9router db — backup skipped'; exit 0; }
          if command -v sqlite3 >/dev/null 2>&1; then
            if ! sqlite3 "$TMP_DB" "PRAGMA integrity_check;" 2>/dev/null | grep -qx "ok"; then
              echo '::warning::backed-up 9router db failed integrity_check — NOT pushed'
              exit 0
            fi
          fi
          # Push with a bounded retry: a second machine shutting down
          # at the same time can win the race on the same file.
          for attempt in 1 2 3; do
            KEYS_TMP="$(mktemp -d)"
            if git clone --depth 1 --branch main "https://x-access-token:${GH_TOKEN}@github.com/{{OWNER}}/my-9router.git" "$KEYS_TMP"; then
              cp "$TMP_DB" "$KEYS_TMP/9router.sqlite" || continue
              cd "$KEYS_TMP" || continue
              git add 9router.sqlite || continue
              if git diff --cached --quiet; then
                echo '✅ 9router db unchanged — nothing to back up'
                exit 0
              fi
              if git -c user.name="{{OWNER}}" -c user.email="{{OWNER}}@users.noreply.github.com" commit -m "Backup 9router db from ${WF_NAME} (run ${GITHUB_RUN_ID})"; then
                if git push origin main; then
                  echo "✅ 9router db backed up to {{OWNER}}/my-9router (attempt $attempt)"
                  exit 0
                fi
                echo "⚠️ push failed (attempt $attempt) — retrying"
              else
                echo "⚠️ commit failed (attempt $attempt) — retrying"
              fi
              cd "$GITHUB_WORKSPACE" || true
            else
              echo "⚠️ clone failed (attempt $attempt) — retrying"
            fi
            rm -rf "$KEYS_TMP"
            sleep 10
          done
          echo '::warning::9router db backup failed after 3 attempts — the live db died with this runner'
          exit 0

      - name: 16. Tailscale Logout (cleanup on shutdown)
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


def map_runs_to_workflows(runs, inst, label):
    """{(label, wf): run} — the newest run per workflow. The API
    lists newest first, so the first match per workflow wins; a run
    for a file that is no live workflow is ignored."""
    out = {}
    for r in runs:
        for name, info in inst.items():
            if r.get("path", "").endswith(info["file"]):
                out.setdefault((label, name), r)
                break
    return out


# ---------------------------------------------------------------------------
# OpenRouter accounts (per-run choice at Run time)
# ---------------------------------------------------------------------------

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_KEYS_SECRET = "OPENROUTER_KEYS"


def blank_openrouter_account():
    return {"name": "", "token": ""}


def normalize_openrouter_accounts(accounts):
    """Settings-shape OpenRouter accounts: drop entries with no name,
    trim name/token, keep the FIRST entry per name. Tokens are never
    logged or echoed anywhere in this app."""
    seen, out = set(), []
    for a in accounts or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name,
                    "token": str(a.get("token", "")).strip()})
    return out


def openrouter_account_names(accounts):
    return [a["name"] for a in normalize_openrouter_accounts(accounts)]


def _move_account_entry(accounts, idx, delta):
    """Shared list-order move for the provider account dialogs.
    Swaps two entries in place — names and tokens travel with
    their entry, nothing is retyped or dropped. At either end, or
    for an out-of-range index, it is a no-op. Returns the entry's
    new index (the old one when nothing moved)."""
    if not isinstance(accounts, list):
        return idx
    new_idx = idx + delta
    if idx < 0 or idx >= len(accounts) or new_idx < 0 \
            or new_idx >= len(accounts):
        return idx
    accounts[idx], accounts[new_idx] = accounts[new_idx], accounts[idx]
    return new_idx


def move_openrouter_account(accounts, idx, delta):
    """Move one account by `delta` (-1 up, +1 down) in list order.
    List order IS the machine mapping, so this only swaps two
    entries in place — names and tokens travel with their entry,
    nothing is retyped or dropped. At either end, or for an
    out-of-range index, it is a no-op. Returns the entry's new
    index (the old one when nothing moved)."""
    return _move_account_entry(accounts, idx, delta)


def move_nvidia_account(accounts, idx, delta):
    """Same list-order move as move_openrouter_account, for the
    NVIDIA accounts: list order IS the machine mapping."""
    return _move_account_entry(accounts, idx, delta)


def free_openrouter_model_ids(payload):
    """FREE model ids from an OpenRouter /api/v1/models payload:
    prompt AND completion pricing both exactly zero (strings like
    "0" — parsed as floats; anything unparseable is not free).
    Sorted, de-duplicated. The public catalog needs no API key, so
    no token is ever sent to fetch it."""
    ids = set()
    data = payload.get("data") if isinstance(payload, dict) else None
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        pricing = entry.get("pricing")
        if not isinstance(mid, str) or not mid or not isinstance(pricing, dict):
            continue
        try:
            prompt = float(pricing.get("prompt"))
            completion = float(pricing.get("completion"))
        except (TypeError, ValueError):
            continue
        if prompt == 0.0 and completion == 0.0:
            ids.add(mid)
    return sorted(ids, key=str.casefold)


def fetch_openrouter_models(urlopen=None, timeout=10):
    """Free OpenRouter model ids, live from the public catalog.
    [] on ANY failure (network, timeout, bad payload) — the Run
    dialog then offers manual model entry instead of blocking."""
    opener = urlopen or urllib.request.urlopen
    try:
        req = urllib.request.Request(
            OPENROUTER_MODELS_URL,
            headers={"User-Agent": "Remote-Op"})
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return free_openrouter_model_ids(payload)
    except Exception:
        return []


def normalize_openrouter_model(raw):
    """OpenCode model id for a chosen OpenRouter slug: the catalog id
    as listed (vendor/model[:free]), prefixed with openrouter/ when
    it is not already. "" stays "" (no choice)."""
    mid = (raw or "").strip()
    if not mid:
        return ""
    return mid if mid.startswith("openrouter/") else "openrouter/" + mid


def openrouter_token_from_keys(keys_json, account):
    """Mirror of the runner-side lookup in step 12b: the token for
    `account` in an OPENROUTER_KEYS JSON object, else None. Malformed
    JSON, a non-object, or a non-string/empty token all yield None."""
    try:
        keys = json.loads(keys_json or "")
    except Exception:
        return None
    if not isinstance(keys, dict):
        return None
    token = keys.get(account)
    return token if isinstance(token, str) and token else None


def openrouter_keys_json(accounts):
    """The OPENROUTER_KEYS secret content for the configured
    accounts: {name: token}. Only accounts with both a name and a
    token are included; callers copy this to paste into GitHub."""
    return json.dumps(
        {a["name"]: a["token"]
         for a in normalize_openrouter_accounts(accounts)
         if a["token"]},
        indent=2, ensure_ascii=False)


def openrouter_dispatch_inputs(choice):
    """workflow_dispatch inputs for a Run choice: None (run as-is)
    -> no inputs at all; (account, model) -> only the account NAME
    and the model slug. The token is NEVER an input."""
    if not choice:
        return {}
    account, model = choice
    return {"openrouter_account": account, "openrouter_model": model}


def _account_name_for_instance(wf_name, names):
    """Account NAME for one machine, by its trailing instance
    number: machine 1 -> the 1st name in list order, machine 2 ->
    the 2nd, and so on, WRAPPING modulo the count when there are
    more machines than accounts (machine 4 with 3 accounts uses
    the 1st again). A name with no trailing number uses the 1st
    name. None when no names are configured."""
    if not names:
        return None
    m = re.search(r"(\d+)$", wf_name or "")
    n = int(m.group(1)) if m else 1
    return names[(n - 1) % len(names)]


def openrouter_account_for_instance(wf_name, accounts):
    """Account NAME for one machine, by its trailing instance
    number: machine 1 -> the 1st account in settings order,
    machine 2 -> the 2nd, and so on, WRAPPING modulo the account
    count when there are more machines than accounts (machine 4
    with 3 accounts uses the 1st again). A name with no trailing
    number uses the 1st account. None when no accounts are
    configured. Only the account NAME is ever returned — tokens
    never leave settings.json through this app."""
    return _account_name_for_instance(
        wf_name, openrouter_account_names(accounts))


def openrouter_dispatch_inputs_for_machine(choice, wf, accounts):
    """workflow_dispatch inputs for ONE machine. choice is None /
    "asis" -> {}; ("per-machine", model) or ("openrouter", model)
    -> the account resolved for wf's instance number + the model,
    or {} when no account resolves (the caller must NOT dispatch
    plain in that case — a silent Zen fallback would hide the
    misconfiguration); a legacy (account, model) tuple -> that
    account + model."""
    if not choice or choice == "asis":
        return {}
    if isinstance(choice, tuple) and len(choice) == 2 \
            and choice[0] in ("per-machine", "openrouter"):
        account = openrouter_account_for_instance(wf, accounts)
        if account is None:
            return {}
        return {"openrouter_account": account, "openrouter_model": choice[1]}
    return openrouter_dispatch_inputs(choice)


# ---------------------------------------------------------------------------
# NVIDIA accounts (per-run provider choice at Run time)
# ---------------------------------------------------------------------------
# Mirrors the OpenRouter accounts above: same settings shape, same
# per-machine mapping, same JSON-secret delivery. The one difference
# is the model list: NVIDIA's catalog needs an API key, so the Run
# dialog fetches it with the FIRST NVIDIA account's token (from this
# machine, to NVIDIA's API only — never logged, never sent
# anywhere else). The catalog lists paid models too with no free
# mark, so the list is shown as-is and manual typing stays
# available; a non-free id fails visibly at call time.

NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
NVIDIA_KEYS_SECRET = "NVIDIA_KEYS"


def blank_nvidia_account():
    return {"name": "", "token": ""}


def normalize_nvidia_accounts(accounts):
    """Settings-shape NVIDIA accounts: drop entries with no name,
    trim name/token, keep the FIRST entry per name. Tokens are never
    logged or echoed anywhere in this app."""
    seen, out = set(), []
    for a in accounts or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name,
                    "token": str(a.get("token", "")).strip()})
    return out


def nvidia_account_names(accounts):
    return [a["name"] for a in normalize_nvidia_accounts(accounts)]


def nvidia_model_ids(payload):
    """Model ids from an NVIDIA /v1/models payload (OpenAI-
    compatible list shape: {"data": [{"id": ...}]}). Sorted,
    de-duplicated. Entries without a string id are skipped; a
    missing/non-list `data` yields []. No free filter is applied:
    the catalog does not mark free endpoints."""
    ids = set()
    data = payload.get("data") if isinstance(payload, dict) else None
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if isinstance(mid, str) and mid:
            ids.add(mid)
    return sorted(ids, key=str.casefold)


def fetch_nvidia_models(token, urlopen=None, timeout=10):
    """NVIDIA model ids, live from the NIM catalog, authenticated
    with the given token (the first configured account's). [] on
    ANY failure (no/blank token, network, timeout, bad payload) —
    and with a blank token the opener is never called. The Run
    dialog then offers manual model entry instead of blocking.
    The token is sent only to NVIDIA's API, in the Authorization
    header, and is never logged."""
    if not isinstance(token, str) or not token.strip():
        return []
    opener = urlopen or urllib.request.urlopen
    try:
        req = urllib.request.Request(
            NVIDIA_MODELS_URL,
            headers={"User-Agent": "Remote-Op",
                     "Authorization": "Bearer " + token.strip()})
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return nvidia_model_ids(payload)
    except Exception:
        return []


def normalize_nvidia_model(raw):
    """OpenCode model REF for a chosen NVIDIA NIM id: "nvidia/" +
    the NIM id as listed (e.g. nvidia/nemotron-3-ultra-550b-a55b
    -> nvidia/nvidia/nemotron-3-ultra-550b-a55b, because the NIM
    id itself contains a slash). A value already in that triple
    form is returned unchanged; anything else is taken as the NIM
    id as-is. "" stays "" (no choice)."""
    mid = (raw or "").strip()
    if not mid:
        return ""
    if mid.startswith("nvidia/nvidia/"):
        return mid
    return "nvidia/" + mid


def nvidia_token_from_keys(keys_json, account):
    """Mirror of the runner-side lookup in step 12c: the token for
    `account` in a NVIDIA_KEYS JSON object, else None. Malformed
    JSON, a non-object, or a non-string/empty token all yield None."""
    try:
        keys = json.loads(keys_json or "")
    except Exception:
        return None
    if not isinstance(keys, dict):
        return None
    token = keys.get(account)
    return token if isinstance(token, str) and token else None


def nvidia_keys_json(accounts):
    """The NVIDIA_KEYS secret content for the configured accounts:
    {name: token}. Only accounts with both a name and a token are
    included; callers copy this to paste into GitHub."""
    return json.dumps(
        {a["name"]: a["token"]
         for a in normalize_nvidia_accounts(accounts)
         if a["token"]},
        indent=2, ensure_ascii=False)


def nvidia_dispatch_inputs(choice):
    """workflow_dispatch inputs for an NVIDIA Run choice: None
    (run as-is) -> no inputs at all; (account, model) -> only the
    account NAME and the model id. The token is NEVER an input."""
    if not choice:
        return {}
    account, model = choice
    return {"nvidia_account": account, "nvidia_model": model}


def nvidia_account_for_instance(wf_name, accounts):
    """Account NAME for one machine, by its trailing instance
    number — the same rule as OpenRouter (machine 1 -> 1st
    account, wrapping modulo the count). None when no accounts
    are configured. Only the NAME is ever returned."""
    return _account_name_for_instance(
        wf_name, nvidia_account_names(accounts))


def nvidia_dispatch_inputs_for_machine(choice, wf, accounts):
    """workflow_dispatch inputs for ONE machine. choice is None /
    "asis" -> {}; ("nvidia", model) -> the account resolved for
    wf's instance number + the model, or {} when no account
    resolves (the caller must NOT dispatch plain in that case — a
    silent Zen fallback would hide the misconfiguration); a legacy
    (account, model) tuple -> that account + model."""
    if not choice or choice == "asis":
        return {}
    if isinstance(choice, tuple) and len(choice) == 2 \
            and choice[0] == "nvidia":
        account = nvidia_account_for_instance(wf, accounts)
        if account is None:
            return {}
        return {"nvidia_account": account, "nvidia_model": choice[1]}
    return nvidia_dispatch_inputs(choice)


# Custom presets (per-run provider choice at Run time)
# ---------------------------------------------------------------------------
# Named presets for free tokens from various providers: each preset
# carries its own base URL + default model id + token. At Run time
# ONE preset is picked for the whole Run action (every machine uses
# it) and the model may still be changed there — the preset's model
# is only the default. Same delivery as OpenRouter/NVIDIA: the token
# is NEVER sent in the dispatch — the runner reads it from the one
# JSON CUSTOM_KEYS secret per fleet repo (preset name -> token) in
# step 12d — and §4 writes a `custom` provider block
# (OpenAI-compatible, the preset's baseURL) for that run only.
# Only OpenAI-compatible endpoints are supported in v1. The model
# list is fetched live from the preset's own base URL with its own
# token (to that base URL only, never logged); a fetch failure
# falls back to manual entry.

CUSTOM_KEYS_SECRET = "CUSTOM_KEYS"


def blank_custom_preset():
    return {"name": "", "base_url": "", "model": "", "token": ""}


def normalize_custom_presets(presets):
    """Settings-shape Custom presets: drop entries with no name,
    trim name/base_url/model/token, keep the FIRST entry per name.
    Tokens are never logged or echoed anywhere in this app."""
    seen, out = set(), []
    for a in presets or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name,
                    "base_url": str(a.get("base_url", "")).strip(),
                    "model": str(a.get("model", "")).strip(),
                    "token": str(a.get("token", "")).strip()})
    return out


def custom_preset_names(presets):
    return [a["name"] for a in normalize_custom_presets(presets)]


def custom_preset_by_name(presets, name):
    """One preset by name, or None. Only the entry is returned —
    callers never log its token."""
    wanted = (name or "").strip()
    for a in normalize_custom_presets(presets):
        if a["name"] == wanted:
            return a
    return None


def custom_models_url(base_url):
    """<base>/v1/models for a preset's base URL: trailing slashes
    stripped; a URL already ending in /v1/models is kept, one
    ending in /v1 gains /models, anything else gains /v1/models.
    A URL already ending in a version segment (/v2, /v4, ...) gains
    only /models, so a non-/v1 base like Z.ai's .../paas/v4 is kept.
    "" for a blank base URL (the caller then never fetches)."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1/models"):
        return base
    if base.endswith("/v1"):
        return base + "/models"
    _tail = base.rsplit("/", 1)[-1]
    if _tail.startswith("v") and _tail[1:].isdigit():
        return base + "/models"
    return base + "/v1/models"


def custom_provider_base_url(base_url):
    """Provider baseURL for a preset's base URL: trailing slashes
    stripped; a trailing /v1/models is trimmed to /v1; /v1 is
    appended unless already there. A trailing version segment
    (/v2, /v4, ...) is kept as-is, so a non-/v1 base like Z.ai's
    .../paas/v4 is not mangled to .../v4/v1. "" for a blank base
    URL. This mirrors the normalization §4 applies on the runner."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1/models"):
        base = base[:-len("/models")]
    if base.endswith("/v1"):
        return base
    _tail = base.rsplit("/", 1)[-1]
    if _tail.startswith("v") and _tail[1:].isdigit():
        return base
    return base + "/v1"


def custom_model_ids(payload):
    """Model ids from a /v1/models payload (OpenAI-compatible list
    shape: {"data": [{"id": ...}]}). Sorted, de-duplicated.
    Entries without a string id are skipped; a missing/non-list
    `data` yields []. No free filter is applied: the preset's
    provider does not mark free endpoints."""
    ids = set()
    data = payload.get("data") if isinstance(payload, dict) else None
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        if isinstance(mid, str) and mid:
            ids.add(mid)
    return sorted(ids, key=str.casefold)


def fetch_custom_models(base_url, token, urlopen=None, timeout=10):
    """Model ids, live from a preset's own base URL, authenticated
    with that preset's token. [] on ANY failure (no/blank base URL
    or token, network, timeout, bad payload) — and with a blank
    base URL or token the opener is never called. The Run dialog
    then offers manual model entry instead of blocking. The token
    is sent only to the preset's base URL, in the Authorization
    header, and is never logged."""
    if not isinstance(token, str) or not token.strip():
        return []
    url = custom_models_url(base_url)
    if not url:
        return []
    opener = urlopen or urllib.request.urlopen
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Remote-Op",
                     "Authorization": "Bearer " + token.strip()})
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return custom_model_ids(payload)
    except Exception:
        return []


def normalize_custom_model(raw):
    """OpenCode model REF for a chosen Custom model id: "custom/" +
    the id as listed (e.g. vendor/model -> custom/vendor/model).
    A value already in that form is returned unchanged; anything
    else is taken as the model id as-is. "" stays "" (no choice)."""
    mid = (raw or "").strip()
    if not mid:
        return ""
    if mid.startswith("custom/"):
        return mid
    return "custom/" + mid


def custom_token_from_keys(keys_json, preset):
    """Mirror of the runner-side lookup in step 12d: the token for
    `preset` in a CUSTOM_KEYS JSON object, else None. Malformed
    JSON, a non-object, or a non-string/empty token all yield None."""
    try:
        keys = json.loads(keys_json or "")
    except Exception:
        return None
    if not isinstance(keys, dict):
        return None
    token = keys.get(preset)
    return token if isinstance(token, str) and token else None


def custom_keys_json(presets):
    """The CUSTOM_KEYS secret content for the configured presets:
    {name: token}. Only presets with both a name and a token are
    included; callers copy this to paste into GitHub."""
    return json.dumps(
        {a["name"]: a["token"]
         for a in normalize_custom_presets(presets)
         if a["token"]},
        indent=2, ensure_ascii=False)


def custom_dispatch_inputs(choice, presets):
    """workflow_dispatch inputs for a Custom Run choice: None /
    "asis" -> {}; ("custom", preset, model) -> the preset NAME, its
    (normalized) base URL and the model. The token is NEVER an
    input. {} when the preset is unknown in settings, its base URL
    is empty, or the model is empty — the caller must NOT dispatch
    plain in that case (a silent Zen fallback would hide the
    misconfiguration)."""
    if not choice or choice == "asis":
        return {}
    if isinstance(choice, tuple) and len(choice) == 3 \
            and choice[0] == "custom":
        _tag, preset, model = choice
        entry = custom_preset_by_name(presets, preset)
        if entry is None:
            return {}
        base_url = custom_provider_base_url(entry.get("base_url", ""))
        if not base_url or not (model or "").strip():
            return {}
        return {"custom_preset": preset,
                "custom_base_url": base_url,
                "custom_model": (model or "").strip()}
    return {}


def move_custom_preset(presets, idx, delta):
    """Same list-order move as move_openrouter_account, for the
    Custom presets: list order is the order presets are offered
    in the Run dialog (the first is the default pick)."""
    return _move_account_entry(presets, idx, delta)


# ---------------------------------------------------------------------------
# OpenCode accounts (the DEFAULT provider at Run time)
# ---------------------------------------------------------------------------
# Mirrors the OpenRouter accounts above: same settings shape, same
# per-machine mapping, same JSON-secret delivery. The difference is
# that this is the DEFAULT, not a per-run pick: a plain dispatch
# (the "OpenCode (Zen — current setup)" radio, pre-selected in the
# Run dialog) uses each machine's per-instance Zen model — the
# model is NEVER picked here — and only the AUTH comes from the
# account: machine N uses the Nth account's key, automatically.
# The token is NEVER sent in the dispatch — the runner reads it
# from the one JSON OPENCODE_KEYS secret per fleet repo in step
# 12e, which exports OPENCODE_API_KEY for the built-in opencode
# provider (no provider block, no model input). 9router is a
# separate explicit Run choice now, not the default.

OPENCODE_KEYS_SECRET = "OPENCODE_KEYS"


def blank_opencode_account():
    return {"name": "", "token": ""}


def normalize_opencode_accounts(accounts):
    """Settings-shape OpenCode accounts: drop entries with no name,
    trim name/token, keep the FIRST entry per name. Tokens are never
    logged or echoed anywhere in this app."""
    seen, out = set(), []
    for a in accounts or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name,
                    "token": str(a.get("token", "")).strip()})
    return out


def opencode_account_names(accounts):
    return [a["name"] for a in normalize_opencode_accounts(accounts)]


def move_opencode_account(accounts, idx, delta):
    """Same list-order move as move_openrouter_account, for the
    OpenCode accounts: list order IS the machine mapping."""
    return _move_account_entry(accounts, idx, delta)


def opencode_token_from_keys(keys_json, account):
    """Mirror of the runner-side lookup in step 12e: the token for
    `account` in an OPENCODE_KEYS JSON object, else None. Malformed
    JSON, a non-object, or a non-string/empty token all yield None."""
    try:
        keys = json.loads(keys_json or "")
    except Exception:
        return None
    if not isinstance(keys, dict):
        return None
    token = keys.get(account)
    return token if isinstance(token, str) and token else None


def opencode_keys_json(accounts):
    """The OPENCODE_KEYS secret content for the configured
    accounts: {name: token}. Only accounts with both a name and a
    token are included; callers copy this to paste into GitHub."""
    return json.dumps(
        {a["name"]: a["token"]
         for a in normalize_opencode_accounts(accounts)
         if a["token"]},
        indent=2, ensure_ascii=False)


def opencode_account_for_instance(wf_name, accounts):
    """Account NAME for one machine, by its trailing instance
    number — the same rule as OpenRouter (machine 1 -> 1st
    account, wrapping modulo the count). None when no accounts
    are configured. Only the NAME is ever returned."""
    return _account_name_for_instance(
        wf_name, opencode_account_names(accounts))


def opencode_dispatch_inputs(choice):
    """workflow_dispatch inputs for an OpenCode Run choice: None
    (run as-is) -> no inputs at all; ("opencode", account) -> only
    the account NAME. The token is NEVER an input, and there is no
    model input — the model stays the per-instance Zen model."""
    if not choice:
        return {}
    account = choice[1] if isinstance(choice, tuple) \
        and len(choice) == 2 else None
    return {"opencode_account": account} if account else {}


def opencode_dispatch_inputs_for_machine(choice, wf, accounts):
    """workflow_dispatch inputs for ONE machine. choice is None /
    "asis" -> {}; ("opencode",) -> the account resolved for wf's
    instance number, or {} when no account resolves (the caller
    must NOT dispatch in that case — no silent 9router fallback
    hides the misconfiguration); ("opencode", account) -> that
    account."""
    if not choice or choice == "asis":
        return {}
    if isinstance(choice, tuple) and choice and choice[0] == "opencode":
        if len(choice) == 1:
            account = opencode_account_for_instance(wf, accounts)
            if account is None:
                return {}
            return {"opencode_account": account}
        return opencode_dispatch_inputs(choice)
    return {}


def nine_router_dispatch_inputs():
    """workflow_dispatch inputs for the explicit 9router Run
    choice: the nine_router flag only — fixed 9router/code, no
    account, no model. The 9router key still comes from the job
    env (HERMES_CUSTOM_API_KEY secret)."""
    return {"nine_router": "true"}


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
            "hide_down": bool(data.get("hide_down"))
            if isinstance(data, dict) else False,
            "openrouter_accounts": normalize_openrouter_accounts(
                data.get("openrouter_accounts"))
            if isinstance(data, dict) else [],
            "nvidia_accounts": normalize_nvidia_accounts(
                data.get("nvidia_accounts"))
            if isinstance(data, dict) else [],
            "custom_presets": normalize_custom_presets(
                data.get("custom_presets"))
            if isinstance(data, dict) else [],
            "opencode_accounts": normalize_opencode_accounts(
                data.get("opencode_accounts"))
            if isinstance(data, dict) else []}


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

    def list_workflow_files(self, repo, branch):
        """Every .yml file in .github/workflows: {file: {sha, text, wf}}.

        Unlike list_instances this does not filter by instance name —
        Update must see every workflow file, including one whose
        WF_NAME is not a <prefix><number> instance name. Files with
        no WF_NAME come back with wf=None and are never overwritten.
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
            out[fname] = {"sha": f["sha"], "text": text,
                          "wf": workflow_instance(text)}
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
        runs, _ok = self.list_runs_ok(repo, per_page)
        return runs

    def list_runs_ok(self, repo, per_page=100):
        """(runs, ok): like list_runs, but ok is False when the API
        call itself failed — so a caller can tell a real empty list
        from a failed fetch and keep its old data."""
        code, d = self.api("GET", f"repos/{repo}/actions/runs?per_page={per_page}")
        if code != 200 or not isinstance(d, dict):
            return [], False
        return d.get("workflow_runs", []), True

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

    def dispatch(self, repo, branch, workflow_file, inputs=None):
        body = {"ref": branch}
        if inputs:
            body["inputs"] = dict(inputs)
        code, _ = self.api("POST", f"repos/{repo}/actions/workflows/{workflow_file}/dispatches",
                           body)
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
# Delete old runs — fleet-wide (ALL accounts/repos at once)
# ---------------------------------------------------------------------------

def fetch_old_runs_all(targets, gh_factory, repo_of):
    """Completed runs of EVERY target, fetched IN PARALLEL.

    `targets` is the app's configured accounts/repos, `gh_factory`
    builds a GH client from a target's PAT, `repo_of` extracts the
    hub/repo slug. One dict per target, IN TARGET ORDER:
    {"target", "label", "repo", "runs", "error"}. A target whose
    fetch raises lands with runs=[] and its error set — it never
    stops the others (a dead token must not block the cleanup)."""
    def one(t):
        try:
            gh = gh_factory(t.get("pat"))
            runs = gh.list_completed_runs(repo_of(t)) or []
            return {"target": t, "label": t.get("label") or repo_of(t),
                    "repo": repo_of(t), "runs": list(runs), "error": None}
        except Exception as e:
            return {"target": t, "label": t.get("label") or repo_of(t),
                    "repo": repo_of(t), "runs": [], "error": str(e)}
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
        return list(ex.map(one, targets))


def old_runs_totals(collected):
    """(grand total, entries that have runs, entries that failed)."""
    return (sum(len(c["runs"]) for c in collected),
            [c for c in collected if c["runs"]],
            [c for c in collected if c.get("error")])


def delete_old_runs_all(collected, gh_factory, log):
    """Delete every run in `collected` — IN PARALLEL across targets
    and within each target (6 workers each). Logs one line per
    target as it finishes; returns the (deleted, failed) grand
    totals. One target's failures never stop the others."""
    def del_one_target(c):
        gh = gh_factory(c["target"].get("pat"))

        def one(r):
            try:
                return bool(gh.delete_run(c["repo"], r["id"]))
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(one, c["runs"]))
        ok = sum(results)
        fail = len(results) - ok
        log(f"{c['label']}: old runs deleted: {ok}"
            + (f", FAILED: {fail}" if fail else ""))
        return ok, fail

    todo = [c for c in collected if c["runs"]]
    if not todo:
        return 0, 0
    with ThreadPoolExecutor(max_workers=min(4, len(todo))) as ex:
        results = list(ex.map(del_one_target, todo))
    return (sum(r[0] for r in results), sum(r[1] for r in results))


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
    over — file/data operations must not race a running job.
    Bounded wait (30 x 4s polls, short per-poll timeout): an
    unconfirmed stop aborts THIS machine's operation instead of
    holding the app hostage. True when clear."""
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


def create_instances(gh, cfg, count, log, prefix_hint=None):
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

    New machines always start raw (clean state), created by the
    instance's own first sync: machines never sync state back to the
    repo, so there is no backed-up state anywhere to copy. (Plugins,
    skills, agents and MCP config need no copying: they live in the
    SHARED opencode/config every instance already uses.)
    """
    repo, branch = cfg["repo"], cfg["branch"]
    inst = gh.list_instances(repo, branch)
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

    def entries(_blobs):
        out = [{"path": f".github/workflows/{name}.yml", "mode": "100644",
                "type": "blob", "sha": blob_sha[name]} for name in names]
        return out

    msg = (f"Add {names[0]} workflow" if count == 1 else
           f"Add {count} instances from {template_name}: "
           f"{names[0]}..{names[-1]}")
    if not gh.commit_tree_change(repo, branch, msg, entries, log):
        log("create FAILED — no instances were committed")
        return []
    for name in names:
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


def plan_update(gh, cfg, log, only_wfs=None):
    """What an Update would change in one repo, fetched live.

    Returns {"changed": [(wf, file, rendered)], "unchanged": [wf],
    "skipped": [(file, reason)]}. `only_wfs` limits the plan to
    those instance names (Update checked); None means every
    workflow file. Files with no WF_NAME are skipped, never
    overwritten. Nothing is committed here — the caller confirms
    first, then apply_update commits the plan."""
    repo, branch = cfg["repo"], cfg["branch"]
    files = gh.list_workflow_files(repo, branch)
    changed, unchanged, skipped = [], [], []
    for fname in sorted(files):
        info = files[fname]
        wf = info["wf"]
        if not wf:
            skipped.append((fname, "no WF_NAME in the file"))
            continue
        if only_wfs is not None and wf not in only_wfs:
            continue
        rendered = embedded_template(wf, repo)
        if rendered == info["text"]:
            unchanged.append(wf)
        else:
            changed.append((wf, fname, rendered))
    return {"changed": changed, "unchanged": unchanged,
            "skipped": skipped}


def apply_update(gh, cfg, plan, log):
    """Commit a plan_update result: one delta commit per repo,
    only the files that actually change. Returns True when the
    commit landed (or there was nothing to change)."""
    repo, branch = cfg["repo"], cfg["branch"]
    changed = plan["changed"]
    if not changed:
        return True
    blob_sha = {}

    def make_blob(item):
        wf, _fname, rendered = item
        blob_sha[wf] = gh.create_blob(repo, rendered)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(make_blob, changed))
    if any(not s for s in blob_sha.values()):
        log("update aborted: a blob upload FAILED (nothing committed)")
        return False

    def entries(_blobs):
        return [{"path": f".github/workflows/{fname}", "mode": "100644",
                 "type": "blob", "sha": blob_sha[wf]}
                for wf, fname, _r in changed]

    names = ", ".join(wf for wf, _f, _r in changed)
    msg = (f"Update {names} workflow(s) from the Remote-Op embedded "
           f"template (backup sync-back step removed)")
    if not gh.commit_tree_change(repo, branch, msg, entries, log):
        log("update FAILED — the commit did not land")
        return False
    for wf, fname, _r in changed:
        log(f"✅ {wf} updated ({fname}) — takes effect on the next "
            f"dispatch; a running job keeps the old form")
    return True


def update_repo_workflows(gh, cfg, log, only_wfs=None):
    """Re-render a repo's workflow files from the embedded template
    and commit the changes (one delta commit). Returns
    (updated, unchanged, skipped) counts. Running jobs are NOT
    cancelled: a job uses the file snapshot from its dispatch, so
    the new form applies from the next dispatch on."""
    plan = plan_update(gh, cfg, log, only_wfs)
    for fname, reason in plan["skipped"]:
        log(f"{fname}: skipped ({reason})")
    for wf in plan["unchanged"]:
        log(f"{wf}: already on the embedded template")
    if not plan["changed"]:
        return 0, len(plan["unchanged"]), len(plan["skipped"])
    if not apply_update(gh, cfg, plan, log):
        return 0, len(plan["unchanged"]), len(plan["skipped"])
    return len(plan["changed"]), len(plan["unchanged"]), len(plan["skipped"])


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
        self._runs_refresh_failed = set()  # labels whose last loop
        # run-refresh failed (logged once until it recovers)
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
        ttk.Button(top, text="OpenCode accounts…", command=self._opencode_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="OpenRouter accounts…", command=self._openrouter_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="NVIDIA accounts…", command=self._nvidia_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Custom presets…", command=self._custom_dialog).pack(side=tk.LEFT, padx=2)
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
        self.btn_upd = ttk.Button(btns, text="⬆ Update checked", command=self.update_checked)
        self.btn_upd.pack(side=tk.LEFT, padx=2)
        self.btn_upd_all = ttk.Button(btns, text="⬆⬆ Update all", command=self.update_all)
        self.btn_upd_all.pack(side=tk.LEFT, padx=2)
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
        ttk.Button(util, text="🧹 Delete old runs (all)", command=self.delete_old_runs).pack(side=tk.LEFT, padx=2)
        self.hide_down_var = tk.BooleanVar(
            value=bool(self.settings.get("hide_down")))
        ttk.Checkbutton(util, text="Hide down",
                        variable=self.hide_down_var,
                        command=self._toggle_hide_down).pack(side=tk.LEFT, padx=6)
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
                      self.btn_rename, self.btn_upd, self.btn_upd_all):
                b.state(["disabled"])
        self.root.after(0, ui)

    def _op_done(self, text, ok=True):
        def ui():
            self.op_var.set(("✅ " if ok else "⚠️ ") + text)
            self.op_label.config(fg="#166534" if ok else "#991b1b",
                                 bg="#dcfce7" if ok else "#fee2e2")
            for b in (self.btn_add, self.btn_bulk, self.btn_del,
                      self.btn_rename, self.btn_upd, self.btn_upd_all):
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

    def _toggle_hide_down(self):
        self.settings["hide_down"] = bool(self.hide_down_var.get())
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
                srv_state = self._server_state(label, wf)
                if (self.settings.get("hide_down")
                        and srv_state != "ready"):
                    hidden += 1
                    continue
                shown = status if status != "completed" else (run.get("conclusion") or "completed")
                started, remaining, tag = "—", "—", ""
                if run.get("created_at"):
                    started = datetime.fromisoformat(
                        run["created_at"].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
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
                            + (f", {hidden} down hidden" if hidden else "")
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
    # a settled fleet is probed every 5 minutes. The loop also
    # re-fetches run statuses each cycle: a run that ended since the
    # last refresh (a stopped machine's run still reads in_progress
    # while the runner shuts down) must not keep its row orange
    # forever — the probe reads the same runs the tree shows.
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

    def _refresh_runs(self):
        """Re-fetch every repo's runs (runs-only, no instances) so
        the probe — and the tree — stop trusting a stale run status
        after a run ends. A failed repo keeps its old runs; the
        failure is logged once until it recovers. Runs are replaced
        in one assignment, so the UI thread never sees a half-new
        dict."""
        new_runs = dict(self.runs)
        for cfg in list(self.settings["repos"]):
            label = cfg["label"]
            inst = self.instances_of(label)
            if not inst:
                continue
            runs, ok = GH(cfg["pat"]).list_runs_ok(cfg["repo"])
            if not ok:
                if label not in self._runs_refresh_failed:
                    self._runs_refresh_failed.add(label)
                    self.log(f"{label}: run refresh FAILED — keeping "
                             f"last known runs")
                continue
            if label in self._runs_refresh_failed:
                self._runs_refresh_failed.discard(label)
                self.log(f"{label}: run refresh OK again")
            for k in [k for k in new_runs if k[0] == label]:
                del new_runs[k]
            new_runs.update(map_runs_to_workflows(runs, inst, label))
        self.runs = new_runs

    def _check_uptime(self):
        # Runs first: a run that ended since the last refresh must
        # not keep its row orange — the probe below derives its
        # verdict from these runs.
        self._refresh_runs()
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
    def _dispatch(self, cfg, wf, choice=None):
        info = self.instances.get((cfg["label"], wf))
        if not info:
            return
        run = self.runs.get((cfg["label"], wf), {})
        if run.get("status") in ("in_progress", "queued"):
            self.log(f"{cfg['label']}/{wf}: already {run['status']} — skipped")
            return
        or_accounts = self.settings.get("openrouter_accounts", [])
        nv_accounts = self.settings.get("nvidia_accounts", [])
        oc_accounts = self.settings.get("opencode_accounts", [])
        cu_presets = normalize_custom_presets(
            self.settings.get("custom_presets", []))
        is_custom = isinstance(choice, tuple) and bool(choice) \
            and choice[0] == "custom"
        is_nvidia = isinstance(choice, tuple) and bool(choice) \
            and choice[0] == "nvidia"
        is_opencode = isinstance(choice, tuple) and bool(choice) \
            and choice[0] == "opencode"
        is_9router = isinstance(choice, tuple) and bool(choice) \
            and choice[0] == "9router"
        if is_custom:
            inputs = custom_dispatch_inputs(choice, cu_presets)
        elif is_nvidia:
            inputs = nvidia_dispatch_inputs_for_machine(
                choice, wf, nv_accounts)
        elif is_opencode:
            inputs = opencode_dispatch_inputs_for_machine(
                choice, wf, oc_accounts)
        elif is_9router:
            inputs = nine_router_dispatch_inputs()
        else:
            inputs = openrouter_dispatch_inputs_for_machine(
                choice, wf, or_accounts)
        if isinstance(choice, tuple) and choice and not inputs:
            if is_custom:
                self.log(f"{cfg['label']}/{wf}: no Custom preset "
                         f"configured — add presets in 'Custom "
                         f"presets…' or run as-is; NOT dispatched")
            elif is_opencode:
                self.log(f"{cfg['label']}/{wf}: no OpenCode account "
                         f"configured — add accounts in 'OpenCode "
                         f"accounts…' or pick another provider; "
                         f"NOT dispatched")
            else:
                provider = "NVIDIA" if is_nvidia else "OpenRouter"
                self.log(f"{cfg['label']}/{wf}: no {provider} account "
                         f"configured — add accounts in '{provider} "
                         f"accounts…' or run as-is; NOT dispatched")
            return
        if GH(cfg["pat"]).dispatch(cfg["repo"], cfg["branch"], info["file"], inputs):
            if "nine_router" in inputs:
                self.log(f"{cfg['label']}/{wf}: dispatched "
                         f"(9router: 9router/code)")
            elif inputs:
                account = inputs.get("nvidia_account") \
                    or inputs.get("openrouter_account") \
                    or inputs.get("custom_preset") \
                    or inputs.get("opencode_account")
                model = inputs.get("nvidia_model") \
                    or inputs.get("openrouter_model") \
                    or inputs.get("custom_model") \
                    or "Zen (per-instance)"
                provider = "Custom" if "custom_preset" in inputs \
                    else ("NVIDIA" if "nvidia_account" in inputs
                          else ("OpenCode" if "opencode_account" in inputs
                                else "OpenRouter"))
                self.log(f"{cfg['label']}/{wf}: dispatched "
                         f"({provider}: {account} / {model})")
            else:
                self.log(f"{cfg['label']}/{wf}: dispatched")
        else:
            self.log(f"{cfg['label']}/{wf}: dispatch FAILED")

    def _dispatch_many(self, targets, choice=None):
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda t: self._dispatch(*t, choice), targets))
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
        picked = self._provider_run_dialog(targets)
        if picked is None:
            self.log("run cancelled — nothing dispatched")
            return
        choice = picked

        def work():
            self._dispatch_many(targets, choice)
        self.bg(work)

    def run_all(self):
        cfg = self.scope_repo()
        if not cfg:
            return
        targets = [(cfg, wf) for wf in self.instances_of(cfg["label"])]
        if not targets:
            self.log(f"{cfg['label']}: no machines loaded")
            return
        picked = self._provider_run_dialog(targets)
        if picked is None:
            self.log("run cancelled — nothing dispatched")
            return
        choice = picked

        def work():
            self._dispatch_many(targets, choice)
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
        """Clear the completed Actions runs of EVERY configured
        repo at once — not just the selected repo's (Mehdi's ask).
        Machines and state are untouched; runs still in progress
        or queued are kept. One confirmation covers the whole
        fleet, with the per-repo breakdown."""
        repos = list(self.settings["repos"])
        if not repos:
            return

        def work():
            collected = fetch_old_runs_all(repos, GH,
                                           lambda c: c["repo"])
            # The confirmation needs the counts first, and messagebox
            # must run on the UI thread.
            self.root.after(0, lambda: self._confirm_delete_runs(collected))
        self.bg(work)

    def _confirm_delete_runs(self, collected):
        for c in collected:
            if c.get("error"):
                self.log(f"{c['label']}: could not list old runs "
                         f"({c['error']}) — skipped")
        total, todo, _failed = old_runs_totals(collected)
        if not total:
            self.log("delete old runs: no completed runs to delete "
                     "in any repo")
            return
        breakdown = "\n".join(
            f"  {c['label']} ({c['repo']}): {len(c['runs'])}"
            for c in todo)
        if not messagebox.askyesno(
                "Delete old runs",
                f"Delete {total} completed Actions run(s) from "
                f"GitHub's history, across ALL {len(todo)} repo(s)?\n\n"
                f"{breakdown}\n\n"
                f"Machines and their state are untouched; runs still "
                f"in progress or queued are kept."):
            return

        def work():
            if not self._acquire_op(
                    f"delete old runs (all {len(todo)} repos, "
                    f"{total} runs)"):
                return
            outcome = [f"delete old runs: {total} run(s) deleted, "
                       f"all repos", True]
            try:
                ok, fail = delete_old_runs_all(collected, GH, self.log)
                self.log(f"delete old runs: finished — {ok} deleted"
                         + (f", {fail} FAILED" if fail else "")
                         + f" across {len(todo)} repo(s)")
                if fail:
                    outcome[:] = [f"delete old runs: {ok} deleted, "
                                  f"{fail} FAILED — see log", False]
                else:
                    outcome[:] = [f"delete old runs: {ok} run(s) "
                                  f"deleted, all repos", True]
            finally:
                self._release_op()
                self._op_done(outcome[0], outcome[1])
            self.refresh()
        self.bg(work)

    # -- update workflows from the embedded template -------------------------
    def update_checked(self):
        """Re-render the CHECKED machines' workflow files from the
        embedded template (one delta commit per repo)."""
        by_repo = {}
        for label, wf in sorted(self.checked):
            cfg = self.repo_by_label(label)
            if cfg:
                by_repo.setdefault(label, (cfg, []))[1].append(wf)
        if not by_repo:
            self.log("nothing checked — tick the machines to update first")
            return
        targets = [(cfg, sorted(names))
                   for _lbl, (cfg, names) in sorted(by_repo.items())]
        self._start_update(targets, "checked")

    def update_all(self):
        """Re-render EVERY workflow file in EVERY configured repo
        from the embedded template (one delta commit per repo)."""
        repos = list(self.settings["repos"])
        if not repos:
            return
        self._start_update([(cfg, None) for cfg in repos], "all")

    def _start_update(self, targets, scope):
        """Fetch the live plan for `targets` [(cfg, only_wfs|None)],
        confirm on the UI thread, then commit behind the op lock.
        The plan fetch runs in the background; messagebox must run
        on the UI thread, so confirmation is scheduled with after()."""
        def work():
            plans = []
            for cfg, only in targets:
                try:
                    plans.append((cfg, plan_update(
                        GH(cfg["pat"]), cfg, self.log, only)))
                except Exception as e:
                    self.log(f"{cfg['label']}: update plan FAILED ({e})")
            self.root.after(0, lambda: self._confirm_update(plans, scope))
        self.bg(work)

    def _confirm_update(self, plans, scope):
        total_changed = sum(len(p["changed"]) for _c, p in plans)
        total_unchanged = sum(len(p["unchanged"]) for _c, p in plans)
        for cfg, plan in plans:
            for fname, reason in plan["skipped"]:
                self.log(f"{cfg['label']}/{fname}: skipped ({reason})")
        if not total_changed:
            self.log(f"update ({scope}): nothing to change — "
                     f"{total_unchanged} workflow file(s) already on "
                     f"the embedded template")
            return
        detail = "\n".join(
            f"• {cfg['label']} ({cfg['repo']}): "
            f"{', '.join(wf for wf, _f, _r in plan['changed'])}"
            for cfg, plan in plans if plan["changed"])
        if not messagebox.askyesno(
                "Update workflows",
                f"Re-render {total_changed} workflow file(s) from the "
                f"template embedded in Remote-Op?\n\n{detail}\n\n"
                f"One commit per repo, only the files that change. "
                f"Manual edits to those files are overwritten.\n\n"
                f"After Update no state is written back to GitHub "
                f"any more (the sync-back step is gone); restores "
                f"still read the last backup taken, and 9router "
                f"settings are untouched. Takes effect on the next "
                f"dispatch; running jobs keep the old form."):
            return

        def work():
            if not self._acquire_op(
                    f"update workflows ({scope}, {total_changed} file(s))"):
                return
            outcome = [f"update ({scope}): FAILED — see log", False]
            try:
                updated = 0
                for cfg, plan in plans:
                    if not plan["changed"]:
                        continue
                    if apply_update(GH(cfg["pat"]), cfg, plan, self.log):
                        updated += len(plan["changed"])
                if updated == total_changed:
                    outcome[:] = [f"update ({scope}): {updated} "
                                  f"workflow file(s) updated", True]
                else:
                    outcome[:] = [f"update ({scope}): {updated} of "
                                  f"{total_changed} updated — see log",
                                  False]
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
        """Create dialog: pick the repo and the count (preset 1 for
        Add). New machines always start raw (clean state, created by
        their own first sync) — there is no Copy option: machines
        never sync state back to the repo, so there is nothing to
        copy. When the chosen repo has no machines, a prefix field
        appears too: it names the machines when a brand-new repo is
        bootstrapped from the embedded template."""
        cfg = self.scope_repo() or self.target_repo()
        if not cfg:
            self.log("add a repo first (Repos…)")
            return
        win = tk.Toplevel(self.root)
        win.title("Add instance" if preset_count == 1
                  else "Create N from first machine")
        win.geometry(f"500x440+{self.root.winfo_rootx() + 140}"
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

        tk.Label(frm, text="Plugins, skills, agents and MCP config live "
                           "in the shared opencode/config — every new "
                           "machine gets those.",
                 bg=self.BG, fg=self.MUTED, justify="left",
                 wraplength=430).grid(row=3, column=0, columnspan=2,
                                      sticky="w", pady=(2, 0))

        info_var = tk.StringVar(value="")
        tk.Label(frm, textvariable=info_var, bg=self.BG, fg=self.MUTED,
                 justify="left", wraplength=430).grid(
                     row=4, column=0, columnspan=2, sticky="w", pady=(8, 2))

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
            if inst:
                first = sorted(inst, key=lambda x: parse_instance_name(x)[1])[0]
                prefix = parse_instance_name(first)[0]
                used = {parse_instance_name(x)[1] for x in inst}
                names = [f"{prefix}{k}" for k in plan_new_numbers(used, n)]
                info_var.set(
                    f"Template (first machine): {first}\n"
                    f"Will create: {', '.join(names)}\n"
                    "New machines start raw (clean state, first sync "
                    "creates it).\n"
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
                    "New machines start raw (clean state, first sync "
                    "creates it).")

        count_var.trace_add("write", update_info)
        prefix_var.trace_add("write", update_info)
        combo.bind("<<ComboboxSelected>>", update_info)
        update_info()

        btns = tk.Frame(frm, bg=self.BG)
        btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

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
                self._start_create(c, n, prefix=pfx or None)

        ttk.Button(btns, text="Create", style="Accent.TButton", command=go).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.LEFT, padx=2)

    def _start_create(self, cfg, count, prefix=None):
        def work():
            if not self._acquire_op(
                    f"create {count} instance(s) in {cfg['label']}"):
                return
            outcome = [f"{cfg['label']}: create FAILED — see log", False]
            try:
                created = create_instances(GH(cfg["pat"]), cfg, count,
                                           self.log,
                                           prefix_hint=prefix)
                if created:
                    outcome[:] = [f"{cfg['label']}: {len(created)} "
                                  f"instance(s) created "
                                  f"({created[0]}..{created[-1]})",
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

    # -- run choice dialog ------------------------------------------------------------
    def _provider_run_dialog(self, targets=None):
        """Pick the PROVIDER and model for this Run action, once for
        every machine it dispatches. The account is NOT picked here:
        each machine uses the account matching its instance number
        (machine 1 -> 1st account in that provider's settings order,
        machine 2 -> 2nd, wrapping), and the preview shows the
        mapping for the selected provider. Custom is the exception:
        ONE preset is picked here for the whole run (every machine
        uses it) and its model is only the default — it may still
        be changed in this dialog. OpenCode is the default: the
        pre-selected radio — each machine's per-instance Zen model
        with its OpenCode account's key, no model to pick. 9router
        is an explicit choice: fixed 9router/code, no account or
        model to pick.

        Returns None (cancel — nothing is dispatched),
        ("opencode",) (default: per-instance Zen + per-machine
        OpenCode account), ("9router",) (fixed 9router/code),
        ("openrouter", model), ("nvidia", model), or ("custom",
        preset, model) — the provider and the model slug for this
        run only. Tokens are never part of the return value: the
        runner reads them from the provider's KEYS secret. Each
        provider's model list loads in the background (NVIDIA's
        with the first NVIDIA account's token, to NVIDIA's API
        only; Custom's with the selected preset's token, to that
        preset's base URL only); if a list fails, manual entry
        still works and the dialog never blocks the run."""
        or_accounts = normalize_openrouter_accounts(
            self.settings.get("openrouter_accounts", []))
        nv_accounts = normalize_nvidia_accounts(
            self.settings.get("nvidia_accounts", []))
        oc_accounts = normalize_opencode_accounts(
            self.settings.get("opencode_accounts", []))
        cu_presets = normalize_custom_presets(
            self.settings.get("custom_presets", []))
        or_names = [a["name"] for a in or_accounts]
        nv_names = [a["name"] for a in nv_accounts]
        oc_names = [a["name"] for a in oc_accounts]
        cu_names = [a["name"] for a in cu_presets]
        result = {"value": None}
        win = tk.Toplevel(self.root)
        win.title("Run machines")
        win.geometry("560x640")
        win.transient(self.root)
        # The Run/Cancel bar is packed first, anchored at the bottom,
        # so it can never be pushed off-screen when the dialog is
        # shorter than its content — the model list shrinks instead.
        btns = tk.Frame(win)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)

        tk.Label(win, text="Provider (this run only):",
                 anchor="w").pack(fill=tk.X, padx=10, pady=(10, 2))
        # Zen (OpenCode) is the default: a plain Run uses each
        # machine's per-instance Zen model with its OpenCode
        # account's key, resolved per machine at dispatch.
        provider_var = tk.StringVar(value="opencode")
        radios = tk.Frame(win)
        radios.pack(fill=tk.X, padx=10)
        for _value, _label in (
                ("opencode", "OpenCode (Zen)"),
                ("9router", "9router"),
                ("openrouter", "OpenRouter"),
                ("nvidia", "NVIDIA"),
                ("custom", "Custom…")):
            ttk.Radiobutton(radios, text=_label, variable=provider_var,
                            value=_value).pack(side=tk.LEFT, padx=4)

        preset_row = tk.Frame(win)
        preset_row.pack(fill=tk.X, padx=10, pady=(6, 0))
        tk.Label(preset_row, text="Custom preset:",
                 anchor="w").pack(side=tk.LEFT)
        preset_var = tk.StringVar(
            value=cu_names[0] if cu_names else "")
        preset_combo = ttk.Combobox(preset_row, textvariable=preset_var,
                                    values=cu_names, state="readonly",
                                    width=40)
        preset_combo.pack(side=tk.LEFT, padx=4)

        hint_label = tk.Label(win, text="OpenCode (default): per-instance Zen model + "
                          "the machine's OpenCode account (machine 1 → "
                          "1st account, machine 2 → 2nd, and so on, "
                          "wrapping). 9router: fixed 9router/code. "
                          "OpenRouter / NVIDIA: the account per machine "
                          "is automatic. Custom: one preset for the whole "
                          "run, picked above.",
                 fg="#666", wraplength=520, justify="left",
                 anchor="w")
        hint_label.pack(fill=tk.X, padx=10, pady=(6, 2))
        preview_var = tk.StringVar()
        tk.Label(win, textvariable=preview_var, wraplength=520,
                 justify="left", anchor="w").pack(fill=tk.X, padx=10)

        def update_preview():
            provider = provider_var.get()
            if provider == "opencode":
                if targets and oc_names:
                    seen = []
                    for _cfg, _wf in targets:
                        if _wf not in seen:
                            seen.append(_wf)
                    preview = ", ".join(
                        f"{wf} → {opencode_account_for_instance(wf, oc_accounts)}"
                        for wf in seen[:10])
                    if len(seen) > 10:
                        preview += f", … (+{len(seen) - 10} more)"
                    preview_var.set("This run (Zen, per-instance model): "
                                    + preview)
                elif not oc_names:
                    preview_var.set("No OpenCode accounts yet — add them "
                                    "in 'OpenCode accounts…' (top bar), or "
                                    "pick another provider.")
                else:
                    preview_var.set("Zen: each machine keeps its "
                                    "per-instance model "
                                    "(default-models.txt / variable / "
                                    "built-in list).")
                return
            if provider == "9router":
                preview_var.set("9router: every machine in this run uses "
                                "9router/code (the local 9router combo). "
                                "No account or model to pick.")
                return
            if provider == "custom":
                if not cu_names:
                    preview_var.set("No Custom presets yet — add them in "
                                    "'Custom presets…' (top bar), or pick "
                                    "OpenCode above.")
                    return
                preset = custom_preset_by_name(
                    cu_presets, preset_var.get()) or cu_presets[0]
                preview_var.set(
                    f"Custom: {preset['name']} "
                    f"({preset['base_url'] or 'no base URL yet'}) — "
                    "every machine in this run uses this preset; the "
                    "model defaults to the preset's model "
                    f"({preset['model'] or 'none set'}).")
                return
            accounts = or_accounts if provider == "openrouter" \
                else nv_accounts
            resolver = openrouter_account_for_instance \
                if provider == "openrouter" \
                else nvidia_account_for_instance
            names = or_names if provider == "openrouter" else nv_names
            dialog = "OpenRouter" if provider == "openrouter" else "NVIDIA"
            if targets and names:
                seen = []
                for _cfg, _wf in targets:
                    if _wf not in seen:
                        seen.append(_wf)
                preview = ", ".join(
                    f"{wf} → {resolver(wf, accounts)}" for wf in seen[:10])
                if len(seen) > 10:
                    preview += f", … (+{len(seen) - 10} more)"
                preview_var.set("This run: " + preview)
            elif not names:
                preview_var.set(f"No {dialog} accounts yet — add them in "
                                f"'{dialog} accounts…' (top bar), or pick "
                                "OpenCode above.")
            else:
                preview_var.set("")

        list_label_var = tk.StringVar(
            value="Free model (live list, or type an id below):")
        tk.Label(win, textvariable=list_label_var,
                 anchor="w").pack(fill=tk.X, padx=10, pady=(10, 2))
        search_var = tk.StringVar()
        tk.Entry(win, textvariable=search_var).pack(fill=tk.X, padx=10)
        lb = tk.Listbox(win, height=12)
        lb.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        status_var = tk.StringVar(value="Loading free models…")
        tk.Label(win, textvariable=status_var, fg="#666",
                 anchor="w").pack(fill=tk.X, padx=10)
        # Per-provider lists: None = not loaded yet. NVIDIA's list is
        # fetched with the first NVIDIA account's token (to NVIDIA's
        # API only, never logged); a Custom list is fetched with the
        # selected preset's token (to that preset's base URL only,
        # never logged); OpenRouter's needs no key.
        state = {"openrouter": None, "nvidia": None, "custom": {},
                 "loading": set()}

        def refill():
            provider = provider_var.get()
            if provider == "custom":
                models = state["custom"].get(preset_var.get()) or []
            else:
                models = state.get(provider) or []
            q = search_var.get().strip().casefold()
            lb.delete(0, tk.END)
            for mid in models:
                if q and q not in mid.casefold():
                    continue
                lb.insert(tk.END, mid)

        search_var.trace_add("write", lambda *_a: refill())

        def load_models(provider, preset=None):
            if provider == "opencode":
                return
            if provider == "custom":
                if preset is None:
                    preset = preset_var.get()
                key = "custom:" + preset
                if key in state["loading"]:
                    return
                if state["custom"].get(preset) is not None:
                    return
                state["loading"].add(key)
                entry = custom_preset_by_name(cu_presets, preset)

                def work():
                    models = fetch_custom_models(
                        entry["base_url"] if entry else "",
                        entry["token"] if entry else "")

                    def done():
                        state["loading"].discard(key)
                        if not win.winfo_exists():
                            return
                        state["custom"][preset] = models
                        if provider_var.get() == "custom" \
                                and preset_var.get() == preset:
                            if models:
                                status_var.set(f"{len(models)} models")
                            else:
                                status_var.set("Could not load the model "
                                               "list — type a model ID "
                                               "instead.")
                            refill()
                    self.root.after(0, done)

                threading.Thread(target=work, daemon=True).start()
                return
            if provider in state["loading"]:
                return
            if state.get(provider) is not None:
                return
            state["loading"].add(provider)

            def work():
                if provider == "openrouter":
                    models = fetch_openrouter_models()
                else:
                    token = nv_accounts[0]["token"] if nv_accounts else ""
                    models = fetch_nvidia_models(token)

                def done():
                    state["loading"].discard(provider)
                    if not win.winfo_exists():
                        return
                    state[provider] = models
                    if provider_var.get() == provider:
                        if models:
                            status_var.set(f"{len(models)} models")
                        else:
                            status_var.set("Could not load the model "
                                           "list — type a model ID "
                                           "instead.")
                        refill()
                self.root.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        def on_provider_change(*_a):
            provider = provider_var.get()
            update_preview()
            # The Custom preset row is shown only for the Custom
            # provider — in every other state it is hidden (not just
            # disabled). Re-packed before the hint label so it keeps
            # its original position when it comes back.
            if provider == "custom":
                preset_row.pack(fill=tk.X, padx=10, pady=(6, 0),
                                before=hint_label)
            else:
                preset_row.pack_forget()
            preset_combo.configure(
                state="readonly" if provider == "custom" else "disabled")
            if provider == "opencode":
                list_label_var.set(
                    "Model: none to pick for OpenCode (Zen).")
                if not oc_names:
                    status_var.set("No OpenCode accounts — add them "
                                   "first, or pick another provider.")
                else:
                    status_var.set("Run uses each machine's Zen model "
                                   "+ its OpenCode account.")
                lb.delete(0, tk.END)
            elif provider == "9router":
                list_label_var.set(
                    "Model: none to pick for 9router (fixed "
                    "9router/code).")
                status_var.set("Run uses 9router/code for every machine.")
                lb.delete(0, tk.END)
            elif provider == "custom":
                list_label_var.set(
                    "Custom model (live list from the preset's base "
                    "URL, or type an id below):")
                if not cu_names:
                    status_var.set("No Custom presets — add one first, "
                                   "or pick OpenCode.")
                    lb.delete(0, tk.END)
                else:
                    preset = preset_var.get() or cu_names[0]
                    preset_var.set(preset)
                    loaded = state["custom"].get(preset)
                    entry = custom_preset_by_name(cu_presets, preset)
                    default = entry["model"] if entry else ""
                    if loaded is None:
                        status_var.set(
                            f"Loading models from {preset}…")
                    else:
                        status_var.set(
                            f"{len(loaded)} models "
                            f"(default: {default or 'none set'})")
                    refill()
                    load_models("custom", preset)
            elif provider == "openrouter":
                list_label_var.set(
                    "Free model (live list, or type an id below):")
                loaded = state.get("openrouter")
                status_var.set("Loading free models…" if loaded is None
                               else f"{len(loaded)} free models")
                refill()
                load_models("openrouter")
            else:
                list_label_var.set(
                    "NVIDIA model (live list, or type an id below):")
                if not nv_names:
                    status_var.set("No NVIDIA accounts — add one first, "
                                   "or pick OpenCode.")
                    lb.delete(0, tk.END)
                else:
                    loaded = state.get("nvidia")
                    status_var.set("Loading NVIDIA models…"
                                   if loaded is None
                                   else f"{len(loaded)} models")
                    refill()
                    load_models("nvidia")

        def on_preset_change(_evt=None):
            update_preview()
            if provider_var.get() != "custom":
                return
            preset = preset_var.get()
            entry = custom_preset_by_name(cu_presets, preset)
            loaded = state["custom"].get(preset)
            if loaded is None:
                status_var.set(f"Loading models from {preset}…")
            else:
                status_var.set(
                    f"{len(loaded)} models "
                    f"(default: {entry['model'] if entry else 'none set'})")
            refill()
            load_models("custom", preset)

        preset_combo.bind("<<ComboboxSelected>>", on_preset_change)

        provider_var.trace_add("write", on_provider_change)

        tk.Label(win, text="Or type a model ID (wins over the list):",
                 anchor="w").pack(fill=tk.X, padx=10, pady=(6, 2))
        manual_var = tk.StringVar()
        tk.Entry(win, textvariable=manual_var).pack(fill=tk.X, padx=10)

        def finish(value):
            result["value"] = value
            win.destroy()

        def run_provider():
            provider = provider_var.get()
            if provider == "opencode":
                if not oc_names:
                    status_var.set("No OpenCode accounts — add them in "
                                   "'OpenCode accounts…' first, "
                                   "or pick another provider.")
                    return
                finish(("opencode",))
                return
            if provider == "9router":
                finish(("9router",))
                return
            if provider == "custom":
                if not cu_names:
                    status_var.set("No Custom presets — add them in "
                                   "'Custom presets…' first, "
                                   "or pick OpenCode.")
                    return
                preset = preset_var.get() or cu_names[0]
                model = manual_var.get().strip()
                if not model:
                    sel = lb.curselection()
                    if sel:
                        model = lb.get(sel[0])
                if not model:
                    entry = custom_preset_by_name(cu_presets, preset)
                    model = entry["model"] if entry else ""
                if not model:
                    status_var.set("Pick a model, type a model ID, "
                                   "or pick OpenCode.")
                    return
                finish(("custom", preset, model))
                return
            names = or_names if provider == "openrouter" else nv_names
            if not names:
                dialog = "OpenRouter" if provider == "openrouter" \
                    else "NVIDIA"
                status_var.set(f"No {dialog} accounts — add them in "
                               f"'{dialog} accounts…' first, "
                               "or pick OpenCode.")
                return
            model = manual_var.get().strip()
            if not model:
                sel = lb.curselection()
                if not sel:
                    status_var.set("Pick a model, type a model ID, "
                                   "or pick OpenCode.")
                    return
                model = lb.get(sel[0])
            finish((provider, model))

        ttk.Button(btns, text="▶ Run",
                   style="Accent.TButton",
                   command=run_provider).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Cancel",
                   command=lambda: finish(None)).pack(side=tk.LEFT, padx=2)
        on_provider_change()
        win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        win.grab_set()
        win.wait_window()
        return result["value"]

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

    # -- openrouter accounts dialog -----------------------------------------------------
    def _openrouter_dialog(self):
        """Manage the OpenRouter accounts stored in settings.json
        (name + token, like the PATs in the Repos dialog). The LIST
        ORDER is the machine mapping: machine 1 uses the 1st
        account, machine 2 the 2nd, and so on (wrapping) — the Run
        dialog picks only the model. The token is NEVER sent by
        this app at dispatch and never logged: the runner reads it
        from the OPENROUTER_KEYS GitHub Secret, and Remote-Op sends
        only the account name + model. 'Copy JSON' builds that
        secret's content from these accounts for pasting into
        GitHub — set the secret yourself, in each fleet repo."""
        accounts = self.settings.setdefault("openrouter_accounts", [])
        self.settings["openrouter_accounts"] = normalize_openrouter_accounts(accounts)
        accounts = self.settings["openrouter_accounts"]
        win = tk.Toplevel(self.root)
        win.title("OpenRouter accounts")
        win.geometry("720x400")
        lb = tk.Listbox(win, width=24)
        lb.pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=6)
        for a in accounts:
            lb.insert(tk.END, a["name"])

        form = tk.Frame(win)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)
        vars_ = {}
        rows = [("name", "Name", 30, False),
                ("token", "Token", 46, True)]
        for key, title, w, secret in rows:
            r = tk.Frame(form)
            r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=title, width=12, anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar()
            tk.Entry(r, textvariable=v, width=w,
                     show="•" if secret else "").pack(side=tk.LEFT)
            vars_[key] = v
        tk.Label(form, text="Tokens live in settings.json next to the exe "
                            "(like PATs — keep the file private) and are "
                            "never sent at dispatch. The runner reads the "
                            "token from the OPENROUTER_KEYS GitHub Secret "
                            "(JSON: name -> token), which you set yourself "
                            "in each fleet repo. 'Copy JSON' builds that "
                            "secret's content from the accounts here. "
                            "ORDER MATTERS: machine 1 uses the 1st account "
                            "in this list, machine 2 the 2nd, and so on "
                            "(wrapping when there are more machines than "
                            "accounts) — reorder with Move up / Move down, "
                            "without retyping a token.",
                 fg="#666", wraplength=430,
                 justify="left").pack(anchor="w", pady=4)
        state = {"idx": None}

        def load_fields(idx):
            state["idx"] = idx
            a = accounts[idx] if idx is not None else blank_openrouter_account()
            for k, v in vars_.items():
                v.set(a.get(k, ""))

        def flush():
            if state["idx"] is None:
                return
            a = accounts[state["idx"]]
            for k, v in vars_.items():
                a[k] = v.get().strip()

        def on_select(_evt=None):
            flush()
            sel = lb.curselection()
            if sel:
                load_fields(sel[0])

        lb.bind("<<ListboxSelect>>", on_select)

        btns = tk.Frame(form)
        btns.pack(fill=tk.X, pady=8)

        def new_account():
            flush()
            accounts.append(blank_openrouter_account())
            lb.insert(tk.END, "")
            lb.selection_clear(0, tk.END)
            lb.selection_set(tk.END)
            load_fields(len(accounts) - 1)

        def del_account():
            sel = lb.curselection()
            if not sel:
                return
            del accounts[sel[0]]
            lb.delete(sel[0])
            state["idx"] = None
            load_fields(None)

        def move_account(delta):
            sel = lb.curselection()
            if not sel:
                return
            flush()
            new_idx = move_openrouter_account(accounts, sel[0], delta)
            if new_idx == sel[0]:
                return
            lb.delete(0, tk.END)
            for a in accounts:
                lb.insert(tk.END, a["name"])
            lb.selection_clear(0, tk.END)
            lb.selection_set(new_idx)
            load_fields(new_idx)

        def copy_json():
            flush()
            text = openrouter_keys_json(accounts)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("OPENROUTER_KEYS JSON copied — paste it as the "
                     "OPENROUTER_KEYS secret in each fleet repo "
                     "(token values are never logged).")

        def save_close():
            if state["idx"] is None and any(
                    v.get().strip() for v in vars_.values()):
                accounts.append(blank_openrouter_account())
                lb.insert(tk.END, "")
                state["idx"] = len(accounts) - 1
            flush()
            kept = [a for a in accounts if a.get("name")]
            names = [a["name"] for a in kept]
            if len(names) != len(set(names)):
                messagebox.showerror(
                    "Remote-Op",
                    "Account names must be unique — rename the duplicate "
                    "before saving.")
                return
            self.settings["openrouter_accounts"] = normalize_openrouter_accounts(kept)
            try:
                save_settings(self.settings)
            except OSError as e:
                messagebox.showerror(
                    "Remote-Op",
                    f"Could not save settings:\n{SETTINGS_PATH}\n\n{e}")
                return
            self.log(f"OpenRouter accounts saved "
                     f"({len(self.settings['openrouter_accounts'])} account(s); "
                     f"tokens are never logged).")
            win.destroy()

        tk.Button(btns, text="New", command=new_account).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Delete", command=del_account).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move up",
                  command=lambda: move_account(-1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move down",
                  command=lambda: move_account(1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Copy JSON for GitHub Secret",
                  command=copy_json).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Save & close",
                  command=save_close).pack(side=tk.LEFT, padx=10)
        if accounts:
            lb.selection_set(0)
            load_fields(0)
        else:
            new_account()

    # -- opencode accounts dialog -------------------------------------------------------
    def _opencode_dialog(self):
        """Manage the OpenCode accounts stored in settings.json
        (name + token, like the OpenRouter accounts). The LIST
        ORDER is the machine mapping: machine 1 uses the 1st
        account, machine 2 the 2nd, and so on (wrapping) — a plain
        Run uses each machine's per-instance Zen model and only
        the AUTH comes from its account. The token is NEVER sent by
        this app at dispatch and never logged: the runner reads it
        from the OPENCODE_KEYS GitHub Secret, and Remote-Op sends
        only the account name. 'Copy JSON' builds that secret's
        content from these accounts for pasting into GitHub — set
        the secret yourself, in each fleet repo."""
        accounts = self.settings.setdefault("opencode_accounts", [])
        self.settings["opencode_accounts"] = normalize_opencode_accounts(accounts)
        accounts = self.settings["opencode_accounts"]
        win = tk.Toplevel(self.root)
        win.title("OpenCode accounts")
        win.geometry("720x400")
        lb = tk.Listbox(win, width=24)
        lb.pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=6)
        for a in accounts:
            lb.insert(tk.END, a["name"])

        form = tk.Frame(win)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)
        vars_ = {}
        rows = [("name", "Name", 30, False),
                ("token", "Token", 46, True)]
        for key, title, w, secret in rows:
            r = tk.Frame(form)
            r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=title, width=12, anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar()
            tk.Entry(r, textvariable=v, width=w,
                     show="•" if secret else "").pack(side=tk.LEFT)
            vars_[key] = v
        tk.Label(form, text="Tokens live in settings.json next to the exe "
                            "(like PATs — keep the file private) and are "
                            "never sent at dispatch. The runner reads the "
                            "token from the OPENCODE_KEYS GitHub Secret "
                            "(JSON: name -> token), which you set yourself "
                            "in each fleet repo. 'Copy JSON' builds that "
                            "secret's content from the accounts here. "
                            "ORDER MATTERS: machine 1 uses the 1st account "
                            "in this list, machine 2 the 2nd, and so on "
                            "(wrapping when there are more machines than "
                            "accounts) — reorder with Move up / Move down, "
                            "without retyping a token. The model is NOT "
                            "picked here: a plain Run keeps each machine's "
                            "per-instance Zen model and only the key comes "
                            "from its account.",
                 fg="#666", wraplength=430,
                 justify="left").pack(anchor="w", pady=4)
        state = {"idx": None}

        def load_fields(idx):
            state["idx"] = idx
            a = accounts[idx] if idx is not None else blank_opencode_account()
            for k, v in vars_.items():
                v.set(a.get(k, ""))

        def flush():
            if state["idx"] is None:
                return
            a = accounts[state["idx"]]
            for k, v in vars_.items():
                a[k] = v.get().strip()

        def on_select(_evt=None):
            flush()
            sel = lb.curselection()
            if sel:
                load_fields(sel[0])

        lb.bind("<<ListboxSelect>>", on_select)

        btns = tk.Frame(form)
        btns.pack(fill=tk.X, pady=8)

        def new_account():
            flush()
            accounts.append(blank_opencode_account())
            lb.insert(tk.END, "")
            lb.selection_clear(0, tk.END)
            lb.selection_set(tk.END)
            load_fields(len(accounts) - 1)

        def del_account():
            sel = lb.curselection()
            if not sel:
                return
            del accounts[sel[0]]
            lb.delete(sel[0])
            state["idx"] = None
            load_fields(None)

        def move_account(delta):
            sel = lb.curselection()
            if not sel:
                return
            flush()
            new_idx = move_opencode_account(accounts, sel[0], delta)
            if new_idx == sel[0]:
                return
            lb.delete(0, tk.END)
            for a in accounts:
                lb.insert(tk.END, a["name"])
            lb.selection_clear(0, tk.END)
            lb.selection_set(new_idx)
            load_fields(new_idx)

        def copy_json():
            flush()
            text = opencode_keys_json(accounts)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("OPENCODE_KEYS JSON copied — paste it as the "
                     "OPENCODE_KEYS secret in each fleet repo "
                     "(token values are never logged).")

        def save_close():
            if state["idx"] is None and any(
                    v.get().strip() for v in vars_.values()):
                accounts.append(blank_opencode_account())
                lb.insert(tk.END, "")
                state["idx"] = len(accounts) - 1
            flush()
            kept = [a for a in accounts if a.get("name")]
            names = [a["name"] for a in kept]
            if len(names) != len(set(names)):
                messagebox.showerror(
                    "Remote-Op",
                    "Account names must be unique — rename the duplicate "
                    "before saving.")
                return
            self.settings["opencode_accounts"] = normalize_opencode_accounts(kept)
            try:
                save_settings(self.settings)
            except OSError as e:
                messagebox.showerror(
                    "Remote-Op",
                    f"Could not save settings:\n{SETTINGS_PATH}\n\n{e}")
                return
            self.log(f"OpenCode accounts saved "
                     f"({len(self.settings['opencode_accounts'])} account(s); "
                     f"tokens are never logged).")
            win.destroy()

        tk.Button(btns, text="New", command=new_account).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Delete", command=del_account).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move up",
                  command=lambda: move_account(-1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move down",
                  command=lambda: move_account(1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Copy JSON for GitHub Secret",
                  command=copy_json).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Save & close",
                  command=save_close).pack(side=tk.LEFT, padx=10)
        if accounts:
            lb.selection_set(0)
            load_fields(0)
        else:
            new_account()

    # -- nvidia accounts dialog ---------------------------------------------------------
    def _nvidia_dialog(self):
        """Manage the NVIDIA accounts stored in settings.json
        (name + token, like the OpenRouter accounts). The LIST
        ORDER is the machine mapping: machine 1 uses the 1st
        account, machine 2 the 2nd, and so on (wrapping) — the Run
        dialog picks the provider + model only. The token is NEVER
        sent by this app at dispatch and never logged: the runner
        reads it from the NVIDIA_KEYS GitHub Secret, and Remote-Op
        sends only the account name + model. 'Copy JSON' builds
        that secret's content from these accounts for pasting into
        GitHub — set the secret yourself, in each fleet repo. The
        FIRST account's token is also used, from this machine only,
        to fetch the live NVIDIA model list in the Run dialog."""
        accounts = self.settings.setdefault("nvidia_accounts", [])
        self.settings["nvidia_accounts"] = normalize_nvidia_accounts(accounts)
        accounts = self.settings["nvidia_accounts"]
        win = tk.Toplevel(self.root)
        win.title("NVIDIA accounts")
        win.geometry("720x400")
        lb = tk.Listbox(win, width=24)
        lb.pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=6)
        for a in accounts:
            lb.insert(tk.END, a["name"])

        form = tk.Frame(win)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)
        vars_ = {}
        rows = [("name", "Name", 30, False),
                ("token", "Token", 46, True)]
        for key, title, w, secret in rows:
            r = tk.Frame(form)
            r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=title, width=12, anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar()
            tk.Entry(r, textvariable=v, width=w,
                     show="•" if secret else "").pack(side=tk.LEFT)
            vars_[key] = v
        tk.Label(form, text="Tokens live in settings.json next to the exe "
                            "(like PATs — keep the file private) and are "
                            "never sent at dispatch. The runner reads the "
                            "token from the NVIDIA_KEYS GitHub Secret "
                            "(JSON: name -> token), which you set yourself "
                            "in each fleet repo. 'Copy JSON' builds that "
                            "secret's content from the accounts here. "
                            "ORDER MATTERS: machine 1 uses the 1st account "
                            "in this list, machine 2 the 2nd, and so on "
                            "(wrapping when there are more machines than "
                            "accounts) — reorder with Move up / Move down, "
                            "without retyping a token. The first "
                            "account's token is also used to fetch the "
                            "live NVIDIA model list (to NVIDIA's API "
                            "only, never logged).",
                 fg="#666", wraplength=430,
                 justify="left").pack(anchor="w", pady=4)
        state = {"idx": None}

        def load_fields(idx):
            state["idx"] = idx
            a = accounts[idx] if idx is not None else blank_nvidia_account()
            for k, v in vars_.items():
                v.set(a.get(k, ""))

        def flush():
            if state["idx"] is None:
                return
            a = accounts[state["idx"]]
            for k, v in vars_.items():
                a[k] = v.get().strip()

        def on_select(_evt=None):
            flush()
            sel = lb.curselection()
            if sel:
                load_fields(sel[0])

        lb.bind("<<ListboxSelect>>", on_select)

        btns = tk.Frame(form)
        btns.pack(fill=tk.X, pady=8)

        def new_account():
            flush()
            accounts.append(blank_nvidia_account())
            lb.insert(tk.END, "")
            lb.selection_clear(0, tk.END)
            lb.selection_set(tk.END)
            load_fields(len(accounts) - 1)

        def del_account():
            sel = lb.curselection()
            if not sel:
                return
            del accounts[sel[0]]
            lb.delete(sel[0])
            state["idx"] = None
            load_fields(None)

        def move_account(delta):
            sel = lb.curselection()
            if not sel:
                return
            flush()
            new_idx = move_nvidia_account(accounts, sel[0], delta)
            if new_idx == sel[0]:
                return
            lb.delete(0, tk.END)
            for a in accounts:
                lb.insert(tk.END, a["name"])
            lb.selection_clear(0, tk.END)
            lb.selection_set(new_idx)
            load_fields(new_idx)

        def copy_json():
            flush()
            text = nvidia_keys_json(accounts)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("NVIDIA_KEYS JSON copied — paste it as the "
                     "NVIDIA_KEYS secret in each fleet repo "
                     "(token values are never logged).")

        def save_close():
            if state["idx"] is None and any(
                    v.get().strip() for v in vars_.values()):
                accounts.append(blank_nvidia_account())
                lb.insert(tk.END, "")
                state["idx"] = len(accounts) - 1
            flush()
            kept = [a for a in accounts if a.get("name")]
            names = [a["name"] for a in kept]
            if len(names) != len(set(names)):
                messagebox.showerror(
                    "Remote-Op",
                    "Account names must be unique — rename the duplicate "
                    "before saving.")
                return
            self.settings["nvidia_accounts"] = normalize_nvidia_accounts(kept)
            try:
                save_settings(self.settings)
            except OSError as e:
                messagebox.showerror(
                    "Remote-Op",
                    f"Could not save settings:\n{SETTINGS_PATH}\n\n{e}")
                return
            self.log(f"NVIDIA accounts saved "
                     f"({len(self.settings['nvidia_accounts'])} account(s); "
                     f"tokens are never logged).")
            win.destroy()

        tk.Button(btns, text="New", command=new_account).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Delete", command=del_account).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move up",
                  command=lambda: move_account(-1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move down",
                  command=lambda: move_account(1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Copy JSON for GitHub Secret",
                  command=copy_json).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Save & close",
                  command=save_close).pack(side=tk.LEFT, padx=10)
        if accounts:
            lb.selection_set(0)
            load_fields(0)
        else:
            new_account()

    # -- custom presets dialog --------------------------------------------------------
    def _custom_dialog(self):
        """Manage the Custom presets stored in settings.json (name +
        base URL + default model + token). Each preset is one free
        provider endpoint (OpenAI-compatible only, v1): at Run time
        ONE preset is picked for the whole Run action and every
        machine uses it, with the preset's model as the default —
        the model may still be changed in the Run dialog. List
        order is the order presets are offered in the Run dialog
        (the first is the default pick). The token is NEVER sent by
        this app at dispatch and never logged: the runner reads it
        from the CUSTOM_KEYS GitHub Secret, and Remote-Op sends
        only the preset name + base URL + model. 'Copy JSON' builds
        that secret's content from these presets for pasting into
        GitHub — set the secret yourself, in each fleet repo. A
        preset's token is also used, from this machine only, to
        fetch the live model list from that preset's base URL in
        the Run dialog."""
        presets = self.settings.setdefault("custom_presets", [])
        self.settings["custom_presets"] = normalize_custom_presets(presets)
        presets = self.settings["custom_presets"]
        win = tk.Toplevel(self.root)
        win.title("Custom presets")
        win.geometry("720x400")
        lb = tk.Listbox(win, width=24)
        lb.pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=6)
        for a in presets:
            lb.insert(tk.END, a["name"])

        form = tk.Frame(win)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)
        vars_ = {}
        rows = [("name", "Name", 30, False),
                ("base_url", "Base URL", 46, False),
                ("model", "Default model", 46, False),
                ("token", "Token", 46, True)]
        for key, title, w, secret in rows:
            r = tk.Frame(form)
            r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=title, width=12, anchor="w").pack(side=tk.LEFT)
            v = tk.StringVar()
            tk.Entry(r, textvariable=v, width=w,
                     show="•" if secret else "").pack(side=tk.LEFT)
            vars_[key] = v
        tk.Label(form, text="One preset per provider endpoint "
                            "(OpenAI-compatible only): the base URL is "
                            "the provider's API root, with or without "
                            "a trailing /v1 — e.g. "
                            "https://api.example.com. Tokens live in "
                            "settings.json next to the exe (like PATs — "
                            "keep the file private) and are never sent "
                            "at dispatch. The runner reads the token "
                            "from the CUSTOM_KEYS GitHub Secret (JSON: "
                            "preset name -> token), which you set "
                            "yourself in each fleet repo. 'Copy JSON' "
                            "builds that secret's content from the "
                            "presets here. At Run time one preset is "
                            "picked for the whole run; its model is "
                            "only the default and may be changed "
                            "there. The preset's token is also used "
                            "to fetch the live model list from its "
                            "base URL (to that URL only, never "
                            "logged).",
                 fg="#666", wraplength=430,
                 justify="left").pack(anchor="w", pady=4)
        state = {"idx": None}

        def load_fields(idx):
            state["idx"] = idx
            a = presets[idx] if idx is not None else blank_custom_preset()
            for k, v in vars_.items():
                v.set(a.get(k, ""))

        def flush():
            if state["idx"] is None:
                return
            a = presets[state["idx"]]
            for k, v in vars_.items():
                a[k] = v.get().strip()

        def on_select(_evt=None):
            flush()
            sel = lb.curselection()
            if sel:
                load_fields(sel[0])

        lb.bind("<<ListboxSelect>>", on_select)

        btns = tk.Frame(form)
        btns.pack(fill=tk.X, pady=8)

        def new_preset():
            flush()
            presets.append(blank_custom_preset())
            lb.insert(tk.END, "")
            lb.selection_clear(0, tk.END)
            lb.selection_set(tk.END)
            load_fields(len(presets) - 1)

        def del_preset():
            sel = lb.curselection()
            if not sel:
                return
            del presets[sel[0]]
            lb.delete(sel[0])
            state["idx"] = None
            load_fields(None)

        def move_preset(delta):
            sel = lb.curselection()
            if not sel:
                return
            flush()
            new_idx = move_custom_preset(presets, sel[0], delta)
            if new_idx == sel[0]:
                return
            lb.delete(0, tk.END)
            for a in presets:
                lb.insert(tk.END, a["name"])
            lb.selection_clear(0, tk.END)
            lb.selection_set(new_idx)
            load_fields(new_idx)

        def copy_json():
            flush()
            text = custom_keys_json(presets)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("CUSTOM_KEYS JSON copied — paste it as the "
                     "CUSTOM_KEYS secret in each fleet repo "
                     "(token values are never logged).")

        def save_close():
            if state["idx"] is None and any(
                    v.get().strip() for v in vars_.values()):
                presets.append(blank_custom_preset())
                lb.insert(tk.END, "")
                state["idx"] = len(presets) - 1
            flush()
            kept = [a for a in presets if a.get("name")]
            names = [a["name"] for a in kept]
            if len(names) != len(set(names)):
                messagebox.showerror(
                    "Remote-Op",
                    "Preset names must be unique — rename the duplicate "
                    "before saving.")
                return
            self.settings["custom_presets"] = normalize_custom_presets(kept)
            try:
                save_settings(self.settings)
            except OSError as e:
                messagebox.showerror(
                    "Remote-Op",
                    f"Could not save settings:\n{SETTINGS_PATH}\n\n{e}")
                return
            self.log(f"Custom presets saved "
                     f"({len(self.settings['custom_presets'])} preset(s); "
                     f"tokens are never logged).")
            win.destroy()

        tk.Button(btns, text="New", command=new_preset).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Delete", command=del_preset).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move up",
                  command=lambda: move_preset(-1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Move down",
                  command=lambda: move_preset(1)).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Copy JSON for GitHub Secret",
                  command=copy_json).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="Save & close",
                  command=save_close).pack(side=tk.LEFT, padx=10)
        if presets:
            lb.selection_set(0)
            load_fields(0)
        else:
            new_preset()

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

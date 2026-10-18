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
    as before.

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


def create_instances(gh, cfg, count, log):
    """Clone `count` new instances from the repo's FIRST machine.

    The template is the lowest-numbered instance's workflow file (or,
    with no machine left, the most recently deleted one from git
    history). Numbering gap-fills over workflow numbers ∪ state-dir
    numbers. All new files land in ONE git-tree commit. Returns the
    created instance names ([] when nothing was created).
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
        if not hist:
            log("no instance workflow found, and none in git history "
                "either — nothing to clone from")
            return []
        template_name, template_text = hist
        log(f"repo is empty — using the last deleted workflow "
            f"({template_name}) from git history as the template")
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
        return [{"path": f".github/workflows/{name}.yml", "mode": "100644",
                 "type": "blob", "sha": blob_sha[name]} for name in names]

    msg = (f"Add {names[0]} workflow" if count == 1 else
           f"Add {count} instances from {template_name}: "
           f"{names[0]}..{names[-1]}")
    if not gh.commit_tree_change(repo, branch, msg, entries, log):
        log("create FAILED — no instances were committed")
        return []
    for name in names:
        log(f"✅ Created .github/workflows/{name}.yml — {name} starts "
            f"with a clean state and creates opencode/instances/"
            f"{name}/ on its first sync")
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
        self.uptime = {}      # (label, wf) -> (up: bool, since: ts|None)
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
        self.root.after(60 * 1000, self._uptime_loop)

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

    def _server_text(self, label, wf):
        st = self.uptime.get((label, wf))
        if st is None:
            return "—"
        up, since = st
        if not up:
            return "● DOWN"
        if since:
            mins = int((time.time() - since) // 60)
            h, m = divmod(mins, 60)
            return f"● UP {h}h{m:02d}m" if h else f"● UP {m}m"
        return "● UP"

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
                if status in ("in_progress", "queued"):
                    running += 1
                    mins = remaining_minutes(run.get("created_at"), info["timeout"])
                    remaining = fmt_remaining(mins)
                    tag = "low" if (mins is not None and mins < 120) else "ok"
                srv = self._server_text(label, wf)
                # One row colour only (ttk tags are per-row): a DOWN
                # server outranks the green "plenty of budget" tint,
                # but never the red low-budget warning.
                row_tag = tag
                if srv.startswith("● DOWN") and row_tag != "low":
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

    # -- uptime (every 5 min, integrated in the Server column) -----------------
    def _uptime_loop(self):
        self.bg(self._check_uptime)
        self.root.after(5 * 60 * 1000, self._uptime_loop)

    @staticmethod
    def _ping(url, timeout=6):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status < 500
        except Exception:
            return False

    def _check_uptime(self):
        keys = list(self.instances.keys())

        def probe(key):
            label, wf = key
            ok = self._ping(f"http://{wf}:4096")
            prev = self.uptime.get(key)
            since = prev[1] if (prev and prev[0] and ok) else (
                time.time() if ok else None)
            self.uptime[key] = (ok, since)

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(probe, keys))
        self.root.after(0, self._after_uptime)

    def _after_uptime(self):
        self.uptime_var.set(
            f"server check: {time.strftime('%H:%M:%S')} (every 5 min)")
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
        cfg = self.scope_repo()
        if not cfg:
            self.log("add a repo first (Repos…)")
            return
        if not messagebox.askyesno(
                "Add instance",
                f"Add ONE new instance to {cfg['repo']}, cloned from "
                f"its first machine (numbering gap-fills)?"):
            return
        self._start_create(cfg, 1)

    def create_bulk(self):
        """Create N dialog: pick the repo, type the count (e.g. 10);
        the new machines are cloned from that repo's first machine."""
        cfg = self.scope_repo() or self.target_repo()
        if not cfg:
            self.log("add a repo first (Repos…)")
            return
        win = tk.Toplevel(self.root)
        win.title("Create N from first machine")
        win.geometry("460x250")
        win.configure(bg=self.BG)
        frm = tk.Frame(win, bg=self.BG)
        frm.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        tk.Label(frm, text="Repo:", bg=self.BG).grid(row=0, column=0, sticky="w", pady=3)
        repo_var = tk.StringVar(value=cfg["label"])
        combo = ttk.Combobox(frm, textvariable=repo_var, state="readonly",
                             width=24, values=[a["label"] for a in self.settings["repos"]])
        combo.grid(row=0, column=1, sticky="w", pady=3)

        tk.Label(frm, text="How many machines:", bg=self.BG).grid(row=1, column=0, sticky="w", pady=3)
        count_var = tk.StringVar(value="10")
        ttk.Entry(frm, textvariable=count_var, width=8).grid(row=1, column=1, sticky="w", pady=3)

        info_var = tk.StringVar(value="")
        tk.Label(frm, textvariable=info_var, bg=self.BG, fg=self.MUTED,
                 justify="left", wraplength=420).grid(
                     row=2, column=0, columnspan=2, sticky="w", pady=(8, 2))

        def update_info(*_a):
            c = self.repo_by_label(repo_var.get())
            if not c:
                info_var.set("")
                return
            inst = self.instances_of(c["label"])
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
                    f"(preview from the loaded fleet — numbers that only "
                    f"have a state dir are skipped too, so the final "
                    f"numbering is recomputed at run time)")
            else:
                info_var.set(
                    "No machines loaded for this repo — the template "
                    "will be recovered from git history at run time "
                    "(the most recently deleted machine).")

        count_var.trace_add("write", update_info)
        combo.bind("<<ComboboxSelected>>", update_info)
        update_info()

        btns = tk.Frame(frm, bg=self.BG)
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

        def go():
            try:
                n = int(count_var.get())
            except ValueError:
                messagebox.showerror("Create N", "Count must be a whole number (e.g. 10).", parent=win)
                return
            if n < 1 or n > 100:
                messagebox.showerror("Create N", "Count must be between 1 and 100.", parent=win)
                return
            c = self.repo_by_label(repo_var.get())
            win.destroy()
            if c:
                self._start_create(c, n)

        ttk.Button(btns, text=f"Create", style="Accent.TButton", command=go).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=tk.LEFT, padx=2)

    def _start_create(self, cfg, count):
        def work():
            if not self._acquire_op(
                    f"create {count} instance(s) in {cfg['label']}"):
                return
            outcome = [f"{cfg['label']}: create FAILED — see log", False]
            try:
                created = create_instances(GH(cfg["pat"]), cfg, count, self.log)
                if created:
                    outcome[:] = [f"{cfg['label']}: {len(created)} "
                                  f"instance(s) created "
                                  f"({created[0]}..{created[-1]})", True]
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

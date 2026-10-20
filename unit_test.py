import sys, types, os, json, tempfile
# stub tkinter so app.py imports headless
for name in ("tkinter", "tkinter.ttk", "tkinter.messagebox",
             "tkinter.scrolledtext", "tkinter.simpledialog"):
    sys.modules[name] = types.ModuleType(name)
tk = sys.modules["tkinter"]
for attr in ("Tk", "Frame", "Label", "Button", "Entry", "Listbox",
             "Checkbutton", "StringVar", "BooleanVar", "Toplevel",
             "PhotoImage"):
    setattr(tk, attr, object)
tk.END = "end"; tk.X = "x"; tk.Y = "y"; tk.LEFT = "left"; tk.BOTH = "both"
sys.modules["tkinter.ttk"].Combobox = object
sys.modules["tkinter.ttk"].Treeview = object
sys.modules["tkinter.messagebox"].askyesno = lambda *a, **k: True
sys.modules["tkinter.messagebox"].showerror = lambda *a, **k: None
sys.modules["tkinter.scrolledtext"].ScrolledText = object
sys.modules["tkinter.simpledialog"].askstring = lambda *a, **k: None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

# no real waiting in cancel_and_wait polls
app.time.sleep = lambda *_a, **_k: None

SAMPLE = """name: rey1

on:
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-24.04
    timeout-minutes: 340
    env:
      WF_NAME: rey1
    steps:
      - name: 16. Sync
        run: |
          git commit -m "Sync state from rey1 instance" || true
"""

# --- pure helpers -------------------------------------------------------
assert app.parse_instance_name("rey11") == ("rey", 11)
assert app.parse_instance_name("open1") == ("open", 1)
assert app.parse_instance_name("sydney") is None
assert app.parse_instance_name("1rey") is None
assert app.valid_prefix("fork-2x") and app.valid_prefix("fork-x")
assert not app.valid_prefix("2fork") and not app.valid_prefix("fork-")
assert app.next_free_number({1, 2, 4}) == 3
assert app.plan_new_numbers({2, 3}, 3) == [1, 4, 5]
assert app.plan_new_numbers({1, 2, 4}, 10) == [3, 5, 6, 7, 8, 9, 10, 11, 12, 13]
assert app.plan_new_numbers(set(), 0) == []
print("helpers OK")

# --- renamed_workflow ---------------------------------------------------
out = app.renamed_workflow(SAMPLE, "rey7")
assert "name: rey7" in out and "WF_NAME: rey7" in out
assert "from rey7 instance" in out and "rey1" not in out
try:
    app.renamed_workflow("name: x\n", "rey2")
    raise SystemExit("renamed_workflow should have raised")
except ValueError:
    pass
assert app.workflow_timeout(SAMPLE) == 340
assert app.workflow_timeout("nothing") == app.RUN_BUDGET_MIN
print("renamed_workflow / timeout OK")

# --- remaining time -------------------------------------------------------
from datetime import datetime, timezone, timedelta
now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
started = (now - timedelta(minutes=200)).isoformat().replace("+00:00", "Z")
assert app.remaining_minutes(started, 340, now) == 140
assert app.fmt_remaining(140) == "2h20m left" and app.fmt_remaining(-1) == "overdue"
print("remaining_minutes OK")

# --- settings migration ---------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "settings.json")
    with open(p, "w") as f:
        json.dump({"repo": "haileen5/my-opencode", "password": "pw",
                   "branch": ""}, f)
    s = app.load_settings(p)
    assert len(s["repos"]) == 1, s
    e = s["repos"][0]
    assert e["label"] == "haileen5" and e["repo"] == "haileen5/my-opencode"
    assert e["password"] == "pw" and e["branch"] == "opc", e
    app.save_settings(s, p)
    s2 = app.load_settings(p)
    assert s2["repos"] == s["repos"], (s2, s)
    with open(p, "w") as f:
        f.write("{broken")
    assert app.load_settings(p)["repos"] == []
print("settings migration OK")


# --- fake GitHub ------------------------------------------------------------
class FakeGH(app.GH):
    """In-memory repo: files = {path: text}; one commit per update_ref."""

    def __init__(self, files, active=None, stuck=()):
        super().__init__("")
        self.files = dict(files)
        self.blobs = {}
        self._n = 0
        self.commits = 0
        self.active = dict(active or {})   # workflow_file -> run_id
        self.stuck = set(stuck)            # run_ids that never complete
        self._pending = None

    # content layer
    def list_instances(self, repo, branch):
        out = {}
        for path, text in sorted(self.files.items()):
            if path.startswith(".github/workflows/"):
                wf = app.workflow_instance(text)
                if wf and app.parse_instance_name(wf):
                    out[wf] = {"file": path.rsplit("/", 1)[1], "sha": "x",
                               "text": text,
                               "timeout": app.workflow_timeout(text)}
        return out

    def state_numbers(self, repo, branch):
        nums = set()
        for path in self.files:
            parts = path.split("/")
            if len(parts) > 2 and parts[:2] == ["opencode", "instances"]:
                p = app.parse_instance_name(parts[2])
                if p:
                    nums.add(p[1])
        return nums

    def history_template(self, repo, branch):
        return self._history

    _history = None

    # runs layer
    def list_runs(self, repo, per_page=100):
        return [{"id": rid, "path": f".github/workflows/{f}",
                 "status": "in_progress", "head_branch": "opc"}
                for f, rid in self.active.items()]

    def cancel_run(self, repo, run_id):
        if run_id not in self.stuck:
            self.active = {f: r for f, r in self.active.items()
                           if r != run_id}
            self._done = getattr(self, "_done", set()) | {run_id}
        return True

    def api(self, method, path, body=None, timeout=90):
        rid = int(path.rsplit("/", 1)[1])
        done = rid in getattr(self, "_done", set())
        return 200, {"status": "completed" if done else "in_progress"}

    # git layer (real commit_tree_change runs on top of these)
    def ref_head(self, repo, branch):
        return "head"

    def head_tree(self, repo, commit_sha):
        return "tree"

    def recursive_tree(self, repo, ref):
        return [{"path": p, "type": "blob", "mode": "100644", "sha": p}
                for p in self.files]

    def create_blob(self, repo, text):
        self._n += 1
        sha = f"blob{self._n}"
        self.blobs[sha] = text
        return sha

    def create_tree(self, repo, entries, base_tree=None):
        new = dict(self.files)
        for e in entries:
            if e["sha"] is None:
                new.pop(e["path"], None)
            elif e["sha"] in self.blobs:
                new[e["path"]] = self.blobs[e["sha"]]
            elif e["sha"] in self.files:  # state move: sha is the path
                new[e["path"]] = self.files[e["sha"]]
        self._pending = new
        return "newtree"

    def create_commit(self, repo, message, tree_sha, parents):
        self._msg = message
        return "newcommit"

    def update_ref(self, repo, branch, sha):
        self.files = self._pending
        self.commits += 1
        return True


CFG = {"label": "t", "pat": "", "repo": "o/r", "branch": "opc",
       "password": ""}
logs = []
log = logs.append

# --- bulk create: 10 from the first machine, gap-filling -------------------
files = {".github/workflows/rey1.yml": SAMPLE,
         ".github/workflows/rey2.yml": app.renamed_workflow(SAMPLE, "rey2"),
         "opencode/instances/rey4/state.txt": "old state"}
gh = FakeGH(files)
created = app.create_instances(gh, CFG, 10, log)
assert created == [f"rey{n}" for n in (3, 5, 6, 7, 8, 9, 10, 11, 12, 13)], created
assert gh.commits == 1, gh.commits  # ONE commit for all 10
for n in (3, 5, 13):
    text = gh.files[f".github/workflows/rey{n}.yml"]
    assert app.workflow_instance(text) == f"rey{n}", text[:80]
assert app.workflow_instance(gh.files[".github/workflows/rey1.yml"]) == "rey1"
print("bulk create (10 from first, one commit) OK")

# --- bulk create on an empty repo falls back to git history ------------------
gh = FakeGH({})
gh._history = ("rey9", app.renamed_workflow(SAMPLE, "rey9"))
created = app.create_instances(gh, CFG, 2, log)
assert created == ["rey1", "rey2"], created
print("bulk create from history OK")

# --- bulk create on a BRAND-NEW repo falls back to the embedded template ------
gh = FakeGH({})
gh._history = None  # no machine ever existed here, nothing in history
created = app.create_instances(gh, CFG, 2, log, prefix_hint="syd")
assert created == ["syd1", "syd2"], created
assert gh.commits == 1, gh.commits  # ONE commit for both
text = gh.files[".github/workflows/syd1.yml"]
assert app.workflow_instance(text) == "syd1"
# rendered for CFG's repo o/r: owner o, its h-dashboard, the repo slug
assert "https://github.com/o/h-dashboard.git" in text
assert 'git config user.name "o"' in text
assert "github.com/o/r.git" in text
assert "{{WF_NAME}}" not in text and "{{OWNER}}" not in text
assert "{{REPO}}" not in text
assert app.workflow_instance(gh.files[".github/workflows/syd2.yml"]) == "syd2"
print("bulk create on brand-new repo (embedded template) OK")

# default prefix when none is hinted; copy mode degrades to raw there
gh = FakeGH({})
created = app.create_instances(gh, CFG, 1, log, mode="copy")
assert created == ["op1"], created
assert not any(p.startswith("opencode/instances/") for p in gh.files)
print("brand-new repo default prefix + copy->raw OK")

# the embedded render carries the live fleets' identity pattern
t = app.embedded_template("rey1", "Shabakebehdasht/my-opencode")
assert 'git config user.name "Shabakebehdasht"' in t
assert "https://github.com/Shabakebehdasht/h-dashboard.git" in t
assert "github.com/Shabakebehdasht/my-opencode.git" in t
assert app.workflow_timeout(t) == 340
out = app.renamed_workflow(t, "rey7")
assert app.workflow_instance(out) == "rey7"
print("embedded template render OK")

# --- bulk delete: checked machines, one commit, config untouched --------------
files = {}
for n in (1, 2, 3):
    files[f".github/workflows/rey{n}.yml"] = app.renamed_workflow(SAMPLE, f"rey{n}")
    files[f"opencode/instances/rey{n}/db.sqlite"] = "state"
files["opencode/config/skills/x.md"] = "shared"
gh = FakeGH(files, active={"rey2.yml": 42})
deleted, skipped = app.delete_instances(gh, CFG, ["rey1", "rey2", "rey3"], log)
assert deleted == ["rey1", "rey2", "rey3"] and skipped == [], (deleted, skipped)
assert gh.commits == 1, gh.commits
assert not any("rey" in p for p in gh.files), gh.files.keys()
assert gh.files["opencode/config/skills/x.md"] == "shared"
print("bulk delete (one commit, config untouched) OK")

# --- bulk delete: a machine whose run will not stop is skipped -----------------
gh = FakeGH(files, active={"rey2.yml": 77}, stuck={77})
deleted, skipped = app.delete_instances(gh, CFG, ["rey1", "rey2"], log)
assert deleted == ["rey1"] and skipped == ["rey2"], (deleted, skipped)
assert ".github/workflows/rey2.yml" in gh.files
assert "opencode/instances/rey2/db.sqlite" in gh.files
assert ".github/workflows/rey1.yml" not in gh.files
print("bulk delete skip-on-stuck-run OK")

# --- create with COPY mode: state cloned from the first machine ------------------
files = {".github/workflows/rey1.yml": SAMPLE,
         "opencode/instances/rey1/data/opencode.db": "sessions+history",
         "opencode/instances/rey1/data/shell/x/out.txt": "shell log",
         "opencode/instances/rey1/cbm-cache/h-dashboard.db": "cbm index",
         "opencode/instances/rey1/9router.sqlite": "9router db",
         "opencode/config/skills/x.md": "shared"}
gh = FakeGH(files)
created = app.create_instances(gh, CFG, 1, log, mode="copy")
assert created == ["rey2"], created
assert gh.commits == 1, gh.commits  # workflows + state in ONE commit
assert gh.files["opencode/instances/rey2/data/opencode.db"] == "sessions+history"
assert gh.files["opencode/instances/rey2/cbm-cache/h-dashboard.db"] == "cbm index"
assert gh.files["opencode/instances/rey2/9router.sqlite"] == "9router db"
assert gh.files["opencode/instances/rey2/data/shell/x/out.txt"] == "shell log"
# the template's own state is untouched, config untouched
assert gh.files["opencode/instances/rey1/data/opencode.db"] == "sessions+history"
assert gh.files["opencode/config/skills/x.md"] == "shared"
print("create copy mode (state cloned, one commit) OK")

# bulk copy: every new machine gets the template's state
gh = FakeGH(files)
created = app.create_instances(gh, CFG, 3, log, mode="copy")
assert created == ["rey2", "rey3", "rey4"], created
for n in ("rey2", "rey3", "rey4"):
    assert gh.files[f"opencode/instances/{n}/9router.sqlite"] == "9router db"
assert gh.commits == 1
print("bulk create copy mode OK")

# copy mode with NO backed-up state degrades to raw, still one commit
gh = FakeGH({".github/workflows/rey1.yml": SAMPLE})
created = app.create_instances(gh, CFG, 1, log, mode="copy")
assert created == ["rey2"], created
assert not any(p.startswith("opencode/instances/") for p in gh.files)
assert gh.commits == 1
print("copy mode without state -> raw fallback OK")

# raw mode (default) never copies state
gh = FakeGH(files)
created = app.create_instances(gh, CFG, 1, log)
assert created == ["rey2"] and not any(
    p.startswith("opencode/instances/rey2") for p in gh.files)
print("raw mode (default) OK")

# --- rename prefix in one commit ------------------------------------------------
files = {}
for n in (1, 2, 3):
    files[f".github/workflows/rey{n}.yml"] = app.renamed_workflow(SAMPLE, f"rey{n}")
    files[f"opencode/instances/rey{n}/db.sqlite"] = "state"
files["opencode/config/skills/x.md"] = "shared"
gh = FakeGH(files)
assert app.rename_prefix_op(gh, CFG, "rey", "fork", log)
assert gh.commits == 1
assert app.workflow_instance(gh.files[".github/workflows/fork1.yml"]) == "fork1"
assert "opencode/instances/fork2/db.sqlite" in gh.files
assert not any(p.startswith("opencode/instances/rey") for p in gh.files)
assert gh.files["opencode/config/skills/x.md"] == "shared"
print("rename prefix OK")

print("ALL OK")

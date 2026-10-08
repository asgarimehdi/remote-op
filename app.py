"""Remote-Op - minimal manager. Settings saved to settings.json."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import urllib.request
import webbrowser

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

SERVERS = [f"http://open{i}:4096" for i in range(1, 5)]
DEFAULTS = {"repo": "", "password": "", "branch": ""}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            m = dict(DEFAULTS)
            m.update({k: d[k] for k in DEFAULTS if k in d})
            # migrate old "ref" key
            if "branch" not in m and "ref" in d:
                m["branch"] = d["ref"]
            return m
        except Exception:
            pass
    return dict(DEFAULTS)


def save_settings(d):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Remote-Op")
        self.geometry("800x750")
        self.settings = load_settings()
        self.log_q = queue.Queue()
        self.up_since = {}  # url -> timestamp when first seen UP
        self._triggering = False  # only one workflow trigger batch at a time
        os.environ["OPENCODE_PASSWORD"] = self.settings.get("password", "")
        self._build()
        self.after(100, self._drain_log)
        # initial load + start 5-min uptime loop
        self.after(500, self.reload_workflows)
        self.after(500, self.refresh_runs)
        self.after(1000, self._uptime_loop)

    # ----- UI -----
    def _build(self):
        # settings
        s = ttk.LabelFrame(self, text="Settings", padding=8)
        s.pack(fill="x", padx=8, pady=6)
        self.repo_var = tk.StringVar(value=self.settings["repo"])
        self.pass_var = tk.StringVar(value=self.settings["password"])
        self.branch_var = tk.StringVar(value=self.settings["branch"])
        ttk.Label(s, text="Repo:").grid(row=0, column=0, sticky="w")
        ttk.Entry(s, textvariable=self.repo_var, width=32).grid(row=0, column=1, padx=4)
        ttk.Label(s, text="Pass:").grid(row=0, column=2, sticky="w")
        self._pass_entry = ttk.Entry(s, textvariable=self.pass_var, width=14, show="*")
        self._pass_entry.grid(row=0, column=3, padx=4)
        ttk.Button(s, text="show", width=5, command=self._toggle_pass).grid(row=0, column=4)
        ttk.Label(s, text="Branch:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(s, textvariable=self.branch_var, width=20).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Button(s, text="Save", command=self.on_save).grid(row=1, column=3, pady=(6, 0))

        # workflows
        w = ttk.LabelFrame(self, text="Workflows (from remote)", padding=8)
        w.pack(fill="both", expand=True, padx=8, pady=4)
        wb = ttk.Frame(w)
        wb.pack(fill="x")
        ttk.Button(wb, text="Reload from remote", command=self.reload_workflows).pack(side="left", padx=2)
        ttk.Button(wb, text="Run selected", command=self.run_selected).pack(side="left", padx=2)
        ttk.Button(wb, text="Run ALL", command=self.run_all).pack(side="left", padx=2)
        self.wf_tree = ttk.Treeview(w, columns=("name", "state"), show="headings", height=6)
        self.wf_tree.heading("name", text="workflow")
        self.wf_tree.heading("state", text="state")
        self.wf_tree.column("name", width=300)
        self.wf_tree.column("state", width=120)
        self.wf_tree.pack(fill="both", expand=True, pady=(6, 0))

        # runs (for cancel / delete)
        r = ttk.LabelFrame(self, text="Runs (cancel / delete one / all)", padding=8)
        r.pack(fill="both", expand=True, padx=8, pady=4)
        rb = ttk.Frame(r)
        rb.pack(fill="x")
        ttk.Button(rb, text="Refresh", command=self.refresh_runs).pack(side="left", padx=2)
        ttk.Button(rb, text="Cancel selected", command=self.cancel_selected).pack(side="left", padx=2)
        ttk.Button(rb, text="Cancel ALL", command=self.cancel_all).pack(side="left", padx=2)
        ttk.Button(rb, text="Delete ALL", command=self.delete_all).pack(side="left", padx=2)
        self.run_tree = ttk.Treeview(r, columns=("num", "wf", "status", "branch", "dbid"),
                                     show="headings", height=6)
        for c, t, wd in [("num", "#", 60), ("wf", "workflow", 200),
                         ("status", "status", 110), ("branch", "branch", 110), ("dbid", "id", 110)]:
            self.run_tree.heading(c, text=t)
            self.run_tree.column(c, width=wd)
        self.run_tree.pack(fill="both", expand=True, pady=(6, 0))

        # uptime (every 5 min)
        u = ttk.LabelFrame(self, text="Uptime — checked every 5 min", padding=8)
        u.pack(fill="x", padx=8, pady=4)
        uh = ttk.Frame(u)
        uh.pack(fill="x")
        self.uptime_var = tk.StringVar(value="last check: never")
        ttk.Label(uh, textvariable=self.uptime_var).pack(side="left")
        ttk.Button(uh, text="Check now", command=lambda: self.run_bg(self._check_uptime)).pack(side="right")
        self.srv_labels = {}
        for url in SERVERS:
            row = ttk.Frame(u)
            row.pack(fill="x")
            link = tk.Label(row, text=url, width=22, fg="blue", cursor="hand2", font=("TkDefaultFont", 9, "underline"))
            link.pack(side="left")
            link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
            lbl = ttk.Label(row, text="… checking", foreground="gray")
            lbl.pack(side="left", padx=8)
            self.srv_labels[url] = lbl
            ttk.Button(row, text="Connect", width=9,
                       command=lambda u=url: self.connect_server(u)).pack(side="right", padx=2)

        # log
        lf = ttk.LabelFrame(self, text="Log", padding=6)
        lf.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log_text = scrolledtext.ScrolledText(lf, height=6, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    def _toggle_pass(self):
        self._pass_entry.config(show="" if self._pass_entry.cget("show") == "*" else "*")

    # ----- settings -----
    def on_save(self):
        self.settings = {"repo": self.repo_var.get().strip(),
                         "password": self.pass_var.get(),
                         "branch": self.branch_var.get().strip() or "opc"}
        save_settings(self.settings)
        # set $env:OPENCODE_PASSWORD for this process + persist for future cmd/PowerShell
        os.environ["OPENCODE_PASSWORD"] = self.settings["password"]
        self.log(f"$env:OPENCODE_PASSWORD set ({len(self.settings['password'])} chars)")
        self.log(f"Saved: {self.settings['repo']} / branch={self.settings['branch']}")
        if os.name == "nt":
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.run(["setx", "OPENCODE_PASSWORD", self.settings["password"]],
                               capture_output=True, text=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                               startupinfo=si)
                self.log("Persisted via setx — new terminals will have OPENCODE_PASSWORD.")
            except Exception as e:
                self.log(f"setx failed (session-only): {e}")

    # ----- helpers -----
    def log(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        try:
            while True:
                m = self.log_q.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", m + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def run_bg(self, fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()

    def _gh(self, args):
        cmd = ["gh"] + args + ["--repo", self.repo_var.get().strip()]
        self.log("$ " + " ".join(cmd))
        # hide console window on Windows (no cmd flash)
        no_window = {}
        if os.name == "nt":
            no_window["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            no_window["startupinfo"] = si
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=APP_DIR, timeout=60, **no_window)
            if p.stdout.strip():
                self.log(p.stdout.strip()[:2000])
            if p.returncode != 0 and p.stderr.strip():
                self.log(p.stderr.strip()[:2000])
            return p.returncode, p.stdout
        except FileNotFoundError:
            self.log("ERROR: `gh` not found in PATH.")
            return 1, ""
        except Exception as e:
            self.log(f"ERROR: {e}")
            return 1, ""

    # ----- workflows -----
    def reload_workflows(self):
        self.run_bg(self._reload_workflows)

    def _reload_workflows(self):
        rc, out = self._gh(["workflow", "list", "--json", "name,state", "--limit", "50"])
        if rc != 0:
            return
        try:
            rows = json.loads(out or "[]")
        except Exception as e:
            self.log(f"Parse error: {e}")
            return
        self.after(0, lambda: self._fill_wf(rows))

    def _fill_wf(self, rows):
        for i in self.wf_tree.get_children():
            self.wf_tree.delete(i)
        for r in rows:
            self.wf_tree.insert("", "end", values=(r.get("name"), r.get("state")))
        self.log(f"{len(rows)} workflows loaded.")

    def _wf_names(self, selected_only):
        items = self.wf_tree.selection() if selected_only else self.wf_tree.get_children()
        if selected_only and not items:
            messagebox.showwarning("No selection", "Select a workflow first.")
            return []
        return [self.wf_tree.item(i, "values")[0] for i in items]

    def run_selected(self):
        if self._triggering:
            self.log("A trigger batch is already running — wait for it to finish.")
            return
        names = self._wf_names(True)
        if names:
            self.run_bg(self._trigger, names)

    def run_all(self):
        if self._triggering:
            self.log("A trigger batch is already running — wait for it to finish.")
            return
        names = self._wf_names(False)
        if not names:
            self.log("No workflows loaded — press Reload first.")
            return
        self.run_bg(self._trigger, names)

    ACTIVE_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}

    def _active_workflows(self):
        """Return set of workflow names with an active run on current branch."""
        branch = self.branch_var.get().strip() or "opc"
        rc, out = self._gh(["run", "list", "--limit", "50", "--json",
                            "workflowName,status,headBranch"])
        if rc != 0:
            return set()
        try:
            rows = json.loads(out or "[]")
        except Exception:
            return set()
        return {r.get("workflowName") for r in rows
                if r.get("status") in self.ACTIVE_STATUSES
                and (not branch or r.get("headBranch") == branch)}

    def _trigger(self, names):
        if self._triggering:
            return
        self._triggering = True
        try:
            branch = self.branch_var.get().strip() or "opc"
            active = self._active_workflows()
            for n in names:
                if n in active:
                    self.log(f"skip {n}: already running @ {branch}")
                    continue
                rc, _ = self._gh(["workflow", "run", n, "--ref", branch])
                if rc == 0:
                    self.log(f"triggered {n} @ {branch}")
                    active.add(n)  # prevent double-trigger within same batch
            self._refresh_runs_sync()
        finally:
            self._triggering = False

    def connect_server(self, url):
        """Open a visible terminal and run opencode --server <url> with password set."""
        pwd = self.pass_var.get().replace("'", "''")
        ps_cmd = f"$env:OPENCODE_PASSWORD='{pwd}'; opencode --server {url}"
        self.log(ps_cmd)
        try:
            if os.name == "nt":
                subprocess.Popen(
                    ["powershell.exe", "-NoExit", "-Command", ps_cmd],
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                subprocess.Popen(["opencode", "--server", url],
                                 env={**os.environ, "OPENCODE_PASSWORD": self.pass_var.get()})
        except FileNotFoundError:
            self.log("ERROR: powershell/opencode not found in PATH.")
        except Exception as e:
            self.log(f"Connect failed: {e}")

    # ----- runs / cancel -----
    def refresh_runs(self):
        self.run_bg(self._refresh_runs_sync)

    def _refresh_runs_sync(self):
        rc, out = self._gh(["run", "list", "--limit", "50", "--json",
                            "databaseId,number,workflowName,status,conclusion,headBranch"])
        if rc != 0:
            return
        try:
            rows = json.loads(out or "[]")
        except Exception as e:
            self.log(f"Parse error: {e}")
            return
        self.after(0, lambda: self._fill_runs(rows))

    def _fill_runs(self, rows):
        for i in self.run_tree.get_children():
            self.run_tree.delete(i)
        for r in rows:
            st = r.get("status") or ""
            if r.get("conclusion"):
                st += f"/{r['conclusion']}"
            self.run_tree.insert("", "end", values=(r.get("number"), r.get("workflowName"),
                                                    st, r.get("headBranch"), r.get("databaseId")))

    def _run_ids(self, selected_only):
        items = self.run_tree.selection() if selected_only else self.run_tree.get_children()
        if selected_only and not items:
            messagebox.showwarning("No selection", "Select a run first.")
            return []
        return [str(self.run_tree.item(i, "values")[4]) for i in items]

    def cancel_selected(self):
        ids = self._run_ids(True)
        if ids:
            self.run_bg(self._cancel, ids)

    def cancel_all(self):
        ids = self._run_ids(False)
        if not ids:
            self.log("No runs to cancel.")
            return
        if not messagebox.askyesno("Confirm", f"Cancel ALL {len(ids)} runs?"):
            return
        self.run_bg(self._cancel, ids)

    def _cancel(self, ids):
        for rid in ids:
            self._gh(["run", "cancel", rid])
        self._refresh_runs_sync()

    def delete_all(self):
        ids = self._run_ids(False)
        if not ids:
            self.log("No runs to delete.")
            return
        if not messagebox.askyesno("Confirm", f"DELETE all {len(ids)} runs? (gh run delete)"):
            return
        self.run_bg(self._delete, ids)

    def _delete(self, ids):
        for rid in ids:
            self._gh(["run", "delete", rid])
        self._refresh_runs_sync()

    # ----- uptime every 5 min -----
    def _uptime_loop(self):
        self.run_bg(self._check_uptime)
        self.after(5 * 60 * 1000, self._uptime_loop)  # every 5 min

    def _check_uptime(self):
        for url in SERVERS:
            ok = self._ping(url)
            now = time.strftime("%H:%M:%S")
            if ok:
                if url not in self.up_since:
                    self.up_since[url] = time.time()
                up_for = time.time() - self.up_since[url]
                h, rem = divmod(int(up_for), 3600)
                m, s = divmod(rem, 60)
                txt = f"● UP  uptime {h}h {m}m {s}s"
                col = "green"
            else:
                self.up_since.pop(url, None)
                txt = "● DOWN"
                col = "red"
            self.after(0, self._set_srv_label, url, txt, col)
        self.after(0, lambda: self.uptime_var.set(f"last check: {time.strftime('%H:%M:%S')} (every 5 min)"))

    def _set_srv_label(self, url, txt, col):
        lbl = self.srv_labels.get(url)
        if lbl:
            lbl.config(text=txt, foreground=col)

    @staticmethod
    def _ping(url, timeout=6):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.status < 500
        except Exception:
            return False


if __name__ == "__main__":
    App().mainloop()

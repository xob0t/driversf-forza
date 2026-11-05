"""The car manager window (tkinter, which ships with uv's Python): installed cars, who drives
in traffic, the traffic sets and interesting vehicles. It calls the same functions as the CLI
(car_manager.run_key_command, traffic.TrafficControl), on a worker thread for anything that
writes files, and shows their log.

    uv run dsff car_manager gui          (cars.cmd in a release)
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from dsfforza.build import traffic

DEFAULT = "Default (every story set)"
SOURCES = ("All sources", "FH1", "FM4", "DLC")
VEHICLES_TSV = Path(__file__).resolve().parents[3] / "research" / "vehicles.tsv"


def vanilla_names() -> dict[int, str]:
    """{spool ID: model name} of the vanilla cars (research/vehicles.tsv), for the pool list."""
    try:
        lines = VEHICLES_TSV.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return {}
    out = {}
    for line in lines:
        cols = line.split("\t")
        if len(cols) >= 4 and cols[1].isdigit():
            out[int(cols[1])] = cols[3].split("|")[0].replace("_", " ")
    return out


class App:
    def __init__(self, root: tk.Tk, manager):
        self.root, self.m = root, manager
        self.tc = traffic.TrafficControl(manager)
        self.names = vanilla_names()
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False
        manager.log = self.log
        root.title(f"Driver SF car manager: {manager.game}")
        root.geometry("1100x720")
        tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True)
        self.cars_tab = ttk.Frame(tabs)
        self.sets_tab = ttk.Frame(tabs)
        self.iv_tab = ttk.Frame(tabs)
        tabs.add(self.cars_tab, text="Cars")
        tabs.add(self.sets_tab, text="Traffic sets")
        tabs.add(self.iv_tab, text="Interesting vehicles")
        self.logbox = tk.Text(root, height=8, state="disabled")
        self.logbox.pack(fill="x")
        self.build_cars()
        self.build_sets()
        self.build_iv()
        self.refresh_cars()
        self.load_set()
        root.after(100, self.poll)

    # --- plumbing ---

    def log(self, text: str) -> None:
        self.jobs.put(("log", str(text)))

    def poll(self) -> None:
        while not self.jobs.empty():
            kind, value = self.jobs.get()
            if kind == "log":
                self.logbox.configure(state="normal")
                self.logbox.insert("end", value + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
            elif kind == "done":
                self.busy = False
                self.root.configure(cursor="")
                self.refresh_cars()
                self.load_set()
        self.root.after(100, self.poll)

    def run(self, what: str, work) -> None:
        """Run `work` (writes files) on a worker thread, one at a time."""
        if self.busy:
            messagebox.showinfo("Busy", "The last change is still being written.")
            return
        self.busy = True
        self.root.configure(cursor="watch")
        self.log(f"{what}...")

        def body():
            try:
                work()
                self.log(f"{what}: done")
            except (SystemExit, ValueError, OSError) as e:
                self.log(f"{what}: {e}")
            finally:
                self.jobs.put(("done", None))

        threading.Thread(target=body, daemon=True).start()

    def save_traffic(self) -> None:
        self.m.save_state()
        for r in self.tc.write():
            for note in r.notes:
                self.log(f"traffic: {r.name}: {note}")

    # --- cars tab ---

    def build_cars(self) -> None:
        bar = ttk.Frame(self.cars_tab)
        bar.pack(fill="x")
        self.filter = tk.StringVar()
        self.make = tk.StringVar(value="All makes")
        self.source = tk.StringVar(value=SOURCES[0])
        ttk.Entry(bar, textvariable=self.filter, width=24).pack(side="left", padx=4, pady=4)
        self.make_box = ttk.Combobox(bar, textvariable=self.make, state="readonly", width=20)
        self.make_box.pack(side="left")
        ttk.Combobox(bar, textvariable=self.source, values=SOURCES, state="readonly", width=12).pack(side="left")
        for var in (self.filter, self.make, self.source):
            var.trace_add("write", lambda *_: self.refresh_cars())
        cols = ("id", "name", "make", "source", "enabled", "traffic")
        self.tree = ttk.Treeview(self.cars_tab, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, (50, 330, 160, 70, 70, 70)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        buttons = ttk.Frame(self.cars_tab)
        buttons.pack(fill="x")
        for text, cmd in (
            ("Select shown", lambda: self.tree.selection_set(self.tree.get_children())),
            ("Select none", lambda: self.tree.selection_set(())),
            ("Enable", lambda: self.key_command("enable")),
            ("Disable", lambda: self.key_command("disable")),
            ("Traffic on", lambda: self.traffic_cars(on=True)),
            ("Traffic off", lambda: self.traffic_cars(on=False)),
        ):
            ttk.Button(buttons, text=text, command=cmd).pack(side="left", padx=4, pady=4)

    def shown(self, car: dict) -> bool:
        text = self.filter.get().lower()
        if text and text not in car["name"].lower():
            return False
        if self.make.get() not in ("All makes", car["make"]):
            return False
        src = self.source.get()
        return src == SOURCES[0] or (src == "DLC" and car["dlc"]) or (src == car["game"] and not car["dlc"])

    def refresh_cars(self) -> None:
        cars = self.tc.cars()
        self.make_box["values"] = ["All makes"] + sorted({c["make"] for c in cars.values()})
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for key, c in cars.items():
            if self.shown(c):
                src = "DLC" if c["dlc"] else c["game"]
                values = (
                    c["id"],
                    c["name"],
                    c["make"],
                    src,
                    "yes" if c["enabled"] else "no",
                    "on" if c["traffic"] else "off",
                )
                self.tree.insert("", "end", iid=key, values=values)
        self.tree.selection_set([k for k in selected if self.tree.exists(k)])

    def selected_keys(self) -> list[str]:
        keys = list(self.tree.selection())
        if not keys:
            messagebox.showinfo("No cars", "Select cars in the list first.")
        return keys

    def key_command(self, cmd: str) -> None:
        from dsfforza.build import car_manager

        keys = self.selected_keys()
        if keys:
            self.run(f"{cmd} {len(keys)} cars", lambda: car_manager.run_key_command(self.m, cmd, keys))

    def traffic_cars(self, *, on: bool) -> None:
        keys = self.selected_keys()
        if keys:
            self.tc.set_traffic(keys, on=on)
            self.run(f"traffic {'on' if on else 'off'} for {len(keys)} cars", self.save_traffic)

    # --- traffic sets tab ---

    def build_sets(self) -> None:
        top = ttk.Frame(self.sets_tab)
        top.pack(fill="x")
        self.set_name = tk.StringVar(value=DEFAULT)
        box = ttk.Combobox(top, textvariable=self.set_name, values=[DEFAULT, *traffic.STORY_SETS], state="readonly")
        box.pack(side="left", padx=4, pady=4)
        box.bind("<<ComboboxSelected>>", lambda _e: self.load_set())
        self.ports = tk.IntVar(value=0)
        self.share = tk.DoubleVar(value=0.2)
        self.mode = tk.StringVar(value="vanilla")
        self.blend = tk.DoubleVar(value=0.0)
        ttk.Label(top, text="Ports").pack(side="left")
        ttk.Spinbox(top, from_=0, to=40, textvariable=self.ports, width=4).pack(side="left")
        ttk.Label(top, text="Share").pack(side="left", padx=(8, 0))
        ttk.Scale(top, from_=0.0, to=0.9, variable=self.share, length=120).pack(side="left")
        for mode in traffic.WEIGHT_MODES:
            ttk.Radiobutton(top, text=mode, value=mode, variable=self.mode).pack(side="left", padx=2)
        ttk.Label(top, text="Blend").pack(side="left", padx=(8, 0))
        ttk.Scale(top, from_=0.0, to=1.0, variable=self.blend, length=120).pack(side="left")
        self.budget = ttk.Label(self.sets_tab, text="")
        self.budget.pack(fill="x", padx=4)
        cols = ("id", "model", "state", "factor", "now", "after")
        self.pool = ttk.Treeview(self.sets_tab, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, (60, 330, 90, 60, 70, 70)):
            self.pool.heading(c, text=c.capitalize())
            self.pool.column(c, width=w, anchor="w")
        self.pool.pack(fill="both", expand=True)
        bar = ttk.Frame(self.sets_tab)
        bar.pack(fill="x")
        for text, cmd in (
            ("Preview", self.preview),
            ("On / off", self.toggle_models),
            ("Factor...", self.set_factor),
            ("Add from other chapters...", self.add_extra),
            ("Apply", self.apply_set),
            ("Reset this set", self.reset_set),
            ("Shuffle ports", self.shuffle),
            ("No set changes", self.ambient_off),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=4, pady=4)
        self.pending: dict = {}

    def current_set(self) -> str | None:
        name = self.set_name.get()
        return None if name == DEFAULT else name

    def load_set(self) -> None:
        amb = self.tc.t["ambient"]
        name = self.current_set()
        s = traffic.set_settings(amb, name or traffic.STORY_SETS[0])
        if name is None:
            s = dict(traffic.DEFAULT_SET) | (amb.get("default") or {})
        self.pending = {k: s.get(k, traffic.DEFAULT_SET[k]) for k in traffic.DEFAULT_SET}
        # ports chosen by key stay chosen unless the count is changed here
        self.port_keys = list(s["ports"]) if isinstance(s["ports"], list) else None
        self.ports.set(s["ports"] if isinstance(s["ports"], int) else len(s["ports"]))
        self.share.set(float(s.get("share") or 0.0))
        self.mode.set(s.get("weights") or "vanilla")
        self.blend.set(float(s.get("blend") or 0.0))
        self.preview()

    def gather(self) -> dict:
        out = dict(self.pending)
        count = int(self.ports.get())
        out["ports"] = self.port_keys if self.port_keys and count == len(self.port_keys) else count
        out["share"] = round(float(self.share.get()), 3)
        out["weights"] = self.mode.get()
        out["blend"] = round(float(self.blend.get()), 3)
        return out

    def preview(self) -> None:
        """The set's models now and with the settings on screen (a dry run of traffic.plan)."""
        from dsfforza.game import traffic_ai

        name = self.current_set() or traffic.STORY_SETS[0]
        try:
            ai = traffic_ai.AiTraffic.read(self.tc.game_ai(), traffic_ai.known_names())
            ts = ai.by_name(name)
            now = ai.share(ts)
            amb = dict(self.tc.t["ambient"])
            if self.current_set() is None:
                amb["default"] = self.gather()
            else:
                amb["sets"] = dict(amb.get("sets") or {}) | {name: self.gather()}
            slots, pots = self.tc.facts()
            ids = [i for i, _ in self.tc.in_traffic()]
            key_ids = {k: c["id"] for k, c in self.tc.cars().items()}
            reports = traffic.plan(ai, amb, ids, key_ids, slots=slots, pots=pots, seed=amb.get("seed", 0))
            after = ai.share(ai.by_name(name))
        except (OSError, ValueError, KeyError, SystemExit) as e:
            self.budget.configure(text=f"{name}: {e}")
            return
        rep = next((r for r in reports if r.name == name), None)
        self.show_pool(name, now, after, rep)

    def show_pool(self, name: str, now: dict, after: dict, rep) -> None:
        self.pool.delete(*self.pool.get_children())
        ports = {c["id"]: c["name"] for c in self.tc.cars().values()}
        off = set(self.pending.get("vanilla_off") or [])
        factors = self.pending.get("custom") or {}
        for mid in sorted(set(now) | set(after) | off):
            kind = "port" if mid in ports else ("added" if mid not in now and mid not in off else "vanilla")
            state = "off" if mid in off else kind
            label = ports.get(mid) or self.names.get(mid, "")
            factor = factors.get(str(mid), factors.get(mid, ""))
            values = (mid, label, state, factor, f"{now.get(mid, 0):.1%}", f"{after.get(mid, 0):.1%}")
            self.pool.insert("", "end", iid=str(mid), values=values)
        if rep:
            text = f"{name}: {rep.models} models of {rep.budget} (slot budget)"
            if rep.reaches:
                text += f"; also changes {', '.join(rep.reaches)}"
            text += "".join(f"; {n}" for n in rep.notes)
        else:
            text = f"{name}: as the game has it"
        self.budget.configure(text=text)

    def toggle_models(self) -> None:
        off = set(self.pending.get("vanilla_off") or [])
        extra = list(self.pending.get("extra") or [])
        for iid in self.pool.selection():
            mid = int(iid)
            if mid in extra:
                extra.remove(mid)
            elif mid in off:
                off.discard(mid)
            elif mid not in {c["id"] for c in self.tc.cars().values()}:
                off.add(mid)
        self.pending["vanilla_off"], self.pending["extra"] = sorted(off), extra
        self.preview()

    def set_factor(self) -> None:
        sel = self.pool.selection()
        if not sel:
            return
        value = simpledialog.askfloat("Factor", "Weight factor for the selected models (1 = as is):", minvalue=0.0)
        if value is None:
            return
        custom = dict(self.pending.get("custom") or {})
        for iid in sel:
            custom[iid] = value
        self.pending["custom"] = custom
        self.mode.set("custom")
        self.preview()

    def add_extra(self) -> None:
        text = simpledialog.askstring("Add models", "Vanilla model IDs from other chapters' sets (e.g. 136, 249):")
        if not text:
            return
        ids = [int(x) for x in text.replace(";", ",").split(",") if x.strip().isdigit()]
        self.pending["extra"] = sorted(set(self.pending.get("extra") or []) | set(ids))
        self.preview()

    def apply_set(self) -> None:
        fields = self.gather()
        self.tc.set_ambient(self.current_set(), **fields)
        self.run(f"traffic set {self.set_name.get()}", self.save_traffic)

    def reset_set(self) -> None:
        name = self.current_set()
        amb = self.tc.t["ambient"]
        if name is None:
            amb.pop("default", None)
        else:
            (amb.get("sets") or {}).pop(name, None)
        self.run(f"reset {self.set_name.get()}", self.save_traffic)

    def shuffle(self) -> None:
        self.tc.shuffle()
        self.run("shuffle the ports", self.save_traffic)

    def ambient_off(self) -> None:
        if messagebox.askyesno("No set changes", "Put every traffic set back as the game has it?"):
            self.tc.ambient_off()
            self.run("traffic sets as the game has them", self.save_traffic)

    # --- interesting vehicles tab ---

    def build_iv(self) -> None:
        iv = self.tc.t["iv"]
        self.iv_ports = tk.BooleanVar(value=iv.get("ports") is not False)
        self.iv_vanilla = tk.BooleanVar(value=iv.get("vanilla") is not False)
        self.per_region = tk.IntVar(value=int(iv.get("per_region", 24)))
        self.regions = tk.StringVar()
        frame = ttk.Frame(self.iv_tab, padding=8)
        frame.pack(fill="both", expand=True)
        ttk.Checkbutton(frame, text="Cars in traffic also come as interesting vehicles", variable=self.iv_ports).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Checkbutton(frame, text="The game's own interesting vehicles", variable=self.iv_vanilla).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(16, 0)
        )
        ttk.Label(frame, text="Cars per region (at most 128 IDs with the game's)").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(frame, from_=0, to=128, textvariable=self.per_region, width=5).grid(row=1, column=1, sticky="w")
        ttk.Button(frame, text="Apply", command=self.apply_iv).grid(row=2, column=0, sticky="w", pady=8)
        ttk.Label(frame, text="Pin the cars selected on the Cars tab to regions (1 to 17, e.g. 6,9):").grid(
            row=3, column=0, sticky="w", pady=(16, 0)
        )
        ttk.Entry(frame, textvariable=self.regions, width=20).grid(row=3, column=1, sticky="w", pady=(16, 0))
        ttk.Button(frame, text="Pin", command=lambda: self.pin(unpin=False)).grid(row=4, column=0, sticky="w")
        ttk.Button(frame, text="Unpin", command=lambda: self.pin(unpin=True)).grid(row=4, column=1, sticky="w")

    def apply_iv(self) -> None:
        self.tc.t["iv"]["ports"] = bool(self.iv_ports.get())
        self.tc.t["iv"]["per_region"] = int(self.per_region.get())
        self.tc.t["iv"]["vanilla"] = bool(self.iv_vanilla.get())
        self.run("interesting vehicles", self.save_traffic)

    def pin(self, *, unpin: bool) -> None:
        keys = self.selected_keys()
        if not keys:
            return
        regions = None
        if not unpin:
            try:
                regions = [int(x) for x in self.regions.get().replace(";", ",").split(",") if x.strip()]
            except ValueError:
                messagebox.showerror("Regions", "Regions are numbers 1 to 17, separated by commas.")
                return
        try:
            self.tc.pin(keys, regions)
        except SystemExit as e:
            messagebox.showerror("Regions", str(e))
            return
        self.run(f"{'unpin' if unpin else 'pin'} {len(keys)} cars", self.save_traffic)


def run(manager) -> None:
    """The window, for car_manager's `gui` command."""
    root = tk.Tk()
    App(root, manager)
    root.mainloop()

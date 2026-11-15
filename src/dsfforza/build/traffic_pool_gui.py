"""Choose a chapter's Forza candidate pool without limiting it to the loaded-model budget."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class PoolDialog:
    """Keep membership across search changes; only Use pool returns an edit to the caller."""

    def __init__(self, parent, cars: dict, pool):
        self.cars = cars
        self.keys = None if pool == "all" else set(pool)
        self.result = None
        self.window = tk.Toplevel(parent)
        self.window.title("Forza candidate pool")
        self.window.geometry("850x560")
        self.window.transient(parent)
        self.window.grab_set()
        self.search = tk.StringVar()
        self.summary = tk.StringVar()
        self.build()
        self.search.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def build(self) -> None:
        top = ttk.Frame(self.window, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Search name, make or source").pack(side="left")
        ttk.Entry(top, textvariable=self.search, width=32).pack(side="left", padx=8)
        ttk.Label(self.window, textvariable=self.summary).pack(fill="x", padx=8)
        ttk.Label(
            self.window,
            text="Cars disabled or switched off in Traffic stay excluded. The pool can contain every installed car.",
        ).pack(fill="x", padx=8)
        cols = ("pool", "name", "make", "source", "eligible")
        frame = ttk.Frame(self.window)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for col, width in zip(cols, (60, 340, 130, 70, 70)):
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=width)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        bar = ttk.Frame(self.window, padding=8)
        bar.pack(fill="x")
        for label, command in (
            ("Select shown", lambda: self.tree.selection_set(self.tree.get_children())),
            ("Add selected", lambda: self.edit(include=True)),
            ("Remove selected", lambda: self.edit(include=False)),
            ("All Forza cars", lambda: self.replace(None)),
            ("Clear", lambda: self.replace(set())),
            ("Use pool", self.accept),
            ("Cancel", self.window.destroy),
        ):
            ttk.Button(bar, text=label, command=command).pack(side="left", padx=2)

    def refresh(self) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        text = self.search.get().casefold()
        count = 0
        for key, car in self.cars.items():
            member = self.keys is None or key in self.keys
            eligible = car["enabled"] and car["traffic"]
            count += member and eligible
            source = "DLC" if car["dlc"] else car["game"]
            if text and text not in f"{car['name']} {car['make']} {car['game']} {source}".casefold():
                continue
            self.tree.insert(
                "",
                "end",
                iid=key,
                values=("yes" if member else "no", car["name"], car["make"], source, "yes" if eligible else "no"),
            )
        self.tree.selection_set([key for key in selected if self.tree.exists(key)])
        mode = "All Forza cars, including future additions" if self.keys is None else "Custom pool"
        self.summary.set(f"{mode}: {count} eligible candidates")

    def replace(self, keys: set | None) -> None:
        self.keys = keys
        self.refresh()

    def edit(self, *, include: bool) -> None:
        if self.keys is None:
            self.keys = set(self.cars)
        selected = set(self.tree.selection())
        if include:
            self.keys.update(selected)
        else:
            self.keys.difference_update(selected)
        self.refresh()

    def accept(self) -> None:
        self.result = "all" if self.keys is None else sorted(self.keys)
        self.window.destroy()

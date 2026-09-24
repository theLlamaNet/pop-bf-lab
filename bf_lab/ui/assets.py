"""Asset Browser controls and project open/close actions."""
from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional
import tkinter as tk
from ..models import Asset
from ..project import JadeProject


class AssetBrowserMixin:
    def _build_asset_tab(self) -> None:
        top = ttk.Frame(self.asset_tab)
        top.pack(fill="x")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh_assets())
        ttk.Label(top, text="Filter:").pack(side="left")
        ttk.Entry(top, textvariable=self.filter_var, width=40).pack(side="left", padx=6)
        self.asset_count = ttk.Label(top, text="0 assets")
        self.asset_count.pack(side="right")
        ttk.Button(top, text="Export all assets", command=self.extract_all_assets).pack(side="right", padx=6)
        ttk.Button(top, text="Extract selected asset", command=self.extract_selected).pack(side="right", padx=6)

        split = ttk.Panedwindow(self.asset_tab, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.Frame(split)
        right = ttk.Frame(split, padding=10)
        split.add(left, weight=3)
        split.add(right, weight=2)

        columns = ("index", "name", "size", "compression", "key")
        self.asset_tree = ttk.Treeview(left, columns=columns, show="headings")
        for col, text, width in [("index", "Index", 80), ("name", "Name", 360), ("size", "Size", 110),
                                 ("compression", "Compression", 110), ("key", "Key", 120)]:
            self.asset_tree.heading(col, text=text)
            self.asset_tree.column(col, width=width, anchor="w")
        y = ttk.Scrollbar(left, orient="vertical", command=self.asset_tree.yview)
        self.asset_tree.configure(yscrollcommand=y.set)
        self.asset_tree.pack(side="left", fill="both", expand=True)
        y.pack(side="right", fill="y")
        self.asset_tree.bind("<<TreeviewSelect>>", self.on_asset_selected)

        # Tk's native scrollbar cannot expose a corner radius, so use a slim,
        # quiet Win11-like track/thumb rather than the old bright default.

        ttk.Label(right, text="Asset details", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        self.asset_details = tk.Text(right, wrap="word", height=16, state="disabled",
                                     bg=self._dark["field"], fg=self._dark["fg"],
                                     insertbackground=self._dark["fg"], selectbackground=self._dark["select"],
                                     selectforeground=self._dark["fg"], relief="flat", borderwidth=1,
                                     highlightthickness=1, highlightbackground=self._dark["border_soft"],
                                     highlightcolor=self._dark["border"])
        self.asset_details.pack(fill="both", expand=True, pady=8)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=(2, 8))
        log_header = ttk.Frame(right)
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Log", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(log_header, text="Clear", command=self.clear_log).pack(side="right")
        log_frame = ttk.Frame(right)
        log_frame.pack(fill="both", expand=False, pady=(5, 0))
        self.log_text = tk.Text(log_frame, wrap="none", height=9, state="disabled",
                                bg="#101214", fg=self._dark["fg"],
                                insertbackground=self._dark["fg"], selectbackground=self._dark["select"],
                                selectforeground=self._dark["fg"], relief="flat", borderwidth=1,
                                highlightthickness=1, highlightbackground=self._dark["border_soft"],
                                font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def import_bf(self) -> None:
        self._log("INFO  Opening BF import dialog...")
        path = filedialog.askopenfilename(title="Import Jade Big File", filetypes=[("Jade Big Files", "*.bf"), ("All files", "*.*")])
        if not path:
            return
        try:
            self._log(f"INFO  Parsing BF: {path}")
            self.project.open_bf(Path(path))
            self.refresh_assets()
            self._refresh_title()
            self._refresh_bf_repack_info()
            self._log(f"INFO  BF opened: v{self.project.info.version}, {len(self.project.assets):,} indexed entries, FAT={self.project.info.num_fat}")
        except Exception as exc:
            self._log(f"ERROR Import BF: {exc}")
            messagebox.showerror("Import BF", str(exc))

    def import_bin(self) -> None:
        self._log("INFO  Opening BIN import dialog...")
        path = filedialog.askopenfilename(title="Import BIN", filetypes=[("BIN files", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        try:
            self._log(f"INFO  Reading BIN: {path}")
            bin_path = Path(path)
            raw = bin_path.read_bytes()
            self.project.open_bin(bin_path)
            self.refresh_assets()
            self._refresh_title()
            state = "POP-LZO decompressed" if self.project.direct_compressed else "uncompressed"
            self._log(f"INFO  BIN opened: raw={len(raw):,} B, decoded={len(self.project.decoded_bin or b''):,} B, {state}")
            if bin_path.stem.casefold() == "univers_oin_ff0c008e":
                self.analyze_current_ova()
            else:
                self.tabs.select(self.asset_tab)
        except Exception as exc:
            self._log(f"ERROR Import BIN: {exc}")
            messagebox.showerror("Import BIN", str(exc))

    def import_dec(self) -> None:
        self._log("INFO  Opening DEC import dialog...")
        path = filedialog.askopenfilename(title="Import DEC", filetypes=[("DEC files", "*.dec"), ("All files", "*.*")])
        if not path:
            return
        try:
            dec_path = Path(path)
            self.project.open_dec(dec_path)
            self._ova_data = bytearray(dec_path.read_bytes())
            self._ova_original = bytes(self._ova_data)
            self._ova_dirty = False
            self.refresh_assets()
            self._refresh_title()
            self._log(f"INFO  DEC opened: {dec_path} ({len(self._ova_data):,} B)")
            if dec_path.stem.casefold() == "univers_oin_ff0c008e":
                self.analyze_current_ova()
            else:
                self.tabs.select(self.asset_tab)
        except Exception as exc:
            self._log(f"ERROR Import DEC: {exc}")
            messagebox.showerror("Import DEC", str(exc))

    def close_project(self) -> None:
        self.project = JadeProject()
        self.refresh_assets()
        self._refresh_title()
        self._refresh_bf_repack_info()
        self._clear_tree(self.var_tree)
        self._set_text(self.ova_text, "")
        self._log("Project closed")

    def refresh_assets(self) -> None:
        if not hasattr(self, "asset_tree"):
            return
        self._clear_tree(self.asset_tree)
        self._asset_map.clear()
        needle = self.filter_var.get().lower() if hasattr(self, "filter_var") else ""
        count = 0
        for asset in self.project.assets:
            if needle and needle not in asset.name.lower():
                continue
            iid = f"asset_{asset.index}_{count}"
            self._asset_map[iid] = asset
            self.asset_tree.insert("", "end", iid=iid, values=(asset.index, asset.name, f"{asset.size:,}",
                                                                  "POP-LZO" if asset.compressed else "none", f"0x{asset.key:08X}"))
            count += 1
        self.asset_count.config(text=f"{count:,} assets")
        self.refresh_levels()

    def _selected_asset(self) -> Optional[Asset]:
        selection = self.asset_tree.selection()
        return self._asset_map.get(selection[0]) if selection else None

    def on_asset_selected(self, _event=None) -> None:
        asset = self._selected_asset()
        if not asset:
            return
        lines = [f"Name: {asset.name}", f"Index: {asset.index}", f"Key: 0x{asset.key:08X}",
                 f"Position: 0x{asset.position:08X}", f"Decoded size: {asset.size:,} bytes",
                 f"Compression: {'POP-LZO' if asset.compressed else 'none'}"]
        if self.project.kind == "bf":
            entry = next(e for e in self.project.info.entries if e.index == asset.index)
            lines += [f"FAT: {entry.fat_index}", f"Parent index: {entry.parent}"]
        self._set_text(self.asset_details, "\n".join(lines))

    def extract_selected(self) -> None:
        asset = self._selected_asset()
        if not asset:
            messagebox.showinfo("Extract", "Select an asset first.")
            return
        target = filedialog.asksaveasfilename(title="Extract asset", initialfile=asset.name)
        if not target:
            return
        try:
            self._log(f"INFO  Extracting asset #{asset.index}: {asset.name}")
            data = self.project.read_asset(asset)
            Path(target).write_bytes(data)
            self._log(f"OK    Extracted {asset.name}: {len(data):,} B -> {target}")
        except Exception as exc:
            self._log(f"ERROR Extract: {exc}")
            messagebox.showerror("Extract", str(exc))

"""OVA variable editor controls and editing actions."""
from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import os
import tkinter as tk
from ..archives import _repack_legacy_bigfile
from ..config import OVA_INFO_SIZE
from ..models import OvaVariable
from ..ova import (
    _find_ascii_fallback,
    find_variables,
    ova_diagnostic_report,
)


class OvaEditorMixin:
    def _build_ova_tab(self) -> None:
        bar = ttk.Frame(self.ova_tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="Analyze current asset", command=self.analyze_current_ova).pack(side="left")
        ttk.Button(bar, text="Diagnose", command=self.diagnose_ova).pack(side="left", padx=6)
        self.ova_mode_btn = ttk.Button(bar, text="OVA: Native Jade", command=self.toggle_ova_mode)
        self.ova_mode_btn.pack(side="left", padx=6)
        self.ova_source = ttk.Label(bar, text="No OVA source")
        self.ova_source.pack(side="right")

        split = ttk.Panedwindow(self.ova_tab, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(8, 0))
        left, right = ttk.Frame(split), ttk.Frame(split, padding=8)
        split.add(left, weight=2)
        split.add(right, weight=3)
        self.var_tree = ttk.Treeview(left, columns=("name", "value", "offset", "type", "flags"), show="headings")
        for c, t, w in [("name", "Variable", 230), ("value", "Value", 85), ("offset", "Value offset", 125), ("type", "Type", 70), ("flags", "Flags", 70)]:
            self.var_tree.heading(c, text=t)
            self.var_tree.column(c, width=w)
        self.var_tree.pack(fill="both", expand=True)
        self.var_tree.bind("<<TreeviewSelect>>", self.on_variable_selected)
        editor = ttk.Frame(right)
        editor.pack(fill="x", pady=(0, 8))
        self.ova_selected_label = ttk.Label(editor, text="Select an OVA variable")
        self.ova_selected_label.pack(anchor="w")
        candidate_bar = ttk.Frame(editor)
        candidate_bar.pack(fill="x", pady=7)
        ttk.Label(candidate_bar, text="Boolean candidates 00/01/FF:").pack(side="left")
        self.ova_candidates = ttk.Combobox(candidate_bar, state="readonly", width=52)
        self.ova_candidates.pack(side="left", fill="x", expand=True, padx=7)
        self.ova_candidates.bind("<<ComboboxSelected>>", self.use_ova_candidate)
        ttk.Button(candidate_bar, text="Find candidates", command=self.find_ova_candidates).pack(side="left")
        actions = ttk.Frame(editor)
        actions.pack(fill="x", pady=(0, 7))
        self.ova_false_btn = ttk.Button(actions, text="Set FALSE  00", command=lambda: self.set_ova_bool(0))
        self.ova_true_btn = ttk.Button(actions, text="Set TRUE  01", command=lambda: self.set_ova_bool(1))
        self.ova_false_btn.pack(side="left")
        self.ova_true_btn.pack(side="left", padx=7)
        self.ova_offset = tk.StringVar()
        ttk.Label(actions, text="Offset:").pack(side="left", padx=(18, 5))
        ttk.Entry(actions, textvariable=self.ova_offset, width=12).pack(side="left")
        value_bar = ttk.Frame(editor)
        value_bar.pack(fill="x", pady=(0, 7))
        ttk.Label(value_bar, text="Raw value (hex):").pack(side="left")
        self.ova_value_hex = tk.StringVar()
        ttk.Entry(value_bar, textvariable=self.ova_value_hex, width=42).pack(side="left", fill="x", expand=True, padx=7)
        self.ova_apply_value_btn = ttk.Button(value_bar, text="Apply value", command=self.apply_ova_raw_value)
        self.ova_apply_value_btn.pack(side="left")
        self.ova_hex_text = tk.Text(right, wrap="none", height=9, state="disabled", font=("Consolas", 9))
        self.ova_hex_text.pack(fill="both", expand=True)
        ttk.Label(right, text="Diagnostics / OVA descriptor").pack(anchor="w", pady=(8, 3))
        self.ova_text = tk.Text(right, wrap="word", height=10, state="disabled")
        self.ova_text.pack(fill="both", expand=True)
        self._set_ova_buttons(False)

    def analyze_current_ova(self) -> None:
        if self.project.kind in ("bin", "dec"):
            data = self.project.decoded_bin or b""
            label = self.project.path.name
        else:
            asset = self._selected_asset()
            if not asset:
                return
            try:
                data = self.project.read_asset(asset)
            except Exception as exc:
                messagebox.showerror("OVA", str(exc))
                return
            label = asset.name
        try:
            self._log(f"INFO  OVA analysis: {label}, {len(data):,} B")
            variables = find_variables(data)
            self._ova_data = bytearray(data)
            self._ova_original = bytes(data)
            self._ova_variables = variables
            self._ova_jade_variables = variables
            self._ova_ascii_variables = _find_ascii_fallback(data)
            self._ova_source_asset = None if self.project.kind in ("bin", "dec") else self._selected_asset()
            self._ova_dirty = False
            self._clear_tree(self.var_tree)
            for i, variable in enumerate(variables):
                value = "—"
                if variable.value_absolute is not None and variable.value_absolute < len(data):
                    size = variable.value_size or 1
                    value = bytes(data[variable.value_absolute:min(len(data), variable.value_absolute + size)]).hex(" ").upper()
                self.var_tree.insert("", "end", iid=f"var_{i}", values=(variable.name, value,
                    (f"0x{variable.value_absolute:08X}" if variable.value_absolute is not None else "—"), variable.var_type, variable.flags))
            self.ova_source.config(text=f"{label} — {len(variables)} variables")
            self._set_text(self.ova_text, "\n".join(ova_diagnostic_report(data, label)))
            self._set_text(self.ova_hex_text, "")
            self.ova_candidates["values"] = ()
            self.ova_offset.set("")
            self.ova_value_hex.set("")
            self.ova_selected_label.config(text="Select an OVA variable")
            self._set_ova_buttons(False)
            self.ova_apply_value_btn.configure(state="disabled")
            self.tabs.select(self.ova_tab)
            self._log(f"OK    OVA: {len(variables)} structural variables found")
        except Exception as exc:
            self._log(f"ERROR OVA: {exc}")
            messagebox.showerror("OVA", str(exc))

    def toggle_ova_mode(self) -> None:
        self.ova_variable_mode = "ascii" if self.ova_variable_mode == "jade" else "jade"
        self._ova_variables = self._ova_ascii_variables if self.ova_variable_mode == "ascii" else self._ova_jade_variables
        self.ova_mode_btn.config(text="OVA: ASCII fallback" if self.ova_variable_mode == "ascii" else "OVA: Native Jade")
        self._clear_tree(self.var_tree)
        for i, variable in enumerate(self._ova_variables):
            value = "—"
            if variable.value_absolute is not None and variable.value_absolute < len(self._ova_data):
                size = variable.value_size or 1
                value = bytes(self._ova_data[variable.value_absolute:min(len(self._ova_data), variable.value_absolute + size)]).hex(" ").upper()
            self.var_tree.insert("", "end", iid=f"var_{i}", values=(variable.name, value,
                (f"0x{variable.value_absolute:08X}" if variable.value_absolute is not None else f"0x{variable.offset:08X}"), variable.var_type or "—", variable.flags or "—"))
        self.ova_source.config(text=f"{self.project.title} — {len(self._ova_variables)} {'ASCII candidates' if self.ova_variable_mode == 'ascii' else 'OVA variables'}")
        self._log(f"INFO  OVA view: {'ASCII fallback' if self.ova_variable_mode == 'ascii' else 'Native Jade'} ({len(self._ova_variables)})")

    def on_variable_selected(self, _event=None) -> None:
        selection = self.var_tree.selection()
        if not selection:
            return
        idx = int(selection[0].split("_")[-1])
        if idx < 0 or idx >= len(self._ova_variables):
            return
        variable = self._ova_variables[idx]
        details = [f"{variable.name} • record 0x{variable.offset:08X}"]
        if variable.value_absolute is not None:
            size = variable.value_size or 1
            end = min(len(self._ova_data), variable.value_absolute + size)
            raw_value = bytes(self._ova_data[variable.value_absolute:end])
            details.append(f"value @ 0x{variable.value_absolute:08X} ({len(raw_value)} B)")
            self.ova_value_hex.set(raw_value.hex(" ").upper())
            self.ova_offset.set("+0")
            self._set_ova_buttons(True)
            self.ova_apply_value_btn.configure(state="normal")
        else:
            details.append("value buffer not located")
            self.ova_offset.set("")
            self.ova_value_hex.set("")
            self._set_ova_buttons(False)
            self.ova_apply_value_btn.configure(state="disabled")
        self.ova_selected_label.config(text=" • ".join(details))
        self.ova_candidates["values"] = ()
        self._ova_candidate_offsets = []
        self.show_ova_context(variable)
        self._log(f"OVA variable: {variable.name} @ 0x{variable.offset:08X}")

    def _set_ova_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.ova_false_btn.configure(state=state)
        self.ova_true_btn.configure(state=state)

    def _selected_ova_variable(self) -> OvaVariable | None:
        selection = self.var_tree.selection()
        if not selection:
            return None
        idx = int(selection[0].split("_")[-1])
        return self._ova_variables[idx] if 0 <= idx < len(self._ova_variables) else None

    def _ova_candidate_window(self, variable: OvaVariable) -> tuple[int, int]:
        if variable.value_absolute is not None:
            return max(0, variable.value_absolute - 64), min(len(self._ova_data), variable.value_absolute + 65)
        # Name-table-less / truncated entries still expose the VarInfo record.
        # Use a tight window around the record rather than scanning the whole
        # binary, making every displayed synthetic variable actionable.
        return max(0, variable.offset - 48), min(len(self._ova_data), variable.offset + OVA_INFO_SIZE + 49)

    def find_ova_candidates(self) -> None:
        variable = self._selected_ova_variable()
        if variable is None:
            messagebox.showinfo("OVA", "Select a variable first.")
            return
        lo, hi = self._ova_candidate_window(variable)
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        candidates: list[str] = []
        offsets: list[int] = []
        for absolute in range(lo, hi):
            if self._ova_data[absolute] in (0, 1, 0xFF):
                relative = absolute - anchor
                # The four bytes immediately before a POP initial-value
                # buffer are its serialized size field. It is commonly 00/01
                # and used to appear as a tempting "-4" candidate. It is not
                # a variable value and must never be offered as the selected
                # variable's boolean target.
                if variable.value_absolute is not None and relative == -4:
                    continue
                offsets.append(absolute)
                candidates.append(f"{relative:+d}  @ 0x{absolute:08X}  = {self._ova_data[absolute]:02X}")
        self._ova_candidate_offsets = offsets
        self.ova_candidates["values"] = candidates
        if candidates:
            preferred = 0
            if variable.value_absolute is not None:
                for i, absolute in enumerate(offsets):
                    if absolute == variable.value_absolute:
                        preferred = i
                        break
            self.ova_candidates.current(preferred)
            self.use_ova_candidate()
            self._log(f"OK    Boolean candidates for {variable.name}: {len(candidates)}")
        else:
            self._log(f"INFO  No 00/01 near {variable.name}")
            messagebox.showinfo("OVA candidates", "No 00/01/FF bytes in the variable context. You can enter the relative offset manually and apply the change.")

    def use_ova_candidate(self, _event=None) -> None:
        idx = self.ova_candidates.current()
        if idx < 0 or idx >= len(self._ova_candidate_offsets):
            return
        variable = self._selected_ova_variable()
        if variable is None:
            return
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        self.ova_offset.set(f"{self._ova_candidate_offsets[idx] - anchor:+d}")

    def _parse_ova_offset(self) -> int | None:
        raw = self.ova_offset.get().strip()
        if not raw:
            messagebox.showwarning("Offset", "Enter a relative offset or choose a candidate.")
            return None
        try:
            return int(raw, 0)
        except ValueError:
            messagebox.showerror("Offset", "Use values such as +0, -1, +4 or 0x10.")
            return None

    def set_ova_bool(self, value: int) -> None:
        variable = self._selected_ova_variable()
        if variable is None:
            messagebox.showinfo("OVA", "Select a variable first.")
            return
        rel = self._parse_ova_offset()
        if rel is None:
            return
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        absolute = anchor + rel
        if absolute < 0 or absolute >= len(self._ova_data):
            messagebox.showerror("OVA", "The calculated offset is outside the BIN bounds.")
            return
        old = self._ova_data[absolute]
        if old not in (0, 1):
            if not messagebox.askyesno("Confirm byte", f"0x{absolute:08X} contains {old:02X}, not 00/01.\n\nSet it to {value:02X} anyway?"):
                return
        self._ova_data[absolute] = value
        self._ova_dirty = self._ova_data != bytearray(self._ova_original)
        if self.project.kind == "bin":
            self.project.decoded_bin = bytes(self._ova_data)
        self.refresh_ova_values()
        self.show_ova_context(variable)
        self._log(f"PATCH OVA  {variable.name}: 0x{absolute:08X}  {old:02X} -> {value:02X}")

    def apply_ova_raw_value(self) -> None:
        variable = self._selected_ova_variable()
        if variable is None or variable.value_absolute is None:
            messagebox.showinfo("OVA", "Select a variable with a serialized value.")
            return
        try:
            values = bytes.fromhex(self.ova_value_hex.get().strip())
        except ValueError:
            messagebox.showerror("OVA", "Enter hexadecimal bytes, for example: 00 01 FF 7F")
            return
        if not values:
            messagebox.showwarning("OVA", "Enter at least one byte.")
            return
        max_size = variable.value_size or len(values)
        if len(values) > max_size:
            messagebox.showerror("OVA", f"The variable has {max_size} serialized bytes.")
            return
        start = variable.value_absolute
        end = start + len(values)
        if end > len(self._ova_data):
            messagebox.showerror("OVA", "The value extends beyond the end of the loaded entry.")
            return
        old = bytes(self._ova_data[start:end])
        self._ova_data[start:end] = values
        self._ova_dirty = self._ova_data != bytearray(self._ova_original)
        if self.project.kind == "bin":
            self.project.decoded_bin = bytes(self._ova_data)
        self.refresh_ova_values()
        self.show_ova_context(variable)
        self._log(f"PATCH OVA  {variable.name}: 0x{start:08X}  {old.hex(' ').upper()} -> {values.hex(' ').upper()}")

    def refresh_ova_values(self) -> None:
        for i, variable in enumerate(self._ova_variables):
            iid = f"var_{i}"
            if not self.var_tree.exists(iid):
                continue
            value = "—"
            if variable.value_absolute is not None and variable.value_absolute < len(self._ova_data):
                size = variable.value_size or 1
                value = bytes(self._ova_data[variable.value_absolute:min(len(self._ova_data), variable.value_absolute + size)]).hex(" ").upper()
            current = list(self.var_tree.item(iid, "values"))
            if current:
                current[1] = value
                self.var_tree.item(iid, values=current)

    def show_ova_context(self, variable: OvaVariable) -> None:
        anchor = variable.value_absolute if variable.value_absolute is not None else variable.offset
        lo = max(0, anchor - 64)
        hi = min(len(self._ova_data), anchor + 65)
        block = bytes(self._ova_data[lo:hi])
        lines = []
        for p in range(0, len(block), 16):
            lines.append(f"0x{lo + p:08X}: " + " ".join(f"{b:02X}" for b in block[p:p + 16]))
        self._set_text(self.ova_hex_text, "\n".join(lines))

    def rebuild_edited_bf(self) -> None:
        if self.project.kind != "bf" or self.project.info is None or self._ova_source_asset is None:
            messagebox.showinfo("Rebuild BF", "Open a .BF, analyze an OVA/BIN entry and edit at least one variable.")
            return
        if not self._ova_dirty:
            messagebox.showinfo("Rebuild BF", "No OVA changes to rebuild.")
            return
        target_name = self.project.path.stem + "_edited.bf"
        target = filedialog.asksaveasfilename(title="Rebuild modified .BF", initialfile=target_name,
                                              defaultextension=".bf", filetypes=[("Jade Big Files", "*.bf"), ("All files", "*.*")])
        if not target:
            return
        try:
            entry = next(e for e in self.project.info.entries if e.index == self._ova_source_asset.index)
            rebuilt = Path(target).with_suffix(Path(target).suffix + ".tmp")
            _repack_legacy_bigfile(self.project.path, entry, bytes(self._ova_data), rebuilt)
            os.replace(rebuilt, Path(target))
            self._log(f"OK    .BF rebuilt with OVA: {target}")
            self._log(f"INFO  Modified entry: #{entry.index} {entry.name} • compression={entry.compression}")
            self._ova_dirty = False
            messagebox.showinfo("Rebuild BF", f"Created:\n{target}\n\nThe OVA entry has been reinserted and, if compressed, recompressed using POP-LZO.")
        except Exception as exc:
            self._log(f"ERROR Rebuild BF: {exc}")
            messagebox.showerror("Rebuild BF", str(exc))

    def diagnose_ova(self) -> None:
        if self.project.kind == "bin":
            self.analyze_current_ova()
            return
        asset = self._selected_asset()
        if asset:
            self.analyze_current_ova()
        else:
            messagebox.showinfo("Diagnose OVA", "Open a BIN or select a BF asset.")

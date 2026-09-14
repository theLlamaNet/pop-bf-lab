"""BF Repack controls and project save/export actions."""
from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import os
import struct
import tkinter as tk
from ..archives import (
    _collect_texture_key_replacements,
    _legacy_asset_path,
    _legacy_folder_entries,
    _repack_legacy_bigfile_changes,
    build_legacy_bf_from_folder,
    extract_legacy_bf_as_root,
    read_bigfile,
)
from ..lzo import (
    compress_pop_lzo,
    decompress_pop_lzo,
)


class RepackMixin:
    def _build_bf_repack_tab(self) -> None:
        ttk.Label(self.bf_repack_tab, text="BF Repack", font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        actions = ttk.Frame(self.bf_repack_tab)
        actions.pack(fill="x")
        ttk.Button(actions, text="Export all assets → ROOT", command=self.extract_all_assets).pack(side="left")
        ttk.Button(actions, text="Build .BF from a folder", command=self.build_bf_from_folder).pack(side="left", padx=8)
        ttk.Button(actions, text="Decompress all .BIN", command=self.decompress_all_bins).pack(side="left", padx=8)
        ttk.Button(actions, text="Compress all .DEC", command=self.compress_all_dec).pack(side="left", padx=8)
        box = ttk.LabelFrame(self.bf_repack_tab, text="Extracted folder", padding=10)
        box.pack(fill="x", pady=(18, 0))
        ttk.Label(box, text="Build requires the ROOT folder produced by Export all assets and the original BF open as a template.").pack(anchor="w")
        ttk.Label(box, text="Example: ROOT\Engine Datas\...  /  ROOT\Bin\size.grs").pack(anchor="w", pady=(5, 0))

        info_box = ttk.LabelFrame(self.bf_repack_tab, text="BF Information", padding=8)
        info_box.pack(fill="both", expand=True, pady=(12, 0))
        self.bf_info_text = tk.Text(info_box, height=22, wrap="none", state="disabled", font=("Consolas", 9), relief="flat", borderwidth=0)
        self.bf_info_text.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(info_box, orient="vertical", command=self.bf_info_text.yview).pack(side="right", fill="y")
        self._set_bf_repack_info("No BF open.")

    def _set_bf_repack_info(self, text: str) -> None:
        if hasattr(self, "bf_info_text"):
            self.bf_info_text.configure(state="normal")
            self.bf_info_text.delete("1.0", "end")
            self.bf_info_text.insert("1.0", text)
            self.bf_info_text.configure(state="disabled")

    def _refresh_bf_repack_info(self) -> None:
        path = self.project.path if getattr(self.project, "kind", None) == "bf" else None
        if path is None:
            self._set_bf_repack_info("No BF open.")
            return
        try:
            raw = path.read_bytes()
            if len(raw) < 68:
                raise ValueError("The .bf file is too short for the legacy header.")
            vals = struct.unpack_from("<4sIIIQQIIIIIIiII", raw, 0)
            magic, version, fcount, dcount, unk2, unk3, capacity, unk4, main_id, fcount2, dcount2, file_id_offset, unk5, unk6, last = vals
            if magic != b"BIG\0" or version not in (37, 38):
                raise ValueError("Available for legacy v37/v38 BF archives.")
            fp_entry=68; fp_data=fcount*8; fp_total=capacity*8
            fe_entry=fp_entry+fp_total; fe_data=fcount*84; fe_total=capacity*84
            fo_entry=fe_entry+fe_total; fo_total=capacity*84
            folder_count=0; pos=fo_entry
            while pos+84<=len(raw) and struct.unpack_from("<i",raw,pos+4)[0]!=0:
                folder_count+=1; pos+=84
            fo_data=folder_count*84; unk_off=fo_entry+fo_total-136
            unk=struct.unpack_from("<i",raw,unk_off)[0] if 0<=unk_off<=len(raw)-4 else 0
            hpos=fo_entry+fo_total-4
            while hpos+4<=len(raw) and struct.unpack_from("<i",raw,hpos)[0]==0: hpos+=4
            htotal=struct.unpack_from("<i",raw,hpos)[0] if hpos+4<=len(raw) else 0
            size_i=next((i for i in range(fcount) if raw[fe_entry+i*84+20:fe_entry+i*84+84].split(b"\0",1)[0].decode("ascii",errors="replace").strip()=="size.grs"),None)
            se=sd=st=-1; sc=0
            if size_i is not None:
                se=struct.unpack_from("<I",raw,fp_entry+size_i*8)[0]+4; st=struct.unpack_from("<I",raw,fe_entry+size_i*84)[0]
                q=se
                while q+8<=len(raw) and struct.unpack_from("<I",raw,q)[0]!=0: sc+=1; q+=8
                sd=sc*8
            hx=lambda v:f"0x{v:X}"
            lines=["BF Header Info",f"[00] magic: {magic.decode('ascii',errors='replace').rstrip(chr(0))}",f"[04] unk1: {version}",f"[08] fcount: {fcount}",f"[12] dcount: {dcount}",f"[16] unk2: {unk2}",f"[24] unk3: {unk3}",f"[32] capacity: {capacity}",f"[36] unk4: {unk4}",f"[40] main_id: {hx(main_id)}",f"[44] fcount_2: {fcount2}",f"[48] dcount_2: {dcount2}",f"[52] fileIdTableOffset: {file_id_offset}",f"[56] unk5: {unk5}",f"[60] unk6: {unk6}",f"[64] last: {last}","header length: 68",f"BF header total length: {htotal}","","Field Pos Table Info",f"table entry: {fp_entry}",f"data length: {fp_data}",f"total length: {fp_total}","","File Entry Table Info",f"table entry: {fe_entry}",f"data length: {fe_data}",f"total length: {fe_total}","","Folder Entry Table Info",f"[{unk_off}] unk: {unk}",f"table entry: {fo_entry}",f"data length: {fo_data}",f"total length: {fo_total}","","Size.grs Table Info",f"table entry: {se}",f"data length: {sd}",f"total length: {st}",f"files count: {sc}"]
            self._set_bf_repack_info("\n".join(lines))
        except Exception as exc:
            self._set_bf_repack_info(f"BF information unavailable.\n\n{exc}")

    def extract_all_assets(self) -> None:
        if self.project.kind != "bf" or self.project.path is None:
            messagebox.showinfo("Export all assets", "Open a .BF file first.")
            return
        source = self.project.path
        target_dir = Path(filedialog.askdirectory(title="Select output folder for ROOT", mustexist=True))
        if not target_dir:
            return
        try:
            self._log(f"INFO  Full BF export: {source.name} -> {target_dir}\ROOT")
            extracted = extract_legacy_bf_as_root(source, target_dir)
            root_dir = target_dir / "ROOT"
            self._log(f"OK    Exported {extracted:,} assets to the {root_dir} structure")
            messagebox.showinfo("Export all assets", f"Exported {extracted:,} assets to:\n{root_dir}")
        except Exception as exc:
            self._log(f"ERROR Export all: {exc}")
            messagebox.showerror("Export all assets", str(exc))

    def build_bf_from_folder(self) -> None:
        if self.project.kind != "bf" or self.project.path is None:
            messagebox.showinfo("Build BF", "Open the original BF to use as a template first.")
            return
        root_path = filedialog.askdirectory(title="Select ROOT folder", mustexist=True)
        if not root_path:
            return
        root_dir = Path(root_path)
        if root_dir.name.casefold() != "root":
            messagebox.showerror("Build BF", "Select the ROOT folder, not its parent folder.")
            return
        target = filedialog.asksaveasfilename(title="Save rebuilt BF", initialfile=self.project.path.stem + "_rebuilt.bf", defaultextension=".bf", filetypes=[("Jade Big Files", "*.bf")])
        if not target:
            return
        try:
            self._log(f"INFO  Build BF: template={self.project.path.name}, ROOT={root_dir}")
            count = build_legacy_bf_from_folder(self.project.path, root_dir, Path(target))
            self._log(f"OK    BF rebuilt from ROOT: {target} ({count:,} assets)")
            messagebox.showinfo("Build BF", f"Created:\n{target}\n\nAsset: {count:,}")
        except Exception as exc:
            self._log(f"ERROR Build BF: {exc}")
            messagebox.showerror("Build BF", str(exc))

    def _select_root_for_lzo(self, title: str) -> Path | None:
        root = filedialog.askdirectory(title=title, mustexist=True)
        if not root:
            return None
        path = Path(root)
        if path.name.casefold() != "root":
            messagebox.showerror("BF Repack", "Select the ROOT folder of the extraction.")
            return None
        return path

    def decompress_all_bins(self) -> None:
        root = self._select_root_for_lzo("Select ROOT folder")
        if root is None:
            return
        try:
            folders = _legacy_folder_entries(self.project.path) if self.project.kind == "bf" and self.project.path else None
            if folders is None:
                raise ValueError("Open the original BF first to retain the same folder hierarchy.")
            info = read_bigfile(self.project.path)
            done = 0
            skipped = 0
            for entry in info.entries:
                if entry.parent != 1 or "wolinfo" in entry.name.casefold() or entry.name.casefold() == "size.grs":
                    skipped += 1
                    continue
                enc = _legacy_asset_path(root.parent, folders, entry)
                dec = enc.with_suffix(".dec")
                if not enc.is_file():
                    continue
                raw = enc.read_bytes()
                try:
                    data = decompress_pop_lzo(raw)
                except Exception:
                    continue
                dec.write_bytes(data)
                done += 1
            self._log(f"OK    Decompress all .BIN: {done:,} files decoded")
            messagebox.showinfo("Decompress all .BIN", f"Created {done:,} .DEC files in the ROOT structure.")
        except Exception as exc:
            self._log(f"ERROR Decompress all: {exc}")
            messagebox.showerror("Decompress all .BIN", str(exc))

    def compress_all_dec(self) -> None:
        root = self._select_root_for_lzo("Select ROOT folder")
        if root is None:
            return
        try:
            folders = _legacy_folder_entries(self.project.path) if self.project.kind == "bf" and self.project.path else None
            if folders is None:
                raise ValueError("Open the original BF first to retain the same folder hierarchy.")
            info = read_bigfile(self.project.path)
            done = 0
            failed: list[str] = []
            for entry in info.entries:
                if entry.parent != 1 or "wolinfo" in entry.name.casefold() or entry.name.casefold() == "size.grs":
                    continue
                dec = _legacy_asset_path(root.parent, folders, entry).with_suffix(".dec")
                if not dec.is_file():
                    continue
                bin_path = dec.with_suffix(".bin")
                try:
                    bin_path.write_bytes(compress_pop_lzo(dec.read_bytes()))
                    done += 1
                except Exception as exc:
                    failed.append(f"{dec.name}: {exc}")
                    self._log(f"WARN  Compress all .DEC skipped {dec}: {exc}")
            self._log(f"OK    Compress all .DEC: {done:,} files compressed (.bin)")
            if failed:
                messagebox.showwarning(
                    "Compress all .DEC",
                    f"Created {done:,} .BIN files in the ROOT structure.\n\n"
                    f"{len(failed):,} files were not compressed; see the log for details."
                )
            else:
                messagebox.showinfo("Compress all .DEC", f"Created {done:,} .BIN files in the ROOT structure.")
        except Exception as exc:
            self._log(f"ERROR Compress all: {exc}")
            messagebox.showerror("Compress all .DEC", str(exc))

    def rebuild_bf(self) -> None:
        if self.project.kind != "bf":
            messagebox.showinfo("Rebuild BF", "Open a .bf and select an asset to edit.")
            return
        asset = self._selected_asset()
        if not asset:
            messagebox.showinfo("Rebuild BF", "Select the BF entry to replace.")
            return
        source = filedialog.askopenfilename(title="Replacement payload", initialfile=asset.name)
        if not source:
            return
        target = filedialog.asksaveasfilename(title="Save rebuilt BF", initialfile=self.project.path.stem + "_edited.bf", defaultextension=".bf", filetypes=[("Jade Big Files", "*.bf")])
        if not target:
            return
        try:
            self._log(f"INFO  Rebuilding BF: entry #{asset.index}, payload={source}")
            self.project.replace_bf_entry(asset, Path(source).read_bytes(), Path(target))
            self._log(f"OK    BF rebuilt: {target}")
            messagebox.showinfo("Rebuild BF", f"Created:\n{target}")
        except Exception as exc:
            self._log(f"ERROR Rebuild BF: {exc}")
            messagebox.showerror("Rebuild BF", str(exc))

    def save_bin(self) -> None:
        if self.project.kind != "bin" or self.project.decoded_bin is None:
            messagebox.showinfo("Save BIN", "Open a .bin first.")
            return
        target = filedialog.asksaveasfilename(title="Save BIN", initialfile=self.project.path.stem + "_edited.bin", defaultextension=".bin")
        if not target:
            return
        try:
            data = bytes(self._texture_data) if self._texture_dirty else (bytes(self._ova_data) if self._ova_data else self.project.decoded_bin)
            data = self.project.apply_mesh_patches(0, data)
            self.project.decoded_bin = data
            self._log(f"INFO  Saving BIN: decoded={len(data):,} B -> {target}")
            self.project.save_bin_as(Path(target), data)
            self._log(f"OK    BIN saved: {target}")
            if self._texture_dirty:
                self._texture_original = data
                self._texture_dirty = False
            self._ova_dirty = False
        except Exception as exc:
            self._log(f"ERROR Save BIN: {exc}")
            messagebox.showerror("Save BIN", str(exc))

    def save_dec(self) -> None:
        if self.project.kind != "dec" or self.project.decoded_bin is None:
            messagebox.showinfo("Save DEC", "Open a .dec first.")
            return
        target = filedialog.asksaveasfilename(title="Save DEC", initialfile=self.project.path.stem + "_edited.dec", defaultextension=".dec", filetypes=[("DEC files", "*.dec")])
        if not target:
            return
        try:
            data = bytes(self._texture_data) if self._texture_dirty else (bytes(self._ova_data) if self._ova_data else self.project.decoded_bin)
            data = self.project.apply_mesh_patches(0, data)
            self.project.decoded_bin = data
            Path(target).write_bytes(data)
            self._log(f"OK    DEC saved: {target} ({len(data):,} B)")
            if self._texture_dirty:
                self._texture_original = data
                self._texture_dirty = False
            self._ova_dirty = False
        except Exception as exc:
            self._log(f"ERROR Save DEC: {exc}")
            messagebox.showerror("Save DEC", str(exc))

    def save_edited_bf(self) -> None:
        """Save all currently modified BF-backed assets into one edited .bf."""
        if self.project.kind != "bf" or self.project.info is None or self.project.path is None:
            messagebox.showinfo("Save BF", "Open a .bf first.")
            return

        replacements: dict[int, bytes] = {}
        if self._ova_dirty and self._ova_source_asset is not None:
            replacements[self._ova_source_asset.index] = bytes(self._ova_data)
        if self._texture_dirty and self._texture_source_asset is not None:
            try:
                selected_entry = next((entry for entry in self.project.info.entries
                                       if entry.index == self._texture_source_asset.index), None)
                texture = self._texture_selected()
                if selected_entry is None or texture is None:
                    raise ValueError("Cannot locate the BF entry or the modified texture.")
                texture_replacements, touched = _collect_texture_key_replacements(
                    self.project.path, selected_entry, texture.key,
                    bytes(self._texture_data[texture.data_offset:texture.data_end]),
                    (getattr(self, "_texture_patch_source_type", texture.texture_type)
                     if getattr(self, "_texture_patch_key", texture.key) == texture.key
                     else texture.texture_type),
                    texture.texture_type, texture.width, texture.height, bytes(self._texture_data),
                )
            except Exception as exc:
                self._log(f"ERROR Save BF: {exc}")
                messagebox.showerror("Save BF", str(exc))
                return
            replacements.update(texture_replacements)
            self._log(f"INFO  Texture key 0x{texture.key:08X}: {len(touched)} BF assets to update")
        if self._material_dirty and self._material_source_asset is not None:
            replacements[self._material_source_asset.index] = bytes(self._material_data)

        try:
            for index in self.project.mesh_patches:
                asset = next(a for a in self.project.assets if a.index == index)
                data = replacements.get(index)
                replacements[index] = (self.project.read_asset(asset) if data is None
                                       else self.project.apply_mesh_patches(index, data))
        except Exception as exc:
            messagebox.showerror("Save BF", str(exc))
            return

        if not replacements:
            messagebox.showinfo("Save BF", "No .BF changes to save.")
            return

        target_name = self.project.path.stem + "_edited.bf"
        target = filedialog.asksaveasfilename(
            title="Save modified .BF",
            initialfile=target_name,
            defaultextension=".bf",
            filetypes=[("Jade Big Files", "*.bf"), ("All files", "*.*")],
        )
        if not target:
            return

        rebuilt = Path(target).with_suffix(Path(target).suffix + ".tmp")
        try:
            _repack_legacy_bigfile_changes(self.project.path, replacements, rebuilt)
            os.replace(rebuilt, Path(target))
            if Path(target).resolve() == self.project.path.resolve():
                self.project.info = read_bigfile(self.project.path)
                for patches in self.project.mesh_patches.values():
                    for index, (key, _baseline, payload) in list(patches.items()):
                        patches[index] = (key, payload, payload)
            changed = ", ".join(f"#{index}" for index in sorted(replacements))
            self._ova_dirty = False
            self._texture_dirty = False
            self._material_original = bytes(self._material_data) if self._material_source_asset is not None else getattr(self, "_material_original", b"")
            self._material_dirty = False
            self._log(f"OK    .BF saved: {target} • modified entries: {changed}")
            messagebox.showinfo("Save BF", f"Created:\n{target}\n\nModified entries: {changed}")
        except Exception as exc:
            try:
                rebuilt.unlink(missing_ok=True)
            except Exception:
                pass
            self._log(f"ERROR Save BF: {exc}")
            messagebox.showerror("Save BF", str(exc))

"""Application window, shared widgets, theme and startup."""
from __future__ import annotations

from pathlib import Path
from tkinter import messagebox, ttk
import os
import queue
import tkinter as tk
from ..config import ROOT
from ..models import (
    Asset,
    MaterialInfo,
    MeshInfo,
    OvaVariable,
    PopFileEntry,
    TextureInfo,
)
from ..project import JadeProject
from .assets import AssetBrowserMixin
from .material import MaterialEditorMixin
from .mesh import MeshEditorMixin
from .level import LevelEditorMixin
from .ova import OvaEditorMixin
from .repack import RepackMixin
from .texture import TextureEditorMixin


class JadeToolkit(AssetBrowserMixin, RepackMixin, OvaEditorMixin, LevelEditorMixin, MeshEditorMixin, MaterialEditorMixin, TextureEditorMixin, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PoP BF Lab")
        self.geometry("1380x860")
        self.minsize(1120, 720)
        self._dark_mode = True
        self._setup_theme()
        self.project = JadeProject()
        self._asset_map: dict[str, Asset] = {}
        self._ova_data = bytearray()
        self._ova_original = b""
        self._ova_variables: list[OvaVariable] = []
        self._ova_source_asset: Asset | None = None
        self._ova_dirty = False
        self._ova_candidate_offsets: list[int] = []
        self.ova_variable_mode = "jade"
        self._ova_jade_variables: list[OvaVariable] = []
        self._ova_ascii_variables: list[OvaVariable] = []
        self._texture_data = bytearray()
        self._texture_infos: list[TextureInfo] = []
        self._texture_file_entries: list[PopFileEntry] = []
        self._texture_source_asset: Asset | None = None
        self._texture_original = b""
        self._texture_dirty = False
        self._texture_image = None
        self._texture_photo = None
        self._texture_rotation = 0
        self._texture_flip_x = False
        self._texture_flip_y = False
        self._mesh_data = bytearray()
        self._mesh_infos: list[MeshInfo] = []
        self._mesh_source_asset: Asset | None = None
        self._mesh_textures: dict[int, object] = {}
        self._mesh_material_textures: dict[int, int] = {}
        self._mesh_material_textures_by_mesh: dict[int, dict[int, int]] = {}
        self._mesh_material_colors_by_mesh: dict[int, dict[int, tuple[float, float, float, float]]] = {}
        self._mesh_material_keys_by_mesh: dict[int, list[tuple[int, int | None]]] = {}
        self._swap_mesh: MeshInfo | None = None
        self._swap_mesh_textures: dict[int, object] = {}
        self._swap_mesh_material_textures: dict[int, int] = {}
        self._swap_mesh_material_colors: dict[int, tuple[float, float, float, float]] = {}
        self._swap_mesh_path: Path | None = None
        self._mesh_texture_photos: list[object] = []
        self._mesh_external_texture_cache: dict[int, object] = {}
        self._mesh_external_texture_misses: set[int] = set()
        self._mesh_cache_project: Path | None = None
        self._mesh_pending_texture_keys: set[int] = set()
        self._mesh_texture_search_queue: queue.Queue = queue.Queue()
        self._mesh_texture_search_generation = 0

        self._material_data = bytearray()
        self._material_infos: list[MaterialInfo] = []
        self._material_source_asset: Asset | None = None
        self._material_textures: dict[int, object] = {}
        self._material_texture_sources: dict[int, Asset] = {}
        self._material_inventory_sources: dict[int, Asset] = {}
        self._material_external_texture_cache: dict[int, object] = {}
        self._material_texture_decode_misses: set[int] = set()
        self._material_inventory_complete = False
        self._material_cache_project: Path | None = None
        self._material_index_queue: queue.Queue = queue.Queue()
        self._material_index_generation = 0

        self._material_texture_photos: list[object] = []
        self._material_diffuse_path = tk.StringVar()
        self._material_secondary_path = tk.StringVar()
        self._material_normal_path = tk.StringVar()
        self._material_metallic = tk.DoubleVar(value=0.0)
        self._material_alpha = tk.DoubleVar(value=1.0)
        self._material_projection = tk.DoubleVar(value=1.0)
        self._material_shape = tk.StringVar(value="sphere")
        self._material_dirty = False
        self._material_preview_photo = None
        self._bf_repack_info = None
        self._mesh_drag = None
        self._mesh_yaw = -0.45
        self._mesh_pitch = 0.18
        self._mesh_zoom = 1.0
        self._mesh_render_after = None
        self._build_menu()
        self._build_ui()
        self._log("INFO  PoP BF Lab ready — integrated BF + POP-LZO + OVA core.")
        self._refresh_title()

    def _setup_theme(self) -> None:
        """Configure the permanent dark palette."""
        self._dark = {
            "bg": "#1e1f22", "surface": "#2b2d31", "surface_alt": "#2b2d31",
            "surface_hover": "#36383d", "field": "#17181a", "fg": "#f2f3f5", "muted": "#b5bac1",
            "accent": "#4752c4", "select": "#4752c4", "border": "#3f4147",
            "border_soft": "#3f4147", "accent_border": "#4752c4",
        }
        self._apply_theme()

    def _apply_theme(self) -> None:
        palette = self._dark
        self.configure(bg=palette["bg"])
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=palette["bg"], foreground=palette["fg"], font=("Segoe UI", 10))
        style.configure("TFrame", background=palette["bg"])
        style.configure("TLabel", background=palette["bg"], foreground=palette["fg"], padding=1)
        style.configure("TLabelframe", background=palette["bg"], foreground=palette["fg"])
        style.configure("TLabelframe.Label", background=palette["bg"], foreground=palette["fg"])
        style.configure("TEntry", fieldbackground=palette["field"], foreground=palette["fg"],
                        insertcolor=palette["fg"], bordercolor=palette["border"], padding=(9, 6))
        style.configure("TCombobox", fieldbackground=palette["field"], background=palette["surface"],
                        foreground=palette["fg"], arrowcolor=palette["fg"], bordercolor=palette["border"])
        style.map("TCombobox", fieldbackground=[("readonly", palette["field"])],
                  foreground=[("readonly", palette["fg"])])
        style.configure("TButton", background=palette["surface"], foreground=palette["fg"],
                        bordercolor=palette["border"], padding=(10, 7))
        style.map("TButton", background=[("active", palette["select"]), ("disabled", palette["field"])],
                  foreground=[("disabled", palette["muted"])])
        style.configure("Apply.TButton", background="#7a3a3a", foreground=palette["fg"],
                        bordercolor="#a05a5a", padding=(10, 7))
        style.map("Apply.TButton", background=[("active", "#994848"), ("disabled", "#7a3a3a")],
                  foreground=[("disabled", palette["muted"])])
        style.configure("TNotebook", background=palette["bg"], bordercolor=palette["border"], tabmargins=(2, 2, 2, 0))
        style.configure("TNotebook.Tab", background=palette["surface"], foreground=palette["fg"], padding=(15, 9), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", palette["select"])], foreground=[("selected", palette["fg"])])
        style.configure("Treeview", background=palette["field"], fieldbackground=palette["field"],
                        foreground=palette["fg"], bordercolor=palette["border_soft"], rowheight=30, relief="flat")
        style.map("Treeview", background=[("selected", palette["select"])], foreground=[("selected", palette["fg"])])
        style.configure("Treeview.Heading", background=palette["surface_alt"], foreground=palette["muted"],
                        bordercolor=palette["border_soft"], relief="flat", padding=(9, 8))
        style.configure("TPanedwindow", background=palette["bg"])
        style.configure("TScrollbar", background=palette["surface_alt"], troughcolor=palette["field"],
                        bordercolor=palette["border_soft"], arrowcolor=palette["muted"], relief="flat", width=12)
        style.configure("TCheckbutton", background=palette["bg"], foreground=palette["fg"])
        style.map("TCheckbutton", foreground=[("disabled", palette["muted"])])
        style.configure("TSeparator", background=palette["border"])
        self.option_add("*TCombobox*Listbox.background", palette["field"])
        self.option_add("*TCombobox*Listbox.foreground", palette["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", palette["select"])
        self.option_add("*TCombobox*Listbox.selectForeground", palette["fg"])
        self.option_add("*Listbox.background", palette["field"])
        self.option_add("*Listbox.foreground", palette["fg"])
        self.option_add("*Listbox.selectBackground", palette["select"])
        self.option_add("*Listbox.selectForeground", palette["fg"])
        for name in ("asset_details", "ova_text", "log_text", "ova_hex_text"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(bg=palette["field"], fg=palette["fg"], insertbackground=palette["fg"],
                                 selectbackground=palette["select"], selectforeground=palette["fg"],
                                 highlightbackground=palette["border_soft"], highlightcolor=palette["border"])
        if hasattr(self, "status"):
            self.status.configure(background=palette["surface"], foreground=palette["fg"])

    def _build_menu(self) -> None:
        palette = self._dark
        menu = tk.Menu(self, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                       activebackground=palette["select"], activeforeground=palette["fg"],
                       borderwidth=0)
        file_menu = tk.Menu(menu, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                            activebackground=palette["select"], activeforeground=palette["fg"], borderwidth=0)
        file_menu.add_command(label="Import .BF...", command=self.import_bf)
        file_menu.add_command(label="Save modified .BF as...", command=self.save_edited_bf)
        file_menu.add_command(label="Import .BIN...", command=self.import_bin)
        file_menu.add_command(label="Import .DEC...", command=self.import_dec)
        file_menu.add_separator()
        file_menu.add_command(label="Extract selected asset...", command=self.extract_selected)
        file_menu.add_command(label="Export all assets", command=self.extract_all_assets)
        file_menu.add_command(label="Save edited .BIN as...", command=self.save_bin)
        file_menu.add_command(label="Save edited .DEC as...", command=self.save_dec)
        file_menu.add_command(label="Rebuild .BF as...", command=self.rebuild_bf)
        file_menu.add_separator()
        file_menu.add_command(label="Close", command=self.close_project)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        menu.add_cascade(label="File", menu=file_menu)

        tools = tk.Menu(menu, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                        activebackground=palette["select"], activeforeground=palette["fg"], borderwidth=0)
        tools.add_command(label="Refresh Asset Browser", command=self.refresh_assets)
        tools.add_command(label="Diagnose OVA", command=self.diagnose_ova)
        tools.add_command(label="Open program folder", command=self.open_tools_folder)
        menu.add_cascade(label="Tools", menu=tools)

        help_menu = tk.Menu(menu, tearoff=False, bg=palette["surface"], fg=palette["fg"],
                            activebackground=palette["select"], activeforeground=palette["fg"], borderwidth=0)
        help_menu.add_command(label="About PoP BF Lab", command=self.about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menu)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root, padding=(2, 0, 2, 12))
        header.pack(fill="x")
        title_group = ttk.Frame(header)
        title_group.pack(side="left")
        ttk.Label(title_group, text="PoP BF Lab", font=("Segoe UI Semibold", 20)).pack(anchor="w")
        ttk.Label(title_group, text="Jade assets  •  OVA variables  •  Big File editing").pack(anchor="w", pady=(3, 0))

        toolbar = ttk.Frame(root)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="Import .BF", command=self.import_bf).pack(side="left")
        ttk.Button(toolbar, text="Save modified .BF", command=self.save_edited_bf).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Import .BIN", command=self.import_bin).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Save modified .BIN", command=self.save_bin).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Import .DEC", command=self.import_dec).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Save modified .DEC", command=self.save_dec).pack(side="left", padx=6)
        self.file_label = ttk.Label(toolbar, text="No file open")
        self.file_label.pack(side="right")

        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True)
        self.asset_tab = ttk.Frame(self.tabs, padding=8)
        self.ova_tab = ttk.Frame(self.tabs, padding=8)
        self.level_tab = ttk.Frame(self.tabs, padding=8)
        self.mesh_tab = ttk.Frame(self.tabs, padding=8)
        self.texture_tab = ttk.Frame(self.tabs, padding=8)
        self.material_tab = ttk.Frame(self.tabs, padding=8)
        self.bf_repack_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.asset_tab, text="Asset Browser")
        self.tabs.add(self.bf_repack_tab, text="BF Repack")
        self.tabs.add(self.ova_tab, text="OVA Variables")
        self.tabs.add(self.level_tab, text="Level Editor")
        self.tabs.add(self.mesh_tab, text="Mesh Editor")
        self.tabs.add(self.material_tab, text="Material Editor")
        self.tabs.add(self.texture_tab, text="Texture Editor")
        self._build_asset_tab()
        self._build_bf_repack_tab()
        self._build_ova_tab()
        self._build_level_tab()
        self._build_mesh_tab()
        self._build_material_tab()
        self._build_texture_tab()

        self.status = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", pady=(8, 0))

    def _placeholder(self, parent, title: str, subtitle: str, lines: list[str]) -> None:
        ttk.Label(parent, text=title, font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(parent, text=subtitle).pack(anchor="w", pady=(4, 14))
        for line in lines:
            ttk.Label(parent, text=line).pack(anchor="w", pady=2)

    def _log(self, text: str) -> None:
        print(text, flush=True)
        if hasattr(self, "log_text"):
            self.log_text.config(state="normal")
            self.log_text.insert("end", text.rstrip() + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.status.set(text)

    def clear_log(self) -> None:
        if hasattr(self, "log_text"):
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.config(state="disabled")
        self._log("INFO  Log cleared.")

    def _refresh_title(self) -> None:
        self.title(f"PoP BF Lab — {self.project.title}")
        self.file_label.config(text=self.project.title)

    def _clear_tree(self, tree) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _set_text(self, widget, text: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def open_tools_folder(self) -> None:
        try:
            os.startfile(ROOT)
        except Exception as exc:
            self._log(f"ERROR Opening program folder: {exc}")
            messagebox.showerror("Tools", str(exc))

    def about(self) -> None:
        messagebox.showinfo(
            "About PoP BF Lab",
            "PoP BF Lab\n\n"
            "Made by FulGer\n\n"
            "Prince of Persia Trilogy / Jade Engine v37-v38 toolkit.\n"
            "BF indexing, POP-LZO, OVA Variables and asset browser.\n\n"
            "Thanks & credits\n"
            "• bf_repacker_2018_05_23_1419 — by BlackDaemon\n"
            "• bin_repacker_2018_05_29_0806 — by BlackDaemon\n"
            "• io_scene_pop (Blender addon) — by kugelrund\n"
            "• Jade Toolkit — by kaminoer\n"
            "• popww_world_editor — by Khrysos\n\n"
            "Jade Engine and Prince of Persia are properties of Ubisoft and their respective owners.\n"
            "This project is an independent community tool and is not affiliated with or endorsed by Ubisoft."
        )

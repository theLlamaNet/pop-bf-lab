# PoP BF Lab

A lightweight Python and Tkinter suite designed to parse, inspect, and modify Jade Engine assets, structures, and Big Files (`.bf` / `.bin`). Developed specifically for game research, reverse engineering, and modding titles built on the Jade Engine, such as *Prince of Persia: The Sands of Time*, *Warrior Within*, and *The Two Thrones*.

Unlike generic hex editors or simple string parsers, **PoP BF Lab** natively decodes Jade Engine records and container layouts, offering a comprehensive set of visual editors and tools to safely manipulate game data without corrupting name tables, file alignment, or archive integrity.

---

## Core Features

* **OVA & Variable Editor:** Structurally decodes 12-byte `AI_tdst_VarInfo` engine records and 30-byte editor name table entries for safe boolean, integer, and float editing.
* **Asset Browser:** Graphical interface to index, search, preview, and extract resources embedded within Jade Big Files (`.bf`).
* **Mesh & Texture Swapping:** Automated tools to replace 3D geometry and texture buffers directly within engine archives or loose binary files.
* **Level & Scene Inspection:** Tools for inspecting visual structures, object placement, and scene hierarchy files (`.gao`, `.bin`).
* **Big File (.bf) Integration:** Direct FAT indexing and non-destructive payload substitution for archives containing over 100,000 file entries.
* **In-Memory Decompression:** Automatic detection and decompression of LZO-wrapped data blocks (e.g., `Univers_oin_*.bin`).
* **Binary Diff Utility:** Built-in tool to compare two binary dumps and map structural byte variations instantly.

### Texture Swap: automatic conversion and dump

The Texture Swap tab includes **Dump texture**, which writes the selected texture beside the source `.bf` without changing its embedded compressed payload. DDS/DXT5 textures keep their original mipmap count and payload size.

Replacement images can be PNG, JPG/JPEG, TGA, BMP, WebP, or DDS. The toolkit automatically resizes them to the target texture dimensions and converts them to the original POP texture representation. For DDS/DXT5, the encoder is implemented in Python and reproduces the exact original block/mipmap byte count, so a source such as `349,504 B` remains `349,504 B` after conversion even when a normal DDS exporter would emit a different mipmap chain.

The Pillow runtime used for image decoding is vendored in `vendor/PIL`, so this feature does not require ImageMagick or a separate Pillow installation on the target machine.

---

## Technical Overview

### 1. Structural OVA & Variable Parsing

The variable editor parses engine structures based on native Jade C-definitions:

* **Engine Records:** Decodes 12-byte serialized data blocks containing offsets, element counts, type identifiers, and execution flags (`i_Offset`, `i_NumElem`, `w_Type`, `w_Flags`).
* **Name Tables:** Maps 30-byte fixed string slots to individual variables.
* **Safe Buffer Editing:** Edits are strictly restricted to verified value buffers (`pc_BufferInit`), ensuring boolean toggles (`00` / `01`) do not corrupt adjacent headers or layout blocks.
* **Retail Fallback:** When variable name tables are stripped in retail builds, records are isolated and assigned systematic synthetic labels (`OVA_###`).

### 2. Asset & Big File (.bf) Management

* **Direct FAT Indexing:** Maps linked FAT chains via `ul_NextPosFat` without requiring temporary disk extraction.
* **Asset Swapping:** Facilitates mesh and texture swapping by replacing target payloads while preserving container alignment and uncompressed/compressed boundaries.
* **Filtering & Search:** Real-time filtering by extension (`.ova`, `.bin`, `.gao`, etc.) or string match.

### 3. Differential Analysis (Diff Tool)

Compares two binary dumps from the same file version (such as baseline vs. modified state) to locate changed bytes and correlate them with candidate variable records.

---

## Requirements & Installation

### Requirements

The distributable Windows package includes its own **Python 3.11 runtime** under `runtime/python311/`.
End users do not need Python installed, and their system Python version is not used by the launcher.

The bundled runtime contains the standard library and Tkinter required by the application, while Pillow
and the other application-specific Python modules remain inside this repository/package.

### Installation & Execution

For a shared Windows copy, keep the repository/package layout intact and launch:

```text
run_pop_bf_lab.bat

```

The launcher resolves Python relative to itself, so it works even when the recipient has no Python
installation or has a different Python version installed. Do not remove `runtime/python311/` from the
shared package.

For development, `pop_bf_lab.py` can still be run with a compatible local Python 3.11 installation.

---

## Research & Use Cases

### Prince of Persia: The Sands of Time Trilogy

* **Variable Mapping:** Inspects runtime flags and metadata (e.g., `mk_CheckPoint_CurKey`, `mk_Restart`, `mb_Cam_Invert_Rotation`, `mi_Tutorials`, `mb_DisplayUbiLogo`).
* **Asset Modding:** Enables texture replacement, geometry swapping, and level-specific parameter adjustments across all three titles in the trilogy.

---

## Credits & Acknowledgments

This toolkit was developed using architectural reverse engineering research based on:

* Jade Engine source layout and binary definitions.
* Original `.bf` and `.bin` repacking research by BlackDaemon.

---

## License & Legal Disclaimer

This project is released strictly for research, reverse engineering, and educational purposes. All product names, trademarks, and registered trademarks belong to their respective owners. *Prince of Persia* and the Jade Engine are trademarks or registered trademarks of Ubisoft.
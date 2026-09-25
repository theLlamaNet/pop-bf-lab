!IMPORTANT!
This isn't a plea for personal money—I'm looking for a small contribution to cover a one-month ChatGPT Plus subscription so I can keep developing a solid, functional tool. Any amount helps, and I’d be truly grateful for your support!

<a href="https://www.buymeacoffee.com/fulger" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 37px !important;width: 170px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>



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

### Advanced mesh import (GLB / OBJ)

Mesh Editor distinguishes static geometry from characters and supports GLB and OBJ replacements. `Mantain original skeleton and weights` is enabled by default and keeps the selected BF character's bind matrices while transferring weights to the new topology. With that option disabled, a skinned GLB supplies its own bind matrices, flags and vertex weights. `Adapt bones and weights` maps GLB joints to the BF character by GAO key or unique bone name; native slot IDs are accepted only when their names agree. Unmatched imported bones are ignored, and vertices left unweighted receive the target's paint by proximity. Ambiguous or entirely unmatched joints are rejected before changing the asset. The target bind matrices, skin flags, GAO gizmo links and animation hierarchy are retained. Retail GEO and cooked rendering buffers are rebuilt together. Without adaptation, different bone slots are rejected because this workflow does not create new GAO bone objects or animation resources.

Export mesh writes skinned characters as GLB, including their original bind matrices, joint weights, and the bone names and hierarchy when the owning GAO resolves them. GLB UVs keep Jade's top-left V orientation; static meshes continue to export as OBJ/MTL with the OBJ V conversion.

See [mesh import workflow, validation and limitations](docs/MESH_IMPORT.md). Structural tests passed with a Blender cube on 264 meshes across SOT, WW and T2T; in-game rendering and animation still require validation.

### OBJ shading and texture orientation

OBJ exports include native vertex normals and smoothing, preserving smooth shading through export, reimport, preview and native mesh replacement. Existing Lab OBJ exports without normals recover smoothing from shared vertex indices; export again from the original BF to preserve the exact original normals. Explicit hard edges in imported OBJ files remain supported.

Raw, PAL8 and 4-bit texture previews/exports now use the same top-left row order as DDS. This fixes vertically inverted textures on affected SOT meshes without changing native UV coordinates. Restart the application and export fresh OBJ/PNG files to use both fixes.

Validation: seven regression tests (`runtime/python311/python.exe -B -m unittest discover -s tests -v`); Mahasti head roundtrip retains 351 vertices, 622 faces and 18 bones; five SOT costume meshes retain normals in rebuilt rendering buffers, and the SOT costume PAL8 image matches the native row order. In-game visual validation remains pending.

### Texture Swap: automatic conversion and dump

The Texture Swap tab includes **Dump texture**, which writes the selected texture beside the source `.bf` without changing its embedded compressed payload. DDS/DXT5 textures keep their original mipmap count and payload size.

Replacement images can be PNG, JPG/JPEG, TGA, BMP, WebP, or DDS. The toolkit automatically resizes them to the target texture dimensions and converts them to the original POP texture representation. For DDS/DXT5, the encoder is implemented in Python and reproduces the exact original block/mipmap byte count, so a source such as `349,504 B` remains `349,504 B` after conversion even when a normal DDS exporter would emit a different mipmap chain.

Enable **Keep imported texture dimensions** below the Texture Editor scan count to retain the replacement image's width and height, including larger textures. With the option off (the default), imports continue to use the selected texture's dimensions. Toggling the option also updates an image already imported but not yet applied.

Resized replacements update the cooked surface dimensions and resource size while preserving the original TEX file parameters, following Jade Toolkit. They use a fresh base image level instead of retaining mipmaps or padding from the old dimensions. Saving the BF also synchronizes header-only references and duplicate copies of the same texture key. DXT dimensions must be divisible by four. The existing texture reader supports dimensions from 1 to 8192 pixels per axis. In-game compatibility of custom resolutions still depends on the game.

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
shared package. Startup errors are shown by the launcher and saved to `pop_bf_lab.log` in the program folder.

For development, `pop_bf_lab.py` can still be run with a compatible local Python 3.11 installation.

### Source layout

The interface, diagnostics, logs and launcher messages are in English. The application is split by
responsibility; `pop_bf_lab.py` remains the launcher and re-exports the existing Python API for
scripts that already import it.

| File / directory | Responsibility |
| --- | --- |
| `bf_lab/config.py`, `bf_lab/models.py` | Installation paths, binary constants and shared data models |
| `bf_lab/resources.py`, `bf_lab/archives.py`, `bf_lab/lzo.py` | POP resource streams, BF extraction/rebuilding and compression |
| `bf_lab/ova.py` | OVA descriptors, variables and diagnostics |
| `bf_lab/mesh_parser.py`, `bf_lab/mesh_import.py`, `jade_mesh.py` | Native meshes, GLB/OBJ import and GEO/skin serialization |
| `bf_lab/textures.py`, `bf_lab/materials.py` | Texture codecs and material records |
| `bf_lab/project.py` | Opened project and staged changes |
| `bf_lab/viewports.py` | Optional OpenGL viewports |
| `bf_lab/ui/app.py` | Main window, theme, menus and shared controls |
| `bf_lab/ui/assets.py`, `repack.py`, `ova.py`, `mesh.py`, `material.py`, `texture.py` | Feature panels and their actions |

The UI panels are mixins composed into `JadeToolkit`, preserving the shared project state and
existing callbacks. Core modules can also be imported directly, for example
`from bf_lab.archives import read_bigfile`. Dependencies in `vendor/`, the Python runtime and LZO
helpers are resolved relative to the installation directory. Include the entire `bf_lab/` directory
when sharing the application.

Run the automated regression tests with the bundled interpreter:

```text
runtime\python311\python.exe -B -m unittest discover -s tests -v
```

Run the hidden Tkinter integration smoke test on a Windows desktop:

```text
runtime\python311\python.exe -B tests\ui_smoke.py
```

These checks use synthetic fixtures and temporary output files; they do not modify game archives.

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

### Mesh lighting and imported materials

Static mesh replacement converts original baked vertex lighting to achromatic luminance before transferring it to the imported topology, including per-instance GAO RLI tables and rebuilt rendering buffers. Exact OBJ-round-tripped points are matched at three-decimal precision; moved or new points spatially interpolate only the scalar light level. This deliberately removes blue/orange room-light chrominance while retaining local brightness and shadow, avoiding both coloured textures and the overexposure caused by a white fallback. The serializer respects Jade's two colour representations: primary RLI/GEO uses internal `RGBA` (`0xAABBGGRR`), while the GPU-friendly expanded RLI uses Direct3D `BGRA`; rebuilt RLI validity bytes are normalized to `0xFE`. Lighting is transferred, not recomputed for a radically different shape.

The imported-mesh row includes a `Vertex color` HSV wheel between the OpenGL preview and its material list, an editable `#RRGGBB` field, and a `Color intensity` slider from 0% to 200%. The chosen colour tints the transferred scalar lighting rather than replacing its spatial variation, so shadows and local brightness remain intact. White at 100% is neutral. The OpenGL replacement preview updates live as an approximation, while Apply writes the chosen tint into both GEO point colours and GAO RLI primary/cooked buffers using their correct Jade/Direct3D channel orders. The selected-mesh preview reads and renders GEO colours or the primary instance RLI, including after a saved BF/BIN is closed and reopened.

When `Import and replace materials` is enabled, GLB base-colour textures and OBJ `map_Kd` images replace native material textures. If the imported mesh has more materials than the BF pack, a Yes/No dialog offers to add the excess. Yes extends the native multi-material pack with distinct leaf records and GEO elements. No maps excess faces into the available slots without replacing their textures. OBJ names exported as `mat_N` retain native BF slot `N`, including Blender suffixes such as `mat_N.001`; assignment does not depend on object or `usemtl` encounter order. Non-contiguous runs are regrouped by native slot with face and UV-face indices reordered together. Imported colour factors multiply the texture channels. PNG alpha is preserved; cutouts use alpha testing, smooth transparency uses alpha blending, and opaque materials clear inherited blending flags. When material replacement is disabled, the existing BF materials are retained.

New texture records carry their own resource key internally and are inserted by key into the native descriptor and pixel loading waves, before the stream footer. Imported images use Jade Toolkit's default DXT5 contract, including opaque images, and non-power-of-two images are resized to the nearest power of two. The largest native full texture supplies the inherited logical TEX header; the cooked dimensions and payload are validated before staging. Materials precede their new texture dependencies. This corrects malformed references, load order, pixel offsets and surface contracts produced by the earlier importer. Already exported archives must be rebuilt from a clean source using the corrected importer.

The Mesh Editor's compact, scrollable Import options tree contains `Import and replace materials`, `Mantain original skeleton and weights`, `Adapt bones and weights`, `Fit size and center to the original mesh`, and `Recalculate imported mesh collision`. Collision recalculation builds a triangle collider from the final imported geometry, welds seam vertices, rebuilds normals and adjacency, and attaches a private ColMap to each direct static GAO instance. Its bounds include the instance transform. Existing per-object ColMaps are replaced; separate room collision is not removed. Skinned character collision and indirect LOD instances are unsupported and produce an explicit error instead of saving a partial replacement. The original skeleton option is on by default; the other options are off. Enabling either skeleton checkbox disables the other.

Save BF/BIN/DEC persists the added resources. Native material and texture templates must be present in the selected asset. Shared multi-LOD material replacement is rejected because changing one LOD's slot layout could break the others. Full PBR shaders and normal/metallic/roughness maps are not converted.

Validation: `runtime/python311/python.exe -B -m unittest discover -s tests -v`. Structural checks also covered nine static meshes and three skinned meshes from SOT, WW and T2T, including added slots and retained bones. Visual validation in the games remains pending.

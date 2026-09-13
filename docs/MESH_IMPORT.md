# Importazione e sostituzione mesh 3D

Il Mesh Editor importa GLB e OBJ e distingue **Mesh statica** da **Character / skin** nella colonna Tipo. Il codec retail v7/v8 e in `jade_mesh.py`, distribuito insieme a `pop_bf_lab.py`; non richiede Blender installato o i progetti di riferimento a runtime.

## Procedura

1. Aprire il BF/BIN/DEC, selezionare un asset e premere **Scan selected .wow / asset**.
2. Selezionare la mesh da sostituire; le informazioni mostrano il numero di ossa realmente presenti.
3. Premere **Import GLB / OBJ...**. Per OBJ scegliere gli assi prima dell'importazione: **Auto** riconosce gli export di PoP BF Lab come Jade Z-up; per gli altri OBJ usa Y-up. Scegliere esplicitamente **Jade Z-up** se il file e gia nelle coordinate del gioco.
4. Facoltativamente attivare **Adatta dimensione e centro alla mesh originale**: scala uniforme sulla dimensione maggiore e traslazione del centro, senza deformare le proporzioni. La modifica avviene all'applicazione; l'anteprima della candidata mostra l'import originale.
5. Premere **Apply mesh changes**. La geometria viene ricostruita e verificata prima di registrare le patch in memoria. Il BF/BIN/DEC viene scritto solo tramite il normale comando di salvataggio.

## Character: rig originale e pesi nuovi

La sostituzione conserva gli ID delle matrici, le matrici di bind e i riferimenti GAO/scheletro/animazioni del personaggio BF. Lo scheletro esistente viene quindi associato automaticamente ai nuovi vertici, anche quando il file importato e un semplice cubo senza armatura.

I pesi vengono trasferiti nello spazio locale dalla mesh originale tramite i tre vertici pesati piu vicini, con interpolazione inversamente proporzionale alla distanza al quadrato. Una coincidenza esatta conserva le influenze del punto originale. Il formato GPU magic-3 usa al massimo tre influenze, rinormalizzate. Gli altri percorsi mantengono le influenze interpolate. Il codec CPU rispetta la codifica Jade: la ponderazione e la parte alta di un float32, mentre i 16 bit bassi contengono l'indice del vertice; non e UNORM16.

Il rig presente in un GLB viene riconosciuto e gli attributi JOINTS/WEIGHTS vengono validati. Lo swap usa comunque il rig del **destinatario BF** e ricalcola i pesi: non importa animazioni o la gerarchia di uno scheletro estraneo. Una destinazione statica rimane statica anche se il GLB ha un rig.

Per un character completo, esportare la mesh in posa di riposo e allinearla al personaggio originale. Il trasferimento per prossimita non e un autorig anatomico: parti del corpo molto vicine, proporzioni diverse o pose differenti possono richiedere ulteriore lavoro artistico. L'adattamento automatico non corregge una posa differente.

## Geometria e materiali

- GLB: trasformazioni dei nodi e gerarchie, scene, primitive triangolari/strip/fan, normali, UV, accessor normalizzati e sparse; conversione Y-up verso Jade.
- OBJ: indici positivi e negativi, UV e normali indipendenti, gruppi di smoothing, facce concave semplici triangolate, lettura di colori e texture MTL di base per l'anteprima.
- Gli slot materiali BF vengono conservati. Le primitive importate vengono assegnate agli slot in ordine; quelle eccedenti sono riunite sull'ultimo slot, gli slot inutilizzati restano vuoti. Le texture GLB/MTL sono di anteprima, non vengono inserite automaticamente nel BF.
- I buffer primari e quelli di rendering vengono ricostruiti insieme. Sono supportati i layout verificati da 20, 32, 44, 52 e 64 byte; le tangenti vengono ricalcolate dove richieste.
- Le RLI delle istanze dirette vengono ridimensionate insieme ai vertici. I gruppi StaticLOD con RLI vuota mantengono i riferimenti originali. Si sostituisce il LOD selezionato: gli altri livelli di dettaglio non vengono generati automaticamente.

## Limiti espliciti

Geometria primaria: massimo 32768 vertici e 32768 UV; buffer espanso per le cuciture UV: massimo 65536 vertici. Morph target, layout interleaved del prototipo, metadati MRM/LOD non riconosciuti e tabelle RLI condivise non vuote nei gruppi sono rifiutati con una spiegazione, prima di modificare il progetto. I formati console non equivalenti al layout retail verificato non sono coperti da questi test. I volumi di culling e le collisioni delle istanze non vengono ricalcolati: per modifiche che ampliano molto la sagoma serve anche una verifica nel gioco.

## Verifiche del 13 settembre 2026

`tests/test_mesh_import.py`: 12 test di regressione su pesi nativi, skin 52/64, indici, code non riconosciute, OBJ concavo, GLB con rig e trasformazioni, accessor sparse, RLI e gruppi LOD.

Con il cubo Blender locale `Untitled.glb` (24 vertici / 12 facce):

| Asset | Mesh statiche | Character | Errori |
| --- | ---: | ---: | ---: |
| SOT `0101_Entree_wow_ff02b4f9.bin` | 203 | 11 | 0 |
| WW `Prince_wow_ff0c0226.bin` | 37 | 4 | 0 |
| T2T `PrinceFinal_Shape_wow_ff051dec.bin` | 7 | 2 | 0 |

I report JSON in `tests/` includono rilettura della geometria, preservazione degli ID/matrici, verifica dei pesi, risorse estranee identiche e una seconda sostituzione sulla mesh gia modificata. Su SOT e stato verificato anche il round trip POP-LZO. Nessun BF originale e stato scritto. Questi sono test strutturali e di integrazione; **la resa e le animazioni nel gioco non sono state testate**. Il percorso a 64 byte e verificato con fixture dedicate, non nei tre asset reali sopra.

Esecuzione con il runtime incluso:

```powershell
.\runtime\python311\python.exe -B tests/test_mesh_import.py
.\runtime\python311\python.exe -B tests/mesh_archive_smoke.py --bf "percorso\gioco.bf" --asset "nome_asset_univoco" --glb "percorso\cubo.glb" --compression
```

## Riferimenti locali studiati

- `Jade-Toolkit-main/src/Geometry.cpp`, `Gltf.cpp`, `MeshSwap.cpp`, `Gao.cpp`, `Rli.cpp`: header GEO, skin, buffer espansi, slot materiali e riferimenti delle istanze.
- `Jade Source Code/Libraries/GraphicDK/Sources/GEOmetric/GEO_SKIN.c`, `GEOobject.c`, `GEOstaticLOD.c`: codifica float delle ponderazioni e layout dei riferimenti LOD. La codifica dei pesi e stata verificata contro i valori effettivi del BF, correggendo l'assunzione UNORM16 del Toolkit.
- `popww_world_editor/bin_resources.py`, `writeback_validation.py`: struttura delle risorse e principio di verifica delle aree esterne alla modifica. PoP BF Lab ricostruisce gli header delle risorse quando cambia la dimensione; non adotta il vincolo di dimensione invariata dell'editor di mondo.

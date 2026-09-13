"""Hidden Tk smoke test: exercise Apply for a static and a character mesh."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pop_bf_lab as lab

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--bf',type=Path,required=True)
    parser.add_argument('--asset',required=True)
    parser.add_argument('--glb',type=Path,required=True)
    args=parser.parse_args()
    app=lab.JadeToolkit(); app.withdraw()
    def unexpected_dialog(title,message): raise AssertionError(f'{title}: {message}')
    lab.messagebox.showerror=unexpected_dialog
    lab.messagebox.showinfo=unexpected_dialog
    try:
        app.project.open_bf(args.bf)
        asset=next(a for a in app.project.assets if args.asset in a.name)
        data=app.project.read_asset(asset)
        app._mesh_source_asset=asset
        app._mesh_data=bytearray(data)
        app._mesh_infos=lab._scan_pop_meshes(data)
        for i,m in enumerate(app._mesh_infos):
            app.mesh_tree.insert('', 'end', iid=f'mesh_{i}',values=(m.object_name,len(m.vertices),len(m.faces),m.key,m.layout_name))
        app._swap_mesh,*_=lab._load_mesh_for_swap(args.glb)
        for skinned in (False,True):
            index=next(i for i,m in enumerate(app._mesh_infos) if bool(m.skin_bones)==skinned)
            app.mesh_tree.selection_set(f'mesh_{index}')
            app._mesh_fit_target.set(skinned)
            assert app.apply_mesh_changes()
            updated=app._selected_mesh()
            assert len(updated.vertices)==24 and len(updated.faces)==12
            assert bool(updated.skin_bones)==skinned
            assert app.project.modified
            app.update_idletasks()
        print('GUI Apply OK: static + character, fit, staged patches; no archive written.')
    finally:
        app.destroy()

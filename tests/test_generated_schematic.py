import json
import unittest
from kicad_mcp.utils.generated_schematic import write_bundle


def spec(tmp_path, files=None):
    p=tmp_path/'spec.json'
    p.write_text(json.dumps({'root':'root.kicad_sch','files':files or {'root.kicad_sch':'new'}}))
    return str(p)


def check_parse_failure_preserves_original(tmp_path):
    dest=tmp_path/'dest';dest.mkdir();(dest/'root.kicad_sch').write_text('old')
    result=write_bundle(spec(tmp_path),str(dest),str(tmp_path/'jobs'),True,lambda *a,**kw:{'ok':False})
    assert not result['promoted']
    assert (dest/'root.kicad_sch').read_text()=='old'


def check_promote_preserves_backup_and_complete_sources(tmp_path):
    dest=tmp_path/'dest';dest.mkdir();(dest/'root.kicad_sch').write_text('old')
    calls=[]
    def runner(*a,**kw): calls.append(a);return {'ok':True}
    result=write_bundle(spec(tmp_path,{'root.kicad_sch':'new','child.kicad_sch':'child'}),str(dest),str(tmp_path/'jobs'),True,runner)
    assert result['promoted']
    assert (dest/'child.kicad_sch').read_text()=='child'
    assert (tmp_path/'jobs'/Path(result['backup_dir']).parent.name/'before/root.kicad_sch').read_text()=='old'
    assert calls[0][0]=='sch export netlist'


from pathlib import Path
def check_filename_escape_rejected(tmp_path,name):
    with unittest.TestCase().assertRaises(ValueError):
        write_bundle(spec(tmp_path,{'root.kicad_sch':'new',name:'evil'}),str(tmp_path/'dest'),str(tmp_path/'jobs'),True,lambda *a,**kw:{'ok':True})
    assert not (tmp_path/'dest').exists()


def check_explicit_mutation_required(tmp_path):
    with unittest.TestCase().assertRaisesRegex(ValueError,'allow_mutation'):
        write_bundle(spec(tmp_path),str(tmp_path/'dest'),str(tmp_path/'jobs'),False,lambda *a,**kw:{'ok':True})

class GeneratedSchematicTests(unittest.TestCase):
    def test_transaction(self):
        import tempfile
        for fn in [check_parse_failure_preserves_original,check_promote_preserves_backup_and_complete_sources,check_explicit_mutation_required]:
            with self.subTest(case=fn.__name__), tempfile.TemporaryDirectory() as d:
                fn(Path(d))

    def test_escapes(self):
        import tempfile
        for name in ['../evil.kicad_sch','sub/evil.kicad_sch','sub\\evil.kicad_sch','C:evil.kicad_sch','payload.py','F.pretty/../../evil.kicad_mod']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                check_filename_escape_rejected(Path(d),name)

    def test_local_library_tables_in_transaction(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp_path=Path(d)
            files={'root.kicad_sch':'new','sym-lib-table':'(sym_lib_table)','fp-lib-table':'(fp_lib_table)','root.kicad_pro':'{}'}
            out=tmp_path/'dest'
            result=write_bundle(spec(tmp_path,files),str(out),str(tmp_path/'jobs'),True,lambda *a,**kw:{'ok':True})
            assert result['promoted'] and {p.name for p in out.iterdir()}==set(files)
            files['root.kicad_pro']='[]'
            with self.assertRaisesRegex(ValueError,'JSON object'):
                write_bundle(spec(tmp_path,files),str(out),str(tmp_path/'jobs'),True,lambda *a,**kw:{'ok':True})
    def test_existing_gate_settings_preserved_and_concurrent_edit_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);dest=tmp/'dest';dest.mkdir();project=dest/'root.kicad_pro';project.write_text('{"board":{"design_settings":{"rules":{"min_clearance":0.2}}}}')
            before=project.read_bytes();files={'root.kicad_sch':'new','root.kicad_pro':'{}'}
            result=write_bundle(spec(tmp,files),str(dest),str(tmp/'jobs'),True,lambda *a,**kw:{'ok':True})
            self.assertEqual(project.read_bytes(),before);self.assertEqual(result['preserved_project_settings'],['root.kicad_pro'])
            def racing(*a,**kw):
                project.write_text('{"owner_edit":true}');return {'ok':True}
            with self.assertRaisesRegex(ValueError,'Destination changed'):
                write_bundle(spec(tmp,files),str(dest),str(tmp/'jobs'),True,racing)
            self.assertEqual(project.read_text(),'{"owner_edit":true}')
    def test_project_footprint_library_transaction(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);dest=tmp/'dest';fp=dest/'Local.pretty/Device.kicad_mod';fp.parent.mkdir(parents=True);fp.write_text('old')
            files={'root.kicad_sch':'new','Local.pretty/Device.kicad_mod':'new footprint'}
            result=write_bundle(spec(tmp,files),str(dest),str(tmp/'jobs'),True,lambda *a,**kw:{'ok':True})
            self.assertEqual(fp.read_text(),'new footprint');self.assertEqual((Path(result['backup_dir'])/'Local.pretty/Device.kicad_mod').read_text(),'old')
    def test_optional_obsolete_sheet_cleanup_has_backup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);dest=tmp/'dest';dest.mkdir();old=dest/'obsolete.kicad_sch';old.write_text('old');(dest/'keep.md').write_text('notes')
            result=write_bundle(spec(tmp),str(dest),str(tmp/'jobs'),True,lambda *a,**kw:{'ok':True},True)
            self.assertFalse(old.exists());self.assertEqual((Path(result['backup_dir'])/'obsolete.kicad_sch').read_text(),'old');self.assertTrue((dest/'keep.md').exists())


class GeneratedStaticModelTests(unittest.TestCase):
    def test_model_backup_and_active_content_rejection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d);dest=tmp/'dest';name='Local.3dshapes/Body.wrl'
            model='#VRML V2.0 utf8\nShape { geometry Box { size 1 2 3 } }\n'
            files={'root.kicad_sch':'new',name:model}
            result=write_bundle(spec(tmp,files),str(dest),str(tmp/'jobs'),True,lambda *a,**kw:{'ok':True})
            self.assertEqual((dest/name).read_text(),model)
            result=write_bundle(spec(tmp,files),str(dest),str(tmp/'jobs'),True,lambda *a,**kw:{'ok':True})
            self.assertEqual((Path(result['backup_dir'])/name).read_text(),model)
            for bad in ['#VRML V2.0 utf8\nScript {}','#VRML V2.0 utf8\nInline { url [ "https://example.com" ] }','#VRML V2.0 utf8\nShape {','not vrml']:
                with self.subTest(content=bad),self.assertRaises(ValueError):
                    write_bundle(spec(tmp,{'root.kicad_sch':'new',name:bad}),str(dest),str(tmp/'jobs'),True,lambda *a,**kw:{'ok':True})
            for escape in ['Local.3dshapes/../../evil.wrl','Local.3dshapes/a.py','Local.3dshapes/sub/evil.wrl']:
                with self.subTest(path=escape),self.assertRaises(ValueError):
                    write_bundle(spec(tmp,{'root.kicad_sch':'new',escape:model}),str(dest),str(tmp/'jobs'),True,lambda *a,**kw:{'ok':True})
                self.assertEqual((dest/name).read_text(),model)

"""Audited, candidate-first replacement of a board's electrical population.

This imports an explicitly requested new design, preserving board settings and
mechanical drawings. It never claims placement, routing or DRC acceptance.
"""
from pathlib import Path
import hashlib,json,os,shutil,uuid
from kicad_mcp.utils import schematic_overlap as sx

def val(v):return json.loads('"'+v[1]+'"') if isinstance(v,tuple) else v
def field(node,name,default=''):
    items=sx.sub(node,name)
    return val(items[0][1]) if items and len(items[0])>1 else default

def validate_board_candidate(candidate,evidence,runner):
    # KiCad10's legacy single-SVG mode treats --output as a file, whereas the
    # typed registry's directory output is correct for explicit multi mode.
    return runner('pcb export svg',str(candidate),{'--layers':'Edge.Cuts','--mode-multi':True},str(Path(evidence)/'parse-svg'),timeout_seconds=None,evidence_base=Path(evidence)/'cli-evidence')

def same_bytes(a,b):
    return hashlib.sha256(Path(a).read_bytes()).digest()==hashlib.sha256(Path(b).read_bytes()).digest()

def exported_field_value(node):
    # KiCad omits the scalar entirely for empty fields. The trailing (name ...)
    # child is metadata, not the field's value.
    values=[v for v in node[1:] if not isinstance(v,list)]
    if len(values)>1:raise ValueError('Multiple scalar field values')
    return val(values[0]) if values else ''

def netlist_data(path):
    tree=sx.parse(Path(path).read_text(encoding='utf-8-sig'))[0]
    result={}
    for c in sx.sub(sx.sub(tree,'components')[0],'comp'):
        ref=field(c,'ref')
        if ref in result:raise ValueError('Duplicate reference '+ref)
        sheet=sx.sub(c,'sheetpath')
        fields=sx.sub(c,'fields')
        properties={field(p,'name'):field(p,'value','yes') for p in sx.sub(c,'property')}
        result[ref]={'reference':ref,'value':field(c,'value'),'footprint':field(c,'footprint'),
                     'path':field(sheet[0],'tstamps') if sheet else '',
                     'uuid':field(c,'tstamps'),'pads':{},
                     'fields':{field(f,'name'):exported_field_value(f) for f in sx.sub(fields[0],'field')} if fields else {},
                     'exclude_from_bom':'exclude_from_bom' in properties,
                     'dnp':'dnp' in properties}
    for net in sx.sub(sx.sub(tree,'nets')[0],'net'):
        name=field(net,'name')
        for n in sx.sub(net,'node'):
            ref,pin=field(n,'ref'),field(n,'pin')
            if ref not in result:raise ValueError('Unknown component '+ref)
            if pin in result[ref]['pads']:raise ValueError('Multiply assigned pin '+ref+'.'+pin)
            result[ref]['pads'][pin]=name
    return list(result.values())

def sync(schematic_path,board_path,evidence_root,allow_mutation,replace_design,runner):
    from kicad_mcp.adaptive_editor.kicad_env import kicad_app_root
    from kicad_mcp.utils.pcb_schematic_import import execute
    sch=Path(schematic_path).resolve(strict=True);board=Path(board_path).resolve(strict=True)
    if sch.suffix!='.kicad_sch' or board.suffix!='.kicad_pcb':raise ValueError('Expected KiCad schematic and board')
    if allow_mutation and not replace_design:raise ValueError('Replacing the electrical design requires replace_design=True')
    evidence=Path(evidence_root)/('pcb-sync-'+uuid.uuid4().hex);evidence.mkdir(parents=True)
    net=evidence/'schematic.net';export=runner('sch export netlist',str(sch),{},str(net),timeout_seconds=None)
    if not export.get('ok'):return dict(ok=False,export=export,evidence_dir=str(evidence))
    libraries={}
    for table in [board.parent/'fp-lib-table',sch.parent/'fp-lib-table']:
        if table.exists():
            for lib in sx.sub(sx.parse(table.read_text(encoding='utf-8-sig'))[0],'lib'):
                libraries[field(lib,'name')]=field(lib,'uri').replace('${KIPRJMOD}',str(table.parent))
    stock=kicad_app_root()/'share'/'kicad'/'footprints'
    if stock.is_dir():
        for library in stock.glob('*.pretty'):libraries.setdefault(library.stem,str(library))
    source_hash=hashlib.sha256(board.read_bytes()).hexdigest()
    spec={'board':str(board),'candidate':str(evidence/board.name),'libraries':libraries,
          'components':netlist_data(net),'apply':bool(allow_mutation)}
    specfile=evidence/'import.json';specfile.write_text(json.dumps(spec,indent=2),encoding='utf8')
    receipt=execute(spec)
    if receipt['ok'] and allow_mutation:
        parsed=validate_board_candidate(spec['candidate'],evidence,runner)
        receipt['native_parse']=parsed
        receipt['ok']=bool(parsed.get('ok'))
    receipt.update(evidence_dir=str(evidence),netlist=str(net),board_sha256_before=source_hash,promoted=False)
    if receipt['ok'] and allow_mutation:
        if hashlib.sha256(board.read_bytes()).hexdigest()!=source_hash:raise ValueError('Board changed during import')
        backup=evidence/('before-'+board.name);shutil.copy2(board,backup)
        if hashlib.sha256(backup.read_bytes()).hexdigest()!=source_hash:raise OSError('Backup verification failed')
        candidate=evidence/board.name
        temporary=board.with_name('.'+board.name+'.sync-'+uuid.uuid4().hex)
        try:
            shutil.copy2(candidate,temporary);os.replace(temporary,board)
        finally:temporary.unlink(missing_ok=True)
        if not same_bytes(candidate,board):
            shutil.copy2(backup,board);raise OSError('Promoted board checksum differs; original restored')
        receipt.update(promoted=True,backup=str(backup),board_sha256_after=hashlib.sha256(board.read_bytes()).hexdigest(),candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),promoted_bytes_verified=True)
    (evidence/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf8')
    return receipt

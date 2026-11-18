"""Structural import engine; native KiCad CLI validates each promoted candidate."""
import json,pathlib,uuid,copy
from kicad_mcp.utils import schematic_overlap as sx

def val(v):return json.loads('"'+v[1]+'"') if isinstance(v,tuple) else v
def q(v):return ('s',json.dumps(str(v),ensure_ascii=False)[1:-1])
def ser(v):
    if isinstance(v,list):return '('+' '.join(ser(x) for x in v)+')'
    if isinstance(v,tuple):return '"'+v[1]+'"'
    return str(v)
def field(node,name,default=''):
    items=sx.sub(node,name)
    return val(items[0][1]) if items and len(items[0])>1 else default
def put(node,name,*values):
    node[:]=[v for v in node if not(isinstance(v,list) and sx.head(v)==name)]
    node.append([name,*values])
def refresh_ids(node):
    if not isinstance(node,list):return
    if sx.head(node)=='uuid':node[1]=q(uuid.uuid4());return
    for child in node:refresh_ids(child)

def execute(spec):
    loaded={};issues=[];pads={};cache={}
    for c in spec['components']:
        fp_id=c['footprint']
        if ':' not in fp_id:
            issues.append({'ref':c['reference'],'error':'No qualified footprint','footprint':fp_id});continue
        library,name=fp_id.split(':',1);directory=spec['libraries'].get(library)
        if not directory:
            issues.append({'ref':c['reference'],'error':'Missing footprint library','footprint':fp_id});continue
        path=pathlib.Path(directory)/(name+'.kicad_mod')
        if not path.is_file():
            issues.append({'ref':c['reference'],'error':'Footprint source absent','path':str(path)});continue
        if fp_id not in cache:cache[fp_id]=sx.parse(path.read_text(encoding='utf-8-sig'))[0]
        fp=copy.deepcopy(cache[fp_id])
        if sx.head(fp)!='footprint':raise ValueError('Not a KiCad footprint '+str(path))
        numbers={val(p[1]) for p in sx.sub(fp,'pad') if val(p[1])}
        pads[c['reference']]={'footprint':fp_id,'pads':sorted(numbers),'schematic_pins':sorted(c['pads'])}
        missing=set(c['pads'])-numbers
        if missing:issues.append({'ref':c['reference'],'error':'Connected schematic pins absent on footprint','pins':sorted(missing),'footprint':fp_id})
        loaded[c['reference']]=fp
    result={'ok':not issues,'issues':issues,'footprints':pads,'component_count':len(spec['components']),'applied':False}
    if issues or not spec['apply']:return result
    board=sx.parse(pathlib.Path(spec['board']).read_text(encoding='utf-8-sig'))[0]
    if sx.head(board)!='kicad_pcb':raise ValueError('Not a KiCad board')
    obsolete={'footprint','segment','arc','via','zone','net','group'}
    result['removed']={name:len(sx.sub(board,name)) for name in obsolete}
    board[:]=[v for v in board if not(isinstance(v,list) and sx.head(v) in obsolete)]
    nets={name:i+1 for i,name in enumerate(sorted({name for c in spec['components'] for name in c['pads'].values()}))}
    for name,code in nets.items():board.append(['net',code,q(name)])
    for index,c in enumerate(spec['components']):
        fp=loaded[c['reference']];fp[1]=q(c['footprint'])
        fp[:]=[v for v in fp if not(isinstance(v,list) and sx.head(v) in {'version','generator','generator_version'})]
        put(fp,'at',170+(index%20)*12,20+(index//20)*12)
        put(fp,'path',q(c['path'].rstrip('/')+'/'+c['uuid']))
        refresh_ids(fp);put(fp,'uuid',q(uuid.uuid4()))
        for name,value in [('Reference',c['reference']),('Value',c['value'])]:
            found=False
            for pr in sx.sub(fp,'property'):
                if val(pr[1])==name:pr[2]=q(value);found=True
            for text in sx.sub(fp,'fp_text'):
                if text[1]==name.lower():text[2]=q(value);found=True
            if not found:fp.append(['property',q(name),q(value),['at',0,-3 if name=='Reference' else 3],['layer',q('F.SilkS')],['effects',['font',['size',1,1],['thickness',.15]]]])
        for name,value in c.get('fields',{}).items():
            if name in {'Reference','Value','Footprint'}:continue
            found=False
            for pr in sx.sub(fp,'property'):
                if val(pr[1])==name:pr[2]=q(value);found=True
            if not found:fp.append(['property',q(name),q(value),['at',0,0],['layer',q('F.Fab')],['hide','yes'],['effects',['font',['size',1,1],['thickness',.15]]]])
        attrs=sx.sub(fp,'attr');tokens=[v for v in (attrs[0][1:] if attrs else []) if v not in {'exclude_from_bom','dnp'}]
        if c.get('exclude_from_bom'):tokens.append('exclude_from_bom')
        if c.get('dnp'):tokens.append('dnp')
        put(fp,'attr',*tokens)
        for pad in sx.sub(fp,'pad'):
            name=c['pads'].get(val(pad[1]))
            put(pad,'net',nets[name],q(name)) if name else put(pad,'net',0,q(''))
        board.append(fp)
    candidate=pathlib.Path(spec['candidate']);candidate.write_text(ser(board)+'\n',encoding='utf8')
    check=sx.parse(candidate.read_text(encoding='utf8'))[0];actual={}
    for fp in sx.sub(check,'footprint'):
        props={val(p[1]):val(p[2]) for p in sx.sub(fp,'property')}
        legacy=[val(p[2]) for p in sx.sub(fp,'fp_text') if p[1]=='reference']
        ref=props.get('Reference') or legacy[0]
        actual[ref]={val(p[1]):val(sx.sub(p,'net')[0][2]) for p in sx.sub(fp,'pad') if val(p[1]) and sx.sub(p,'net') and val(sx.sub(p,'net')[0][2])}
    expected={c['reference']:c['pads'] for c in spec['components']}
    result.update(ok=actual==expected,applied=True,connectivity_matches=actual==expected,
                  actual_pad_nets=actual,placement_acceptance=False,routing_acceptance=False)
    return result

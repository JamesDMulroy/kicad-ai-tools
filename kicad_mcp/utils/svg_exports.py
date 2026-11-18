"""Normalize exported SVG formatting without changing its XML drawing content."""
import hashlib,json,shutil,uuid,os
from pathlib import Path
import defusedxml.ElementTree as ET  # untrusted SVG text: no entity expansion

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def drawing(node):
    attrs={k:(' '.join(v.split()) if k in {'style','d','points'} else v) for k,v in node.attrib.items()}
    tag=node.tag.rsplit('}',1)[-1]
    text=node.text
    if tag not in {'desc','title','text','tspan','textPath'} and not (text or '').strip():text=None
    tail=node.tail if (node.tail or '').strip() else None
    return node.tag,attrs,text,tail,[drawing(c) for c in node]

def normalize(svg_dir,evidence_root,allow_mutation=False):
    if not allow_mutation:raise ValueError('SVG formatting requires allow_mutation=True')
    folder=Path(svg_dir).resolve(strict=True)
    paths=sorted(folder.glob('*.svg'))
    if not paths:raise ValueError('No exported SVG sheets')
    evidence=Path(evidence_root)/('svg-format-'+uuid.uuid4().hex);backup=evidence/'before';candidate=evidence/'candidate'
    backup.mkdir(parents=True);candidate.mkdir()
    records=[]
    for path in paths:
        if path.resolve().parent!=folder:raise ValueError('SVG resolves outside the selected directory')
        raw=path.read_bytes();source=raw.decode('utf8')
        formatted='\n'.join(line.rstrip(' \t\r') for line in source.split('\n'))
        if drawing(ET.fromstring(source))!=drawing(ET.fromstring(formatted)):
            raise ValueError('SVG drawing/text semantics would change: '+path.name)
        (backup/path.name).write_bytes(raw);(candidate/path.name).write_text(formatted,encoding='utf8',newline='\n')
        records.append({'file':path.name,'native_sha256':digest(path),'formatted_sha256':digest(candidate/path.name)})
    for row in records:
        if digest(folder/row['file'])!=row['native_sha256']:raise ValueError('SVG changed during formatting')
    replaced=[]
    try:
        for row in records:
            path=folder/row['file'];temporary=folder/('.format-'+uuid.uuid4().hex+'.svg')
            try:shutil.copy2(candidate/row['file'],temporary);os.replace(temporary,path)
            finally:temporary.unlink(missing_ok=True)
            replaced.append(path.name)
            if digest(path)!=row['formatted_sha256']:raise OSError('SVG promotion verification failed')
    except BaseException:
        for name in replaced:shutil.copy2(backup/name,folder/name)
        raise
    result={'ok':True,'files':records,'backup_dir':str(backup),'evidence_dir':str(evidence),'xml_drawing_semantics_unchanged':True,'raster_equivalence_requires_verification':True}
    (evidence/'receipt.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result

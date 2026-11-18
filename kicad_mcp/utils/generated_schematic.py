"""Write generator-produced schematic sources through a parse-before-promote transaction.

The input is data, never executable code. KiCad exports the candidate netlist
before any existing destination file is replaced. Files outside the bundle's
flat schematic/symbol namespace and safe local footprint/static-model libraries cannot be written. This is authoring, not ERC
or design acceptance; callers must run the normal gates after promotion.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import uuid
import re


def write_bundle(spec_path: str, output_dir: str, evidence_root: str,
                 allow_mutation: bool, runner, remove_obsolete_sheets: bool = False) -> dict:
    if not allow_mutation:
        raise ValueError('Schematic generation requires allow_mutation=True')
    spec_file = Path(spec_path).resolve(strict=True)
    spec = json.loads(spec_file.read_text(encoding='utf-8'))
    if set(spec) != {'root', 'files'} or not isinstance(spec['files'], dict):
        raise ValueError('Expected exactly root and files')
    root = spec['root']
    for name, content in spec['files'].items():
        p = Path(name)
        footprint=bool(re.fullmatch(r'[A-Za-z0-9_-]+\.pretty/[A-Za-z0-9_-]+\.kicad_mod',name))
        model=bool(re.fullmatch(r'[A-Za-z0-9_-]+\.3dshapes/[A-Za-z0-9_-]+\.wrl',name))
        flat=(p.name==name and '/' not in name and '\\' not in name and ':' not in name and
              (p.suffix in {'.kicad_sch','.kicad_sym'} or name in {'sym-lib-table','fp-lib-table',Path(root).stem+'.kicad_pro'}))
        if not (flat or footprint or model):
            raise ValueError(f'Not a flat schematic/symbol filename: {name}')
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f'Empty or non-text source: {name}')
        if model:
            # Static local VRML geometry only: never network or script-bearing scene nodes.
            if not content.startswith('#VRML V2.0 utf8') or re.search(r'\b(?:Script|Inline|EXTERNPROTO)\b|\burl\s*[\[\"]', content):
                raise ValueError('Only self-contained static VRML models are supported: '+name)
            if content.count('{') != content.count('}') or content.count('[') != content.count(']'):
                raise ValueError('Unbalanced VRML model: '+name)
        if p.suffix=='.kicad_pro' and not isinstance(json.loads(content),dict):
            raise ValueError('Project settings must be a JSON object')
    if root not in spec['files'] or Path(root).suffix != '.kicad_sch':
        raise ValueError('root must name a schematic in files')
    dest = Path(output_dir).resolve()
    obsolete=sorted(p.name for p in dest.glob('*.kicad_sch') if p.name not in spec['files']) if remove_obsolete_sheets else []
    originals={name:(dest/name).read_bytes() if (dest/name).exists() else None for name in [*spec['files'],*obsolete]}
    preserved=[]
    evidence = Path(evidence_root).resolve() / ('generated-' + uuid.uuid4().hex)
    candidate = evidence / 'candidate'
    candidate.mkdir(parents=True)
    for name, content in spec['files'].items():
        (candidate/name).parent.mkdir(parents=True,exist_ok=True)
        if Path(name).suffix=='.kicad_pro' and originals[name] is not None:
            (candidate/name).write_bytes(originals[name]);preserved.append(name)
        else:
            (candidate / name).write_text(content, encoding='utf-8', newline='\n')
    parsed = runner('sch export netlist', str(candidate / root), {},
                    str(evidence / 'candidate.net'), timeout_seconds=None)
    if not parsed.get('ok'):
        return dict(ok=False, promoted=False, parse_result=parsed, evidence_dir=str(evidence))
    for name,before in originals.items():
        now=(dest/name).read_bytes() if (dest/name).exists() else None
        if now!=before:raise ValueError('Destination changed during candidate parsing: '+name)
    # Preserve complete old bytes, including files unchanged by this invocation.
    backups = evidence / 'before'
    backups.mkdir()
    hashes = {}
    for name in [*spec['files'],*obsolete]:
        old = dest / name
        if old.exists():
            (backups/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(old, backups / name)
            if (backups/name).read_bytes() != old.read_bytes():
                raise OSError(f'Backup verification failed: {name}')
        if name in spec['files']:hashes[name] = hashlib.sha256((candidate/name).read_bytes()).hexdigest()
    dest.mkdir(parents=True, exist_ok=True)
    replaced = []
    try:
        for name in spec['files']:
            replaced.append(name)
            (dest/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(candidate/name, dest/name)
            if hashlib.sha256((dest/name).read_bytes()).hexdigest() != hashes[name]:
                raise OSError(f'Promotion verification failed: {name}')
        for name in obsolete:
            replaced.append(name);(dest/name).unlink()
    except BaseException:
        for name in reversed(replaced):
            if (backups/name).exists():
                shutil.copy2(backups/name, dest/name)
            else:
                (dest/name).unlink(missing_ok=True)
        raise
    receipt = dict(ok=True, promoted=True, root=str(dest/root), files=hashes,
                   spec_sha256=hashlib.sha256(spec_file.read_bytes()).hexdigest(),
                   candidate_netlist=str(evidence/'candidate.net'), evidence_dir=str(evidence),
                   backup_dir=str(backups), erc_and_visual_acceptance=False)
    receipt['preserved_project_settings']=preserved
    receipt['removed_obsolete_sheets']=obsolete
    (evidence/'receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    return receipt

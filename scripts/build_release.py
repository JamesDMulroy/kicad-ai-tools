"""Build a release: wheel, sdist, SHA256SUMS and a CycloneDX software bill of materials.

    python scripts/build_release.py [--out DIR] [--env-python PYTHON]

- The wheel and sdist are built with hatchling (it must be importable by the running interpreter).
- ``SHA256SUMS`` lists both artifacts in ``sha256sum`` format (verify with ``sha256sum -c``).
- ``kicad-ai-tools-<version>.cdx.json`` (CycloneDX 1.5) lists:
  - every distribution the package needs at run time, resolved transitively (requested extras such as
    ``mcp[cli]`` followed) from the declared dependencies and the ``boardtooling`` / ``l4`` extras as they
    are installed in ``--env-python``; what only an extra reaches has scope ``optional``
    (default: this interpreter): name, version, license, purl. A declared dependency that is not
    installed there is listed with its version range and ``kicad-ai-tools:installed=false``;
  - the KiCad plugin's pinned requirements (installed by KiCad into the plugin's own venv);
  - KiCad itself as an external, not bundled, component;
  - the SHA-256 of the wheel and the sdist.
Nothing is downloaded and nothing is installed. ``--out`` defaults to ``dist/`` (git-ignored).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRAS = ("boardtooling", "l4")

# Runs inside --env-python: the transitive closure of the requested requirements, as installed there.
_CLOSURE = r"""
import json, re, sys
from importlib import metadata
wanted = json.loads(sys.argv[1])
def norm(n): return re.sub(r"[-_.]+", "-", n).lower()
def parse(req):
    m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*(?:\[([^\]]*)\])?", req)
    return m.group(1), {e.strip() for e in (m.group(2) or "").split(",") if e.strip()}
def applies(req, extras):
    # the requirement's environment marker, evaluated for no extra and for each requested extra
    if ";" not in req: return True
    try:
        from packaging.requirements import Requirement
        marker = Requirement(req).marker
    except Exception:
        return "extra ==" not in req or any(f'extra == "{e}"' in req or f"extra == '{e}'" in req for e in extras)
    return marker is None or any(marker.evaluate({"extra": e}) for e in ({""} | extras))
out, extras_of, queue = {}, {}, [(r, True) for r in wanted]
while queue:
    spec, top = queue.pop()
    name, extras = parse(spec)
    key = norm(name)
    known = extras_of.get(key)
    if known is not None and extras <= known and (not top or out[key]["top_level"]):
        continue
    extras_of[key] = (known or set()) | extras
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        out[key] = {"name": name, "installed": False, "declared": spec, "top_level": top, "requires": []}
        continue
    meta = dist.metadata
    lic = meta.get("License-Expression") or meta.get("License") or ""
    if not lic or len(lic) > 80:
        classifiers = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
        lic = ", ".join(classifiers) if classifiers else (lic.splitlines()[0][:80] if lic else "")
    entry = out.get(key) or {"top_level": False, "declared": None}
    reqs = [r for r in (dist.requires or []) if applies(r, extras_of[key])]
    out[key] = {"name": meta["Name"], "version": dist.version, "license": lic, "installed": True,
                "declared": spec if top else entry["declared"], "top_level": top or entry["top_level"],
                "extras": sorted(extras_of[key]),
                "requires": sorted({norm(parse(r)[0]) for r in reqs})}
    for req in reqs:
        queue.append((req.split(";", 1)[0].strip(), False))
print(json.dumps(out))
"""


def _project() -> dict:
    import tomllib
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def declared_requirements(project: dict, extras: tuple[str, ...] = EXTRAS) -> list[str]:
    reqs = list(project.get("dependencies", []))
    for extra in extras:
        reqs += project.get("optional-dependencies", {}).get(extra, [])
    return reqs


def _reachable(closure: dict, roots: list[str]) -> set[str]:
    """Closure keys reachable from the given requirement strings through each entry's ``requires``."""
    def key(req):
        return re.sub(r"[-_.]+", "-", re.match(r"\s*([A-Za-z0-9_.\-]+)", req).group(1)).lower()
    seen, stack = set(), [key(r) for r in roots]
    while stack:
        k = stack.pop()
        if k in seen or k not in closure:
            continue
        seen.add(k)
        stack.extend(closure[k].get("requires", []))
    return seen


def resolve_closure(env_python: str, requirements: list[str]) -> dict:
    done = subprocess.run([env_python, "-I", "-c", _CLOSURE, json.dumps(requirements)], capture_output=True,
                          text=True, timeout=120, creationflags=0x08000000 if os.name == "nt" else 0)
    if done.returncode != 0:
        raise SystemExit(f"could not read installed metadata from {env_python}: {done.stderr[-800:]}")
    return json.loads(done.stdout)


def plugin_pins() -> list[tuple[str, str]]:
    pins = []
    for line in (ROOT / "kicad_mcp/adaptive_editor/plugin/requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.+\-]+)", line)
            if not m:
                raise SystemExit(f"plugin requirement is not an exact pin: {line!r}")
            pins.append((m.group(1), m.group(2)))
    return pins


def _purl(name: str, version: str | None) -> str:
    n = re.sub(r"[-_.]+", "-", name).lower()
    return f"pkg:pypi/{n}@{version}" if version else f"pkg:pypi/{n}"


def build_sbom(project: dict, closure: dict, pins: list[tuple[str, str]], artifacts: dict[str, str],
               timestamp: str, required: set[str] | None = None) -> dict:
    """CycloneDX 1.5 document (pure: everything it lists is passed in). ``required`` holds the closure keys
    the core dependencies reach; everything else in the closure is reached only through an extra and is
    ``optional``. Dependencies are a graph: the package depends on its direct requirements, each component
    on what it requires."""
    version = project["version"]
    required = set(closure) if required is None else required
    components = []
    for key in sorted(closure):
        dep = closure[key]
        comp = {"type": "library", "bom-ref": f"server:{key}", "name": dep["name"],
                "purl": _purl(dep["name"], dep.get("version")),
                "scope": "required" if key in required else "optional",
                "properties": [{"name": "kicad-ai-tools:part", "value": "mcp-server"},
                               {"name": "kicad-ai-tools:installed", "value": str(dep["installed"]).lower()},
                               {"name": "kicad-ai-tools:direct", "value": str(dep["top_level"]).lower()}]}
        if dep.get("version"):
            comp["version"] = dep["version"]
        if dep.get("license"):
            comp["licenses"] = [{"license": {"name": dep["license"]}}]
        if dep.get("declared"):
            comp["properties"].append({"name": "kicad-ai-tools:declared", "value": dep["declared"]})
        components.append(comp)
    for name, pin in pins:
        components.append({"type": "library", "bom-ref": f"plugin:{name.lower()}", "name": name, "version": pin,
                           "purl": _purl(name, pin), "scope": "required",
                           "properties": [{"name": "kicad-ai-tools:part", "value": "kicad-plugin-venv"},
                                          {"name": "kicad-ai-tools:pinned", "value": "true"}]})
    components.append({"type": "application", "bom-ref": "external:kicad", "name": "KiCad", "version": "10.0.5",
                       "scope": "excluded", "licenses": [{"license": {"id": "GPL-3.0-or-later"}}],
                       "properties": [{"name": "kicad-ai-tools:part", "value": "external-not-bundled"}]})
    hashes = [{"alg": "SHA-256", "content": digest} for digest in artifacts.values()]
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'kicad-ai-tools/' + version + '/' + '/'.join(sorted(artifacts.values())))}",
        "metadata": {"timestamp": timestamp,
                     "component": {"type": "application", "bom-ref": "kicad-ai-tools", "name": project["name"],
                                   "version": version, "purl": _purl(project["name"], version),
                                   "licenses": [{"license": {"id": "MIT"}}], "hashes": hashes,
                                   "properties": [{"name": f"kicad-ai-tools:artifact:{n}", "value": d}
                                                  for n, d in artifacts.items()]}},
        "components": components,
        "dependencies": [{"ref": "kicad-ai-tools",
                          "dependsOn": [f"server:{k}" for k in sorted(closure) if closure[k]["top_level"]]
                          + [f"plugin:{name.lower()}" for name, _ in pins]}]
                        + [{"ref": f"server:{k}", "dependsOn": [f"server:{r}" for r in closure[k].get("requires", [])
                                                                 if r in closure]} for k in sorted(closure)],
    }


def write_checksums(path: Path, artifacts: dict[str, str]) -> None:
    """``sha256sum`` format with LF line endings on every platform (a CR would read as part of the name)."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"{digest}  {name}\n" for name, digest in artifacts.items()))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--env-python", default=sys.executable,
                    help="interpreter whose installed packages the SBOM describes (the server's)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder
    wheel = Path(next(WheelBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])))
    sdist = Path(next(SdistBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])))
    artifacts = {p.name: _sha256(p) for p in (wheel, sdist)}
    write_checksums(out / "SHA256SUMS", artifacts)
    project = _project()
    closure = resolve_closure(a.env_python, declared_requirements(project))
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch) if epoch else None))
    required = _reachable(closure, declared_requirements(project, ()))
    sbom = build_sbom(project, closure, plugin_pins(), artifacts, stamp, required)
    sbom_path = out / f"kicad-ai-tools-{project['version']}.cdx.json"
    sbom_path.write_text(json.dumps(sbom, indent=1), encoding="utf-8")
    missing = sorted(d["name"] for d in closure.values() if not d["installed"])
    print(json.dumps({"wheel": wheel.name, "sdist": sdist.name, "checksums": "SHA256SUMS", "sbom": sbom_path.name,
                      "server_components": len(closure), "plugin_components": len(plugin_pins()),
                      "declared_not_installed": missing}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

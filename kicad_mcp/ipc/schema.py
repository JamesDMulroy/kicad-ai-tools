"""The IPC API schema of the installed KiCad, built from that build's own .proto files.

The client never relies on bundled, possibly older protocol definitions: for the installed KiCad's
commit it fetches ``api/proto`` from KiCad's GitLab (once, then cached), compiles it with ``protoc``
into a descriptor set, and loads that into a private descriptor pool. Every message the build defines
is then available by name, including ones added after any client library was released.

Nothing is generated as Python code and nothing from KiCad is redistributed with this package: the
.proto files are fetched and compiled on the user's machine.
"""
from __future__ import annotations

from functools import lru_cache
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import zipfile

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.descriptor import Descriptor

ARCHIVE_URL = ("https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/archive.zip"
               "?sha={commit}&path=api/proto")
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_BYTES = 32 * 1024 * 1024  # api/proto is well under 1 MiB; a zip bomb stops here


class SchemaError(RuntimeError):
    pass


def cache_root() -> Path:
    base = os.environ.get("KICAD_AI_TOOLS_CACHE")
    if base:
        return Path(base)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "kicad-ai-tools"  # portability-ok: platform default when the variable is unset
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kicad-ai-tools"  # portability-ok: platform default when the variable is unset


def find_protoc() -> str:
    explicit = os.environ.get("PROTOC")
    if explicit:
        return explicit
    beside = Path(sys.executable).parent / ("protoc.exe" if sys.platform == "win32" else "protoc")
    if beside.exists():
        return str(beside)
    found = shutil.which("protoc")
    if found:
        return found
    raise SchemaError("protoc not found: install the 'protoc-wheel-0' package or set PROTOC")


def fetch_protos(commit: str, dest: Path) -> Path:
    """api/proto at ``commit`` into ``dest`` (returns the directory holding board/, common/, ...).

    The request goes to gitlab.com over HTTPS only and refuses any redirect (the pinned-source opener)."""
    from kicad_mcp.utils.source_catalog import SourceCatalogError, _opener

    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise SchemaError(f"need the full 40-hex commit of the installed KiCad, got {commit!r}")
    try:
        with _opener.open(ARCHIVE_URL.format(commit=commit), timeout=60) as response:
            body = response.read(MAX_ARCHIVE_BYTES + 1)
    except SourceCatalogError as exc:
        raise SchemaError(str(exc)) from exc
    except OSError as exc:  # URLError, HTTPError, timeouts and resets: the tool's own error, not a traceback
        raise SchemaError(f"could not download api/proto for {commit}: {exc}") from exc
    if len(body) > MAX_ARCHIVE_BYTES:
        raise SchemaError("api/proto archive larger than expected; refusing")
    extract_protos(body, dest)
    if not any(dest.rglob("*.proto")):
        raise SchemaError(f"no .proto files at commit {commit}")
    return dest


def extract_protos(body: bytes, dest: Path) -> None:
    """Write the archive's ``api/proto`` members under ``dest``; a member that would land anywhere else
    (``..``, an absolute, drive-qualified or UNC name) refuses the whole archive, and so does more than
    ``MAX_EXTRACTED_BYTES`` of decompressed data (counted as it is read, not taken from the archive's header)."""
    root = dest.resolve()
    written = 0
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for member in archive.namelist():
            marker = "/api/proto/"
            if marker not in member or member.endswith("/"):
                continue
            relative = member.split(marker, 1)[1].replace("\\", "/")
            parts = PurePosixPath(relative).parts
            if (not parts or relative.startswith("/") or ":" in relative or ".." in parts):
                raise SchemaError(f"unsafe path in archive: {member}")
            target = (root / relative).resolve()
            if root not in target.parents:
                raise SchemaError(f"unsafe path in archive: {member}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source:
                data = source.read(MAX_EXTRACTED_BYTES - written + 1)
            written += len(data)
            if written > MAX_EXTRACTED_BYTES:
                raise SchemaError("api/proto archive expands beyond the expected size; refusing")
            target.write_bytes(data)


def descriptor_set(commit: str, proto_dir: Path | None = None) -> Path:
    """The compiled descriptor set for ``commit`` (built once, then reused from the cache)."""
    out = cache_root() / "api-schema" / commit / "kiapi.desc"
    if out.exists():
        return out
    source = proto_dir or fetch_protos(commit, out.parent / "proto")
    protos = sorted(p.relative_to(source).as_posix() for p in source.rglob("*.proto"))
    out.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    run = subprocess.run([find_protoc(), f"--proto_path={source}", "--include_imports",
                          f"--descriptor_set_out={out}.tmp", *protos],
                         capture_output=True, text=True, timeout=120, creationflags=flags)
    if run.returncode != 0:
        raise SchemaError(f"protoc failed for {commit}: {run.stderr.strip()[:500]}")
    os.replace(f"{out}.tmp", out)
    return out


class Schema:
    """A private pool holding one KiCad build's API messages."""

    def __init__(self, commit: str, desc_path: Path):
        self.commit = commit
        files = descriptor_pb2.FileDescriptorSet.FromString(desc_path.read_bytes())
        self.pool = descriptor_pool.DescriptorPool()
        for file_proto in files.file:
            self.pool.Add(file_proto)
        self.messages: dict[str, Descriptor] = {}
        for file_proto in files.file:
            stack = list(self.pool.FindFileByName(file_proto.name).message_types_by_name.values())
            while stack:
                descriptor = stack.pop()
                self.messages[descriptor.full_name] = descriptor
                stack.extend(descriptor.nested_types)

    def message_class(self, full_name: str):
        try:
            return message_factory.GetMessageClass(self.messages[full_name])
        except KeyError:
            raise SchemaError(f"{full_name} is not defined by KiCad {self.commit[:10]}") from None

    def new(self, full_name: str):
        return self.message_class(full_name)()


@lru_cache(maxsize=4)
def load(commit: str, proto_dir: str | None = None) -> Schema:
    return Schema(commit, descriptor_set(commit, Path(proto_dir) if proto_dir else None))

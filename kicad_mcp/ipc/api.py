"""Generic access to every KiCad IPC API command, with no hand-kept command table.

- Commands are found by name in the installed build's own schema (``schema.Schema``), so every
  command that build defines is reachable, including ones newer than any client library.
- Requests are built from JSON and checked against the command's own schema (unknown fields and wrong
  types are refused before anything is sent).
- Replies are decoded by the type name KiCad puts in the reply envelope, so no response table either.
- A request that takes a document and was given none is pointed at the session's open document.
"""
from __future__ import annotations

from typing import Any

from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor, FieldDescriptor

from kicad_mcp.ipc.client import ApiCallError
from kicad_mcp.ipc.schema import Schema

COMMAND_PACKAGES = ("kiapi.common.commands", "kiapi.board.commands", "kiapi.schematic.commands",
                    "kiapi.board.jobs", "kiapi.schematic.jobs")
DOCUMENT_TYPE = "kiapi.common.types.DocumentSpecifier"
ITEM_HEADER_TYPE = "kiapi.common.types.ItemHeader"

__all__ = ["ApiCallError", "resolve", "describe", "build_request", "fill_document", "call"]


def resolve(schema: Schema, name: str) -> Descriptor:
    """A command message by short or full name; short names are looked up in the command packages."""
    if name in schema.messages:
        return schema.messages[name]
    hits = [d for d in schema.messages.values() if d.name == name and d.file.package in COMMAND_PACKAGES]
    if not hits:
        raise ApiCallError(f"KiCad {schema.commit[:10]} defines no command {name!r}", "unknown_command", name)
    if len(hits) > 1:
        raise ApiCallError(f"{name!r} is ambiguous: {[h.full_name for h in hits]}", "ambiguous", name)
    return hits[0]


def describe(schema: Schema, name: str, depth: int = 3) -> dict[str, Any]:
    """The command's request schema: field names, types, repetition, nested messages, enum values."""
    return _schema(resolve(schema, name), depth, set())


def _schema(descriptor: Descriptor, depth: int, seen: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"message": descriptor.full_name, "fields": {}}
    for f in descriptor.fields:
        entry: dict[str, Any] = {"type": _type_name(f)}
        if f.label == FieldDescriptor.LABEL_REPEATED:
            entry["repeated"] = True
        if f.enum_type is not None:
            entry["values"] = [v.name for v in f.enum_type.values]
        if f.message_type is not None and depth > 0 and f.message_type.full_name not in seen \
                and not f.message_type.full_name.startswith("google.protobuf."):
            entry["schema"] = _schema(f.message_type, depth - 1, seen | {descriptor.full_name})
        out["fields"][f.name] = entry
    return out


def _type_name(f: FieldDescriptor) -> str:
    if f.message_type is not None:
        return f.message_type.full_name
    if f.enum_type is not None:
        return f.enum_type.full_name
    return {FieldDescriptor.TYPE_STRING: "string", FieldDescriptor.TYPE_BOOL: "bool",
            FieldDescriptor.TYPE_DOUBLE: "double", FieldDescriptor.TYPE_FLOAT: "float",
            FieldDescriptor.TYPE_BYTES: "bytes"}.get(f.type, "int")


def build_request(schema: Schema, name: str, payload: dict[str, Any] | None):
    """The request message from JSON, strictly: unknown fields or wrong types raise ApiCallError."""
    descriptor = resolve(schema, name)
    request = schema.new(descriptor.full_name)
    try:
        json_format.ParseDict(payload or {}, request, ignore_unknown_fields=False,
                              descriptor_pool=schema.pool)
    except json_format.ParseError as exc:
        raise ApiCallError(f"invalid {descriptor.name} request: {exc}", "bad_request", name) from None
    return request


def fill_document(request, document) -> Any:
    """Point a request's empty document (or item header's document) at the session's document."""
    if document is None:
        return request
    for f in request.DESCRIPTOR.fields:
        if f.message_type is None or f.label == FieldDescriptor.LABEL_REPEATED:
            continue
        if f.message_type.full_name == DOCUMENT_TYPE and not request.HasField(f.name):
            getattr(request, f.name).CopyFrom(document)
        elif f.message_type.full_name == ITEM_HEADER_TYPE:
            header = getattr(request, f.name)
            if not header.HasField("document"):
                header.document.CopyFrom(document)
        elif not f.message_type.full_name.startswith("google.protobuf.") and any(
                g.name == "document" and g.message_type is not None
                and g.message_type.full_name == DOCUMENT_TYPE for g in f.message_type.fields):
            nested = getattr(request, f.name)  # e.g. a job's job_settings.document
            if not nested.HasField("document"):
                nested.document.CopyFrom(document)
    return request


def call(session, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one API command on a session; returns {"command", "response_type", "response"}."""
    request = fill_document(build_request(session.schema, name, payload), session.document_spec())
    response = session.client.send(request)
    return {"command": request.DESCRIPTOR.name, "response_type": response.DESCRIPTOR.full_name,
            "response": json_format.MessageToDict(response, preserving_proto_field_name=True,
                                                  descriptor_pool=session.schema.pool)}

"""Minimal IPC API client: an NNG request socket carrying KiCad's own envelope messages.

Requests and replies are typed by the installed build's schema (``schema.Schema``), so the client
needs no generated code and no client library pinned to some other KiCad version.
"""
from __future__ import annotations

from google.protobuf import any_pb2

from kicad_mcp.ipc.schema import Schema

REQUEST = "kiapi.common.ApiRequest"
RESPONSE = "kiapi.common.ApiResponse"
STATUS_ENUM = "kiapi.common.ApiStatusCode"


class ApiCallError(RuntimeError):
    def __init__(self, message: str, status: str = "", command: str = ""):
        super().__init__(message)
        self.status = status
        self.command = command


class Client:
    def __init__(self, socket_url: str, schema: Schema, client_name: str = "kicad-ai-tools",
                 timeout_ms: int = 30000):
        self.socket_url = socket_url
        self.schema = schema
        self.client_name = client_name
        self.timeout_ms = timeout_ms
        self.token = ""
        self._socket = None

    def _connect(self):
        import pynng

        if self._socket is None:
            try:
                self._socket = pynng.Req0(dial=self.socket_url, block_on_dial=True,
                                          send_timeout=self.timeout_ms, recv_timeout=self.timeout_ms)
            except pynng.exceptions.NNGException as exc:
                raise ConnectionError(f"cannot connect to KiCad at {self.socket_url}: {exc}") from None
        return self._socket

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def send(self, request):
        """Send a request message; return the reply message, decoded by the type KiCad names."""
        import pynng

        envelope = self.schema.new(REQUEST)
        envelope.header.kicad_token = self.token
        envelope.header.client_name = self.client_name
        envelope.message.Pack(request)
        socket = self._connect()
        try:
            socket.send(envelope.SerializeToString())
            raw = socket.recv_msg().bytes
        except pynng.exceptions.NNGException as exc:
            self.close()  # a Req0 socket is unusable after a failed exchange
            raise ConnectionError(f"KiCad did not answer {request.DESCRIPTOR.name}: {exc}") from None
        reply = self.schema.new(RESPONSE)
        reply.ParseFromString(raw)
        status_enum = self.schema.pool.FindEnumTypeByName(STATUS_ENUM)
        status = status_enum.values_by_number[reply.status.status].name
        if status != "AS_OK":
            raise ApiCallError(reply.status.error_message or "KiCad refused the request", status,
                               request.DESCRIPTOR.name)
        if not self.token:
            self.token = reply.header.kicad_token
        return unpack(self.schema, reply.message)


def unpack(schema: Schema, packed: any_pb2.Any):
    type_name = packed.TypeName()
    if type_name in schema.messages:
        message = schema.new(type_name)
    elif type_name.startswith("google.protobuf."):
        # A well-known type KiCad replies with but its .proto files never import (Empty for Ping, ...).
        message = _well_known(type_name)
    else:
        raise ApiCallError(f"KiCad replied with {type_name}, which its schema does not define", "bad_reply")
    packed.Unpack(message)
    return message


def _well_known(type_name: str):
    from google.protobuf import (
        any_pb2,
        duration_pb2,
        empty_pb2,
        field_mask_pb2,
        struct_pb2,
        symbol_database,
        timestamp_pb2,
        wrappers_pb2,
    )

    del any_pb2, duration_pb2, empty_pb2, field_mask_pb2, struct_pb2, timestamp_pb2, wrappers_pb2  # registered
    return symbol_database.Default().GetSymbol(type_name)()

"""Stateless MCP 2026-07-28 core for the read-only House Brain access lane.

Pure protocol/HTTP-envelope logic only. It opens no listener, performs no OAuth
verification, owns no Broker credential, and delegates authorized reads to the
already-qualified SnapshotReadBoundary.

Every tools/list and tools/call first asks the injected #56 kill-switch read gate.
Only an exact ``{"decision": "ALLOW"}`` result lets the request through; a missing
gate, an exception or any other result is refused with AI_KILL_SWITCH_BLOCKED.
"""
from __future__ import annotations
import json, math, re
from typing import Any, Final
from tools.house_brain_ai_broker_boundary import BoundaryError, SnapshotReadBoundary, TOOL_SCOPES
from tools.build_house_brain_ai_snapshot_catalog import build_catalog

PROTOCOL_VERSION: Final = "2026-07-28"
SERVER_NAME: Final = "house-brain-ai-gateway"
SERVER_VERSION: Final = "0.1.0-candidate"
MAX_RPC_BYTES: Final = 32768
MAX_RPC_DEPTH: Final = 20
MAX_RPC_NODES: Final = 4096
SERVER_INFO_KEY: Final = "io.modelcontextprotocol/serverInfo"
PROTOCOL_META_KEY: Final = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_KEY: Final = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_KEY: Final = "io.modelcontextprotocol/clientCapabilities"
HEADER_NAME_RE: Final = re.compile(r"[A-Za-z0-9-]{1,64}")

class McpError(ValueError):
    def __init__(self, code:int, message:str, *, http_status:int=200, requested_version:str|None=None):
        super().__init__(message); self.code=code; self.message=message; self.http_status=http_status
        self.requested_version=requested_version
    def as_error(self):
        result={"code":self.code,"message":self.message}
        if self.code == -32022 and self.requested_version is not None:
            result["data"]={"supported":[PROTOCOL_VERSION],"requested":self.requested_version}
        return result

def _protocol(value):
    # Only bounded version identifiers may be reflected in compatibility errors.
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",value) is None:
        raise McpError(-32600,"PROTOCOL_VERSION_INVALID",http_status=400)
    if value != PROTOCOL_VERSION:
        raise McpError(-32022,"UNSUPPORTED_PROTOCOL_VERSION",http_status=400,requested_version=value)

def _server_meta():
    return {SERVER_INFO_KEY: {"name":SERVER_NAME,"version":SERVER_VERSION}}

def response_id(value):
    """Return only a valid bounded correlation ID; never reflect arbitrary data."""
    try:
        return _rpc_id(value)
    except McpError:
        return None

def _error(request_id, exc):
    return exc.http_status, {"jsonrpc":"2.0","id":response_id(request_id),"error":exc.as_error(),"_meta":_server_meta()}

def _rpc_id(value):
    if value is None or type(value) not in (str,int) or isinstance(value,bool): raise McpError(-32600,"INVALID_REQUEST")
    if type(value) is str and (not value or len(value)>128): raise McpError(-32600,"INVALID_REQUEST")
    if type(value) is int and abs(value)>9007199254740991: raise McpError(-32600,"INVALID_REQUEST")
    if type(value) is str:
        try: value.encode("utf-8")
        except UnicodeError: raise McpError(-32600,"INVALID_REQUEST") from None
    return value

def _validate_complexity(value):
    stack=[(value,0)]; nodes=0
    while stack:
        item,depth=stack.pop(); nodes+=1
        if nodes>MAX_RPC_NODES or depth>MAX_RPC_DEPTH: raise McpError(-32600,"REQUEST_COMPLEXITY_LIMIT",http_status=400)
        if type(item) is dict: stack.extend((v,depth+1) for pair in item.items() for v in pair)
        elif type(item) is list: stack.extend((v,depth+1) for v in item)
        elif type(item) is float and not math.isfinite(item): raise McpError(-32700,"INVALID_JSON",http_status=400)
        elif type(item) is str:
            try: item.encode("utf-8")
            except UnicodeError: raise McpError(-32700,"INVALID_JSON",http_status=400) from None

def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise McpError(-32700,"DUPLICATE_JSON_KEY",http_status=400)
        result[key]=value
    return result

def _nonfinite(_):
    raise McpError(-32700,"INVALID_JSON",http_status=400)

def decode_rpc(raw):
    if type(raw) is not bytes or not 0<len(raw)<=MAX_RPC_BYTES: raise McpError(-32600,"REQUEST_SIZE_INVALID",http_status=413)
    try: value=json.loads(raw.decode("utf-8"),object_pairs_hook=_pairs,parse_constant=_nonfinite)
    except McpError: raise
    except (ValueError,UnicodeError,RecursionError): raise McpError(-32700,"INVALID_JSON",http_status=400) from None
    if type(value) is not dict: raise McpError(-32600,"INVALID_REQUEST",http_status=400)
    _validate_complexity(value); return value

def _envelope(request):
    if type(request) is not dict: raise McpError(-32600,"INVALID_REQUEST",http_status=400)
    _validate_complexity(request)
    if set(request)!={"jsonrpc","id","method","params"} or request.get("jsonrpc")!="2.0": raise McpError(-32600,"INVALID_REQUEST",http_status=400)
    rid=_rpc_id(request.get("id")); method=request.get("method"); params=request.get("params")
    if type(method) is not str or not method or len(method)>96 or type(params) is not dict: raise McpError(-32600,"INVALID_REQUEST",http_status=400)
    meta=params.get("_meta")
    if type(meta) is not dict: raise McpError(-32600,"META_REQUIRED",http_status=400)
    _protocol(meta.get(PROTOCOL_META_KEY))
    if type(meta.get(CLIENT_CAPABILITIES_KEY)) is not dict:
        raise McpError(-32600,"CLIENT_CAPABILITIES_REQUIRED",http_status=400)
    client=meta.get(CLIENT_INFO_KEY)
    if client is not None and (type(client) is not dict or type(client.get("name")) is not str or not 1<=len(client["name"])<=128 or type(client.get("version")) is not str or not 1<=len(client["version"])<=64):
        raise McpError(-32600,"CLIENT_INFO_INVALID",http_status=400)
    return rid,method,params

def _tool_wire(tool, auth_profile):
    scope=TOOL_SCOPES[tool["name"]]
    descriptor={"name":tool["name"],"description":tool["description"],"inputSchema":tool["inputSchema"],"outputSchema":tool["outputSchema"],"annotations":tool["annotations"],"securitySchemes":[{"type":"oauth2","scopes":[scope]}]}

    # noauth describes MCP-level linking only. The private stdio entrypoint has
    # an external tunnel perimeter and still enforces every local grant/scope.
    if auth_profile == "private_tunnel":
        descriptor["securitySchemes"]=[{"type":"noauth"}]
    return descriptor

def _gate_allows(read_gate):
    # Fail closed: no gate, a raising gate or anything but an exact ALLOW blocks.
    if read_gate is None: return False
    try: result=read_gate()
    except Exception: return False
    return type(result) is dict and result.get("decision")=="ALLOW"

class HouseBrainMcpCore:
    def __init__(self, *, boundary, contract, auth_profile="oauth2", read_gate=None):
        if type(boundary) is not SnapshotReadBoundary or type(contract) is not bytes: raise TypeError("invalid wiring")
        if read_gate is not None and not callable(read_gate): raise TypeError("invalid wiring")
        self._read_gate=read_gate
        if type(auth_profile) is not str or auth_profile not in ("oauth2","private_tunnel"):
            raise ValueError("AUTH_PROFILE_INVALID")
        self._auth_profile=auth_profile
        # Prove identity before discovery OR any tool call, not only on listing.
        build_catalog(contract)
        self._boundary=boundary; self._contract=bytes(contract)
    async def dispatch(self, request):
        rid,method,params=_envelope(request)
        if method=="server/discover":
            if set(params)!={"_meta"}: raise McpError(-32602,"INVALID_PARAMS")
            result={"resultType":"complete","supportedVersions":[PROTOCOL_VERSION],"capabilities":{"tools":{"listChanged":False}},"_meta":_server_meta(),"instructions":"Read-only House Brain engineering evidence; no physical or mutation authority.","ttlMs":0,"cacheScope":"private"}
        elif method=="tools/list":
            if set(params)!={"_meta"}: raise McpError(-32602,"INVALID_PARAMS")
            if not _gate_allows(self._read_gate): raise McpError(-32001,"AI_KILL_SWITCH_BLOCKED")
            try: catalog=self._boundary.catalog(self._contract)
            except BoundaryError as exc: raise McpError(-32001,str(exc)) from None
            result={"tools":[_tool_wire(t,self._auth_profile) for t in catalog["tools"]],"ttlMs":0,"cacheScope":"private"}
        elif method=="tools/call":
            if set(params)!={"_meta","name","arguments"} or type(params.get("name")) is not str: raise McpError(-32602,"INVALID_PARAMS")
            if not _gate_allows(self._read_gate): raise McpError(-32001,"AI_KILL_SWITCH_BLOCKED")
            try: output=await self._boundary.call(params["name"],params["arguments"])
            except BoundaryError as exc: raise McpError(-32001,str(exc)) from None
            encoded=json.dumps(output,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)
            result={"content":[{"type":"text","text":encoded}],"structuredContent":output,"isError":False}
        else: raise McpError(-32601,"METHOD_NOT_FOUND")
        result["resultType"]="complete"
        result["_meta"]=_server_meta()
        return {"jsonrpc":"2.0","id":rid,"result":result,"_meta":_server_meta()}
    async def handle_http(self, *, method, headers, body):
        request_id=None
        try:
            if self._auth_profile == "private_tunnel":
                raise McpError(-32600,"PRIVATE_PROFILE_HTTP_FORBIDDEN",http_status=405)
            if method!="POST": raise McpError(-32600,"HTTP_METHOD_REJECTED",http_status=405)
            if type(headers) is not tuple or len(headers)>64: raise McpError(-32600,"HEADERS_INVALID",http_status=400)
            parsed={}; total=0
            for pair in headers:
                if type(pair) is not tuple or len(pair)!=2 or any(type(x) is not str for x in pair): raise McpError(-32600,"HEADERS_INVALID",http_status=400)
                name,value=pair; total+=len(name)+len(value)
                if total>8192 or HEADER_NAME_RE.fullmatch(name) is None or len(value)>2048 or any(ord(c)<32 or ord(c)==127 for c in value): raise McpError(-32600,"HEADERS_INVALID",http_status=400)
                key=name.lower()
                if key in parsed: raise McpError(-32600,"DUPLICATE_HEADER",http_status=400)
                parsed[key]=value.strip()
            if parsed.get("content-type","").lower().replace(" ","") not in ("application/json","application/json;charset=utf-8"): raise McpError(-32600,"CONTENT_TYPE_REJECTED",http_status=415)
            _protocol(parsed.get("mcp-protocol-version"))
            request=decode_rpc(body); request_id=request.get("id"); _rid,rpc_method,params=_envelope(request)
            if parsed.get("mcp-method")!=rpc_method: raise McpError(-32020,"HEADER_MISMATCH",http_status=400)
            expected=params.get("name") if rpc_method=="tools/call" else None; supplied=parsed.get("mcp-name")
            if (expected is None and supplied is not None) or (expected is not None and supplied!=expected): raise McpError(-32020,"HEADER_MISMATCH",http_status=400)
            response=await self.dispatch(request); status=200
        except McpError as exc: status,response=_error(request_id,exc)
        encoded=json.dumps(response,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
        return status,{"content-type":"application/json; charset=utf-8","cache-control":"no-store","x-content-type-options":"nosniff"},encoded

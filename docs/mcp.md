# MCP Streamable HTTP tool adapter

Agentlog ships a narrow optional MCP client boundary:

```text
ToolCallRequested
→ ToolRegistry / ExecutionPolicy
→ MCPTool.execute(operation_id=...)
→ MCP Streamable HTTP call
→ ToolCallSucceeded | ToolCallFailed
```

Install it explicitly:

```bash
python -m pip install "agentlog[mcp]"
```

## Static registration

`MCPTool` implements the existing `Tool` protocol. The application owns the
tool definition and therefore owns the versioned model-visible catalog:

```python
from agentlog import MCPTool, ToolDefinition, ToolRegistry

definition = ToolDefinition(
    "lookup_invoice",
    "Look up one invoice",
    {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}},
        "required": ("invoice_id",),
        "additionalProperties": False,
    },
)

registry = ToolRegistry()
registry.register(
    MCPTool(
        definition,
        url="http://127.0.0.1:8001/mcp",
    )
)
```

Automatic `list_tools()` discovery is intentionally absent. Discovering and
silently changing the catalog would bypass Agentlog's definition/resource
version contract.

## Operation identity

Agentlog passes the durable `ToolCallRequested` operation identity into every
physical retry. A mutating MCP server can receive it as an argument that is
not exposed to the model:

```python
MCPTool(
    definition,
    url="http://127.0.0.1:8001/mcp",
    operation_id_argument="operation_id",
)
```

The configured argument name must not appear in the model-visible input
schema. The adapter injects it after registry and policy validation, preventing
the model from choosing or replacing the idempotency key.

The adapter does not deduplicate calls. Physical execution remains
at-least-once; Agentlog commits at most one outcome for the durable request.
The MCP server must implement deduplication for non-idempotent operations.

## Result and failure boundary

Each invocation opens, initializes, and closes one official MCP Python SDK
Streamable HTTP session. The adapter accepts:

- `structuredContent`; or
- exactly one text content block containing valid JSON.

The value is frozen through Agentlog's JSON event boundary before it is
returned. Transport, initialization, timeout, protocol, MCP `isError`, and
invalid-content failures become `ToolExecutionFailed`; the durable model loop
records the existing `ToolCallFailed` outcome. Argument and application-policy
rejections still occur before the network call.

## Explicit limits

This adapter does not provide:

- an MCP server framework;
- stdio or legacy SSE transport;
- dynamic tool discovery;
- resources, prompts, sampling, or elicitation integration;
- authentication or trust policy;
- persistent session pooling;
- provider-side exactly-once execution.

The real FastMCP/MinIO integration and crash/restart evidence live in
[`examples/local_qaqc/`](../examples/local_qaqc/).

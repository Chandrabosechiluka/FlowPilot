# Connect FlowPilot to an AI client

FlowPilot speaks MCP in two ways:

- **stdio**: the AI client starts FlowPilot itself as a child process and talks over stdin/stdout.
  This is what desktop clients use. `uv run flowpilot-mcp`
- **Streamable HTTP**: FlowPilot runs as a web service at `http://127.0.0.1:8765/mcp` and clients
  connect by URL. This is what a deployed service uses (Week 5). `uv run flowpilot-mcp --http`

Either way, GarageFlow (real, or the sandbox) must be running first, and `.env` must point at it.

## Claude Desktop (stdio)

Open Claude Desktop → Settings → Developer → Edit Config. That opens `claude_desktop_config.json`.
Add FlowPilot under `mcpServers` (adjust the folder):

```json
{
  "mcpServers": {
    "flowpilot": {
      "command": "uv",
      "args": ["--directory", "F:\\projects\\FlowPilot", "run", "flowpilot-mcp"],
      "env": {
        "GARAGEFLOW_BASE_URL": "http://localhost:5090",
        "GARAGEFLOW_EMAIL": "advisor@garageflow.local",
        "GARAGEFLOW_PASSWORD": "Garage@12345"
      }
    }
  }
}
```

Restart Claude Desktop. The tools appear in the tools menu. Try:

- "Is GarageFlow healthy?"
- "What is booked today? Anything look wrong?"
- "Find the customer with car TS07 HJ 9012 and tell me about their visit today."
- Use the **triage_ticket** prompt with: "Shop manager says yesterday's no-shows were handled but
  today's late customers are still showing as Booked."

If `uv` is not found, put its full path in `command` (run `where.exe uv` to find it).

## Claude Code (stdio)

```powershell
claude mcp add flowpilot -- uv --directory F:\projects\FlowPilot run flowpilot-mcp
```

The variables come from FlowPilot's `.env`, because `uv --directory` runs in that folder.

## MCP Inspector (see the raw protocol)

The Inspector is a web page for calling tools by hand and seeing the exact JSON. It is the
best way to learn what an AI client actually sees (needs Node.js):

```powershell
npx @modelcontextprotocol/inspector uv --directory F:\projects\FlowPilot run flowpilot-mcp
```

Look at: the tool list and each tool's input schema (from the Python type hints and `Field`
descriptions), `structuredContent` versus `content` in a result, and what `isError: true` looks
like when you call `search_audit_log` as an Advisor.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Server disconnects immediately | It crashed at start-up: usually a missing `.env` value. The message is on stderr (Claude Desktop: Settings → Developer → open logs). |
| "Could not reach GarageFlow" | GarageFlow (or the sandbox) is not running, or the URL/port is wrong. |
| "Sign-in failed" | Wrong email/password. Demo users exist only when GarageFlow runs in Development. |
| Garbled protocol errors | Something printed to stdout. With stdio, only MCP may use stdout; log to stderr. |

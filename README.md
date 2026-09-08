# MCP Server — Wholesale Distributor Inventory & Orders

Local **Model Context Protocol (MCP)** server that exposes the inventory and order
management operations of a fictional wholesale distributor (*Distribuidora El Quetzal*)
so that an LLM-powered chatbot can answer stock questions and place orders in natural
language.

**Course:** CC3067 Networks — Universidad del Valle de Guatemala
**Project 1:** Use of an existing protocol
**Author:** Nicolle Gordillo (22246)

The repository contains the complete project: a host chatbot, an MCP client, the custom
MCP server in both local (stdio) and remote (HTTP) form, and integration with the official
Filesystem and Git MCP servers.

> The JSON-RPC 2.0 message format, method routing and request/response correlation are
> implemented **manually** with the Python standard library. No MCP SDK or helper
> library (FastMCP, `mcp`, etc.) is used, as required by the assignment.

---

## Table of contents

- [Implemented features](#implemented-features)
- [Use case](#use-case)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the chatbot](#running-the-chatbot)
- [Running the server standalone](#running-the-server-standalone)
- [Remote server](#remote-server)
- [Connecting the server to another host](#connecting-the-server-to-another-host)
- [Tool specification](#tool-specification)
- [Protocol specification](#protocol-specification)
- [Usage examples](#usage-examples)
- [Data sources](#data-sources)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## Implemented features

| # | Requirement | Where |
|---|---|---|
| 1 | LLM connection at API level | `llm_providers.py`, raw HTTP with `urllib` (no SDK). Gemini and Anthropic supported |
| 2 | Session context | `Anfitrion.historial`, full history sent on every request |
| 3 | Log of all MCP interactions | `BitacoraMCP`, live display + `/log` command + `logs/chatbot_mcp.log` |
| 4 | Official Filesystem and Git MCP servers | `servers_config.json` |
| 5 | Custom local MCP server (industry use case) | `server.py` + `tools.py` |
| 6 | Same server running remotely | `server_http.py` + `Dockerfile` → Cloud Run |
| 7 | Wireshark traffic analysis | `docs/analisis_wireshark.md` |
| — | Terminal UI (extra credit) | `chatbot.py`, ANSI colors, structured layout, commands |

The JSON-RPC layer is written by hand on both sides — server (`server.py`) and client
(`mcp_client.py`) — as required.

---

## Use case

A wholesale distributor of consumer goods serves dozens of small retail stores that call
or message every day to ask about product availability, prices and the status of their
orders. Those questions are plain reads against the inventory system, but they consume
the sales team's time. This server exposes those operations as MCP tools so a chatbot can
resolve them directly, including placing a new order.

The server covers both **read** operations (catalog, stock, orders, customer history) and
a **write** operation (`crear_pedido`), which makes validation, atomicity and error
reporting a real part of the implementation rather than a formality.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  HOST — chatbot.py                                          │
│    · Anthropic Messages API (urllib, no SDK)                │
│    · conversation context · MCP interaction log             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  MCP CLIENTS — mcp_client.py (manual JSON-RPC 2.0)    │  │
│  └───┬───────────────┬───────────────┬───────────────┬───┘  │
└──────┼───────────────┼───────────────┼───────────────┼──────┘
       │ stdio         │ stdio         │ stdio         │ HTTP
       ▼               ▼               ▼               ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌──────────────┐
│ server.py   │ │ Filesystem  │ │ Git         │ │server_http.py│
│ (local)     │ │ (official)  │ │ (official)  │ │ (Cloud Run)  │
└──────┬──────┘ └─────────────┘ └─────────────┘ └──────┬───────┘
       │                                               │
       └──────────────► tools.py ◄─────────────────────┘
                      (business logic,
                       shared unchanged)
                            │
                ┌───────────┴────────────┐
                ▼                        ▼
        ┌───────────────┐      ┌────────────────────┐
        │ SQLite        │      │ Banco de Guatemala │
        │ distribuidora │      │ exchange-rate API  │
        └───────────────┘      └────────────────────┘
```

`server.py` holds only the protocol; `tools.py` holds only the business logic. The
separation is deliberate: `server_http.py` imports `process_message` from `server.py` and
reuses `tools.py` **without a single change** — the only thing that differs between the
local and the remote server is the transport. The same symmetry exists on the client side:
`MCPClient` is identical for both, and only the transport object changes.

---

## Requirements

- **Python 3.8 or newer** — no third-party packages required (standard library only).
- Internet access is optional. It is used solely by `convertir_total_a_dolares`, which
  falls back to a local rate when the external service is unreachable.

Verify your version:

```bash
python3 --version
```

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/<your-username>/mcp-inventario.git
cd mcp-inventario
```

**2. Create the database**

The repository does not ship the `.db` file; it is generated from a script so the dataset
is reproducible on any machine.

```bash
python3 db/seed.py
```

Expected output:

```
Base de datos creada en: .../db/distribuidora.db
  productos     : 92
  bodegas       : 3
  existencias   : 276
  clientes      : 20
  pedidos       : 40
  lineas pedido : 117
```

**3. Verify the installation**

```bash
python3 test_client.py
```

This launches the server as a subprocess and walks the full protocol: handshake, tool
listing, successful calls, business errors and protocol errors. If it ends with
`Recorrido completo finalizado sin excepciones`, the server is working.

> To regenerate the database from scratch at any point (for example after a demo that
> created orders), just run `python3 db/seed.py` again — it drops and rebuilds everything.

**4. Set your API key** (only needed for the chatbot)

The host works with more than one LLM provider. Set the key for the one you want; the
provider is auto-detected from whichever key is present.

```powershell
$env:GEMINI_API_KEY="..."                # Google Gemini — free tier
$env:ANTHROPIC_API_KEY="sk-ant-..."      # Anthropic Claude
```

```bash
export GEMINI_API_KEY=...                # Linux / macOS
```

Get a Gemini key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
To force a provider or model: `--provider gemini --model gemini-2.0-flash`.

**5. Optional — official MCP servers**

The Filesystem and Git servers are launched on demand by `npx` and `uvx`, so they need
[Node.js](https://nodejs.org) and [uv](https://docs.astral.sh/uv/) installed. If you don't
have them, set `"disabled": true` on those entries in `servers_config.json`; everything
else still works.

```bash
mkdir -p sandbox/demo-repo && git -C sandbox/demo-repo init
```

---

## Running the chatbot

```bash
python3 chatbot.py
```

The chatbot connects to every server declared in `servers_config.json`, aggregates their
tools and offers them to the model. Tool names are namespaced as `server__tool` to avoid
collisions between servers.

| Option | Description |
|---|---|
| `--config PATH` | Server configuration file. Default `servers_config.json`. |
| `--provider NAME` | `gemini` or `anthropic`. Auto-detected from the key in the environment. |
| `--model NAME` | LLM model. Also settable via `LLM_MODEL`. |
| `--quiet` | Hides the live MCP message feed. |
| `--no-color` | Disables ANSI colors. |

In-session commands:

| Command | Description |
|---|---|
| `/tools` | Lists available tools grouped by server. |
| `/servers` | Connection status and ping per server. |
| `/log` | Recent MCP interactions (`/log todo` for full messages). |
| `/quiet` | Toggles the live message feed. |
| `/reset` | Clears the conversation context. |
| `/salir` | Ends the session. |

Things worth trying:

```
¿Tienen aceite vegetal disponible?
Soy el cliente 3, quiero 10 cajas de ese aceite
¿Cuánto sería en dólares?
¿Quién fue Alan Turing?        ← general question, no tools
¿En qué fecha nació?           ← follow-up, proves context is kept
```

---

## Running the server standalone

The server communicates over **stdin/stdout**, so running it directly leaves it waiting
for JSON-RPC messages on standard input. That is the expected behaviour: the host process
is the one meant to launch it.

```bash
python3 server.py
```

Options:

| Option | Description |
|---|---|
| `--log-file PATH` | Log file path. Default: `logs/mcp_server.log`. Use `--log-file ''` to disable. |
| `--verbose` | Logs the full content of every JSON-RPC message exchanged (`>>` inbound, `<<` outbound). |

Environment variables:

| Variable | Description |
|---|---|
| `MCP_DB_PATH` | Overrides the SQLite database location. Default: `db/distribuidora.db`. |

**Manual smoke test** — send a message by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 server.py --log-file ''
```

> **Important:** stdout is reserved exclusively for protocol messages. All logging goes to
> stderr and to the log file. Printing anything else to stdout would corrupt the stream.

---

## Remote server

The same server, deployed to the cloud over HTTP. `server_http.py` imports the protocol
layer from `server.py` and the business logic from `tools.py` without modifying either —
only the transport changes.

Run it locally first:

```bash
python3 server_http.py --port 8080 --verbose
```

```bash
curl http://localhost:8080/health
curl -X POST http://localhost:8080/mcp -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

| Endpoint | Method | Description |
|---|---|---|
| `/mcp` | POST | JSON-RPC message in, JSON-RPC message out. Notifications get `202` with no body. |
| `/mcp` | DELETE | Closes the session identified by `Mcp-Session-Id`. |
| `/health` | GET | Health check for the cloud load balancer. |
| `/` | GET | Server info and tool names. |

Deploy to Cloud Run:

```bash
gcloud run deploy mcp-distribuidora --source . --region us-central1 --allow-unauthenticated
```

Then set the returned URL in `servers_config.json` under `distribuidora_remoto` and remove
`"disabled": true`. Full instructions, including troubleshooting and the caveat about
ephemeral storage, are in [`docs/despliegue_cloud_run.md`](docs/despliegue_cloud_run.md).

---

## Connecting the server to a host

### Claude Desktop

Add the server to `claude_desktop_config.json` (see `examples/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "distribuidora-inventario": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp-inventario/server.py"]
    }
  }
}
```

Config file location:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

Restart Claude Desktop; the tools appear in the tools menu.

### Custom chatbot

The host must spawn the server as a subprocess with piped stdin/stdout and then:

1. send `initialize` and wait for the response;
2. send the `notifications/initialized` notification (no response expected);
3. call `tools/list` and pass the returned schemas to the LLM as available tools;
4. call `tools/call` whenever the model requests a tool, and feed the result back.

`test_client.py` is a minimal, working reference implementation of exactly this sequence.

---

## Tool specification

All prices are in Guatemalan quetzales (GTQ). Warehouses: `1` Central, `2` Occidente,
`3` Oriente.

### `buscar_productos`

Searches the catalog by partial name match and/or category.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `consulta` | string | no | Text to match inside the product name. |
| `categoria` | string | no | Exact category. One of: Abarrotes, Bebidas, Lacteos, Limpieza, Higiene personal, Snacks, Enlatados, Desechables. |
| `limite` | integer | no | Max results, 1–50. Default `10`. |

Returns `encontrados`, `criterio` and a `productos` array with `sku`, `nombre`,
`categoria`, `precio`, `unidad_medida`.

### `consultar_stock`

Returns on-hand quantities for a SKU.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `sku` | string | **yes** | Product code, e.g. `AB-0001`. |
| `bodega_id` | integer | no | Restrict to one warehouse (1, 2 or 3). |

Returns the `producto` block, an `existencias` breakdown per warehouse,
`total_disponible` and the boolean `hay_stock`.
Business error if the SKU does not exist.

### `crear_pedido`

Registers a new order. **The only tool that writes.**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `cliente_id` | integer | **yes** | Customer id (1–20 in the seed data). |
| `lineas` | array | **yes** | One or more `{ "sku": string, "cantidad": integer }` objects. |
| `bodega_id` | integer | no | Fulfilling warehouse. Default `1`. |

Validates that the customer, warehouse and every SKU exist and that stock is sufficient;
then deducts inventory and returns `numero`, `estado`, `fecha`, `cliente`, `bodega`,
`lineas` and `total`.

The operation is **atomic**: validation of every line happens before any write, and the
transaction is rolled back on failure — a rejected order never leaves a partial record or
a partial stock deduction.

### `consultar_pedido`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `numero` | string | **yes** | Order number, format `PED-YYYY-NNNN`. |

Returns the order header, customer, warehouse, line detail and total.

### `historial_cliente`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `cliente_id` | integer | **yes** | Customer id. |
| `limite` | integer | no | Orders to list, 1–50. Default `5`. |

Returns the customer block, the recent orders, plus `pedidos_totales` and
`monto_acumulado` across the customer's whole history.

### `convertir_total_a_dolares`

Converts an order total from GTQ to USD using the reference rate published by the Bank of
Guatemala. **This is the only tool that consumes an external data source.**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `numero` | string | **yes** | Order number to convert. |

Returns `total_gtq`, `total_usd`, `tipo_cambio`, `fuente_tipo_cambio` and the boolean
`consulta_en_linea`. If the web service times out or is unreachable, a local fallback rate
is used and `consulta_en_linea` is set to `false` so the chatbot can warn the user instead
of presenting a stale figure as current.

---

## Protocol specification

**Transport:** stdio, newline-delimited — one JSON-RPC message per line.
**Protocol versions supported:** `2025-11-25`, `2025-06-18`, `2025-03-26`, `2024-11-05`
(negotiated during `initialize`; the server falls back to its newest version if the client
requests an unknown one).

| Method | Type | Description |
|---|---|---|
| `initialize` | request | Handshake and capability exchange. |
| `notifications/initialized` | notification | Client signals it is ready. No response is sent. |
| `tools/list` | request | Returns the tool catalog with each JSON Schema. |
| `tools/call` | request | Invokes a tool by name with an `arguments` object. |
| `ping` | request | Liveness check; returns an empty result. |
| `shutdown` | request | Convenience extension; replies and then closes the loop. |

### Error handling

The implementation distinguishes the two levels the specification requires:

**Protocol errors** — returned as a JSON-RPC `error` object:

| Code | Condition |
|---|---|
| `-32700` | Malformed JSON on the input line. |
| `-32600` | Missing or invalid `jsonrpc` / `method` field. |
| `-32601` | Unsupported method (e.g. `resources/list`). |
| `-32602` | Invalid parameters, or unknown tool name in `tools/call`. |
| `-32603` | Unhandled internal error. |

**Business errors** — returned as a *successful* JSON-RPC response whose result carries
`isError: true` and a human-readable message. Out-of-stock, unknown SKU or unknown
customer fall here: the call itself was well-formed, so the model should read the message
and react (suggest another warehouse, ask for a valid code) rather than treat it as a
transport failure.

---

## Usage examples

**Handshake**

```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"chatbot","version":"1.0.0"}}}
← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"distribuidora-inventario","version":"1.0.0"}}}
```

**Checking stock**

```json
→ {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"consultar_stock","arguments":{"sku":"AB-0009"}}}
← {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{ \"producto\": {...}, \"total_disponible\": 434, \"hay_stock\": true }"}],"isError":false}}
```

**Placing an order**

```json
→ {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"crear_pedido","arguments":{"cliente_id":3,"bodega_id":1,"lineas":[{"sku":"AB-0009","cantidad":10}]}}}
← {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{ \"numero\": \"PED-2026-0041\", \"total\": 1272.60 }"}],"isError":false}}
```

**Business error — insufficient stock**

```json
→ {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"crear_pedido","arguments":{"cliente_id":3,"lineas":[{"sku":"AB-0009","cantidad":999999}]}}}
← {"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"Error: Stock insuficiente ... se solicitaron 999999 y hay 308."}],"isError":true}}
```

A full annotated transcript, plus sample natural-language conversations that exercise
several tools in sequence, is in [`examples/ejemplos_de_uso.md`](examples/ejemplos_de_uso.md).

---

## Data sources

The server works on a **local SQLite database** with six tables — `productos`, `bodegas`,
`existencias`, `clientes`, `pedidos`, `pedido_lineas` — defined in `db/schema.sql`.

Since no real company data is available, the database is populated with a **synthetic
dataset** generated by `db/seed.py`: roughly 100 products across 8 categories with
plausible prices, 3 warehouses, 20 retail customers and 40 historical orders with their
line detail. The generator uses a fixed seed (`3067`), so the dataset is byte-identical on
every machine and the demo scenario is fully reproducible. Some SKUs are intentionally
seeded with zero stock in the secondary warehouses so the out-of-stock path can be
demonstrated.

The single **external source** is the Bank of Guatemala exchange-rate web service
(`TipoCambioDia`), consumed over SOAP/HTTP by `convertir_total_a_dolares`.

---

## Project structure

```
mcp-inventario/
├── chatbot.py                # HOST: context, MCP log, terminal UI
├── llm_providers.py          # LLM providers (Gemini, Anthropic) — swappable
├── mcp_client.py             # CLIENT: manual JSON-RPC, stdio + HTTP transports
├── server.py                 # SERVER (local): protocol layer over stdio
├── server_http.py            # SERVER (remote): same protocol over HTTP
├── tools.py                  # Tool definitions and business logic (shared)
├── test_client.py            # Protocol walkthrough / smoke test
├── test_llm.py               # Provider translation test (no API calls)
├── servers_config.json       # Which MCP servers the host connects to
├── Dockerfile                # Image for the remote deployment
├── .dockerignore
├── .gitignore
├── README.md
│
├── db/
│   ├── schema.sql            # Relational schema
│   ├── seed.py               # Synthetic data generator (fixed seed)
│   └── distribuidora.db      # Generated — not tracked
│
├── docs/
│   ├── despliegue_cloud_run.md   # Deployment guide (part 6)
│   └── analisis_wireshark.md     # Capture and layer analysis guide (parts 7 and 9)
│
├── examples/
│   ├── ejemplos_de_uso.md    # Annotated transcript and scenarios
│   └── claude_desktop_config.json
│
├── sandbox/                  # Working area for the Filesystem and Git servers
├── capturas/                 # Wireshark .pcapng files
└── logs/                     # Generated — not tracked
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No se encontro la base de datos` | The database has not been generated. Run `python3 db/seed.py`. |
| The server appears to hang with no output | Expected. It is waiting for JSON-RPC messages on stdin. Pipe a message in, or launch it from a host. |
| Claude Desktop does not list the tools | The `args` path must be **absolute**, and `python3` must be on the system PATH. On Windows use `python` and escape backslashes. Restart the app after editing the config. |
| `consulta_en_linea: false` in the conversion tool | The Bank of Guatemala service was unreachable (no internet, timeout or a firewall). The fallback rate was used; the returned figure is not the live rate. |
| Stock keeps dropping between demos | `crear_pedido` really deducts inventory. Run `python3 db/seed.py` to reset the dataset. |
| `credit balance is too low` | The Anthropic account has no credit. Switch provider: `--provider gemini`. |
| `400` from Gemini mentioning an unknown schema field | Gemini accepts a subset of JSON Schema. `limpiar_esquema()` in `llm_providers.py` strips the unsupported keywords; add any new one to `CLAVES_NO_SOPORTADAS`. |
| `404` model not found | The model identifier changed. Pass a current one with `--model`. |
| `JSON invalido` (`-32700`) in the log | A line arrived that was not valid JSON, or a message was split across lines. Each message must occupy exactly one line. |

# Open-source reuse decision — T00/T01 preparation

Principle: reuse external data/MCP capabilities as dependencies or adapters; keep customer-specific business logic in this repository.

| Task | Need | Reuse candidate | Decision |
|---|---|---|---|
| T00 | Golden Case regression fixture | pytest patterns / standard SHA256 manifest | Implement locally; no framework needed |
| T01 | Pause/resume + human checkpoint | LangGraph `interrupt()` + FastAPI HITL examples | Reuse pattern, do not copy full demo app |
| T02 | SellerSprite data | `JieXi-11/SellerSprite` | Use directly as MCP dependency; do not reimplement web auth |
| T03 | BSR raw pipeline | SellerSprite MCP operations + customer rules | MCP for collection; local deterministic code for dedupe/Top100 |
| T04 | ASIN enrichment | SellerSprite MCP ASIN/product operations | Reuse MCP |
| T05 | Product classification + review | LangGraph HITL pattern | Implement customer classification schema locally |
| T06 | Market metrics | No sufficiently matching stable OSS implementation found | Implement small deterministic Python/Polars module |
| T07 | Research narrative | `liangdabiao/amazon-sorftime-research-MCP-skill` | Reuse prompt/skill ideas selectively; do not reuse auto Yes/No logic |
| T08 | Competition metrics | SellerSprite market/concentration operations can cross-check | Calculate locally from normalized dataset; MCP is source/check |
| T09 | New-product success + VC | No direct OSS fit found | Implement customer-specific deterministic rules locally |
| T10 | Research routing | LangGraph conditional edges + HITL | Reuse framework primitive only |
| T11 | Research dossier | Existing Amazon research repos provide report layouts | Reuse report structure ideas; output from our normalized data |

## Sources checked

- SellerSprite MCP — https://github.com/JieXi-11/SellerSprite — MIT
- Apify MCP Server — https://github.com/apify/apify-mcp-server — MIT
- SIF research reference — https://github.com/liangdabiao/sif-amazon-research — MIT
- SIF MCP tool catalog — https://github.com/liangdabiao/sif-amazon-research/blob/main/docs/sif-mcp-tool-list.md
- Amazon/Sorftime/SIF/西柚/SellerSprite skill bundle — https://github.com/liangdabiao/amazon-sorftime-research-MCP-skill — MIT
- LangGraph HITL FastAPI demo — https://github.com/esurovtsev/langgraph-hitl-fastapi-demo
- Official MCP Python SDK — https://github.com/modelcontextprotocol/python-sdk

## MCP choices

- SellerSprite: local stdio `sellersprite-mcp` by default. This preserves browser/session state locally.
- SIF: remote Streamable HTTP MCP; token injected through environment.
- 西柚洞察: remote HTTP MCP; token injected through environment.
- Apify: official Apify MCP; token injected through environment.

## Security

Never copy credentials from public repositories. Public GitHub examples were found containing live-looking tokens. They are treated as compromised examples, not reusable secrets.

# Batch Operations Design

## Problem

MCP-driven ingestion workflows (e.g., email indexing) require hundreds of individual create calls. Each call consumes agent context and round-trip time. A 74-email ingestion required 526 MCP calls across 9 threads, with relation creation being the biggest multiplier.

## Scope

Batch **create** operations for entities and relations. Same-type per call (one entity or relation type per batch). No batch update or delete — those are rare during ingestion.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Operations | Create only | Ingestion workload; update/delete are rare |
| Type per call | Same-type | Simpler validation, single UNWIND query |
| Error handling | Validate-all-then-commit | Actionable errors without partial state |
| API surfaces | REST + MCP | Project keeps both in sync |
| Batch limit | 100 items | Covers real workloads, fits MCP context |
| Architecture | Dedicated endpoints/tools | Clean separation from single-item API |

## API Surface

### REST Endpoints

**Batch create entities:**
`POST /api/runtime/{ontologyKey}/entities/{entityTypeKey}/batch`

Request:
```json
{"items": [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]}
```

Response (201):
```json
{"created": [{ ...entity }, { ...entity }], "count": 2}
```

**Batch create relations:**
`POST /api/runtime/{ontologyKey}/relations/{relationTypeKey}/batch`

Request:
```json
{"items": [{"fromEntityId": "...", "toEntityId": "...", "role": "engineer"}]}
```

Response (201):
```json
{"created": [{ ...relation }], "count": 1}
```

### MCP Tools

- `batch_create_entities(entity_type_key: str, items: list[dict])` — each item is a property dict
- `batch_create_relations(relation_type_key: str, items: list[dict])` — each item has `fromEntityId`, `toEntityId`, plus optional properties

### Error Responses (422)

Validation failure (entire batch rejected):
```json
{
  "error": "Batch validation failed",
  "details": {
    "items": {
      "0": {"fields": {"name": "Required property missing"}},
      "3": {"fields": {"age": "Expected integer"}}
    }
  }
}
```

Batch size exceeded:
```json
{"error": "Batch size exceeds limit of 100 items"}
```

Entity not found (batch relations):
```json
{
  "error": "Batch validation failed",
  "details": {
    "items": {
      "2": {"fromEntityId": "Entity not found: abc-123"}
    }
  }
}
```

## Service Layer

Both batch functions reuse existing validation logic unchanged:

**Batch create entities:**
1. Load schema cache (once)
2. Validate item count (1–100)
3. Loop: call `validate_properties()` per item, collect errors by index
4. If any errors, raise `ValidationError` with all item errors
5. Generate UUIDs, build text representations, compute embeddings
6. Single batch repository call
7. Return created entities

**Batch create relations:**
1. Load schema cache (once)
2. Validate item count (1–100)
3. Loop: validate properties per item, collect errors by index
4. Collect all unique entity IDs, batch-verify existence and type match (single query)
5. If any errors, reject entire batch
6. Generate UUIDs, single batch repository call
7. Return created relations

## Repository Layer

**Batch create entities** — single UNWIND Cypher query creating all nodes at once.

**Batch create relations** — single UNWIND Cypher query matching source/target nodes and creating all relationships.

**Batch entity existence check** — single query with `WHERE _id IN [...]` replacing N individual lookups.

## Code Reuse

Existing functions used unchanged: `validate_properties()`, `coerce_value()`, `build_text_repr()`, `_load_schema()`, `ValidationError`, `_enrich_errors`.

New code: batch service functions (loop + validate + delegate), batch repository functions (UNWIND queries), REST endpoints, MCP tools.

## Artifacts

| Artifact | Action |
|----------|--------|
| `runtime/service.py` | Add `batch_create_entities()`, `batch_create_relations()` |
| `runtime/repository.py` | Add batch create queries + batch existence check |
| `runtime/router.py` | Add 2 REST endpoints |
| `runtime/schemas.py` | Add batch request/response Pydantic models |
| `mcp/runtime.py` | Add 2 MCP tools |
| `tests/runtime/test_batch_entities.py` | New test file |
| `tests/runtime/test_batch_relations.py` | New test file |
| `docs/api-contracts/runtime-api.md` | Add batch section + update summary |
| `docs/runtime-usage.md` | Add batch curl examples |
| `docs/architecture.md` | Update endpoint summary |
| `docs/mcp-architecture.md` | Add batch tools to catalog |
| `frontend/src/api/runtimeClient.ts` | Add batch functions |
| `frontend/src/types/runtime.ts` | Add batch types |

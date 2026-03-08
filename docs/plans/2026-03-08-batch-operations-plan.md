# Batch Operations Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add batch create endpoints for entities and relations across REST and MCP, with validate-all-then-commit semantics and a 100-item limit.

**Architecture:** Dedicated `/batch` endpoints and MCP tools that reuse existing validation (`validate_properties`, `coerce_value`, `build_text_repr`) and add new UNWIND-based repository functions. The service layer validates all items, then commits in a single Neo4j transaction.

**Tech Stack:** Python, FastAPI, Pydantic, Neo4j (Cypher UNWIND), FastMCP, TypeScript

---

### Task 1: Pydantic Batch Schemas

**Files:**
- Modify: `backend/src/ontoforge_server/runtime/schemas.py`

**Step 1: Write the batch request/response models**

Add these models after the existing `RelationInstanceCreate` class:

```python
BATCH_MAX_ITEMS = 100


class BatchCreateResponse(BaseModel):
    created: list[dict]
    count: int
```

**Step 2: Run existing tests to verify no breakage**

Run: `cd backend && uv run pytest tests/runtime/ -v`
Expected: All existing tests PASS

**Step 3: Commit**

```bash
git add backend/src/ontoforge_server/runtime/schemas.py
git commit -m "Add batch create response schema"
```

---

### Task 2: Batch Repository Functions

**Files:**
- Modify: `backend/src/ontoforge_server/runtime/repository.py`
- Test: `backend/tests/runtime/test_batch_entities.py`

**Step 1: Write the failing test for batch_create_entities**

Create `backend/tests/runtime/test_batch_entities.py`:

```python
"""Tests for batch entity creation endpoint."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.runtime.conftest import ONTOLOGY_KEY


NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
PREFIX = f"/api/runtime/{ONTOLOGY_KEY}"

PERSON_ENTITY_1 = {
    "_id": "ent-1",
    "_entityTypeKey": "person",
    "_createdAt": NOW,
    "_updatedAt": NOW,
    "name": "Alice",
    "age": 30,
}

PERSON_ENTITY_2 = {
    "_id": "ent-2",
    "_entityTypeKey": "person",
    "_createdAt": NOW,
    "_updatedAt": NOW,
    "name": "Bob",
    "age": 25,
}


def _mock_repo(**overrides):
    defaults = {
        "batch_create_entities": AsyncMock(
            return_value=[PERSON_ENTITY_1, PERSON_ENTITY_2]
        ),
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def repo_patch():
    def _patch(**overrides):
        mocks = _mock_repo(**overrides)
        return patch.multiple(
            "ontoforge_server.runtime.service.repository", **mocks
        )
    return _patch


# --- Batch Create ---


async def test_batch_create_entities_valid(client, repo_patch):
    """POST /entities/{type_key}/batch with valid items returns 201."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/entities/person/batch",
            json={"items": [
                {"name": "Alice", "age": 30},
                {"name": "Bob", "age": 25},
            ]},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["count"] == 2
    assert len(data["created"]) == 2
    assert data["created"][0]["name"] == "Alice"
    assert data["created"][1]["name"] == "Bob"


async def test_batch_create_entities_empty_items_returns_422(client, repo_patch):
    """POST /entities/{type_key}/batch with empty items returns 422."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/entities/person/batch",
            json={"items": []},
        )
    assert resp.status_code == 422


async def test_batch_create_entities_exceeds_limit_returns_422(client, repo_patch):
    """POST /entities/{type_key}/batch with >100 items returns 422."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/entities/person/batch",
            json={"items": [{"name": f"Person {i}"} for i in range(101)]},
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "100" in data["error"]["message"]


async def test_batch_create_entities_validation_error_rejects_all(client, repo_patch):
    """POST /entities/{type_key}/batch rejects entire batch if any item invalid."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/entities/person/batch",
            json={"items": [
                {"name": "Alice"},
                {"age": "not-a-number"},  # missing required 'name' + bad type
            ]},
        )
    assert resp.status_code == 422
    data = resp.json()
    # Item 1 should have errors
    assert "1" in data["error"]["details"]["items"]


async def test_batch_create_entities_unknown_prop_rejects_all(client, repo_patch):
    """POST /entities/{type_key}/batch rejects batch if any item has unknown prop."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/entities/person/batch",
            json={"items": [
                {"name": "Alice"},
                {"name": "Bob", "nonexistent": "bad"},
            ]},
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "1" in data["error"]["details"]["items"]
    assert "nonexistent" in data["error"]["details"]["items"]["1"]["fields"]


async def test_batch_create_entities_nonexistent_type(client, repo_patch):
    """POST /entities/{type_key}/batch with unknown entity type returns 404."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/entities/nonexistent/batch",
            json={"items": [{"name": "Alice"}]},
        )
    assert resp.status_code == 404


async def test_batch_create_entities_single_item(client, repo_patch):
    """POST /entities/{type_key}/batch works with a single item."""
    with repo_patch(
        batch_create_entities=AsyncMock(return_value=[PERSON_ENTITY_1]),
    ):
        resp = await client.post(
            f"{PREFIX}/entities/person/batch",
            json={"items": [{"name": "Alice"}]},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["count"] == 1
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/runtime/test_batch_entities.py -v`
Expected: FAIL — endpoint does not exist yet

**Step 3: Write `batch_create_entities` in repository**

Add to `backend/src/ontoforge_server/runtime/repository.py`, after the `delete_entity` function (before the `# --- Relation Instance CRUD ---` section):

```python
async def batch_create_entities(
    session: AsyncSession,
    entity_type_key: str,
    pascal_label: str,
    items: list[dict],
) -> list[dict]:
    """Create multiple entity instances in a single UNWIND query.

    Each item in `items` must have keys: id, properties, embedding (or None).
    """
    result = await session.run(
        f"""
        UNWIND $items AS item
        CREATE (n:_Entity:{pascal_label} {{
            _id: item.id,
            _entityTypeKey: $entity_type_key,
            _createdAt: datetime(),
            _updatedAt: datetime()
        }})
        SET n += item.properties
        FOREACH (_ IN CASE WHEN item.embedding IS NOT NULL THEN [1] ELSE [] END |
            SET n._embedding = item.embedding
        )
        RETURN n {{.*}} AS entity
        """,
        entity_type_key=entity_type_key,
        items=items,
    )
    records = [record async for record in result]
    return [_strip_embedding(_convert_neo4j_types(r["entity"])) for r in records]
```

**Step 4: Commit**

```bash
git add backend/src/ontoforge_server/runtime/repository.py backend/tests/runtime/test_batch_entities.py
git commit -m "Add batch entity repository function and tests"
```

---

### Task 3: Batch Entity Service Function

**Files:**
- Modify: `backend/src/ontoforge_server/runtime/service.py`

**Step 1: Write `batch_create_entities` in service**

Add after the `delete_entity` function (before `# --- Service Functions — Relation Instance CRUD ---`):

```python
async def batch_create_entities(
    ontology_key: str,
    entity_type_key: str,
    items: list[dict],
    driver: AsyncDriver,
) -> list[dict]:
    """Create multiple entity instances of the same type in a single batch.

    Validates all items first. If any fail, the entire batch is rejected.
    """
    from ontoforge_server.runtime.schemas import BATCH_MAX_ITEMS

    cache = await _load_schema(ontology_key, driver)
    et_def = cache.entity_types.get(entity_type_key)
    if not et_def:
        raise NotFoundError(f"Entity type '{entity_type_key}' not found")

    if not items:
        raise ValidationError("Batch must contain at least 1 item")
    if len(items) > BATCH_MAX_ITEMS:
        raise ValidationError(f"Batch size exceeds limit of {BATCH_MAX_ITEMS} items")

    # Validate all items, collecting errors by index
    all_coerced: list[dict] = []
    item_errors: dict[str, dict] = {}

    for i, item in enumerate(items):
        coerced, errors = validate_properties(item, et_def.properties, entity_type_key)
        if errors:
            item_errors[str(i)] = {"fields": errors}
        all_coerced.append(coerced)

    if item_errors:
        raise ValidationError(
            "Batch validation failed",
            details={"items": item_errors},
        )

    pascal_label = to_pascal_case(entity_type_key)
    provider = get_embedding_provider()

    # Build repository items: id, properties, embedding
    repo_items = []
    for coerced in all_coerced:
        entity_id = str(uuid4())
        embedding = None
        if provider:
            text = build_text_repr(entity_type_key, coerced, et_def.properties)
            embedding = await provider.embed(text)
        repo_items.append({
            "id": entity_id,
            "properties": coerced,
            "embedding": embedding,
        })

    async with driver.session() as session:
        entities = await repository.batch_create_entities(
            session, entity_type_key, pascal_label, repo_items,
        )

    return entities
```

**Step 2: Run test to verify progress**

Run: `cd backend && uv run pytest tests/runtime/test_batch_entities.py -v`
Expected: Still FAIL — REST endpoint not created yet

**Step 3: Commit**

```bash
git add backend/src/ontoforge_server/runtime/service.py
git commit -m "Add batch entity creation service function"
```

---

### Task 4: Batch Entity REST Endpoint

**Files:**
- Modify: `backend/src/ontoforge_server/runtime/router.py`

**Step 1: Add the batch entity endpoint**

Add after the single-entity `create_entity` endpoint (after line 98), before the `list_entities` endpoint:

```python
@router.post("/entities/{entity_type_key}/batch", status_code=201)
async def batch_create_entities(
    ontology_key: str,
    entity_type_key: str,
    request: Request,
    driver: AsyncDriver = Depends(get_driver),
):
    body = await request.json()
    items = body.get("items", [])
    entities = await service.batch_create_entities(
        ontology_key, entity_type_key, items, driver
    )
    return {"created": entities, "count": len(entities)}
```

**Step 2: Run batch entity tests**

Run: `cd backend && uv run pytest tests/runtime/test_batch_entities.py -v`
Expected: All PASS

**Step 3: Run all existing tests to verify no breakage**

Run: `cd backend && uv run pytest tests/runtime/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add backend/src/ontoforge_server/runtime/router.py
git commit -m "Add batch entity creation REST endpoint"
```

---

### Task 5: Batch Relation Repository Functions

**Files:**
- Modify: `backend/src/ontoforge_server/runtime/repository.py`
- Test: `backend/tests/runtime/test_batch_relations.py`

**Step 1: Write the failing tests for batch relation creation**

Create `backend/tests/runtime/test_batch_relations.py`:

```python
"""Tests for batch relation creation endpoint."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tests.runtime.conftest import ONTOLOGY_KEY


NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
PREFIX = f"/api/runtime/{ONTOLOGY_KEY}"

PERSON_ENTITY = {
    "_id": "ent-person-1",
    "_entityTypeKey": "person",
    "_createdAt": NOW,
    "_updatedAt": NOW,
    "name": "Alice",
}

COMPANY_ENTITY = {
    "_id": "ent-company-1",
    "_entityTypeKey": "company",
    "_createdAt": NOW,
    "_updatedAt": NOW,
    "name": "Acme Corp",
}

RELATION_1 = {
    "_id": "rel-1",
    "_relationTypeKey": "works_for",
    "_createdAt": NOW,
    "_updatedAt": NOW,
    "fromEntityId": "ent-person-1",
    "toEntityId": "ent-company-1",
    "role": "Engineer",
}

RELATION_2 = {
    "_id": "rel-2",
    "_relationTypeKey": "works_for",
    "_createdAt": NOW,
    "_updatedAt": NOW,
    "fromEntityId": "ent-person-2",
    "toEntityId": "ent-company-1",
    "role": "Designer",
}


def _mock_repo(**overrides):
    defaults = {
        "batch_get_entities_by_ids": AsyncMock(return_value=[
            {"_id": "ent-person-1", "_entityTypeKey": "person"},
            {"_id": "ent-company-1", "_entityTypeKey": "company"},
        ]),
        "batch_create_relations": AsyncMock(
            return_value=[RELATION_1, RELATION_2]
        ),
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def repo_patch():
    def _patch(**overrides):
        mocks = _mock_repo(**overrides)
        return patch.multiple(
            "ontoforge_server.runtime.service.repository", **mocks
        )
    return _patch


# --- Batch Create ---


async def test_batch_create_relations_valid(client, repo_patch):
    """POST /relations/{type_key}/batch with valid items returns 201."""
    with repo_patch(
        batch_get_entities_by_ids=AsyncMock(return_value=[
            {"_id": "ent-person-1", "_entityTypeKey": "person"},
            {"_id": "ent-person-2", "_entityTypeKey": "person"},
            {"_id": "ent-company-1", "_entityTypeKey": "company"},
        ]),
    ):
        resp = await client.post(
            f"{PREFIX}/relations/works_for/batch",
            json={"items": [
                {
                    "fromEntityId": "ent-person-1",
                    "toEntityId": "ent-company-1",
                    "role": "Engineer",
                },
                {
                    "fromEntityId": "ent-person-2",
                    "toEntityId": "ent-company-1",
                    "role": "Designer",
                },
            ]},
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["count"] == 2
    assert len(data["created"]) == 2


async def test_batch_create_relations_empty_items_returns_422(client, repo_patch):
    """POST /relations/{type_key}/batch with empty items returns 422."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/relations/works_for/batch",
            json={"items": []},
        )
    assert resp.status_code == 422


async def test_batch_create_relations_exceeds_limit_returns_422(client, repo_patch):
    """POST /relations/{type_key}/batch with >100 items returns 422."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/relations/works_for/batch",
            json={"items": [
                {"fromEntityId": f"p-{i}", "toEntityId": f"c-{i}"}
                for i in range(101)
            ]},
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "100" in data["error"]["message"]


async def test_batch_create_relations_missing_from_entity(client, repo_patch):
    """POST /relations/{type_key}/batch rejects batch if fromEntityId missing."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/relations/works_for/batch",
            json={"items": [
                {"toEntityId": "ent-company-1"},
            ]},
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "0" in data["error"]["details"]["items"]


async def test_batch_create_relations_entity_not_found(client, repo_patch):
    """POST /relations/{type_key}/batch rejects batch if referenced entity not found."""
    with repo_patch(
        batch_get_entities_by_ids=AsyncMock(return_value=[
            {"_id": "ent-company-1", "_entityTypeKey": "company"},
        ]),
    ):
        resp = await client.post(
            f"{PREFIX}/relations/works_for/batch",
            json={"items": [
                {
                    "fromEntityId": "nonexistent",
                    "toEntityId": "ent-company-1",
                },
            ]},
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "0" in data["error"]["details"]["items"]
    assert "fromEntityId" in data["error"]["details"]["items"]["0"]


async def test_batch_create_relations_entity_type_mismatch(client, repo_patch):
    """POST /relations/{type_key}/batch rejects batch if entity type mismatches."""
    # works_for expects person -> company, provide company -> company
    with repo_patch(
        batch_get_entities_by_ids=AsyncMock(return_value=[
            {"_id": "ent-company-1", "_entityTypeKey": "company"},
            {"_id": "ent-company-2", "_entityTypeKey": "company"},
        ]),
    ):
        resp = await client.post(
            f"{PREFIX}/relations/works_for/batch",
            json={"items": [
                {
                    "fromEntityId": "ent-company-1",
                    "toEntityId": "ent-company-2",
                },
            ]},
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "0" in data["error"]["details"]["items"]
    assert "fromEntityId" in data["error"]["details"]["items"]["0"]


async def test_batch_create_relations_nonexistent_type(client, repo_patch):
    """POST /relations/{type_key}/batch with unknown relation type returns 404."""
    with repo_patch():
        resp = await client.post(
            f"{PREFIX}/relations/nonexistent/batch",
            json={"items": [
                {"fromEntityId": "e1", "toEntityId": "e2"},
            ]},
        )
    assert resp.status_code == 404


async def test_batch_create_relations_property_validation(client, repo_patch):
    """POST /relations/{type_key}/batch rejects batch if property validation fails."""
    with repo_patch(
        batch_get_entities_by_ids=AsyncMock(return_value=[
            {"_id": "ent-person-1", "_entityTypeKey": "person"},
            {"_id": "ent-company-1", "_entityTypeKey": "company"},
        ]),
    ):
        resp = await client.post(
            f"{PREFIX}/relations/works_for/batch",
            json={"items": [
                {
                    "fromEntityId": "ent-person-1",
                    "toEntityId": "ent-company-1",
                    "unknown_prop": "bad",
                },
            ]},
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "0" in data["error"]["details"]["items"]
```

**Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/runtime/test_batch_relations.py -v`
Expected: FAIL — repository functions don't exist

**Step 3: Write `batch_get_entities_by_ids` and `batch_create_relations` in repository**

Add `batch_get_entities_by_ids` after `get_entity_by_id` in repository.py:

```python
async def batch_get_entities_by_ids(
    session: AsyncSession,
    entity_ids: list[str],
) -> list[dict]:
    """Get multiple entities by ID in a single query. Returns list of dicts with _id and _entityTypeKey."""
    result = await session.run(
        "MATCH (n:_Entity) WHERE n._id IN $ids RETURN n._id AS _id, n._entityTypeKey AS _entityTypeKey",
        ids=entity_ids,
    )
    return [{"_id": record["_id"], "_entityTypeKey": record["_entityTypeKey"]} async for record in result]
```

Add `batch_create_relations` after `create_relation` in repository.py:

```python
async def batch_create_relations(
    session: AsyncSession,
    relation_type_key: str,
    rel_type_upper: str,
    items: list[dict],
) -> list[dict]:
    """Create multiple relation instances in a single UNWIND query.

    Each item in `items` must have keys: id, fromEntityId, toEntityId, properties.
    """
    result = await session.run(
        f"""
        UNWIND $items AS item
        MATCH (from:_Entity {{_id: item.fromEntityId}})
        MATCH (to:_Entity {{_id: item.toEntityId}})
        CREATE (from)-[r:{rel_type_upper} {{
            _id: item.id,
            _relationTypeKey: $relation_type_key,
            _createdAt: datetime(),
            _updatedAt: datetime()
        }}]->(to)
        SET r += item.properties
        RETURN r {{.*}} AS relation,
               from._id AS fromEntityId,
               to._id AS toEntityId
        """,
        relation_type_key=relation_type_key,
        items=items,
    )
    results = []
    async for record in result:
        rel = _convert_neo4j_types(record["relation"])
        rel["fromEntityId"] = record["fromEntityId"]
        rel["toEntityId"] = record["toEntityId"]
        results.append(rel)
    return results
```

**Step 4: Commit**

```bash
git add backend/src/ontoforge_server/runtime/repository.py backend/tests/runtime/test_batch_relations.py
git commit -m "Add batch relation repository functions and tests"
```

---

### Task 6: Batch Relation Service Function

**Files:**
- Modify: `backend/src/ontoforge_server/runtime/service.py`

**Step 1: Write `batch_create_relations` in service**

Add after `batch_create_entities` in service.py:

```python
async def batch_create_relations(
    ontology_key: str,
    relation_type_key: str,
    items: list[dict],
    driver: AsyncDriver,
) -> list[dict]:
    """Create multiple relation instances of the same type in a single batch.

    Validates all items first. If any fail, the entire batch is rejected.
    """
    from ontoforge_server.runtime.schemas import BATCH_MAX_ITEMS

    cache = await _load_schema(ontology_key, driver)
    rt_def = cache.relation_types.get(relation_type_key)
    if not rt_def:
        raise NotFoundError(f"Relation type '{relation_type_key}' not found")

    if not items:
        raise ValidationError("Batch must contain at least 1 item")
    if len(items) > BATCH_MAX_ITEMS:
        raise ValidationError(f"Batch size exceeds limit of {BATCH_MAX_ITEMS} items")

    # Phase 1: Validate structure and properties per item
    item_errors: dict[str, dict] = {}
    all_entity_ids: set[str] = set()
    parsed_items: list[dict] = []

    for i, item in enumerate(items):
        errors: dict[str, str] = {}
        from_id = item.get("fromEntityId")
        to_id = item.get("toEntityId")

        if not from_id:
            errors["fromEntityId"] = "fromEntityId is required"
        if not to_id:
            errors["toEntityId"] = "toEntityId is required"

        # Extract user properties (everything except fromEntityId/toEntityId)
        user_props = {k: v for k, v in item.items() if k not in ("fromEntityId", "toEntityId")}
        coerced, prop_errors = validate_properties(user_props, rt_def.properties, relation_type_key)
        errors.update({"fields": prop_errors} if prop_errors else {})

        if errors:
            # Flatten: if only "fields" key, use it directly; otherwise wrap
            if "fields" in errors and len(errors) == 1:
                item_errors[str(i)] = errors
            else:
                # Mix of field errors and structural errors
                field_errs = errors.pop("fields", {})
                field_errs.update({k: v for k, v in errors.items()})
                item_errors[str(i)] = {"fields": field_errs} if field_errs else errors

        if from_id:
            all_entity_ids.add(from_id)
        if to_id:
            all_entity_ids.add(to_id)

        parsed_items.append({
            "from_id": from_id,
            "to_id": to_id,
            "coerced": coerced,
        })

    # Phase 2: Batch-verify all referenced entities exist and have correct types
    if all_entity_ids and not item_errors:
        async with driver.session() as session:
            found_entities = await repository.batch_get_entities_by_ids(
                session, list(all_entity_ids),
            )
        entity_map = {e["_id"]: e["_entityTypeKey"] for e in found_entities}

        for i, parsed in enumerate(parsed_items):
            errors = {}
            from_id = parsed["from_id"]
            to_id = parsed["to_id"]

            if from_id and from_id not in entity_map:
                errors["fromEntityId"] = f"Source entity '{from_id}' not found"
            elif from_id and entity_map.get(from_id) != rt_def.from_entity_type_key:
                errors["fromEntityId"] = (
                    f"Source entity type mismatch: expected '{rt_def.from_entity_type_key}', "
                    f"got '{entity_map[from_id]}'"
                )

            if to_id and to_id not in entity_map:
                errors["toEntityId"] = f"Target entity '{to_id}' not found"
            elif to_id and entity_map.get(to_id) != rt_def.to_entity_type_key:
                errors["toEntityId"] = (
                    f"Target entity type mismatch: expected '{rt_def.to_entity_type_key}', "
                    f"got '{entity_map[to_id]}'"
                )

            if errors:
                item_errors[str(i)] = errors

    if item_errors:
        raise ValidationError(
            "Batch validation failed",
            details={"items": item_errors},
        )

    # Phase 3: Build repo items and create
    rel_type_upper = to_upper_snake_case(relation_type_key)
    repo_items = []
    for parsed in parsed_items:
        repo_items.append({
            "id": str(uuid4()),
            "fromEntityId": parsed["from_id"],
            "toEntityId": parsed["to_id"],
            "properties": parsed["coerced"],
        })

    async with driver.session() as session:
        relations = await repository.batch_create_relations(
            session, relation_type_key, rel_type_upper, repo_items,
        )

    return relations
```

**Step 2: Run test to verify progress**

Run: `cd backend && uv run pytest tests/runtime/test_batch_relations.py -v`
Expected: Still FAIL — REST endpoint not created yet

**Step 3: Commit**

```bash
git add backend/src/ontoforge_server/runtime/service.py
git commit -m "Add batch relation creation service function"
```

---

### Task 7: Batch Relation REST Endpoint

**Files:**
- Modify: `backend/src/ontoforge_server/runtime/router.py`

**Step 1: Add the batch relation endpoint**

Add after the single-relation `create_relation` endpoint (after line 192), before `list_relations`:

```python
@router.post("/relations/{relation_type_key}/batch", status_code=201)
async def batch_create_relations(
    ontology_key: str,
    relation_type_key: str,
    request: Request,
    driver: AsyncDriver = Depends(get_driver),
):
    body = await request.json()
    items = body.get("items", [])
    relations = await service.batch_create_relations(
        ontology_key, relation_type_key, items, driver
    )
    return {"created": relations, "count": len(relations)}
```

**Step 2: Run batch relation tests**

Run: `cd backend && uv run pytest tests/runtime/test_batch_relations.py -v`
Expected: All PASS

**Step 3: Run all tests**

Run: `cd backend && uv run pytest tests/runtime/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add backend/src/ontoforge_server/runtime/router.py
git commit -m "Add batch relation creation REST endpoint"
```

---

### Task 8: MCP Batch Tools

**Files:**
- Modify: `backend/src/ontoforge_server/mcp/runtime.py`

**Step 1: Add `batch_create_entities` MCP tool**

Add after the `create_entity` tool:

```python
@runtime_mcp.tool()
@_enrich_errors
async def batch_create_entities(
    entity_type_key: str,
    items: list[dict],
) -> dict:
    """Create multiple entities of the same type in a single batch (max 100).
    Each item is a property dict matching the schema. All items are validated
    first — if any fail, the entire batch is rejected with per-item errors."""
    ontology_key = _get_ontology_key()
    driver = await get_driver()
    entities = await service.batch_create_entities(
        ontology_key, entity_type_key, items, driver
    )
    return {"created": entities, "count": len(entities)}
```

**Step 2: Add `batch_create_relations` MCP tool**

Add after the `create_relation` tool:

```python
@runtime_mcp.tool()
@_enrich_errors
async def batch_create_relations(
    relation_type_key: str,
    items: list[dict],
) -> dict:
    """Create multiple relations of the same type in a single batch (max 100).
    Each item must have fromEntityId, toEntityId, and optional properties.
    All items are validated first — if any fail, the entire batch is rejected."""
    ontology_key = _get_ontology_key()
    driver = await get_driver()
    relations = await service.batch_create_relations(
        ontology_key, relation_type_key, items, driver
    )
    return {"created": relations, "count": len(relations)}
```

**Step 3: Run all tests**

Run: `cd backend && uv run pytest tests/runtime/ -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add backend/src/ontoforge_server/mcp/runtime.py
git commit -m "Add batch create MCP tools for entities and relations"
```

---

### Task 9: Frontend Types and Client

**Files:**
- Modify: `frontend/src/types/runtime.ts`
- Modify: `frontend/src/api/runtimeClient.ts`

**Step 1: Add batch types**

Add to `frontend/src/types/runtime.ts` after `SemanticSearchResponse`:

```typescript
// Batch operations
export interface BatchCreateResponse<T> {
  created: T[];
  count: number;
}
```

**Step 2: Add batch client functions**

Add to `frontend/src/api/runtimeClient.ts` after `createEntity`:

```typescript
export const batchCreateEntities = (ontologyKey: string, entityTypeKey: string, items: Record<string, unknown>[]) =>
  request<BatchCreateResponse<EntityInstance>>(`/${ontologyKey}/entities/${entityTypeKey}/batch`, {
    method: 'POST',
    body: JSON.stringify({ items }),
  });
```

Add after `createRelation`:

```typescript
export const batchCreateRelations = (ontologyKey: string, relationTypeKey: string, items: Record<string, unknown>[]) =>
  request<BatchCreateResponse<RelationInstance>>(`/${ontologyKey}/relations/${relationTypeKey}/batch`, {
    method: 'POST',
    body: JSON.stringify({ items }),
  });
```

**Step 3: Add import for `BatchCreateResponse`**

Update the import block at the top of `runtimeClient.ts` to include `BatchCreateResponse`:

```typescript
import type {
  RuntimeSchema,
  EntityInstance,
  RelationInstance,
  PaginatedResponse,
  FeaturesResponse,
  SemanticSearchResponse,
  BatchCreateResponse,
} from '../types/runtime';
```

**Step 4: Commit**

```bash
git add frontend/src/types/runtime.ts frontend/src/api/runtimeClient.ts
git commit -m "Add batch create functions to frontend API client"
```

---

### Task 10: Update Documentation

**Files:**
- Modify: `docs/api-contracts/runtime-api.md`
- Modify: `docs/runtime-usage.md`
- Modify: `docs/architecture.md`
- Modify: `docs/mcp-architecture.md`

**Step 1: Add batch section to runtime-api.md**

Insert a new section between §4 (Relation Instance CRUD) and §5 (Graph Traversal). This means the current §5–§9 become §6–§10. Add:

```markdown
## 5. Batch Operations

Batch endpoints accept multiple items and create them in a single database transaction. All items are validated before any writes occur — if any item fails validation, the entire batch is rejected.

Maximum batch size: 100 items per request.

### POST /api/runtime/{ontologyKey}/entities/{entityTypeKey}/batch

Create multiple entity instances of the same type.

**Request body:**
```json
{
  "items": [
    {"name": "Alice", "age": 30},
    {"name": "Bob", "age": 25}
  ]
}
```

**Response:** `201 Created`
```json
{
  "created": [
    {
      "_id": "b7e3f1a2-...",
      "_entityTypeKey": "person",
      "_createdAt": "2026-03-08T10:00:00Z",
      "_updatedAt": "2026-03-08T10:00:00Z",
      "name": "Alice",
      "age": 30
    },
    {
      "_id": "c8d4e2b3-...",
      "_entityTypeKey": "person",
      "_createdAt": "2026-03-08T10:00:00Z",
      "_updatedAt": "2026-03-08T10:00:00Z",
      "name": "Bob",
      "age": 25
    }
  ],
  "count": 2
}
```

**Validation:** Same per-item rules as single entity creation. Errors are collected per item and returned with item indices:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Batch validation failed",
    "details": {
      "items": {
        "1": {
          "fields": {
            "name": "Required property missing"
          }
        }
      }
    }
  }
}
```

**Errors:** 404 if entity type not found. 422 if validation fails or batch size exceeded.

### POST /api/runtime/{ontologyKey}/relations/{relationTypeKey}/batch

Create multiple relation instances of the same type.

**Request body:**
```json
{
  "items": [
    {"fromEntityId": "b7e3f1a2-...", "toEntityId": "a1b2c3d4-...", "role": "Engineer"},
    {"fromEntityId": "c8d4e2b3-...", "toEntityId": "a1b2c3d4-...", "role": "Designer"}
  ]
}
```

**Response:** `201 Created` — same shape as batch entity response, with relation instances including `fromEntityId` and `toEntityId`.

**Validation:** Same per-item rules as single relation creation. Entity existence and type matching are checked in a single batch query.

**Errors:** 404 if relation type not found. 422 if validation fails or batch size exceeded.
```

**Step 2: Update the endpoint summary table in runtime-api.md**

Add two rows to the §9 (now §10) endpoint summary table:

```markdown
| `POST` | `/api/runtime/{ontologyKey}/entities/{entityTypeKey}/batch` | Batch create entity instances |
| `POST` | `/api/runtime/{ontologyKey}/relations/{relationTypeKey}/batch` | Batch create relation instances |
```

**Step 3: Add batch section to runtime-usage.md**

Add a new section after §3 (Relation Instances), before §4 (Filtering and Search). Insert:

```markdown
## Batch Operations

Create multiple entities or relations in a single request (max 100 items). All items are validated first — if any fail, the entire batch is rejected.

### Batch Create Entities

```bash
curl -X POST http://localhost:8000/api/runtime/test_ontology/entities/person/batch \
  -H 'Content-Type: application/json' \
  -d '{"items": [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]}'
```

Response: `{"created": [...], "count": 2}`

### Batch Create Relations

```bash
curl -X POST http://localhost:8000/api/runtime/test_ontology/relations/works_for/batch \
  -H 'Content-Type: application/json' \
  -d '{"items": [{"fromEntityId": "<person-id-1>", "toEntityId": "<company-id>", "role": "Engineer"}, {"fromEntityId": "<person-id-2>", "toEntityId": "<company-id>", "role": "Designer"}]}'
```

Response: `{"created": [...], "count": 2}`
```

**Step 4: Update architecture.md endpoint summary table**

Add two rows to the table in §5.3:

```markdown
| `POST` | `/api/runtime/{ontologyKey}/entities/{entityTypeKey}/batch` | Batch create entity instances |
| `POST` | `/api/runtime/{ontologyKey}/relations/{relationTypeKey}/batch` | Batch create relation instances |
```

**Step 5: Update mcp-architecture.md tool catalog**

In §3.2, update the heading to "Runtime MCP Tools (15 tools)" and add a new sub-section after Entity Operations:

```markdown
#### Batch Operations

| Tool | Arguments | Returns | Description |
|------|-----------|---------|-------------|
| `batch_create_entities` | `entity_type_key`, `items` (list of property objects, max 100) | `{created: [...], count: N}` | Create multiple entities of the same type in a single batch. All items validated first — if any fail, the entire batch is rejected. |
| `batch_create_relations` | `relation_type_key`, `items` (list of objects with `fromEntityId`, `toEntityId`, and optional properties, max 100) | `{created: [...], count: N}` | Create multiple relations of the same type in a single batch. Entity existence and type matching validated in batch. |
```

**Step 6: Run all tests to verify nothing broke**

Run: `cd backend && uv run pytest tests/runtime/ -v`
Expected: All PASS

**Step 7: Commit**

```bash
git add docs/api-contracts/runtime-api.md docs/runtime-usage.md docs/architecture.md docs/mcp-architecture.md
git commit -m "Document batch create operations in API docs"
```

---

### Task 11: Final Verification

**Step 1: Run all backend tests**

Run: `cd backend && uv run pytest tests/ -v`
Expected: All PASS

**Step 2: Verify frontend compiles**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors

**Step 3: Review all changes**

Run: `git log --oneline main..HEAD`
Expected: See all batch operation commits in order

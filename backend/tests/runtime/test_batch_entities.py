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

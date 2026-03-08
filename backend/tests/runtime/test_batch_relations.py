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

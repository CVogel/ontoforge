"""Regressionstest für Issue #2: Offset-Pagination braucht eine stabile,
eindeutige Sortierung.

Ohne eindeutigen Sekundär-Schlüssel im ORDER BY bricht Neo4j Gleichstände
(z. B. identische `_createdAt`-Werte aus Batch-Anlagen) nichtdeterministisch —
SKIP/LIMIT liefert dann über Seiten hinweg Duplikate und überspringt Zeilen.
Diese Tests prüfen direkt die generierte Cypher-Query der Repository-Schicht.
"""

from unittest.mock import AsyncMock

import pytest

from ontoforge_server.runtime import repository


class _CountResult:
    def __init__(self, total: int):
        self._total = total

    async def single(self):
        return {"total": self._total}


class _DataResult:
    """Async-iterierbares, leeres Datenergebnis (Zeileninhalt ist hier egal)."""

    def __aiter__(self):
        async def _gen():
            return
            yield  # pragma: no cover — macht die Funktion zum Generator

        return _gen()


def _capturing_session(count_marker: str):
    """Mock-Session, die alle abgesetzten Queries einsammelt."""
    captured: list[str] = []

    async def _run(query, params=None, **kwargs):
        captured.append(query)
        if count_marker in query:
            return _CountResult(1)
        return _DataResult()

    session = AsyncMock()
    session.run = _run
    return session, captured


def _data_query(captured: list[str]) -> str:
    return next(q for q in captured if "SKIP" in q)


@pytest.mark.asyncio
async def test_list_relations_orders_by_unique_tiebreaker():
    session, captured = _capturing_session("count(r)")

    await repository.list_relations(
        session,
        rel_type_upper="WORKS_FOR",
        relation_type_key="works_for",
        where_clauses=[],
        params={},
        sort_field="_createdAt",
        order="ASC",
        limit=200,
        offset=0,
    )

    data_query = _data_query(captured)
    assert "elementId(r)" in data_query
    # Tiebreaker muss NACH dem primären Sortierfeld stehen
    assert "ORDER BY r._createdAt ASC, elementId(r)" in data_query


@pytest.mark.asyncio
async def test_list_entities_orders_by_unique_tiebreaker():
    session, captured = _capturing_session("count(n)")

    await repository.list_entities(
        session,
        pascal_label="Person",
        entity_type_key="person",
        where_clauses=[],
        params={},
        sort_field="_createdAt",
        order="ASC",
        limit=200,
        offset=0,
    )

    data_query = _data_query(captured)
    assert "elementId(n)" in data_query
    assert "ORDER BY n._createdAt ASC, elementId(n)" in data_query

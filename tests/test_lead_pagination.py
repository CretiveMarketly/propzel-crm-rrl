"""Offline pagination regressions for the actual lead route; no Mongo access."""
import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Optional


def load_route(db):
    source = Path(__file__).resolve().parents[1] / 'server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    route = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'list_leads')
    route.decorator_list = []
    namespace = dict(
        db=db, asyncio=asyncio, Optional=Optional, Literal=Literal,
        Query=lambda default, **kwargs: default, Depends=lambda dependency: None,
        get_current_user=lambda: None,
        organization_scope=lambda user: {'organization_id': user['organization_id']},
        sanitize_many=lambda docs, user: docs,
    )
    exec(compile(ast.Module(body=[route], type_ignores=[]), '<lead-route>', 'exec'), namespace)
    return namespace['list_leads']


class Cursor:
    def __init__(self, size):
        self.size, self.offset, self.bound, self.sort_fields = size, 0, None, None
    def sort(self, fields):
        self.sort_fields = fields
        return self
    def skip(self, offset):
        self.offset = offset
        return self
    def limit(self, bound):
        self.bound = bound
        return self
    async def to_list(self, size):
        assert self.bound is not None, 'A client to_list bound is not a MongoDB query limit'
        return [{'id': str(i)} for i in range(self.offset, min(self.size, self.offset + min(size, self.bound)))]


class Collection:
    def __init__(self, size):
        self.size, self.cursors, self.queries = size, [], []
    def find(self, query, projection):
        self.queries.append(query)
        cursor = Cursor(self.size)
        self.cursors.append(cursor)
        return cursor
    async def count_documents(self, query):
        assert query == self.queries[-1], 'The total must use the same access and filter scope'
        return self.size


class LeadPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_is_not_capped_and_next_pages_are_reachable(self):
        collection = Collection(26922)
        route = load_route(SimpleNamespace(leads=collection))
        actor = {'role': 'super_admin', 'organization_id': 'test-org', 'id': 'user'}
        first = await route(page=1, page_size=100, user=actor)
        second = await route(page=2, page_size=100, user=actor)
        later = await route(page=21, page_size=100, user=actor)
        self.assertEqual(first['total'], 26922)
        self.assertEqual(len(first['items']), 100)
        self.assertTrue(set(x['id'] for x in first['items']).isdisjoint(x['id'] for x in second['items']))
        self.assertEqual(later['items'][0]['id'], '2000')
        self.assertEqual(collection.cursors[0].sort_fields, [('created_at', -1), ('id', -1)])

    async def test_filters_and_manager_scope_apply_to_total_and_rows(self):
        collection = Collection(12)
        route = load_route(SimpleNamespace(leads=collection))
        actor = {'role': 'manager', 'organization_id': 'test-org', 'id': 'manager'}
        result = await route(page=1, page_size=100, user=actor, stage='new', created_after='2026-10-01')
        clauses = collection.queries[-1]['$and']
        self.assertIn({'organization_id': 'test-org'}, clauses)
        self.assertIn({'stage': 'new'}, clauses)
        self.assertIn({'created_at': {'$gte': '2026-10-01'}}, clauses)
        self.assertIn({'$or': [{'assigned_to': 'manager'}, {'co_assigned_to': 'manager'}]}, clauses)
        self.assertEqual(result['total'], 12)

    async def test_legacy_array_response_also_bounds_the_database_sort(self):
        collection = Collection(26922)
        route = load_route(SimpleNamespace(leads=collection))
        result = await route(page=None, user={'role': 'admin', 'organization_id': 'test-org'})
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2000)
        self.assertEqual(collection.cursors[-1].bound, 2000)

    async def test_empty_organization_reports_zero_without_phantom_pages(self):
        collection = Collection(0)
        result = await load_route(SimpleNamespace(leads=collection))(page=1, page_size=100, user={'role': 'admin', 'organization_id': 'test-org'})
        self.assertEqual(result['items'], [])
        self.assertEqual(result['total'], 0)


if __name__ == '__main__':
    unittest.main()

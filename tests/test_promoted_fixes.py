import ast
import html
import re
import unittest
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]

def load(name, functions):
    tree = ast.parse((ROOT / name).read_text(encoding='utf-8-sig'))
    scope = dict(re=re, html=html, unicodedata=unicodedata,
                 parse_qsl=parse_qsl, urlencode=urlencode, urlsplit=urlsplit,
                 urlunsplit=urlunsplit, Signal=SimpleNamespace)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), name, 'exec'), scope)
    return scope

class PromotedFixes(unittest.TestCase):
    def test_tracking_and_category_aliases(self):
        for name in ('collector_ver72.py', 'collector_ver72_hakodate.py'):
            canonical = load(name, {'canonical_url'})['canonical_url']
            a = 'https://mrs.living.jp/sapporo/newopen/article/6985501/?utm_source=ranking'
            b = 'https://mrs.living.jp/sapporo/a_feature/article/6985501/#top'
            self.assertEqual(canonical(a), canonical(b))
            self.assertNotEqual(canonical(a), canonical(b.replace('6985501','6985502')))

    def test_meaningful_query_preserved(self):
        for name in ('collector_ver72.py', 'collector_ver72_hakodate.py'):
            canonical = load(name, {'canonical_url'})['canonical_url']
            self.assertEqual(canonical('https://example.test/event?id=123&utm_source=a&fbclid=b'),
                             'https://example.test/event?id=123')
            self.assertNotEqual(canonical('https://example.test/event?id=123'),
                                canonical('https://example.test/event?id=124'))

    def test_same_event_merge_and_different_dates_preserved(self):
        fn = load('collector_ver7380.py', {'norm','dedupe_signals'})['dedupe_signals']
        def signal(day, url):
            return SimpleNamespace(candidate_type='named_store', area='東区',
                store_name='店舗A', status='open', date_hint=day, title='店舗A',
                url=url, snippet='', address_hint='', reasons=[], confidence=0)
        self.assertEqual(len(fn([signal('2026-10-10','a'),signal('2026-10-10','b')])),1)
        self.assertEqual(len(fn([signal('2026-10-10','a'),signal('2026-11-10','b')])),2)
        self.assertEqual(len(fn([signal('','a'),signal('','b')])),2)

if __name__ == '__main__':
    unittest.main()

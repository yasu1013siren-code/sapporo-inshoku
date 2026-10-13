import unittest
from datetime import datetime, timezone
from hakodate_hotpepper_filter import select

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, tzinfo=timezone.utc)
        self.item = {'name':'函館テスト店','place':'函館市本町1-1'}
        self.payload = {'areas':{'hakodate':[self.item], 'higashi':[{'name':'札幌店'}]}}
        self.record = dict(self.item, status='not_found', checked_at=self.now.isoformat(),
            evidence_urls=['https://www.hotpepper.jp/SA41/Y883/'],
            name_variants_checked=True, address_checked=True)
    def test_absence_includes_and_preserves_sapporo_and_input(self):
        result, queue = select(self.payload, [self.record], self.now)
        self.assertEqual(len(result['hakodate_directory']), 1)
        self.assertEqual(result['areas']['hakodate'],self.payload['areas']['hakodate'])
        self.assertEqual(result['areas']['higashi'], self.payload['areas']['higashi'])
        self.assertNotIn('hotpepper_check', self.item)
    def test_listed_excluded(self):
        self.record['status']='listed'
        result, queue=select(self.payload,[self.record],self.now)
        self.assertEqual(result['hakodate_directory'],[])
        self.assertEqual(len(queue['excluded_listed']),1)
    def test_unknown_stale_conflicts_and_wrong_address_pending(self):
        scenarios=[[],[dict(self.record,status='error')],[dict(self.record,checked_at='2026-10-01T00:00:00+00:00')],
            [self.record,self.record],[dict(self.record,place='函館市本町2-2')],
            [dict(self.record,address_checked=False)],[dict(self.record,evidence_urls=['https://hotpepper.jp.evil.test/'])],
            [dict(self.record,checked_at='2026-10-12T00:00:00')]]
        for records in scenarios:
            with self.subTest(records=records):
                result,queue=select(self.payload,records,self.now)
                self.assertEqual(result['hakodate_directory'],[])
                self.assertEqual(len(queue['pending']),1)

if __name__=='__main__': unittest.main()

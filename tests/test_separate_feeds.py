import unittest
from datetime import datetime, timezone
from hakodate_hotpepper_filter import select
from hakodate_directory import parse_spot

class SeparateFeeds(unittest.TestCase):
    def test_old_and_undated_directory_and_listed_new_opening(self):
        now=datetime.now(timezone.utc)
        old={'name':'老舗','place':'函館市本町1-1','date':'1980-01-01','type':'restaurant'}
        new={'name':'新店','place':'函館市本町2-2','date':'2026-10-13','type':'open'}
        records=[dict(old,status='not_found',checked_at=now.isoformat(),evidence_urls=['https://www.hotpepper.jp/SA41/Y883/'],name_variants_checked=True,address_checked=True)]
        payload={'areas':{'hakodate':[new]},'hakodate_directory_candidates':[old]}
        result,_=select(payload,records,now)
        self.assertEqual(result['areas']['hakodate'],[new])
        self.assertEqual(result['hakodate_directory'][0]['date'],'1980-01-01')
        old['date']=''
        self.assertEqual(len(select(payload,records,now)[0]['hakodate_directory']),1)
    def test_source_address_and_no_opening_date_required(self):
        page='<h1>喫茶テスト</h1><table><tr><th>住所</th><td>函館市本町1-1</td></tr></table>'
        item=parse_spot(page,'https://www.hakobura.jp/spots/1')
        self.assertEqual(item['date'],'')
        self.assertTrue(item['address_verified'])
        self.assertIsNone(parse_spot(page.replace('函館市','北斗市'),'test'))

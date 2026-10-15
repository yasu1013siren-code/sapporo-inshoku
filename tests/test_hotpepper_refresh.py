import unittest
from unittest.mock import patch
from hotpepper_refresh import search, check

class LiveCheckTests(unittest.TestCase):
    @patch('hotpepper_refresh.fetch')
    def test_zero_requires_query_echo(self, fetch):
        fetch.return_value='<input name="FWT" value="Cafe Ikazuwa"><span>検索結果</span><span class="count">0</span>件'
        self.assertEqual(search('Cafe Ikazuwa')[1],0)
        fetch.return_value='<input name="FWT" value=""><span>検索結果</span><span>0</span>件'
        with self.assertRaises(ValueError): search('Cafe Ikazuwa')
    @patch('hotpepper_refresh.search', side_effect=TimeoutError)
    def test_failed_search_cannot_reuse_absence(self, search):
        item={'name':'Test','place':'函館市'}
        record=check(item,dict(item,status='not_found'))
        self.assertEqual(record['status'],'unknown')
    @patch('hotpepper_refresh.search',return_value=('https://www.hotpepper.jp/SA41/Y883/ff/?FWT=Test',0))
    def test_zero_requires_verified_address(self, search):
        item={'name':'Test','place':'函館市'}
        self.assertEqual(check(item,None)['status'],'unknown')
        record=check(item,{'verified_address':'函館市本町1-1'})
        self.assertEqual(record['status'],'unknown')
        self.assertEqual(record['reason'],'external_search_review_required')
    @patch('hotpepper_refresh.search',return_value=('https://www.hotpepper.jp/SA41/Y883/ff/?FWT=Test',1))
    def test_result_requires_identity_review(self, search):
        self.assertEqual(check({'name':'Test','place':'函館市'}, {'verified_address':'函館市本町1-1'})['status'],'unknown')

    @patch('hotpepper_refresh.search',return_value=('https://www.hotpepper.jp/SA41/Y883/ff/?FWT=Test',0))
    def test_stale_external_review_cannot_be_inherited(self, search):
        item={'name':'Test','place':'函館市本町1-1'}
        prior=dict(item,status='not_found',verified_address=item['place'],
                   checked_at='2020-01-01T00:00:00+09:00',
                   external_search_checked=True,
                   identity_review={'decision':'not_found'})
        record=check(item,prior)
        self.assertEqual(record['status'],'unknown')
        self.assertEqual(record['reason'],'external_search_review_required')

if __name__=='__main__': unittest.main()

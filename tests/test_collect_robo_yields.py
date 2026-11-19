import importlib.util
import json
import unittest
from datetime import date, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location('yields', Path(__file__).resolve().parents[1] / 'scripts/collect_robo_yields.py')
yields = importlib.util.module_from_spec(spec)
spec.loader.exec_module(yields)


def payload(code, name, rows, total=None):
    return json.dumps({'StatisticSearch': {'list_total_count': len(rows) if total is None else total, 'row': [
        {'STAT_CODE': '817Y002', 'ITEM_CODE1': code, 'ITEM_NAME1': name, 'UNIT_NAME': '연%', 'TIME': day, 'DATA_VALUE': val}
        for day, val in rows
    ]}}).encode()


class EcosCollectorTests(unittest.TestCase):
    def test_treasury_urls_extend_to_future_years(self):
        urls = yields.treasury_urls(date(2024, 12, 31), date(2028, 1, 1))
        self.assertEqual(urls[0], yields.ARCHIVE)
        self.assertEqual(len(urls), 5)
        self.assertTrue(urls[-1].endswith('field_tdr_date_value=2028'))

    def test_normalizes_official_shape_and_preserves_unknown_publication(self):
        result, total = yields.parse_ecos_page(payload('010190000', '국고채(1년)', [('20261110', '3.57')]), '010190000', '2026-11-19T12:00:00+00:00')
        self.assertEqual(total, 1)
        self.assertEqual(result[0]['series'], 'KR_YIELD_1Y')
        self.assertEqual(result[0]['observation_date'], '2026-11-10')
        self.assertEqual(result[0]['value'], '3.57')
        self.assertEqual(result[0]['source'], 'bok_ecos_817Y002')
        self.assertEqual(result[0]['published_at'], '')
        self.assertEqual(result[0]['availability_policy'], 'historical_dated_yield')

    def test_rejects_wrong_series_or_nonpercent(self):
        body = payload('010190000', '국고채(1년)', [('20261110', '3.57')])
        with self.assertRaises(ValueError):
            yields.parse_ecos_page(body, '010200000', 'now')
        wrong_unit = json.loads(body)
        wrong_unit['StatisticSearch']['row'][0]['UNIT_NAME'] = 'KRW'
        with self.assertRaises(ValueError):
            yields.parse_ecos_page(json.dumps(wrong_unit).encode(), '010190000', 'now')

    def test_rejects_ecos_error_without_echoing_response(self):
        with self.assertRaisesRegex(ValueError, 'ECOS') as caught:
            yields.parse_ecos_page(b'{"RESULT":{"MESSAGE":"secret"}}', '010190000', 'now')
        self.assertNotIn('secret', str(caught.exception))

    def test_paginated_collection_rejects_incomplete_tenor_dates(self):
        def fake_fetch(url):
            code = url.rsplit('/', 1)[-1]
            name = {'010190000':'국고채(1년)', '010200000':'국고채(3년)', '010210000':'국고채(10년)'}[code]
            dates = [('20261110', '3.57')] if code != '010210000' else [('20261111', '3.57')]
            return payload(code, name, dates), '2026-11-19T12:00:00+00:00'
        with self.assertRaisesRegex(ValueError, 'dates'):
            yields.collect_ecos('abc123', fetcher=fake_fetch)

    def test_key_missing_and_sample_keys_are_rejected(self):
        for key in ('', 'sample'):
            with self.assertRaises(ValueError):
                yields.collect_ecos(key)

    def test_collects_all_pages_without_mixing_tenors(self):
        dates = [(date(2019, 1, 2) + timedelta(days=i)).strftime('%Y%m%d') for i in range(1001)]
        names = {'010190000':'국고채(1년)', '010200000':'국고채(3년)', '010210000':'국고채(10년)'}
        def fake_fetch(url):
            code = url.rsplit('/', 1)[-1]
            start = int(url.split('/817Y002/')[0].split('/')[-2])
            return payload(code, names[code], [(d, '3.57') for d in dates[start-1:start+999]], total=1001), 'now'
        rows = yields.collect_ecos('abc123', fetcher=fake_fetch)
        self.assertEqual(len(rows), 3003)
        self.assertEqual({r['series'] for r in rows}, {'KR_YIELD_1Y', 'KR_YIELD_3Y', 'KR_YIELD_10Y'})

    def test_network_error_does_not_expose_key(self):
        def fail(url):
            raise RuntimeError(url)
        with self.assertRaises(ValueError) as caught:
            yields.collect_ecos('sensitivekey', fetcher=fail)
        self.assertNotIn('sensitivekey', str(caught.exception))

if __name__ == '__main__':
    unittest.main()

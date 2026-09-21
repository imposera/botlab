import json
import tempfile
import unittest
from pathlib import Path

from tb_runner_shape import shape_symbols, shape_class, price_with_source
from tb_shape_review import race_runner_records, export_json, aggregate_shapes


class RunnerShapeTests(unittest.TestCase):
    def test_stage_aliases_and_gaps(self):
        self.assertEqual(shape_symbols({'t15': 10, 'T-10': 9, 'T5': 8}), '▼▼??')
        self.assertEqual(shape_symbols({'T15': 10, 'T30': 5}), '????')
        self.assertEqual(shape_class({'t15': 10, 't30': 5}), 'steady_firm')
        self.assertEqual(shape_class({'T15': float('nan')}), 'insufficient')
        self.assertEqual(price_with_source({'last_price_traded': True, 'back_levels': [{'price': 4}]}), (4, 'best_back'))

    def records(self, root, removal=False, missing_time=False, reverse_time=False):
        race = root / '2026-09-08' / '1.2'
        race.mkdir(parents=True, exist_ok=True)
        for stage, minute, price in [('t15', 0, 10), ('t10', 1, 9), ('t5', 2, 8), ('t2', 3, 7), ('t30', 4, 6)]:
            book = {'captured_at': f'2026-09-08T00:0{minute}:00Z', 'market': {},
                    'runners': [{'selection_id': 1, 'last_price_traded': price},
                                {'selection_id': 2, 'status': 'REMOVED' if removal and minute > 1 else 'ACTIVE'}]}
            if missing_time and minute == 1:
                del book['captured_at']
            if reverse_time and minute == 4:
                book['captured_at'] = '2026-09-07T23:59:00Z'
            (race / f'market_book_{stage}.json').write_text(json.dumps(book))
        return race_runner_records(race)

    def test_capture_timing_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            records = self.records(root)
            r = records[0]
            self.assertEqual(r['slope_time_basis'], 'captured_at')
            self.assertAlmostEqual(r['early_slope'], __import__('math').log(.8) / 2)
            self.assertEqual(r['reversal_count'], 0)
            out = root / 'export.json'
            export_json(out, records, aggregate_shapes(records))
            self.assertEqual(json.loads(out.read_text())['runners'][0]['runner_key'], 'sid:1')
            r = self.records(root, missing_time=True)[0]
            self.assertIn('nominal_time_fallback', r['quality_flags'])
            r = self.records(root, reverse_time=True)[0]
            self.assertIsNone(r['early_slope'])
            self.assertIsNone(r['fit_r2'])

    def test_field_removal_suppresses_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            r = self.records(Path(temp), removal=True)[0]
            self.assertEqual(r['shape_class'], 'scratching_break')
            for key in ('move_pct', 'early_slope', 'late_slope', 'fit_r2', 'reversal_count'):
                self.assertIsNone(r[key])

    def test_coverage_separates_identical_gap_symbols(self):
        with tempfile.TemporaryDirectory() as temp:
            r = self.records(Path(temp))[0]
            a = dict(r, shape='????', coverage=['T15', 'T30'])
            b = dict(r, shape='????', coverage=['T10', 'T2'])
            self.assertEqual(len(aggregate_shapes([a, b])), 2)

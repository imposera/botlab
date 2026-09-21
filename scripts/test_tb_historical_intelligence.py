import unittest

from tb_historical_intelligence import class_summary, fit, score, movement_bucket, shape_bucket


class HistoricalIntelligenceTests(unittest.TestCase):
    def test_movement_and_shape_are_pre_start_only(self):
        prices = {'T15': 4.0, 'T10': 3.8, 'T5': 3.2, 'T2': 3.0, 'T30': 9.0}
        self.assertEqual(movement_bucket(prices), 'firming')
        self.assertTrue(shape_bucket(prices))

    def test_smoothed_score_does_not_overreact_to_one_winner(self):
        records = [
            {'prices': {'T15': 4, 'T10': 3.8, 'T5': 3.2, 'T2': 3}, 'race_class': 'Late firm', 'winner': True},
            {'prices': {'T15': 4, 'T10': 4.1, 'T5': 4.2, 'T2': 4.3}, 'race_class': 'Stable', 'winner': False},
        ]
        model = fit(records)
        result = score(records[0]['prices'], 'Late firm', model)
        self.assertEqual(result['starts'], 1)
        self.assertLess(result['score'], 50)
        self.assertEqual(result['confidence'], 'Low')

    def test_scratched_records_are_excluded(self):
        model = fit([{'prices': {'T15': 3, 'T2': 2.5}, 'race_class': 'Firm', 'winner': True, 'scratched': True}])
        self.assertEqual(model['groups'], {})
        self.assertEqual(model['components']['movement'], {})

    def test_component_history_breaks_global_tie(self):
        records = []
        for _ in range(8):
            records.append({'prices': {'T15': 5, 'T10': 4, 'T5': 3, 'T2': 2.8}, 'race_class': 'Strong', 'winner': True})
        for _ in range(8):
            records.append({'prices': {'T15': 5, 'T10': 5.1, 'T5': 5.2, 'T2': 5.3}, 'race_class': 'Weak', 'winner': False})
        model = fit(records)
        firm = score(records[0]['prices'], 'Strong', model)
        drift = score(records[-1]['prices'], 'Weak', model)
        self.assertGreater(firm['score'], drift['score'])
        self.assertGreater(firm['evidence'], 0)

    def test_class_summary_includes_average_winning_price(self):
        records=[{'prices': {'T15': 5, 'T10': 4, 'T5': 3.5, 'T2': price}, 'race_class': 'Late firm', 'winner': True}
                 for price in (2.0, 4.0)]
        records += [{'prices': {'T15': 5, 'T10': 5.1}, 'race_class': 'Late firm', 'winner': False} for _ in range(20)]
        summary=class_summary(fit(records), minimum_starts=1)
        self.assertEqual(summary[0]['average_winning_price'], 3.0)


if __name__ == '__main__':
    unittest.main()

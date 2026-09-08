import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from tb_blackbook_page import page
from tb_wall import WallHandler


class BlackbookPageTests(unittest.TestCase):
    def test_search_filters_pagination_and_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'state').mkdir()
            entries = [dict(key=f'sid:{i}', runner_name=f'Horse {i}', wins=[],
                            tracks_seen=['Test'], tags=[], notes='') for i in range(55)]
            entries[0].update(runner_name='<script>Horse</script>', tags=['watch'], notes='special note')
            entries[0]['wins'] = [{'country': 'Australia'}]
            (base / 'state/tb_blackbook.json').write_text(json.dumps({'entries': entries}))
            rendered = page(base, {})
            self.assertIn('Page 1 of 2', rendered)
            self.assertNotIn('<script>Horse</script>', rendered)
            self.assertIn('&lt;script&gt;Horse', rendered)
            self.assertIn('1 matching runners', page(base, {'q': ['special note']}))
            self.assertIn('1 matching runners', page(base, {'tagged': ['1']}))
            self.assertIn('No matching runners', page(base, {'track': ['Other']}))
            self.assertIn('1 matching runners', page(base, {'min_wins': ['1']}))
            self.assertIn('1 matching runners', page(base, {'country': ['Australia']}))
            self.assertIn('No matching runners', page(base, {'country': ['France']}))
            self.assertIn('Page 2 of 2', page(base, {'page': ['999']}))
            self.assertIn('Page 1 of 2', page(base, {'page': ['invalid']}))
            handler = object.__new__(WallHandler)
            handler.base_dir = base
            handler.path = '/blackbook?q=special+note'
            handler.send_text = Mock()
            handler.do_GET()
            self.assertIn('1 matching runners', handler.send_text.call_args.args[0])

    def test_unavailable_register(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIn('Register unavailable', page(Path(directory), {}))

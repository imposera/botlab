import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import tb_race_queue as queue
from tb_race_queue_wall import make_handler


class WallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        queue.atomic_write_json(self.state / queue.QUEUE_FILE_NAME, {
            'decisions': {}, 'races': [{'market_id': '1.2', 'eligible': True}],
        })
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.state))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        status, page = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.token = re.search(r"'X-Queue-Token':'([^']+)'", page).group(1)

    def request(self, method, path, data=None, token=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Queue-Token'] = token
        connection.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        result = response.status, response.read().decode()
        connection.close()
        return result

    def test_decisions_persist_and_audit(self):
        for action, eligible in [('remove', False), ('keep', True), ('restore', True)]:
            status, body = self.request('POST', '/api/decision', {
                'market_id': '1.2', 'action': action, 'reason': 'test',
            }, self.token)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)['races'][0]['eligible'], eligible)
        saved = queue.read_json(self.state / queue.QUEUE_FILE_NAME)
        self.assertEqual(saved['decisions'], {})
        events = (self.state / queue.DECISION_LOG_FILE_NAME).read_text().splitlines()
        self.assertEqual([json.loads(e)['action'] for e in events], ['remove', 'keep', 'restore'])

    def test_invalid_requests_do_not_write(self):
        path = self.state / queue.QUEUE_FILE_NAME
        before = path.read_bytes()
        for data, token, expected in [
            ({'market_id': '1.2', 'action': 'remove'}, None, 403),
            ({'market_id': '1.2', 'action': 'bet'}, self.token, 400),
            ({'market_id': 'missing', 'action': 'remove'}, self.token, 409),
            ([], self.token, 400),
        ]:
            self.assertEqual(self.request('POST', '/api/decision', data, token)[0], expected)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_and_corrupt_state(self):
        path = self.state / queue.QUEUE_FILE_NAME
        path.unlink()
        self.assertEqual(self.request('GET', '/api/queue')[0], 503)
        path.write_text('{broken')
        self.assertEqual(self.request('GET', '/api/queue')[0], 503)


if __name__ == '__main__':
    unittest.main()

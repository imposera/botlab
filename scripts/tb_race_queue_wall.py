#!/usr/bin/env python3
"""Local HTTP race queue wall; decisions reuse the queue's lock and audit log."""
from __future__ import annotations

import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import tb_race_queue as queue


def make_handler(state_dir: Path):
    token = secrets.token_urlsafe(32)
    page = Path(__file__).with_suffix('.html').read_text().replace('__TOKEN__', token)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, value, status=200, html=False):
            body = (value if html else json.dumps(value)).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/':
                return self.reply(page, html=True)
            if path == '/api/queue':
                try:
                    data = queue.load_queue_for_update(state_dir)
                    if not data:
                        raise RuntimeError("Queue missing. Run tb_race_queue.py refresh first.")
                    return self.reply(data)
                except (RuntimeError, OSError) as exc:
                    return self.reply({'error': str(exc)}, 503)
            self.reply({'error': 'Not found'}, 404)

        def do_POST(self):
            if urlsplit(self.path).path != '/api/decision':
                return self.reply({'error': 'Not found'}, 404)
            if self.headers.get('X-Queue-Token') != token:
                return self.reply({'error': 'Reload this page before making a decision.'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise ValueError('Invalid request size')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Expected JSON')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Expected an object')
                action, market_id, reason = data.get('action'), data.get('market_id'), data.get('reason')
                if action not in ('keep', 'remove', 'restore'):
                    raise ValueError('Invalid decision')
                if not isinstance(market_id, str) or not market_id:
                    raise ValueError('Missing market ID')
                if reason is not None and (not isinstance(reason, str) or len(reason) > 1000):
                    raise ValueError('Reason must be at most 1000 characters')
                current = queue.load_queue_for_update(state_dir)
                if not any(str(row.get('market_id')) == market_id for row in current.get('races', [])):
                    return self.reply({'error': 'Race is no longer in the queue. Reload the queue.'}, 409)
                result = queue.update_decision(state_dir, market_id,
                                               None if action == 'restore' else action,
                                               None if action == 'restore' else reason)
                self.reply(result)
            except (ValueError, UnicodeError) as exc:
                self.reply({'error': str(exc)}, 400)
            except (RuntimeError, OSError) as exc:
                self.reply({'error': str(exc)}, 503)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=queue.DEFAULT_STATE_DIR)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8792)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.state_dir.expanduser()))
    print(f'TB Queue Wall: http://{args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

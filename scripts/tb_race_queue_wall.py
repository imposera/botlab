#!/usr/bin/env python3
"""Local HTTP race queue wall; decisions reuse the queue's lock and audit log."""
from __future__ import annotations

import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import tb_race_queue as queue
import tb_today_card as today_card
from tb_ra_import import Importer
from tb_ra_form_import import FormImporter
from tb_au_capture_wall import response as capture_response
from tb_next_five import response as next_five_response
from tb_au_coverage import summary as au_coverage_summary


def make_handler(state_dir: Path):
    token = secrets.token_urlsafe(32)
    importer = Importer(state_dir)
    form_importer=FormImporter(state_dir.parent)
    form_page=Path(__file__).with_name('tb_ra_form_import.html').read_text().replace('__TOKEN__',token)
    import_page = Path(__file__).with_name("tb_ra_import.html").read_text().replace("__TOKEN__", token)
    page = Path(__file__).with_suffix('.html').read_text().replace('__TOKEN__', token)
    card_page = Path(__file__).with_name('tb_today_card.html').read_text().replace('__TOKEN__', token)

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
            if path == '/import-form':return self.reply(form_page,html=True)
            if path == '/import-ra':
                return self.reply(import_page, html=True)
            if path in ('/au-captures', '/api/au-capture', '/next-five', '/api/next-five'):
                body, content_type, status = (next_five_response if path in ('/next-five', '/api/next-five') else capture_response)(state_dir.parent, path, parse_qs(urlsplit(self.path).query))
                payload = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == '/':
                return self.reply(page, html=True)
            if path == '/today':
                return self.reply(card_page, html=True)
            if path == '/api/au-coverage':
                data = au_coverage_summary(state_dir)
                return self.reply(data, 503 if data['freshness'] == 'unavailable' else 200)
            if path == '/api/today-card':
                query = parse_qs(urlsplit(self.path).query)
                data = today_card.summary(state_dir, meeting=query.get('meeting', [''])[0], view=query.get('view', ['all'])[0])
                return self.reply(data, 503 if data['freshness'] == 'unavailable' else 200)
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
            endpoint = urlsplit(self.path).path
            if endpoint in ('/api/ra-import/preview', '/api/ra-import/commit','/api/form-import/preview','/api/form-import/commit'):
                if self.headers.get('X-Queue-Token') != token:
                    return self.reply({'error':'Reload the import page before submitting'},403)
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0 < length <= 16000000:raise ValueError('Invalid request size; maximum 16 MB')
                    if self.headers.get('Content-Type','').split(';')[0] != 'application/json':raise ValueError('Expected JSON')
                    data=json.loads(self.rfile.read(length))
                    if not isinstance(data,dict):raise ValueError('Expected an object')
                    if endpoint.startswith('/api/form-import/'):
                        result=form_importer.preview() if endpoint.endswith('/preview') else form_importer.commit(data.get('preview_id',''))
                    elif endpoint.endswith('/preview'):result=importer.preview(data.get('files'))
                    else:
                        if not isinstance(data.get('preview_id'),str):raise ValueError('Missing preview')
                        result=importer.commit(data['preview_id'])
                    return self.reply(result)
                except (ValueError,UnicodeError) as exc:return self.reply({'error':str(exc)},400)
                except OSError:return self.reply({'error':'Unable to store import'},503)
            if endpoint not in ('/api/decision', '/api/card-decision'):
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
                snapshot = None
                if endpoint == '/api/card-decision':
                    snapshot = today_card.decision_race(state_dir, market_id)
                else:
                    current = queue.load_queue_for_update(state_dir)
                    if not any(str(row.get('market_id')) == market_id for row in current.get('races', [])):
                        return self.reply({'error': 'Race is no longer in the queue. Reload the queue.'}, 409)
                result = queue.update_decision(state_dir, market_id,
                                               None if action == 'restore' else action,
                                               None if action == 'restore' else reason,
                                               race_snapshot=snapshot)
                self.reply(today_card.summary(state_dir) if endpoint == '/api/card-decision' else result)
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

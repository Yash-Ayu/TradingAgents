"""Loopback-only dashboard API for the paper application."""
from __future__ import annotations

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from .market_adapter import get_active_market_adapter, search_instruments
from .market_view import research_chart


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service, host=None, port=8765):
        self.service = service
        self.control_token = secrets.token_urlsafe(32)
        bind_host = host or os.environ.get('TRADINGAGENTS_HOST') or os.environ.get('HOST') or '127.0.0.1'
        super().__init__((bind_host, port), DashboardHandler)
        self.bind_host = bind_host
        self.origin = f'http://{bind_host}:{self.server_port}'


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, status, data, content_type='application/json'):
        try:
            payload = json.dumps(data, allow_nan=False).encode() if content_type == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def _valid_host(self):
        host_header = self.headers.get('Host', '')
        if not host_header:
            return False
        bind_host = getattr(self.server, 'bind_host', '127.0.0.1')
        if bind_host in {'0.0.0.0', '::', ''}:
            return True
        port = str(self.server.server_port)
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}', f'{bind_host}:{port}', '127.0.0.1', 'localhost', bind_host}
        allowed_hosts_env = os.environ.get('TRADINGAGENTS_ALLOWED_HOSTS', '')
        if allowed_hosts_env:
            for h in allowed_hosts_env.split(','):
                h = h.strip()
                if h:
                    allowed.add(h)
                    allowed.add(f'{h}:{port}')
                    allowed.add(f'{h}:80')
                    allowed.add(f'{h}:443')
        return host_header in allowed

    def do_GET(self):
        if not self._valid_host():
            self._send(403, {'error': 'invalid_host'})
            return
        if self.path == '/favicon.ico':
            self._send(204, b'', 'image/x-icon')
            return

        parsed_url = urlsplit(self.path)
        req_path = parsed_url.path

        if req_path == '/api/chart':
            query = parse_qs(parsed_url.query)
            symbol = query.get('symbol', [''])[0]
            interval = query.get('interval', ['5m'])[0]
            pref = query.get('source', [None])[0]
            adapter = get_active_market_adapter(pref)
            try:
                self._send(200, adapter.get_candles(symbol, interval=interval))
            except Exception:
                try:
                    self._send(200, research_chart(symbol, interval=interval))
                except Exception:
                    self._send(400, {'error': 'market_chart_unavailable'})
            return

        if req_path == '/api/instruments/search':
            query = parse_qs(parsed_url.query)
            q = query.get('q', [''])[0]
            try:
                results = search_instruments(q)
                self._send(200, {'query': q, 'results': results})
            except Exception:
                self._send(500, {'error': 'search_failed'})
            return

        if req_path == '/api/tick':
            query = parse_qs(parsed_url.query)
            symbol = query.get('symbol', [''])[0]
            adapter = get_active_market_adapter()
            try:
                quote = adapter.get_quote(symbol)
                self._send(200, quote)
            except Exception:
                self._send(400, {'error': 'quote_unavailable'})
            return

        if req_path == '/api/broker/account':
            adapter = get_active_market_adapter()
            if adapter.name == 'angel_one':
                try:
                    client = adapter._get_client()
                    profile = client.get_profile()
                    rms = client._smart_connect.rmsLimit()
                    pos = client._smart_connect.position()
                    orders = client._smart_connect.orderBook()
                    rms_data = rms.get('data', {}) if rms and rms.get('status') else {}
                    self._send(200, {
                        'mode': 'live_read_only',
                        'broker': 'Angel One SmartAPI',
                        'profile': profile.get('data', {}),
                        'funds': {
                            'available_cash': float(rms_data.get('availablecash', 0.0) or 0.0),
                            'net_margin': float(rms_data.get('net', 0.0) or 0.0),
                        },
                        'positions': pos.get('data', []) if pos and pos.get('status') else [],
                        'orders': orders.get('data', []) if orders and orders.get('status') else [],
                    })
                    return
                except Exception as exc:
                    self._send(200, {'mode': 'paper', 'broker': 'paper', 'error': str(exc)})
                    return

            self._send(200, {
                'mode': 'paper',
                'broker': 'Paper Ledger',
                'account': self.server.service.ledger.account(),
            })
            return

        if req_path == '/api/status':
            self._send(200, self.server.service.status())
            return

        assets = {'/': ('dashboard.html', 'text/html; charset=utf-8'),
                  '/app.js': ('dashboard.js', 'text/javascript; charset=utf-8'),
                  '/chart.js': ('lightweight-charts.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('dashboard.css', 'text/css; charset=utf-8')}

        if req_path not in assets:
            self._send(404, {'error': 'not_found'})
            return
        name, mime = assets[req_path]
        content = files('tradingagents.runtime').joinpath('static', name).read_text(encoding='utf-8-sig')
        content = content.replace('__CONTROL_TOKEN__', self.server.control_token)
        self._send(200, content.encode(), mime)

    def do_POST(self):
        host_header = self.headers.get('Host', '')
        port = str(self.server.server_port)
        valid_origins = {
            None,
            self.server.origin,
            f"http://{host_header}",
            f"https://{host_header}",
            f"http://{host_header}:{port}",
            f"https://{host_header}:{port}",
        }
        allowed_origins_env = os.environ.get('TRADINGAGENTS_ALLOWED_ORIGINS', '')
        if allowed_origins_env:
            for orig in allowed_origins_env.split(','):
                orig = orig.strip()
                if orig:
                    valid_origins.add(orig)
        allowed_hosts_env = os.environ.get('TRADINGAGENTS_ALLOWED_HOSTS', '')
        if allowed_hosts_env:
            for h in allowed_hosts_env.split(','):
                h = h.strip()
                if h:
                    valid_origins.add(f"http://{h}")
                    valid_origins.add(f"https://{h}")
                    valid_origins.add(f"http://{h}:{port}")
                    valid_origins.add(f"https://{h}:{port}")
        if (not self._valid_host()
                or self.headers.get('Origin') not in valid_origins
                or not secrets.compare_digest(self.headers.get('X-Control-Token', ''), self.server.control_token)):
            self._send(403, {'error': 'control_authorization_required'})
            return
        if self.headers.get('Content-Type') != 'application/json':
            self._send(415, {'error': 'json_required'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 8192:
                raise ValueError
            body = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            self._send(400, {'error': 'invalid_control_body'})
            return
        if self.path == '/api/auto/start':
            try:
                self._send(200, self.server.service.start_auto(body))
            except ValueError as exc:
                self._send(409, {'error': str(exc)})
            return
        ai_handlers = {'/api/ai/configure': self.server.service.ai.configure,
                       '/api/ai/analyze': self.server.service.ai.analyze}
        if self.path in ai_handlers:
            if self.server.service.graph_factory is not None:
                self._send(409, {'error': 'cli_engine_active_restart_without_engine_flag'})
                return
            try:
                ai_handlers[self.path](body)
                self._send(200, self.server.service.status())
            except ValueError as exc:
                self._send(409, {'error': str(exc)})
            return
        if body:
            self._send(400, {'error': 'invalid_control_body'})
            return
        if self.path == '/api/ai/disconnect':
            self.server.service.stop()
            self.server.service.ai.disconnect()
            self._send(200, self.server.service.status())
            return
        handlers = {'/api/start': self.server.service.start,
                    '/api/stop': self.server.service.stop,
                    '/api/kill': self.server.service.emergency_stop,
                    '/api/reset': self.server.service.reset_stop}
        handler = handlers.get(self.path)
        if handler is None:
            self._send(404, {'error': 'not_found'})
            return
        try:
            self._send(200, handler())
        except ValueError as exc:
            self._send(409, {'error': str(exc)})

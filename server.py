#!/usr/bin/env python3
"""
Portfolio Tracker Backend
- Static file server (local dev only)
- GET /proxy?url=...         → CORS proxy for Yahoo Finance chart
- GET /yfundamentals?symbol= → PE/FPE/PEG/EPS via yfinance
"""
import http.server
import urllib.request
import urllib.parse
import json
import os
import threading

PORT = int(os.environ.get('PORT', 3000))
DIR  = os.path.dirname(os.path.abspath(__file__))

_yf_cache = {}
_yf_lock  = threading.Lock()

def get_fundamentals(symbol):
    with _yf_lock:
        if symbol in _yf_cache:
            return _yf_cache[symbol]
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        result = {
            'pe':  info.get('trailingPE'),
            'fpe': info.get('forwardPE'),
            'peg': info.get('trailingPegRatio'),
            'eps': info.get('trailingEps'),
        }
    except Exception as e:
        result = {'error': str(e)}
    with _yf_lock:
        _yf_cache[symbol] = result
    return result


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/proxy':
            self._proxy(parsed)
        elif parsed.path == '/yfundamentals':
            self._fundamentals(parsed)
        else:
            super().do_GET()

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET')
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        target = params.get('url', [''])[0]
        if not target:
            self.send_error(400, 'Missing url parameter'); return
        try:
            req = urllib.request.Request(target, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
                              ' AppleWebKit/537.36 (KHTML, like Gecko)'
                              ' Chrome/124.0 Safari/537.36',
                'Accept': 'application/json, */*',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, str(e))

    def _fundamentals(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        symbol = params.get('symbol', [''])[0].upper()
        if not symbol:
            self.send_error(400, 'Missing symbol parameter'); return
        self._send_json(get_fundamentals(symbol))

    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    with http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler) as srv:
        print(f'Server running: http://localhost:{PORT}')
        srv.serve_forever()

#!/usr/bin/env python3
"""API рейтинга: GET /api/leaderboard, POST /api/score {token, score}."""
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get('SLG_DB', '/var/lib/slg/leaderboard.db')
CLIENT_ID = os.environ.get('SLG_GOOGLE_CLIENT_ID', '')
MAX_SCORE = 5500  # 100*(1+2+...+10)
TOP_N = 7


def db():
    conn = sqlite3.connect(DB)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS players ('
        ' sub TEXT PRIMARY KEY, name TEXT, score INTEGER,'
        ' updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    return conn


def verify(token):
    # access-токен: сверяем, что выдан нашему приложению, затем берём профиль
    url = 'https://oauth2.googleapis.com/tokeninfo?access_token=' + urllib.parse.quote(token)
    with urllib.request.urlopen(url, timeout=10) as r:
        info = json.load(r)
    if CLIENT_ID and CLIENT_ID not in (info.get('aud'), info.get('azp')):
        return None
    req = urllib.request.Request(
        'https://openidconnect.googleapis.com/v1/userinfo',
        headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=10) as r:
        prof = json.load(r)
    if 'sub' not in prof:
        return None
    return prof


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split('?')[0] == '/api/leaderboard':
            conn = db()
            rows = conn.execute(
                'SELECT name, score FROM players'
                ' ORDER BY score DESC, updated_at ASC LIMIT ?', (TOP_N,)).fetchall()
            conn.close()
            self._send(200, [{'name': n, 'score': s} for n, s in rows])
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/api/score':
            return self._send(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length))
            token = data['token']
            score = int(data['score'])
        except (KeyError, ValueError, json.JSONDecodeError):
            return self._send(400, {'error': 'bad request'})
        if not 0 <= score <= MAX_SCORE:
            return self._send(400, {'error': 'bad score'})
        try:
            info = verify(token)
        except Exception:
            info = None
        if not info:
            return self._send(401, {'error': 'auth failed'})
        name = info.get('name') or info.get('email', 'аноним').split('@')[0]
        conn = db()
        cur = conn.execute('SELECT score FROM players WHERE sub=?', (info['sub'],)).fetchone()
        best = max(score, cur[0] if cur else 0)
        conn.execute(
            'INSERT INTO players (sub, name, score) VALUES (?,?,?)'
            ' ON CONFLICT(sub) DO UPDATE SET'
            ' name=excluded.name, score=excluded.score, updated_at=CURRENT_TIMESTAMP',
            (info['sub'], name, best))
        conn.commit()
        conn.close()
        self._send(200, {'ok': True, 'best': best})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    ThreadingHTTPServer(('127.0.0.1', 8000), Handler).serve_forever()

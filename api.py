#!/usr/bin/env python3
"""API игры: рейтинг, серверные раунды, вызовы «перебей меня» и живые дуэли.

Всё — обычные короткие HTTP-запросы под /api/ (никаких вебсокетов): на проде
трафик идёт через xray → nginx-fallback → этот сервер, и такой формат проходит
гарантированно. Живая дуэль работает на short-polling; состояние матчей — в
SQLite, просроченные фазы продвигаются лениво при очередном опросе.
"""
import json
import os
import random
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get('SLG_DB', '/var/lib/slg/leaderboard.db')
CLIENT_ID = os.environ.get('SLG_GOOGLE_CLIENT_ID', '')
MAX_SCORE = 5500  # 100*(1+2+...+10), для legacy-заявок счёта
TOP_N = 7
CHARS = ['hat', 'goose', 'loh']
ATTEMPTS = 10
BASE_POINTS = 100
DUEL_ROUNDS = 10      # базовая длина дуэли
DUEL_MAX_ROUNDS = 16  # потолок вместе с «внезапной смертью» при ничьей
HIDE_T, GUESS_T, REVEAL_T = 20, 15, 5
TTL = 2 * 86400  # старые партии/вызовы/дуэли чистятся


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # транзакциями управляем явно (BEGIN IMMEDIATE)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=3000')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS players ('
        ' sub TEXT PRIMARY KEY, name TEXT, score INTEGER,'
        ' updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS games ('
        ' id TEXT PRIMARY KEY, seed INTEGER, attempt INTEGER DEFAULT 0,'
        ' score INTEGER DEFAULT 0, streak INTEGER DEFAULT 0,'
        ' done INTEGER DEFAULT 0, challenge TEXT, created REAL)')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS challenges ('
        ' id TEXT PRIMARY KEY, seed INTEGER, from_name TEXT, from_score INTEGER,'
        ' to_name TEXT, to_score INTEGER, status TEXT, created REAL)')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS duels ('
        ' id TEXT PRIMARY KEY, k1 TEXT, k2 TEXT, n1 TEXT, n2 TEXT,'
        ' round INTEGER DEFAULT 0, phase TEXT, hider INTEGER DEFAULT 2,'
        ' target TEXT, hidden INTEGER, guess INTEGER,'
        ' s1 INTEGER DEFAULT 0, s2 INTEGER DEFAULT 0,'
        ' st1 INTEGER DEFAULT 0, st2 INTEGER DEFAULT 0,'
        ' result TEXT, deadline REAL, created REAL)')
    return conn


def cleanup(conn):
    old = time.time() - TTL
    for t in ('games', 'challenges', 'duels'):
        conn.execute(f'DELETE FROM {t} WHERE created < ?', (old,))


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


def rounds_for(seed):
    """Детерминированная последовательность раундов: (цель, раскладка)×10."""
    rng = random.Random(seed)
    out = []
    for _ in range(ATTEMPTS):
        target = rng.choice(CHARS)
        layout = CHARS[:]
        rng.shuffle(layout)
        out.append((target, layout))
    return out


def clean_name(v):
    s = ''.join(ch for ch in str(v or '') if ch.isprintable()).strip()
    return s[:20] or None


def new_id():
    return secrets.token_urlsafe(6)


# ---------- дуэль: переходы состояний ----------

def duel_start_round(d, rnd):
    d['round'] = rnd
    d['hider'] = 2 if d['hider'] == 1 else 1
    d['target'] = random.choice(CHARS)
    d['hidden'] = None
    d['guess'] = None
    d['result'] = None
    d['phase'] = 'hide'
    d['deadline'] = time.time() + HIDE_T


def duel_resolve(d):
    others = [c for c in CHARS if c != d['target']]
    random.shuffle(others)
    layout = others[:]
    layout.insert(d['hidden'], d['target'])
    correct = d['guess'] == d['hidden']
    guesser = 2 if d['hider'] == 1 else 1
    winner = guesser if correct else d['hider']
    loser = 1 if winner == 2 else 2
    d[f'st{winner}'] += 1
    d[f'st{loser}'] = 0
    gain = BASE_POINTS * d[f'st{winner}']
    d[f's{winner}'] += gain
    d['result'] = json.dumps({
        'layout': layout, 'hidden': d['hidden'], 'guess': d['guess'],
        'winner': winner, 'gain': gain, 'correct': correct})
    d['phase'] = 'reveal'
    d['deadline'] = time.time() + REVEAL_T


def duel_next(d):
    r = d['round'] + 1
    if (r > DUEL_ROUNDS and d['s1'] != d['s2']) or r > DUEL_MAX_ROUNDS:
        d['phase'] = 'done'
        d['deadline'] = None
    else:
        duel_start_round(d, r)


def duel_advance(d):
    """Ленивое продвижение по таймаутам: не сходил вовремя — ход за игрока."""
    guard = 0
    while (d['deadline'] and time.time() > d['deadline']
           and d['phase'] not in ('wait', 'done') and guard < 64):
        guard += 1
        if d['phase'] == 'hide':
            d['hidden'] = random.randrange(3)
            d['phase'] = 'guess'
            d['deadline'] = time.time() + GUESS_T
        elif d['phase'] == 'guess':
            d['guess'] = random.randrange(3)
            duel_resolve(d)
        elif d['phase'] == 'reveal':
            duel_next(d)


DUEL_FIELDS = ('round', 'phase', 'hider', 'target', 'hidden', 'guess',
               's1', 's2', 'st1', 'st2', 'result', 'deadline')


def duel_save(conn, d):
    conn.execute(
        'UPDATE duels SET ' + ', '.join(f'{f}=?' for f in DUEL_FIELDS) + ' WHERE id=?',
        [d[f] for f in DUEL_FIELDS] + [d['id']])


def duel_view(d, player):
    now = time.time()
    v = {
        'round': d['round'], 'phase': d['phase'], 'you': player,
        'hider': d['hider'], 'names': [d['n1'], d['n2'] or ''],
        'scores': [d['s1'], d['s2']], 'streaks': [d['st1'], d['st2']],
        'target': d['target'], 'total': DUEL_ROUNDS,
        'left': max(0, round((d['deadline'] or now) - now)),
    }
    if d['phase'] in ('reveal', 'done') and d['result']:
        v['result'] = json.loads(d['result'])
    if d['phase'] == 'done':
        v['winner'] = 0 if d['s1'] == d['s2'] else (1 if d['s1'] > d['s2'] else 2)
    return v


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get('Content-Length', 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    # ---------- GET ----------

    def do_GET(self):
        path, _, query = self.path.partition('?')
        seg = [s for s in path.split('/') if s]
        q = urllib.parse.parse_qs(query)
        if seg == ['api', 'leaderboard']:
            return self.get_leaderboard()
        if len(seg) == 3 and seg[:2] == ['api', 'challenge']:
            return self.get_challenge(seg[2])
        if len(seg) == 4 and seg[:2] == ['api', 'duel'] and seg[3] == 'state':
            return self.get_duel_state(seg[2], (q.get('key') or [''])[0])
        self._send(404, {'error': 'not found'})

    def get_leaderboard(self):
        conn = db()
        rows = conn.execute(
            'SELECT name, score FROM players'
            ' ORDER BY score DESC, updated_at ASC LIMIT ?', (TOP_N,)).fetchall()
        conn.close()
        self._send(200, [{'name': r['name'], 'score': r['score']} for r in rows])

    def get_challenge(self, cid):
        conn = db()
        c = conn.execute('SELECT * FROM challenges WHERE id=?', (cid,)).fetchone()
        conn.close()
        if not c:
            return self._send(404, {'error': 'not found'})
        self._send(200, {
            'from_name': c['from_name'], 'from_score': c['from_score'],
            'to_name': c['to_name'], 'to_score': c['to_score'], 'status': c['status']})

    def get_duel_state(self, did, key):
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM duels WHERE id=?', (did,)).fetchone()
        if not row:
            conn.execute('COMMIT')
            conn.close()
            return self._send(404, {'error': 'not found'})
        d = dict(row)
        player = 1 if key == d['k1'] else 2 if key and key == d['k2'] else None
        if not player:
            conn.execute('COMMIT')
            conn.close()
            return self._send(403, {'error': 'not your duel'})
        before = [d[f] for f in DUEL_FIELDS]
        duel_advance(d)
        if [d[f] for f in DUEL_FIELDS] != before:
            duel_save(conn, d)
        conn.execute('COMMIT')
        conn.close()
        self._send(200, duel_view(d, player))

    # ---------- POST ----------

    def do_POST(self):
        seg = [s for s in self.path.split('?')[0].split('/') if s]
        try:
            data = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {'error': 'bad request'})
        try:
            if seg == ['api', 'score']:
                return self.post_score(data)
            if seg == ['api', 'game']:
                return self.post_game()
            if len(seg) == 4 and seg[:2] == ['api', 'game'] and seg[3] == 'pick':
                return self.post_pick(seg[2], data)
            if seg == ['api', 'challenge']:
                return self.post_challenge(data)
            if len(seg) == 4 and seg[:2] == ['api', 'challenge'] and seg[3] == 'accept':
                return self.post_challenge_accept(seg[2], data)
            if seg == ['api', 'duel']:
                return self.post_duel(data)
            if len(seg) == 4 and seg[:2] == ['api', 'duel']:
                return self.post_duel_action(seg[2], seg[3], data)
        except (KeyError, TypeError, ValueError):
            return self._send(400, {'error': 'bad request'})
        self._send(404, {'error': 'not found'})

    # очки в рейтинг: по id серверной партии (честно) или сырым числом (legacy)
    def post_score(self, data):
        token = data['token']
        game_id = data.get('game')
        conn = db()
        if game_id:
            g = conn.execute(
                'SELECT score, done FROM games WHERE id=?', (game_id,)).fetchone()
            if not g or not g['done']:
                conn.close()
                return self._send(400, {'error': 'bad game'})
            score = g['score']
        else:
            score = int(data['score'])
            if not 0 <= score <= MAX_SCORE:
                conn.close()
                return self._send(400, {'error': 'bad score'})
        try:
            info = verify(token)
        except Exception:
            info = None
        if not info:
            conn.close()
            return self._send(401, {'error': 'auth failed'})
        name = info.get('name') or info.get('email', 'аноним').split('@')[0]
        cur = conn.execute(
            'SELECT score FROM players WHERE sub=?', (info['sub'],)).fetchone()
        best = max(score, cur['score'] if cur else 0)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'INSERT INTO players (sub, name, score) VALUES (?,?,?)'
            ' ON CONFLICT(sub) DO UPDATE SET'
            ' name=excluded.name, score=excluded.score, updated_at=CURRENT_TIMESTAMP',
            (info['sub'], name, best))
        conn.execute('COMMIT')
        conn.close()
        self._send(200, {'ok': True, 'best': best})

    def post_game(self, seed=None, challenge=None):
        conn = db()
        cleanup(conn)
        gid = new_id()
        seed = seed if seed is not None else random.getrandbits(48)
        conn.execute(
            'INSERT INTO games (id, seed, challenge, created) VALUES (?,?,?,?)',
            (gid, seed, challenge, time.time()))
        conn.close()
        self._send(200, {'game': gid, 'target': rounds_for(seed)[0][0]})

    def post_pick(self, gid, data):
        pos = int(data['pos'])
        if not 0 <= pos <= 2:
            return self._send(400, {'error': 'bad pos'})
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        g = conn.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone()
        if not g or g['done']:
            conn.execute('COMMIT')
            conn.close()
            return self._send(404 if not g else 409, {'error': 'no active game'})
        target, layout = rounds_for(g['seed'])[g['attempt']]
        win = layout[pos] == target
        attempt = g['attempt'] + 1
        streak = g['streak'] + 1 if win else 0
        gain = BASE_POINTS * streak if win else 0
        score = g['score'] + gain
        done = attempt >= ATTEMPTS
        conn.execute(
            'UPDATE games SET attempt=?, score=?, streak=?, done=? WHERE id=?',
            (attempt, score, streak, int(done), gid))
        if done and g['challenge']:
            conn.execute(
                'UPDATE challenges SET to_score=?, status=? WHERE id=? AND status=?',
                (score, 'done', g['challenge'], 'playing'))
        conn.execute('COMMIT')
        conn.close()
        resp = {'layout': layout, 'win': win, 'gain': gain,
                'score': score, 'streak': streak, 'attempt': attempt, 'done': done}
        if not done:
            resp['next_target'] = rounds_for(g['seed'])[attempt][0]
        self._send(200, resp)

    def post_challenge(self, data):
        name = clean_name(data.get('name'))
        if not name:
            return self._send(400, {'error': 'bad name'})
        conn = db()
        g = conn.execute('SELECT * FROM games WHERE id=?', (data['game'],)).fetchone()
        if not g or not g['done']:
            conn.close()
            return self._send(400, {'error': 'game not finished'})
        cid = new_id()
        conn.execute(
            'INSERT INTO challenges (id, seed, from_name, from_score, status, created)'
            ' VALUES (?,?,?,?,?,?)',
            (cid, g['seed'], name, g['score'], 'open', time.time()))
        conn.close()
        self._send(200, {'id': cid})

    def post_challenge_accept(self, cid, data):
        name = clean_name(data.get('name'))
        if not name:
            return self._send(400, {'error': 'bad name'})
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        c = conn.execute('SELECT * FROM challenges WHERE id=?', (cid,)).fetchone()
        if not c or c['status'] == 'done':
            conn.execute('COMMIT')
            conn.close()
            return self._send(404 if not c else 409, {'error': 'challenge unavailable'})
        gid = new_id()
        conn.execute(
            'INSERT INTO games (id, seed, challenge, created) VALUES (?,?,?,?)',
            (gid, c['seed'], cid, time.time()))
        conn.execute(
            'UPDATE challenges SET to_name=?, status=? WHERE id=?',
            (name, 'playing', cid))
        conn.execute('COMMIT')
        conn.close()
        self._send(200, {'game': gid, 'target': rounds_for(c['seed'])[0][0],
                         'from_name': c['from_name'], 'from_score': c['from_score']})

    def post_duel(self, data):
        name = clean_name(data.get('name'))
        if not name:
            return self._send(400, {'error': 'bad name'})
        conn = db()
        cleanup(conn)
        did, key = new_id(), secrets.token_urlsafe(16)
        conn.execute(
            'INSERT INTO duels (id, k1, n1, phase, created) VALUES (?,?,?,?,?)',
            (did, key, name, 'wait', time.time()))
        conn.close()
        self._send(200, {'id': did, 'key': key, 'player': 1})

    def post_duel_action(self, did, action, data):
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM duels WHERE id=?', (did,)).fetchone()
        if not row:
            conn.execute('COMMIT')
            conn.close()
            return self._send(404, {'error': 'not found'})
        d = dict(row)

        def finish(code, obj):
            duel_save(conn, d)
            conn.execute('COMMIT')
            conn.close()
            self._send(code, obj)

        if action == 'join':
            name = clean_name(data.get('name'))
            if not name:
                conn.execute('COMMIT')
                conn.close()
                return self._send(400, {'error': 'bad name'})
            if d['phase'] != 'wait':
                conn.execute('COMMIT')
                conn.close()
                return self._send(409, {'error': 'duel is full'})
            key = secrets.token_urlsafe(16)
            conn.execute('UPDATE duels SET k2=?, n2=? WHERE id=?', (key, name, did))
            d['k2'], d['n2'] = key, name
            duel_start_round(d, 1)
            return finish(200, {'id': did, 'key': key, 'player': 2})

        key = data.get('key', '')
        player = 1 if key == d['k1'] else 2 if key and key == d['k2'] else None
        if not player:
            conn.execute('COMMIT')
            conn.close()
            return self._send(403, {'error': 'not your duel'})
        duel_advance(d)
        pos = int(data.get('pos', -1))
        guesser = 2 if d['hider'] == 1 else 1
        if (action == 'hide' and d['phase'] == 'hide'
                and player == d['hider'] and 0 <= pos <= 2):
            d['hidden'] = pos
            d['phase'] = 'guess'
            d['deadline'] = time.time() + GUESS_T
            return finish(200, duel_view(d, player))
        if (action == 'guess' and d['phase'] == 'guess'
                and player == guesser and 0 <= pos <= 2):
            d['guess'] = pos
            duel_resolve(d)
            return finish(200, duel_view(d, player))
        # ход невпопад (фаза уже ушла по таймауту и т.п.) — просто вернуть состояние
        return finish(200, duel_view(d, player))

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    ThreadingHTTPServer(('127.0.0.1', 8000), Handler).serve_forever()

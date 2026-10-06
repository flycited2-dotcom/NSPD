import json
import sqlite3
import threading
import hashlib
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
LOCK = threading.RLock()


def atomic_write(path, raw):
    """Keep the previous file intact if a source/cache write cannot finish."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    DATA.mkdir(exist_ok=True)
    c = sqlite3.connect(DATA / 'land.sqlite', timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS layers (project TEXT, role TEXT, body TEXT, PRIMARY KEY(project,role));
        CREATE TABLE IF NOT EXISTS candidates (project TEXT, id TEXT, hash TEXT, body TEXT, PRIMARY KEY(project,id), UNIQUE(project,hash));
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, project TEXT, at TEXT, kind TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, body TEXT);
        ''')


def event(db, project, kind, body):
    db.execute('INSERT INTO events(project,at,kind,body) VALUES(?,?,?,?)', (project, now(), kind, json.dumps(body, ensure_ascii=False)))


def layers(project):
    with connect() as db:
        return [json.loads(r[0]) for r in db.execute('SELECT body FROM layers WHERE project=? ORDER BY role', (project,))]


def save_layer(project, layer):
    with LOCK, connect() as db:
        raw = json.dumps(layer, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        snapshots = DATA / 'layer_snapshots'
        snapshots.mkdir(parents=True, exist_ok=True)
        snapshot = snapshots / (digest + '.json')
        if not snapshot.exists():
            snapshot.write_text(raw, encoding='utf-8')
        db.execute('INSERT OR REPLACE INTO layers VALUES(?,?,?)', (project, layer['role'], raw))
        # Imported revisions invalidate prior green checks, while keeping the evidence trail.
        for r in db.execute('SELECT id,body FROM candidates WHERE project=?', (project,)).fetchall():
            c = json.loads(r['body'])
            c['stale'] = True
            db.execute('UPDATE candidates SET body=? WHERE project=? AND id=?', (json.dumps(c, ensure_ascii=False), project, r['id']))
        event(db, project, 'layer_import', {'role': layer['role'], 'metadata': layer['metadata'], 'count': len(layer['geojson']['features']), 'snapshot_sha256': digest})


def candidates(project):
    with connect() as db:
        return [json.loads(r[0]) for r in db.execute('SELECT body FROM candidates WHERE project=? ORDER BY id', (project,))]


def save_results(project, results, summary, expected_fingerprint=None):
    with LOCK, connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if expected_fingerprint is not None:
            current = [json.loads(r[0]) for r in db.execute('SELECT body FROM layers WHERE project=? ORDER BY role', (project,))]
            actual = hashlib.sha256(json.dumps(current, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if actual != expected_fingerprint:
                raise ValueError('Слои изменились во время поиска. Запустите расчёт повторно.')
        old = {x['geometry_hash']: x for x in candidates(project)}
        sequence = max([int(x['id'].split('-')[-1]) for x in old.values()] + [0])
        for c in old.values():
            c['active'] = False
            db.execute('UPDATE candidates SET body=? WHERE project=? AND id=?', (json.dumps(c, ensure_ascii=False), project, c['id']))
        for item in results:
            previous = old.get(item['geometry_hash'])
            if previous:
                changed = previous['fingerprint'] != item['fingerprint']
                item.update({k: previous.get(k) for k in ['id', 'created_at', 'notes', 'workflow', 'checks', 'history', 'reference', 'deadline']})
                if changed:
                    item.setdefault('history', []).append({'at': now(), 'reason': 'Изменились исходные данные', 'checks': item['checks']})
                    item['checks'] = {}
            else:
                sequence += 1
                item.update(id=f'T-{sequence:03}', created_at=now(), notes='', workflow='review', checks={}, history=[], reference='', deadline='')
            item.update(updated_at=now(), active=True, stale=False)
            db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?)', (project, item['id'], item['geometry_hash'], json.dumps(item, ensure_ascii=False)))
        event(db, project, 'analysis', {**summary, 'count': len(results)})
    return candidates(project)


def update_candidate(project, cid, changes):
    with LOCK, connect() as db:
        row = db.execute('SELECT body FROM candidates WHERE project=? AND id=?', (project, cid)).fetchone()
        if not row:
            raise ValueError('Кандидат не найден')
        c = json.loads(row[0])
        c['history'].append({'at': now(), 'checks': c['checks'], 'workflow': c['workflow'], 'notes': c['notes']})
        c.update(changes, updated_at=now())
        db.execute('UPDATE candidates SET body=? WHERE project=? AND id=?', (json.dumps(c, ensure_ascii=False), project, cid))
        event(db, project, 'candidate_update', {'id': cid, 'changes': changes})
        return c


def get_setting(key, default=None):
    with connect() as db:
        row = db.execute('SELECT body FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default


def set_setting(key, value):
    with LOCK, connect() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))


def events(project):
    with connect() as db:
        return [dict(r, body=json.loads(r['body'])) for r in db.execute('SELECT * FROM events WHERE project=? ORDER BY id DESC LIMIT 100', (project,))]

import json
import urllib.request

base = 'http://127.0.0.1:8765'
try:
    with urllib.request.urlopen(base + '/api/session', timeout=2) as r:
        session = json.load(r)
    if session.get('app') == 'land-recon':
        req = urllib.request.Request(base + '/api/shutdown', data=b'{}', headers={'Content-Type':'application/json', 'X-Local-Token':session['token']})
        urllib.request.urlopen(req, timeout=3).close()
except OSError:
    pass

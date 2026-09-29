"""Double-click entry point; helper server runs without a console window."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'


def healthy():
    try:
        with urllib.request.urlopen(URL + '/api/session', timeout=1) as r:
            return json.load(r).get('app') == 'land-recon'
    except Exception:
        return False


def main():
    os.chdir(ROOT)
    envpython = ROOT / '.venv/Scripts/python.exe'
    if not envpython.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
        subprocess.run([str(envpython), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
    (ROOT / 'data').mkdir(exist_ok=True)
    if not healthy():
        with (ROOT / 'data/server.log').open('a', encoding='utf-8') as out, (ROOT / 'data/server-errors.log').open('a', encoding='utf-8') as err:
            p = subprocess.Popen([str(envpython), '-u', str(ROOT / 'app.py')], cwd=ROOT,
                                 stdout=out, stderr=err, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                 env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        for _ in range(30):
            if healthy():
                break
            if p.poll() is not None:
                raise RuntimeError('Приложение не запустилось. Проверьте data/server-errors.log и порт 8765.')
            time.sleep(.2)
        else:
            raise RuntimeError('Нет ответа приложения. Проверьте data/server-errors.log.')
    webbrowser.open(URL)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        (ROOT / 'data').mkdir(exist_ok=True)
        (ROOT / 'data/launcher-error.txt').write_text(str(exc), encoding='utf-8')
        # Open a local diagnostic file rather than leaving a silent failed launch.
        if hasattr(os, 'startfile'):
            os.startfile(str(ROOT / 'data/launcher-error.txt'))
        raise

"""Preview wrapper: uses the real dashboard with separate local storage."""
from pathlib import Path
import runpy
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# No external service requests in this preview, including credentials typed into forms.
if not getattr(socket.socket, '_pricepilot_preview_only', False):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise OSError('Collegamenti esterni disabilitati nella prova locale.')
        return original_connect(sock, address)

    def local_only_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise OSError('Collegamenti esterni disabilitati nella prova locale.')
        return original_connect_ex(sock, address)

    socket.socket.connect = local_only
    socket.socket.connect_ex = local_only_ex
    socket.socket._pricepilot_preview_only = True

from pricepilot.core import config
preview_dir = ROOT / 'data' / 'local-preview'
preview_dir.mkdir(parents=True, exist_ok=True)
config.CONFIG_FILE = preview_dir / 'config.json'
config.CONFIG['db_path'] = str(preview_dir / 'preview.db')

import streamlit as st
st.info('PROVA LOCALE PRELAUNCH · account e dati separati · collegamenti esterni disabilitati. '
        'I calendari reali appariranno dopo la configurazione. Alcune schermate conservano ancora testi della versione precedente.')
runpy.run_path(str(ROOT / 'pricepilot' / 'dashboard' / 'app.py'), run_name='__main__')

"""Launch the actual dashboard in a separate, local-only preview environment."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def preview_environment():
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith(('PRICEPILOT_', 'SUPABASE_', 'STRIPE_', 'TELEGRAM_',
                           'AIRBNB_', 'VRBO_', 'SMOOBU_', 'BEDS24_', 'TICKETMASTER_')):
            environment.pop(key)
    environment.update(PRICEPILOT_ENV='development', PRICEPILOT_TESTING='1',
        PRICEPILOT_AUTH_MODE='local', PRICEPILOT_DATABASE_BACKEND='sqlite',
        PRICEPILOT_DATA_PROVIDER='unconfigured', PRICEPILOT_PRICING_BASIS='calendar_only',
        PRICEPILOT_ALLOW_CHANNEL_WRITES='0', PRICEPILOT_RUNTIME='dashboard')
    return environment


if __name__ == '__main__':
    print('PricePilot Prelaunch - prova locale', flush=True)
    print('Apri http://127.0.0.1:8511 nel browser. Crea un account locale di prova.', flush=True)
    print('Dati separati in data/local-preview. Nessuna credenziale di ULTIMO PP viene caricata.', flush=True)
    print('Per chiudere: Ctrl+C in questa finestra.', flush=True)
    try:
        result = subprocess.run([sys.executable, '-m', 'streamlit', 'run',
            str(ROOT / 'scripts' / 'preview_app.py'), '--server.address', '127.0.0.1',
            '--server.port', '8511', '--server.headless', 'true',
            '--browser.gatherUsageStats', 'false'], cwd=ROOT, env=preview_environment())
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        pass

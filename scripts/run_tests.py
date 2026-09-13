"""Run isolated tests: no inherited service keys, no external network, temp DB."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(ROOT)
for key in list(os.environ):
    if key.startswith(("PRICEPILOT_", "SUPABASE_", "STRIPE_", "TELEGRAM_", "AIRBNB_", "VRBO_", "SMOOBU_", "BEDS24_", "TICKETMASTER_")):
        os.environ.pop(key)
os.environ.update(PRICEPILOT_ENV="development", PRICEPILOT_DATA_PROVIDER="demo",
                  PRICEPILOT_DATABASE_BACKEND="sqlite", PRICEPILOT_RUNTIME="api", PRICEPILOT_TESTING="1")

_connect = socket.socket.connect


def local_only(sock, address):
    if isinstance(address, tuple) and address[0] not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("External network disabled by the test runner")
    return _connect(sock, address)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pricepilot-tests-") as temporary:
        from pricepilot.core import config
        # Never read real .env/config if this runner is later used after setup.
        config.CONFIG["db_path"] = str(Path(temporary) / "suite.db")
        with patch.object(socket.socket, "connect", local_only):
            suite = unittest.defaultTestLoader.discover("tests", pattern=sys.argv[1] if len(sys.argv) > 1 else "test_*.py")
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        report = {"tests": result.testsRun, "failures": len(result.failures),
                  "errors": len(result.errors), "skipped": len(result.skipped),
                  "successful": result.wasSuccessful(), "external_network": "blocked",
                  "database": "temporary", "live_integrations_verified": False}
        (ROOT / "docs" / "test-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        sys.exit(0 if result.wasSuccessful() else 1)

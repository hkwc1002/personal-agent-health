"""Run only synthetic tests, with bytecode disabled and temporary databases."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / 'service'), str(ROOT / 'scripts')]

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='personal-agent-health-tests-') as directory:
        os.environ['HEALTH_TEST_ROOT'] = directory
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

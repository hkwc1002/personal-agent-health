import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from check_release import inspect, scan_content
from release import build


class ReleaseTest(unittest.TestCase):
    def fixture(self, root):
        root.mkdir()
        (root / 'release-files.json').write_text(json.dumps(['release-files.json','README.md']), encoding='utf-8')
        (root / 'README.md').write_text('# Example\n__REPOSITORY_URL__/blob/main/docs/INSTALL.md\n', encoding='utf-8')

    def test_rejects_files_not_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            self.fixture(root)
            (root / 'credential.txt').write_text('synthetic-private-data', encoding='utf-8')
            self.assertIn('not_allowlisted:credential.txt', inspect(root)[1])

    def test_secret_diagnostics_do_not_echo_values(self):
        synthetic = ('sk-' + 'a' * 32).encode()
        self.assertEqual(scan_content(synthetic), ['provider_key'])

    def test_bundle_has_real_repository_link_and_no_unlisted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            self.fixture(root)
            result = build('example/personal-agent-health', Path(directory) / 'out', root)
            with zipfile.ZipFile(result['archive']) as archive:
                readme = archive.read('personal-agent-health/README.md').decode()
                self.assertIn('https://github.com/example/personal-agent-health/', readme)
                self.assertNotIn('__REPOSITORY_URL__', readme)
                self.assertEqual(len(archive.namelist()), 2)
            self.assertFalse(result['published'])

    def test_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            self.fixture(root)
            (root / 'release-files.json').write_text(json.dumps(['../secret.txt']), encoding='utf-8')
            self.assertIn('unsafe_path:../secret.txt', inspect(root)[1])

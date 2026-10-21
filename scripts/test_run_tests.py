"""Verify that test scratch configuration is isolated and restored."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from run_tests import test_environment


class TestScratchEnvironment(unittest.TestCase):
    def test_child_temp_directory_and_exception_cleanup(self):
        scratch = Path(__file__).resolve().parent.parent / '.llm-output' / 'test-runner-check'
        before = {key: os.environ.get(key) for key in ('TEMP', 'TMP', 'TMPDIR')}
        previous_tempdir = tempfile.tempdir
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            with test_environment(scratch) as directory:
                self.assertEqual(Path(tempfile.gettempdir()), directory)
                child = subprocess.run(
                    [sys.executable, '-c', 'import tempfile; print(tempfile.gettempdir())'],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(Path(child.stdout.strip()), directory)
                (directory / 'scratch.txt').write_text('temporary')
                raise RuntimeError('fixture failure')
        self.assertFalse(directory.exists())
        self.assertEqual({key: os.environ.get(key) for key in before}, before)
        self.assertEqual(tempfile.tempdir, previous_tempdir)

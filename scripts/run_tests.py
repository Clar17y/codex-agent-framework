"""Run offline tests with scratch files confined to the workspace."""
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import unittest


@contextmanager
def test_environment(scratch):
    scratch = Path(scratch).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    keys = ("TEMP", "TMP", "TMPDIR")
    previous = {key: os.environ.get(key) for key in keys}
    previous_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="suite-", dir=scratch) as directory:
        try:
            for key in keys:
                os.environ[key] = directory
            tempfile.tempdir = directory
            yield Path(directory)
        finally:
            tempfile.tempdir = previous_tempdir
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main():
    scripts = Path(__file__).resolve().parent
    with test_environment(scripts.parent / ".llm-output" / "test-temp"):
        program = unittest.main(
            module=None,
            argv=[sys.argv[0], "discover", "-s", str(scripts), "-p", "test_*.py", *sys.argv[1:]],
            exit=False,
        )
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

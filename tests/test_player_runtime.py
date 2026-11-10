"""Run the shipped listening player's asynchronous lifecycle in isolation."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_listening_player_runtime():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is needed for the listening-player runtime regressions')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, '--test', str(root / 'tests' / 'player-runtime.test.cjs')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

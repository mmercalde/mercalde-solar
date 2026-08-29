import json
import os
import sys

import pytest

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT_DIR)

import config  # noqa: E402
import history  # noqa: E402


@pytest.fixture
def cfg():
    """The shipped example config, which is also the documented default."""
    return config.load(config.EXAMPLE_PATH)


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    yield c
    c.close()

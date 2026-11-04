import sqlite3
import json
import os
import sys

import pytest

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT_DIR)

import agent as agentmod  # noqa: E402
import config  # noqa: E402
import history  # noqa: E402
import weather  # noqa: E402


@pytest.fixture(autouse=True)
def no_live_files(monkeypatch, tmp_path):
    """Nothing in the suite reaches agent/data, on any test, however it is built.

    These are read at the moment of use, so a test that forgot to redirect them
    wrote to the real thing: on the KAMRUI, a fake policy_miss in the audit log
    the owner reads. Fixtures that need their own paths set them afterwards.
    """
    data = tmp_path / "no_live_files"
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    monkeypatch.setattr(config, "DB_PATH", str(data / "history.sqlite"))
    monkeypatch.setattr(config, "AUDIT_LOG", str(data / "audit.log"))
    monkeypatch.setattr(weather, "ARCHIVE_CACHE", str(data / "weather_archive.json"))


@pytest.fixture
def cfg():
    """The shipped example config, which is also the documented default.

    With the top-up window pinned to sunset, whatever hour the site is
    currently tuned to. These tests are about what the rules do, and should
    not all move the next time the owner retunes the window; the clock-time
    form of the setting has its own tests in test_policy.py, and
    test_system.py is where the manifest's own value is checked.
    """
    return dict(config.load(config.EXAMPLE_PATH), topup_earliest="sunset")


@pytest.fixture
def conn():
    c = history.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def a(cfg, conn, monkeypatch, tmp_path):
    # Agent resolves a connection per thread; hand every thread the same
    # in-memory one. `connect` is only used once, to create the schema, and
    # its result is closed, so it must not be the shared connection.
    monkeypatch.setattr(agentmod.history, "connect",
                        lambda *a, **k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(agentmod.history, "thread_connection", lambda *a, **k: conn)
    # DATA_DIR before the Agent is built, not the state path after: the guard
    # reads its state file in __init__, so redirecting it afterwards leaves
    # the test holding whatever the last real run left on disk.
    monkeypatch.setattr(agentmod.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agentmod.guardmod.Guard, "_save_state", lambda self: None)
    return agentmod.Agent(cfg, dry_run=True)

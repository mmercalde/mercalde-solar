"""The gateway's energy counters, and the burst they used to be.

72 reads at the gateway the Pi5 polls every 5 seconds. What is tested here
is not the arithmetic of the registers - `docs/energy_registers.md` and the
live gateway settled that - but the three things that keep the burst from
costing the Pi5 a register: it is spaced, it is off the tick, and it stands
down while the Pi5 is already losing reads.
"""

import time

import agent as agentmod
import counters
import history


class FakeClient:
    """Records every read, and fails the ones named in `fail`."""

    def __init__(self, fail=(), value=1234):
        self.fail = set(fail)
        self.value = value
        self.reads = []

    def read_holding_register_32(self, host, port, slave, reg):
        self.reads.append((time.monotonic(), host, port, slave, reg))
        if (slave, reg) in self.fail:
            return None
        return self.value


# --- the burst --------------------------------------------------------------

def test_every_device_and_period_is_read_once(cfg):
    client = FakeClient()
    readings, failed = counters.read_all(cfg, client=client, spacing=0)
    expected = sum(len(d["bases"]) for d in counters.DEVICES) * len(counters.PERIODS)
    assert len(client.reads) == expected == 72
    assert len(readings) == expected
    assert failed == 0


def test_the_reads_are_spaced_apart(cfg):
    """The gap is what keeps this off the Pi5's poll. Without it the whole
    burst lands inside one 5-second cycle."""
    client = FakeClient()
    counters.read_all(cfg, client=client, spacing=0.01)
    gaps = [b[0] - a[0] for a, b in zip(client.reads, client.reads[1:])]
    assert len(gaps) == 71
    assert min(gaps) >= 0.01


def test_the_first_read_is_not_delayed(cfg):
    """The gap goes before each read but the first: 72 reads cost 71 gaps."""
    client = FakeClient()
    started = time.monotonic()
    counters.read_all(cfg, client=client, spacing=0.01)
    assert client.reads[0][0] - started < 0.01


def test_the_shipped_spacing_holds_the_gateway_about_eleven_seconds(cfg):
    """A number worth noticing if it is ever raised: it is gateway time
    taken away from the Pi5's poll, once an hour."""
    assert counters.READ_SPACING == 0.15
    assert 10.0 < 71 * counters.READ_SPACING < 12.0


def test_a_failed_read_is_counted_and_left_out(cfg):
    """Left out, not zeroed: a missing counter must not look like a reset one."""
    dev = counters.DEVICES[0]
    addr = dev["bases"]["load_output"] + counters.PERIOD_OFFSET["today"]
    client = FakeClient(fail=[(dev["slave"], addr)])
    readings, failed = counters.read_all(cfg, client=client, spacing=0)
    assert failed == 1
    assert (dev["name"], "load_output", "today") not in readings
    assert len(readings) == 71


def test_a_snapshot_carries_the_derived_totals_too(conn, cfg):
    client = FakeClient()
    n = counters.record(conn, cfg, ts=1000, client=client, spacing=0)
    assert n == 92          # 72 read + 20 derived
    rows = conn.execute("SELECT COUNT(*) FROM counters WHERE ts=1000").fetchone()
    assert rows[0] == 92


def test_a_run_that_reads_nothing_writes_nothing(conn, cfg):
    client = FakeClient(fail=[(d["slave"], b + o)
                              for d in counters.DEVICES
                              for b in d["bases"].values()
                              for o in counters.PERIOD_OFFSET.values()])
    assert counters.record(conn, cfg, ts=1000, client=client, spacing=0) == 0
    assert conn.execute("SELECT COUNT(*) FROM counters").fetchone()[0] == 0


def test_the_run_logs_its_duration_and_failed_reads(conn, cfg, caplog):
    dev = counters.DEVICES[0]
    addr = dev["bases"]["load_output"] + counters.PERIOD_OFFSET["today"]
    client = FakeClient(fail=[(dev["slave"], addr)])
    with caplog.at_level("INFO", logger="counters"):
        counters.record(conn, cfg, ts=1000, client=client, spacing=0)
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("counters:")]
    assert len(line) == 1
    assert "1 failed read(s)" in line[0]
    assert " s," in line[0]


# --- off the tick -----------------------------------------------------------

def test_the_tick_no_longer_reads_the_counters():
    """This is the whole point. Every "Incomplete response header" the Pi5
    logged between 2026-08-28 and 2026-09-01 landed 2.2-3.4 s after a tick,
    which is where this ran. The tick must not reach the module at all."""
    assert "counters" not in agentmod.Agent._tick.__code__.co_names
    assert "counters" in agentmod.Agent.record_counters.__code__.co_names


def test_the_counters_job_is_clear_of_the_tick_and_the_digests(cfg):
    """A 15-minute interval from process start lands on one of four phases
    over a run of restarts, and every one of them is a multiple of 15 past
    some minute - so :37 can never be a tick minute. The digests are at :00."""
    assert agentmod.COUNTERS_MINUTE == 37
    assert agentmod.COUNTERS_MINUTE % cfg["tick_minutes"] != 0
    assert agentmod.COUNTERS_MINUTE != 0


# --- standing down ----------------------------------------------------------

def _live(monkeypatch, poll_errors):
    monkeypatch.setattr(agentmod.history, "fetch_data",
                        lambda *a, **k: {"pollErrors": poll_errors})


def test_a_quiet_two_minutes_lets_the_run_go_ahead(a, conn, monkeypatch):
    now = 1_788_000_000
    for i in range(3):
        history.record_sample(conn, {"pollErrors": 0}, ts=now - 60 * i)
    _live(monkeypatch, 0)
    assert a.poll_errors_seen(now) is False
    client = FakeClient()
    monkeypatch.setattr(agentmod.counters, "record",
                        lambda *ar, **kw: 92)
    assert a.record_counters(now) == 92


def test_a_lost_read_in_the_window_stands_the_run_down(a, conn, monkeypatch):
    now = 1_788_000_000
    history.record_sample(conn, {"pollErrors": 0}, ts=now - 120)
    history.record_sample(conn, {"pollErrors": 1}, ts=now - 60)
    _live(monkeypatch, 0)
    assert a.poll_errors_seen(now) is True
    called = []
    monkeypatch.setattr(agentmod.counters, "record",
                        lambda *ar, **kw: called.append(True))
    assert a.record_counters(now) == 0
    assert called == []


def test_a_lost_read_older_than_the_window_does_not(a, conn, monkeypatch):
    now = 1_788_000_000
    history.record_sample(conn, {"pollErrors": 1}, ts=now - 121)
    _live(monkeypatch, 0)
    assert a.poll_errors_seen(now) is False


def test_the_live_reading_counts_when_no_sample_caught_it(a, conn, monkeypatch):
    """The samples are 60 s apart and a lost read is gone in 5, so the stored
    rows alone would almost never see one - which is exactly what the agent's
    3,845 samples over the first live nights showed."""
    now = 1_788_000_000
    history.record_sample(conn, {"pollErrors": 0}, ts=now - 30)
    _live(monkeypatch, 1)
    assert a.poll_errors_seen(now) is True


def test_an_unreachable_dashboard_does_not_stand_the_run_down_by_itself(a, conn,
                                                                       monkeypatch):
    """A dashboard that cannot be read says nothing about the gateway; the
    stored window still does."""
    import requests
    now = 1_788_000_000
    history.record_sample(conn, {"pollErrors": 0}, ts=now - 60)
    monkeypatch.setattr(agentmod.history, "fetch_data",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.RequestException("down")))
    assert a.poll_errors_seen(now) is False


def test_a_failing_counters_run_does_not_take_the_scheduler_down(a, conn,
                                                                 monkeypatch):
    now = 1_788_000_000
    _live(monkeypatch, 0)
    monkeypatch.setattr(agentmod.counters, "record",
                        lambda *ar, **kw: (_ for _ in ()).throw(OSError("no route")))
    assert a.record_counters(now) == 0

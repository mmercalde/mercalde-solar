"""The check that says so when llama-server has fallen back to the CPU.

On 2026-11-04 it passed with llama-server stopped: its floor was 1 GB and this box
idles at 1.69 GB. The journal warning stays the primary test, and each is tested
alone, so neither is covering for the other.
"""

import pytest

import llama_gpu_check as check

MIB = 1 << 20
IDLE = int(1.69 * 1024) * MIB     # this box with no model loaded
LOADED = int(7.6 * 1024) * MIB    # Gemma 4 E4B


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(check.config, "load", lambda: {"gpu_check_min_gtt_mib": 5000})
    monkeypatch.setattr(check.telegram, "configured", lambda cfg: True)
    monkeypatch.setattr(check.telegram, "send", lambda cfg, text: box.append(text))
    monkeypatch.setattr(check, "WAIT_SECONDS", 0.01)
    monkeypatch.setattr(check, "POLL_SECONDS", 0.001)
    return box


def machine(monkeypatch, gtt, fallback):
    monkeypatch.setattr(check, "gtt_used_bytes", lambda: gtt)
    monkeypatch.setattr(check, "journal_says_fallback", lambda: fallback)


def test_a_loaded_model_with_a_clean_journal_passes(monkeypatch, sent):
    machine(monkeypatch, LOADED, False)
    assert check.main() == 0
    assert sent == []


def test_idle_gtt_no_longer_passes(monkeypatch, sent):
    """The false pass of 2026-11-04: no warning, and more than the old floor."""
    machine(monkeypatch, IDLE, False)
    assert check.main() == 1
    assert "GTT in use is" in sent[0]


def test_the_journal_warning_fails_it_even_with_gtt_high(monkeypatch, sent):
    machine(monkeypatch, LOADED, True)
    assert check.main() == 1
    assert check.WARNING in sent[0]
    assert "GTT in use is" not in sent[0]


def test_unreadable_gtt_fails(monkeypatch, sent):
    machine(monkeypatch, None, False)
    assert check.main() == 1
    assert "unknown" in sent[0]


def test_the_floor_comes_from_the_config_and_defaults_to_5000():
    assert check.floor_bytes({}) == 5000 * MIB
    assert check.floor_bytes({"gpu_check_min_gtt_mib": 2000}) == 2000 * MIB


def test_a_smaller_model_can_lower_it(monkeypatch, sent):
    monkeypatch.setattr(check.config, "load", lambda: {"gpu_check_min_gtt_mib": 1000})
    machine(monkeypatch, IDLE, False)
    assert check.main() == 0


def test_the_shipped_config_carries_the_setting():
    import config
    with open(config.EXAMPLE_PATH) as f:
        assert '"gpu_check_min_gtt_mib": 5000' in f.read()

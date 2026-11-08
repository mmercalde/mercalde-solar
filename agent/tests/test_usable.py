"""usable.py: the agent's own amp-hour counter for "Battery 41% usable".

The 0 % anchor is the pack reaching 52.0 V, never a generator start; the
100 % anchor is the pack tapering at >= 59.5 V. Calibration stays inside its
bounds, the figure says when it is uncertain, and the words come out in both
languages. See docs/battery_soc_2026-11.md section 9.
"""

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request

import pytest

import ask_server
import history
import usable

H = 3600
T0 = 1_790_000_000


def rows(*segments, start=T0, step=60):
    """(ts, V, A) rows from (minutes, volts, amps) segments, 60 s apart."""
    out, t = [], start
    for minutes, v, a in segments:
        for _ in range(minutes):
            out.append((t, v, a))
            t += step
    return out


def scan_all(r, last=None, window=800, cef=1.0):
    st = usable.new_state(last, window)
    return usable.scan(r, st, window, cef), st


def put_samples(conn, r, gen_running=False):
    for ts, v, a in r:
        conn.execute("INSERT INTO samples (ts, battery_v, batt_current, batt_soc, "
                     "mep_action, kub_action) VALUES (?, ?, ?, ?, ?, ?)",
                     (ts, v, a, 80, 9 if gen_running else 10, 10))
    conn.commit()


# --- the top anchor ------------------------------------------------------------

def test_a_taper_held_four_minutes_at_59_5_is_the_top():
    found, _ = scan_all(rows((10, 58.0, 80), (6, 59.8, 20)))
    assert [f["kind"] for f in found] == ["top"]
    # Fires on the minute the hold reaches 240 s: the 5th qualifying sample.
    assert found[0]["ts"] == T0 + (10 + 4) * 60


def test_three_minutes_of_taper_is_not_enough():
    found, _ = scan_all(rows((10, 58.0, 80), (4, 59.8, 20), (10, 58.0, -30)))
    assert found == []


def test_current_at_or_above_36_a_either_way_is_not_a_taper():
    assert scan_all(rows((10, 59.8, 40)))[0] == []
    assert scan_all(rows((10, 59.8, -40)))[0] == []
    assert scan_all(rows((10, 59.4, 5)))[0] == []


def test_one_top_per_day_not_one_per_minute_of_the_hold():
    found, _ = scan_all(rows((120, 60.1, 10)))
    assert len(found) == 1
    later, _ = scan_all(rows((120, 60.1, 10), (5 * 60, 57.0, -30), (70, 60.1, 10)))
    assert len(later) == 2 and later[1]["ts"] - later[0]["ts"] >= usable.TOP_REARM_S


def test_the_top_resets_the_counter_to_the_window():
    last = {"kind": "floor", "ts": T0 - 60}
    found, st = scan_all(rows((60, 56.0, 100), (6, 59.8, 10)), last=last, window=800)
    top = found[0]
    # 59 one-minute steps at 100 A, one stepping down to 10 A, then four at
    # 10 A to complete the hold; efficiency 1.0.
    assert top["counter_ah"] == pytest.approx((59 * 100 + 55 + 4 * 10) / 60, abs=0.1)
    assert top["error_ah"] == pytest.approx(top["counter_ah"] - 800)
    assert st["ah"] == pytest.approx(800 + 10 / 60, abs=0.5)


# --- the floor anchor: 52.0 V, never a generator start ------------------------

def test_the_pack_reaching_52_0_v_is_the_floor():
    found, _ = scan_all(rows((180, 53.0, -30), (2, 52.0, -30)))
    assert [f["kind"] for f in found] == ["floor"]
    assert found[0]["battery_v"] == 52.0


def test_a_generator_start_at_a_raised_threshold_is_not_the_floor(conn, cfg):
    """The agent can raise a start to 56 V. A start at 54.0 V must not read 0 %."""
    last_top = {"kind": "top", "ts": T0 - 60}
    r = rows((240, 54.5, -30), (1, 54.0, -30), (120, 54.6, 150))
    found, st = scan_all(r, last=last_top, window=800)
    assert found == []
    assert st["ah"] > 600

    # And through the table, with the samples saying a generator is running.
    put_samples(conn, rows((6, 59.8, 10), start=T0 - 6 * 60))
    put_samples(conn, r[:241])
    put_samples(conn, r[241:], gen_running=True)
    usable.update(conn, now=r[-1][0])
    kinds = [a["kind"] for a in usable.anchors(conn)]
    assert kinds == ["top"]
    u = usable.current(conn, cfg, now=r[-1][0])
    assert u["usable_pct"] > 75


def test_the_floor_waits_two_hours_above_52_3_before_it_fires_again():
    r = rows((180, 53.0, -30), (2, 52.0, -30), (30, 52.5, 100), (5, 52.0, -30),
             (130, 53.0, 50), (2, 52.0, -30))
    found, _ = scan_all(r)
    assert [f["kind"] for f in found] == ["floor", "floor"]
    assert found[1]["ts"] - found[0]["ts"] > usable.FLOOR_REARM_S


def test_a_heavy_load_dip_to_52_is_not_the_floor():
    found, _ = scan_all(rows((180, 53.2, -30), (2, 51.9, -150), (30, 53.2, -30)))
    assert found == []


def test_the_floor_logs_the_counter_error():
    last = {"kind": "top", "ts": T0 - 60}
    found, _ = scan_all(rows((600, 55.0, -78), (1, 52.0, -30)), last=last, window=800)
    floor = found[0]
    assert floor["kind"] == "floor"
    assert floor["error_ah"] == pytest.approx(floor["counter_ah"])
    assert floor["counter_ah"] == pytest.approx(800 - 78 * 600 / 60, abs=3)
    assert floor["ah_out"] == pytest.approx(78 * 600 / 60, abs=3)


def test_charge_is_counted_at_the_efficiency_and_discharge_at_one():
    last = {"kind": "floor", "ts": T0 - 60}
    _, st = scan_all(rows((61, 54.0, 100)), last=last, window=800, cef=0.95)
    assert st["ah"] == pytest.approx(100 * 0.95, abs=0.1)
    assert st["ah_in"] == pytest.approx(100, abs=0.1)
    _, st = scan_all(rows((61, 54.0, -100)), last=last, window=800, cef=0.95)
    assert st["ah"] == pytest.approx(-100, abs=0.1)


def test_a_gap_is_not_integrated_and_is_counted():
    last = {"kind": "floor", "ts": T0 - 60}
    r = rows((10, 54.0, 100)) + rows((10, 54.0, 100), start=T0 + 2 * H)
    _, st = scan_all(r, last=last, window=800)
    assert st["gap_s"] > 3600
    assert st["ah"] == pytest.approx(100 * 18 / 60, abs=0.5)


# --- calibration bounds --------------------------------------------------------

def floor_row(ts, ah_in, ah_out, gap_s=0):
    return {"ts": ts, "kind": "floor", "ah_in": ah_in, "ah_out": ah_out, "gap_s": gap_s}


def top_row(ts, ah_in=0, ah_out=0):
    return {"ts": ts, "kind": "top", "ah_in": ah_in, "ah_out": ah_out, "gap_s": 0}


def test_too_few_pairs_keep_the_measured_defaults():
    cal = usable.calibrate([floor_row(T0, 0, 0), floor_row(T0 + 86400, 500, 480)], now=T0 + 2 * 86400)
    assert (cal["cef"], cal["cef_source"]) == (usable.DEFAULT_CEF, "measured default")
    assert (cal["window_ah"], cal["window_source"]) == (usable.DEFAULT_WINDOW_AH, "measured default")


def test_efficiency_is_fitted_from_floor_pairs():
    rows_ = [floor_row(T0 + i * 86400, 500, 480) for i in range(5)]
    cal = usable.calibrate(rows_, now=T0 + 5 * 86400)
    assert cal["cef_source"] == "fitted" and cal["cef"] == pytest.approx(0.96)


@pytest.mark.parametrize("ratio,expect", [(0.80, 0.90), (1.10, 1.00)])
def test_efficiency_stays_inside_90_to_100_percent(ratio, expect):
    rows_ = [floor_row(T0 + i * 86400, 500, 500 * ratio) for i in range(5)]
    cal = usable.calibrate(rows_, now=T0 + 5 * 86400)
    assert cal["cef"] == expect
    assert cal["cef_fitted"] == pytest.approx(ratio)


def test_pairs_with_a_gap_or_too_little_charge_do_not_fit():
    rows_ = ([floor_row(T0 + i * 86400, 500, 400, gap_s=600) for i in range(5)]
             + [floor_row(T0 + (5 + i) * 86400, 50, 40) for i in range(5)])
    assert usable.calibrate(rows_, now=T0 + 10 * 86400)["cef_source"] == "measured default"


@pytest.mark.parametrize("out,expect", [(600, 700), (1000, 900), (820, 820)])
def test_the_window_stays_inside_700_to_900_ah(out, expect):
    rows_ = []
    for i in range(3):
        rows_ += [top_row(T0 + i * 3 * 86400), floor_row(T0 + i * 3 * 86400 + 86400, 0, out)]
    cal = usable.calibrate(rows_, now=T0 + 10 * 86400)
    assert cal["window_source"] == "fitted" and cal["window_ah"] == expect


def test_a_window_pair_with_heavy_top_up_charge_is_not_used():
    rows_ = []
    for i in range(3):
        rows_ += [top_row(T0 + i * 3 * 86400), floor_row(T0 + i * 3 * 86400 + 86400, 2000, 2700)]
    assert usable.calibrate(rows_, now=T0 + 10 * 86400)["window_source"] == "measured default"


# --- the value and the uncertain flag -------------------------------------------

def test_no_anchor_yet_has_no_usable_figure(conn, cfg):
    put_samples(conn, rows((30, 54.0, -30)))
    usable.update(conn)
    u = usable.current(conn, cfg, now=T0 + 30 * 60)
    assert u["usable_pct"] is None and u["uncertain"] is True
    assert u["monitor_soc_pct"] == 80


def floor_then(conn, days):
    put_samples(conn, rows((180, 53.0, -30), (2, 52.0, -30)))
    put_samples(conn, rows((10, 54.0, 60), start=T0 + 200 * 60))
    usable.update(conn, now=T0 + 210 * 60)
    return T0 + 181 * 60 + days * 86400


def test_six_days_without_an_anchor_is_still_certain(conn, cfg):
    now = floor_then(conn, 6)
    u = usable.current(conn, cfg, now=now)
    assert u["usable_pct"] is not None and u["uncertain"] is False


def test_seven_days_without_an_anchor_is_uncertain(conn, cfg):
    now = floor_then(conn, 7)
    u = usable.current(conn, cfg, now=now)
    assert u["uncertain"] is True and "no anchor for 7 days" in u["uncertain_reason"]


def test_the_figure_counts_up_from_the_floor(conn, cfg):
    floor_then(conn, 0)
    u = usable.current(conn, cfg, now=T0 + 210 * 60)
    assert u["last_anchor"]["kind"] == "floor"
    # One minute at -30 A after the floor sample, a 19-minute gap that is
    # not integrated, then 9 one-minute steps at 60 A at the default efficiency.
    assert u["ah_above_floor"] == round(-0.5 + 9 * usable.DEFAULT_CEF)
    assert u["usable_pct"] == round(100 * u["ah_above_floor"] / usable.DEFAULT_WINDOW_AH)


def test_update_logs_every_anchor_once(conn):
    put_samples(conn, rows((180, 53.0, -30), (2, 52.0, -30), (300, 56.0, 120), (6, 59.8, 10)))
    usable.update(conn)
    usable.update(conn)
    assert [a["kind"] for a in usable.anchors(conn)] == ["floor", "top"]
    top = usable.anchors(conn)[1]
    assert top["error_ah"] is not None and top["ah_in"] > 0


# --- words ------------------------------------------------------------------------

U = {"usable_pct": 41, "monitor_soc_pct": 82, "uncertain": False}


def test_english_and_spanish_strings():
    assert usable.phrase(U, "en") == "Battery 41% usable (monitor 82%)"
    assert usable.phrase(U, "es") == "Batería 41 % útil (monitor 82 %)"


def test_spoken_strings():
    assert usable.phrase(U, "en", spoken=True) == "Battery 41 percent usable (monitor 82 percent)"
    assert usable.phrase(U, "es", spoken=True) == "Batería al 41 por ciento útil (monitor 82 por ciento)"


def test_uncertain_strings():
    u = dict(U, uncertain=True)
    assert usable.phrase(u, "en") == "Battery 41% usable, uncertain (monitor 82%)"
    assert usable.phrase(u, "es") == "Batería 41 % útil, incierto (monitor 82 %)"


def test_before_the_first_anchor_only_the_monitor_is_named():
    u = {"usable_pct": None, "monitor_soc_pct": 82, "uncertain": True}
    assert usable.phrase(u, "en") == "Battery monitor 82%"
    assert usable.phrase(u, "es") == "Batería: monitor 82 %"


def test_status_sentence_uses_the_usable_figure(a, monkeypatch):
    data = {"batteryVoltage": 54.7, "battSocBM": 82, "mppt80PVPower": 100}
    monkeypatch.setattr(history, "fetch_data", lambda cfg, timeout=10: data)
    monkeypatch.setattr(a, "usable_now", lambda d=None, now=None, conn=None: dict(U))
    assert a.status_sentence("en").startswith(
        "Battery 41 percent usable (monitor 82 percent), 54.70 volts.")
    assert a.status_sentence("es").startswith(
        "Batería al 41 por ciento útil (monitor 82 por ciento), 54.70 voltios.")


# --- the endpoint, and who must not read it ----------------------------------------

def test_get_battery_serves_the_figure(cfg, tmp_path):
    path = str(tmp_path / "h.sqlite")
    c = history.connect(path)
    put_samples(c, rows((180, 53.0, -30), (2, 52.0, -30), (10, 54.0, 60)))
    usable.update(c)
    c.close()

    def provider(conn, query):
        u = usable.current(conn, cfg, now=T0 + 200 * 60)
        u["text"] = {"en": usable.phrase(u, "en"), "es": usable.phrase(u, "es")}
        return u

    srv = ask_server.serve(cfg, lambda t, l: "", lambda conn: {}, host="127.0.0.1",
                           port=0, db_path=path, battery_provider=provider)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/battery"
        with urllib.request.urlopen(url, timeout=5) as r:
            body = json.loads(r.read().decode())
    finally:
        srv.shutdown()
    for key in ("usable_pct", "ah_above_floor", "window_ah", "last_anchor",
                "last_top", "last_floor", "uncertain"):
        assert key in body
    assert body["last_floor"]["voltage"] == 52.0
    assert body["text"]["en"].startswith("Battery ")


def test_no_decision_path_reads_the_usable_figure():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("policy.py", "guard.py", "topup.py"):
        src = open(os.path.join(here, name), encoding="utf-8").read()
        for word in ("import usable", "usablemod", "usable_pct", "usable_now"):
            assert word not in src, (name, word)


# --- the day series for the dashboard chart ------------------------------------------

def day_of(cfg, ts):
    return history.local_day(ts, cfg)


def test_day_series_has_points_per_step_and_marks_the_anchors(conn, cfg):
    start, _ = history.day_bounds(day_of(cfg, T0), cfg)
    r = rows((180, 53.0, -30), (2, 52.0, -30), (300, 56.0, 120), (6, 59.8, 10),
             (60, 58.0, -30), start=start + 60)
    put_samples(conn, r)
    usable.update(conn)
    d = usable.day_series(conn, cfg, day_of(cfg, T0), step_min=5)
    assert [a["kind"] for a in d["anchors"]] == ["floor", "top"]
    assert d["anchors"][0]["label"] == "0% (52.0 V floor)"
    assert d["anchors"][1]["label"] == "100% (tapered top)"
    # 548 one-minute samples from 00:01: one point per 5-minute step.
    assert len(d["points"]) == len({(p["ts"] - start) // 300 for p in d["points"]})
    assert 100 <= len(d["points"]) <= 112
    assert d["points"][0]["usable_pct"] is None            # before the first anchor
    at_floor = next(p for p in d["points"] if p["ts"] >= d["anchors"][0]["ts"])
    assert at_floor["usable_pct"] <= 1
    after_top = [p for p in d["points"] if p["ts"] >= d["anchors"][1]["ts"]]
    assert after_top[0]["usable_pct"] >= 99
    assert re.fullmatch(r"\d{1,2}:\d{2} [ap]m", d["points"][0]["time"])
    assert len(usable.day_series(conn, cfg, day_of(cfg, T0), step_min=1)["points"]) == len(r)


def test_day_series_carries_on_from_the_day_before(conn, cfg):
    start, _ = history.day_bounds(day_of(cfg, T0), cfg)
    put_samples(conn, rows((180, 53.0, -30), (2, 52.0, -30), (100, 54.0, 60),
                           start=start - 200 * 60))
    usable.update(conn)
    d = usable.day_series(conn, cfg, day_of(cfg, T0), step_min=5)
    assert d["anchors"] == []
    assert d["points"] and all(p["usable_pct"] is not None for p in d["points"])


def test_day_series_refuses_other_steps(conn, cfg):
    with pytest.raises(ValueError):
        usable.day_series(conn, cfg, "2026-11-08", step_min=15)


def test_get_battery_with_a_date_returns_the_day(a, cfg, tmp_path):
    path = str(tmp_path / "h2.sqlite")
    # The `a` fixture points history.connect at :memory:, so build the file here.
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(history.SCHEMA)
    put_samples(c, rows((180, 53.0, -30), (2, 52.0, -30), (10, 54.0, 60)))
    usable.update(c)
    c.close()
    srv = ask_server.serve(cfg, lambda t, l: "", lambda conn: {}, host="127.0.0.1",
                           port=0, db_path=path, battery_provider=a.battery_json)
    base = f"http://127.0.0.1:{srv.server_address[1]}/battery"
    try:
        with urllib.request.urlopen(f"{base}?date={day_of(cfg, T0)}&step=1", timeout=5) as r:
            body = json.loads(r.read().decode())
        with pytest.raises(urllib.error.HTTPError) as bad:
            urllib.request.urlopen(f"{base}?date=yesterday", timeout=5)
    finally:
        srv.shutdown()
    assert body["day"]["step_min"] == 1 and body["day"]["anchors"][0]["kind"] == "floor"
    assert "usable_pct" in body and body["day"]["tz"] == cfg["tz"]
    assert bad.value.code == 400

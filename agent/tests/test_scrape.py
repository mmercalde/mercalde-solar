"""Parsing of InsightLocal chartdata CSV, against the real header formats.

Fixtures use the exact column names the gateway returned on 2026-08-27, from
agent/data/gateway_api.json. The HTTP side needs live credentials; these
cover everything that does not.
"""

import pytest

import history
import scrape_gateway as sg

HOURS_HEADER = (
    "TIME,/SYS/DC_IN/ENERGY_HOUR(kwh),/SYS/DC_OUT/ENERGY_HOUR(kwh),"
    "/SYS/GRID_IN/ENERGY_HOUR(kwh),/SYS/GRID_OUT/ENERGY_HOUR(kwh),"
    "/SYS/LOAD/ENERGY_HOUR(kwh),/SYS/GEN/ENERGY_HOUR(kwh),"
    "/SYS/PV/ENERGY_HOUR(kwh),/SYS/BATT_CHG/ENERGY_HOUR(kwh),"
    "/SYS/BATT_INV/ENERGY_HOUR(kwh),/SYS/PV_TOTAL/ENERGY_HOUR(kwh)")

HOURS_CSV = f"""\
# Conext Gateway
{HOURS_HEADER}
2026-08-27 00:00:00,1303,0,0,0,1269,0,0,0,1304,0
2026-08-27 01:00:00,1194,0,0,0,1194,0,0,0,1200,0
2026-08-27 12:00:00,0,0,0,0,1500,500,4000,3800,0,4000
"""

MINUTES_HEADER = (
    "TIME,/SYS/LOAD/P(W),/SYS/GEN/P(W),/SYS/PV_TOTAL/P(W),"
    "/SYS/BATT1/V(V),/SYS/BATT1/I(A),/SYS/BATT1/T(degC),/SYS/BATT1/SOC(%),"
    "/SYS/BATT2/V(V),/SYS/BATT2/I(A),/SYS/BATT2/T(degC),/SYS/BATT2/SOC(%)")

MINUTES_CSV = f"""\
# Conext Gateway
{MINUTES_HEADER}
2026-08-27 00:00:00,1246,0,0,53400,-23960,29500,81,0,0,0,0
2026-08-27 00:01:00,1246,0,0,52460,-24000,29500,81,0,0,0,0
2026-08-27 00:02:00,1246,0,0,55250,-24040,29500,80,0,0,0,0
2026-08-27 01:00:00,1194,0,0,54000,60000,29500,85,0,0,0,0
"""


def hour_of(cfg, hh):
    return history.hour_floor(int(
        __import__("datetime").datetime.strptime(
            f"2026-08-27 {hh}:00:00", "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=history.tzinfo(cfg)).timestamp()))


# --- generic CSV ------------------------------------------------------------

def test_comments_and_blanks_are_dropped():
    header, rows = sg.parse_chart_csv(HOURS_CSV)
    assert header[0] == "TIME" and len(rows) == 3


def test_empty_response_is_not_an_error():
    assert sg.parse_chart_csv("") == ([], [])
    assert sg.parse_chart_csv("# nothing here\n\n") == ([], [])


def test_column_map_ignores_the_unit_suffix():
    header, _ = sg.parse_chart_csv(HOURS_CSV)
    cols = sg.column_map(header)
    assert "/SYS/LOAD/ENERGY_HOUR" in cols
    assert "/SYS/LOAD/ENERGY_HOUR(kwh)" not in cols


# --- hourly energy (the primary source) -------------------------------------

def test_hours_give_energy_per_device(cfg):
    header, rows = sg.parse_chart_csv(HOURS_CSV)
    e = sg.hours_to_energy(header, rows, cfg)
    midnight = e[hour_of(cfg, "00")]
    assert midnight["load"]["wh_out"] == 1269
    assert midnight["battery"]["wh_out"] == 1304
    assert midnight["battery"]["wh_in"] == 0
    noon = e[hour_of(cfg, "12")]
    assert noon["solar"]["wh_in"] == 4000
    assert noon["gen"]["wh_in"] == 500
    assert noon["battery"]["wh_in"] == 3800


def test_energy_values_are_watt_hours_despite_the_kwh_label(cfg):
    """Verified live: the hourly column and integrated minute power agree
    only if the raw integer is Wh."""
    header, rows = sg.parse_chart_csv(HOURS_CSV)
    e = sg.hours_to_energy(header, rows, cfg)
    assert e[hour_of(cfg, "00")]["load"]["wh_out"] == 1269  # not 1_269_000


def test_pv_total_is_preferred_over_pv(cfg):
    """Both columns exist and carry the same numbers; only one is read."""
    header, rows = sg.parse_chart_csv(HOURS_CSV)
    e = sg.hours_to_energy(header, rows, cfg)
    # /SYS/PV is 0 at noon in the fixture while /SYS/PV_TOTAL is 4000.
    assert e[hour_of(cfg, "12")]["solar"]["wh_in"] == 4000


def test_unknown_columns_are_ignored(cfg):
    header, rows = sg.parse_chart_csv(
        "TIME,/SYS/SOMETHING_NEW/ENERGY_HOUR(kwh)\n2026-08-27 00:00:00,42\n")
    assert sg.hours_to_energy(header, rows, cfg) == {}


# --- minute voltage (peak and minimum only) ---------------------------------

def test_minutes_give_voltage_statistics(cfg):
    header, rows = sg.parse_chart_csv(MINUTES_CSV)
    v = sg.minutes_to_voltage(header, rows, cfg)
    h0 = v[hour_of(cfg, "00")]
    assert h0["n"] == 3
    assert h0["min_v"] == pytest.approx(52.46)
    assert h0["max_v"] == pytest.approx(55.25)
    assert h0["mean_v"] == pytest.approx((53.400 + 52.460 + 55.250) / 3)
    assert h0["mean_a"] == pytest.approx(-24.0, abs=0.05)


def test_volts_and_amps_are_scaled_by_a_thousand(cfg):
    header, rows = sg.parse_chart_csv(MINUTES_CSV)
    v = sg.minutes_to_voltage(header, rows, cfg)
    assert 50 < v[hour_of(cfg, "01")]["mean_v"] < 60
    assert v[hour_of(cfg, "01")]["mean_a"] == pytest.approx(60.0)


def test_an_unused_battery_bank_is_skipped(cfg):
    """BATT2..BATT5 are always in the header and always zero."""
    header, rows = sg.parse_chart_csv(MINUTES_CSV)
    vi, ai, si, scale = sg._battery_columns(header, rows)
    assert header[vi] == "/SYS/BATT1/V(V)"
    assert header[ai] == "/SYS/BATT1/I(A)"
    assert header[si] == "/SYS/BATT1/SOC(%)"
    assert scale == sg.MILLI


def test_a_later_bank_is_used_when_the_first_is_empty(cfg):
    csv = (MINUTES_HEADER + "\n"
           "2026-08-27 00:00:00,1246,0,0,0,0,0,0,53400,-23960,29500,81\n")
    header, rows = sg.parse_chart_csv(csv)
    vi, _, si, _ = sg._battery_columns(header, rows)
    assert header[vi] == "/SYS/BATT2/V(V)"
    assert header[si] == "/SYS/BATT2/SOC(%)"


def test_friendly_labels_still_parse(cfg):
    """SPEC section 5's column names, in case another device's chart uses them."""
    csv = ("Date,Volts(V),Current(A)\n"
           "2026-08-27 00:00:00,52.5,-20.0\n"
           "2026-08-27 00:01:00,52.3,-20.0\n")
    header, rows = sg.parse_chart_csv(csv)
    v = sg.minutes_to_voltage(header, rows, cfg)
    h = v[hour_of(cfg, "00")]
    assert h["min_v"] == pytest.approx(52.3) and h["max_v"] == pytest.approx(52.5)


def test_no_voltage_column_yields_nothing(cfg):
    header, rows = sg.parse_chart_csv(
        "TIME,/SYS/LOAD/P(W)\n2026-08-27 00:00:00,1000\n")
    assert sg.minutes_to_voltage(header, rows, cfg) == {}


def test_hours_are_local_to_the_configured_timezone(cfg):
    header, rows = sg.parse_chart_csv(
        MINUTES_HEADER + "\n2026-08-27 13:30:00,0,0,0,54000,10000,29500,80,0,0,0,0\n")
    v = sg.minutes_to_voltage(header, rows, cfg)
    assert history.local(next(iter(v)), cfg).hour == 13


# --- empty days -------------------------------------------------------------

def test_a_day_of_all_zeros_counts_as_empty(cfg):
    header, rows = sg.parse_chart_csv(
        HOURS_HEADER + "\n2026-08-27 00:00:00,0,0,0,0,0,0,0,0,0,0\n")
    assert sg.day_is_empty(sg.hours_to_energy(header, rows, cfg))


def test_no_rows_counts_as_empty(cfg):
    assert sg.day_is_empty({})


def test_a_day_with_load_is_not_empty(cfg):
    header, rows = sg.parse_chart_csv(HOURS_CSV)
    assert not sg.day_is_empty(sg.hours_to_energy(header, rows, cfg))


# --- writing ----------------------------------------------------------------

def test_scrape_day_merges_energy_and_voltage(conn, cfg):
    class FakeGateway:
        def chartdata(self, device, instance, day, resolution="minutes"):
            return HOURS_CSV if resolution == "hours" else MINUTES_CSV

    import datetime as dt
    written = sg.scrape_day(FakeGateway(), conn, cfg, dt.date(2026, 8, 27))
    assert written > 0

    row = conn.execute(
        "SELECT * FROM hourly WHERE device='battery' AND hour_ts=?",
        (hour_of(cfg, "00"),)).fetchone()
    assert row["wh_out"] == 1304, "energy from the hours endpoint"
    assert row["min_v"] == pytest.approx(52.46), "voltage from the minutes endpoint"
    assert row["max_v"] == pytest.approx(55.25)
    assert row["source"] == "insightlocal"

    load = conn.execute(
        "SELECT * FROM hourly WHERE device='load' AND hour_ts=?",
        (hour_of(cfg, "00"),)).fetchone()
    assert load["wh_out"] == 1269 and load["mean_v"] is None


def test_scrape_day_skips_the_minute_fetch_on_an_empty_day(conn, cfg):
    calls = []

    class FakeGateway:
        def chartdata(self, device, instance, day, resolution="minutes"):
            calls.append(resolution)
            return HOURS_HEADER + "\n2026-08-27 00:00:00,0,0,0,0,0,0,0,0,0,0\n"

    import datetime as dt
    assert sg.scrape_day(FakeGateway(), conn, cfg, dt.date(2026, 8, 27)) == 0
    assert calls == ["hours"], "a second request would be wasted"


def test_scraped_rows_do_not_overwrite_live_rows(conn, cfg):
    history.put_hourly(conn, 1787961600, "battery", 54.0, 10.0, 100, 0, 53, 55, 60, "live")
    assert history.put_hourly(conn, 1787961600, "battery", 9.9, 9.9, 1, 1, 9, 9, 1,
                              "insightlocal") == 0
    row = conn.execute("SELECT * FROM hourly").fetchone()
    assert row["mean_v"] == 54.0 and row["source"] == "live"


def test_scraped_rows_fill_hours_live_sampling_missed(conn, cfg):
    assert history.put_hourly(conn, 1787961600, "battery", 52.0, -20.0, 0, 100,
                              51, 53, 60, "insightlocal") == 1
    row = conn.execute("SELECT * FROM hourly").fetchone()
    assert row["source"] == "insightlocal" and row["mean_v"] == 52.0


# --- 429 handling -----------------------------------------------------------

class Resp:
    def __init__(self, status):
        self.status_code = status


def make_gateway(cfg, slept):
    gw = sg.Gateway(cfg, sleep=slept.append)
    return gw


def test_a_429_is_waited_out_and_retried(cfg):
    slept = []
    gw = make_gateway(cfg, slept)
    seq = [Resp(429), Resp(429), Resp(200)]
    out = gw._retry_429("test", lambda: seq.pop(0))
    assert out.status_code == 200
    assert slept == [sg.RATE_LIMIT_SLEEP, sg.RATE_LIMIT_SLEEP]


def test_429_gives_up_after_six_attempts(cfg):
    slept = []
    gw = make_gateway(cfg, slept)
    with pytest.raises(sg.GatewayError) as e:
        gw._retry_429("test", lambda: Resp(429))
    assert "429" in str(e.value)
    assert len(slept) == sg.RATE_LIMIT_RETRIES - 1, "no sleep after the last try"


def test_a_non_429_response_is_returned_immediately(cfg):
    slept = []
    gw = make_gateway(cfg, slept)
    assert gw._retry_429("test", lambda: Resp(500)).status_code == 500
    assert slept == []


# --- voltage/SOC histogram --------------------------------------------------

SOC_CSV = f"""\
{MINUTES_HEADER}
2026-08-27 00:00:00,1246,0,0,52000,-24000,29500,40,0,0,0,0
2026-08-27 00:01:00,1246,0,0,52000,-24000,29500,40,0,0,0,0
2026-08-27 00:02:00,1246,0,0,52020,-24000,29500,41,0,0,0,0
2026-08-27 00:03:00,1246,0,0,55000,60000,29500,90,0,0,0,0
2026-08-27 00:04:00,1246,4000,0,52000,-24000,29500,99,0,0,0,0
"""


def test_soc_counts_bin_by_voltage(cfg):
    header, rows = sg.parse_chart_csv(SOC_CSV)
    counts = sg.minutes_to_soc_counts(header, rows)
    # 52.000 and 52.020 fall in the same 0.05 V bin.
    assert counts == {(history.soc_bin(52.0), 40): 2,
                      (history.soc_bin(52.02), 41): 1}


def test_charging_minutes_are_excluded(cfg):
    """A charging pack sits above its resting voltage."""
    header, rows = sg.parse_chart_csv(SOC_CSV)
    counts = sg.minutes_to_soc_counts(header, rows)
    assert not any(soc == 90 for _, soc in counts), "the charging row at 55 V"


def test_generator_minutes_are_excluded(cfg):
    header, rows = sg.parse_chart_csv(SOC_CSV)
    counts = sg.minutes_to_soc_counts(header, rows)
    assert not any(soc == 99 for _, soc in counts), "the row with GEN/P > 0"


def test_no_soc_column_yields_nothing(cfg):
    header, rows = sg.parse_chart_csv(
        "TIME,/SYS/BATT1/V(V),/SYS/BATT1/I(A)\n2026-08-27 00:00:00,52000,-24000\n")
    assert sg.minutes_to_soc_counts(header, rows) == {}


def test_recording_a_day_twice_does_not_double_count(conn, cfg):
    header, rows = sg.parse_chart_csv(SOC_CSV)
    counts = sg.minutes_to_soc_counts(header, rows)
    for _ in range(3):
        history.record_soc_observations(conn, "2026-08-27", counts)
    total = conn.execute("SELECT SUM(n) n FROM soc_curve").fetchone()["n"]
    assert total == 3, "two at 52.00 plus one at 52.02, however often rescraped"


def test_scrape_day_fills_the_soc_curve(conn, cfg):
    class FakeGateway:
        def chartdata(self, device, instance, day, resolution="minutes"):
            return HOURS_CSV if resolution == "hours" else SOC_CSV

    import datetime as dt
    sg.scrape_day(FakeGateway(), conn, cfg, dt.date(2026, 8, 27))
    span = history.soc_curve_span(conn)
    assert span is not None and span[2] == 3


def test_soc_only_skips_the_hours_request(conn, cfg):
    calls = []

    class FakeGateway:
        def chartdata(self, device, instance, day, resolution="minutes"):
            calls.append(resolution)
            return SOC_CSV

    import datetime as dt
    n = sg.scrape_soc_only(FakeGateway(), conn, cfg, dt.date(2026, 8, 27))
    assert calls == ["minutes"] and n == 3

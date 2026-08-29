"""Parsing and downsampling of InsightLocal chartdata CSV.

The HTTP side needs live credentials; these cover everything that does not.
"""

import history
import scrape_gateway as sg

# Shaped like a Battery Summary minute export: comment banner, header, one row
# per minute. Column names follow SPEC section 5.
CSV = """\
# Conext Gateway
# Battery 1
Date,Volts(V),Current(A),Temperature(C),State Of Charge(%)
2026-08-27 00:00:00,52.50,-20.0,24.1,71
2026-08-27 00:01:00,52.40,-20.0,24.1,71
2026-08-27 00:02:00,52.30,-20.0,24.1,70
2026-08-27 01:00:00,54.00,60.0,25.0,80
2026-08-27 01:01:00,54.20,60.0,25.0,81
"""


def test_comments_and_blanks_are_dropped():
    header, rows = sg.parse_chart_csv(CSV)
    assert header[0] == "Date"
    assert len(rows) == 5


def test_empty_response_is_not_an_error():
    assert sg.parse_chart_csv("") == ([], [])
    assert sg.parse_chart_csv("# nothing here\n\n") == ([], [])


def test_column_map_finds_quantities():
    header, _ = sg.parse_chart_csv(CSV)
    cols = sg.column_map(header)
    assert cols == {"v": 1, "a": 2, "temp": 3, "soc": 4}


def test_column_map_tolerates_spacing_and_case():
    cols = sg.column_map(["Date", "VOLTS (V)", "current (A)", "State of Charge (%)"])
    assert cols["v"] == 1 and cols["a"] == 2 and cols["soc"] == 3


def test_downsample_to_hourly(cfg):
    header, rows = sg.parse_chart_csv(CSV)
    hourly = sg.rows_to_hourly(header, rows, cfg)
    assert len(hourly) == 2, "two distinct hours in the fixture"

    h0 = hourly[min(hourly)]
    assert h0["n"] == 3
    assert round(h0["mean_v"], 3) == 52.4
    assert h0["min_v"] == 52.3 and h0["max_v"] == 52.5
    assert h0["mean_a"] == -20.0
    # Discharging: three minutes at about 52.4 V * 20 A / 60 min.
    assert h0["wh_in"] == 0
    assert round(h0["wh_out"], 1) == 52.4

    h1 = hourly[max(hourly)]
    assert h1["n"] == 2 and h1["wh_out"] == 0
    assert round(h1["wh_in"], 1) == round((54.0 + 54.2) * 60 / 60.0, 1)


def test_rows_without_voltage_are_skipped(cfg):
    header, rows = sg.parse_chart_csv(
        "Date,Volts(V),Current(A)\n"
        "2026-08-27 00:00:00,,-20.0\n"
        "2026-08-27 00:01:00,52.0,-20.0\n"
        "bad row\n")
    hourly = sg.rows_to_hourly(header, rows, cfg)
    assert sum(h["n"] for h in hourly.values()) == 1


def test_no_voltage_column_yields_nothing(cfg):
    header, rows = sg.parse_chart_csv("Date,Frequency(Hz)\n2026-08-27 00:00:00,60.0\n")
    assert sg.rows_to_hourly(header, rows, cfg) == {}


def test_hours_are_local_to_the_configured_timezone(cfg):
    """The export carries local wall-clock times; hour keys must honour cfg['tz']."""
    header, rows = sg.parse_chart_csv(
        "Date,Volts(V),Current(A)\n2026-08-27 13:30:00,54.0,10.0\n")
    hourly = sg.rows_to_hourly(header, rows, cfg)
    hour_ts = next(iter(hourly))
    assert history.local(hour_ts, cfg).hour == 13


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

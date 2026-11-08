"""The XW+ generator-AC fields ride along in samples, from /data, so the next
Kubota run can confirm 0x00A2/0x00A4/0x00AC. See docs/xwplus_gen_ac_2026-11.md."""

import sqlite3

import history

XW = {"xwPlusAcInVoltage": 241.3, "xwPlusAcInCurrent": 13.62, "xwPlusAcInPower": 3240,
      "xwPlusChargeCurrent": 54.1, "xwPlusChargePower": 2950, "xwPlusBatteryVoltage": 54.6,
      "xwPlusChargerStatus": "bulk", "kubotaChargeRateLive": 65}
COLS = ["xw_gen_ac_v", "xw_gen_ac_a", "xw_gen_ac_w", "xw_charge_dc_a", "xw_charge_dc_w",
        "xw_battery_v", "xw_charger_status", "kub_charge_rate"]


def test_a_sample_keeps_the_xwplus_fields(conn):
    history.record_sample(conn, dict(XW, batteryVoltage=54.5), ts=1_790_000_000)
    row = dict(conn.execute(f"SELECT {','.join(COLS)} FROM samples").fetchone())
    assert row == {"xw_gen_ac_v": 241.3, "xw_gen_ac_a": 13.62, "xw_gen_ac_w": 3240,
                   "xw_charge_dc_a": 54.1, "xw_charge_dc_w": 2950, "xw_battery_v": 54.6,
                   "xw_charger_status": "bulk", "kub_charge_rate": 65}


def test_a_dashboard_without_them_leaves_them_null(conn):
    history.record_sample(conn, {"batteryVoltage": 54.5}, ts=1_790_000_000)
    row = conn.execute(f"SELECT {','.join(COLS)} FROM samples").fetchone()
    assert all(v is None for v in row)


def test_an_existing_database_gains_the_columns(tmp_path):
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    # samples as it shipped before these columns: today's definition without them.
    create = history.SCHEMA.split("CREATE TABLE IF NOT EXISTS samples (")[1].split(");")[0]
    create = create.replace(""",
  xw_gen_ac_v REAL, xw_gen_ac_a REAL, xw_gen_ac_w REAL,
  xw_charge_dc_a REAL, xw_charge_dc_w REAL, xw_battery_v REAL,
  xw_charger_status TEXT, kub_charge_rate INTEGER""", "")
    assert "xw_" not in create
    old.execute(f"CREATE TABLE samples ({create})")
    old.commit()
    old.close()
    conn = history.connect(path)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(samples)")}
    assert set(COLS) <= have
    history.record_sample(conn, XW, ts=1_790_000_000)
    assert conn.execute("SELECT xw_gen_ac_w FROM samples").fetchone()[0] == 3240

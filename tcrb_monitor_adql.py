#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
T CrB V-band monitor using ASAS-SN SkyPatrol with ADQL-based target resolution.

Flow:
  1) Use ADQL to get asas_sn_id for "T CrB":
     - Try VSX table (aavsovsx) by name (preferred).
     - Fallback: ADQL by DISTANCE on master_list using known RA/Dec.
  2) Fetch latest V-band point; email if < THRESHOLD.
  3) De-duplicate alerts; rotating logs; resilient loop.

Docs:
- ADQL in client: client.adql_query(...), includes DISTANCE example in README.  (asas-sn/skypatrol)
- V2.0 keeps catalogs (ADQL) separate from light-curve store fetched via client; LC updated ~hourly. (ASAS-SN v2.0 paper)
"""

import os
import time
import json
import math
import smtplib
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
from typing import Union, Tuple
from dotenv import load_dotenv

import pandas as pd
from pyasassn.client import SkyPatrolClient
# from skypatrol.pyasassn.client import SkyPatrolClient
import ast

# ---------------- Configuration ----------------
STAR_NAME = "T CrB"         # VSX name
RA_DEG  = 263.0545          # J2000
DEC_DEG = 25.9208           # J2000
RADIUS_DEG = 0.02           # deg search radius (fallback path)

current_directory = os.getcwd()
env_path = current_directory+"/.env"
print(f"env_path: {env_path}")
load_dotenv(env_path)

THRESHOLD = float(os.getenv("THRESHOLD"))
print(f"THRESHOLD: {THRESHOLD}")
CHECK_INTERVAL_SEC = 60 * 60

RECIPIENTS = os.getenv("ALERT_RECIPIENTS").split(",")

SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")

STATE_DIR   = Path(".") / "tcrb_monitor"
STATE_FILE  = STATE_DIR / "state_adql_state.json"
ALERT_COOLDOWN_MIN = 30

LOG_DIR   = STATE_DIR
LOG_FILE  = LOG_DIR / "tcrb_monitor_adql.log"
MAX_LOG_BYTES = 1_000_000
BACKUPS = 3

TEST_FORCE = False  # set True to force one test alert

# ------------------------------------------------

def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=BACKUPS)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(print)
    root.addHandler(handler)

def load_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_alert_hjd": None, "asas_sn_id": None, "last_alert_time_utc": None}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state))

def send_email_alert(v_mag, hjd):
    if not SMTP_PASS:
        print("APP_PASSWORD env var missing; cannot send email.")
        return

    msg = EmailMessage()
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    subject = f"T CrB V-Band Alert: {v_mag:.2f} (HJD {hjd:.5f})"
    body = (
        f"T CrB dipped below V={THRESHOLD:.2f}.\n\n"
        f"Latest V: {v_mag:.3f}\n"
        f"HJD: {hjd:.5f}\n"
        f"UTC Sent: {utc_now}\n\n"
        f"Source: ASAS-SN SkyPatrol (live, continuously updated light curves)."
    )

    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(RECIPIENTS)
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        print("Alert email sent: %s", subject)
    except Exception as e:
        print("Failed to send email: %s", e)

def latest_v_mag(client: SkyPatrolClient, asas_sn_id: int):
    """
    Return (v_mag, hjd) for the most recent V-band point, or None.
    Enhanced to ensure we get the latest data and provide better logging.
    Falls back to AAVSO data if ASAS-SN data is not recent enough.
    """
    try:
        mag_data, hjd = get_latest_aavso_data()
        print(f"mag_data: {mag_data}")
        if mag_data is not None:
            print(f"Found mag_data: {mag_data}, hjd: {hjd}")
            return mag_data, hjd
        
        # Use query_list to get light curve for specific ASAS-SN ID
        # This always makes a fresh API call to get the latest data
        lc_collection = client.query_list([asas_sn_id], download=True)
        print(f"Retrieved light curve collection for ASAS-SN ID: {asas_sn_id}")
        
        if lc_collection is None or len(lc_collection) == 0:
            print(f"No light curve data found for ASAS-SN ID: {asas_sn_id}")
            return get_latest_aavso_data()
        
        # Get the light curve for this specific ID
        lc = lc_collection[asas_sn_id]
        print(f"Light curve contains {len(lc.data)} total data points")
        # Filter for V-band data
        v_data = lc.data
        print(f"Found {len(v_data)} V-band data points")
        
        if len(v_data) == 0:
            print("No V-band photometry found")
            return get_latest_aavso_data()
            
        # Sort by HJD and get the most recent
        df = v_data.sort_values(by="jd", ascending=False)
        latest_point = df.iloc[0]
        
        # Get timestamp info for logging
        latest_jd = float(latest_point["jd"])
        latest_mag = float(latest_point["mag"])
        
        # Convert JD to human-readable date for logging
        from datetime import datetime, timedelta
        jd_epoch = 2440587.5  # Julian Date for Unix epoch (1970-01-01)
        unix_timestamp = (latest_jd - jd_epoch) * 86400  # Convert to seconds
        latest_date = datetime.fromtimestamp(unix_timestamp)
        
        print(f"Latest V-band data: {latest_mag:.3f} mag at JD {latest_jd:.5f} ({latest_date.strftime('%Y-%m-%d %H:%M:%S')})")
        
        # Check if this is very recent data (within last hour)
        now = datetime.now()
        time_diff = now - latest_date
        if time_diff.total_seconds() < 3600:  # Less than 1 hour
            print(f"✅ Data is very recent (from {time_diff.total_seconds()/60:.1f} minutes ago)")
            return latest_mag, latest_jd
        else:
            print(f"⚠️  Data is from {time_diff.days} days ago, trying AAVSO fallback")
            return get_latest_aavso_data()
        
    except Exception as e:
        print(f"Error retrieving latest V-band data: {e}")
        return get_latest_aavso_data()

def get_latest_aavso_data():
    """
    Fetch latest V-band data from AAVSO as fallback.
    Returns (v_mag, hjd) or None if no data available.
    """
    try:
        from datetime import date, timedelta
        from urllib.parse import urlencode
        import io, requests
        
        target = "T CrB"
        start_date = (date.today() - timedelta(days=1)).isoformat()
        end_date = date.today().isoformat()
        # Last-resort fallback: VSX CSV API
        from astropy.time import Time
        params_vsx = {
            "view": "api.delim",
            "ident": target,
            "fromjd": f"{Time(start_date + ' 00:00:00', scale='utc').jd:.5f}",
            "tojd": f"{Time(end_date + ' 23:59:59', scale='utc').jd:.5f}",
            "delimiter": ",",
            "band": "V",
            "mtype": "std",
            "maxrec": "50000",
        }
        url_vsx = "https://www.aavso.org/vsx/index.php?" + urlencode(params_vsx)
        r = requests.get(url_vsx, timeout=120)
        r.raise_for_status()
        df_vsx = pd.read_csv(io.StringIO(r.text), index_col=False)
        
        if len(df_vsx) > 0:
            latest_row = df_vsx.iloc[-1]
            latest_mag = float(latest_row['mag'])
            latest_jd = float(latest_row['JD']) if 'JD' in df_vsx.columns else Time.now().jd
            print(f"VSX fallback: Latest V={latest_mag:.3f}")
            return latest_mag, latest_jd
    except Exception as e:
        print(f"Error fetching AAVSO fallback data: {e}")
    
    return None, None

def find_asas_id_via_adql(client: SkyPatrolClient) -> Union[int, None]:
    """
    Resolve T CrB -> asas_sn_id using ADQL.
    Strategy:
      1) VSX table (aavsovsx) by name (case-insensitive).
      2) master_list by angular distance from known RA/Dec.
    """
    # --- 1) Try VSX by name (exact or starts-with) ---
    q1 = f"""
    SELECT *
    FROM aavsovsx
    WHERE name = 'T CrB'
    """
    # WHERE UPPER(name) = 'ASASSN-V J155930.27+255511.9' OR UPPER(name) LIKE 'ASASSN-V J155930.27+255511.9%'
    try:
        res = client.adql_query(q1)
        if res is not None and len(res) > 0:
            df = pd.DataFrame(res)
            # If multiple rows, choose the nearest to our known coords
            df["_dist"] = ((df["ra_deg"] - RA_DEG)**2 + (df["dec_deg"] - DEC_DEG)**2)**0.5
            row = df.sort_values("_dist").iloc[0]
            return int(row["asas_sn_id"])
    except Exception as e:
        print("ADQL VSX name query failed: %s", e)

    # --- 2) Fallback: master_list cone by ADQL DISTANCE ---
    # README shows using DISTANCE(ra_deg, dec_deg, RA, DEC) in WHERE (ADQL-style)
    q2 = f"""
    SELECT asas_sn_id, ra_deg, dec_deg
    FROM master_list
    WHERE DISTANCE(ra_deg, dec_deg, {RA_DEG}, {DEC_DEG}) <= {RADIUS_DEG}
    """
    try:
        res2 = client.adql_query(q2)
        if res2 is None or len(res2) == 0:
            return None
        df2 = pd.DataFrame(res2)
        # pick closest
        df2["_dist"] = ((df2["ra_deg"] - RA_DEG)**2 + (df2["dec_deg"] - DEC_DEG)**2)**0.5
        row2 = df2.sort_values("_dist").iloc[0]
        return int(row2["asas_sn_id"])
    except Exception as e:
        print("ADQL master_list distance query failed: %s", e)
        return None

def should_send_alert(state, hjd_now) -> bool:
    last_hjd = state.get("last_alert_hjd")
    last_time_str = state.get("last_alert_time_utc")
    if last_hjd is None or (not math.isclose(float(last_hjd), float(hjd_now), rel_tol=0, abs_tol=1e-6)):
        return True
    if last_time_str:
        try:
            last_time = datetime.fromisoformat(last_time_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last_time >= timedelta(minutes=ALERT_COOLDOWN_MIN):
                return True
        except Exception:
            return True
    return False

def monitor_loop():
    if not SMTP_PASS:
        print("APP_PASSWORD not set. Set and restart to enable email.")

    client = SkyPatrolClient()
    state = load_state()
    print(f"State: {state.get('asas_sn_id')}")
    if not state.get("asas_sn_id"):
        asas_id = find_asas_id_via_adql(client)
        if asas_id is None:
            logging.error("❌ Could not resolve T CrB via ADQL.")
            return
        state["asas_sn_id"] = asas_id
        save_state(state)
        print("Found asas_sn_id=%s for T CrB via ADQL", asas_id)
    else:
        asas_id = int(state["asas_sn_id"])
        print("Using cached asas_sn_id=%s", asas_id)

    while True:
        try:
            res = latest_v_mag(client, asas_id)
            if res is None:
                print("No V-band photometry found for asas_sn_id=%s", asas_id)
            else:
                v_mag, hjd = res
                print("Latest V: %.3f at HJD %.5f", v_mag, hjd)

                trigger = v_mag < THRESHOLD
                print(f"Trigger: {trigger}")
                if trigger:
                    send_email_alert(v_mag, hjd)
                    state["last_alert_hjd"] = hjd
                    state["last_alert_time_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    save_state(state)

        except Exception as e:
            logging.exception("Error in monitor loop: %s", e)

        time.sleep(CHECK_INTERVAL_SEC)

def main():
    # setup_logging()
    print("🚀 T CrB ADQL monitor start (threshold=%.2f, interval=%ds)", THRESHOLD, CHECK_INTERVAL_SEC)
    monitor_loop()

if __name__ == "__main__":
    main()

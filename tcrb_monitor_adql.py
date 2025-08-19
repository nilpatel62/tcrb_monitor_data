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
    """
    # Use query_list to get light curve for specific ASAS-SN ID
    lc_collection = client.query_list([asas_sn_id], download=True)
    print(f"lc_collection: {lc_collection}")
    if lc_collection is None or len(lc_collection) == 0:
        return None
    
    # Get the light curve for this specific ID
    lc = lc_collection[asas_sn_id]
    print(f"lc: {lc}")
    # Filter for V-band data
    v_data = lc.data[lc.data['phot_filter'] == 'V']
    print(f"v_data: {v_data}")
    
    if len(v_data) == 0:
        return None
    # Sort by HJD and get the most recent
    df = v_data.sort_values(by="jd", ascending=False)
    print(f"df: {df}")
    r = df.iloc[0]
    print(f"r: {r}")
    return float(r["mag"]), float(r["jd"])

def find_asas_id_via_adql(client: SkyPatrolClient) -> Union[int, None]:
    """
    Resolve T CrB -> asas_sn_id using ADQL.
    Strategy:
      1) VSX table (aavsovsx) by name (case-insensitive).
      2) master_list by angular distance from known RA/Dec.
    """
    # --- 1) Try VSX by name (exact or starts-with) ---
    q1 = f"""
    SELECT asas_sn_id, ra_deg, dec_deg, name
    FROM aavsovsx
    WHERE UPPER(name) = 'T CRB' OR UPPER(name) LIKE 'T CRB%'
    """
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
T CrB V-band monitor using ASAS-SN SkyPatrol.

- Uses official client: https://github.com/asas-sn/skypatrol
- Fetches the latest V-band point for T CrB and emails if mag < THRESHOLD.
- Caches asas_sn_id after first cone search.
- Rate-limits duplicate alerts (ALERT_COOLDOWN_MIN).
- Rotating logs; resilient to transient API/outage issues.

Author: you + ChatGPT
"""

import os
import sys
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

import pandas as pd
from pyasassn.client import SkyPatrolClient

# ---------------- Configuration ----------------
RA_DEG  = 263.0545     # T CrB RA (J2000)
DEC_DEG = 25.9208      # T CrB Dec (J2000)
RADIUS_DEG = 0.02      # cone search radius

THRESHOLD = 18.5        # production threshold
CHECK_INTERVAL_SEC = 15 * 60  # 15 minutes

# Send to these emails
RECIPIENTS = ["nilpatel62@gmail.com"]

# Gmail creds: use App Password via env vars
SMTP_USER = "lilmcharris@gmail.com"
SMTP_PASS = "dcutaelnmlcapiwq" #os.getenv("APP_PASSWORD", "")  # export APP_PASSWORD="xxxx xxxx xxxx xxxx"

# Alert de-duplication
STATE_DIR   = Path(".") / "tcrb_monitor"
print(f"State directory: {STATE_DIR}")
STATE_FILE  = STATE_DIR / "state.json"
ALERT_COOLDOWN_MIN = 30  # don't send more than 1 alert per 30 minutes unless new obs

# Logging
LOG_DIR   = STATE_DIR
LOG_FILE  = LOG_DIR / "tcrb_monitor.log"
MAX_LOG_BYTES = 1_000_000
BACKUPS = 3

# Optional quick test knob (forces an alert if recent V ~= 10)
TEST_FORCE = False  # set True to force an alert for end-to-end email test

# ------------------------------------------------

def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=BACKUPS)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(print)
    root.addHandler(handler)
    print(f"Logging setup complete. Log file: {LOG_FILE}")

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
        f"Source: ASAS-SN SkyPatrol (live, nightly-updated light curves)."
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

def latest_v_mag(client: SkyPatrolClient, asas_sn_id: int) -> Union[Tuple[float, float], None]:
    """
    Returns (v_mag, hjd) for the most recent V-band point, or None if not found.
    """
    # Use cone_search with download=True to get lightcurve data
    print(f"Getting light curve for ASAS-SN ID: {asas_sn_id}")
    
    # First get the object's coordinates from a catalog search
    res = client.cone_search(ra_deg=RA_DEG, dec_deg=DEC_DEG, radius=RADIUS_DEG, catalog="aavsovsx")
    if res is None or len(res) == 0:
        print("No object found in cone search")
        return None
    
    # Find the specific object by asas_sn_id
    df = pd.DataFrame(res)
    target_obj = df[df['asas_sn_id'] == asas_sn_id]
    if len(target_obj) == 0:
        print(f"Object with asas_sn_id {asas_sn_id} not found in search results")
        return None
    
    # Get the lightcurve data for this specific object
    lc_collection = client.cone_search(
        ra_deg=RA_DEG, 
        dec_deg=DEC_DEG, 
        radius=RADIUS_DEG, 
        catalog="master_list",
        download=True
    )
    
    if lc_collection is None or len(lc_collection) == 0:
        print("No lightcurve data found")
        return None
    
    # Get the specific lightcurve for our target
    try:
        lc = lc_collection[asas_sn_id]
        print(f"Light curve data shape:\n {lc.data}")
        
        # Filter for V-band data only
        v_data = lc.data[lc.data['phot_filter'] == 'V']
        if len(v_data) == 0:
            print("No V-band data found")
            return None
        
        # Sort by Julian Date (jd) and get the most recent
        v_data = v_data.sort_values(by="jd", ascending=False)
        latest_row = v_data.iloc[0]
        
        print(f"Latest V-band data: mag={latest_row['mag']}, jd={latest_row['jd']}")
        return float(latest_row["mag"]), float(latest_row["jd"])
        
    except Exception as e:
        print(f"Error accessing lightcurve data: {e}")
        return None

def find_tcrb_asas_id(client: SkyPatrolClient) -> Union[int, None]:
    """
    Cone-search around T CrB coords; return first asas_sn_id.
    """
    res = client.cone_search(ra_deg=RA_DEG, dec_deg=DEC_DEG, radius=RADIUS_DEG, catalog="master_list")
    if res is None or len(res) == 0:
        return None
    # Expect a DataFrame-like with 'asas_sn_id'
    df = pd.DataFrame(res)
    return int(df.iloc[0]["asas_sn_id"])

def should_send_alert(state, hjd_now) -> bool:
    """
    De-duplicate: send if new observation (new HJD) OR cooldown expired.
    """
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
    print("Starting monitor loop")
    if not SMTP_PASS:
        print("APP_PASSWORD not set. Set and restart to enable email.")

    client = SkyPatrolClient()
    state = load_state()
    print(f"State: {state}")
    if not state.get("asas_sn_id"):
        print("No asas_sn_id found in state. Finding...")
        asas_id = find_tcrb_asas_id(client)
        if asas_id is None:
            print("No object found near RA=%.6f Dec=%.6f", RA_DEG, DEC_DEG)
            return
        state["asas_sn_id"] = asas_id
        save_state(state)
        print("Found asas_sn_id=%s for T CrB", asas_id)
    else:
        print("asas_sn_id found in state. Using cached value.")
        asas_id = int(state["asas_sn_id"])
        print("Using cached asas_sn_id=%s", asas_id)

    while True:
        try:
            print(f"Getting latest V-band magnitude for asas_sn_id: {asas_id}")
            res = latest_v_mag(client, asas_id)
            if res is None:
                print("No V-band photometry found for asas_sn_id=%s", asas_id)
            else:
                v_mag, hjd = res
                print("Latest V: %.3f at HJD %.5f", v_mag, hjd)

                trigger = (v_mag < THRESHOLD) or (TEST_FORCE and v_mag < 99.0)
                print(f"Trigger: {trigger}")
                print(f"Should send alert: {should_send_alert(state, hjd)}")
                if trigger and should_send_alert(state, hjd):
                    send_email_alert(v_mag, hjd)
                    state["last_alert_hjd"] = hjd
                    state["last_alert_time_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    save_state(state)

        except Exception as e:
            print("Error in monitor loop: %s", e)

        time.sleep(CHECK_INTERVAL_SEC)

def main():
    # setup_logging()
    print("🚀 T CrB monitor starting (threshold=%.2f, interval=%ds)", THRESHOLD, CHECK_INTERVAL_SEC)
    monitor_loop()

if __name__ == "__main__":
    main()

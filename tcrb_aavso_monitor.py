#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
T CrB monitor via AAVSO LCGv2 API:
- Queries latest Johnson V, CCD observations for T CrB
- Emails if magnitude < THRESHOLD
- De-duplicates by JD and uses a cooldown to avoid spam
Docs: AAVSO LCGv2 'api.delim' with params (ident, fromjd, tojd, RequestedBands, delimiter) and columns incl. Band & Obstype. 
"""

import os, time, json, math, smtplib, logging, requests
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
import pandas as pd

# ===================== CONFIG =====================
STAR_NAME = "T CrB"
BAND_FILTER = "V"          # Johnson V
OBSTYPE_FILTER = "CCD"     # Observation type
THRESHOLD = 8.5            # Alert threshold
CHECK_EVERY_MIN = 15       # Polling interval
ROLLING_DAYS = 14          # Window to fetch each poll

SMTP_USER = "lilmcharris@gmail.com"
SMTP_PASS = "dcutaelnmlcapiwq" #os.getenv("APP_PASSWORD", "")   # export APP_PASSWORD="xxxx xxxx xxxx xxxx"
RECIPIENTS = ["nilpatel62@gmail.com"]

STATE_DIR = os.path.expanduser("~/.tcrb_monitor")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE   = os.path.join(STATE_DIR, "tcrb_monitor.log")
ALERT_COOLDOWN_MIN = 30     # re-alert if still below after cooldown
TEST_FORCE = False          # Set True once to force a test email
# ==================================================

AAVSO_LCG_URL = "https://www.aavso.org/LCGv2/index.htm"  # documented API entry point
DELIM = "@@@"  # safe delimiter per docs

def setup_logging():
    os.makedirs(STATE_DIR, exist_ok=True)
    h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    logging.basicConfig(level=print,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[h])

def jd_utc(dt_utc: datetime) -> float:
    """Julian Date for a timezone-aware UTC datetime (no astropy needed)."""
    # Algorithm: Fliegel & Van Flandern style
    y = dt_utc.year
    m = dt_utc.month
    d = dt_utc.day + (dt_utc.hour + (dt_utc.minute + dt_utc.second/60)/60)/24
    if m <= 2:
        y -= 1
        m += 12
    A = y // 100
    B = 2 - A + (A // 25)  # Gregorian calendar correction
    jd = int(365.25*(y + 4716)) + int(30.6001*(m + 1)) + d + B - 1524.5
    return jd

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            return json.loads(open(STATE_FILE).read())
    except Exception:
        pass
    return {"last_alert_jd": None, "last_alert_time_utc": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def fetch_latest_v_ccd():
    """Return (mag, jd) for latest Johnson V, CCD point from AAVSO for STAR_NAME."""
    now = datetime.now(timezone.utc)
    tojd = jd_utc(now) + 1.0
    fromjd = tojd - ROLLING_DAYS

    params = {
        "view": "api.delim",          # documented API mode
        "DateFormat": "Julian",
        "RequestedBands": BAND_FILTER,  # limit server-side to V
        "ident": STAR_NAME,           # star name, e.g., 'T CrB'
        "fromjd": f"{fromjd:.2f}",
        "tojd":   f"{tojd:.2f}",
        "delimiter": DELIM            # use uncommon delimiter to parse safely
    }
    r = requests.get(AAVSO_LCG_URL, params=params, timeout=45)
    r.raise_for_status()
    text = r.text.strip()

    # Parse "api.delim" output: one header line + data lines, DELIM-separated.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Find header (should contain 'JD', 'Magnitude', 'Band', 'Obstype' per docs)
    header = None
    header_idx = -1
    for i, ln in enumerate(lines):
        if all(k in ln for k in ["JD", "Magnitude", "Band"]):
            header = [c.strip() for c in ln.split(DELIM)]
            header_idx = i
            break
    if not header:
        print("AAVSO response missing header; size=%d", len(lines))
        return None

    rows = []
    for ln in lines[header_idx+1:]:
        parts = [p.strip() for p in ln.split(DELIM)]
        if len(parts) < len(header):
            continue
        rec = {header[j]: parts[j] for j in range(len(header))}
        rows.append(rec)

    if not rows:
        return None

    df = pd.DataFrame(rows)

    # Normalize column names that may differ slightly
    def pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    col_jd  = pick("HJD", "JD")
    col_mag = pick("Magnitude", "Mag", "mag")
    col_band= pick("Band", "Filter")
    col_type= pick("Obstype", "Type")

    for c in (col_jd, col_mag, col_band, col_type):
        if c is None:
            print("Expected columns not found in AAVSO data.")
            return None

    # Filter to Johnson V + CCD (string guards: sometimes 'Johnson V' or 'V')
    df = df[df[col_band].astype(str).str.upper().str.contains(r"\bV\b")]
    df = df[df[col_type].astype(str).str.upper().str.contains("CCD")]

    if df.empty:
        return None

    # Coerce numeric JD/Mag
    df[col_jd]  = pd.to_numeric(df[col_jd], errors="coerce")
    df[col_mag] = pd.to_numeric(df[col_mag], errors="coerce")
    df = df.dropna(subset=[col_jd, col_mag]).sort_values(col_jd, ascending=False)

    if df.empty:
        return None

    latest = df.iloc[0]
    return float(latest[col_mag]), float(latest[col_jd])

def send_email(v_mag, jd):
    if not SMTP_PASS:
        logging.error("APP_PASSWORD env var missing; cannot send email.")
        return
    msg = EmailMessage()
    msg["Subject"] = f"T CrB alert: V={v_mag:.3f} (JD {jd:.5f})"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(RECIPIENTS)
    body = (
        f"T CrB latest Johnson V (CCD) magnitude is {v_mag:.3f} at JD {jd:.5f}.\n"
        f"Threshold: V<{THRESHOLD:.2f}\n"
        f"Source: AAVSO LCGv2 API (Johnson V, CCD filters).\n"
    )
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        print("Alert email sent.")
    except Exception as e:
        print("Failed to send email: %s", e)

def should_alert(state, jd_now):
    last_jd = state.get("last_alert_jd")
    last_t  = state.get("last_alert_time_utc")
    if (last_jd is None) or (not math.isclose(float(last_jd), float(jd_now), abs_tol=1e-6)):
        return True
    if last_t:
        try:
            prev = datetime.fromisoformat(last_t.replace("Z","+00:00"))
            if datetime.now(timezone.utc) - prev >= timedelta(minutes=ALERT_COOLDOWN_MIN):
                return True
        except Exception:
            return True
    return False

def main_loop():
    # setup_logging()
    print("🚀 T CrB AAVSO monitor (V CCD) start; threshold=%.2f; every=%d min",
                 THRESHOLD, CHECK_EVERY_MIN)
    os.makedirs(STATE_DIR, exist_ok=True)
    state = load_state()

    while True:
        try:
            out = fetch_latest_v_ccd()
            if not out:
                print("No V/CCD photometry returned this cycle.")
            else:
                v_mag, jd = out
                print("Latest Johnson V (CCD): %.3f at JD %.5f", v_mag, jd)
                trigger = (v_mag < THRESHOLD) or (TEST_FORCE and v_mag < 99)
                if trigger and should_alert(state, jd):
                    send_email(v_mag, jd)
                    state["last_alert_jd"] = jd
                    state["last_alert_time_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
                    save_state(state)
        except Exception as e:
            print("Cycle error: %s", e)

        time.sleep(CHECK_EVERY_MIN * 60)

if __name__ == "__main__":
    main_loop()

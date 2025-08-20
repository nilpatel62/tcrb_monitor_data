from datetime import date, timedelta
from urllib.parse import urlencode
import io, requests, pandas as pd

target = "T CrB"
start_date = (date.today() - timedelta(days=1)).isoformat()  # e.g. 2025-08-19
end_date   = date.today().isoformat()                        # e.g. 2025-08-20

print(f"start_date: {start_date}")
print(f"end_date: {end_date}")

# App param semantics:
# band=2 -> Johnson V, obstype=2 -> CCD   (same as the UI)
base_params = {
    "target": target,
    "start_date": start_date,
    "end_date": end_date,   # <-- explicit date (NOT 'today')
    "band": 2,
    "obstype": 2,
    "observer": "",
    "obs_campaign": "",
    "format": "csv",
}

# Known/likely download endpoints used by the app
download_paths = [
    "https://apps.aavso.org/data/download/photometry/",     # preferred
    "https://apps.aavso.org/v2/data/download/photometry/",  # alt (some deployments)
    "https://apps.aavso.org/data/search/photometry/download/",  # legacy alt
]

def try_download():
    for base in download_paths:
        url = base + "?" + urlencode(base_params, doseq=True)
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 200 and r.text.strip():
                return r.text, url
        except requests.RequestException:
            pass
    return None, None

csv_text, used_url = try_download()

if csv_text:
    df = pd.read_csv(io.StringIO(csv_text))
else:
    print(f"No data found for {target} in the specified date range.")
    print(f"Using VSX CSV API as fallback.")
    
    # --- Last-resort fallback: VSX CSV API (works reliably programmatically) ---
    from astropy.time import Time
    params_vsx = {
        "view": "api.delim",
        "ident": target,
        "fromjd": f"{Time(start_date + ' 00:00:00', scale='utc').jd:.5f}",
        "tojd": f"{Time(end_date   + ' 23:59:59', scale='utc').jd:.5f}",
        "delimiter": ",",
        "band": "V",
        "mtype": "std",
        "maxrec": "50000",
    }
    url_vsx = "https://www.aavso.org/vsx/index.php?" + urlencode(params_vsx)
    r = requests.get(url_vsx, timeout=120)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), index_col=False)
    used_url = url_vsx

print(f"Downloaded {len(df)} rows from:\n{used_url}")
# print(df.head())
df.to_csv("AAVSO_TCrB_V_CCD_lastday.csv", index=False)
print("Saved -> AAVSO_TCrB_V_CCD_lastday.csv")

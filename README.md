# T CrB Monitor

A Python script to monitor the V-band brightness of the recurrent nova T Coronae Borealis (T CrB) using ASAS-SN data and send email alerts when brightness drops below a threshold.

## Setup Instructions

### 1. Prerequisites
- Python 3.8 or 3.9 (required for pyasassn compatibility)
- Git

### 2. Environment Setup
```bash
# Run the setup script
chmod +x setup.sh
./setup.sh
```

### 3. Configuration
1. Edit the `.env` file with your Gmail credentials:
   ```env
   SMTP_USER=your_email@gmail.com
   SMTP_PASS=your_app_password_here
   ALERT_RECIPIENTS=email1@gmail.com,email2@gmail.com
   ```

2. Generate a Gmail App Password:
   - Go to Google Account settings
   - Security → 2-Step Verification → App passwords
   - Generate a new app password for "Mail"

### 4. Testing
```bash
# Activate the environment
source tcrb-env/bin/activate

# Test with a high threshold to trigger an email
python tcrb_monitor_latest.py --threshold 10.2 --interval 1
```

### 5. Production Deployment

#### Option A: Manual Run
```bash
source tcrb-env/bin/activate
python tcrb_monitor_latest.py --threshold 10.0 --interval 1
```

#### Option B: macOS launchd Service
```bash
# Copy the plist file to LaunchAgents
cp com.tcrb.monitor.plist ~/Library/LaunchAgents/

# Load the service
launchctl load ~/Library/LaunchAgents/com.tcrb.monitor.plist

# Start the service
launchctl start com.tcrb.monitor

# Check status
launchctl list | grep tcrb
```

#### Option C: Cron Job
```bash
# Add to crontab (runs every 5 minutes)
crontab -e
# Add this line:
*/5 * * * * cd /Users/neelpatel/Desktop/tcrb_monitor && ./tcrb-env/bin/python tcrb_monitor_latest.py --threshold 10.0 --interval 5
```

## Troubleshooting

### Common Issues

1. **"No module named 'pyasassn'"**
   - Make sure you're in the virtual environment: `source tcrb-env/bin/activate`

2. **"Authentication failed"**
   - Check your Gmail app password in the `.env` file
   - Ensure 2-factor authentication is enabled on your Google account

3. **"No photometry data found"**
   - The ASAS-SN API might be temporarily unavailable
   - Check the log file for detailed error messages

4. **Python version issues**
   - Ensure you're using Python 3.8 or 3.9
   - Use pyenv to manage Python versions: `pyenv install 3.9.18`

### Log Files
- Main log: `tcrb_monitor.log`
- Cache file: `tcrb_cache.json` (stores ASAS-SN ID and last alert time)

## Monitoring

The script will:
- Check T CrB brightness every minute (configurable)
- Send email alerts when V < 10.0 (configurable)
- Prevent duplicate alerts for the same observation
- Log all activities and errors
- Gracefully handle API failures and retry

## Files
- `tcrb_monitor_latest.py` - Main monitoring script
- `requirements.txt` - Python dependencies
- `.env` - Configuration (create from template)
- `setup.sh` - Automated setup script
- `com.tcrb.monitor.plist` - macOS service definition

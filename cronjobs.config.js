module.exports = {
    apps: [
        {
            name: 'tcrb-monitor',
            script: 'tcrb_monitor_adql.py',
            autorestart: false,
            watch: false,
            max_memory_restart: '1G',
            cron_restart: '0 * * * *',  // Runs every hour
        }
    ]
};
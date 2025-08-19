module.exports = {
    apps: [
        {
            name: 'tcrb-monitor',
            script: 'tcrb_monitor_adql.py',
            autorestart: false,
            watch: false,
            max_memory_restart: '1G',
            cron_restart: '*/15 * * * *',  // Runs every 15 minutes
        }
    ]
};
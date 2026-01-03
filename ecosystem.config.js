module.exports = {
  apps: [{
    name: 'hawkeye',
    script: 'main.py',
    interpreter: './venv/bin/python',
    cwd: '/www/wwwroot/binance_alert',
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
    watch: false,
    env: {
      PYTHONUNBUFFERED: '1'
    }
  }]
}
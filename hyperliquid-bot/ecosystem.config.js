module.exports = {
  apps: [
    {
      name: "razorhl",
      script: "C:\\Python313\\python.exe",
      args: "run.py",
      cwd: "C:\\Users\\User\\Documents\\Vibe Code\\RazorHL\\hyperliquid-bot",
      interpreter: "none",
      instances: 1,
      autorestart: true,
      watch: false,
      max_restarts: 10,
      min_uptime: "10s",
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      error_file: "logs/pm2-error.log",
      out_file: "logs/pm2-out.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};

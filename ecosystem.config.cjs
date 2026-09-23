module.exports = {
  apps: [
    {
      name: "jev-trade",
      cwd: "/root/ai-trading",
      script: "/root/ai-trading/.venv/bin/python",
      args: "-m app",
      interpreter: "none",
      autorestart: true,
      max_restarts: 30,
      min_uptime: "5s",
      kill_timeout: 8000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};

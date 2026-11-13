(() => {
  const labels = {
    starting: ['Starting the service', 'The HTTP server is online. The bot is starting.'],
    database: ['Preparing the database', 'The HTTP server is online. Auction data is being prepared.'],
    telegram: ['Connecting to Telegram', 'The database is ready. The bot is connecting to Telegram.'],
    ready: ['Ready for your next pick', 'The auction and draft bot is ready in Telegram.'],
    unresponsive: ['Bot response delayed', 'The server is online, but the bot heartbeat is overdue. Please retry shortly.'],
    stopping: ['Service restarting', 'The bot is stopping. Please retry shortly.']
  };
  const set = (id, text) => { document.getElementById(id).textContent = text; };
  async function refresh() {
    if (document.hidden) return;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
      const response = await fetch('/healthz', {cache: 'no-store', signal: controller.signal});
      const data = await response.json();
      const [title, description] = labels[data.status] || labels.starting;
      set('status-title', title);
      set('status-description', description);
      set('readiness', data.ready ? 'Ready' : title);
      set('uptime', data.uptime_seconds < 60 ? data.uptime_seconds + ' seconds' : Math.floor(data.uptime_seconds / 60) + ' minutes');
      document.getElementById('status-badge').dataset.state = data.ready ? 'ready' : 'starting';
      set('status-badge', data.ready ? 'Bot online' : 'HTTP server online');
    } catch {
      set('status-badge', 'Reconnecting');
      document.getElementById('status-badge').dataset.state = 'starting';
      set('status-description', 'The status check could not connect. Retrying shortly.');
    } finally {
      clearTimeout(timeout);
    }
  }
  refresh();
  setInterval(refresh, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();

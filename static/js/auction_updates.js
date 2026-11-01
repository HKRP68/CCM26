/* Authenticated change notifications; API reads remain the source of auction data. */
const AuctionUpdates = (() => {
  'use strict';
  const ENDPOINT = '/api/webapp/auction/updates';
  const MAX_EVENT_SIZE = 64 * 1024;
  const READ_TIMEOUT = 12000;

  function call(callback, value) {
    try {
      const result = callback?.(value);
      if (result?.catch) result.catch(() => {});
    } catch (_) { /* A view callback must not leave the connection running twice. */ }
  }

  function watch(options) {
    const seasonId = Number(options.seasonId);
    let stopped = false, blocked = false, running = false, resume = false;
    let controller = null, reader = null, retryTimer = null, watchdog = null;
    let generation = 0, failures = 0;
    const available = () => !stopped && !blocked && !document.hidden &&
      (typeof navigator === 'undefined' || navigator.onLine !== false);
    const status = value => { if (!stopped) call(options.onStatus, value); };
    const clearRetry = () => { clearTimeout(retryTimer); retryTimer = null; };
    const clearWatchdog = () => { clearTimeout(watchdog); watchdog = null; };
    const armWatchdog = () => {
      clearWatchdog();
      watchdog = setTimeout(() => controller?.abort(), READ_TIMEOUT);
    };

    function schedule(delay) {
      clearRetry();
      if (!available()) return;
      retryTimer = setTimeout(() => { retryTimer = null; connect(); }, delay);
    }

    function terminalAccess(payload) {
      const code = Number(payload.status);
      return [400,401,403].includes(code) ||
        (code === 503 && ['maintenance','login_unavailable'].includes(payload.error));
    }

    function accessDenied(payload) {
      const terminal = terminalAccess(payload);
      if (terminal) blocked = true;
      status({state:'fallback', ...payload});
      if (!stopped) call(options.onAccess, payload);
      controller?.abort();
      return terminal;
    }

    async function connect() {
      if (running || !available()) return;
      running = true; resume = false;
      const ticket = ++generation;
      controller = new AbortController();
      const activeController = controller;
      let delay = 1000, ended = false;
      status({state:failures ? 'reconnecting' : 'connecting'});
      armWatchdog();
      try {
        const supplied = typeof options.headers === 'function' ? options.headers() : options.headers;
        const response = await fetch(ENDPOINT, {
          method:'POST', credentials:'same-origin', cache:'no-store',
          headers:{'Content-Type':'application/json', 'Accept':'text/event-stream', ...(supplied || {})},
          body:JSON.stringify({season_id:seasonId}), signal:activeController.signal
        });
        if (stopped || ticket !== generation) return;
        if (!response.ok) {
          let payload = {};
          try { payload = await response.json(); } catch (_) {}
          payload = {...payload, status:response.status};
          if (terminalAccess(payload)) {
            accessDenied(payload);
            return;
          }
          failures++;
          delay = Math.min(15000, 1000 * 2 ** Math.min(failures - 1, 4));
          if (response.status === 503) {
            const retry = response.headers?.get?.('Retry-After');
            const seconds = Number(retry);
            const requested = retry && !Number.isFinite(seconds) ? Date.parse(retry) - Date.now() : seconds * 1000;
            delay = Math.max(delay, 5000, Number.isFinite(requested) ? Math.min(requested,60000) : 0);
          }
          status({state:'fallback', ...payload});
          return;
        }
        if (!response.body?.getReader || typeof TextDecoder === 'undefined') {
          blocked = true;
          status({state:'fallback', reason:'stream_unsupported'});
          return;
        }
        const contentType = response.headers?.get?.('Content-Type');
        if (contentType && !contentType.includes('text/event-stream')) throw new Error('Unexpected stream response');
        reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '', eventName = 'message', lines = [], eventSize = 0;

        function dispatch() {
          const name = eventName, text = lines.join('\n');
          eventName = 'message'; lines = []; eventSize = 0;
          if (!text || !available() || ticket !== generation || ended) return;
          const payload = JSON.parse(text);
          if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
          if (payload.season_id != null && Number(payload.season_id) !== seasonId) return;
          if (name === 'access') {
            const terminal = accessDenied(payload);
            ended = true;
            if (!terminal) {
              failures++;
              delay = Math.min(15000, 1000 * 2 ** Math.min(failures - 1,4));
            }
            return;
          }
          if (name === 'end') { ended = true; delay = 100; return; }
          if (!['ready','changed','heartbeat'].includes(name)) return;
          failures = 0;
          status({state:'live', event:name, ...payload});
          // Ready covers changes committed between the first API read and subscription.
          if (name !== 'heartbeat' && !stopped && !blocked && ticket === generation) call(options.onChange, payload);
        }

        function line(value) {
          if (!value) { dispatch(); return; }
          if (value.startsWith(':')) return;
          const colon = value.indexOf(':');
          const field = colon < 0 ? value : value.slice(0,colon);
          let content = colon < 0 ? '' : value.slice(colon + 1);
          if (content.startsWith(' ')) content = content.slice(1);
          if (field === 'event') eventName = content;
          if (field === 'data') { lines.push(content); eventSize += content.length + 1; }
          if (eventSize > MAX_EVENT_SIZE) throw new Error('Auction event exceeded limit');
        }

        function feed(text, final = false) {
          buffer += text;
          if (buffer.length + eventSize > MAX_EVENT_SIZE) throw new Error('Auction stream exceeded limit');
          let boundary;
          while ((boundary = /\r\n|\r|\n/.exec(buffer))) {
            // A CR at the chunk boundary may be the first half of CRLF.
            if (!final && boundary[0] === '\r' && boundary.index === buffer.length - 1) break;
            const value = buffer.slice(0,boundary.index);
            buffer = buffer.slice(boundary.index + boundary[0].length);
            line(value);
            if (ended || blocked || stopped) break;
          }
          if (final && buffer && !ended && !blocked) { line(buffer); buffer = ''; }
        }

        while (!stopped && !blocked && ticket === generation && !ended) {
          armWatchdog();
          const chunk = await reader.read();
          if (stopped || ticket !== generation) return;
          if (chunk.done) { feed(decoder.decode(),true); break; }
          feed(decoder.decode(chunk.value,{stream:true}));
        }
        if (!ended && !blocked && !stopped && available()) {
          failures++;
          delay = Math.min(15000, 1000 * 2 ** Math.min(failures - 1,4));
          status({state:'fallback', reason:'stream_closed'});
        }
      } catch (error) {
        if (!stopped && !blocked && available()) {
          failures++;
          delay = Math.min(15000, 1000 * 2 ** Math.min(failures - 1,4));
          status({state:'fallback', reason:error.name === 'AbortError' ? 'stream_timeout' : 'connection_error'});
        }
      } finally {
        clearWatchdog();
        if (reader) {
          try { await reader.cancel(); } catch (_) {}
          try { reader.releaseLock(); } catch (_) {}
          reader = null;
        }
        if (ticket === generation) controller = null;
        running = false;
        if (available()) schedule(resume ? 0 : delay);
      }
    }

    function pause(reason) {
      clearRetry(); clearWatchdog();
      controller?.abort();
      status({state:'fallback', reason});
    }
    function resumeConnection() {
      if (!available()) return;
      resume = true;
      if (!running) schedule(0);
    }
    function visibility() { if (document.hidden) pause('hidden'); else resumeConnection(); }
    const offline = () => pause('offline');
    document.addEventListener('visibilitychange',visibility);
    window.addEventListener('online',resumeConnection);
    window.addEventListener('offline',offline);

    function stop() {
      if (stopped) return;
      stopped = true; generation++;
      clearRetry(); clearWatchdog(); controller?.abort();
      if (reader) { try { Promise.resolve(reader.cancel()).catch(() => {}); } catch (_) {} }
      document.removeEventListener('visibilitychange',visibility);
      window.removeEventListener('online',resumeConnection);
      window.removeEventListener('offline',offline);
    }
    if (!Number.isSafeInteger(seasonId) || seasonId <= 0 ||
        typeof fetch !== 'function' || typeof AbortController === 'undefined' || typeof TextDecoder === 'undefined') {
      blocked = true;
      status({state:'fallback', reason:'stream_unsupported'});
    } else if (available()) connect();
    else status({state:'fallback', reason:document.hidden ? 'hidden' : 'offline'});
    return stop;
  }
  return {watch};
})();

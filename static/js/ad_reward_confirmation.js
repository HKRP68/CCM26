// Remember the SDK completion so a network retry uses the same view reference.
window.AdRewardConfirmation = function (initData, saveCompletion) {
  const pendingFor = 5 * 60 * 1000;
  const delays = [0, 600, 1200];
  let key;
  let pendingView = null;
  try {
    const user = JSON.parse(new URLSearchParams(initData).get('user'));
    if (user?.id) key = 'ccm26:completed-ad:' + user.id;
  } catch (_) {}

  function readPending() {
    try {
      if (key) return JSON.parse(localStorage.getItem(key));
    } catch (_) {}
    return pendingView;
  }

  function writePending(value) {
    pendingView = value;
    try {
      if (!key) return;
      if (value) localStorage.setItem(key, JSON.stringify(value));
      else localStorage.removeItem(key);
    } catch (_) {}
  }

  function currentView() {
    const view = readPending();
    return view && Date.now() - view.at < pendingFor ? view : null;
  }

  async function waitForCredit() {
    const view = currentView();
    if (!view) return false;
    for (const delay of delays) {
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      const {ok, status, data} = await saveCompletion(view.id);
      if (ok && data?.confirmed) return true;
      if ([400, 401, 403, 409].includes(status)) return false;
    }
    return false;
  }

  return {
    markWatched() {
      const random = crypto.getRandomValues(new Uint8Array(16));
      const id = Array.from(random, byte => byte.toString(16).padStart(2, '0')).join('');
      writePending({ id, at: Date.now() });
    },
    isPending: () => currentView() !== null,
    clear: () => writePending(null),
    waitForCredit,
    pendingMessage: 'Your reward is still pending. Tap Check reward to retry without watching another ad.',
  };
};

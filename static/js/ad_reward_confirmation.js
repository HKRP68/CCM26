// Presentation state only. Adsgram's authenticated callback creates the credit.
window.AdRewardConfirmation = function (initData, checkCredit) {
  const pendingFor = 5 * 60 * 1000;
  const delays = [0, 1000, 2000, 3500, 5000, 5000];
  let key;
  let pendingAt = 0;
  try {
    const user = JSON.parse(new URLSearchParams(initData).get('user'));
    if (user?.id) key = 'ccm26:ad-pending:' + user.id;
  } catch (_) {}

  function readPending() {
    try {
      if (key) return Number(localStorage.getItem(key) || 0);
    } catch (_) {}
    return pendingAt;
  }

  function writePending(value) {
    pendingAt = value;
    try {
      if (!key) return;
      if (value) localStorage.setItem(key, String(value));
      else localStorage.removeItem(key);
    } catch (_) {}
  }

  async function waitForCredit() {
    for (const delay of delays) {
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      const {ok, status, data} = await checkCredit();
      if (ok && data?.confirmed) return true;
      if ([400, 401, 403].includes(status)) return false;
    }
    return false;
  }

  return {
    markWatched: () => writePending(Date.now()),
    isPending() {
      const when = readPending();
      return when > 0 && Date.now() - when < pendingFor;
    },
    clear: () => writePending(0),
    waitForCredit,
    pendingMessage: 'Adsgram hasn’t confirmed the ad yet. Tap Check reward to try again without watching another ad.',
  };
};

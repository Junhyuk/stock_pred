(() => {
  'use strict';
  // Historical B/C cards remain in the archive; never project them as current forecasts.
  const source = document.getElementById('model-bc-range');
  function openAnchoredDetails() {
    const destination = document.getElementById(location.hash.slice(1));
    const details = destination === source ? destination?.querySelector('details') : destination?.closest('#model-compare');
    if (details) details.open = true;
  }
  window.addEventListener('hashchange', openAnchoredDetails);
  openAnchoredDetails();
})();

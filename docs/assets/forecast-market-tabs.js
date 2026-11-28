(() => {
  'use strict';
  const tabs = Array.from(document.querySelectorAll('.market-tabs [role="tab"]'));
  const panels = tabs.map(tab => document.getElementById(tab.getAttribute('aria-controls')));

  function select(index) {
    tabs.forEach((tab, position) => {
      const active = position === index;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      panels[position].hidden = !active;
    });
  }

  function activate(index) {
    select(index);
    const target = tabs[index].hash;
    if (window.location.hash !== target) {
      window.history.pushState(null, '', target);
    }
  }

  function syncLocation() {
    const destination = document.getElementById(window.location.hash.slice(1));
    const index = panels.findIndex((panel, position) =>
      panel.contains(destination) || tabs[position] === destination
    );
    if (!window.location.hash || index >= 0) {
      select(Math.max(index, 0));
    }
    if (index >= 0 && destination !== panels[index] && destination !== tabs[index]) {
      window.requestAnimationFrame(() => {
        if (destination.id === window.location.hash.slice(1) && destination.getClientRects().length) {
          destination.scrollIntoView();
        }
      });
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      activate(index);
    });
    tab.addEventListener('keydown', event => {
      let next = index;
      switch (event.key) {
        case 'ArrowLeft': next = (index + tabs.length - 1) % tabs.length; break;
        case 'ArrowRight': next = (index + 1) % tabs.length; break;
        case 'Home': next = 0; break;
        case 'End': next = tabs.length - 1; break;
        case ' ': break;
        default: return;
      }
      event.preventDefault();
      activate(next);
      tabs[next].focus();
    });
  });
  const menu = document.querySelector('.market-menu');
  menu?.addEventListener('click', event => {
    if (event.target.closest('a')) menu.open = false;
  });
  window.addEventListener('hashchange', syncLocation);
  select(0);
  syncLocation();
})();

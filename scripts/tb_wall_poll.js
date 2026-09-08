/* Patch live server-rendered components without replacing the page or filters. */
(() => {
  'use strict';
  const status = document.getElementById('poll-status');
  if (!status) return;
  const key = node => node.nodeType === 1 ? node.getAttribute('data-poll-key') : null;

  function patch(current, incoming) {
    if (current.isEqualNode(incoming)) return current;
    if (current.nodeType !== incoming.nodeType || current.nodeName !== incoming.nodeName) {
      const replacement = incoming.cloneNode(true);
      current.replaceWith(replacement);
      return replacement;
    }
    if (current.nodeType !== 1) {
      current.nodeValue = incoming.nodeValue;
      return current;
    }
    // Preserve user-opened disclosures and in-progress controls.
    if (current.matches('input, select, textarea, form')) return current;
    for (const attribute of [...current.attributes]) {
      if (current.tagName === 'DETAILS' && attribute.name === 'open') continue;
      if (!incoming.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
    }
    for (const attribute of incoming.attributes) {
      if (current.tagName === 'DETAILS' && attribute.name === 'open') continue;
      if (current.getAttribute(attribute.name) !== attribute.value) {
        current.setAttribute(attribute.name, attribute.value);
      }
    }
    // Runner IDs keep disclosures attached to the same horse when prices reorder rows.
    const keyed = new Map([...current.childNodes].filter(key).map(n => [key(n), n]));
    let cursor = current.firstChild;
    for (const child of incoming.childNodes) {
      const childKey = key(child);
      const match = childKey ? keyed.get(childKey) : (cursor && !key(cursor) ? cursor : null);
      if (match) {
        if (match !== cursor) current.insertBefore(match, cursor);
        cursor = patch(match, child).nextSibling;
      } else {
        current.insertBefore(child.cloneNode(true), cursor);
      }
    }
    while (cursor) {
      const next = cursor.nextSibling;
      cursor.remove();
      cursor = next;
    }
    return current;
  }

  let busy = false;
  async function poll() {
    if (busy || document.hidden) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(window.location.href, {
        cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const updates = [...document.querySelectorAll('[data-poll]')].map(current => {
        const incoming = page.querySelector(`[data-poll="${current.dataset.poll}"]`);
        if (!incoming) throw new Error('Incomplete wall response');
        return [current, incoming];
      });
      const scroll = [window.scrollX, window.scrollY];
      const tableScroll = [...document.querySelectorAll('.table-scroll')].map(n => [n, n.scrollLeft]);
      updates.forEach(([current, incoming]) => patch(current, incoming));
      tableScroll.forEach(([node, left]) => { node.scrollLeft = left; });
      window.scrollTo(...scroll);
      status.textContent = `Updated ${new Date().toLocaleTimeString()}. Checking every 15 seconds.`;
    } catch (error) {
      status.textContent = 'Updates unavailable; showing last data. Retrying every 15 seconds.';
    } finally {
      clearTimeout(timeout);
      busy = false;
    }
  }
  setInterval(poll, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();

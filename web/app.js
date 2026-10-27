'use strict';
const $ = id => document.getElementById(id);
let snapshot = null;
let filter = 'all';
let region = 'all';
let selected = null;
let fetching = false;
let bookmarklet = null;
const text = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clock = value => value ? new Date(value).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : 'Waiting for Apple';
const duration = (start, end) => { const seconds = Math.max(0, Math.floor(((end ? new Date(end) : new Date()) - new Date(start)) / 1000)); return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds/60)}m ${seconds%60}s`; };
const statusLabel = s => ({available:'Available ↗',unavailable:'Sold out',unknown:'Unknown'}[s] || 'Unknown');
function rowsFor(sid) { return snapshot.rows.filter(r => r.store_id === sid); }
function storeStatus(sid) { const rows = rowsFor(sid); return rows.some(r => r.status === 'available') ? 'available' : rows.some(r => r.status === 'unknown') ? 'unknown' : 'unavailable'; }
function inRegion(store) { return region === 'all' || (region === 'NJ' ? store.state === 'NJ' : store.state === 'NY'); }
function bindMapPoints(map) { map.querySelectorAll('[data-store]').forEach(node => { node.addEventListener('click', () => inspect(node.dataset.store)); node.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault();inspect(node.dataset.store);}}); }); }
function renderMap() {
  const project = (lng,lat) => [155 + (lng + 75.12) * 330, 50 + (41.12 - lat) * 330];
  const outline = [[-75.12,39.86],[-75.04,39.94],[-75.01,40.08],[-74.87,40.15],[-74.83,40.3],[-75.04,40.59],[-75.04,40.78],[-74.75,41.0],[-74.69,41.05],[-74.37,41.22],[-73.95,40.99],[-74.02,40.78],[-74.12,40.7],[-74.02,40.45],[-73.98,40.29],[-74.15,40.1],[-74.19,39.9],[-74.4,39.67],[-74.63,39.4],[-74.95,39.2],[-75.0,39.45],[-75.2,39.6],[-75.12,39.86]];
  const path = outline.map((p,i) => `${i ? 'L' : 'M'}${project(...p).join(',')}`).join(' ') + ' Z';
  const positions = new Map(snapshot.stores.map(s => [s.id,project(s.lng,s.lat)]));
  const links = [['R013','R117'],['R117','R755'],['R117','R325'],['R325','R103'],['R325','R043'],['R043','R104'],['R104','R067'],['R067','R138'],['R138','R549'],['R549','R206'],['R206','R088']];
  let svg = '<circle class="ring" cx="370" cy="210" r="100"/><circle class="ring" cx="370" cy="210" r="190"/><circle class="ring" cx="370" cy="210" r="280"/>';
  svg += `<path class="state-outline" d="${path}"/>`;
  svg += '<text class="map-state" x="70" y="330" transform="rotate(-56 70 330)">PENNSYLVANIA</text><text class="map-state" x="470" y="135">NEW YORK</text><text class="map-state" x="440" y="440">ATLANTIC</text>';
  svg += links.filter(([a,b]) => positions.has(a) && positions.has(b)).map(([a,b]) => `<line class="map-line" x1="${positions.get(a)[0]}" y1="${positions.get(a)[1]}" x2="${positions.get(b)[0]}" y2="${positions.get(b)[1]}"/>`).join('');
  const labels = {R013:[11,-10],R117:[13,-5],R755:[13,15],R325:[-13,-10],R103:[-13,-10],R043:[-13,-9],R104:[-13,-10],R067:[13,12],R138:[13,7],R549:[-13,15],R206:[-13,-10],R088:[13,13]};
  svg += snapshot.stores.filter(s => s.state === 'NJ').map(s => {const [x,y] = positions.get(s.id); const [dx,dy] = labels[s.id] || [13,-10];const status = storeStatus(s.id);return `<g class="store-point ${status}" data-store="${s.id}" tabindex="0" role="button" aria-label="${text(s.name)}: ${status}" transform="translate(${x},${y})"><title>${text(s.name)}: ${status}</title><circle r="5"/><circle class="hit" r="18"/><text x="${dx}" y="${dy}" text-anchor="${dx<0?'end':'start'}">${text(s.name.replace('Freehold Raceway Mall','Freehold').replace('Garden State Plaza','Garden State'))}</text></g>`;}).join('');
  $('radar').innerHTML = svg;
  bindMapPoints($('radar'));
  const cityStores = snapshot.stores.filter(s => s.state === 'NY');
  const rowHeight = 34;
  $('city-radar').setAttribute('viewBox', `0 0 420 ${Math.max(80,cityStores.length * rowHeight + 28)}`);
  $('city-radar').innerHTML = `<line class="city-spine" x1="17" y1="20" x2="17" y2="${Math.max(20,(cityStores.length-1)*rowHeight+20)}"/>` + cityStores.map((s,i) => `<g class="store-point ${storeStatus(s.id)}" data-store="${s.id}" tabindex="0" role="button" aria-label="${text(s.name)}: ${storeStatus(s.id)}" transform="translate(17,${20+i*rowHeight})"><title>${text(s.name)} · ${text(s.city)}: ${storeStatus(s.id)}</title><rect class="city-hit" x="-12" y="-15" width="410" height="31"/><circle r="4"/><text x="18" y="4">${text(s.name)}</text><text class="city-location" x="391" y="4" text-anchor="end">${text(s.city === 'New York' ? 'Manhattan' : s.city === 'Elmhurst' ? 'Queens' : s.city)}</text></g>`).join('');
  bindMapPoints($('city-radar'));
}
function inspect(id) {
  selected = id;
  const store = snapshot.stores.find(s => s.id === id);
  if (!store) return;
  const details = `<strong>${text(store.name)}</strong> · ${text(store.city)}, ${text(store.state)}<br>${rowsFor(id).map(r => `${text(snapshot.parts[r.part].color)} 256GB: ${statusLabel(r.status)}`).join(' &nbsp; / &nbsp; ')}`;
  $('inspect').innerHTML = details;
  $('city-inspect').innerHTML = store.state === 'NY' ? details : 'Choose a city store for pickup details.';
}
function render() {
  const {status, stores, rows, parts, events} = snapshot;
  const lastSweep = status.last_sweep_at ? new Date(status.last_sweep_at).getTime() : 0;
  const fresh = lastSweep && Date.now() - lastSweep < Math.max(20000,status.interval_seconds*3000);
  if (!fresh) rows.forEach(r => {r.status='unknown';r.reason='Waiting for a fresh Apple sweep';});
  const ready = rows.filter(r => r.status === 'available');
  const expectedProducts = stores.length * Object.keys(parts).length;
  const full = fresh && status.stores_seen === stores.length && status.products_verified === expectedProducts;
  $('connection').className = `connection ${fresh && full ? 'live' : 'offline'}`;
  $('connection-text').textContent = !fresh ? 'Waiting for a fresh sweep' : full ? 'Radar is live' : 'Partial coverage';
  $('coverage').textContent = `${status.stores_seen} / ${stores.length}`;
  $('intro').textContent = `Two colors. ${stores.length} stores across New Jersey + Manhattan. A quiet watch for your next pickup.`;
  $('region-count-all').textContent = stores.length;
  $('region-count-NJ').textContent = stores.filter(s => s.state === 'NJ').length;
  $('region-count-NYC').textContent = stores.filter(s => s.state === 'NY').length;
  $('ready-count').textContent = new Set(ready.map(r => r.store_id)).size.toString().padStart(2,'0');
  $('shelf-status').textContent = full ? '● LIVE' : '● WAITING';
  $('discord-tag').textContent = status.discord_configured ? (status.discord_last_error ? 'DELIVERY ISSUE' : 'CONFIGURED') : 'SETUP NEEDED';
  $('discord-status').textContent = status.discord_configured ? 'Local configuration loaded · duplicates suppressed' : 'Add your webhook to config.local.json';
  $('last-check').textContent = clock(status.last_sweep_at);
  $('duration').textContent = `${status.requests} requests · ${status.duration_ms}ms`;
  $('health-note').textContent = `${status.products_verified}/${expectedProducts} product titles verified · ${status.proxy_count ? (status.proxies_healthy ?? status.proxy_count) + '/' + status.proxy_count + ' HTTP proxies ready' : 'Direct Apple connection'} · ${status.sweeps} sweeps`;
  if (status.proxy_failovers) $('health-note').textContent += ` · ${status.proxy_failovers} connection${status.proxy_failovers === 1 ? '' : 's'} recovered`;
  const warnings = status.warnings || [];
  $('network-details').hidden = !warnings.length;
  $('network-summary').textContent = `All stores covered; ${warnings.length} upstream check${warnings.length === 1 ? '' : 's'} recovered through overlapping responses`;
  $('network-warnings').textContent = warnings.join('\n');
  const issues = [...status.errors];
  if (status.discord_last_error) issues.push(status.discord_last_error);
  if (!full && status.last_sweep_at) issues.push('Incomplete coverage: unconfirmed products remain unknown.');
  $('error').hidden = !issues.length;
  $('error').textContent = [...new Set(issues)].join('\n');
  $('available-list').innerHTML = ready.length ? ready.map(r => {const s=stores.find(s=>s.id===r.store_id);return `<div class="drop"><strong>${text(s.name)}</strong><small>256GB · ${text(parts[r.part].color)}</small><a class="buy-link" href="${text(r.buy_url)}" target="_blank" rel="noopener noreferrer">OPEN EXACT MODEL <span>↗</span></a></div>`;}).join('') : `<h2>${full ? 'Quiet on the shelves.' : 'Scanning the shelves.'}</h2><p>${full ? 'No verified 256GB stock right now. The radar keeps watching.' : 'Waiting for complete, verified Apple pickup data.'}</p>`;
  const visible = stores.filter(s => inRegion(s) && (filter === 'all' || storeStatus(s.id) === 'available'));
  $('stores').innerHTML = visible.map(s => `<article class="store-card ${storeStatus(s.id)==='available'?'lit':''}" data-store-id="${s.id}" data-state="${s.state}"><div class="store-name"><h3>${text(s.name)}</h3><span class="store-id">${s.id}</span></div><p class="store-city">${text(s.city)}, ${text(s.state)} · ${s.zip}</p>${rowsFor(s.id).map(r => `<div class="variant"><i class="swatch ${parts[r.part].color==='Night Sky'?'night':'white'}"></i><span>${text(parts[r.part].color)}</span>${r.status==='available'?`<a class="status available" href="${text(r.buy_url)}" target="_blank" rel="noopener noreferrer">${statusLabel(r.status)}</a>`:`<span class="status ${r.status}" title="${text(r.reason || 'Apple-confirmed 256GB')}">${statusLabel(r.status)}</span>`}</div>`).join('')}</article>`).join('');
  $('empty-filter').hidden = visible.length !== 0;
  $('history').innerHTML = events.length ? events.map(e => {const s=stores.find(s=>s.id===e.store_id);return `<tr><td>${text(s?.name || e.store_id)}</td><td>${text(parts[e.part]?.color || e.part)}</td><td title="${text(new Date(e.started_at).toLocaleString())}">${clock(e.started_at)}</td><td>${duration(e.started_at,e.ended_at)}${e.ended_at?'':' · open'}</td><td class="${e.delivery==='delivered'?'delivered':''}" title="${text(e.delivery_error || '')}">${text(e.delivery)}</td></tr>`;}).join('') : '<tr><td colspan="5" class="empty">No stock transitions recorded yet. Initial stock and restocks will appear here.</td></tr>';
  renderMap();
  if (selected) inspect(selected);
}
async function refresh() {
  if (fetching) return;
  fetching = true;
  try {
    const response = await fetch('/api/state',{cache:'no-store'});
    if (!response.ok) throw new Error('Monitor unavailable');
    snapshot = await response.json();
    render();
  } catch {
    $('connection').className = 'connection offline';
    $('connection-text').textContent = 'Monitor disconnected';
    // Previously available stock must not look live after the server disappears.
    if (snapshot) {
      snapshot.rows.forEach(r => {r.status='unknown';r.verified=false;r.reason='Monitor disconnected';});
      snapshot.status.errors = ['Cannot reach the local monitor. Start it with Start.cmd.'];
      snapshot.status.stores_seen = 0;
      snapshot.status.products_verified = 0;
      render();
      $('connection-text').textContent = 'Monitor disconnected';
    }
  } finally {fetching=false;}
}
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});if(snapshot)render();}));
document.querySelectorAll('[data-region]').forEach(button => button.addEventListener('click', () => {region=button.dataset.region;document.querySelectorAll('[data-region]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});if(snapshot)render();}));
async function loadBookmarklet() {
  if (bookmarklet) return bookmarklet;
  const response = await fetch('/bookmarklet.js', {cache:'no-store'});
  if (!response.ok) throw new Error('Checkout helper unavailable');
  const source = await response.text();
  if (!source.trim()) throw new Error('Checkout helper is empty');
  bookmarklet = 'javascript:' + encodeURIComponent(source);
  $('bookmarklet-drag').href = bookmarklet;
  $('bookmarklet-drag').setAttribute('aria-disabled','false');
  return bookmarklet;
}
$('copy-bookmarklet').addEventListener('click', async () => {
  const button = $('copy-bookmarklet');
  button.disabled = true;
  try {
    const value = await loadBookmarklet();
    await navigator.clipboard.writeText(value);
    $('bookmarklet-status').textContent = 'Copied. Create an Add to Bag bookmark, edit its URL, and paste.';
    $('bookmarklet-url').hidden = true;
    $('bookmarklet-label').hidden = true;
  } catch {
    if (bookmarklet) {
      $('bookmarklet-label').hidden = false;
      $('bookmarklet-url').hidden = false;
      $('bookmarklet-url').value = bookmarklet;
      $('bookmarklet-url').focus();
      $('bookmarklet-url').select();
      $('bookmarklet-status').textContent = 'Clipboard access is unavailable. Copy the selected bookmark URL below.';
    } else {
      $('bookmarklet-status').textContent = 'The checkout helper could not load. Check the local monitor and try again.';
    }
  } finally {button.disabled = false;}
});
$('bookmarklet-drag').addEventListener('click', event => {event.preventDefault();$('bookmarklet-status').textContent='Drag Add to Bag to your bookmarks bar, or use Copy bookmarklet for manual setup.';});
loadBookmarklet().catch(() => {});
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
refresh();
setInterval(refresh, 1000);

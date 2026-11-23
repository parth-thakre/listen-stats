(() => {
'use strict';
const L = window.LIBRARY, P = window.PROFILE || {}, view = document.querySelector('#view');
if (!L) { view.innerHTML = '<p class="empty">The library file is missing. Run the stats refresh, then reload this page.</p>'; return; }

/* ---------- data ---------- */
const TZ_OFF = Number.isFinite(L.tz_offset) ? L.tz_offset : 0;
const TZ_LABEL = L.tz_name || 'UTC';
const STANDALONE = Boolean(L.standalone);
const FALLBACK = 'assets/cover-unavailable.svg';
const artists = L.artists.map(([name, mbid, area], i) => ({i, name, mbid, area}));
const albums = L.albums.map(([title, people, cover], i) => ({i, title, people, cover}));
const tracks = L.tracks.map(([title, artist, credit, cover, length, year, genres], i) => ({i, title, artist, credit, cover, length, year, genres}));
const genreNames = L.genres;
const listens = L.listens.map(([ts, t, al, sec]) => {
  const d = new Date((ts + TZ_OFF) * 1000), day = d.toISOString().slice(0, 10);
  return {ts, t, al, sec, day, month: day.slice(0, 7), hour: d.getUTCHours(), wd: (d.getUTCDay() + 6) % 7};
});
const firstDay = listens[0].day, lastDay = listens.at(-1).day;
const addDays = (day, k) => new Date(Date.parse(day + 'T00:00:00Z') + k * 864e5).toISOString().slice(0, 10);
const daysBetween = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 864e5);
const weekdayOf = day => (new Date(day + 'T00:00:00Z').getUTCDay() + 6) % 7;
const mondayOf = day => addDays(day, -weekdayOf(day));
const count = (rows, key) => rows.reduce((m, l) => m.set(key(l), (m.get(key(l)) || 0) + 1), new Map());

// Built once: listens per artist, album and track, and the day each artist was first heard.
const byArtist = new Map(), byAlbum = new Map(), byTrack = new Map(), firstHeard = new Map();
const push = (m, k, v) => (m.get(k) || m.set(k, []).get(k)).push(v);
for (const l of listens) {
  push(byTrack, l.t, l);
  if (l.al >= 0) push(byAlbum, l.al, l);
  for (const a of tracks[l.t].credit) { push(byArtist, a, l); if (!firstHeard.has(a)) firstHeard.set(a, l.day); }
}

// Portraits embedded directly in library.js, with fallback to older snapshots if present.
const faces = new Map(Object.entries(L.portraits || {}));
if (!faces.size) {
  const spotifyOk = a => {
    const s = a.spotify_portrait;
    return s && a.artist_mbid && s.artist_mbid === a.artist_mbid
      && /^https:\/\/(?:i\.scdn\.co|image-cdn-(?:ak|fa)\.spotifycdn\.com)\/image\/[A-Za-z0-9]+$/.test(s.url) ? s.url : null;
  };
  [...Object.values(P.ranges || {}).flatMap(r => r.top_artists),
   ...(P.origins?.areas || []).flatMap(o => [o.lead, ...o.top]),
   ...Object.values(P.compare || {}).flatMap(c => [c.now.top_artist, c.prev.top_artist]),
   ...(window.LB?.top_artists || []), ...(window.DAY?.artists || [])].forEach(a => {
    if (!a?.name || faces.has(a.name)) return;
    const src = a.portrait || spotifyOk(a);
    if (src) faces.set(a.name, src);
  });
}

/* ---------- ranges & state ---------- */
const RANGES = {
  week: {label: '7 days', title: 'Last 7 days', long: 'the last 7 days', start: addDays(lastDay, -6), len: 7},
  month: {label: '30 days', title: 'Last 30 days', long: 'the last 30 days', start: addDays(lastDay, -29), len: 30},
  year: {label: 'This year', title: 'This year', long: 'this year', start: lastDay.slice(0, 4) + '-01-01'},
  all: {label: 'All time', title: 'All time', long: 'all time', start: firstDay},
};
let range = RANGES[localStorage.getItem('lb-range')] ? localStorage.getItem('lb-range') : 'month';
let sortMode = 'plays', listFilter = '', activeHour = null;
const R = () => RANGES[range];
const startDay = () => R().start < firstDay ? firstDay : R().start;
const inRange = rows => rows.filter(l => l.day >= R().start);
const prevRows = rows => {
  const r = R();
  if (!r.len) return null;
  const from = addDays(r.start, -r.len);
  return rows.filter(l => l.day >= from && l.day < r.start);
};
const Long = () => R().long[0].toUpperCase() + R().long.slice(1);

function tally(rows) {
  const art = new Map(), trk = new Map(), alb = new Map(), days = new Set();
  let sec = 0;
  const bump = (m, k, s, ts) => {
    const e = m.get(k) || m.set(k, {k, n: 0, sec: 0, last: 0}).get(k);
    e.n++; e.sec += s; if (ts > e.last) e.last = ts;
  };
  for (const l of rows) {
    sec += l.sec; days.add(l.day); bump(trk, l.t, l.sec, l.ts);
    if (l.al >= 0) bump(alb, l.al, l.sec, l.ts);
    for (const a of tracks[l.t].credit) bump(art, a, l.sec, l.ts);
  }
  const sort = m => [...m.values()].sort((a, b) => b.n - a.n || b.sec - a.sec);
  return {rows, n: rows.length, sec, days: days.size, artists: sort(art), tracks: sort(trk), albums: sort(alb)};
}
const cache = new Map();
const rangeTally = () => cache.get(range) || cache.set(range, tally(inRange(listens))).get(range);

function sortEntries(items, mode) {
  const copy = items.slice();
  if (mode === 'time') return copy.sort((a, b) => b.sec - a.sec || b.n - a.n);
  if (mode === 'recent') return copy.sort((a, b) => b.last - a.last || b.n - a.n);
  return copy.sort((a, b) => b.n - a.n || b.sec - a.sec);
}

/* ---------- formatting ---------- */
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n = v => Number(v || 0).toLocaleString();
const plays = c => `${n(c)} ${c === 1 ? 'play' : 'plays'}`;
const dur = sec => sec >= 5400 ? `${n(Math.round(sec / 3600))} h` : `${n(Math.round(sec / 60))} min`;
const mmss = ms => { const s = Math.round(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const dfmt = (day, o = {day: 'numeric', month: 'short', year: 'numeric'}) => new Date(day + 'T00:00:00Z').toLocaleDateString('en-GB', {timeZone: 'UTC', ...o});
const short = day => dfmt(day, {day: 'numeric', month: 'short'});
const mfmt = (m, o = {month: 'long', year: 'numeric'}) => dfmt(m + '-01', o);
const clock = ts => new Date((ts + TZ_OFF) * 1000).toISOString().slice(11, 16);
const pct = (a, b) => b ? Math.round(a / b * 100) : 0;
const WD = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const hourName = h => h === 0 ? 'Midnight' : h === 12 ? 'Noon' : `${h % 12} ${h < 12 ? 'am' : 'pm'}`;
const ago = ts => {
  const d = Math.max(0, Math.round(Date.parse(L.generated_at) / 1000) - ts);
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return short(new Date((ts + TZ_OFF) * 1000).toISOString().slice(0, 10));
};

/* ---------- pieces ---------- */
const monogram = s => {
  const words = String(s || '').trim().split(/\s+/).filter(Boolean);
  const chars = words.slice(0, 2).map(w => (w.match(/[\p{L}\p{N}]/u) || [''])[0]).filter(Boolean);
  return esc((chars.join('') || '?').toUpperCase());
};
const letter = s => monogram(s);
const hueOf = s => {
  let h = 2166136261;
  for (const ch of String(s || '')) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
  return Math.abs(h) % 360;
};
const artSrc = src => src && src !== FALLBACK ? src : null;
const art = (src, title, cls = '') => {
  const h = hueOf(title), m = monogram(title);
  return artSrc(src)
    ? `<img class="art ${cls}" src="${esc(src)}" alt="" loading="lazy" data-letter="${m}" data-hue="${h}">`
    : `<span class="art blank ${cls}" style="--h:${h}" aria-hidden="true">${m}</span>`;
};
const faceSrc = a => artSrc(faces.get(artists[a].name));
const trackSrc = t => artSrc(tracks[t].cover || albums[byTrack.get(t)?.[0]?.al]?.cover);
const albumSrc = al => artSrc(albums[al].cover);
const face = (a, cls = '') => art(faceSrc(a), artists[a].name, 'face ' + cls);
const trackArt = (t, cls) => art(trackSrc(t), tracks[t].title, cls);
const albumArt = (al, cls) => art(albumSrc(al), albums[al].title, cls);
document.addEventListener('error', e => {
  const im = e.target;
  if (im.tagName !== 'IMG' || !im.dataset.letter) return;
  const s = document.createElement('span');
  s.className = im.className + ' blank'; s.textContent = im.dataset.letter;
  if (im.dataset.hue) s.style.setProperty('--h', im.dataset.hue);
  s.setAttribute('aria-hidden', 'true');
  im.replaceWith(s);
}, true);

const href = path => '#/' + String(path).split('/').map(encodeURIComponent).join('/');
const A = (path, text, cls = '') => `<a href="${href(path)}"${cls ? ` class="${cls}"` : ''}>${esc(text)}</a>`;
const go = path => `data-go="${href(path)}"`;
const extLink = (url, label) => `<a class="ext" href="${esc(url)}" target="_blank" rel="noopener">${esc(label)} ↗</a>`;
const spotifySearch = q => `https://open.spotify.com/search/${encodeURIComponent(q)}`;
const credits = t => tracks[t].credit.length
  ? tracks[t].credit.map(a => A(`artist/${a}`, artists[a].name)).join(', ') : esc(tracks[t].artist);
const albumCredits = al => albums[al].people.map(a => A(`artist/${a}`, artists[a].name)).join(', ');
const head = (title, extra = '') => `<div class="head"><h2>${title}</h2>${extra}</div>`;
const crumb = (path, label) => `<a class="crumb" href="${href(path)}">← ${esc(label)}</a>`;
const safeImgUrl = u => {
  if (typeof u !== 'string' || !u || u === FALLBACK) return null;
  if (/^assets\/[A-Za-z0-9._/-]+$/.test(u) && !u.includes('..')) return u;
  try {
    const parsed = new URL(u);
    if (parsed.protocol === 'https:') return parsed.href;
  } catch {}
  return null;
};
const safeHttpUrl = u => {
  if (typeof u !== 'string' || !u) return null;
  try {
    const parsed = new URL(u);
    if (parsed.protocol === 'https:' || parsed.protocol === 'http:') return parsed.href;
  } catch {}
  return null;
};
const heroWrap = (bgSrc, inner) => {
  const safe = safeImgUrl(bgSrc);
  return `<div class="hero-wrap">${safe ? `<div class="hero-bg" data-bg="${esc(safe)}"></div>` : ''}<div class="hero">${inner}</div></div>`;
};

let limit = 50;
const moreButton = (total, shown = limit) => total > shown
  ? `<button class="more" type="button" data-more>Show ${n(Math.min(50, total - shown))} more of ${n(total)}</button>` : '';

function artistRows(items, cap = limit, metric = 'plays') {
  const max = items[0]?.[metric === 'time' ? 'sec' : 'n'] || 1;
  return `<ol class="rows">${items.slice(0, cap).map((e, i) => {
    const val = metric === 'time' ? e.sec : e.n;
    const right = metric === 'time'
      ? `${dur(e.sec)}<br><small>${plays(e.n)}</small>`
      : metric === 'recent' && e.last
        ? `${short(new Date((e.last + TZ_OFF) * 1000).toISOString().slice(0, 10))}<br><small>${plays(e.n)}</small>`
        : `${plays(e.n)}<br><small>${dur(e.sec)}</small>`;
    return `<li ${go(`artist/${e.k}`)}><span class="rank">${i + 1}</span>${face(e.k, 'sm')}
      <span>${A(`artist/${e.k}`, artists[e.k].name, 't1')}<span class="meter"><i style="width:${val / max * 100}%"></i></span></span>
      <span class="n">${right}</span></li>`;
  }).join('')}</ol>`;
}
function trackRows(items, cap = limit, numbered = true, metric = 'plays') {
  return `<ol class="rows">${items.slice(0, cap).map((e, i) => {
    const right = metric === 'time'
      ? `${dur(e.sec)}<br><small>${plays(e.n)}</small>`
      : metric === 'recent' && e.last
        ? `${short(new Date((e.last + TZ_OFF) * 1000).toISOString().slice(0, 10))}<br><small>${plays(e.n)}</small>`
        : plays(e.n);
    return `<li ${go(`track/${e.k}`)}><span class="rank">${numbered ? i + 1 : ''}</span>${trackArt(e.k, 'sm')}
      <span>${A(`track/${e.k}`, tracks[e.k].title, 't1')}<span class="t2">${credits(e.k)}</span></span>
      <span class="n">${right}</span></li>`;
  }).join('')}</ol>`;
}
function albumRows(items, cap = limit) {
  return `<ol class="rows plain">${items.slice(0, cap).map(e => `<li ${go(`album/${e.k}`)}>${albumArt(e.k, 'sm')}<span>${A(`album/${e.k}`, albums[e.k].title, 't1')}<span class="t2">${albumCredits(e.k)}</span></span><span class="n">${plays(e.n)}</span></li>`).join('')}</ol>`;
}
function albumGrid(items, cap = limit, shelf = false, metric = 'plays') {
  return `<div class="grid-albums${shelf ? ' shelf' : ''}">${items.slice(0, cap).map(e => `<div ${go(`album/${e.k}`)}>${albumArt(e.k)}
    ${A(`album/${e.k}`, albums[e.k].title, 't1')}<span class="t2">${albumCredits(e.k)}</span><span class="n">${metric === 'time' ? dur(e.sec) : plays(e.n)}</span></div>`).join('')}</div>`;
}
function faceGrid(items, cap, shelf = false) {
  return `<div class="faces${shelf ? ' shelf' : ''}">${items.slice(0, cap).map(e => `<a href="${href(`artist/${e.k}`)}">${face(e.k)}<b>${esc(artists[e.k].name)}</b><small>${plays(e.n)}</small></a>`).join('')}</div>`;
}
function playRows(rows, cap = limit, showDay = true) {
  return `<ol class="rows played">${rows.slice(0, cap).map(l => `<li ${go(`track/${l.t}`)}><span class="rank">${clock(l.ts)}</span>${trackArt(l.t, 'xs')}
    <span>${A(`track/${l.t}`, tracks[l.t].title, 't1')}<span class="t2">${credits(l.t)}</span></span>
    <span class="n">${showDay ? A(`day/${l.day}`, short(l.day)) : ''}</span></li>`).join('')}</ol>`;
}
function bars(items, h = 140) {
  const max = Math.max(1, ...items.map(i => i.v));
  return `<div class="bars" style="height:${h}px">${items.map(i => {
    const cls = `bar${i.off ? ' off' : ''}${i.active ? ' active' : ''}`;
    if (i.href) {
      return `<a class="${cls}" href="${href(i.href)}" data-tip="${esc(i.tip)}" aria-label="${esc(i.tip)}"><i style="height:${i.v ? Math.max(2, i.v / max * 100) : 0}%"></i></a>`;
    }
    return `<button type="button" class="${cls}"${i.hour != null ? ` data-hour="${i.hour}"` : ''}${i.readout ? ` data-readout="${esc(i.readout)}"` : ''} data-tip="${esc(i.tip)}" aria-label="${esc(i.tip)}"><i style="height:${i.v ? Math.max(2, i.v / max * 100) : 0}%"></i></button>`;
  }).join('')}</div>`;
}
const axis = labels => `<div class="axis">${labels.map(l => `<span>${esc(l)}</span>`).join('')}</div>`;
function monthChart(rows) {
  const c = count(rows, l => l.month), months = [], start = R().start.slice(0, 7);
  for (let m = firstDay.slice(0, 7); m <= lastDay.slice(0, 7); m = addDays(m + '-01', 32).slice(0, 7)) months.push(m);
  return bars(months.map(m => ({v: c.get(m) || 0, off: m < start, href: `month/${m}`,
    tip: `${mfmt(m, {month: 'short', year: 'numeric'})}: ${plays(c.get(m) || 0)}`})), 120)
    + axis([mfmt(months[0], {month: 'short'}), mfmt(months.at(-1), {month: 'short', year: 'numeric'})]);
}
function hourChart(rows) {
  const h = Array(24).fill(0), byH = Array.from({length: 24}, () => []);
  rows.forEach(l => { h[l.hour]++; byH[l.hour].push(l); });
  const peak = h.indexOf(Math.max(...h));
  const sel = activeHour != null && h[activeHour] ? activeHour : null;
  const readoutFor = i => {
    const sub = byH[i];
    if (!sub.length) return `<b>${hourName(i)}</b> · no plays`;
    const topT = [...count(sub, l => l.t)].sort((a, b) => b[1] - a[1])[0];
    return `<b>${hourName(i)}</b> · ${plays(sub.length)} · most played: ${A(`track/${topT[0]}`, tracks[topT[0]].title)}`;
  };
  return `<div data-hour-chart>` + bars(h.map((v, i) => ({
    v, hour: i, active: sel === i,
    tip: `${hourName(i)}: ${plays(v)}`,
    readout: `${hourName(i)}: ${plays(v)}`,
  })), 120) + axis(['Midnight', '6 am', 'Noon', '6 pm', '11 pm'])
    + `<p class="chart-readout">${sel != null ? readoutFor(sel) : h[peak] ? `Peak: ${readoutFor(peak)} <span class="note">(tap any bar)</span>` : ''}</p></div>`;
}
function dayChart(days, rows) {
  const c = count(rows, l => l.day);
  return bars(days.map(d => ({v: c.get(d) || 0, href: `day/${d}`, tip: `${dfmt(d, {weekday: 'short', day: 'numeric', month: 'short'})}: ${plays(c.get(d) || 0)}`})), 120)
    + axis([short(days[0]), short(days.at(-1))]);
}
function stat(value, label, now, before) {
  let d = '';
  if (before) {
    const p = Math.round((now - before) / before * 100);
    d = `<span class="d ${p >= 0 ? 'up' : 'down'}" title="Compared with the ${R().len} days before">${p >= 0 ? '+' : '−'}${Math.abs(p)}%</span>`;
  }
  return `<div><b>${value}${d}</b><small>${label}</small></div>`;
}
const rangeNote = () => R().len ? `${short(R().start)} to ${short(lastDay)}` : range === 'year' ? `1 January to ${short(lastDay)}` : `${dfmt(firstDay)} to ${short(lastDay)}`;

/* ---------- pages ---------- */
function home() {
  const T = rangeTally(), prev = prevRows(listens), B = prev && tally(prev);
  const fresh = T.artists.filter(e => firstHeard.get(e.k) >= R().start);
  const weeks = [], wc = count(listens, l => mondayOf(l.day));
  for (let w = mondayOf(firstDay); w <= lastDay; w = addDays(w, 7)) weeks.push(w);
  const latest = listens.at(-1), lt = tracks[latest.t];
  const nowCard = `<div class="now-card" ${go(`track/${latest.t}`)}>${trackArt(latest.t, 'sm')}
    <div class="meta"><span class="k">Latest · ${esc(ago(latest.ts))}</span>${A(`track/${latest.t}`, lt.title, 't1')}<span class="t2">${credits(latest.t)}</span></div></div>`;
  if (!T.n) return `<div class="home-head"><div><h1 class="page-title">${R().title}</h1><p class="sub">${rangeNote()}</p></div>${nowCard}</div><p class="empty">Nothing played in ${esc(R().long)}.</p>`;
  return `<div class="home-head"><div><h1 class="page-title">${R().title}</h1><p class="sub">${rangeNote()}${B ? `, compared with the ${R().len} days before` : ''}</p></div>${nowCard}</div>
    <div class="stats">
      ${stat(n(T.n), 'listens', T.n, B?.n)}${stat(dur(T.sec), 'listening time', T.sec, B?.sec)}
      ${stat(n(T.artists.length), 'artists', T.artists.length, B?.artists.length)}${stat(n(T.tracks.length), 'songs', T.tracks.length, B?.tracks.length)}
      ${stat(n(T.albums.length), 'albums', T.albums.length, B?.albums.length)}${stat(n(T.days), 'active days', T.days, B?.days)}
    </div>
    <section>${head('Top artists', A('artists', 'See all'))}${faceGrid(T.artists, 12, true)}</section>
    <section class="two">
      <div>${head('Top songs', A('tracks', 'See all'))}${trackRows(T.tracks, 10)}</div>
      <div>${head('Recently played', A(`day/${lastDay}`, 'Today'))}${playRows(listens.slice(-10).reverse(), 10)}</div>
    </section>
    <section>${head('Top albums', A('albums', 'See all'))}${albumGrid(T.albums, 12, true)}</section>
    <section>${head('Listens per week', A('calendar', 'Calendar'))}<p class="note lead">Gold weeks fall in ${esc(R().long)}. Click a week to open it.</p>
      ${bars(weeks.map(w => ({v: wc.get(w) || 0, off: addDays(w, 6) < R().start, href: `week/${w}`, tip: `Week of ${short(w)}: ${plays(wc.get(w) || 0)}`})), 145)}
      ${axis([dfmt(weeks[0], {month: 'short', year: 'numeric'}), short(weeks.at(-1))])}</section>
    ${fresh.length && range !== 'all' ? `<section>${head('New to you')}<p class="note lead">${n(fresh.length)} artists heard for the first time in ${esc(R().long)}.</p>${faceGrid(fresh, 12, true)}</section>` : ''}`;
}
function listPage(kind) {
  const T = rangeTally();
  const cfg = {
    artists: {title: 'Artists', items: T.artists, match: e => artists[e.k].name, render: (xs) => artistRows(xs, limit, sortMode)},
    tracks: {title: 'Songs', items: T.tracks, match: e => `${tracks[e.k].title} ${tracks[e.k].artist}`, render: (xs) => trackRows(xs, limit, true, sortMode)},
    albums: {title: 'Albums', items: T.albums, match: e => `${albums[e.k].title} ${albums[e.k].people.map(a => artists[a].name).join(' ')}`, render: (xs) => albumGrid(xs, limit, false, sortMode)},
  }[kind];
  const needle = listFilter.trim().toLowerCase();
  const filtered = needle ? cfg.items.filter(e => cfg.match(e).toLowerCase().includes(needle)) : cfg.items;
  const sorted = sortEntries(filtered, sortMode);
  const sorts = [['plays', 'Plays'], ['time', 'Time'], ['recent', 'Recent']];
  return `<h1 class="page-title">${cfg.title}</h1><p class="sub">${n(filtered.length)}${needle ? ` of ${n(cfg.items.length)}` : ''} in ${esc(R().long)}</p>
    <div class="toolbar">
      <div class="pills" role="group" aria-label="Sort by">${sorts.map(([m, label]) => `<button type="button" data-sort="${m}" aria-pressed="${sortMode === m}">${label}</button>`).join('')}</div>
      <input class="filter-in" id="list-filter" type="search" value="${esc(listFilter)}" placeholder="Filter ${cfg.title.toLowerCase()}…" aria-label="Filter ${cfg.title.toLowerCase()}">
    </div>
    <section style="margin-top:20px">${sorted.length ? cfg.render(sorted) + moreButton(sorted.length) : `<p class="empty">Nothing matches in ${esc(R().long)}.</p>`}</section>`;
}
// Detail pages follow the range; if nothing was played in it, they fall back to all time.
function scoped(all) {
  const rows = inRange(all);
  return rows.length ? {rows, scope: R().long} : {rows: all, scope: 'all time', fallback: true};
}
const scopeNote = d => d.fallback ? `<p class="note" style="margin-top:16px">No plays in ${esc(R().long)}, so this page shows all time.</p>` : '';
const firstLast = all => stat(A(`day/${all[0].day}`, dfmt(all[0].day)), 'first played') + stat(A(`day/${all.at(-1).day}`, dfmt(all.at(-1).day)), 'last played');

function artistPage(i) {
  const a = artists[i], all = byArtist.get(i);
  if (!a || !all) return notFound();
  const d = scoped(all), T = tally(d.rows), rank = rangeTally().artists.findIndex(e => e.k === i);
  const co = new Map();
  d.rows.forEach(l => tracks[l.t].credit.forEach(c => c !== i && co.set(c, (co.get(c) || 0) + 1)));
  const coList = [...co].sort((x, y) => y[1] - x[1]).slice(0, 12);
  const songCap = limit === 50 ? 10 : limit - 40;
  const actions = [
    extLink(spotifySearch(a.name), 'Spotify'),
    a.mbid ? extLink(`https://musicbrainz.org/artist/${a.mbid}`, 'MusicBrainz') : '',
  ].filter(Boolean).join('');
  return `${crumb('artists', 'Artists')}
    ${heroWrap(faceSrc(i), `${face(i)}<div><p class="kind">Artist${a.area ? `, from ${A(`place/${a.area}`, a.area)}` : ''}</p><h1>${esc(a.name)}</h1>
      <p class="by">${plays(T.n)} in ${esc(d.scope)}${d.fallback || range === 'all' ? '' : `, ${plays(all.length)} all time`}</p>
      <div class="hero-actions">${actions}</div></div>`)}
    ${scopeNote(d)}
    <div class="stats">${rank >= 0 ? stat('#' + (rank + 1), `artist in ${esc(R().long)}`) : ''}${stat(dur(T.sec), 'listening time')}
      ${stat(n(T.tracks.length), 'songs played')}${stat(n(T.albums.length), 'albums')}${firstLast(all)}</div>
    <section class="two">
      <div>${head('Top songs', `<span class="note">${n(T.tracks.length)} in total</span>`)}${trackRows(T.tracks, songCap)}${moreButton(T.tracks.length, songCap)}</div>
      <div>${head('Albums')}${T.albums.length ? albumRows(T.albums, 10) : '<p class="note">No album tags on these plays.</p>'}</div>
    </section>
    <section class="two">
      <div>${head('Plays per month')}${monthChart(all)}</div>
      <div>${head('Time of day')}${hourChart(d.rows)}</div>
    </section>
    ${coList.length ? `<section>${head('Often credited with')}<div class="tags">${coList.map(([c, k]) => `<a href="${href(`artist/${c}`)}">${esc(artists[c].name)} <span class="note">${n(k)}</span></a>`).join('')}</div></section>` : ''}
    <section>${head('Recent plays')}${playRows(all.slice(-10).reverse(), 10)}</section>`;
}
function albumPage(i) {
  const al = albums[i], all = byAlbum.get(i);
  if (!al || !all) return notFound();
  const d = scoped(all), T = tally(d.rows), year = tracks[all[0].t].year;
  const leadArtist = al.people[0] != null ? artists[al.people[0]].name : '';
  return `${crumb('albums', 'Albums')}
    ${heroWrap(albumSrc(i), `${albumArt(i)}<div><p class="kind">Album${year ? `, ${A(`decade/${Math.floor(year / 10) * 10}`, year)}` : ''}</p><h1>${esc(al.title)}</h1>
      <p class="by">${albumCredits(i)}</p>
      <div class="hero-actions">${extLink(spotifySearch(`${al.title} ${leadArtist}`.trim()), 'Spotify')}</div></div>`)}
    ${scopeNote(d)}
    <div class="stats">${stat(n(T.n), `plays in ${esc(d.scope)}`)}${d.fallback || range === 'all' ? '' : stat(n(all.length), 'plays all time')}
      ${stat(dur(T.sec), 'listening time')}${stat(n(T.tracks.length), 'songs played')}${firstLast(all)}</div>
    <section class="two">
      <div>${head('Songs you played')}${trackRows(T.tracks, 200)}</div>
      <div>${head('Plays per month')}${monthChart(all)}<section>${head('Recent plays')}${playRows(all.slice(-8).reverse(), 8)}</section></div>
    </section>`;
}
function trackPage(i) {
  const t = tracks[i], all = byTrack.get(i);
  if (!t || !all) return notFound();
  const d = scoped(all), onAlbums = [...new Set(all.map(l => l.al).filter(x => x >= 0))];
  const best = [...count(all, l => l.day)].sort((a, b) => b[1] - a[1])[0];
  const newest = all.slice().reverse();
  return `${crumb('tracks', 'Songs')}
    ${heroWrap(trackSrc(i), `${trackArt(i)}<div><p class="kind">Song${t.year ? `, ${A(`decade/${Math.floor(t.year / 10) * 10}`, t.year)}` : ''}${t.length ? `, ${mmss(t.length)}` : ''}</p><h1>${esc(t.title)}</h1>
      <p class="by">${credits(i)}${onAlbums.length ? `<br>from ${onAlbums.map(al => A(`album/${al}`, albums[al].title)).join(', ')}` : ''}</p>
      <div class="hero-actions">${extLink(spotifySearch(`${t.title} ${t.artist}`), 'Spotify')}</div></div>`)}
    ${scopeNote(d)}
    <div class="stats">${stat(n(d.rows.length), `plays in ${esc(d.scope)}`)}${d.fallback || range === 'all' ? '' : stat(n(all.length), 'plays all time')}
      ${stat(dur(all.reduce((s, l) => s + l.sec, 0)), 'listening time, all time')}
      ${best[1] > 1 ? stat(A(`day/${best[0]}`, short(best[0])), `most in one day, ${n(best[1])} plays`) : ''}
      ${firstLast(all)}</div>
    ${t.genres.length ? `<section style="margin-top:20px"><div class="tags">${t.genres.map(g => `<a href="${href(`genre/${g}`)}">${esc(genreNames[g])}</a>`).join('')}</div></section>` : ''}
    <section class="two">
      <div>${head('Plays per month')}${monthChart(all)}</div>
      <div>${head('Time of day')}${hourChart(all)}</div>
    </section>
    <section>${head('Every play', `<span class="note">${plays(all.length)}, newest first</span>`)}${playRows(newest)}${moreButton(newest.length)}</section>`;
}
function spanPage(kind, title, days, prevPath, nextPath) {
  const set = new Set(days), rows = listens.filter(l => set.has(l.day)), T = tally(rows);
  const pager = `<div class="pager">${prevPath ? A(prevPath, `← Previous ${kind}`) : ''}${nextPath ? A(nextPath, `Next ${kind} →`) : ''}</div>`;
  if (!rows.length) return `${crumb('calendar', 'Calendar')}<h1 class="page-title">${esc(title)}</h1>${pager}<p class="empty">Nothing played.</p>`;
  const single = days.length === 1;
  return `${crumb('calendar', 'Calendar')}<h1 class="page-title">${esc(title)}</h1>${pager}
    <div class="stats">${stat(n(T.n), 'listens')}${stat(dur(T.sec), 'listening time')}${stat(n(T.artists.length), 'artists')}${stat(n(T.tracks.length), 'songs')}${single ? '' : stat(`${n(T.days)} of ${n(days.length)}`, 'days you listened')}</div>
    ${single ? `<section class="two"><div>${head('In order', `<span class="note">${esc(TZ_LABEL)}</span>`)}${playRows(rows, 1000, false)}</div><div>${head('Top artists')}${artistRows(T.artists, 10)}<section>${head('Time of day')}${hourChart(rows)}</section></div></section>`
      : `<section>${head('By day')}${dayChart(days, rows)}</section>
         <section class="two"><div>${head('Top artists')}${artistRows(T.artists, 10)}</div><div>${head('Top songs')}${trackRows(T.tracks, 10)}</div></section>
         <section>${head('Top albums')}${albumGrid(T.albums, 6)}</section>`}`;
}
const isDay = s => /^\d{4}-\d\d-\d\d$/.test(s) && !isNaN(Date.parse(s));
function dayPage(day) {
  if (!isDay(day)) return notFound();
  return spanPage('day', dfmt(day, {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'}), [day],
    day > firstDay ? `day/${addDays(day, -1)}` : '', day < lastDay ? `day/${addDays(day, 1)}` : '');
}
function weekPage(w) {
  if (!isDay(w)) return notFound();
  w = mondayOf(w);
  const days = [...Array(7)].map((_, k) => addDays(w, k));
  return spanPage('week', `Week of ${dfmt(w, {day: 'numeric', month: 'long', year: 'numeric'})}`, days,
    w > firstDay ? `week/${addDays(w, -7)}` : '', days[6] < lastDay ? `week/${addDays(w, 7)}` : '');
}
function monthPage(m) {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(m)) return notFound();
  const days = [];
  for (let d = m + '-01'; d.startsWith(m); d = addDays(d, 1)) days.push(d);
  const prev = addDays(m + '-01', -1).slice(0, 7), next = addDays(days.at(-1), 1).slice(0, 7);
  return spanPage('month', mfmt(m), days, prev >= firstDay.slice(0, 7) ? `month/${prev}` : '', next <= lastDay.slice(0, 7) ? `month/${next}` : '');
}
function calendarPage() {
  const c = count(listens, l => l.day);
  const vals = [...c.values()].sort((a, b) => a - b), q = p => vals[Math.floor(p * (vals.length - 1))];
  const cuts = [q(.25), q(.5), q(.75)];
  const level = v => !v ? '' : v <= cuts[0] ? 'l1' : v <= cuts[1] ? 'l2' : v <= cuts[2] ? 'l3' : 'l4';
  let cells = '';
  for (let d = mondayOf(firstDay); d <= lastDay; d = addDays(d, 1)) {
    const v = c.get(d) || 0, tip = `${dfmt(d, {weekday: 'short', day: 'numeric', month: 'short'})}: ${plays(v)}`;
    cells += d < firstDay ? '<span class="pad"></span>' : `<a class="${level(v)}" href="${href(`day/${d}`)}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"></a>`;
  }
  const months = [...new Set(listens.map(l => l.month))].reverse();
  const busiest = [...c].sort((a, b) => b[1] - a[1]).slice(0, 10);
  const topOf = rows => tally(rows).artists[0];
  return `<h1 class="page-title">Calendar</h1><p class="sub">Every day since ${dfmt(firstDay)}. Click a day to see what you played.</p>
    <section style="margin-top:24px"><div class="heat">${cells}</div>
      <div class="legend">Fewer <i style="background:var(--line)"></i><i style="background:var(--heat1)"></i><i style="background:var(--heat2)"></i><i style="background:var(--heat3)"></i><i style="background:var(--heat4)"></i> More</div></section>
    <section class="two">
      <div>${head('Months')}<ol class="rows plain">${months.map(m => {
        const rows = listens.filter(l => l.month === m), top = topOf(rows);
        return `<li ${go(`month/${m}`)}>${face(top.k, 'xs')}<span>${A(`month/${m}`, mfmt(m), 't1')}<span class="t2">Most played: ${A(`artist/${top.k}`, artists[top.k].name)}</span></span><span class="n">${plays(rows.length)}</span></li>`;
      }).join('')}</ol></div>
      <div>${head('Busiest days')}<ol class="rows plain">${busiest.map(([d, v]) => {
        const top = topOf(listens.filter(l => l.day === d));
        return `<li ${go(`day/${d}`)}>${face(top.k, 'xs')}<span>${A(`day/${d}`, dfmt(d, {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'}), 't1')}<span class="t2">Mostly ${A(`artist/${top.k}`, artists[top.k].name)}</span></span><span class="n">${plays(v)}</span></li>`;
      }).join('')}</ol></div>
    </section>`;
}
function genresPage() {
  const rows = inRange(listens), g = new Map(), dec = new Map();
  let tagged = 0, dated = 0;
  rows.forEach(l => {
    const t = tracks[l.t];
    if (t.genres.length) tagged++;
    t.genres.forEach(x => g.set(x, (g.get(x) || 0) + 1));
    if (t.year) { dated++; const d = Math.floor(t.year / 10) * 10; dec.set(d, (dec.get(d) || 0) + 1); }
  });
  const top = [...g].sort((a, b) => b[1] - a[1]).slice(0, 30), max = top[0]?.[1] || 1;
  const decades = [...dec].sort((a, b) => a[0] - b[0]);
  return `<h1 class="page-title">Genres and decades</h1><p class="sub">${Long()}. MusicBrainz tags cover ${pct(tagged, rows.length)}% of these listens and release years ${pct(dated, rows.length)}%. A song can carry up to three genres.</p>
    <section class="two">
      <div>${head('Genres')}<ol class="rows plain">${top.map(([x, v]) => `<li ${go(`genre/${x}`)}><span></span><span>${A(`genre/${x}`, genreNames[x], 't1')}<span class="meter"><i style="width:${v / max * 100}%"></i></span></span><span class="n">${plays(v)}</span></li>`).join('')}</ol></div>
      <div>${head('Decades')}${decades.length ? bars(decades.map(([d, v]) => ({v, href: `decade/${d}`, tip: `${d}s: ${plays(v)}`})), 180) + axis([decades[0][0] + 's', decades.at(-1)[0] + 's']) : ''}
        <ol class="rows plain" style="margin-top:16px">${decades.slice().reverse().map(([d, v]) => `<li ${go(`decade/${d}`)}><span></span><span>${A(`decade/${d}`, d + 's', 't1')}</span><span class="n">${plays(v)}, ${pct(v, dated)}%</span></li>`).join('')}</ol></div>
    </section>`;
}
function filteredPage(title, sub, test, backPath, backLabel) {
  const all = listens.filter(test);
  if (!all.length) return notFound();
  const d = scoped(all), T = tally(d.rows);
  return `${crumb(backPath, backLabel)}<h1 class="page-title">${esc(title)}</h1><p class="sub">${sub}, ${plays(all.length)} all time${d.fallback || range === 'all' ? '' : `, ${plays(T.n)} in ${esc(R().long)}`}</p>${scopeNote(d)}
    <section class="two"><div>${head('Top artists')}${artistRows(T.artists, 15)}</div><div>${head('Top songs')}${trackRows(T.tracks, 15)}</div></section>
    <section>${head('Top albums')}${albumGrid(T.albums, 6)}</section>`;
}
const genrePage = g => genreNames[g] ? filteredPage(genreNames[g], 'Genre', l => tracks[l.t].genres.includes(+g), 'genres', 'Genres & decades') : notFound();
const decadePage = d => filteredPage(`The ${d}s`, `Songs first released ${d} to ${+d + 9}`, l => tracks[l.t].year >= +d && tracks[l.t].year < +d + 10, 'genres', 'Genres & decades');
function placesPage() {
  const rows = inRange(listens), c = new Map(), who = new Map();
  let placed = 0;
  rows.forEach(l => {
    const areas = new Set();
    tracks[l.t].credit.forEach(a => {
      const x = artists[a].area;
      if (!x) return;
      areas.add(x);
      const m = who.get(x) || who.set(x, new Map()).get(x);
      m.set(a, (m.get(a) || 0) + 1);
    });
    if (areas.size) placed++;
    areas.forEach(x => c.set(x, (c.get(x) || 0) + 1));
  });
  const list = [...c].sort((a, b) => b[1] - a[1]), max = list[0]?.[1] || 1;
  return `<h1 class="page-title">Where artists are from</h1><p class="sub">${Long()}. MusicBrainz knows where the artists are from on ${pct(placed, rows.length)}% of these listens; a song with artists from two countries counts for both.</p>
    <section style="margin-top:24px">${list.length ? `<ol class="rows plain">${list.map(([x, v]) => {
      const top = [...who.get(x)].sort((a, b) => b[1] - a[1]);
      return `<li ${go(`place/${x}`)}>${face(top[0][0], 'sm')}<span>${A(`place/${x}`, x, 't1')}<span class="t2">${top.slice(0, 3).map(([a]) => A(`artist/${a}`, artists[a].name)).join(', ')}${top.length > 3 ? ` and ${n(top.length - 3)} more` : ''}</span><span class="meter"><i style="width:${v / max * 100}%"></i></span></span><span class="n">${plays(v)}</span></li>`;
    }).join('')}</ol>` : `<p class="empty">No artist locations in ${esc(R().long)}.</p>`}</section>`;
}
function placePage(x) {
  if (!artists.some(a => a.area === x)) return notFound();
  const list = rangeTally().artists.filter(e => artists[e.k].area === x);
  return `${crumb('places', 'Places')}<h1 class="page-title">${esc(x)}</h1><p class="sub">Artists from ${esc(x)} you played in ${esc(R().long)}</p>
    <section style="margin-top:24px">${list.length ? artistRows(list) + moreButton(list.length) : `<p class="empty">None in ${esc(R().long)}. Pick a longer range above.</p>`}</section>`;
}
function duetsPage() {
  const pairs = new Map();
  inRange(listens).forEach(l => {
    const c = [...new Set(tracks[l.t].credit)].sort((a, b) => a - b);
    for (let i = 0; i < c.length; i++) for (let j = i + 1; j < c.length; j++) { const k = c[i] + ',' + c[j]; pairs.set(k, (pairs.get(k) || 0) + 1); }
  });
  const list = [...pairs].sort((a, b) => b[1] - a[1]).slice(0, 30), sim = L.similar_users || P.similar_users || [];
  const pairings = `<div>${head('Top pairings')}${list.length ? `<ol class="rows plain">${list.map(([k, v]) => {
    const [a, b] = k.split(',').map(Number);
    return `<li ${go(`artist/${a}`)}><span class="pair">${face(a, 'xs')}${face(b, 'xs')}</span><span>${A(`artist/${a}`, artists[a].name, 't1')}<span class="t2">with ${A(`artist/${b}`, artists[b].name)}</span></span><span class="n">${plays(v)}</span></li>`;
  }).join('')}</ol>` : `<p class="empty">No shared credits in ${esc(R().long)}.</p>`}</div>`;
  const simCol = sim.length ? `<div>${head('Similar listeners')}<p class="note lead">ListenBrainz users whose taste overlaps with yours. Opens on ListenBrainz.</p><ol class="rows plain">${sim.map(s => `<li><span class="art blank xs face">${letter(s.user)}</span><span><a class="t1" href="https://listenbrainz.org/user/${encodeURIComponent(s.user)}/" target="_blank" rel="noopener">${esc(s.user)}</a></span><span class="n">${Math.round(s.similarity * 100)}% similar</span></li>`).join('')}</ol></div>` : '';
  return `<h1 class="page-title">${sim.length ? 'Duets and similar listeners' : 'Duets'}</h1><p class="sub">Artists credited together on the songs you played in ${esc(R().long)}</p>
    <section${sim.length ? ' class="two"' : ''}>${pairings}${simCol}</section>`;
}
function habitsPage() {
  const rows = inRange(listens), T = rangeTally();
  if (!rows.length) return `<h1 class="page-title">Habits</h1><p class="empty">Nothing played in ${esc(R().long)}.</p>`;
  const from = startDay(), span = daysBetween(from, lastDay) + 1, daySet = new Set(rows.map(l => l.day));
  let best = {len: 0}, run = 0, runStart = null;
  const wdDays = Array(7).fill(0);
  for (let d = from; d <= lastDay; d = addDays(d, 1)) {
    wdDays[weekdayOf(d)]++;
    if (daySet.has(d)) { if (!run) runStart = d; run++; if (run > best.len) best = {len: run, start: runStart, end: d}; } else run = 0;
  }
  const sessions = [];
  rows.forEach((l, k) => { if (!k || l.ts - rows[k - 1].ts > 1800) sessions.push([l]); else sessions.at(-1).push(l); });
  const sizes = sessions.map(s => s.length).sort((a, b) => a - b), longest = sessions.reduce((a, b) => b.length > a.length ? b : a);
  const wd = Array(7).fill(0);
  rows.forEach(l => wd[l.wd]++);
  const weekend = wd[5] + wd[6], weekendDays = wdDays[5] + wdDays[6], weekdayDays = span - weekendDays;
  const once = T.tracks.filter(e => e.n === 1).length;
  const [loopKey, loopN] = [...count(rows, l => l.day + '|' + l.t)].sort((a, b) => b[1] - a[1])[0];
  const [loopDay, loopTrack] = loopKey.split('|');
  const lens = T.tracks.map(e => tracks[e.k]).filter(t => t.length > 30000).sort((a, b) => a.length - b.length);
  const avg = lens.length ? lens.reduce((s, t) => s + t.length, 0) / lens.length : 0;
  const fresh = T.artists.filter(e => firstHeard.get(e.k) >= R().start);
  const fact = (big, text) => `<div class="fact"><b>${big}</b><p>${text}</p></div>`;
  return `<h1 class="page-title">Habits</h1><p class="sub">${Long()}, ${rangeNote()}</p>
    <section class="facts" style="margin-top:24px">
      ${fact(`${n(daySet.size)} of ${n(span)} days`, `You listened on ${pct(daySet.size, span)}% of days.`)}
      ${fact(`${n(best.len)}-day streak`, `Your longest run, ${A(`day/${best.start}`, short(best.start))} to ${A(`day/${best.end}`, short(best.end))}.${run > 1 ? ` You're on a ${n(run)}-day streak now.` : ''}`)}
      ${fact(`${n(sessions.length)} sessions`, `A session ends after 30 quiet minutes. Half of yours were ${n(sizes[Math.floor(sizes.length / 2)])} songs or fewer.`)}
      ${fact(`${n(longest.length)} songs straight`, `Your longest session, on ${A(`day/${longest[0].day}`, short(longest[0].day))} from ${clock(longest[0].ts)} to ${clock(longest.at(-1).ts)}.`)}
      ${fact(`${n(weekdayDays ? Math.round((rows.length - weekend) / weekdayDays) : 0)} a weekday`, `Average plays on a weekday, against ${n(weekendDays ? Math.round(weekend / weekendDays) : 0)} on a weekend day.`)}
      ${fact(`${n(once)} one-play songs`, `${pct(once, T.tracks.length)}% of the ${n(T.tracks.length)} songs you played got exactly one play.`)}
      ${fact(`${n(loopN)} times in one day`, `${A(`track/${loopTrack}`, tracks[loopTrack].title)} on ${A(`day/${loopDay}`, short(loopDay))}, your biggest single-day repeat.`)}
      ${lens.length ? fact(`${mmss(avg)} average`, `Song length. Shortest: ${A(`track/${lens[0].i}`, lens[0].title)} (${mmss(lens[0].length)}). Longest: ${A(`track/${lens.at(-1).i}`, lens.at(-1).title)} (${mmss(lens.at(-1).length)}).`) : ''}
      ${range === 'all' ? '' : fact(`${n(fresh.length)} new artists`, fresh.length ? `Heard for the first time in this range. Most played: ${fresh.slice(0, 3).map(e => A(`artist/${e.k}`, artists[e.k].name)).join(', ')}.` : 'You had heard everyone here before.')}
    </section>
    <section class="two">
      <div>${head('Day of the week')}<p class="note lead">Average plays per day</p><div data-wd-chart>${bars(wd.map((v, k) => { const avgD = (wdDays[k] ? v / wdDays[k] : 0).toFixed(1); return {v: wdDays[k] ? v / wdDays[k] : 0, tip: `${WD[k]}: ${avgD} plays/day`, readout: `${WD[k]} · ${avgD} plays/day (${plays(v)} total)`}; }), 140)}${axis(['Mon', 'Wed', 'Fri', 'Sun'])}<p class="chart-readout"></p></div></div>
      <div>${head('Time of day')}<p class="note lead">${esc(TZ_LABEL)}</p>${hourChart(rows)}</div>
    </section>`;
}
function searchPage(q) {
  const s = q.trim().toLowerCase();
  if (!s) return `<h1 class="page-title">Search</h1><p class="empty">Type an artist, song or album name.</p>`;
  const plays_ = (m, k) => m.get(k)?.length || 0;
  const find = (list, m, test, cap) => list.filter(test).sort((a, b) => plays_(m, b.i) - plays_(m, a.i)).slice(0, cap).map(x => ({k: x.i, n: plays_(m, x.i)}));
  const ar = find(artists, byArtist, a => a.name.toLowerCase().includes(s), 12);
  const tr = find(tracks, byTrack, t => t.title.toLowerCase().includes(s), 20);
  const al = find(albums, byAlbum, a => a.title.toLowerCase().includes(s), 12);
  if (!ar.length && !tr.length && !al.length) return `<h1 class="page-title">Nothing matches “${esc(q)}”</h1><p class="empty">Check the spelling, or try part of the name.</p>`;
  return `<h1 class="page-title">Results for “${esc(q)}”</h1><p class="sub">Play counts are for all time</p>
    ${ar.length ? `<section>${head('Artists')}${faceGrid(ar, 12)}</section>` : ''}
    ${tr.length ? `<section>${head('Songs')}${trackRows(tr, 20, false)}</section>` : ''}
    ${al.length ? `<section>${head('Albums')}${albumGrid(al, 12)}</section>` : ''}`;
}
function creditsPage() {
  const list = L.portrait_credits || [];
  return `<h1 class="page-title">Credits &amp; data sources</h1>
    <p class="sub">Listening history from <a href="https://listenbrainz.org/" target="_blank" rel="noopener">ListenBrainz</a>${L.sources?.lastfm ? ' and <a href="https://www.last.fm/" target="_blank" rel="noopener">Last.fm</a>' : ''}, enriched with <a href="https://musicbrainz.org/" target="_blank" rel="noopener">MusicBrainz</a> metadata.</p>
    ${list.length ? `<section>${head('Artist portrait attributions', `<span class="note">${n(list.length)} licensed photos</span>`)}<ol class="rows plain">${list.map(c => {
      const srcUrl = safeHttpUrl(c.source), licUrl = safeHttpUrl(c.license_url);
      const lic = c.license ? (licUrl ? `<a href="${esc(licUrl)}" target="_blank" rel="noopener">${esc(c.license)}</a>` : esc(c.license)) : '';
      const meta = [esc(c.attribution), lic, srcUrl ? `<a href="${esc(srcUrl)}" target="_blank" rel="noopener">Source ↗</a>` : ''].filter(Boolean).join(' · ');
      return `<li><span></span><span><span class="t1">${esc(c.name)}</span><span class="t2">${meta}</span></span><span></span></li>`;
    }).join('')}</ol></section>` : '<section><p class="note">No third-party portrait photographs are bundled in this build.</p></section>'}`;
}
const morePage = () => `<h1 class="page-title">More</h1><section style="margin-top:20px"><ol class="rows plain">${MORE.map(([p, label, text]) =>
  `<li ${go(p)}><span></span><span>${A(p, label, 't1')}<span class="t2">${text}</span></span><span></span></li>`).join('')}
  <li ${go('credits')}><span></span><span>${A('credits', 'Credits & sources', 't1')}<span class="t2">Data attributions and artist photo licenses</span></span><span></span></li>
  ${STANDALONE ? '' : '<li><span></span><span><a href="shop-wrapped.html" class="t1">Wrapped roundup</a><span class="t2">Sixteen-card recap story you can save as images</span></span><span>↗</span></li>'}</ol></section>`;
const notFound = () => `<h1 class="page-title">Not in your library</h1><p class="empty">This link doesn’t match anything you’ve played. ${A('', 'Back to Home')}</p>`;

/* ---------- shell ---------- */
const NAV = [['', 'Home'], ['artists', 'Artists'], ['tracks', 'Songs'], ['albums', 'Albums']];
const MORE = [['calendar', 'Calendar', 'Every day and month, and your busiest days'], ['habits', 'Habits', 'Streaks, sessions, repeats and routines'],
  ['genres', 'Genres and decades', 'What kind of music, and from when'], ['places', 'Places', 'Where your artists are from'],
  ['duets', 'Duets', 'Artist pairings and collaborators']];
const ROUTES = [
  [/^$/, home], [/^(artists|tracks|albums)$/, listPage], [/^artist\/(\d+)$/, i => artistPage(+i)], [/^album\/(\d+)$/, i => albumPage(+i)],
  [/^track\/(\d+)$/, i => trackPage(+i)], [/^day\/(.+)$/, dayPage], [/^week\/(.+)$/, weekPage], [/^month\/(.+)$/, monthPage],
  [/^calendar$/, calendarPage], [/^habits$/, habitsPage], [/^genres$/, genresPage], [/^genre\/(\d+)$/, genrePage], [/^decade\/(\d{4})$/, decadePage],
  [/^places$/, placesPage], [/^place\/(.+)$/, placePage], [/^duets$/, duetsPage], [/^credits$/, creditsPage], [/^search\/?(.*)$/, searchPage], [/^more$/, morePage],
];
const sectionOf = p => {
  const top = p.split('/')[0];
  return {artist: 'artists', track: 'tracks', album: 'albums', day: 'calendar', week: 'calendar', month: 'calendar', genre: 'genres', decade: 'genres', place: 'places'}[top] ?? top;
};
const navLink = ([p, l]) => `<a href="${href(p)}" data-section="${p}">${l}</a>`;
document.querySelector('#me').innerHTML = `${esc(L.user || P.user || 'Your stats')} <small>${n(listens.length)} listens since ${dfmt(firstDay, {month: 'short', year: 'numeric'})}</small>`;
document.querySelector('#nav').innerHTML = NAV.map(navLink).join('') + '<hr>' + MORE.map(navLink).join('')
  + (STANDALONE ? '' : '<hr><a href="shop-wrapped.html">Wrapped <span>↗</span></a>');
document.querySelector('#tabs').innerHTML = [...NAV, ['more', 'More']].map(navLink).join('');
const updated = new Date(L.generated_at).toLocaleString('en-GB', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
const fromLastfm = L.sources?.lastfm || 0;
const sourceLinks = `<a href="https://listenbrainz.org/" target="_blank" rel="noopener">ListenBrainz</a>`
  + (fromLastfm ? ` + <a href="https://www.last.fm/" target="_blank" rel="noopener">Last.fm</a>` : '')
  + ` · <a href="https://musicbrainz.org/" target="_blank" rel="noopener">MusicBrainz</a>`;
const creditsLink = (L.portrait_credits || []).length ? `<br>${A('credits', 'Photo credits &amp; licenses')}` : '';
const oldLink = STANDALONE ? '' : '<br><a href="index.html">Older design experiments</a>';
document.querySelector('#foot').innerHTML = `Updated ${esc(updated)}.<br>Data: ${sourceLinks}${creditsLink}${oldLink}`;
const rangeEl = document.querySelector('#range'), q = document.querySelector('#q');
rangeEl.innerHTML = Object.entries(RANGES).map(([k, r]) => `<button type="button" data-range="${k}">${r.label}</button>`).join('');

const path = () => { try { return decodeURIComponent(location.hash.replace(/^#\/?/, '')); } catch { return '?'; } };
function render(keepScroll) {
  const p = path(), sec = sectionOf(p), inMore = sec === 'more' || sec === 'credits' || MORE.some(([m]) => m === sec);
  rangeEl.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.range === range)));
  document.querySelectorAll('[data-section]').forEach(a => {
    const on = a.dataset.section === sec || (a.dataset.section === 'more' && inMore);
    on ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current');
  });
  const hit = ROUTES.find(([re]) => re.test(p)), y = scrollY;
  view.innerHTML = hit ? hit[1](...p.match(hit[0]).slice(1)) : notFound();
  view.querySelectorAll('.hero-bg[data-bg]').forEach(el => {
    const u = safeImgUrl(el.dataset.bg);
    if (u) el.style.backgroundImage = `url("${encodeURI(u).replace(/["\\()]/g, c => '%' + c.charCodeAt(0).toString(16))}")`;
  });
  const h1 = view.querySelector('h1')?.textContent;
  document.title = h1 ? `${h1} – listening stats` : 'Listening stats';
  scrollTo(0, keepScroll ? y : 0);
}
addEventListener('hashchange', () => { limit = 50; listFilter = ''; activeHour = null; if (!path().startsWith('search')) q.value = ''; render(); });
rangeEl.addEventListener('click', e => {
  const b = e.target.closest('[data-range]');
  if (!b || b.dataset.range === range) return;
  range = b.dataset.range; localStorage.setItem('lb-range', range); limit = 50; activeHour = null; render(true);
});
view.addEventListener('click', e => {
  if (e.target.closest('[data-more]')) { limit += 50; render(true); return; }
  const sortBtn = e.target.closest('[data-sort]');
  if (sortBtn) { sortMode = sortBtn.dataset.sort; limit = 50; render(true); return; }
  const hourBtn = e.target.closest('[data-hour]');
  if (hourBtn) { activeHour = activeHour === +hourBtn.dataset.hour ? null : +hourBtn.dataset.hour; render(true); return; }
  const wdBtn = e.target.closest('[data-wd-chart] [data-readout]');
  if (wdBtn) {
    const out = wdBtn.closest('[data-wd-chart]').querySelector('.chart-readout');
    if (out) out.textContent = wdBtn.dataset.readout;
    wdBtn.closest('.bars').querySelectorAll('.bar').forEach(b => b.classList.toggle('active', b === wdBtn));
    return;
  }
  if (e.target.closest('a,button,input')) return;
  if (getSelection()?.toString()) return;
  const row = e.target.closest('[data-go]');
  if (row) {
    if (e.metaKey || e.ctrlKey) window.open(row.dataset.go, '_blank', 'noopener');
    else location.hash = row.dataset.go;
  }
});
view.addEventListener('input', e => {
  if (e.target.id !== 'list-filter') return;
  listFilter = e.target.value;
  const pos = e.target.selectionStart;
  render(true);
  const el = document.querySelector('#list-filter');
  if (el) { el.focus(); el.setSelectionRange(pos, pos); }
});
q.addEventListener('input', () => {
  const next = q.value ? '#/search/' + encodeURIComponent(q.value) : '#/';
  history.replaceState(null, '', next);
  render();
});
q.addEventListener('keydown', e => { if (e.key === 'Escape') { q.value = ''; location.hash = '#/'; q.blur(); } });
addEventListener('keydown', e => { if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) { e.preventDefault(); q.focus(); } });

const tip = document.querySelector('#tip');
document.addEventListener('pointermove', e => {
  if (e.pointerType === 'touch') { tip.style.opacity = 0; return; }
  const c = e.target.closest('[data-tip]');
  if (!c) { tip.style.opacity = 0; return; }
  tip.textContent = c.dataset.tip;
  const w = tip.offsetWidth;
  tip.style.left = Math.min(innerWidth - w - 8, Math.max(8, e.clientX - w / 2)) + 'px';
  tip.style.top = (e.clientY - 38) + 'px';
  tip.style.opacity = 1;
});
if (path().startsWith('search/')) q.value = path().slice(7);
render();
})();

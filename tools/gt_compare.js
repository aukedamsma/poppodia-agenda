// Vergelijkt de events die een podiumsite zelf toont (SITE, array van {t,s,p,u}) met onze data/events.json op GitHub.
// Draaien in de browserconsole op de podiumsite (same-origin voor hun API; GitHub raw staat CORS toe).
// Gebruik:  const SITE=[...]; const VENUE='Boerderij';  + inhoud van deze functie.  Geeft een compacte samenvatting.
async function gtCompare(SITE, VENUE, opts = {}) {
  const src = opts.src || 'https://raw.githubusercontent.com/aukedamsma/poppodia-agenda/main/data/events.json';
  const all = await (await fetch(src, {cache: 'no-store'})).json();
  const today = new Date().toISOString().slice(0, 10);
  const fold = s => (s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/\(.*?\)|\[.*?\]/g, ' ')
    .replace(/[^a-z0-9]+/g, ' ').replace(/\b(uitverkocht|sold out|afgelast|cancelled|verplaatst|support|presents|live|concert|tour|\d{4})\b/g, ' ').replace(/\s+/g, ' ').trim();
  const nurl = u => { if (!u) return ''; u = u.split('#')[0]; if (!/[?&](p|post_type|event_id|id)=/.test(u)) u = u.split('?')[0]; return u.replace('http://', 'https://').replace('://www.', '://').replace(/\/$/, '').toLowerCase(); };
  const pnum = p => { if (p == null) return null; const s = String(p).toLowerCase(); if (/gratis|free/.test(s)) return 0; const m = s.match(/(\d+)(?:[.,](\d{1,2}))?/); return m ? parseFloat(m[1] + '.' + (m[2] || 0)) : null; };
  const tmatch = (a, b) => { const fa = fold(a), fb = fold(b); if (!fa || !fb) return false; if (fa === fb || fa.includes(fb) || fb.includes(fa)) return true;
    const wa = new Set(fa.split(' ')), wb = new Set(fb.split(' ')); const c = [...wa].filter(w => wb.has(w)).length; return c >= 2 && c >= Math.min(wa.size, wb.size) * 0.6; };
  const site = SITE.filter(e => e.s && e.s.slice(0, 10) >= today).sort((a, b) => a.s.localeCompare(b.s));
  const ours = all.filter(e => e.venue === VENUE && e.start.slice(0, 10) >= today);
  const byUrl = new Map(ours.map(o => [nurl(o.url), o]));
  const used = new Set(); const missing = [], dateDiff = [], timeDiff = [], priceDiff = []; let matched = 0;
  for (const s of site) {
    let o = byUrl.get(nurl(s.u)); if (o && used.has(o)) o = null;
    if (!o) o = ours.find(x => !used.has(x) && x.start.slice(0, 10) === s.s.slice(0, 10) && tmatch(x.title, s.t));
    if (!o) o = ours.find(x => !used.has(x) && fold(x.title) && fold(x.title) === fold(s.t));
    if (!o) { missing.push([s.t, s.s, s.u || '']); continue; }
    used.add(o); matched++;
    if (o.start.slice(0, 10) !== s.s.slice(0, 10)) { dateDiff.push([s.t, s.s, o.start]); continue; }
    if (s.s.length >= 16) { const st = s.s.slice(11, 16), ot = o.start.slice(11, 16); if (!ot || ot === '00:00') timeDiff.push([s.t, st, null]); else if (st !== ot) timeDiff.push([s.t, st, ot]); }
    const sp = pnum(s.p), op = pnum(o.price); if (sp != null && (op == null || Math.abs(sp - op) > 0.5)) priceDiff.push([s.t, s.p, o.price]);
  }
  const siteLast = site.length ? site[site.length - 1].s.slice(0, 10) : null;
  const extra = ours.filter(o => !used.has(o) && (!siteLast || o.start.slice(0, 10) <= siteLast)).map(o => [o.title, o.start, o.url]);
  const oursLast = ours.map(o => o.start.slice(0, 10)).sort().pop() || null;
  return {venue: VENUE, date: today, site_events: site.length, our_events: ours.length, matched, missing: missing.length, extra: extra.length,
    date_diff: dateDiff.length, time_diff: timeDiff.length, price_diff: priceDiff.length, site_horizon: siteLast, our_horizon: oursLast,
    score: site.length ? Math.round(100 * Math.max(0, matched - dateDiff.length - timeDiff.length - priceDiff.length) / site.length) / 100 : null,
    detail: {missing: missing.slice(0, opts.max || 40), extra: extra.slice(0, opts.max || 40), date_diff: dateDiff.slice(0, 20), time_diff: timeDiff.slice(0, 20), price_diff: priceDiff.slice(0, 20)}};
}

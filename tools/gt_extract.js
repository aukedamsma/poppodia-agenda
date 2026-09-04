// Generieke grondwaarheid-extractor voor in de browserconsole (Claude Browser javascript_tool).
// 1) JSON-LD Event-objecten op de pagina; 2) anders: kaarten met een link + datumtekst.
// Resultaat: compacte JSON-string met {n, first, last, events:[{t,s,p,u,g}]}  (t=title, s=start, p=price, u=url, g=tags)
(() => {
  const MONTHS = {jan:1,januari:1,feb:2,februari:2,mrt:3,maa:3,maart:3,apr:4,april:4,mei:5,jun:6,juni:6,jul:7,juli:7,aug:8,augustus:8,sep:9,sept:9,september:9,okt:10,oktober:10,nov:11,november:11,dec:12,december:12,
                  january:1,february:2,mar:3,march:3,may:5,june:6,july:7,october:10,oct:10,august:8,december:12};
  const today = new Date(); const y0 = today.getFullYear();
  const pad = n => String(n).padStart(2, '0');
  const iso = (y, m, d, hh, mm) => `${y}-${pad(m)}-${pad(d)}` + (hh != null ? `T${pad(hh)}:${pad(mm || 0)}` : '');
  const parseDate = (txt) => {
    txt = (txt || '').toLowerCase().replace(/\s+/g, ' ');
    let m = txt.match(/(\d{1,2})[ -]([a-z]{3,9})\.?[ -]?(\d{4})?/);
    let y, mo, d;
    if (m && MONTHS[m[2]]) { d = +m[1]; mo = MONTHS[m[2]]; y = m[3] ? +m[3] : null; }
    else { m = txt.match(/(\d{1,2})[-\/.](\d{1,2})(?:[-\/.](\d{2,4}))?/); if (m) { d = +m[1]; mo = +m[2]; y = m[3] ? (+m[3] < 100 ? 2000 + +m[3] : +m[3]) : null; } }
    if (!d || !mo) return null;
    if (!y) { y = y0; const cand = new Date(y, mo - 1, d); if (cand < new Date(today.getTime() - 30 * 864e5)) y++; }
    const t = txt.match(/(\d{1,2})[:.u](\d{2})/);
    return iso(y, mo, d, t ? +t[1] : null, t ? +t[2] : null);
  };
  const price = (txt) => { const m = (txt || '').match(/gratis|free|€\s?\d+(?:[,.]\d{2})?|\d+(?:[,.]\d{2})?\s?(?:euro|€)/i); return m ? m[0].replace(/\s+/g, ' ') : null; };
  let out = [];
  // 1) JSON-LD
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const walk = (o) => { if (!o) return; if (Array.isArray(o)) return o.forEach(walk);
        if (typeof o === 'object') { const t = String(o['@type'] || ''); if (/Event/.test(t) && o.startDate) out.push({t: o.name, s: String(o.startDate).slice(0, 16), p: price(JSON.stringify(o.offers || '')), u: o.url || o['@id'] || null, g: [].concat(o.genre || o.keywords || []).map(String)}); for (const k of ['@graph', 'itemListElement', 'item', 'subEvent']) if (o[k]) walk(o[k]); } };
      walk(JSON.parse(s.textContent));
    } catch (e) {}
  }
  if (out.length >= 3) return JSON.stringify({src: 'jsonld', n: out.length, events: out});
  // 2) heuristiek: elk element met een link en een datum, zo klein mogelijk
  out = []; const seen = new Set();
  const cands = [...document.querySelectorAll('article, li, .card, [class*="event"], [class*="item"], [class*="card"], [class*="programma"], [class*="agenda"]')];
  for (const el of cands) {
    if (el.querySelector('article, li, [class*="event-item"], [class*="card"]') && el.children.length > 3 && el.innerText.length > 600) continue; // container, geen kaart
    const a = el.querySelector('a[href]') || (el.tagName === 'A' ? el : null); if (!a) continue;
    const txt = el.innerText || ''; if (txt.length > 700 || txt.length < 8) continue;
    const s = parseDate((el.querySelector('time') || {}).getAttribute?.('datetime') || txt); if (!s) continue;
    const u = a.href.split('#')[0]; if (seen.has(u + s)) continue; seen.add(u + s);
    const h = el.querySelector('h1,h2,h3,h4,[class*="title"],[class*="name"]');
    const t = (h ? h.innerText : a.innerText || a.title).trim().replace(/\s+/g, ' ').slice(0, 120); if (!t) continue;
    out.push({t, s, p: price(txt), u});
  }
  return JSON.stringify({src: 'heuristic', n: out.length, events: out});
})()

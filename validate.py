"""Grondwaarheid-validatie: vergelijkt wat een podium zelf op zijn site toont met wat de fetcher heeft opgehaald.

state/groundtruth/<podium>.json wordt met de hand (browser) per podium vastgelegd: alle events die de site toont, tot het
einde van de zichtbare agenda. Formaat:
  {"venue": "Vera", "fetched": "2026-09-05", "source": "https://…/programma/",
   "events": [{"title": "…", "start": "2026-09-12T20:30" | "2026-09-12", "price": "€ 12,50" | "gratis" | null,
               "url": "https://…", "tags": ["Concert"]}]}

Gebruik:
  python validate.py                 alle podia met grondwaarheid, samenvatting per podium
  python validate.py Vera -v         één podium, met alle afwijkingen
  python validate.py --json out.json machineleesbaar (ook gebruikt door fetch.py voor het rapport)

Vergelijking per event: eerst op URL (zonder query/slash), anders op datum + genormaliseerde titel (bevat-relatie in beide
richtingen, zodat "Moss" en "Moss + support" matchen). Per podium:
  missing   op de site, niet bij ons (binnen onze horizon van 400 dagen)
  extra     bij ons, niet op de site (binnen de horizon van de site) -> verkeerde locatie, verlopen, categoriepagina
  date/time/price  gematcht maar afwijkend (tijd alleen als de site een tijd toont; prijs als getal vergeleken)
  horizon   verste datum op de site vs. verste datum bij ons
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
GT_DIR = ROOT / "state" / "groundtruth"
EVENTS = ROOT / "data" / "events.json"
HORIZON_DAYS = 400


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(uitverkocht|sold out|afgelast|cancelled|verplaatst|nieuwe datum|support|presents|live|concert|tour|\d{4})\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_url(u: str | None) -> str:
    if not u:
        return ""
    u = u.split("#")[0]
    if not re.search(r"[?&](p|post_type|event_id|id)=", u):
        u = u.split("?")[0]
    return u.replace("http://", "https://").replace("://www.", "://").rstrip("/").lower()


def price_num(p) -> float | None:
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return float(p)
    s = str(p).lower()
    if "gratis" in s or "free" in s:
        return 0.0
    m = re.search(r"(\d+)(?:[.,](\d{1,2}))?", s)
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2) or 0}")


def titles_match(a: str, b: str) -> bool:
    fa, fb = fold(a), fold(b)
    if not fa or not fb:
        return False
    if fa == fb or fa in fb or fb in fa:
        return True
    wa, wb = set(fa.split()), set(fb.split())
    common = wa & wb
    return len(common) >= 2 and len(common) >= min(len(wa), len(wb)) * 0.6


def compare(gt: dict, ours: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    horizon = (today + timedelta(days=HORIZON_DAYS)).isoformat()
    site = [e for e in gt.get("events", []) if e.get("start") and e["start"][:10] >= today.isoformat()]
    site_last = max((e["start"][:10] for e in site), default=None)
    ours = [e for e in ours if e.get("start", "")[:10] >= today.isoformat()]
    ours_last = max((e["start"][:10] for e in ours), default=None)

    by_url = {norm_url(e.get("url")): e for e in ours if e.get("url")}
    used: set[int] = set()
    matched: list[tuple[dict, dict]] = []
    missing: list[dict] = []
    for s in site:
        cand = by_url.get(norm_url(s.get("url")))
        if cand is None or id(cand) in used:
            cand = None
            for o in ours:
                if id(o) in used or o["start"][:10] != s["start"][:10]:
                    continue
                if titles_match(o.get("title", ""), s.get("title", "")):
                    cand = o
                    break
        if cand is None:
            # zelfde titel op een andere datum = datumfout, geen missend event
            for o in ours:
                if id(o) in used:
                    continue
                if norm_url(o.get("url")) == norm_url(s.get("url")) and s.get("url") or \
                        (abs((date.fromisoformat(o["start"][:10]) - date.fromisoformat(s["start"][:10])).days) <= 400
                         and fold(o.get("title", "")) == fold(s.get("title", "")) and fold(s.get("title", ""))):
                    cand = o
                    break
        if cand is None:
            if s["start"][:10] <= horizon:
                missing.append(s)
            continue
        used.add(id(cand))
        matched.append((s, cand))

    extra = [o for o in ours if id(o) not in used and (site_last is None or o["start"][:10] <= site_last)]
    date_diff, time_diff, price_diff = [], [], []
    for s, o in matched:
        if s["start"][:10] != o["start"][:10]:
            date_diff.append({"title": s["title"], "site": s["start"], "ours": o["start"], "url": o.get("url")})
            continue
        if len(s["start"]) >= 16:
            st, ot = s["start"][11:16], o["start"][11:16]
            if ot in ("", "00:00"):
                time_diff.append({"title": s["title"], "site": st, "ours": None, "url": o.get("url")})
            elif st != ot:
                time_diff.append({"title": s["title"], "site": st, "ours": ot, "url": o.get("url")})
        sp, op = price_num(s.get("price")), price_num(o.get("price"))
        if sp is not None and (op is None or abs(sp - op) > 0.5):
            price_diff.append({"title": s["title"], "site": s.get("price"), "ours": o.get("price"), "url": o.get("url")})
    n_site = len(site)
    score = None
    if n_site:
        ok = len(matched) - len(date_diff) - len(time_diff) - len(price_diff)
        score = round(max(0, ok) / n_site, 2)
    return {
        "venue": gt.get("venue"), "fetched": gt.get("fetched"), "site_events": n_site, "our_events": len(ours),
        "matched": len(matched), "missing": len(missing), "extra": len(extra),
        "date_diff": len(date_diff), "time_diff": len(time_diff), "price_diff": len(price_diff),
        "site_horizon": site_last, "our_horizon": ours_last, "score": score,
        "detail": {"missing": missing, "extra": [{"title": o.get("title"), "start": o.get("start"), "url": o.get("url")} for o in extra],
                   "date_diff": date_diff, "time_diff": time_diff, "price_diff": price_diff},
    }


def load_groundtruth(only: list[str] | None = None) -> list[dict]:
    out = []
    for p in sorted(GT_DIR.glob("*.json")):
        gt = json.loads(p.read_text(encoding="utf-8"))
        if only and gt.get("venue") not in only and p.stem not in only:
            continue
        out.append(gt)
    return out


def run(only: list[str] | None = None, events_path: Path = EVENTS) -> list[dict]:
    events = json.loads(events_path.read_text(encoding="utf-8")) if events_path.exists() else []
    by_venue: dict[str, list[dict]] = {}
    for e in events:
        by_venue.setdefault(e.get("venue"), []).append(e)
    return [compare(gt, by_venue.get(gt.get("venue"), [])) for gt in load_groundtruth(only)]


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    out_json = argv[argv.index("--json") + 1] if "--json" in argv else None
    only = [a for a in argv if not a.startswith("-") and a != out_json] or None
    results = run(only)
    if out_json:
        Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{'podium':<22}{'site':>5}{'ours':>5}{'match':>6}{'miss':>5}{'extra':>6}{'dat':>4}{'tijd':>5}{'prijs':>6}  {'score':>5}  horizon site / ours")
    for r in results:
        print(f"{(r['venue'] or '?')[:21]:<22}{r['site_events']:>5}{r['our_events']:>5}{r['matched']:>6}{r['missing']:>5}{r['extra']:>6}"
              f"{r['date_diff']:>4}{r['time_diff']:>5}{r['price_diff']:>6}  {str(r['score']):>5}  {r['site_horizon']} / {r['our_horizon']}")
        if verbose:
            for k, rows in r["detail"].items():
                for row in rows:
                    print(f"   {k:<10} {json.dumps(row, ensure_ascii=False)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

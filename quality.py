"""quality.py — kwaliteitsbewaking: audit_venue (tijdcontrole/steekproef per podium), strategiescore (_source_score,
_good_enough), run_diff (verschil met de vorige run) en check_groundtruth (tests/groundtruth.json)."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path
from taxonomy import price_number, _fold
from common import Event, ROOT, TODAY, log
from sources import detail_extra


def audit_venue(v: dict, evs: list[Event], cache: dict) -> dict:
    """Generieke kwaliteitscontrole per podium; de lessen van eerdere fouten, toegepast op álle podia.
    - tijdcontrole: steekproef van eventpagina's; staat daar een expliciete aanvang die structureel afwijkt van de
      lijsttijd (Doornroosje: +1u door foute tijdzone), dan worden de gecontroleerde events gecorrigeerd en wordt
      het podium gemarkeerd (time_shift) zodat `time_is_local` gezet kan worden.
    - datumcluster: >25% van de events op één dag zonder tijd = vrijwel zeker een parserfout (ECI: 74 op 'vandaag');
      die events worden verwijderd.
    - late tijden: >35% van de events om 23:00 of later is verdacht voor een concertpodium.
    - dekking: aandeel events met tijd, prijs, genre.
    """
    a: dict = {}
    n = len(evs)
    if not n:
        return a
    # datumcluster
    by_day = Counter(e.start[:10] for e in evs)
    day, cnt = by_day.most_common(1)[0]
    if n >= 10 and cnt / n > 0.25:
        notime = [e for e in evs if e.start[:10] == day and e.start[11:16] in ("", "00:00")]
        if len(notime) >= 0.8 * cnt:
            for e in notime:
                evs.remove(e)
            a["date_cluster_removed"] = {"day": day, "events": len(notime)}
    # tijdcontrole (steekproef, gecached)
    if v.get("time_check", True) and v.get("type", "auto") not in ("disabled",):
        sample = [e for e in evs if e.start[11:16] not in ("", "00:00") and e.url and e.url.rstrip("/") != v["url"].rstrip("/")]
        sample = sample[:: max(1, len(sample) // 4)][:4]
        offsets = []
        for e in sample:
            x = detail_extra(v, e.url, cache)
            if x and x.get("start"):
                hh, mm = x["start"]
                st = datetime.fromisoformat(e.start)
                diff = round(((hh * 60 + mm) - (st.hour * 60 + st.minute)) / 60, 1)
                offsets.append(diff)
                # gestructureerde bronnen (JSON-LD, Stager, JSON-API) winnen van tekstheuristiek: LantarenVenster "Dezelfde
                # avond om 20:30 uur treedt Teus Nobel op" gaf een valse -1u voor een event van 19:00. Alleen lijst-HTML corrigeren.
                if diff and not (e.source or "").startswith(("jsonld", "stager", "json_api", "flight", "microdata", "tribe", "wp_event")):
                    e.start = st.replace(hour=hh, minute=mm).isoformat(timespec="minutes")
        if offsets:
            common = Counter(offsets).most_common(1)[0]
            a["time_check"] = {"sampled": len(offsets), "offsets": offsets}
            if common[0] and common[1] >= 2 and common[1] >= len(offsets) - 1 and abs(common[0]) in (1.0, 2.0):
                a["time_shift"] = common[0]
    late = sum(1 for e in evs if e.start[11:16] >= "23:00")
    if n >= 10 and late / n > 0.35:
        a["many_late"] = round(late / n, 2)
    a["coverage"] = {"time": round(sum(1 for e in evs if e.start[11:16] not in ("", "00:00")) / n, 2),
                     "price": round(sum(1 for e in evs if e.price) / n, 2),
                     "genre": round(sum(1 for e in evs if e.genres) / n, 2)}
    return a


# ----------------------------------------------------------------------------
# per podium
# ----------------------------------------------------------------------------

def _source_score(evs: list[Event]) -> float:
    """Kwaliteit van een bron: aantal events, gewogen met tijd- en prijsdekking. Een bron met 60 events zonder tijden
    verliest van een bron met 50 events mét tijden (fase 2: vergelijken in plaats van 'de eerste treffer')."""
    if not evs:
        return 0.0
    n = len(evs)
    t = sum(1 for e in evs if e.start[11:16] not in ("", "00:00")) / n
    p = sum(1 for e in evs if e.price) / n
    return n * (0.6 + 0.25 * t + 0.15 * p)


def _good_enough(v: dict, evs: list[Event]) -> bool:
    """Stoppen met verdere strategieën? Niet alleen 'minstens 20 events': een groot podium (>= 400 plaatsen) met minder
    dan 40 events of een horizon korter dan 6 weken is verdacht (JSON-LD van alleen de eerste pagina) — dan ook de
    volgende strategie proberen en de beste kiezen."""
    if len(evs) < int(v.get("good_enough", 20)):
        return False
    cap = int(v.get("capacity") or 0)
    horizon = max((e.start[:10] for e in evs), default="")
    if cap >= 400 and len(evs) < 40:
        return False
    if horizon and horizon < (TODAY + timedelta(weeks=6)).isoformat():
        return False
    return True


def run_diff(prev: list[Event], cur: list[Event], skip_venues: set[str] | None = None, today: date | None = None) -> dict:
    """Vergelijk de toekomstige events van de vorige run met die van nu (sleutel = id, podium|bron-url).
    - gone: stond vorige run (niet afgelast), nu weg, terwijl het podium gewoon gelezen is (niet stale/error/timeout)
    - day: zelfde event, andere dag; time: zelfde dag, tijd ≥ 60 min anders (beide bekend); price: > € 5 verschil
    - vanished_venues: podia die ≥ 30% van ≥ 10 toekomstige events kwijt zijn — dat is zelden echte programmering"""
    today = today or TODAY
    skip_venues = skip_venues or set()
    key = lambda e: e.id or f"{e.venue}|{e.url}"
    # huidige events onder hun id én onder podium|url, zodat de vorige run (oude url-sleutels of nog zonder id) ook matcht
    now = {f"{e.venue}|{e.url}": e for e in cur}
    now.update({key(e): e for e in cur})
    gone, day, tm, price = [], [], [], []
    prev_future: Counter = Counter()
    gone_by_venue: Counter = Counter()
    for p in prev:
        if p.start[:10] < today.isoformat() or p.venue in skip_venues:
            continue
        prev_future[p.venue] += 1
        c = now.get(key(p))
        rec = {"venue": p.venue, "title": p.title, "start": p.start, "url": p.url}
        if c is None:
            if p.status != "afgelast":
                gone.append(rec)
                gone_by_venue[p.venue] += 1
            continue
        if c.start[:10] != p.start[:10]:
            day.append(dict(rec, now=c.start))
        elif p.start[11:16] not in ("", "00:00") and c.start[11:16] not in ("", "00:00") and p.start[11:16] != c.start[11:16]:
            a, b = p.start[11:16], c.start[11:16]
            if abs((int(a[:2]) * 60 + int(a[3:])) - (int(b[:2]) * 60 + int(b[3:]))) >= 60:
                tm.append(dict(rec, now=c.start))
        pa, pb = price_number(p.price), price_number(c.price)
        if pa and pb and abs(pa - pb) > 5:
            price.append(dict(rec, price=p.price, now=c.price))
    vanished = {v: n for v, n in gone_by_venue.items() if prev_future[v] >= 10 and n >= 0.3 * prev_future[v]}
    return {"n_gone": len(gone), "n_day": len(day), "n_time": len(tm), "n_price": len(price),
            "gone": gone[:200], "day": day[:100], "time": tm[:100], "price": price[:100], "vanished_venues": vanished}


def check_groundtruth(events: list[Event], path: Path | None = None) -> dict:
    """Vaste regressietest: tests/groundtruth.json bevat events die met de browser op de podiumsite zijn geverifieerd
    (fase 1). Per item: bestaat het (podium + dag + titel), en kloppen tijd en prijs als die zijn opgegeven? Verstreken
    items tellen niet mee. Resultaat in report.json onder 'groundtruth'; missers ook als LET OP in de log."""
    path = path or (ROOT / "tests" / "groundtruth.json")
    if not path.exists():
        return {}
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    except (ValueError, OSError):
        return {}
    by_vd: dict[tuple, list[Event]] = {}
    for e in events:
        by_vd.setdefault((_fold(e.venue), e.start[:10]), []).append(e)
    checked, ok, misses = 0, 0, []
    b_checked, b_ok, b_changed = 0, 0, []   # baseline-items: uit een eerdere run, niet met de hand gecontroleerd
    for it in items:
        if not it.get("date") or it["date"] < TODAY.isoformat():
            continue
        baseline = bool(it.get("baseline"))
        if baseline:
            b_checked += 1
        else:
            checked += 1
        cands = by_vd.get((_fold(it["venue"]), it["date"]), [])
        want = _fold(it.get("title", ""))
        hits = [e for e in cands if want and want in _fold(e.title)]
        # meerdere shows op één dag (Roxy Dekker 15:00 en 20:30): die met de verwachte tijd
        hit = next((e for e in hits if it.get("time") and e.start[11:16] == it["time"]), hits[0] if hits else None)
        if hit is None:
            (b_changed if baseline else misses).append({**it, "problem": "ontbreekt"})
            continue
        problems = []
        if it.get("time") and hit.start[11:16] != it["time"]:
            problems.append(f"tijd {hit.start[11:16]} i.p.v. {it['time']}")
        if it.get("price") is not None:
            got = price_number(hit.price or "")
            if got is None or abs(got - float(it["price"])) > 0.01:
                problems.append(f"prijs {hit.price} i.p.v. {it['price']}")
        if problems:
            (b_changed if baseline else misses).append({**it, "problem": "; ".join(problems)})
        elif baseline:
            b_ok += 1
        else:
            ok += 1
    for m in misses:
        log(f"LET OP groundtruth {m['venue']} · {m['title']} {m['date']}: {m['problem']}")
    for m in b_changed:
        log(f"    baseline {m.get('baseline')} afwijking {m['venue']} · {m['title']} {m['date']}: {m['problem']}")
    log(f"Groundtruth: {ok}/{checked} gecontroleerde steekproef-events kloppen; baseline: {b_ok}/{b_checked} ongewijzigd")
    return {"checked": checked, "ok": ok, "misses": misses, "baseline_checked": b_checked, "baseline_ok": b_ok, "baseline_changed": b_changed}

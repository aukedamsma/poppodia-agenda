#!/usr/bin/env python3
"""
Haalt de concertagenda's van poppodia op en schrijft ze naar data/events.json.

Per podium (venues.yaml) worden strategieën geprobeerd, van schoon naar ruw:
  jsonld        schema.org Event-objecten op de agendapagina zelf
  embedded      JSON in de pagina (__NEXT_DATA__, Angular ng-state, andere application/json)
  tribe         The Events Calendar REST API (/wp-json/tribe/events/v1/events)
  wp_event      WordPress REST API voor een event-posttype (/wp-json/wp/v2/<type>)
  jsonld_detail eventlinks op de agendapagina volgen en per eventpagina de JSON-LD lezen (gecached)
  html          CSS-selectors uit venues.yaml

Een falend podium blokkeert nooit de rest; de uitkomst per podium staat in data/report.json.

Indeling (fetch.py is de regie en re-exporteert alles, zodat `import fetch` en de tests één ingang houden):
  common.py   paden, TODAY, Event-datamodel, log, tekst-hulpjes
  net.py      HTTP-laag: sessie, drie-traps escalatie (eigen UA -> browser-headers -> Chrome-TLS), blokkadedetectie
  extract.py  datums/tijden/prijzen uit tekst en JSON, met herkomst; prijsweergave; JSON-LD-blokken
  sources.py  de strat_*-strategieën, eventpagina's (fetch_detail/detail_extra/apply_extra) en de detailcache
  merge.py    dedupe, coproducties, herlabelen naar locatie, categorie-tags
  quality.py  audit per podium, strategiescore, run-op-run verschil, groundtruth
  fetch.py    fetch_venue (strategieketen per podium) en main (parallel, waakhond, verrijking, rapport, archief)
"""
from __future__ import annotations

import json
from collections import Counter
import os
import socket
import re
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, date, timedelta
from urllib.parse import urlparse

import requests
import yaml

import artists as artistdb
import series as seriesdb
from taxonomy import _HINT_NOISE
from taxonomy import strip_country, strip_city, strip_title_date, classify_kind_ex, extract_artists, normalize_genres, price_number, _taxonomy, artist_key, normalize_subgenres, learn_subgenres, promote_subgenres, learn_kinds, promote_kinds, subgenre_label, subgenre_group, _fold
from common import *  # noqa: F401,F403
from common import _LOG  # noqa: F401
from net import *  # noqa: F401,F403
from net import _BLOCK_RE, _BROWSER_UA_HOSTS, _IMPERSONATE_HOSTS, _impersonated_get, _looks_blocked  # noqa: F401
from extract import *  # noqa: F401,F403
from extract import _AMT, _DISCOUNT_CTX, _FEE_WORDS, _ISO_TAIL, _JSON_PRICE, _JSON_TIME_DOORS, _JSON_TIME_START, _NON_TICKET_AFTER, _NON_TICKET_CTX, _NOT_FREE_CTX, _PREFERRED_CTX, _TICKET_URL, _add_fee, _ampm_to_24h, _decoded, _fee_inclusive, _free_mentioned, _hm_from_json, _pick_price, _pick_price_ex, _price_rank, _richness, _same_event, _strip_service_fee, _strip_tz, _title_key  # noqa: F401
from sources import *  # noqa: F401,F403
from sources import _GRAPHQL_CREDS, _LOC_TEXT, _PUBLISH_CLASS, _STAGER_LINK, _STAGER_SESSIONS, _acf_date, _cache_version, _event_time_tag, _fee_cents, _fill, _find_date, _find_genres, _find_title, _find_url, _graphql_endpoint, _known_venue_names, _ld_location, _path, _stager_acf, _stager_local, _stager_price, _stager_session, _walk_event_lists  # noqa: F401
from merge import *  # noqa: F401,F403
from merge import _LOC_IN_TITLE, _take_better_price  # noqa: F401
from quality import *  # noqa: F401,F403
from quality import _good_enough, _source_score  # noqa: F401
import net  # noqa: F401  (tests: fetch.net.SESSION = ...)


DATA.mkdir(exist_ok=True)
STATE.mkdir(exist_ok=True)


def fetch_venue(v: dict, cache: dict) -> tuple[list[Event], str, str, dict]:
    """Geeft (events, gebruikte strategie, opmerking, audit)."""
    base = v["url"]
    t = v.get("type", "auto")
    if v.get("passive"):
        return [], "passive", "geen eigen bron: ontvangt events die een ander podium hier programmeert (relabel_by_location)", {}
    if t == "disabled" or v.get("enabled") is False:
        return [], "disabled", "uitgeschakeld in venues.yaml", {}

    html = ""
    notes = []
    if t not in ("tribe", "sitemap_detail", "graphql_detail", "json_api", "stager") and not (t == "html" and v.get("api")):
        try:
            r0 = get(base, delay=float(v.get("crawl_delay", 0)))
            html = r0.text
            final = str(getattr(r0, "url", "") or "")
            # verhuisde agenda: de agendapagina stuurt door naar een ander adres (andere host of ander pad)
            if final and urlparse(final).netloc.replace("www.", "") != urlparse(base).netloc.replace("www.", "") \
                    or (final and urlparse(final).path.rstrip("/") and urlparse(final).path.rstrip("/") != urlparse(base).path.rstrip("/")
                        and not urlparse(final).path.rstrip("/").startswith(urlparse(base).path.rstrip("/"))):
                notes.append(f"redirect: {base} -> {final} (controleer venues.yaml)")
                v = {**v, "_redirect": final}
        except requests.RequestException as ex:
            # site weigert ons (Het Podium, So What!: Cloudflare 403 ook met browser-headers) -> toch de extra bronnen (ticketshop) proberen
            if not v.get("extra_sources") and not v.get("fallback_sources"):
                raise
            notes.append(f"agendapagina mislukt: {type(ex).__name__} {str(ex)[:100]} -> alleen extra bronnen")
            t = "none"

    order = {
        "auto": ["jsonld", "microdata", "embedded", "html_preset", "tribe", "wp_event", "jsonld_detail"],
        "microdata": ["microdata"],
        "jsonld": ["jsonld"], "embedded": ["embedded"], "nextdata": ["embedded"],
        "tribe": ["tribe"], "wp_event": ["wp_event"], "json_api": ["json_api"], "stager": ["stager"], "jsonld_detail": ["jsonld_detail"], "html": ["html"],
        "sitemap_detail": ["sitemap_detail"], "graphql_detail": ["graphql_detail"],
    }.get(t, [t] if t != "none" else [])
    best: tuple[list[Event], str] = ([], "none")
    for strat in order:
        try:
            if strat == "jsonld":
                evs = strat_jsonld(v, html)
            elif strat == "microdata":
                evs = strat_microdata(v, html)
            elif strat == "embedded":
                evs = strat_embedded(v, html)
            elif strat == "tribe":
                evs = strat_tribe(v, base)
            elif strat == "wp_event":
                evs = strat_wp_event(v, base, cache)
            elif strat == "json_api":
                evs = strat_json_api(v, base, cache)
            elif strat == "stager":
                try:
                    evs = strat_stager(v, base, cache)
                except Exception as ex:  # noqa: BLE001 — API veranderd? dan de JSON-LD van de shoppagina
                    notes.append(f"stager-api: {type(ex).__name__} {str(ex)[:80]} -> JSON-LD")
                    evs = strat_jsonld(v, html or get(base).text)
            elif strat == "jsonld_detail":
                evs = strat_jsonld_detail(v, html, base, cache)
            elif strat == "html":
                if v.get("api"):
                    evs = strat_html_api(v, base)
                elif v.get("facetwp") or (v.get("facetwp") is None and "facetwp-template" in html):
                    evs = strat_facetwp(v, base)
                    if len(evs) < 3:
                        evs = strat_html(v, html, base)
                else:
                    evs = strat_html(v, html, base)
                    if v.get("list_pages_template"):
                        # gepagineerde HTML-agenda (Willem Twee ?page=2…): doorgaan tot een pagina niets nieuws oplevert
                        known = {e.url for e in evs}
                        for n in range(2, int(v.get("list_pages_max", 40)) + 1):
                            try:
                                r = net.SESSION.get(v["list_pages_template"].format(n=n + int(v.get("list_pages_offset", 0))), timeout=TIMEOUT)
                            except requests.RequestException:
                                break
                            time.sleep(float(v.get("crawl_delay", 0.6)))
                            if r.status_code != 200:
                                break
                            more = [e for e in strat_html(v, r.text, base) if e.url not in known]
                            if not more:
                                break
                            known.update(e.url for e in more)
                            evs += more
            elif strat == "html_preset":
                evs, preset_name = strat_html_preset(v, html, base)
                if preset_name:
                    strat = f"html:{preset_name}"
            elif strat == "sitemap_detail":
                evs = strat_sitemap_detail(v, base, cache)
            elif strat == "graphql_detail":
                evs = strat_graphql_detail(v, base, cache)
            else:
                notes.append(f"onbekend type {strat}")
                continue
        except requests.RequestException as ex:
            notes.append(f"{strat}: {type(ex).__name__} {str(ex)[:120]}")
            continue
        except Exception as ex:  # noqa: BLE001 — één podium mag de run niet breken
            notes.append(f"{strat}: {type(ex).__name__} {str(ex)[:120]}")
            continue
        evs = [e for e in evs if in_window(datetime.fromisoformat(e.start))]
        if v.get("only_genres"):
            want = {g.lower() for g in v["only_genres"]}
            evs = [e for e in evs if {g.lower() for g in e.genres} & want]
        if _source_score(evs) > _source_score(best[0]):
            best = (evs, strat)
        if _good_enough(v, evs):
            break
        notes.append(f"{strat}: {len(evs)} events")
    evs, strat = best
    # ticketshops met server-side data als automatische extra bron: Stager (<slug>.stager.co/shop/default/events, JSON-LD
    # ItemList met 50 komende events incl. tijd) wordt herkend aan ticketlinks op de agenda- of eventpagina's
    if v.get("ticketshops", True) and not v.get("_is_extra"):
        scan = html
        if not scan:
            # API-typen (tribe, json_api, sitemap) halen de agendapagina niet op; voor de shopherkenning alsnog even kijken
            # (dB's: tribe-API zonder prijzen, maar dbs.stager.co heeft ze)
            try:
                scan = get(base, delay=0.3).text
            except requests.RequestException:
                scan = ""
        found = re.findall(r"https?://([a-z0-9-]+)\.stager\.co/(?:shop/([a-z0-9_-]+))?", (scan or "").lower())
        shops = {f[0] for f in found}
        shop_path = {f[0]: f[1] for f in found if f[1] and f[1] not in ("v1",)}
        if not shops and evs:
            # de ticketlink staat vaak alleen op de eventpagina (dB's: dbs.stager.co/shop/default/events/…): drie steekproeven
            for e in evs[:3]:
                try:
                    scan = get(e.url, delay=0.3).text if e.url and e.url.rstrip("/") != base.rstrip("/") else ""
                except requests.RequestException:
                    continue
                found = re.findall(r"https?://([a-z0-9-]+)\.stager\.co/(?:shop/([a-z0-9_-]+))?", scan.lower())
                shops |= {f[0] for f in found}
                shop_path.update({f[0]: f[1] for f in found if f[1] and f[1] not in ("v1",)})
                if shops:
                    break
        # alleen shops die bij dít podium horen: Luxor Live linkt ook naar willemeen.stager.co, De Spot naar deoostkerk, Neushoorn
        # naar explorethenorth (festival) en veel sites naar app.stager.co — die zouden andermans events opleveren
        vf = re.sub(r"[^a-z0-9]", "", _fold(v["name"]))
        host = urlparse(v["url"]).netloc.replace("www.", "").split(".")[0].replace("-", "")
        def mine(slug: str) -> bool:
            sf = slug.replace("-", "")
            return slug != "app" and len(sf) >= 3 and (sf in vf or vf in sf or sf in host or host in sf)
        shops = {s_ for s_ in shops if mine(s_)}
        for slug in sorted(shops)[:2]:
            src = {"url": f"https://{slug}.stager.co/shop/{shop_path.get(slug, 'default')}/events", "type": "stager", "enrich": False}
            if src["url"] not in [x.get("url") for x in as_list(v.get("extra_sources") or [])]:
                v = {**v, "extra_sources": as_list(v.get("extra_sources") or []) + [src]}
                notes.append(f"ticketshop herkend: {slug}.stager.co")
    # extra bronnen voor hetzelfde podium (eigen site toont maar 3 weken, de Stager-ticketshop 50 events; of een tweede
    # agendapagina): elk met eigen type/instellingen; dubbele events (zelfde dag + titel) worden later samengevoegd
    # laatste redmiddel: bronnen die alleen meedoen als de eigen site niets oplevert (Het Podium: botmuur blokkeert ook de
    # browser-TLS-handdruk; pop-agenda.nl heeft de events wel)
    if not evs and v.get("fallback_sources"):
        v = {**v, "extra_sources": as_list(v.get("extra_sources") or []) + as_list(v["fallback_sources"])}
        notes.append("eigen site leverde niets -> terugvalbron(nen)")
    for src in as_list(v.get("extra_sources") or []):
        sub = {**v, **src, "extra_sources": None, "category_pages": None, "name": v["name"], "city": v["city"], "_is_extra": True}
        try:
            more, sstrat, snote, _ = fetch_venue(sub, cache)
            if v.get("only_genres"):   # ook de extra bron alleen muziek (Groene Engel: Stager-shop verkoopt ook FilmClub-kaarten)
                want = {g.lower() for g in v["only_genres"]}
                more = [e for e in more if {g.lower() for g in e.genres} & want]
            known = {e.url.rstrip("/") for e in evs}
            new = [e for e in more if e.url.rstrip("/") not in known]
            evs = evs + new
            notes.append(f"extra bron {src.get('url')}: {len(more)} events via {sstrat}, {len(new)} nieuw")
        except Exception as ex:  # noqa: BLE001
            notes.append(f"extra bron {src.get('url')}: {type(ex).__name__} {str(ex)[:80]}")
    if v.get("_redirect"):
        audit_extra = {"redirect": v["_redirect"]}
    else:
        audit_extra = {}
    if len(evs) >= int(v.get("min_events", 3)):
        if v.get("enrich", True) and strat not in ("jsonld_detail", "sitemap_detail", "graphql_detail"):
            try:
                enrich_from_detail(v, evs, cache)
            except Exception as ex:  # noqa: BLE001
                notes.append(f"enrich: {type(ex).__name__}")
        if v.get("category_pages"):
            try:
                cats, excl = category_tags(v, cache)
                before = len(evs)
                evs, tagged = apply_category_tags(evs, cats, excl)
                notes.append(f"{tagged} events getagd via podiumfilters" + (f", {before - len(evs)} uitgesloten (andere locatie)" if before != len(evs) else ""))
            except Exception as ex:  # noqa: BLE001
                notes.append(f"podiumfilters: {type(ex).__name__} {str(ex)[:80]}")
        audit = {}
        try:
            audit = audit_venue(v, evs, cache)
        except Exception as ex:  # noqa: BLE001
            notes.append(f"audit: {type(ex).__name__}")
        if audit.get("time_shift"):
            notes.append(f"LET OP: aanvang op eventpagina's wijkt {audit['time_shift']:+.0f}u af van de lijst -> zet time_is_local of controleer tijdzone")
        if audit.get("date_cluster_removed"):
            notes.append(f"{audit['date_cluster_removed']['events']} events zonder tijd op {audit['date_cluster_removed']['day']} verwijderd (datumcluster)")
        if audit.get("many_late"):
            notes.append(f"{int(audit['many_late']*100)}% van de events begint na 23:00")
        audit.update(audit_extra)
        return evs, strat, "; ".join(notes), audit
    return [], "none", "; ".join(notes), dict(audit_extra)


def main(only: list[str] | None = None) -> int:
    venues = yaml.safe_load((ROOT / "venues.yaml").read_text(encoding="utf-8"))
    seen_path, cache_path = STATE / "seen.json", STATE / "detail_cache.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    first_run = not seen

    all_events: list[Event] = []
    report = {"generated": datetime.now().isoformat(timespec="seconds"), "venues": []}
    def run_one(v):
        t0 = time.time()
        try:
            evs, strat, note, audit = fetch_venue(v, cache)
        except Exception as ex:  # noqa: BLE001
            evs, strat, note, audit = [], "error", f"{type(ex).__name__}: {str(ex)[:200]}", {}
            traceback.print_exc()
            log(traceback.format_exc()[-1500:])
        evs = dedupe(evs)
        log(f"== {v['name']} ({v['city']}): {len(evs)} events via {strat} ({time.time()-t0:.0f}s) {note}")
        return v, evs, strat, note, audit

    # vorige uitkomst per podium: als een site vandaag onbereikbaar is (Astrant, Loburg: ConnectionError om de andere run),
    # blijven de events van de vorige run staan in plaats van dat het podium leeg valt (max. 3 runs achter elkaar)
    prev_path = DATA / "events.json"
    prev_by_venue: dict[str, list[Event]] = {}
    if prev_path.exists():
        try:
            for rec in json.loads(prev_path.read_text(encoding="utf-8")):
                prev_by_venue.setdefault(rec["venue"], []).append(Event(**{k: val for k, val in rec.items() if k in Event.__dataclass_fields__}))
        except (ValueError, TypeError, KeyError):
            prev_by_venue = {}
    hist_path0 = STATE / "history.json"
    history0 = json.loads(hist_path0.read_text()) if hist_path0.exists() else {}
    todo = [v for v in venues if not only or v["name"] in only]
    # podia parallel (elk podium zelf netjes sequentieel met zijn eigen crawl_delay)
    # Waakhond: één hangend podium mag de run niet blokkeren (run #28 liep 6 uur en werd door GitHub afgebroken; niets
    # geschreven). Per podium max VENUE_MINUTES (25), voor alle podia samen RUN_MINUTES (150); wat dan nog loopt wordt
    # als 'timeout' gerapporteerd en de vorige events van dat podium blijven staan (hergebruik hieronder).
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    venue_budget = float(os.environ.get("VENUE_MINUTES", "25")) * 60
    run_budget = float(os.environ.get("RUN_MINUTES", "150")) * 60
    t_run = time.time()
    results = []
    pool = ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "8")))
    futures = {pool.submit(run_one, v): (v, time.time()) for v in todo}
    pending = set(futures)
    while pending:
        remaining = run_budget - (time.time() - t_run)
        done, pending = wait(pending, timeout=max(5.0, min(30.0, remaining)) if remaining > 0 else 5.0, return_when=FIRST_COMPLETED)
        for f in done:
            results.append(f.result())
        now = time.time()
        for f in list(pending):
            v, t0 = futures[f]
            if now - t0 > venue_budget or now - t_run > run_budget:
                pending.discard(f)
                results.append((v, [], "timeout", f"afgebroken na {int((now - t0) / 60)} min (waakhond)", {}))
                log(f"== {v['name']} ({v['city']}): TIMEOUT na {int((now - t0) / 60)} min, run gaat door zonder dit podium")
        if not pending:
            break
    pool.shutdown(wait=False, cancel_futures=True)
    _STRAGGLERS = any(not f.done() for f in futures)
    for v, evs, strat, note, audit in results:
        if not evs and strat in ("error", "none", "timeout") and not v.get("passive"):
            prev = [e for e in prev_by_venue.get(v["name"], []) if e.start[:10] >= TODAY.isoformat()]
            trailing_stale = 0
            for h in reversed(history0.get(v["name"], [])):
                if h.get("stale"):
                    trailing_stale += 1
                else:
                    break
            if len(prev) >= 3 and trailing_stale < 3:
                evs = prev
                audit = dict(audit or {}, stale=True)
                note = (note + "; " if note else "") + f"bron onbereikbaar: {len(prev)} events van de vorige run hergebruikt"
                log(f"    {v['name']}: bron onbereikbaar, {len(prev)} events van de vorige run hergebruikt")
        # volledigheidsindicatoren: weekenddekking (vr/za met minstens één event in de komende 8 weken, van 16) en horizon
        # (verste datum). Een podium met 400+ plaatsen heeft normaal (bijna) elk weekend iets: minder dan 11/16 is verdacht dun.
        wk_days = {e.start[:10] for e in evs if TODAY <= date.fromisoformat(e.start[:10]) < TODAY + timedelta(weeks=8)
                   and date.fromisoformat(e.start[:10]).weekday() in (4, 5)}
        horizon = max((e.start[:10] for e in evs), default=None)
        audit = dict(audit or {})
        audit["weekend"] = len(wk_days)
        audit["horizon"] = horizon
        cap = v.get("capacity") or 0
        if evs and cap >= 400 and len(wk_days) < 11:
            audit["thin"] = True
            note = (note + "; " if note else "") + f"dun programma: {len(wk_days)}/16 weekenddagen met een event (podium van {cap} plaatsen)"
        report["venues"].append({"name": v["name"], "city": v["city"], "url": v["url"], "strategy": strat,
                                 "events": len(evs), "note": note, "ok": len(evs) > 0 or bool(v.get("passive")), "audit": audit})
        all_events.extend(evs)
    # events die een podium elders programmeert (Paradiso in Tolhuistuin) horen bij dat andere podium; daarna dubbelen weg
    moved = relabel_by_location(all_events, venues)
    before = len(all_events)
    all_events = dedupe(all_events)
    report["relocated"] = moved
    report["deduped"] = before - len(all_events)
    log(f"Locaties: {moved} events verhuisd naar het podium waar ze plaatsvinden; {before - len(all_events)} dubbele events samengevoegd")
    # regressie-alarm: per podium het aantal events (na verhuizing/dedupe) vergelijken met de vorige runs (state/history.json).
    # Een daling tot onder 60% van het beste van de laatste 7 runs, of van >= 10 naar 0, is een parser- of blokkadeprobleem
    # (Grenswerk 138 -> 1 door een datumparserfix, Groene Engel/De Helling gehalveerd door een dedupe-fout) en staat in het
    # rapport en de log — ook als het podium 'ok' lijkt.
    hist_path = STATE / "history.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else {}
    counts = Counter(e.venue for e in all_events)
    regressions = []
    for r in report["venues"]:
        name = r["name"]
        now = counts.get(name, 0)
        past = [h for h in history.get(name, []) if h.get("date") != TODAY.isoformat()][-7:]
        best = max((h["events"] for h in past), default=None)
        if best is not None and best >= 10 and now < 0.6 * best:
            r["audit"]["regression"] = {"was": best, "now": now}
            r["note"] = (r["note"] + "; " if r["note"] else "") + f"LET OP: {now} events, was {best} in de laatste 7 runs"
            regressions.append((name, best, now))
            log(f"LET OP regressie {name}: {now} events, was {best}")
        history[name] = past + [{"date": TODAY.isoformat(), "events": now, "strategy": r["strategy"], "stale": bool(r["audit"].get("stale"))}]
    report["regressions"] = [{"venue": n, "was": b, "now": c} for n, b, c in regressions]
    # plausibiliteit van prijzen per podium: veel bedragen onder € 5 (Patronaat: "Stage 3 € 15,50" gaf € 3 voor 127 events) of
    # een podium van 400+ plaatsen dat overwegend 'gratis' zou zijn (Nieuwe Nor: 'kans op gratis tickets' in de footer)
    for r in report["venues"]:
        evs_v = [e for e in all_events if e.venue == r["name"]]
        nums = [(price_number(e.price), e.price) for e in evs_v if e.price]
        cheap = sum(1 for n, p in nums if n is not None and 0 < n < 5)
        free = sum(1 for _, p in nums if p == "gratis")
        flags = []
        if len(nums) >= 10 and cheap >= 0.3 * len(nums):
            r["audit"]["cheap_prices"] = cheap
            flags.append(f"{cheap} van {len(nums)} prijzen onder € 5")
        if len(nums) >= 10 and (vmeta_cap := (next((v.get("capacity") for v in venues if v["name"] == r["name"]), 0) or 0)) >= 400 and free >= 0.5 * len(nums):
            r["audit"]["many_free"] = free
            flags.append(f"{free} van {len(nums)} events 'gratis' (podium van {vmeta_cap} plaatsen)")
        if flags:
            r["note"] = (r["note"] + "; " if r["note"] else "") + "LET OP prijzen: " + ", ".join(flags)
            log(f"LET OP prijzen {r['name']}: {', '.join(flags)}")
    hist_path.write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # --- identiteit: titel opschonen en een stabiele id toekennen (podium|dag|titel, met url-alias in state/ids.json) ---
    vmeta = {v["name"]: v for v in venues}
    for e in all_events:
        # "junkyardUK", "Band (USA)", "Popronde Alkmaar", "13-11-2026 : Roberto Jacketti", "X | ZATERDAG 14 NOVEMBER": geen deel van de naam
        e.title = strip_title_date(strip_city(strip_country(e.title), e.city))
    ids_path = STATE / "ids.json"
    ids_state = json.loads(ids_path.read_text()) if ids_path.exists() else {}
    id_info = assign_ids(all_events, seen, ids_state, vmeta)
    ids_path.write_text(json.dumps(ids_state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if id_info["migrated"]:
        log(f"Identiteit: {id_info['migrated']} events van url-sleutel naar podium|dag|titel gemigreerd (first_seen behouden)")
    # run-op-run verschil: wélke toekomstige events verdwenen (zonder afgelast te zijn), van dag/tijd verschoven of fors
    # van prijs veranderd — het snelste signaal voor een parserregressie die de aantallen niet raakt
    skip = {r["name"] for r in report["venues"] if r["audit"].get("stale") or r["strategy"] in ("error", "none", "timeout")}
    diff = run_diff([e for vs in prev_by_venue.values() for e in vs], all_events, skip)
    report["diff"] = diff
    for r in report["venues"]:
        if r["name"] in diff["vanished_venues"]:
            r["audit"]["vanished"] = diff["vanished_venues"][r["name"]]
            r["note"] = (r["note"] + "; " if r["note"] else "") + f"LET OP: {diff['vanished_venues'][r['name']]} toekomstige events van de vorige run zijn weg"
    log(f"Verschil met vorige run: {diff['n_gone']} events verdwenen, {diff['n_day']} van dag verschoven, {diff['n_time']} van tijd (≥1 uur), {diff['n_price']} van prijs (>€ 5)"
        + (f"; podia met veel verdwenen events: {', '.join(diff['vanished_venues'])}" if diff["vanished_venues"] else ""))

    # 'nieuw' bepalen; een podium dat voor het eerst meedoet levert geen 'nieuwe' events op
    known_venues = {k.split("|", 1)[0] for k in seen}
    backdate = (TODAY - timedelta(days=30)).isoformat()
    for e in all_events:
        if e.id not in seen:
            seen[e.id] = TODAY.isoformat() if (e.venue in known_venues or not seen) else backdate
        e.first_seen = seen[e.id]
    # opschonen: sleutels van events die > 60 dagen weg zijn
    live = {e.id for e in all_events}
    for k in list(seen):
        if k not in live and date.fromisoformat(seen[k]) < TODAY - timedelta(days=60):
            del seen[k]
    if first_run:
        # eerste run: niets als 'nieuw' markeren, anders is alles nieuw
        for e in all_events:
            e.first_seen = (TODAY - timedelta(days=30)).isoformat()
            seen[e.id] = e.first_seen

    # --- verrijking: artiesten, genres, type, prijs ---
    unknown_genres: dict[str, int] = {}
    adb = artistdb.load()
    seen_ev_path = STATE / "artists_seen.json"
    seen_ev = set(json.loads(seen_ev_path.read_text())) if seen_ev_path.exists() else set()
    kind_learn_path, sub_learn_path = STATE / "kind_learn.json", STATE / "subgenre_learn.json"
    kind_learn = json.loads(kind_learn_path.read_text()) if kind_learn_path.exists() else {}
    sub_learn = json.loads(sub_learn_path.read_text()) if sub_learn_path.exists() else {}
    for f, v in list(sub_learn.get("votes", {}).items()):      # hernoemde hoofdgenres in het leergeheugen omzetten
        sub_learn["votes"][f] = artistdb.migrate_groups(v)
    for f, v in sub_learn.get("accepted", {}).items():
        v["group"] = artistdb.GROUP_RENAMES.get(v.get("group"), v.get("group"))
    for e in all_events:
        e.section = vmeta.get(e.venue, {}).get("section", "poppodium")
        e.price = display_price(normalize_price(canonical_price(e.price)))
        if e.price and not e.price_src:
            e.price_src = "shop" if e.source == "stager" else "list"
        elif not e.price:
            e.price_src = None
        if is_ticket_url(e.url):
            # de kaart linkt altijd naar de agenda van het podium, nooit naar een ticketshop; de shoplink blijft apart bewaard
            e.ticket_url = e.ticket_url or e.url
            e.url = vmeta.get(e.venue, {}).get("url") or e.url
        e.artists = extract_artists(e.title, e.subtitle)
        if e.lineup:
            known = {artist_key(a) for a in e.artists}
            e.artists += [a for a in e.lineup if artist_key(a) not in known and len(artist_key(a)) > 1][: 15 - len(e.artists)]
            if not e.subtitle and len(e.lineup) > 1:
                e.subtitle = "met " + ", ".join(e.lineup[:8]) + (" e.a." if len(e.lineup) > 8 else "")
        # tekst-hints uit eerdere runs (cache) die geen genre zijn maar een gewoon woord ("twee", "liedjes", "piano"): weg
        e.genres = [g for g in e.genres if not (g == g.lower() and g in _HINT_NOISE)]
        e.genre_norm, unk = normalize_genres(e.genres, e.title, e.subtitle or "")
        for u in unk:
            unknown_genres[u] = unknown_genres.get(u, 0) + 1
        e.kind, sure = classify_kind_ex(e.title, e.subtitle, e.genres, e.genre_norm, e.start, kind_learn)
        if sure:
            learn_kinds(kind_learn, e.genres, e.kind)
        e.subgenres, unk_sub = normalize_subgenres(e.genres, e.genre_norm, sub_learn)
        learn_subgenres(sub_learn, unk_sub, e.genre_norm)
        e.price_num = price_number(e.price)
        e.free = e.price_num == 0.0
        if e.kind in ("concert", "festival", "club"):
            artistdb.record_event(adb, e.artists, e.venue, e.genres, e.genre_norm, f"{e.venue}|{e.url}", seen_ev)
    try:
        artistdb.musicbrainz_lookup(adb, budget=int(os.environ.get("MB_BUDGET", "150")), log=log)
        artistdb.spotify_lookup(adb, budget=int(os.environ.get("SPOTIFY_BUDGET", "300")), log=log)
    except Exception as ex:  # noqa: BLE001
        log(f"externe lookup mislukt: {type(ex).__name__}: {str(ex)[:120]}")
    artistdb.derive_genres(adb)
    filled = 0
    for e in all_events:
        if not e.genre_norm and e.artists:
            g, subs = artistdb.genres_for(adb, e.artists)
            if g:
                e.genre_norm = g
                if not e.genres:
                    e.genres = subs
                filled += 1
    artistdb.save(adb)
    seen_ev_path.write_text(json.dumps(sorted(seen_ev)[-30000:]))
    nk, ns = promote_kinds(kind_learn), promote_subgenres(sub_learn)
    for e in all_events:
        if not e.subgenres:
            e.subgenres, _ = normalize_subgenres(e.genres, e.genre_norm, sub_learn)
    kind_learn_path.write_text(json.dumps(kind_learn, ensure_ascii=False, indent=0), encoding="utf-8")
    sub_learn_path.write_text(json.dumps(sub_learn, ensure_ascii=False, indent=0), encoding="utf-8")
    log(f"Zelflerend: {len(kind_learn.get('accepted', {}))} tag->type-koppelingen (+{nk}), {len(sub_learn.get('accepted', {}))} geleerde subgenres (+{ns})")
    report["learned"] = {"kind_tags": kind_learn.get("accepted", {}), "subgenres": sub_learn.get("accepted", {})}
    report["subgenre_labels"] = {k: subgenre_label(k) for e in all_events for k in e.subgenres}
    report["subgenre_labels"].update({k: v["label"] for k, v in sub_learn.get("accepted", {}).items()})
    report["subgenre_groups"] = {k: subgenre_group(k) for e in all_events for k in e.subgenres}
    report["subgenre_groups"].update({k: v["group"] for k, v in sub_learn.get("accepted", {}).items()})
    log(f"Artiestenbank: {len(adb)} artiesten; {filled} events kregen genre via de kennisbank")

    # --- reeksengeheugen: prijs/tijd van terugkerende events onthouden en aanvullen ---
    sdb, sseen = seriesdb.load()
    for e in all_events:
        seriesdb.record(sdb, sseen, e.venue, e.title, f"{e.venue}|{e.url}", e.price, e.start, e.kind)
    est_p = est_t = est_k = 0
    for e in all_events:
        gp, gt, gk = seriesdb.guess(sdb, e.venue, e.title)
        # type: een reeks die eerder duidelijk als feest/talk/… herkend werd, corrigeert het standaardtype "concert"
        if gk and gk != e.kind and e.kind == "concert":
            e.kind = gk
            est_k += 1
        # prijs wordt NOOIT geschat: alleen wat op de site staat telt (de reeks onthoudt prijzen wel, voor onderzoek)
        if gt and not (len(e.start) > 10 and e.start[11:16] != "00:00"):
            e.start, e.time_est = f"{e.start[:10]}T{gt}", True
            est_t += 1
    seriesdb.save(sdb, sseen)
    log(f"Reeksengeheugen: {len(sdb)} reeksen; {est_t} tijden en {est_k} typen overgenomen uit eerdere edities (prijzen worden nooit geschat)")
    report["series"] = len(sdb)
    report["estimated"] = {"price": est_p, "time": est_t, "kind": est_k}
    report["unknown_genres"] = dict(sorted(unknown_genres.items(), key=lambda x: -x[1])[:150])
    report["artists"] = len(adb)
    report["genre_groups"] = {k: v["label"] for k, v in _taxonomy()[0].items()}
    for rv in report["venues"]:
        meta = vmeta.get(rv["name"], {})
        rv["section"] = meta.get("section", "poppodium")
        rv["capacity"] = meta.get("capacity")
    report["kinds"] = {k: sum(1 for e in all_events if e.kind == k) for k in ("concert", "club", "festival", "talk", "other")}
    # herkomst van prijzen en tijden: hoeveel komt van een shop/lijst en hoeveel uit zwakke tekstlezing (kwaliteitsmaat per run)
    report["price_src"] = {k: sum(1 for e in all_events if e.price_src == k) for k in ("shop", "list", "jsonld", "labeled", "embedded", "text")}
    report["price_src"]["none"] = sum(1 for e in all_events if not e.price)
    report["time_src"] = {k: sum(1 for e in all_events if e.time_src == k) for k in ("label", "after_date", "paren", "schedule", "embedded")}
    log("Prijsherkomst: " + ", ".join(f"{k} {n}" for k, n in report["price_src"].items()) + " · tijd uit pagina: " + ", ".join(f"{k} {n}" for k, n in report["time_src"].items() if n))

    # --- archief: elk event dat ooit is gezien blijft bewaard (onderzoeksdata: programmering, prijzen, genres per podium) ---
    arch_path = DATA / "archive.json"
    archive = json.loads(arch_path.read_text(encoding="utf-8")) if arch_path.exists() else {}
    arch_migrated = 0
    for e in all_events:
        k = e.id
        old = id_info["legacy"].get(k)
        if k not in archive and old and old in archive:   # archiefrecord onder de oude url-sleutel meenemen
            archive[k] = archive.pop(old)
            arch_migrated += 1
        rec = archive.get(k, {"first_seen": e.first_seen or TODAY.isoformat()})
        rec.update({"venue": e.venue, "city": e.city, "title": e.title, "start": e.start, "url": e.url, "subtitle": e.subtitle,
                    "genres": e.genres, "genre_norm": e.genre_norm, "subgenres": e.subgenres, "kind": e.kind, "price": e.price,
                    "price_num": e.price_num, "status": e.status, "artists": e.artists, "lineup": e.lineup, "section": e.section,
                    "last_seen": TODAY.isoformat()})
        archive[k] = rec
    arch_path.write_text(json.dumps(archive, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    report["archive"] = len(archive)
    log(f"Archief: {len(archive)} events bewaard (data/archive.json)" + (f"; {arch_migrated} records naar de nieuwe id verhuisd" if arch_migrated else ""))

    report["groundtruth"] = check_groundtruth(all_events)
    all_events.sort(key=lambda e: (e.start, e.venue, e.title))
    (DATA / "events.json").write_text(json.dumps([asdict(e) for e in all_events], ensure_ascii=False, indent=1), encoding="utf-8")
    report["total_events"] = len(all_events)
    (DATA / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    # het logboek van deze run meeschrijven (data/run.log), zodat een mislukt podium ook zonder de GitHub-console te onderzoeken is
    (DATA / "run.log").write_text("\n".join(_LOG)[-400000:], encoding="utf-8")
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    ok = sum(1 for r in report["venues"] if r["ok"])
    log(f"\nKlaar: {len(all_events)} events uit {ok}/{len(report['venues'])} podia")
    (DATA / "run.log").write_text("\n".join(_LOG)[-400000:], encoding="utf-8")
    if _STRAGGLERS:
        # een hangende podium-thread zou het proces open houden tot GitHub het na 6 uur afbreekt: alles is geschreven, dus hard stoppen
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    socket.setdefaulttimeout(60)   # vangnet voor netwerkcalls zonder eigen timeout
    sys.exit(main(sys.argv[1:] or None))

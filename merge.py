"""merge.py — samenvoegen en herlabelen: dedupe (zelfde event uit twee bronnen), merge_coproductions (twee podia, één plek),
relabel_by_location (Paradiso in Tolhuistuin), category_tags (@Locatie/genre-tags van podiumfilters), prijs met sterkste herkomst."""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests

from taxonomy import GENERIC_TITLE
from taxonomy import strip_city, _fold
from common import Event, TODAY, as_list, log
from net import http_get
from extract import _price_rank, _richness, _same_event, _title_key, is_ticket_url
from sources import event_links


def category_tags(v: dict, cache: dict) -> tuple[dict[str, set[str]], set[str]]:
    """Podium-eigen genre-/type-/locatiefilters als bron van tags (venues.yaml -> category_pages).

    Veel podia hebben op hun agenda een filter per genre, type of locatie (Tivoli: ?sf_genre=pop, Doornroosje: ?genre=metal
    en ?location=de-vereeniging, FLUOR: /programma/categorie/dance/). Die indeling is door de programmeurs zelf gemaakt en
    dus betrouwbaarder dan woorden uit de beschrijving. Per filter (slug) worden de overzichtspagina('s) opgehaald en de
    eventlinks verzameld. Resultaat: (eventurl -> {tags}, uit te sluiten eventurls). De tags gaan door dezelfde regels
    (genres.yaml) als andere podiumtags en sturen zo hoofdgenre, subgenre én type (Muziek/Overig); `exclude`-slugs
    (bv. een locatiefilter voor een ander gebouw dat al als eigen podium in venues.yaml staat) verwijderen events.
    Eén keer per dag per podium gecached (cache-sleutel catpages|<podium>). Eén filtergroep of een lijst:

      category_pages:
        - url:   "https://…/programma/?genre={slug}"            # eerste pagina
          paged: "https://…/programma/page/{n}/?genre={slug}"   # optioneel: vervolgpagina's (n >= 2); stop bij 404/geen nieuwe links
          max_pages: 30
          link_pattern: "…"                                     # optioneel: alleen voor deze pagina's (anders die van het podium)
          tags: {pop: Pop, "kennis-debat": "Kennis & debat", "soul-funk-jazz": [Soul, Funk, Jazz]}
        - url: "https://…/programma/?location={slug}"
          exclude: [de-vereeniging, waalhalla]
    """
    cfgs = as_list(v.get("category_pages") or [])
    cfgs = [c for c in cfgs if c.get("url") and (c.get("tags") or c.get("exclude"))]
    if not cfgs:
        return {}, set()
    key = f"catpages|{v['name']}"
    c = cache.get(key)
    if c and c.get("fetched") == TODAY.isoformat() and "map" in c:
        return {u: set(t) for u, t in c["map"].items()}, set(c.get("exclude", []))
    base = v["url"]
    out: dict[str, set[str]] = {}
    excl: set[str] = set()
    delay = float(v.get("crawl_delay", 0.6))
    pages_read = n_cat = 0

    def links_for(cfg: dict, slug: str) -> set[str]:
        nonlocal pages_read
        vv = dict(v, link_pattern=cfg["link_pattern"]) if cfg.get("link_pattern") else v
        seen: set[str] = set()
        for n in range(1, int(cfg.get("max_pages", 30)) + 1):
            if n == 1:
                page_url = cfg["url"].format(slug=slug)
            elif cfg.get("paged"):
                page_url = cfg["paged"].format(slug=slug, n=n + int(cfg.get("page_offset", 0)))
            else:
                break
            try:
                r = http_get(page_url)
            except requests.RequestException:
                break
            time.sleep(delay)
            if r.status_code != 200:
                break
            pages_read += 1
            new = [l for l in event_links(vv, r.text, base) if l not in seen]
            if not new:
                break
            seen.update(new)
        return {l.rstrip("/") for l in seen}

    for cfg in cfgs:
        for slug, labels in (cfg.get("tags") or {}).items():
            n_cat += 1
            for l in links_for(cfg, slug):
                out.setdefault(l, set()).update(as_list(labels))
        for slug in as_list(cfg.get("exclude") or []):
            n_cat += 1
            excl.update(links_for(cfg, slug))
    log(f"    podiumfilters: {n_cat} categorieën, {pages_read} pagina's, {len(out)} events getagd, {len(excl)} uitgesloten")
    cache[key] = {"fetched": TODAY.isoformat(), "map": {u: sorted(t) for u, t in out.items()}, "exclude": sorted(excl)}
    return out, excl


def apply_category_tags(evs: list[Event], cats: dict[str, set[str]], exclude: set[str] | None = None) -> tuple[list[Event], int]:
    """Voegt de podiumfilter-tags toe aan e.genres (vooraan: ze zijn betrouwbaarder dan tekst-hints) en verwijdert
    uitgesloten events. Geeft (events, aantal getagde events)."""
    n = 0
    kept = []
    for e in evs:
        u = (e.url or "").rstrip("/")
        if exclude and u in exclude:
            continue
        kept.append(e)
        tags = cats.get(u)
        if not tags:
            continue
        n += 1
        # "@Merleyn": geen genre maar een locatie (Doornroosje ?location=merleyn) -> relabel_by_location verhuist het event
        locs = [t[1:] for t in tags if t.startswith("@")]
        if locs and not e.location:
            e.location = locs[0]
        tags = {t for t in tags if not t.startswith("@")}
        e.genres = sorted(tags) + [g for g in e.genres if g not in tags]
    return kept, n


def relabel_by_location(events: list[Event], venues: list[dict]) -> int:
    """Een podium dat ook elders programmeert (Paradiso in Tolhuistuin, Doornroosje in De Vereeniging) levert events die
    bij dat andere gebouw horen. Staat de genoemde locatie zelf als podium in venues.yaml, dan verhuist het event
    daarheen (venue/stad); de dedupe hieronder haalt daarna de dubbele met het eigen programma van dat podium weg."""
    by_name = {}
    for v in venues:
        by_name[_fold(v["name"])] = v
        for a in as_list(v.get("aliases") or []):
            by_name[_fold(a)] = v
    n = 0
    for e in events:
        if not e.location and not e.subtitle:
            continue
        f = _fold(e.location or "")
        target = (by_name.get(f) or next((v for k, v in by_name.items() if len(k) > 4 and (f.startswith(k + " ") or f.startswith(k + " -"))), None)) if f else None
        if target and target["name"] != e.venue:
            e.venue, e.city = target["name"], target["city"]
            n += 1
        elif not target and e.subtitle:
            # "New Yorkse blackmetal cultband | in De Helling": podiumnaam in de ondertitel
            for k, tv in by_name.items():
                if len(k) > 4 and tv["name"] != e.venue and tv["city"] == e.city and re.search(r"\b(?:in|@|at|bij)\s+" + re.escape(k) + r"\b", _fold(e.subtitle)):
                    e.venue, e.city = tv["name"], tv["city"]
                    n += 1
                    break
    return n


def dedupe(events: list[Event]) -> list[Event]:
    """Dubbele events weg: dezelfde URL binnen een podium, en daarna hetzelfde podium + dag + titel (bv. na verhuizing op
    locatie, of als een site een event twee keer toont). De rijkste versie (tijd, prijs, genres, line-up) blijft."""
    seen, out = {}, []
    for e in events:
        key = (e.venue, e.url.rstrip("/").lower()) if e.url else (e.venue, e.title.lower(), e.start[:10])
        if key in seen:
            old = seen[key]
            if _richness(e) > _richness(old):
                out[out.index(old)] = e
                seen[key] = e
            continue
        seen[key] = e
        out.append(e)
    by_day: dict[tuple, list[Event]] = {}
    final = []
    for e in out:
        day_key = (e.venue, e.start[:10])
        def _two_shows(o: Event) -> bool:
            # twee voorstellingen op één dag (Ziggo Dome: Roxy Dekker 15:00 en 20:30, eigen URL per show): geen dubbele
            sa, sb = o.start[11:16], e.start[11:16]
            if sa in ("", "00:00") or sb in ("", "00:00") or (o.url or "").rstrip("/") == (e.url or "").rstrip("/"):
                return False
            return abs((int(sa[:2]) * 60 + int(sa[3:])) - (int(sb[:2]) * 60 + int(sb[3:]))) >= 120
        old = next((o for o in by_day.get(day_key, []) if _same_event(o, e) and not _two_shows(o)), None)
        if old is not None:
            rich, poor = (e, old) if _richness(e) > _richness(old) else (old, e)
            # velden aanvullen uit de armere versie; de URL van de eigen site gaat vóór die van een ticketshop
            _take_better_price(rich, poor)
            if rich.start[11:16] in ("", "00:00") and poor.start[11:16] not in ("", "00:00"):
                rich.start = poor.start
            if not rich.genres and poor.genres:
                rich.genres = poor.genres
            if not rich.subtitle and poor.subtitle:
                rich.subtitle = poor.subtitle
            if is_ticket_url(rich.url) and poor.url and not is_ticket_url(poor.url):
                rich.ticket_url, rich.url = rich.url, poor.url
            elif is_ticket_url(poor.url) and not rich.ticket_url:
                rich.ticket_url = poor.url
            if rich is e:
                final[final.index(old)] = e
                by_day[day_key][by_day[day_key].index(old)] = e
            continue
        by_day.setdefault(day_key, []).append(e)
        final.append(e)
    return merge_coproductions(final)


def _take_better_price(rich: Event, poor: Event) -> None:
    """Bij het samenvoegen van twee versies van een event wint de prijs met de sterkste herkomst (PRICE_RANK), niet
    per se die van de rijkste versie: een Stager-shopprijs (incl. servicekosten) gaat vóór een los bedrag uit paginatekst."""
    if rich.price == "uitverkocht":
        return
    if poor.price and (not rich.price or _price_rank(poor.price_src) > _price_rank(rich.price_src)):
        rich.price, rich.price_src = poor.price, poor.price_src


_LOC_IN_TITLE = re.compile(r"\s*[|@•]\s*(?:in\s+|@\s*)?([A-Z][\w'’&.\-]*(?: [A-Z][\w'’&.\-]*){0,3})\s*$")


def merge_coproductions(events: list[Event]) -> list[Event]:
    """Zelfde event bij twee podia in dezelfde stad, zelfde dag en (vrijwel) zelfde tijd = een samenwerking op één plek
    (BIRD + Rotown: "Zwangere Guy | Maassilo Rotterdam" en "Zwangere Guy", beide do 3 dec 20:00). Eén kaart blijft, met
    de medeorganisator in co_venues en de plek (uit de titel, "| Maassilo Rotterdam") in location."""
    by_key: dict[tuple, list[Event]] = {}
    out: list[Event] = []
    for e in events:
        if e.start[11:16] in ("", "00:00"):
            out.append(e)
            continue
        key = (_fold(e.city), e.start[:10])
        st = datetime.fromisoformat(e.start)
        match = None
        for o in by_key.get(key, []):
            if o.venue != e.venue and o.venue not in e.co_venues and abs((datetime.fromisoformat(o.start) - st).total_seconds()) <= 1800 \
                    and _same_event(o, e, strict=True) and not GENERIC_TITLE.search(_title_key(e.title)) and len(_title_key(e.title)) >= 5:
                match = o
                break
        if match is None:
            by_key.setdefault(key, []).append(e)
            out.append(e)
            continue
        rich, poor = (e, match) if _richness(e) > _richness(match) else (match, e)
        rich.co_venues = sorted(set(rich.co_venues + poor.co_venues + [poor.venue]) - {rich.venue})
        for t in (rich.title, poor.title):
            m = _LOC_IN_TITLE.search(t)
            if m and not rich.location:
                rich.location = strip_city(m.group(1).strip(), rich.city)
        if rich.location:
            rich.title = _LOC_IN_TITLE.sub("", rich.title).strip() or rich.title
        _take_better_price(rich, poor)
        if not rich.genres and poor.genres:
            rich.genres = poor.genres
        if not rich.subtitle and poor.subtitle:
            rich.subtitle = poor.subtitle
        if rich is e:
            out[out.index(match)] = e
            by_key[key][by_key[key].index(match)] = e
    return out


def event_key(e: Event) -> str:
    """Inhoudelijke identiteit van een event: podium|dag|titelsleutel. Onafhankelijk van de url, zodat een podium dat zijn
    urls herschrijft (of een event dat via een andere bron binnenkomt: shop i.p.v. agenda, /en/ i.p.v. /nl/) geen 'nieuw'
    event en geen dubbel archiefrecord oplevert."""
    return f"{e.venue}|{e.start[:10]}|{_title_key(e.title) or _fold(e.title)}"


def assign_ids(events: list[Event], seen: dict, ids: dict, vmeta: dict) -> dict:
    """Geeft elk event een stabiele `id`. Volgorde:
    1. de url-alias: is deze bron-url (podium|url) eerder aan een id gekoppeld (state/ids.json), dan die id — zo blijft een
       event dezelfde identiteit houden als de titel verandert ("+ support") of de datum verschuift (verplaatst);
    2. anders de inhoudelijke sleutel podium|dag|titelsleutel (event_key); twee shows op één dag met dezelfde titel
       (Roxy Dekker 15:00 en 20:30) krijgen de tijd erachter;
    3. migratie: bestond het event al onder de oude url-sleutel (podium|url) in seen.json, dan verhuist de first_seen-datum mee.
    De agenda-overzichtspagina zelf is geen alias (daar hangen meerdere events aan). Geeft {"migrated": n, "legacy": {id: podium|url}}."""
    url_to_id: dict = ids.setdefault("url", {})
    taken: dict[str, Event] = {}
    legacy: dict[str, str] = {}
    migrated = 0
    for e in sorted(events, key=lambda x: (x.start, x.venue, x.title)):
        src_key = f"{e.venue}|{e.url}"
        base = (vmeta.get(e.venue, {}).get("url") or "").rstrip("/")
        aliasable = bool(e.url) and e.url.rstrip("/") != base and not e.url.rstrip("/").endswith(("/agenda", "/programma", "/events", "/evenementen"))
        eid = url_to_id.get(src_key) if aliasable else None
        if eid and eid in taken:
            eid = None   # twee events met dezelfde bron-url: de alias is niet uniek, val terug op de inhoud
        if not eid:
            base_id = event_key(e)
            eid = base_id
            if eid in taken:
                eid = f"{base_id}|{e.start[11:16]}"
            n = 2
            while eid in taken:
                eid = f"{base_id}|{e.start[11:16]}|{n}"
                n += 1
            if eid not in seen and src_key in seen:
                seen[eid] = seen.pop(src_key)
                migrated += 1
        taken[eid] = e
        e.id = eid
        legacy[eid] = src_key
        if aliasable:
            url_to_id[src_key] = eid
    # aliassen van events die uit seen.json zijn verdwenen (> 60 dagen weg) opruimen
    live_ids = set(taken) | set(seen)
    for k in [k for k, v in url_to_id.items() if v not in live_ids]:
        del url_to_id[k]
    return {"migrated": migrated, "legacy": legacy}

"""sources.py — de bronstrategieën per podium (strat_*: jsonld, microdata, embedded, tribe, wp_event, json_api, stager,
jsonld_detail, sitemap_detail, graphql_detail, html/html_api/facetwp/presets), eventpagina's lezen (fetch_detail, detail_extra,
apply_extra, enrich_from_detail) en de detailcache (CACHE_VERSION). Alles wat een site in Events omzet staat hier."""
from __future__ import annotations

import json
import re
from functools import lru_cache
import time
from dataclasses import asdict
from datetime import datetime, date, timedelta, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

from taxonomy import normalize_tag, price_number, genre_hints, _fold
import net as _net
from common import EVENT_LINK_HINTS, Event, ROOT, TIMEOUT, TODAY, as_list, clean, in_window, log, page_text, soup_of
from net import BROWSER_HEADERS, _BROWSER_UA_HOSTS, get, http_get, http_post
from extract import _DISCOUNT_CTX, _PREFERRED_CTX, _ampm_to_24h, _fee_inclusive, _strip_tz, _title_key, clean_lineup, date_from_url, extract_from_text, extract_from_text_ex, fmt_price, jsonld_events, normalize_price, parse_dt, price_from_embedded_json, times_from_embedded_json, to_local


def event_from_jsonld(n: dict, v: dict, page_url: str, source: str) -> Event | None:
    if v.get("time_is_local"):
        n = {**n, "startDate": _strip_tz(n.get("startDate")), "endDate": _strip_tz(n.get("endDate")), "doorTime": _strip_tz(n.get("doorTime"))}
    start = parse_dt(n.get("startDate"))
    if not start:
        return None
    url = n.get("url") or page_url
    if isinstance(url, dict):
        url = url.get("@id") or page_url
    if source in ("jsonld_detail", "x") and isinstance(url, str) and "://" in url and "://" in page_url \
            and urlparse(url).netloc.replace("www.", "").lower() != urlparse(page_url).netloc.replace("www.", "").lower():
        url = page_url   # Metropool: JSON-LD op de eventpagina wijst naar de site van een ander podium (zelfde CMS-leverancier); de pagina zelf is de bron
    offers = as_list(n.get("offers"))
    price = None
    if offers and isinstance(offers[0], dict):
        p = offers[0].get("price") or offers[0].get("lowPrice")
        price = f"€ {p}" if p not in (None, "", "0", 0) else ("gratis" if p in ("0", 0) else None)
    status = None
    es = str(n.get("eventStatus", ""))
    if "Cancelled" in es:
        status = "afgelast"
    elif "Postponed" in es or "Rescheduled" in es:
        status = "verplaatst"
    genres = [clean(g) for g in as_list(n.get("genre")) + as_list(n.get("keywords")) if clean(g)]
    if len(genres) == 1 and "," in genres[0]:
        genres = [g.strip() for g in genres[0].split(",")]
    lineup = [clean(p.get("name")) if isinstance(p, dict) else clean(str(p)) for p in as_list(n.get("performer"))]
    lineup = [x for x in lineup if x and len(x) <= 60]
    perf = ", ".join(lineup) or None
    if not genres and n.get("description"):
        genres = genre_hints(str(n.get("description")))
    return Event(
        venue=v["name"], city=v["city"], title=clean(n.get("name")) or "(zonder titel)",
        start=start.isoformat(timespec="minutes"), url=urljoin(page_url, str(url)),
        end=(parse_dt(n.get("endDate")) or start).isoformat(timespec="minutes") if n.get("endDate") else None,
        subtitle=perf if perf and perf.lower() != (clean(n.get("name")) or "").lower() else None,
        genres=genres, price=price, status=status, source=source, lineup=lineup,
        location=_ld_location(n, v),
    )


def _ld_location(n: dict, v: dict) -> str | None:
    """Locatienaam uit JSON-LD als die afwijkt van het podium zelf (Rotown programmeert in V11, LantarenVenster, Annabel…)."""
    loc = n.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    name = loc.get("name") if isinstance(loc, dict) else (loc if isinstance(loc, str) else None)
    name = clean(name) if name else None
    if not name or _fold(name) == _fold(v["name"]) or _fold(v["name"]) in _fold(name) or _fold(name) in _fold(v["name"]):
        return None
    return name


_LOC_TEXT = re.compile(r"(?i:vindt plaats (?:in|bij|op)|takes place (?:in|at)|locatie\s*[:\-]?|location\s*[:\-]?|\|\s*(?:in|@|at)|(?:^|\s)@)\s*((?:de |het |'t |the )?[A-Z0-9][\w'’&.\-]*(?: [A-Z0-9][\w'’&.\-]*){0,3})", re.M)


@lru_cache(maxsize=1)
def _known_venue_names() -> dict[str, str]:
    """gefolde naam/alias -> podiumnaam, uit venues.yaml (voor locatieherkenning in tekst)."""
    out = {}
    try:
        for v in yaml.safe_load((ROOT / "venues.yaml").read_text(encoding="utf-8")) or []:
            out[_fold(v["name"])] = v["name"]
            for a in as_list(v.get("aliases") or []):
                out[_fold(a)] = v["name"]
    except Exception:  # noqa: BLE001
        pass
    return out


def location_from_text(txt: str, v: dict) -> str | None:
    """Andere locatie uit de paginatekst: Tivoli "Dit concert vindt plaats in De Helling, Helling 7 in Utrecht" /
    ondertitel "… | in De Helling". Alleen podia uit venues.yaml tellen; zaalnamen ("Locatie: Ronda") zijn geen locatie."""
    known = _known_venue_names()
    for m in _LOC_TEXT.finditer(txt[:6000]):
        cand = m.group(1).strip(" ,.")
        f = _fold(cand)
        if not cand or f == _fold(v["name"]) or f in _fold(v["name"]):
            continue
        hit = known.get(f) or next((n for k, n in known.items() if len(k) > 4 and f.startswith(k + " ")), None)
        if hit and hit != v["name"]:
            return hit
    return None


# ----------------------------------------------------------------------------
# strategieën
# ----------------------------------------------------------------------------

def strat_microdata(v: dict, html: str) -> list[Event]:
    """schema.org-microdata in de HTML: <div itemscope itemtype=".../Event"> met itemprop name/startDate/url/offers."""
    s = soup_of(html)
    out = []
    for it in s.select('[itemtype*="schema.org/Event"], [itemtype*="schema.org/MusicEvent"], [itemtype*="schema.org/Festival"]'):
        def prop(name):
            el = it.find(attrs={"itemprop": name})
            if el is None:
                return None
            return el.get("content") or el.get("datetime") or el.get("href") or clean(el.get_text())
        # name: de eerste itemprop=name die niet in een geneste location zit
        name = None
        for el in it.find_all(attrs={"itemprop": "name"}):
            if not el.find_parent(attrs={"itemprop": "location"}) and not el.find_parent(attrs={"itemprop": "performer"}):
                name = el.get("content") or clean(el.get_text())
                if name:
                    break
        start = parse_dt(prop("startDate"))
        if not (name and start):
            continue
        url = prop("url") or v["url"]
        price = None
        for pe in it.find_all(attrs={"itemprop": ["price", "lowPrice"]}):
            price = fmt_price(pe.get("content") or clean(pe.get_text()))
            if price:
                break
        genres = [clean(g.get("content") or g.get_text()) for g in it.find_all(attrs={"itemprop": "genre"})]
        sub_el = it.select_one(".subtitle, .subtitel, .sub, .support, .tagline")
        status = None
        es = prop("eventStatus") or ""
        if "Cancelled" in es:
            status = "afgelast"
        elif it.find(class_=re.compile(r"sold-?out|uitverkocht", re.I)):
            status = "uitverkocht"
        end = parse_dt(prop("endDate"))
        out.append(Event(venue=v["name"], city=v["city"], title=clean(name) or "?", start=start.isoformat(timespec="minutes"),
                         url=urljoin(v["url"], url), end=end.isoformat(timespec="minutes") if end else None,
                         subtitle=clean(sub_el.get_text()) if sub_el else None, genres=[g for g in genres if g],
                         price=price, status=status, source="microdata"))
    return out


def strat_jsonld(v: dict, html: str) -> list[Event]:
    evs = [event_from_jsonld(n, v, v["url"], "jsonld") for n in jsonld_events(html)]
    return [e for e in evs if e]


# --- ingebedde JSON -----------------------------------------------------------

DATE_KEYS = re.compile(r"^(start|event|begin)?_?(date|datum|start|starts?_?at|time|startdate|eventdate|date_start|start_date|machine)$", re.I)
TITLE_KEYS = ("title", "name", "naam", "titel")


def _find_date(obj: dict) -> datetime | None:
    for k, val in obj.items():
        if DATE_KEYS.match(k) and isinstance(val, (str, int, float)):
            dt = parse_dt(val)
            if dt and 2000 < dt.year < 2100:
                if isinstance(val, (int, float)) and dt.minute == 0 and dt.hour in (0, 1, 2, 12, 13, 14):
                    dt = dt.replace(hour=0, minute=0)  # timestamp op middernacht/middag UTC = alleen een datum
                return dt
    for k, val in obj.items():
        if isinstance(val, dict) and re.search(r"date|start|datum", k, re.I):
            dt = _find_date(val)
            if dt:
                return dt
    return None


def _find_title(obj: dict) -> str | None:
    for k in TITLE_KEYS:
        val = obj.get(k)
        if isinstance(val, str) and val.strip():
            return clean(val)
        if isinstance(val, dict):
            for kk in ("rendered", "nl", "en"):
                if isinstance(val.get(kk), str) and val[kk].strip():
                    return clean(val[kk])
    return None


def _find_url(obj: dict) -> str | None:
    for k in ("url", "link", "permalink", "slug", "endpoint", "path", "href"):
        val = obj.get(k)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict) and isinstance(val.get("nl"), str):
            return val["nl"]
    return None


def _find_genres(obj: dict) -> list[str]:
    out = []
    for k in ("eventType", "event_type", "category", "type", "profile"):
        val = obj.get(k)
        if isinstance(val, dict):
            n = val.get("label") or val.get("name") or val.get("title")
            if isinstance(n, str) and n.lower() not in ("event", "events", "evenement"):
                out.append(clean(n))
    for k in ("genres", "genre", "tags", "categories", "categorieen", "styles"):
        for g in as_list(obj.get(k)):
            if isinstance(g, str) and not g.isdigit():
                out.append(clean(g))
            elif isinstance(g, dict):
                n = g.get("name") or g.get("title") or g.get("label")
                if isinstance(n, dict):
                    n = n.get("nl") or n.get("en")
                if isinstance(n, str):
                    out.append(clean(n))
    return [g for g in out if g]


def _walk_event_lists(obj, depth=0, found=None):
    """Zoekt lijsten van dicts die op events lijken (titel + datum)."""
    if found is None:
        found = []
    if depth > 40:
        return found
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if len(dicts) >= 5:
            # JSON:API-stijl {type, attributes}
            flat = [x.get("attributes", x) if isinstance(x.get("attributes"), dict) else x for x in dicts]
            hits = [x for x in flat if _find_title(x) and _find_date(x)]
            if len(hits) >= max(5, len(flat) // 2):
                found.append(hits)
                return found
        for x in obj:
            _walk_event_lists(x, depth + 1, found)
    elif isinstance(obj, dict):
        for val in obj.values():
            _walk_event_lists(val, depth + 1, found)
    return found


def strat_embedded(v: dict, html: str) -> list[Event]:
    s = soup_of(html)
    blobs = []
    for sc in s.find_all("script"):
        t = (sc.get("type") or "").lower()
        if sc.get("id") == "__NEXT_DATA__" or "json" in t and "ld+json" not in t:
            blobs.append(sc.string or sc.get_text())
    # JSON in HTML-attributen (Vue-props zoals :all-items='[{&quot;...', data-events="...")
    for m in re.finditer(r"=(['\"])((?:\[\{|\{)&quot;.{200,}?)\1", html, re.S):
        blobs.append(unescape(m.group(2)))
    # ook window.__NUXT__ / __INITIAL_STATE__ als JSON-literal
    for m in re.finditer(r"window\.__(?:NUXT|INITIAL_STATE|APOLLO_STATE|PRELOADED_STATE)__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S):
        blobs.append(m.group(1))
    # alle eventlijsten samenvoegen (Melkweg: per maand een lijst; alleen de grootste nemen gaf 235 van 283 events);
    # dubbelen (zelfde url/id of titel+datum) vallen weg
    best: list[dict] = []
    seen_keys: set = set()
    for raw in blobs:
        if not raw or len(raw) < 200:
            continue
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for lst in sorted(_walk_event_lists(j), key=len, reverse=True):
            for o in lst:
                k = _find_url(o) or o.get("id") or (str(_find_title(o)), str(_find_date(o)))
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                best.append(o)
    out = []
    for o in best:
        title, dt = _find_title(o), _find_date(o)
        if not (title and dt):
            continue
        if dt.hour == 0 and dt.minute == 0:
            # tijd staat soms los van de datum (Effenaar: times.starts_at)
            tm = None
            for k in ("times", "time", "tijden"):
                if isinstance(o.get(k), dict):
                    tm = o[k].get("starts_at") or o[k].get("start") or o[k].get("aanvang")
            tm = tm or o.get("starts_at") or o.get("start_time") or o.get("startTime") or o.get("aanvang")
            tm = _ampm_to_24h(tm) if isinstance(tm, str) else tm
            if isinstance(tm, str) and re.fullmatch(r"\d{1,2}[:.]\d{2}(:\d{2})?", tm.strip()):
                hh, mm = re.split(r"[:.]", tm.strip())[:2]
                dt = dt.replace(hour=int(hh), minute=int(mm))
        if v.get("url_template") and o.get("id") is not None:
            url = v["url_template"].format(**{k: str(val) for k, val in o.items() if isinstance(val, (str, int))})
        else:
            url = _find_url(o) or v["url"]
        if url and not url.startswith("http"):
            # Effenaar: slug; Melkweg: /nl/agenda/slug
            url = urljoin(v["url"], url)
        profile = o.get("profile") or o.get("type") or o.get("category")
        extra_genres = [clean(profile)] if isinstance(profile, str) and profile.lower() not in ("event", "events", "evenement") else []
        status = None
        for k in ("status", "state", "eventStatus"):
            sv = o.get(k)
            if isinstance(sv, str) and re.search(r"afgelast|cancel|uitverkocht|sold", sv, re.I):
                status = "afgelast" if re.search(r"afgelast|cancel", sv, re.I) else "uitverkocht"
        if o.get("isCancelled") is True or o.get("cancelled") is True:
            status = "afgelast"
        elif o.get("isSoldOut") is True or o.get("soldOut") is True or o.get("sold_out") is True:
            status = status or "uitverkocht"
        # Melkweg zet isPublished:false op uitverkochte en afgelaste events (ze verdwijnen van de agendapagina, maar bestaan wel:
        # 49 van 282). Uitverkocht hoort in onze agenda (met 'uitverkocht' als prijs), afgelast ook (doorgestreept);
        # alleen echt ongepubliceerde events zonder status en besloten events slaan we over (isConfirmed is bij Melkweg
        # altijd false en zegt niets)
        if (o.get("isPublished") is False or o.get("publish") is False) and not status:
            continue
        if o.get("isPrivateEvent") is True:
            continue
        price = o.get("price") or o.get("ticket_price") or o.get("ticketPrice") or o.get("priceFrom")
        if price in (None, "", 0) and o.get("freeEvent") is True:
            price = "gratis"
        sub = o.get("subtitle") or o.get("tagline") or o.get("one_liner")
        if not sub and isinstance(o.get("description"), str) and 0 < len(o["description"]) < 140:
            sub = o["description"]
        end = None
        for k in ("endDate", "end_date", "end", "ends_at"):
            if isinstance(o.get(k), str):
                e = parse_dt(o[k])
                end = e.isoformat(timespec="minutes") if e else None
                break
        out.append(Event(
            venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"),
            url=url, end=end, subtitle=clean(sub) if isinstance(sub, str) else None,
            genres=list(dict.fromkeys(_find_genres(o) + extra_genres)), price=fmt_price(price),
            status=status, source="embedded",
        ))
    return out


# --- WordPress: The Events Calendar -----------------------------------------

def strat_tribe(v: dict, base: str) -> list[Event]:
    api = v.get("api") or urljoin(base, "/wp-json/tribe/events/v1/events")
    out, page = [], 1
    while page <= 20:
        r = get(api, params={"per_page": 50, "page": page, "start_date": TODAY.isoformat(), "status": "publish"}, delay=0.5)
        j = r.json()
        for e in j.get("events", []):
            dt = parse_dt(e.get("start_date"))
            if not dt:
                continue
            cats = [clean(c.get("name")) for c in e.get("categories", []) if isinstance(c, dict)]
            cost = clean(e.get("cost")) if e.get("cost") else None
            out.append(Event(venue=v["name"], city=v["city"], title=clean(e.get("title")) or "?", start=dt.isoformat(timespec="minutes"),
                             url=e.get("url") or base, end=(parse_dt(e.get("end_date")) or dt).isoformat(timespec="minutes") if e.get("end_date") else None,
                             genres=[c for c in cats if c and c.lower() not in ("concert", "evenement", "event")], price=cost, source="tribe"))
        if page >= int(j.get("total_pages") or 1):
            break
        page += 1
    return out


# --- WordPress: eigen event-posttype -----------------------------------------

def _stager_acf(acf: dict) -> dict:
    """Stager-WordPress-koppeling (Muziekgieterij e.a.): ACF-velden stager_* met programma-start, deuren, tickets, status.
    Geeft {start, doors, price, subtitle, status} voor zover aanwezig."""
    out: dict = {}
    if not isinstance(acf, dict) or not any(k.startswith("stager_") for k in acf):
        return out
    for k in ("stager_program_start", "stager_doors_open", "stager_production_start"):
        dt = parse_dt(acf.get(k)) if acf.get(k) else None
        if dt:
            out.setdefault("start", dt)
            if k == "stager_doors_open":
                out["doors"] = dt
    if acf.get("stager_production_free") is True:
        out["price"] = "gratis"
    else:
        prices = []
        for t in as_list(acf.get("stager_tickets") or []):
            if not isinstance(t, dict) or t.get("stager_ticket_valid") is False:
                continue
            p = t.get("stager_ticket_price")
            if isinstance(p, (int, float)) and p > 0 and str(t.get("stager_ticket_type", "REGULAR")).upper() in ("REGULAR", "EARLYBIRD", "EARLY_BIRD", "PRESALE", "DOOR"):
                # inclusief servicekosten (stager_ticket_online_fee / *_fee): dat is wat je online betaalt
                fee = sum(float(fv) for fk, fv in t.items() if "fee" in fk and isinstance(fv, (int, float)))
                prices.append((0 if str(t.get("stager_ticket_type")).upper() == "REGULAR" else 1, float(p) + fee))
        if prices:
            out["price"] = fmt_price(min(prices)[1])
    sub = acf.get("stager_production_subtitle")
    if isinstance(sub, str) and sub.strip():
        out["subtitle"] = sub
    if acf.get("production_tickets_soldout") is True:
        out["status"] = "uitverkocht"
    elif acf.get("stager_production_postponed") is True or acf.get("stager_production_moved") is True:
        out["status"] = "verplaatst"
    return out


def _acf_date(acf: dict) -> datetime | None:
    if not isinstance(acf, dict):
        return None
    st = _stager_acf(acf)
    if st.get("start"):
        return st["start"]
    for k in ("date_time", "start_date", "startdate", "date", "datum", "event_date", "start", "begin"):
        if acf.get(k):
            dt = parse_dt(acf[k])
            if dt:
                t = acf.get("time_start") or acf.get("start_time") or acf.get("aanvang")
                if dt.hour == 0 and isinstance(t, str) and re.match(r"\d{1,2}[:.]\d{2}", t):
                    hh, mm = re.split(r"[:.]", t)[:2]
                    dt = dt.replace(hour=int(hh), minute=int(mm))
                return dt
    return None


def strat_wp_event(v: dict, base: str, detail_cache: dict) -> list[Event]:
    api = v.get("api")
    if not api:
        rest_base = None
        try:
            types = get(urljoin(base, "/wp-json/wp/v2/types"), delay=0.5).json()
            cands = [t for t in types.values() if re.search(r"event|evenement|agenda|programma|voorstelling|concert|show", t.get("rest_base", "") + t.get("slug", ""), re.I)
                     and not re.search(r"log|activity|ticket|serie|categor|type$|types$|keynius|label|genre|locat", t.get("rest_base", ""), re.I)]
            # Bibelot: 'eventtype' (zakelijke eventsoorten) en 'keynius-event' (lockers) zijn geen agenda; 'programma' wel
            pref = ["events", "event", "evenementen", "evenement", "programma", "agenda", "voorstellingen", "voorstelling", "concerten", "concert", "shows", "show"]
            cands.sort(key=lambda t: pref.index(t["rest_base"]) if t.get("rest_base") in pref else 99)
            if cands:
                rest_base = cands[0]["rest_base"]
        except (requests.RequestException, ValueError):
            pass
        if not rest_base:
            # typenlijst afgeschermd: gangbare namen proberen
            for guess in ("event", "events", "evenement", "evenementen", "agenda", "programma", "voorstelling", "voorstellingen", "concert", "concerten", "show", "shows"):
                try:
                    r = _net.SESSION.get(urljoin(base, f"/wp-json/wp/v2/{guess}?per_page=1"), timeout=TIMEOUT)
                    if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                        rest_base = guess
                        break
                except (requests.RequestException, ValueError):
                    continue
        if not rest_base:
            return []
        api = urljoin(base, f"/wp-json/wp/v2/{rest_base}?per_page=100&_embed=1")
    out, page = [], 1
    while page <= int(v.get("max_pages", 3)):
        sep = "&" if "?" in api else "?"
        r = get(f"{api}{sep}page={page}", delay=0.5)
        items = r.json()
        if not isinstance(items, list) or not items:
            break
        for it in items:
            title = _find_title(it)
            link = it.get("link")
            if not (title and link):
                continue
            acf = it.get("acf") or it.get("meta") or {}
            dt = _acf_date(acf) or date_from_url(link)
            genres = []
            want_tax = v.get("genre_taxonomies")  # bv. [production_genre]; anders alles behalve categorie/tag/taal
            for grp in (it.get("_embedded", {}) or {}).get("wp:term", []):
                for term in grp:
                    tax = term.get("taxonomy")
                    ok = (tax in want_tax) if want_tax else (tax not in ("category", "post_tag", "language"))
                    if ok and term.get("name"):
                        genres.append(clean(term["name"]))
            stg = _stager_acf(acf)
            price = stg.get("price") or (acf.get("price") if isinstance(acf, dict) else None)
            sub = stg.get("subtitle") or (acf.get("one_liner") or acf.get("subtitle") or acf.get("support_act") if isinstance(acf, dict) else None)
            if not dt and v.get("detail_jsonld", True):
                # datum staat alleen op de eventpagina -> JSON-LD of HTML daar lezen
                ev = fetch_detail(v, link, detail_cache, title=title)
                if ev:
                    ev.genres = ev.genres or genres
                    ev.source = "wp_event+detail"
                    out.append(ev)
                continue
            if not dt:
                continue
            if isinstance(price, str) and (price.startswith("€") or price == "gratis"):
                price_s = price
            else:
                price_s = (f"€ {price}" if price not in (None, "", 0) else None) if isinstance(price, (str, int, float)) else None
            out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=link,
                             subtitle=clean(sub) if isinstance(sub, str) else None, genres=genres,
                             price=normalize_price(price_s) if price_s else None, status=stg.get("status"), source="wp_event"))
        if len(items) < 100 and "per_page=100" in api:
            break
        if int(r.headers.get("X-WP-TotalPages", 1)) <= page:
            break
        page += 1
    return out


def _path(obj, path: str):
    """Waarde uit geneste dicts/lijsten via een puntpad: 'label.title', 'dates.0.start'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _fill(template: str, item: dict):
    """'https://site/programma/{seo_slug}/' -> ingevuld; een pad zonder accolades is een directe veldnaam."""
    if "{" not in template:
        return _path(item, template)
    missing = False

    def rep(m):
        nonlocal missing
        val = _path(item, m.group(1))
        if val in (None, ""):
            missing = True
        return str(val) if val is not None else ""
    out = re.sub(r"\{([^}]+)\}", rep, template)
    return None if missing else out


def strat_json_api(v: dict, base: str, detail_cache: dict) -> list[Event]:
    """Eigen JSON-eindpunt van een podium (venues.yaml: type: json_api). Veel sites laden hun agenda via een
    AJAX-call die een JSON-lijst teruggeeft (Boerderij: includes/ajax/events.php). De veldnamen verschillen per site,
    dus `fields` beschrijft de afbeelding; de waarden zijn puntpaden of templates met {veld}:

      type: json_api
      api: https://…/includes/ajax/events.php?limit=69420
      items: data.events            # optioneel: pad naar de lijst (standaard: het antwoord zelf, of het eerste lijstveld)
      fields:
        title: title                # verplicht
        date: event_date            # verplicht: ISO-datum of datum+tijd (parse_dt)
        url: "https://…/programma/{seo_slug}/"   # verplicht
        subtitle: subtitle
        price: ticket_price
        time: start_time            # optioneel: "20:30" los van de datum
        genres: genre               # string of lijst
        status: label.title         # 'Uitverkocht' / 'Afgelast' e.d.
      page_param: offset            # optioneel: paginering via ?offset=N (met page_size) of ?page=N
    Ontbreken tijd/prijs, dan haalt de generieke verrijking ze van de eventpagina (JSON-LD of tekst)."""
    api = v["api"]
    f = v.get("fields") or {}
    out: list[Event] = []
    page, offset = 1, 0
    while page <= int(v.get("max_pages", 5)):
        url = api
        if v.get("page_param") and page > 1:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{v['page_param']}={offset if v['page_param'] == 'offset' else page}"
        r = get(url, delay=float(v.get("crawl_delay", 0.5)))
        try:
            data = r.json()
        except ValueError:
            data = json.loads(r.text.strip())
        items = _path(data, v["items"]) if v.get("items") else data
        if isinstance(items, dict):
            # {"events": [...]} of een dict van dicts met de slug als sleutel (Vorstin ?agenda_json)
            lst = next((x for x in items.values() if isinstance(x, list)), None)
            items = lst if lst is not None else [x for x in items.values() if isinstance(x, dict)]
        if not isinstance(items, list) or not items:
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            title = clean(str(_fill(f.get("title", "title"), it) or ""))
            link = _fill(f.get("url", "url"), it)
            raw_date = _fill(f.get("date", "date"), it)
            if not (title and link and raw_date):
                continue
            raw_date = str(raw_date)
            if re.fullmatch(r"\d{12}", raw_date):   # 202609111915 (Vorstin program_start)
                dt = datetime.strptime(raw_date, "%Y%m%d%H%M")
            elif re.fullmatch(r"\d{8}", raw_date):
                dt = datetime.strptime(raw_date, "%Y%m%d")
            else:
                dt = parse_dt(raw_date)
            if not dt:
                continue
            if v.get("time_utc") and dt.tzinfo is None:
                dt = to_local(dt.replace(tzinfo=timezone.utc))   # Ziggo Dome: "2026-09-05 13:00:00" is UTC (site toont 15:00)
            if f.get("time") and (dt.hour, dt.minute) == (0, 0):
                tm = _fill(f["time"], it)
                m = re.match(r"(\d{1,2})[:.u](\d{2})", _ampm_to_24h(str(tm or "")))
                if m:
                    dt = dt.replace(hour=int(m.group(1)), minute=int(m.group(2)))
            link = urljoin(base, str(link))
            if v.get("filter") and any(str(_path(it, k) or "").strip().lower() != str(want).strip().lower() for k, want in v["filter"].items()):
                continue   # alleen items met deze veldwaarden (pop-agenda: acf.venue == "Het Podium")
            genres = _fill(f["genres"], it) if f.get("genres") else None
            if isinstance(genres, str) and "," in genres and not genres.strip().startswith("["):
                genres = [g.strip() for g in genres.split(",")]   # "Jazz, House, Pop"
            if isinstance(genres, str) and genres.strip().startswith("["):
                try:
                    genres = json.loads(genres)   # Ziggo Dome: '[{"name": "Pop"}, …]' als string
                except ValueError:
                    pass
            genres = [g.get("name") or g.get("title") if isinstance(g, dict) else g for g in as_list(genres)] if genres else []
            genres = [clean(str(g)) for g in genres if g]
            raw_status = clean(str(_fill(f["status"], it) or "")) if f.get("status") else None
            status = None
            price = fmt_price(_fill(f["price"], it)) if f.get("price") else None
            if raw_status:
                low = raw_status.lower()
                if re.search(r"uitverkocht|sold[ _-]?out", low):
                    status = "uitverkocht"
                elif re.search(r"afgelast|cancel", low):
                    status = "afgelast"
                elif re.search(r"verplaatst|postponed|moved", low):
                    status = "verplaatst"
                elif re.search(r"gratis|free", low) and not price:
                    price = "gratis"        # Mezz: event.status = "Gratis"
            if price and not re.search(r"\d|gratis|free", price, re.I):
                price = None                # statusachtige tekst ("Tickets via TIDT") is geen prijs
            sub = _fill(f["subtitle"], it) if f.get("subtitle") else None
            out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=link,
                             subtitle=clean(str(sub)) if sub else None, genres=genres,
                             price=normalize_price(price) if price else None, status=status, source="json_api"))
        if not v.get("page_param") or len(items) < int(v.get("page_size", len(items) or 1)):
            break
        offset += len(items)
        page += 1
    return out


def strat_stager(v: dict, base: str, cache: dict) -> list[Event]:
    """Stager-ticketshop (<podium>.stager.co) via de shop-API die de webshop zelf gebruikt. Veel poppodia verkopen via
    Stager (Vera, Simplon, Boerderij, So What!, Bibelot, Willemeen, Hall of Fame, Muziekgieterij…). De shoppagina heeft
    JSON-LD met 50 events (zonder prijs); de API geeft álle komende events én per event de ticketprijzen:
      1. GET  /shop/default/events            -> data-flags {"shopId": 301}
      2. POST /shop/v1/session/new?shopId=301&locale=NL&hasOrderToken=false  (body {})  -> accessToken.jwt (anonieme sessie)
      3. GET  /shop/v1/events?offset=0&limit=20  (Bearer)  -> eventId, name, startsOn (UTC), soldOut; doorgaan tot leeg
      4. GET  /shop/v1/events/{id}/tickets-overview -> ticketGroups[{name, priceInCents}] -> reguliere online prijs
    Lukt de sessie niet, dan valt de aanroeper terug op de JSON-LD van de shoppagina (type: jsonld)."""
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    # shopnaam uit de URL: meestal /shop/default/…, maar Luxor Live verkoopt via /shop/luxor-live/… (default is daar leeg)
    ms = re.search(r"/shop/([a-z0-9_-]+)", urlparse(base).path, re.I)
    shop = ms.group(1) if ms else "default"
    html = get(f"{root}/shop/{shop}/events", delay=0.3).text
    m = re.search(r'data-flags="([^"]+)"', html)
    if not m:
        raise ValueError("Stager: geen data-flags/shopId op de shoppagina")
    import html as _html
    flags = json.loads(_html.unescape(m.group(1)))
    shop_id = flags.get("shopId")
    r = http_post(f"{root}/shop/v1/session/new", params={"shopId": shop_id, "locale": "NL", "hasOrderToken": "false"}, json={})
    r.raise_for_status()
    token = r.json()["accessToken"]["jwt"]
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    items = []
    offset = 0
    while offset < 2000:
        rr = _net.SESSION.get(f"{root}/shop/v1/events", params={"offset": offset, "limit": 20}, headers=hdr, timeout=TIMEOUT)
        rr.raise_for_status()
        chunk = rr.json()
        if not isinstance(chunk, list) or not chunk:
            break
        items += chunk
        offset += len(chunk)
        time.sleep(0.3)
    out = []
    for it in items:
        start = _stager_local(it.get("startsOn"))
        if not start or not it.get("eventId"):
            continue
        url = f"{root}/shop/{shop}/events/{it['eventId']}"
        # prijs per event, één keer per dag gecached
        ck = "stager|" + url
        c = cache.get(ck)
        if c and c.get("fetched") == TODAY.isoformat():
            price = c.get("price")
        else:
            price = None
            try:
                tv = _net.SESSION.get(f"{root}/shop/v1/events/{it['eventId']}/tickets-overview", headers=hdr, timeout=TIMEOUT)
                time.sleep(0.25)
                if tv.ok:
                    price = _stager_price(tv.json().get("ticketGroups") or [])
            except (requests.RequestException, ValueError):
                pass
            cache[ck] = {"fetched": TODAY.isoformat(), "price": price}
        out.append(Event(venue=v["name"], city=v["city"], title=clean(it.get("name")) or "?", start=start.isoformat(timespec="minutes"),
                         url=url, price=price, status="uitverkocht" if it.get("soldOut") else None, source="stager"))
    return out


_STAGER_SESSIONS: dict[str, tuple[str, dict]] = {}   # shop-root -> (token, headers), één anonieme sessie per shop per run
_STAGER_LINK = re.compile(r"https?://([a-z0-9-]+)\.stager\.co/shop/([a-z0-9_-]+)(?:/events/(\d+))?", re.I)


def _stager_session(root: str, shop: str) -> dict | None:
    key = f"{root}/shop/{shop}"
    if key in _STAGER_SESSIONS:
        return _STAGER_SESSIONS[key][1]
    try:
        html = get(f"{key}/events", delay=0.3).text
        m = re.search(r'data-flags="([^"]+)"', html)
        if not m:
            return None
        import html as _html
        shop_id = json.loads(_html.unescape(m.group(1))).get("shopId")
        r = http_post(f"{root}/shop/v1/session/new", params={"shopId": shop_id, "locale": "NL", "hasOrderToken": "false"}, json={})
        r.raise_for_status()
        hdr = {"Authorization": f"Bearer {r.json()['accessToken']['jwt']}", "Accept": "application/json"}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    _STAGER_SESSIONS[key] = (shop_id, hdr)
    return hdr


def stager_price_from_link(html: str, start: str | None, title: str | None) -> str | None:
    """Prijs via de Stager-ticketlink op een eventpagina (WORM: elk event verkoopt via de shop van de organisator,
    strictlykpop.stager.co/shop/strictlykpopworm; dB's: dbs.stager.co/shop/default/events/111671858). Met event-id direct
    de tickets-overview; zonder id de shoplijst en het event met dezelfde dag (en liefst titel) kiezen."""
    m = _STAGER_LINK.search(html or "")
    if not m or m.group(1).lower() == "app":
        return None
    slug, shop, ev_id = m.group(1).lower(), m.group(2), m.group(3)
    root = f"https://{slug}.stager.co"
    hdr = _stager_session(root, shop)
    if not hdr:
        return None
    try:
        if not ev_id:
            rr = _net.SESSION.get(f"{root}/shop/v1/events", params={"offset": 0, "limit": 50}, headers=hdr, timeout=TIMEOUT)
            rr.raise_for_status()
            items = [it for it in (rr.json() or []) if isinstance(it, dict) and it.get("eventId")]
            day = (start or "")[:10]
            same_day = [it for it in items if (_stager_local(it.get("startsOn")) or datetime.min).date().isoformat() == day] if day else []
            pick = None
            if title:
                tk = _title_key(title)
                pick = next((it for it in same_day if tk and (tk in _title_key(it.get("name") or "") or _title_key(it.get("name") or "") in tk)), None)
            pick = pick or (same_day[0] if len(same_day) == 1 else None) or (items[0] if len(items) == 1 else None)
            if not pick:
                return None
            ev_id = str(pick["eventId"])
        tv = _net.SESSION.get(f"{root}/shop/v1/events/{ev_id}/tickets-overview", headers=hdr, timeout=TIMEOUT)
        time.sleep(0.25)
        if not tv.ok:
            return None
        return _stager_price(tv.json().get("ticketGroups") or [])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def _stager_local(value) -> datetime | None:
    """Stager-API 'startsOn' eindigt op Z maar is Nederlandse tijd (Simplon: site 21:45, shop "21:45:00Z"; met omrekening
    stonden alle shop-events 2 uur te laat en werden ze niet meer als dubbel herkend). Dus: als lokale tijd lezen."""
    if not isinstance(value, str):
        return parse_dt(value)
    return parse_dt(re.sub(r"(Z|[+-]00:?00)$", "", value.strip()))


def _fee_cents(g: dict) -> int:
    """Servicekosten (in centen) uit een Stager-ticketgroep: velden als feeInCents / serviceFeeInCents / fee.priceInCents."""
    total = 0
    for k, v in g.items():
        if not re.search(r"fee", k, re.I) or re.search(r"free|refund", k, re.I):
            continue
        if isinstance(v, (int, float)) and re.search(r"cents", k, re.I):
            total += int(v)
        elif isinstance(v, dict) and isinstance(v.get("priceInCents"), (int, float)):
            total += int(v["priceInCents"])
    return total


def _stager_price(groups: list[dict]) -> str | None:
    """Reguliere online prijs (inclusief servicekosten: wat je betaalt) uit Stager-ticketgroepen: kortingsgroepen (leden,
    studenten, early bird, deur) vallen af als er andere zijn; daarna 'voorverkoop/regulier' vóór de rest; alles 0 = gratis."""
    cands = []
    for g in groups:
        if not isinstance(g, dict) or g.get("priceInCents") is None:
            continue
        cents = int(g["priceInCents"]) + (_fee_cents(g) if int(g["priceInCents"]) > 0 else 0)
        cands.append((int(g.get("weight") or 0), g.get("name") or "", cents))
    if not cands:
        return None
    cands.sort()
    paid = [c for c in cands if c[2] > 0]
    if not paid:
        return "gratis"
    regular = [c for c in paid if not _DISCOUNT_CTX.search(c[1])] or paid
    online = [c for c in regular if re.search(r"voorverkoop|vvk|presale|pre-?sale|online", c[1], re.I)]
    pref = online or [c for c in regular if _PREFERRED_CTX.search(c[1])] or regular
    return fmt_price(pref[0][2] / 100)


# --- eventlinks volgen + JSON-LD op detailpagina --------------------------------

NON_EVENT_PATH = re.compile(r"/page/?\d+/?$|/page/\d+|/en/|/english|/tag/|/tags/|/genre/|/genres/|/categor|/zoek|/search|/filter|/feed|/wp-|/nieuws|/news|/blog|/over|/about|/contact|/vacature|/verhuur|/faq|/privacy|/cookie|/algemene|/login|/account|/cart|/winkel|/shop|/merch|/pers|/partners|/steun|/vrienden|/locatie|/route|/tickets?$|/programma/?$|/agenda/?$|/events?/?$|/evenementen/?$|/agenda/(concerten|exposities?|expo|film|films|kids|jeugd|kidsjeugd|theater|cabaret|comedy|dans|workshops?|cursussen|festivals?|clubs?|party|feesten|overig|alles|all)/?$|\.(pdf|jpe?g|png|ics)$", re.I)


def event_links(v: dict, html: str, base: str) -> list[str]:
    """Kandidaat-eventlinks op een overzichtspagina.
    Met `link_pattern` is het simpel; zonder: links onder hetzelfde pad als de agendapagina (bijv. /programma/<slug>/),
    of met een event-achtig padsegment, minus paginering/filters/andere secties."""
    s = soup_of(html)
    pat = re.compile(v["link_pattern"]) if v.get("link_pattern") else None
    host = urlparse(base).netloc.replace("www.", "")
    base_path = urlparse(base).path.rstrip("/")
    seen, out = set(), []
    for a in s.find_all("a", href=True):
        raw = a["href"].split("#")[0]
        if not re.search(r"[?&](p|post_type|event_id|id)=", raw):
            raw = raw.split("?")[0]
        href = urljoin(base, raw)
        if urlparse(href).netloc.replace("www.", "") != host:
            continue
        path = urlparse(href).path + ("?" + urlparse(href).query if "post_type=" in href else "")
        if pat:
            if not pat.search(href):
                continue
        else:
            if path.rstrip("/") in ("", base_path):
                continue
            under_base = bool(base_path) and path.startswith(base_path + "/")
            hinted = any(h in path for h in EVENT_LINK_HINTS)
            if not (under_base or hinted):
                continue
            if NON_EVENT_PATH.search(path):
                continue
            # een eventpagina heeft minstens één 'slug'-segment onder de sectie
            depth = path.rstrip("/").count("/")
            if depth < 2:
                continue
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


FLIGHT_VERSION = 2  # idem, maar alleen voor pagina's die via event_from_flight_json (Paradiso) zijn gelezen
def _cache_version(v: dict) -> int:
    """Per podium hoger te zetten (venues.yaml `cache_version: 9`) om alleen díe eventpagina's opnieuw te lezen na een
    parserfix, zonder alle ~13.000 gecachte pagina's opnieuw op te halen."""
    return max(CACHE_VERSION, int(v.get("cache_version") or 0))


CACHE_VERSION = 10  # 10: vaste tijdzone-offset als kloktijd (Nieuwe Nor), locker/munt-bedragen en nieuwsbrief-'gratis' geen prijs. Verhogen als fetch_detail/extract_from_text meer of betere velden oplevert: oude cache-items worden dan opnieuw opgehaald


_PUBLISH_CLASS = re.compile(r"publish|updated|entry-date|post-date|author-date|posted|meta-date|byline", re.I)


def _event_time_tag(s: BeautifulSoup):
    """De <time datetime> van het event, niet die van het blogbericht. WordPress-thema's zetten de publicatiedatum
    in <time class="entry-date updated"> (Gelderlandfabriek: 18 augustus, terwijl het event 19 september is); die
    valt buiten het venster en het event verdween. Voorkeur: een <time> zonder publicatie-klasse met een datum in het
    venster; anders de eerste zonder publicatie-klasse; anders niets (dan telt de datum uit de tekst)."""
    tags = s.find_all("time", attrs={"datetime": True})
    cands = []
    for t in tags:
        cls = " ".join(t.get("class", [])) + " " + " ".join(t.parent.get("class", []) if t.parent else [])
        if _PUBLISH_CLASS.search(cls):
            continue
        cands.append(t)
    for t in cands:
        if in_window(parse_dt(t["datetime"])):
            return t
    return cands[0] if cands else None


def fetch_detail(v: dict, url: str, cache: dict, title: str | None = None) -> Event | None:
    """Leest een eventpagina; cache op URL zodat dit maar zelden opnieuw hoeft."""
    c = cache.get(url)
    if c and c.get("fetched") and c.get("event") and c["event"].get("start", "9999") >= TODAY.isoformat():
        stale = c.get("v", 1) < _cache_version(v) or (c["event"].get("source") == "flight_json" and c.get("fv", 1) < FLIGHT_VERSION)
        if stale:
            c = None  # verouderd cache-item van een oudere parser (bv. zonder prijs/line-up, of met gelekte startMain): opnieuw ophalen
    if c and c.get("fetched") and not c.get("event") and c.get("v", 1) < _cache_version(v):
        c = None  # mislukking van een oudere parser: na een fix direct opnieuw proberen (Gelderlandfabriek, Q-factory)
    if c and c.get("fetched"):
        # geslaagde resultaten 10 dagen bewaren; mislukkingen maar 1 dag, zodat een fix snel doorwerkt
        ttl = int(v.get("detail_ttl_days", 10)) if c.get("event") else 1
        if c.get("event") and c["event"].get("start", "9999") < TODAY.isoformat():
            ttl = 3650  # voorbij: nooit meer ophalen
        if date.fromisoformat(c["fetched"]) > TODAY - timedelta(days=ttl):
            return Event(**c["event"]) if c.get("event") else None
    try:
        html = get(url, delay=float(v.get("crawl_delay", 0.6))).text
    except requests.RequestException as ex:
        cache[url] = {"fetched": TODAY.isoformat(), "event": None, "error": str(ex)[:200]}
        return None
    ev = None
    for n in jsonld_events(html):
        ev = event_from_jsonld(n, v, url, "jsonld_detail")
        if ev:
            break
    if ev is None:
        ev = event_from_flight_json(html, url, v)
    txt = page_text(html)
    tdt, tstart, tdoors, tprice, tkind, tpkind = extract_from_text_ex(txt)
    if not tprice:
        tprice = price_from_embedded_json(html)
        tpkind = "embedded" if tprice else None
    if not tstart and not tdoors:
        tstart, tdoors = times_from_embedded_json(html)
        tkind = "embedded" if tstart else None
    if ev is None:
        s = soup_of(html)
        t = _event_time_tag(s)
        h1 = s.find("h1")
        if title is None and h1:
            title = clean(h1.get_text())
        if title is None:
            og = s.find("meta", property="og:title")
            title = re.split(r"\s[|–-]\s", clean(og["content"]))[0] if og and og.get("content") else None
        # datum uit losse tekst is onbetrouwbaar als die precies vandaag is zonder tijd: veel sites tonen de
        # datum van vandaag in een header/agenda-widget (ECI: 74 "voorstellingen" op één dag)
        if tdt and tdt.date() == TODAY and not (tstart or tdoors):
            tdt = None
        sel_dt = None
        if v.get("detail_date"):
            # vaste plek van de datum op de eventpagina (P60: eerste .datum- element; de rest zijn 'binnenkort'-tips)
            el = s.select_one(v["detail_date"])
            if el is not None:
                sel_dt = parse_dt(clean(el.get_text())) or extract_from_text(el.get_text(" "))[0]
        dt = sel_dt or (parse_dt(t["datetime"]) if t else None) or date_from_url(url) or tdt
        if dt and (dt.hour, dt.minute) == (0, 0) and (tstart or tdoors):
            dt = dt.replace(hour=(tstart or tdoors)[0], minute=(tstart or tdoors)[1])
        if dt and title:
            ev = Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=url,
                       price=tprice, price_src=tpkind, source="detail_text")
    if ev is not None:
        if ev.price and not ev.price_src:
            ev.price_src = "jsonld" if ev.source in ("jsonld_detail", "x") else "list"
        if v.get("lineup") and not ev.lineup:
            ev.lineup = clean_lineup([x.get_text() for x in soup_of(html).select(v["lineup"])], ev.title)
        if v.get("subtitle") and not ev.subtitle:   # ondertitel/tagline op de eventpagina (Tivoli p.event__subtitle)
            el = soup_of(html).select_one(v["subtitle"])
            ev.subtitle = clean(el.get_text(" ")) if el is not None else None
        if not ev.location:
            ev.location = location_from_text(txt, v)
        if not ev.genres:
            hints = genre_hints(txt[:1500], limit=4)
            ev.genres = hints if len(hints) <= 2 else []  # >2 treffers = waarschijnlijk een genremenu op de pagina (Tivoli), geen beschrijving
        # aanvang gaat vóór deuren-open: als de gevonden tijd gelijk is aan de deurtijd en er staat een aanvang, neem die
        st = datetime.fromisoformat(ev.start)
        if tstart and (((st.hour, st.minute) == (0, 0)) or (tdoors and (st.hour, st.minute) == tdoors and tstart != tdoors)):
            ev.start = st.replace(hour=tstart[0], minute=tstart[1]).isoformat(timespec="minutes")
            ev.time_src = tkind
        if not ev.price and tprice:
            ev.price, ev.price_src = tprice, tpkind
        if not ev.price and v.get("ticketshops", True):
            ev.price = stager_price_from_link(html, ev.start, ev.title)   # prijs via de Stager-link op de pagina (WORM, dB's)
            ev.price_src = "shop" if ev.price else None
        elif tprice and ev.price and ev.price_src != "shop" and _fee_inclusive(txt):
            # LantarenVenster: JSON-LD offers 19 (excl.), pagina "€ 22 incl. € 3,00 servicekosten": de prijs is wat je betaalt
            a, b = price_number(ev.price), price_number(tprice)
            if a and b and 0 < b - a <= 6:
                ev.price, ev.price_src = tprice, "labeled"
    cache[url] = {"fetched": TODAY.isoformat(), "v": _cache_version(v), "fv": FLIGHT_VERSION, "event": asdict(ev) if ev else None}
    return ev


def event_from_flight_json(html: str, url: str, v: dict) -> Event | None:
    """Eventgegevens uit een React Server Components-payload (o.a. Paradiso: Craft CMS via Next.js)."""
    ev_id = url.rstrip("/").rsplit("/", 1)[-1]
    clean_html = html.replace('\\"', '"')
    # lange line-ups (Paradiso: "artists" per zaal) kunnen duizenden tekens tussen id en startDateTime zetten
    m = re.search(r'"__typename":\s*"event_\w+_Entry",\s*"id":\s*"%s".{0,40000}?"startDateTime":\s*"([^"]+)"' % re.escape(ev_id), clean_html, re.S)
    if not m:
        return None
    # blok = dit event t/m het volgende event-object; anders lekken velden (startMain, prijs) van "gerelateerde events"
    tail = clean_html[m.end(): m.end() + 4000]
    nxt = re.search(r'"__typename":\s*"event_\w+_Entry"|"(?:relatedEvents|related|highlightedItems|upcoming|events)":\s*\[', tail)
    block = clean_html[m.start(): m.end() + (nxt.start() if nxt else 2500)]
    head = clean_html[m.start(): m.end()]  # id t/m startDateTime: hier staan titel, ondertitel en line-up
    # tijdvelden staan direct na startDateTime (date, doorsOpen, doorsClose, startMain); "startMain":null betekent: geen aparte aanvang
    near = tail[:600]
    sm = re.search(r'"startMain":\s*(?:"([^"]*)"|null)', near)
    start_main = sm.group(1) if sm and sm.group(1) else None

    def _unesc(v: str) -> str:  # JSON-escapes in de RSC-payload: \u0026 -> &, \/ -> /
        return re.sub(r"\\u([0-9a-fA-F]{4})", lambda mm: chr(int(mm.group(1), 16)), v).replace("\\/", "/")

    def fld(name):
        mm = re.search(r'"%s":\s*"([^"]*)"' % name, block)
        return clean(_unesc(mm.group(1))) if mm else None

    # startDateTime draagt de juiste DATUM (UTC); startMain/doorsOpen hebben een bruikbare TIJD maar een onzinnige datum
    # (Paradiso vult daar de dag van vandaag in). Dus: datum uit startDateTime, aanvangstijd uit startMain als die er is.
    start = parse_dt(m.group(1))
    if start and start_main:
        sm = parse_dt(start_main)
        if sm:
            start = start.replace(hour=sm.hour, minute=sm.minute)
    if not start:
        return None
    title = fld("title") or "?"
    lineup = []
    for seg in re.findall(r'"artists":\s*\[(.*?)\]\s*\}', head, re.S):
        lineup += [clean(_unesc(t)) for t in re.findall(r'"title":\s*"([^"]+)"', seg)]
    lineup = [x for x in dict.fromkeys(lineup) if x and not re.search(r"\btba\b|more tba|special guest", x, re.I)][:20]
    genres = []
    if '"subBrand"' in block:
        # subBrand = programmareeks (Club Paradiso, Indiestad, Tones, Sugar Mountain…): alleen houden als het een genre is
        cands = [_unesc(g) for g in re.findall(r'"title":\s*"([^"]+)"', block.split('"subBrand"', 1)[1].split("]", 1)[0]) if g][:3]
        genres = [g for g in cands if normalize_tag(g) not in (None, "overig")]
    loc = re.search(r'"location":\s*\{[^{}]*?"title":\s*"([^"]+)"', block)
    status = None
    if re.search(r'"(cancelled|isCancelled)":true', block) or "afgelast" in block.lower():
        status = "afgelast"
    elif re.search(r'"(soldOut|isSoldOut)":true', block) or "uitverkocht" in block.lower():
        status = "uitverkocht"
    sub = fld("subtitle")
    if loc and loc.group(1).lower() not in (v["name"].lower(),):
        sub = (sub + " · " if sub else "") + loc.group(1)
    price = fld("ticketPriceFormatted") or fld("price") or fld("priceFrom")
    if price:
        price = price.replace("€", "").strip()
    if fld("soldOut") == "yes":
        status = status or "uitverkocht"
    return Event(venue=v["name"], city=v["city"], title=title, start=start.isoformat(timespec="minutes"), url=url,
                 subtitle=sub, genres=genres, price=(f"€ {price}" if price else None), status=status, source="flight_json", lineup=lineup,
                 location=_unesc(loc.group(1)) if loc else None)


def strat_sitemap_detail(v: dict, base: str, cache: dict) -> list[Event]:
    """Eventlinks uit de sitemap (laatste N bestanden = nieuwste events), daarna per eventpagina lezen (gecached)."""
    sitemap = v.get("sitemap") or urljoin(base, "/sitemap.xml")
    idx = get(sitemap, delay=0.5).text
    if "<sitemapindex" in idx or ("<sitemap>" in idx and "<url>" not in idx):
        files = [m.group(1) for m in re.finditer(r"<loc>(.*?)</loc>", idx)]
        pat = re.compile(v.get("sitemap_pattern", r"event"))
        files = [f for f in files if pat.search(f)]
    else:
        files = [sitemap]   # gewone urlset (Q-factory): de sitemap zelf bevat de eventlinks

    def num(f):
        mm = re.search(r"(\d+)\.xml$", f)
        return int(mm.group(1)) if mm else 0

    files.sort(key=num)
    files = files[-int(v.get("last_files", 6)):] or [sitemap]
    # per URL de lastmod meenemen: aankomende events worden bijgewerkt (ticketstatus), oude niet meer.
    recent_days = int(v.get("sitemap_recent_days", 0))
    cutoff = (TODAY - timedelta(days=recent_days)).isoformat() if recent_days else None
    urls: list[str] = []
    for f in files:
        try:
            xml = get(f, delay=0.5).text
        except requests.RequestException as ex:
            log(f"    sitemap mislukt {f}: {ex}")
            continue
        for m in re.finditer(r"<url>(.*?)</url>", xml, re.S):
            loc = re.search(r"<loc>(.*?)</loc>", m.group(1))
            mod = re.search(r"<lastmod>(.*?)</lastmod>", m.group(1))
            if not loc:
                continue
            if cutoff and mod and mod.group(1)[:10] < cutoff:
                continue
            urls.append(loc.group(1))
        if not re.search(r"<url>", xml):  # sitemap zonder <url>-blokken: alleen <loc>
            urls += [m.group(1) for m in re.finditer(r"<loc>(.*?)</loc>", xml)]
    lp = re.compile(v["link_pattern"]) if v.get("link_pattern") else None
    if v.get("url_replace"):   # Q-factory: sitemap noemt alleen /en/events/…, de Nederlandse pagina staat op /nl/events/…
        a, b = v["url_replace"]
        urls = [u.replace(a, b) for u in urls]
    urls = [u for u in dict.fromkeys(urls) if not lp or lp.search(u)]
    urls = urls[-int(v.get("max_detail", 600)):]
    if not urls and files:
        try:
            r0 = http_get(files[-1])
            log(f"    sitemap {files[-1]}: status {getattr(r0, 'status_code', '?')}, url {getattr(r0, 'url', '?')}, begin: {(r0.text or '')[:160]!r}")
        except Exception as ex:  # noqa: BLE001
            log(f"    sitemap {files[-1]}: {type(ex).__name__} {str(ex)[:120]}")
    log(f"    {len(urls)} eventlinks uit sitemap, detailpagina's ophalen (gecached)…")
    out = []
    for u in urls:
        ev = fetch_detail(v, u, cache)
        if ev:
            out.append(ev)
    return out


_GRAPHQL_CREDS: dict[str, tuple[str, str]] = {}


def _graphql_endpoint(v: dict) -> tuple[str, str]:
    """Endpoint + publieke client-token uit de JavaScript-bundels van de site (Paradiso: Next.js-chunks bevatten
    "https://….execute-api….amazonaws.com" en "Bearer ".concat("…")). Elke run opnieuw gelezen, nooit opgeslagen."""
    g = v["graphql"]
    page = g.get("endpoint_from") or v["url"]
    if page in _GRAPHQL_CREDS:
        return _GRAPHQL_CREDS[page]
    html = get(page, delay=0.3).text
    scripts = [urljoin(page, m) for m in re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html)]
    scripts += [urljoin(page, m) for m in re.findall(r'"(/_next/static/chunks/[^"]+\.js)"', html)]
    ep_re, tok_re = re.compile(g["endpoint_pattern"]), re.compile(g["token_pattern"])
    endpoint = token = None
    for src in dict.fromkeys(scripts):
        try:
            js = get(src, delay=0.2).text
        except requests.RequestException:
            continue
        m1, m2 = ep_re.search(js), tok_re.search(js)
        if m1 and m2:
            endpoint, token = m1.group(0), m2.group(1)
            break
        if len(_GRAPHQL_CREDS) > 60:
            break
    if not endpoint:
        raise ValueError("graphql: endpoint/token niet gevonden in de scripts van " + page)
    _GRAPHQL_CREDS[page] = (endpoint.rstrip("/") + g.get("endpoint_path", ""), token)
    return _GRAPHQL_CREDS[page]


def strat_graphql_detail(v: dict, base: str, cache: dict) -> list[Event]:
    """Eventlijst uit de GraphQL-API die de site zelf gebruikt (Paradiso: programItemsQuery, 100 per pagina, cursor via
    `searchAfter` = sort van het laatste item), daarna per event de eventpagina (gecached) voor prijs/aanvang/genres.
      type: graphql_detail
      graphql:
        endpoint_from: https://www.paradiso.nl/        # pagina waarvan de JS-bundels endpoint en token bevatten
        endpoint_pattern: 'https://[a-z0-9.-]+\\.execute-api\\.[a-z0-9.-]+\\.amazonaws\\.com'
        endpoint_path: /graphql
        token_pattern: '"Bearer "\\.concat\\("([^"]+)"\\)'
        query: "query … { program(size: $size, searchAfter: $searchAfter) { events { id uri title startDateTime … sort location { title } } } }"
        variables: {size: 100}
        items: data.program.events
        cursor: sort              # veld van het laatste item -> variables[cursor_var]
        cursor_var: searchAfter
        fields: {url: uri, title: title, start: startDateTime, location: location.0.title, soldout: soldOut, status: eventStatus, subtitle: subtitle}
    Items zonder eventpagina-resultaat worden uit de lijst zelf opgebouwd."""
    g = v["graphql"]
    endpoint, token = _graphql_endpoint(v)
    f = g.get("fields") or {}
    variables = dict(g.get("variables") or {})
    items: list[dict] = []
    for _ in range(int(g.get("max_pages", 30))):
        r = http_post(endpoint, json={"query": g["query"], "variables": variables},
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
        r.raise_for_status()
        page = _path(r.json(), g.get("items", "data")) or []
        if not isinstance(page, list) or not page:
            break
        items += page
        cur = _path(page[-1], g["cursor"]) if g.get("cursor") else None
        if cur is None:
            break
        variables[g.get("cursor_var", "searchAfter")] = cur
        time.sleep(float(v.get("crawl_delay", 0.3)))
    log(f"    {len(items)} events via graphql, eventpagina's ophalen (gecached)…")
    out: list[Event] = []
    for it in items:
        uri = _path(it, f.get("url", "uri")) or ""
        url = urljoin(base, str(uri)) if uri else None
        ev = fetch_detail(v, url, cache, title=clean(str(_path(it, f.get("title", "title")) or ""))) if url else None
        start = parse_dt(_path(it, f.get("start", "startDateTime")))
        if ev is None:
            title = clean(str(_path(it, f.get("title", "title")) or ""))
            if not (title and start and url):
                continue
            ev = Event(venue=v["name"], city=v["city"], title=title, start=start.isoformat(timespec="minutes"), url=url,
                       subtitle=clean(str(_path(it, f.get("subtitle", "subtitle")) or "")) or None, source="graphql")
        loc = _path(it, f["location"]) if f.get("location") else None
        if isinstance(loc, str) and loc and _fold(loc) != _fold(v["name"]) and not ev.location:
            ev.location = clean(loc)
        st = str(_path(it, f.get("status", "eventStatus")) or "").lower()
        so = _path(it, f.get("soldout", "soldOut"))
        if re.search(r"cancel|afgelast", st):
            ev.status = "afgelast"
        elif re.search(r"postpone|verplaatst|moved", st):
            ev.status = ev.status or "verplaatst"
        elif so is True or (isinstance(so, str) and so.lower().startswith("yes")):
            ev.status = ev.status or "uitverkocht"
        out.append(ev)
    return out


def detail_extra(v: dict, url: str, cache: dict) -> dict | None:
    """Gestructureerde + tekstuele gegevens van een eventpagina (gecached onder "x|url"); None als niet opgehaald."""
    key = "x|" + url
    c = cache.get(key)
    if c and c.get("v", 1) >= _cache_version(v) and date.fromisoformat(c["fetched"]) >= TODAY - timedelta(days=int(v.get("detail_ttl_days", 10))):
        return c.get("extra") or {}
    try:
        html = get(url, delay=float(v.get("crawl_delay", 0.6))).text
    except requests.RequestException:
        cache[key] = {"fetched": TODAY.isoformat(), "extra": {}}
        return {}
    extra: dict = {}
    for n in jsonld_events(html):
        ld = event_from_jsonld(n, v, url, "x")
        if ld:
            extra.update({"ld_start": ld.start, "ld_price": ld.price, "ld_genres": ld.genres, "lineup": ld.lineup})
            break
    txt = page_text(html)
    tdt, tstart, tdoors, tprice, tkind, tpkind = extract_from_text_ex(txt)
    if not tprice:
        tprice = price_from_embedded_json(html)
        tpkind = "embedded" if tprice else None
    if not tstart and not tdoors:
        tstart, tdoors = times_from_embedded_json(html)
        tkind = "embedded" if tstart else None
    if not tprice and not extra.get("ld_price") and v.get("ticketshops", True):
        tprice = stager_price_from_link(html, extra.get("ld_start"), None)
        tpkind = "shop" if tprice else None
    extra.update({"start": tstart, "doors": tdoors, "price": tprice, "start_kind": tkind, "price_kind": tpkind})
    if v.get("lineup") and not extra.get("lineup"):
        extra["lineup"] = clean_lineup([x.get_text() for x in soup_of(html).select(v["lineup"])])
    if not extra.get("ld_genres"):
        hints = genre_hints(txt[:1500], limit=4)
        extra["hint_genres"] = hints if len(hints) <= 2 else []
    cache[key] = {"fetched": TODAY.isoformat(), "v": _cache_version(v), "extra": extra}
    return extra


def apply_extra(e: Event, x: dict, needs_time: bool) -> None:
    """Regels voor het samenvoegen van eventpaginagegevens met een event uit een overzichtslijst.
    Herkomst (x['start_kind']) bepaalt hoeveel gewicht een paginatijd krijgt:
    - 'label' (expliciet 'aanvang/start 20:30'): mag een bestaande lijsttijd corrigeren (deuren -> aanvang), maar
      nooit met sprongen van meer dan 3 uur (dat is een leesfout: dB's "Tijd: 8:00 pm" las ooit als 08:00);
    - 'after_date', 'paren', 'schedule', 'embedded' (tijd zonder label, uit een schema of uit JSON): alleen gebruikt
      als de lijst geen tijd had of de lijsttijd gelijk is aan de deurtijd van de pagina;
    - deuren en JSON-LD alleen als er niets beters is."""
    st = datetime.fromisoformat(e.start)
    if x.get("start"):
        hh, mm = x["start"]
        diff = abs((hh * 60 + mm) - (st.hour * 60 + st.minute))
        kind = x.get("start_kind") or "label"  # oude cache-items zonder herkomst: gedrag van voorheen
        same = (hh, mm) == (st.hour, st.minute)
        at_doors = bool(x.get("doors")) and tuple(x["doors"]) == (st.hour, st.minute)
        if needs_time or (not same and diff <= 180 and (kind == "label" or at_doors)):
            e.start, e.time_est = st.replace(hour=hh, minute=mm).isoformat(timespec="minutes"), False
            e.time_src = kind
    elif needs_time:
        if x.get("ld_start") and x["ld_start"][11:16] != "00:00":
            e.start = st.replace(hour=int(x["ld_start"][11:13]), minute=int(x["ld_start"][14:16])).isoformat(timespec="minutes")
        elif x.get("doors"):
            e.start = st.replace(hour=x["doors"][0], minute=x["doors"][1]).isoformat(timespec="minutes")
    if not e.price:
        # prijsherkomst: een gelabelde tekstprijs of shopprijs gaat vóór JSON-LD (dat is vaak excl. servicekosten of
        # 'vanaf'); een los bedrag in de tekst ("text") pas als er geen JSON-LD-prijs is
        pk = x.get("price_kind") or ("labeled" if x.get("price") else None)   # oude cache-items zonder herkomst
        if x.get("price") and (pk in ("shop", "labeled") or not x.get("ld_price")):
            e.price, e.price_src = x["price"], pk
        elif x.get("ld_price"):
            e.price, e.price_src = x["ld_price"], "jsonld"
    if not e.genres:
        e.genres = x.get("ld_genres") or x.get("hint_genres") or []
    if not e.lineup and x.get("lineup"):
        e.lineup = x["lineup"]


def enrich_from_detail(v: dict, evs: list[Event], cache: dict) -> None:
    """Vul ontbrekende aanvangstijd/prijs/genre/line-up aan vanaf de eventpagina (gecached, met budget per run)."""
    budget = int(v.get("enrich_max", 120))
    for e in evs:
        needs_time = e.start[11:16] in ("", "00:00")
        if not (needs_time or not e.price or not e.genres) or not e.url or e.url.rstrip("/") == v["url"].rstrip("/"):
            continue
        cached = ("x|" + e.url) in cache
        if not cached:
            if budget <= 0:
                continue
            budget -= 1
        x = detail_extra(v, e.url, cache)
        if x:
            apply_extra(e, x, needs_time)


def strat_jsonld_detail(v: dict, html: str, base: str, cache: dict) -> list[Event]:
    pages = [html]
    for extra in v.get("list_pages", [])[1:]:
        try:
            pages.append(get(extra, delay=1.0).text)
        except requests.RequestException as ex:
            log(f"    extra pagina mislukt {extra}: {ex}")
    if v.get("list_pages_template"):
        # paginering: doorgaan tot een pagina 404 geeft of geen nieuwe eventlinks meer bevat
        seen_links = set(event_links(v, html, base))
        for n in range(2, int(v.get("list_pages_max", 60)) + 1):
            try:
                r = _net.SESSION.get(v["list_pages_template"].format(n=n + int(v.get("list_pages_offset", 0))), timeout=TIMEOUT)
            except requests.RequestException:
                break
            time.sleep(float(v.get("crawl_delay", 0.6)))
            if r.status_code != 200:
                break
            new_links = [l for l in event_links(v, r.text, base) if l not in seen_links]
            if not new_links:
                break
            seen_links.update(new_links)
            pages.append(r.text)
    links: list[str] = []
    for p in pages:
        for l in event_links(v, p, base):
            if l not in links:
                links.append(l)
    links = links[: int(v.get("max_detail", 400))]   # was 80: Nieuwe Nor en SPOT bleven daardoor op precies 80 events hangen
    log(f"    {len(links)} eventlinks, detailpagina's ophalen (gecached)…")
    out = []
    for l in links:
        ev = fetch_detail(v, l, cache)
        if ev:
            out.append(ev)
    return out


# --- HTML met selectors ----------------------------------------------------------

def strat_html(v: dict, html: str, base: str) -> list[Event]:
    s = soup_of(html)
    out = []
    for item in s.select(v["item"]):
        def pick(key):
            sel = v.get(key)
            return item.select_one(sel) if sel else None
        t = pick("title")
        title = clean(t.get(v["title_attr"]) if t is not None and v.get("title_attr") and t.has_attr(v["title_attr"]) else t.get_text()) if t else None
        # link: uit attribuut (bijv. data-target), uit selector, of de eerste <a>
        url = None
        if v.get("link_attr"):
            holder = item if item.has_attr(v["link_attr"]) else item.find(attrs={v["link_attr"]: True})
            url = urljoin(base, holder[v["link_attr"]]) if holder else None
        if not url:
            # de kaart zelf als die een <a> is (De Pul: <a class="agenda-event" href="/agenda/…"> met daarin een ticketlink naar
            # shop.tickets.cm.com), anders de eerste link op de eigen site, anders de eerste link
            a = pick("link") or (item if item.name == "a" and item.has_attr("href") else None)
            if a is None:
                links = item.find_all("a", href=True)
                host = urlparse(base).netloc.replace("www.", "")
                a = next((x for x in links if urlparse(urljoin(base, x["href"])).netloc.replace("www.", "") == host), None) or (links[0] if links else None)
            url = urljoin(base, a["href"]) if a and a.has_attr("href") else base
        d = pick("date")
        dt = None
        if d is not None:
            if d.has_attr("datetime") and not v.get("date_text_only"):
                dt = parse_dt(d.get("datetime"))
            dt = dt or parse_dt(clean(d.get_text(" ")))   # spaties tussen elementen: "Sun 18 . 10" i.p.v. "Sun18.10"
            if dt is None:
                dt = extract_from_text(d.get_text(" "))[0]
        if not dt and v.get("date_from_url"):
            dt = date_from_url(url)
        if not dt and v.get("group") and v.get("group_date"):
            # datum staat als kop boven een groep kaarten (Willemeen: .we__agenda-row > .we__agenda-item-date "vr 04 sep")
            grp = item.find_parent(class_=v["group"].lstrip(".")) if v["group"].startswith(".") else item.find_parent(v["group"])
            gd = grp.select_one(v["group_date"]) if grp else None
            if gd is not None:
                dt = parse_dt(clean(gd.get_text())) or extract_from_text(gd.get_text(" "))[0]
        if not dt:
            dt = extract_from_text(item.get_text(" "))[0]
        if not (title and dt):
            continue
        # tijd en prijs uit de tekst van het item (bijv. "Open 17:30 / Aanvang 18:00 / € 8,50")
        _, tstart, tdoors, tprice = extract_from_text(item.get_text(" "))
        if not tprice:
            # prijs als data-attribuut (Metropool: data-event-price="17.00" op de ticketknop)
            holder = item if any(k.endswith("price") for k in item.attrs) else item.find(lambda tag: any(k.endswith("price") for k in tag.attrs))
            if holder is not None:
                key = next(k for k in holder.attrs if k.endswith("price"))
                tprice = normalize_price(fmt_price(holder[key]))
        if not tstart and not tdoors:
            # één losse tijd in de kaart zonder label (Willemeen "12:00"): dat is de aanvang
            times = re.findall(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", item.get_text(" "))
            times = [(int(h), int(m)) for h, m in times if int(h) < 24 and int(m) < 60]
            if len(times) == 1:
                tstart = times[0]
        if (dt.hour, dt.minute) == (0, 0) and (tstart or tdoors):
            hh, mm = tstart or tdoors
            dt = dt.replace(hour=hh, minute=mm)
        genres = []
        for ga in as_list(v.get("genre_attr") or []):   # één of meer data-attributen (SPOT: data-genres + data-subgenres)
            holder = item if item.has_attr(ga) else item.find(attrs={ga: True})
            if holder:
                genres += [x.strip() for x in re.split(r"[,/|·•]", str(holder[ga])) if x.strip()]
        for g in item.select(v["genre"]) if v.get("genre") else []:
            # "VR 04 SEP | Rock, Symfo- & Progressive Rock" (De Pul): datumdelen zijn geen genre
            genres += [x.strip() for x in re.split(r"[,/|·•]", clean(g.get_text()) or "") if x.strip() and not re.search(r"\d", x)]
        sub = pick("subtitle")
        status = None
        if item.find(class_=re.compile(r"sold-?out|uitverkocht", re.I)) or re.search(r"\buitverkocht\b|\bsold[ -]?out\b", item.get_text(" "), re.I):
            status = "uitverkocht"
        if item.find(class_=re.compile(r"cancel|afgelast", re.I)) or re.search(r"\bafgelast\b|\bgeannuleerd\b", item.get_text(" "), re.I):
            status = "afgelast"
        out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=url,
                         subtitle=clean(sub.get_text()) if sub else None, genres=list(dict.fromkeys(g for g in genres if g)),
                         price=tprice, status=status, source="html"))
    return out


def strat_html_api(v: dict, base: str) -> list[Event]:
    """HTML-fragmenten uit een eigen 'laad meer'-eindpunt (De Pul: query.php?…&amount_of_events_already_shown={offset}
    geeft {"output": "<a class=agenda-event>…"}). Dezelfde CSS-selectors als type: html, maar de bron is de API:
      type: html
      api: "https://…/query.php?source=agenda&amount_of_events_already_shown={offset}"   # {offset} = al getoonde items, {page} = paginanummer
      api_html_key: output          # JSON-sleutel met het fragment; weglaten als het antwoord zelf HTML is
    Stopt als een pagina geen (nieuwe) items meer oplevert."""
    out: list[Event] = []
    seen: set[str] = set()
    offset, page = 0, 1
    while page <= int(v.get("max_pages", 30)):
        url = v["api"].replace("{offset}", str(offset)).replace("{page}", str(page))
        r = get(url, delay=float(v.get("crawl_delay", 0.6)))
        html = r.text
        if v.get("api_html_key"):
            try:
                html = str(_path(r.json(), v["api_html_key"]) or "")
            except ValueError:
                html = str(_path(json.loads(r.text.strip()), v["api_html_key"]) or "")
        n_items = len(soup_of(html).select(v["item"]))
        evs = [e for e in strat_html(v, html, base) if e.url not in seen]
        if not n_items or not evs:
            break
        seen.update(e.url for e in evs)
        out += evs
        offset += n_items
        page += 1
        if "{offset}" not in v["api"] and "{page}" not in v["api"]:
            break
    return out


def strat_facetwp(v: dict, base: str) -> list[Event]:
    """FacetWP (WordPress-plugin voor filters + 'laad meer'; Bibelot): de pagina toont 30 events, de rest komt via een
    JSON-POST naar dezelfde URL met {"action":"facetwp_refresh","data":{"paged":n,…}}; het antwoord bevat "template"
    (HTML-fragment) en settings.pager.total_pages. Zelfde CSS-selectors als type: html.
      type: html
      facetwp: true          # of automatisch: de pagina bevat 'facetwp' (class facetwp-template / facetwp-load-more)
    """
    path = urlparse(base).path.strip("/")
    out: list[Event] = []
    seen: set[str] = set()
    total_pages = int(v.get("max_pages", 20))
    page = 1
    while page <= total_pages:
        body = {"action": "facetwp_refresh", "data": {"facets": {}, "frozen_facets": {}, "http_params": {"get": [], "uri": path, "url_vars": []},
                                                      "template": "wp", "extras": {}, "soft_refresh": 1, "first_load": 0, "paged": page}}
        r = http_post(base, json=body, headers={**(BROWSER_HEADERS if urlparse(base).netloc in _BROWSER_UA_HOSTS else {}), "Accept": "application/json"})
        time.sleep(float(v.get("crawl_delay", 0.6)))
        r.raise_for_status()
        try:
            j = r.json()
        except ValueError:
            break
        html = j.get("template") or ""
        pager = (j.get("settings") or {}).get("pager") or {}
        if pager.get("total_pages"):
            total_pages = min(total_pages, int(pager["total_pages"]))
        evs = [e for e in strat_html(v, html, base) if e.url not in seen]
        if not evs:
            break
        seen.update(e.url for e in evs)
        out += evs
        page += 1
    return out


# bekende WordPress-thema's/plugins met vaste class-namen: automatisch herkend
HTML_PRESETS = [
    {"detect": ".wp_theatre_event", "item": ".wp_theatre_event", "title": ".wp_theatre_event_title", "date": ".wp_theatre_event_startdate, .wp_theatre_event_date",
     "subtitle": ".wp_theatre_event_subtitle, .wp_theatre_event_support_title", "genre": ".wp_theatre_event_categories", "name": "theater-for-wordpress"},
    {"detect": ".event-program", "item": ".event-program", "title": ".event-program__name", "date": ".event-program__date", "subtitle": ".event-program__subtitle",
     "genre": ".event-program__genres, .event-program__tags", "date_from_url": True, "name": "patronaat-thema"},
]


def strat_html_preset(v: dict, html: str, base: str) -> tuple[list[Event], str | None]:
    s = soup_of(html)
    for preset in HTML_PRESETS:
        if len(s.select(preset["detect"])) >= 3:
            cfg = {**preset, **v}
            return strat_html(cfg, html, base), preset["name"]
    return [], None

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
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE = ROOT / "state"
DATA.mkdir(exist_ok=True)
STATE.mkdir(exist_ok=True)

UA = "poppodia-agenda/1.0 (persoonlijke concertagenda; 1 run per dag; contact via github.com/aukedamsma/poppodia-agenda)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "nl,en;q=0.8"})
TIMEOUT = 25
TODAY = date.today()
HORIZON = TODAY + timedelta(days=400)

EVENT_LINK_HINTS = ("/agenda/", "/event/", "/events/", "/evenement", "/programma/", "/voorstelling/", "/concert", "/shows/", "/show/")

NL_MONTHS = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "maart": 3, "mrt": 3, "april": 4, "apr": 4,
    "mei": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "augustus": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


@dataclass
class Event:
    venue: str
    city: str
    title: str
    start: str                     # ISO 8601, lokale tijd (Europe/Amsterdam) zonder tz of met tz
    url: str
    end: str | None = None
    subtitle: str | None = None
    genres: list[str] = field(default_factory=list)
    price: str | None = None
    status: str | None = None      # afgelast / uitverkocht / verplaatst
    source: str = ""               # strategie waarmee gevonden
    first_seen: str | None = None  # wordt gezet uit state/seen.json


# ----------------------------------------------------------------------------
# hulpfuncties
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def get(url: str, delay: float = 0.0, **kw) -> requests.Response:
    if delay:
        time.sleep(delay)
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = unescape(str(s))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def parse_dt(value, default_year: int | None = None) -> datetime | None:
    """Zet allerlei datumvormen om naar datetime. Geeft None bij mislukking."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # ISO of bijna-ISO
    try:
        dt = dtparser.isoparse(s)
        if dt.tzinfo is not None:
            # naar Nederlandse lokale tijd (benadering: CET/CEST via systeem-tz van de runner is UTC,
            # daarom expliciet +1/+2 aan de hand van zomertijd)
            dt = to_local(dt)
        return dt
    except (ValueError, OverflowError):
        pass
    # Nederlandse datum: "vr 4 sep 2026", "donderdag 29 april 2027 20:30", "do 25.03.27", "04-09-2026"
    low = s.lower()
    m = re.search(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})(?:\D+(\d{1,2})[:.](\d{2}))?", low)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        try:
            return datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s+([a-z]+)\.?\s*(\d{4})?(?:\D+(\d{1,2})[:.](\d{2}))?", low)
    if m and m.group(2) in NL_MONTHS:
        d, mo = int(m.group(1)), NL_MONTHS[m.group(2)]
        y = int(m.group(3)) if m.group(3) else (default_year or TODAY.year)
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        try:
            dt = datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
        if not m.group(3) and dt.date() < TODAY - timedelta(days=30):
            dt = dt.replace(year=dt.year + 1)
        return dt
    try:
        return dtparser.parse(s, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def to_local(dt: datetime) -> datetime:
    """UTC/tz-aware -> naive Nederlandse tijd (CET/CEST)."""
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    # zomertijd: laatste zondag maart 01:00 UTC t/m laatste zondag oktober 01:00 UTC
    y = utc.year
    def last_sunday(month):
        d = date(y, month, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    start = datetime.combine(last_sunday(3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(last_sunday(10), datetime.min.time()) + timedelta(hours=1)
    offset = 2 if start <= utc < end else 1
    return utc + timedelta(hours=offset)


def date_from_url(url: str) -> datetime | None:
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})(?:/|$)", url) or re.search(r"(\d{2})-(\d{2})-(\d{2})(?:/|$)", url)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def in_window(dt: datetime | None) -> bool:
    return dt is not None and TODAY - timedelta(days=1) <= dt.date() <= HORIZON


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def jsonld_blocks(html: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            try:
                j = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        items = j if isinstance(j, list) else [j]
        for it in items:
            if isinstance(it, dict):
                if "@graph" in it and isinstance(it["@graph"], list):
                    out.extend(x for x in it["@graph"] if isinstance(x, dict))
                else:
                    out.append(it)
    return out


def jsonld_events(html: str) -> list[dict]:
    return [n for n in jsonld_blocks(html) if "Event" in str(n.get("@type", ""))]


def event_from_jsonld(n: dict, v: dict, page_url: str, source: str) -> Event | None:
    start = parse_dt(n.get("startDate"))
    if not start:
        return None
    url = n.get("url") or page_url
    if isinstance(url, dict):
        url = url.get("@id") or page_url
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
    perf = ", ".join(clean(p.get("name")) for p in as_list(n.get("performer")) if isinstance(p, dict) and clean(p.get("name"))) or None
    return Event(
        venue=v["name"], city=v["city"], title=clean(n.get("name")) or "(zonder titel)",
        start=start.isoformat(timespec="minutes"), url=urljoin(page_url, str(url)),
        end=(parse_dt(n.get("endDate")) or start).isoformat(timespec="minutes") if n.get("endDate") else None,
        subtitle=perf if perf and perf.lower() != (clean(n.get("name")) or "").lower() else None,
        genres=genres, price=price, status=status, source=source,
    )


# ----------------------------------------------------------------------------
# strategieën
# ----------------------------------------------------------------------------

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
    if depth > 12:
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
    # ook window.__NUXT__ / __INITIAL_STATE__ als JSON-literal
    for m in re.finditer(r"window\.__(?:NUXT|INITIAL_STATE|APOLLO_STATE|PRELOADED_STATE)__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S):
        blobs.append(m.group(1))
    best: list[dict] = []
    for raw in blobs:
        if not raw or len(raw) < 200:
            continue
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for lst in _walk_event_lists(j):
            if len(lst) > len(best):
                best = lst
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
        if o.get("isCancelled") is True:
            status = "afgelast"
        elif o.get("isSoldOut") is True:
            status = status or "uitverkocht"
        if o.get("isPublished") is False or o.get("publish") is False:
            continue
        price = o.get("price") or o.get("ticket_price") or o.get("priceFrom")
        sub = o.get("subtitle") or o.get("tagline") or o.get("one_liner")
        end = None
        for k in ("endDate", "end_date", "end", "ends_at"):
            if isinstance(o.get(k), str):
                e = parse_dt(o[k])
                end = e.isoformat(timespec="minutes") if e else None
                break
        out.append(Event(
            venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"),
            url=url, end=end, subtitle=clean(sub) if isinstance(sub, str) else None,
            genres=list(dict.fromkeys(_find_genres(o) + extra_genres)), price=(f"€ {price}" if isinstance(price, (int, float)) and price else (clean(price) if isinstance(price, str) else None)),
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

def _acf_date(acf: dict) -> datetime | None:
    if not isinstance(acf, dict):
        return None
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
        types = get(urljoin(base, "/wp-json/wp/v2/types"), delay=0.5).json()
        cands = [t for t in types.values() if re.search(r"event|evenement|agenda|programma|voorstelling|concert|show", t.get("rest_base", "") + t.get("slug", ""), re.I)]
        if not cands:
            return []
        api = urljoin(base, f"/wp-json/wp/v2/{cands[0]['rest_base']}?per_page=100&_embed=1")
    out, page = [], 1
    while page <= 10:
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
            for grp in (it.get("_embedded", {}) or {}).get("wp:term", []):
                for term in grp:
                    if term.get("taxonomy") not in ("category", "post_tag", "language") and term.get("name"):
                        genres.append(clean(term["name"]))
            price = acf.get("price") if isinstance(acf, dict) else None
            sub = acf.get("one_liner") or acf.get("subtitle") or acf.get("support_act") if isinstance(acf, dict) else None
            if not dt and v.get("detail_jsonld", True):
                # datum staat alleen op de eventpagina -> JSON-LD of HTML daar lezen
                ev = fetch_detail(v, link, detail_cache)
                if ev:
                    ev.genres = ev.genres or genres
                    ev.source = "wp_event+detail"
                    out.append(ev)
                continue
            if not dt:
                continue
            out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=link,
                             subtitle=clean(sub) if isinstance(sub, str) else None, genres=genres,
                             price=(f"€ {price}" if price not in (None, "", 0) else None) if isinstance(price, (str, int, float)) else None, source="wp_event"))
        if len(items) < 100 and "per_page=100" in api:
            break
        if int(r.headers.get("X-WP-TotalPages", 1)) <= page:
            break
        page += 1
    return out


# --- eventlinks volgen + JSON-LD op detailpagina --------------------------------

def event_links(v: dict, html: str, base: str) -> list[str]:
    s = soup_of(html)
    pat = re.compile(v["link_pattern"]) if v.get("link_pattern") else None
    host = urlparse(base).netloc.replace("www.", "")
    seen, out = set(), []
    for a in s.find_all("a", href=True):
        href = urljoin(base, a["href"].split("#")[0])
        if urlparse(href).netloc.replace("www.", "") != host:
            continue
        path = urlparse(href).path
        if pat:
            if not pat.search(href):
                continue
        elif not any(h in path for h in EVENT_LINK_HINTS) or path.rstrip("/") in ("", urlparse(base).path.rstrip("/")) or re.search(r"/page/\d+|/en/|/tag/|/genre/|/categor", path):
            continue
        # sla de agenda-overzichtspagina zelf en filterlinks over
        if path.rstrip("/").count("/") < 2 and not re.search(r"\d", path):
            continue
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


def fetch_detail(v: dict, url: str, cache: dict) -> Event | None:
    """Leest een eventpagina; cache op URL zodat dit maar zelden opnieuw hoeft."""
    c = cache.get(url)
    if c and c.get("fetched") and date.fromisoformat(c["fetched"]) > TODAY - timedelta(days=int(v.get("detail_ttl_days", 10))):
        return Event(**c["event"]) if c.get("event") else None
    try:
        html = get(url, delay=float(v.get("crawl_delay", 1.0))).text
    except requests.RequestException as ex:
        cache[url] = {"fetched": TODAY.isoformat(), "event": None, "error": str(ex)[:200]}
        return None
    ev = None
    for n in jsonld_events(html):
        ev = event_from_jsonld(n, v, url, "jsonld_detail")
        if ev:
            break
    if ev is None and v.get("type") == "html":
        ev = None  # html-type haalt de datum uit de lijstpagina
    if ev is None:
        # laatste redmiddel: <time datetime> op de pagina + <h1>
        s = soup_of(html)
        t = s.find("time", attrs={"datetime": True})
        h1 = s.find("h1")
        dt = parse_dt(t["datetime"]) if t else date_from_url(url)
        if not dt:
            # voluit geschreven Nederlandse datum in de paginatekst ("zaterdag 3 oktober 2026")
            body = s.find("main") or s.body or s
            m = re.search(r"\b(\d{1,2})\s+(januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december)\s+(\d{4})", body.get_text(" "), re.I)
            if m:
                dt = parse_dt(m.group(0))
                tm = re.search(r"(?:aanvang|start|begin)\D{0,12}(\d{1,2})[:.](\d{2})", body.get_text(" "), re.I)
                if dt and tm:
                    dt = dt.replace(hour=int(tm.group(1)), minute=int(tm.group(2)))
        if dt and h1:
            ev = Event(venue=v["name"], city=v["city"], title=clean(h1.get_text()) or "?", start=dt.isoformat(timespec="minutes"), url=url, source="detail_time")
    cache[url] = {"fetched": TODAY.isoformat(), "event": asdict(ev) if ev else None}
    return ev


def strat_jsonld_detail(v: dict, html: str, base: str, cache: dict) -> list[Event]:
    pages = [html]
    for extra in v.get("list_pages", [])[1:]:
        try:
            pages.append(get(extra, delay=1.0).text)
        except requests.RequestException as ex:
            log(f"    extra pagina mislukt {extra}: {ex}")
    links: list[str] = []
    for p in pages:
        for l in event_links(v, p, base):
            if l not in links:
                links.append(l)
    links = links[: int(v.get("max_detail", 80))]
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
            if not sel:
                return None
            el = item.select_one(sel)
            return el
        t = pick("title")
        title = clean(t.get_text()) if t else None
        a = pick("link") or item.find("a", href=True) or (item if item.name == "a" else None)
        url = urljoin(base, a["href"]) if a and a.has_attr("href") else base
        d = pick("date")
        dt = None
        if d is not None:
            dt = parse_dt(d.get("datetime")) if d.has_attr("datetime") else parse_dt(d.get_text())
        if not dt and v.get("date_from_url"):
            dt = date_from_url(url)
        if not (title and dt):
            continue
        g = pick("genre")
        genres = [x.strip() for x in re.split(r"[,/|·•]", clean(g.get_text()) or "") if x.strip()] if g else []
        sub = pick("subtitle")
        out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=url,
                         subtitle=clean(sub.get_text()) if sub else None, genres=genres, source="html"))
    return out


# ----------------------------------------------------------------------------
# per podium
# ----------------------------------------------------------------------------

def fetch_venue(v: dict, cache: dict) -> tuple[list[Event], str, str]:
    """Geeft (events, gebruikte strategie, opmerking)."""
    base = v["url"]
    t = v.get("type", "auto")
    if t == "disabled" or v.get("enabled") is False:
        return [], "disabled", "uitgeschakeld in venues.yaml"

    html = ""
    if t not in ("tribe",):
        html = get(base, delay=float(v.get("crawl_delay", 0))).text

    order = {
        "auto": ["jsonld", "embedded", "tribe", "wp_event", "jsonld_detail"],
        "jsonld": ["jsonld"], "embedded": ["embedded"], "nextdata": ["embedded"],
        "tribe": ["tribe"], "wp_event": ["wp_event"], "jsonld_detail": ["jsonld_detail"], "html": ["html"],
    }.get(t, [t])
    notes = []
    for strat in order:
        try:
            if strat == "jsonld":
                evs = strat_jsonld(v, html)
            elif strat == "embedded":
                evs = strat_embedded(v, html)
            elif strat == "tribe":
                evs = strat_tribe(v, base)
            elif strat == "wp_event":
                evs = strat_wp_event(v, base, cache)
            elif strat == "jsonld_detail":
                evs = strat_jsonld_detail(v, html, base, cache)
            elif strat == "html":
                evs = strat_html(v, html, base)
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
        if len(evs) >= int(v.get("min_events", 3)):
            return evs, strat, "; ".join(notes)
        notes.append(f"{strat}: {len(evs)} events")
    return [], "none", "; ".join(notes)


def dedupe(events: list[Event]) -> list[Event]:
    seen, out = {}, []
    for e in events:
        key = (e.venue, e.url.rstrip("/").lower()) if e.url else (e.venue, e.title.lower(), e.start[:10])
        if key in seen:
            # bewaar de rijkste versie
            old = seen[key]
            if len(e.genres) > len(old.genres) or (e.price and not old.price):
                out[out.index(old)] = e
                seen[key] = e
            continue
        seen[key] = e
        out.append(e)
    return out


def main(only: list[str] | None = None) -> int:
    venues = yaml.safe_load((ROOT / "venues.yaml").read_text(encoding="utf-8"))
    seen_path, cache_path = STATE / "seen.json", STATE / "detail_cache.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    first_run = not seen

    all_events: list[Event] = []
    report = {"generated": datetime.now().isoformat(timespec="seconds"), "venues": []}
    for v in venues:
        if only and v["name"] not in only:
            continue
        log(f"== {v['name']} ({v['city']})")
        t0 = time.time()
        try:
            evs, strat, note = fetch_venue(v, cache)
        except Exception as ex:  # noqa: BLE001
            evs, strat, note = [], "error", f"{type(ex).__name__}: {str(ex)[:200]}"
            traceback.print_exc()
        evs = dedupe(evs)
        log(f"   -> {len(evs)} events via {strat} ({time.time()-t0:.1f}s) {note}")
        report["venues"].append({"name": v["name"], "city": v["city"], "url": v["url"], "strategy": strat,
                                 "events": len(evs), "note": note, "ok": len(evs) > 0})
        all_events.extend(evs)

    # 'nieuw' bepalen
    for e in all_events:
        key = f"{e.venue}|{e.url}"
        if key not in seen:
            seen[key] = TODAY.isoformat()
        e.first_seen = seen[key]
    # opschonen: sleutels van events die > 60 dagen weg zijn
    live = {f"{e.venue}|{e.url}" for e in all_events}
    for k in list(seen):
        if k not in live and date.fromisoformat(seen[k]) < TODAY - timedelta(days=60):
            del seen[k]
    if first_run:
        # eerste run: niets als 'nieuw' markeren, anders is alles nieuw
        for e in all_events:
            e.first_seen = (TODAY - timedelta(days=30)).isoformat()
            seen[f"{e.venue}|{e.url}"] = e.first_seen

    all_events.sort(key=lambda e: (e.start, e.venue, e.title))
    (DATA / "events.json").write_text(json.dumps([asdict(e) for e in all_events], ensure_ascii=False, indent=1), encoding="utf-8")
    report["total_events"] = len(all_events)
    (DATA / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    ok = sum(1 for r in report["venues"] if r["ok"])
    log(f"\nKlaar: {len(all_events)} events uit {ok}/{len(report['venues'])} podia")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))

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
import os
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

import artists as artistdb
import series as seriesdb
from taxonomy import classify_kind, extract_artists, normalize_genres, price_number, group_label, _taxonomy, genre_hints, artist_key

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

EVENT_LINK_HINTS = ("/agenda/", "/event/", "/events/", "/evenement", "/programma/", "/program/", "/voorstelling", "/concert", "/shows/", "/show/", "/production/", "/productie/", "/activiteit")

NL_MONTHS = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "maart": 3, "mrt": 3, "april": 4, "apr": 4,
    "mei": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "augustus": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
    # Engels (sommige podia tonen Engelse datums)
    "january": 1, "february": 2, "march": 3, "mar": 3, "may": 5, "june": 6, "july": 7, "august": 8,
    "october": 10, "oct": 10,
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
    artists: list[str] = field(default_factory=list)      # herkende artiesten, eerste = headliner
    genre_norm: list[str] = field(default_factory=list)   # hoofdgenres (genres.yaml)
    kind: str = "concert"          # concert | club | festival | other
    price_num: float | None = None
    free: bool = False
    lineup: list[str] = field(default_factory=list)       # line-up van de eventpagina (JSON-LD performer of `lineup`-selector)
    price_est: bool = False        # prijs geschat uit eerdere edities van dezelfde reeks (state/series.json)
    time_est: bool = False         # aanvangstijd idem
    section: str = "poppodium"     # poppodium | overig (uit venues.yaml)


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


def fmt_price(p) -> str | None:
    if p in (None, "", 0, "0"):
        return None
    if isinstance(p, (int, float)):
        return f"€ {p:g}".replace(".", ",")
    p = clean(str(p)) or ""
    if re.fullmatch(r"\d+([.,]\d{1,2})?", p):
        return "€ " + p.replace(".", ",").removesuffix(",00")
    return p or None


def parse_dt(value, default_year: int | None = None) -> datetime | None:
    """Zet allerlei datumvormen om naar datetime. Geeft None bij mislukking."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            if value > 1e12:  # milliseconden
                value = value / 1000
            return to_local(datetime.fromtimestamp(value, tz=timezone.utc))
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
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:\D+(\d{1,2})[:.](\d{2}))?", low)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mm = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        try:
            return datetime(y, mo, d, hh, mm)
        except ValueError:
            return None
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
    m = re.fullmatch(r"(?:[a-z]{2,9}\.?\s+)?(\d{1,2})[./](\d{1,2})\.?(?:\s+(\d{1,2})[:.](\d{2}))?", low)
    if m and 1 <= int(m.group(2)) <= 12:
        d, mo = int(m.group(1)), int(m.group(2))
        hh, mm = (int(m.group(3)), int(m.group(4))) if m.group(3) else (0, 0)
        try:
            dt = datetime(default_year or TODAY.year, mo, d, hh, mm)
        except ValueError:
            return None
        if dt.date() < TODAY - timedelta(days=30):
            dt = dt.replace(year=dt.year + 1)
        return dt
    m = re.search(r"(\d{1,2})\s+([a-z]+)\.?\s*(\d{4})?(?:\D*?(\d{1,2})[:.](\d{2})(?!\d))?", low)
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


def _strip_tz(val):
    """Voor podia die lokale tijd met een verkeerde tijdzone publiceren (bv. "22:00:00+00:00" voor 22:00 NL)."""
    return re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", str(val).strip()) if isinstance(val, str) else val


def event_from_jsonld(n: dict, v: dict, page_url: str, source: str) -> Event | None:
    if v.get("time_is_local"):
        n = {**n, "startDate": _strip_tz(n.get("startDate")), "endDate": _strip_tz(n.get("endDate")), "doorTime": _strip_tz(n.get("doorTime"))}
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
    )


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
        if o.get("isCancelled") is True or o.get("cancelled") is True:
            status = "afgelast"
        elif o.get("isSoldOut") is True or o.get("soldOut") is True or o.get("sold_out") is True:
            status = status or "uitverkocht"
        if o.get("isPublished") is False or o.get("publish") is False:
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
        rest_base = None
        try:
            types = get(urljoin(base, "/wp-json/wp/v2/types"), delay=0.5).json()
            cands = [t for t in types.values() if re.search(r"event|evenement|agenda|programma|voorstelling|concert|show", t.get("rest_base", "") + t.get("slug", ""), re.I)
                     and not re.search(r"log|activity|ticket|serie|categor", t.get("rest_base", ""), re.I)]
            if cands:
                rest_base = cands[0]["rest_base"]
        except (requests.RequestException, ValueError):
            pass
        if not rest_base:
            # typenlijst afgeschermd: gangbare namen proberen
            for guess in ("event", "events", "evenement", "evenementen", "agenda", "programma", "voorstelling", "voorstellingen", "concert", "concerten", "show", "shows"):
                try:
                    r = SESSION.get(urljoin(base, f"/wp-json/wp/v2/{guess}?per_page=1"), timeout=TIMEOUT)
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
            for grp in (it.get("_embedded", {}) or {}).get("wp:term", []):
                for term in grp:
                    if term.get("taxonomy") not in ("category", "post_tag", "language") and term.get("name"):
                        genres.append(clean(term["name"]))
            price = acf.get("price") if isinstance(acf, dict) else None
            sub = acf.get("one_liner") or acf.get("subtitle") or acf.get("support_act") if isinstance(acf, dict) else None
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

NON_EVENT_PATH = re.compile(r"/page/\d+|/en/|/english|/tag/|/tags/|/genre/|/genres/|/categor|/zoek|/search|/filter|/feed|/wp-|/nieuws|/news|/blog|/over|/about|/contact|/vacature|/verhuur|/faq|/privacy|/cookie|/algemene|/login|/account|/cart|/winkel|/shop|/merch|/pers|/partners|/steun|/vrienden|/locatie|/route|/tickets?$|/programma/?$|/agenda/?$|/events?/?$|/evenementen/?$|/agenda/(concerten|exposities?|expo|film|films|kids|jeugd|kidsjeugd|theater|cabaret|comedy|dans|workshops?|cursussen|festivals?|clubs?|party|feesten|overig|alles|all)/?$|\.(pdf|jpe?g|png|ics)$", re.I)


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


def fetch_detail(v: dict, url: str, cache: dict, title: str | None = None) -> Event | None:
    """Leest een eventpagina; cache op URL zodat dit maar zelden opnieuw hoeft."""
    c = cache.get(url)
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
    tdt, tstart, tdoors, tprice = extract_from_text(txt)
    if ev is None:
        s = soup_of(html)
        t = s.find("time", attrs={"datetime": True})
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
        dt = (parse_dt(t["datetime"]) if t else None) or date_from_url(url) or tdt
        if dt and (dt.hour, dt.minute) == (0, 0) and (tstart or tdoors):
            dt = dt.replace(hour=(tstart or tdoors)[0], minute=(tstart or tdoors)[1])
        if dt and title:
            ev = Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=url,
                       price=tprice, source="detail_text")
    if ev is not None:
        if v.get("lineup") and not ev.lineup:
            names = [clean(x.get_text()) for x in soup_of(html).select(v["lineup"])]
            ev.lineup = [x for x in dict.fromkeys(names) if x and 1 < len(x) <= 60 and x.lower() != ev.title.lower()][:20]
        if not ev.genres:
            ev.genres = genre_hints(txt[:1500])
        # aanvang gaat vóór deuren-open: als de gevonden tijd gelijk is aan de deurtijd en er staat een aanvang, neem die
        st = datetime.fromisoformat(ev.start)
        if tstart and (((st.hour, st.minute) == (0, 0)) or (tdoors and (st.hour, st.minute) == tdoors and tstart != tdoors)):
            ev.start = st.replace(hour=tstart[0], minute=tstart[1]).isoformat(timespec="minutes")
        if not ev.price and tprice:
            ev.price = tprice
    cache[url] = {"fetched": TODAY.isoformat(), "event": asdict(ev) if ev else None}
    return ev


MONTH_RE = r"(januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december|jan|feb|mrt|apr|jun|jul|aug|sep|sept|okt|nov|dec|january|february|march|may|june|july|october|mar|oct)"
WEEKDAY_RE = r"(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|ma|di|wo|do|vr|za|zo|monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)"


def page_text(html: str) -> str:
    """Zichtbare tekst van het hoofddeel van een pagina (zonder scripts/styles/nav zover herkenbaar)."""
    s = soup_of(html)
    for t in s(["script", "style", "noscript", "nav", "footer", "header"]):
        t.decompose()
    body = s.find("main") or s.find("article") or s.body or s
    return re.sub(r"\s+", " ", body.get_text(" "))


def extract_from_text(txt: str) -> tuple[datetime | None, tuple[int, int] | None, tuple[int, int] | None, str | None]:
    """(datum, aanvangstijd, deurtijd, prijs) uit vrije tekst van een eventpagina.
    Datum: liefst met weekdag of 'datum:' ervoor, anders de eerste dag+maand(+jaar)."""
    low = txt.lower()
    dt = None
    for pat in (rf"\b{WEEKDAY_RE}\.?\s+(\d{{1,2}})\s+{MONTH_RE}\.?(?:\s+(\d{{4}}))?",
                rf"\b{WEEKDAY_RE}\.?\s+(\d{{1,2}}[./]\d{{1,2}}(?:[./]\d{{2,4}})?)\b",
                rf"\bdatum\W{{0,6}}(\d{{1,2}})\s+{MONTH_RE}\.?(?:\s+(\d{{4}}))?",
                rf"\b(\d{{1,2}})\s+{MONTH_RE}\.?\s+(\d{{4}})\b",
                rf"\b(\d{{1,2}})\s+{MONTH_RE}\b\.?"):
        m = re.search(pat, low)
        if m:
            groups = [g for g in m.groups() if g]
            dt = parse_dt(" ".join(groups[-3:] if len(groups) >= 3 and groups[-1].isdigit() and len(groups[-1]) == 4 else groups[-2:]))
            if dt:
                break
    def hm(pat):
        mm = re.search(pat, low)
        return (int(mm.group(1)), int(mm.group(2))) if mm else None
    start = hm(r"(?:aanvang|start|begin|show|showtime|concert)\W{0,12}(\d{1,2})[:.u](\d{2})")
    doors = hm(r"(?:deur|deuren|doors|zaal open|open)\W{0,14}(\d{1,2})[:.u](\d{2})")
    pm = (re.search(r"€\s?(\d{1,3}(?:[.,]\d{2})?)(?:,-)?", low)
          or re.search(r"(\d{1,3}(?:[.,]\d{2})?)\s?(?:€|(?<![a-z])euro\b)", low)
          or re.search(r"(?<![a-z])euro?\b\s?(\d{1,3}(?:[.,]\d{2})?)(?:,-)?", low)
          or re.search(r"(?:tickets?|entree|kaarten|prijs|vvk|voorverkoop)\W{0,12}(\d{1,3}[.,]\d{2})\b", low))
    price = None
    if re.search(r"\bgratis\b|\bfree\b(?! ?(wifi|parking))", low) and not pm:
        price = "gratis"
    elif pm:
        price = fmt_price(pm.group(1))
    return dt, start, doors, price


def event_from_flight_json(html: str, url: str, v: dict) -> Event | None:
    """Eventgegevens uit een React Server Components-payload (o.a. Paradiso: Craft CMS via Next.js)."""
    ev_id = url.rstrip("/").rsplit("/", 1)[-1]
    clean_html = html.replace('\\"', '"')
    m = re.search(r'"__typename":\s*"event_\w+_Entry",\s*"id":\s*"%s".{0,4000}?"startDateTime":\s*"([^"]+)"' % re.escape(ev_id), clean_html, re.S)
    if not m:
        return None
    block = clean_html[m.start(): m.end() + 2500]

    def fld(name):
        mm = re.search(r'"%s":\s*"([^"]*)"' % name, block)
        return clean(mm.group(1)) if mm else None

    start = parse_dt(m.group(1))
    if not start:
        return None
    title = fld("title") or "?"
    genres = []
    if '"subBrand"' in block:
        genres = [g for g in re.findall(r'"title":\s*"([^"]+)"', block.split('"subBrand"', 1)[1]) if g][:3]
    loc = re.search(r'"location":\s*\{[^{}]*?"title":\s*"([^"]+)"', block)
    status = None
    if re.search(r'"(cancelled|isCancelled)":true', block) or "afgelast" in block.lower():
        status = "afgelast"
    elif re.search(r'"(soldOut|isSoldOut)":true', block) or "uitverkocht" in block.lower():
        status = "uitverkocht"
    sub = fld("subtitle")
    if loc and loc.group(1).lower() not in (v["name"].lower(),):
        sub = (sub + " · " if sub else "") + loc.group(1)
    price = fld("price") or fld("priceFrom")
    return Event(venue=v["name"], city=v["city"], title=title, start=start.isoformat(timespec="minutes"), url=url,
                 subtitle=sub, genres=genres, price=(f"€ {price}" if price else None), status=status, source="flight_json")


def strat_sitemap_detail(v: dict, base: str, cache: dict) -> list[Event]:
    """Eventlinks uit de sitemap (laatste N bestanden = nieuwste events), daarna per eventpagina lezen (gecached)."""
    sitemap = v.get("sitemap") or urljoin(base, "/sitemap.xml")
    idx = get(sitemap, delay=0.5).text
    files = [m.group(1) for m in re.finditer(r"<loc>(.*?)</loc>", idx)]
    pat = re.compile(v.get("sitemap_pattern", r"event"))
    files = [f for f in files if pat.search(f)]

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
    urls = [u for u in dict.fromkeys(urls) if not lp or lp.search(u)]
    urls = urls[-int(v.get("max_detail", 600)):]
    log(f"    {len(urls)} eventlinks uit sitemap, detailpagina's ophalen (gecached)…")
    out = []
    for u in urls:
        ev = fetch_detail(v, u, cache)
        if ev:
            out.append(ev)
    return out


def enrich_from_detail(v: dict, evs: list[Event], cache: dict) -> None:
    """Vul ontbrekende aanvangstijd/prijs aan vanaf de eventpagina (tekst-extractie, gecached)."""
    budget = int(v.get("enrich_max", 120))
    for e in evs:
        needs_time = e.start[11:16] in ("", "00:00")
        if not (needs_time or not e.price) or not e.url or e.url.rstrip("/") == v["url"].rstrip("/"):
            continue
        key = "x|" + e.url
        c = cache.get(key)
        if not c or date.fromisoformat(c["fetched"]) < TODAY - timedelta(days=int(v.get("detail_ttl_days", 10))):
            if budget <= 0:
                continue
            budget -= 1
            try:
                html = get(e.url, delay=float(v.get("crawl_delay", 0.6))).text
            except requests.RequestException:
                cache[key] = {"fetched": TODAY.isoformat(), "extra": {}}
                continue
            # eerst gestructureerd (JSON-LD op de eventpagina), dan tekst
            extra = {}
            for n in jsonld_events(html):
                ld = event_from_jsonld(n, v, e.url, "x")
                if ld:
                    extra["ld_start"] = ld.start
                    extra["ld_price"] = ld.price
                    extra["ld_genres"] = ld.genres
                    break
            tdt, tstart, tdoors, tprice = extract_from_text(page_text(html))
            extra.update({"start": tstart, "doors": tdoors, "price": tprice})
            cache[key] = {"fetched": TODAY.isoformat(), "extra": extra}
            c = cache[key]
        x = c.get("extra") or {}
        st = datetime.fromisoformat(e.start)
        if needs_time:
            if x.get("start"):
                st = st.replace(hour=x["start"][0], minute=x["start"][1])
            elif x.get("ld_start") and x["ld_start"][11:16] != "00:00":
                st = st.replace(hour=int(x["ld_start"][11:13]), minute=int(x["ld_start"][14:16]))
            elif x.get("doors"):
                st = st.replace(hour=x["doors"][0], minute=x["doors"][1])
            e.start = st.isoformat(timespec="minutes")
        if not e.price:
            e.price = x.get("price") or x.get("ld_price")
        if not e.genres and x.get("ld_genres"):
            e.genres = x["ld_genres"]


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
                r = SESSION.get(v["list_pages_template"].format(n=n), timeout=TIMEOUT)
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
            return item.select_one(sel) if sel else None
        t = pick("title")
        title = clean(t.get_text()) if t else None
        # link: uit attribuut (bijv. data-target), uit selector, of de eerste <a>
        url = None
        if v.get("link_attr"):
            holder = item if item.has_attr(v["link_attr"]) else item.find(attrs={v["link_attr"]: True})
            url = urljoin(base, holder[v["link_attr"]]) if holder else None
        if not url:
            a = pick("link") or item.find("a", href=True) or (item if item.name == "a" else None)
            url = urljoin(base, a["href"]) if a and a.has_attr("href") else base
        d = pick("date")
        dt = None
        if d is not None:
            if d.has_attr("datetime") and not v.get("date_text_only"):
                dt = parse_dt(d.get("datetime"))
            dt = dt or parse_dt(clean(d.get_text()))
            if dt is None:
                dt = extract_from_text(d.get_text(" "))[0]
        if not dt and v.get("date_from_url"):
            dt = date_from_url(url)
        if not dt:
            dt = extract_from_text(item.get_text(" "))[0]
        if not (title and dt):
            continue
        # tijd en prijs uit de tekst van het item (bijv. "Open 17:30 / Aanvang 18:00 / € 8,50")
        _, tstart, tdoors, tprice = extract_from_text(item.get_text(" "))
        if (dt.hour, dt.minute) == (0, 0) and (tstart or tdoors):
            hh, mm = tstart or tdoors
            dt = dt.replace(hour=hh, minute=mm)
        genres = []
        if v.get("genre_attr"):
            holder = item if item.has_attr(v["genre_attr"]) else item.find(attrs={v["genre_attr"]: True})
            if holder:
                genres = [x.strip() for x in re.split(r"[,/|·•]", str(holder[v["genre_attr"]])) if x.strip()]
        for g in item.select(v["genre"]) if v.get("genre") else []:
            genres += [x.strip() for x in re.split(r"[,/|·•]", clean(g.get_text()) or "") if x.strip()]
        sub = pick("subtitle")
        status = None
        if item.find(class_=re.compile(r"sold-?out|uitverkocht", re.I)) or re.search(r"\buitverkocht\b", item.get_text(" "), re.I):
            status = "uitverkocht"
        if item.find(class_=re.compile(r"cancel|afgelast", re.I)) or re.search(r"\bafgelast\b|\bgeannuleerd\b", item.get_text(" "), re.I):
            status = "afgelast"
        out.append(Event(venue=v["name"], city=v["city"], title=title, start=dt.isoformat(timespec="minutes"), url=url,
                         subtitle=clean(sub.get_text()) if sub else None, genres=list(dict.fromkeys(g for g in genres if g)),
                         price=tprice, status=status, source="html"))
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
    if t not in ("tribe", "sitemap_detail"):
        html = get(base, delay=float(v.get("crawl_delay", 0))).text

    order = {
        "auto": ["jsonld", "microdata", "embedded", "html_preset", "tribe", "wp_event", "jsonld_detail"],
        "microdata": ["microdata"],
        "jsonld": ["jsonld"], "embedded": ["embedded"], "nextdata": ["embedded"],
        "tribe": ["tribe"], "wp_event": ["wp_event"], "jsonld_detail": ["jsonld_detail"], "html": ["html"],
        "sitemap_detail": ["sitemap_detail"],
    }.get(t, [t])
    notes = []
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
            elif strat == "jsonld_detail":
                evs = strat_jsonld_detail(v, html, base, cache)
            elif strat == "html":
                evs = strat_html(v, html, base)
            elif strat == "html_preset":
                evs, preset_name = strat_html_preset(v, html, base)
                if preset_name:
                    strat = f"html:{preset_name}"
            elif strat == "sitemap_detail":
                evs = strat_sitemap_detail(v, base, cache)
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
        if len(evs) > len(best[0]):
            best = (evs, strat)
        if len(evs) >= int(v.get("good_enough", 20)):
            break
        notes.append(f"{strat}: {len(evs)} events")
    evs, strat = best
    if len(evs) >= int(v.get("min_events", 3)):
        if v.get("enrich", True) and strat not in ("jsonld_detail", "sitemap_detail"):
            try:
                enrich_from_detail(v, evs, cache)
            except Exception as ex:  # noqa: BLE001
                notes.append(f"enrich: {type(ex).__name__}")
        return evs, strat, "; ".join(notes)
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
    def run_one(v):
        t0 = time.time()
        try:
            evs, strat, note = fetch_venue(v, cache)
        except Exception as ex:  # noqa: BLE001
            evs, strat, note = [], "error", f"{type(ex).__name__}: {str(ex)[:200]}"
            traceback.print_exc()
        evs = dedupe(evs)
        log(f"== {v['name']} ({v['city']}): {len(evs)} events via {strat} ({time.time()-t0:.0f}s) {note}")
        return v, evs, strat, note

    todo = [v for v in venues if not only or v["name"] in only]
    # podia parallel (elk podium zelf netjes sequentieel met zijn eigen crawl_delay)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "8"))) as pool:
        results = list(pool.map(run_one, todo))
    for v, evs, strat, note in results:
        report["venues"].append({"name": v["name"], "city": v["city"], "url": v["url"], "strategy": strat,
                                 "events": len(evs), "note": note, "ok": len(evs) > 0})
        all_events.extend(evs)

    # 'nieuw' bepalen; een podium dat voor het eerst meedoet levert geen 'nieuwe' events op
    known_venues = {k.split("|", 1)[0] for k in seen}
    backdate = (TODAY - timedelta(days=30)).isoformat()
    for e in all_events:
        key = f"{e.venue}|{e.url}"
        if key not in seen:
            seen[key] = TODAY.isoformat() if (e.venue in known_venues or not seen) else backdate
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

    # --- verrijking: artiesten, genres, type, prijs ---
    vmeta = {v["name"]: v for v in venues}
    unknown_genres: dict[str, int] = {}
    adb = artistdb.load()
    seen_ev_path = STATE / "artists_seen.json"
    seen_ev = set(json.loads(seen_ev_path.read_text())) if seen_ev_path.exists() else set()
    for e in all_events:
        e.section = vmeta.get(e.venue, {}).get("section", "poppodium")
        e.artists = extract_artists(e.title, e.subtitle)
        if e.lineup:
            known = {artist_key(a) for a in e.artists}
            e.artists += [a for a in e.lineup if artist_key(a) not in known and len(artist_key(a)) > 1][: 15 - len(e.artists)]
            if not e.subtitle and len(e.lineup) > 1:
                e.subtitle = "met " + ", ".join(e.lineup[:8]) + (" e.a." if len(e.lineup) > 8 else "")
        e.genre_norm, unk = normalize_genres(e.genres, e.title, e.subtitle or "")
        for u in unk:
            unknown_genres[u] = unknown_genres.get(u, 0) + 1
        e.kind = classify_kind(e.title, e.subtitle, e.genres, e.genre_norm, e.start)
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
        if e.price and (len(e.start) > 10 and e.start[11:16] != "00:00"):
            continue
        if not e.price and gp:
            e.price, e.price_est = gp, True
            e.price_num = price_number(gp)
            e.free = e.price_num == 0.0
            est_p += 1
        if gt and not (len(e.start) > 10 and e.start[11:16] != "00:00"):
            e.start, e.time_est = f"{e.start[:10]}T{gt}", True
            est_t += 1
    seriesdb.save(sdb, sseen)
    log(f"Reeksengeheugen: {len(sdb)} reeksen; {est_p} prijzen, {est_t} tijden en {est_k} typen overgenomen uit eerdere edities")
    report["series"] = len(sdb)
    report["estimated"] = {"price": est_p, "time": est_t, "kind": est_k}
    report["unknown_genres"] = dict(sorted(unknown_genres.items(), key=lambda x: -x[1])[:150])
    report["artists"] = len(adb)
    report["genre_groups"] = {k: v["label"] for k, v in _taxonomy()[0].items()}
    for rv in report["venues"]:
        meta = vmeta.get(rv["name"], {})
        rv["section"] = meta.get("section", "poppodium")
        rv["capacity"] = meta.get("capacity")
    report["kinds"] = {k: sum(1 for e in all_events if e.kind == k) for k in ("concert", "club", "festival", "other")}

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

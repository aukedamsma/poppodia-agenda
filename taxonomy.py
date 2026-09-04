"""Genre-normalisatie, artiestherkenning en eventtype.

- normalize_genres(raw_tags, title, subtitle) -> (genre_norm: list[str], unknown: list[str])
- extract_artists(title, subtitle) -> list[str]   (eerste = headliner)
- classify_kind(title, subtitle, raw_tags) -> "concert" | "club" | "festival" | "other"
- price_number("€ 12,50") -> 12.5
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).parent


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&amp;", "&")
    s = re.sub(r"[\s\-_/·•|]+", " ", s)
    return s.strip()


@lru_cache(maxsize=1)
def _taxonomy():
    t = yaml.safe_load((ROOT / "genres.yaml").read_text(encoding="utf-8"))
    rules = []
    for r in t["rules"]:
        for m in r["match"]:
            rules.append((_fold(m), r["group"]))
    return t["groups"], rules


def group_label(group: str) -> str:
    groups, _ = _taxonomy()
    return groups.get(group, {}).get("label", group)


def normalize_tag(tag: str) -> str | None:
    """Ruwe tag -> groep, of None als onbekend."""
    f = _fold(tag)
    if not f:
        return None
    _, rules = _taxonomy()
    # exacte match eerst
    for m, g in rules:
        if f == m:
            return g
    # daarna: de tag bevat de term als los woord (langste termen eerst zijn al door volgorde geregeld)
    for m, g in rules:
        if re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", f):
            return g
    # samengestelde tags: "postpunk revival" -> probeer per woord
    words = f.split()
    if len(words) > 1:
        for w in words:
            for m, g in rules:
                if w == m:
                    return g
    return None


def normalize_genres(raw: list[str], title: str = "", subtitle: str = "") -> tuple[list[str], list[str]]:
    out: list[str] = []
    unknown: list[str] = []
    for tag in raw or []:
        for part in re.split(r"[,/|·•;]", str(tag)):
            part = part.strip()
            if not part or len(part) > 40:
                continue
            g = normalize_tag(part)
            if g and g not in out and g != "overig":
                out.append(g)
            elif g is None and part not in unknown:
                unknown.append(part)
    if not out:
        # laatste redmiddel: woorden in titel/ondertitel die duidelijk een genre of type aanduiden
        txt = _fold(f"{title} {subtitle}")
        for m, g in _taxonomy()[1]:
            if g in ("kids", "talk", "party", "dance", "metal", "punk", "hiphop", "jazz", "klassiek") and len(m) >= 4 \
                    and re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", txt):
                out.append(g)
                break
    return out, unknown


KIND_FESTIVAL = re.compile(r"\bfestival\b|\bfest\b|weekender|\bdagen\b", re.I)
KIND_CLUB = re.compile(r"\bclub(nacht|night|avond)?\b|\bnight\b|\bnacht\b|\bparty\b|\bfeest\b|\bdj[- ]?set\b|\bdj'?s\b|\brave\b|\bdisco\b|\bdansen\b|all night", re.I)
KIND_OTHER = re.compile(r"\bquiz\b|\bbingo\b|\bkaraoke\b|\blezing\b|\btalk\b|\bcomedy\b|\bcabaret\b|\bfilm\b|\bexpo\b|\bmarkt\b|\bworkshop\b|\bopen mic\b|\bjam\b|\bpodcast\b|\bbenefiet\b", re.I)


def classify_kind(title: str, subtitle: str | None, raw_tags: list[str], genre_norm: list[str]) -> str:
    txt = f"{title} {subtitle or ''} {' '.join(raw_tags or [])}"
    if KIND_FESTIVAL.search(txt):
        return "festival"
    if "talk" in genre_norm or "kids" in genre_norm or KIND_OTHER.search(txt):
        return "other"
    if "party" in genre_norm or KIND_CLUB.search(txt) or any(t.lower() in ("clubnacht", "club", "dance", "nacht") for t in raw_tags or []):
        return "club"
    return "concert"


# ----------------------------------------------------------------------------
# artiesten uit titel/ondertitel
# ----------------------------------------------------------------------------

NOISE_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")          # (USA), [uitverkocht], (18+)
NOISE_SUFFIX = re.compile(r"\s*[-–—|:]\s*(extra show|extra concert|uitverkocht|sold out|afgelast|verplaatst|nieuwe datum|2e show|tweede show|album ?release( ?show| ?party)?|release ?show|release ?party|ep ?release|reunion|reünie|tour \d{4}|\d{4} tour|live in concert|in concert|live|matinee|late show|early show|nl exclusive)\b.*$", re.I)
NOISE_PREFIX = re.compile(r"^(?:[a-z0-9 .&'’\-]{2,40}?\s+(?:presents?|presenteert|pres\.|x)\s*:?\s+|(?:concert|live|tip|premiere|première|exclusief|special|extra)\s*:\s*)", re.I)
SERIES_PREFIX = re.compile(r"^([^:]{2,40}):\s+(.+)$")
SPLIT_ACTS = re.compile(r"\s+\+\s+|\s*,\s+|\s+/\s+|\s+\|\s+|\s+w/\s+|\s+with\s+|\s+met\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|\s+b2b\s+|\s+vs\.?\s+", re.I)
SUPPORT_WORDS = re.compile(r"^(support|voorprogramma|special guest|guests?|tba|tbc|dj|dj's|djs|live|meer|more|e\.a\.|en meer|en anderen|and more|\+ more|friends)$", re.I)
GENERIC_TITLE = re.compile(r"\b(festival|fest|clubnacht|club night|night|nacht|party|feest|quiz|bingo|karaoke|jam|open mic|sessie|session|markt|comedy|cabaret|lezing|talk|film|expo|dansen|disco|bandavond|showcase|releaseparty|kelderbar|cafe|café)\b", re.I)


def _clean_name(n: str) -> str | None:
    n = NOISE_PAREN.sub("", n)
    n = re.sub(r"\s+", " ", n).strip(" -–—:|,.!")
    n = re.sub(r"^(the\s+)?(band|dj)\s*$", "", n, flags=re.I)
    if not n or len(n) < 2 or len(n) > 60 or SUPPORT_WORDS.match(n) or n.isdigit():
        return None
    return n


def extract_artists(title: str, subtitle: str | None = None) -> list[str]:
    """Best-effort: artiestnamen uit titel (+ ondertitel). Eerste = headliner. Lege lijst bij duidelijk niet-artiest-events."""
    t = (title or "").strip()
    t = NOISE_SUFFIX.sub("", t)
    t = NOISE_PREFIX.sub("", t)
    m = SERIES_PREFIX.match(t)
    if m:
        # "Cheeky Monday: MURDOCK!" -> MURDOCK ; "Merol: Tour 2026" -> Merol (rechts is geen naam)
        left, right = m.group(1).strip(), m.group(2).strip()
        t = right if not GENERIC_TITLE.search(right) and len(right.split()) <= 6 else left
    names: list[str] = []
    if not GENERIC_TITLE.search(t):
        for part in SPLIT_ACTS.split(t):
            c = _clean_name(part)
            if c and c.lower() not in {x.lower() for x in names}:
                names.append(c)
    # ondertitel met support acts: "+ Helen Jewett + Eolith", "met U.K. Subs, GBH", "support: X"
    if subtitle:
        s = subtitle.strip()
        if re.match(r"^\s*(\+|met |with |support:?|voorprogramma:?|w/)", s, re.I) or (" + " in s and len(s) < 80):
            if not re.match(r"^\s*(\+|met |with |support:?|voorprogramma:?|w/)", s, re.I):
                s = s[s.index(" + "):]  # "Beschrijvende zin | + Support" -> alleen het deel met support acts
            s = re.sub(r"^\s*(\+|support:?|voorprogramma:?|met|with|w/)\s*", "", s, flags=re.I)
            for part in SPLIT_ACTS.split(s):
                c = _clean_name(part)
                if c and c.lower() not in {x.lower() for x in names} and not GENERIC_TITLE.search(c):
                    names.append(c)
    return names[:6]


def artist_key(name: str) -> str:
    k = _fold(name)
    k = re.sub(r"^(the|de|het|los|las|les|die) ", "", k)
    k = re.sub(r"[^a-z0-9 ]", "", k)
    return re.sub(r"\s+", " ", k).strip()


def price_number(p: str | None) -> float | None:
    if not p:
        return None
    if "gratis" in p.lower() or "free" in p.lower():
        return 0.0
    m = re.search(r"(\d{1,3})(?:[.,](\d{1,2}))?", p)
    if not m:
        return None
    return float(m.group(1)) + (float("0." + m.group(2)) if m.group(2) else 0.0)

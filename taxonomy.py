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


# genre-woorden die in een beschrijving veilig genoeg zijn om als tag te gebruiken (geen 'pop', 'rock', 'club', 'house'…)
_HINT_SKIP = {"pop", "rock", "club", "dance", "house", "live", "show", "muziek", "music", "concert", "wave", "bass", "roots", "world",
              "global", "urban", "swing", "acid", "minimal", "breaks", "trap", "drill", "loud", "heavy", "psych", "prog", "garage",
              "glam", "surf", "western", "country", "singer", "songwriter", "folk", "soul", "funk", "jazz", "blues", "disco", "koor",
              "ensemble", "orkest", "opera", "kids", "familie", "family", "college", "talk", "feest", "party", "hits", "classics",
              "legends", "tribute", "covers", "retro", "fout", "foute", "nederlands", "hollandse", "brazil", "mali", "afro", "latin"}


def genre_hints(text: str, limit: int = 3) -> list[str]:
    """Genre-woorden uit een beschrijving ('Garagerock, postpunk, punk, sleaze, fuzz, wave' -> ['garagerock', 'postpunk', 'punk']).
    Alleen specifieke termen uit genres.yaml; algemene woorden (pop, rock, club…) worden overgeslagen."""
    f = _fold(text or "")
    if not f:
        return []
    out: list[str] = []
    for m, g in _taxonomy()[1]:
        if g in ("overig", "party", "talk", "kids") or len(m) < 4 or m in _HINT_SKIP:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", f) and m not in out:
            out.append(m)
            if len(out) >= limit:
                break
    return out


KIND_ORDER = ("festival", "talk", "other", "club")
KIND_LABELS = {"concert": "Concert", "club": "Clubnacht / feest", "festival": "Festival", "talk": "Talk / lezing", "other": "Overig (quiz, film, kids)"}


@lru_cache(maxsize=1)
def _kind_rules():
    y = yaml.safe_load((ROOT / "genres.yaml").read_text(encoding="utf-8"))
    out = {}
    for kind, spec in y.get("kinds", {}).items():
        def rx(terms):
            terms = [_fold(x) for x in (terms or []) if _fold(x)]
            return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(x) for x in sorted(terms, key=len, reverse=True)) + r")(?![a-z0-9])") if terms else None
        extra = [re.compile(x, re.I) for x in (spec.get("title_regex") or [])]
        out[kind] = (rx(spec.get("title")), {_fold(x) for x in spec.get("tags", [])}, rx(spec.get("strong")), rx(spec.get("weak")), extra)
    kt = y.get("kind_time", {})
    out["_time"] = (kt.get("club_from", "23:00"), kt.get("weak_from", "21:30"))
    return out


def classify_kind(title: str, subtitle: str | None, raw_tags: list[str], genre_norm: list[str], start: str | None = None,
                  learned: dict | None = None) -> str:
    return classify_kind_ex(title, subtitle, raw_tags, genre_norm, start, learned)[0]


def classify_kind_ex(title: str, subtitle: str | None, raw_tags: list[str], genre_norm: list[str], start: str | None = None,
                     learned: dict | None = None) -> tuple[str, bool]:
    """(type, zeker). concert | club | festival | talk | other. Titel en podium-tags wegen; de ondertitel alleen voor
    sterke termen; zwakke feest-termen en een late aanvang tellen alleen samen. Zonder duidelijk bewijs: concert (muziek),
    niet zeker. `learned` = state/kind_learn.json: podiumtags die het systeem zelf aan een type heeft gekoppeld."""
    ft, fs = _fold(title), _fold(subtitle or "")
    time = start[11:16] if start and len(start) > 10 and start[11:16] != "00:00" else None
    tags = {_fold(x) for x in (raw_tags or [])}
    tagwords = set()
    for x in tags:
        tagwords.update(x.split())
    rules = _kind_rules()
    club_from, weak_from = rules["_time"]
    for kind in KIND_ORDER:
        if kind not in rules:
            continue
        title_rx, tag_set, strong_rx, weak_rx, extra = rules[kind]
        if title_rx and title_rx.search(ft):
            return kind, True
        if any(x.search(title or "") for x in extra):
            return kind, True
        if tag_set & (tags | tagwords):
            return kind, True
        if strong_rx and strong_rx.search(fs):
            return kind, True
        if weak_rx and time and time >= weak_from and weak_rx.search(ft):
            return kind, True
    if "talk" in genre_norm:
        return "talk", True
    if "kids" in genre_norm:
        return "other", True
    # geleerde koppeling: een podiumtag die eerder consequent bij één type hoorde (bv. Tivoli "Kennis & Debat" -> talk)
    if learned:
        acc = learned.get("accepted", {})
        for t in tags:
            if t in acc:
                return acc[t]["kind"], True
    if time and time >= club_from:
        return "club", False
    return "concert", False


def learn_kinds(learned: dict, raw_tags: list[str], kind: str) -> None:
    """Stem per podiumtag op het (zeker vastgestelde) type."""
    votes = learned.setdefault("votes", {})
    for t in raw_tags or []:
        f = _fold(t)
        if f and len(f) <= 40:
            v = votes.setdefault(f, {})
            v[kind] = v.get(kind, 0) + 1


def promote_kinds(learned: dict, min_obs: int = 5, min_share: float = 0.85) -> int:
    """Tags die >= min_obs keer zijn gezien en in >= min_share van de gevallen bij één type: vanaf nu bepalend."""
    accepted = learned.setdefault("accepted", {})
    n = 0
    for f, v in learned.get("votes", {}).items():
        total = sum(v.values())
        if total < min_obs or f in accepted:
            continue
        k, c = max(v.items(), key=lambda x: x[1])
        if c / total >= min_share and k != "concert":  # alleen niet-muziek leren; concert is al de standaard
            accepted[f] = {"kind": k, "obs": total}
            n += 1
    return n


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


# ----------------------------------------------------------------------------
# subgenres: vaste lijst (Bandcamp-indeling) + zelflerend
# ----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _subgenres():
    y = yaml.safe_load((ROOT / "genres.yaml").read_text(encoding="utf-8"))
    alias: dict[str, str] = {}
    for key, spec in (y.get("subgenres") or {}).items():
        for a in [key] + list(spec.get("aliases") or []):
            alias[_fold(a)] = key
    noise = {_fold(x) for x in (y.get("subgenre_noise") or [])}
    return y.get("subgenres") or {}, alias, noise


@lru_cache(maxsize=1)
def _group_words() -> set[str]:
    groups, _ = _taxonomy()
    words = set(groups.keys())
    for g in groups.values():
        for part in re.split(r"[/,]", g.get("label", "")):
            words.add(_fold(part))
    words |= {"popmuziek", "rockmuziek", "hip hop", "hip-hop", "hiphop", "r&b", "electronic", "dance", "klassiek", "classical"}
    return words


def subgenre_label(key: str) -> str:
    specs, _, _ = _subgenres()
    return specs.get(key, {}).get("label", key)


def subgenre_group(key: str) -> str | None:
    specs, _, _ = _subgenres()
    return specs.get(key, {}).get("group")


def normalize_subgenres(raw: list[str], genre_norm: list[str], learned: dict | None = None) -> tuple[list[str], list[str]]:
    """Ruwe tags -> (canonieke subgenre-sleutels, onbekende tags). `learned` = state/subgenre_learn.json: tags die het
    systeem zelf aan een hoofdgenre heeft gekoppeld (>= 5 keer gezien, >= 80% bij één groep) tellen ook als subgenre."""
    specs, alias, noise = _subgenres()
    out: list[str] = []
    unknown: list[str] = []
    for tag in raw or []:
        for part in re.split(r"[,/|·•;]", str(tag)):
            f = _fold(part)
            if not f or len(f) > 40 or f in noise or normalize_tag(part) in ("overig",):
                continue
            if f in _group_words():
                continue  # hoofdgenre-woord (pop, rock, jazz…) is geen subgenre
            key = alias.get(f)
            if not key and learned and f in learned.get("accepted", {}):
                key = f  # zelfgeleerd subgenre (sleutel = de gevouwen tag)
            if key:
                if key not in out:
                    out.append(key)
            elif f not in unknown and not re.search(r"\d{2}[:.]\d{2}|^\d+$", f):
                unknown.append(f)
    return out[:6], unknown


def learn_subgenres(learned: dict, unknown: list[str], genre_norm: list[str]) -> None:
    """Tel per onbekende tag bij welke hoofdgenres hij voorkomt; promoveer bij voldoende bewijs."""
    if not genre_norm:
        return
    votes = learned.setdefault("votes", {})
    for f in unknown:
        v = votes.setdefault(f, {})
        for g in genre_norm[:1]:  # alleen het eerste (sterkste) hoofdgenre telt
            v[g] = v.get(g, 0) + 1


def promote_subgenres(learned: dict, min_obs: int = 5, min_share: float = 0.8) -> int:
    accepted = learned.setdefault("accepted", {})
    n = 0
    for f, v in learned.get("votes", {}).items():
        total = sum(v.values())
        if total < min_obs or f in accepted:
            continue
        g, c = max(v.items(), key=lambda x: x[1])
        if c / total >= min_share:
            accepted[f] = {"group": g, "label": f, "obs": total}
            n += 1
    return n


def price_number(p: str | None) -> float | None:
    if not p:
        return None
    if "gratis" in p.lower() or "free" in p.lower():
        return 0.0
    m = re.search(r"(\d{1,3})(?:[.,](\d{1,2}))?", p)
    if not m:
        return None
    return float(m.group(1)) + (float("0." + m.group(2)) if m.group(2) else 0.0)

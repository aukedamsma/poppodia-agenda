"""Artiestenkennisbank (state/artists.json) die met elke run groeit.

Per artiest (sleutel = genormaliseerde naam):
  name         meest voorkomende schrijfwijze
  aliases      andere schrijfwijzen
  votes        {genregroep: aantal keer dat een podium die toekende}
  raw_tags     {ruwe tag: aantal}   (subgenres, waardevol voor smaak)
  venues       {podium: aantal optredens gezien}
  with         {artiestsleutel: aantal keer samen op de affiche}
  seen         aantal events, first_seen, last_seen
  mb           MusicBrainz: {id, name, tags:[...], country, type, checked}
  spotify      {id, name, genres:[...], popularity, followers, checked}   (alleen met SPOTIFY_CLIENT_ID/SECRET)
  genre_norm   afgeleide hoofdgenres (stemmen podia > MusicBrainz/Spotify > gezelschap)

Externe bronnen worden met een budget per run bevraagd (MusicBrainz 1 req/s, verplicht nette User-Agent).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path

import requests

from taxonomy import artist_key, normalize_tag

ROOT = Path(__file__).parent
STATE = ROOT / "state"
PATH = STATE / "artists.json"
UA = "poppodia-agenda/1.0 (https://github.com/aukedamsma/poppodia-agenda)"
TODAY = date.today().isoformat()


# hoofdgenres die hernoemd zijn (sept. 2026: Bandcamp-indeling); oude stemmen in de kennisbank worden bij laden omgezet
GROUP_RENAMES = {"indie": "alternative", "dance": "electronic", "bass": "electronic", "klassiek": "classical",
                 "experimenteel": "experimental", "roots": "country", "talk": "spokenword"}


def migrate_groups(counts: dict) -> dict:
    out: dict = {}
    for g, n in (counts or {}).items():
        g2 = GROUP_RENAMES.get(g, g)
        out[g2] = out.get(g2, 0) + n
    return out


def load() -> dict:
    if PATH.exists():
        try:
            db = json.loads(PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        for a in db.values():
            if any(g in GROUP_RENAMES for g in a.get("votes", {})):
                a["votes"] = migrate_groups(a["votes"])
        return db
    return {}


def save(db: dict) -> None:
    STATE.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(db, ensure_ascii=False, indent=0), encoding="utf-8")


def _entry(db: dict, name: str) -> dict | None:
    key = artist_key(name)
    if len(key) < 2:
        return None
    a = db.setdefault(key, {"name": name, "aliases": [], "votes": {}, "raw_tags": {}, "venues": {}, "with": {}, "seen": 0,
                            "first_seen": TODAY, "last_seen": TODAY})
    if name != a["name"] and name not in a["aliases"]:
        a["aliases"] = (a["aliases"] + [name])[-6:]
    return a


def record_event(db: dict, artists: list[str], venue: str, raw_tags: list[str], genre_norm: list[str], event_key: str, seen_keys: set) -> None:
    """Werk de kennisbank bij met één event. `seen_keys` voorkomt dubbel tellen over runs heen."""
    if not artists or event_key in seen_keys:
        return
    seen_keys.add(event_key)
    keys = []
    for i, name in enumerate(artists):
        a = _entry(db, name)
        if not a:
            continue
        keys.append(artist_key(name))
        a["seen"] += 1
        a["last_seen"] = TODAY
        a["venues"][venue] = a["venues"].get(venue, 0) + 1
        weight = 2 if i == 0 else 1  # headliner telt dubbel voor genre-stemmen
        for g in genre_norm:
            a["votes"][g] = a["votes"].get(g, 0) + weight
        for t in raw_tags:
            t = t.strip()
            if t and len(t) <= 40:
                a["raw_tags"][t] = a["raw_tags"].get(t, 0) + 1
    for k in keys:
        for other in keys:
            if other != k:
                db[k]["with"][other] = db[k]["with"].get(other, 0) + 1


# ----------------------------------------------------------------------------
# externe kennis
# ----------------------------------------------------------------------------

def musicbrainz_lookup(db: dict, budget: int = 150, log=print) -> int:
    """Zoekt artiesten zonder MusicBrainz-check op; max `budget` per run, 1 verzoek per seconde."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    todo = [k for k, a in db.items() if "mb" not in a and a["seen"] >= 1]
    # eerst de artiesten die het vaakst voorkomen
    todo.sort(key=lambda k: -db[k]["seen"])
    done = 0
    for k in todo[:budget]:
        a = db[k]
        name = a["name"]
        try:
            r = s.get("https://musicbrainz.org/ws/2/artist/", params={"query": f'artist:"{name}"', "fmt": "json", "limit": 3}, timeout=20)
            time.sleep(1.1)
            if r.status_code != 200:
                a["mb"] = {"checked": TODAY, "error": r.status_code}
                continue
            hits = r.json().get("artists", [])
            best = next((h for h in hits if int(h.get("score", 0)) >= 90 and artist_key(h.get("name", "")) == k), None)
            if not best:
                best = next((h for h in hits if int(h.get("score", 0)) >= 95), None)
            if best:
                tags = sorted(best.get("tags", []), key=lambda t: -t.get("count", 0))
                a["mb"] = {"checked": TODAY, "id": best["id"], "name": best.get("name"), "type": best.get("type"),
                           "country": best.get("country"), "tags": [t["name"] for t in tags[:8]],
                           "disambiguation": best.get("disambiguation")}
            else:
                a["mb"] = {"checked": TODAY, "id": None}
            done += 1
        except requests.RequestException as ex:
            a["mb"] = {"checked": TODAY, "error": str(ex)[:80]}
    if done:
        log(f"MusicBrainz: {done} artiesten opgezocht ({len(todo) - done} nog te doen)")
    return done


def spotify_lookup(db: dict, budget: int = 300, log=print) -> int:
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        return 0
    s = requests.Session()
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    tok = s.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                 headers={"Authorization": f"Basic {auth}"}, timeout=20)
    if tok.status_code != 200:
        log(f"Spotify: token mislukt ({tok.status_code})")
        return 0
    s.headers["Authorization"] = f"Bearer {tok.json()['access_token']}"
    todo = [k for k, a in db.items() if "spotify" not in a]
    todo.sort(key=lambda k: -db[k]["seen"])
    done = 0
    for k in todo[:budget]:
        a = db[k]
        try:
            r = s.get("https://api.spotify.com/v1/search", params={"q": f'artist:"{a["name"]}"', "type": "artist", "limit": 3, "market": "NL"}, timeout=20)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            if r.status_code != 200:
                a["spotify"] = {"checked": TODAY, "error": r.status_code}
                continue
            items = r.json().get("artists", {}).get("items", [])
            best = next((it for it in items if artist_key(it["name"]) == k), None)
            if best:
                a["spotify"] = {"checked": TODAY, "id": best["id"], "name": best["name"], "genres": best.get("genres", []),
                                "popularity": best.get("popularity"), "followers": (best.get("followers") or {}).get("total")}
            else:
                a["spotify"] = {"checked": TODAY, "id": None}
            done += 1
            time.sleep(0.15)
        except requests.RequestException as ex:
            a["spotify"] = {"checked": TODAY, "error": str(ex)[:80]}
    if done:
        log(f"Spotify: {done} artiesten opgezocht")
    return done


# ----------------------------------------------------------------------------
# afleiding
# ----------------------------------------------------------------------------

def derive_genres(db: dict) -> None:
    """Zet per artiest genre_norm: podiumstemmen wegen zwaarst, dan MusicBrainz/Spotify-tags, dan gezelschap."""
    # eerste ronde: eigen bewijs
    for k, a in db.items():
        score: Counter = Counter()
        for g, n in a.get("votes", {}).items():
            score[g] += 3 * n
        for src, w in (("spotify", 2), ("mb", 1.5)):
            for t in (a.get(src) or {}).get("genres", []) or (a.get(src) or {}).get("tags", []):
                g = normalize_tag(t)
                if g and g != "overig":
                    score[g] += w
        a["_score"] = score
    # tweede ronde: gezelschap (alleen als eigen bewijs dun is)
    for k, a in db.items():
        if sum(a["_score"].values()) < 3:
            for other, n in a.get("with", {}).items():
                o = db.get(other)
                if o:
                    for g, sc in o["_score"].items():
                        a["_score"][g] += 0.5 * min(n, 3) * (sc / max(1, sum(o["_score"].values())))
    for k, a in db.items():
        sc = a.pop("_score")
        total = sum(sc.values())
        a["genre_norm"] = [g for g, v in sc.most_common(3) if total and v / total >= 0.2] if total else []
        a["confidence"] = round(min(1.0, total / 12), 2) if total else 0.0


def genres_for(db: dict, artists: list[str]) -> tuple[list[str], list[str]]:
    """(hoofdgenres, subgenre-tags) op basis van de kennisbank, voor events zonder eigen genre."""
    groups: Counter = Counter()
    subs: Counter = Counter()
    for i, name in enumerate(artists):
        a = db.get(artist_key(name))
        if not a:
            continue
        w = 2 if i == 0 else 1
        for g in a.get("genre_norm", []):
            groups[g] += w * (a.get("confidence") or 0.3)
        for t, n in list(a.get("raw_tags", {}).items())[:10]:
            subs[t] += n
        for t in (a.get("spotify") or {}).get("genres", [])[:4]:
            subs[t] += 1
    return [g for g, _ in groups.most_common(2)], [t for t, _ in subs.most_common(4)]

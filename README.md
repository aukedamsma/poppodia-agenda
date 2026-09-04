# Podiumagenda

Eén dagelijks bijgewerkte agenda met alle concerten van de Nederlandse poppodia die ik volg,
zodat ik niet elke site apart hoef af te zoeken. Draait gratis op GitHub Actions, de pagina staat op GitHub Pages.

- **Agenda-pagina**: `docs/index.html` (via Pages op `https://aukedamsma.github.io/poppodia-agenda/`)
- **Agenda-app**: abonneer op `docs/agenda.ics`
- **Ruwe data**: `data/events.json`, status per podium in `data/report.json`

## Hoe het werkt

1. `fetch.py` leest `venues.yaml` en haalt per podium de agenda op. Per podium wordt van schoon naar ruw geprobeerd:
   schema.org JSON-LD op de agendapagina → ingebedde JSON (Next.js, Angular) → The Events Calendar API →
   WordPress REST API → JSON-LD op de losse eventpagina's (gecached, 10 dagen) → CSS-selectors uit `venues.yaml`.
2. Alles wordt genormaliseerd naar één lijst (`data/events.json`). Event-URL's die de vorige run nog niet bestonden
   krijgen een `first_seen`-datum; de pagina toont die 7 dagen als **nieuw**.
3. `build.py` bouwt `docs/index.html` en `docs/agenda.ics`.
4. `.github/workflows/daily.yml` draait dit elke ochtend om 07:00 en commit het resultaat.

Een falend podium blokkeert nooit de rest: het staat dan met een ✕ in de status-voettekst van de pagina,
met de reden in `data/report.json`.

## Genres en artiesten

- `genres.yaml` bevat de taxonomie: ~23 hoofdgenres (naar RateYourMusic/Discogs/Bandcamp, toegesneden op de Nederlandse
  poppodia) plus regels die ruwe podiumtags op een hoofdgenre afbeelden. Ruwe tags blijven bewaard als subgenre.
  Onbekende tags staan na elke run in `data/report.json` onder `unknown_genres`; voeg ze toe aan de regels.
- `taxonomy.py` herkent artiesten in titels/ondertitels, bepaalt het eventtype (concert/club/festival/other) en de prijs als getal.
- `artists.py` beheert `state/artists.json`: een kennisbank die elke run groeit (genre-stemmen van podia, medespelers,
  podia, MusicBrainz-tags en — met `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` als GitHub-secrets — Spotify-genres en
  populariteit). Events zonder genre krijgen er een via de kennisbank. Basis voor de smaakscore.

## Een podium toevoegen

Voeg een blok toe aan `venues.yaml`. Meestal is dit genoeg:

```yaml
- name: Vera
  city: Groningen
  url: https://www.vera-groningen.nl/programma/
```

Zonder `type` probeert de fetcher alle strategieën automatisch. Levert de volgende run niets op,
kijk dan in `data/report.json` wat er per strategie gebeurde. Als de site alleen platte HTML heeft,
geef dan CSS-selectors op:

```yaml
- name: Patronaat
  city: Haarlem
  url: https://www.patronaat.nl/programma/
  type: html
  item: ".event-program"          # één blok per event
  title: ".event-program__name"
  date: ".event-program__date"    # tekst of <time datetime>
  link: ".event-program__name a"
  genre: ".event-program__genres"
  subtitle: ".event-program__subtitle"
  date_from_url: true             # datum uit de URL (DD-MM-YYYY) als hij ontbreekt
  crawl_delay: 10                 # seconden tussen requests, als robots.txt daarom vraagt
```

Twee bijzondere types voor lastige sites:

- `sitemap_detail` (Paradiso): eventlinks komen uit de sitemap (`sitemap`, `sitemap_pattern`, `last_files`), de data uit de
  eventpagina's (JSON-LD of de ingebedde React Server Components-payload). Zwaar bij de eerste run, daarna incrementeel via de cache.
- `embedded` leest ook JSON die in HTML-attributen zit (Tolhuistuin: Vue-prop `:all-items`). Met `only_genres: [Muziek, Clubnacht]`
  filter je een gemengd programma (theater, workshops) weg.

Alle opties staan bovenin `venues.yaml`. Tijdelijk uitzetten: `type: disabled`.

## Zelf draaien

```bash
pip install -r requirements.txt
python fetch.py            # alle podia
python fetch.py Hedon EKKO # alleen deze
python build.py
open docs/index.html
python tests/test_offline.py
```

## Netheid

Eén run per dag, één request per pagina, een herkenbare User-Agent en respect voor `crawl_delay`
waar een podium daarom vraagt. Podia die geautomatiseerd ophalen expliciet weren, staan op `disabled`.

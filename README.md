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
- `taxonomy.py` herkent artiesten in titels/ondertitels, bepaalt het eventtype (concert / club / festival / talk / other;
  regels in `genres.yaml` onder `kinds`, aanvangstijd telt mee: vanaf 23:00 is het een feest) en de prijs als getal.
- `series.py` beheert `state/series.json`: terugkerende events per podium ("Cheeky Monday", "Jazz Jam #14", "VroegZat")
  met hun gebruikelijke prijs, aanvangstijd en type. Ontbrekende waarden worden daaruit ingevuld en gemarkeerd (`~ € 7,50`).
- `artists.py` beheert `state/artists.json`: een kennisbank die elke run groeit (genre-stemmen van podia, medespelers,
  podia, MusicBrainz-tags en — met `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` als GitHub-secrets — Spotify-genres en
  populariteit). Events zonder genre krijgen er een via de kennisbank. Basis voor de smaakscore.

## Methodiek: regels die voor álle podia gelden

Elke fout die bij één podium is gevonden, is omgezet in een generieke regel in `fetch.py`, zodat hij bij alle
(ook toekomstige) podia wordt afgevangen:

| Les (waar gevonden) | Regel |
|---|---|
| Aanvang vs. deuren (PAARD, Tivoli) | Een expliciete *aanvang/start* op de eventpagina wint altijd van de tijd in de lijst; *deuren* alleen als er niets beters is (`apply_extra`). |
| Lokale tijd met foute tijdzone (Doornroosje, +1u) | Steekproef per podium: wijkt de aanvang op de eventpagina structureel 1–2 uur af, dan worden die events gecorrigeerd en krijgt het podium een `!` met "zet `time_is_local`" in het rapport (`audit_venue`). |
| Datum van vandaag uit de sitekop (ECI, 74 films op één dag) | Een datum uit losse tekst die precies vandaag is zonder tijd wordt genegeerd; blijft er toch een cluster (>25% van de events op één dag zonder tijd), dan worden die events verwijderd en gemeld. |
| Categoriepagina's als event (Cpunt "Film", "Exposities") | `/agenda/<categorie>` en soortgelijke paden zijn nooit een event (`NON_EVENT_PATH`). |
| Sitemap op id, niet op datum (Paradiso) | Sitemap-URL's worden op `lastmod` gefilterd (`sitemap_recent_days`): aankomende events worden bijgewerkt, oude niet meer. |
| Gemengd programma (ECI, Cacaofabriek, Tivoli) | Eventtype uit titel + podiumtags + aanvangstijd; film/theater/talk/kids verdwijnen uit "Concert". Waar het podium een aparte muziekpagina heeft, gebruiken we die. |
| Line-up en genre alleen in de beschrijving (Tivoli, pop-agenda) | JSON-LD `performer`, een `lineup`-selector of de beschrijving leveren artiesten; specifieke genrewoorden in de beschrijving ("postpunk", "garagerock") worden tags als het podium er geen geeft. |
| Het podium heeft zelf genre-/typefilters op de agenda (Tivoli `?sf_genre=pop`, `kennis-debat`, `familie`…) | `category_pages` in `venues.yaml`: per filter worden de overzichtspagina's opgehaald (gepagineerd tot 404) en de events getagd met de eigen indeling van de programmeurs. Die tags gaan vóór tekst-hints en sturen hoofdgenre, subgenre én type Muziek/Overig. Eén keer per dag gecached. Bij nieuwe podia eerst kijken of zo'n filter bestaat. |
| Bandnamen die op feesten lijken ("Jungle By Night", "Two Door Cinema Club") | Zwakke feestwoorden (night, club, disco) tellen alleen samen met een late aanvang. |
| Terugkerende events zonder prijs/tijd | Reeksengeheugen vult aan uit eerdere edities, gemarkeerd als schatting. |
| Prijs/tijd alleen in ingebedde JSON (Melkweg `"price":"€ 24,05"`, Vorstin URL-gecodeerde `program_start`/`door_open`) | `price_from_embedded_json` en `times_from_embedded_json` lezen JSON-blobs in de HTML (ook URL-gecodeerd) als de zichtbare tekst niets oplevert; het 'primary' ticket wint. |
| Tijd vóór het label ("19:30 Zaal Open", 013) en tijdschema's zonder 'aanvang' (Nobel "19:00 - Deuren open 19:30 - Band") | Beide woordvolgordes worden herkend; de vroegste tijd is de deurtijd, de eerste tijd erna de start. |
| Verouderde cache na een parserverbetering (013: prijs bleef leeg omdat de pagina al gecached was) | `CACHE_VERSION` in `fetch.py`: verhogen dwingt het opnieuw ophalen van alle aankomende events af; verleden events blijven gecached. |

Twee zelflerende lagen (bewijs verzamelen → bij voldoende consistentie promoveren tot regel; handmatig vast te zetten in `genres.yaml`):

- **Subgenres** (`state/subgenre_learn.json`): ruwe tags worden afgebeeld op een vaste lijst van ~105 subgenres naar de indeling van
  Bandcamp Discover (spellingvarianten via `aliases`; niet-genres via `subgenre_noise`). Een onbekende tag die ≥ 5 keer en in ≥ 80% van de
  gevallen bij één hoofdgenre voorkomt, wordt automatisch subgenre van dat hoofdgenre.
- **Type** (`state/kind_learn.json`): elke keer dat een regel het type zeker vaststelt, stemmen de podiumtags van dat event mee. Een tag die
  ≥ 5 keer en in ≥ 85% van de gevallen bij één niet-muziektype hoort (Tivoli "Kennis & Debat" → talk) bepaalt daarna zelf het type van
  events zonder andere aanwijzing. Zonder aanwijzing blijft het type muziek (concert).

Elk podium krijgt in `data/report.json` een `audit` met dekking (aandeel events met tijd / prijs / genre) en
waarschuwingen; de voettekst van de site toont dezelfde cijfers. Zo is in één blik te zien waar de data dun is.

## Archief

`data/archive.json` bewaart elk event dat ooit is opgehaald (sleutel podium|url) met titel, datum, prijs, genres, type,
artiesten, status en first/last_seen. De site toont alleen wat nog komt; het archief groeit elke run en is bedoeld als
onderzoeksdata (programmering en prijzen per podium door de tijd).

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

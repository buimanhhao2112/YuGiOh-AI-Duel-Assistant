# Yu-Gi-Oh! AI Combo Lab

Prototype pipeline for Master Duel card data, heuristic PSCT parsing, selected Snake-Eye and Vanquish Soul x K9 operators, and MCTS combo search. It is a research simulator, not a complete rules judge or a Master Duel control bot. Validate candidate lines with `ocgcore`/YGOPro before treating them as tournament-legal.

## Components

- `mdm_scraper.py` fetches the public MDM card API, deduplicates card IDs across pages, diff-syncs SQLite and exports normalized JSON.
- `mdm_banlist.py` reads MDM's banlist change feed and retains the latest status for each card.
- `ygo_ai/cards.py` defines the normalized card schema.
- `ygo_ai/psct.py` separates condition, cost, target and resolution text, flags HOPT/OPT, quick effects and continuous effects, then extracts a small set of symbolic actions. Complex clauses remain marked for review.
- `ygo_ai/state.py` defines duel zones, positions, turn/phase state, overlay materials, priority and LIFO chain resolution.
- `ygo_ai/state.py` also checks Spell Speed when chaining and provides SEGOC ordering for simultaneous trigger effects.
- `mcts_search.py` runs MCTS over selected Snake-Eye/Diabellstar and VS/K9 operators, including attribute reveals, Level 5 K9 summons, Rank 4/5 Xyz, K9 Xyz material transfer, and Link Summons. It does not infer executable rules for every card from PSCT.
- `database_manager.py` stores order-independent hand keys, route JSON, end board, score and Gold-route status in an indexed WAL-mode SQLite cache.
- `scripts/build_vs_k9_deck.py` builds the supplied 40/15 deck list from local MDM records and maps the display name K9-00 Lupis to MDM's canonical K9-ØØ Lupis record.
- `examples/vs_k9_deck.json` contains the supplied Vanquish Soul x K9 list and the selected card operators.
- `generate_combo.py` accepts a specified or random hand and writes `output/vs_k9_combo_result.md` by default. The default deck is Vanquish Soul x K9.
- `web_app.py` serves a local FastAPI/Jinja dashboard with an in-memory card catalog, client-side Fuse search, deck JSON editing, five-card AI route generation (`/api/generate-route`, cache then MCTS), and a separate manual route benchmark (`/api/benchmark-route`).
- `vision_engine.py` provides opt-in screen-pixel capture, local grayscale card-template matching, and combat-log OCR. It never reads game memory or sends inputs.
- `overlay_assistant.py` provides a frameless PyQt6 HUD. Process detection, vision capture and MCTS run on separate QThreads; the HUD appears only while Master Duel is foreground and hides on Alt-Tab or game exit. Route-step lightning buttons restart a 5,000-iteration search from the state after that step.
- `run_app.py` and `run_app.bat` create/use `.venv`, install missing application dependencies and launch the web dashboard and HUD together.
- `examples/snake_eye_deck.json` contains the 40-card sample and Extra Deck target definitions, including Apollousa marked Forbidden.
- `architecture.md` describes the data, rules-engine and MCTS boundaries.

## Run the data sync

Python 3.11+ is required. SQLite uses the standard library. PostgreSQL is optional and needs `psycopg` from `requirements-postgres.txt`.

```powershell
python mdm_banlist.py --output data/banlist.json
python mdm_scraper.py sync --banlist-json data/banlist.json
```

The default output is `data/cards.db` and `data/cards.json`. Useful options:

```powershell
python mdm_scraper.py sync --limit 200 --force
python mdm_scraper.py sync --db postgresql://USER:PASSWORD@HOST:5432/DATABASE --banlist-json data/banlist.json
```

`--limit` caps the total cards fetched; `--force` rewrites matching rows. If PostgreSQL cannot be reached or a PostgreSQL sync fails, the scraper falls back to `data/cards.db`. Card API rows without an authoritative ban status retain `ban_status_verified: false`; the separate snapshot supplies current list entries to the deck filter. Cards absent from both the snapshot and reviewed deck metadata are treated as Unlimited by the prototype.

The scraper reads all card pages on each run because MDM does not expose a verified updated-since filter. It only inserts or updates changed database rows. It does not delete old rows if the API returns an incomplete page. HTTP 429/5xx responses are retried with a limit; HTTP 403 stops the run rather than attempting anti-bot bypasses.

## Parse PSCT and search a combo

```powershell
python -m ygo_ai.psct examples/psct_examples.json
python generate_combo.py --hand-size 5 --iterations 15000 --seed 24
python generate_combo.py --deck examples/snake_eye_deck.json --hand-size 5 --iterations 12000 --rollout-depth 30 --seed 7 --output output/combo_result.md
```

If `data/banlist.json` exists, the deck loader applies its copy limits before drawing or validating a supplied hand. The report and terminal show the sampled hand, action sequence, final field, and score. Search settings include `--rollout-depth`, `--output`, `--deck`, and `--banlist`. Pass `--opponent-hand-effect-activated` to enable K9 Quick Effects that require an opponent monster effect in hand/GY this turn.

The MCTS evaluator scores modeled negates, attribute-enabled interruptions, FIRE/DARK/EARTH coverage in hand, hand/field count, total ATK and deck resources used. Stake your Soul! bodies are marked temporary because their End Phase return is not simulated. Opponent responses, full chain timing, battle, and many individual card effects are outside this model; unsupported text is not silently treated as an action.

## Run the local web dashboard

```powershell
python -m pip install -r requirements-web.txt
python web_app.py
```

Open `http://127.0.0.1:8000`. The dashboard loads the normalized card catalog once and keeps search in the browser. Fuse.js 7.5 and Tailwind Play CDN are optional browser enhancements; local styles and a text-search fallback keep the dashboard usable if the CDN is unreachable. Saved decks go to `examples/custom_*.json`; benchmarked routes are written to `data/combo_cache.db`.

Route Lab has two distinct actions: **AI Route Generator** sends a five-card hand and mode to `/api/generate-route`, which returns a compatible cached Gold/MCTS route or runs MCTS and caches its result. **Human Route Benchmark** accepts manual `route_steps` at `/api/benchmark-route` and stores the route as Gold when it ties or beats the MCTS baseline.

To enable screenshot capture and template matching, install `requirements-vision.txt` and place card images in `data/card_thumbnails/`, named after the card. OCR also needs the Tesseract executable installed; set `YGO_TESSERACT_CMD` if it is not on `PATH`. Capture starts only after you press a scan control in the dashboard and `MasterDuel.exe` is foreground.

## Run the full Web + HUD application

Double-click `run_app.bat`, or run the following from PowerShell:

```powershell
.\.venv\Scripts\python.exe run_app.py
```

The launcher installs missing packages from `requirements-app.txt`, starts the web dashboard at `http://127.0.0.1:8000`, opens it in your browser, and starts the HUD. The HUD remains hidden until `MasterDuel.exe` is running in the foreground. It hides when you Alt-Tab or close the game. MCTS and screen recognition run away from the UI thread. Press `Ctrl+C` in the launcher console to stop both parts.

To verify the install without opening the HUD, run:

```powershell
.\.venv\Scripts\python.exe run_app.py --check
```

Hand-image matching needs local templates in `data/card_thumbnails/`; add one cropped card-face image per card and name it after the card. Tesseract OCR is optional. The app searches `YGO_TESSERACT_CMD`, the standard Windows install paths and `PATH`; if the executable is unavailable, OCR is skipped without stopping the dashboard or HUD. `data/card_thumbnails/README.md` describes the template setup.

## Tests

```powershell
python -m unittest discover -s tests -v
```

These modules are a selected-card simulator and a passive HUD, not a complete Master Duel rules engine or an input automation tool. The chain utility models LIFO, basic spell-speed legality and SEGOC ordering; unsupported rulings still need validation against `ocgcore`.

## Data sources

- [Master Duel Meta cards API](https://www.masterduelmeta.com/api/v1/cards)
- [Master Duel Meta Forbidden/Limited List](https://www.masterduelmeta.com/forbidden-limited-list)
- [MDM Snake-Eye Ash](https://www.masterduelmeta.com/cards/Snake-Eye%20Ash)
- [MDM Snake-Eyes Poplar](https://www.masterduelmeta.com/cards/Snake-Eyes%20Poplar)

# agribot

**An autonomous agricultural monitoring agent for a single vegetable garden, delivered through Telegram.** It runs as a Docker container on a Synology NAS and watches a leafy-greens plot in the humid subtropical microclimate of Wenshan District, Taipei (near National Chengchi University).

agribot pairs a **deterministic science layer** with an **LLM interpreter**: physical quantities — growing degree days, reference evapotranspiration, disease pressure — are computed by pure, auditable functions, and Google Gemini is used only to *interpret* those numbers, converse, and ground its advice in an official agricultural knowledge base — never to invent the numbers itself.

## What it does

**Sensing & data**
- Reads soil temperature / moisture / EC and air temperature / humidity from 阿龜微氣候 (AgriWeather) IoT sensors via the official API, plus the platform's native irrigation/fertilization advice via an isolated Playwright path.
- Pulls 7-day forecasts and live station observations (including rainfall) from Taiwan's Central Weather Administration (CWA).

**Agronomic reasoning (deterministic core)**
- **GDD engine** — multi-crop growing-degree-day accumulation with per-crop base/upper temperatures.
- **Water demand** — ET₀ via FAO-56 Penman–Monteith, converted to crop water demand (ETc) through a growth-stage crop coefficient (Kc).
- **Disease early-warning** — a two-pathway "disease pressure" index (leaf-wetness from humidity/dew duration; rain-splash from actual rainfall) that flags foliar disease risk *before* lesions appear, then deterministically attaches official prevention guidance from the knowledge base.
- **Harvest cadence**, **prediction self-calibration**, and **photo-based growth cross-check** against the GDD model.

**Knowledge base (RAG)**
- Full-text retrieval (SQLite FTS5 with CJK bigram tokenization) over 116 official Taiwan Ministry of Agriculture publications, with citations.

**Delivery**
- A Telegram bot with scheduled daily briefings, an hourly safety sentinel (proactive alerts for drought / waterlogging / salinity / elevated disease risk), local commands, and multimodal photo diagnosis. Built on Google Gemini with an agent tool-set and a ReAct loop.

## Design philosophy

- **Honesty over confidence** — when data is missing it reports "no data" rather than fabricating; estimates (ET₀, ETc, disease risk) are labelled as estimates, not measurements or diagnoses.
- **Self-watch** — the system assumes it will fail: heartbeat + watchdog + dead-man switch, scraper health accounting, command clamping and confirmation flows, and isolation of external/scraped text from instructions (prompt-injection defense).

## Architecture

A layered, modular Python codebase — `config` · `science` (pure computation) · `storage` (SQLite + JSON state) · `scrapers` · `agent` (Gemini session, tools, guard) · `tg` (Telegram) · `services` (scheduled push, sentinel) · `watchdog` — driven by four concurrent asyncio loops.

```
tests/behavior_test.py   # behavior regression suite (pure-logic checks + manual model checklist)
```

## Scope & status

This is a personal system built for one specific garden and microclimate, not a general-purpose product. Thresholds and crop parameters are calibrated to that context and are honestly flagged as heuristics. It's shared as a reference for anyone building similar *deterministic-science-plus-LLM* agricultural tooling.

## License

GPLv3 — see [LICENSE](LICENSE).

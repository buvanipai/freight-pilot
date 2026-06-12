# FreightPilot — Real-Time Freight Intelligence Platform

> Near real-time freight event streaming pipeline with ML anomaly detection, ETA prediction, and LLM-powered rerouting.

**Live Dashboard → [buvanipai.github.io/freight-pilot](https://buvanipai.github.io/freight-pilot/)**  
**API → [d2yu3oa51qymd0.cloudfront.net](https://d2yu3oa51qymd0.cloudfront.net/docs)**

---

## What It Does

| Layer | Tech | Detail |
|---|---|---|
| **Data** | FMCSA + FAF5 | 7,000+ real shipments seeded from federal freight datasets |
| **Streaming** | Redis Streams | Producer publishes carrier events every 2–5s; consumer processes at 4–10ms latency |
| **Storage** | PostgreSQL 15 | Shipments, carriers, customers, events tables |
| **Anomaly Detection** | IsolationForest | 200 estimators, 15% contamination, 6 features — flags ~877 anomalies across 5,853 active shipments |
| **ETA Prediction** | RandomForestRegressor | 100 estimators, trained on 1,597 delivered shipments |
| **Exception Detection** | SQL rules | Delayed, silent (no update > 2h), unresponsive carrier, anomaly |
| **Rerouting Agent** | Claude (LangChain) | Proposes 3 reroute options with carrier, mode, risk level, cost delta, and reasoning |
| **API** | FastAPI | Rate-limited REST API with connection pooling |
| **RL Agent** | Stable-Baselines3 PPO | Reroute action recommender trained on Gymnasium env; served via `/rl/recommend/{id}` |
| **Frontend** | React 18 + Tailwind | Single-page dashboard on GitHub Pages |
| **Infra** | AWS EC2 + ECR + CloudFront | Docker image pushed to ECR, pulled by EC2; CloudFront in front for HTTPS + caching |

---

## Architecture

```
FMCSA / FAF5 data
       │
       ▼
   db/seed.py ──────────────────────► PostgreSQL
                                           │
Producer (daemon thread)                   │
  generates carrier events                 │
       │                                   │
       ▼                                   │
 Redis Stream ──► Consumer (daemon) ──────►│
                                           │
                                     FastAPI (uvicorn · EC2 us-east-2)
                                      ├── /health
                                      ├── /shipments
                                      ├── /exceptions
                                      ├── /pipeline/metrics
                                      ├── /reroute/{id}        ◄── Claude Agent
                                      ├── /rl/recommend/{id}   ◄── PPO Agent
                                      └── /resolve/{id}
                                           │
                                    CloudFront (HTTPS)
                                           │
                                    GitHub Pages (React)
```

Producer, consumer, and ML model fitting all run as daemon threads inside the single FastAPI process. The API is containerised via Docker, pushed to ECR, and deployed on EC2 (us-east-2) with CloudFront in front for HTTPS termination and caching. CI/CD via GitHub Actions on every push to `main`.

---

## Project Structure

```
├── api/
│   ├── main.py          # FastAPI app, lifespan threads, endpoints
│   ├── agent.py         # LangChain + Claude reroute proposals
│   └── models.py        # IsolationForest + RandomForest training & inference
├── producer/
│   └── main.py          # Redis Streams event publisher
├── consumer/
│   └── main.py          # Redis Streams consumer → PostgreSQL writer
├── db/
│   ├── schema.sql        # PostgreSQL schema (idempotent)
│   ├── seed.py           # FMCSA + FAF5 seeder
│   ├── seed_dump.sql     # 3.4MB SQL dump (replaces 765MB raw files)
│   ├── seed_restore.py   # Restores seed_dump.sql via psql subprocess
│   └── schema_apply.py  # Applies schema idempotently on startup
├── env/
│   ├── freight_env.py    # Gymnasium environment (state, actions, reward)
│   └── data_generator.py # Synthetic shipment generator (no DB needed)
├── rl/
│   ├── train.py          # PPO training via Stable-Baselines3
│   ├── configs/          # sparse / shaped_v1 / shaped_v2 reward configs
│   └── experiments/      # comparison runner + README (reward hacking docs)
├── docs/
│   ├── index.html        # React dashboard (GitHub Pages)
│   └── config.js         # API base URL (localhost vs CloudFront)
├── data/raw/             # gitignored — FMCSA + FAF5 source files
├── models/               # gitignored — fitted .joblib model files
├── rl/checkpoints/       # gitignored — trained PPO .zip files
├── docker-compose.yml    # Local PostgreSQL + Redis
└── requirements.txt
```

---

## Running Locally

**Prerequisites:** Docker, Python 3.11+

```bash
# 1. Start PostgreSQL + Redis
docker-compose up -d

# 2. Install dependencies
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Set environment variables
cp .env.example .env
# Fill in ANTHROPIC_API_KEY

# 4. Apply schema and seed data
python db/schema_apply.py
python db/seed.py          # requires data/raw/ files (see note below)
# OR restore the committed dump (no raw files needed):
python db/seed_restore.py

# 5. Start the API (producer + consumer start automatically)
uvicorn api.main:app --reload --port 8000
```

> **Raw data files** (`data/raw/`) are gitignored due to size (765MB combined). `db/seed_dump.sql` (3.4MB) is included as a pre-seeded alternative.

Open `docs/index.html` directly in a browser or serve it with any static file server.

---

## Key Design Decisions

- **Single-process deployment** — Producer, consumer, and model fitting run as daemon threads inside the FastAPI process, started via the lifespan context manager. Keeps the Docker image simple and avoids orchestrating multiple containers on EC2.
- **Seed dump over raw data** — The FMCSA and FAF5 source files are 470MB + 295MB. A `pg_dump` of the seeded database is 3.4MB and committed to the repo, making cold deploys fast.
- **Deferred model fitting** — IsolationForest and RandomForest are fitted in a background thread at startup so uvicorn binds its port immediately (avoids Render's port scan timeout).
- **GitHub Pages for frontend** — The React app is a single HTML file in `docs/` served natively by GitHub Pages. No build step, no Node.js required.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | DB + Redis status |
| GET | `/shipments` | Paginated shipment list with filters + sort |
| GET | `/shipments/{id}` | Single shipment + last 5 events |
| GET | `/exceptions` | Active exceptions (delayed, silent, unresponsive, anomaly) |
| GET | `/pipeline/metrics` | Throughput, latency, exception counts |
| GET | `/pipeline/stream-info` | Redis stream length + consumer lag |
| GET | `/rl/recommend/{id}` | PPO agent action recommendation (hold / reroute_cheap / reroute_fast) |
| POST | `/reroute/{id}` | Claude agent proposes 3 reroute options |
| POST | `/resolve/{id}` | Mark exception as resolved |

Interactive docs: `https://d2yu3oa51qymd0.cloudfront.net/docs`

---

## Data Sources

- **FMCSA Motor Carrier Census** — Real US carrier names, DOT numbers, operating authority, safety ratings
- **FAF5 Freight Analysis Framework** — State-to-state freight flows (2018–2024), commodity codes, tonnage, value by mode

# Corvo Engineering — Policy-Driven PR-to-PO Procurement Portal

An end-to-end procurement intake, policy evaluation, and PO consolidation platform built with **FastAPI**, **Next.js (TypeScript)**, **SQLite/PostgreSQL**, and **Google Gemini (`gemini-3.8-flash`)**.

---

## Project Structure

```text
corvo-procurement-ai/
├── backend/
│   ├── app/
│   │   ├── api/routes.py               # REST endpoints (PR batch, chat, policies, contracts, CSV export)
│   │   ├── engine/
│   │   │   ├── decision_engine.py      # 6-stage deterministic PR-to-PO pipeline
│   │   │   ├── item_matcher.py         # Exact & fuzzy item catalog matcher
│   │   │   └── supplier_ranker.py      # 548-day lookback 5-signal supplier scoring
│   │   ├── services/
│   │   │   ├── chat_intake.py          # Conversational PR slot-filling assistant
│   │   │   ├── pdf_contract_parser.py  # PDF rate contract & Schedule A extractor
│   │   │   └── policy_assistant.py     # Policy-as-Data compiler & diff generator
│   │   ├── config.py                   # Environment configuration
│   │   ├── database.py                 # SQLAlchemy engine & session setup
│   │   ├── models.py                   # ORM tables (policies, contracts, POs, PRs, audit logs)
│   │   ├── schemas.py                  # Pydantic models & Policy v5.0 baseline DSL
│   │   ├── seed.py                     # Startup loader for spreadsheets & contract PDFs
│   │   └── main.py                     # FastAPI app entrypoint
│   ├── data/                           # Master data, contracts (XLSX + PDFs), and PR batches
│   ├── tests/test_worked_examples.py   # Automated tests for 14 worked examples & supplier rankings
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── app/
│   │   ├── globals.css
│   │   ├── layout.tsx
│   │   └── page.tsx                    # 4-tab Buyer & Requester Workspace UI
│   ├── Dockerfile
│   └── package.json
├── docker-compose.yml
├── APPROACH.md
├── DECISIONS.md
└── README.md
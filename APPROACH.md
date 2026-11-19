# Technical Approach & Architecture

## 1. System Architecture
The application separates **deterministic procurement execution** from **AI-assisted intake and policy compilation**:

- **Backend (`FastAPI` + `SQLAlchemy` + `SQLite/PostgreSQL`):**
  - `app/engine/decision_engine.py`: Pure deterministic pipeline executing validation, item matching, chronological clubbing, contract ceiling tracking, priority-ordered rule evaluation, supplier assignment, and 24-hour PO consolidation.
  - `app/engine/supplier_ranker.py`: Implements the 5-signal weighted scoring model (Price Competitiveness 30%, On-Time Delivery 25%, Quality Acceptance 20%, Responsiveness 15%, Contract Status 10%) over the 548-day lookback window.
  - `app/engine/item_matcher.py`: Exact code lookup + normalized token-set fuzzy matching against `item_master.xlsx`.
  - `app/services/policy_assistant.py`: Compiles policy amendments into validated `PolicyRuleSet` JSON using Google Gemini (`gemini-3.8-flash` via `google-genai`) with a deterministic rule-parser fallback.
  - `app/services/chat_intake.py`: Multi-turn slot-filling assistant for conversational PR creation using `gemini-3.8-flash` and deterministic fallback extraction.
  - `app/services/pdf_contract_parser.py`: Extracts contract headers and `Schedule A` pricing tables from PDFs via `pdfplumber` with a `gemini-3.8-flash` fallback for non-standard layouts.
- **Frontend (`Next.js` + `TypeScript` + `Tailwind CSS`):**
  - Single-page 4-tab buyer and requester workspace: **Buyer Workspace & Batch**, **Requester Chat Intake**, **Policy as Data**, and **Contracts & Preferred Suppliers**.

---

## 2. How LLMs Are Used (and Constrained)
- **Strict Separation from Financial/Routing Math:** The LLM never computes PO values, contract ceilings, supplier scores, or routing decisions. All financial calculations and rule evaluations run deterministically in Python against the active `PolicyRuleSet`.
- **Schema Validation & Deterministic Safety Net:** Gemini outputs are constrained to structured JSON (`response_mime_type="application/json"`), validated strictly with Pydantic (`PolicyRuleSet.model_validate`), and backed by deterministic fallbacks so temporary API rate limits (`503`) never interrupt buyer workflows.

---

## 3. Verification Strategy
- **Automated Regression Suite (`pytest`):** `backend/tests/test_worked_examples.py` verifies all 14 worked examples from the specification, preferred supplier ranking benchmarks (`ITM-1048`, `ITM-1012`, `ITM-1085`), and live Policy-as-Data rule mutations.
# Engineering Decisions & Edge-Case Handling

This document records the core engineering choices and edge-case resolutions made while building the Corvo Engineering PR-to-PO procurement application.

---

## 1. Chronological State & Order of Execution
- **Deterministic Sort Key:** Before clubbing or contract ceiling consumption, all valid PR lines are sorted by `(created_at, pr_no, pr_line)` so earlier requisitions always claim contract ceiling and anchor clubbing windows first.
- **Clubbing Anchors:** A clubbing group is opened by the earliest eligible PR line for `(plant, item_code)` and absorbs subsequent lines whose `created_at` is within 14 calendar days of the anchor's `created_at` and whose `need_by_date` is within ±7 calendar days of the anchor's `need_by_date`.
- **Two-Pass Valuation & Ceiling Consumption:** Because clubbed lines share a combined quantity, clubbing runs first so that the *total grouped quantity* is checked against remaining contract ceiling—preventing partial-group contract splits when a combined group exceeds a contract's remaining balance.

---

## 2. Contract Ingestion & Ceiling Edge Cases
- **PDF-Only Contracts (`RC-2026-031`, `RC-2026-033`):** On startup, the seed pipeline parses all 12 PDFs in `contracts/pdf/` via `pdfplumber` and merges them with `contracts_master.xlsx` so contracts present only as signed PDFs are loaded with full `Schedule A` line rates and value caps.
- **Multiple Valid Contracts for the Same Item:** When two active contracts cover an item within the PR's `created_at` date and both have sufficient remaining ceiling for the grouped requirement, the engine selects the contract with the lowest unit rate.
- **Insufficient Ceiling:** If a contract's remaining ceiling (`value_cap - consumed_value`) is less than `group_qty * contract_rate`, that contract is skipped without partially consuming its balance, and valuation falls back to recent PO history (365 days) or `standard_price`.

---

## 3. Data Validation & Non-Catalog Items
- **Plant Scope & Zero/Negative Quantity:** Requisitions for out-of-scope plants (e.g., `DEL1`) or with `quantity <= 0` are marked `NOT_PROCESSED` with explicit reasons and excluded from clubbing, valuation, and PO generation.
- **New / Unmatched Items (`item_code` blank or `NEW`):** Fuzzy matching (`rapidfuzz` token-set similarity + synonym normalization) runs only when `item_code` is blank; items below the match threshold are explicitly preserved as `NEW` items (never force-mapped to an arbitrary catalog SKU) and routed to `NEW_ITEM_SOURCING` with 3 category-matched preferred suppliers.

---

## 4. Preferred Supplier Ranking & Top-Up Logic
- **Strict Eligibility:** Suppliers marked `Blocked` or `Inactive` in `supplier_master.xlsx` are excluded before scoring, even if they had historical POs in the 548-day lookback window.
- **Lookback Window & Reference Price:** Only POs within `[as_of_date - 548 days, as_of_date]` are scored. `reference_price` uses the median unit price paid for that item within the lookback window, falling back to `standard_price` when no prior POs exist for that SKU.
- **Short-List Top-Up:** When fewer than 3 active suppliers have historical POs for a specific item, the remaining slots (up to 3) are filled using active suppliers in the same item category ranked by their category-level weighted performance score (and `supplier_id` tie-breaker).

---

## 5. Policy as Data (Zero Hardcoded Thresholds)
- **Declarative `PolicyRuleSet` DSL:** All thresholds (clubbing windows, lookback days, consolidation hours, and priority-ordered `DecisionRule` conditions) are stored as structured JSON in the database (`policy_versions` table), not hardcoded in Python `if/else` blocks.
- **Safe Amendment Workflow:** Natural-language or PDF policy amendments are compiled into a new `PolicyRuleSet` draft via `gemini-3.8-flash` (with a deterministic regex fallback if the API is temporarily unavailable), validated via Pydantic, and previewed as a plain-language diff before a human clicks **Activate**.
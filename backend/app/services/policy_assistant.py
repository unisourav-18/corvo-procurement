import copy
import json
import re
import time
from google import genai
from google.genai import types
from app.config import settings
from app.schemas import PolicyRuleSet, DecisionRule, RuleCondition
from app.services.pdf_contract_parser import extract_text_from_pdf

KNOWN_CATEGORIES = [
    "Steel & Metals",
    "Bearings & Transmission",
    "Electrical Spares",
    "Fasteners",
    "Welding Consumables",
    "Hydraulics & Pneumatics",
    "Safety & PPE",
    "Packaging",
    "Office & IT",
    "Capital Equipment",
    "Lubricants",
    "Facility Services",
    "Testing & Calibration",
]


def compute_policy_diff(old_policy: PolicyRuleSet, new_policy: PolicyRuleSet) -> list[str]:
    diffs: list[str] = []
    if old_policy.effective_date != new_policy.effective_date:
        diffs.append(
            f"Effective date changed from {old_policy.effective_date} to {new_policy.effective_date}."
        )
    if set(old_policy.applicable_plants) != set(new_policy.applicable_plants):
        diffs.append(
            f"Applicable plants changed from {old_policy.applicable_plants} to {new_policy.applicable_plants}."
        )
    if old_policy.clubbing != new_policy.clubbing:
        diffs.append(
            f"Clubbing rules updated: creation window={new_policy.clubbing.creation_window_days}d "
            f"(was {old_policy.clubbing.creation_window_days}d), need-by window=±{new_policy.clubbing.need_by_window_days}d "
            f"(was ±{old_policy.clubbing.need_by_window_days}d)."
        )
    if old_policy.valuation != new_policy.valuation:
        diffs.append(
            f"Valuation lookback changed to {new_policy.valuation.recent_po_lookback_days}d "
            f"(was {old_policy.valuation.recent_po_lookback_days}d)."
        )
    if old_policy.consolidation != new_policy.consolidation:
        diffs.append(
            f"PO consolidation window changed to {new_policy.consolidation.window_hours}h "
            f"(was {old_policy.consolidation.window_hours}h)."
        )

    old_rules = {r.rule_id: r for r in old_policy.decision_rules}
    new_rules = {r.rule_id: r for r in new_policy.decision_rules}

    for rid, nr in new_rules.items():
        if rid not in old_rules:
            cond_str = " AND ".join(f"{c.field} {c.op} {c.value}" for c in nr.conditions)
            diffs.append(
                f"Added rule {rid} ({nr.clause}, priority {nr.priority}): IF [{cond_str}] -> "
                f"{nr.route} (suggest={nr.suggest_count}). {nr.description}"
            )
        else:
            orule = old_rules[rid]
            if orule.model_dump() != nr.model_dump():
                old_c = " AND ".join(f"{c.field} {c.op} {c.value}" for c in orule.conditions)
                new_c = " AND ".join(f"{c.field} {c.op} {c.value}" for c in nr.conditions)
                diffs.append(
                    f"Modified rule {rid} ({nr.clause}): changed from [{old_c} -> {orule.route}] "
                    f"to [{new_c} -> {nr.route}]. {nr.description}"
                )

    for rid in old_rules:
        if rid not in new_rules:
            diffs.append(f"Removed rule {rid} ({old_rules[rid].clause}).")

    if not diffs:
        diffs.append("Version metadata updated with no structural rule changes.")
    return diffs


def _deterministic_amendment_fallback(
    current_policy: PolicyRuleSet,
    amendment_text: str,
    new_version: str = "",
    effective_date: str = "",
) -> PolicyRuleSet:
    updated = copy.deepcopy(current_policy)
    ver_m = re.search(r"version\s*(v?\d+\.\d+)", amendment_text, re.IGNORECASE)
    eff_m = re.search(r"effective\s*(?:date\s*)?(\d{4}-\d{2}-\d{2})", amendment_text, re.IGNORECASE)
    updated.version = new_version or (ver_m.group(1) if ver_m else f"{current_policy.version}-amended")
    if effective_date:
        updated.effective_date = effective_date
    elif eff_m:
        updated.effective_date = eff_m.group(1)

    plant_m = re.search(r"\bplant\s+(PUN1|CHK1)\b", amendment_text, re.IGNORECASE)
    plant_code = plant_m.group(1).upper() if plant_m else None

    # 1. Spot PO limit update (global or plant-specific)
    spot_m = re.search(r"spot\s*po.*?(?:limit|below|<|to)\s*(?:inr|rs\.?|₹)?\s*([\d,]+)", amendment_text, re.IGNORECASE)
    if spot_m:
        new_limit = float(spot_m.group(1).replace(",", ""))
        if plant_code:
            updated.decision_rules.insert(
                0,
                DecisionRule(
                    rule_id=f"R-5.4-{plant_code}",
                    clause="Sec 5.4a",
                    description=f"For {plant_code}, Spot PO allowed only below INR {int(new_limit):,}",
                    priority=35,
                    conditions=[
                        RuleCondition(field="plant", op="==", value=plant_code),
                        RuleCondition(field="group_value_inr", op="<", value=new_limit),
                        RuleCondition(field="bought_from_active_supplier_in_lookback", op="==", value=True),
                    ],
                    decision="PO",
                    route="SPOT_PO",
                    supplier_mode="FIRST_PREFERRED",
                    suggest_count=0,
                ),
            )
            updated.decision_rules.insert(
                1,
                DecisionRule(
                    rule_id=f"R-5.4b-{plant_code}",
                    clause="Sec 5.4b",
                    description=f"For {plant_code}, requirements >= INR {int(new_limit):,} and < 10,00,000 require RFQ",
                    priority=36,
                    conditions=[
                        RuleCondition(field="plant", op="==", value=plant_code),
                        RuleCondition(field="group_value_inr", op=">=", value=new_limit),
                        RuleCondition(field="group_value_inr", op="<", value=1000000),
                    ],
                    decision="SOURCING_REQUEST",
                    route="RFQ",
                    supplier_mode="SUGGEST_PREFERRED",
                    suggest_count=3,
                ),
            )
        else:
            for r in updated.decision_rules:
                if r.route == "SPOT_PO":
                    for c in r.conditions:
                        if c.field == "group_value_inr":
                            c.value = new_limit
                    r.description = f"Requirement < INR {int(new_limit):,} with Active supplier PO in lookback"

    # 2. Emergency PO limit update
    emerg_m = re.search(r"emergency\s*po.*?(?:limit|<=|to|below)\s*(?:inr|rs\.?|₹)?\s*([\d,]+)", amendment_text, re.IGNORECASE)
    if emerg_m:
        new_emerg = float(emerg_m.group(1).replace(",", ""))
        for r in updated.decision_rules:
            if r.route == "EMERGENCY_PO":
                for c in r.conditions:
                    if c.field == "group_value_inr":
                        c.value = new_emerg
                r.description = f"Emergency requirement <= INR {int(new_emerg):,} with Active supplier PO in lookback"

    # 3. RFP threshold update
    rfp_m = re.search(r"rfp.*?(?:limit|>=|above|from|to)\s*(?:inr|rs\.?|₹)?\s*([\d,]+)", amendment_text, re.IGNORECASE)
    if rfp_m:
        new_rfp = float(rfp_m.group(1).replace(",", ""))
        for r in updated.decision_rules:
            if r.route == "RFQ":
                for c in r.conditions:
                    if c.field == "group_value_inr":
                        c.value = new_rfp
            elif r.route == "RFP":
                for c in r.conditions:
                    if c.field == "group_value_inr":
                        c.value = new_rfp

    # 4. Category-specific RFQ / RFP rule
    for cat in KNOWN_CATEGORIES:
        if cat.lower() in amendment_text.lower():
            cat_val_m = re.search(
                rf"{re.escape(cat)}.*?(?:above|>=|>)\s*(?:inr|rs\.?|₹)?\s*([\d,]+)",
                amendment_text,
                re.IGNORECASE,
            )
            if cat_val_m:
                thresh = float(cat_val_m.group(1).replace(",", ""))
                route = "RFP" if "rfp" in amendment_text.lower() else "RFQ"
                s_count = 5 if route == "RFP" else 3
                slug = re.sub(r"[^A-Z0-9]+", "", cat.upper())[:8]
                conds = [
                    RuleCondition(field="category", op="==", value=cat),
                    RuleCondition(field="group_value_inr", op=">", value=thresh),
                ]
                if plant_code:
                    conds.insert(0, RuleCondition(field="plant", op="==", value=plant_code))
                updated.decision_rules.insert(
                    0,
                    DecisionRule(
                        rule_id=f"R-CAT-{slug}",
                        clause="Sec 5.4c",
                        description=f"Category {cat} > INR {int(thresh):,} routed to {route}",
                        priority=25,
                        conditions=conds,
                        decision="SOURCING_REQUEST",
                        route=route,
                        supplier_mode="SUGGEST_PREFERRED",
                        suggest_count=s_count,
                    ),
                )

    # 5. Clubbing & Consolidation window updates
    club_m = re.search(r"clubbing.*?(\d+)\s*(?:calendar\s*)?days", amendment_text, re.IGNORECASE)
    if club_m:
        updated.clubbing.creation_window_days = int(club_m.group(1))

    cons_m = re.search(r"consolidation.*?(\d+)\s*hours?", amendment_text, re.IGNORECASE)
    if cons_m:
        updated.consolidation.window_hours = int(cons_m.group(1))

    return updated


def parse_policy_amendment(
    current_policy: PolicyRuleSet,
    amendment_text: str,
    new_version: str = "",
    effective_date: str = "",
) -> tuple[PolicyRuleSet, list[str]]:
    """
    Uses gemini-3.8-flash (with 1 retry and deterministic fallback)
    to produce an updated PolicyRuleSet JSON and a human-readable diff list.
    """
    if settings.gemini_api_key:
        system_prompt = """You are a Procurement Policy Compiler for Corvo Engineering.
Given the current active PolicyRuleSet JSON and a policy amendment document or chat instruction, output the COMPLETE updated PolicyRuleSet JSON.

Rules for compiling:
1. Preserve all unaffected sections and decision_rules exactly.
2. Increment version (e.g., v5.0 -> v5.1 or v6.0, or use the version stated in the amendment).
3. Extract effective_date in YYYY-MM-DD format if stated in the amendment.
4. Every decision rule has:
   - rule_id (e.g. "R-5.1", "R-5.2a", "R-5.4-PUN1")
   - clause (e.g. "Sec 5.4a")
   - description (clear plain-English explanation)
   - priority (integer; smaller numbers run FIRST! If a new plant-specific or category-specific exception must override a general rule, give it a LOWER priority number than the general rule, e.g., 25 or 35).
   - conditions: list of {field, op, value} where field is one of:
     ["item_type", "category", "plant", "department", "group_value_inr", "total_qty", "is_emergency", "has_usable_contract", "bought_from_active_supplier_in_lookback"]
     and op is one of: ["==", "!=", "<", "<=", ">", ">=", "in", "not_in"]
   - decision: "PO" or "SOURCING_REQUEST"
   - route: "CONTRACT_PO", "EMERGENCY_PO", "SPOT_PO", "RFQ", "RFP", or "CAPEX_SOURCING"
   - supplier_mode: "CONTRACT_SUPPLIER", "FIRST_PREFERRED", or "SUGGEST_PREFERRED"
   - suggest_count: integer (0 for PO routes, 3 for RFQ, 5 for RFP/CAPEX unless amended)
5. Return ONLY valid JSON matching the PolicyRuleSet schema."""

        user_prompt = (
            f"CURRENT ACTIVE POLICY JSON:\n{current_policy.model_dump_json(indent=2)}\n\n"
            f"AMENDMENT / INSTRUCTION:\n{amendment_text}\n"
        )
        if new_version:
            user_prompt += f"\nTarget Version Override: {new_version}"
        if effective_date:
            user_prompt += f"\nTarget Effective Date Override: {effective_date}"

        client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=15_000),
        )

        for attempt in range(2):
            try:
                print(f"[PolicyAssistant] Calling {settings.gemini_model} (attempt {attempt + 1}/2)...")
                resp = client.models.generate_content(
                    model=settings.gemini_model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        temperature=0.0,
                    ),
                )
                raw_json = json.loads(resp.text)
                updated = PolicyRuleSet.model_validate(raw_json)
                if new_version:
                    updated.version = new_version
                if effective_date:
                    updated.effective_date = effective_date
                diffs = compute_policy_diff(current_policy, updated)
                print(f"[PolicyAssistant] Compiled successfully via {settings.gemini_model}.")
                return updated, diffs
            except Exception as exc:
                print(f"[PolicyAssistant] Attempt {attempt + 1} failed: {exc}")
                if attempt == 0:
                    time.sleep(1.5)

    print("[PolicyAssistant] Using deterministic policy amendment parser.")
    updated = _deterministic_amendment_fallback(
        current_policy, amendment_text, new_version, effective_date
    )
    diffs = compute_policy_diff(current_policy, updated)
    return updated, diffs
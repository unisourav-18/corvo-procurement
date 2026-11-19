import json
import re
from datetime import datetime, timedelta
from typing import Optional
from google import genai
from google.genai import types
from app.config import settings
from app.engine.item_matcher import ItemMatcher


def _parse_relative_date(text: str, ref_date: datetime) -> Optional[str]:
    iso_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso_m:
        return iso_m.group(1)
    t = text.lower()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for idx, day_name in enumerate(weekdays):
        if day_name in t:
            days_ahead = (idx - ref_date.weekday() + 7) % 7
            if days_ahead == 0 or "next" in t:
                days_ahead += 7
            return (ref_date + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    if "tomorrow" in t:
        return (ref_date + timedelta(days=1)).strftime("%Y-%m-%d")
    in_days = re.search(r"in\s+(\d+)\s+days?", t)
    if in_days:
        return (ref_date + timedelta(days=int(in_days.group(1)))).strftime("%Y-%m-%d")
    return None


def _deterministic_slot_extract(latest_user_msg: str, state: dict, ref_dt: datetime) -> None:
    t = latest_user_msg
    if "chakan" in t.lower() or "chk1" in t.lower():
        state["plant"] = "CHK1"
    elif "bhosari" in t.lower() or "pune" in t.lower() or "pun1" in t.lower():
        state["plant"] = "PUN1"

    if "emergency" in t.lower() or "urgent" in t.lower():
        state["urgency"] = "Emergency"
    elif "normal" in t.lower() or "standard" in t.lower():
        state["urgency"] = "Normal"

    parsed_date = _parse_relative_date(t, ref_dt)
    if parsed_date:
        state["need_by_date"] = parsed_date

    code_m = re.search(r"\b(ITM-\d+)\b", t, re.IGNORECASE)
    if code_m:
        state["item_code"] = code_m.group(1).upper()

    qty_m = re.search(r"(?:need|want|order|qty|quantity)?\s*\b(\d+(?:\.\d+)?)\b", t, re.IGNORECASE)
    if qty_m and not state.get("quantity"):
        val = float(qty_m.group(1))
        if val > 0:
            state["quantity"] = val

    price_m = re.search(r"(?:inr|rs\.?|₹|price|estimate)\s*(\d+(?:\.\d+)?)", t, re.IGNORECASE)
    if price_m:
        state["estimated_unit_price"] = float(price_m.group(1))

    if not state.get("description"):
        cleaned = re.sub(
            r"\b(need|want|please|order|for|chakan|bhosari|pun1|chk1|by|next|friday|monday|tuesday|wednesday|thursday|tomorrow|\d+)\b",
            " ",
            t,
            flags=re.IGNORECASE,
        )
        cleaned = " ".join(cleaned.split())
        if cleaned:
            state["description"] = cleaned


def process_chat_turn(
    messages: list[dict],
    draft_state: dict,
    items: list[dict],
    current_date_str: str = "2026-11-19",
) -> dict:
    matcher = ItemMatcher(items)
    ref_dt = datetime.fromisoformat(current_date_str)
    latest_user_msg = messages[-1]["content"] if messages else ""
    state = dict(draft_state or {})

    if state.get("_awaiting_confirmation") and re.search(
        r"\b(yes|confirm|create|submit|go ahead|ok|okay|sure|proceed)\b",
        latest_user_msg.lower(),
    ):
        return {
            "reply": "Purchase Requisition created and submitted to the Procurement decision engine!",
            "draft_state": state,
            "ready_to_confirm": True,
            "confirmed": True,
            "item_match": state.get("_item_match"),
        }

    used_llm = False
    if settings.gemini_api_key:
        try:
            client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=12_000),
            )
            sys_prompt = f"""You are Corvo Engineering's Procurement Intake Assistant. Today's date is {current_date_str}.
Extract PR fields from the conversation and merge with current draft_state: {json.dumps(state)}.
Valid plants: PUN1 (Bhosari / Pune), CHK1 (Chakan).
Valid urgency: Normal, Emergency.
NEVER invent item codes. Extract the user's raw item description into 'description' (and 'item_code' ONLY if the user explicitly typed a code like ITM-1048).
Return JSON with keys:
- item_code (string or null)
- description (string or null)
- quantity (number or null)
- plant ("PUN1", "CHK1", or null)
- need_by_date ("YYYY-MM-DD" or null)
- urgency ("Normal", "Emergency", or null)
- estimated_unit_price (number or null, needed ONLY if item is not in catalog)"""

            convo_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
            resp = client.models.generate_content(
                model=settings.gemini_model,
                contents=convo_text,
                config=types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            extracted = json.loads(resp.text)
            for k in ["item_code", "description", "quantity", "plant", "need_by_date", "urgency", "estimated_unit_price"]:
                if extracted.get(k) is not None:
                    state[k] = extracted[k]
            used_llm = True
        except Exception as exc:
            print(f"[ChatIntake] {settings.gemini_model} unavailable ({exc}); using deterministic slot extractor.")

    if not used_llm:
        _deterministic_slot_extract(latest_user_msg, state, ref_dt)

    # Resolve item against Item Master
    match_info = None
    if state.get("item_code") or state.get("description"):
        res = matcher.match(state.get("item_code"), state.get("description", ""))
        match_info = {
            "resolved_item_code": res.item_code,
            "matched_description": res.matched_description,
            "confidence": res.confidence,
            "method": res.method,
            "candidates": res.candidates,
        }
        state["_item_match"] = match_info
        if res.item_code != "NEW":
            state["item_code"] = res.item_code
            state["uom"] = next(
                (i["uom"] for i in items if i["item_code"] == res.item_code), "NOS"
            )
        else:
            state["item_code"] = None
            state.setdefault("uom", "NOS")

    missing = []
    if not state.get("description") and not state.get("item_code"):
        missing.append("what item you need")
    if not state.get("quantity") or float(state["quantity"]) <= 0:
        missing.append("quantity")
    if state.get("plant") not in ("PUN1", "CHK1"):
        missing.append("plant (PUN1 Bhosari or CHK1 Chakan)")
    if not state.get("need_by_date"):
        missing.append("need-by date")
    if not state.get("urgency"):
        missing.append("urgency (Normal or Emergency)")
    if match_info and match_info["resolved_item_code"] == "NEW" and not state.get("estimated_unit_price"):
        missing.append("estimated unit price in INR (since this is a new non-catalog item)")

    if missing:
        state["_awaiting_confirmation"] = False
        prefix = ""
        if match_info:
            if match_info["resolved_item_code"] != "NEW":
                prefix = f"Matched **{match_info['resolved_item_code']} ({match_info['matched_description']})**. "
            else:
                cand_txt = ""
                if match_info["candidates"]:
                    names = ", ".join(f"{c['item_code']} ({c['description']})" for c in match_info["candidates"][:2])
                    cand_txt = f" (Closest catalog items: {names} — let me know if you meant one of those, otherwise I'll raise this as a **NEW** item.)"
                prefix = f"I didn't find an exact match in the item catalog{cand_txt}. "
        reply = f"{prefix}To complete your PR, please specify: **{', '.join(missing)}**."
        return {
            "reply": reply,
            "draft_state": state,
            "ready_to_confirm": False,
            "confirmed": False,
            "item_match": match_info,
        }

    state["_awaiting_confirmation"] = True
    item_label = (
        f"{match_info['resolved_item_code']} — {match_info['matched_description']}"
        if match_info and match_info["resolved_item_code"] != "NEW"
        else f"NEW ITEM — {state['description']} (Est. ₹{state['estimated_unit_price']}/unit)"
    )
    summary = (
        f"Please confirm your Purchase Requisition details:\n"
        f"- **Item:** {item_label}\n"
        f"- **Quantity:** {state['quantity']} {state.get('uom', 'NOS')}\n"
        f"- **Plant:** {state['plant']}\n"
        f"- **Need-by Date:** {state['need_by_date']}\n"
        f"- **Urgency:** {state['urgency']}\n\n"
        f"Reply **Confirm** to raise this PR."
    )
    return {
        "reply": summary,
        "draft_state": state,
        "ready_to_confirm": True,
        "confirmed": False,
        "item_match": match_info,
    }
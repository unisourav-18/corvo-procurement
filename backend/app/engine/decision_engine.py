from datetime import datetime, timedelta
from typing import Any
from app.schemas import PolicyRuleSet, RuleCondition
from app.engine.item_matcher import ItemMatcher
from app.engine.supplier_ranker import SupplierRanker


def _eval_condition(cond: RuleCondition, ctx: dict[str, Any]) -> bool:
    actual = ctx.get(cond.field)
    target = cond.value
    op = cond.op
    if op == "==":
        return actual == target
    if op == "!=":
        return actual != target
    if op == "<":
        return actual is not None and actual < target
    if op == "<=":
        return actual is not None and actual <= target
    if op == ">":
        return actual is not None and actual > target
    if op == ">=":
        return actual is not None and actual >= target
    if op == "in":
        return actual in target
    if op == "not_in":
        return actual not in target
    return False


class ProcurementDecisionEngine:
    def __init__(
        self,
        policy: PolicyRuleSet,
        items: list[dict],
        suppliers: list[dict],
        po_history: list[dict],
        contracts: list[dict],
        contract_lines: list[dict],
    ):
        self.policy = policy
        self.items_by_code = {i["item_code"].strip(): i for i in items}
        self.suppliers_by_id = {s["supplier_id"].strip(): s for s in suppliers}
        self.po_history = po_history
        self.contracts = contracts
        self.contract_lines = contract_lines

        self.matcher = ItemMatcher(items)
        self.ranker = SupplierRanker(
            suppliers=suppliers,
            po_history=po_history,
            contracts=contracts,
            contract_lines=contract_lines,
            excluded_statuses=policy.excluded_supplier_statuses,
            weights=policy.supplier_ranking,
        )

    def process_batch(self, raw_lines: list[dict]) -> dict[str, Any]:
        """
        Processes a list of PR lines and returns:
          - line_results: exact Section 4 schema rows (1 per input PR line)
          - requirements: grouped requirement summaries for Buyer Workspace
          - purchase_orders: consolidated PO summaries
        """
        # Preserve original index to return lines in exact input order
        indexed_lines = []
        for orig_idx, line in enumerate(raw_lines):
            item_copy = dict(line)
            item_copy["_orig_idx"] = orig_idx
            item_copy["_created_dt"] = datetime.fromisoformat(str(line["created_at"]).strip())
            item_copy["_need_by_dt"] = datetime.fromisoformat(str(line["need_by_date"]).strip())
            indexed_lines.append(item_copy)

        # Sort per Section 2.2: creation time, PR number, line number
        indexed_lines.sort(
            key=lambda x: (x["_created_dt"], str(x["pr_no"]), int(x["pr_line"]))
        )

        valid_plants = set(self.policy.applicable_plants)
        not_processed_map: dict[int, str] = {}
        line_match_map: dict[int, dict] = {}
        groups: list[dict] = []
        line_to_group_id: dict[int, str] = {}

        # Step 1 & 2: Validate, Match Item, and Club Requirements
        for line in indexed_lines:
            oidx = line["_orig_idx"]
            plant = str(line.get("plant") or "").strip()
            qty = line.get("quantity")

            if plant not in valid_plants:
                not_processed_map[oidx] = (
                    f"{self.policy.version}: Plant '{plant}' is outside policy scope {sorted(valid_plants)}"
                )
                continue

            if qty is None or float(qty) <= 0:
                not_processed_map[oidx] = (
                    f"{self.policy.version}: Invalid quantity ({qty}); must be > 0"
                )
                continue

            match = self.matcher.match(line.get("item_code"), line.get("description", ""))
            line_match_map[oidx] = {
                "resolved_item_code": match.item_code,
                "confidence": match.confidence,
                "method": match.method,
            }

            is_emerg = str(line.get("urgency") or "").strip().lower() == "emergency"

            # New items are never clubbed (Sec 1.2)
            if match.item_code == "NEW" and self.policy.clubbing.never_club_new_items:
                est_price = line.get("estimated_unit_price")
                if est_price is None or str(est_price).strip() == "" or float(est_price) <= 0:
                    not_processed_map[oidx] = (
                        f"{self.policy.version} Sec 1.2: Missing estimated_unit_price for NEW item"
                    )
                    continue
                gid = f"REQ-{len(groups) + 1:04d}"
                groups.append({
                    "group_id": gid,
                    "item_code": "NEW",
                    "description": line.get("description", ""),
                    "plant": plant,
                    "first_created": line["_created_dt"],
                    "first_need_by": line["_need_by_dt"],
                    "lines": [line],
                    "total_qty": float(qty),
                    "is_emergency": is_emerg,
                    "est_unit_price": float(est_price),
                })
                line_to_group_id[oidx] = gid
                continue

            # Try joining earliest qualifying requirement (Sec 2.2)
            joined = False
            for g in groups:
                if self.policy.clubbing.same_item_required and g["item_code"] != match.item_code:
                    continue
                if self.policy.clubbing.same_plant_required and g["plant"] != plant:
                    continue
                if (line["_created_dt"] - g["first_created"]) <= timedelta(
                    days=self.policy.clubbing.creation_window_days
                ):
                    if abs((line["_need_by_dt"] - g["first_need_by"]).days) <= (
                        self.policy.clubbing.need_by_window_days
                    ):
                        g["lines"].append(line)
                        g["total_qty"] += float(qty)
                        if is_emerg:
                            g["is_emergency"] = True
                        line_to_group_id[oidx] = g["group_id"]
                        joined = True
                        break

            if not joined:
                gid = f"REQ-{len(groups) + 1:04d}"
                groups.append({
                    "group_id": gid,
                    "item_code": match.item_code,
                    "description": self.items_by_code[match.item_code]["description"],
                    "plant": plant,
                    "first_created": line["_created_dt"],
                    "first_need_by": line["_need_by_dt"],
                    "lines": [line],
                    "total_qty": float(qty),
                    "is_emergency": is_emerg,
                    "est_unit_price": line.get("estimated_unit_price"),
                })
                line_to_group_id[oidx] = gid

        # Step 3 & 4: Evaluate Contracts, Valuation & Policy Rules in requirement_date order
        contract_remaining = {
            c["contract_id"]: float(c["value_cap"]) - float(c["consumed_value"])
            for c in self.contracts
        }
        groups.sort(key=lambda g: g["first_created"])
        sorted_rules = sorted(self.policy.decision_rules, key=lambda r: r.priority)
        active_supplier_ids = {
            sid for sid, s in self.suppliers_by_id.items()
            if s["status"] not in self.policy.excluded_supplier_statuses
        }

        group_decisions: dict[str, dict] = {}
        for g in groups:
            gid = g["group_id"]
            code = g["item_code"]
            req_dt = g["first_created"]
            req_day = req_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            qty = g["total_qty"]

            if code == "NEW":
                val = round(qty * g["est_unit_price"], 2)
                group_decisions[gid] = {
                    "group_id": gid,
                    "item_code": "NEW",
                    "description": g["description"],
                    "plant": g["plant"],
                    "first_created": req_dt,
                    "total_qty": qty,
                    "is_emergency": g["is_emergency"],
                    "decision": "SOURCING_REQUEST",
                    "route": "NEW_ITEM_SOURCING",
                    "supplier_id": "",
                    "unit_price": None,
                    "group_value_inr": val,
                    "suggested_supplier_ids": "",
                    "suggested_details": [],
                    "policy_version": self.policy.version,
                    "reason": f"{self.policy.version} Sec 1.2: Unmatched item routed to NEW_ITEM_SOURCING",
                }
                continue

            item_meta = self.items_by_code[code]

            # Applicable contracts (Sec 4.1)
            app_contracts = []
            for cl in self.contract_lines:
                if cl["item_code"] != code:
                    continue
                cid = cl["contract_id"]
                c_obj = next(c for c in self.contracts if c["contract_id"] == cid)
                sid = c_obj["supplier_id"]
                if sid not in active_supplier_ids:
                    continue
                if c_obj["start_dt"] <= req_day <= c_obj["end_dt"]:
                    app_contracts.append((float(cl["rate"]), cid, sid))
            app_contracts.sort(key=lambda x: x[0])

            # Usable contract with remaining ceiling (Sec 4.2 & 4.3)
            chosen_contract = None
            for rate, cid, sid in app_contracts:
                if contract_remaining[cid] >= qty * rate:
                    chosen_contract = (rate, cid, sid)
                    break

            # Valuation (Sec 3.1)
            lookback_365 = req_dt - timedelta(days=self.policy.valuation.recent_po_lookback_days)
            recent_pos = sorted(
                [
                    p for p in self.po_history
                    if p["item_code"] == code and lookback_365 <= p["po_date_dt"] < req_dt
                ],
                key=lambda x: (x["po_date_dt"], str(x["po_number"])),
                reverse=True,
            )

            val_price = None
            val_basis = ""
            for step in self.policy.valuation.priority_order:
                if step == "usable_contract_rate" and chosen_contract is not None:
                    val_price = chosen_contract[0]
                    val_basis = f"Contract {chosen_contract[1]} rate"
                    break
                if step == "lowest_applicable_contract_rate" and app_contracts:
                    val_price = app_contracts[0][0]
                    val_basis = f"Contract {app_contracts[0][1]} rate (ceiling exceeded)"
                    break
                if step == "recent_po_price" and recent_pos:
                    val_price = float(recent_pos[0]["unit_price"])
                    val_basis = f"PO {recent_pos[0]['po_number']} ({recent_pos[0]['po_date']})"
                    break
                if step == "standard_price":
                    val_price = float(item_meta["standard_price"])
                    val_basis = "Item master standard price"
                    break

            group_val = round(qty * float(val_price), 2)
            bought_active_365 = any(p["supplier_id"] in active_supplier_ids for p in recent_pos)

            ctx = {
                "item_type": item_meta["item_type"],
                "category": item_meta["category"],
                "plant": g["plant"],
                "department": g["lines"][0].get("department"),
                "group_value_inr": group_val,
                "total_qty": qty,
                "is_emergency": g["is_emergency"],
                "has_usable_contract": chosen_contract is not None,
                "bought_from_active_supplier_in_lookback": bought_active_365,
            }

            matched_rule = None
            for rule in sorted_rules:
                if all(_eval_condition(c, ctx) for c in rule.conditions):
                    matched_rule = rule
                    break

            if matched_rule is None:
                raise RuntimeError(f"No policy rule matched requirement {gid} with context {ctx}")

            supplier_id = ""
            unit_price = None
            suggested_ids = ""
            suggested_details = []
            evidence = val_basis

            if matched_rule.supplier_mode == "CONTRACT_SUPPLIER" and chosen_contract is not None:
                rate, cid, sid = chosen_contract
                contract_remaining[cid] -= qty * rate
                supplier_id = sid
                unit_price = round(rate, 2)
                group_val = round(qty * rate, 2)
                evidence = f"Contract {cid} ({sid})"

            elif matched_rule.supplier_mode == "FIRST_PREFERRED":
                pref_list = self.ranker.rank_for_item(code, req_dt)
                top = pref_list[0]
                supplier_id = top["supplier_id"]
                unit_price = round(float(top["reference_price"]), 2)
                group_val = round(qty * unit_price, 2)
                evidence = f"1st preferred {supplier_id} via {top['price_source']}"

            elif matched_rule.supplier_mode == "SUGGEST_PREFERRED":
                suggested_details = self.ranker.suggest_suppliers(
                    item_code=code,
                    category=item_meta["category"],
                    as_of=req_dt,
                    count=matched_rule.suggest_count,
                )
                suggested_ids = ";".join(s["supplier_id"] for s in suggested_details)

            group_decisions[gid] = {
                "group_id": gid,
                "item_code": code,
                "description": g["description"],
                "plant": g["plant"],
                "first_created": req_dt,
                "total_qty": qty,
                "is_emergency": g["is_emergency"],
                "decision": matched_rule.decision,
                "route": matched_rule.route,
                "supplier_id": supplier_id,
                "unit_price": unit_price,
                "group_value_inr": group_val,
                "suggested_supplier_ids": suggested_ids,
                "suggested_details": suggested_details,
                "policy_version": self.policy.version,
                "reason": f"{self.policy.version} {matched_rule.clause}: {matched_rule.description} [{evidence}]",
            }

        # Step 5: PO Consolidation (Sec 7)
        po_groups = [
            gd for gd in group_decisions.values() if gd["decision"] == "PO"
        ]
        po_groups.sort(key=lambda x: x["first_created"])

        open_pos: list[dict] = []
        group_to_po_id: dict[str, str] = {}

        for gd in po_groups:
            joined_po = None
            if self.policy.consolidation.enabled:
                for po in open_pos:
                    if self.policy.consolidation.match_supplier and po["supplier_id"] != gd["supplier_id"]:
                        continue
                    if self.policy.consolidation.match_plant and po["plant"] != gd["plant"]:
                        continue
                    if self.policy.consolidation.match_route and po["route"] != gd["route"]:
                        continue
                    if (gd["first_created"] - po["first_created"]) <= timedelta(
                        hours=self.policy.consolidation.window_hours
                    ):
                        joined_po = po
                        break

            if joined_po is not None:
                joined_po["requirements"].append(gd["group_id"])
                joined_po["total_value_inr"] = round(
                    joined_po["total_value_inr"] + gd["group_value_inr"], 2
                )
                group_to_po_id[gd["group_id"]] = joined_po["po_id"]
            else:
                po_id = f"PO-{len(open_pos) + 1:04d}"
                open_pos.append({
                    "po_id": po_id,
                    "supplier_id": gd["supplier_id"],
                    "plant": gd["plant"],
                    "route": gd["route"],
                    "first_created": gd["first_created"],
                    "requirements": [gd["group_id"]],
                    "total_value_inr": gd["group_value_inr"],
                })
                group_to_po_id[gd["group_id"]] = po_id

        # Step 6: Assemble Section 4 Output Rows (1 row per input PR line)
        line_results = []
        for orig_idx, raw in enumerate(raw_lines):
            if orig_idx in not_processed_map:
                line_results.append({
                    "pr_no": raw["pr_no"],
                    "pr_line": raw["pr_line"],
                    "resolved_item_code": "",
                    "group_id": "",
                    "po_id": "",
                    "decision": "NOT_PROCESSED",
                    "route": "NOT_PROCESSED",
                    "supplier_id": "",
                    "unit_price": "",
                    "group_value_inr": "",
                    "suggested_supplier_ids": "",
                    "policy_version": self.policy.version,
                    "reason": not_processed_map[orig_idx],
                })
                continue

            gid = line_to_group_id[orig_idx]
            gd = group_decisions[gid]
            po_id = group_to_po_id.get(gid, "")
            line_results.append({
                "pr_no": raw["pr_no"],
                "pr_line": raw["pr_line"],
                "resolved_item_code": line_match_map[orig_idx]["resolved_item_code"],
                "group_id": gid,
                "po_id": po_id,
                "decision": gd["decision"],
                "route": gd["route"],
                "supplier_id": gd["supplier_id"],
                "unit_price": gd["unit_price"] if gd["unit_price"] is not None else "",
                "group_value_inr": gd["group_value_inr"],
                "suggested_supplier_ids": gd["suggested_supplier_ids"],
                "policy_version": gd["policy_version"],
                "reason": gd["reason"],
            })

        return {
            "line_results": line_results,
            "requirements": list(group_decisions.values()),
            "purchase_orders": open_pos,
        }
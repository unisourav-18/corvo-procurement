from datetime import datetime, timedelta
from typing import Optional
from app.schemas import SupplierRankingWeights


class SupplierRanker:
    def __init__(
        self,
        suppliers: list[dict],
        po_history: list[dict],
        contracts: list[dict],
        contract_lines: list[dict],
        excluded_statuses: list[str],
        weights: SupplierRankingWeights,
    ):
        self.suppliers_by_id = {s["supplier_id"]: s for s in suppliers}
        self.po_history = po_history
        self.contracts = contracts
        self.contract_lines = contract_lines
        self.excluded_statuses = set(excluded_statuses)
        self.w = weights

    def _is_eligible(self, supplier_id: str) -> bool:
        s = self.suppliers_by_id.get(supplier_id)
        return s is not None and s["status"] not in self.excluded_statuses

    def rank_for_item(self, item_code: str, as_of: datetime) -> list[dict]:
        cutoff = as_of - timedelta(days=self.w.lookback_days)

        # POs strictly before as_of within lookback window
        item_pos = [
            p for p in self.po_history
            if p["item_code"] == item_code
            and cutoff <= p["po_date_dt"] < as_of
        ]

        # Contracts covering item with start_date <= as_of (even if expired)
        valid_cids = {
            cl["contract_id"] for cl in self.contract_lines if cl["item_code"] == item_code
        }
        item_contracts = [
            c for c in self.contracts
            if c["contract_id"] in valid_cids and c["start_dt"] <= as_of
        ]

        cand_ids = {
            sid for sid in (
                {p["supplier_id"] for p in item_pos}
                | {c["supplier_id"] for c in item_contracts}
            )
            if self._is_eligible(sid)
        }

        ranked = []
        for sid in cand_ids:
            s_info = self.suppliers_by_id[sid]
            s_pos = sorted(
                [p for p in item_pos if p["supplier_id"] == sid],
                key=lambda x: (x["po_date_dt"], str(x["po_number"])),
                reverse=True,
            )
            po_count = len(s_pos)

            if po_count > 0:
                days_since = (as_of - s_pos[0]["po_date_dt"]).days
                if days_since <= self.w.recency_high_days:
                    recency = self.w.recency_high_score
                elif days_since <= self.w.recency_mid_days:
                    recency = self.w.recency_mid_score
                else:
                    recency = self.w.recency_low_score
                ref_price = float(s_pos[0]["unit_price"])
                price_source = f"PO {s_pos[0]['po_number']} ({s_pos[0]['po_date']})"
            else:
                recency = self.w.recency_low_score
                s_c = sorted(
                    [c for c in item_contracts if c["supplier_id"] == sid],
                    key=lambda x: x["start_dt"],
                    reverse=True,
                )
                cid = s_c[0]["contract_id"]
                line = next(
                    cl for cl in self.contract_lines
                    if cl["contract_id"] == cid and cl["item_code"] == item_code
                )
                ref_price = float(line["rate"])
                price_source = f"Contract {cid}"

            has_contract = 1 if any(c["supplier_id"] == sid for c in item_contracts) else 0
            on_time_norm = float(s_info["on_time_pct"]) / 100.0
            quality_norm = float(s_info["quality_rating"]) / 5.0
            freq_norm = min(po_count / max(self.w.po_count_cap, 1), 1.0)

            score = (
                self.w.weight_frequency * freq_norm
                + self.w.weight_recency * recency
                + self.w.weight_contract * has_contract
                + self.w.weight_on_time * on_time_norm
                + self.w.weight_quality * quality_norm
            )

            explanation = (
                f"Score {score:.3f}: {po_count} PO(s) in {self.w.lookback_days}d, "
                f"recency={recency}, contract={'Yes' if has_contract else 'No'}, "
                f"on-time={s_info['on_time_pct']}%, quality={s_info['quality_rating']}/5.0"
            )

            ranked.append({
                "item_code": item_code,
                "supplier_id": sid,
                "supplier_name": s_info["name"],
                "score": round(score, 4),
                "po_count": po_count,
                "recency": recency,
                "has_contract": bool(has_contract),
                "on_time_pct": s_info["on_time_pct"],
                "quality_rating": s_info["quality_rating"],
                "reference_price": round(ref_price, 2),
                "price_source": price_source,
                "explanation": explanation,
                "is_topup": False,
            })

        ranked.sort(
            key=lambda x: (x["score"], x["po_count"], x["on_time_pct"], x["quality_rating"]),
            reverse=True,
        )
        for idx, r in enumerate(ranked, start=1):
            r["rank"] = idx
        return ranked

    def suggest_suppliers(self, item_code: str, category: str, as_of: datetime, count: int) -> list[dict]:
        preferred = self.rank_for_item(item_code, as_of)
        chosen_ids = {r["supplier_id"] for r in preferred}
        result = list(preferred)

        if len(result) < count:
            category_pool = []
            for sid, s in self.suppliers_by_id.items():
                if sid in chosen_ids or not self._is_eligible(sid):
                    continue
                cats = [c.strip() for c in str(s["categories"]).split(";")]
                if category in cats:
                    perf = (
                        self.w.topup_weight_on_time * (float(s["on_time_pct"]) / 100.0)
                        + self.w.topup_weight_quality * (float(s["quality_rating"]) / 5.0)
                    )
                    category_pool.append((perf, s["on_time_pct"], s["quality_rating"], sid, s))

            category_pool.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
            for perf, _, _, sid, s in category_pool:
                if len(result) >= count:
                    break
                result.append({
                    "item_code": item_code,
                    "supplier_id": sid,
                    "supplier_name": s["name"],
                    "rank": len(result) + 1,
                    "score": round(perf, 4),
                    "reference_price": None,
                    "explanation": (
                        f"Category top-up ({category}): on-time={s['on_time_pct']}%, "
                        f"quality={s['quality_rating']}/5.0"
                    ),
                    "is_topup": True,
                })
        return result[:count]
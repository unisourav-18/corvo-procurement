import re
from dataclasses import dataclass
from typing import Optional

SYNONYMS = {
    "brg": "bearing",
    "pneu": "pneumatic",
    "hyd": "hydraulic",
    "elec": "electrode",
    "cyl": "cylinder",
    "assy": "assembly",
    "qty": "quantity",
}

NOISE_PATTERNS = [
    r"\bas per sample\b",
    r"\bfor maintenance\b",
    r"\(repeat\)",
    r"-\s*urgent\b",
    r"\burgent\b",
]


@dataclass
class MatchResult:
    item_code: str  # Matched item_code or "NEW"
    confidence: float
    matched_description: Optional[str]
    method: str  # "EXACT_CODE" | "FUZZY_TOKEN" | "NEW_ITEM"
    candidates: list[dict]


def normalize_tokens(text: str) -> set[str]:
    t = str(text or "").lower()
    for pat in NOISE_PATTERNS:
        t = re.sub(pat, " ", t)
    t = t.replace('"', " inch ")
    t = re.sub(r"[^a-z0-9./]+", " ", t)
    return {SYNONYMS.get(tok, tok) for tok in t.split() if tok}


class ItemMatcher:
    def __init__(self, items: list[dict]):
        """
        items: list of dicts with keys: item_code, description, uom, category, item_type, standard_price
        """
        self.items_by_code = {row["item_code"].strip(): row for row in items}
        self.item_tokens = {
            code: normalize_tokens(row["description"])
            for code, row in self.items_by_code.items()
        }

    def match(self, item_code: Optional[str], description: str) -> MatchResult:
        if item_code and str(item_code).strip() in self.items_by_code:
            code = str(item_code).strip()
            meta = self.items_by_code[code]
            return MatchResult(
                item_code=code,
                confidence=1.0,
                matched_description=meta["description"],
                method="EXACT_CODE",
                candidates=[meta],
            )

        q_toks = normalize_tokens(description)
        if not q_toks:
            return MatchResult("NEW", 0.0, None, "NEW_ITEM", [])

        scored = []
        for code, i_toks in self.item_tokens.items():
            if not i_toks:
                continue
            inter = len(q_toks & i_toks)
            if inter == 0:
                continue
            jacc = inter / len(q_toks | i_toks)
            recall = inter / len(i_toks)
            prec = inter / len(q_toks)
            score = 0.50 * jacc + 0.25 * recall + 0.25 * prec
            scored.append((score, recall, jacc, code))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_candidates = [
            {**self.items_by_code[code], "match_score": round(sc, 3)}
            for sc, _, _, code in scored[:3]
        ]

        if scored:
            best_score, best_recall, best_jacc, best_code = scored[0]
            if best_recall >= 0.60 or best_jacc >= 0.50:
                return MatchResult(
                    item_code=best_code,
                    confidence=round(best_score, 3),
                    matched_description=self.items_by_code[best_code]["description"],
                    method="FUZZY_TOKEN",
                    candidates=top_candidates,
                )

        return MatchResult(
            item_code="NEW",
            confidence=0.0,
            matched_description=None,
            method="NEW_ITEM",
            candidates=top_candidates,
        )
import copy
import os
import pandas as pd
import pytest
from datetime import datetime
from app.schemas import POLICY_V5_DEFAULT, DecisionRule, RuleCondition
from app.seed import load_master_data, _read_table
from app.engine.decision_engine import ProcurementDecisionEngine
from app.engine.supplier_ranker import SupplierRanker
from app.services.pdf_contract_parser import parse_contract_pdf


@pytest.fixture(scope="module")
def engine_v5():
    master = load_master_data()
    data_dir = os.getenv("DATA_DIR", "./data")
    reg_df = _read_table(os.path.join(data_dir, "contracts", "contract_register"))
    lines_df = _read_table(os.path.join(data_dir, "contracts", "contract_lines"))

    contracts = []
    for _, r in reg_df.iterrows():
        s_str = pd.to_datetime(r["start_date"]).strftime("%Y-%m-%d")
        e_str = pd.to_datetime(r["end_date"]).strftime("%Y-%m-%d")
        contracts.append({
            "contract_id": str(r["contract_id"]).strip(),
            "supplier_id": str(r["supplier_id"]).strip(),
            "supplier_name": str(r["supplier_name"]).strip(),
            "start_date": s_str,
            "end_date": e_str,
            "start_dt": datetime.fromisoformat(s_str),
            "end_dt": datetime.fromisoformat(e_str),
            "value_cap": float(r["value_cap"]),
            "consumed_value": float(r["consumed_value"]),
        })

    clines = lines_df.to_dict(orient="records")

    # Also load PDF-only contracts from contracts/pdf/
    pdf_dir = os.path.join(data_dir, "contracts", "pdf")
    for fname in sorted(os.listdir(pdf_dir)):
        if not fname.endswith(".pdf"):
            continue
        with open(os.path.join(pdf_dir, fname), "rb") as f:
            parsed = parse_contract_pdf(f.read())
        if not any(c["contract_id"] == parsed["contract_id"] for c in contracts):
            contracts.append({
                "contract_id": parsed["contract_id"],
                "supplier_id": parsed["supplier_id"],
                "supplier_name": parsed["supplier_name"],
                "start_date": parsed["start_date"],
                "end_date": parsed["end_date"],
                "start_dt": datetime.fromisoformat(parsed["start_date"]),
                "end_dt": datetime.fromisoformat(parsed["end_date"]),
                "value_cap": float(parsed["value_cap"]),
                "consumed_value": 0.0,
            })
            for l in parsed["lines"]:
                clines.append({
                    "contract_id": parsed["contract_id"],
                    "item_code": l["item_code"],
                    "description": l["description"],
                    "uom": l["uom"],
                    "rate": float(l["rate"]),
                })

    return ProcurementDecisionEngine(
        policy=POLICY_V5_DEFAULT,
        items=master["items"],
        suppliers=master["suppliers"],
        po_history=master["po_history"],
        contracts=contracts,
        contract_lines=clines,
    )


def test_preferred_supplier_benchmarks(engine_v5):
    as_of = datetime.fromisoformat("2026-11-01")

    # Benchmark 1: ITM-1048 -> S029 (1), S030 (2), S031 excluded (Inactive)
    r1 = engine_v5.ranker.rank_for_item("ITM-1048", as_of)
    assert [x["supplier_id"] for x in r1] == ["S029", "S030"]

    # Benchmark 2: ITM-1036 -> S023 (1), S022 (2), S024 (3)
    r2 = engine_v5.ranker.rank_for_item("ITM-1036", as_of)
    assert [x["supplier_id"] for x in r2] == ["S023", "S022", "S024"]

    # Benchmark 3: ITM-1016 -> S009 (1), S010 (2)
    r3 = engine_v5.ranker.rank_for_item("ITM-1016", as_of)
    assert [x["supplier_id"] for x in r3] == ["S009", "S010"]


def test_all_14_worked_examples(engine_v5):
    data_dir = os.getenv("DATA_DIR", "./data")
    batch_df = _read_table(os.path.join(data_dir, "pr_batches", "sample_pr_batch"))
    batch_df = batch_df.where(pd.notnull(batch_df), None)
    out = engine_v5.process_batch(batch_df.to_dict(orient="records"))
    by_pr = {r["pr_no"]: r for r in out["line_results"]}

    # Ex 1: Contract PO
    assert by_pr["PR-26-00019"]["route"] == "CONTRACT_PO"
    assert by_pr["PR-26-00019"]["supplier_id"] == "S001"
    assert by_pr["PR-26-00019"]["unit_price"] == 9168.71

    # Ex 2: PDF-only Contract RC-2026-031
    assert by_pr["PR-26-00045"]["route"] == "CONTRACT_PO"
    assert by_pr["PR-26-00045"]["supplier_id"] == "S021"
    assert by_pr["PR-26-00045"]["unit_price"] == 2439.01

    # Ex 3: Contract ceiling nearly used up -> RFQ
    assert by_pr["PR-26-00004"]["route"] == "RFQ"
    assert by_pr["PR-26-00004"]["group_id"] == by_pr["PR-26-00026"]["group_id"]
    assert by_pr["PR-26-00004"]["suggested_supplier_ids"] == "S014;S013;S016"

    # Ex 4: Two contracts -> lower-rate RC-2026-015 full, falls back to RC-2026-013 (S007 at 7.06)
    assert by_pr["PR-26-00034"]["route"] == "CONTRACT_PO"
    assert by_pr["PR-26-00034"]["supplier_id"] == "S007"
    assert by_pr["PR-26-00034"]["unit_price"] == 7.06

    # Ex 5: 3 split PRs clubbed into 1 requirement
    assert by_pr["PR-26-00146"]["group_id"] == by_pr["PR-26-00144"]["group_id"] == by_pr["PR-26-00145"]["group_id"]
    assert by_pr["PR-26-00146"]["route"] == "SPOT_PO"
    assert by_pr["PR-26-00146"]["supplier_id"] == "S024"

    # Ex 6: Different plants -> NOT clubbed
    assert by_pr["PR-26-00106"]["group_id"] != by_pr["PR-26-00103"]["group_id"]
    assert by_pr["PR-26-00106"]["route"] == "SPOT_PO"
    assert by_pr["PR-26-00103"]["route"] == "SPOT_PO"

    # Ex 7: Free-text shorthand match
    assert by_pr["PR-26-00088"]["resolved_item_code"] == "ITM-1035"
    assert by_pr["PR-26-00088"]["route"] == "SPOT_PO"
    assert by_pr["PR-26-00088"]["supplier_id"] == "S023"

    # Ex 8: Emergency PO
    assert by_pr["PR-26-00059"]["route"] == "EMERGENCY_PO"
    assert by_pr["PR-26-00059"]["supplier_id"] == "S014"

    # Ex 9: RFQ above 2 lakh
    assert by_pr["PR-26-00089"]["route"] == "RFQ"
    assert by_pr["PR-26-00089"]["suggested_supplier_ids"] == "S043;S044;S042"

    # Ex 10: RFQ small but never bought in 365d
    assert by_pr["PR-26-00050"]["route"] == "RFQ"
    assert by_pr["PR-26-00050"]["suggested_supplier_ids"] == "S003;S002;S005"

    # Ex 11: RFP >= 10 lakh
    assert by_pr["PR-26-00100"]["route"] == "RFP"
    assert by_pr["PR-26-00100"]["suggested_supplier_ids"] == "S020;S017;S019;S018"

    # Ex 12: Capex Sourcing
    assert by_pr["PR-26-00047"]["route"] == "CAPEX_SOURCING"
    assert by_pr["PR-26-00047"]["suggested_supplier_ids"] == "S045;S047;S020;S048;S046"

    # Ex 13: New item
    assert by_pr["PR-26-00170"]["resolved_item_code"] == "NEW"
    assert by_pr["PR-26-00170"]["route"] == "NEW_ITEM_SOURCING"

    # Ex 14: PO consolidation across separate requirements within 24h
    assert by_pr["PR-26-00031"]["po_id"] == by_pr["PR-26-00035"]["po_id"]
    assert by_pr["PR-26-00034"]["po_id"] == by_pr["PR-26-00032"]["po_id"]

    # Edge cases: zero qty and invalid plant -> NOT_PROCESSED
    assert by_pr["PR-26-00172"]["route"] == "NOT_PROCESSED"
    assert by_pr["PR-26-00173"]["route"] == "NOT_PROCESSED"


def test_policy_as_data_live_mutation(engine_v5):
    """
    Verifies that adding a plant-specific rule in JSON changes decisions with zero code changes.
    """
    amended = copy.deepcopy(POLICY_V5_DEFAULT)
    amended.version = "v5.1"
    # Add high-priority rule: for PUN1, any requirement >= 50,000 without contract must go to RFQ
    amended.decision_rules.insert(
        0,
        DecisionRule(
            rule_id="R-5.2a-PUN1",
            clause="Sec 5.2a",
            description="PUN1 Spot PO cap lowered to INR 50,000",
            priority=25,
            conditions=[
                RuleCondition(field="plant", op="==", value="PUN1"),
                RuleCondition(field="group_value_inr", op=">=", value=50000),
            ],
            decision="SOURCING_REQUEST",
            route="RFQ",
            supplier_mode="SUGGEST_PREFERRED",
            suggest_count=3,
        ),
    )

    mutated_engine = ProcurementDecisionEngine(
        policy=amended,
        items=list(engine_v5.items_by_code.values()),
        suppliers=list(engine_v5.suppliers_by_id.values()),
        po_history=engine_v5.po_history,
        contracts=engine_v5.contracts,
        contract_lines=engine_v5.contract_lines,
    )

    data_dir = os.getenv("DATA_DIR", "./data")
    batch_df = _read_table(os.path.join(data_dir, "pr_batches", "sample_pr_batch"))
    batch_df = batch_df.where(pd.notnull(batch_df), None)
    out = mutated_engine.process_batch(batch_df.to_dict(orient="records"))
    by_pr = {r["pr_no"]: r for r in out["line_results"]}

    # PR-26-00088 (PUN1, ₹97,955) was SPOT_PO in v5.0, must now become RFQ in v5.1!
    assert by_pr["PR-26-00088"]["route"] == "RFQ"
    assert by_pr["PR-26-00088"]["policy_version"] == "v5.1"
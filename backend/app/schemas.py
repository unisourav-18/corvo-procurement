from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class ClubbingConfig(BaseModel):
    creation_window_days: int = 14
    need_by_window_days: int = 7
    same_item_required: bool = True
    same_plant_required: bool = True
    never_club_new_items: bool = True


class ValuationConfig(BaseModel):
    priority_order: list[Literal[
        "usable_contract_rate",
        "lowest_applicable_contract_rate",
        "recent_po_price",
        "standard_price",
    ]] = [
        "usable_contract_rate",
        "lowest_applicable_contract_rate",
        "recent_po_price",
        "standard_price",
    ]
    recent_po_lookback_days: int = 365


class SupplierRankingWeights(BaseModel):
    lookback_days: int = 548
    recency_high_days: int = 90
    recency_mid_days: int = 365
    recency_high_score: float = 1.0
    recency_mid_score: float = 0.5
    recency_low_score: float = 0.0
    po_count_cap: int = 5
    weight_frequency: float = 0.30
    weight_recency: float = 0.25
    weight_contract: float = 0.20
    weight_on_time: float = 0.15
    weight_quality: float = 0.10
    topup_weight_on_time: float = 0.50
    topup_weight_quality: float = 0.50


class ConsolidationConfig(BaseModel):
    enabled: bool = True
    window_hours: int = 24
    match_supplier: bool = True
    match_plant: bool = True
    match_route: bool = True


class RuleCondition(BaseModel):
    """
    Generic predicate evaluated against a RequirementContext.
    Supports plant-specific rules, category rules, value thresholds, emergency flags,
    contract availability, and purchase history checks.
    """
    field: Literal[
        "item_type",
        "category",
        "plant",
        "department",
        "group_value_inr",
        "total_qty",
        "is_emergency",
        "has_usable_contract",
        "bought_from_active_supplier_in_lookback",
    ]
    op: Literal["==", "!=", "<", "<=", ">", ">=", "in", "not_in"]
    value: Any


class DecisionRule(BaseModel):
    rule_id: str
    clause: str
    description: str
    priority: int  # Evaluated in ascending priority order
    conditions: list[RuleCondition] = Field(default_factory=list)
    decision: Literal["PO", "SOURCING_REQUEST"]
    route: Literal[
        "CONTRACT_PO",
        "EMERGENCY_PO",
        "SPOT_PO",
        "RFQ",
        "RFP",
        "CAPEX_SOURCING",
    ]
    supplier_mode: Literal[
        "CONTRACT_SUPPLIER",
        "FIRST_PREFERRED",
        "SUGGEST_PREFERRED",
    ]
    suggest_count: int = 0


class PolicyRuleSet(BaseModel):
    version: str
    effective_date: str  # YYYY-MM-DD
    title: str
    applicable_plants: list[str] = ["PUN1", "CHK1"]
    excluded_supplier_statuses: list[str] = ["Blocked", "Inactive"]
    clubbing: ClubbingConfig = Field(default_factory=ClubbingConfig)
    valuation: ValuationConfig = Field(default_factory=ValuationConfig)
    supplier_ranking: SupplierRankingWeights = Field(default_factory=SupplierRankingWeights)
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)
    decision_rules: list[DecisionRule]


# Seed representation of Corvo Procurement Policy v5.0
POLICY_V5_DEFAULT = PolicyRuleSet(
    version="v5.0",
    effective_date="2026-04-01",
    title="Corvo Engineering Procurement Policy v5.0",
    applicable_plants=["PUN1", "CHK1"],
    excluded_supplier_statuses=["Blocked", "Inactive"],
    decision_rules=[
        DecisionRule(
            rule_id="R-5.1",
            clause="Sec 5.1",
            description="Capex items always go to Capex Sourcing with 5 suggested suppliers",
            priority=10,
            conditions=[RuleCondition(field="item_type", op="==", value="Capex")],
            decision="SOURCING_REQUEST",
            route="CAPEX_SOURCING",
            supplier_mode="SUGGEST_PREFERRED",
            suggest_count=5,
        ),
        DecisionRule(
            rule_id="R-5.2",
            clause="Sec 5.2",
            description="Active valid rate contract with sufficient ceiling produces Contract PO",
            priority=20,
            conditions=[RuleCondition(field="has_usable_contract", op="==", value=True)],
            decision="PO",
            route="CONTRACT_PO",
            supplier_mode="CONTRACT_SUPPLIER",
            suggest_count=0,
        ),
        DecisionRule(
            rule_id="R-5.3",
            clause="Sec 5.3",
            description="Emergency requirement <= INR 5,00,000 with Active supplier PO in last 365 days",
            priority=30,
            conditions=[
                RuleCondition(field="is_emergency", op="==", value=True),
                RuleCondition(field="group_value_inr", op="<=", value=500000),
                RuleCondition(field="bought_from_active_supplier_in_lookback", op="==", value=True),
            ],
            decision="PO",
            route="EMERGENCY_PO",
            supplier_mode="FIRST_PREFERRED",
            suggest_count=0,
        ),
        DecisionRule(
            rule_id="R-5.4",
            clause="Sec 5.4",
            description="Requirement < INR 2,00,000 with Active supplier PO in last 365 days",
            priority=40,
            conditions=[
                RuleCondition(field="group_value_inr", op="<", value=200000),
                RuleCondition(field="bought_from_active_supplier_in_lookback", op="==", value=True),
            ],
            decision="PO",
            route="SPOT_PO",
            supplier_mode="FIRST_PREFERRED",
            suggest_count=0,
        ),
        DecisionRule(
            rule_id="R-5.5",
            clause="Sec 5.5",
            description="Requirement < INR 10,00,000 (or never bought in 365 days) goes to RFQ (3 suppliers)",
            priority=50,
            conditions=[RuleCondition(field="group_value_inr", op="<", value=1000000)],
            decision="SOURCING_REQUEST",
            route="RFQ",
            supplier_mode="SUGGEST_PREFERRED",
            suggest_count=3,
        ),
        DecisionRule(
            rule_id="R-5.6",
            clause="Sec 5.6",
            description="Requirement >= INR 10,00,000 goes to formal RFP (5 suppliers)",
            priority=60,
            conditions=[RuleCondition(field="group_value_inr", op=">=", value=1000000)],
            decision="SOURCING_REQUEST",
            route="RFP",
            supplier_mode="SUGGEST_PREFERRED",
            suggest_count=5,
        ),
    ],
)
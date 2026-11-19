import io
from datetime import datetime
from typing import Optional
import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditLog, Contract, ContractLine, PolicyVersion, PRLineRecord, RequirementRecord
from app.schemas import PolicyRuleSet
from app.seed import load_master_data
from app.engine.decision_engine import ProcurementDecisionEngine
from app.engine.supplier_ranker import SupplierRanker
from app.services.pdf_contract_parser import extract_text_from_pdf, parse_contract_pdf
from app.services.policy_assistant import parse_policy_amendment
from app.services.chat_intake import process_chat_turn

router = APIRouter(prefix="/api")


def _get_active_policy(db: Session) -> PolicyRuleSet:
    pv = db.query(PolicyVersion).filter_by(status="ACTIVE").order_by(PolicyVersion.id.desc()).first()
    if not pv:
        raise HTTPException(status_code=404, detail="No active policy found")
    return PolicyRuleSet.model_validate(pv.rules_json)


def _get_contracts_data(db: Session) -> tuple[list[dict], list[dict]]:
    contracts = []
    for c in db.query(Contract).all():
        contracts.append({
            "contract_id": c.contract_id,
            "supplier_id": c.supplier_id,
            "supplier_name": c.supplier_name,
            "start_date": c.start_date,
            "end_date": c.end_date,
            "start_dt": datetime.fromisoformat(c.start_date),
            "end_dt": datetime.fromisoformat(c.end_date),
            "value_cap": c.value_cap,
            "consumed_value": c.consumed_value,
            "source": c.source,
        })
    clines = [
        {
            "contract_id": cl.contract_id,
            "item_code": cl.item_code,
            "description": cl.description,
            "uom": cl.uom,
            "rate": cl.rate,
        }
        for cl in db.query(ContractLine).all()
    ]
    return contracts, clines


def _build_engine(db: Session) -> ProcurementDecisionEngine:
    master = load_master_data()
    policy = _get_active_policy(db)
    contracts, clines = _get_contracts_data(db)
    return ProcurementDecisionEngine(
        policy=policy,
        items=master["items"],
        suppliers=master["suppliers"],
        po_history=master["po_history"],
        contracts=contracts,
        contract_lines=clines,
    )


# --- 1. BATCH PROCESSING & EXPORTS ---

@router.post("/process-batch")
async def process_batch(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    raw_bytes = await file.read()
    if file.filename.endswith(".xlsx"):
        df = pd.read_excel(io.BytesIO(raw_bytes))
    else:
        df = pd.read_csv(io.BytesIO(raw_bytes))

    df = df.where(pd.notnull(df), None)
    records = df.to_dict(orient="records")

    engine = _build_engine(db)
    result = engine.process_batch(records)

    batch_id = f"BATCH-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    db.query(RequirementRecord).delete()
    db.query(PRLineRecord).delete()

    line_res_by_key = {(r["pr_no"], int(r["pr_line"])): r for r in result["line_results"]}
    for r in records:
        lr = line_res_by_key[(r["pr_no"], int(r["pr_line"]))]
        db.add(PRLineRecord(
            batch_id=batch_id,
            pr_no=str(r["pr_no"]),
            pr_line=int(r["pr_line"]),
            created_at=str(r["created_at"]),
            requester=str(r.get("requester") or ""),
            department=str(r.get("department") or ""),
            plant=str(r["plant"]),
            item_code=r.get("item_code"),
            resolved_item_code=lr["resolved_item_code"],
            description=str(r["description"]),
            quantity=float(r["quantity"] or 0),
            uom=str(r.get("uom") or "NOS"),
            estimated_unit_price=float(r["estimated_unit_price"]) if r.get("estimated_unit_price") is not None else None,
            need_by_date=str(r["need_by_date"]),
            urgency=str(r.get("urgency") or "Normal"),
            group_id=lr["group_id"],
        ))

    po_lookup = {}
    for po in result["purchase_orders"]:
        for gid in po["requirements"]:
            po_lookup[gid] = po["po_id"]

    for req in result["requirements"]:
        db.add(RequirementRecord(
            batch_id=batch_id,
            group_id=req["group_id"],
            item_code=req["item_code"],
            description=req["description"],
            plant=req["plant"],
            first_created=req["first_created"].strftime("%Y-%m-%d %H:%M"),
            total_qty=req["total_qty"],
            is_emergency=req["is_emergency"],
            decision=req["decision"],
            route=req["route"],
            supplier_id=req["supplier_id"],
            unit_price=req["unit_price"],
            group_value_inr=req["group_value_inr"],
            suggested_supplier_ids=req["suggested_supplier_ids"],
            suggested_details=req["suggested_details"],
            po_id=po_lookup.get(req["group_id"], ""),
            policy_version=req["policy_version"],
            reason=req["reason"],
            buyer_status="PENDING",
        ))
    db.commit()

    return {
        "batch_id": batch_id,
        "policy_version": engine.policy.version,
        "total_lines": len(result["line_results"]),
        "total_requirements": len(result["requirements"]),
        "total_pos": len(result["purchase_orders"]),
        "line_results": result["line_results"],
        "requirements": [
            {**r, "first_created": r["first_created"].strftime("%Y-%m-%d %H:%M"), "po_id": po_lookup.get(r["group_id"], "")}
            for r in result["requirements"]
        ],
        "purchase_orders": [
            {**p, "first_created": p["first_created"].strftime("%Y-%m-%d %H:%M")}
            for p in result["purchase_orders"]
        ],
    }


@router.get("/export/decisions-csv")
def export_decisions_csv(db: Session = Depends(get_db)):
    lines = db.query(PRLineRecord).order_by(PRLineRecord.id.asc()).all()
    if not lines:
        raise HTTPException(status_code=404, detail="No processed PR lines found")

    reqs = {r.group_id: r for r in db.query(RequirementRecord).all()}
    rows = []
    for l in lines:
        req = reqs.get(l.group_id)
        if not req:
            rows.append({
                "pr_no": l.pr_no,
                "pr_line": l.pr_line,
                "resolved_item_code": "",
                "group_id": "",
                "po_id": "",
                "decision": "NOT_PROCESSED",
                "route": "NOT_PROCESSED",
                "supplier_id": "",
                "unit_price": "",
                "group_value_inr": "",
                "suggested_supplier_ids": "",
                "policy_version": _get_active_policy(db).version,
                "reason": f"Invalid line (plant={l.plant}, qty={l.quantity})",
            })
        else:
            rows.append({
                "pr_no": l.pr_no,
                "pr_line": l.pr_line,
                "resolved_item_code": l.resolved_item_code,
                "group_id": req.group_id,
                "po_id": req.po_id or "",
                "decision": req.decision,
                "route": req.route,
                "supplier_id": req.supplier_id or "",
                "unit_price": req.unit_price if req.unit_price is not None else "",
                "group_value_inr": req.group_value_inr,
                "suggested_supplier_ids": req.suggested_supplier_ids or "",
                "policy_version": req.policy_version,
                "reason": req.reason,
            })

    out_df = pd.DataFrame(rows)
    buf = io.StringIO()
    out_df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pr_decisions.csv"},
    )


@router.get("/preferred-suppliers")
def list_preferred_suppliers(as_of_date: str = "2026-11-19", db: Session = Depends(get_db)):
    master = load_master_data()
    policy = _get_active_policy(db)
    contracts, clines = _get_contracts_data(db)
    ranker = SupplierRanker(
        suppliers=master["suppliers"],
        po_history=master["po_history"],
        contracts=contracts,
        contract_lines=clines,
        excluded_statuses=policy.excluded_supplier_statuses,
        weights=policy.supplier_ranking,
    )
    as_of_dt = datetime.fromisoformat(as_of_date)
    all_rankings = {}
    for item in master["items"]:
        code = item["item_code"]
        all_rankings[code] = {
            "item_code": code,
            "description": item["description"],
            "category": item["category"],
            "suppliers": ranker.rank_for_item(code, as_of_dt)[:3],
        }
    return {"as_of_date": as_of_date, "items": all_rankings}


@router.get("/export/preferred-suppliers-csv")
def export_preferred_suppliers_csv(as_of_date: str = "2026-11-19", db: Session = Depends(get_db)):
    data = list_preferred_suppliers(as_of_date=as_of_date, db=db)
    rows = []
    for code, info in data["items"].items():
        for s in info["suppliers"][:3]:
            rows.append({
                "item_code": code,
                "rank": s["rank"],
                "supplier_id": s["supplier_id"],
                "reference_price": s["reference_price"],
            })
    out_df = pd.DataFrame(rows, columns=["item_code", "rank", "supplier_id", "reference_price"])
    buf = io.StringIO()
    out_df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=preferred_suppliers_{as_of_date}.csv"},
    )


# --- 2. POLICY AS DATA ---

@router.get("/policies")
def get_policies(db: Session = Depends(get_db)):
    versions = db.query(PolicyVersion).order_by(PolicyVersion.id.desc()).all()
    return [
        {
            "id": v.id,
            "version": v.version,
            "title": v.title,
            "effective_date": v.effective_date,
            "status": v.status,
            "diff_summary": v.diff_summary,
            "rules_json": v.rules_json,
            "approved_by": v.approved_by,
            "approved_at": v.approved_at.isoformat() if v.approved_at else None,
        }
        for v in versions
    ]


@router.post("/policies/propose")
async def propose_policy_update(
    amendment_text: Optional[str] = Form(None),
    new_version: Optional[str] = Form(None),
    effective_date: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    text = amendment_text or ""
    if file is not None:
        raw = await file.read()
        if file.filename.lower().endswith(".pdf"):
            text += "\n" + extract_text_from_pdf(raw)
        else:
            text += "\n" + raw.decode("utf-8", errors="ignore")

    if not text.strip():
        raise HTTPException(status_code=400, detail="Provide amendment text or upload a policy document.")

    active_policy = _get_active_policy(db)
    updated_policy, diffs = parse_policy_amendment(
        current_policy=active_policy,
        amendment_text=text,
        new_version=new_version or "",
        effective_date=effective_date or "",
    )

    # Replace existing draft with same version if present
    existing = db.query(PolicyVersion).filter_by(version=updated_policy.version).first()
    if existing:
        db.delete(existing)
        db.commit()

    draft = PolicyVersion(
        version=updated_policy.version,
        title=updated_policy.title,
        effective_date=updated_policy.effective_date,
        status="DRAFT",
        source_text=text,
        diff_summary=diffs,
        rules_json=updated_policy.model_dump(),
        created_by="Procurement Manager",
    )
    db.add(draft)
    db.add(AuditLog(
        entity_type="POLICY",
        entity_id=updated_policy.version,
        action="PROPOSED",
        actor="Procurement Manager",
        comment="; ".join(diffs),
    ))
    db.commit()
    db.refresh(draft)

    return {
        "id": draft.id,
        "version": draft.version,
        "effective_date": draft.effective_date,
        "status": draft.status,
        "diff_summary": draft.diff_summary,
        "rules_json": draft.rules_json,
    }


@router.post("/policies/{version}/approve")
def approve_policy_version(
    version: str,
    approver: str = "Head of Procurement",
    db: Session = Depends(get_db),
):
    target = db.query(PolicyVersion).filter_by(version=version).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"Policy {version} not found")

    for pv in db.query(PolicyVersion).filter_by(status="ACTIVE").all():
        pv.status = "ARCHIVED"

    target.status = "ACTIVE"
    target.approved_by = approver
    target.approved_at = datetime.utcnow()
    db.add(AuditLog(
        entity_type="POLICY",
        entity_id=version,
        action="ACTIVATED",
        actor=approver,
        comment=f"Activated policy {version} effective {target.effective_date}",
    ))
    db.commit()
    return {"status": "ACTIVE", "version": version, "effective_date": target.effective_date}


# --- 3. CHAT INTAKE ---

class ChatIntakeRequest(BaseModel):
    messages: list[dict]
    draft_state: dict = {}
    requester: str = "Employee"
    department: str = "Production"
    current_date: str = "2026-11-19"


@router.post("/chat/intake")
def chat_intake_turn(payload: ChatIntakeRequest, db: Session = Depends(get_db)):
    master = load_master_data()
    res = process_chat_turn(
        messages=payload.messages,
        draft_state=payload.draft_state,
        items=master["items"],
        current_date_str=payload.current_date,
    )

    created_decision = None
    if res["confirmed"]:
        st = res["draft_state"]
        pr_no = f"PR-26-{datetime.utcnow().strftime('%H%M%S')}"
        now_str = f"{payload.current_date} {datetime.utcnow().strftime('%H:%M')}"
        line_dict = {
            "pr_no": pr_no,
            "pr_line": 1,
            "created_at": now_str,
            "requester": payload.requester,
            "department": payload.department,
            "plant": st["plant"],
            "item_code": st.get("item_code"),
            "description": st.get("description") or st.get("item_code"),
            "quantity": float(st["quantity"]),
            "uom": st.get("uom", "NOS"),
            "estimated_unit_price": st.get("estimated_unit_price"),
            "need_by_date": st["need_by_date"],
            "urgency": st.get("urgency", "Normal"),
        }
        engine = _build_engine(db)
        eval_out = engine.process_batch([line_dict])
        created_decision = eval_out["line_results"][0]

        if eval_out["requirements"]:
            req = eval_out["requirements"][0]
            db.add(RequirementRecord(
                batch_id="CHAT_INTAKE",
                group_id=f"REQ-CHAT-{datetime.utcnow().strftime('%H%M%S')}",
                item_code=req["item_code"],
                description=req["description"],
                plant=req["plant"],
                first_created=now_str,
                total_qty=req["total_qty"],
                is_emergency=req["is_emergency"],
                decision=req["decision"],
                route=req["route"],
                supplier_id=req["supplier_id"],
                unit_price=req["unit_price"],
                group_value_inr=req["group_value_inr"],
                suggested_supplier_ids=req["suggested_supplier_ids"],
                suggested_details=req["suggested_details"],
                po_id=created_decision["po_id"],
                policy_version=req["policy_version"],
                reason=req["reason"],
                buyer_status="PENDING",
            ))
            db.commit()

    return {**res, "created_decision": created_decision}


# --- 4. BUYER WORKSPACE (APPROVE / OVERRIDE / AUDIT LOG) ---

@router.get("/requirements")
def get_requirements(db: Session = Depends(get_db)):
    reqs = db.query(RequirementRecord).order_by(RequirementRecord.id.asc()).all()
    lines = db.query(PRLineRecord).all()
    lines_by_gid: dict[str, list] = {}
    for l in lines:
        lines_by_gid.setdefault(l.group_id, []).append({
            "pr_no": l.pr_no,
            "pr_line": l.pr_line,
            "created_at": l.created_at,
            "requester": l.requester,
            "department": l.department,
            "quantity": l.quantity,
            "uom": l.uom,
            "need_by_date": l.need_by_date,
            "urgency": l.urgency,
        })

    return [
        {
            "id": r.id,
            "group_id": r.group_id,
            "item_code": r.item_code,
            "description": r.description,
            "plant": r.plant,
            "first_created": r.first_created,
            "total_qty": r.total_qty,
            "is_emergency": r.is_emergency,
            "decision": r.decision,
            "route": r.route,
            "supplier_id": r.supplier_id,
            "unit_price": r.unit_price,
            "group_value_inr": r.group_value_inr,
            "suggested_supplier_ids": r.suggested_supplier_ids,
            "suggested_details": r.suggested_details,
            "po_id": r.po_id,
            "policy_version": r.policy_version,
            "reason": r.reason,
            "buyer_status": r.buyer_status,
            "override_comment": r.override_comment,
            "actioned_by": r.actioned_by,
            "actioned_at": r.actioned_at.isoformat() if r.actioned_at else None,
            "pr_lines": lines_by_gid.get(r.group_id, []),
        }
        for r in reqs
    ]


class BuyerActionRequest(BaseModel):
    buyer_name: str = "Senior Buyer"
    comment: Optional[str] = None
    new_route: Optional[str] = None
    new_supplier_id: Optional[str] = None


@router.post("/requirements/{req_id}/approve")
def approve_requirement(req_id: int, payload: BuyerActionRequest, db: Session = Depends(get_db)):
    req = db.query(RequirementRecord).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Requirement not found")
    req.buyer_status = "APPROVED"
    req.actioned_by = payload.buyer_name
    req.actioned_at = datetime.utcnow()
    db.add(AuditLog(
        entity_type="REQUIREMENT",
        entity_id=req.group_id,
        action="APPROVED",
        actor=payload.buyer_name,
        comment=f"Approved {req.route} ({req.po_id or 'Sourcing'})",
    ))
    db.commit()
    return {"status": "APPROVED", "group_id": req.group_id}


@router.post("/requirements/{req_id}/override")
def override_requirement(req_id: int, payload: BuyerActionRequest, db: Session = Depends(get_db)):
    if not payload.comment or not payload.comment.strip():
        raise HTTPException(status_code=400, detail="Override requires a mandatory written reason (Sec 8.1).")
    req = db.query(RequirementRecord).filter_by(id=req_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Requirement not found")
    old_route = req.route
    if payload.new_route:
        req.route = payload.new_route
        req.decision = "PO" if payload.new_route.endswith("_PO") else "SOURCING_REQUEST"
    if payload.new_supplier_id:
        req.supplier_id = payload.new_supplier_id
    req.buyer_status = "OVERRIDDEN"
    req.override_comment = payload.comment.strip()
    req.actioned_by = payload.buyer_name
    req.actioned_at = datetime.utcnow()
    db.add(AuditLog(
        entity_type="REQUIREMENT",
        entity_id=req.group_id,
        action="OVERRIDDEN",
        actor=payload.buyer_name,
        comment=payload.comment.strip(),
        details={"old_route": old_route, "new_route": req.route, "supplier_id": req.supplier_id},
    ))
    db.commit()
    return {"status": "OVERRIDDEN", "group_id": req.group_id}


@router.get("/audit-logs")
def get_audit_logs(db: Session = Depends(get_db)):
    logs = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(100).all()
    return [
        {
            "id": l.id,
            "entity_type": l.entity_type,
            "entity_id": l.entity_id,
            "action": l.action,
            "actor": l.actor,
            "comment": l.comment,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


# --- 5. CONTRACTS ---

@router.get("/contracts")
def list_contracts(db: Session = Depends(get_db)):
    contracts = db.query(Contract).all()
    lines = db.query(ContractLine).all()
    lines_by_cid: dict[str, list] = {}
    for l in lines:
        lines_by_cid.setdefault(l.contract_id, []).append({
            "item_code": l.item_code,
            "description": l.description,
            "uom": l.uom,
            "rate": l.rate,
        })
    return [
        {
            "contract_id": c.contract_id,
            "supplier_id": c.supplier_id,
            "supplier_name": c.supplier_name,
            "start_date": c.start_date,
            "end_date": c.end_date,
            "value_cap": c.value_cap,
            "consumed_value": c.consumed_value,
            "remaining_value": c.value_cap - c.consumed_value,
            "source": c.source,
            "lines": lines_by_cid.get(c.contract_id, []),
        }
        for c in contracts
    ]


@router.post("/contracts/upload-pdf")
async def upload_contract_pdf(file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = await file.read()
    parsed = parse_contract_pdf(raw)
    cid = parsed["contract_id"]
    existing = db.query(Contract).filter_by(contract_id=cid).first()
    if existing:
        db.query(ContractLine).filter_by(contract_id=cid).delete()
        db.delete(existing)
        db.commit()

    db.add(Contract(
        contract_id=cid,
        supplier_id=parsed["supplier_id"],
        supplier_name=parsed["supplier_name"],
        start_date=parsed["start_date"],
        end_date=parsed["end_date"],
        value_cap=float(parsed["value_cap"]),
        consumed_value=0.0,
        source="PDF_UPLOAD",
    ))
    for l in parsed["lines"]:
        db.add(ContractLine(
            contract_id=cid,
            item_code=l["item_code"],
            description=l["description"],
            uom=l["uom"],
            rate=float(l["rate"]),
        ))
    db.add(AuditLog(
        entity_type="CONTRACT",
        entity_id=cid,
        action="PDF_UPLOADED",
        actor="Buyer",
        comment=f"Extracted contract {cid} ({parsed['supplier_name']}) with {len(parsed['lines'])} items",
    ))
    db.commit()
    return parsed
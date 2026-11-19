from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, JSON, ForeignKey
from app.database import Base


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(String(32), unique=True, index=True, nullable=False)
    title = Column(String(255), nullable=False)
    effective_date = Column(String(20), nullable=False)  # YYYY-MM-DD
    status = Column(String(20), default="DRAFT", index=True)  # DRAFT | ACTIVE | ARCHIVED
    source_text = Column(Text, default="")
    diff_summary = Column(JSON, default=list)  # Plain-language bullet list of changes
    rules_json = Column(JSON, nullable=False)
    created_by = Column(String(100), default="system")
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Contract(Base):
    __tablename__ = "contracts"

    id = Column(Integer, primary_key=True, index=True)
    contract_id = Column(String(64), unique=True, index=True, nullable=False)
    supplier_id = Column(String(32), nullable=False)
    supplier_name = Column(String(255), nullable=False)
    start_date = Column(String(20), nullable=False)
    end_date = Column(String(20), nullable=False)
    value_cap = Column(Float, nullable=False)
    consumed_value = Column(Float, default=0.0)
    source = Column(String(32), default="REGISTER")  # REGISTER | PDF_UPLOAD


class ContractLine(Base):
    __tablename__ = "contract_lines"

    id = Column(Integer, primary_key=True, index=True)
    contract_id = Column(String(64), ForeignKey("contracts.contract_id"), index=True, nullable=False)
    item_code = Column(String(32), index=True, nullable=False)
    description = Column(String(255), nullable=False)
    uom = Column(String(32), nullable=False)
    rate = Column(Float, nullable=False)


class PRLineRecord(Base):
    __tablename__ = "pr_lines"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(String(64), index=True, default="MANUAL_CHAT")
    pr_no = Column(String(64), index=True, nullable=False)
    pr_line = Column(Integer, nullable=False)
    created_at = Column(String(32), nullable=False)
    requester = Column(String(100), default="Employee")
    department = Column(String(100), default="General")
    plant = Column(String(20), nullable=False)
    item_code = Column(String(32), nullable=True)
    resolved_item_code = Column(String(32), nullable=True)
    description = Column(String(255), nullable=False)
    quantity = Column(Float, nullable=False)
    uom = Column(String(32), default="NOS")
    estimated_unit_price = Column(Float, nullable=True)
    need_by_date = Column(String(20), nullable=False)
    urgency = Column(String(20), default="Normal")
    group_id = Column(String(32), index=True, nullable=True)


class RequirementRecord(Base):
    __tablename__ = "requirements"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(String(64), index=True, default="LATEST")
    group_id = Column(String(32), index=True, nullable=False)
    item_code = Column(String(32), nullable=False)
    description = Column(String(255), nullable=False)
    plant = Column(String(20), nullable=False)
    first_created = Column(String(32), nullable=False)
    total_qty = Column(Float, nullable=False)
    is_emergency = Column(Boolean, default=False)
    decision = Column(String(32), nullable=False)
    route = Column(String(32), nullable=False)
    supplier_id = Column(String(32), default="")
    unit_price = Column(Float, nullable=True)
    group_value_inr = Column(Float, nullable=False)
    suggested_supplier_ids = Column(String(255), default="")
    suggested_details = Column(JSON, default=list)
    po_id = Column(String(32), default="")
    policy_version = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False)
    buyer_status = Column(String(32), default="PENDING")  # PENDING | APPROVED | OVERRIDDEN
    override_comment = Column(Text, nullable=True)
    actioned_by = Column(String(100), nullable=True)
    actioned_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String(32), nullable=False)  # REQUIREMENT | POLICY | CONTRACT | PR
    entity_id = Column(String(64), nullable=False)
    action = Column(String(64), nullable=False)
    actor = Column(String(100), nullable=False)
    comment = Column(Text, default="")
    details = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
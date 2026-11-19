import os
import glob
import pandas as pd
from sqlalchemy.orm import Session
from app.config import settings
from app.models import PolicyVersion, Contract, ContractLine
from app.schemas import POLICY_V5_DEFAULT
from app.services.pdf_contract_parser import parse_contract_pdf


def _read_table(base_path_no_ext: str) -> pd.DataFrame:
    if os.path.exists(base_path_no_ext + ".xlsx"):
        return pd.read_excel(base_path_no_ext + ".xlsx")
    if os.path.exists(base_path_no_ext + ".csv"):
        return pd.read_csv(base_path_no_ext + ".csv")
    raise FileNotFoundError(f"Neither .xlsx nor .csv found for {base_path_no_ext}")


def load_master_data() -> dict:
    d = settings.data_dir
    items_df = _read_table(os.path.join(d, "master_data", "item_master"))
    suppliers_df = _read_table(os.path.join(d, "master_data", "supplier_master"))
    plants_df = _read_table(os.path.join(d, "master_data", "plants"))
    po_df = _read_table(os.path.join(d, "master_data", "po_history"))

    po_df["po_date"] = pd.to_datetime(po_df["po_date"]).dt.strftime("%Y-%m-%d")
    po_records = po_df.to_dict(orient="records")
    for p in po_records:
        p["po_date_dt"] = pd.to_datetime(p["po_date"]).to_pydatetime()

    return {
        "items": items_df.to_dict(orient="records"),
        "suppliers": suppliers_df.to_dict(orient="records"),
        "plants": plants_df.to_dict(orient="records"),
        "po_history": po_records,
    }


def seed_database(db: Session):
    # 1. Seed Policy v5.0 if not present
    existing_policy = db.query(PolicyVersion).filter_by(version="v5.0").first()
    if not existing_policy:
        pv = PolicyVersion(
            version=POLICY_V5_DEFAULT.version,
            title=POLICY_V5_DEFAULT.title,
            effective_date=POLICY_V5_DEFAULT.effective_date,
            status="ACTIVE",
            source_text="Corvo Engineering Pvt Ltd: Procurement Policy (PR to PO) v5.0",
            diff_summary=["Initial baseline policy v5.0 effective 2026-04-01."],
            rules_json=POLICY_V5_DEFAULT.model_dump(),
            created_by="system",
            approved_by="Head of Procurement",
        )
        db.add(pv)
        db.commit()

    # 2. Seed contracts from register + PDFs if empty
    if db.query(Contract).count() == 0:
        d = settings.data_dir
        reg_df = _read_table(os.path.join(d, "contracts", "contract_register"))
        lines_df = _read_table(os.path.join(d, "contracts", "contract_lines"))

        for _, r in reg_df.iterrows():
            db.add(Contract(
                contract_id=str(r["contract_id"]).strip(),
                supplier_id=str(r["supplier_id"]).strip(),
                supplier_name=str(r["supplier_name"]).strip(),
                start_date=pd.to_datetime(r["start_date"]).strftime("%Y-%m-%d"),
                end_date=pd.to_datetime(r["end_date"]).strftime("%Y-%m-%d"),
                value_cap=float(r["value_cap"]),
                consumed_value=float(r["consumed_value"]),
                source="REGISTER",
            ))

        for _, l in lines_df.iterrows():
            db.add(ContractLine(
                contract_id=str(l["contract_id"]).strip(),
                item_code=str(l["item_code"]).strip(),
                description=str(l["description"]).strip(),
                uom=str(l["uom"]).strip(),
                rate=float(l["rate"]),
            ))
        db.commit()

        # Ingest PDF-only contracts signed after register update
        pdf_dir = os.path.join(d, "contracts", "pdf")
        if os.path.isdir(pdf_dir):
            for pdf_path in sorted(glob.glob(os.path.join(pdf_dir, "*.pdf"))):
                with open(pdf_path, "rb") as f:
                    parsed = parse_contract_pdf(f.read())
                cid = parsed["contract_id"]
                if not db.query(Contract).filter_by(contract_id=cid).first():
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
                    for line in parsed["lines"]:
                        db.add(ContractLine(
                            contract_id=cid,
                            item_code=line["item_code"],
                            description=line["description"],
                            uom=line["uom"],
                            rate=float(line["rate"]),
                        ))
                    db.commit()
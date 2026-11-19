"use client";

import React, { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type Tab = "chat" | "buyer" | "policy" | "contracts";

export default function ProcurementPortal() {
  const [activeTab, setActiveTab] = useState<Tab>("buyer");
  const [policies, setPolicies] = useState<any[]>([]);
  const [requirements, setRequirements] = useState<any[]>([]);
  const [contracts, setContracts] = useState<any[]>([]);
  const [auditLogs, setAuditLogs] = useState<any[]>([]);
  const [prefSuppliers, setPrefSuppliers] = useState<any>({});
  const [asOfDate, setAsOfDate] = useState("2026-11-19");

  // Chat state
  const [chatMessages, setChatMessages] = useState<any[]>([
    {
      role: "assistant",
      content:
        "Hi! Tell me what you need to purchase (for example: 'need 200 nitrile gloves for Chakan by next Friday').",
    },
  ]);
  const [chatInput, setChatInput] = useState("");
  const [draftState, setDraftState] = useState<any>({});
  const [chatLoading, setChatLoading] = useState(false);

  // Batch & Policy upload states
  const [batchFile, setBatchFile] = useState<File | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchSummary, setBatchSummary] = useState<any>(null);

  const [amendmentText, setAmendmentText] = useState("");
  const [amendmentFile, setAmendmentFile] = useState<File | null>(null);
  const [newVersion, setNewVersion] = useState("");
  const [effectiveDate, setEffectiveDate] = useState("");
  const [proposedDraft, setProposedDraft] = useState<any>(null);
  const [policyLoading, setPolicyLoading] = useState(false);

  // Override modal state
  const [overrideTarget, setOverrideTarget] = useState<any>(null);
  const [overrideRoute, setOverrideRoute] = useState("");
  const [overrideSupplier, setOverrideSupplier] = useState("");
  const [overrideComment, setOverrideComment] = useState("");

  // Contract PDF upload
  const [contractPdf, setContractPdf] = useState<File | null>(null);

  const activePolicy = policies.find((p) => p.status === "ACTIVE");

  async function refreshAll() {
    try {
      const [pRes, rRes, cRes, aRes] = await Promise.all([
        fetch(`${API_BASE}/api/policies`),
        fetch(`${API_BASE}/api/requirements`),
        fetch(`${API_BASE}/api/contracts`),
        fetch(`${API_BASE}/api/audit-logs`),
      ]);
      if (pRes.ok) setPolicies(await pRes.json());
      if (rRes.ok) setRequirements(await rRes.json());
      if (cRes.ok) setContracts(await cRes.json());
      if (aRes.ok) setAuditLogs(await aRes.json());
    } catch (e) {
      console.error("Failed to fetch initial state", e);
    }
  }

  async function fetchPreferredSuppliers(dateStr: string) {
    const res = await fetch(`${API_BASE}/api/preferred-suppliers?as_of_date=${dateStr}`);
    if (res.ok) {
      const data = await res.json();
      setPrefSuppliers(data.items || {});
    }
  }

  useEffect(() => {
    refreshAll();
    fetchPreferredSuppliers(asOfDate);
  }, []);

  // --- Handlers ---
  async function handleSendChat(e: React.FormEvent) {
    e.preventDefault();
    if (!chatInput.trim()) return;
    const nextMsgs = [...chatMessages, { role: "user", content: chatInput }];
    setChatMessages(nextMsgs);
    setChatInput("");
    setChatLoading(true);

    try {
      const res = await fetch(`${API_BASE}/api/chat/intake`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: nextMsgs,
          draft_state: draftState,
          requester: "Shopfloor Requester",
          department: "Production",
        }),
      });
      const data = await res.json();
      let assistantReply = data.reply;
      if (data.confirmed && data.created_decision) {
        const d = data.created_decision;
        assistantReply += `\n\n**Decision Result:** ${d.route} | Group: ${d.group_id} | Value: ₹${Number(
          d.group_value_inr
        ).toLocaleString("en-IN")} | ${d.supplier_id ? `Supplier: ${d.supplier_id}` : `Suggested: ${d.suggested_supplier_ids}`}\n*Rule:* ${d.reason}`;
        setDraftState({});
        refreshAll();
      } else {
        setDraftState(data.draft_state || {});
      }
      setChatMessages([...nextMsgs, { role: "assistant", content: assistantReply }]);
    } finally {
      setChatLoading(false);
    }
  }

  async function handleProcessBatch() {
    if (!batchFile) return;
    setBatchLoading(true);
    try {
      const fd = new FormData();
      fd.append("file", batchFile);
      const res = await fetch(`${API_BASE}/api/process-batch`, {
        method: "POST",
        body: fd,
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setBatchSummary(data);
      await refreshAll();
    } catch (err: any) {
      alert("Batch processing failed: " + err.message);
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleApproveReq(reqId: number) {
    await fetch(`${API_BASE}/api/requirements/${reqId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ buyer_name: "Senior Buyer" }),
    });
    refreshAll();
  }

  async function handleSubmitOverride() {
    if (!overrideTarget || !overrideComment.trim()) {
      alert("Written override reason is mandatory.");
      return;
    }
    await fetch(`${API_BASE}/api/requirements/${overrideTarget.id}/override`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        buyer_name: "Senior Buyer",
        comment: overrideComment,
        new_route: overrideRoute || undefined,
        new_supplier_id: overrideSupplier || undefined,
      }),
    });
    setOverrideTarget(null);
    setOverrideComment("");
    setOverrideRoute("");
    setOverrideSupplier("");
    refreshAll();
  }

  async function handleProposePolicy() {
    if (!amendmentText.trim() && !amendmentFile) return;
    setPolicyLoading(true);
    try {
      const fd = new FormData();
      if (amendmentText.trim()) fd.append("amendment_text", amendmentText);
      if (newVersion.trim()) fd.append("new_version", newVersion);
      if (effectiveDate.trim()) fd.append("effective_date", effectiveDate);
      if (amendmentFile) fd.append("file", amendmentFile);

      const res = await fetch(`${API_BASE}/api/policies/propose`, {
        method: "POST",
        body: fd,
      });
      if (!res.ok) throw new Error(await res.text());
      const draft = await res.json();
      setProposedDraft(draft);
      refreshAll();
    } catch (err: any) {
      alert("Policy parse error: " + err.message);
    } finally {
      setPolicyLoading(false);
    }
  }

  async function handleActivatePolicy(version: string) {
    await fetch(`${API_BASE}/api/policies/${encodeURIComponent(version)}/approve`, {
      method: "POST",
    });
    setProposedDraft(null);
    setAmendmentText("");
    setAmendmentFile(null);
    refreshAll();
  }

  async function handleUploadContractPdf() {
    if (!contractPdf) return;
    const fd = new FormData();
    fd.append("file", contractPdf);
    const res = await fetch(`${API_BASE}/api/contracts/upload-pdf`, {
      method: "POST",
      body: fd,
    });
    if (res.ok) {
      setContractPdf(null);
      refreshAll();
    } else {
      alert("Failed to extract PDF contract.");
    }
  }

  return (
    <div className="min-h-screen flex flex-col">
      {/* Top Navbar */}
      <header className="bg-slate-900 text-white px-6 py-4 flex flex-wrap items-center justify-between gap-4 shadow">
        <div>
          <h1 className="text-xl font-bold tracking-tight">
            Corvo Engineering (Policy-Driven PR-to-PO Engine)
          </h1>
          <p className="text-xs text-slate-300">
            Active Policy:{" "}
            <span className="font-semibold text-emerald-400">
              {activePolicy ? `${activePolicy.version} (Effective ${activePolicy.effective_date})` : "Loading..."}
            </span>
          </p>
        </div>

        <nav className="flex gap-2">
          {[
            { id: "buyer", label: "1. Buyer Workspace & Batch" },
            { id: "chat", label: "2. Requester Chat Intake" },
            { id: "policy", label: "3. Policy as Data" },
            { id: "contracts", label: "4. Contracts & Preferred Suppliers" },
          ].map((t) => (
            <button
              key={t.id}
              onClick={() => setActiveTab(t.id as Tab)}
              className={`px-3 py-2 rounded-md text-sm font-medium transition ${
                activeTab === t.id
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="flex-1 max-w-7xl w-full mx-auto p-6 space-y-6">
        {/* TAB 1: BUYER WORKSPACE */}
        {activeTab === "buyer" && (
          <div className="space-y-6">
            {/* Primary Action Banner: Process Batch CSV */}
            <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm flex flex-wrap items-center justify-between gap-4">
              <div>
                <h2 className="text-base font-semibold">Process PR Batch (CSV / XLSX)</h2>
                <p className="text-sm text-slate-500">
                  Evaluates all lines under active policy <strong>{activePolicy?.version}</strong>, clubs split PRs, checks contract ceilings, and consolidates POs.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <input
                  type="file"
                  accept=".csv,.xlsx"
                  onChange={(e) => setBatchFile(e.target.files?.[0] || null)}
                  className="text-sm text-slate-600 file:mr-3 file:py-2 file:px-3 file:rounded-md file:border-0 file:bg-slate-100 file:text-slate-700 hover:file:bg-slate-200"
                />
                <button
                  onClick={handleProcessBatch}
                  disabled={!batchFile || batchLoading}
                  className="px-4 py-2 bg-indigo-600 text-white text-sm font-semibold rounded-md hover:bg-indigo-700 disabled:opacity-50"
                >
                  {batchLoading ? "Running Engine..." : "Run Policy Engine"}
                </button>
                {requirements.length > 0 && (
                  <a
                    href={`${API_BASE}/api/export/decisions-csv`}
                    className="px-4 py-2 bg-emerald-600 text-white text-sm font-semibold rounded-md hover:bg-emerald-700"
                  >
                    Download Decisions CSV
                  </a>
                )}
              </div>
            </div>

            {batchSummary && (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="bg-white p-4 rounded-lg border border-slate-200">
                  <div className="text-xs text-slate-500">Policy Applied</div>
                  <div className="text-xl font-bold text-indigo-600">{batchSummary.policy_version}</div>
                </div>
                <div className="bg-white p-4 rounded-lg border border-slate-200">
                  <div className="text-xs text-slate-500">Input PR Lines</div>
                  <div className="text-xl font-bold">{batchSummary.total_lines}</div>
                </div>
                <div className="bg-white p-4 rounded-lg border border-slate-200">
                  <div className="text-xs text-slate-500">Clubbed Requirements</div>
                  <div className="text-xl font-bold">{batchSummary.total_requirements}</div>
                </div>
                <div className="bg-white p-4 rounded-lg border border-slate-200">
                  <div className="text-xs text-slate-500">Consolidated POs</div>
                  <div className="text-xl font-bold text-emerald-600">{batchSummary.total_pos}</div>
                </div>
              </div>
            )}

            {/* Requirements Queue */}
            <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
              <div className="px-5 py-4 border-b border-slate-200 flex items-center justify-between">
                <h3 className="font-semibold">Requirement Decisions ({requirements.length})</h3>
                <span className="text-xs text-slate-500">
                  Each card shows matched PR lines, value, route, clause, and buyer actions
                </span>
              </div>

              {requirements.length === 0 ? (
                <div className="p-8 text-center text-sm text-slate-500">
                  Upload <code>sample_pr_batch.csv</code> above or raise a PR via Chat Intake to populate the queue.
                </div>
              ) : (
                <div className="divide-y divide-slate-200">
                  {requirements.map((req) => (
                    <div key={req.id} className="p-5 flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                      <div className="space-y-1.5 max-w-3xl">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-xs font-bold px-2 py-0.5 rounded bg-slate-100">
                            {req.group_id}
                          </span>
                          <span
                            className={`text-xs font-bold px-2.5 py-0.5 rounded-full ${
                              req.decision === "PO"
                                ? "bg-emerald-100 text-emerald-800"
                                : "bg-amber-100 text-amber-800"
                            }`}
                          >
                            {req.route}
                          </span>
                          {req.po_id && (
                            <span className="font-mono text-xs font-semibold px-2 py-0.5 rounded bg-indigo-50 text-indigo-700 border border-indigo-200">
                              {req.po_id}
                            </span>
                          )}
                          <span className="text-xs text-slate-500">
                            Plant: <strong>{req.plant}</strong> • Qty: <strong>{req.total_qty}</strong> • Value:{" "}
                            <strong>₹{Number(req.group_value_inr).toLocaleString("en-IN")}</strong>
                          </span>
                        </div>

                        <div className="font-medium text-slate-900">
                          {req.item_code} — {req.description}
                        </div>

                        <div className="text-xs text-slate-600">
                          {req.decision === "PO" ? (
                            <>
                              Award Supplier: <strong>{req.supplier_id}</strong> @{" "}
                              <strong>₹{req.unit_price}</strong>
                            </>
                          ) : (
                            <>
                              Suggested Suppliers:{" "}
                              <strong>{req.suggested_supplier_ids || "None (New Item)"}</strong>
                            </>
                          )}
                        </div>

                        <div className="text-xs bg-slate-50 border border-slate-200 rounded p-2 text-slate-700">
                          <strong>Why:</strong> {req.reason}
                        </div>

                        {req.pr_lines?.length > 0 && (
                          <div className="text-xs text-slate-500">
                            Clubbed PRs:{" "}
                            {req.pr_lines
                              .map((l: any) => `${l.pr_no}/${l.pr_line} (${l.quantity} ${l.uom})`)
                              .join(", ")}
                          </div>
                        )}

                        {req.override_comment && (
                          <div className="text-xs text-rose-700 bg-rose-50 px-2.5 py-1 rounded">
                            Override by {req.actioned_by}: &ldquo;{req.override_comment}&rdquo;
                          </div>
                        )}
                      </div>

                      <div className="flex items-center gap-2 shrink-0">
                        {req.buyer_status === "PENDING" ? (
                          <>
                            <button
                              onClick={() => handleApproveReq(req.id)}
                              className="px-3.5 py-2 bg-emerald-600 text-white text-xs font-semibold rounded-md hover:bg-emerald-700"
                            >
                              Approve {req.decision === "PO" ? "PO" : "Sourcing"}
                            </button>
                            <button
                              onClick={() => setOverrideTarget(req)}
                              className="px-3.5 py-2 bg-white border border-slate-300 text-slate-700 text-xs font-semibold rounded-md hover:bg-slate-50"
                            >
                              Override
                            </button>
                          </>
                        ) : (
                          <span
                            className={`text-xs font-bold px-3 py-1.5 rounded-md ${
                              req.buyer_status === "APPROVED"
                                ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                                : "bg-rose-50 text-rose-700 border border-rose-200"
                            }`}
                          >
                            {req.buyer_status} ({req.actioned_by})
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Audit Trail */}
            {auditLogs.length > 0 && (
              <div className="bg-white rounded-xl border border-slate-200 p-5">
                <h3 className="font-semibold text-sm mb-3">Governance & Override Audit Trail</h3>
                <div className="space-y-1.5 text-xs text-slate-600 max-h-48 overflow-y-auto">
                  {auditLogs.map((log) => (
                    <div key={log.id} className="flex justify-between border-b border-slate-100 pb-1">
                      <span>
                        <strong>[{log.entity_type} {log.entity_id}]</strong> {log.action} by{" "}
                        <strong>{log.actor}</strong> — {log.comment}
                      </span>
                      <span className="text-slate-400">{log.created_at}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* TAB 2: CHAT INTAKE */}
        {activeTab === "chat" && (
          <div className="max-w-3xl mx-auto bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden flex flex-col h-150">
            <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
              <h2 className="font-semibold">Conversational PR Intake</h2>
              <p className="text-xs text-slate-500">
                Describe what you need in plain English. The assistant resolves catalog items, asks for missing fields, and confirms before creating the PR.
              </p>
            </div>

            <div className="flex-1 p-5 overflow-y-auto space-y-3">
              {chatMessages.map((m, idx) => (
                <div
                  key={idx}
                  className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  <div
                    className={`max-w-[80%] rounded-xl px-4 py-3 text-sm whitespace-pre-line ${
                      m.role === "user"
                        ? "bg-indigo-600 text-white"
                        : "bg-slate-100 text-slate-800"
                    }`}
                  >
                    {m.content}
                  </div>
                </div>
              ))}
            </div>

            <form onSubmit={handleSendChat} className="p-4 border-t border-slate-200 flex gap-2">
              <input
                type="text"
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                placeholder="e.g., need 200 nitrile gloves for Chakan by next Friday..."
                className="flex-1 border border-slate-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              <button
                type="submit"
                disabled={chatLoading}
                className="px-4 py-2 bg-indigo-600 text-white text-sm font-semibold rounded-md hover:bg-indigo-700 disabled:opacity-50"
              >
                {chatLoading ? "..." : "Send"}
              </button>
            </form>
          </div>
        )}

        {/* TAB 3: POLICY AS DATA */}
        {activeTab === "policy" && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Left: Propose Policy Amendment */}
            <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm space-y-4">
              <div>
                <h2 className="font-semibold">Upload Policy Amendment or Describe Change</h2>
                <p className="text-xs text-slate-500">
                  Compiles policy updates into structured rules without code changes. Review the plain-language diff before activating.
                </p>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1">
                    New Version Label (optional)
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. v5.1"
                    value={newVersion}
                    onChange={(e) => setNewVersion(e.target.value)}
                    className="w-full border border-slate-300 rounded-md px-3 py-1.5 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1">
                    Effective Date (optional)
                  </label>
                  <input
                    type="date"
                    value={effectiveDate}
                    onChange={(e) => setEffectiveDate(e.target.value)}
                    className="w-full border border-slate-300 rounded-md px-3 py-1.5 text-sm"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  Upload Amendment Document (PDF / TXT)
                </label>
                <input
                  type="file"
                  accept=".pdf,.txt,.md"
                  onChange={(e) => setAmendmentFile(e.target.files?.[0] || null)}
                  className="text-sm text-slate-600"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  Or Describe Policy Change in Chat
                </label>
                <textarea
                  rows={4}
                  value={amendmentText}
                  onChange={(e) => setAmendmentText(e.target.value)}
                  placeholder="e.g., Effective 2026-11-19, for plant PUN1 lower Spot PO limit to INR 1,00,000 and require RFQ for all Office & IT items above INR 50,000..."
                  className="w-full border border-slate-300 rounded-md p-3 text-sm"
                />
              </div>

              <button
                onClick={handleProposePolicy}
                disabled={policyLoading}
                className="w-full py-2.5 bg-indigo-600 text-white text-sm font-semibold rounded-md hover:bg-indigo-700 disabled:opacity-50"
              >
                {policyLoading ? "Compiling Policy Diff..." : "Parse Amendment & Preview Changes"}
              </button>

              {proposedDraft && (
                <div className="p-4 bg-amber-50 border border-amber-300 rounded-lg space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-sm text-amber-900">
                      Draft {proposedDraft.version} (Effective {proposedDraft.effective_date})
                    </span>
                    <button
                      onClick={() => handleActivatePolicy(proposedDraft.version)}
                      className="px-3.5 py-1.5 bg-emerald-600 text-white text-xs font-bold rounded-md hover:bg-emerald-700"
                    >
                      Approve & Activate Live
                    </button>
                  </div>
                  <ul className="list-disc pl-5 text-xs text-amber-950 space-y-1">
                    {proposedDraft.diff_summary?.map((d: string, i: number) => (
                      <li key={i}>{d}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            {/* Right: Active Rules & Version History */}
            <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm space-y-4">
              <h2 className="font-semibold">
                Active Decision Rules ({activePolicy?.version})
              </h2>
              <div className="space-y-2 max-h-96 overflow-y-auto">
                {activePolicy?.rules_json?.decision_rules
                  ?.slice()
                  .sort((a: any, b: any) => a.priority - b.priority)
                  .map((r: any) => (
                    <div key={r.rule_id} className="p-3 rounded-lg border border-slate-200 bg-slate-50 text-xs">
                      <div className="flex justify-between font-bold text-slate-800">
                        <span>
                          {r.rule_id} ({r.clause}) — {r.route}
                        </span>
                        <span className="text-slate-400">Priority {r.priority}</span>
                      </div>
                      <p className="text-slate-600 mt-1">{r.description}</p>
                      <div className="font-mono text-[11px] text-indigo-700 mt-1">
                        IF{" "}
                        {r.conditions.length > 0
                          ? r.conditions.map((c: any) => `${c.field} ${c.op} ${JSON.stringify(c.value)}`).join(" AND ")
                          : "TRUE"}
                      </div>
                    </div>
                  ))}
              </div>

              <div className="border-t border-slate-200 pt-3">
                <h3 className="text-xs font-semibold text-slate-500 uppercase mb-2">Version History</h3>
                <div className="space-y-1.5">
                  {policies.map((p) => (
                    <div key={p.id} className="flex items-center justify-between text-xs bg-slate-50 px-3 py-2 rounded">
                      <span>
                        <strong>{p.version}</strong> (Effective {p.effective_date}) —{" "}
                        <span className="font-semibold">{p.status}</span>
                      </span>
                      {p.status !== "ACTIVE" && (
                        <button
                          onClick={() => handleActivatePolicy(p.version)}
                          className="text-indigo-600 font-semibold hover:underline"
                        >
                          Activate
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 4: CONTRACTS & PREFERRED SUPPLIERS */}
        {activeTab === "contracts" && (
          <div className="space-y-6">
            {/* Contracts Section */}
            <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <h2 className="font-semibold">Rate Contracts ({contracts.length})</h2>
                  <p className="text-xs text-slate-500">
                    Includes register contracts plus PDF-only contracts extracted automatically.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <input
                    type="file"
                    accept=".pdf"
                    onChange={(e) => setContractPdf(e.target.files?.[0] || null)}
                    className="text-xs text-slate-600"
                  />
                  <button
                    onClick={handleUploadContractPdf}
                    disabled={!contractPdf}
                    className="px-3.5 py-2 bg-indigo-600 text-white text-xs font-semibold rounded-md hover:bg-indigo-700 disabled:opacity-50"
                  >
                    Extract & Add Contract PDF
                  </button>
                </div>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="bg-slate-50 border-b border-slate-200 text-slate-600">
                    <tr>
                      <th className="p-2.5">Contract ID</th>
                      <th className="p-2.5">Supplier</th>
                      <th className="p-2.5">Validity</th>
                      <th className="p-2.5">Ceiling (INR)</th>
                      <th className="p-2.5">Remaining (INR)</th>
                      <th className="p-2.5">Source</th>
                      <th className="p-2.5">Items Covered</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {contracts.map((c) => (
                      <tr key={c.contract_id}>
                        <td className="p-2.5 font-mono font-semibold">{c.contract_id}</td>
                        <td className="p-2.5">
                          {c.supplier_id} — {c.supplier_name}
                        </td>
                        <td className="p-2.5">
                          {c.start_date} to {c.end_date}
                        </td>
                        <td className="p-2.5">₹{Number(c.value_cap).toLocaleString("en-IN")}</td>
                        <td className="p-2.5 font-semibold text-emerald-700">
                          ₹{Number(c.remaining_value).toLocaleString("en-IN")}
                        </td>
                        <td className="p-2.5">{c.source}</td>
                        <td className="p-2.5">
                          {c.lines?.map((l: any) => `${l.item_code} (₹${l.rate})`).join(", ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Preferred Suppliers Section */}
            <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <h2 className="font-semibold">Preferred Supplier Rankings (Top 3 per Item)</h2>
                  <p className="text-xs text-slate-500">
                    Recalculated dynamically as of chosen date using 548-day PO history, recency, contracts, on-time %, and quality.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <input
                    type="date"
                    value={asOfDate}
                    onChange={(e) => {
                      setAsOfDate(e.target.value);
                      fetchPreferredSuppliers(e.target.value);
                    }}
                    className="border border-slate-300 rounded-md px-3 py-1.5 text-xs"
                  />
                  <a
                    href={`${API_BASE}/api/export/preferred-suppliers-csv?as_of_date=${asOfDate}`}
                    className="px-3.5 py-2 bg-emerald-600 text-white text-xs font-semibold rounded-md hover:bg-emerald-700"
                  >
                    Export Preferred Suppliers CSV
                  </a>
                </div>
              </div>

              <div className="max-h-96 overflow-y-auto divide-y divide-slate-100 text-xs">
                {Object.values(prefSuppliers)
                  .filter((item: any) => item.suppliers?.length > 0)
                  .map((item: any) => (
                    <div key={item.item_code} className="py-3 flex flex-col md:flex-row md:items-center justify-between gap-2">
                      <div>
                        <span className="font-mono font-bold">{item.item_code}</span> — {item.description}{" "}
                        <span className="text-slate-400">({item.category})</span>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {item.suppliers.map((s: any) => (
                          <span
                            key={s.supplier_id}
                            title={s.explanation}
                            className="px-2.5 py-1 rounded bg-slate-100 border border-slate-200"
                          >
                            <strong>#{s.rank} {s.supplier_id}</strong> ({s.supplier_name}) • Ref ₹{s.reference_price}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          </div>
        )}
      </main>

      {/* Override Modal */}
      {overrideTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-xl max-w-md w-full p-6 space-y-4 shadow-xl">
            <h3 className="font-bold text-base">
              Override Requirement {overrideTarget.group_id}
            </h3>
            <p className="text-xs text-slate-500">
              Policy Sec 8.1 requires a written reason for any manual buyer override.
            </p>
            <div>
              <label className="block text-xs font-medium mb-1">New Route (optional)</label>
              <select
                value={overrideRoute}
                onChange={(e) => setOverrideRoute(e.target.value)}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
              >
                <option value="">Keep {overrideTarget.route}</option>
                <option value="CONTRACT_PO">CONTRACT_PO</option>
                <option value="EMERGENCY_PO">EMERGENCY_PO</option>
                <option value="SPOT_PO">SPOT_PO</option>
                <option value="RFQ">RFQ</option>
                <option value="RFP">RFP</option>
                <option value="CAPEX_SOURCING">CAPEX_SOURCING</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium mb-1">Override Supplier ID (optional)</label>
              <input
                type="text"
                placeholder="e.g. S024"
                value={overrideSupplier}
                onChange={(e) => setOverrideSupplier(e.target.value)}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-medium mb-1">Written Reason (Required)</label>
              <textarea
                rows={3}
                value={overrideComment}
                onChange={(e) => setOverrideComment(e.target.value)}
                placeholder="Explain why this policy route is being overridden..."
                className="w-full border border-slate-300 rounded-md p-2.5 text-sm"
              />
            </div>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setOverrideTarget(null)}
                className="px-4 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100 rounded-md"
              >
                Cancel
              </button>
              <button
                onClick={handleSubmitOverride}
                className="px-4 py-2 text-xs font-semibold bg-rose-600 text-white rounded-md hover:bg-rose-700"
              >
                Confirm Override
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
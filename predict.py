import json
import os
import sys


from ml.rootcause_model import DEFAULT_REPO, extract_evidence_lines, load, predict_log
from ml.severity_llm import classify_severity

REPO_ID = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_REPO
REPORT_PATH = os.environ.get("ROOTCAUSE_REPORT", "rootcause_report.json")

print(f"Loading root-cause model from HuggingFace Hub: {REPO_ID} ...")
tokenizer, model, device = load(REPO_ID)

with open(sys.argv[1], encoding="utf-8-sig", errors="replace") as f:
    log_text = f.read()

root_cause, confidence, top3 = predict_log(tokenizer, model, device, log_text)

evidence_lines, evidence_block = extract_evidence_lines(log_text)
sev, sev_source, sev_note = classify_severity(root_cause, evidence_block)

report = {
    "job": os.path.basename(sys.argv[1]),
    "root_cause": root_cause,
    "confidence": round(confidence, 4),
    "top_causes": [{"cause": c, "prob": round(p, 4)} for c, p in top3],
    "severity": sev,
    "severity_source": sev_source,
    "severity_reason": sev_note,
    "evidence_lines": evidence_lines,
}

print("=" * 64)
print(f"ROOT CAUSE : {root_cause}")
print(f"CONFIDENCE : {confidence:.3f}")
for c, p in top3[1:]:
    print(f"  alt      : {c} ({p:.3f})")
print(f"SEVERITY   : {sev}   [source: {sev_source}]")
if sev_note:
    print(f"  reason   : {sev_note}")
print(f"EVIDENCE   : {len(evidence_lines)} distinct error/failure lines")
print("-" * 64)
for i, line in enumerate(evidence_lines[:15], 1):
    print(f"  {i:>2}. {line[:180]}")
print("=" * 64)

print(f"\n[report written] {REPORT_PATH}")
with open(REPORT_PATH, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
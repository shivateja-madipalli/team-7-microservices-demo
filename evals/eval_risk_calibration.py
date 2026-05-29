"""
Eval 2: Risk Calibration (extended)

Runs the agent on labeled test cases and checks that:
  - The output Risk Level matches the expected label
  - The output Change Category matches the expected category
  - The agent called the required tools (read_prd, read_design_doc,
    traverse_graph, grep_codebase, read_file) — i.e. it didn't skip
    the grounding steps

Extended checks (applied per case based on case flags):
  - check_tool_order (all cases by default):
        read_prd before read_design_doc; traverse_graph before
        grep_codebase; grep_codebase before read_file.
  - expected_dropped_candidates + expected_not_in_assets:
        graph candidates eliminated by grep must be explicitly rejected
        in the report and must not appear in Affected Assets as impacted.
  - expects_new_service:
        when the proposed service doesn't exist in src/, Confidence Level
        must be Medium or Low (not High) and the report must acknowledge
        the limitation explicitly.

Ground truth labels:
  Low    — INR currency: adding a JSON key, no code or proto changes.
  Medium — Category filter: backward-compat proto field + code in 2 services.
  Medium — Shipping estimate: new gRPC client in frontend, no proto change.
  High   — Order history: new service + persistent store + auth concept.

Usage:
    python evals/eval_risk_calibration.py

Requires OPENAI_API_KEY (reads from ~/Documents/problem_first_ai/.env).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── Paths & env ──────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
ENV_PATH = Path.home() / "Documents" / "problem_first_ai" / ".env"
load_dotenv(ENV_PATH)

if not os.environ.get("OPENAI_API_KEY"):
    print("ERROR: OPENAI_API_KEY not set. Check ENV_PATH in this file.")
    sys.exit(1)

# ── Test cases ───────────────────────────────────────────────────────────────
# Fields:
#   prd, design_doc        — paths relative to REPO_ROOT
#   expected_risk          — Low / Medium / High / Critical (case-insensitive)
#   expected_category      — one of the 6 valid categories, or None to skip check
#   rationale              — why this expectation holds (shown in failure messages)
#   required_tools         — tools the agent MUST call
#   skip_tool_order_check  — set True to skip ordering assertions for this case
#   expected_dropped_candidates — service names that grep should eliminate; the
#                                 report must explicitly reject them as non-callers
#   expected_not_in_assets — service names that must not appear in Affected Assets
#                            as impacted (rows with impact language)
#   expects_new_service    — True: Confidence Level must be Medium/Low (not High)
#                            and report must acknowledge the unverifiable service

TEST_CASES = [
    # ── Low-risk baseline ────────────────────────────────────────────────────
    {
        "name": "INR Currency (data-only, low risk)",
        "prd": "prd_design_docs/inr-currency-prd.md",
        "design_doc": "prd_design_docs/inr-currency-design-doc.md",
        "expected_risk": "low",
        "expected_category": "data/config change",
        "rationale": (
            "Adding INR only requires editing currency_conversion.json. "
            "No proto changes, no code changes, no new services. "
            "CurrencyService loads the file at startup so the change is "
            "a single JSON key addition."
        ),
        "required_tools": ["read_prd", "read_design_doc", "traverse_graph", "grep_codebase"],
    },
    # ── High-risk baseline ───────────────────────────────────────────────────
    {
        "name": "Order History (new service + auth, high risk)",
        "prd": "prd_design_docs/order-history-prd.md",
        "design_doc": "prd_design_docs/order-history-design-doc.md",
        "expected_risk": "high",
        "expected_category": None,  # new service or mixed are both valid
        "rationale": (
            "Introduces a new OrderHistoryService, requires a persistent store, "
            "adds an account identity concept the system doesn't currently have, "
            "and modifies the checkout flow critical path."
        ),
        "required_tools": ["read_prd", "read_design_doc", "traverse_graph", "grep_codebase", "read_file"],
        "expects_new_service": True,
    },
    # ── Medium-risk: backward-compat proto + code in two services ────────────
    {
        "name": "Category Filter (backward-compat proto + code change, medium risk)",
        "prd": "prd_design_docs/filter-prd.md",
        "design_doc": "prd_design_docs/filter-design-doc.md",
        "expected_risk": "medium",
        "expected_category": None,  # "proto change" or "mixed" are both valid
        "rationale": (
            "Adds a backward-compatible 'repeated string categories = 2' field to "
            "SearchProductsRequest. Code changes in productcatalogservice (search handler) "
            "and frontend (UI + RPC call). No new service. No new persistent store. "
            "Only frontend calls SearchProducts at runtime — recommendationservice uses "
            "ListProducts and checkoutservice uses GetProduct; neither is a SearchProducts caller. "
            "Blast radius is one confirmed caller (frontend). Risk rubric: Medium."
        ),
        "required_tools": ["read_prd", "read_design_doc", "traverse_graph", "grep_codebase", "read_file"],
        # Graph traversal finds recommendationservice and checkoutservice as neighbors of
        # productcatalogservice, but grep confirms neither calls SearchProducts.
        # The report must explicitly drop them, and they must not appear as impacted
        # in Affected Assets.
        "expected_dropped_candidates": ["recommendationservice", "checkoutservice"],
        "expected_not_in_assets": ["recommendationservice"],
    },
    # ── Medium-risk: code change in one existing service, no proto change ────
    {
        "name": "Shipping Estimate (new gRPC client in frontend, medium risk)",
        "prd": "prd_design_docs/shipping-estimate-prd.md",
        "design_doc": "prd_design_docs/shipping-estimate-design-doc.md",
        "expected_risk": "medium",
        "expected_category": None,  # "code change" or "mixed" (manifest + code) are both valid
        "rationale": (
            "Adds a ShippingServiceClient inside frontend and a getShippingEstimate helper. "
            "No proto changes. No new services. shippingservice is existing and its code is "
            "unchanged — it merely receives additional GetQuote calls. The estimate call is "
            "best-effort (non-fatal on timeout/error) and is not on the checkout critical path. "
            "Code changes in one service (frontend) with blast radius ≤ 2. Risk rubric: Medium."
        ),
        "required_tools": ["read_prd", "read_design_doc", "traverse_graph", "grep_codebase", "read_file"],
    },
]

# ── Agent setup (mirrors the notebook exactly) ───────────────────────────────

import json as _json
from pypdf import PdfReader

from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent

# Load graph
GRAPH_PATH = REPO_ROOT / "graphify-out" / "graph.json"
HANDBOOK_PATH = REPO_ROOT / "product_handbook.pdf"
SRC_ROOT = REPO_ROOT / "src"

with open(GRAPH_PATH) as f:
    _graph_data = _json.load(f)
GRAPH_NODES = _graph_data["nodes"]
GRAPH_LINKS = _graph_data["links"]
NODE_BY_ID = {n["id"]: n for n in GRAPH_NODES}

_reader = PdfReader(str(HANDBOOK_PATH))
HANDBOOK_TEXT = "\n\n".join(page.extract_text() or "" for page in _reader.pages)


@tool
def read_prd(prd_path: str) -> str:
    """Read a PRD markdown file."""
    path = Path(prd_path)
    if not path.is_absolute():
        path = REPO_ROOT / prd_path
    return path.read_text() if path.exists() else f"ERROR: PRD not found at {path}"


@tool
def read_design_doc(design_doc_path: str) -> str:
    """Read a Design Doc markdown file."""
    path = Path(design_doc_path)
    if not path.is_absolute():
        path = REPO_ROOT / design_doc_path
    return path.read_text() if path.exists() else f"ERROR: Design Doc not found at {path}"


@tool
def traverse_graph(service_name: str, hops: int = 1) -> str:
    """Find services connected to a given service in the dependency graph."""
    service_node_ids = {
        n["id"] for n in GRAPH_NODES
        if service_name.lower() in (n.get("source_file") or "").lower()
    }
    if not service_node_ids:
        return _json.dumps({"error": f"No nodes found for '{service_name}'", "candidates": []})
    frontier, visited = service_node_ids.copy(), service_node_ids.copy()
    for _ in range(hops):
        nxt = set()
        for link in GRAPH_LINKS:
            src, tgt = link["source"], link["target"]
            if src in frontier and tgt not in visited:
                nxt.add(tgt)
            elif tgt in frontier and src not in visited:
                nxt.add(src)
        frontier = nxt
        visited |= nxt
    neighbor_files = {
        NODE_BY_ID[nid].get("source_file", "")
        for nid in (visited - service_node_ids)
        if nid in NODE_BY_ID
    }
    candidates = set()
    for f in neighbor_files:
        parts = f.replace("\\", "/").split("/")
        if "src" in parts:
            idx = parts.index("src")
            if idx + 1 < len(parts) and parts[idx + 1] != service_name:
                candidates.add(parts[idx + 1])
    return _json.dumps({
        "query_service": service_name, "hops": hops,
        "candidates": sorted(candidates),
        "warning": "Candidates include genproto stubs. Verify with grep_codebase.",
    }, indent=2)


@tool
def grep_codebase(service_names: str, pattern: str) -> str:
    """Search source code of specified services for a pattern."""
    results = {}
    for svc in [s.strip() for s in service_names.split(",") if s.strip()]:
        src_dir = SRC_ROOT / svc
        if not src_dir.exists():
            results[svc] = {"status": "directory not found", "match_count": 0, "matches": []}
            continue
        cmd = [
            "grep", "-rn", "-E",
            "--include=*.go", "--include=*.js", "--include=*.py",
            "--exclude-dir=genproto", "--exclude-dir=node_modules",
            pattern, str(src_dir),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        raw = [l.strip() for l in proc.stdout.strip().split("\n") if l.strip()]
        results[svc] = {
            "status": "CONFIRMED CALLER" if raw else "not a runtime caller (genproto stub only)",
            "match_count": len(raw),
            "matches": raw[:20],
        }
    return _json.dumps(results, indent=2)


@tool
def read_file(file_path: str) -> str:
    """Read a source file from the repository."""
    path = Path(file_path)
    if not path.is_absolute():
        path = REPO_ROOT / file_path
    if not path.exists():
        return f"ERROR: Not found: {path}"
    content = path.read_text(errors="replace")
    return content[:8000] + (f"\n\n[... truncated ...]" if len(content) > 8000 else "")


@tool
def read_product_handbook(query: str) -> str:
    """Search the product handbook for sections relevant to the query."""
    terms = query.lower().split()
    paras = [p.strip() for p in HANDBOOK_TEXT.split("\n\n") if len(p.strip()) > 80]
    def score(p): return sum(1 for t in terms if t in p.lower())
    top = sorted(paras, key=score, reverse=True)[:6]
    if not top or score(top[0]) == 0:
        return f"No relevant sections found for: '{query}'"
    return (f"Handbook sections for '{query}':\n\n" + "\n\n---\n\n".join(top))[:6000]


SYSTEM_PROMPT = """\
You are an AI Change Impact Analyzer for Online Boutique — an 11-service
microservices system using gRPC and protobuf contracts.

Your job: Given a PRD and Design Doc, produce a grounded 7-section risk report.

WORKFLOW — follow this order every time
1. read_prd          → goals, requirements, success metrics
2. read_design_doc   → what is changing (files, services, proto fields)
3. traverse_graph    → candidate services in the blast radius (hops=1)
   Always note that graph edges include generated proto stubs.
4. grep_codebase     → verify which candidates actually make runtime calls.
   Search for the gRPC client constructor (e.g. NewCurrencyServiceClient).
   Drop any service with zero matches outside genproto/.
5. read_file         → entry point of changed service + each confirmed caller.
   Look for: data loading pattern, error handling, exact call sites.
6. read_product_handbook (optional) → compliance rules, proto constraints.

OUTPUT — produce exactly these 7 sections:

### Change Summary
One paragraph. What is changing, why, and what it achieves.

### Change Category
One of: Data/Config Change | Proto Change | New Service | Code Change |
Infrastructure Change | Mixed

### Affected Assets
Table: Asset | Type | Impact. Only list assets you verified.

### Dependency Paths
Call paths involving the changed service. For EVERY service returned by
traverse_graph, state its grep verdict explicitly:
  - Confirmed: "<service> — CONFIRMED CALLER (<N> matches)"
  - Dropped:   "<service> — not a runtime caller of <RPC> (grep: 0 matches, dropped)"
Do not silently omit candidates; name them and explain why they were kept or dropped.

### Risk Assessment
Risk Level: Low / Medium / High / Critical
Structural risk (blast radius) + behavioral risk (how failures propagate).
Reference specific file names you read.

Use these criteria — apply the HIGHEST matching level:

  Low    — Change touches ONLY static data or config files (JSON, YAML, env vars).
            No proto changes. No new services. No code logic added or removed.
            No new runtime dependencies. Rollback = revert one file.
            The fact that existing code READS the config file does NOT raise risk —
            the code path already exists and is already tested.
            Example: adding a currency entry to currency_conversion.json.

  Medium — Code changes in existing services (any number). Backward-compatible
            proto field addition NOT on the checkout or payment critical path.
            No new persistent store. No new service introduced.
            Blast radius ≤ 2 confirmed runtime callers.

  High   — Any of: new service introduced; new persistent store (DB, cache);
            new auth/identity concept; backward-compatible proto field that changes
            the checkout or payment critical path; ≥ 3 confirmed runtime callers
            affected; requires data migration or schema change.

  Critical — Breaking proto change; payment/security regression risk; irreversible
             data loss potential; no rollback path.

RULE: When a change introduces a new service OR a new persistent store, the
minimum risk level is High. Never assign Low or Medium in that case.

### Recommended Mitigation Steps
Numbered. Reference actual files, commands, monitoring targets.

### Summary
- Confidence Level: High/Medium/Low — explain what you verified vs. inferred
- Approval Recommendation: Approve | Approve with conditions | Needs re-design

RULES
- Only name files and services you actually verified.
- If you drop a candidate service (false positive), say so explicitly in
  Dependency Paths (e.g. "does not call SearchProducts — dropped").
- If grep_codebase returns "directory not found" for a proposed new service,
  state this explicitly in the report (e.g. "The proposed service was not found
  in src/ and could not be verified"). Confidence Level must be Medium or Low,
  not High, in that case.
- Confidence Level must reflect which tools you could actually call.
"""

TOOLS = [read_prd, read_design_doc, traverse_graph, grep_codebase, read_file, read_product_handbook]


# ── Assertion helpers ────────────────────────────────────────────────────────

def _extract_section(text: str, header: str) -> str:
    """Return content between `header` and the next ### header (or end of text)."""
    pattern = re.escape(header) + r"(.*?)(?=\n###|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def extract_risk_level(report: str) -> str | None:
    m = re.search(r"risk\s*level\s*:\s*(\w+)", report, re.IGNORECASE)
    return m.group(1).lower() if m else None


def extract_change_category(report: str) -> str | None:
    m = re.search(r"### Change Category\s*\n(.+)", report, re.IGNORECASE)
    if not m:
        return None
    cat = re.sub(r"[*_`]", "", m.group(1)).strip().lower()
    return cat


def tools_called(messages) -> list[str]:
    names = []
    for msg in messages:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                names.append(tc["name"])
    return names


def check_tool_ordering(messages) -> list[str]:
    """Assert the agent followed the prescribed workflow order.

    Rules (only checked when both tools in a pair were actually called):
      1. read_prd before read_design_doc
      2. traverse_graph before grep_codebase (first traverse before first grep)
      3. grep_codebase before read_file (first grep before first read_file)
    """
    ordered = []
    for msg in messages:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                ordered.append(tc["name"])

    def first(name: str) -> int:
        for i, n in enumerate(ordered):
            if n == name:
                return i
        return -1

    failures = []

    prd_idx = first("read_prd")
    doc_idx = first("read_design_doc")
    if prd_idx != -1 and doc_idx != -1 and prd_idx > doc_idx:
        failures.append(
            f"Tool order: read_design_doc (call #{doc_idx}) fired before "
            f"read_prd (call #{prd_idx}) — PRD must be read first"
        )

    tg_idx = first("traverse_graph")
    gc_idx = first("grep_codebase")
    if tg_idx != -1 and gc_idx != -1 and tg_idx > gc_idx:
        failures.append(
            f"Tool order: grep_codebase (call #{gc_idx}) fired before "
            f"traverse_graph (call #{tg_idx}) — graph must be traversed before grepping"
        )

    rf_idx = first("read_file")
    if rf_idx != -1:
        if gc_idx == -1:
            failures.append(
                "Tool order: read_file was called but grep_codebase was never called — "
                "agent read source files without first verifying callers via grep"
            )
        elif gc_idx > rf_idx:
            failures.append(
                f"Tool order: read_file (call #{rf_idx}) fired before "
                f"grep_codebase (call #{gc_idx}) — callers must be verified before reading files"
            )

    return failures


def check_candidate_rejection(
    report: str,
    expected_dropped: list[str],
    expected_not_in_assets: list[str],
) -> list[str]:
    """Verify that graph candidates eliminated by grep are handled correctly.

    For each service in expected_dropped:
      - It must be mentioned in the report in a non-caller context (explicitly
        rejected as a runtime caller rather than silently ignored).

    For each service in expected_not_in_assets:
      - It must not appear in Affected Assets as an impacted row (a row with
        impact language but no "no change" / "unchanged" qualifier).
    """
    failures = []

    drop_pattern = re.compile(
        r"(not a.*caller|no.*runtime.*call|genproto stub|does not call|"
        r"excluded|dropped|only.*listproducts|only.*getproduct|no.*searchproduct|"
        r"not.*confirmed|zero match|no match|not affected|no changes?)",
        re.IGNORECASE,
    )

    for svc in expected_dropped:
        lines = [l.strip() for l in report.splitlines() if svc.lower() in l.lower()]
        if not lines:
            failures.append(
                f"Candidate rejection: '{svc}' is a graph candidate that grep should "
                f"eliminate, but the service is never mentioned in the report"
            )
        else:
            has_drop_context = any(drop_pattern.search(l) for l in lines)
            if not has_drop_context:
                failures.append(
                    f"Candidate rejection: '{svc}' is mentioned but never explicitly "
                    f"rejected as a non-caller (expected language such as 'not a caller', "
                    f"'does not call SearchProducts', 'dropped', 'genproto stub only')"
                )

    assets_section = _extract_section(report, "### Affected Assets")
    impact_pattern = re.compile(
        r"(updated|changed|modified|added|new|requires|affected|impacted|calls)",
        re.IGNORECASE,
    )
    no_change_pattern = re.compile(r"no change|unchanged|not affected|no impact", re.IGNORECASE)

    for svc in expected_not_in_assets:
        asset_rows = [
            l for l in assets_section.splitlines()
            if svc.lower() in l.lower() and "|" in l
        ]
        for row in asset_rows:
            if impact_pattern.search(row) and not no_change_pattern.search(row):
                failures.append(
                    f"Grounding: '{svc}' appears in Affected Assets as impacted, "
                    f"but grep confirmed it does not call the changed RPC. "
                    f"Row: {row.strip()!r}"
                )

    return failures


def check_new_service_confidence(report: str) -> list[str]:
    """When a proposed service doesn't exist in src/, confidence must not be High.

    The report must:
      1. Set Confidence Level to Medium or Low (not High).
      2. Acknowledge that the new service could not be fully verified
         (e.g. 'directory not found', 'not yet implemented', 'proposed service').
    """
    failures = []

    m = re.search(r"confidence\s*level\s*:\s*(\w+)", report, re.IGNORECASE)
    if m and m.group(1).lower() == "high":
        failures.append(
            "New service confidence: Confidence Level is 'High' but the proposed "
            "service does not exist in src/ and cannot be fully verified. "
            "Expected Medium or Low."
        )

    unverifiable_pattern = re.compile(
        r"(directory not found|not yet implement|does not exist|proposed.*service|"
        r"new.*service.*not|cannot verify|unverified|no.*source.*code|"
        r"not.*in src|could not.*verify|not.*built|not.*deployed)",
        re.IGNORECASE,
    )
    if not unverifiable_pattern.search(report):
        failures.append(
            "New service confidence: Report does not acknowledge that the proposed "
            "new service could not be verified in src/ — the agent should explicitly "
            "note this limitation (e.g. 'directory not found', 'not yet implemented')"
        )

    return failures


# ── Runner ───────────────────────────────────────────────────────────────────

def run_case(case: dict, agent) -> dict:
    prd_path = REPO_ROOT / case["prd"]
    doc_path = REPO_ROOT / case["design_doc"]

    user_msg = (
        "Analyze the following proposed change and produce a complete risk assessment report.\n\n"
        f"PRD path        : {prd_path}\n"
        f"Design Doc path : {doc_path}\n"
        f"Repository root : {REPO_ROOT}\n\n"
        "Follow the workflow: read PRD → Design Doc → traverse graph → "
        "grep codebase → read files → produce the 7-section report."
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content=user_msg)]},
        config={"recursion_limit": 30},
    )

    report = result["messages"][-1].content
    called = tools_called(result["messages"])

    failures = []

    # ── Risk level ───────────────────────────────────────────────────────────
    actual_risk = extract_risk_level(report)
    expected_risk = case["expected_risk"]
    if actual_risk != expected_risk:
        failures.append(
            f"Risk Level: expected '{expected_risk}', got '{actual_risk}'. "
            f"Rationale: {case['rationale']}"
        )

    # ── Change category (optional per case) ─────────────────────────────────
    expected_cat = case.get("expected_category")
    if expected_cat:
        actual_cat = extract_change_category(report)
        if actual_cat != expected_cat:
            failures.append(
                f"Change Category: expected '{expected_cat}', got '{actual_cat}'"
            )

    # ── Required tools ───────────────────────────────────────────────────────
    for required_tool in case.get("required_tools", []):
        if required_tool not in called:
            failures.append(
                f"Required tool '{required_tool}' was never called — "
                f"agent skipped a grounding step"
            )

    # ── Tool ordering ────────────────────────────────────────────────────────
    if not case.get("skip_tool_order_check", False):
        failures += check_tool_ordering(result["messages"])

    # ── Candidate rejection ──────────────────────────────────────────────────
    dropped = case.get("expected_dropped_candidates", [])
    not_in_assets = case.get("expected_not_in_assets", [])
    if dropped or not_in_assets:
        failures += check_candidate_rejection(report, dropped, not_in_assets)

    # ── New-service confidence ────────────────────────────────────────────────
    if case.get("expects_new_service", False):
        failures += check_new_service_confidence(report)

    return {
        "name": case["name"],
        "passed": len(failures) == 0,
        "failures": failures,
        "actual_risk": actual_risk,
        "tools_called": called,
        "tool_call_count": len(called),
    }


def main():
    print("Building agent...")
    model = init_chat_model("gpt-4o-mini", model_provider="openai")
    agent = create_agent(model, TOOLS, system_prompt=SYSTEM_PROMPT)
    print(f"Agent ready. Running {len(TEST_CASES)} test case(s).\n")

    all_passed = True

    for i, case in enumerate(TEST_CASES, 1):
        # Summarise which extended checks are active for this case
        extra_checks = []
        if not case.get("skip_tool_order_check", False):
            extra_checks.append("tool-order")
        if case.get("expected_dropped_candidates"):
            extra_checks.append("candidate-rejection")
        if case.get("expects_new_service"):
            extra_checks.append("new-svc-confidence")
        checks_str = ", ".join(extra_checks) if extra_checks else "none"

        print(f"[{i}/{len(TEST_CASES)}] {case['name']}")
        print(f"  Extended checks : {checks_str}")

        result = run_case(case, agent)

        status = "PASS" if result["passed"] else "FAIL"
        print(
            f"  [{status}] Risk Level: {result['actual_risk']}  |  "
            f"Tool calls: {result['tool_call_count']}  |  "
            f"Tools: {result['tools_called']}"
        )

        for f in result["failures"]:
            print(f"  ✗ {f}")
            all_passed = False
        print()

    if all_passed:
        print(f"All {len(TEST_CASES)} calibration test(s) passed.")
    else:
        print("Some calibration tests failed — see details above.")
        sys.exit(1)


if __name__ == "__main__":
    main()

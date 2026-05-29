"""
Eval 1: Output Structure & Grounding

Checks that every risk assessment report:
  1. Contains all 7 required sections (by header)
  2. Uses only valid enum values for Change Category, Risk Level,
     Confidence Level, and Approval Recommendation
  3. Mentions only services that actually exist in src/ (grounding —
     no hallucinated service names)
  4. Has a markdown table in the Affected Assets section

Run against saved reports:
    python evals/eval_output_structure.py

Or point at a specific file:
    python evals/eval_output_structure.py path/to/report.md
"""

import re
import sys
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent

REQUIRED_SECTIONS = [
    "### Change Summary",
    "### Change Category",
    "### Affected Assets",
    "### Dependency Paths",
    "### Risk Assessment",
    "### Recommended Mitigation Steps",
    "### Summary",
]

VALID_CHANGE_CATEGORIES = {
    "data/config change",
    "proto change",
    "new service",
    "code change",
    "infrastructure change",
    "mixed",
}

VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}

VALID_CONFIDENCE_LEVELS = {"high", "medium", "low"}

VALID_APPROVAL_RECOMMENDATIONS = {
    "approve",
    "approve with conditions",
    "needs re-design",
    "needs redesign",  # tolerate minor casing/hyphen variation
}

# Services that actually exist under src/
REAL_SERVICES = {d.name for d in (REPO_ROOT / "src").iterdir() if d.is_dir()}


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_section(text: str, header: str) -> str:
    """Return the content of a section between `header` and the next ### header."""
    pattern = re.escape(header) + r"(.*?)(?=\n###|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def check_sections_present(text: str) -> list[str]:
    failures = []
    for section in REQUIRED_SECTIONS:
        if section.lower() not in text.lower():
            failures.append(f"Missing section: {section}")
    return failures


def check_change_category(text: str) -> list[str]:
    section = extract_section(text, "### Change Category")
    if not section:
        return ["Change Category section is empty"]
    category = section.strip().lower().splitlines()[0]
    # Strip markdown formatting
    category = re.sub(r"[*_`]", "", category).strip()
    if category not in VALID_CHANGE_CATEGORIES:
        return [
            f"Invalid Change Category '{category}'. "
            f"Must be one of: {sorted(VALID_CHANGE_CATEGORIES)}"
        ]
    return []


def check_risk_level(text: str) -> list[str]:
    section = extract_section(text, "### Risk Assessment")
    if not section:
        return ["Risk Assessment section is empty"]
    m = re.search(r"risk\s*level\s*:\s*(\w+)", section, re.IGNORECASE)
    if not m:
        return ["Risk Level line not found in Risk Assessment section"]
    level = m.group(1).lower()
    if level not in VALID_RISK_LEVELS:
        return [
            f"Invalid Risk Level '{m.group(1)}'. "
            f"Must be one of: {sorted(VALID_RISK_LEVELS)}"
        ]
    return []


def check_confidence_and_recommendation(text: str) -> list[str]:
    section = extract_section(text, "### Summary")
    if not section:
        return ["Summary section is empty"]
    failures = []

    conf_match = re.search(r"confidence\s*level\s*:\s*(\w+)", section, re.IGNORECASE)
    if not conf_match:
        failures.append("Confidence Level line not found in Summary section")
    else:
        level = conf_match.group(1).lower()
        if level not in VALID_CONFIDENCE_LEVELS:
            failures.append(
                f"Invalid Confidence Level '{conf_match.group(1)}'. "
                f"Must be one of: {sorted(VALID_CONFIDENCE_LEVELS)}"
            )

    rec_match = re.search(
        r"approval\s*recommendation\s*:\s*(.+?)(?:\n|$)", section, re.IGNORECASE
    )
    if not rec_match:
        failures.append("Approval Recommendation line not found in Summary section")
    else:
        rec = re.sub(r"[*_`]", "", rec_match.group(1)).strip().lower()
        # Accept if any valid value is a prefix of the recommendation text
        if not any(rec.startswith(v) for v in VALID_APPROVAL_RECOMMENDATIONS):
            failures.append(
                f"Invalid Approval Recommendation '{rec_match.group(1).strip()}'. "
                f"Must start with one of: {sorted(VALID_APPROVAL_RECOMMENDATIONS)}"
            )

    return failures


def check_affected_assets_table(text: str) -> list[str]:
    """Affected Assets must contain a markdown table (lines with |)."""
    section = extract_section(text, "### Affected Assets")
    if not section:
        return ["Affected Assets section is empty"]
    if "|" not in section:
        return ["Affected Assets section has no markdown table (expected | delimited rows)"]
    return []


def check_grounding(text: str) -> list[str]:
    """
    Any service name mentioned in backticks that looks like a service name
    (ends in 'service' or is a known service) should exist in src/.
    False-positives are capped: only flag names ending in 'service' or 'worker'
    that don't appear in REAL_SERVICES and aren't describing a concept.
    """
    # Extract all backtick-quoted words
    candidates = re.findall(r"`([a-z][a-z0-9-]+(?:service|worker))`", text, re.IGNORECASE)
    hallucinated = []
    for name in set(candidates):
        name_lower = name.lower().replace("-", "")
        # Check exact match or close match (e.g. subscription-renewal-worker is new/expected)
        if name_lower not in {s.lower() for s in REAL_SERVICES}:
            hallucinated.append(name)
    if hallucinated:
        return [
            f"Service(s) mentioned that don't exist in src/: {sorted(hallucinated)}. "
            f"Known services: {sorted(REAL_SERVICES)}"
        ]
    return []


# ── Runner ───────────────────────────────────────────────────────────────────

def evaluate_report(path: Path) -> dict:
    text = path.read_text()
    failures = []
    failures += check_sections_present(text)
    failures += check_change_category(text)
    failures += check_risk_level(text)
    failures += check_confidence_and_recommendation(text)
    failures += check_affected_assets_table(text)
    # Grounding: only report as warnings — new services are expected
    grounding_warnings = check_grounding(text)
    return {
        "file": path.name,
        "passed": len(failures) == 0,
        "failures": failures,
        "warnings": grounding_warnings,
    }


def main():
    if len(sys.argv) > 1:
        report_paths = [Path(p) for p in sys.argv[1:]]
    else:
        reports_dir = REPO_ROOT / "risk_assessments"
        report_paths = sorted(reports_dir.glob("*.md"))

    if not report_paths:
        print("No reports found. Run the agent first to generate reports in risk_assessments/")
        sys.exit(1)

    print(f"Evaluating {len(report_paths)} report(s)...\n")
    all_passed = True

    for path in report_paths:
        result = evaluate_report(path)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['file']}")
        for f in result["failures"]:
            print(f"  ✗ {f}")
            all_passed = False
        for w in result["warnings"]:
            print(f"  ⚠ {w}")

    print()
    if all_passed:
        print(f"All {len(report_paths)} report(s) passed structure checks.")
    else:
        print("Some reports have structural issues — see failures above.")
        sys.exit(1)


if __name__ == "__main__":
    main()

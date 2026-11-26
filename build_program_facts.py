"""
Build a structured facts table: program -> total credits
=========================================================================
Aggregation questions ("how many programs require more than 120 credits",
"list all programs over X credits") can't be answered reliably by vector
retrieval - it returns the k most SIMILAR chunks, never ALL matching ones.
This script extracts an exact, complete table instead, built from
CREDITS_ANALYSIS.md's findings (55 files in docs/programs/ scanned):

  - 19 files: canonical "Program Structure: X | Required Total Credits (N): Y"
    table rows -> regex A
  - 2 files (MBA single/dual): "Major Courses (Required: N credits)" heading
    -> regex B
  - 2 files (MS in AI single/dual): no stated total, but cleanly computable
    from explicit course counts in the prose -> hardcoded, high confidence
  - 4 files: genuinely self-contradictory or ambiguous source text (three
    different totals in one file, or a heading total that silently excludes
    a required-looking electives section) -> hardcoded with needs_review=True
  - 28 files: hub/staff/certificate/index pages with no program total at all
    -> naturally unmatched, logged for transparency, not included

Output: db/facts/programs.json - a plain JSON array of
{program, total_credits, source_url, filename, extraction, needs_review, note}

Usage:
    python build_program_facts.py
    python build_program_facts.py --docs-dir docs/programs
"""

import os
import re
import json
import glob
import argparse

DOCS_DIR = "docs/programs"
OUTPUT_PATH = "db/facts/programs.json"

SOURCE_LINE_PATTERN = re.compile(r"^Source:\s*(\S+)\s*$")

# Regex A: the canonical table-row convention. Handles both "Required Total
# Credits (N)" and the one file using the singular "Required Total Credit (N)".
CANONICAL_PATTERN = re.compile(
    r"Program Structure:\s*(?P<category>[^|]+?)\s*\|\s*"
    r"Required Total Credits?\s*\((?P<total>\d+)\)\s*:\s*(?P<value>\d+)"
)

# Regex B: the MBA single/dual heading pattern.
MBA_HEADING_PATTERN = re.compile(
    r"(?:Major Courses|Dual MBA Curriculum)\s*\(Required:\s*(?P<total>\d+)\s*credits?\)"
)

# Files with no cleanly-regexable total, resolved from CREDITS_ANALYSIS.md's
# findings. Two confidence tiers - see module docstring.
MANUAL_FACTS = {
    "faculty-of-digital-technologies-master-of-science-in-computer-science-ai-master-of-science-in-ai.txt": {
        "total_credits": 45,
        "extraction": "computed_from_course_list",
        "needs_review": False,
        "note": "Not stated as a single number in the source text. Computed "
                "from prose: 13 courses x 3 credits + 1 project x 6 credits = 45.",
    },
    "faculty-of-digital-technologies-master-of-science-in-computer-science-ai-master-of-science-in-computer-science-ai-dual-degree.txt": {
        "total_credits": 45,
        "extraction": "computed_from_course_list",
        "needs_review": False,
        "note": "Not stated as a single number. Computed from two labeled "
                "subtotals in the source: FHSU 24 credits + AUPP 21 credits = 45.",
    },
    "faculty-of-law-master-of-laws-in-artificial-intelligence.txt": {
        "total_credits": 45,
        "extraction": "manual",
        "needs_review": True,
        "note": "Source text states THREE different totals: 36 (prose, "
                "doesn't match any breakdown), 39 (heading, explicitly "
                "excludes a separate Elective Courses section), 45 (full "
                "sum including electives). Assumed 45 on the basis that "
                "electives appear as a required section, not an optional "
                "one - NOT confirmed against the live page. Please verify.",
    },
    "faculty-of-law-master-of-laws-in-cybersecurity.txt": {
        "total_credits": 45,
        "extraction": "manual",
        "needs_review": True,
        "note": "Same structure as the AI LLM: a 'Required Courses (39 "
                "Credits)' heading that excludes a separate 6-credit "
                "Elective Courses section. Assumed 45 (electives required) "
                "for consistency with the sibling AI program - NOT confirmed. "
                "Please verify.",
    },
    "faculty-of-law-master-of-laws-in-international-business-and-digital-technologies.txt": {
        "total_credits": 45,
        "extraction": "manual",
        "needs_review": True,
        "note": "Same ambiguity as the Cybersecurity LLM: 39-credit heading "
                "excludes a separate 6-credit electives section. Assumed 45 "
                "- NOT confirmed. Please verify.",
    },
    "faculty-of-law-master-of-legal-studies.txt": {
        "total_credits": 45,
        "extraction": "manual",
        "needs_review": True,
        "note": "No total stated anywhere. 15 courses x 3 credits = 45 from "
                "the listed coursework, but the source also mentions "
                "unspecified-credit 'Bridging courses' that may add to this "
                "total. Please verify against the live page.",
    },
}

# Program name is taken from the first non-blank line after the Source: line.


def extract_program_name(lines):
    for line in lines[1:]:
        if line.strip():
            return line.strip()
    return None


def extract_canonical(body, filename, source_url, program_name):
    matches = list(CANONICAL_PATTERN.finditer(body))
    if not matches:
        return None

    totals_found = {int(m.group("total")) for m in matches}
    total_credits = matches[0].group("total")
    total_credits = int(total_credits)

    row_sum = sum(int(m.group("value")) for m in matches)

    note = None
    if len(totals_found) > 1:
        note = (f"Inconsistent: table rows state {len(totals_found)} "
                f"different totals ({sorted(totals_found)}); used the first "
                f"one found ({total_credits}).")
    elif row_sum != total_credits:
        note = (f"Category breakdown sums to {row_sum}, which does not match "
                f"the stated total of {total_credits}. Stored the STATED "
                f"total (matches what's printed on the page), not the sum.")

    return {
        "program": program_name,
        "total_credits": total_credits,
        "source_url": source_url,
        "filename": filename,
        "extraction": "regex_canonical",
        "needs_review": False,
        "note": note,
    }


def extract_mba_heading(body, filename, source_url, program_name):
    match = MBA_HEADING_PATTERN.search(body)
    if not match:
        return None
    return {
        "program": program_name,
        "total_credits": int(match.group("total")),
        "source_url": source_url,
        "filename": filename,
        "extraction": "regex_mba_heading",
        "needs_review": False,
        "note": None,
    }


def process_file(filepath):
    filename = os.path.basename(filepath)
    with open(filepath, encoding="utf-8") as f:
        raw = f.read()

    lines = raw.split("\n")
    match = SOURCE_LINE_PATTERN.match(lines[0]) if lines else None
    source_url = match.group(1) if match else "unknown"
    program_name = extract_program_name(lines) or filename
    body = raw

    if filename in MANUAL_FACTS:
        fact = dict(MANUAL_FACTS[filename])
        fact.update({
            "program": program_name,
            "source_url": source_url,
            "filename": filename,
        })
        return fact

    fact = extract_canonical(body, filename, source_url, program_name)
    if fact:
        return fact

    fact = extract_mba_heading(body, filename, source_url, program_name)
    if fact:
        return fact

    return None  # no credit fact found - logged as excluded by the caller


def build_facts(docs_dir):
    filepaths = sorted(glob.glob(os.path.join(docs_dir, "*.txt")))
    facts, excluded = [], []

    for filepath in filepaths:
        fact = process_file(filepath)
        if fact:
            facts.append(fact)
        else:
            excluded.append(os.path.basename(filepath))

    return facts, excluded


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs-dir", default=DOCS_DIR)
    parser.add_argument("--output", default=OUTPUT_PATH)
    args = parser.parse_args()

    print(f"Scanning {args.docs_dir}/ for program credit totals...\n")
    facts, excluded = build_facts(args.docs_dir)

    review_needed = [f for f in facts if f["needs_review"]]
    with_notes = [f for f in facts if f["note"] and not f["needs_review"]]

    print(f"Extracted {len(facts)} program facts:")
    for f in sorted(facts, key=lambda x: x["total_credits"]):
        flag = " ⚠️ NEEDS REVIEW" if f["needs_review"] else ""
        print(f"  {f['total_credits']:>4} credits  {f['program']}{flag}")

    if with_notes:
        print(f"\n{len(with_notes)} file(s) had a resolvable note (stated "
              f"total used, informational only):")
        for f in with_notes:
            print(f"  {f['filename']}: {f['note']}")

    if review_needed:
        print(f"\n⚠️  {len(review_needed)} file(s) NEED HUMAN REVIEW "
              f"(source text is genuinely ambiguous/contradictory):")
        for f in review_needed:
            print(f"  {f['filename']}: {f['note']}")

    print(f"\n{len(excluded)} file(s) had no credit fact and were excluded "
          f"(hub/staff/certificate pages etc.):")
    for filename in excluded:
        print(f"  {filename}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)
    print(f"\nWrote {len(facts)} facts to {args.output}")


if __name__ == "__main__":
    main()
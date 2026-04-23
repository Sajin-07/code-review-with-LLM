#!/usr/bin/env python3
"""
Evaluation harness for the Code Review Assistant.
Calls the /review endpoint with known test cases and checks:
  - Did it find the expected issues? (true positives)
  - Did it flag things it shouldn't? (false positives)

Score formula:
  score = (correctly_identified / expected_issues) - (false_positives / should_not_flag)

Usage:
  python3 eval/run-eval.py                    # run against localhost:8090
  python3 eval/run-eval.py --url http://X:8090  # run against custom URL
  python3 eval/run-eval.py --verbose           # show details for each test
"""

import json
import sys
import os
import argparse
import httpx
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
TEST_CASES_FILE = EVAL_DIR / "test-cases.json"
HISTORY_FILE = EVAL_DIR / "score-history.json"
DEFAULT_URL = "http://localhost:8090"


def load_test_cases() -> list[dict]:
    with open(TEST_CASES_FILE) as f:
        return json.load(f)


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(history: list[dict]):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def check_issue_match(issues: list[dict], keywords: list[str]) -> list[dict]:
    """Check which keywords are found in the issues text."""
    matches = []
    for kw in keywords:
        kw_lower = kw.lower()
        found = False
        for issue in issues:
            # Search across all text fields in the issue
            text = " ".join([
                str(issue.get("problem", "")),
                str(issue.get("explanation", "")),
                str(issue.get("fix", "")),
                str(issue.get("rule_violated", "")),
                str(issue.get("location", "")),
            ]).lower()
            if kw_lower in text:
                found = True
                break
        matches.append({"keyword": kw, "found": found})
    return matches


def run_single_test(test: dict, base_url: str, verbose: bool) -> dict:
    """Run one test case against the API."""
    tc_id = test["id"]
    language = test["language"]
    code = test["input_code"]
    expected = test["expected_issues"]
    should_not = test["should_not_flag"]

    if verbose:
        print(f"\n  [{tc_id}] {test['description']}")

    # Call the API
    try:
        resp = httpx.post(
            f"{base_url}/review",
            json={"language": language, "code": code, "question": "What is wrong with this code?"},
            timeout=180.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [{tc_id}] ERROR: {e}")
        return {
            "id": tc_id,
            "status": "error",
            "error": str(e),
            "true_positives": 0,
            "expected_count": len(expected),
            "false_positives": 0,
            "should_not_count": len(should_not),
            "score": 0.0,
        }

    issues = data.get("issues", [])

    # Check true positives: did it find what we expected?
    expected_matches = check_issue_match(issues, expected)
    true_positives = sum(1 for m in expected_matches if m["found"])

    # Check false positives: did it flag things it shouldn't?
    false_positive_matches = check_issue_match(issues, should_not)
    false_positives = sum(1 for m in false_positive_matches if m["found"])

    # Score calculation
    if len(expected) > 0:
        tp_rate = true_positives / len(expected)
    else:
        # Clean code test: score is 1.0 if no issues found, penalized for each issue
        tp_rate = 1.0 if len(issues) == 0 else max(0, 1.0 - len(issues) * 0.2)

    if len(should_not) > 0:
        fp_penalty = false_positives / len(should_not)
    else:
        fp_penalty = 0.0

    score = max(0.0, tp_rate - fp_penalty)

    if verbose:
        status_icon = "PASS" if score >= 0.5 else "FAIL"
        print(f"    {status_icon} score={score:.2f}  TP={true_positives}/{len(expected)}  FP={false_positives}/{len(should_not)}  issues_found={len(issues)}")
        if expected:
            for m in expected_matches:
                icon = "+" if m["found"] else "-"
                print(f"      [{icon}] expected: '{m['keyword']}' {'found' if m['found'] else 'MISSING'}")
        if should_not:
            for m in false_positive_matches:
                if m["found"]:
                    print(f"      [!] false positive: '{m['keyword']}' was flagged but shouldn't be")

    return {
        "id": tc_id,
        "status": "pass" if score >= 0.5 else "fail",
        "true_positives": true_positives,
        "expected_count": len(expected),
        "false_positives": false_positives,
        "should_not_count": len(should_not),
        "issues_found": len(issues),
        "score": round(score, 3),
        "expected_detail": expected_matches,
        "false_positive_detail": false_positive_matches,
    }


def run_eval(base_url: str, verbose: bool, tag: str = ""):
    """Run all test cases and produce a report."""
    test_cases = load_test_cases()
    print(f"=== Code Review Eval ===")
    print(f"URL: {base_url}")
    print(f"Test cases: {len(test_cases)}")
    print(f"Time: {datetime.now().isoformat()}")
    if tag:
        print(f"Tag: {tag}")
    print()

    results = []
    for tc in test_cases:
        r = run_single_test(tc, base_url, verbose)
        results.append(r)

    # Compute aggregates
    total_score = sum(r["score"] for r in results)
    avg_score = total_score / len(results) if results else 0
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    errors = sum(1 for r in results if r["status"] == "error")

    total_tp = sum(r["true_positives"] for r in results)
    total_expected = sum(r["expected_count"] for r in results)
    total_fp = sum(r["false_positives"] for r in results)
    total_should_not = sum(r["should_not_count"] for r in results)

    # Java vs TypeScript breakdown
    java_results = [r for r in results if r["id"].startswith("java")]
    ts_results = [r for r in results if r["id"].startswith("ts")]
    java_avg = sum(r["score"] for r in java_results) / len(java_results) if java_results else 0
    ts_avg = sum(r["score"] for r in ts_results) / len(ts_results) if ts_results else 0

    print(f"\n{'='*50}")
    print(f"RESULTS")
    print(f"{'='*50}")
    print(f"  Overall score:  {avg_score:.3f}")
    print(f"  Java score:     {java_avg:.3f}")
    print(f"  TypeScript:     {ts_avg:.3f}")
    print(f"  Passed:         {passed}/{len(results)}")
    print(f"  Failed:         {failed}/{len(results)}")
    print(f"  Errors:         {errors}/{len(results)}")
    print(f"  True positives: {total_tp}/{total_expected}")
    print(f"  False positives:{total_fp}/{total_should_not}")
    print()

    # Check regression
    history = load_history()
    if history:
        last = history[-1]
        last_score = last["avg_score"]
        diff = avg_score - last_score
        pct_change = (diff / last_score * 100) if last_score > 0 else 0

        if pct_change < -10:
            print(f"  *** REGRESSION DETECTED ***")
            print(f"  Previous score: {last_score:.3f}")
            print(f"  Current score:  {avg_score:.3f}")
            print(f"  Change:         {pct_change:+.1f}%")
            print(f"  This change should NOT ship.")
            print()
        elif diff < 0:
            print(f"  Minor decrease: {last_score:.3f} -> {avg_score:.3f} ({pct_change:+.1f}%)")
        else:
            print(f"  Improvement: {last_score:.3f} -> {avg_score:.3f} ({pct_change:+.1f}%)")
        print()

    # Save to history
    entry = {
        "timestamp": datetime.now().isoformat(),
        "tag": tag or "manual",
        "avg_score": round(avg_score, 3),
        "java_score": round(java_avg, 3),
        "ts_score": round(ts_avg, 3),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "true_positives": total_tp,
        "total_expected": total_expected,
        "false_positives": total_fp,
        "total_should_not": total_should_not,
        "results": results,
    }
    history.append(entry)
    save_history(history)
    print(f"Score saved to {HISTORY_FILE}")

    # Individual test summary table
    print(f"\n{'='*50}")
    print(f"{'ID':<15} {'Score':>6} {'TP':>5} {'FP':>5} {'Issues':>7} {'Status':<6}")
    print(f"{'-'*50}")
    for r in results:
        print(f"{r['id']:<15} {r['score']:>6.2f} {r['true_positives']}/{r['expected_count']:>3} {r['false_positives']}/{r['should_not_count']:>3} {r['issues_found']:>7} {r['status']:<6}")

    return avg_score, results


def main():
    parser = argparse.ArgumentParser(description="Code Review Eval Harness")
    parser.add_argument("--url", default=DEFAULT_URL, help="API base URL")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detail per test")
    parser.add_argument("--tag", "-t", default="", help="Tag this run (e.g. 'new-prompt-v2')")
    parser.add_argument("--history", action="store_true", help="Show score history")
    args = parser.parse_args()

    if args.history:
        history = load_history()
        if not history:
            print("No history yet. Run an eval first.")
            return
        print(f"\n{'Timestamp':<26} {'Tag':<20} {'Score':>6} {'Java':>6} {'TS':>6} {'Pass':>5}")
        print("-" * 80)
        for h in history:
            print(f"{h['timestamp']:<26} {h['tag']:<20} {h['avg_score']:>6.3f} {h['java_score']:>6.3f} {h['ts_score']:>6.3f} {h['passed']:>3}/{h['passed']+h['failed']+h['errors']}")
        return

    score, _ = run_eval(args.url, args.verbose, args.tag)

    # Exit code: 1 if regression
    history = load_history()
    if len(history) >= 2:
        prev = history[-2]["avg_score"]
        if prev > 0 and ((score - prev) / prev * 100) < -10:
            sys.exit(1)


if __name__ == "__main__":
    main()

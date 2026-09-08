#!/usr/bin/env python3
"""
Scores candidate Bedrock models against the same adversarial fixture set
the current model was validated on, so a model swap is a measured decision
rather than a guess.

WHY: the binding constraint on how many pull requests SHIP can review per
minute is the per-model Bedrock requests-per-minute quota, and it varies
enormously on this account — every Claude model is capped at 10/min while
Amazon Nova Lite allows 200/min. Twenty times the throughput is worth
having, but only if detection quality survives, and detection quality IS
the product. A model that over-flags a display-only function, or misses
that a loan status is already human-gated, would trade the one thing that
makes SHIP worth running for speed nobody asked for.

WHAT IT MEASURES: the fixtures are the real planted violations in
pandayv/micro-finance PRs #1 and #2 — five genuine violations interleaved
with four deliberate false-positive look-alikes plus one subtle true
negative. Catching the violations is the easy half. Correctly DISMISSING
the look-alikes is what separates a compliance reviewer from a keyword
matcher, so both halves are scored equally.

Fragments are produced once, by the deterministic half of the pipeline
(split, screen, isolate), and then held constant across every model. Only
the model varies.

    python3 scripts/benchmark_models.py                  # default shortlist
    python3 scripts/benchmark_models.py amazon.nova-lite-v1:0 zai.glm-5

Each model runs in its own subprocess, because the model id is read at
import time and because separate processes give genuine isolation. Models
are run concurrently on purpose: each Bedrock model has its own quota
bucket, so parallelism across models costs nothing, while parallelism
within one model would just throttle it.
"""

import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_MODELS = [
    "amazon.nova-lite-v1:0",
    "us.amazon.nova-pro-v1:0",
    "zai.glm-5",
    "deepseek.v3.2",
    "qwen.qwen3-235b-a22b-2507-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",  # incumbent, the control
]

# Ground truth, keyed by the function each fragment contains. None means
# "no violation" — the look-alikes and the true negative, which a model
# must actively dismiss rather than merely fail to flag for the wrong
# reason.
EXPECTED: dict[str, str | None] = {
    "run_admin_command": "TLGP-001",            # shell exposed to an agent, ungated
    "run_nightly_backup": None,                 # look-alike: subprocess, but not agent-exposed
    "calculate_risk_adjustment": "ALBP-001",    # pincode materially adjusts the score
    "render_applicant_summary": None,           # look-alike: protected fields displayed, never scored
    "auto_triage_small_loans": "TLGP-002",      # AI verdict applied with no human checkpoint
    "cache_application_session": "PIIE-003",    # PII into an unencrypted cache
    "cache_current_interest_rate": None,        # look-alike: cache write, no PII
    "log_application_received": "PIIE-002",     # PII into a log sink
    "log_application_received_safely": None,    # look-alike: hashed before logging
    "get_ai_risk_opinion": "PIIE-001",          # raw profile to an external model
}


def build_fixtures() -> list[dict]:
    """Runs the deterministic pipeline once and labels each fragment."""
    from src.agents.screener import isolate_fragment, scan, split_diff_into_fragments
    from src.api.github_client import fetch_pr_diff

    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()

    fixtures = []
    for pr in (1, 2):
        diff = fetch_pr_diff("pandayv/micro-finance", pr, token=token)
        for frag in split_diff_into_fragments(diff):
            result = scan(frag["text"])
            if not result.matched:
                continue
            isolated = isolate_fragment(frag["text"], result.matched_terms)
            name = next((fn for fn in EXPECTED if f"def {fn}(" in frag["text"]), None)
            if name is None:
                continue  # not one of the labelled fixtures
            fixtures.append({
                "name": name, "file": frag["file"],
                "expected": EXPECTED[name], "fragment": isolated,
            })
    return fixtures


WORKER = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["SHIP_REPO_ROOT"])
from src.agents.diagnostician import diagnose

fixtures = json.loads(sys.stdin.read())
out = []
for f in fixtures:
    t = time.time()
    try:
        d = diagnose(f["fragment"])
        out.append({"name": f["name"], "expected": f["expected"],
                    "matched": d.matched, "got": d.taxonomy_id if d.matched else None,
                    "risk": d.risk_score, "citation": (d.citation or "")[:60],
                    "secs": round(time.time() - t, 1), "error": None})
    except Exception as e:
        out.append({"name": f["name"], "expected": f["expected"], "matched": None,
                    "got": None, "risk": None, "citation": "",
                    "secs": round(time.time() - t, 1),
                    "error": f"{type(e).__name__}: {str(e)[:90]}"})
print("###RESULTS###" + json.dumps(out))
'''


def run_model(model_id: str, fixtures: list[dict]) -> dict:
    env = {**os.environ, "SHIP_BEDROCK_MODEL_ID": model_id,
           "SHIP_MODEL_BACKEND": "bedrock", "SHIP_DIAGNOSTICIAN_MODE": "in_process",
           "SHIP_REPO_ROOT": str(REPO_ROOT)}
    started = time.time()
    proc = subprocess.run([sys.executable, "-c", WORKER], input=json.dumps(fixtures),
                          capture_output=True, text=True, env=env, timeout=2400)
    elapsed = time.time() - started

    marker = "###RESULTS###"
    if marker not in proc.stdout:
        return {"model": model_id, "fatal": (proc.stderr or proc.stdout)[-200:].strip(),
                "results": [], "elapsed": elapsed}
    results = json.loads(proc.stdout.split(marker, 1)[1].strip())
    return {"model": model_id, "fatal": None, "results": results, "elapsed": elapsed}


def score(run: dict) -> dict:
    tp_hit = tp_total = fp_ok = fp_total = errors = 0
    for r in run["results"]:
        if r["error"]:
            errors += 1
        if r["expected"] is not None:
            tp_total += 1
            if r["got"] == r["expected"]:
                tp_hit += 1
        else:
            fp_total += 1
            if r["matched"] is False:
                fp_ok += 1
    times = [r["secs"] for r in run["results"] if not r["error"]]
    return {"tp_hit": tp_hit, "tp_total": tp_total, "fp_ok": fp_ok, "fp_total": fp_total,
            "errors": errors, "median_secs": sorted(times)[len(times) // 2] if times else None}


def main() -> int:
    models = sys.argv[1:] or DEFAULT_MODELS
    print("Building fixtures from the real PR diffs...")
    fixtures = build_fixtures()
    print(f"{len(fixtures)} labelled fragments: "
          f"{sum(1 for f in fixtures if f['expected'])} violations, "
          f"{sum(1 for f in fixtures if not f['expected'])} look-alikes\n")

    missing = set(EXPECTED) - {f["name"] for f in fixtures}
    if missing:
        print(f"WARNING: never reached Diagnostician (Screener did not escalate): {sorted(missing)}\n")

    print(f"Running {len(models)} models concurrently (separate quota buckets)...\n")
    runs = []
    with cf.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {pool.submit(run_model, m, fixtures): m for m in models}
        for fut in cf.as_completed(futures):
            run = fut.result()
            runs.append(run)
            s = score(run)
            tag = "FATAL" if run["fatal"] else f"{s['tp_hit']}/{s['tp_total']} caught, {s['fp_ok']}/{s['fp_total']} dismissed"
            print(f"  done: {run['model']:<48} {tag}")

    print("\n" + "=" * 104)
    print(f"{'MODEL':<46} {'CAUGHT':>8} {'DISMISSED':>10} {'TOTAL':>7} {'ERR':>4} {'MED s':>7}")
    print("-" * 104)
    ranked = sorted(runs, key=lambda r: -(score(r)["tp_hit"] + score(r)["fp_ok"]))
    for run in ranked:
        if run["fatal"]:
            print(f"{run['model']:<46} {'FATAL':>8}   {run['fatal'][:40]}")
            continue
        s = score(run)
        total = f"{s['tp_hit'] + s['fp_ok']}/{s['tp_total'] + s['fp_total']}"
        print(f"{run['model']:<46} {s['tp_hit']}/{s['tp_total']:<6} {s['fp_ok']}/{s['fp_total']:<8} "
              f"{total:>7} {s['errors']:>4} {str(s['median_secs']):>7}")

    print("\nPer-case detail (only where a model disagreed with ground truth):")
    for run in ranked:
        bad = [r for r in run["results"]
               if (r["expected"] is not None and r["got"] != r["expected"])
               or (r["expected"] is None and r["matched"] is not False)]
        if bad:
            print(f"\n  {run['model']}")
            for r in bad:
                exp = r["expected"] or "no violation"
                got = r["error"] or (r["got"] or "no violation")
                print(f"    {r['name']:<34} expected {exp:<14} got {got}")

    out = REPO_ROOT / "benchmark_results.json"
    out.write_text(json.dumps(runs, indent=2))
    print(f"\nFull results: {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

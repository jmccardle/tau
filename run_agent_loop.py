#!/usr/bin/env python3
"""
τ Agent Loop Orchestrator

Runs an iterative implementation loop per subphase using pi sessions.
Each iteration calls 4 roles in sequence:
  1. Tester   → creates/updates tests
  2. Implementer → implements the subphase code
  3. QC        → reviews code quality + test results (JSON: success/feedback)
  4. Evaluator → checks completion criteria (JSON: success/feedback)

If QC or Evaluator reject, the loop restarts with their feedback appended
to the subphase doc. On approval, changes are committed and we move on.

Usage:
    python run_agent_loop.py                        # run all subphases from Phase 0
    python run_agent_loop.py 4.2                    # run only Phase 4 Subphase 2
    python run_agent_loop.py 4                      # run all of Phase 4
    python run_agent_loop.py --from 6.0             # resume from Phase 6 Subphase 0
    python run_agent_loop.py --skip 0.3             # skip subphase 0.3
    python run_agent_loop.py --iterations 6         # max 6 iterations per subphase
    python run_agent_loop.py --timeout 600          # 600s per pi session
    python run_agent_loop.py --dry-run              # show what would be done, don't run
    python run_agent_loop.py --help                 # show help
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent
DOCS_DIR = REPO_ROOT / "docs"
SRC_DIRS = [
    REPO_ROOT / "tau-ai",
    REPO_ROOT / "tau-agent-core",
    REPO_ROOT / "tau-coding-agent",
]
PI_TIMEOUT = int(os.environ.get("PI_TIMEOUT", "600"))  # seconds per pi session

class Config:
    """Mutable config shared across functions."""
    timeout: int = PI_TIMEOUT

config = Config()
DEFAULT_ITERATIONS = 4
DEFAULT_MAX_TURNS = 3 * DEFAULT_ITERATIONS  # 3 real turns + 1 final

# JSON output delimiters for pi responses
JSON_START = "<JSON_OUTPUT>"
JSON_END = "</JSON_OUTPUT>"

# ──────────────────────────────────────────────
# Subphase Discovery
# ──────────────────────────────────────────────

SUBPHASE_FILES = sorted(
    f.name
    for f in DOCS_DIR.glob("PHASE-*-SUBPHASE*.md")
    if f.suffix == ".md" and f.stem != "INDEX"
)


def parse_subphase_filename(filename: str) -> Optional[tuple[int, int]]:
    """Extract (phase, subphase) from a subphase doc filename.
    
    Handles both PHASE-0-SUBPHASE.md (Phase 0 has no sub-number)
    and PHASE-X-SUBPHASE-Y.md (standard pattern).
    """
    # Standard pattern: PHASE-X-SUBPHASE-Y.md
    m = re.match(r"PHASE-(\d+)-SUBPHASE-(\d+)\.md", filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Phase 0 special case: PHASE-0-SUBPHASE.md
    m = re.match(r"PHASE-(\d+)-SUBPHASE\.md", filename)
    if m:
        return int(m.group(1)), 0
    return None


def get_all_subphases() -> list[tuple[int, int, str]]:
    """Return list of (phase, subphase, filename) sorted by phase then subphase."""
    results = []
    for filename in SUBPHASE_FILES:
        parsed = parse_subphase_filename(filename)
        if parsed:
            results.append((*parsed, filename))
    return sorted(results, key=lambda x: (x[0], x[1]))


def subphase_filename(phase: int, sub: int) -> str:
    """Generate the expected filename for a subphase."""
    if phase == 0 and sub == 0:
        return "PHASE-0-SUBPHASE.md"
    return f"PHASE-{phase}-SUBPHASE-{sub}.md"


def parse_target(target: str) -> Optional[list[tuple[int, int, str]]]:
    """Parse a target like '4.2', '4', or '6.0' into subphase entries.
    
    Returns None to indicate 'all'.
    """
    all_subphases = get_all_subphases()
    
    if target == "":
        return all_subphases
    
    if "." in target:
        phase, sub = target.split(".", 1)
        try:
            p, s = int(phase), int(sub)
            filename = subphase_filename(p, s)
            result = [(p, s, filename)]
            if filename not in [s[2] for s in all_subphases]:
                print(f"⚠️  Subphase {target} not found")
                return None
            return result
        except ValueError:
            print(f"⚠️  Invalid target: {target}")
            return None
    else:
        try:
            phase = int(target)
            return [(p, s, f) for p, s, f in all_subphases if p == phase]
        except ValueError:
            print(f"⚠️  Invalid target: {target}")
            return None


# ──────────────────────────────────────────────
# Utility Functions
# ──────────────────────────────────────────────

def log(msg: str, prefix: str = ""):
    """Print a log message."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {prefix}{msg}", file=sys.stderr)


def read_subphase_doc(phase: int, sub: int) -> Path:
    """Return the path to a subphase doc.
    
    Phase 0 uses the special filename PHASE-0-SUBPHASE.md (no sub-number).
    """
    if phase == 0 and sub == 0:
        return DOCS_DIR / f"PHASE-0-SUBPHASE.md"
    return DOCS_DIR / f"PHASE-{phase}-SUBPHASE-{sub}.md"


def read_subphase_feedback(doc: Path) -> str:
    """Read accumulated feedback from the subphase doc."""
    if not doc.exists():
        return ""
    content = doc.read_text()
    
    # Extract feedback sections
    qc_match = re.search(r"## QC Feedback\n(.*?)(?=## Evaluator Feedback|$)", content, re.DOTALL)
    eval_match = re.search(r"## Evaluator Feedback\n(.*?)(?=\Z)", content, re.DOTALL)
    
    feedback = ""
    if qc_match:
        feedback += "### QC Feedback\n" + qc_match.group(1).strip() + "\n\n"
    if eval_match:
        feedback += "### Evaluator Feedback\n" + eval_match.group(1).strip() + "\n"
    
    return feedback.strip()


def append_feedback(doc: Path, role: str, feedback: str):
    """Append feedback to the subphase doc."""
    if not doc.exists():
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("")
    
    content = doc.read_text()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n- **{ts}** {role}: {feedback}"
    
    # Ensure feedback sections exist
    qc_section = f"## QC Feedback\n"
    eval_section = f"## Evaluator Feedback\n"
    
    if f"{role}:" not in content:
        # Append at end
        if content:
            content += "\n\n"
        content += f"{qc_section if role == 'qc' else eval_section}{entry}"
    else:
        # Find the section and append
        if role == "qc":
            if qc_section not in content:
                content += f"\n\n{qc_section}{entry}"
            else:
                content = content.replace(
                    qc_section,
                    qc_section + entry,
                    1
                )
        else:
            if eval_section not in content:
                content += f"\n\n{eval_section}{entry}"
            else:
                content = content.replace(
                    eval_section,
                    eval_section + entry,
                    1
                )
    
    doc.write_text(content)


def find_source_files(phase: int, sub: int) -> list[Path]:
    """Find source files that this subphase should create/modify."""
    doc = read_subphase_doc(phase, sub)
    if not doc.exists():
        return []
    
    content = doc.read_text()
    # Look for file paths mentioned in the doc
    file_patterns = [
        r'`tau_?ai[/\\].*?\.(?:py|toml|json|md)`,',
        r'`tau_?agent.?core[/\\].*?\.(?:py|toml|json|md)`,',
        r'`tau_?coding.?agent[/\\].*?\.(?:py|toml|json|md)`,',
        r'"tau_?ai[/\\].*?\.(?:py|toml|json|md)"',
        r'"tau_?agent.?core[/\\].*?\.(?:py|toml|json|md)"',
        r'"tau_?coding.?agent[/\\].*?\.(?:py|toml|json|md)"',
    ]
    
    found_paths = set()
    for pattern in file_patterns:
        for m in re.finditer(pattern, content):
            found_paths.add(m.group(0).strip('`,'))
    
    results = []
    for path_str in found_paths:
        # Normalize path separators
        normalized = path_str.replace("\\", "/")
        for src_dir in SRC_DIRS:
            candidate = src_dir / normalized
            if candidate.exists():
                results.append(candidate)
    
    # Always include test files
    for src_dir in SRC_DIRS:
        test_dir = src_dir / "tests"
        if test_dir.exists():
            results.extend(test_dir.glob("**/*.py"))
    
    # If no files found, include the expected structure
    if not results and phase > 0:
        expected = [
            f"tau_{'ai' if phase <= 1 else 'agent-core' if phase <= 3 else 'coding-agent'}",
        ]
    
    return results


def run_pi(prompt: str, timeout: int | None = None) -> tuple[str, bool]:
    """Run pi -p '<prompt>' and return (output, success)."""
    if timeout is None:
        timeout = Config.timeout
    log(f"Running pi (timeout={timeout}s)...")
    start = time.time()
    
    # Escape single quotes in the prompt
    escaped_prompt = prompt.replace("'", "'\\''")
    cmd = f"cd '{REPO_ROOT}' && timeout {timeout} pi -p '{escaped_prompt}'"
    
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        elapsed = time.time() - start
        log(f"pi completed in {elapsed:.1f}s (exit={result.returncode})")
        return result.stdout + result.stderr, result.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"⚠️  pi timed out after {timeout}s", "⚠️  ")
        return "", False
    except Exception as e:
        log(f"⚠️  pi error: {e}", "⚠️  ")
        return str(e), False


def extract_json(output: str) -> Optional[dict]:
    """Extract JSON from pi output, looking for JSON_OUTPUT markers."""
    # Try markers first
    start = output.find(JSON_START)
    end = output.rfind(JSON_END)
    if start >= 0 and end >= 0 and end > start:
        json_str = output[start + len(JSON_START):end].strip()
    else:
        # Try to find JSON object anywhere
        json_str = output
    
    # Find the first { and last } to extract JSON
    brace_start = json_str.find("{")
    brace_end = json_str.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        json_str = json_str[brace_start:brace_end + 1]
    
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def get_uncommitted_changes() -> str:
    """Get a summary of uncommitted changes."""
    try:
        result = subprocess.run(
            "cd '{}' && git status --short".format(REPO_ROOT),
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() or "(no changes)"
    except Exception:
        return "(git status failed)"


def get_recent_diff() -> str:
    """Get a brief diff of recent changes."""
    try:
        result = subprocess.run(
            "cd '{}' && git diff --stat HEAD".format(REPO_ROOT),
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() or "(no diff available)"
    except Exception:
        return ""


# ──────────────────────────────────────────────
# Role Prompts
# ──────────────────────────────────────────────

def build_tester_prompt(phase: int, sub: int, feedback: str) -> str:
    """Build the prompt for the Tester role."""
    doc_path = f"docs/PHASE-{phase}-SUBPHASE-{sub}.md"
    
    prompt = f"""You are the **Tester** for the τ (tau-agent-core) implementation.

## Your Task
Create or update tests for Phase {phase} Subphase {sub}.

## Context
- Read the subphase documentation: `{doc_path}`
- Read the cross-phase contracts: `docs/SUBPHASE-0.0.md`
- Read any previous feedback below
- Examine existing source and test files in the tau-* directories

## Requirements
1. Create test files that match the test cases listed in the subphase doc's "Testing Strategy" section
2. Tests must be runnable with `pytest` and produce clear pass/fail output
3. If the subphase doc already has specific test names/assertions, implement exactly those
4. Place test files in the appropriate `tau-*/tests/` directory
5. Use `asyncio`-compatible tests where the subphase involves async code

## Feedback from Previous Iterations
{feedback if feedback else "No feedback yet. Start fresh."}

## Output
After writing tests, run them with: pytest tau-*/tests/ -v

Report what you created/changed and whether the tests pass.
"""
    return prompt


def build_implementer_prompt(phase: int, sub: int, feedback: str) -> str:
    """Build the prompt for the Implementer role."""
    doc_path = f"docs/PHASE-{phase}-SUBPHASE-{sub}.md"
    
    prompt = f"""You are the **Implementer** for the τ (tau-agent-core) implementation.

## Your Task
Implement Phase {phase} Subphase {sub}.

## Context
- Read the subphase documentation: `{doc_path}`
- Read the cross-phase contracts: `docs/SUBPHASE-0.0.md`
- Read the parent phase docs if relevant (e.g., tau-agent-core.md for Phase 2)
- Read any previous feedback below
- Examine existing source and test files

## Requirements
1. Implement the code described in the subphase doc's "Implementation Outline"
2. Follow the type signatures and data contracts in `docs/SUBPHASE-0.0.md`
3. Match the "Implementation Outline" code skeletons — they define the expected API
4. Ensure all tests from the Tester pass
5. Do NOT change unrelated files
6. Write clean, well-documented Python 3.12+ code

## Feedback from Previous Iterations
{feedback if feedback else "No feedback yet. Start fresh."}

## Output
After implementing, run: pytest tau-*/tests/ -v
Report what you created/changed and whether the tests pass.
"""
    return prompt


def build_qc_prompt(phase: int, sub: int) -> str:
    """Build the prompt for the Quality Control role."""
    changes = get_uncommitted_changes()
    diff = get_recent_diff()
    
    prompt = f"""You are the **Quality Control** agent for the τ (tau-agent-core) implementation.

## Your Task
Evaluate the current uncommitted code for Phase {phase} Subphase {sub}.

## What to Check
1. **Code organization**: Is the code well-structured? Are types and modules coherently organized?
2. **Tests pass**: Run `pytest tau-*/tests/ -v` and verify all tests pass
3. **Test quality**: Do the tests actually test the right behavior? Are they comprehensive?
4. **No regressions**: Did any previously passing tests break?

## Repository State
Uncommitted changes:
```
{changes}
```
Recent diff:
```
{diff}
```

## Output Format
You MUST output a JSON object wrapped in these markers:

{JSON_START}
{{
    "success": true,
    "feedback": "Brief description of issues or 'LGTM - ready for evaluator'"
}}
{JSON_END}

Use "success": false if:
- Tests don't pass
- Code has obvious bugs
- Code is disorganized or incoherent
- Tests don't match the behavior being tested

Use "success": true if:
- Tests pass cleanly
- Code is well-organized and cohesive
- Tests accurately test the expected behavior
- No regressions

Be specific in your feedback.
"""
    return prompt


def build_evaluator_prompt(phase: int, sub: int, feedback: str) -> str:
    """Build the prompt for the Evaluator role."""
    doc_path = f"docs/PHASE-{phase}-SUBPHASE-{sub}.md"
    
    prompt = f"""You are the **Evaluator** agent for the τ (tau-agent-core) implementation.

## Your Task
Determine whether Phase {phase} Subphase {sub} is 100% complete.

## Read the Subphase Documentation
- Subphase doc: `{doc_path}`
- Cross-phase contracts: `docs/SUBPHASE-0.0.md`
- Parent design docs as needed

## Evaluation Criteria
1. **Completion criteria met**: Check every item in the "Done Criteria" section. Are they all true?
2. **Tests pass and are comprehensive**: All tests in the "Testing Strategy" section pass
3. **No outstanding feedback**: Check for feedback from QC and previous evaluator rounds
4. **Code quality**: Code matches the quality expected by the design docs
5. **Integration**: Does this subphase integrate correctly with adjacent subphases?

## Previous Feedback
{feedback if feedback else "No previous feedback."}

## Output Format
You MUST output a JSON object wrapped in these markers:

{JSON_START}
{{
    "success": true,
    "feedback": "Brief description or 'Subphase complete'"
}}
{JSON_END}

Use "success": false if:
- Any "Done Criteria" item is not met
- Tests fail or are missing
- Feedback from QC is not addressed
- Code is incomplete or broken

Use "success": true ONLY if every single criterion in "Done Criteria" is met and tests pass.

Be very specific about what's missing if you return false.
"""
    return prompt


# ──────────────────────────────────────────────
# Core Loop
# ──────────────────────────────────────────────

def run_subphase(phase: int, sub: int, max_iterations: int, dry_run: bool) -> bool:
    """Run the agent loop for one subphase.
    
    Returns True if the subphase completed successfully.
    """
    doc = read_subphase_doc(phase, sub)
    if not doc.exists():
        log(f"⚠️  Subphase doc not found: {doc}", "⚠️  ")
        return False
    
    log(f"▶️  Phase {phase}.{sub}: Starting loop (max {max_iterations} iterations)")
    log(f"   Doc: {doc}")
    
    for iteration in range(1, max_iterations + 1):
        log(f"\n{'='*60}")
        log(f"Phase {phase}.{sub} — Iteration {iteration}/{max_iterations}")
        log(f"{'='*60}")
        
        # Read accumulated feedback
        feedback = read_subphase_feedback(doc)
        if feedback:
            log(f"   Previous feedback:\n{feedback[:500]}...")
        
        # ── Step 1: Tester ──
        if not dry_run:
            tester_prompt = build_tester_prompt(phase, sub, feedback)
            log("   → Running Tester...")
            tester_output, tester_ok = run_pi(tester_prompt)
            if not tester_ok:
                log("   ⚠️  Tester failed (timeout/error)", "⚠️  ")
        else:
            log("   → [DRY-RUN] Would run Tester")
        
        # ── Step 2: Implementer ──
        if not dry_run:
            impl_prompt = build_implementer_prompt(phase, sub, feedback)
            log("   → Running Implementer...")
            impl_output, impl_ok = run_pi(impl_prompt)
            if not impl_ok:
                log("   ⚠️  Implementer failed (timeout/error)", "⚠️  ")
        else:
            log("   → [DRY-RUN] Would run Implementer")
        
        # ── Step 3: Quality Control ──
        if not dry_run:
            qc_prompt = build_qc_prompt(phase, sub)
            log("   → Running Quality Control...")
            qc_output, qc_ok = run_pi(qc_prompt)
            
            if not qc_ok:
                log("   ⚠️  QC failed (timeout/error)", "⚠️  ")
                append_feedback(doc, "QC", f"Session failed (timeout/error)")
                continue
            
            qc_result = extract_json(qc_output)
            if not qc_result:
                log("   ⚠️  QC output doesn't contain valid JSON", "⚠️  ")
                append_feedback(doc, "QC", "Failed to parse JSON output")
                continue
            
            qc_success = qc_result.get("success", False)
            qc_feedback = qc_result.get("feedback", "No feedback")
            log(f"   QC: {'✅ PASS' if qc_success else '❌ REJECT'} — {qc_feedback}")
            
            if not qc_success:
                append_feedback(doc, "QC", qc_feedback)
                continue  # Restart loop
        
        # ── Step 4: Evaluator ──
        if not dry_run:
            eval_prompt = build_evaluator_prompt(phase, sub, feedback)
            log("   → Running Evaluator...")
            eval_output, eval_ok = run_pi(eval_prompt)
            
            if not eval_ok:
                log("   ⚠️  Evaluator failed (timeout/error)", "⚠️  ")
                append_feedback(doc, "Evaluator", f"Session failed (timeout/error)")
                continue
            
            eval_result = extract_json(eval_output)
            if not eval_result:
                log("   ⚠️  Evaluator output doesn't contain valid JSON", "⚠️  ")
                append_feedback(doc, "Evaluator", "Failed to parse JSON output")
                continue
            
            eval_success = eval_result.get("success", False)
            eval_feedback = eval_result.get("feedback", "No feedback")
            log(f"   Evaluator: {'✅ PASS' if eval_success else '❌ REJECT'} — {eval_feedback}")
            
            if not eval_success:
                append_feedback(doc, "Evaluator", eval_feedback)
                continue  # Restart loop
        
        # ── Both passed! Commit and proceed ──
        if not dry_run:
            log(f"\n✅ Phase {phase}.{sub} APPROVED! Committing changes...")
            
            # Git add, commit, and optionally push
            try:
                subprocess.run(
                    f"cd '{REPO_ROOT}' && git add -A",
                    shell=True,
                    check=True,
                    timeout=10,
                )
                commit_msg = f"Phase {phase}.{sub}: implement subphase\n\n"
                commit_msg += f"Completion criteria met. Iterations: {iteration}\n\n"
                # Add QC and evaluator feedback summary
                if feedback:
                    commit_msg += f"Feedback addressed:\n{feedback[-200:]}\n"
                
                subprocess.run(
                    f"cd '{REPO_ROOT}' && git commit -m '{commit_msg}' --allow-empty",
                    shell=True,
                    check=True,
                    timeout=30,
                )
                log(f"   Committed with message:\n{commit_msg[:200]}")
            except Exception as e:
                log(f"   ⚠️  Commit failed: {e}")
                log("   But subphase is still considered complete.")
        else:
            log(f"\n✅ Phase {phase}.{sub} would be approved!")
        
        log(f"\n🎉 Phase {phase}.{sub} COMPLETE!")
        return True
    
    # Exhausted all iterations
    log(f"\n❌ Phase {phase}.{sub} FAILED: exhausted {max_iterations} iterations", "❌  ")
    log(f"   Final feedback appended to {doc}")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="τ Agent Loop Orchestrator — Implement the τ plan via pi sessions",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="",
        help="Subphase target: '4.2' = Phase 4 Subphase 2, '4' = all of Phase 4, '' = all",
    )
    parser.add_argument(
        "--from",
        dest="from_target",
        default=None,
        help="Resume from this subphase (e.g., '6.0')",
    )
    parser.add_argument(
        "--skip",
        dest="skip",
        nargs="+",
        default=[],
        help="Subphases to skip (e.g., '0.3' '1.0')",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"Max iterations per subphase (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=PI_TIMEOUT,
        help=f"Timeout per pi session in seconds (default: {PI_TIMEOUT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without running pi",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all subphases and exit",
    )
    
    args = parser.parse_args()
    
    # Set timeout (use module-level mutable wrapper)
    config.timeout = args.timeout
    
    # List mode
    if args.list:
        print("τ Subphases:")
        print("-" * 60)
        for phase, sub, filename in get_all_subphases():
            doc = read_subphase_doc(phase, sub)
            # Try to read the scope line
            scope = ""
            if doc.exists():
                content = doc.read_text()
                m = re.search(r"> \*\*Topic\*\*: (.*?)(?:\n|$)", content)
                if m:
                    scope = m.group(1).strip()
            marker = "  "
            print(f"  {marker}{phase}.{sub:1d}  {filename:40s}  {scope}")
        print(f"\n  Total: {len(get_all_subphases())} subphases")
        return
    
    # Determine target subphases
    if args.from_target:
        subphases = get_all_subphases()
        from_parsed = parse_target(args.from_target)
        if from_parsed:
            from_idx = next(
                (i for i, (p, s, _) in enumerate(subphases) if p == from_parsed[0][0] and s == from_parsed[0][1]),
                len(subphases),
            )
            subphases = subphases[from_idx:]
        else:
            subphases = get_all_subphases()
    elif args.target:
        subphases = parse_target(args.target)
        if subphases is None:
            subphases = get_all_subphases()
    else:
        subphases = get_all_subphases()
    
    # Filter skips
    skip_set = set()
    for s in args.skip:
        parsed = parse_target(s)
        if parsed:
            skip_set.add((parsed[0][0], parsed[0][1]))
    
    subphases = [(p, s, f) for p, s, f in subphases if (p, s) not in skip_set]
    
    if not subphases:
        print("No subphases to run.")
        return
    
    # Summary
    print(f"\n{'='*60}")
    print(f"  τ Agent Loop Orchestrator")
    print(f"{'='*60}")
    print(f"  Subphases: {len(subphases)}")
    print(f"  Max iterations/subphase: {args.iterations}")
    print(f"  Timeout per pi session: {args.timeout}s")
    print(f"  Dry run: {args.dry_run}")
    print(f"{'='*60}\n")
    
    # Run loop
    results = {}
    for phase, sub, filename in subphases:
        log(f"\n{'#'*60}")
        log(f"Phase {phase}.{sub}")
        log(f"{'#'*60}\n")
        
        success = run_subphase(phase, sub, args.iterations, args.dry_run)
        results[(phase, sub)] = success
    
    # Summary
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    failed = total - passed
    
    print(f"\n{'='*60}")
    print(f"  τ Agent Loop Summary")
    print(f"{'='*60}")
    print(f"  Total:  {total}")
    print(f"  Passed: {passed}")
    print(f"  Failed: {failed}")
    print(f"{'='*60}")
    
    # Show failures
    for (p, s), success in results.items():
        status = "✅" if success else "❌"
        print(f"  {status} Phase {p}.{s}")
    
    if failed > 0:
        sys.exit(1)
    else:
        print(f"\n🎉 All subphases passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()

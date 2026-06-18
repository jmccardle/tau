# τ Agent Loop Orchestrator

Automated iterative implementation loop using pi sessions to execute the τ implementation plan subphase by subphase.

## How It Works

For each subphase, the loop runs up to 4 iterations. Each iteration executes **4 pi sessions** in sequence:

```
Iteration:
  1. Tester      → creates/updates tests in tau-*/tests/
  2. Implementer → implements the subphase code
  3. QC          → reviews code quality + test results (JSON: success/feedback)
  4. Evaluator   → checks completion criteria (JSON: success/feedback)

If QC or Evaluator reject → append feedback to subphase doc → restart loop
If both approve → git commit → move to next subphase
```

## Usage

```bash
# Run all subphases sequentially (Phase 0 → Phase 6)
python run_agent_loop.py

# Run a specific subphase
python run_agent_loop.py 4.2          # Phase 4, Subphase 2
python run_agent_loop.py 2            # All of Phase 2

# Resume from a specific subphase
python run_agent_loop.py --from 5.0   # Start from Phase 5 Subphase 0

# Skip specific subphases
python run_agent_loop.py --skip 0.3 1.0

# Adjust timing
python run_agent_loop.py --iterations 6 --timeout 900

# Preview without running
python run_agent_loop.py --dry-run

# List all subphases
python run_agent_loop.py --list
```

## JSON Protocol

QC and Evaluator must output JSON wrapped in markers:

```
<JSON_OUTPUT>
{
    "success": true,
    "feedback": "All criteria met. Ready to commit."
}
</JSON_OUTPUT>
```

The orchestrator extracts JSON from the `pi -p` output using these delimiters.

## Feedback Mechanism

When QC or Evaluator reject, their feedback is appended to the subphase doc under:

- `## QC Feedback` — code quality issues
- `## Evaluator Feedback` — completion criteria gaps

On the next iteration, the Tester and Implementer read these feedback sections before working.

## Architecture

```
run_agent_loop.py (orchestrator)
├── parse_target()      — parse '4.2' → (phase=4, sub=2)
├── run_subphase()      — main loop for one subphase
├── build_tester_prompt()    — prompt for tester role
├── build_implementer_prompt() — prompt for implementer role
├── build_qc_prompt()        — prompt for QC role
├── build_evaluator_prompt() — prompt for evaluator role
└── extract_json()           — parse JSON from pi output
```

## Files

| File | Role |
|------|------|
| `run_agent_loop.py` | Orchestrator script |
| `docs/PHASE-*-SUBPHASE-*.md` | Subphase specifications |
| `docs/SUBPHASE-0.0.md` | Cross-phase data contracts |
| `docs/PHASES-INDEX.md` | Subphase dependency graph |

## Integration with Existing Docs

The orchestrator reads from all subphase docs to:
1. Determine what to implement (implementation outline)
2. Know what tests to write (testing strategy)
3. Verify completion (done criteria)

It writes back:
1. Source files in `tau-ai/`, `tau-agent-core/`, `tau-coding-agent/`
2. Test files in `tau-*/tests/`
3. Feedback appended to subphase docs

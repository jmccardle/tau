# Pi: planning → implementing → evaluating

Companion to `pi_orchestration_patterns.md`. That doc gave you the orchestration
primitives (`run_pi`, `fan_out`, `pipeline`, `refine_until`, `Ledger`, `MODELS`).
This one is the **method**: how to assign models across a plan→implement→evaluate
pipeline, how to steer small models in-loop, and how to stop them gaming the tests.

The whole thing is one idea: **message injection is feedback control.** The
reference signal is the spec / frozen interface / failing test; you observe state
through pi's lifecycle events; when error crosses a threshold you inject a
corrective message. [pi-system-reminders](https://github.com/Michaelliv/pi-system-reminders)
is a bank of bang-bang controllers (steering); [ponytail](https://github.com/DietrichGebert/ponytail)
is an always-on constraint (anti-sprawl); the gates below are the hard interlock.

Evidence this rests on: SLMs are sufficient/economical for repetitive specialized
agent work with LLMs reserved for hard/open-ended/long-context jobs (arXiv
2506.02153); test generation should be **decoupled from code** or it inherits the
code's blind spots (AgentCoder, arXiv 2312.13010); "make the tests pass" loops
invite **reward hacking** — hardcoding, editing/deleting tests, breaking the grader
— and access controls (read-only / held-out tests) cut it to near zero (EvilGenie
2511.21654; ImpossibleBench 2510.xxxxx); and spec-driven workflows warn that specs
**drift within hours** unless re-anchored.

---

## 1. The revised pipeline & routing

Big models do bounded, high-leverage judgment on a **distilled brief — never the
repo**. Small models do mechanical fill **inside a frozen interface**. No
escalation: when the small loop thrashes you get a *diagnostic hint*, not a
takeover.

| Stage | Model (`MODELS[...]`) | Why this size | Hard constraints |
|---|---|---|---|
| Architecture + library choice | `plan` (big) | design judgment, long horizon, expensive-if-wrong | fed only the brief; no repo read; one short session |
| **Frozen interface** (stubs+types) | `plan` (big) | boundaries set sprawl; cheap to get right here | signatures/docstrings only, no bodies; YAGNI |
| Acceptance criteria + **test suite** | `implement` (medium) | **the contract** — your highest-leverage step | ratify against spec; tests read-only afterward |
| Implement to green | `mechanical` (small/local) | mechanical fill inside a fixed interface | reminders on; gates must pass; cannot edit tests |
| Make-green loop | `mechanical` (small/local) | execution-feedback iteration | capped rounds; **stop** on thrash; bounded consult |
| Code review | `implement` (medium) | catch shortcuts + over-engineering | read-only; pair with `/ponytail-review` |
| Consult (only on thrash) | `plan`/`review` (big) | diagnosis, **not** takeover | hermetic, `--no-tools`, ≤200 words, tiny budget |

The one change from your draft: **don't leave the test suite solely to the small
model.** Tests are the oracle everything downstream optimizes toward; let the small
model draft them but have the medium model ratify them against the spec, like
reviewing a PR. Everything else matches your instinct.

```python
# Stage -> model mapping, reusing the MODELS roster from pi_orchestra.py
STAGES = {
    "architecture":      "plan",        # big, scoped, short
    "interface":         "plan",        # big: freeze signatures/types
    "acceptance+tests":  "implement",   # medium: ratify the contract
    "implement":         "mechanical",  # small/local
    "make_green":        "mechanical",  # small/local, in a feedback loop
    "review":            "implement",   # medium (your call)
    "consult_on_thrash": "review",      # big, diagnosis only (or medium; it only sees the pack)
}
```

---

## 2. In-loop steering: pi-system-reminders

Install once, then drop reminder files in `.pi/reminders/` (project) or
`~/.pi/agent/reminders/` (global):

```bash
pi install npm:pi-system-reminders
```

Each file exports a function that gets the `ExtensionAPI`, tracks state via
`pi.on(event, ...)`, and **returns** a reminder:
`{ on, when, message, cooldown?, once? }`. When `when()` is true a
`<system-reminder>` is injected.

> Reminders **steer**; they don't **block**. The hard guarantee against cheating
> is the gates in §3. For true path-protection you'd write a full pi extension
> with a `tool_call` veto — confirm that API in `docs/extensions.md`.
>
> Event payload field names (`args` vs `input` for `path`/`command`) are inferred
> from the repo's examples — confirm against your pi version. Access is defensive
> below so a wrong guess fails closed (no fire), not loud.

For small models these in-loop corrections matter *more* than for big ones — they
drift faster, so controller bandwidth (how often / with what you re-anchor) is
where your performance gains hide.

### `.pi/reminders/tests-readonly.ts` — don't let the coder touch the oracle

```ts
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const TEST_RE = /(^|\/)(tests?|spec)\/|(^|\/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$/;

export default function (pi: ExtensionAPI) {
  let editedTest: string | null = null;

  pi.on("tool_call", async (event: any) => {
    if (event.toolName === "edit" || event.toolName === "write") {
      const path = event.args?.path ?? event.input?.path ?? "";
      editedTest = TEST_RE.test(path) ? path : null;
    }
  });

  return {
    on: "tool_execution_end",
    when: ({ event }: any) =>
      (event?.toolName === "edit" || event?.toolName === "write") && editedTest !== null,
    message: () =>
      `You modified a test file (${editedTest}). Tests are the contract, not a target ` +
      `to bend. Revert that edit and fix the implementation. If a test contradicts the ` +
      `spec, STOP and report the conflict — do not edit it to pass.`,
    cooldown: 3,
  };
}
```

### `.pi/reminders/reanchor-contract.ts` — fight spec drift

```ts
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { readFileSync, existsSync } from "node:fs";

const CONTRACT = ".pi/CONTRACT.md";   // architecture + frozen interface summary
const EVERY = 8;                       // turns between re-anchors (via cooldown)

export default function (_pi: ExtensionAPI) {
  return {
    on: "turn_start",
    when: () => existsSync(CONTRACT),
    message: () =>
      `<contract reminder>\n${readFileSync(CONTRACT, "utf8").slice(0, 4000)}\n` +
      `</contract reminder>\nStay within this interface. Do not add modules, files, ` +
      `or dependencies it does not define.`,
    cooldown: EVERY,   // fires, then skips EVERY evaluations
  };
}
```

### `.pi/reminders/root-cause.ts` — break the limit-cycle without escalating

Your COCONUT work already showed these loops limit-cycle rather than converge.
Forcing a diagnosis step before the next edit is the cheapest way to perturb the
cycle.

```ts
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const TEST_CMD = /\b(pytest|npm test|go test|cargo test|jest|vitest)\b/;

export default function (pi: ExtensionAPI) {
  let failures = 0;

  pi.on("tool_result", async (event: any) => {
    if (event.toolName === "bash") {
      const cmd = event.args?.command ?? event.input?.command ?? "";
      if (TEST_CMD.test(cmd)) failures = event.isError ? failures + 1 : 0;
    }
  });

  return {
    on: "tool_execution_end",
    when: () => failures >= 2,
    message:
      "Tests have failed 2+ times in a row. Before editing again, write ONE paragraph " +
      "naming the root cause — which assumption was wrong and why. Do not edit until " +
      "you've stated it. If you've tried the same fix twice, change approach structurally.",
    cooldown: 4,
  };
}
```

### `.pi/reminders/scope-guard.ts` — interface-first, anti-sprawl

```ts
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { existsSync, readFileSync } from "node:fs";

const ALLOW = ".pi/scope.txt";   // newline-separated path prefixes the task may touch

export default function (pi: ExtensionAPI) {
  let offending: string | null = null;

  pi.on("tool_call", async (event: any) => {
    if (event.toolName === "write" || event.toolName === "edit") {
      const path = event.args?.path ?? event.input?.path ?? "";
      if (path && existsSync(ALLOW)) {
        const ok = readFileSync(ALLOW, "utf8").split("\n").map(s => s.trim()).filter(Boolean)
          .some(p => path.startsWith(p) || path.includes(p));
        offending = ok ? null : path;
      }
    }
  });

  return {
    on: "tool_execution_end",
    when: () => offending !== null,
    message: () =>
      `You touched ${offending}, outside this task's declared scope. Stay within the ` +
      `frozen interface. If it genuinely must change, stop and say why first.`,
    cooldown: 2,
  };
}
```

### `.pi/reminders/no-new-deps.ts` — enforce the top of ponytail's ladder

```ts
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const INSTALL = /\b(pip3?|uv|poetry)\s+(install|add)\b|\bnpm\s+(install|i|add)\b|\b(pnpm|yarn|bun)\s+add\b/;
const MANIFEST = /(requirements[^/]*\.txt|pyproject\.toml|package\.json|Cargo\.toml|go\.mod)$/;

export default function (pi: ExtensionAPI) {
  let tripped: string | null = null;

  pi.on("tool_call", async (event: any) => {
    if (event.toolName === "bash") {
      const cmd = event.args?.command ?? event.input?.command ?? "";
      tripped = INSTALL.test(cmd) ? `command: ${cmd}` : null;
    } else if (event.toolName === "edit" || event.toolName === "write") {
      const path = event.args?.path ?? event.input?.path ?? "";
      tripped = MANIFEST.test(path) ? `manifest: ${path}` : null;
    }
  });

  return {
    on: "tool_execution_end",
    when: () => tripped !== null,
    message: () =>
      `You're adding a dependency (${tripped}). Climb the ladder first: stdlib, then a ` +
      `native platform feature, then something already installed. New deps need sign-off.`,
    cooldown: 1,
  };
}
```

Also worth copying from the repo's own `examples/`: `file-churn.ts`,
`prefer-edit.ts`, `context-large.ts`. And install ponytail so its YAGNI ruleset
runs every turn: `pi install git:github.com/DietrichGebert/ponytail`.

---

## 3. Hard gates: anti-cheat + cohesion

The reminders ask nicely. These enforce. Put them in `pi_pipeline.py` next to
`pi_orchestra.py`.

```python
import subprocess
from pathlib import Path

def _run(cmd, cwd="."):
    p = subprocess.run(cmd, cwd=str(cwd), shell=isinstance(cmd, str),
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def verify_no_cheat(repo: str, *, visible_cmd: str, test_paths: list[str],
                    heldout_cmd: str | None = None) -> dict:
    """Detect test-gaming and grade on held-out tests.

    1) run the visible suite
    2) revert any agent edits under test_paths, re-run
         passed-before but fails-after  =>  it edited tests to pass  (cheating)
    3) run the held-out suite the implementer never saw  ->  the real grade
    """
    repo = str(repo)
    vis_rc, vis_out = _run(visible_cmd, repo)

    changed = []
    for tp in test_paths:
        _, out = _run(["git", "diff", "--name-only", "--", tp], repo)
        changed += [l for l in out.splitlines() if l.strip()]

    cheated = False
    if changed and vis_rc == 0:
        _run(["git", "checkout", "--", *changed], repo)     # revert test edits
        revert_rc, _ = _run(visible_cmd, repo)
        cheated = revert_rc != 0                             # only green because tests were edited

    held_rc, held_out = (None, "")
    if heldout_cmd:
        held_rc, held_out = _run(heldout_cmd, repo)

    passed = (vis_rc == 0) and (not cheated) and (held_rc in (None, 0))
    return {
        "passed": passed, "visible_ok": vis_rc == 0, "cheated": cheated,
        "edited_tests": changed,
        "heldout_ok": (held_rc == 0) if held_rc is not None else None,
        "detail": (vis_out[-1500:] if vis_rc else "")
                  + (("\n--- held-out ---\n" + held_out[-1500:]) if held_rc else ""),
    }


def deps_within_allowlist(repo: str, allow: set[str], manifest="pyproject.toml") -> dict:
    import re, tomllib
    p = Path(repo) / manifest
    if not p.exists():
        return {"passed": True, "added": []}
    data = tomllib.loads(p.read_text())
    deps = [re.split(r"[<>=!~ \[]", d, 1)[0].strip().lower()
            for d in data.get("project", {}).get("dependencies", [])]
    extra = sorted(set(deps) - {a.lower() for a in allow})
    return {"passed": not extra, "added": extra}


def cohesion_gate(repo: str = ".", *, gates: dict[str, str] | None = None) -> dict:
    """All gates pass, not just tests: format, lint, types, layering.
    Enforces house style + architecture mechanically. Configure each tool in pyproject."""
    gates = gates or {
        "format": "ruff format --check .",
        "lint":   "ruff check .",
        "types":  "mypy .",            # or pyright / ty
        "layers": "lint-imports",      # import-linter: enforce allowed inter-package deps
    }
    results, ok = {}, True
    for name, cmd in gates.items():
        rc, out = _run(cmd, repo)
        results[name] = {"ok": rc == 0, "out": out[-600:]}
        ok = ok and rc == 0
    return {"passed": ok, "gates": results}
```

The implement loop must clear **both** `cohesion_gate` and `verify_no_cheat`, not
just "tests green." Keep `tests_heldout/` listed in `.pi/scope.txt`'s *deny* sense
(never in the allowed set) so the coder can't read or touch it — the literature's
clearest finding is that access control works.

---

## 4. Bounded diagnostic consult (the only place a big model touches a stuck loop)

This respects "no escalation": the big model **diagnoses on a tiny scoped pack and
never sees the repo, never edits, never loops.** It returns a hint the small model
acts on.

```python
import ast
from pathlib import Path
from pi_orchestra import run_pi, Model, Limits   # from the first doc

def _signatures(py: Path) -> str:
    """Signatures + first docstring line, bodies stripped — the interface only."""
    try:
        src = py.read_text().splitlines()
        tree = ast.parse("\n".join(src))
    except (SyntaxError, OSError):
        return ""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
            body = node.body[0].lineno - 1 if node.body else node.lineno
            header = "\n".join(src[start:body]).rstrip()
            doc = ast.get_docstring(node)
            out.append(header + (f'\n    """{doc.splitlines()[0]}"""' if doc else ""))
    return "\n".join(out)

def _expand(repo: Path, items, suffix=".py"):
    out = []
    for it in items:
        p = repo / it
        if p.is_dir():
            out += [str(q.relative_to(repo)) for q in p.rglob(f"*{suffix}")]
        elif p.exists():
            out.append(it)
    return out

def build_context_pack(*, brief: str, contract: str | None = None,
                       interface_files=(), target_files=(), failing_tests=(),
                       repo: str = ".", char_budget: int = 12000) -> str:
    repo = Path(repo)
    parts = [f"# Brief\n{brief}\n"]
    if contract and (repo / contract).exists():
        parts.append(f"# Contract / architecture\n{(repo / contract).read_text()[:3000]}\n")
    sigs = "\n".join(_signatures(repo / f) for f in _expand(repo, interface_files))
    if sigs:
        parts.append(f"# Frozen interface (signatures only)\n```\n{sigs}\n```\n")
    for t in failing_tests:
        p = repo / t
        if p.exists():
            parts.append(f"# Failing test: {t}\n```python\n{p.read_text()[:2500]}\n```\n")
    for f in _expand(repo, target_files):
        parts.append(f"# Target: {f}\n```python\n{(repo / f).read_text()[:3000]}\n```\n")
    _, diff = _run(["git", "diff"], repo)
    if diff.strip():
        parts.append(f"# Current diff\n```diff\n{diff[:3000]}\n```\n")
    return "\n".join(parts)[:char_budget]


DIAGNOSE = (
    "You are a senior engineer doing DIAGNOSIS ONLY. You see a brief, the frozen "
    "interface, the failing test(s), the target file, and the diff — nothing else. "
    "Do NOT propose a rewrite or a patch. In under 200 words, name the single most "
    "likely root cause and the smallest corrective step, as a hint for a junior coder."
)

async def consult(pack: str, model: Model, *, max_usd: float = 0.25) -> str:
    # hermetic: no repo, no extensions, no skills, no tools, ephemeral, one shot
    r = await run_pi(
        f"{DIAGNOSE}\n\n{pack}", model,
        limits=Limits(max_usd=max_usd, max_turns=2, max_seconds=120),
        session_args=["--no-session"],
        extra_args=["-nc", "--no-extensions", "--no-skills", "--no-tools"],
    )
    return r.text
```

`--no-tools` is what makes this a *consult* and not an escalation: the big model
literally cannot read the codebase or run anything — it answers from the pack you
chose to show it. That is your "narrowly scope what big models see" constraint,
enforced by capability rather than discipline.

---

## 5. The make-green loop (no escalation) and the end-to-end driver

```python
from pi_orchestra import run_pi, Limits, MODELS

async def implement_until(goal, *, contract, interface_files, target_files,
                          visible_cmd, test_paths, heldout_cmd=None,
                          coder=MODELS["mechanical"], consultant=MODELS["review"],
                          max_rounds=8, max_usd=3.0, consult_budget=2, ledger=None):
    spent, sid, hint, consults = 0.0, None, "", 0
    for rnd in range(1, max_rounds + 1):
        prompt = (
            f"Goal:\n{goal}\nFrozen interface: {', '.join(interface_files)}\n"
            f"Implement ONLY within it to make `{visible_cmd}` pass. Do not edit tests "
            f"or add dependencies."
            + (f"\n\nDiagnostic hint:\n{hint}" if hint else "")
        )
        sess = ["--session", sid] if sid else ["--name", f"impl-{rnd}"]
        r = await run_pi(prompt, coder, limits=Limits(max_usd=max_usd - spent),
                         session_args=sess, ledger=ledger)
        sid = r.session_id or sid
        spent += r.cost_usd

        gate = cohesion_gate(".")
        cheat = verify_no_cheat(".", visible_cmd=visible_cmd,
                                test_paths=test_paths, heldout_cmd=heldout_cmd)
        if gate["passed"] and cheat["passed"]:
            return {"ok": True, "rounds": rnd, "spent": round(spent, 4)}

        # thrash -> ONE bounded consult, then keep the SAME (small) coder
        if r.stop_reason in ("stuck", "turns") and consults < consult_budget:
            pack = build_context_pack(brief=goal, contract=contract,
                                      interface_files=interface_files,
                                      target_files=target_files, failing_tests=test_paths)
            hint = await consult(pack, consultant); consults += 1
        else:
            # otherwise vary the signal: feed the real failure, force a root-cause step
            hint = ("Still failing. State the root cause, then make the smallest change.\n"
                    + (cheat["detail"] or str(gate["gates"]))[:1500])

        if spent >= max_usd:
            return {"ok": False, "rounds": rnd, "spent": round(spent, 4), "reason": "budget"}
    return {"ok": False, "rounds": max_rounds, "spent": round(spent, 4), "reason": "max_rounds"}


async def build_feature(brief: str, *, ledger=None):
    # 1+2. Architecture + frozen interface — big model, fed ONLY the brief
    await run_pi(
        "From the brief: choose libraries, write the architecture to .pi/CONTRACT.md, "
        "then create stub files (signatures, types, docstrings — NO implementations) "
        "for each module. Keep it minimal (YAGNI).\n\n"
        + build_context_pack(brief=brief),
        MODELS["plan"], limits=Limits(max_usd=0.75, max_turns=12),
        session_args=["--no-session"], ledger=ledger)

    # 3. Acceptance criteria + the test suite — medium model RATIFIES the contract
    await run_pi(
        "Read .pi/CONTRACT.md and the stubs. Turn the plan into testable acceptance "
        "criteria, then write the visible suite under tests/ encoding them. Tests follow "
        "the SPEC, not an assumed implementation. Also write 3-5 adversarial cases under "
        "tests_heldout/ (edge/abuse) — these are the real grade.",
        MODELS["implement"], limits=Limits(max_usd=0.75, max_turns=15),
        session_args=["--no-session"], ledger=ledger)

    # 4. Implement to green — small/local, gates + reminders + bounded consults
    result = await implement_until(
        brief, contract="CONTRACT.md",
        interface_files=["src/"], target_files=["src/"],
        visible_cmd="pytest -q tests/", test_paths=["tests/"],
        heldout_cmd="pytest -q tests_heldout/",
        coder=MODELS["mechanical"], consultant=MODELS["review"], ledger=ledger)

    # 5. Review for shortcuts + over-engineering — medium + ponytail
    await run_pi(
        "Review the diff for spec-violating shortcuts (hardcoding, special-casing, "
        "gaming tests) AND over-engineering. Then run /ponytail-review. Findings only.",
        MODELS["implement"], limits=Limits(max_usd=0.5), session_args=["--no-session"],
        extra_args=["--tools", "read,grep,find,ls,bash"], ledger=ledger)
    return result
```

What each model never does is as important as what it does: the big model never
reads the repo (only the brief / a pack), never loops, and only writes the contract
and stubs; the small model never edits tests, never adds deps, never leaves the
interface; the consult never touches the repo at all.

---

## 6. Confirm / tune

- **Reminder event fields** (`args` vs `input` for `path`/`command`) are inferred
  from the repo examples — verify on your pi version. The guards fail closed.
- **Reminders steer, they don't block.** The guarantees are `verify_no_cheat` +
  `cohesion_gate`. For real path-protection, write a pi extension with a `tool_call`
  veto (the README lists path protection as possible) and confirm the veto API.
- **Held-out hygiene:** keep `tests_heldout/` out of the coder's allowed scope and,
  ideally, out of its working tree during the loop. Access control is the single
  most effective anti-cheat lever in the literature.
- **Gate tooling must be configured:** `ruff`, `mypy`/`pyright`, and `import-linter`
  contracts (in `pyproject.toml` / `.importlinter`). The gate only runs what exists.
- **Escalation is still off by default.** The only big-model touch on a stuck loop
  is the hermetic `consult` (diagnosis). If you later decide a diagnosis-that-feeds-a-
  hint is too close to escalation for your taste, drop `consult_budget=0` and rely on
  signal-variation + root-cause + decomposition alone.
- **Cost ledger:** every `run_pi` already logs to your `Ledger`; group by stage/model
  (the first doc's `report`) to see $/passing-feature and which stage is the spend.

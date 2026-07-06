# τ Implementation Plan — Complete Index

This document indexes all subphase documents and defines the dependency graph between them.

## Dependency Graph

```
Phase 0 (Monorepo Setup)
  ├── Subphase 0.1: Workspace and Virtual Environment
  ├── Subphase 0.2: Package Scaffolding
  ├── Subphase 0.3: Cross-Phase Type Skeleton  ──┐
  └── Subphase 0.4: Testing Infrastructure        │
                                                 │
Phase 1 (τ-ai: OpenAI Provider) ─────────────────┼──▶ Phase 2 (Agent Core)
  ├── Subphase 1.0: Data Contract Definition ────┘   ├── Subphase 2.0: Data Contract
  ├── Subphase 1.1: Core Types Implementation        ├── Subphase 2.1: Agent Loop
  ├── Subphase 1.2: OpenAI Provider Implementation   ├── Subphase 2.2: Session Manager
  └── Subphase 1.3: Streaming Protocol & Client      ├── Subphase 2.3: Built-in Tools
                                                     └── Subphase 2.4: Agent Session & SDK
                                                              │
                                                     Phase 3 (Extensions) ──┐
  ├── Subphase 3.0: Data Contract Definition  ───┘                          │
  ├── Subphase 3.1: Event Bus                 ───┐                          │
  ├── Subphase 3.2: Extension Loader & Registry ─┤                          │
  └── Subphase 3.3: Extension API Surface        ─┤                         │
                                                  │                         │
Phase 4 (TUI: τ-coding-agent) ──────────────────┼─────────────────────────┤
  ├── Subphase 4.0: Data Contract Definition     │                         │
  ├── Subphase 4.1: TUI App Shell     ──────────┘                         │
  ├── Subphase 4.2: Agent-Aware Widgets     ──────────────────────────────┤
  └── Subphase 4.3: Session Tree & Input Bar ─────────────────────────────┤
                                                                         │
Phase 5 (Compaction & Session Features) ─────────────────────────────────┘
  ├── Subphase 5.0: Data Contract Definition
  ├── Subphase 5.1: Compaction Engine
  └── Subphase 5.2: Session Operations & Settings
                                                   │
Phase 6 (Polish & RPC) ────────────────────────────┘
  ├── Subphase 6.0: Data Contract Definition
  ├── Subphase 6.1: RPC Mode
  ├── Subphase 6.2: Session Export
  └── Subphase 6.3: Polish & Integration Tests
```

## Cross-Phase Data Contracts

The contracts defined in `SUBPHASE-0.0.md` are the immutable interface between phases. These are the types that cannot change without breaking downstream phases.

| Contract | Owner Phase | Consumers |
|----------|------------|-----------|
| Message types | Phase 1 | All |
| Tool definitions | Phase 1 | Phase 2, 3 |
| AbortSignal | Phase 1 | Phase 2, 3, 6 |
| Streaming protocol | Phase 1 | Phase 2 |
| AgentEvent | Phase 2 | Phase 3, 4 |
| AgentTool / AgentToolResult | Phase 2 | Phase 3 |
| Session entry JSON schema | Phase 2 | Phase 2, 5 |
| SessionManager interface | Phase 2 | Phase 2–6 |
| AgentSession interface | Phase 2 | Phase 4 |
| Extension API | Phase 3 | Phase 4, all extensions |
| EventBus interface | Phase 3 | Phase 2, 4 |
| TUI widget data types | Phase 4 | Phase 4 |
| RPC message format | Phase 6 | Phase 6 |

## Phase Summaries

### Phase 0 — Monorepo Setup (1 day)

Sets up the workspace, package scaffolding, type skeletons, and testing infrastructure. No actual implementation — just the foundation.

**Documents**: `PHASE-0-SUBPHASE.md` (4 subphases)

### Phase 1 — τ-ai (1 week)

Implements the OpenAI provider: types, tool definitions, provider, and streaming protocol. This is the foundation that everything else depends on.

**Documents**: `PHASE-1-SUBPHASE-0.md`, `PHASE-1-SUBPHASE-1.md`, `PHASE-1-SUBPHASE-2.md`, `PHASE-1-SUBPHASE-3.md` (4 subphases)

### Phase 2 — τ-agent-core (2 weeks)

Implements the agent loop, session manager, built-in tools, and the `AgentSession` public API. This is the core of τ — the agent runtime.

**Documents**: `PHASE-2-SUBPHASE-0.md`, `PHASE-2-SUBPHASE-1.md`, `PHASE-2-SUBPHASE-2.md`, `PHASE-2-SUBPHASE-3.md`, `PHASE-2-SUBPHASE-4.md` (5 subphases)

### Phase 3 — Extensions (1 week)

Implements the extension system: event bus, loader, registry, and API surface. Enables users to write Python modules that extend τ.

**Documents**: `PHASE-3-SUBPHASE-0.md`, `PHASE-3-SUBPHASE-1.md`, `PHASE-3-SUBPHASE-2.md`, `PHASE-3-SUBPHASE-3.md` (4 subphases)

### Phase 4 — TUI (1.5 weeks)

Implements the τ-coding-agent TUI: app shell, agent-aware widgets, session tree, and input bar. Forks Parley and replaces the backend with τ-agent-core.

**Documents**: `PHASE-4-SUBPHASE-0.md`, `PHASE-4-SUBPHASE-1.md`, `PHASE-4-SUBPHASE-2.md`, `PHASE-4-SUBPHASE-3.md` (4 subphases)

### Phase 5 — Compaction & Session Features (1 week)

Implements the compaction engine, session operations (fork, clone, navigate), and settings management.

**Documents**: `PHASE-5-SUBPHASE-0.md`, `PHASE-5-SUBPHASE-1.md`, `PHASE-5-SUBPHASE-2.md` (3 subphases)

### Phase 6 — Polish & RPC (1 week)

Implements RPC mode, session export, error handling, performance tuning, integration tests, and documentation.

**Documents**: `PHASE-6-SUBPHASE-0.md`, `PHASE-6-SUBPHASE-1.md`, `PHASE-6-SUBPHASE-2.md`, `PHASE-6-SUBPHASE-3.md` (4 subphases)

## Total: 27 Subphases

| Phase | Subphases | Days |
|-------|-----------|------|
| 0 | 4 | 1 |
| 1 | 4 | 5 |
| 2 | 5 | 10 |
| 3 | 4 | 5 |
| 4 | 4 | 7.5 |
| 5 | 3 | 5 |
| 6 | 4 | 5 |
| **Total** | **28** | **~38.5** |

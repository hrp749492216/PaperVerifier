> **STATUS: ALL ISSUES RESOLVED**
> All findings in this review have been addressed across multiple fix commits.
> This file is retained for historical reference only.
> See git log for the specific fix commits.

---

# PaperVerifier -- Comprehensive Code Review Report

**Date:** 2026-03-29
**Scope:** Full codebase audit (64 files, ~14,000 lines)
**Methodology:** 7 parallel deep-dive analyses covering security, implementation correctness, UI/UX, enterprise readiness, code quality/architecture, prompt engineering, and dependency/supply chain
**Verdict:** **Not production-ready.** 147 distinct issues identified across all domains.

---

## Executive Summary

PaperVerifier demonstrates strong foundational design in several areas: a well-structured LLM exception hierarchy, keyring-based secrets management, SSRF protection with DNS resolution, circuit breakers for external APIs, and provider-agnostic prompt engineering. However, the audit uncovered **12 critical bugs**, **31 high-severity issues**, **55 medium issues**, and **49 low issues** that collectively block enterprise deployment.

The five most impactful findings are:

1. **`str.format()` crashes on any LaTeX/code content** -- the vast majority of STEM papers will crash the verification pipeline
2. **SSRF bypass via HTTP redirects** -- the entire SSRF protection is circumventable
3. **Double-counting of all findings** -- the orchestrator adds consolidated findings alongside raw findings, inflating every metric
4. **PyMuPDF is AGPL-licensed** -- fundamentally incompatible with the proprietary license when served via Streamlit
5. **Zero tests, zero CI/CD** -- no quality gates of any kind exist

---

## Severity Distribution

| Severity | Count | Domains |
|----------|-------|---------|
| CRITICAL | 12 | Security (3), Implementation (3), UI/UX (2), Enterprise (4) |
| HIGH | 31 | Security (4), Implementation (4), UI/UX (8), Enterprise (7), Code Quality (5), Prompts (2), Dependencies (1) |
| MEDIUM | 55 | All domains |
| LOW | 49 | All domains |
| **Total** | **147** | |

---

## CRITICAL Issues (12) -- Must Fix Before Any Use

### CRIT-1: `str.format()` crashes on LaTeX curly braces

**Files:** `paperverifier/agents/base.py:337`, all agent `_run_analysis` methods
**Domain:** Implementation / Prompt Engineering

```python
return template.format(document_text=chunk.text)
```

Python's `str.format()` interprets `{` and `}` as format placeholders. Any document containing LaTeX (`\begin{equation}`, `$x^{2}$`, `\textbf{result}`), code listings, or JSON will crash with `KeyError`. This affects the **vast majority** of STEM research papers.

**Fix:** Escape curly braces in document text before substitution:
```python
safe_text = chunk.text.replace("{", "{{").replace("}", "}}")
return template.format(document_text=safe_text)
```

---

### CRIT-2: SSRF bypass via redirect following

**Files:** `paperverifier/parsers/url_parser.py:162` (aiohttp), `url_parser.py:222` (httpx)
**Domain:** Security

```python
async with session.get(url, allow_redirects=True) as response:  # aiohttp
async with httpx.AsyncClient(follow_redirects=True, ...) as client:  # httpx
```

URLs are validated for SSRF via `validate_url()`, but both HTTP clients follow redirects automatically. An attacker-controlled server at a public IP can issue a 302 redirect to `http://169.254.169.254/latest/meta-data/` (AWS metadata) or any internal service. The redirect target is never re-validated. **This completely bypasses all SSRF protection.**

**Fix:** Disable automatic redirects and re-validate each redirect target:
```python
async with session.get(url, allow_redirects=False) as response:
    if response.status in (301, 302, 307, 308):
        target = response.headers.get("Location")
        validate_url(target)  # Re-validate
        # Then follow manually
```

---

### CRIT-3: DNS rebinding / TOCTOU SSRF bypass

**File:** `paperverifier/security/input_validator.py:119-143`
**Domain:** Security

DNS resolution happens at validation time via `socket.getaddrinfo()`, but the HTTP client performs a second DNS resolution when making the actual request. An attacker controlling a DNS server can respond with a public IP for the first query and `169.254.169.254` for the second (classic DNS rebinding).

**Fix:** Resolve DNS once and pass the resolved IP directly to the HTTP client, or use a custom DNS resolver in the HTTP client configuration.

---

### CRIT-4: Double-counting of all findings in reports

**File:** `paperverifier/agents/orchestrator.py:444-457`
**Domain:** Implementation

```python
if consolidated_findings:
    orch_report = AgentReport(
        agent_role=AgentRole.ORCHESTRATOR.value,
        status="completed",
        findings=consolidated_findings,
    )
    report.agent_reports.append(orch_report)  # ADDED alongside originals
```

The orchestrator appends consolidated findings as an **additional** agent report. `compute_severity_counts()` and `generate_feedback_items()` iterate over **all** agent reports via `_all_findings()`. This means every finding is counted twice: once from the original agent and again from the orchestrator's synthesis. If 7 agents produce 20 findings and the orchestrator consolidates to 15, `total_findings` shows 35 instead of 15. All severity counts, feedback items, and the overall score are wrong.

**Fix:** Either replace original agent findings with consolidated findings, or filter `_all_findings()` to exclude originals when an orchestrator report exists.

---

### CRIT-5: PyMuPDF (AGPL-3.0) incompatible with proprietary license

**File:** `pyproject.toml:27`
**Domain:** Legal / Dependencies

```toml
"pymupdf>=1.24.0",
```

PyMuPDF is licensed under **AGPL-3.0**, which requires source code disclosure when the software is accessed over a network (i.e., the Streamlit web UI). This is **fundamentally incompatible** with the `LicenseRef-Proprietary` license declared in `pyproject.toml`.

**Fix:** Either (a) obtain a commercial PyMuPDF license from Artifex Software (~$5K/year), (b) replace PyMuPDF with pdfplumber-only (already the fallback), or (c) use pypdf (BSD-licensed) as the primary parser.

---

### CRIT-6: Zero tests exist

**Files:** `tests/unit/__init__.py`, `tests/integration/__init__.py`
**Domain:** Enterprise / Quality

Both test directories contain only empty `__init__.py` files. Zero test functions, zero coverage. `pytest` is configured in `pyproject.toml` but never used. Every code change is unvalidated.

---

### CRIT-7: Zero CI/CD pipeline

**Domain:** Enterprise

No `.github/workflows/`, no GitHub Actions, no GitLab CI, no pre-commit hooks. `pre-commit` is listed in dev dependencies but never configured. No automated linting, type checking, or security scanning.

---

### CRIT-8: No Dockerfile or deployment infrastructure

**Domain:** Enterprise

No `Dockerfile`, no `docker-compose.yaml`, no Kubernetes manifests, no Helm charts. No health check, readiness, or liveness endpoints anywhere in the codebase.

---

### CRIT-9: `conflicts` variable undefined on exception path (NameError crash)

**File:** `streamlit_app/pages/3_Apply.py:172`
**Domain:** UI/UX

```python
# Line 102-129: conflicts defined inside try block
try:
    conflicts = applier.detect_conflicts(...)
except Exception:
    force_mode = False  # conflicts never defined

# Line 172: references conflicts
applied_feedback = run_async(applier.apply(
    ..., force=force_mode if conflicts else False,  # NameError!
))
```

If conflict detection throws an exception, `conflicts` is never assigned. Clicking "Apply Changes" then crashes with `NameError: name 'conflicts' is not defined`.

**Fix:** Initialize `conflicts = []` before the try block.

---

### CRIT-10: Verification blocks entire Streamlit server

**File:** `streamlit_app/pages/1_Upload.py:291`
**Domain:** UI/UX

```python
report = run_async(orchestrator.verify(doc))
```

`run_async()` calls `loop.run_until_complete()`, blocking the Streamlit server thread for the entire multi-agent pipeline (potentially minutes). During this time, the app is completely unresponsive -- no other users can interact, and the current user cannot cancel.

---

### CRIT-11: No audit logging

**Domain:** Enterprise / Compliance

No record of who verified what document, which API keys were used, what findings were generated, or what feedback was applied. Blocks SOC 2, ISO 27001, and GDPR compliance.

---

### CRIT-12: External API client sessions never closed (resource leak)

**Files:** `paperverifier/external/crossref.py`, `openalex.py`, `semantic_scholar.py`
**Domain:** Code Quality

All three clients lazily create `aiohttp.ClientSession` instances with a `close()` method that is **never called** anywhere. No `__aenter__`/`__aexit__` protocol. Under sustained use, this causes TCP connection pool exhaustion.

---

## HIGH Issues (31)

### Security (4)

| # | Issue | File | Description |
|---|-------|------|-------------|
| HIGH-S1 | XML bomb in DOCX parsing | `parsers/docx_parser.py:272` | `etree.fromstring()` without `resolve_entities=False` allows billion-laughs XML bombs |
| HIGH-S2 | IPv4-mapped IPv6 bypass | `security/input_validator.py:33-43` | `::ffff:169.254.169.254` bypasses IP blocklist |
| HIGH-S3 | XSS pattern via `unsafe_allow_html` | `streamlit_app/pages/2_Review.py:139` | Establishes dangerous pattern; future data source changes create stored XSS |
| HIGH-S4 | Error messages leak internals | `pages/1_Upload.py:332-337` | Full stack traces with file paths, API details shown to users via `st.code(traceback.format_exc())` |

### Implementation (4)

| # | Issue | File | Description |
|---|-------|------|-------------|
| HIGH-I1 | Anthropic client created per-call, not cached | `llm/client.py:184` | New `AsyncAnthropic()` on every call; no connection reuse; potential resource leak. OpenAI clients are cached correctly. |
| HIGH-I2 | Circuit breaker state persists across `verify()` calls | `agents/orchestrator.py:89-90` | `_failure_counts` and `_disabled_agents` never reset. Stale counts from prior verifications can prematurely disable agents. |
| HIGH-I3 | Feedback items applied with stale character offsets | `feedback/applier.py:275-319` | After applying a change, subsequent items use `start_char`/`end_char` from the original document tree, but the text has shifted. Bottom-up sort only partially mitigates. |
| HIGH-I4 | Token usage metrics redacted in logs | `config.py:192-193` | Substring match for `"token"` causes `input_tokens` and `output_tokens` to be redacted to `[REDACTED]`, breaking observability. |

### UI/UX (8)

| # | Issue | File | Description |
|---|-------|------|-------------|
| HIGH-U1 | No file size limit on uploads | `pages/1_Upload.py:67` | Streamlit allows 200 MB by default; no application-level check. OOM crash risk. |
| HIGH-U2 | Progress bar never updates incrementally | `pages/1_Upload.py:267-308` | Bar stays at 0% during entire verification, jumps to 100%. Progress callback writes to unused dict. |
| HIGH-U3 | Checkbox selections persist across different reports | `pages/2_Review.py:268-271` | Keys like `cb_finding_3` survive new verifications. Findings from document A can be pre-selected for document B. |
| HIGH-U4 | `st.rerun()` erases success message after saving API key | `pages/4_Settings.py:105` | Success toast flashes briefly then vanishes. User can't confirm key was saved. |
| HIGH-U5 | No session cleanup or expiry | All Streamlit files | Documents and reports accumulate in server memory indefinitely. No "New Analysis" button. |
| HIGH-U6 | No multi-tab safety | All Streamlit files | Two tabs share session state. Concurrent uploads cause data corruption. |
| HIGH-U7 | No authentication on web UI | All Streamlit files | Anyone who can reach the server can use the app, view API key config, and consume tokens. |
| HIGH-U8 | Score color scale mismatch: CLI uses 0-10, Streamlit uses 0-1 | `cli.py:54-63` vs `pages/2_Review.py:115-123` | One of these is always wrong. Good papers show as red or bad papers as green. |

### Enterprise / Architecture (7)

| # | Issue | File | Description |
|---|-------|------|-------------|
| HIGH-E1 | No request/correlation IDs | `config.py:221-243` | Cannot trace a verification across agents, LLM calls, and external APIs in production logs. |
| HIGH-E2 | No metrics or instrumentation | Entire codebase | No Prometheus, OpenTelemetry, or any metrics library. Zero visibility into latency, error rates, token trends. |
| HIGH-E3 | No data retention or cleanup policy | `config.py:37` | Temp files and cloned repos accumulate. GDPR data minimization violation. |
| HIGH-E4 | No GDPR / data classification | Entire codebase | Document content sent to 8 LLM providers with no consent mechanism or disclosure. |
| HIGH-E5 | `estimated_cost_usd` never populated | `models/report.py:67` | Field exists, always `None`. No cost calculation despite token tracking. No budget limits. |
| HIGH-E6 | No lock file for dependencies | `pyproject.toml` | All 22 deps use `>=` only. Builds non-reproducible. Different installs get different versions. |
| HIGH-E7 | No deployment or operational documentation | Entire project | No runbook, no architecture docs, no deployment guide. |

### Code Quality (5)

| # | Issue | File | Description |
|---|-------|------|-------------|
| HIGH-Q1 | `cli.py` is 1,026-line God module | `cli.py` | Contains all 3 commands, all display helpers, all serializers. Should be split into 5+ files. |
| HIGH-Q2 | `run_async()` duplicated in 4 Streamlit files | `app.py`, all pages | Identical 4-line function copy-pasted. Uses deprecated `get_event_loop()`. |
| HIGH-Q3 | Parser factory logic duplicated 3 times | `parsers/router.py`, `url_parser.py`, `github_parser.py` | Adding a parser format requires changes in 3 places. |
| HIGH-Q4 | Private `_all_findings()` called from `cli.py` | `cli.py:248,908,981` | CLI directly calls a private method, violating encapsulation. |
| HIGH-Q5 | External API clients fully implemented but never called | `external/*.py` | `CrossRefClient`, `OpenAlexClient`, `SemanticScholarClient` exist but nothing in the pipeline invokes them. Dead code. |

### Prompts (2)

| # | Issue | File | Description |
|---|-------|------|-------------|
| HIGH-P1 | Most agents don't inform LLM about partial chunk context | All chunked agents | Only `HallucinationDetectionAgent` prepends chunk metadata. Others send fragments with no indication, causing false positives (e.g., "missing conclusion" when it's in the next chunk). |
| HIGH-P2 | No prompt injection defense | `utils/prompts.py` | Document text injected via `str.format()` with only `=== DELIMITER ===` markers. Adversarial paper content could override instructions. |

### Dependencies (1)

| # | Issue | File | Description |
|---|-------|------|-------------|
| HIGH-D1 | 4 unused dependencies installed | `pyproject.toml:25,26,31,30` | `pypdf`, `requests`, `gitpython`, `markdown` are never imported anywhere. Dead weight with CVE surface. |

---

## MEDIUM Issues (55)

### Security (10)

| # | Issue | File |
|---|-------|------|
| MED-S1 | Git clone size checked post-download, not during | `security/sandbox.py:316` |
| MED-S2 | API key used as dict cache key (memory exposure) | `llm/client.py:152` |
| MED-S3 | SDK exception messages may contain API keys/URLs | `llm/client.py:193-225` |
| MED-S4 | PDF page count unlimited (DoS via thousands of pages) | `parsers/pdf_parser.py:190` |
| MED-S5 | PDF parsing runs in-process (no sandbox for binary parsing) | `parsers/pdf_parser.py:165` |
| MED-S6 | python-docx may be vulnerable to XXE | `parsers/docx_parser.py:73` |
| MED-S7 | pypandoc spawns pandoc with untrusted LaTeX | `parsers/latex_parser.py:448` |
| MED-S8 | HTTP allowed after redirect (cleartext data) | `parsers/url_parser.py:162` |
| MED-S9 | DOI strings interpolated into API URLs unescaped | `external/crossref.py:158`, `openalex.py:163` |
| MED-S10 | No rate limiting on Streamlit uploads/verifications | Cross-cutting |

### Implementation (10)

| # | Issue | File |
|---|-------|------|
| MED-I1 | `_extract_json_block` finds first `[`/`{`, not the correct one | `utils/json_parser.py:168` |
| MED-I2 | `ResultsConsistencyAgent` sends unbounded text, no chunking | `agents/results_consistency.py:143` |
| MED-I3 | `SectionStructureAgent` sends full `document.full_text` unchecked | `agents/section_structure.py:100` |
| MED-I4 | `_extract_section_text` only recurses one subsection level | `agents/results_consistency.py:82-85` |
| MED-I5 | Sort comment says "applied last" but items are applied first | `feedback/applier.py:359-361` |
| MED-I6 | `_apply_change` validation fails after prior modifications shift text | `feedback/applier.py:476-491` |
| MED-I7 | `ReferenceVerificationAgent` role is `RESEARCH` but prompt key is `reference_verification` | `agents/reference_verification.py:162` |
| MED-I8 | `agents_total` defaults to 9 but only 7 agent classes exist | `models/report.py:64` |
| MED-I9 | `setup_logging()` called at import time, overrides existing config | `config.py:249` |
| MED-I10 | `Sentence.line_number` field never populated by any parser | `models/document.py:33` |

### UI/UX (15)

| # | Issue | File |
|---|-------|------|
| MED-U1 | Empty file upload not handled | `pages/1_Upload.py:73` |
| MED-U2 | Temp file leaked on parse failure | `pages/1_Upload.py:91-104` |
| MED-U3 | No URL format validation before fetch | `pages/1_Upload.py:118` |
| MED-U4 | No GitHub URL validation before clone | `pages/1_Upload.py:147` |
| MED-U5 | New document keeps stale downstream state | `pages/1_Upload.py:106` |
| MED-U6 | Filter changes trigger full page reruns | `pages/2_Review.py:223` |
| MED-U7 | Report export recomputed on every rerun | `pages/2_Review.py:367` |
| MED-U8 | "Apply Selected" doesn't auto-navigate | `pages/2_Review.py:347` |
| MED-U9 | HTML diff fixed at 600px | `pages/3_Apply.py:237` |
| MED-U10 | Downloads only as plain text (not original format) | `pages/3_Apply.py:284` |
| MED-U11 | No undo/re-apply capability | `pages/3_Apply.py` |
| MED-U12 | Escaped unicode `\\u2705` renders as literal text | `pages/4_Settings.py:76` |
| MED-U13 | API key field always empty even when configured | `pages/4_Settings.py:84` |
| MED-U14 | Summary shows unsaved config as current | `pages/4_Settings.py:284` |
| MED-U15 | No model validation on provider change | `pages/4_Settings.py:212` |

### Enterprise / Architecture (8)

| # | Issue | File |
|---|-------|------|
| MED-E1 | No distributed tracing | Entire codebase |
| MED-E2 | No environment-specific config profiles | `config.py` |
| MED-E3 | No feature flags | Entire codebase |
| MED-E4 | No REST/HTTP API for programmatic access | Entire codebase |
| MED-E5 | No graceful shutdown (SIGTERM handling) | Entire codebase |
| MED-E6 | No fallback provider configuration | `llm/roles.py:31` |
| MED-E7 | No caching strategy (re-verification wastes tokens) | Entire codebase |
| MED-E8 | No vulnerability scanning for dependencies | Entire codebase |

### Code Quality (8)

| # | Issue | File |
|---|-------|------|
| MED-Q1 | `nest_asyncio.apply()` called at import time in 5 files | Multiple |
| MED-Q2 | Version string duplicated in 3+ places | `__init__.py`, `cli.py`, `pyproject.toml` |
| MED-Q3 | Abstract extraction duplicated in 4 files | 3 parsers + `utils/text.py` |
| MED-Q4 | Magic number thresholds scattered (e.g., `30` in 4 parsers) | Multiple |
| MED-Q5 | `RoleAssignment` uses `@dataclass` while everything else uses Pydantic | `llm/roles.py:31` |
| MED-Q6 | `pdfplumber.open()` / `fitz.open()` not using context managers | `parsers/pdf_parser.py:172,252` |
| MED-Q7 | `get_settings()` singleton not thread-safe | `config.py:146-159` |
| MED-Q8 | `_all_findings()` recreates list on every call (called 4+ times) | `models/report.py` |

### Prompts (4)

| # | Issue | File |
|---|-------|------|
| MED-P1 | Full reference list repeated with every chunk in claim verification | `agents/claim_verification.py:102-126` |
| MED-P2 | 4096 `max_tokens` may truncate findings JSON silently | `config/llm_config.yaml.example` |
| MED-P3 | No structured output / JSON mode API usage | `llm/client.py` |
| MED-P4 | No per-agent deduplication for overlapping chunks | All chunked agents |

---

## Architectural Hotspots

### 1. The Feedback Application Pipeline Is Fundamentally Fragile

The feedback system has a cascading chain of bugs:
- **Stale offsets** (HIGH-I3): After applying change #1, change #2's character positions are wrong
- **Wrong sort order** (MED-I5): Comment says "applied last" but code does the opposite
- **Validation failures** (MED-I6): Pre-condition checks fail because text has shifted
- **Double-counted items** (CRIT-4): Users see duplicate findings to select from

**Recommendation:** Redesign feedback application to use a diff-patch model (like `unidiff`) rather than character-offset replacement. Re-index the document after each change, or compute offset deltas.

### 2. External API Integration Is Dead Code

`CrossRefClient`, `OpenAlexClient`, and `SemanticScholarClient` are fully implemented (~800 lines total) with rate limiting, circuit breakers, and error handling. But **nothing in the pipeline calls them**. The orchestrator accepts `external_data` as an optional dict that always defaults to `{}`. The `ReferenceVerificationAgent` accepts `api_results` kwargs but receives empty dicts.

This means:
- Reference verification relies entirely on the LLM's training data
- Novelty assessment has no access to actual related works
- The `RESEARCH` agent role exists but performs no external research

### 3. The Streamlit Architecture Cannot Scale

| Limitation | Impact |
|-----------|--------|
| `run_async()` blocks the server thread | One user's verification freezes all other users |
| No authentication | Anyone on the network can use the app |
| Session state in memory | No persistence across server restarts |
| `nest_asyncio` patches global loop | Incompatible with production ASGI servers |
| No background task execution | No way to cancel or monitor long verifications |

---

## Dependency Risk Matrix

| Dependency | Status | Risk |
|-----------|--------|------|
| `pymupdf>=1.24.0` | **AGPL-3.0** | License incompatible with proprietary |
| `pypdf>=4.0` | **Unused** | Remove immediately |
| `requests>=2.31` | **Unused** | Remove; has CVE-2024-35195 in older 2.31.x |
| `gitpython>=3.1` | **Unused** | Remove; has CVE-2024-22190 |
| `markdown>=3.5` | **Unused** | Remove |
| `aiohttp>=3.9` | **CVE risk** | Bump to `>=3.10.2` (CVE-2024-23334, CVE-2024-23829) |
| `streamlit>=1.38.0` | **~500MB** | Move to optional `[ui]` extra |
| `pypandoc>=1.13` | **System dep** | Requires `pandoc` binary; not documented |
| All dependencies | **No upper bounds** | `>=` only; no reproducible builds |

---

## Enterprise Readiness Scorecard

| Category | Score | Key Gap |
|----------|-------|---------|
| Security | 5/10 | SSRF bypass, XML bombs, no auth |
| Implementation correctness | 4/10 | Finding double-count, format crash, stale offsets |
| UI/UX | 3/10 | Server blocking, no progress, stale state |
| Testing | 0/10 | Zero tests |
| CI/CD | 0/10 | No pipeline |
| Deployment | 0/10 | No Docker, no health checks |
| Observability | 3/10 | Good structured logging; no metrics/traces/IDs |
| Compliance | 1/10 | No audit log, no retention, no consent |
| Cost management | 2/10 | Token tracking exists; no cost calc or budgets |
| Reliability | 7/10 | Circuit breakers, retries, timeouts -- well done |
| Secrets management | 7/10 | Keyring-based, redacted logs -- well done |
| Documentation | 2/10 | Good docstrings; no ops docs |
| Dependencies | 2/10 | AGPL conflict, unused deps, no lock file |
| **Overall** | **2.8/10** | |

---

## Prioritized Remediation Roadmap

### Phase 0: Stop-Ship (Before ANY deployment)

| Priority | Issue | Effort |
|----------|-------|--------|
| P0-1 | Fix `str.format()` LaTeX crash (CRIT-1) | 30 min |
| P0-2 | Fix SSRF redirect bypass (CRIT-2) | 2 hrs |
| P0-3 | Fix finding double-count (CRIT-4) | 1 hr |
| P0-4 | Replace/license PyMuPDF (CRIT-5) | 1 hr |
| P0-5 | Remove 4 unused dependencies (HIGH-D1) | 15 min |
| P0-6 | Bump aiohttp to >=3.10.2 | 5 min |
| P0-7 | Fix `conflicts` NameError (CRIT-9) | 5 min |
| P0-8 | Fix token metric redaction (HIGH-I4) | 15 min |
| P0-9 | Cache Anthropic client (HIGH-I1) | 30 min |
| P0-10 | Add XML bomb protection to DOCX parser (HIGH-S1) | 15 min |

### Phase 1: Foundation (Week 1-2)

- Initialize git, create CI/CD pipeline with ruff + mypy + pytest gates
- Write unit tests for: JSON parser, input validators, circuit breaker, chunking, models
- Create Dockerfile with health check endpoint
- Add correlation IDs to structured logging
- Fix feedback application pipeline (offset tracking)
- Wire up external API clients to the agent pipeline
- Add chunk context headers to all agents (follow HallucinationDetectionAgent pattern)

### Phase 2: Production Readiness (Week 3-4)

- Add authentication (OAuth2 proxy or Streamlit-Authenticator)
- Move verification to background task (threading or Celery)
- Add OpenTelemetry metrics and tracing
- Implement cost calculator and budget limits
- Add audit logging
- Create deployment documentation and runbook
- Add dependency lock file and vulnerability scanning

### Phase 3: Enterprise Hardening (Week 5-6)

- Replace Streamlit with FastAPI + frontend for production workloads
- Add environment-specific config profiles
- Implement provider failover chains
- Add data retention policy and automated cleanup
- GDPR compliance: consent mechanism, data classification
- SSO integration (SAML/OIDC)
- Add REST API for programmatic access

---

## Methodology Notes

This review was conducted by 7 specialized analysis agents running in parallel, each performing line-level code inspection:

1. **Security Audit** -- 33 findings across injection, SSRF, DoS, secrets exposure, TOCTOU
2. **Implementation Correctness** -- 32 findings across logic bugs, async issues, data model inconsistencies
3. **UI/UX Review** -- 46 findings across Streamlit anti-patterns, session state, error handling
4. **Enterprise Readiness** -- 40 gaps across deployment, observability, testing, compliance
5. **Code Quality & Architecture** -- 31 findings across coupling, duplication, performance
6. **Prompt Engineering** -- 21 findings across injection, chunking, token efficiency, compatibility
7. **Dependency & Supply Chain** -- 8 categories covering licensing, CVEs, unused deps, pinning

All findings include specific file paths, line numbers, and code snippets. Severity ratings follow a consistent scale:
- **CRITICAL**: Blocks deployment; data corruption, security exploit, or crash in normal usage
- **HIGH**: Must fix before production; significant risk or broken functionality
- **MEDIUM**: Should fix; quality, maintainability, or edge-case issues
- **LOW**: Nice to fix; style, minor edge cases, documentation

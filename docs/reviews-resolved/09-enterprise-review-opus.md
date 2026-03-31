> **STATUS: ALL ISSUES RESOLVED**
> All findings in this review have been addressed across multiple fix commits.
> This file is retained for historical reference only.
> See git log for the specific fix commits.

---

# PaperVerifier Enterprise Code Review

**Date:** 2026-03-30
**Reviewer:** Claude Opus 4.6 (5 parallel specialized agents)
**Scope:** Full codebase (~50 source files, all configuration, CI/CD, Docker)

---

## Executive Summary

Five specialized review agents (Security, Architecture, Correctness, Reliability, DevOps) performed an exhaustive enterprise-grade review of the PaperVerifier codebase. The review identified **119 distinct issues** across all dimensions:

| Severity | Count |
|----------|-------|
| CRITICAL | 12 |
| HIGH | 34 |
| MEDIUM | 43 |
| LOW | 30 |

The codebase demonstrates genuine security awareness (SSRF protection, input validation, magic byte checks, structured logging with redaction) and solid architectural foundations (multi-provider LLM abstraction, Pydantic data models, async I/O). However, critical issues remain in async/concurrency correctness, serialization integrity, prompt injection defense, deployment configuration, and build reproducibility.

---

## Top 20 Most Critical Issues (Deduplicated Across All Agents)

These are the highest-impact findings that require immediate attention, ordered by severity and cross-agent consensus.

---

### 1. CRITICAL: `consolidated_findings` Excluded from Serialization — Data Corruption on Report Round-Trip

**File:** `paperverifier/models/report.py:58-60`
**Agents:** Architecture, Correctness, Reliability (3/5 flagged)

`consolidated_findings` is declared with `exclude=True` in Pydantic's `Field`, meaning it is silently omitted from `model_dump_json()`. When a report is saved to disk and reloaded (e.g., for the `apply` CLI command), this field is `None`. The `_all_findings()` method then falls into the `else` branch and iterates ALL `agent_reports` including the orchestrator report, causing every finding to be double-counted. This corrupts `total_findings`, `severity_counts`, and feedback item generation.

```python
consolidated_findings: list[Finding] | None = Field(
    default=None, exclude=True,  # <-- silently dropped on serialization
)
```

**Fix:** Remove `exclude=True`, or add a `model_validator(mode="after")` that reconstructs `consolidated_findings` from the orchestrator's `AgentReport` after deserialization.

---

### 2. CRITICAL: `nest_asyncio.apply()` at Module Import Time — Corrupts Event-Loop Semantics System-Wide

**Files:** `paperverifier/cli.py:29-31`, `streamlit_app/utils.py:13-14`
**Agents:** Architecture, Reliability, DevOps (3/5 flagged)

`nest_asyncio.apply()` is called unconditionally at module **import time**. This patches the global asyncio event loop with re-entrancy support, permanently changing asyncio's thread-safety and cancellation invariants for the entire process. `asyncio.wait_for` timeouts may be delayed or ignored. In the Streamlit server, the patch applies to every user's session.

```python
import nest_asyncio
nest_asyncio.apply()  # Runs on import — affects entire process
```

**Fix:** Remove `nest_asyncio.apply()` entirely. The CLI should call `asyncio.run()` once. The Streamlit background thread already creates its own event loop correctly.

---

### 3. CRITICAL: `CircuitBreaker` Uses `threading.Lock` in Async Context — Blocks Entire Event Loop

**File:** `paperverifier/external/rate_limiter.py:138`
**Agents:** Architecture, Correctness, Reliability (3/5 flagged)

`CircuitBreaker` uses `threading.Lock` for `can_execute()`, `record_success()`, and `record_failure()`. These are called from async coroutines running on the event loop thread. A blocking `threading.Lock` acquire stalls the entire event loop, freezing all concurrent I/O. With 3 external API clients all using this pattern, the impact compounds under load.

```python
self._lock = threading.Lock()  # Blocks event loop thread

def can_execute(self) -> bool:
    with self._lock:  # Called from async context
        ...
```

**Fix:** Replace `threading.Lock` with `asyncio.Lock` and make all three methods `async def`.

---

### 4. CRITICAL: Prompt Injection — XML Boundary Escape in Document Content

**File:** `paperverifier/utils/prompts.py:84-102`
**Agent:** Security

All prompts wrap document text in XML-like tags (e.g., `<document_content>{document_text}</document_content>`). A paper containing the literal text `</document_content>` followed by new instructions will break out of the document boundary, allowing prompt injection. There is no sanitization of closing tags in document text before interpolation.

```python
SECTION_STRUCTURE_USER = """...
<document_content>
{document_text}        # <-- unsanitized; can contain </document_content>
</document_content>
```

**Fix:** Escape `<` and `>` characters in `document_text` before interpolation, or use a boundary marker that cannot appear in natural text (e.g., random UUID-based delimiters).

---

### 5. CRITICAL: Stored XSS via LLM-Controlled HTML Rendered in Streamlit

**File:** `streamlit_app/pages/3_Apply.py:235-236`
**Agent:** Security

HTML diff content derived from LLM-generated `Finding.suggestion` fields is rendered directly via `st.components.v1.html()`. A malicious paper can perform prompt injection to have the LLM emit a suggestion containing HTML/JavaScript. When the user renders the diff, that content executes in the Streamlit iframe context. Additionally, `diff_generator.py` shadows the `html` module import with a local variable named `html`, preventing `html.escape()` from working correctly in that scope.

```python
html_diff = DiffGenerator.html_diff(applied.original_text, applied.modified_text)
st.components.v1.html(html_diff, height=800, scrolling=True)  # Unsanitized LLM output
```

**Fix:** Sanitize the `suggestion` field from all LLM findings. Use a server-side HTML sanitizer (e.g., `bleach`) on `html_diff`. Rename the shadowed local variable.

---

### 6. CRITICAL: SSRF via DNS Rebinding — Validation at Check Time, Request at Use Time

**File:** `paperverifier/security/input_validator.py:118-122`
**Agents:** Security, Architecture, Reliability (3/5 flagged)

`validate_url()` resolves DNS at validation time and checks the IP against a blocklist. The actual HTTP request resolves DNS again via `aiohttp`. A DNS rebinding attack returns a public IP for validation, then switches to a private/loopback IP for the actual connection, bypassing SSRF protection entirely.

```python
_check_hostname_ip(hostname)  # DNS resolved here at validation time

# Later in url_parser.py:
async with session.get(current_url):  # DNS re-resolved by aiohttp — rebinding window
```

**Fix:** Perform IP validation at TCP connection time using a custom `aiohttp.TCPConnector` with a resolver that checks resolved IPs before establishing connections.

---

### 7. CRITICAL: No Process Supervisor (PID 1 Problem) in Dockerfile

**File:** `Dockerfile:45-49`
**Agent:** DevOps

`streamlit` runs as PID 1. Python does not properly handle signals or reap zombie processes as PID 1, so `docker stop` hangs for 10 seconds before SIGKILL. No `tini` or `dumb-init` is present.

**Fix:** `RUN apt-get install -y tini` and `ENTRYPOINT ["/usr/bin/tini", "--"]`

---

### 8. CRITICAL: Non-Reproducible Builds — No Dependency Lockfile

**Files:** `Dockerfile:28`, `.github/workflows/ci.yml:30,65,95`, `pyproject.toml`
**Agent:** DevOps

All 17 runtime dependencies use `>=` lower bounds with no upper bounds and no lockfile. Every build resolves the latest compatible versions, making builds non-reproducible. A new `aiohttp`, `pydantic`, or `anthropic` release can silently break previously-passing builds.

**Fix:** Generate and commit a `requirements.lock` via `pip-compile` or `uv lock`.

---

### 9. CRITICAL: Dev Source Mounts in Production docker-compose.yml

**File:** `docker-compose.yml:9-11`
**Agents:** Security, DevOps (2/5 flagged)

Volume mounts shadow the built image with host source code. This is a development pattern present in the only compose file, with no production/dev split. An attacker modifying host files (e.g., replacing `auth.py`) immediately changes production behavior.

```yaml
volumes:
  - ./paperverifier:/app/paperverifier    # Overrides built image
  - ./streamlit_app:/app/streamlit_app
```

**Fix:** Split into `docker-compose.yml` (production, no mounts) and `docker-compose.override.yml` (development with mounts).

---

### 10. HIGH: Orchestrator `_failure_counts` Race Condition Under Concurrent Agent Execution

**File:** `paperverifier/agents/orchestrator.py:90-91, 309-313`
**Agent:** Correctness

`_failure_counts` (defaultdict) and `_disabled_agents` (set) are modified by concurrent coroutines in `asyncio.gather` with no locking. Concurrent failures can cause the circuit breaker to trip incorrectly.

```python
self._failure_counts[role_name] += 1  # Not atomic across await points
if self._failure_counts[role_name] >= _CIRCUIT_BREAKER_THRESHOLD:
    self._disabled_agents.add(role_name)
```

**Fix:** Protect with `asyncio.Lock`.

---

### 11. HIGH: `text_parts` Potentially Unbound — `UnboundLocalError` in PDF Parser

**File:** `paperverifier/parsers/pdf_parser.py:182-219`
**Agent:** Correctness

If an exception occurs between `pdfplumber.open()` and `text_parts: list[str] = []`, the `finally` block closes the PDF, then line 219 references `text_parts` which was never assigned, raising `UnboundLocalError`.

**Fix:** Initialize `text_parts = []` before the `try` block.

---

### 12. HIGH: DOCX Section `char_offset` Calculation Diverges from `full_text`

**File:** `paperverifier/parsers/docx_parser.py:236`
**Agent:** Correctness

`char_offset += len(heading) + len(body) + 4` is an approximation that diverges from the actual `"\n\n".join()` offsets after the first section. All downstream `start_char`/`end_char` values on `Section` objects are wrong.

**Fix:** Use `full_text.find(heading, search_pos)` to anchor each section's actual position.

---

### 13. HIGH: `AsyncRateLimiter.acquire()` Rate-Check and Timestamp Append Not Atomic

**File:** `paperverifier/external/rate_limiter.py:68-83`
**Agent:** Correctness

The window check and timestamp append happen in two separate critical sections with an `asyncio.sleep()` in between (outside the lock). Multiple coroutines can compute the same sleep time and burst simultaneously at window boundaries.

**Fix:** Hold the lock across the sleep, or use a token-bucket pattern with atomic check-and-append.

---

### 14. HIGH: Double Crossref HTTP Fetch for Every DOI

**File:** `paperverifier/external/enrichment.py:122-128`
**Agent:** Correctness

`_lookup_reference` calls both `crossref.verify_doi(ref.doi)` and `crossref.check_retraction(ref.doi)`, both hitting the same Crossref REST endpoint. This doubles HTTP requests per DOI, potentially triggering rate limiting.

**Fix:** Extract retraction status from the already-fetched `cr_data` response.

---

### 15. HIGH: Orchestrator Calls Private Methods Across Class Boundaries

**File:** `paperverifier/agents/orchestrator.py` (`_synthesize()`)
**Agent:** Architecture

`_synthesize()` calls `orch_agent._call_llm()` and `orch_agent._parse_findings()` — private methods. This creates tight coupling and bypasses `analyze()`'s token tracking, error wrapping, and structured report construction.

**Fix:** Add a public `synthesize()` method to `SynthesisAgent`.

---

### 16. HIGH: `WriterAgent._run_analysis()` Returns Empty List — Liskov Substitution Violation

**File:** `paperverifier/agents/writer.py`
**Agent:** Architecture

`WriterAgent` inherits from `BaseAgent` but overrides `_run_analysis()` to return `[]`. Callers who invoke `analyze()` get back an empty `AgentReport` with no error indication. The class violates the base class contract.

**Fix:** `WriterAgent` should not inherit from `BaseAgent`, or `_run_analysis()` should raise `NotImplementedError`.

---

### 17. HIGH: No Global Pipeline Timeout — Verification Can Run Indefinitely

**File:** `paperverifier/agents/orchestrator.py:97-171`
**Agent:** Reliability

Individual LLM calls have 180s timeouts, but there is no timeout on the entire `verify()` pipeline. With 7 agents x N chunks x 180s each, the total wall time is unbounded. In Streamlit, the polling loop blocks the server thread indefinitely.

**Fix:** Add `asyncio.wait_for(orchestrator.verify(...), timeout=settings.pipeline_timeout)`.

---

### 18. HIGH: `setup_logging()` Never Called in Streamlit — No Log Redaction Active

**File:** `streamlit_app/app.py`
**Agents:** Reliability, DevOps (2/5 flagged)

The CLI calls `setup_logging()`, but Streamlit never does. Without initialization, the `_redact_sensitive_keys` processor (which strips API keys from logs) is never active. API keys and document content may appear in logs.

**Fix:** Call `setup_logging()` in `streamlit_app/app.py` after `get_settings()`.

---

### 19. HIGH: mypy Type Check Silently Passes in CI (`|| true`)

**File:** `.github/workflows/ci.yml:41`
**Agent:** DevOps

`mypy paperverifier --ignore-missing-imports || true` makes the step always succeed. Type errors never fail the pipeline despite `strict = true` in pyproject.toml.

**Fix:** Remove `|| true`. Fix existing mypy errors.

---

### 20. HIGH: GitHub Actions Not Pinned to SHA Digests (Supply Chain Risk)

**File:** `.github/workflows/ci.yml`
**Agent:** DevOps

All GitHub Actions use mutable version tags (`@v4`, `@v5`). A compromised tag could execute malicious code in CI with full access to secrets.

**Fix:** Pin every action to its SHA commit hash.

---

## Complete Issue Catalog by Category

### Security (28 issues)

| # | Sev | Issue | File |
|---|-----|-------|------|
| S1 | CRIT | Stored XSS via LLM-controlled HTML diff | `3_Apply.py:235` |
| S2 | CRIT | Prompt injection via XML boundary escape | `prompts.py:84` |
| S3 | CRIT | SSRF via DNS rebinding (TOCTOU) | `input_validator.py:118` |
| S4 | HIGH | Plaintext password in memory, no rate limiting | `auth.py:39-72` |
| S5 | HIGH | API keys cached as plaintext in `_api_keys` dict | `client.py:91-92` |
| S6 | HIGH | Nested key redaction only checks top-level keys | `config.py:194-203` |
| S7 | HIGH | pypandoc executes system pandoc with user content | `latex_parser.py:468` |
| S8 | HIGH | Temp file suffix from user-controlled filename | `1_Upload.py:100-106` |
| S9 | HIGH | CRLF injection via user-controlled email in User-Agent | `crossref.py:80-86` |
| S10 | HIGH | Dev volumes in docker-compose (code injection risk) | `docker-compose.yml:9-12` |
| S11 | HIGH | DEBUG log level in docker-compose | `docker-compose.yml:15` |
| S12 | HIGH | `nest_asyncio.apply()` security/concurrency | `cli.py:31`, `utils.py:14` |
| S13 | MED | GitHub URL pattern allows `.` in owner names | `input_validator.py:59` |
| S14 | MED | LaTeX `\input{}` root_dir from subdirectory | `latex_parser.py:264` |
| S15 | MED | SHA-256 without salt for password comparison | `auth.py:24-27` |
| S16 | MED | `validate_file_path` uses `strict=False` for symlinks | `input_validator.py:361` |
| S17 | MED | No rate limiting on LLM calls from UI (cost DoS) | `1_Upload.py` |
| S18 | MED | No privacy consent gate before sending to LLM | `1_Upload.py:298` |
| S19 | MED | `cleanup_temp_dir` `rmtree` on original path, not resolved | `sandbox.py:344-353` |
| S20 | MED | `max_document_size_mb` up to 500 MB (memory DoS) | `config.py:91` |
| S21 | MED | `json5.loads()` with no input size limit | `json_parser.py:105` |
| S22 | LOW | `.docx` magic bytes only checks ZIP header | `input_validator.py:52` |
| S23 | LOW | `.doc` in safe clone extensions (legacy OLE2 risk) | `sandbox.py:31` |
| S24 | LOW | `config_dir` overridable to arbitrary paths | `config.py:132` |
| S25 | LOW | No CSP headers on Streamlit deployment | `Dockerfile` |
| S26 | LOW | Broad dependency version ranges (CVE risk) | `pyproject.toml` |
| S27 | LOW | Audit logs not separated from app logs | `audit.py` |
| S28 | LOW | Blocking DNS in async context | `input_validator.py:136` |

### Architecture & Design (25 issues)

| # | Sev | Issue | File |
|---|-----|-------|------|
| A1 | CRIT | `nest_asyncio.apply()` at module import | `cli.py:31`, `utils.py:14` |
| A2 | CRIT | Orchestrator calls private methods across class boundaries | `orchestrator.py` |
| A3 | CRIT | `consolidated_findings` excluded from serialization | `report.py:58` |
| A4 | HIGH | Chunk-loop pattern duplicated across 5 agent subclasses | 5 agent files |
| A5 | HIGH | 3 external API clients share ~200 lines of identical code | 3 external files |
| A6 | HIGH | `CircuitBreaker` uses `threading.Lock` in async context | `rate_limiter.py` |
| A7 | HIGH | DNS rebinding SSRF (TOCTOU) | `input_validator.py` |
| A8 | HIGH | `WriterAgent` Liskov Substitution violation | `writer.py` |
| A9 | HIGH | `run_async()` blocks Streamlit server thread | `utils.py` |
| A10 | MED | `CONFIG_DIR` defined in two modules independently | `config.py`, `config_store.py` |
| A11 | MED | Fixed 4-chars/token heuristic in `count_tokens_estimate()` | `chunking.py` |
| A12 | MED | Truncated UUID IDs (12 chars) increase collision risk | `document.py`, `findings.py` |
| A13 | MED | `_all_findings()` is private but accessed from CLI | `report.py`, `cli.py` |
| A14 | MED | O(n^2) inner loop in `_detect_figure_table_refs()` | `base.py` |
| A15 | MED | Double `asyncio.wait_for` — outer guard is dead code | `base.py`, `client.py` |
| A16 | MED | `MODEL_CONTEXT_WINDOWS` hardcoded in chunker | `chunking.py` |
| A17 | MED | `compute_estimated_cost()` uses hardcoded pricing | `report.py` |
| A18 | LOW | `html` variable shadows `import html` in diff_generator | `diff_generator.py` |
| A19 | LOW | `DiffGenerator` is all static methods (should be module functions) | `diff_generator.py` |
| A20 | LOW | `_register_builtin_parsers()` at import time (side effect) | `router.py` |
| A21 | LOW | Upload page imports private `_AGENT_CLASSES` | `1_Upload.py` |
| A22 | LOW | `ReferenceVerificationAgent` role name mismatch | `reference_verification.py` |
| A23 | LOW | `SemanticScholarClient.__del__` unreliable task scheduling | `semantic_scholar.py` |
| A24 | LOW | `_httpx_download()` reads full body without size check | `url_parser.py` |
| A25 | LOW | `get_settings()` has no public test reset API | `config.py` |

### Correctness & Logic (22 issues)

| # | Sev | Issue | File |
|---|-----|-------|------|
| C1 | CRIT | `consolidated_findings` double-counting on reload | `report.py:58` |
| C2 | CRIT | `items[num-1]` IndexError on numbering gaps | `report.py:154` |
| C3 | HIGH | Race condition on `_failure_counts`/`_disabled_agents` | `orchestrator.py:90` |
| C4 | HIGH | `CircuitBreaker` `threading.Lock` blocks event loop | `rate_limiter.py:138` |
| C5 | HIGH | `text_parts` potentially unbound (UnboundLocalError) | `pdf_parser.py:182-219` |
| C6 | HIGH | DOCX section `char_offset` calculation diverges | `docx_parser.py:236` |
| C7 | HIGH | `AsyncRateLimiter.acquire()` not atomic | `rate_limiter.py:68-83` |
| C8 | HIGH | Double Crossref HTTP fetch per DOI | `enrichment.py:122-128` |
| C9 | HIGH | `_sliding_window_chunks` infinite-loop guard incorrect | `chunking.py:232-235` |
| C10 | MED | Empty-paragraph `char_offset += 1` drift | `base.py:173` |
| C11 | MED | `_maybe_wrap` passes non-dict list items unchecked | `json_parser.py:299-306` |
| C12 | MED | JSON strategy ordering (truncation before regex) | `json_parser.py:85-100` |
| C13 | MED | `_format_user_prompt` `**kwargs` silently dropped | `base.py:~339-378` |
| C14 | MED | Nested `asyncio.wait_for` — outer unreachable for LLM timeouts | `client.py:441`, `base.py` |
| C15 | MED | `_parse_item_numbers` accepts negative/zero numbers | `cli.py:706-740` |
| C16 | MED | `asyncio.run()` in Click loop — nested event loop conflict | `cli.py:691` |
| C17 | MED | Section segment uses stale post-edit offsets in feedback applier | `applier.py:416-429` |
| C18 | LOW | `_parse_latex_direct` declared `| None` but never returns `None` | `latex_parser.py` |
| C19 | LOW | `\part` and `\chapter` both map to level 0 | `latex_parser.py:38-46` |
| C20 | LOW | `budget_chars` doesn't account for context header overhead | `chunking.py:281-282` |
| C21 | LOW | ALL-CAPS section regex too permissive | `pdf_parser.py:239-241` |
| C22 | LOW | Numbered + BibTeX extractors both run (duplicate citations) | `base.py:268-284` |

### Reliability & Robustness (32 issues)

| # | Sev | Issue | File |
|---|-----|-------|------|
| R1 | CRIT | `nest_asyncio.apply()` corrupts asyncio semantics | `cli.py:31`, `utils.py:14` |
| R2 | CRIT | `aiohttp.ClientSession` shared across thread event loops | 3 external files |
| R3 | CRIT | `CircuitBreaker` blocking lock in async code | `rate_limiter.py:138` |
| R4 | HIGH | No global pipeline timeout | `orchestrator.py:97` |
| R5 | HIGH | Nested `wait_for` defeats retry logic timing | `base.py:100-151` |
| R6 | HIGH | Token counters fragile under shared state | `base.py:134-137` |
| R7 | HIGH | DNS SSRF TOCTOU + blocking DNS | `input_validator.py:125-149` |
| R8 | HIGH | Non-atomic YAML config writes | `config_store.py:122-129` |
| R9 | HIGH | Semaphore held during rate-limit sleep | `rate_limiter.py:68-94` |
| R10 | HIGH | Silent None return on empty PDF hides per-page errors | `pdf_parser.py:219-225` |
| R11 | HIGH | Keyword substring match causes section double-counting | `results_consistency.py:27-42` |
| R12 | MED | `setup_logging()` never called in Streamlit | `app.py` |
| R13 | MED | httpx fallback buffers full response before size check | `url_parser.py:293` |
| R14 | MED | UI role assignments never persisted | `4_Settings.py` |
| R15 | MED | Timestamp recorded even on failed requests | `rate_limiter.py:82` |
| R16 | MED | Double-buffering of large PDFs (3x memory) | `pdf_parser.py:176` |
| R17 | MED | `_parse_item_numbers` accepts negative ranges | `cli.py:706` |
| R18 | MED | Default `_format_user_prompt` fails for extra template vars | `base.py:378` |
| R19 | MED | Per-invocation semaphore allows 50+ concurrent requests | `enrichment.py:42` |
| R20 | MED | Unsafe `_settings = None` reset pattern | `config.py:147` |
| R21 | MED | Blocking `socket.getaddrinfo` with no timeout | `input_validator.py:136` |
| R22 | MED | Synchronous file reads inside async parse | `latex_parser.py:312` |
| R23 | MED | `time.sleep(0.3)` polling blocks Streamlit server thread | `1_Upload.py:392` |
| R24 | LOW | `__del__` creates uncollectable tasks | `semantic_scholar.py:89` |
| R25 | LOW | Audit logs not separable from application logs | `audit.py` |
| R26 | LOW | `consolidated_findings exclude=True` breaks round-trip | `report.py:58` |
| R27 | LOW | LLM response content logged on parse failure | `base.py:168` |
| R28 | LOW | No validation on saved role assignments | `config_store.py:92` |
| R29 | LOW | Source-code volume mounts run uninstalled code | `docker-compose.yml:9` |
| R30 | LOW | No cap on synthesis prompt size | `orchestrator.py:395` |
| R31 | LOW | Sequential connection tests instead of concurrent | `cli.py:691` |
| R32 | LOW | Unsalted SHA-256 password comparison | `auth.py:24` |

### DevOps & Configuration (34 issues)

| # | Sev | Issue | File |
|---|-----|-------|------|
| D1 | CRIT | No PID 1 init process (signal handling) | `Dockerfile:45` |
| D2 | CRIT | No lockfile; non-reproducible builds | `Dockerfile:28`, `ci.yml` |
| D3 | CRIT | Dev source mounts in only compose file | `docker-compose.yml:9` |
| D4 | HIGH | pip-audit omits `[ui]` dependency tree | `ci.yml:96-99` |
| D5 | HIGH | mypy silently passes with `\|\| true` | `ci.yml:41` |
| D6 | HIGH | No resource limits (CPU/memory) in docker-compose | `docker-compose.yml` |
| D7 | HIGH | No network isolation in docker-compose | `docker-compose.yml` |
| D8 | HIGH | Port bound to `0.0.0.0`, no TLS guidance | `docker-compose.yml:7` |
| D9 | HIGH | Base image not pinned to digest | `Dockerfile:1` |
| D10 | HIGH | No container image CVE scanning in CI | `ci.yml` |
| D11 | HIGH | Layer ordering breaks dependency cache | `Dockerfile:21-28` |
| D12 | HIGH | No hatch build config; `streamlit_app` ships in wheel | `pyproject.toml` |
| D13 | HIGH | GitHub Actions not pinned to SHA | `ci.yml` |
| D14 | HIGH | `PV_APP_PASSWORD` undocumented; app defaults to open | `.env.example`, `auth.py` |
| D15 | MED | `log_level` not validated against known levels | `config.py:116` |
| D16 | MED | `.env.example` missing 11 of 14 settings | `.env.example` |
| D17 | MED | No `timeout-minutes` on CI jobs | `ci.yml` |
| D18 | MED | No pip dependency caching in CI | `ci.yml` |
| D19 | MED | No concurrency group to cancel stale CI runs | `ci.yml` |
| D20 | MED | Missing pre-commit hooks (secrets, yaml, no-commit) | `.pre-commit-config.yaml` |
| D21 | MED | No upper bounds on any dependency | `pyproject.toml` |
| D22 | MED | `requires-python` has no upper bound; only 1 version tested | `pyproject.toml:11` |
| D23 | MED | `lxml` hard dependency for single optional code path | `pyproject.toml:39` |
| D24 | MED | `TEMP_DIR` computed via `__file__` (broken when installed) | `config.py:31-37` |
| D25 | MED | `setup_logging()` never called in Streamlit | `app.py` |
| D26 | MED | `nest_asyncio.apply()` at import time in CLI module | `cli.py:29-31` |
| D27 | MED | Dev-mode log settings in compose file | `docker-compose.yml:14-16` |
| D28 | MED | No startup validation of keys or directory writability | `config.py`, `app.py` |
| D29 | LOW | Single-stage Dockerfile (fake multi-stage `AS base`) | `Dockerfile:1` |
| D30 | LOW | `curl` in production image only for health check | `Dockerfile:8` |
| D31 | LOW | Tests excluded from Docker but not from wheel | `.dockerignore:13` |
| D32 | LOW | Pre-commit hook revs are mutable version tags | `.pre-commit-config.yaml` |
| D33 | LOW | No `LABEL` OCI metadata in Dockerfile | `Dockerfile` |
| D34 | LOW | No SBOM generation in CI | `ci.yml` |

### Test Coverage Gaps

| # | Area | Missing Coverage |
|---|------|-----------------|
| T1 | Parsers | No tests for any of the 7 document parsers |
| T2 | Rate Limiting | No tests for `AsyncRateLimiter` or `CircuitBreaker` |
| T3 | External APIs | No tests for Crossref, OpenAlex, Semantic Scholar clients |
| T4 | Chunking | `_sliding_window_chunks` not tested for edge cases |
| T5 | CLI Workflow | No end-to-end CLI integration tests |

---

## Prioritized Fix Roadmap

### Sprint 1: Data Integrity & Crash Bugs (Week 1)
1. **Fix `consolidated_findings` serialization** — data corruption on save/reload
2. **Fix `text_parts` UnboundLocalError** in PDF parser
3. **Fix `items[num-1]` IndexError** in report feedback generation
4. **Fix DOCX `char_offset` calculation** — all section offsets wrong
5. **Fix `html` variable shadowing** `import html` in diff_generator

### Sprint 2: Async & Concurrency Safety (Week 2)
6. **Remove `nest_asyncio.apply()` calls** — use proper async patterns
7. **Replace `threading.Lock` with `asyncio.Lock`** in CircuitBreaker
8. **Fix `AsyncRateLimiter` atomicity** — check-and-append in single lock scope
9. **Add `asyncio.Lock` to orchestrator** `_failure_counts`/`_disabled_agents`
10. **Remove double `asyncio.wait_for`** nesting in agent base/client

### Sprint 3: Security Hardening (Week 3)
11. **Sanitize prompt injection boundaries** — escape `<`/`>` in document text
12. **Sanitize HTML diff output** — use `bleach` before `st.components.v1.html()`
13. **Fix SSRF DNS rebinding** — validate IPs at connection time
14. **Call `setup_logging()` in Streamlit** — enable log redaction
15. **Add `tini` to Dockerfile** — proper PID 1 signal handling

### Sprint 4: Build & Deploy Hardening (Week 4)
16. **Generate and commit dependency lockfile**
17. **Split docker-compose** into production and development files
18. **Remove `|| true` from mypy** CI step
19. **Pin GitHub Actions to SHA hashes**
20. **Add container image scanning** to CI

### Sprint 5: Architecture Cleanup (Weeks 5-6)
21. **Extract chunk-loop template** into BaseAgent (eliminate 5-way duplication)
22. **Extract AsyncAPIClient** base class (eliminate 3-way duplication)
23. **Decouple WriterAgent** from BaseAgent inheritance
24. **Add public `synthesize()` method** to orchestrator agent
25. **Add unit tests** for parsers, rate limiter, and chunking edge cases

---

## What the Codebase Does Well

Despite the issues found, the codebase demonstrates several positive patterns:

- **Per-agent failure isolation** via `asyncio.gather(return_exceptions=True)`
- **Circuit breakers** on all external API clients
- **Structured logging** with structlog (when initialized)
- **Input validation** with magic bytes, file size limits, and extension checks
- **SSRF protection** (address-family checks, redirect following, private IP blocklist)
- **Curly-brace escaping** in prompts to prevent `str.format()` crashes on LaTeX
- **Pydantic v2 data models** with strong typing throughout
- **6-strategy JSON parsing fallback** chain for resilient LLM output handling
- **Audit logging** for verification events
- **Multi-provider LLM abstraction** with clean role-based configuration

---

*Generated by Claude Opus 4.6 — 5 specialized agents, full codebase review*
*Total: 119 issues (12 Critical, 34 High, 43 Medium, 30 Low)*

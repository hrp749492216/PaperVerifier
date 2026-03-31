> **STATUS: ALL ISSUES RESOLVED**
> All findings in this review have been addressed across multiple fix commits.
> This file is retained for historical reference only.
> See git log for the specific fix commits.

---

# Reliability And Operability Review

## Scope

This review focused on runtime resilience, recoverability, observability, and operational test coverage across the CLI, Streamlit app, parser layer, LLM/external API calls, and container setup.

## Findings

### 1. High: The primary UI workflow is transient and unrecoverable across restarts

The end-to-end review flow depends on `st.session_state` for the parsed document, verification report, selected findings, and applied feedback, and the downstream pages hard-fail when that in-memory state is missing. That means a Streamlit restart, container recycle, browser reconnect, or deployment during a long verification loses the run with no resume path.

Evidence:
- [streamlit_app/pages/1_Upload.py:43](/Users/hariramanpokhrel/Desktop/PaperVerifier/streamlit_app/pages/1_Upload.py:43) initializes `parsed_document`, `verification_report`, `selected_items`, and `applied_feedback` only in session state.
- [streamlit_app/pages/1_Upload.py:114](/Users/hariramanpokhrel/Desktop/PaperVerifier/streamlit_app/pages/1_Upload.py:114) and [streamlit_app/pages/1_Upload.py:163](/Users/hariramanpokhrel/Desktop/PaperVerifier/streamlit_app/pages/1_Upload.py:163) explicitly clear downstream state on new parses.
- [streamlit_app/pages/2_Review.py:89](/Users/hariramanpokhrel/Desktop/PaperVerifier/streamlit_app/pages/2_Review.py:89) refuses to load if `verification_report` is missing.
- [streamlit_app/pages/3_Apply.py:35](/Users/hariramanpokhrel/Desktop/PaperVerifier/streamlit_app/pages/3_Apply.py:35) refuses to load if the selected items, report, or parsed document are missing.
- The only persistence exposed to the user is manual export after completion at [streamlit_app/pages/2_Review.py:380](/Users/hariramanpokhrel/Desktop/PaperVerifier/streamlit_app/pages/2_Review.py:380).

Why this matters:
- Long-running enterprise review jobs need resumability and post-failure recovery.
- Support cannot reliably recover or inspect a failed/aborted run after process loss.
- Horizontal scaling and rolling deploys are unsafe because the app state is node-local and ephemeral.

Recommended direction:
- Persist run state behind a durable run ID.
- Store parsed documents, reports, and apply results outside Streamlit session memory.
- Make Review and Apply pages load by run ID instead of requiring the same process-local session.

### 2. High: Runtime controls exist in configuration, but several critical code paths ignore them

The repo defines operator-facing settings for LLM timeout, external API timeout, Git clone timeout, temp directory, and LLM concurrency, but the live code paths use hardcoded defaults or never consume those settings. In practice, operators cannot tune the system predictably during incidents or per environment.

Evidence:
- Config exposes the knobs at [paperverifier/config.py:53](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/config.py:53) through [paperverifier/config.py:85](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/config.py:85).
- LLM agent calls use a hardcoded `_DEFAULT_CALL_TIMEOUT = 180.0` at [paperverifier/agents/base.py:49](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/agents/base.py:49) instead of `AppSettings.llm_call_timeout`.
- URL downloads use `_DOWNLOAD_TIMEOUT = 60` at [paperverifier/parsers/url_parser.py:31](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/parsers/url_parser.py:31) and [paperverifier/parsers/url_parser.py:162](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/parsers/url_parser.py:162), not `AppSettings.external_api_timeout`.
- GitHub cloning is invoked with defaults at [paperverifier/parsers/github_parser.py:82](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/parsers/github_parser.py:82), not `AppSettings.git_clone_timeout`.
- External enrichment reads settings at [paperverifier/external/enrichment.py:36](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/external/enrichment.py:36) but constructs API clients without passing timeout or shared rate/concurrency controls at [paperverifier/external/enrichment.py:54](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/external/enrichment.py:54).
- The external clients do accept configurable timeouts in their constructors at [paperverifier/external/crossref.py:49](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/external/crossref.py:49), [paperverifier/external/openalex.py:49](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/external/openalex.py:49), and [paperverifier/external/semantic_scholar.py:61](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/external/semantic_scholar.py:61), but the caller does not wire them up.
- `max_concurrent_llm_calls` and `temp_dir` are defined in config but were not referenced anywhere in the runtime search.

Why this matters:
- Incident response depends on being able to tighten or relax timeouts and concurrency without code changes.
- Environment-specific behavior becomes inconsistent and hard to reason about.
- Declared controls become misleading operationally because changing env vars does not reliably change behavior.

Recommended direction:
- Centralize runtime settings injection and remove hardcoded timeout/concurrency constants from request paths.
- Fail CI if a declared setting is unused.
- Add startup logging that prints the effective runtime settings for the current process.

### 3. Medium: Observability is not strong enough for enterprise incident response

The repo has structured logging and audit helper functions, but they are still effectively best-effort stdout logs with no durable run record, no explicit correlation model, and no separate audit sink despite the docs claiming audit events can be routed independently. The container also creates a logs directory that the app never uses.

Evidence:
- Logging is configured with `structlog.PrintLoggerFactory()` at [paperverifier/config.py:242](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/config.py:242), which keeps logs on the default process sink.
- Audit helpers only emit standard structlog events with `audit=True` at [paperverifier/audit.py:46](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/audit.py:46) and [paperverifier/audit.py:71](/Users/hariramanpokhrel/Desktop/PaperVerifier/paperverifier/audit.py:71); there is no separate transport, store, or retention mechanism in the repo.
- The container creates `/app/logs` at [Dockerfile:30](/Users/hariramanpokhrel/Desktop/PaperVerifier/Dockerfile:30), but no runtime path writes there.
- The healthcheck only verifies that Streamlit is serving HTTP at [Dockerfile:40](/Users/hariramanpokhrel/Desktop/PaperVerifier/Dockerfile:40); it does not validate provider configuration, key availability, or dependency reachability.

Why this matters:
- Enterprise operability depends on reconstructing a failed run by run ID, report ID, tenant/user, and downstream dependency state.
- A UI process being alive is not the same as the verification pipeline being healthy.
- Audit events that share the same volatile sink as application logs are weak for compliance and incident investigation.

Recommended direction:
- Add a durable run store for lifecycle metadata and failure states.
- Bind a correlation ID or report ID into every log line across upload, verify, review, and apply flows.
- Separate application logs, audit logs, and health/readiness signals.
- Expose a readiness endpoint that validates critical dependencies, not just the Streamlit shell.

### 4. Medium: Operationally critical paths are largely untested

The current automated test suite is fast and clean, but it does not cover the failure-heavy paths that dominate reliability work: orchestrator parallelism, external API degradation, LLM timeout/retry behavior, sandboxed Git clone behavior, Streamlit workflow persistence, or CLI end-to-end execution.

Evidence:
- The repo's tests are limited to [tests/unit/test_models.py](/Users/hariramanpokhrel/Desktop/PaperVerifier/tests/unit/test_models.py), [tests/unit/test_json_parser.py](/Users/hariramanpokhrel/Desktop/PaperVerifier/tests/unit/test_json_parser.py), [tests/unit/test_config.py](/Users/hariramanpokhrel/Desktop/PaperVerifier/tests/unit/test_config.py), [tests/unit/test_input_validator.py](/Users/hariramanpokhrel/Desktop/PaperVerifier/tests/unit/test_input_validator.py), and [tests/unit/test_chunking.py](/Users/hariramanpokhrel/Desktop/PaperVerifier/tests/unit/test_chunking.py).
- The integration test directory is effectively empty at [tests/integration/__init__.py](/Users/hariramanpokhrel/Desktop/PaperVerifier/tests/integration/__init__.py).
- `pytest -q` passed `61` tests locally, but all of them completed in `0.13s`, which is consistent with a pure unit-test suite and no exercised network/process/UI paths.

Why this matters:
- Most operability regressions appear only under retries, timeouts, partial failures, and process restarts.
- The current suite will not catch drift in the exact areas this review found weakest.

Recommended direction:
- Add integration tests for orchestrator partial failure, provider timeout/rate-limit behavior, and sandbox clone cleanup.
- Add at least one UI workflow test that proves a report can be resumed or reloaded after process loss.
- Add CLI smoke tests for `verify` and `apply` with deterministic fake providers.

## Positive Notes

- External metadata clients already include rate limiting and circuit breakers, which is a solid resilience baseline.
- Agent execution uses partial-failure handling, so one failing agent does not necessarily abort the full run.
- The container runs as a non-root user and includes a healthcheck and restart policy.
- The current unit suite is clean: `61 passed in 0.13s`.

## Bottom Line

This repo has a decent reliability foundation at the code-primitive level, but it is not yet enterprise-grade operationally. The biggest gaps are resumability of user workflows, configuration drift between declared and effective runtime controls, and the lack of durable observability around runs and failures.

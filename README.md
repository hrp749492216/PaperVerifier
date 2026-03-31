<p align="center">
  <h1 align="center">PaperVerifier</h1>
  <p align="center">
    <strong>Automated research paper verification powered by multi-agent LLM pipelines</strong>
  </p>
  <p align="center">
    <a href="#quickstart">Quickstart</a> &bull;
    <a href="#how-it-works">How It Works</a> &bull;
    <a href="#installation">Installation</a> &bull;
    <a href="#configuration">Configuration</a> &bull;
    <a href="#docker">Docker</a> &bull;
    <a href="#contributing">Contributing</a>
  </p>
</p>

---

PaperVerifier runs **7 specialised verification agents in parallel** across your manuscript, cross-referencing claims against live academic databases, and returns actionable, numbered feedback you can selectively apply — all before you hit "Submit."

It accepts **PDF, DOCX, LaTeX, Markdown, plain text, GitHub repos, and URLs**, and works with **8 LLM providers** out of the box.

## Quickstart

```bash
pip install paperverifier

# Verify a local PDF
paperverifier verify paper.pdf

# Verify from a URL
paperverifier verify https://arxiv.org/abs/2401.12345

# Verify a GitHub repo containing a manuscript
paperverifier verify https://github.com/user/paper-repo

# Output a structured JSON report
paperverifier verify paper.pdf -o report.json -f json
```

Or launch the web UI:

```bash
pip install "paperverifier[ui]"
streamlit run streamlit_app/app.py
```

## How It Works

```
  Document ──► Parser Router ──► 9 Parallel Agents ──► Consolidated Report
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
               CrossRef          OpenAlex       Semantic Scholar
            (DOI / retraction)  (related works)  (citation graph)
```

PaperVerifier parses your document into a hierarchical model with **stable semantic IDs** (e.g. `sec-2.para-3.sent-1`), enriches references against three academic APIs, then dispatches the following agents concurrently:

| Agent | What It Checks |
|-------|---------------|
| **Section Structure** | Organisation, heading hierarchy, paragraph placement |
| **Claim Verification** | Every claim is backed by a cited source |
| **Reference Verification** | References exist, resolve correctly, and are not retracted |
| **Results Consistency** | Numbers, tables, and figures agree with methodology and conclusions |
| **Novelty Assessment** | Originality relative to related work in the field |
| **Language & Flow** | Writing quality, coherence, transitions, and readability |
| **Hallucination Detection** | Unsupported statistics, fabricated data, phantom citations |
| **Writer** | Generates concrete rewrites for every finding |
| **Orchestrator** | Deduplicates, prioritises, and synthesises the final report |

Each finding includes a severity level, confidence score, evidence trail, and a suggested fix. You review the numbered feedback items and **selectively apply** the ones you want — PaperVerifier rewrites the affected passages for you, with conflict detection for overlapping edits.

## Supported Input Formats

| Format | Extensions / Patterns |
|--------|-----------------------|
| PDF | `.pdf` |
| Microsoft Word | `.docx`, `.doc` |
| LaTeX | `.tex` |
| Markdown | `.md`, `.markdown` |
| Plain text | `.txt` |
| GitHub repository | `https://github.com/owner/repo` |
| Web URL | Any `https://` document link |

File type is detected automatically via extension and magic-byte verification.

## LLM Providers

PaperVerifier supports **8 providers**. Each verification agent can be independently assigned to any provider and model:

| Provider | Example Models | Env Variable |
|----------|---------------|-------------|
| **Anthropic** | Claude Sonnet 4, Claude Opus 4 | `ANTHROPIC_API_KEY` |
| **OpenAI** | GPT-4o, GPT-4o-mini, o3-mini | `OPENAI_API_KEY` |
| **Google Gemini** | Gemini 2.5 Pro, Gemini 2.5 Flash | `GEMINI_API_KEY` |
| **Grok (xAI)** | Grok-3, Grok-3-mini | `GROK_API_KEY` |
| **DeepSeek** | DeepSeek-Chat, DeepSeek-Reasoner | `DEEPSEEK_API_KEY` |
| **OpenRouter** | Any model via routing | `OPENROUTER_API_KEY` |
| **Minimax** | MiniMax-Text-01 | `MINIMAX_API_KEY` |
| **Kimi (Moonshot)** | Moonshot-v1-auto | `KIMI_API_KEY` |

Configure provider assignments per agent through the CLI (`paperverifier config`) or the Settings page in the web UI.

## Installation

**Requirements:** Python 3.11+

```bash
# Core (CLI only)
pip install paperverifier

# With web UI
pip install "paperverifier[ui]"

# Development
pip install "paperverifier[dev]"
```

Copy the environment template and add at least one LLM API key:

```bash
cp .env.example .env
# Edit .env with your API keys
```

## Configuration

Application settings use the `PAPERVERIFIER_` prefix. Provider and external API credentials use vendor-specific names (see the provider table above). Both can also be managed via the interactive CLI:

```bash
paperverifier config
```

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `PAPERVERIFIER_MAX_CONCURRENT_AGENTS` | `9` | Parallel agent limit |
| `PAPERVERIFIER_MAX_CONCURRENT_LLM_CALLS` | `20` | Global outbound LLM call ceiling |
| `PAPERVERIFIER_MAX_DOCUMENT_SIZE_MB` | `100` | Upload size cap |
| `PAPERVERIFIER_MAX_DOCUMENT_PAGES` | `500` | Page limit for PDF parsing |
| `PAPERVERIFIER_PIPELINE_TIMEOUT` | `1800` | End-to-end timeout (seconds) |
| `PAPERVERIFIER_LOG_FORMAT` | `json` | `json` (production) or `console` (development) |

For external API enrichment, optionally provide polite-pool emails for higher rate limits:

```bash
PAPERVERIFIER_OPENALEX_EMAIL=you@university.edu
PAPERVERIFIER_CROSSREF_EMAIL=you@university.edu
SEMANTIC_SCHOLAR_API_KEY=your-key    # 100 req/s vs 10 req/s unauthenticated
```

## Docker

```bash
# Production
docker compose up -d

# Development (hot-reload, debug logging)
docker compose -f docker-compose.yml -f docker-compose.override.yml up
```

The image runs as a non-root user behind `tini` for proper signal handling, with a built-in health check on the Streamlit port.

## Interfaces

### CLI

```bash
# Full verification with markdown output
paperverifier verify paper.pdf -f markdown -v

# Run only specific agents
paperverifier verify paper.pdf --agents claim_verification,reference,hallucination_detection

# Filter by severity
paperverifier verify paper.pdf --severity major

# Apply feedback
paperverifier apply paper.pdf report.json
```

### Web UI

The Streamlit app provides a four-step workflow:

1. **Upload** — drag-and-drop or paste a URL
2. **Review** — browse findings by agent, severity, and section
3. **Apply** — select feedback items and generate a revised manuscript
4. **Settings** — manage LLM providers and API keys

## Security

PaperVerifier is designed to process untrusted documents safely:

- **SSRF protection** — DNS-pinned URL validation blocks private/link-local/metadata IPs
- **Prompt injection isolation** — document content wrapped in `<untrusted_document_content>` boundaries with XML escaping
- **Sandboxed git clones** — disabled hooks, symlink restrictions, shallow depth, file-type allowlist
- **Input validation** — magic-byte verification, path traversal prevention, filename sanitisation
- **Rate limiting** — per-session throttling, token-bucket rate limiters, circuit breakers on external APIs
- **Sensitive key redaction** — API keys and tokens automatically stripped from structured logs
- **Non-root Docker** — container runs as `appuser` (uid 1000)

## Architecture

```
paperverifier/
├── agents/           # 9 verification agents + base class + orchestrator
├── external/         # CrossRef, OpenAlex, Semantic Scholar clients
├── feedback/         # Selective feedback application with conflict detection
├── llm/              # Unified multi-provider LLM client, role assignments
├── models/           # Pydantic models (document, findings, report)
├── parsers/          # Format-specific parsers + router
├── security/         # Input validation, SSRF protection, sandbox
└── utils/            # Chunking, prompt templates, JSON parsing
streamlit_app/        # Web UI (upload, review, apply, settings)
tests/
├── unit/             # Fast, isolated tests
└── integration/      # CLI smoke tests, enrichment tests
```

## Development

```bash
# Install dev dependencies
pip install "paperverifier[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=paperverifier

# Lint & format
ruff check .
ruff format .

# Type check
mypy paperverifier
```

## License

Proprietary — All rights reserved.

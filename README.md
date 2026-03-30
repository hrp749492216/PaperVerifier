# PaperVerifier

Enterprise-grade research paper verification tool that performs comprehensive automated review before submission to conferences and journals.

## What It Does

Takes a research paper (PDF, Word, Markdown, LaTeX, text, GitHub repo, or URL) and runs **9 parallel verification agents** to check:

- **Section Structure** - validates paper organization and paragraph placement
- **Claim Verification** - checks claims are supported by citations
- **Reference Verification** - validates references exist, are relevant, and not retracted
- **Results Consistency** - cross-checks results with methodology and conclusions
- **Novelty Assessment** - evaluates originality against existing work
- **Language & Flow** - semantic flow, writing quality, transitions
- **Hallucination Detection** - finds unsupported facts and fabricated data
- **Writer** - generates fixes and rewrites for feedback application
- **Orchestrator** - coordinates all agents and generates final summary

Produces numbered feedback items that users can **selectively apply** to automatically fix their paper.

## Multi-LLM Provider Support

Supports **8 LLM providers** — users can assign any provider+model to any agent role:

| Provider | Default Models |
|----------|---------------|
| Anthropic | Claude Sonnet 4, Claude Opus 4 |
| OpenAI | GPT-4o, GPT-4o-mini |
| Grok (xAI) | Grok-3, Grok-3-mini |
| OpenRouter | Any model via routing |
| Gemini | Gemini 2.5 Pro, Gemini 2.5 Flash |
| Minimax | MiniMax-Text-01 |
| Kimi (Moonshot) | Moonshot-v1-auto |
| DeepSeek | DeepSeek-Chat, DeepSeek-Reasoner |

## Interfaces

- **Streamlit Web App** - file upload, interactive review, selective feedback application
- **CLI** - terminal-based verification via `click`

## Status

Under active development. See the implementation plan in `docs/plan.md`.

## License

Proprietary - All rights reserved.

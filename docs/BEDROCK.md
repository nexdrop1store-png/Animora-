# Running Animora's AI backend on Amazon Bedrock

The backend supports two LLM providers, chosen at startup:

| Provider | When to use | Auth | Model IDs |
|---|---|---|---|
| `anthropic` (default) | Production | `ANTHROPIC_API_KEY` (or addon BYOK) | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` |
| `bedrock` | Dev / CI when Anthropic credits are tight | `AWS_BEARER_TOKEN_BEDROCK` | Same logical names; backend translates them transparently |

The switch is a single env var, `ANIMORA_LLM_PROVIDER`. Router, orchestrator, eval harness, and tests are all provider-agnostic — they pass logical model names and the `AnthropicClient` translates them.

## One-time setup

1. **Mint a long-term Bedrock API key.** AWS console → Bedrock → "API keys" → "Create long-term API key." The key format starts with `ABSK...` and is base64 inside (encodes account + secret).
2. **Request model access** in the same Bedrock console: Anthropic → Claude Sonnet 4.6, Claude Haiku 4.5, Claude Opus 4.6 (and 4.5 / 4.1 if you want fallback options). Opus 4.7 is also worth requesting but as of 2026-05 most accounts don't have it yet.
3. **Update `ai-backend/.env`** (gitignored):

```bash
ANIMORA_LLM_PROVIDER=bedrock
AWS_BEARER_TOKEN_BEDROCK=ABSK...   # the key from step 1
BEDROCK_AWS_REGION=us-east-1       # cross-region inference profile region
```

That's it. No code changes. Run `python ai-backend/dev_server.py` as normal and the backend will talk to Bedrock.

## Model translation

| Logical name (what code uses) | Bedrock ID (what's actually called) | Notes |
|---|---|---|
| `claude-haiku-4-5-20251001` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Same model |
| `claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6` | Same model |
| `claude-opus-4-7` | `us.anthropic.claude-opus-4-6-v1` | **Substitute** — Opus 4.7 gated on most accounts |

The `us.` prefix is the AWS cross-region inference profile — required for on-demand invocation of every Claude 4.x model. Bare IDs like `anthropic.claude-sonnet-4-6` return `"Invocation of model ID ... with on-demand throughput isn't supported."`

The full map lives in [`ai-backend/llm_provider.py`](../ai-backend/llm_provider.py) at `_BEDROCK_MODEL_MAP`. Add a new entry when Anthropic ships a model AWS publishes the Bedrock equivalent.

## What's different about Opus 4.6 vs Opus 4.7

For dev/CI work, treat them as interchangeable:

- **Both support `thinking={"type":"adaptive"}` + `output_config.effort`** — the Opus 4.7 API shape works unchanged.
- **Both return `thinking` content blocks** in responses (different from how 4.7 sometimes optimises them away). Our `anthropic_client.py:441-475` round-trips these blocks regardless, so the tool-use loop survives.
- **Quality-wise, 4.6 produces comparable scripts.** The 2026-05-24 smoke run against `primitive.cube` hit the same failure mode as 4.7 (default `"Cube"` naming) — same script style, same quality bar.
- **The wire-protocol cost is similar** (~$0.04 per 1k output tokens). Bedrock bills to your AWS account; direct API bills to your Anthropic account.

Production ships on direct Anthropic API + Opus 4.7. Eval baselines, regression runs, and any dev-loop work can use Bedrock without changing code.

## Switching back to direct Anthropic

```bash
# In ai-backend/.env
ANIMORA_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

The `AWS_BEARER_TOKEN_BEDROCK` setting is ignored in `anthropic` mode (no need to remove it). Restart `dev_server.py`.

## Things to be aware of

- **BYOK is ignored on Bedrock.** When the addon sends a user's `sk-ant-...` key in the WS hello and the backend is in Bedrock mode, we silently drop the BYOK key and use the server-side Bedrock credentials. Users pay nothing; AWS bills the account that minted the bearer token. Production deploys must use `ANIMORA_LLM_PROVIDER=anthropic` so BYOK works as designed.
- **Region matters.** Cross-region inference profiles span US-East, US-West-2, and a few others. If you change `BEDROCK_AWS_REGION`, also confirm the `us.` prefix in `_BEDROCK_MODEL_MAP` still resolves — for European regions you'd need an `eu.` prefix.
- **Streaming latency is similar** to direct API for Haiku/Sonnet. Opus 4.6 on Bedrock is ~5-15% slower than Opus 4.7 on the direct API for equivalent prompts, because the smaller model thinks less in adaptive mode.
- **`/validate-key` REST endpoint** still expects an Anthropic-style key shape and will reject a Bedrock token. That endpoint is a Settings-UI smoke test for users on the Anthropic path; on Bedrock the panel should hide the "Test Connection" button (Phase 5.5 / panel polish work).
- **AWS_BEARER_TOKEN_BEDROCK in logs.** The token is treated like any other secret: never logged in raw form. `anthropic_client.py` logs `key_fingerprint="bedrock-bearer"` instead of the actual key when on Bedrock.

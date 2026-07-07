# Running Animora end-to-end locally

This is the dev loop: backend on your machine, Animora.exe pointing at it,
real Claude responses streaming into the AI panel.

The key from your Anthropic account is already saved in `ai-backend/.env`
(gitignored). Fingerprint **`39f3e29f6979`** — that's the only reference
you'll see in logs.

## One-time setup

```bash
# 1. Install backend Python deps (you already did this once)
cd ai-backend
pip install -r requirements.txt

# 2. (Optional, recommended) install websocket-client for the addon side
pip install websocket-client
```

You do **not** need Redis. You do **not** need any auth infrastructure. The
`dev_server.py` launcher stubs both with in-process equivalents.

## Every-time launch

In a terminal:
```bash
cd ai-backend
python dev_server.py
```

You'll see:
```
[dev_server] Stubbed Redis with in-memory store.
[dev_server] Stubbed auth: any token accepted as trial-plan.
[dev_server] Animora AI backend starting on http://127.0.0.1:8000
[dev_server] WebSocket endpoint: ws://localhost:8000/ws/<session_id>?token=dev
```

That's it. The backend is now serving Animora's AI panel.

## Pointing Animora at the local backend

1. Launch Animora.
2. Open **Edit → Preferences → Add-ons → Animora**.
3. Toggle **Dev Mode** in the Connection section. This flips the URLs to
   `ws://localhost:8000/ws` automatically.
4. (Optional) In the **Anthropic Account** section, paste your Anthropic
   API key into the field, click **Save**. This stores it in your OS
   keyring and the addon sends it in the WS hello — so the backend uses
   *your* key (BYOK mode, `key_source=byok`).
   - If you skip step 4, the backend falls back to the key in `.env`
     (`key_source=pooled`). Either works.
5. Close Preferences. Click **Test Connection** if you set a key — green
   check means Anthropic accepts it.
6. Type a message in the AI panel and hit **Send Command**.

## How to verify it's working

The backend's log will print a JSON line like:
```json
{"event":"anthropic.client.stream.completed","session_id":"...","model":"claude-sonnet-4-6","input_tokens":400,"output_tokens":45,"cache_hit_ratio":0.0,"elapsed_ms":2099,"attempts":1}
```

A second message in the same session will show `cache_hit_ratio` jump to
~0.99 — that's the prompt-cache savings the master prompt is designed for.

## Smoke tests you can run without launching Animora

Both confirm the integration is healthy without needing the desktop app:

```bash
cd ai-backend

# Direct AnthropicClient test (uses real wrapper + master prompt + sample scene)
python test_call.py

# Full WebSocket test (same wire protocol the addon uses)
python test_ws.py
```

`test_call.py` should print "PASS — response is in-character as Animora AI".
`test_ws.py` should print "PASS" after the streaming response.

## Common issues

**WebSocket connects but immediately closes with `no_api_key`**
→ The addon sent an empty `api_key` in hello and `.env` is empty too. Either
paste a key in Animora Settings or fill `ANTHROPIC_API_KEY` in `.env`.

**HTTP 404 on `/ws/...`**
→ uvicorn was installed without WebSocket protocol support. Run:
`pip install "uvicorn[standard]" websockets`

**`anthropic.client.stream.failed` with `error_code=invalid_key`**
→ Anthropic rejected the key. Generate a new one at console.anthropic.com
and update `.env` or paste in Animora Settings.

**Anthropic rate-limited (429)**
→ The wrapper retries with exponential backoff (1s/2s/4s). If the limit
is hit hard, the error surfaces to the panel after 3 attempts.

## What dev_server.py is NOT

It's a **dev shortcut**. Production deploys to AWS Fargate via the real
`main.py` with real Redis (ElastiCache) and real Supabase-issued JWTs.
The monkey-patches in `dev_server.py` are ONLY for local development.

# Deploying the Animora backend (Fly.io + Upstash Redis)

This stands up the server that **holds the Bedrock key as an encrypted secret**
and talks to Bedrock for the app. The installed Animora app never has the key —
it connects to this server over an authenticated WebSocket.

```
[Fly secret]  AWS_BEARER_TOKEN_BEDROCK = ABSK...   (encrypted, never in git/image)
      │ injected as env var at runtime
      ▼
[Fly container: ai_backend.main:app] -- Anthropic SDK --> Amazon Bedrock
      ▲ authenticated WebSocket (Supabase token)
      │
[installed Animora app]  no key — just wss://api.animora.tech/ws
```

## Prerequisites
- A Fly.io account + `flyctl` (`curl -L https://fly.io/install.sh | sh`, then `fly auth login`).
- An Upstash account (free) for Redis.

## 1. Redis (Upstash — free, serverless)
1. https://console.upstash.com → **Create Database** → Redis → a region near
   `iad`/us-east.
2. Copy the **`rediss://…` connection URL** (the TLS one). You'll set it as a
   secret below. (The backend uses Redis for sessions, the vision buffer, and
   rate limiting.)

## 2. Create the Fly app
From the **repo root** (where `fly.toml` lives):
```bash
fly apps create animora-backend          # or: fly launch --no-deploy --copy-config
```
If `animora-backend` is taken, pick another name and update `app =` in `fly.toml`.

## 3. Set the secrets (THIS is "deploying the key safely")
These are encrypted by Fly, injected as env vars at runtime, and **never** stored
in git, the image, or `fly.toml`:
```bash
fly secrets set AWS_BEARER_TOKEN_BEDROCK="ABSK...your bedrock key..."
fly secrets set JWT_SECRET="$(openssl rand -hex 32)"      # a real production secret
fly secrets set REDIS_URL="rediss://...your-upstash-url..."
```
> `ANIMORA_LLM_PROVIDER=bedrock`, `BEDROCK_AWS_REGION`, and `ANIMORA_ENV` are
> non-secret and already in `fly.toml`/Dockerfile — don't put them here.

## 4. Deploy
```bash
fly deploy            # builds ai-backend/Dockerfile, ships, starts 1 machine
fly logs             # watch it boot
curl https://animora-backend.fly.dev/health     # -> {"status":"ok",...}
```

## 5. Point your domain (optional but recommended)
The app ships pointing at `wss://api.animora.tech/ws`. Map that to Fly:
```bash
fly certs add api.animora.tech
# then add the DNS records Fly prints (an A/AAAA or CNAME to animora-backend.fly.dev)
```
Until DNS is set, you can temporarily point the addon's **Preferences →
Connection → AI Backend URL** at `wss://animora-backend.fly.dev/ws` to test.

## 6. Verify the key is a secret, not a file
```bash
fly ssh console -C "printenv AWS_BEARER_TOKEN_BEDROCK | cut -c1-8"   # shows ABSKQmVk…
fly ssh console -C "ls -la /app/ai_backend/.env"                     # -> No such file (good)
```

## Security checklist
- ✅ Key is a **Fly secret** (encrypted at rest, env-injected) — not in git, image, or installer.
- ✅ `.dockerignore` excludes `.env`, so it can't slip into an image layer.
- ✅ The public installer carries **no** key (verified by `scripts/check_no_secrets.py`).
- 🔁 **Rotate the key** if it was ever pasted into chat/a file: mint a new one in
  the AWS Bedrock console, `fly secrets set AWS_BEARER_TOKEN_BEDROCK=...` (triggers
  a rolling restart), and revoke the old one.
- 💡 Watch usage: Bedrock has per-day token quotas independent of the $120
  credits — raise the key's limit / request a quota bump if you hit 429s.

## Cost note
Fly: ~$2-5/mo for one always-on shared-cpu-1x/512MB machine (WebSocket needs
`min_machines_running = 1`, so don't scale to zero). Upstash free tier covers
early usage. Bedrock is pay-per-token against your credits.

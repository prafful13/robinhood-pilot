# Robinhood RSI Trader

An automated equity trading bot for [Robinhood's Agentic MCP API](https://robinhood.com/agentic), deployed on a local k3s cluster. Trades RSI mean-reversion signals on a configurable watchlist with a live Streamlit tax dashboard.

---

## Strategy

| Parameter | Value |
|---|---|
| Indicator | RSI(14) on 15-minute bars |
| Buy signal | RSI < 30 (oversold) |
| Sell signal | RSI > 70 (overbought) |
| Max per trade | $300 |
| Max open positions | 5 |
| Daily loss limit | $50 |
| Default watchlist | GOOGL, AAPL, NVDA, AVGO, CRWV, XLE |

---

## Architecture

```
macOS Keychain  ──► inv k8s-seal ──► k8s/sealed/*.yaml (gitignored)
                                           │
                                    kubeseal encrypts
                                           │
                                           ▼
                               k3s: Sealed Secrets controller
                                           │
                                    decrypts → k8s Secrets
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    ▼                      ▼                      ▼
             robinhood-bot          robinhood-dashboard        postgres
          (RSI strategy loop)     (Streamlit :30501)        (trades DB)
          TOKEN_FILE from Secret   DB_URL from Secret        5Gi PVC
```

**Secrets never touch disk or git.** The flow is always:
- Locally: macOS Keychain → `vault/keychain.py`
- In k3s pods: Bitnami Sealed Secrets → k8s Secret → env var / file mount

---

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.11+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Bazelisk](https://github.com/bazelbuild/bazelisk) | any | `brew install bazelisk` |
| [Rancher Desktop](https://rancherdesktop.io/) | 1.35+ | Download from site — provides k3s + Docker |
| [kubeseal](https://github.com/bitnami-labs/sealed-secrets) | 0.27+ | `brew install kubeseal` |
| Python | 3.13+ | managed by uv |

A [Robinhood account with Agentic API access](https://robinhood.com/agentic) is required.

---

## First-Time Setup

### 1. Clone and install

```bash
git clone git@github.com:prafful13/robinhood-pilot.git
cd robinhood-pilot
uv sync --all-groups        # creates .venv, installs all deps
```

### 2. Configure

Edit `config.yaml` and set your account number and watchlist:

```yaml
account_number: "YOUR_ACCOUNT_NUMBER"   # Robinhood → Account → Account number
watchlist:
  - GOOGL
  - AAPL
  # add/remove tickers here
```

### 3. Install Sealed Secrets controller into k3s

```bash
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.27.0/controller.yaml
```

### 4. Generate secrets

```bash
uv run inv secrets-init     # generates postgres password → macOS Keychain
uv run inv auth             # OAuth browser flow → token → macOS Keychain
```

`inv auth` will open a browser tab. Log in to Robinhood and grant access. The token is saved to your macOS Keychain (never written to disk).

### 5. Seal secrets for k8s

```bash
kubectl apply -f k8s/namespace.yaml

uv run inv k8s-seal                    # Keychain → encrypted YAMLs in k8s/sealed/
kubectl apply -f k8s/sealed/           # controller decrypts → k8s Secrets
```

### 6. Build and deploy

```bash
uv run inv docker-build     # builds robinhood-bot:latest and robinhood-dashboard:latest
uv run inv k8s-apply        # deploys postgres, bot, dashboard to k3s
```

After ~30 seconds:

```bash
uv run inv k8s-status       # should show 3 pods Running
```

Dashboard is live at **http://localhost:30501**.

---

## Day-to-Day

```bash
uv run inv k8s-status       # check pod health
uv run inv k8s-logs         # stream bot logs
uv run inv k8s-restart      # rolling restart after code changes
uv run inv k8s-psql         # psql shell into the live database
```

### After code changes

```bash
uv run inv docker-build && uv run inv k8s-restart
```

### Re-authenticate (token expires ~30 days)

```bash
uv run inv auth
uv run inv k8s-seal
kubectl apply -f k8s/sealed/
uv run inv k8s-restart
```

### Add/change dependencies

```bash
# edit pyproject.toml, then:
uv sync --all-groups
uv run inv lock-update      # regenerates requirements.lock for Docker
```

---

## All Tasks

```bash
uv run inv --list
```

| Task | Purpose |
|---|---|
| `secrets-init` | Generate postgres password → Keychain (run once) |
| `auth` | OAuth browser flow → token → Keychain |
| `keychain-set KEY VALUE` | Store any secret in Keychain manually |
| `keychain-get KEY` | Read a secret from Keychain |
| `k8s-seal` | Keychain → `k8s/sealed/` SealedSecret YAMLs |
| `k8s-apply` | Apply all k8s manifests |
| `k8s-status` | Show pods, services, PVCs |
| `k8s-logs` | Stream bot logs |
| `k8s-restart` | Rolling restart |
| `k8s-postgres` | Deploy/redeploy postgres |
| `k8s-psql` | Open psql shell (port-forwards automatically) |
| `k8s-delete` | Tear down all resources (keeps namespace) |
| `docker-build` | Build container images |
| `lock-update` | Regenerate `requirements.lock` |
| `build` | Run Bazel build |
| `bot` | Run bot locally via Bazel (not k8s) |
| `dashboard` | Run dashboard locally via Bazel |

---

## Project Structure

```
robinhood-pilot/
├── broker/           MCP client + OAuth PKCE flow
│   ├── oauth.py      Keychain (local) / TOKEN_FILE (k8s) — auto-detected
│   └── robinhood.py  JSON-RPC HTTP client for Robinhood MCP
├── strategy/
│   └── rsi.py        RSI(14) via Wilder's EWM — no external TA library
├── risk/
│   └── manager.py    Position limits, daily loss cap
├── db/
│   ├── models.py     SQLAlchemy Trade model
│   └── database.py   PostgreSQL (DB_URL env) or Keychain+port-forward (local)
├── tax/
│   └── calculator.py FIFO cost basis, ST/LT gain split
├── vault/
│   └── keychain.py   macOS Keychain wrapper (dev only — not shipped in containers)
├── k8s/
│   ├── sealed/       Encrypted SealedSecret YAMLs — gitignored (safe to commit,
│   │                 but kept local by preference). Run `inv k8s-seal` to regenerate.
│   ├── postgres-*.yaml
│   ├── bot-deployment.yaml
│   ├── dashboard-deployment.yaml
│   └── dashboard-service.yaml
├── main.py           Bot main loop (market hours check, 15-min polling)
├── dashboard.py      Streamlit tax + trade history dashboard
├── tasks.py          invoke task runner (replaces Makefile)
├── config.yaml       Runtime config — edit this, never hardcode values
├── pyproject.toml    Python deps (psycopg2, sqlalchemy, streamlit, plotly…)
├── requirements.lock Pinned lockfile for Docker images
├── MODULE.bazel      Bazel Bzlmod config
└── Dockerfile.{bot,dashboard}
```

---

## Security Model

| What | Where | Notes |
|---|---|---|
| OAuth token | macOS Keychain | Key: `oauth_tokens`, service: `robinhood-trader` |
| Postgres password | macOS Keychain | Key: `postgres_password` |
| k8s Secrets | Sealed Secrets controller | Decrypted in-cluster only |
| Pods | Receive secrets as env vars | Never touch Keychain directly |
| Git repo | No secrets, ever | `k8s/sealed/` gitignored; `.rh_tokens.json` gitignored |

The sealed YAMLs in `k8s/sealed/` are encrypted with your cluster's RSA key — they are safe to commit to public git if you want reproducibility. They are gitignored in this repo by preference only.

---

## Teardown

```bash
uv run inv k8s-delete                              # delete all app resources
kubectl delete namespace robinhood-trader          # delete the namespace
kubectl delete -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.27.0/controller.yaml
```

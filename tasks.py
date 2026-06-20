"""
Project task runner.
Usage:
    uv run inv <task>    # no venv activation needed
    inv <task>           # with venv activated

Secrets:   keychain-set, keychain-get, secrets-init, k8s-seal
Local dev: auth, bot, dashboard, build, lock-update
Postgres:  k8s-postgres, k8s-psql
Containers: docker-build, k8s-apply, k8s-status, k8s-logs, k8s-restart, k8s-delete

Workflow (first time):
  1. uv run inv secrets-init        # generate + store postgres password in Keychain
  2. uv run inv auth                # OAuth → token stored in Keychain
  3. kubectl apply -f k8s/namespace.yaml
  4. uv run inv k8s-seal            # Keychain → SealedSecret YAMLs in k8s/sealed/
  5. kubectl apply -f k8s/sealed/   # SealedSecrets → k8s Secrets (via controller)
  6. uv run inv docker-build
  7. uv run inv k8s-apply           # namespace, configmap, postgres, bot, dashboard
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import yaml
from invoke import task

PROJECT = Path(__file__).parent.resolve()
VENV_PYTHON = PROJECT / ".venv" / "bin" / "python"
VENV_STREAMLIT = PROJECT / ".venv" / "bin" / "streamlit"
LOGS = PROJECT / "logs"

NS = "robinhood-trader"
SEALED_DIR = PROJECT / "k8s" / "sealed"


# ── Keychain helpers ─────────────────────────────────────────────────────────

@task(name="keychain-set")
def keychain_set(c, key, value):
    """Store a secret in macOS Keychain: inv keychain-set <key> <value>"""
    from vault.keychain import set as kc_set
    kc_set(key, value)
    print(f"✓ '{key}' stored in Keychain (service=robinhood-trader)")


@task(name="keychain-get")
def keychain_get(c, key):
    """Print a secret from macOS Keychain: inv keychain-get <key>"""
    from vault.keychain import get
    val = get(key)
    if val:
        print(f"{key}: {val}")
    else:
        print(f"'{key}' not found in Keychain")


@task(name="secrets-init")
def secrets_init(c):
    """Generate and store all app secrets in Keychain (run once on first setup).
    Skips any key that already exists."""
    import secrets as py_secrets
    from vault.keychain import get, set as kc_set

    if not get("postgres_password"):
        pg_pass = py_secrets.token_urlsafe(32)
        kc_set("postgres_password", pg_pass)
        print("✓ postgres_password generated and stored in Keychain")
    else:
        print("  postgres_password already in Keychain")

    print("\nNext steps:")
    print("  1. Run:  uv run inv auth          (OAuth browser flow)")
    print("  2. Run:  uv run inv k8s-seal      (seal all secrets for k8s)")
    print("  3. Run:  kubectl apply -f k8s/sealed/")


# ── OAuth ────────────────────────────────────────────────────────────────────

@task
def auth(c):
    """Run the OAuth browser flow. Token is saved to macOS Keychain."""
    c.run(
        f"{VENV_PYTHON} -c \""
        "import asyncio, yaml; "
        "from broker.oauth import get_access_token; "
        "cfg = yaml.safe_load(open('config.yaml'))['broker']; "
        "token = asyncio.run(get_access_token(cfg)); "
        "print('Token obtained, length:', len(token))"
        "\""
    )


# ── Sealed Secrets ────────────────────────────────────────────────────────────

def _seal_secret(name: str, data: dict[str, str]) -> None:
    """Seal a k8s Secret from plain-text data dict → k8s/sealed/<name>.yaml"""
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": NS},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }
    SEALED_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(secret, f)
        tmp = f.name

    import subprocess
    result = subprocess.run(
        ["kubeseal", "--format", "yaml"],
        input=Path(tmp).read_bytes(),
        capture_output=True,
    )
    Path(tmp).unlink()
    if result.returncode != 0:
        raise RuntimeError(f"kubeseal failed: {result.stderr.decode()}")

    out = SEALED_DIR / f"{name}.yaml"
    out.write_bytes(result.stdout)
    print(f"  ✓ k8s/sealed/{name}.yaml  (safe to commit)")


@task(name="k8s-seal")
def k8s_seal(c):
    """Read all secrets from Keychain → produce SealedSecret YAMLs in k8s/sealed/.
    These YAML files are safe to commit to git.
    Apply them with: kubectl apply -f k8s/sealed/
    """
    from vault.keychain import get

    sealed_any = False

    # OAuth token
    tokens_json = get("oauth_tokens")
    if tokens_json:
        _seal_secret("rh-tokens", {"rh_tokens.json": tokens_json})
        sealed_any = True
    else:
        print("  ⚠ oauth_tokens not in Keychain — run 'inv auth' first")

    # Postgres credentials
    pg_pass = get("postgres_password")
    if pg_pass:
        db_url = f"postgresql+psycopg2://trader:{pg_pass}@postgres:5432/robinhood_trader"
        _seal_secret("postgres-credentials", {"password": pg_pass, "db_url": db_url})
        sealed_any = True
    else:
        print("  ⚠ postgres_password not in Keychain — run 'inv secrets-init' first")

    if sealed_any:
        print("\nApply with:")
        print(f"  kubectl apply -f k8s/sealed/")


# ── Dev server tasks ─────────────────────────────────────────────────────────

@task
def bot(c):
    """Run the trading bot via Bazel."""
    c.run("bazelisk run //:bot", pty=True)


@task
def dashboard(c):
    """Launch the Streamlit tax dashboard at http://localhost:8501."""
    c.run("bazelisk run //:dashboard", pty=True)


@task
def build(c):
    """Build all Bazel targets."""
    c.run("bazelisk build //...")


@task(name="lock-update")
def lock_update(c):
    """Regenerate requirements.lock from pyproject.toml (run after adding deps)."""
    c.run("uv export --no-dev --format requirements-txt --no-hashes > requirements.lock")
    c.run("sed -i '' '/^-e \\./d' requirements.lock")
    print("✓ requirements.lock updated — commit both pyproject.toml and requirements.lock")


# ── Docker + Kubernetes ───────────────────────────────────────────────────────

@task(name="docker-build")
def docker_build(c):
    """Build bot and dashboard container images."""
    c.run("docker build -f Dockerfile.bot      -t robinhood-bot:latest .")
    c.run("docker build -f Dockerfile.dashboard -t robinhood-dashboard:latest .")
    print("✓ Images built: robinhood-bot:latest  robinhood-dashboard:latest")


@task(name="k8s-load-images")
def k8s_load_images(c):
    """Import local Docker images into k3s (only needed for bare k3s, not Rancher Desktop)."""
    c.run("docker save robinhood-bot:latest      | k3s ctr images import -")
    c.run("docker save robinhood-dashboard:latest | k3s ctr images import -")
    print("✓ Images loaded into k3s")


@task(name="k8s-postgres")
def k8s_postgres(c):
    """Deploy PostgreSQL to k3s (requires postgres-credentials SealedSecret already applied)."""
    c.run("kubectl apply -f k8s/postgres-pvc.yaml")
    c.run("kubectl apply -f k8s/postgres-deployment.yaml")
    c.run("kubectl apply -f k8s/postgres-service.yaml")
    print(f"✓ PostgreSQL deploying in namespace '{NS}'")
    print("  Watch:       kubectl get pods -n robinhood-trader -l app=postgres")
    print("  Local access: inv k8s-psql")


@task(name="k8s-psql")
def k8s_psql(c):
    """Open a psql shell to the in-cluster PostgreSQL (via port-forward)."""
    from vault.keychain import get
    pg_pass = get("postgres_password") or ""
    print("Starting port-forward on localhost:5432 → postgres:5432 ...")
    print("Connecting as trader@robinhood_trader ...")
    c.run(
        f"kubectl port-forward svc/postgres 5432:5432 -n {NS} &"
        f" sleep 2 && PGPASSWORD={pg_pass!r} psql -h localhost -U trader -d robinhood_trader",
        pty=True,
        warn=True,
    )


@task(name="k8s-apply")
def k8s_apply(c):
    """Apply all Kubernetes manifests.
    Prerequisite: kubectl apply -f k8s/sealed/  (SealedSecrets must exist first)
    """
    # Namespace
    c.run("kubectl apply -f k8s/namespace.yaml")

    # ConfigMap from config.yaml
    c.run(
        f"kubectl create configmap trader-config "
        f"--from-file=config.yaml=config.yaml "
        f"-n {NS} "
        f"--dry-run=client -o yaml | kubectl apply -f -"
    )

    # PostgreSQL
    c.run("kubectl apply -f k8s/postgres-pvc.yaml")
    c.run("kubectl apply -f k8s/postgres-deployment.yaml")
    c.run("kubectl apply -f k8s/postgres-service.yaml")

    # Application deployments
    for manifest in ("bot-deployment.yaml", "dashboard-deployment.yaml", "dashboard-service.yaml"):
        c.run(f"kubectl apply -f k8s/{manifest}")

    print(f"\n✓ All manifests applied to namespace '{NS}'")
    print("  Dashboard:    http://localhost:30501")
    print("  Watch:        inv k8s-status")


@task(name="k8s-status")
def k8s_status(c):
    """Show pods, deployments, services, and PVCs in the robinhood-trader namespace."""
    c.run(f"kubectl get pods,deployments,services,pvc -n {NS}")


@task(name="k8s-logs")
def k8s_logs(c):
    """Stream logs from the trading bot pod."""
    result = c.run(
        f"kubectl get pod -n {NS} -l app=robinhood-bot -o jsonpath='{{.items[0].metadata.name}}'",
        hide=True, warn=True,
    )
    pod = result.stdout.strip()
    if not pod:
        print("Bot pod not found. Is it running? Check: inv k8s-status")
        return
    c.run(f"kubectl logs -f {pod} -n {NS}", pty=True)


@task(name="k8s-restart")
def k8s_restart(c):
    """Rolling restart of both application deployments."""
    c.run(f"kubectl rollout restart deployment/robinhood-bot       -n {NS}")
    c.run(f"kubectl rollout restart deployment/robinhood-dashboard  -n {NS}")
    c.run(f"kubectl rollout status  deployment/robinhood-bot       -n {NS}")


@task(name="k8s-delete")
def k8s_delete(c):
    """Delete all resources in the namespace (keeps the namespace and SealedSecrets)."""
    c.run(f"kubectl delete deployments,services,pvc,configmap,secret --all -n {NS}", warn=True)
    print("✓ All resources deleted (namespace kept; re-apply SealedSecrets before redeploying)")


# ── launchd (kept for reference; k8s is the preferred deployment) ─────────────

PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
BOT_LABEL = "com.robinhoodtrader.bot"
DASH_LABEL = "com.robinhoodtrader.dashboard"


def _plist_content(label: str, args: list[str], log_name: str) -> str:
    args_xml = "\n".join(f"        <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{PROJECT}</string>
    <key>KeepAlive</key>
    <dict><key>Crashed</key><true/></dict>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{LOGS}/{log_name}.log</string>
    <key>StandardErrorPath</key>
    <string>{LOGS}/{log_name}.error.log</string>
</dict>
</plist>
"""


@task(name="svc-install")
def svc_install(c):
    """Install bot + dashboard as macOS launchd services (legacy; prefer k8s)."""
    LOGS.mkdir(exist_ok=True)
    for label, args, name in (
        (BOT_LABEL, [str(VENV_PYTHON), str(PROJECT / "main.py")], "bot"),
        (DASH_LABEL, [str(VENV_STREAMLIT), "run", str(PROJECT / "dashboard.py"),
                      "--server.port=8501", "--server.headless=true",
                      "--browser.gatherUsageStats=false"], "dashboard"),
    ):
        dest = PLIST_DIR / f"{label}.plist"
        dest.write_text(_plist_content(label, args, name))
        c.run(f"launchctl load {dest}")
        print(f"  loaded {dest}")
    print("✓ Services installed")


@task(name="svc-uninstall")
def svc_uninstall(c):
    """Remove launchd services entirely."""
    for label in (BOT_LABEL, DASH_LABEL):
        plist = PLIST_DIR / f"{label}.plist"
        c.run(f"launchctl unload {plist}", warn=True)
        plist.unlink(missing_ok=True)
    print("✓ Services removed")


@task(name="svc-start")
def svc_start(c):
    c.run(f"launchctl start {BOT_LABEL}")
    c.run(f"launchctl start {DASH_LABEL}")


@task(name="svc-stop")
def svc_stop(c):
    c.run(f"launchctl stop {BOT_LABEL}", warn=True)
    c.run(f"launchctl stop {DASH_LABEL}", warn=True)


@task(name="svc-restart")
def svc_restart(c):
    svc_stop(c)
    svc_start(c)

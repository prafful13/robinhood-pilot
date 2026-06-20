"""
Project task runner.
Usage:
    uv run inv <task>    # no venv activation needed
    inv <task>           # with venv activated

Secrets:    keychain-set, keychain-get, secrets-init, k8s-seal
Local dev:  auth, bot, dashboard, build, lock-update
Postgres:   k8s-psql  (provisioned externally via k3s-dev)
Containers: docker-build, k8s-apply, k8s-status, k8s-logs, k8s-stop, k8s-start, k8s-restart, k8s-delete

Workflow (first time):
  1. k3s-dev init --skip-postgres          # bootstrap Sealed Secrets controller
  2. k3s-dev namespace add robinhood-trader
  3. k3s-dev postgres add robinhood \\
       --namespace robinhood-trader \\
       --user trader \\
       --db robinhood_trader \\
       --secret-name postgres-credentials \\
       --backup
  4. uv run inv auth                       # OAuth → token stored in Keychain
  5. uv run inv k8s-seal                   # Keychain → k8s/sealed/rh-tokens.yaml
  6. kubectl apply -f k8s/sealed/
  7. uv run inv docker-build
  8. uv run inv k8s-apply
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import yaml
from invoke import task

PROJECT = Path(__file__).parent.resolve()
VENV_PYTHON = PROJECT / ".venv" / "bin" / "python"

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
    """Check OAuth secrets in Keychain. Postgres is managed by k3s-dev."""
    from vault.keychain import get

    if not get("client_id"):
        print("  ⚠ client_id not in Keychain — run:")
        print("      uv run inv keychain-set client_id <value>")
    else:
        print("  ✓ client_id in Keychain")

    print("\nNext steps:")
    print("  1. uv run inv keychain-set client_id <value>  (if not set)")
    print("  2. uv run inv auth          (OAuth browser flow)")
    print("  3. uv run inv k8s-seal      (seal rh-tokens for k8s)")
    print("  4. kubectl apply -f k8s/sealed/")
    print()
    print("Postgres is provisioned by k3s-dev — see workflow in tasks.py header.")


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
    """Seal OAuth tokens → k8s/sealed/rh-tokens.yaml.
    Postgres credentials are a direct k8s Secret managed by k3s-dev (no sealing needed).
    Apply with: kubectl apply -f k8s/sealed/
    """
    from vault.keychain import get

    tokens_json = get("oauth_tokens")
    client_id = get("client_id")
    if not tokens_json:
        print("  ⚠ oauth_tokens not in Keychain — run 'inv auth' first")
        return

    rh_data = {"rh_tokens.json": tokens_json}
    if client_id:
        rh_data["client_id"] = client_id
    else:
        print("  ⚠ client_id not in Keychain — ROBINHOOD_CLIENT_ID won't be set in k8s")
    _seal_secret("rh-tokens", rh_data)

    print("\nApply with:")
    print("  kubectl apply -f k8s/sealed/")


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


@task(name="k8s-psql")
def k8s_psql(c):
    """Open a psql shell to Postgres (NodePort managed by k3s-dev)."""
    import json
    import keyring
    state_file = Path.home() / ".k3s-dev" / "state.json"
    if not state_file.exists():
        print("k3s-dev state not found — provision postgres first:")
        print("  k3s-dev postgres add robinhood --namespace robinhood-trader \\")
        print("    --user trader --db robinhood_trader --secret-name postgres-credentials --backup")
        return
    inst = json.loads(state_file.read_text()).get("postgres_instances", {}).get("robinhood", {})
    port = inst.get("node_port", 30432)
    pw = keyring.get_password("k3s-dev", "postgres/robinhood") or ""
    c.run(f"PGPASSWORD={pw!r} psql -h localhost -p {port} -U trader -d robinhood_trader", pty=True)


@task(name="k8s-apply")
def k8s_apply(c):
    """Apply all Kubernetes manifests.
    Prerequisites:
      - k3s-dev postgres add robinhood ... (postgres-credentials Secret must exist)
      - kubectl apply -f k8s/sealed/       (rh-tokens SealedSecret must exist)
    """
    # ConfigMap from config.yaml
    c.run(
        f"kubectl create configmap trader-config "
        f"--from-file=config.yaml=config.yaml "
        f"-n {NS} "
        f"--dry-run=client -o yaml | kubectl apply -f -"
    )

    # Logs PVC
    c.run("kubectl apply -f k8s/logs-pvc.yaml")

    # Application deployments
    for manifest in ("bot-deployment.yaml", "dashboard-deployment.yaml", "dashboard-service.yaml"):
        c.run(f"kubectl apply -f k8s/{manifest}")

    print(f"\n✓ Manifests applied to namespace '{NS}'")
    print("  Dashboard:    http://localhost:30501")
    print("  Watch:        inv k8s-status")


@task(name="k8s-status")
def k8s_status(c):
    """Show pods, deployments, services, and PVCs in the robinhood-trader namespace."""
    c.run(f"kubectl get pods,deployments,services,pvc -n {NS}")


@task(name="k8s-logs")
def k8s_logs(c):
    """Stream live bot logs via kubectl (stdout only — last ~2 MiB of in-memory buffer)."""
    result = c.run(
        f"kubectl get pod -n {NS} -l app=robinhood-bot -o jsonpath='{{.items[0].metadata.name}}'",
        hide=True, warn=True,
    )
    pod = result.stdout.strip()
    if not pod:
        print("Bot pod not found. Is it running? Check: inv k8s-status")
        return
    c.run(f"kubectl logs -f {pod} -n {NS}", pty=True)


@task(name="k8s-logs-file")
def k8s_logs_file(c, date=""):
    """Read persisted bot log files from the PVC (full history, daily rotation).
    Usage:
      inv k8s-logs-file            # tail today's log
      inv k8s-logs-file --date 2026-06-19   # read a specific day
    """
    result = c.run(
        f"kubectl get pod -n {NS} -l app=robinhood-bot -o jsonpath='{{.items[0].metadata.name}}'",
        hide=True, warn=True,
    )
    pod = result.stdout.strip()
    if not pod:
        print("Bot pod not found.")
        return
    if date:
        c.run(f"kubectl exec {pod} -n {NS} -- cat /logs/bot.log.{date}", pty=True, warn=True)
    else:
        c.run(f"kubectl exec {pod} -n {NS} -- tail -f /logs/bot.log", pty=True)


@task(name="k8s-logs-ls")
def k8s_logs_ls(c):
    """List all rotated log files on the PVC with sizes."""
    result = c.run(
        f"kubectl get pod -n {NS} -l app=robinhood-bot -o jsonpath='{{.items[0].metadata.name}}'",
        hide=True, warn=True,
    )
    pod = result.stdout.strip()
    if not pod:
        print("Bot pod not found.")
        return
    c.run(f"kubectl exec {pod} -n {NS} -- ls -lh /logs/", pty=True)


@task(name="k8s-logs-pull")
def k8s_logs_pull(c, dest="./bot-logs"):
    """Copy all PVC log files to local disk.
    Usage:
      inv k8s-logs-pull                 # copies to ./bot-logs/
      inv k8s-logs-pull --dest ~/logs   # custom destination
    """
    result = c.run(
        f"kubectl get pod -n {NS} -l app=robinhood-bot -o jsonpath='{{.items[0].metadata.name}}'",
        hide=True, warn=True,
    )
    pod = result.stdout.strip()
    if not pod:
        print("Bot pod not found.")
        return
    Path(dest).mkdir(parents=True, exist_ok=True)
    c.run(f"kubectl cp {NS}/{pod}:/logs {dest}", warn=True)
    print(f"✓ Logs copied to {dest}/")
    import subprocess
    subprocess.run(["ls", "-lh", dest])


@task(name="k8s-stop")
def k8s_stop(c):
    """Scale bot and dashboard to 0 replicas (preserves PVCs and Secrets)."""
    c.run(f"kubectl scale deployment/robinhood-bot       --replicas=0 -n {NS}")
    c.run(f"kubectl scale deployment/robinhood-dashboard --replicas=0 -n {NS}")
    print("✓ Bot and dashboard stopped (postgres untouched)")


@task(name="k8s-start")
def k8s_start(c):
    """Scale bot and dashboard back to 1 replica each."""
    c.run(f"kubectl scale deployment/robinhood-bot       --replicas=1 -n {NS}")
    c.run(f"kubectl scale deployment/robinhood-dashboard --replicas=1 -n {NS}")
    c.run(f"kubectl rollout status deployment/robinhood-bot -n {NS}")


@task(name="k8s-restart")
def k8s_restart(c):
    """Rolling restart of both application deployments."""
    c.run(f"kubectl rollout restart deployment/robinhood-bot       -n {NS}")
    c.run(f"kubectl rollout restart deployment/robinhood-dashboard  -n {NS}")
    c.run(f"kubectl rollout status  deployment/robinhood-bot       -n {NS}")


@task(name="k8s-backup-now")
def k8s_backup_now(c):
    """Trigger an immediate Postgres backup (delegated to k3s-dev)."""
    c.run("k3s-dev postgres backup robinhood")


@task(name="k8s-backup-ls")
def k8s_backup_ls(c):
    """List all database backups on the backup PVC."""
    result = c.run(
        f"kubectl get pod -n {NS} -l app=robinhood -o jsonpath='{{.items[0].metadata.name}}'",
        hide=True, warn=True,
    )
    pod = result.stdout.strip()
    if not pod:
        print("Postgres pod not found — check: kubectl get pods -n robinhood-trader")
        return
    c.run(
        f"kubectl exec {pod} -n {NS} -- "
        f"sh -c 'ls -lht /backups/*.sql.gz 2>/dev/null || echo No backups yet'",
        warn=True,
    )


@task(name="k8s-delete")
def k8s_delete(c):
    """Delete app resources (bot, dashboard, configmap). Postgres is owned by k3s-dev."""
    for resource in ("deployment/robinhood-bot", "deployment/robinhood-dashboard",
                     "service/robinhood-dashboard", "pvc/bot-logs", "configmap/trader-config"):
        c.run(f"kubectl delete {resource} -n {NS} --ignore-not-found", warn=True)
    print("✓ App resources deleted (postgres untouched — use: k3s-dev postgres remove robinhood)")



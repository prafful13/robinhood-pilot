"""Generate docs/architecture.png — run with: uv run python docs/gen_architecture.py

Requires graphviz system package:
    brew install graphviz
"""

from __future__ import annotations

from diagrams import Diagram, Cluster, Edge
from diagrams.generic.compute import Rack
from diagrams.k8s.compute import Deploy
from diagrams.k8s.storage import PVC
from diagrams.k8s.network import SVC
from diagrams.k8s.others import CRD
from diagrams.onprem.client import Client, User
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.network import Internet

OUTPUT = "docs/architecture"

graph_attr = {
    "pad": "0.75",
    "splines": "ortho",
    "fontsize": "14",
    "bgcolor": "white",
    "rankdir": "LR",
}
node_attr = {"fontsize": "11"}

with Diagram(
    "Robinhood Pilot — System Architecture",
    filename=OUTPUT,
    show=False,
    graph_attr=graph_attr,
    node_attr=node_attr,
):
    keychain = Rack("macOS Keychain\noauth_tokens · client_id\npostgres_password")
    browser = Client("Browser\nlocalhost:30501")
    rh_api = Internet("Robinhood MCP API\nagent.robinhood.com")

    with Cluster("k8s namespace: robinhood-trader"):
        sealed = CRD("SealedSecrets\nk8s/sealed/")

        with Cluster("robinhood-bot"):
            bot = Deploy("bot (15-min cycles)")
            log_pvc = PVC("10Gi PVC  /logs")

        with Cluster("robinhood-dashboard"):
            dashboard = Deploy("dashboard")
            nodeport = SVC("NodePort :30501")

        with Cluster("postgres"):
            pg = PostgreSQL("robinhood_trader")
            pg_pvc = PVC("5Gi PVC")

    # Secrets flow
    keychain >> Edge(label="inv k8s-seal\n(kubeseal)") >> sealed
    sealed >> Edge(label="controller decrypts") >> bot
    sealed >> Edge(label="controller decrypts") >> dashboard

    # Bot → external + DB
    bot >> Edge(label="MCP JSON-RPC") >> rh_api
    bot >> Edge(label="trades · snapshots\nbot_status") >> pg
    bot >> log_pvc

    # Dashboard ↔ DB + browser
    dashboard >> Edge(label="RuntimeConfig · BotControl") >> pg
    pg >> Edge(label="read portfolio\ntrades · status") >> dashboard
    browser >> Edge(label="HTTP :30501") >> nodeport >> dashboard

    # Postgres storage
    pg >> pg_pvc

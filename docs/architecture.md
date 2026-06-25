# robinhood-pilot — Architecture

```mermaid
flowchart TB
    subgraph ext["External"]
        RH["Robinhood API"]
    end

    subgraph sec["Secrets Pipeline"]
        KC["macOS Keychain<br/>OAuth tokens + DB password"]
        SS["Bitnami Sealed Secrets<br/>encrypted YAMLs committed to git"]
        K8S["k8s Secrets"]
        KC --> SS --> K8S
    end

    subgraph k3s["k3s Cluster — namespace: robinhood"]
        subgraph bot["Bot Pod"]
            LOOP["main.py<br/>async loop · 15 min · market hours only"]
            STRAT["Strategy Selector<br/>RSIMeanReversion / MACDCrossover / BollingerBands / RSIMACDCombo"]
            RISK["RiskManager<br/>max positions · max trade USD · daily loss cap"]
            DESIRED["DesiredPosition State Machine<br/>pending → achieved / failed / superseded"]
            CLIENT["RobinhoodClient<br/>OAuth session"]
            LOOP --> STRAT --> RISK --> DESIRED --> CLIENT
        end

        subgraph dash["Dashboard Pod — NodePort 30501"]
            STREAM["Streamlit App<br/>portfolio · trades · signals<br/>RuntimeConfig · BotControl · manual refresh"]
        end

        PG[("PostgreSQL<br/>Trades · PortfolioSnapshot · SymbolSnapshot<br/>BotStatus · BotControl · RuntimeConfig<br/>DesiredPosition")]
        LOGPVC[("Logs PVC<br/>daily-rotating /logs/bot.log")]
    end

    USER["Browser<br/>localhost:30501"]

    K8S -->|"rh-tokens + postgres-creds"| bot
    K8S -->|"postgres-creds"| dash
    CLIENT -->|"buy / sell orders"| RH
    bot -->|"write snapshots, trades, status"| PG
    LOOP -->|"rotating logs"| LOGPVC
    STREAM -->|"read metrics + state"| PG
    USER --> STREAM

    classDef external fill:#f5f5f5,stroke:#888,color:#333
    classDef secrets fill:#fff8e1,stroke:#f9a825,color:#333
    classDef pod fill:#e3f2fd,stroke:#1565c0,color:#333
    classDef db fill:#e8f5e9,stroke:#2e7d32,color:#333
    classDef user fill:#fce4ec,stroke:#c62828,color:#333

    class RH external
    class KC,SS,K8S secrets
    class LOOP,STRAT,RISK,DESIRED,CLIENT,STREAM pod
    class PG,LOGPVC db
    class USER user
```

**Legend**
- Light grey — external services
- Yellow — secrets pipeline (Keychain → Sealed Secrets → k8s)
- Blue — application pods / components
- Green — persistent storage
- Pink — end-user entry points

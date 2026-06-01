# 🔬 sudiviz

[![Website](https://img.shields.io/badge/Website-sudiviz-blue?style=flat-square)](https://d2ewlh2csw2k2n.cloudfront.net) [![PyPI](https://img.shields.io/pypi/v/sudiviz?style=flat-square)](https://pypi.org/project/sudiviz/) [![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

> *X-ray vision for your cloud infrastructure*

**sudiviz** visualizes your live AWS infrastructure as an interactive graph. Auto-detects misconfigurations, unhealthy targets, and orphan resources — then fixes them with one command.

🚀 Zero AI tokens | 💸 Zero cost | 🐍 Pure Python

![Web Graph](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_ingress_traffic_dark_mode.png)

---

## 📦 Quick Start

```bash
pip install 'sudiviz[all]'

# Diagnose your infrastructure
sudiviz diagnose

# Interactive web visualization
sudiviz graph --output web --open

# Auto-fix issues
sudiviz fix --apply
```

> **Auth:** Uses standard boto3 credentials (`~/.aws/credentials`, env vars, SSO, or instance profile)

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| **Live Topology** | Real-time graph of ALB → Target Groups → EC2 → Security Groups |
| **Health Detection** | Unhealthy targets, failing health checks, orphan resources |
| **Auto-Fix** | One-click remediation with `sudiviz fix --apply` |
| **Traffic Animation** | Visualize request flow with animated pulses |
| **Health Heatmaps** | Color-code infrastructure by health status |
| **Cost Heatmap** | FinOps view — visualize estimated monthly costs per resource |
| **Security Group Flows** | Visualize ingress/egress rules between security groups |
| **CloudWatch Integration** | One-click links to metrics and logs for each resource |
| **Dark/Light Mode** | Toggle theme in web UI |
| **Cluster Grouping** | Group resources by service type (Load Balancers, ECS, Security, etc.) |
| **Terraform Drift** | Compare live AWS vs Terraform state |
| **Multi-Service** | ALB, EC2, ECS, EKS, RDS, Lambda, S3, Security Groups |

---

## 🎨 Visualization Modes

### Terminal
```bash
sudiviz diagnose --region us-east-1
```

### TUI (Interactive Terminal)
```bash
sudiviz tui
```

### Web (Cytoscape.js)
```bash
sudiviz graph --output web --port 8000 --open
```

### PNG Export
```bash
sudiviz graph --output png --file topology.png
```

---

## 🔧 Auto-Fix

```bash
sudiviz fix                    # Preview fixes (dry-run)
sudiviz fix --apply            # Apply all fixes
sudiviz fix 1 --apply          # Apply specific fix
sudiviz fix --apply --force    # Include destructive operations
```

**Supported fixes:**
- Security group missing ingress rules
- S3 public access / encryption
- RDS public accessibility
- Orphan target groups (with `--force`)
- Unused security groups (with `--force`)

---

## 🔒 Security

sudiviz is built with security in mind. Every release is scanned for vulnerabilities.

| Check | Status |
|-------|--------|
| **Bandit SAST** | ✅ No issues |
| **XSS Protection** | ✅ HTML sanitization enabled |
| **Dependency CVEs** | ✅ All patched |
| **Hardcoded Secrets** | ✅ None |
| **Shell Injection** | ✅ No `shell=True` |
| **Code Injection** | ✅ No `eval()` |

Run security scan locally:
```bash
pip install bandit[toml]
bandit -c pyproject.toml -r sudiviz/
```

---

## 🔄 Terraform Drift

```bash
terraform show -json > tfstate.json
sudiviz drift --tfstate tfstate.json
```

---

## 📊 CI Integration

```bash
# Fail CI on critical issues
sudiviz diagnose --json | jq '.diagnosis.fixes[] | select(.severity=="critical")'

# Drift detection gate
sudiviz drift --tfstate tfstate.json --json
```

| Exit Code | Meaning |
|-----------|---------|
| `0` | No issues |
| `1` | Drift detected |
| `2` | Critical issues found |

---

## 📸 More Screenshots

<details>
<summary>Click to expand</summary>

### Terminal TUI
![TUI](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_tui.png)

### Diagnose Output
![Diagnose](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_diagnose_before_lb.png)

### Auto-Fix Preview
![Fix](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_fix.png)

### Traffic Flow Animation (Dark Mode)
![Traffic](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_traffic_flow_in_dark-mode.png)

### Health Heatmap
![Heatmap](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_health_status.png)

### Cluster Grouping (Dark Mode)
![Cluster](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_cluster_dark_mode.png)

### Cost Heatmap (FinOps)
![Cost](https://raw.githubusercontent.com/pydevsg/sudiviz/main/docs/images/sudiviz_cost_graph.png)

</details>

---

## 🆚 Comparison

| Feature | sudiviz | Hava.io | Cloudcraft |
|---------|:-------:|:-------:|:----------:|
| Live data | ✅ | ❌ | ❌ |
| Auto-fix | ✅ | ❌ | ❌ |
| Traffic animation | ✅ | ❌ | ❌ |
| Health heatmaps | ✅ | ❌ | ❌ |
| Cost heatmap | ✅ | ❌ | ❌ |
| Cluster grouping | ✅ | ❌ | ❌ |
| Terraform drift | ✅ | ❌ | ❌ |
| Orphan detection | ✅ | ❌ | ❌ |
| Free & open source | ✅ MIT | $29/mo | $49/mo |

---

## 🔐 IAM Permissions

**Read-only** (`sudiviz diagnose`):
- `ReadOnlyAccess` AWS managed policy

**Write** (`sudiviz fix --apply`):
- `AmazonEC2FullAccess`
- `ElasticLoadBalancingFullAccess`
- `AmazonS3FullAccess`
- `AmazonRDSFullAccess`

---

## 📖 Documentation

<details>
<summary>AWS Services Discovered</summary>

| Service | What's collected |
|---------|-----------------|
| **ALB / NLB** | Load balancers, listeners, rules |
| **Target Groups** | Health status per target |
| **EC2** | State, IPs, security groups |
| **Security Groups** | Ingress/egress rules |
| **ECS** | Clusters, services, task counts |
| **EKS** | Clusters, node groups |
| **RDS** | Instances, encryption, public access |
| **Lambda** | Functions, VPC config |
| **S3** | Buckets, encryption, public access |

</details>

<details>
<summary>Diagnostic Rules</summary>

| Check | Severity |
|-------|----------|
| Unhealthy targets | critical |
| SG missing port from ALB | critical |
| S3 public access open | critical |
| RDS publicly accessible | warning |
| Storage not encrypted | warning |
| Orphan target group | warning |
| Unused security group | info |

</details>

<details>
<summary>Architecture</summary>

```
sudiviz/
├── cli.py           # Typer commands
├── tui.py           # Textual TUI
├── web.py           # FastAPI + WebSocket
├── discovery/       # AWS discovery (boto3)
├── graph/           # NetworkX + analyzers
├── remediation/     # Auto-fix engine
└── web_templates/   # Cytoscape.js UI
```

</details>

---

## 📝 License

GPL-3.0-or-later — see [LICENSE](LICENSE)

---

<p align="center">
  <b>Built by <a href="https://github.com/pydevsg">@pydevsg</a></b>
</p>
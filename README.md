# 🔔 AlertNotes

**Operational memory for your production alerts.**

Your team fixes the same alert three times a year. Each time, a different engineer spends 45 minutes digging through Slack, logs, and Git history to figure out what caused it and what fixed it last time. AlertNotes ends that.

When an alert fires, AlertNotes automatically shows the previous resolution history in Slack before the engineer starts investigating. When an alert resolves, it prompts the on-call engineer for a 30-second note. That note lives forever, attached to that alert, surfaced automatically every time it fires again.

---

## How it works

1. Alert fires in Grafana
2. AlertNotes receives the webhook and checks for previous resolutions
3. Posts history to Slack immediately before you start investigating
4. Alert resolves — AlertNotes auto-pulls Slack messages and Git commits from the incident window
5. Sends a resolution prompt — engineer fills in cause and fix in 30 seconds
6. Saved forever. Next engineer is protected.

---

## Setup in 5 minutes

Prerequisites: Docker, Docker Compose

git clone https://github.com/SAMREEN111-hash/alertnotes
cd alertnotes
cp .env.example .env
docker-compose up -d

Open http://localhost:8000 and your dashboard is live.

Configure Grafana: Add a webhook contact point pointing to http://your-host:8000/webhook/alertmanager and set Send resolved to ON.

---

## Environment Variables

SLACK_BOT_TOKEN — Required. Slack bot token starting with xoxb-
SLACK_ALERTS_CHANNEL — Required. Channel where alerts are posted e.g. #incidents
SLACK_INCIDENT_CHANNEL — Optional. Channel to pull context messages from
GITHUB_TOKEN — Optional. GitHub PAT for commit context enrichment
GITHUB_REPO — Optional. Format org/repo for commit lookups
ALERTNOTES_BASE_URL — Optional. Public URL of this instance for Slack links

---

## CLI Usage

pip install -e .

alertnotes why "HighMemoryUsage"
alertnotes why "CPUThrottle" --service api-gateway
alertnotes list
alertnotes stats

---

## API Endpoints

GET  /api/alerts
GET  /api/alert/{fingerprint}/history
GET  /api/stats
POST /api/resolution/{id}/helpful?helpful=true
POST /webhook/alertmanager

---

## Stack

Python 3.12, FastAPI, SQLite, Docker, Slack API, GitHub API

---

## Why not just use PagerDuty notes or Opsgenie postmortems?

Those tools have three problems. First they are manual and buried — engineers have to remember to write a note and navigate to the incident page to do it. Second the context is not surfaced automatically when the same alert fires again. Third you cannot search by alert type to see every time a specific alert fired and what caused it each time.

AlertNotes makes documentation the path of least resistance and surfaces it exactly when it is needed.

---

## Running Tests

pip install -r requirements.txt
pytest tests/ -v

---

## License

MIT

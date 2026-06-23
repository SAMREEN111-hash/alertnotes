"""
AlertNotes - Context Enrichment
Automatically pulls Slack thread messages and Git commits
that happened around the time an alert fired.
This is what makes AlertNotes work even when engineers don't write notes.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_INCIDENT_CHANNEL = os.environ.get("SLACK_INCIDENT_CHANNEL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "org/repo"


async def enrich_context(alert_name: str, service: str,
                         fired_at: str, resolved_at: str) -> dict:
    """
    Pull automatic context from Slack and GitHub around the alert window.
    Returns a dict with slack_messages and commits.
    Called after an alert resolves, before prompting the engineer.
    """
    context = {
        "slack_messages": [],
        "commits": [],
        "pre_fire_commits": [],
    }

    fired_dt = datetime.fromisoformat(fired_at)
    resolved_dt = datetime.fromisoformat(resolved_at) if resolved_at else datetime.utcnow()

    # Pull Slack messages from incident window
    if SLACK_TOKEN and SLACK_INCIDENT_CHANNEL:
        context["slack_messages"] = await _get_slack_messages(
            fired_dt, resolved_dt
        )

    # Pull commits deployed 30 min before alert fired (likely culprits)
    if GITHUB_TOKEN and GITHUB_REPO:
        window_start = fired_dt - timedelta(minutes=30)
        context["pre_fire_commits"] = await _get_recent_commits(
            window_start, fired_dt, service
        )
        # Also pull commits during incident (hotfixes)
        context["commits"] = await _get_recent_commits(
            fired_dt, resolved_dt, service
        )

    return context


async def _get_slack_messages(fired_at: datetime, resolved_at: datetime) -> list:
    """Fetch messages from the incident Slack channel during the alert window."""
    if not SLACK_TOKEN:
        return []

    oldest = fired_at.timestamp()
    latest = resolved_at.timestamp()

    headers = {
        "Authorization": f"Bearer {SLACK_TOKEN}",
        "Content-Type": "application/json",
    }
    params = {
        "channel": SLACK_INCIDENT_CHANNEL,
        "oldest": str(oldest),
        "latest": str(latest),
        "limit": 20,
        "inclusive": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://slack.com/api/conversations.history",
                headers=headers,
                params=params,
            )
            data = resp.json()

        if not data.get("ok"):
            logger.warning("Slack API error: %s", data.get("error"))
            return []

        messages = []
        for msg in data.get("messages", []):
            if msg.get("subtype"):
                continue  # skip join/leave/bot messages
            text = msg.get("text", "").strip()
            if text and len(text) > 10:
                messages.append({
                    "text": text[:500],  # cap length
                    "user": msg.get("user", "unknown"),
                    "ts": msg.get("ts"),
                })
        return messages

    except Exception as e:
        logger.warning("Failed to fetch Slack context: %s", e)
        return []


async def _get_recent_commits(since: datetime, until: datetime, service: str) -> list:
    """Fetch commits from GitHub in the given time window."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return []

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {
        "since": since.isoformat() + "Z",
        "until": until.isoformat() + "Z",
        "per_page": 10,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/commits",
                headers=headers,
                params=params,
            )
            data = resp.json()

        if not isinstance(data, list):
            logger.warning("GitHub API error: %s", data.get("message", "unknown"))
            return []

        commits = []
        for commit in data:
            commits.append({
                "sha": commit["sha"][:8],
                "message": commit["commit"]["message"].split("\n")[0][:120],
                "author": commit["commit"]["author"]["name"],
                "url": commit["html_url"],
                "timestamp": commit["commit"]["author"]["date"],
            })
        return commits

    except Exception as e:
        logger.warning("Failed to fetch GitHub context: %s", e)
        return []


def summarize_context(context: dict) -> str:
    """
    Generate a human-readable summary of auto-pulled context.
    This gets pre-filled in the resolution form so engineers
    don't start from a blank slate.
    """
    parts = []

    pre_commits = context.get("pre_fire_commits", [])
    if pre_commits:
        parts.append("**Commits deployed 30min before alert fired:**")
        for c in pre_commits[:3]:
            parts.append(f"  • `{c['sha']}` {c['message']} — {c['author']}")

    slack = context.get("slack_messages", [])
    if slack:
        parts.append("\n**Slack activity during incident:**")
        for msg in slack[:5]:
            parts.append(f"  • {msg['text'][:100]}")

    fix_commits = context.get("commits", [])
    if fix_commits:
        parts.append("\n**Commits during incident (possible fixes):**")
        for c in fix_commits[:3]:
            parts.append(f"  • `{c['sha']}` {c['message']} — {c['author']}")

    return "\n".join(parts) if parts else ""

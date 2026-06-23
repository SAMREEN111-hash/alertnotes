"""
AlertNotes - Slack Integration
Posts resolution history when an alert fires, and sends
interactive resolution prompts when alerts resolve.
This is the core UX that engineers see every day.
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_ALERTS_CHANNEL = os.environ.get("SLACK_ALERTS_CHANNEL", "")
BASE_URL = os.environ.get("ALERTNOTES_BASE_URL", "http://localhost:8000")


async def post_history_on_fire(alert_name: str, service: str,
                               environment: str, severity: str,
                               resolutions: list, fingerprint: str):
    """
    Called when an alert fires. Posts the previous resolution history
    to Slack so the on-call engineer has context immediately.
    """
    if not SLACK_TOKEN or not SLACK_ALERTS_CHANNEL:
        logger.info("Slack not configured, skipping history post")
        return

    severity_emoji = {
        "critical": "🔴",
        "warning": "🟡",
        "info": "🔵",
    }.get(severity, "🔴")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{severity_emoji} Alert: {alert_name}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Service:*\n{service or 'unknown'}"},
                {"type": "mrkdwn", "text": f"*Environment:*\n{environment}"},
            ],
        },
    ]

    if resolutions:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📚 This alert has fired {len(resolutions)} time(s) before. Here's what fixed it:*",
            },
        })

        for i, res in enumerate(resolutions[:3], 1):
            fired_dt = datetime.fromisoformat(res["fired_at"])
            date_str = fired_dt.strftime("%b %d, %Y")
            duration_min = round((res.get("duration_secs") or 0) / 60)
            resolved_by = res.get("resolved_by") or "unknown"

            cause = res.get("cause") or "_No cause recorded_"
            fix = res.get("fix") or "_No fix recorded_"

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*#{i} — {date_str}* (resolved in {duration_min}m by {resolved_by})\n"
                        f"*Cause:* {cause[:200]}\n"
                        f"*Fix:* {fix[:200]}"
                    ),
                },
            })

        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Full History", "emoji": True},
                    "url": f"{BASE_URL}/alert/{fingerprint}",
                    "style": "primary",
                },
            ],
        })
    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "📭 *No previous resolutions recorded for this alert.*\nBe the first to document what fixes it.",
            },
        })

    await _post_message(blocks=blocks)


async def send_resolution_prompt(alert_name: str, service: str,
                                  fired_at: str, token: str,
                                  auto_context_summary: str):
    """
    Called when an alert resolves. Sends an interactive Slack message
    asking the engineer to document what caused it and what fixed it.
    """
    if not SLACK_TOKEN or not SLACK_ALERTS_CHANNEL:
        logger.info("Slack not configured, skipping resolution prompt")
        return

    fired_dt = datetime.fromisoformat(fired_at)
    date_str = fired_dt.strftime("%b %d at %H:%M UTC")

    resolve_url = f"{BASE_URL}/resolve/{token}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "✅ Alert Resolved — Help Future You",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{alert_name}* ({service or 'unknown service'}) resolved.\n"
                    f"Fired at {date_str}.\n\n"
                    "Taking 30 seconds to document this saves the next engineer 30 minutes. 👇"
                ),
            },
        },
    ]

    if auto_context_summary:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🤖 Auto-detected context:*\n{auto_context_summary[:600]}",
            },
        })

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📝 Document This Resolution", "emoji": True},
                "url": resolve_url,
                "style": "primary",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Skip", "emoji": True},
                "url": f"{resolve_url}?skip=true",
            },
        ],
    })

    await _post_message(blocks=blocks)


async def _post_message(text: str = "", blocks: Optional[list] = None):
    if not SLACK_TOKEN or not SLACK_ALERTS_CHANNEL:
        return

    payload = {
        "channel": SLACK_ALERTS_CHANNEL,
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {SLACK_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Slack post failed: %s", data.get("error"))
            return data.get("ts")  # thread timestamp
    except Exception as e:
        logger.error("Slack request failed: %s", e)
        return None

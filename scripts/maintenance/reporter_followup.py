#!/usr/bin/env python3
"""Remind and close only explicit, unanswered maintainer requests for information."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

STATE_MARKER = "<!-- autonomous-maintenance:state:v1"
AUTOMATED_UPDATE_MARKER = "<!-- hal-automated-update:v1 -->"
REMINDER_MARKER = "<!-- autonomous-maintenance:reporter-reminder:v1"
CLOSE_MARKER = "<!-- autonomous-maintenance:reporter-close:v1"
WAITING_LABEL = "status/awaiting-reporter"
TRUSTED_ACTORS = frozenset({"barneyonline", "github-actions[bot]"})
KNOWN_STATES = frozenset(
    {
        "needs-triage",
        "awaiting-reporter",
        "investigating",
        "awaiting-review",
        "awaiting-release",
        "awaiting-validation",
        "needs-owner",
        "released",
        "closed-incomplete",
    }
)


def timestamp(value: Any) -> datetime:
    """Parse an explicit timezone-aware API timestamp."""
    if not isinstance(value, str):
        raise ValueError("Timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include its timezone")
    return parsed.astimezone(UTC)


def marker_payload(body: str, marker: str) -> dict[str, Any]:
    """Read one JSON marker and reject ambiguity instead of using an older state."""
    if body.count(marker) != 1:
        raise ValueError("Expected exactly one state marker")
    remainder = body.split(marker, 1)[1]
    if "-->" not in remainder:
        raise ValueError("Unterminated state marker")
    payload = json.loads(remainder.split("-->", 1)[0].strip())
    if not isinstance(payload, dict):
        raise ValueError("State marker must contain an object")
    return payload


def followup_decision(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    now: datetime,
    trusted_actors: frozenset[str] = TRUSTED_ACTORS,
) -> dict[str, Any]:
    """Return a safe action without making a GitHub mutation."""
    if issue.get("state") != "open" or "pull_request" in issue:
        return {"action": "none", "reason": "Issue is not an open issue"}
    labels = {item["name"] for item in issue.get("labels", [])}
    if "automation/control" in labels:
        return {"action": "none", "reason": "Internal control records are excluded"}
    if WAITING_LABEL not in labels:
        return {"action": "none", "reason": "Issue is not awaiting its reporter"}

    states = [
        comment
        for comment in comments
        if comment.get("user", {}).get("login", "").lower() in trusted_actors
        and STATE_MARKER in comment.get("body", "")
    ]
    if not states:
        return {"action": "none", "reason": "No explicit trusted request marker"}
    try:
        latest = max(
            states,
            key=lambda comment: (timestamp(comment["created_at"]), comment["id"]),
        )
        state = marker_payload(latest["body"], STATE_MARKER)
        if (
            type(state.get("issue_number")) is not int
            or state["issue_number"] != issue["number"]
        ):
            raise ValueError("Marker issue number does not match")
        if state.get("status") not in KNOWN_STATES:
            raise ValueError("Unknown maintenance state")
        if state["status"] != "awaiting-reporter":
            return {
                "action": "none",
                "reason": "Current state is not awaiting the reporter",
            }
        requested_at = timestamp(state["requested_at"])
        created_at = timestamp(latest["created_at"])
        if requested_at > now or created_at > now:
            raise ValueError("Request is in the future")
        started_at = max(requested_at, created_at)
        request_id = latest["id"]
        if type(request_id) is not int or request_id <= 0:
            raise ValueError("Request comment identifier is invalid")
        body_fingerprint = state.get("issue_body_sha256")
        if not isinstance(body_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", body_fingerprint
        ):
            raise ValueError("Request must include the issue body SHA-256 baseline")
        current_body = issue.get("body")
        if current_body is None:
            current_body = ""
        if not isinstance(current_body, str):
            raise ValueError("Issue body must be text")
        if hashlib.sha256(current_body.encode("utf-8")).hexdigest() != body_fingerprint:
            return {
                "action": "reporter-replied",
                "reason": "Issue body changed after the request",
            }

        reporter = issue["user"]["login"].lower()
        if any(
            comment.get("user", {}).get("login", "").lower() == reporter
            and not (
                comment.get("user", {}).get("login", "").lower() in trusted_actors
                and (
                    comment.get("body", "").startswith(AUTOMATED_UPDATE_MARKER)
                    or STATE_MARKER in comment.get("body", "")
                )
            )
            and max(timestamp(comment["created_at"]), timestamp(comment["updated_at"]))
            > started_at
            for comment in comments
        ):
            return {
                "action": "reporter-replied",
                "reason": "Reporter responded after the request",
            }
        reminded = False
        close_comment_id = None
        for comment in comments:
            if comment.get("user", {}).get("login", "").lower() not in trusted_actors:
                continue
            if REMINDER_MARKER in comment.get("body", ""):
                reminder = marker_payload(comment["body"], REMINDER_MARKER)
                if reminder.get("request_comment_id") == request_id:
                    reminded = True
            if CLOSE_MARKER in comment.get("body", ""):
                closure = marker_payload(comment["body"], CLOSE_MARKER)
                if closure.get("request_comment_id") == request_id:
                    close_comment_id = comment["id"]
        deadline = started_at + timedelta(days=21)
        result: dict[str, Any] = {
            "request_comment_id": request_id,
            "deadline": deadline.isoformat(),
        }
        if now >= deadline:
            return {
                **result,
                "action": "close",
                "reason": "Reporter has not responded for 21 days",
                "close_comment_id": close_comment_id,
            }
        if now >= started_at + timedelta(days=7) and not reminded:
            return {
                **result,
                "action": "remind",
                "reason": "Reporter has not responded for seven days",
            }
        return {
            **result,
            "action": "none",
            "reason": "Request is within its response period",
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {"action": "blocked", "reason": f"Invalid follow-up evidence: {error}"}


class NoRedirect(HTTPRedirectHandler):
    """Keep credentials within the explicitly selected API origin."""

    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        raise ValueError("GitHub API redirect requires maintainer review")


class GitHub:
    """Small REST client whose token is sent only to the fixed GitHub API host."""

    def __init__(self, repository: str, token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected owner/repository")
        self.prefix = f"/repos/{repository}"
        self.token = token

    def request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> Any:
        request = Request(
            f"https://api.github.com{self.prefix}{path}",
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else None

    def list(self, path: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 1001):
            items = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(items, list):
                raise ValueError("Expected a paginated list")
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("Pagination limit reached; no follow-up action taken")

    def remove_label(self, number: int, label: str) -> None:
        try:
            self.request("DELETE", f"/issues/{number}/labels/{quote(label, safe='')}")
        except HTTPError as error:
            if error.code != 404:
                raise


def apply_followup(
    client: GitHub, issue: dict[str, Any], decision: dict[str, Any]
) -> None:
    """Apply only actions computed from a complete, current issue snapshot."""
    number = issue["number"]
    action = decision["action"]
    if action == "reporter-replied":
        client.remove_label(number, WAITING_LABEL)
        client.remove_label(number, "status/stale")
    elif action == "remind":
        marker = json.dumps({"request_comment_id": decision["request_comment_id"]})
        body = (
            f"{AUTOMATED_UPDATE_MARKER}\nAutomated maintenance update\n\n"
            "Please provide the information requested above. "
            f"If there is no response by {decision['deadline'][:10]} (UTC), this issue will close "
            "as incomplete. Reopen it, or reply here with the missing information so HAL can reopen it.\n\n"
            f"{REMINDER_MARKER}\n{marker}\n-->"
        )
        client.request("POST", f"/issues/{number}/comments", {"body": body})
    elif action == "close":
        marker = json.dumps({"request_comment_id": decision["request_comment_id"]})
        body = (
            f"{AUTOMATED_UPDATE_MARKER}\nAutomated maintenance update\n\n"
            "Closing as incomplete after three weeks without a response to the information "
            "request above. Please reopen it, or reply here with the missing information so HAL "
            "can reopen it and continue investigating. This closure does not indicate that a fix has been released.\n\n"
            f"{CLOSE_MARKER}\n{marker}\n-->"
        )
        if not decision.get("close_comment_id"):
            client.request("POST", f"/issues/{number}/comments", {"body": body})
        labels = [
            label["name"]
            for label in issue.get("labels", [])
            if not label["name"].startswith("status/")
        ]
        labels.append("status/closed-incomplete")
        client.request(
            "PATCH",
            f"/issues/{number}",
            {"state": "closed", "state_reason": "not_planned", "labels": labels},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply actions; default is a read-only plan",
    )
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not args.repository or not token:
        parser.error("Repository and GH_TOKEN or GITHUB_TOKEN are required")
    client = GitHub(args.repository, token)
    now = datetime.now(UTC)
    issues = client.list(f"/issues?state=open&labels={quote(WAITING_LABEL, safe='')}")
    blocked = False
    for issue in issues:
        if "pull_request" in issue:
            continue
        comments = client.list(f"/issues/{issue['number']}/comments")
        decision = followup_decision(issue, comments, now)
        print(json.dumps({"issue_number": issue["number"], **decision}, sort_keys=True))
        if decision["action"] == "blocked":
            blocked = True
        elif args.apply:
            # Re-read immediately before a mutation so a new reply/state cancels closure.
            current = client.request("GET", f"/issues/{issue['number']}")
            comments = client.list(f"/issues/{issue['number']}/comments")
            verified = followup_decision(current, comments, datetime.now(UTC))
            if verified == decision:
                apply_followup(client, current, verified)
    if blocked:
        raise SystemExit(
            "Malformed waiting-state evidence requires maintainer attention"
        )


if __name__ == "__main__":
    main()

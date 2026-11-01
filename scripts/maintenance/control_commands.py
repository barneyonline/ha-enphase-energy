#!/usr/bin/env python3
"""Validate owner-only control-issue requests into fixed workflow dispatch plans."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

MARKER = "hal-control-request:v1"
OPERATIONS = {"candidate": "release-candidate.yml", "gate": "maintenance-gate.yml"}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def plan_dispatch(
    event: dict[str, Any], policy: dict[str, Any], repository: str
) -> dict[str, Any]:
    """Return a fixed allowlisted operation, or ignore events outside the control issue."""
    required = {
        "schema",
        "repository",
        "default_branch",
        "control_issue_number",
        "control_label",
        "actor",
        "enabled_operations",
    }
    if (
        not isinstance(event, dict)
        or not isinstance(policy, dict)
        or set(policy) != required
        or type(policy.get("schema")) is not int
        or policy["schema"] != 1
        or policy.get("repository") != repository
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
    ):
        raise ValueError("Invalid trusted control policy scope")
    if (
        policy["default_branch"] != "main"
        or policy["control_label"] != "automation/control"
        or type(policy["control_issue_number"]) is not int
        or policy["control_issue_number"] <= 0
    ):
        raise ValueError("Control policy requires a fixed issue and default branch")
    actor = policy["actor"]
    if (
        not isinstance(actor, dict)
        or set(actor) != {"id", "login"}
        or type(actor["id"]) is not int
        or actor["id"] <= 0
        or not isinstance(actor["login"], str)
        or not actor["login"]
    ):
        raise ValueError(
            "Control actor must have a verified numeric identity and login"
        )
    operations = policy["enabled_operations"]
    if (
        not isinstance(operations, list)
        or not operations
        or any(
            not isinstance(operation, str) or operation not in OPERATIONS
            for operation in operations
        )
        or len(operations) != len(set(operations))
    ):
        raise ValueError(
            "Only explicitly enabled candidate and gate operations are supported"
        )
    issue = event.get("issue", {})
    comment = event.get("comment", {})
    if (
        event.get("action") != "created"
        or event.get("repository", {}).get("full_name") != repository
        or issue.get("number") != policy["control_issue_number"]
        or issue.get("state") not in ("open", "closed")
        or "pull_request" in issue
    ):
        return {"action": "ignore", "reason": "Outside the configured control issue"}
    if policy["control_label"] not in {
        label.get("name") for label in issue.get("labels", [])
    }:
        raise ValueError("Control issue label is missing")
    sender = comment.get("user", {})
    if sender.get("id") != actor["id"] or sender.get("login") != actor["login"]:
        return {
            "action": "ignore",
            "reason": "Comment author is not the configured control actor",
        }
    initiator = event.get("sender", {})
    if initiator.get("id") != actor["id"] or initiator.get("login") != actor["login"]:
        return {
            "action": "ignore",
            "reason": "Event initiator is not the control actor",
        }
    body = comment.get("body")
    if not isinstance(body, str):
        raise ValueError("Control request body must be text")
    if not body.lstrip().startswith("<!-- hal-control-request:"):
        return {"action": "ignore", "reason": "Ordinary control-issue note"}
    created = comment.get("created_at")
    if (
        not isinstance(created, str)
        or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", created
        )
        or comment.get("updated_at") != created
        or comment.get("issue_url")
        != f"https://api.github.com/repos/{repository}/issues/{policy['control_issue_number']}"
    ):
        raise ValueError(
            "Control command requires an unedited creation in the configured issue"
        )
    datetime.fromisoformat(created.replace("Z", "+00:00"))
    if len(body) > 4096:
        raise ValueError("Control request must be a bounded standalone marker")
    match = re.fullmatch(
        r"\s*<!-- hal-control-request:v1\s+(\{.*\})\s*-->\s*", body, flags=re.DOTALL
    )
    if not match:
        raise ValueError("Control request must be a standalone versioned JSON marker")
    command = json.loads(match.group(1), object_pairs_hook=unique_object)
    if (
        not isinstance(command, dict)
        or type(command.get("schema")) is not int
        or command["schema"] != 1
    ):
        raise ValueError("Invalid control request schema")
    operation = command.get("operation")
    if operation not in operations:
        raise ValueError(
            "Requested operation is disabled; publication is not supported"
        )
    request_id = comment.get("id")
    if type(request_id) is not int or request_id <= 0:
        raise ValueError("Invalid request comment identifier")
    inputs = {"control_request_id": str(request_id)}
    if operation == "candidate":
        if (
            set(command) != {"schema", "operation", "candidate_sha", "version"}
            or not isinstance(command.get("candidate_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", command["candidate_sha"])
            or not isinstance(command.get("version"), str)
            or not re.fullmatch(
                r"v?[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*", command["version"]
            )
        ):
            raise ValueError(
                "Candidate operation requires only an exact SHA and safe manifest version"
            )
        inputs.update(
            candidate_sha=command["candidate_sha"], version=command["version"]
        )
    else:
        if (
            set(command) != {"schema", "operation", "pull_number"}
            or type(command.get("pull_number")) is not int
            or not 0 < command["pull_number"] < 2**31
        ):
            raise ValueError(
                "Gate operation requires only a positive pull request number"
            )
        inputs["pull_number"] = str(command["pull_number"])
    return {
        "action": "dispatch",
        "schema": 1,
        "operation": operation,
        "workflow": OPERATIONS[operation],
        "ref": policy["default_branch"],
        "inputs": inputs,
        "request_comment_id": request_id,
        "control_issue_number": policy["control_issue_number"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH")
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args()
    try:
        if not args.event or not args.repository:
            raise ValueError("Event file and repository are required")
        event = json.loads(args.event.read_text(), object_pairs_hook=unique_object)
        policy = json.loads(args.policy.read_text(), object_pairs_hook=unique_object)
        result = plan_dispatch(event, policy, args.repository)
    except (OSError, TypeError, ValueError, KeyError) as error:
        print(
            json.dumps(
                {
                    "action": "blocked",
                    "error_type": type(error).__name__,
                    "reason": "Invalid or unauthorized control request/policy; inspect the trusted control issue.",
                }
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

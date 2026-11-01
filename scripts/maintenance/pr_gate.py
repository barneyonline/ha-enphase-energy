"""Read-only, fail-closed PR gate. Live policy is fetched from the PR base commit.

No PR text is executed. Offline snapshots are for rehearsal, never merge authority.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

POLICY_PATH = ".github/maintenance/merge-policy.json"
LEDGER_MARKER = "hal-feedback-ledger:v1"
INDEPENDENT_MARKER = "hal-independent-review:v1"
CLEAN_MARKER = "hal-clean-verdict:v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
DISPOSITIONS = {"addressed", "not-applicable", "duplicate", "false-positive"}
COLLECTIONS = {
    "issue_comments",
    "reviews",
    "review_meta",
    "review_comments",
    "threads",
    "checks",
    "check_suites",
    "statuses",
    "workflows",
}
COMPLETE_KEYS = COLLECTIONS | {"comparison"}
OPERATIONAL_KEY = (15368, ".github/workflows/maintenance-gate.yml", "maintenance-gate")
LEDGER_KEYS = {
    "schema",
    "repository",
    "pull_number",
    "head_sha",
    "base_sha",
    "policy_sha256",
    "inventory_digest",
    "entries",
}
ENTRY_KEYS = {
    "source",
    "updated_at",
    "content_sha256",
    "reviewed_head_sha",
    "disposition",
    "reason",
    "evidence_urls",
}
CLEAN_KEYS = {
    "schema",
    "repository",
    "pull_number",
    "head_sha",
    "base_sha",
    "policy_sha256",
    "feedback_digest",
    "dispositions_digest",
    "verdict",
    "reason",
    "evidence_urls",
}


class GateError(ValueError):
    """Invalid or incomplete evidence, always a blocking result."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise GateError(f"invalid {label}")
    return value


def integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise GateError(f"invalid {label}")
    return value


def timestamp(value: Any, label: str) -> str:
    value = text(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
    except ValueError as exc:
        raise GateError(f"invalid {label}") from exc
    return value


def marker(body: str, name: str) -> dict[str, Any] | None:
    """Only a standalone exact marker is machine-readable, never a fenced quote."""
    match = re.fullmatch(
        r"\s*<!-- " + re.escape(name) + r"\s+(\{[\s\S]*\})\s*-->\s*", body
    )
    if not match:
        return None
    try:
        result = json.loads(match.group(1), object_pairs_hook=unique_object)
    except (ValueError, TypeError) as exc:
        raise GateError(f"malformed {name} JSON") from exc
    return result


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError("duplicate JSON key")
        result[key] = value
    return result


def actor_key(actor: dict[str, Any]) -> tuple[int, str]:
    return integer(actor.get("id"), "actor id"), text(actor.get("login"), "actor login")


def workflow_path(value: Any) -> str | None:
    """Canonical identity; raw ref-qualified path stays in source provenance."""
    if value is None:
        return None
    raw = text(value, "workflow path")
    path, separator, ref = raw.partition("@")
    if separator and (not ref or any(c.isspace() for c in ref)):
        raise GateError("invalid workflow ref")
    if path == "dynamic/github-code-scanning/codeql":
        return path
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path):
        raise GateError("invalid workflow_path")
    return path


def check_key(check: dict[str, Any]) -> tuple[int, str | None, str]:
    app = check.get("app") or {}
    workflow = check.get("_workflow")
    path = workflow_path(workflow.get("path")) if isinstance(workflow, dict) else None
    return (
        integer(app.get("id"), "check app id"),
        path,
        text(check.get("name"), "check name"),
    )


def policy_check_key(check: dict[str, Any]) -> tuple[int, str | None, str]:
    path = workflow_path(check.get("workflow_path"))
    return (
        integer(check.get("app_id"), "policy app id"),
        path,
        text(check.get("name"), "policy check name"),
    )


def operational_keys(policy: dict[str, Any]) -> set[tuple[int, str | None, str]]:
    return {
        policy_check_key(policy["gate_check"]),
        policy_check_key(policy["operational_check"]),
    }


def operational_run(
    run: dict[str, Any], checks: list[dict[str, Any]], policy: dict[str, Any]
) -> bool:
    """Exclude only one exact gate job/check per observed run attempt."""
    jobs = run.get("_jobs")
    path = workflow_path(run["path"])
    by_id = {str(check["id"]): check for check in checks}
    for key in operational_keys(policy):
        if path != key[1] or not jobs:
            continue
        if all(
            job.get("name") == key[2]
            and job.get("check_run_url", "").rsplit("/", 1)[-1] in by_id
            and check_key(by_id[job["check_run_url"].rsplit("/", 1)[-1]]) == key
            and by_id[job["check_run_url"].rsplit("/", 1)[-1]]["_workflow"]["id"]
            == run["id"]
            and by_id[job["check_run_url"].rsplit("/", 1)[-1]]["_workflow"][
                "run_attempt"
            ]
            == integer(job.get("run_attempt"), "operational job attempt")
            for job in jobs
        ):
            associated = [
                check
                for check in checks
                if check_key(check) == key
                and (check.get("_workflow") or {}).get("id") == run["id"]
            ]
            if (
                len({job["run_attempt"] for job in jobs}) == len(jobs)
                and len(associated) == len(jobs)
                and {str(check["id"]) for check in associated}
                == {job["check_run_url"].rsplit("/", 1)[-1] for job in jobs}
            ):
                return True
    return False


def operational_check_ids(
    checks: list[dict[str, Any]],
    workflows: list[dict[str, Any]],
    policy: dict[str, Any],
) -> set[int]:
    own = {
        check["id"]
        for check in checks
        if check_key(check) == policy_check_key(policy["gate_check"])
    }
    for run in workflows:
        if operational_run(run, checks, policy):
            own.update(
                int(job["check_run_url"].rsplit("/", 1)[-1]) for job in run["_jobs"]
            )
    return own


def decision_view(snapshot: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Self-only checks/suites/runs carry no independent merge evidence."""
    view = json.loads(json.dumps(snapshot))
    own = operational_check_ids(view["checks"], view["workflows"], policy)
    by_suite: dict[int, list[dict[str, Any]]] = {}
    for check in view["checks"]:
        by_suite.setdefault(check["check_suite"]["id"], []).append(check)
    own_suites = {
        ident
        for ident, checks in by_suite.items()
        if checks and all(check["id"] in own for check in checks)
    }
    view["workflows"] = [
        run
        for run in view["workflows"]
        if not operational_run(run, view["checks"], policy)
    ]
    view["checks"] = [check for check in view["checks"] if check["id"] not in own]
    view["check_suites"] = [
        suite for suite in view["check_suites"] if suite["id"] not in own_suites
    ]
    return view


def ledger_shape(value: dict[str, Any]) -> bool:
    """Exclusion requires exact, known, standalone machine-only ledger data."""
    if (
        set(value) != LEDGER_KEYS
        or value.get("schema") != 1
        or not isinstance(value.get("repository"), str)
        or not REPO.fullmatch(value["repository"])
        or type(value.get("pull_number")) is not int
        or value["pull_number"] < 1
    ):
        return False
    for field, size in (
        ("head_sha", 40),
        ("base_sha", 40),
        ("policy_sha256", 64),
        ("inventory_digest", 64),
    ):
        if not isinstance(value.get(field), str) or not re.fullmatch(
            r"[0-9a-f]{" + str(size) + "}", value[field]
        ):
            return False
    entries = value.get("entries")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) - (ENTRY_KEYS | {"independent_evidence_source"})
            or not ENTRY_KEYS.issubset(entry)
            or entry.get("disposition") not in DISPOSITIONS
        ):
            return False
        if not all(
            isinstance(entry.get(k), str) for k in ENTRY_KEYS - {"evidence_urls"}
        ):
            return False
        if not isinstance(entry["evidence_urls"], list) or not all(
            isinstance(u, str) for u in entry["evidence_urls"]
        ):
            return False
    return True


def validate_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy, dict) or policy.get("schema") != 1:
        raise GateError("unsupported policy schema")
    supported = {
        "schema",
        "repository",
        "base_branch",
        "trusted_ledger_authors",
        "codex_reviewers",
        "independent_reviewers",
        "gate_check",
        "operational_check",
        "checks",
        "statuses",
    }
    if set(policy) != supported:
        raise GateError("missing or unsupported policy fields")
    if not REPO.fullmatch(text(policy.get("repository"), "policy repository")):
        raise GateError("invalid policy repository")
    text(policy.get("base_branch"), "base_branch")
    for field in ("trusted_ledger_authors", "codex_reviewers", "independent_reviewers"):
        values = policy.get(field)
        if not isinstance(values, list) or not values:
            raise GateError(f"missing {field}")
        keys = [actor_key(a) for a in values]
        if len(set(keys)) != len(keys):
            raise GateError(f"duplicate {field}")
    for field in ("checks", "statuses"):
        if not isinstance(policy.get(field), list):
            raise GateError(f"missing {field} policy")
    gate = policy_check_key(policy.get("gate_check", {}))
    operational = policy.get("operational_check", {})
    if (
        set(operational) != {"app_id", "workflow_path", "name"}
        or policy_check_key(operational) != OPERATIONAL_KEY
        or gate == OPERATIONAL_KEY
    ):
        raise GateError(
            "operational_check must be the exact native maintenance gate identity"
        )
    checks = [policy_check_key(c) for c in policy["checks"]]
    if gate in checks or OPERATIONAL_KEY in checks or len(set(checks)) != len(checks):
        raise GateError("duplicate check or gate in checks policy")
    if not any(c.get("required") is True for c in policy["checks"]):
        raise GateError("at least one required check must be configured")
    statuses = []
    for item in policy["statuses"]:
        statuses.append(text(item.get("context"), "status context"))
        integer(item.get("creator_id"), "status creator id")
        if type(item.get("required")) is not bool:
            raise GateError("status required must be boolean")
    if len(set(statuses)) != len(statuses):
        raise GateError("duplicate status context policy")
    for item in policy["checks"]:
        if type(item.get("required")) is not bool:
            raise GateError("check required must be boolean")


def source(
    kind: str, item: dict[str, Any], updated: str, *, body: str | None = None
) -> dict[str, Any]:
    ident = integer(item.get("id"), f"{kind} id")
    url = text(item.get("html_url") or item.get("url"), f"{kind} URL")
    actor = item.get("user") or item.get("creator")
    if actor is not None:
        actor_key(actor)
    fingerprint = dict(item)
    return {
        "source": f"{kind}:{ident}",
        "updated_at": timestamp(updated, f"{kind} updated_at"),
        "content_sha256": digest(fingerprint),
        "url": url,
        "actor": actor,
        "body": text(body, f"{kind} body", empty=True) if body is not None else None,
        "kind": kind,
    }


def inventory(
    snapshot: dict[str, Any], policy: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Validate raw sources and return feedback, selected ledger, latest checks."""
    pr = snapshot["pr"]
    head = pr["head"]["sha"]
    trusted = {actor_key(a) for a in policy["trusted_ledger_authors"]}
    ledgers = []
    feedback = []
    for item in snapshot["issue_comments"]:
        body = text(item.get("body"), "issue comment body", empty=True)
        who = actor_key(item.get("user", {}))
        is_trusted = who in trusted
        if is_trusted and LEDGER_MARKER in body:
            parsed = marker(body, LEDGER_MARKER)
            if parsed is not None and ledger_shape(parsed):
                ledgers.append((integer(item.get("id"), "ledger id"), parsed, item))
                continue
            # A prefix, appended prose or unknown JSON schema is feedback, never
            # silently hidden as an old ledger. Malformed JSON itself fails closed.
        # Only an exact review invocation by the PR author or a trusted orchestrator.
        if (
            who == actor_key(pr["user"]) or is_trusted
        ) and body.strip() == "@codex review":
            continue
        feedback.append(
            source("issue_comment", item, item.get("updated_at"), body=body)
        )
    ledger: dict[str, Any] = {}
    ledger_comment: dict[str, Any] = {}
    if ledgers:
        _, ledger, ledger_comment = max(ledgers, key=lambda x: x[0])
    meta = {
        integer(m.get("databaseId"), "review database id"): m
        for m in snapshot["review_meta"]
    }
    if len(meta) != len(snapshot["review_meta"]):
        raise GateError("duplicate review metadata")
    review_ids = set()
    for item in snapshot["reviews"]:
        ident = integer(item.get("id"), "review id")
        review_ids.add(ident)
        if item.get("state") not in {
            "COMMENTED",
            "APPROVED",
            "CHANGES_REQUESTED",
            "DISMISSED",
            "PENDING",
        }:
            raise GateError("unknown formal review state")
        if not isinstance(item.get("commit_id"), str) or not SHA.fullmatch(
            item["commit_id"]
        ):
            raise GateError("review commit SHA missing")
        if item["state"] != "PENDING":
            timestamp(item.get("submitted_at"), "review submitted_at")
        m = meta.get(ident)
        if not m or any(
            (m.get(key) != value)
            for key, value in (
                ("body", item.get("body")),
                ("state", item.get("state")),
                ("commit", {"oid": item.get("commit_id")}),
            )
        ):
            raise GateError("review metadata missing or changed during collection")
        feedback.append(
            source("review", item, m.get("updatedAt"), body=item.get("body"))
        )
    if set(meta) != review_ids:
        raise GateError("review metadata set mismatch")
    inline_ids = set()
    for item in snapshot["review_comments"]:
        inline_ids.add(integer(item.get("id"), "inline comment id"))
        feedback.append(
            source(
                "inline_comment", item, item.get("updated_at"), body=item.get("body")
            )
        )
    thread_ids = set()
    for thread in snapshot["threads"]:
        text(thread.get("id"), "thread id")
        if (
            type(thread.get("isResolved")) is not bool
            or type(thread.get("isOutdated")) is not bool
        ):
            raise GateError("malformed review thread state")
        comments = thread.get("comments")
        if not isinstance(comments, list) or not comments:
            raise GateError("empty or incomplete review thread")
        for comment in comments:
            ident = integer(comment.get("databaseId"), "thread comment id")
            if ident in thread_ids:
                raise GateError("duplicate thread comment")
            thread_ids.add(ident)
            original = next(
                (c for c in snapshot["review_comments"] if c["id"] == ident), None
            )
            if (
                not original
                or comment.get("body") != original.get("body")
                or comment.get("updatedAt") != original.get("updated_at")
            ):
                raise GateError("thread comment missing or changed during collection")
    if inline_ids != thread_ids:
        raise GateError("review-thread inventory incomplete")

    own = operational_check_ids(snapshot["checks"], snapshot["workflows"], policy)
    suites = {integer(s.get("id"), "check suite id") for s in snapshot["check_suites"]}
    if len(suites) != len(snapshot["check_suites"]) or len(suites) >= 1000:
        raise GateError("check-suite inventory duplicate or at API completeness limit")
    if suites - {
        integer(check.get("check_suite", {}).get("id"), "check run suite id")
        for check in snapshot["checks"]
    }:
        raise GateError(
            "check suite has no observed check; operational identity cannot be proved"
        )
    groups: dict[tuple[int, str | None, str], list[dict[str, Any]]] = {}
    for item in snapshot["checks"]:
        if item.get("head_sha") != head:
            raise GateError("check belongs to a different SHA")
        key = check_key(item)
        if item["id"] in own:
            continue
        if (
            integer(item.get("check_suite", {}).get("id"), "check run suite id")
            not in suites
        ):
            raise GateError("check run suite missing from complete inventory")
        annotations = item.get("_annotations")
        output = item.get("output")
        if (
            not isinstance(annotations, list)
            or not isinstance(output, dict)
            or type(output.get("annotations_count")) is not int
            or output["annotations_count"] != len(annotations)
        ):
            raise GateError("check annotations/output incomplete")
        groups.setdefault(key, []).append(item)
        # Inventory ALL attempts, including superseded failures and annotations.
        feedback.append(
            source(
                "check",
                item,
                item.get("completed_at")
                or item.get("started_at")
                or item.get("_created_at"),
            )
        )
    latest = []
    for values in groups.values():

        def order(item: dict[str, Any]) -> tuple[int, int, int]:
            workflow = item.get("_workflow")
            if workflow:
                return (
                    integer(workflow.get("run_number"), "workflow run_number"),
                    integer(workflow.get("run_attempt"), "job run_attempt"),
                    integer(item["id"], "check id"),
                )
            return (0, 0, integer(item["id"], "check id"))

        latest.append(max(values, key=order))
    for item in snapshot["statuses"]:
        feedback.append(source("status", item, item.get("updated_at")))
    ids = [f["source"] for f in feedback]
    if len(set(ids)) != len(ids):
        raise GateError("duplicate feedback source ids")
    ledger = dict(ledger)
    if ledgers:
        ledger["_comment"] = ledger_comment
    return sorted(feedback, key=lambda f: f["source"]), ledger, latest


def inventory_digest(feedback: list[dict[str, Any]]) -> str:
    return digest(
        [
            {k: f[k] for k in ("source", "updated_at", "content_sha256")}
            for f in feedback
        ]
    )


def evidence_urls(value: Any, repository: str) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for url in value:
        if not isinstance(url, str):
            return False
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or not parsed.path.startswith(f"/{repository}/")
        ):
            return False
        if not re.match(
            r"/(?:[^/]+/){2}(?:pull|issues|commit|actions|blob|compare)/", parsed.path
        ):
            return False
    return True


def handling_proof(
    feedback: list[dict[str, Any]],
    entries: dict[str, dict[str, Any]],
    snapshot: dict[str, Any],
    policy: dict[str, Any],
    ledger_actor: tuple[int, str],
) -> dict[str, Any]:
    """Independent clean handling, independent of an author's disposition words."""
    author = actor_key(snapshot["pr"]["user"])
    excluded_ids = {author[0], ledger_actor[0]}
    trusted = {
        actor_key(a)
        for a in policy["independent_reviewers"]
        if actor_key(a)[0] not in excluded_ids
    }
    pr = snapshot["pr"]
    scope = {
        "schema": 1,
        "repository": policy["repository"],
        "pull_number": pr["number"],
        "head_sha": pr["head"]["sha"],
        "base_sha": pr["base"]["sha"],
        "policy_sha256": digest(policy),
        "verdict": "clean",
    }
    proofs: dict[str, Any] = {
        # Prospective new witness: there is no source to exclude yet.
        "feedback_digest": inventory_digest(feedback),
        "dispositions_digest": digest(
            [entries[item["source"]] for item in feedback if item["source"] in entries]
        ),
        "candidates": [],
        "accepted": None,
    }
    formal = {f"review:{review['id']}": review for review in snapshot["reviews"]}
    for item in feedback:
        if not item["actor"] or actor_key(item["actor"]) not in trusted:
            continue
        review = formal.get(item["source"])
        if review is not None and (
            review["state"] not in {"APPROVED", "COMMENTED"}
            or review["commit_id"] != pr["head"]["sha"]
        ):
            continue
        verdict = marker(item["body"] or "", CLEAN_MARKER)
        if (
            verdict is None
            or set(verdict) != CLEAN_KEYS
            or type(verdict.get("schema")) is not int
            or type(verdict.get("pull_number")) is not int
            or not all(verdict.get(k) == v for k, v in scope.items())
            or not all(
                isinstance(verdict.get(k), str)
                and re.fullmatch(r"[0-9a-f]{64}", verdict[k])
                for k in ("feedback_digest", "dispositions_digest")
            )
            or not isinstance(verdict.get("reason"), str)
            or not verdict["reason"].strip()
            or not evidence_urls(verdict.get("evidence_urls"), policy["repository"])
        ):
            continue
        # Exclude this candidate alone. Every earlier/later/invalid/adverse
        # verdict remains ordinary feedback and must be bound by this proof.
        targets = [other for other in feedback if other["source"] != item["source"]]
        expected = {
            "feedback_digest": inventory_digest(targets),
            "dispositions_digest": digest(
                [
                    entries[other["source"]]
                    for other in targets
                    if other["source"] in entries
                ]
            ),
        }
        proofs["candidates"].append({"source": item["source"], **expected})
        if all(verdict[k] == value for k, value in expected.items()):
            proofs["accepted"] = {
                "source": item["source"],
                "kind": "structured-clean-verdict",
                "url": item["url"],
            }
            return proofs
    return proofs


def pr_state(pr: dict[str, Any]) -> dict[str, Any]:
    """All metadata that can invalidate a decision, including final read state."""
    return {
        k: pr.get(k)
        for k in (
            "number",
            "state",
            "draft",
            "merged",
            "mergeable",
            "mergeable_state",
            "merge_commit_sha",
            "head",
            "base",
            "user",
        )
    }


def evaluate(snapshot: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Pure deterministic evaluator: no I/O, mutation, source execution or secrets."""
    result: dict[str, Any] = {
        "schema": 1,
        "allow": False,
        "reasons": [],
        "head_sha": None,
        "base_sha": None,
        "feedback": [],
        "validation_claimed": False,
        "merge_authority": False,
    }
    reasons = result["reasons"]

    def block(code: str, **context: Any) -> None:
        reasons.append({"code": code, **context})

    try:
        validate_policy(policy)
        if not isinstance(snapshot, dict) or snapshot.get("schema") != 1:
            raise GateError("unsupported snapshot schema")
        if snapshot.get("errors"):
            raise GateError("snapshot has collection errors")
        if (
            snapshot.get("complete") != {name: True for name in COMPLETE_KEYS}
            or snapshot.get("stable") is not True
        ):
            raise GateError("snapshot incomplete or unstable")
        for name in COLLECTIONS:
            if not isinstance(snapshot.get(name), list):
                raise GateError(f"missing collection {name}")
        pr = snapshot["pr"]
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        if (
            not isinstance(head, str)
            or not SHA.fullmatch(head)
            or not isinstance(base, str)
            or not SHA.fullmatch(base)
        ):
            raise GateError("invalid PR head/base SHA")
        result.update(
            head_sha=head,
            base_sha=base,
            repository=snapshot.get("repository"),
            pull_number=pr.get("number"),
            policy_sha256=digest(policy),
        )
        if (
            snapshot.get("repository") != policy["repository"]
            or pr["base"].get("ref") != policy["base_branch"]
        ):
            block("repository_or_base_mismatch")
        if (
            pr.get("state") != "open"
            or pr.get("draft") is not False
            or pr.get("merged") is not False
        ):
            block("pr_not_open_ready")
        if pr.get("mergeable") is not True:
            block("mergeability_unknown_or_conflicted")
        comparison = snapshot.get("comparison")
        if (
            not isinstance(comparison, dict)
            or comparison.get("_requested_base_sha") != base
            or comparison.get("_requested_head_sha") != head
            or comparison.get("base_commit", {}).get("sha") != base
            or comparison.get("merge_base_commit", {}).get("sha") != base
            or type(comparison.get("behind_by")) is not int
            or comparison["behind_by"] != 0
            or comparison.get("status") not in {"ahead", "identical"}
        ):
            block("current_base_ancestry_unproven")
        feedback, ledger, latest_checks = inventory(snapshot, policy)
        result["inventory_digest"] = inventory_digest(feedback)
        result["feedback"] = [
            {k: f[k] for k in ("source", "updated_at", "content_sha256", "url", "kind")}
            for f in feedback
        ]
        if not ledger:
            block("missing_trusted_feedback_ledger")
            return result
        if (
            ledger.get("schema") != 1
            or ledger.get("repository") != policy["repository"]
            or ledger.get("pull_number") != pr["number"]
        ):
            block("ledger_scope_mismatch")
        if ledger.get("head_sha") != head or ledger.get("base_sha") != base:
            block("ledger_sha_stale")
        if ledger.get("policy_sha256") != digest(policy):
            block("ledger_policy_stale")
        if ledger.get("inventory_digest") != result["inventory_digest"]:
            block("ledger_inventory_stale")
        entries = ledger.get("entries")
        entry_map = {}
        for entry in entries:
            key = text(entry.get("source"), "ledger source")
            if key in entry_map:
                raise GateError("duplicate ledger source")
            entry_map[key] = entry
        if set(entry_map) != {f["source"] for f in feedback}:
            block("ledger_source_set_mismatch")
        ledger_actor = actor_key(ledger["_comment"]["user"])
        author = actor_key(pr["user"])
        for item in feedback:
            entry = entry_map.get(item["source"])
            if not entry:
                block("feedback_unreviewed", source=item["source"])
                continue
            if (
                entry.get("updated_at") != item["updated_at"]
                or entry.get("content_sha256") != item["content_sha256"]
                or entry.get("reviewed_head_sha") != head
            ):
                block("feedback_review_stale", source=item["source"])
            disposition = entry.get("disposition")
            if (
                disposition not in DISPOSITIONS
                or not isinstance(entry.get("reason"), str)
                or not entry["reason"].strip()
            ):
                block("feedback_disposition_invalid", source=item["source"])
            if not evidence_urls(entry.get("evidence_urls"), policy["repository"]):
                block("feedback_evidence_missing", source=item["source"])
        proof = handling_proof(feedback, entry_map, snapshot, policy, ledger_actor)
        result["handling_digests"] = {
            k: proof[k] for k in ("feedback_digest", "dispositions_digest")
        }
        result["handling_candidates"] = proof["candidates"]
        if proof["accepted"] is None:
            block("independent_handling_verdict_missing_or_stale")
        else:
            result["handling_evidence"] = proof["accepted"]
        for thread in snapshot["threads"]:
            if not thread["isResolved"]:
                block(
                    "unresolved_review_thread",
                    thread=thread["id"],
                    outdated=thread["isOutdated"],
                )
        decisions: dict[tuple[int, str], dict[str, Any]] = {}
        codex = {
            actor_key(a)
            for a in policy["codex_reviewers"]
            if actor_key(a)[0] not in {author[0], ledger_actor[0]}
        }
        codex_reviews = []
        for review in sorted(
            snapshot["reviews"], key=lambda r: integer(r.get("id"), "review id")
        ):
            who = actor_key(review["user"])
            if review.get("state") == "PENDING":
                block("review_not_completed", review=review["id"])
            if review.get("state") in {"APPROVED", "CHANGES_REQUESTED"}:
                decisions[who] = review
            if who in codex and review.get("commit_id") == head:
                codex_reviews.append(review)
        for review in decisions.values():
            if review["state"] == "CHANGES_REQUESTED":
                block("changes_requested", review=review["id"])
        completed = [
            r
            for r in codex_reviews
            if r.get("state") in {"COMMENTED", "APPROVED"} and r.get("submitted_at")
        ]
        if not completed or (codex_reviews and codex_reviews[-1] not in completed):
            block("codex_review_missing_or_stale")
        else:
            result["codex_review"] = {
                "id": completed[-1]["id"],
                "head_sha": head,
                "url": completed[-1]["html_url"],
            }
        allowed = {policy_check_key(c): c for c in policy["checks"]}
        observed = set()
        for check in latest_checks:
            key = check_key(check)
            observed.add(key)
            if key not in allowed:
                block(
                    "unknown_check_context",
                    check=check["id"],
                    name=check["name"],
                    app_id=key[0],
                    workflow_path=key[1],
                )
            if (
                check.get("status") != "completed"
                or check.get("conclusion") != "success"
            ):
                block(
                    "latest_check_not_successful",
                    check=check["id"],
                    name=check["name"],
                    status=check.get("status"),
                    conclusion=check.get("conclusion"),
                )
        for key, config in allowed.items():
            if config["required"] and key not in observed:
                block(
                    "required_check_missing",
                    name=key[2],
                    app_id=key[0],
                    workflow_path=key[1],
                )
        status_policy = {s["context"]: s for s in policy["statuses"]}
        latest_status: dict[str, dict[str, Any]] = {}
        for status in sorted(
            snapshot["statuses"], key=lambda s: integer(s.get("id"), "status id")
        ):
            latest_status[text(status.get("context"), "status context")] = status
        for context, status in latest_status.items():
            config = status_policy.get(context)
            if (
                not config
                or status.get("creator", {}).get("id") != config["creator_id"]
            ):
                block("unknown_status_context", context=context)
            if status.get("state") != "success":
                block(
                    "latest_status_not_successful",
                    context=context,
                    state=status.get("state"),
                )
        for context, config in status_policy.items():
            if config["required"] and context not in latest_status:
                block("required_status_missing", context=context)
        # A newly queued workflow may not have emitted check runs yet.
        latest_runs: dict[tuple[str, str], dict[str, Any]] = {}
        for run in snapshot["workflows"]:
            if run.get("head_sha") != head:
                raise GateError("workflow belongs to a different SHA")
            key = (
                workflow_path(run.get("path")),
                text(run.get("event"), "workflow event"),
            )
            previous = latest_runs.get(key)
            if previous is None or (run["run_number"], run["run_attempt"]) > (
                previous["run_number"],
                previous["run_attempt"],
            ):
                latest_runs[key] = run
        known_paths = {k[1] for k in allowed if k[1] is not None}
        for run in latest_runs.values():
            jobs = run.get("_jobs")
            if not isinstance(jobs, list):
                raise GateError("workflow jobs missing")
            path = workflow_path(run["path"])
            if operational_run(run, snapshot["checks"], policy):
                continue
            if path not in known_paths:
                block("unknown_workflow", path=run["path"])
            links = run.get("pull_requests")
            if not isinstance(links, list) or any(
                p.get("number") == pr["number"]
                and (
                    p.get("head", {}).get("sha") != head
                    or p.get("base", {}).get("sha") != base
                )
                for p in links
            ):
                block(
                    "workflow_current_base_unproven",
                    run=run.get("id"),
                    path=run["path"],
                )
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                block(
                    "latest_workflow_not_successful",
                    run=run.get("id"),
                    path=run["path"],
                )
        by_run_id = {r["id"]: r for r in snapshot["workflows"]}
        for check in latest_checks:
            metadata = check.get("_workflow")
            if metadata is None:
                continue
            run = by_run_id.get(metadata.get("id"))
            if not run:
                block("check_workflow_evidence_missing", check=check["id"])
        result["snapshot_digest"] = digest(decision_view(snapshot, policy))
        result["allow"] = not reasons
    except (GateError, KeyError, TypeError, AttributeError, ValueError) as exc:
        block("invalid_evidence", detail=str(exc))
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        raise GateError(
            "GitHub API redirect refused; use the canonical repository name"
        )


class GitHub:
    """GET REST and query-only GraphQL. Never accepts a PR-provided URL."""

    def __init__(self, repository: str, token: str, transport: Any = None):
        if not REPO.fullmatch(repository) or not token:
            raise GateError("canonical repository and GitHub token required")
        self.repository = repository
        self.token = token
        self.transport = transport
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(
        self, path: str, query: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        if path.startswith("https://"):
            url = path
        else:
            url = "https://api.github.com" + path
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.username
            or parsed.password
        ):
            raise GateError("API URL outside trusted GitHub host")
        if parsed.path != "/graphql" and not parsed.path.startswith(
            f"/repos/{self.repository}/"
        ):
            raise GateError("API URL outside configured repository")
        if query is not None and (
            parsed.path != "/graphql"
            or not query.get("query", "").lstrip().startswith("query(")
        ):
            raise GateError("only read-only GraphQL queries permitted")
        if self.transport:
            return self.transport(url, query)
        data = json.dumps(query).encode() if query is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=45) as response:
                if int(response.headers.get("Content-Length", "0")) > 20_000_000:
                    raise GateError("API response too large")
                raw = response.read(20_000_001)
                if len(raw) > 20_000_000:
                    raise GateError("API response too large")
                return json.loads(raw, object_pairs_hook=unique_object), dict(
                    response.headers
                )
        except urllib.error.HTTPError as exc:
            # Never print response bodies/headers or the authorization token.
            raise GateError(f"GitHub API HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GateError("GitHub API unavailable") from exc

    def get(self, suffix: str) -> Any:
        return self.request(f"/repos/{self.repository}/{suffix}")[0]

    def pages(self, suffix: str, field: str | None = None) -> list[dict[str, Any]]:
        url = "https://api.github.com/repos/" + self.repository + "/" + suffix
        values: list[dict[str, Any]] = []
        seen = set()
        for _ in range(1000):
            if url in seen:
                raise GateError("REST pagination loop")
            seen.add(url)
            data, headers = self.request(url)
            batch = data.get(field) if field and isinstance(data, dict) else data
            if not isinstance(batch, list):
                raise GateError("malformed REST collection")
            values.extend(batch)
            link = next((v for k, v in headers.items() if k.lower() == "link"), "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not match:
                if 'rel="next"' in link:
                    raise GateError("malformed pagination link")
                if field and data.get("total_count", len(values)) > len(values):
                    raise GateError("truncated REST collection")
                return values
            url = match.group(1)
        raise GateError("REST pagination limit exceeded")

    def graphql_pages(
        self, number: int, connection: str, fields: str, *, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        owner, repo = self.repository.split("/")
        cursor = None
        seen = set()
        output = []
        for _ in range(1000):
            if thread_id:
                query = (
                    "query($id:ID!,$after:String){node(id:$id){... on PullRequestReviewThread{"
                    "comments(first:100,after:$after){nodes{"
                    + fields
                    + "} pageInfo{hasNextPage endCursor}}}}}"
                )
                variables = {"id": thread_id, "after": cursor}
            else:
                query = (
                    "query($owner:String!,$repo:String!,$number:Int!,$after:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){"
                    + connection
                    + "(first:100,after:$after){nodes{"
                    + fields
                    + "} pageInfo{hasNextPage endCursor}}}}}"
                )
                variables = {
                    "owner": owner,
                    "repo": repo,
                    "number": number,
                    "after": cursor,
                }
            data, _ = self.request("/graphql", {"query": query, "variables": variables})
            if data.get("errors"):
                raise GateError("GraphQL returned errors")
            try:
                conn = (
                    data["data"]["node"]["comments"]
                    if thread_id
                    else data["data"]["repository"]["pullRequest"][connection]
                )
                nodes, page = conn["nodes"], conn["pageInfo"]
                if not isinstance(nodes, list) or type(page["hasNextPage"]) is not bool:
                    raise GateError("malformed GraphQL pagination")
                output.extend(nodes)
                if not page["hasNextPage"]:
                    return output
                cursor = text(page["endCursor"], "GraphQL cursor")
                if cursor in seen:
                    raise GateError("GraphQL pagination loop")
                seen.add(cursor)
            except (KeyError, TypeError) as exc:
                raise GateError("incomplete GraphQL response") from exc
        raise GateError("GraphQL pagination limit exceeded")

    def collect_once(
        self, number: int, policy: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        pr = self.get(f"pulls/{number}")
        head = pr["head"]["sha"]
        snap: dict[str, Any] = {
            "schema": 1,
            "repository": self.repository,
            "pr": pr,
            "errors": [],
            "stable": False,
            "complete": {name: True for name in COMPLETE_KEYS},
        }
        comparison = self.get(f"compare/{pr['base']['sha']}...{head}")
        comparison["_requested_base_sha"], comparison["_requested_head_sha"] = (
            pr["base"]["sha"],
            head,
        )
        snap["comparison"] = comparison
        snap["issue_comments"] = self.pages(f"issues/{number}/comments?per_page=100")
        snap["reviews"] = self.pages(f"pulls/{number}/reviews?per_page=100")
        snap["review_comments"] = self.pages(f"pulls/{number}/comments?per_page=100")
        snap["review_meta"] = self.graphql_pages(
            number, "reviews", "databaseId updatedAt body state commit{oid}"
        )
        fields = "databaseId updatedAt body"
        threads = self.graphql_pages(
            number,
            "reviewThreads",
            "id isResolved isOutdated comments(first:100){nodes{"
            + fields
            + "} pageInfo{hasNextPage endCursor}}",
        )
        for thread in threads:
            if thread["comments"]["pageInfo"]["hasNextPage"]:
                thread["comments"] = self.graphql_pages(
                    number, "comments", fields, thread_id=thread["id"]
                )
            else:
                thread["comments"] = thread["comments"]["nodes"]
        snap["threads"] = threads
        suites = self.pages(f"commits/{head}/check-suites?per_page=100", "check_suites")
        if len(suites) >= 1000:
            raise GateError(
                "check-suite API limit reached; completeness cannot be proved"
            )
        snap["check_suites"] = suites
        checks = self.pages(
            f"commits/{head}/check-runs?filter=all&per_page=100", "check_runs"
        )
        snap["statuses"] = self.pages(f"commits/{head}/statuses?per_page=100")
        workflows = self.pages(
            f"actions/runs?head_sha={head}&per_page=100", "workflow_runs"
        )
        job_checks = {}
        for run in workflows:
            run["_jobs"] = []
            run_id = integer(run.get("id"), "run id")
            for attempt in range(1, integer(run.get("run_attempt"), "run attempt") + 1):
                jobs = self.pages(
                    f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                    "jobs",
                )
                for job in jobs:
                    if "run_attempt" in job and job["run_attempt"] != attempt:
                        raise GateError("job attempt disagrees with attempt endpoint")
                    job["run_attempt"] = attempt
                run["_jobs"].extend(jobs)
            for job in run["_jobs"]:
                ident = int(job["check_run_url"].rsplit("/", 1)[-1])
                job_checks[ident] = {
                    "id": run["id"],
                    "path": run["path"],
                    "run_number": run["run_number"],
                    "run_attempt": job["run_attempt"],
                }
        for check in checks:
            check["_workflow"] = job_checks.get(check["id"])
            own_gate = policy is not None and check_key(check) in operational_keys(
                policy
            )
            if (
                check.get("app", {}).get("slug") == "github-actions"
                and check["_workflow"] is None
                and not own_gate
            ):
                raise GateError("GitHub Actions check has no matching workflow job")
            count = check.get("output", {}).get("annotations_count")
            if type(count) is not int or count < 0:
                raise GateError("missing check annotation count")
            check["_annotations"] = (
                self.pages(f"check-runs/{check['id']}/annotations?per_page=100")
                if count
                else []
            )
            if len(check["_annotations"]) != count:
                raise GateError("incomplete check annotations")
        snap["checks"], snap["workflows"] = checks, workflows
        # Head/base movement during an individual sweep invalidates all its evidence.
        after = self.get(f"pulls/{number}")
        if pr_state(after) != pr_state(pr):
            raise GateError("PR state/head/base changed during collection")
        return snap

    def collect(self, number: int) -> tuple[dict[str, Any], dict[str, Any]]:
        initial_pr = self.get(f"pulls/{number}")
        policy_sha = initial_pr["base"]["sha"]
        policy_file = self.get(f"contents/{POLICY_PATH}?ref={policy_sha}")
        if policy_file.get("encoding") != "base64" or policy_file.get("type") != "file":
            raise GateError("trusted base policy unavailable")
        policy = json.loads(
            base64.b64decode(policy_file["content"], validate=False),
            object_pairs_hook=unique_object,
        )
        validate_policy(policy)
        first = self.collect_once(number, policy)
        second = self.collect_once(number, policy)
        if (
            first["pr"]["base"]["sha"] != policy_sha
            or second["pr"]["base"]["sha"] != policy_sha
        ):
            raise GateError("PR base changed after trusted policy was fetched")

        if digest(decision_view(first, policy)) != digest(
            decision_view(second, policy)
        ):
            raise GateError("feedback/checks changed between complete reads")
        second["stable"] = True
        return second, policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", "--repo")
    parser.add_argument("--pull-number", "--pr", type=int)
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="offline rehearsal snapshot; never live merge authority",
    )
    parser.add_argument("--policy", type=Path, help="offline rehearsal policy only")
    args = parser.parse_args(argv)
    try:
        if args.snapshot:
            if not args.policy:
                raise GateError("offline snapshot requires --policy")
            snapshot = json.loads(
                args.snapshot.read_text(), object_pairs_hook=unique_object
            )
            policy = json.loads(
                args.policy.read_text(), object_pairs_hook=unique_object
            )
            mode = "offline-rehearsal"
        else:
            if (
                args.policy
                or not args.repository
                or not args.pull_number
                or args.pull_number < 1
            ):
                raise GateError(
                    "live mode requires --repository and --pull-number; --policy is offline only"
                )
            snapshot, policy = GitHub(
                args.repository,
                os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "",
            ).collect(args.pull_number)
            mode = "live"
        result = evaluate(snapshot, policy)
        result["mode"] = mode
        result["merge_authority"] = mode == "live" and result["allow"]
        result["authority_scope"] = (
            "immediate-sha-conditional-eligibility"
            if mode == "live"
            else "rehearsal-only"
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["allow"] else 1
    except (
        GateError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
    ) as exc:
        detail = (
            str(exc)
            if isinstance(exc, GateError)
            else f"input or transport error ({type(exc).__name__})"
        )
        print(
            json.dumps(
                {
                    "schema": 1,
                    "allow": False,
                    "merge_authority": False,
                    "validation_claimed": False,
                    "reasons": [
                        {"code": "collection_or_input_error", "detail": detail}
                    ],
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())

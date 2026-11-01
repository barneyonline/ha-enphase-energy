# PR feedback and merge eligibility gate

`pr_gate.py` is a standard-library Python 3 script. It reads GitHub REST/GraphQL
evidence, evaluates it and prints one JSON result. It never posts feedback,
resolves threads, invokes reviewers, runs PR code, executes comment commands,
changes settings, closes issues, publishes releases or merges.

Run gate regressions with
`python3 -m unittest discover -s tests/scripts -p 'test_maintenance_pr_gate*.py'`.

## CLI and trust boundary

```sh
python3 scripts/maintenance/pr_gate.py \
  --repository OWNER/REPOSITORY --pull-number 123
```

Use a read-capable `GH_TOKEN` (or `GITHUB_TOKEN`) in the environment. The script
does not print it or API response bodies on errors. REST uses GET only; GraphQL
uses query-only POST. API host/repository and pagination URLs are constrained;
redirects and incomplete/partial responses block.

Live mode fetches `.github/maintenance/merge-policy.json` from the exact current
PR **base SHA**, before collecting two complete snapshots. It never falls back
to a PR policy or a local override. The invoking workflow must also load the
gate code from a trusted base/default checkout and must not execute PR code.
Bootstrap has no automatic bypass when the base lacks this policy.

For fixture rehearsal only:

```sh
python3 scripts/maintenance/pr_gate.py --snapshot snapshot.json --policy policy.json
```

Exit codes: `0` means eligible under the supplied evidence, `1` means blocked,
`2` means collection/input failure. **An offline exit 0 is not merge authority**:
its JSON always says `mode: offline-rehearsal`, `merge_authority: false`, and
`validation_claimed: false`. Only a successful live collection can set
`merge_authority: true`; this means immediate, SHA-conditional eligibility only.
The gate does not claim to have run tests, validated a release, measured physical
devices or proved semantic correctness of a fix.

## Policy schema 1

Use `merge-policy.example.json` as a schema template. Its zero actor IDs and
placeholder check deliberately fail validation until replaced with verified
identities and actual contexts. Do not deploy the example unchanged.

- `repository`, `base_branch`, `schema`: exact scope.
- `trusted_ledger_authors`: numeric GitHub user ID **and** exact login, for the
  maintainer/HAL identities allowed to attest that feedback was reviewed.
- `codex_reviewers`: independently verified Codex GitHub bot IDs/logins. The
  documented login is `chatgpt-codex-connector[bot]`; verify its numeric identity
  in the real review rehearsal rather than guessing it.
- `independent_reviewers`: trusted reviewer IDs/logins allowed to support a
  dismissal. The proof author must differ from both PR author and ledger author.
- `gate_check`: exact name, App ID and `workflow_path`. Recommended custom head
  check: `Autonomous maintenance / merge gate`, App ID `15368`, path `null`.
- `operational_check`: required singular native execution identity, exactly
  `{name: "maintenance-gate", app_id: 15368, workflow_path: ".github/workflows/maintenance-gate.yml"}`.
  Arbitrary operational contexts and wildcard values are rejected.
- `checks`: all allowed check identities `{name, app_id, workflow_path, required}`.
  GitHub Actions jobs use their workflow file path; external app checks use
  `null`. Duplicate job names in different workflows remain distinct. Include
  CodeQL, Codecov and assignment checks where they actually exist. Required
  contexts must be present. Every observed latest context must be allowlisted.
- `statuses`: all allowed legacy commit-status contexts
  `{context, creator_id, required}`. The latest value per context must succeed.

Unknown policy fields fail closed. There is no wildcard or implicit bot trust.
Missing/pending/failed/cancelled/neutral/skipped checks block. There are initially
no skip exceptions: a future per-context not-applicable rule needs explicit
trusted policy, exact classifier/condition evidence and tests; never add a global
"skipped is success" switch.

## Complete feedback inventory and ledger

The gate inventories every PR issue comment (including edited Codecov reports),
formal review, inline thread comment, check attempt (with annotations) and legacy
status. All collections are paginated, including nested thread comments. REST and
GraphQL review/comment fingerprints must agree. All unresolved threads block,
including outdated threads. Active changes-requested or pending reviews block.

Each source has an ID such as `issue_comment:123`, `review:456`,
`inline_comment:789`, `check:987`, `status:654`, plus `updated_at`,
`content_sha256` and URL. Body/output hashing also detects edits whose timestamps
did not change. Formal review edits use GraphQL `updatedAt` alongside REST data.

The only ignored feedback is the exact custom gate and native operational check, trusted standalone
ledger comments and an exact `@codex review` comment by the PR author or a trusted
ledger author. Appending explanations or commands makes it normal feedback.
No arbitrary bot, author, success-sounding text or PR-body checkbox is ignored.

A trusted author posts a **standalone** hidden JSON ledger comment:

```text
<!-- hal-feedback-ledger:v1 {"schema":1,"repository":"OWNER/REPOSITORY","pull_number":123,"head_sha":"40_HEX_CHARACTERS","base_sha":"40_HEX_CHARACTERS","policy_sha256":"POLICY_HASH","inventory_digest":"INVENTORY_HASH","entries":[{"source":"issue_comment:123","updated_at":"2026-11-01T01:00:00Z","content_sha256":"SOURCE_HASH","reviewed_head_sha":"40_HEX_CHARACTERS","disposition":"addressed","reason":"Reviewed the generated report and verified its claims against the linked run.","evidence_urls":["https://github.com/OWNER/REPOSITORY/actions/runs/123"]}]} -->
```

Only exact standalone, known-schema ledger comments are eligible for exclusion.
The newest such trusted ledger by numeric ID supersedes older valid ledgers.
Appended prose, unknown schema or extra JSON fields stay in the feedback
inventory even on an older ledger-like comment. Malformed ledger JSON blocks.
Entries must cover the exact source set and fingerprints, current head/base and
policy digest. New commits, feedback, report edits, check attempts or policy/base
changes invalidate the ledger. When missing, the blocked result still exposes
the inventory/digests needed to prepare a ledger, without dumping raw comments.

Dispositions are `addressed`, `not-applicable`, `duplicate`, `false-positive`.
Every entry needs a specific reason and repository HTTPS evidence URLs.
`addressed` can mean a clean informational report was reviewed and its claims
verified; it does not require an unnecessary code change. For an actual finding,
link the corrective commit/test/run and explain the outcome. A trusted author
attests this semantic judgment; the deterministic gate checks provenance and
binding, not the truth of arbitrary prose or whether a linked test proves a fix.

All arbitrary sources are conservatively potentially substantive. Every final
disposition, including `addressed`, needs independent clean handling evidence;
changing the label to `addressed` cannot bypass independence. The reviewer must
have a different numeric GitHub ID from both PR author and ledger author.
Accepted proof is an exact standalone independently authored, digest-bound
**top-level PR conversation comment (issue comment)**. Formal review bodies and
inline review comments remain ordinary feedback and cannot supply the handling
witness, even with an exact marker. A bare approval or timestamp order never
substitutes for these bindings: CheckRun output
can change without changing `completed_at`, and ledger dispositions can change
after a review.

Complete the native exact-head code review first and wait for all resulting
`review-event` jobs, bot summaries and other checks/reports to settle. Then
prepare the current feedback ledger and obtain the independent issue-comment
verdict. Review submission or editing emits another observer check, which would
invalidate a verdict embedded in that review. The issue-comment path avoids
that cycle while keeping every observer check in the inventory and digests.
Pending or failed observer checks still block; they have no operational exclusion.

```text
<!-- hal-clean-verdict:v1 {"schema":1,"repository":"OWNER/REPOSITORY","pull_number":123,"head_sha":"40_HEX_CHARACTERS","base_sha":"40_HEX_CHARACTERS","policy_sha256":"POLICY_HASH","feedback_digest":"FEEDBACK_HASH","dispositions_digest":"DISPOSITIONS_HASH","verdict":"clean","reason":"Independently verified the actual handling and final result.","evidence_urls":["https://github.com/OWNER/REPOSITORY/commit/COMMIT_SHA"]} -->
```

The output `handling_digests` provides hashes over all current source fingerprints
and ledger dispositions for a prospective new witness. Once a candidate is
posted, `handling_candidates` exposes expected hashes keyed by that source.
Verification excludes **only the candidate's own source and ledger entry** to
avoid a self-hash cycle. Every other verdict, including invalid, blocked and
different-scope verdicts, remains ordinary covered feedback. Witnesses stay in
the inventory and require ledger accounting. Sequential witnesses therefore bind
all prior witnesses. Changing any other source/disposition invalidates a verdict.
Legacy per-source
`hal-independent-review:v1` markers do not substitute for a final clean verdict.
The gate never fabricates reviewer output or treats an author's claim of a false
positive as independent evidence.

## Codex completion and checks

A completed formal `COMMENTED` or `APPROVED` review from an explicitly trusted
Codex identity must have API `commit_id` equal to the candidate head SHA. Its
numeric identity must differ from the PR/ledger author. A `COMMENTED` review proves
completion only; it does not itself prove clean handling. Its feedback still
requires ledger accounting, independent clean evidence and thread resolution. A bare thumbs-up
or eyes reaction, missing review, old review, matching text or PR comment cannot
prove this binding and does not pass. Current official documentation describes
standard GitHub reviews: [Codex GitHub integration](https://learn.chatgpt.com/docs/third-party/github).
Rehearsal must verify real clean-review output before enabling unattended merge.

Every snapshot read fetches trusted `compare/{currentBaseSHA}...{headSHA}`. The exact
requested SHAs, `base_commit.sha`, `merge_base_commit.sha`, zero `behind_by` and
`ahead`/`identical` status must prove the current base is an ancestor of the tested
head. Empty run PR metadata is common and does not prove anything by itself;
explicitly contradictory metadata still blocks. A base advance requires updating
the candidate and retesting. Comparison data participates in snapshot stability.

GitHub job IDs are mapped using each attempt-specific Jobs endpoint, so the
optional per-job `run_attempt` field is not assumed. Ref-qualified workflow paths
are normalized consistently while their raw provenance is retained. The sole
supported synthetic path is exact `dynamic/github-code-scanning/codeql`; there
is no dynamic-path wildcard. The check-suite inventory must be below the API's
1000-suite completeness boundary, otherwise collection blocks. A superseded failed attempt does not remain
the current check failure after a successful rerun; its feedback must still have
a ledger entry. A newer failure overrides an older success. A newly queued
workflow with no check run yet also blocks. Unknown apps/contexts never silently
disappear.

## Event strategy and final merge boundary

Run the trusted wrapper on PR opened/synchronize/reopened/ready-for-review/base
changes; issue-comment created/edited/deleted; review submitted/edited/dismissed;
inline-comment created/edited/deleted; CI `workflow_run` completion; external
check/status events and explicit coordinator recovery/relay dispatch. Resolve the PR number
from trusted event/API metadata, not shell interpolation of titles/comments.
Where supported, review-thread resolution changes should invalidate/recompute;
durable event delivery replay and explicit recovery handle missed or unavailable events. There is no periodic polling schedule. GitHub Actions does not fire all
check events recursively for its own checks, so do not rely on `check_run` alone.

Use per-PR concurrency and execute trusted base code. The custom CheckRun and the
native `pull_request_target` job can both be associated with the candidate head.
Exclude only their two exact operational identities. Workflow execution metadata
is excluded only when each observed run attempt has exactly one job and matching
check mapping to the exact gate identity. Historical rerun attempts remain
operational; duplicate same-name jobs/checks within an attempt remain feedback
and block. Extra jobs or
mismatched App IDs/names/paths stay inventoried and block. A CheckSuite is ignored
for decision stability only when its complete observed checks are exclusively
these operational identities; any additional check keeps the suite in evidence.
Do not publish a changing
commit status for the gate: statuses are feedback and would invalidate its own
ledger. Do not broadly ignore a maintenance workflow or the `github-actions` app.

The wrapper needs read access to contents, pull requests, issues, actions, checks
and statuses; publishing the result separately needs checks write. It must never
checkout/execute untrusted PR code with that token. Gate code itself stays read-only.

Two identical complete snapshot reads detect movement during collection. GitHub has no
atomic lease on the entire feedback snapshot: a comment can arrive after the
read. Immediately re-evaluate live before any merge and supply the exact expected
head SHA to GitHub's merge API. Invalidate on feedback updates, serialize the
coordinator, and never treat an old green check or saved JSON as lasting authority.
This residual API race is an explicit limitation, not a claim of atomic approval.

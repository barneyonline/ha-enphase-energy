## Summary

Refs #<!-- issue number; keep the issue open until its verified release -->

<!-- Describe the user-visible problem and resulting behavior. State whether this is a bug fix, feature, maintenance, or release preparation. -->

```text
Before: <concrete trigger and observed behavior>
After:  <resulting behavior>
```

<!-- For structural changes, replace the sketch with a small call tree or diagram that explains the change. -->

## Evidence

- **Before:** <!-- Failing reproducer, test, or observed evidence; state when unavailable. -->
- **After:** <!-- Passing verification of the same behavior. -->
- **Feedback:** <!-- Link the current feedback ledger and independent GitHub review. Account for Codecov and all automated reports; disclose remaining blockers. -->
- **Release:** <!-- Candidate version/scope, or state that this PR is not itself a release. -->

## Testing

<!-- List exact commands and results from the repository's AGENTS.md validation contract, including applicable Docker checks, typing, quality, translations and touched-file coverage. Link CI and sanitized artifacts. Explain any unavailable or not-applicable check with evidence. -->

## Merge Danger

**Door:** <!-- two-way or one-way; explain any irreversible effect -->

**Blast Radius:** <!-- One-word scope, followed by the concrete affected behavior. -->

<!-- State the recovery/revert path and material limitations. Distinguish cloud tests from physical-device validation. -->

## Checklist

- [ ] The change has a focused scope and meaningful verification.
- [ ] Changelog and relevant user/contributor documentation are current.
- [ ] Applicable translations, service schemas, diagnostics redaction and quality-scale evidence are current.
- [ ] Required checks, touched-file coverage and all PR/automated feedback have been reviewed and actioned; unresolved items are disclosed.
- [ ] The issue remains open until the verified release delivers its fix.

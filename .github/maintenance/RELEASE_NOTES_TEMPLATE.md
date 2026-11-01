# Release notes template

Use the same category order as the changelog. Fill this from the reviewed diff between the previous stable release and the exact candidate, preserving the repository's version and asset names. Replace placeholders and include the actual issue/PR links. State `None` for empty categories. Automated stable releases contain bug fixes and eligible maintenance only.

```markdown
## Summary

<Version and concise user-visible result.>

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- <Trigger, corrected result, and related issue/PR.>

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## Validation

- <Relevant regression and repository checks, linked to evidence.>
- <Reporter/prerelease verification when required, or the material verification limit.>
- <Verified version, package assets, and checksum when supplied.>

## Update and recovery

- <Verified installation/update steps and any required action.>
- <Safe recovery path and previous stable version.>

## Related issues

- <Issue links delivered by this release.>
```

Keep prose concise and factual. Distinguish build/test success, public package delivery, and observed device behavior. Release verification precedes issue closure; each closure comment links the release and invites reopening if updating does not fix the problem.

# Changelog entry template

Add entries under `## Unreleased` using these categories in this order. Preserve the repository's existing released-version heading and tag conventions. Keep historical releases intact. Use `- None` for an empty category. Move the selected entries into a dated version section when preparing a release.

```markdown
## Unreleased

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- <Fix the concrete trigger and explain the corrected user-visible behavior.> (#<issue>)

### 🔧 Improvements

- None

### 🔄 Other changes

- None
```

Each entry describes one coherent outcome and links its originating issue or PR. Explain compatibility or update action when relevant. Put tests and implementation evidence in the PR; put material validation limits in the release notes. Classify new behavior as a feature even if the implementation is small. Automated bug releases contain no unreleased features or breaking changes.

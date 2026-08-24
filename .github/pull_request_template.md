## Summary

Include a concise description of the change and its motivation. Reference related issues or discussions (e.g., `Fixes #123`).

## Related Issues

List related issues or discussions if this PR does not close them directly.

## Type of change

- [ ] Bugfix
- [ ] Device support / compatibility
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / tech debt
- [ ] Translation update
- [ ] Other (describe below)

## Testing

List the exact commands you ran. Prefer the pinned Docker environment from `devtools/docker/`.

```bash
docker compose -f devtools/docker/docker-compose.yml up -d ha-dev
docker compose -f devtools/docker/docker-compose.yml exec ha-dev ruff check .
docker compose -f devtools/docker/docker-compose.yml exec ha-dev black <changed-python-files> tests/components/enphase_ev/<changed-test-files>
docker compose -f devtools/docker/docker-compose.yml exec ha-dev python scripts/validate_quality_scale.py --validate-remote-brands
docker compose -f devtools/docker/docker-compose.yml exec ha-dev python scripts/importtime_profile.py --strict-integration-warnings --output importtime-enphase-ev.log
docker compose -f devtools/docker/docker-compose.yml exec ha-dev mypy --strict --ignore-missing-imports --follow-imports=skip custom_components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec ha-dev pre-commit run --all-files
docker compose -f devtools/docker/docker-compose.yml exec -e COVERAGE_FILE=/tmp/enphase_ev.coverage ha-dev python -m coverage run --source=custom_components.enphase_ev -m pytest -q tests/components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec -e COVERAGE_FILE=/tmp/enphase_ev.coverage ha-dev python -m coverage report -m --fail-under=95
docker compose -f devtools/docker/docker-compose.yml exec -e COVERAGE_FILE=/tmp/enphase_ev.coverage ha-dev python -m coverage report -m --include=<touched-module-paths-comma-separated> --fail-under=100
```

Add any extra commands, targeted tests, or manual validation below.

## Checklist

- [ ] I updated `CHANGELOG.md` for user-facing changes.
- [ ] I updated documentation (`README.md`, docs/) when behaviour or options changed.
- [ ] I verified translations (`custom_components/enphase_ev/translations/`) are complete and valid.
- [ ] I ran targeted coverage for each touched Python module and confirmed 100% coverage.
- [ ] I reviewed GitHub Actions results (tests, hassfest, quality scale, validate).
- [ ] I confirm this PR is scoped to a single logical change set.

## Diagnostics / Screenshots / Notes

Add screenshots, diagnostics, repair-issue context, or implementation notes that reviewers should see.

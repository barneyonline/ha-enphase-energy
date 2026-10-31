# Contributing to Enphase Energy

Thanks for helping improve this Home Assistant custom integration! We follow Home Assistant and HACS standards to keep the project healthy and dependable. Please read through this guide before opening a pull request.

## Code of Conduct

By participating you agree to uphold the [Home Assistant Code of Conduct](https://www.home-assistant.io/code_of_conduct/). Be respectful and inclusive when interacting with the community.

## How to Help

- Report reproducible bugs and attach diagnostics or logs where possible.
- Report device-specific support gaps with the affected model, firmware, and site layout.
- Suggest enhancements or improvements to documentation.
- Contribute code, tests, translations, or quality scale compliance work.
- Review open pull requests and share constructive feedback.

Before starting large features, open an issue or discussion so we can agree on scope and fit.

## Reporting Bugs And Device Support Gaps

Use the GitHub issue forms so reports are routed into the right triage path:

- `Bug report` for regressions, broken controls, incorrect telemetry, repair issues, or diagnostics/export problems.
- `Device support request` for unsupported device models, missing entities for a specific device family, or missing site capabilities such as HEMS-derived channels.
- `Feature request` for broader improvements that are not tied to a single unsupported device or broken behavior.

Before opening a bug or device-support issue, capture diagnostics in Home Assistant:

1. Go to `Settings -> Devices & Services -> Integrations`.
2. Open `Enphase Energy` and choose `Download diagnostics`.
3. For device-specific problems, open the affected device page and download device diagnostics too.
4. Review the redacted file, then drag it into the GitHub issue body or a follow-up comment.

Include the integration version, Home Assistant version, affected entity IDs, device model or firmware details, and whether the issue started after an update.

## Development Workflow

1. **Fork and clone** the repository.
2. **Create a feature branch** (`feature/...`, `bugfix/...`, or `docs/...`) from the latest `main`.
3. **Build the pinned Docker environment**:
   ```bash
   docker compose -f devtools/docker/docker-compose.yml build ha-dev
   ```
4. **Start `ha-dev` once, then develop and test inside it**:
   ```bash
   docker compose -f devtools/docker/docker-compose.yml up -d ha-dev
   docker compose -f devtools/docker/docker-compose.yml exec ha-dev pytest -q tests/components/enphase_ev
   ```
   Reuse the running container with `docker compose exec` for subsequent checks
   to avoid paying container startup costs on every command.
   The default `ha-dev` lane pins Home Assistant 2026.10.0, the matching test
   plugin, and independently upgradable tooling constraints in
   `devtools/docker/constraints-dev.txt`. Rebuild after changing requirements.
   The minimum supported Home Assistant version is 2026.10.0, paired with
   `pytest-homeassistant-custom-component==0.13.371`. The full integration suite
   runs on this exact version in Docker and CI, so a separate minimum-version
   environment and repeated compatibility subset are unnecessary.
   Upgrade Home Assistant together with its matching test plugin and validate
   the tooling constraints; do not overlay a beta on an unrelated dependency stack.
5. **Start `ha-runtime` when you need a real Home Assistant UI for manual verification**:
   ```bash
   mkdir -p .ha-config
   docker compose -f devtools/docker/docker-compose.yml up -d ha-runtime
   ```
   This runs the official Home Assistant container, mounts `.ha-config/` to `/config`, and mounts this checkout's `custom_components/enphase_ev` into Home Assistant.
6. **Commit with clear messages** and push your branch.
7. **Open a pull request** using the template. Fill in every section and link any related issues.

Use the pinned Docker environment for linting, formatting, coverage, and tests:

```bash
docker compose -f devtools/docker/docker-compose.yml exec ha-dev ruff check .
docker compose -f devtools/docker/docker-compose.yml exec ha-dev black custom_components/enphase_ev tests/components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec ha-dev python scripts/validate_quality_scale.py
docker compose -f devtools/docker/docker-compose.yml exec ha-dev mypy --strict --ignore-missing-imports --follow-imports=skip custom_components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec ha-dev pre-commit run --all-files
docker compose -f devtools/docker/docker-compose.yml exec ha-dev pytest -q tests/components/enphase_ev
```

The quality-scale validator checks the bundled `brand/` PNG images. The legacy
`--validate-remote-brands` flag remains accepted for existing CI commands and
performs the same local checks; it no longer requests retired brands CDN assets.

The full integration suite must maintain 100% statement and branch coverage.
The shared `.coveragerc` enables branch measurement and the 100% report gate.
Collect coverage once and reuse the data for the full gate and changed-module
statement report. Replace the example `--include` value with your changed modules.
This coverage run replaces the separate integration pytest invocation above.

```bash
docker compose -f devtools/docker/docker-compose.yml exec -e COVERAGE_FILE=/tmp/enphase_ev.coverage ha-dev python -m coverage run --branch --source=custom_components.enphase_ev -m pytest -q tests/components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec -e COVERAGE_FILE=/tmp/enphase_ev.coverage ha-dev python -m coverage report -m --fail-under=100
docker compose -f devtools/docker/docker-compose.yml exec -e COVERAGE_FILE=/tmp/enphase_ev.coverage ha-dev python -m coverage json -o /tmp/enphase_ev.coverage.json
docker compose -f devtools/docker/docker-compose.yml exec ha-dev python scripts/check_statement_coverage.py --report /tmp/enphase_ev.coverage.json --include="custom_components/enphase_ev/api.py,custom_components/enphase_ev/coordinator.py"
```

Shared fixtures preserve explicit empty serial lists and payloads. Give unit-test
coordinator doubles an explicit `inventory_view` with only the capabilities the
test needs; do not modify standard-library classes to supply missing behavior.
`test_integration_lifecycle.py` covers charger and site-only setup with real
platforms, topology-option reload, registry customization, polling, and unload.
It replaces cloud methods and fails on unexpected cloud calls. These scenarios
also run in the full integration suite on the supported minimum Home Assistant
version.

Passing pytest output can hide runtime warning and error logs. CI retains these
as the `pytest-runtime-warnings-python314` and `script-runtime-warnings` artifacts.
Capture the same evidence locally when investigating warnings:

```bash
docker compose -f devtools/docker/docker-compose.yml exec ha-dev pytest -q tests/components/enphase_ev --log-file=/tmp/enphase_ev-runtime-warnings.log --log-file-level=WARNING
```

Keep test doubles complete for the lifecycle they participate in, unload loaded
entries, and cancel or await each coordinator's owned work during fixture teardown.
Home Assistant's custom-component test plugin permits lingering timers by default;
our fixture checks must not rely on that permission to hide incomplete teardown.
Preserve explicit failure-path logging and assert expected failures in their tests.
Home Assistant API deprecation logs require runtime coverage; the import-time
warning gate alone cannot detect a deprecated method called after setup.

Some external diagnostics are expected: Home Assistant identifies custom
integrations as untested by Core, optional compression backends can fall back to
zlib, and AMD64 hassfest under ARM/QEMU can report jemalloc fallback messages.
Codecov's valid GPG signature can still carry a key-certification warning; retain
signature checks rather than automatically trusting a downloaded key. Its missing
optional YAML message means the uploader is using defaults. CodeQL's native-module
path probe is upstream-owned: check its extraction coverage and tool version before
changing integration behavior. The managed job is separate from this test workflow;
setting the unsupported `CODEQL_PYTHON` variable adds another warning rather than
controlling that interpreter. Asyncio debug timing under a shared Docker VM must
be reproduced in a focused run before treating it as live event-loop blocking.

Maintenance-script tests use only lightweight dependencies and can be run
separately while iterating on `scripts/`:

```bash
docker compose -f devtools/docker/docker-compose.yml exec ha-dev pytest -q tests/scripts
```

Pull-request CI selects runtime, import-diagnostic, quality, and maintenance-script
jobs from the changed paths. Manifest edits limited to `categories`, `codeowners`,
`documentation`, and `issue_tracker` skip the full integration pytest job when no
other path requires it. Hassfest, HACS, scoped lint, and quality checks still run.
The classifier compares JSON values against the PR merge base; changing any other
manifest field (including `version`, dependencies, or discovery), adding or removing
the manifest, or supplying invalid JSON keeps the full integration suite enabled.
Changes to the classifier or test workflow also run the full suite. Pushes to main
and manual runs retain all test jobs.

The HACS validator copies Python source from a pinned AMD64-only upstream image
onto the pinned Home Assistant runtime. `HACS_SOURCE_PLATFORM` selects that source
stage; the final image retains Docker's requested target platform. Keep both image
digests and the Home Assistant version assertion when updating this validator.

## Dependency Maintenance

Declare the dependencies this repository can choose directly. Keep the supported
Home Assistant version and its matching test plugin in `requirements-dev.txt`,
and the independently upgradable certificate bundle `certifi` in
`constraints-dev.txt`. Let pre-commit select its own virtualenv and filelock
dependencies. Keep the Black requirement and pre-commit
hook aligned when updating the formatter.

Home Assistant and its test plugin provide their own transitive requirements.
Do not duplicate their exact pins in a local dependency freeze or submit their
complete installed environment to GitHub's dependency graph. Packages such as
`Pillow`, `uv`, `aiohttp`, and `cryptography` remain installed at the versions
required by Home Assistant. Upgrade the Home Assistant/test-plugin pair together
when changing the supported minimum, then rebuild and validate in Docker.

Dependabot alerts and security updates remain enabled for the repository's
declared dependencies. The `open-pull-requests-limit: 0` setting disables only
routine Python version-update PRs. Removing upstream pins from repository
metadata does not remediate vulnerabilities in a Home Assistant installation.
See GitHub's
[security-update configuration guidance](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-security-updates).

## Coding Standards and Tooling

Home Assistant integrations must follow the [core development guidelines](https://developers.home-assistant.io/docs/development_guidelines/). Key points:

- Use modern Python syntax (f-strings, type hints) and keep logging format strings lazy.
- Interact with external services via dedicated client libraries or well-structured helpers.
- Use Google-style docstrings when detailed documentation is needed.
- Keep YAML and documentation formatting consistent with the [Home Assistant style guide](https://developers.home-assistant.io/docs/documenting/yaml-style-guide/).

This repository relies on the following checks. Please run them locally before pushing:

```bash
docker compose -f devtools/docker/docker-compose.yml exec ha-dev ruff check .
docker compose -f devtools/docker/docker-compose.yml exec ha-dev black custom_components/enphase_ev tests/components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec ha-dev pytest -q tests/components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec ha-dev python scripts/validate_quality_scale.py
docker compose -f devtools/docker/docker-compose.yml exec ha-dev mypy --strict --ignore-missing-imports --follow-imports=skip custom_components/enphase_ev
docker compose -f devtools/docker/docker-compose.yml exec ha-dev pre-commit run --all-files
```

> hassfest validation runs automatically in CI via [`home-assistant/actions/hassfest`](https://github.com/home-assistant/actions/tree/master/hassfest). If you need to run it locally, clone the Home Assistant Core repository and execute `python -m script.hassfest` from your integration checkout.

> Tip: `pre-commit` still runs inside the Docker environment, so local hook installation is optional rather than required.

> Tip: `pre-commit` runs a scoped codespell check over Markdown, YAML, Python, and English user-facing translation JSON. Add intentional project terms to `.codespellignore`.

> Tip: the `ha-runtime` service is for manual verification only. Keep automated checks on `ha-dev` so test and lint runs stay fast and deterministic.

> Tip: `ha-runtime` inherits the `TZ` environment variable from your shell and defaults to `UTC` when `TZ` is unset.

## Translations

- Place new or updated translations under `custom_components/enphase_ev/translations/<language>.json`.
- Follow Home Assistant’s translation conventions (language files use native phrasing and mirror English keys). When adding a new language, ensure the manifest `version` and documentation reflect the addition and include both the English and native language names where applicable.
- Keep JSON alphabetised and valid UTF-8 (ASCII is preferred unless the language requires accented characters).

## Documentation and Changelog

- Update `README.md` when behaviour, options, or supported features change. Include new configuration parameters using the recommended `configuration_basic` formatting when appropriate.
- For codebase orientation, see [`docs/architecture.md`](docs/architecture.md) and [`docs/glossary.md`](docs/glossary.md).
- Record user-facing changes in `CHANGELOG.md` under an `Unreleased` entry (or add a new version section when preparing a release).

## Tests

- Add or update tests in `tests/components/enphase_ev/` for new functionality or bug fixes.
- Ensure pytest remains fast and deterministic; prefer fixtures over network calls.
- If adding significant functionality, mention how you verified it in the PR description and consider attaching diagnostics captured via Home Assistant’s download tools.

## Pull Request Expectations

- Keep pull requests focused. Separate refactors from functional changes.
- Rebase on top of the latest `main` before requesting review to avoid merge conflicts.
- Ensure GitHub Actions workflows (`tests`, `hassfest`, `Validate`, and `Quality Scale`) pass. They mirror the commands listed above and help maintain HACS compliance (`hacs/action` requires the repository to meet manifest, structure, and metadata checks).
- For UI or translation changes, include screenshots or highlight impacted strings.

## Release Process

Maintainers cut releases by updating the manifest version, changelog, and tagging the release. For beta tags (for example `v2.0.0b4`), publish the GitHub release as a **pre-release** so default HACS users stay on the latest stable line (currently `v1.9.1`) unless they explicitly enable beta releases. Contributors generally do not publish releases directly, but you can help by keeping entries in `CHANGELOG.md` clear and actionable.

## Getting Help

Open a discussion or issue if you are blocked. When in doubt, reference:

- [Home Assistant developer documentation](https://developers.home-assistant.io/) for integration patterns and quality scale rules.
- [HACS documentation](https://hacs.xyz/docs/) for repository requirements, including manifests and validation expectations.

Thank you for contributing!

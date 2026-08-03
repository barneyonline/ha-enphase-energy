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
4. **Develop and test** your changes inside `ha-dev`.
   To reproduce the forward-compatibility lane against the pinned Home Assistant
   2026.8 beta, run:
   ```bash
   docker compose -f devtools/docker/docker-compose.yml build ha-2026-8
   docker compose -f devtools/docker/docker-compose.yml run --rm ha-2026-8 bash -lc "pytest -q tests/compatibility/test_ha_2026_8_device_registry.py tests/components/enphase_ev/test_init_module.py::test_async_setup_entry_updates_existing_device tests/components/enphase_ev/test_init_module.py::test_remove_legacy_site_device_preserves_real_devices_with_site_identifier tests/components/enphase_ev/test_init_module.py::test_remove_legacy_site_device_removes_empty_device_without_gateway tests/components/enphase_ev/test_services.py::test_services_route_evse_targets_to_owning_entry_with_site_only_entry"
   ```
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
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "ruff check ."
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "black custom_components/enphase_ev tests/components/enphase_ev"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python scripts/validate_quality_scale.py"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "mypy --strict --ignore-missing-imports --follow-imports=skip custom_components/enphase_ev"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest -q tests/components/enphase_ev"
```

## Coding Standards and Tooling

Home Assistant integrations must follow the [core development guidelines](https://developers.home-assistant.io/docs/development_guidelines/). Key points:

- Use modern Python syntax (f-strings, type hints) and keep logging format strings lazy.
- Interact with external services via dedicated client libraries or well-structured helpers.
- Use Google-style docstrings when detailed documentation is needed.
- Keep YAML and documentation formatting consistent with the [Home Assistant style guide](https://developers.home-assistant.io/docs/documenting/yaml-style-guide/).

This repository relies on the following checks. Please run them locally before pushing:

```bash
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "ruff check ."
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "black custom_components/enphase_ev tests/components/enphase_ev"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pytest -q tests/components/enphase_ev"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "python scripts/validate_quality_scale.py"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "mypy --strict --ignore-missing-imports --follow-imports=skip custom_components/enphase_ev"
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc "pre-commit run --all-files"
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

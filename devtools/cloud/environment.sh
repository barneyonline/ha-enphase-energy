#!/usr/bin/env bash
# Managed-cloud adapter for the repository's pinned Docker development lane.
set -euo pipefail
umask 077

repo_dir="${HA_CLOUD_REPO_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"
repo_dir="$(cd -- "$repo_dir" && pwd)"
state_dir="${HA_CLOUD_STATE_DIR:-/workspace/.managed-ha}"
case "$state_dir" in
  /*) ;;
  *) echo 'HA_CLOUD_STATE_DIR must be an absolute path on the Docker host' >&2; exit 2 ;;
esac
image=ha-enphase-energy-ha-dev:managed
action="${1:-start}"
if [ "$#" -gt 0 ]; then shift; fi
case "$action" in
  install|rebuild|start|check|settings) [ "$#" -eq 0 ] ;;
  compose) ;;
  *) echo 'Usage: environment.sh {install|rebuild|start|check|settings|compose [arguments...]}' >&2; exit 2 ;;
esac

if [ "$action" = settings ]; then
  python3 - "$repo_dir" "$state_dir" "${BASH_SOURCE[0]}" <<'PY'
import json
from pathlib import Path
import shlex
import sys

repo, state, script = map(Path, sys.argv[1:])
adapter = state / 'environment.sh'
prefix = f'HA_CLOUD_REPO_DIR={shlex.quote(str(repo))} HA_CLOUD_STATE_DIR={shlex.quote(str(state))}'
source = script.read_text()
delimiter = 'MANAGED_HA_CLOUD_ADAPTER'
assert delimiter not in source.splitlines()
install = f'''set -euo pipefail
umask 077
mkdir -p {shlex.quote(str(state))}
cat > {shlex.quote(str(adapter))} <<'{delimiter}'
{source}{delimiter}
{prefix} bash {shlex.quote(str(adapter))} install
'''
startup = f'''Read AGENTS.md and CONTRIBUTING.md in {repo} before working.
Use the pinned Docker lane for tests and quality checks. At session startup run:

{prefix} bash {shlex.quote(str(adapter))} start

This recreates ha-dev with current proxy defaults and CA trust, checks dependencies
and pinned versions, and builds only if the image is missing or its inputs changed.
Use the repository adapter instead when developing changes to the adapter itself.
For subsequent commands use:
{prefix} bash {shlex.quote(str(adapter))} compose exec -T ha-dev <command>

Run relevant tests for code changes; do not run the full suite as routine startup.
Start ha-runtime only for
requested manual UI verification. Do not wipe source, configuration, or caches
during normal startup. No live Enphase credentials are needed for mocked tests.
'''
print(json.dumps({'working_directory': str(repo), 'install_script': install, 'start_skill': startup}, indent=2))
PY
  exit
fi

mkdir -p "$state_dir/buildx"
export BUILDX_CONFIG="$state_dir/buildx"
dck() {
  env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
    docker --host=unix:///var/run/docker.sock "$@"
}
dc() {
  dck compose -p managed-ha -f "$repo_dir/devtools/docker/docker-compose.yml" \
    -f "$state_dir/compose.yml" "$@"
}

# Keep the repository Dockerfile unchanged. Supply current cloud CA trust only
# through build secrets and read-only runtime mounts, never through image layers.
python3 - "$repo_dir" "$state_dir" "${BASH_SOURCE[0]}" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys

repo, state, adapter = map(Path, sys.argv[1:])
docker_dir = repo / 'devtools/docker'
ha_pin = re.search(r'^homeassistant==([^\s#]+)', (docker_dir / 'requirements-dev.txt').read_text(), re.M)
if not ha_pin:
    raise SystemExit('Expected an exact Home Assistant version in requirements-dev.txt')
source = (docker_dir / 'Dockerfile').read_text()
first = 'RUN python -m pip install'
second = '    pip install --no-cache-dir'
if source.count(first) != 1 or source.count(second) != 1:
    raise SystemExit('Review changed Dockerfile before adapting cloud CA trust')
trust = 'PIP_CERT=/run/secrets/managed_ca SSL_CERT_FILE=/run/secrets/managed_ca REQUESTS_CA_BUNDLE=/run/secrets/managed_ca '
source = source.replace(first, 'RUN --mount=type=secret,id=managed_ca,required=true ' + trust + 'python -m pip install')
source = source.replace(second, '    ' + trust + 'pip install --no-cache-dir')
inputs = hashlib.sha256(source.encode() + adapter.read_bytes())
for path in sorted({*docker_dir.glob('requirements*.txt'), *docker_dir.glob('constraints*.txt')}):
    inputs.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
fingerprint = inputs.hexdigest()
ca = Path('/etc/ssl/certs/ca-certificates.crt')
if not ca.is_file():
    raise SystemExit('Managed cloud CA bundle is missing')
(state / 'Dockerfile').write_text(source)
(state / 'build-input.sha256').write_text(fingerprint + '\n')
runtime = {
    'volumes': [{'type': 'bind', 'source': str(ca), 'target': '/run/managed-ca.pem', 'read_only': True}],
    'environment': {
        'PIP_CERT': '/run/managed-ca.pem',
        'SSL_CERT_FILE': '/run/managed-ca.pem',
        'REQUESTS_CA_BUNDLE': '/run/managed-ca.pem',
        'GIT_SSL_CAINFO': '/run/managed-ca.pem',
        'CURL_CA_BUNDLE': '/run/managed-ca.pem',
    },
}
config = {
    'services': {
        'ha-dev': {
            **runtime,
            'image': 'ha-enphase-energy-ha-dev:managed',
            'build': {
                'dockerfile': str(state / 'Dockerfile'),
                'secrets': ['managed_ca'],
                'labels': {'io.barneyonline.ha-dev.inputs': fingerprint},
            },
        },
        'ha-runtime': {**runtime, 'image': f'ghcr.io/home-assistant/home-assistant:{ha_pin[1]}'},
    },
    'secrets': {'managed_ca': {'file': str(ca)}},
}
(state / 'compose.yml').write_text(json.dumps(config, indent=2) + '\n')
PY

check_runtime() {
  dc exec -T ha-dev python -m pip check
  dc exec -T ha-dev python - <<'PY'
from importlib import metadata
from pathlib import Path
import re
import sys

docker_dir = Path('/workspace/devtools/docker')
requirements = (docker_dir / 'requirements-dev.txt').read_text()
python_pin = re.search(r'^FROM python:(\d+)\.(\d+)-slim\b', (docker_dir / 'Dockerfile').read_text(), re.M)
assert python_pin, 'Expected an explicit Python major/minor in the Dockerfile'
assert sys.version_info[:2] == tuple(map(int, python_pin.groups())), sys.version
for package in ('homeassistant', 'pytest-homeassistant-custom-component'):
    pin = re.search(rf'^{re.escape(package)}==([^\s#]+)', requirements, re.M)
    assert pin, f'Missing exact {package} pin'
    actual = metadata.version(package)
    assert actual == pin[1], f'{package}: installed {actual}, expected {pin[1]}'
    print(f'{package} {actual}')
import custom_components.enphase_ev
print(sys.version)
PY
}

if [ "$action" = compose ]; then
  dc "$@"
  exit
fi
dck info --format 'Docker {{.ServerVersion}} ({{.Driver}})'
dc config --quiet
case "$action" in
  install) dc build ha-dev ;;
  rebuild) dc build --pull --no-cache ha-dev ;;
  start)
    expected="$(cat "$state_dir/build-input.sha256")"
    actual="$(dck image inspect "$image" --format '{{ index .Config.Labels "io.barneyonline.ha-dev.inputs" }}' 2>/dev/null || true)"
    if [ "$actual" != "$expected" ]; then
      dc build ha-dev
    fi
    ;;
  check) check_runtime; exit ;;
esac
# Recreate at session startup so Docker injects this session's proxy defaults.
dc up -d --no-build --force-recreate ha-dev
check_runtime

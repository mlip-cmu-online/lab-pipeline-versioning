#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

source .venv/bin/activate
bash scripts/smoke_environment.sh

cat <<'EOF'

Lab 9 environment ready.
Activate it in a new shell with: source .venv/bin/activate
Then begin with the Setup checks in README.md.
EOF

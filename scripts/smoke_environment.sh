#!/usr/bin/env bash
set -Eeuo pipefail

expected_dvc="3.67.1"
expected_roar="0.4.3"
with_glaas=false

case "${1:-}" in
  "") ;;
  --with-glaas) with_glaas=true ;;
  *)
    echo "Usage: $0 [--with-glaas]" >&2
    exit 2
    ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

actual_dvc="$(dvc --version)"
actual_roar="$(roar --version | awk '{print $NF}')"
test "$actual_dvc" = "$expected_dvc" || {
  echo "Expected DVC $expected_dvc, found $actual_dvc" >&2
  exit 1
}
test "$actual_roar" = "$expected_roar" || {
  echo "Expected Roar $expected_roar, found $actual_roar" >&2
  exit 1
}

smoke_dir="$(mktemp -d -t mlip-lab9-smoke.XXXXXX)"
cleanup() {
  chmod -R u+w "$smoke_dir" 2>/dev/null || true
  rm -r -- "$smoke_dir"
}
trap cleanup EXIT

cp -R "$repo_root/data" "$repo_root/scripts" "$smoke_dir/"
cp "$repo_root/.gitignore" "$repo_root/dvc.yaml" "$repo_root/params.yaml" "$smoke_dir/"

cd "$smoke_dir"
git init --quiet
git config user.name "Lab environment smoke test"
git config user.email "lab-smoke@example.invalid"
git add -A
git commit --quiet -m "Create disposable smoke-test fixture"

export DO_NOT_TRACK=1

echo "==> Testing DVC $actual_dvc"
dvc init --quiet
dvc repro
dvc exp show --no-pager
test -f dvc.lock
test -f metrics/scores.json
git add -A
git commit --quiet -m "Record disposable DVC outputs"

echo "==> Testing Roar $actual_roar"
roar init --yes
roar config set filters.ignore_tmp_files false
roar tracer
test -z "$(git status --porcelain)" || {
  echo "Roar requires a clean Git worktree before 'roar run'." >&2
  git status --short >&2
  exit 1
}
roar run python3 scripts/preprocess.py
roar run python3 scripts/train.py
roar run python3 scripts/evaluate.py
roar dag
roar show @3

echo "==> Testing GLaaS registration preparation"
roar register --dry-run models/classifier.pkl

if $with_glaas; then
  echo "==> Testing authenticated GLaaS registration"
  roar auth status
  roar register --private --yes models/classifier.pkl
else
  if roar auth status; then
    echo "GLaaS credentials are valid; authenticated registration was not requested."
  else
    echo "No valid GLaaS authentication is active; review the server and credential status above."
    echo "Run 'roar auth login', then rerun '$0 --with-glaas' to test registration."
  fi
fi

echo "Environment smoke test passed."

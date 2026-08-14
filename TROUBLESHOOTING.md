# Troubleshooting

Common issues and how to fix them. Check here if you get stuck during the lab.

---

### A1: `roar run` — "failed to spawn traced command: No such file or directory"

**Symptom**: You run `roar run python scripts/preprocess.py` and get:
```
roar-tracer-preload: failed to spawn traced command: No such file or directory (os error 2)
Exit code: 1
Duration: 0.0s
```

**Cause**: On macOS, `python` often does not exist — only `python3` does. Roar's tracer tries to exec the exact command you give it.

**Fix**: Use `python3` instead:
```bash
roar run python3 scripts/preprocess.py
```

---

### A2: `roar run` — exits with code 1, Duration 0.0s, no file I/O captured

**Symptom**: Roar says the command completed but with `Exit code: 1`, `Duration: 0.0s`, and no Read/Written files listed.

**Cause**: The traced command crashed immediately. Common reasons:
- Wrong Python binary (see A1)
- Missing dependencies (forgot `pip install -r requirements.txt`)
- Running outside the project directory

**Fix**: First verify the script runs on its own: `python3 scripts/preprocess.py`. If that works but `roar run` doesn't, check A1 and A3.

---

### A3: macOS — Roar captures no file events (SIP)

**Symptom**: `roar run` completes with `Exit code: 0` but lists no Read/Written files.

**Cause**: macOS System Integrity Protection (SIP) blocks Roar's tracer on Apple-signed binaries under `/usr/bin/`. If your `python3` is `/usr/bin/python3`, SIP silently prevents file I/O observation.

**Fix**: Use a non-Apple Python. Check with:
```bash
which python3
```

If it shows `/usr/bin/python3`, install Python via one of:
- **Homebrew**: `brew install python3` (uses `/opt/homebrew/bin/python3`)
- **python.org installer**: downloads to `/Library/Frameworks/Python.framework/...`
- **pyenv**: `pyenv install 3.12`
- **conda**: `conda create -n lab python=3.12`

If you're using a virtual environment (recommended), the venv's `python3` will not be SIP-protected.

Alternatively, you can run this lab on Linux using your team's VM, where SIP is not an issue.

---

### A4: `roar run` — "Git repo has uncommitted changes"

**Symptom**:
```
Error: Git repo has uncommitted changes: M dvc.lock M metrics/scores.json
```

**Cause**: Roar requires a clean Git working directory before running. This is by design — it records the Git commit as part of lineage.

**Fix**: Commit or stash your changes first:
```bash
git add -A
git commit -m "Commit before Roar run"
```

Check that the worktree is now clean before retrying:

```bash
git status --short
```

---

### A5: `roar dag` — "No active session"

**Symptom**: Running `roar dag` says "No active session. Run 'roar init' or 'roar run' first."

**Cause**: Either Roar was never initialized, or all previous `roar run` commands failed (exit code 1), so no successful session exists.

**Fix**:
1. Make sure you ran `roar init`
2. Ensure at least one `roar run` completed with `Exit code: 0`
3. If you previously deleted `.roar/`, run `roar init` again

---

### A6: DVC — "output is already tracked by SCM (e.g. Git)"

**Symptom**: Running `dvc add data/raw/data.csv` gives:
```
ERROR: output 'data/raw/data.csv' is already tracked by SCM (e.g. Git).
```

**Cause**: The file is tracked by Git. DVC cannot manage a file that Git is also tracking.

**Fix**: Remove the file from Git tracking (but keep it on disk), then add to DVC:
```bash
git rm -r --cached data/raw/data.csv
git commit -m "Untrack data.csv from Git"
dvc add data/raw/data.csv
git add data/raw/data.csv.dvc data/raw/.gitignore
git commit -m "Track data.csv with DVC"
```

---

### A7: DVC — "No such file or directory" during `dvc repro` or `dvc exp run`

**Symptom**: `dvc repro` fails because a data file doesn't exist on disk.

**Cause**: The file was removed from disk, or you're in a detached HEAD state where the data file wasn't committed.

**Fix**: Ensure the raw data file is present:
```bash
ls data/raw/data.csv
```

If missing, restore it:
```bash
dvc checkout          # if tracked by DVC
git checkout HEAD -- data/raw/data.csv   # if tracked by Git
```

---

### A8: DVC experiments — metrics show `!` or are missing

**Symptom**: `dvc exp show` displays `!` instead of metric values.

**Cause**: `metrics/scores.json` is not being found or is in the DVC cache instead of being readable.

**Fix**: In your `dvc.yaml`, ensure the metrics output has `cache: false`:
```yaml
metrics:
  - metrics/scores.json:
      cache: false
```

---

### A9: Windows — Roar not available

**Symptom**: `pip install roar-cli` fails on Windows, or `roar run` is not available.

**Cause**: Roar does not yet have native Windows support.

**Fix**: Use WSL2 or your team's VM:
1. Open PowerShell as Administrator and run: `wsl --install`
2. Restart your computer
3. Open the Ubuntu terminal from the Start menu
4. Do the entire lab inside WSL (clone, install, run everything there)

Alternatively, you can run this lab on Linux using your team's VM, where Roar works natively.

The supplied Codespace/DevContainer is the recommended Linux environment and avoids this platform mismatch.

---

### A10: `dvc exp run` — experiment fails but `dvc repro` works

**Symptom**: `dvc repro` runs fine, but `dvc exp run` fails with a file-not-found error.

**Cause**: `dvc exp run` creates a temporary workspace (a copy of your repo). If a file that your pipeline needs is neither committed to Git nor tracked by DVC, it won't exist in that workspace.

**Fix**: Make sure all input files are either:
- Committed to Git (small files like `params.yaml`, scripts)
- Tracked by DVC (large files like datasets)

```bash
git add params.yaml scripts/
git commit -m "Ensure all pipeline inputs are committed"
```

---

### A11: GLaaS authentication or registration fails

**Symptom**: `roar auth status` reports that GLaaS is reachable but you are not authenticated, or `roar register` is rejected.

**Cause**: GLaaS is an external, GitHub-authenticated service. Container setup cannot create account credentials for you, and credentials from one Codespace are not automatically available in another.

**Fix**:

1. Run `roar auth login` and complete the browser flow with your GitHub account.
2. Run `roar auth status` and confirm that the active method is authenticated.
3. Preview with `roar register --dry-run models/classifier.pkl`.
4. Register with `roar register models/classifier.pkl`.

Never paste a private SSH key or authentication token into the repository.

---

### A12: Roar cannot select a tracing backend

**Symptom**: `roar run` reports that no tracer is ready, or completes without observing the pipeline's input and output files.

**Cause**: Roar must observe file I/O through preload, eBPF, or ptrace. Native Windows is unsupported, macOS system binaries can block preload tracing, and a restricted Linux container may disable some backends.

**Fix**: Run `roar tracer` to inspect backend readiness. Use the supplied Codespace/DevContainer, where preload or ptrace should be ready, and rerun `bash scripts/smoke_environment.sh` before continuing the lab.

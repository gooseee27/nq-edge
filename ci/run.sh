#!/usr/bin/env bash
# One NQ Edge scan on GitHub: load the saved data, scan, refresh the dashboard numbers, save the data again.
# The saved data lives on the `data` branch, which is replaced by a single new commit every run
# (so the repository does not grow by a copy of the data every 5 minutes).
set -uo pipefail

PY="${PYTHON:-python}"
REPO_URL="${NQ_REPO_URL:-https://x-access-token:${GH_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"

rm -rf data_branch
if git ls-remote --exit-code --heads "$REPO_URL" data >/dev/null 2>&1; then
  git clone --quiet --depth 1 --branch data "$REPO_URL" data_branch
else
  mkdir data_branch
  git -C data_branch init -q
  git -C data_branch remote add origin "$REPO_URL"
fi
mkdir -p data_branch/data
export NQ_EDGE_DATA="$PWD/data_branch/data"

# Trade rules live in the repo (config/paper_settings.json), so editing the file there changes the rules.
cp config/paper_settings.json "$NQ_EDGE_DATA/paper_settings.json"

# 1. The scan: settles open trades, looks for new setups, updates the market read.
"$PY" scan.py || echo "scan reported a problem (details are in data/run_log.txt)"

# 2. The gamma snapshot: twice a day during US market hours (New York time), or when asked for by hand.
if [ "${NQ_ACTION:-scan}" = "snapshot" ] || [ "$("$PY" ci/snapshot_due.py)" = "yes" ]; then
  "$PY" logger.py || echo "snapshot reported a problem (details are in data/run_log.txt)"
  "$PY" scan.py || true      # scan again so the market read uses the new levels
fi

# 2b. When asked for by hand: redo the "do the levels matter" and experiments numbers right now.
if [ "${NQ_ACTION:-scan}" = "refresh-all" ]; then
  "$PY" -c "import reactions, experiments; reactions.run(); experiments.run()" \
    || echo "refresh reported a problem (details are in data/run_log.txt)"
fi

# 3. The numbers the hosted page reads.
"$PY" export_site.py state data_branch || echo "could not write state.json"

# 4. Save. The phone alert topic must never be published.
rm -f "$NQ_EDGE_DATA/notify.json"
cd data_branch
printf 'data/notify.json\n__pycache__/\n' > .gitignore
git config user.name "nq-edge-bot"
git config user.email "nq-edge-bot@users.noreply.github.com"
git symbolic-ref HEAD refs/heads/publish
git add -A
git commit -q -m "NQ Edge data $(date -u +%Y-%m-%dT%H:%MZ)"
for attempt in 1 2 3; do
  git push -q --force origin publish:data && break
  echo "push failed (attempt $attempt), retrying"; sleep 5
done
exit 0   # never fail the run: a red run emails the repo owner every 5 minutes. Problems show on the dashboard.

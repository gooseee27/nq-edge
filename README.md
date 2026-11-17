# NQ Edge

Gamma levels from the QQQ options chain, mapped to NQ, plus an orderflow proxy and a paper trader. Free (delayed) Yahoo data. Paper trading only.

## How it runs on GitHub

- `.github/workflows/scan.yml` runs `ci/run.sh` every 5 minutes over the US and Asia sessions (Monday to Friday UTC).
- Each run loads the saved data from the `data` branch, scans, takes the twice-a-day gamma snapshot when due, and saves the data again. The `data` branch is replaced by one new commit every run, so the repo does not grow.
- `docs/index.html` is a read-only copy of the dashboard (GitHub Pages, from the `main` branch, `/docs` folder). It loads `state.json` from the `data` branch.

## The buttons on the hosted page

GitHub Pages is a static page and cannot run Python, so the buttons open the right GitHub page instead:

- Scan now, Take snapshot and the two Update buttons open "Run workflow". Pick `scan`, `snapshot` or `refresh-all`, then press the green button. The page refreshes about a minute later.
- Trade rules opens `config/paper_settings.json` for editing. Phone alerts opens the Actions secrets page.

## Changing the trade rules

Edit `config/paper_settings.json` on the `main` branch (the pencil icon on GitHub). The next scan uses the new rules. Open trades keep their old stop and target.

## Phone alerts

Add a repository secret named `NTFY_TOPIC` (Settings, Secrets and variables, Actions) with your ntfy topic. The topic works like a password, so it is never written into the repo or the published data.

## If it stops

- Open the Actions tab and click "Run workflow" on "NQ Edge scan". GitHub switches off scheduled workflows in a repo with no activity for 60 days.
- Yahoo sometimes blocks cloud servers. The dashboard then shows delayed prices and `data/run_log.txt` on the `data` branch shows the reason.

## Rebuilding the page

`python export_site.py page docs` after changing `dashboard.html`.

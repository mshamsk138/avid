# Publishing checklist — 0.1.0a2

Nothing in this checklist has been done by the release preparation: the repository is not public, no
tag was pushed, no release was published and nothing was uploaded to PyPI. Each step is for the
project owner. Items marked **owner decision** must not be guessed.

## 1. Owner decisions (before anything is public)

- [x] **Copyright holder:** Mehdi Shams (confirmed by owner); MIT license retained.
- [ ] **Private security and conduct contact:** owner supplied `m.shams3141`, which is not a complete
      email address. Confirm a deliverable address or another functioning private route before going public.
      Enable GitHub private vulnerability reporting as documented in `SECURITY.md`.
- [x] **Initial maintainer:** Mehdi Shams ([@mshamsk138](https://github.com/mshamsk138)); additional reviewers
      and optional CODEOWNERS can be appointed later.
- [x] **Repository owner/name:** `mshamsk138/avid`; `[project.urls]` and README CI badge URL are populated.
      Keep the badge commented until the first GitHub CI run actually succeeds.
- [ ] Whether to publish on **PyPI** at all, and under which name (`avid-rebalancer` is the package name;
      its availability on PyPI was not checked).
- [ ] Legal review of publishing a trading tool for Nobitex in your jurisdiction (sanctions, licensing).

## 2. Secrets and history

- [ ] `python3 -I scripts/check-secrets.py .` -> 0 findings (done for this tree, see
      [audits/TEST_EVIDENCE.md](audits/TEST_EVIDENCE.md)).
- [ ] If you import an existing git history: `git log -p --all | python3 -I scripts/check-secrets.py -`.
      A key that was ever committed must be **revoked and rotated** (Nobitex panel; re-pair the agent),
      not only deleted.
- [ ] No `data/`, `state.json`, `*.jsonl`, `outbox.sqlite3`, `~/.nobitex` content anywhere.

## 3. Create the repository (private first)

```bash
cd avid
git init -b main
git add -A
python3 -I scripts/check-secrets.py .
git commit -m "AVID 0.1.0a2"
gh repo create mshamsk138/avid --private --source . --push        # private until section 6 is done
```

Never force-push `main` after it is shared; never move or rewrite a published tag.

## 4. Repository settings

- [ ] Description: "Auditable fixed-weight rebalancer and private agent for Nobitex (alpha, simulation by default)".
- [ ] Topics: `rebalancing`, `portfolio-rebalancing`, `nobitex`, `trading-bot`, `cryptocurrency`,
      `risk-management`, `fail-safe`, `python`, `systemd`, `offline-simulation`, `audit`.
- [ ] Social preview: export `docs/assets/avid-logo.svg` to PNG (1280 × 640 canvas, navy background).
- [ ] Features: Issues on; Discussions optional; Wiki off (docs live in the repository).
- [ ] Pull requests: squash merge only; delete head branches; "Always suggest updating PR branches".
- [ ] Actions: allow only actions pinned to a full SHA if your plan supports it; workflow permissions
      **read-only** by default; "Allow GitHub Actions to create and approve pull requests" off;
      require approval for workflows from outside collaborators.

## 5. Branch protection / ruleset for `main`

- [ ] Require a pull request with at least 1 approval; dismiss stale approvals; require review from a
      maintainer for safety-relevant paths (GOVERNANCE.md).
- [ ] Required status checks (job names as GitHub shows them): `syntax, docs links, scripts`, the eight
      `tests (py3.1x, <signer> signer)` jobs, `build, install the wheel, CLI smoke test`,
      `secret scan, dependency audit`, `CodeQL (python)`, `CodeQL (actions)`,
      `dependency review (pull requests)`.
- [ ] Require branches to be up to date; require linear history; block force pushes and deletion.
- [ ] Tag ruleset for `v*`: only maintainers create; no update, no deletion.

## 6. Security features

- [ ] Private vulnerability reporting: on.
- [ ] Dependabot alerts and security updates: on (`.github/dependabot.yml` covers actions and pip).
- [ ] Secret scanning and push protection: on.
- [ ] Code scanning: CodeQL via `.github/workflows/codeql.yml`.
- [ ] First CI run is green on all matrix legs (records PRA-12 / PRA-14 as verified - update
      [audits/RELEASE_READINESS.md](audits/RELEASE_READINESS.md)).

## 7. Release 0.1.0a2

- [ ] CHANGELOG: change "(unreleased)" to the date; `docs/releases/v0.1.0a2.md` final.
- [ ] `git tag -s v0.1.0a2 -m "AVID 0.1.0a2"` (signed) and `git push origin v0.1.0a2`.
- [ ] The `release` workflow builds sdist, wheel, source archive, SBOM and `SHA256SUMS`, attests build
      provenance and opens a **draft** pre-release. Compare its `SHA256SUMS` with a local
      `python3 scripts/build-dist.py` of the same commit (sdist/wheel bytes can differ by timestamps;
      the file lists must match).
- [ ] Review the draft (notes, files, "pre-release" ticked), then publish it by hand.
- [ ] Make the repository public only after sections 1-6 are done.

## 8. PyPI (optional, owner decision)

- [ ] On PyPI, add a **pending trusted publisher**: project `avid-rebalancer`, owner/repo, workflow
      `release.yml`, environment `pypi`.
- [ ] In GitHub, create the environment `pypi` with required reviewers.
- [ ] Run the `release` workflow manually with `tag = v0.1.0a2` and `publish_to_pypi = true`; approve
      the environment. No API token is stored anywhere.
- [ ] Check the project page: README relative links do not resolve on PyPI (KNOWN_ISSUES 17) - add
      `[project.urls]` first.

## 9. After publication

- [ ] Run the Nobitex contract tests on the official testnet and record the result in
      [audits/TEST_EVIDENCE.md](audits/TEST_EVIDENCE.md).
- [ ] Watch the weekly `long-fuzz` workflow; a red run is a release blocker for the next version.

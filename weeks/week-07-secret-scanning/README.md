DevSecOps Project – Week 7 (CDK)
Secret Scanning (Gitleaks)
Overview

CDK port of the Terraform sibling's Week 7 —
[`devsecops-bootcamp/weeks/week-07-secret-scanning/README.md`](https://github.com/adenoch1/devsecops-bootcamp/blob/main/weeks/week-07-secret-scanning/README.md).
Unlike Week 6 (an account-level singleton, not ported here by design),
this is per-repo CI configuration — genuinely duplicable, same as every
Week 5 stage.

What Changed

`.github/workflows/02-security.yml` gains a third job, `gitleaks`,
identical to the Terraform sibling's: `gitleaks/gitleaks-action@v2` with
`fetch-depth: 0` on checkout, scanning full git history on every PR
rather than just the diff.

License note: free for this repo too — public, personal account, no
GitHub organization involved.

Verification before enabling: ran the `gitleaks` CLI directly against
this repo's full commit history locally before pushing
(`gitleaks detect --source . --log-opts="--all"`) — clean, no leaks
(12 commits scanned).

What Was Achieved in Week 7

✔ Every PR now scans full git history for hardcoded secrets, not just
  the diff — same coverage as the Terraform sibling
✔ Verified against real repo history before enabling in CI

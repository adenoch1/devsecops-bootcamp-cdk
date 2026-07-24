DevSecOps Project – Week 10 (CDK)
Dynamic Application Security Testing (OWASP ZAP)
Overview

CDK port of the Terraform sibling's Week 10 —
[`devsecops-bootcamp/weeks/week-10-dast-zap/README.md`](https://github.com/adenoch1/devsecops-bootcamp/blob/main/weeks/week-10-dast-zap/README.md).
Genuinely identical this time — this is app-layer work (`app/app.py`,
`docker/Dockerfile`), not infrastructure, and the two repos' Flask apps
share the same structure (same lack of third-party resources/inline
scripts in `templates/index.html`, confirmed before assuming the same
header policy was safe here too).

What Changed

`.github/workflows/05-dast-zap.yml` (new): identical to the Terraform
sibling's — builds this repo's `docker/Dockerfile` image, runs it
locally in the CI job, waits for `/health`, then runs
`zaproxy/action-baseline` against `http://localhost:5000`. Same
reasoning for scanning a disposable local container rather than the live
app: passive-only baseline scan, but no reason to risk this project's
own CloudWatch alarms or GuardDuty on a routine PR check when a local
copy of the exact same image tests identically.

`app/app.py` gains the same `after_request` security-headers hook as the
Terraform sibling — verified independently against this repo's own
build, not assumed identical just because the code looks the same:

```
FAIL-NEW: 0    WARN-NEW: 0    IGNORE: 1    PASS: 66
```

Same six real findings fixed, same one informational finding
(`Non-Storable Content`, from `Cache-Control: no-store` working
correctly) allowlisted in `.zap/rules.tsv` with the reasoning written
inline — see the Terraform sibling's Week 10 notes for the full
before/after scan output and the two-pass fix detail (CSP directives
with no fallback to `default-src`; the COEP/COOP/CORP trio needing all
three headers, not just COEP).

What Was Achieved in Week 10

✔ Same dynamic security testing coverage as the Terraform sibling,
  independently verified against this repo's own build rather than
  assumed from parity
✔ 6 real findings fixed, 1 non-issue allowlisted with a written reason
✔ Full test parity between both repos' apps

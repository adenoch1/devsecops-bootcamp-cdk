DevSecOps Project – Week 11 (CDK)
Written Threat Model — Not Duplicated Here, By Design
Overview

The Terraform sibling's Week 11 is a full STRIDE threat model:
[`devsecops-bootcamp/THREAT-MODEL.md`](https://github.com/adenoch1/devsecops-bootcamp/blob/main/THREAT-MODEL.md).
It is **not copied into this repo** — the threat model analyzes the
*architecture* (ALB/WAF, ECS Fargate, the CI/CD pipeline, the account-level
security baseline), and that architecture is the same regardless of which
IaC tool deployed it. A second copy here would drift from the canonical
one over time for no benefit; this repo's `weeks/` folders already point
at the Terraform sibling's docs whenever content is genuinely shared
rather than CDK-specific (see Week 06's note for the same pattern).

**One real distinction worth stating explicitly**, called out in the
Terraform sibling's Residual Risks section too: this repo's stacks are
currently **not deployed** (no live infrastructure exists here right now
— see `04-cdk-deploy.yml`, manual `workflow_dispatch` only). Every
mitigation this repo implements (WAFv2, security groups, least-privilege
IAM, X-Ray, VPC endpoints, blue/green, secrets management, ZAP-verified
security headers) has been verified **as designed** — `cdk synth` clean,
`cdk-nag` clean, tests passing — but not **as running** the way the
Terraform sibling's have been (including surviving and recovering from a
real production incident this session). If this repo is ever deployed
live again, that's the gap to close first: verify the designed
mitigations actually hold under a real running system, the same way the
Terraform side already has.

What Was Achieved in Week 11

✔ Explicit scope decision, matching this repo's own established pattern:
  share analysis that's genuinely architecture-level, don't duplicate it
✔ The one real CDK-specific caveat (design-verified vs. run-verified)
  named directly rather than left implicit

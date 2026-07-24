DevSecOps Project – Week 9 (CDK)
Real Secrets Management
Overview

CDK port of the Terraform sibling's Week 9 —
[`devsecops-bootcamp/weeks/week-09-secrets-management/README.md`](https://github.com/adenoch1/devsecops-bootcamp/blob/main/weeks/week-09-secrets-management/README.md).
Same secret (Flask's session/CSRF-signing key), same non-contrived
reasoning for why it's the first real one — but a genuinely different
AWS service, for a specific, documented CDK-ecosystem reason rather than
a shortcut.

**This one uses AWS Secrets Manager, not SSM Parameter Store**, unlike
the Terraform sibling. Why: inspecting CDK's `ssm.StringParameter` L2
construct directly (`help(StringParameter.__init__)`) confirms it can
only create plaintext `String` type parameters — there's no way to
create a `SecureString` through it at all. Getting a real SSM
SecureString with "generate the value once, keep it stable across
redeploys" behavior from CDK would require a custom Lambda-backed custom
resource. `secretsmanager.Secret`'s `generate_secret_string` gives
exactly that behavior natively — generated once at creation, untouched
by subsequent `cdk deploy` runs — with no custom Lambda to write and
maintain.

What Changed

`cdk/stacks/ecs_stack.py`:

1. **A dedicated KMS key** (`FlaskSecretKmsKey`) — matches this stack's
   existing per-purpose key convention (the WAF logs key is separate
   too).
2. **`secretsmanager.Secret`** with `generate_secret_string` — CDK/
   CloudFormation generates the random value once at creation via its
   own custom-resource machinery; nothing in this codebase ever sees or
   types the value.
3. **`flask_secret.grant_read(self.ecs_task_execution_role)`** — one
   call grants both `secretsmanager:GetSecretValue`/`DescribeSecret` and
   the matching KMS decrypt permission. Notably simpler than the
   Terraform sibling's hand-written IAM policy document for the SSM
   equivalent — a genuine CDK L2 convenience.
4. **`secrets={"FLASK_SECRET_KEY": ecs.Secret.from_secrets_manager(flask_secret)}`**
   on the app container — distinct from `environment`, fetched and
   decrypted by the execution role before the container starts.

`app/app.py`: identical to the Terraform sibling —
`app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-dev-only-not-a-real-secret")`,
verified clean with `bandit -ll -ii`.

cdk-nag note: `AwsSolutions-SMG4` (secret has no rotation schedule) is
suppressed with a specific reason, not hand-waved away: no AWS-provided
rotation Lambda template exists for a generic application signing key
(unlike RDS/DocumentDB credentials, which ship with one) — a real
rotation Lambda would need to be hand-written. More fundamentally, ECS
`secrets` values are read once at container startup, not live, so
meaningful rotation would also require coordinating an ECS service
redeploy as part of the rotation Lambda's post-rotation step —
disproportionate complexity for a key no route in this app currently
uses for anything session-dependent.

**Honest cost note**: Secrets Manager bills ~$0.40/secret/month — a
real, small cost the Terraform sibling's SSM standard-tier parameter
doesn't have (SSM standard parameters are free; both sides pay for the
KMS key regardless). Worth knowing rather than assuming both ports cost
the same.

What Was Achieved in Week 9

✔ A real, non-contrived secret, generated once and never typed by a
  human, same as the Terraform sibling
✔ A genuinely different, well-reasoned AWS service choice — documented
  why, not silently diverged
✔ Least-privilege IAM via a single `grant_read()` call
✔ `cdk synth` clean — 0 errors after the documented SMG4 suppression

DevSecOps Project – Week 8 (CDK)
Supply-Chain Security: SBOM + Cosign Image Signing
Overview

CDK port of the Terraform sibling's Week 8 —
[`devsecops-bootcamp/weeks/week-08-sbom-cosign/README.md`](https://github.com/adenoch1/devsecops-bootcamp/blob/main/weeks/week-08-sbom-cosign/README.md).
Same tools, same reasoning; the one real difference is how the image
digest gets captured, covered below.

What Changed

`.github/workflows/04-cdk-deploy.yml`'s `build-and-push-image` job gains
the same four steps as the Terraform sibling's `03-release.yml`: install
cosign, generate an SBOM (Syft, SPDX-JSON) against the pushed image,
attach it as an in-toto attestation, sign the image keyless (Sigstore
Fulcio + Rekor, this workflow run's own GitHub Actions OIDC identity),
then a self-verification step (`cosign verify`).

**The one real difference from the Terraform port**: this repo's image
build step is a plain `docker build` / `docker push` (not
`docker/build-push-action` the way the Terraform sibling's is), which
doesn't expose a digest as a step output the same way. Read it back
instead, right after the push:

```bash
DIGEST_REF="$(docker inspect --format='{{index .RepoDigests 0}}' "$REGISTRY/$REPOSITORY:$IMAGE_TAG")"
```

`docker inspect`'s `RepoDigests` field is populated after a push
completes, giving the exact `registry/repo@sha256:...` reference cosign
needs — same effect as the Terraform side's action output, different
mechanism because the underlying build tool differs.

IAM note: no new permissions needed here either — `docker push` and
`cosign sign`/`attest`'s additional manifest pushes both go through the
same `github-ecr-role` this repo already shares with the Terraform
sibling for ECR access.

What Was Achieved in Week 8

✔ Every image this pipeline builds has a real, queryable SBOM attached in
  the registry
✔ Every image is signed with a verifiable, keyless signature tied to the
  exact workflow run that built it
✔ A self-verification step in the same job
✔ Zero new secrets to manage (keyless signing)
✔ Full parity with the Terraform sibling, with the digest-capture
  difference documented rather than silently worked around

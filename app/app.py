from flask import Flask, jsonify, render_template
from werkzeug.exceptions import HTTPException
from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.ext.flask.middleware import XRayMiddleware
import json
import logging
import os
import sys

app = Flask(__name__)

# Flask's own session/CSRF-signing key — the one secret every real Flask
# app has, whether or not it's actively using sessions yet. Injected via
# ECS's native `secrets` container-definition field (Week 9), which pulls
# straight from SSM Parameter Store (SecureString, KMS-encrypted) into the
# task's environment at startup — the value never touches the CDK template,
# CloudFormation, or application code. The literal fallback here only ever
# runs locally: every real deployment always sets FLASK_SECRET_KEY, so
# this default is never reachable outside a laptop.
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-dev-only-not-a-real-secret")

# Build/deploy metadata injected by CI as env vars (see CDK stack / workflow).
# Defaults keep local runs working without any setup.
BUILD_INFO = {
    "service": os.getenv("SERVICE_NAME", "devsecops-bootcamp-cdk"),
    "environment": os.getenv("APP_ENV", "local"),
    "git_sha": os.getenv("GIT_SHA", "dev"),
    "image_tag": os.getenv("IMAGE_TAG", "local"),
    "built_at": os.getenv("BUILD_TIME", "unknown"),
}

# X-Ray daemon runs as a sidecar container in the same ECS task (awsvpc mode
# shares the network namespace), so 127.0.0.1:2000 reaches it in every real
# environment. Locally, with no daemon listening, trace segments are just
# UDP sends into the void — harmless, no crash, no daemon required for dev.
xray_recorder.configure(
    service=BUILD_INFO["service"],
    daemon_address=os.getenv("AWS_XRAY_DAEMON_ADDRESS", "127.0.0.1:2000"),
    context_missing="LOG_ERROR",
)
XRayMiddleware(app, xray_recorder)


class JsonLogFormatter(logging.Formatter):
    """Structured JSON logs so CloudWatch metric filters can match on `level`."""

    def format(self, record):
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "service": BUILD_INFO["service"],
            "environment": BUILD_INFO["environment"],
            "git_sha": BUILD_INFO["git_sha"],
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonLogFormatter())
app.logger.handlers = [_handler]
app.logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    app.logger.error("Unhandled exception: %s", exc, exc_info=exc)
    return jsonify(status="error"), 500


# Week 10: response headers a ZAP baseline scan flagged as missing (see
# weeks/week-10-dast-zap/README.md for the actual scan output). Safe to set
# unconditionally: this app serves no third-party resources and no per-user
# sensitive content, so there's no compatibility cost to a strict policy.
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # base-uri/form-action/frame-ancestors don't fall back to default-src
    # per the CSP spec — each needs its own directive or a scanner (rightly)
    # flags the policy as incomplete.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # The cross-origin-isolation trio — COEP alone isn't sufficient without
    # CORP; browsers expect all three set consistently.
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    # ZAP's HSTS check only flags this over HTTPS, so it didn't fire against
    # the plain-HTTP container this scan runs against locally/in CI — but
    # this same code also serves real traffic through the ALB's HTTPS
    # listener in every deployed environment, where it matters for real.
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.route("/health")
def health():
    return jsonify(status="ok"), 200


@app.route("/version")
def version():
    return jsonify(BUILD_INFO), 200


@app.route("/")
def index():
    return render_template("index.html", build=BUILD_INFO)


if __name__ == "__main__":
    # Secure default: bind only to localhost
    host = os.getenv("FLASK_HOST", "127.0.0.1")

    # Container environments require binding to all interfaces
    if os.getenv("ENV") == "container":
        host = "0.0.0.0"  # nosec B104 - required to expose Flask from Docker container

    port = int(os.getenv("FLASK_PORT", "5000"))

    app.run(host=host, port=port)

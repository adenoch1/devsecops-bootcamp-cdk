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

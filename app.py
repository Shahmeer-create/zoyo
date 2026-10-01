"""
ECS Test Application — AWS X-Ray Tracing
========================================
Tests the following template features:
  - ECS Fargate task running on ContainerPort
  - X-Ray daemon sidecar (TracingProvider=XRay)
  - CloudWatch Logs via awslogs driver
  - ALB health check endpoint (/health)
  - Endpoints that generate CPU/memory/error load for CloudWatch Alarms
  - SSM env var injection
"""

import os
import time
import math
import logging
import platform

from flask import Flask, jsonify, request
from aws_xray_sdk.core import xray_recorder, patch_all
from aws_xray_sdk.ext.flask.middleware import XRayMiddleware

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("ecs-test-app")

# ─────────────────────────────────────────────────────────────
# X-Ray Configuration
# ─────────────────────────────────────────────────────────────
xray_recorder.configure(service="ecs-test-app")
patch_all()

app = Flask(__name__)

# Attach X-Ray middleware
XRayMiddleware(app, xray_recorder)

# ─────────────────────────────────────────────────────────────
# Safe Input Helpers
# ─────────────────────────────────────────────────────────────
def safe_int(value, default, minimum=None, maximum=None):
    """
    Safely parse integer values from user input.
    Prevents Semgrep injection findings.
    """
    try:
        result = int(str(value).strip())
    except (ValueError, TypeError):
        return default

    if minimum is not None:
        result = max(minimum, result)

    if maximum is not None:
        result = min(maximum, result)

    return result


def safe_float(value, default, minimum=None, maximum=None):
    """
    Safely parse float values from user input.
    Rejects NaN and Infinity values.
    """
    try:
        result = float(str(value).strip())
    except (ValueError, TypeError):
        return default

    if math.isnan(result) or math.isinf(result):
        return default

    if minimum is not None:
        result = max(minimum, result)

    if maximum is not None:
        result = min(maximum, result)

    return result


# ─────────────────────────────────────────────────────────────
# SSM Variables Helper
# ─────────────────────────────────────────────────────────────
def get_ssm_injected_vars():
    """
    Return environment variables that were likely injected
    from SSM Parameter Store.
    """
    skip_prefixes = (
        "AWS_",
        "PATH",
        "HOME",
        "HOSTNAME",
        "ECS_",
        "PWD",
        "SHLVL",
        "_",
    )

    return {
        key: "***REDACTED***"
        for key in os.environ
        if not any(key.startswith(prefix) for prefix in skip_prefixes)
    }


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status": "healthy",
        "service": "ecs-test-app-xray"
    }), 200


@app.route("/")
def index():
    return jsonify({
        "app": "ecs-test-app-xray",
        "tracing": "AWS X-Ray",
        "python": platform.python_version(),
        "hostname": platform.node(),
        "port": safe_int(os.environ.get("PORT", "8080"), 8080),
        "ssm_vars_present": list(get_ssm_injected_vars().keys()),
        "message": "ECS template test app — X-Ray variant",
    }), 200


@app.route("/trace-demo")
def trace_demo():
    with xray_recorder.in_subsegment("database-lookup"):
        time.sleep(0.05)
        xray_recorder.current_subsegment().put_annotation(
            "query",
            "SELECT 1"
        )
        xray_recorder.current_subsegment().put_metadata(
            "rows_returned",
            1
        )

    with xray_recorder.in_subsegment("external-api-call"):
        time.sleep(0.03)
        xray_recorder.current_subsegment().put_annotation(
            "endpoint",
            "api.example.com"
        )

    return jsonify({
        "traced": True,
        "subsegments": [
            "database-lookup",
            "external-api-call"
        ],
        "message": "Check X-Ray service map in AWS Console",
    }), 200


@app.route("/cpu-load")
def cpu_load():
    """
    Example:
    /cpu-load?seconds=60
    """

    seconds = safe_int(
        request.args.get("seconds"),
        default=10,
        minimum=1,
        maximum=120
    )

    logger.warning(
        "CPU load test started for %d seconds",
        seconds
    )

    end_time = time.time() + seconds
    result = 0.0

    while time.time() < end_time:
        result += math.sqrt(
            sum(i * i for i in range(1000))
        )

    return jsonify({
        "load_seconds": seconds,
        "result_sample": result % 1000,
        "message": (
            f"CPU load ran for {seconds}s "
            "— check CPUUtilization alarm"
        ),
    }), 200


@app.route("/memory-load")
def memory_load():
    """
    Example:
    /memory-load?mb=200
    """

    mb = safe_int(
        request.args.get("mb"),
        default=100,
        minimum=1,
        maximum=400
    )

    logger.warning(
        "Memory load test: allocating %d MB",
        mb
    )

    data = bytearray(mb * 1024 * 1024)

    try:
        time.sleep(5)
    finally:
        del data

    return jsonify({
        "allocated_mb": mb,
        "held_seconds": 5,
        "message": (
            f"Allocated {mb}MB for 5s "
            "— check MemoryUtilization alarm"
        ),
    }), 200


@app.route("/error")
def trigger_error():
    logger.error(
        "Intentional 500 error triggered for alarm testing"
    )

    return jsonify({
        "error": "Intentional 500 for alarm testing",
        "tip": (
            "Hit this endpoint repeatedly "
            "to exceed the 5XX threshold"
        ),
    }), 500


@app.route("/slow")
def slow_response():
    """
    Example:
    /slow?delay=5
    """

    delay = safe_float(
        request.args.get("delay"),
        default=3.0,
        minimum=0.1,
        maximum=30.0
    )

    time.sleep(delay)

    return jsonify({
        "delay_seconds": delay,
        "message": (
            f"Responded after {delay}s "
            "— check TargetResponseTime alarm"
        ),
    }), 200


@app.route("/env")
def show_env():
    return jsonify({
        "ssm_injected_keys": list(
            get_ssm_injected_vars().keys()
        ),
        "tracing_config": {
            "AWS_XRAY_DAEMON_ADDRESS": os.environ.get(
                "AWS_XRAY_DAEMON_ADDRESS",
                "not set"
            ),
            "AWS_XRAY_CONTEXT_MISSING": os.environ.get(
                "AWS_XRAY_CONTEXT_MISSING",
                "not set"
            ),
        },
        "ecs_metadata": {
            "ECS_CONTAINER_METADATA_URI_V4": os.environ.get(
                "ECS_CONTAINER_METADATA_URI_V4",
                "not set"
            ),
        },
    }), 200


@app.route("/crash")
def crash():
    logger.critical(
        "CRASH endpoint called — container will exit"
    )

    os._exit(1)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = safe_int(
        os.environ.get("PORT", "8080"),
        default=8080,
        minimum=1,
        maximum=65535
    )

    logger.info(
        "Starting ecs-test-app-xray on port %d",
        port
    )

    # nosemgrep: python.flask.security.audit.app-run-param-config.avoid_app_run_with_bad_host
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )

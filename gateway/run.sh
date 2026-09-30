#!/bin/bash
# Entrypoint used by the AWS Lambda Web Adapter (and for local dev).
# The adapter forwards buffered Lambda Function URL requests to this
# uvicorn server.
#
# Lambda layers unpack to /opt/python, which the managed Python runtime
# adds to sys.path only for native Python handlers. This uvicorn child
# process is started by the Web Adapter, so the shared quota-periods
# layer must be added to PYTHONPATH explicitly.
export PYTHONPATH="/opt/python${PYTHONPATH:+:$PYTHONPATH}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"

"""
Webhook Lambda handler — Phase 2 stub.

Phase 2 goal: prove the deploy pipeline end-to-end with dummy logic.
Real HMAC verification and payload parsing come in Phase 3.

This stub:
  - Accepts any POST
  - Writes a record to DynamoDB with the raw body
  - Returns 200 within 5 seconds (Ring's requirement)
"""

import json
import os
import time
from datetime import datetime, timezone


def handler(event, context):
    """
    Lambda handler for Ring webhook events.

    Phase 2: stub that logs the event and writes to DynamoDB.
    Phase 3: adds HMAC-SHA256 verification and real payload parsing.
    """
    # Log receipt time for latency tracking
    receive_time = datetime.now(timezone.utc).isoformat()

    # Extract body — API Gateway sends it as a string in event["body"]
    body = event.get("body", "{}")
    if isinstance(body, str):
        try:
            body_parsed = json.loads(body)
        except json.JSONDecodeError:
            body_parsed = {"raw": body}
    else:
        body_parsed = body

    # Phase 2: write a stub record to DynamoDB
    table_name = os.environ.get("STATE_TABLE_NAME", "ThresholdState")

    try:
        import boto3
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)
        table.put_item(
            Item={
                "PK": f"EVENT#{receive_time}",
                "SK": "WEBHOOK_STUB",
                "received_at": receive_time,
                "raw_body": json.dumps(body_parsed),
                "source": "webhook_handler_stub",
            }
        )
        db_status = "written"
    except Exception as e:
        db_status = f"error: {str(e)}"

    print(json.dumps({
        "message": "Webhook stub received event",
        "received_at": receive_time,
        "db_status": db_status,
        "body_preview": str(body_parsed)[:200],
    }))

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "received", "handler": "webhook_stub"}),
    }

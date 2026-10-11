"""
Scheduler Lambda handler — Phase 2 stub.

Phase 2 goal: prove the EventBridge → Lambda pipeline works.
Real routine evaluation and absence detection come in Phase 4.

This stub:
  - Logs that it was triggered
  - Writes a heartbeat record to DynamoDB
  - Confirms the scheduler→Lambda path is wired correctly
"""

import json
import os
from datetime import datetime, timezone


def handler(event, context):
    """
    Lambda handler for the EventBridge Scheduler.

    Phase 2: stub that logs a heartbeat.
    Phase 4: reads real DynamoDB state, derives routine statuses,
             runs edge detection, fires triage engine.
    """
    tick_time = datetime.now(timezone.utc).isoformat()

    table_name = os.environ.get("STATE_TABLE_NAME", "ThresholdState")

    try:
        import boto3
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(table_name)
        table.put_item(
            Item={
                "PK": "SCHEDULER_HEARTBEAT",
                "SK": f"TICK#{tick_time}",
                "tick_time": tick_time,
                "source": "scheduler_handler_stub",
            }
        )
        db_status = "written"
    except Exception as e:
        db_status = f"error: {str(e)}"

    print(json.dumps({
        "message": "Scheduler stub tick",
        "tick_time": tick_time,
        "db_status": db_status,
    }))

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "tick_complete", "handler": "scheduler_stub"}),
    }

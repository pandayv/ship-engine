"""
Which Alexa users SHIP may notify.

The Alexa skill (alexa-skill/lambda/lambda_function.py) saves a person's
Alexa user ID here whenever they use the skill. The notifier reads it back
to send that person a notification when a finding needs them.

Why a table of its own: the skill and the notifier run in different Lambda
functions with different roles, and this is the only data they share.

The skill's Lambda zip cannot import from src/, so the skill writes to this
table by name. tests/test_alexa_skill.py checks that its table name and
key match TABLE_NAME and KEY below, so the two cannot drift apart silently.
"""

import logging
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-alexa-users"
KEY = "user_id"


@lru_cache(maxsize=1)
def _table():
    import boto3

    return boto3.resource("dynamodb").Table(TABLE_NAME)


def create_table_if_not_exists() -> None:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("dynamodb")
    try:
        client.describe_table(TableName=TABLE_NAME)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": KEY, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": KEY, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def list_user_ids() -> list[str]:
    """Every saved Alexa user ID. The table holds a handful of rows (the people
    who have opened the skill), so a plain scan is the right size."""
    items = []
    kwargs = {}
    while True:
        page = _table().scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return [item[KEY] for item in items]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


if __name__ == "__main__":
    create_table_if_not_exists()
    print(f"{TABLE_NAME} is ready")

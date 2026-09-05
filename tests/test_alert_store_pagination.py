"""
Finding #14: DynamoDB Scan reads at most 1MB BEFORE applying
FilterExpression, then returns LastEvaluatedKey if more of the table
remains unscanned. The old list_active_alerts() called .scan() exactly
once and returned whatever came back - once the table grows past 1MB,
some "frozen" alerts silently fall off the dashboard with no indication
more data exists. This test proves the fix actually loops until
LastEvaluatedKey is genuinely absent, using a fake table that requires two
calls to exhaust the table (matching real DynamoDB's own pagination
contract), not just a single-page happy path.
"""

from decimal import Decimal

from src.storage import alert_store


def _raw_item(n: int) -> dict:
    return {
        "alert_id": f"alert-{n}",
        "repo": "pandayv/micro-finance",
        "pr_number": Decimal(1),
        "file": f"file{n}.py",
        "taxonomy_id": "PIIE-001",
        "risk_score": Decimal("9"),
        "plain_english_summary": "summary",
        "citation": "citation",
        "remediation_patch": "patch",
        "status": "frozen",
        "created_at": "2026-09-05T00:00:00+00:00",
    }


class _FakePaginatedTable:
    """Simulates a real DynamoDB table whose data spans two pages -
    exactly the shape a single-page .scan() call would silently truncate."""

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.scan_call_count = 0

    def scan(self, **kwargs):
        page_index = self.scan_call_count
        self.scan_call_count += 1
        items = self._pages[page_index]
        response = {"Items": items}
        if page_index < len(self._pages) - 1:
            response["LastEvaluatedKey"] = {"alert_id": items[-1]["alert_id"]}
        return response


def test_list_active_alerts_follows_pagination_across_multiple_scan_pages(monkeypatch):
    page1 = [_raw_item(1), _raw_item(2)]
    page2 = [_raw_item(3)]
    fake_table = _FakePaginatedTable([page1, page2])
    monkeypatch.setattr(alert_store, "_table", lambda: fake_table)

    alerts = alert_store.list_active_alerts()

    assert fake_table.scan_call_count == 2, "must call scan() again when LastEvaluatedKey is present"
    assert {a.alert_id for a in alerts} == {"alert-1", "alert-2", "alert-3"}


def test_list_active_alerts_stops_after_a_single_page_when_no_more_data(monkeypatch):
    fake_table = _FakePaginatedTable([[_raw_item(1)]])
    monkeypatch.setattr(alert_store, "_table", lambda: fake_table)

    alerts = alert_store.list_active_alerts()

    assert fake_table.scan_call_count == 1, "must not call scan() again once LastEvaluatedKey is absent"
    assert len(alerts) == 1

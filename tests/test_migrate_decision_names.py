"""
scripts/migrate_decision_names.py against a fake ship-alerts table: a dry run by default that writes
nothing, a conditional write per row when applied, and a second run that finds nothing to do.
Nothing here reaches AWS.
"""

import pytest

import scripts.migrate_decision_names as migration
from tests.fake_dynamo import FakeTable


def row(alert_id, resolution, **extra):
    base = {"alert_id": alert_id, "repo": "pandayv/micro-finance", "pr_number": 2, "status": "resolved",
            "resolved_at": "2026-10-01T00:00:00+00:00", "resolution_reason": "kept as is", "resolved_by": "voice"}
    if resolution is not None:
        base["resolution"] = resolution
    return {**base, **extra}


@pytest.fixture
def table():
    fake = FakeTable("alert_id", {
        "a1": row("a1", "approved"),
        "a2": row("a2", "rejected", fragment_key="k"),
        "a3": row("a3", "allowed"),
        "a4": row("a4", "blocked"),
        "a5": {"alert_id": "a5", "repo": "pandayv/micro-finance", "pr_number": 3, "status": "frozen"},
    })
    updates = []
    original = fake.update_item

    def recording_update(**kw):
        updates.append(kw["Key"]["alert_id"])
        return original(**kw)

    fake.update_item = recording_update
    fake.updates = updates
    return fake


def run(table, apply=False):
    lines = []
    result = migration.migrate(table, apply=apply, out=lines.append)
    return result, lines


def test_a_dry_run_lists_what_it_would_change_and_writes_nothing(table):
    before = {k: dict(v) for k, v in table.rows.items()}
    result, lines = run(table)

    assert result == {"changed": 2, "skipped": 0, "applied": False}
    assert lines[0] == "would change a1 (pandayv/micro-finance#2): approved -> allowed"
    assert lines[1] == "would change a2 (pandayv/micro-finance#2): rejected -> blocked"
    assert "Dry run: nothing was written" in lines[-1] and "--apply" in lines[-1]
    assert table.updates == [] and table.rows == before


def test_apply_rewrites_only_the_resolution_of_the_rows_that_hold_an_old_name(table):
    before = {k: dict(v) for k, v in table.rows.items()}
    result, lines = run(table, apply=True)

    assert result == {"changed": 2, "skipped": 0, "applied": True}
    assert table.rows["a1"]["resolution"] == "allowed" and table.rows["a2"]["resolution"] == "blocked"
    for untouched in ("a3", "a4", "a5"):
        assert table.rows[untouched] == before[untouched]
    for migrated in ("a1", "a2"):       # nothing else on the row moved
        assert {k: v for k, v in table.rows[migrated].items() if k != "resolution"} == \
               {k: v for k, v in before[migrated].items() if k != "resolution"}
    assert lines[0].startswith("changed a1") and lines[-1] == "changed 2 row(s) in ship-alerts"


def test_running_it_twice_changes_nothing_the_second_time(table):
    run(table, apply=True)
    table.updates.clear()
    snapshot = {k: dict(v) for k, v in table.rows.items()}

    result, lines = run(table, apply=True)

    assert result == {"changed": 0, "skipped": 0, "applied": True}
    assert lines == ["changed 0 row(s) in ship-alerts"]
    assert table.updates == [] and table.rows == snapshot


def test_a_dry_run_after_applying_finds_nothing(table):
    run(table, apply=True)
    result, lines = run(table)
    assert result["changed"] == 0 and lines[0].startswith("would change 0 row(s)")


def test_a_row_that_changed_since_it_was_read_is_left_alone(table):
    # The write is conditional on the row still holding the old name: a decision recorded between the
    # scan and the update must not be overwritten.
    original_scan = table.scan

    def scan_then_change(**kw):
        page = original_scan(**kw)
        table.rows["a1"]["resolution"] = "blocked"      # someone else got there first
        return page

    table.scan = scan_then_change
    result, lines = run(table, apply=True)

    assert result["changed"] == 1 and result["skipped"] == 1
    assert table.rows["a1"]["resolution"] == "blocked"           # not forced to "allowed"
    assert "left a1 (pandayv/micro-finance#2): no longer approved" in lines


def test_every_page_of_a_long_scan_is_read(table):
    pages = []

    def paged(**kw):
        pages.append(kw.get("ExclusiveStartKey"))
        if kw.get("ExclusiveStartKey") is None:
            return {"Items": [table.rows["a1"]], "LastEvaluatedKey": {"alert_id": "a1"}}
        return {"Items": [table.rows["a2"]]}

    table.scan = paged
    result, _ = run(table)
    assert result["changed"] == 2 and pages == [None, {"alert_id": "a1"}]


def test_the_scan_is_strongly_consistent(table):
    seen = []
    original_scan = table.scan
    table.scan = lambda **kw: seen.append(kw.get("ConsistentRead")) or original_scan(**kw)
    run(table)
    assert seen == [True]


def test_the_mapping_uses_the_stored_names_the_store_defines():
    from src.storage import alert_store
    assert migration.RENAMES == {"approved": alert_store.RESOLUTION_ALLOWED, "rejected": alert_store.RESOLUTION_BLOCKED}
    assert migration.TABLE_NAME == "ship-alerts"


def test_the_command_line_defaults_to_a_dry_run(monkeypatch, table, capsys):
    import types

    seen = {}

    class FakeResource:
        def Table(self, name):
            seen["name"] = name
            return table

    monkeypatch.setitem(__import__("sys").modules, "boto3", types.SimpleNamespace(resource=lambda service: FakeResource()))
    assert migration.main([]) == 0
    assert seen["name"] == "ship-alerts" and table.updates == []
    assert "would change a1" in capsys.readouterr().out

    assert migration.main(["--apply"]) == 0
    assert sorted(table.updates) == ["a1", "a2"]

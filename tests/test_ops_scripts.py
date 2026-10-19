"""
The operations scripts (scripts/demo_reset.py, scripts/preflight.py): logic only, with a fake
GitHub CLI and fake AWS. Nothing here touches GitHub or AWS.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_packages  # noqa: E402
import demo_reset as reset  # noqa: E402
import iam_tidy  # noqa: E402
import preflight  # noqa: E402


class FakeGh:
    def __init__(self, pulls=(), branches=()):
        self.calls, self._pulls, self._branches = [], list(pulls), list(branches)

    def run(self, *args):
        self.calls.append(args)
        if args[:2] == ("api", f"repos/pandayv/micro-finance/branches"):
            return "\n".join(self._branches)
        if args[0] == "api" and "/git/ref/heads/" in args[1]:
            return "headsha\n"
        if args[0] == "api" and "/git/commits/headsha" in args[1]:
            return "treesha\n"
        if args[:2] == ("api", "-X") and args[2] == "POST" and args[3].endswith("/git/commits"):
            return "newcommit\n"
        if args[0] == "pr" and args[1] == "create":
            return "https://github.com/pandayv/micro-finance/pull/42\n"
        return ""

    def json(self, *args):
        self.calls.append(args)
        return self._pulls


def test_the_plan_lists_every_change_before_any_is_made():
    gh = FakeGh(pulls=[{"number": 8, "headRefName": "demo/x", "title": "t"}, {"number": 3, "headRefName": "feature/keep", "title": "t"}],
                branches=["master", "demo/x", "feature/keep", "e2e/y"])
    steps = reset.plan(gh, "pandayv/micro-finance")
    assert "close pull request #8 (demo/x)" in steps
    assert not any("#3" in s or "feature/keep" in s for s in steps)       # a real pull request is never touched
    assert "delete branch demo/x" in steps and "delete branch e2e/y" in steps and "delete branch master" not in steps
    assert any("delete every finding" in s for s in steps)
    assert sum(1 for s in steps if s.startswith("open a pull request")) == len(reset.DEMOS)


def test_a_fresh_pull_request_is_a_new_commit_with_the_same_files_so_its_check_is_its_own():
    gh = FakeGh()
    number = reset.open_fresh_pull(gh, "pandayv/micro-finance", "master", "feature/a", "demo/a", "Title", "Body")
    assert number == 42
    commit_call = next(c for c in gh.calls if c[:2] == ("api", "-X") and c[3].endswith("/git/commits"))
    assert "tree=treesha" in commit_call and "parents[]=headsha" in commit_call
    ref_call = next(c for c in gh.calls if c[3].endswith("/git/refs"))
    assert "ref=refs/heads/demo/a" in ref_call and "sha=newcommit" in ref_call


def test_waiting_ends_once_every_new_pull_request_has_findings_and_the_count_is_steady(monkeypatch):
    counts = iter([{}, {9: 1}, {9: 1, 10: 3}, {9: 1, 10: 5}, {9: 1, 10: 5}, {9: 1, 10: 5}, {9: 1, 10: 5}])
    monkeypatch.setattr(reset, "findings_per_pr", lambda session: next(counts))
    monkeypatch.setattr(reset.time, "sleep", lambda s: None)
    assert reset.wait_for_review(None, [9, 10], timeout=3600, say=lambda *_: None) == {9: 1, 10: 5}


def test_the_demo_is_reset_only_for_the_account_that_owns_the_repository(monkeypatch, capsys):
    monkeypatch.setattr(reset, "active_account", lambda gh: "someone-else")
    assert reset.main(["--yes"]) == 2
    assert "gh auth switch --user pandayv" in capsys.readouterr().out


def test_without_yes_nothing_is_changed(monkeypatch, capsys):
    gh = FakeGh(pulls=[{"number": 8, "headRefName": "demo/x", "title": "t"}], branches=["demo/x"])
    monkeypatch.setattr(reset, "Gh", lambda: gh)
    monkeypatch.setattr(reset, "active_account", lambda g: "pandayv")
    assert reset.main([]) == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert not any(c[0] == "pr" and c[1] == "close" for c in gh.calls)


def test_the_mcp_reply_can_be_plain_json_or_server_sent_events(monkeypatch):
    mcp = preflight.Mcp("https://example.test/mcp")
    for raw in ('{"jsonrpc":"2.0","id":1,"result":{"ok":true}}', 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return raw.encode()
        monkeypatch.setattr(preflight.urllib.request, "urlopen", lambda req, timeout=0: Resp())
        assert mcp._post({"x": 1})["result"]["ok"] is True


# ------------------------------------------------------------------ package build


def test_a_package_holds_exactly_the_first_party_files_its_handler_imports():
    gateway = build_packages.first_party_closure(["device_gateway_handler.py"])
    assert "device_gateway_handler.py" in gateway and "src/storage/device_store.py" in gateway and "src/storage/push_store.py" in gateway
    assert not any(f.startswith("src/relay/") or f.startswith("src/agents/") for f in gateway)   # a gateway never needs Relay or Detector


def test_relay_gets_what_it_imports_even_when_the_import_is_inside_a_function():
    relay = build_packages.first_party_closure(["relay_lambda_handler.py"])
    for needed in ("src/relay/server.py", "src/relay/webpush.py", "src/storage/push_store.py", "src/storage/session_store.py",
                   "src/api/github_writeback.py", "src/relay/audit.py"):
        assert needed in relay, needed


def test_every_package_declares_a_handler_that_exists():
    for target in build_packages.TARGETS.values():
        for entry in target["entries"]:
            assert (build_packages.ROOT / entry).exists(), entry


# ------------------------------------------------------------------ IAM tidy


class FakeIam:
    """Just enough of the IAM client for iam_tidy: roles with inline and attached policies."""

    class exceptions:
        class NoSuchEntityException(Exception):
            pass

        class ClientError(Exception):
            pass

    def __init__(self, roles):
        self.roles = roles          # name -> {"inline": {name: doc}, "attached": {arn: name}}
        self.log = []

    def _role(self, name):
        if name not in self.roles:
            raise self.exceptions.NoSuchEntityException(name)
        return self.roles[name]

    def list_role_policies(self, RoleName, Marker=None):
        return {"PolicyNames": list(self._role(RoleName)["inline"]), "IsTruncated": False}

    def get_role_policy(self, RoleName, PolicyName):
        return {"PolicyDocument": self._role(RoleName)["inline"][PolicyName]}

    def list_attached_role_policies(self, RoleName, Marker=None):
        return {"AttachedPolicies": [{"PolicyArn": a, "PolicyName": n} for a, n in self._role(RoleName)["attached"].items()], "IsTruncated": False}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self._role(RoleName)["inline"][PolicyName] = json.loads(PolicyDocument)
        self.log.append(("put", RoleName, PolicyName))

    def delete_role_policy(self, RoleName, PolicyName):
        del self._role(RoleName)["inline"][PolicyName]
        self.log.append(("delete", RoleName, PolicyName))

    def detach_role_policy(self, RoleName, PolicyArn):
        del self._role(RoleName)["attached"][PolicyArn]
        self.log.append(("detach", RoleName, PolicyArn))

    def attach_role_policy(self, RoleName, PolicyArn):
        self._role(RoleName)["attached"][PolicyArn] = PolicyArn.split("/")[-1]
        self.log.append(("attach", RoleName, PolicyArn))

    def list_roles(self, Marker=None):
        return {"Roles": [{"RoleName": n} for n in self.roles], "IsTruncated": False}


def messy_account(plan):
    roles = {}
    for r in plan["roles"]:
        roles[r["role"]] = {"inline": {r["policy"]: {"old": True}, "ship-thing-2026-10-17": {"Version": "x"}, "random-paste": {"Version": "y"}},
                            "attached": {"arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole": "AWSLambdaBasicExecutionRole"}}
    roles["ship-agent-extra-role"] = {"inline": {"keep-me": {}}, "attached": {}}
    roles["unrelated-role"] = {"inline": {"x": {}}, "attached": {}}
    return roles


def test_the_plan_names_one_policy_per_role_after_its_function_and_every_file_exists():
    plan = iam_tidy.load_plan()
    names = [r["policy"] for r in plan["roles"]]
    assert len(names) == len(set(names)) == 5
    assert all(n.startswith("ship-") and n.endswith("-access") for n in names)
    assert all(r["document"]["Statement"] for r in plan["roles"])


def test_the_policies_are_least_privilege_no_wildcard_actions_and_no_account_wide_resources():
    for role in iam_tidy.load_plan()["roles"]:
        for statement in role["document"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            assert not any(a == "*" or a.endswith(":*") for a in actions), (role["role"], actions)
            assert "*" != statement["Resource"] and all(r != "*" for r in resources), (role["role"], statement["Sid"])


def test_apply_only_adds_the_policy_and_removes_nothing():
    plan = iam_tidy.load_plan()
    iam = FakeIam(messy_account(plan))
    iam_tidy.apply(iam, plan, say=lambda *_: None)
    assert {e[0] for e in iam.log} == {"put"}
    for role in plan["roles"]:
        assert iam.roles[role["role"]]["inline"][role["policy"]] == role["document"]
        assert "random-paste" in iam.roles[role["role"]]["inline"]          # the old ones are still there


def test_prune_refuses_until_every_role_has_its_policy_exactly(tmp_path, monkeypatch):
    plan = iam_tidy.load_plan()
    iam = FakeIam(messy_account(plan))              # the policies are the stale {"old": True} documents
    monkeypatch.setattr(iam_tidy, "BACKUPS", tmp_path)
    said = []
    assert iam_tidy.prune(iam, plan, say=said.append) is None
    assert any("Run with --apply first" in s for s in said) and not iam.log


def test_prune_backs_up_first_then_leaves_each_role_with_only_its_one_policy(tmp_path, monkeypatch):
    plan = iam_tidy.load_plan()
    iam = FakeIam(messy_account(plan))
    monkeypatch.setattr(iam_tidy, "BACKUPS", tmp_path)
    monkeypatch.setattr(iam_tidy, "ROOT", tmp_path.parent)
    iam_tidy.apply(iam, plan, say=lambda *_: None)
    iam.log.clear()
    path = iam_tidy.prune(iam, plan, say=lambda *_: None)
    assert path and path.exists()
    for role in plan["roles"]:
        assert list(iam.roles[role["role"]]["inline"]) == [role["policy"]] and iam.roles[role["role"]]["attached"] == {}
    # nothing else in the account was touched
    assert iam.roles["ship-agent-extra-role"] == {"inline": {"keep-me": {}}, "attached": {}}
    assert iam.roles["unrelated-role"] == {"inline": {"x": {}}, "attached": {}}
    saved = json.loads(path.read_text())
    assert "random-paste" in saved["roles"][plan["roles"][0]["role"]]["inline"]


def test_restore_puts_back_everything_prune_removed(tmp_path, monkeypatch):
    plan = iam_tidy.load_plan()
    iam = FakeIam(messy_account(plan))
    monkeypatch.setattr(iam_tidy, "BACKUPS", tmp_path)
    monkeypatch.setattr(iam_tidy, "ROOT", tmp_path.parent)
    iam_tidy.apply(iam, plan, say=lambda *_: None)
    path = iam_tidy.prune(iam, plan, say=lambda *_: None)
    iam_tidy.restore(iam, path, say=lambda *_: None)
    for role in plan["roles"]:
        inline = iam.roles[role["role"]]["inline"]
        assert {"random-paste", "ship-thing-2026-10-17", role["policy"]} <= set(inline)
        assert "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" in iam.roles[role["role"]]["attached"]


def test_roles_that_look_like_ships_but_are_not_listed_are_reported_and_never_changed():
    plan = iam_tidy.load_plan()
    iam = FakeIam(messy_account(plan))
    assert iam_tidy.unlisted_roles(iam, plan) == ["ship-agent-extra-role"]


def test_a_missing_role_is_skipped_not_an_error():
    plan = iam_tidy.load_plan()
    iam = FakeIam({})
    iam_tidy.apply(iam, plan, say=lambda *_: None)
    assert iam.log == []

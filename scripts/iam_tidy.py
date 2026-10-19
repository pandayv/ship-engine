#!/usr/bin/env python3
"""
Gives each SHIP Lambda role exactly one policy: the reviewed one in infra/iam/, named
`<function>-access`. Removes the dated, one-off and leftover policies that built up around them.

Run it with credentials that can manage IAM (the deploy user cannot). Four stages, each safe to
stop after:

    python scripts/iam_tidy.py                    # 1. show what each role has now and what would change
    python scripts/iam_tidy.py --apply            # 2. add the one policy to every role (changes nothing else)
    python scripts/preflight.py                   #    check the demo still works
    python scripts/iam_tidy.py --prune            # 3. back up, then remove every other policy from those roles
    python scripts/iam_tidy.py --restore FILE     # 4. (only if needed) put back what --prune removed

--prune refuses to run unless every role already has its policy exactly as in infra/iam/, and it
writes a backup of everything it removes (infra/iam/backups/) before it removes anything. A role's
trust policy, tags and the roles themselves are never touched. Roles that are not listed in
infra/iam/roles.json are only reported.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IAM = ROOT / "infra" / "iam"
BACKUPS = IAM / "backups"
LOOKS_LIKE_SHIP = ("ship", "ask-lambda-ship", "agentcore")


def load_plan() -> dict:
    plan = json.loads((IAM / "roles.json").read_text())
    for role in plan["roles"]:
        role["document"] = json.loads((IAM / role["file"]).read_text())
    return plan


def current(iam, role_name: str) -> dict:
    """What a role has now: its inline policies (name -> document) and its attached managed policies."""
    inline = {}
    for name in _all(iam.list_role_policies, "PolicyNames", RoleName=role_name):
        inline[name] = iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]
    attached = {p["PolicyArn"]: p["PolicyName"] for p in _all(iam.list_attached_role_policies, "AttachedPolicies", RoleName=role_name)}
    return {"inline": inline, "attached": attached}


def _all(call, key, **kwargs):
    items, marker = [], None
    while True:
        page = call(**kwargs, **({"Marker": marker} if marker else {}))
        items += page[key]
        if not page.get("IsTruncated"):
            return items
        marker = page["Marker"]


def same(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def describe(iam, plan, say=print) -> dict[str, dict]:
    state = {}
    for role in plan["roles"]:
        try:
            now = current(iam, role["role"])
        except iam.exceptions.NoSuchEntityException:
            say(f"\n{role['role']}  -- not found in this account")
            continue
        state[role["role"]] = now
        say(f"\n{role['role']}   ({role['purpose']})")
        have = now["inline"].get(role["policy"])
        status = "missing" if have is None else ("up to date" if same(have, role["document"]) else "DIFFERENT from infra/iam")
        say(f"  keep    {role['policy']}   [{status}]")
        for name in sorted(n for n in now["inline"] if n != role["policy"]):
            say(f"  remove  {name}   (inline)")
        for arn, name in sorted(now["attached"].items()):
            say(f"  detach  {name}   (managed, {arn})")
    return state


def unlisted_roles(iam, plan) -> list[str]:
    listed = {r["role"] for r in plan["roles"]}
    names = [r["RoleName"] for r in _all(iam.list_roles, "Roles")]
    return sorted(n for n in names if n not in listed and any(k in n.lower() for k in LOOKS_LIKE_SHIP))


def apply(iam, plan, say=print) -> None:
    for role in plan["roles"]:
        try:
            iam.put_role_policy(RoleName=role["role"], PolicyName=role["policy"], PolicyDocument=json.dumps(role["document"]))
            say(f"put {role['policy']} on {role['role']}")
        except iam.exceptions.NoSuchEntityException:
            say(f"skipped {role['role']}: not found")


def backup(plan, state, path: Path) -> dict:
    data = {"taken": time.strftime("%Y-%m-%d %H:%M:%S"), "roles": {}}
    for role in plan["roles"]:
        now = state.get(role["role"])
        if not now:
            continue
        data["roles"][role["role"]] = {
            "inline": {n: d for n, d in now["inline"].items() if n != role["policy"]},
            "attached": dict(now["attached"]),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return data


def prune(iam, plan, say=print, now=None) -> Path | None:
    state = describe(iam, plan, say=lambda *_: None)
    for role in plan["roles"]:
        if role["role"] not in state:
            continue
        have = state[role["role"]]["inline"].get(role["policy"])
        if have is None or not same(have, role["document"]):
            say(f"Not pruning: {role['role']} does not have {role['policy']} exactly as in infra/iam/. Run with --apply first.")
            return None
    path = BACKUPS / f"iam-backup-{time.strftime('%Y%m%d-%H%M%S', now or time.localtime())}.json"
    backup(plan, state, path)
    say(f"Backed up what will be removed to {path.relative_to(ROOT)}")
    for role in plan["roles"]:
        if role["role"] not in state:
            continue
        for name in state[role["role"]]["inline"]:
            if name != role["policy"]:
                iam.delete_role_policy(RoleName=role["role"], PolicyName=name)
                say(f"removed {name} from {role['role']}")
        for arn in state[role["role"]]["attached"]:
            iam.detach_role_policy(RoleName=role["role"], PolicyArn=arn)
            say(f"detached {arn} from {role['role']}")
    return path


def restore(iam, path: Path, say=print) -> None:
    data = json.loads(path.read_text())
    for role, saved in data["roles"].items():
        for name, document in saved["inline"].items():
            iam.put_role_policy(RoleName=role, PolicyName=name, PolicyDocument=json.dumps(document))
            say(f"restored {name} on {role}")
        for arn in saved["attached"]:
            iam.attach_role_policy(RoleName=role, PolicyArn=arn)
            say(f"re-attached {arn} to {role}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="add the one policy to every role")
    parser.add_argument("--prune", action="store_true", help="back up, then remove every other policy from those roles")
    parser.add_argument("--restore", metavar="FILE", help="put back what an earlier --prune removed")
    args = parser.parse_args(argv)

    import boto3

    iam = boto3.client("iam")
    who = boto3.client("sts").get_caller_identity()
    plan = load_plan()
    if who["Account"] != plan["account"]:
        print(f"Signed in to {who['Account']}, expected {plan['account']}.")
        return 2
    try:
        if args.restore:
            restore(iam, Path(args.restore))
        elif args.prune:
            prune(iam, plan)
        elif args.apply:
            apply(iam, plan)
            print("\nNext: python scripts/preflight.py, and a quick voice test. Then --prune.")
        else:
            describe(iam, plan)
            others = unlisted_roles(iam, plan)
            if others:
                print("\nOther roles that look like SHIP's (not touched, review them in the console):")
                for name in others:
                    print(f"  {name}")
            print("\nNothing was changed. --apply adds the policies; --prune then removes the rest.")
    except iam.exceptions.ClientError as exc:
        print(f"IAM refused: {exc.response['Error']['Message']}\nThis needs credentials that can manage IAM, not the deploy user.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

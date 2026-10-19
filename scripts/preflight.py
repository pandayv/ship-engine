#!/usr/bin/env python3
"""
Pre-recording check: is everything SHIP needs for a demo up, and in the state a demo needs?

Read-only. Every line is PASS, WARN or FAIL, with what to do about it. Exit code 1 if anything
FAILs. Run it before a recording, after a deploy, and after resetting the demo data.

    python scripts/preflight.py
    python scripts/preflight.py --screens laptop,tablet --repo pandayv/micro-finance

What it looks at: the AWS account and the five Lambdas, Relay answering over MCP with every tool
it should have, the WebSocket gateway accepting a screen, the screens you registered, the demo
findings and their pull requests' checks, the dead-letter queue, recent errors in the logs,
the Alexa skill's build status, the alert credentials, and the public pages.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REGION = "us-west-2"
ACCOUNT = "680160265218"
LAMBDAS = ["ship-relay", "ship-webhook", "ship-fragment-processor", "ship-device-gateway", "ship-alexa-skill"]
WS_URL = "wss://9vkr6eelzd.execute-api.us-west-2.amazonaws.com/prod"
SKILL_ID = "amzn1.ask.skill.6cab9db1-218c-4a2c-a62d-a2e6b0d90063"
PAGES = ["architecture.html", "alexa-simulator.html", "ship-display.html", "ship-render.js", "ship-sw.js"]
PAGES_BASE = "https://pandayv.github.io/ship-engine/"
QUEUE = "ship-fragment-queue"

results: list[tuple[str, str, str]] = []


def record(level: str, what: str, detail: str = "") -> None:
    results.append((level, what, detail))
    mark = {"PASS": "  ok  ", "WARN": " warn ", "FAIL": " FAIL "}[level]
    print(f"[{mark}] {what}" + (f"  ({detail})" if detail else ""))


def passed(what, detail=""): record("PASS", what, detail)
def warn(what, detail=""): record("WARN", what, detail)
def fail(what, detail=""): record("FAIL", what, detail)


def run(cmd: list[str], timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------------------ AWS

def check_account(session):
    who = session.client("sts").get_caller_identity()
    if who["Account"] == ACCOUNT:
        passed("AWS account", f"{who['Account']} as {who['Arn'].split('/')[-1]}")
    else:
        fail("AWS account", f"signed in to {who['Account']}, expected {ACCOUNT}")


def check_lambdas(session):
    lam = session.client("lambda", region_name=REGION)
    for name in LAMBDAS:
        try:
            cfg = lam.get_function_configuration(FunctionName=name)
        except Exception as exc:  # noqa: BLE001
            fail(f"Lambda {name}", str(exc)[:80])
            continue
        if cfg["State"] == "Active" and cfg["LastUpdateStatus"] == "Successful":
            passed(f"Lambda {name}", f"{cfg['Runtime']} {cfg['Architectures'][0]}")
        else:
            fail(f"Lambda {name}", f"{cfg['State']} / {cfg['LastUpdateStatus']}")
    try:
        alias = lam.get_alias(FunctionName="ship-relay", Name="live")
        passed("Relay alias live", f"version {alias['FunctionVersion']}")
    except Exception as exc:  # noqa: BLE001
        fail("Relay alias live", str(exc)[:80])


def relay_url(session) -> str | None:
    try:
        cfg = session.client("lambda", region_name=REGION).get_function_url_config(FunctionName="ship-relay", Qualifier="live")
        return cfg["FunctionUrl"].rstrip("/") + "/mcp"
    except Exception:  # noqa: BLE001
        return None


class Mcp:
    """The smallest MCP client that does the handshake and calls tools."""

    def __init__(self, url):
        self.url, self.n, self.protocol = url, 0, None

    def _post(self, body):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
        if not raw.strip():
            return None
        for line in raw.splitlines():       # the reply may be server-sent events
            if line.startswith("data:"):
                return json.loads(line[5:])
        return json.loads(raw)

    def rpc(self, method, params=None):
        self.n += 1
        return self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})

    def start(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                       "clientInfo": {"name": "ship-preflight", "version": "1"}})
        self.protocol = init["result"]["protocolVersion"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tools(self):
        return {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}

    def call(self, name, args=None):
        out = self.rpc("tools/call", {"name": name, "arguments": args or {}})["result"]
        text = out["content"][0]["text"]
        try:
            return json.loads(text)
        except ValueError:
            return text


def check_relay(session, screens):
    url = relay_url(session)
    if not url:
        fail("Relay URL", "could not read the function URL of ship-relay:live")
        return None
    try:
        mcp = Mcp(url)
        mcp.start()
    except Exception as exc:  # noqa: BLE001
        fail("Relay answers MCP", str(exc)[:100])
        return None
    from src.relay import server

    expected = {fn.__name__ for fn in server.TOOLS}
    have = mcp.tools()
    if expected <= have:
        passed("Relay tools", f"{len(have)} tools, including every one the code defines")
    else:
        fail("Relay tools", f"missing {sorted(expected - have)}: deploy Relay again")
    status = mcp.call("release_status")
    (warn if "can't check" in str(status) else passed)("Relay status sentence", str(status)[:110])
    connected = mcp.call("connected_screens")
    names = set(connected.get("screens", [])) if isinstance(connected, dict) else set()
    want = set(screens)
    if want <= names:
        passed("Screens registered", ", ".join(sorted(names)))
    else:
        warn("Screens registered", f"missing {sorted(want - names)}: open the display page on those devices, pick the name, tap Get alerts")
    stale_open = connected.get("open_now", []) if isinstance(connected, dict) else []
    if stale_open:
        passed("Screens open right now", ", ".join(stale_open))
    elif want:
        warn("Screens open right now", "none; findings will go out as notifications until a page is opened")
    return mcp


def check_gateway():
    try:
        import asyncio

        import websockets
    except ImportError:
        warn("WebSocket gateway", "the websockets package is not installed, skipped")
        return

    async def probe():
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.send(json.dumps({"action": "register", "device_name": "preflight"}))
            return json.loads(await asyncio.wait_for(ws.recv(), 8))

    try:
        reply = asyncio.run(probe())
        (passed if reply.get("registered") == "preflight" else fail)("WebSocket gateway accepts a screen", str(reply)[:80])
    except Exception as exc:  # noqa: BLE001
        fail("WebSocket gateway accepts a screen", str(exc)[:100])


def check_findings(session, mcp, repo):
    if mcp is None:
        return
    data = mcp.call("blocked_pull_requests")
    prs = data.get("pull_requests", []) if isinstance(data, dict) else []
    held = data.get("held", []) if isinstance(data, dict) else []
    if not prs:
        warn("Demo findings", "nothing is waiting for a decision; run scripts/demo_reset.py for fresh demo data")
    else:
        summary = ", ".join(f"PR {p['pr_number']}: {len(p['findings'])}" for p in prs)
        passed("Demo findings waiting", summary)
        if any(f.get("pending") for p in prs for f in p["findings"]):
            warn("Staged decisions", "some findings show a pending decision from an earlier take; they expire after ten minutes")
    if held:
        warn("Blocked findings still hold a PR", str(held))
    out = run(["gh", "pr", "list", "--repo", repo, "--state", "open", "--json", "number,headRefOid,title"])
    if out.returncode != 0:
        warn("Demo pull requests", "could not read them with the GitHub CLI")
        return
    for pr in json.loads(out.stdout or "[]"):
        st = run(["gh", "api", f"repos/{repo}/commits/{pr['headRefOid']}/status", "--jq",
                  '[.statuses[]|select(.context=="ship/compliance")|"\\(.state) - \\(.description)"][0]'])
        line = st.stdout.strip() or "no ship/compliance status"
        (passed if line.startswith(("failure", "success")) else warn)(f"PR #{pr['number']} check", line)


def check_queues(session):
    sqs = session.client("sqs", region_name=REGION)
    url = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{QUEUE}"
    try:
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "RedrivePolicy"])["Attributes"]
    except Exception as exc:  # noqa: BLE001
        warn("Fragment queue", str(exc)[:90])
        return
    waiting = int(attrs.get("ApproximateNumberOfMessages", 0)) + int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
    (passed if waiting == 0 else warn)("Fragment queue", "empty" if waiting == 0 else f"{waiting} jobs still being reviewed")
    redrive = attrs.get("RedrivePolicy")
    if redrive:
        dlq_name = json.loads(redrive)["deadLetterTargetArn"].split(":")[-1]
        try:
            depth = int(sqs.get_queue_attributes(QueueUrl=url.rsplit("/", 1)[0] + "/" + dlq_name,
                                                 AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"])
            (passed if depth == 0 else fail)("Dead-letter queue", "empty" if depth == 0 else f"{depth} failed jobs: a review did not finish")
        except Exception as exc:  # noqa: BLE001
            warn("Dead-letter queue", str(exc)[:90])


def check_logs(session):
    logs = session.client("logs", region_name=REGION)
    since = int((time.time() - 1800) * 1000)
    for name in LAMBDAS:
        try:
            events = logs.filter_log_events(logGroupName=f"/aws/lambda/{name}", startTime=since,
                                            filterPattern='?"[ERROR]" ?Traceback ?"Task timed out"', limit=5)["events"]
        except Exception:  # noqa: BLE001
            continue
        (passed if not events else warn)(f"{name} errors, last 30 min", "none" if not events else f"{len(events)}+: {events[0]['message'][:70]!r}")


def check_alexa(session):
    out = run(["ask", "smapi", "get-skill-status", "-s", SKILL_ID], timeout=60)
    try:
        status = json.loads(out.stdout)["manifest"]["lastUpdateRequest"]["status"]
        (passed if status == "SUCCEEDED" else fail)("Alexa skill manifest", status)
    except Exception:  # noqa: BLE001
        warn("Alexa skill manifest", "could not read it with the ASK CLI")
    # The language model itself: ask Alexa how it reads the phrases the demo leans on.
    for phrase, intent in (("what's up", "StatusIntent"), ("laptop", "ShowIntent"), ("let's go through them", "StartReviewIntent"),
                           ("allow the first one because it is test data", "AllowIntent")):
        out = run(["ask", "smapi", "profile-nlu", "-s", SKILL_ID, "-g", "development", "-l", "en-US", "--utterance", phrase], timeout=60)
        try:
            got = json.loads(out.stdout)["selectedIntent"]["name"]
        except Exception:  # noqa: BLE001
            got = "unreadable"
        (passed if got == intent else fail)(f"Alexa hears '{phrase}'", got if got != intent else intent)
    ssm = session.client("ssm", region_name=REGION)
    for param in ("/ship/alexa/client_id", "/ship/alexa/client_secret", "/ship/webpush/vapid_private"):
        try:
            ssm.get_parameter(Name=param)
            passed(f"Parameter {param}")
        except Exception:  # noqa: BLE001
            fail(f"Parameter {param}", "missing: alerts or notifications will not work")
    users = session.resource("dynamodb", region_name=REGION).Table("ship-alexa-users").scan(Select="COUNT")["Count"]
    (passed if users else warn)("Alexa users saved", f"{users}" if users else "none: say 'ask ship review turn on alerts' on the Echo")


def check_pages():
    for page in PAGES:
        try:
            with urllib.request.urlopen(PAGES_BASE + page, timeout=15) as resp:
                passed(f"Page {page}", f"HTTP {resp.status}")
        except urllib.error.URLError as exc:
            fail(f"Page {page}", str(exc)[:80])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--screens", default="laptop,tablet", help="screen names that should be registered")
    parser.add_argument("--repo", default="pandayv/micro-finance", help="the demo repository")
    args = parser.parse_args(argv)
    screens = [s for s in args.screens.split(",") if s]

    import boto3

    session = boto3.Session()
    print(f"SHIP pre-recording check  ({time.strftime('%Y-%m-%d %H:%M')})\n")
    check_account(session)
    check_lambdas(session)
    mcp = check_relay(session, screens)
    check_gateway()
    check_findings(session, mcp, args.repo)
    check_queues(session)
    check_logs(session)
    check_alexa(session)
    check_pages()

    bad = [r for r in results if r[0] == "FAIL"]
    warned = [r for r in results if r[0] == "WARN"]
    print(f"\n{len(results) - len(bad) - len(warned)} ok, {len(warned)} to look at, {len(bad)} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

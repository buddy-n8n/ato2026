#!/usr/bin/env python3
"""Run the demo workflows on the local stack so the dashboard has data.

Each workflow starts from a webhook trigger. This script signs in with the demo
owner account, activates any workflow that is not active yet (production webhooks
only listen while a workflow is active), then POSTs to each webhook. The runs go
through the queue to the workers, so they show up in Prometheus, Grafana and the
worker logs like real traffic.

Standard library only. Needs the stack from this repo running (docker compose up -d).

Examples:
    python3 scripts/run_workflows.py                 # 10 runs of every workflow, incl. the real-LLM agent
    python3 scripts/run_workflows.py -n 30           # 30 runs of each
    python3 scripts/run_workflows.py -w billing -n 40
    python3 scripts/run_workflows.py --skip-llm      # leave out the real-LLM agent (no API key needed)

Activation uses n8n's internal editor API (/rest), which is not a stable public
API, so it may need a tweak after an n8n upgrade. The stack pins n8n 2.43.7.
You can also skip the script and call a webhook yourself once a workflow is
active: curl -X POST http://localhost:5678/webhook/billing-sync
"""

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("N8N_URL", "http://localhost:5678")
# Public demo defaults from .env. Override with N8N_EMAIL / N8N_PASSWORD if you changed them.
EMAIL = os.environ.get("N8N_EMAIL", "demo@example.com")
PASSWORD = os.environ.get("N8N_PASSWORD", "Ato2026-demo")

LLM_MARKER = "real llm"
WEBHOOK_TYPE = "n8n-nodes-base.webhook"


class N8n:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(self, method, path, body=None, ok_statuses=()):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "browser-id": "run-workflows-script"},
        )
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            if err.code in ok_statuses:
                return err.code, {}
            detail = err.read().decode(errors="replace")[:300]
            raise SystemExit(f"{method} {path} failed: HTTP {err.code} {detail}")
        except urllib.error.URLError as err:
            raise SystemExit(f"Cannot reach {self.base} ({err.reason}). Is the stack up? docker compose up -d")

    def login(self, email, password):
        self.call("POST", "/rest/login", {"emailOrLdapLoginId": email, "password": password})

    def workflows(self):
        data = self.call("GET", "/rest/workflows?take=250")[1]["data"]
        return data["data"] if isinstance(data, dict) else data

    def workflow(self, workflow_id):
        return self.call("GET", f"/rest/workflows/{workflow_id}")[1]["data"]

    def activate(self, workflow):
        self.call("POST", f"/rest/workflows/{workflow['id']}/activate", {"versionId": workflow["versionId"]})

    def trigger(self, path):
        status, _ = self.call("POST", f"/webhook/{path}", {"source": "run_workflows.py"})
        return status


def webhook_path(workflow):
    for node in workflow["nodes"]:
        if node["type"] == WEBHOOK_TYPE:
            return node["parameters"]["path"]
    return None


def is_llm(workflow):
    return LLM_MARKER in workflow["name"].lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--count", type=int, default=10, help="runs per workflow (default 10)")
    parser.add_argument("-w", "--workflow", help="only workflows whose name contains this text")
    parser.add_argument("-d", "--delay", type=float, default=1.0, help="seconds between runs (default 1)")
    parser.add_argument("--skip-llm", action="store_true", help="skip the real-LLM agent (it needs your API key and costs money)")
    parser.add_argument("--url", default=BASE_URL, help=f"n8n base URL (default {BASE_URL})")
    args = parser.parse_args()

    n8n = N8n(args.url)
    n8n.login(EMAIL, PASSWORD)

    selected = []
    for wf in n8n.workflows():
        name = wf["name"]
        if args.workflow and args.workflow.lower() not in name.lower():
            continue
        if is_llm(wf) and args.skip_llm:
            continue
        selected.append(n8n.workflow(wf["id"]))

    if not selected:
        raise SystemExit("No matching workflows.")
    selected.sort(key=is_llm)  # real-LLM agent last: its failure must not block the others

    ready = []
    for wf in selected:
        if not webhook_path(wf):
            raise SystemExit(f"'{wf['name']}' has no webhook trigger, nothing to call")
        try:
            if not wf["active"]:
                print(f"Activating {wf['name']}")
                n8n.activate(wf)
            ready.append(wf)
        except SystemExit as err:
            if not is_llm(wf):
                raise
            print(f"WARNING: skipping {wf['name']} (add the Anthropic credential first): {err}")
    time.sleep(2)  # let the webhooks register

    print(f"Running {args.count}x each of: " + ", ".join(w["name"] for w in ready))
    for wf in ready:
        path = webhook_path(wf)
        for i in range(1, args.count + 1):
            try:
                status = n8n.trigger(path)
            except SystemExit as err:
                if not is_llm(wf):
                    raise
                print(f"WARNING: {wf['name']} failed, skipping the rest of its runs: {err}")
                break
            print(f"  {wf['name']}: run {i}/{args.count} (webhook /{path}, HTTP {status})")
            time.sleep(args.delay)

    print("Done. Give Prometheus about 15 seconds, then open http://localhost:3000")


if __name__ == "__main__":
    sys.exit(main())

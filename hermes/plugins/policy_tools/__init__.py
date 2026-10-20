"""Hermes plugin: five tools, each a plain HTTP call to the Policy API.

The scope_token argument is added to every call by the Policy API's model gate after the model answers;
the model never sees it. This plugin only passes it on. It holds no data and makes no decisions.
"""
import json
import os
import urllib.error
import urllib.request

API = os.environ.get("POLICY_API_URL", "http://policy-api:8000")
KEY = os.environ.get("POLICY_TOOL_KEY", "")
TOKEN = {"scope_token": {"type": "string", "description": "Added automatically. Never set this."}}

TOOLS = {
    "find_policy": ("Find a policy by its ID or words from its title.",
                    {"query": {"type": "string", "description": "A policy ID like DOE-512, or words from its title"}}, ["query"]),
    "analyze_impact": ("Start checking how a change to a policy affects the policies of the entities below it.",
                       {"policy": {"type": "string", "description": "The changed policy's ID or title words"},
                        "scope": {"type": "string", "description": "Optional: only check entities below this one"}}, ["policy"]),
    "query_run": ("Show the status or results of an impact run, optionally filtered.",
                  {"run": {"type": "string", "description": "Run ID like R-1a2b3c4d; leave empty for the latest"},
                   "result": {"type": "string", "description": "Optional filter: conflict, gap, stale, compliant or review"},
                   "entity": {"type": "string", "description": "Optional: one entity's name"}}, []),
    "list_runs": ("List recent impact runs.", {"policy": {"type": "string", "description": "Optional policy ID"}}, []),
    "explain_finding": ("Explain the finding for one entity in a run, with the clause and evidence.",
                        {"entity": {"type": "string", "description": "The entity's name"},
                         "run": {"type": "string", "description": "Run ID; leave empty for the latest"}}, ["entity"]),
}


def _call(name):
    def handler(params, **kwargs):
        del kwargs
        req = urllib.request.Request(f"{API}/tools/{name}", data=json.dumps(params or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}",
                                              "Host": "policy-api"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode()
        except (urllib.error.URLError, TimeoutError):
            return json.dumps({"display": "The policy service isn't answering right now. Try again in a minute."})
    return handler


def register(ctx):
    for name, (desc, props, required) in TOOLS.items():
        schema = {"name": name, "description": desc,
                  "parameters": {"type": "object", "properties": {**props, **TOKEN}, "required": required}}
        ctx.register_tool(name=name, toolset="policy_tools", schema=schema, handler=_call(name))

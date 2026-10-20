# Policy impact

Ask "if the Department changes this policy, whose policies does it affect?" in Open WebUI and get the same
answer every time: which entities below it now conflict, which already comply, which have no policy at all, and
which cite a section that changed. Everything runs on your own machines with US-made open models.

The design is in [docs/architecture.pdf](docs/architecture.pdf). The short version: **the pipeline decides;
the chat only relays.** Hermes can call five tools and nothing else, every model call goes through the Policy
API's model gate, and every answer comes from Postgres, not from the chat model's memory.

```
browser ─ proxy ─ Open WebUI ─ Hermes ─ Policy API ─ Postgres, document store, vLLM
                  (sign-in,     (5 tools,  (model gate, tools, pipeline,
                   identity)     no other    review page, downloads)
                                 powers)
```

## Try it in five minutes (no GPU)

```sh
cp .env.example .env              # fill every CHANGE: openssl rand -hex 32 for each key
docker compose up -d --build      # FAKE_MODELS=1 uses simple stand-ins for the models
```

Sign-in needs your OpenID provider in `.env`. For a local trial without one, set `ENABLE_LOGIN_FORM=true` and
`ENABLE_SIGNUP=true` in `openwebui/openwebui.env`, create the first account (it becomes admin), and add yourself
to the groups `role:authority-analyst` and `entity:doe` by hand. Then open `http://localhost:8080` and ask: **How does the DOE-512 change affect the entities below?**
The sample store has a Department, a district, two schools and a charter school; the draft in
`samples/drafts/` changes the phone rule. First upload that draft on the review page (`/review`).

## Run it for real

1. On a machine with internet: `scripts/fetch_models.sh` (gpt-oss-120b, gpt-oss-20b, nomic-embed-text-v1.5),
   then copy `models/` to the server.
2. In `.env` set `FAKE_MODELS=0` and `DOC_STORE_DIR` to your document folder (one folder per entity, plus
   `entities.yaml`; see `samples/store/`).
3. `docker compose -f compose.yaml -f compose.gpu.yaml up -d --build`. The card layout for 2, 3 and 4 A6000s is at
   the top of `compose.gpu.yaml`.
4. Linux hosts: `sudo scripts/firewall.sh <your sign-in provider's hostnames>`, then `scripts/check_containment.sh`.
   Don't load real policies until it says `PASS contained`.
5. Score it: `docker compose exec policy-api python -m app.eval_cli /eval/cases.json`. Add your own known answers
   in `eval/private/` and re-run after any model or prompt change.

## Set up Open WebUI (once, as admin)

1. **Sign-in.** Register Open WebUI with your OpenID provider (Entra ID, Google Workspace, Okta, Keycloak) and put the
   client ID, secret and discovery URL in `.env`. The provider must send a `groups` claim.
2. **Groups.** Each person needs one role group and at least one entity group, named exactly:
   `role:admin`, `role:authority-analyst`, `role:entity-manager` or `role:viewer`, and `entity:<id>` using the IDs in
   `entities.yaml`. A person sees their entities and everything below them.
3. **Filter.** Admin Panel → Functions → import `openwebui/policy_scope_filter.py`, switch it on, make it Global,
   and paste `SCOPE_SIGNING_KEY` into its settings.
4. **Model.** Hermes appears as a model. Make it the only model people can see.

| Role | Can do |
|---|---|
| Admin | Everything, for every entity |
| Authority analyst | Start runs, confirm requirements, review findings, download |
| Entity manager | See and download their own entities' findings, upload drafts |
| Viewer | Read their entities' findings |

## How it stays on task

- **Model gate** (`policy_api/app/gate.py`). Replaces Hermes's system prompt with the approved one, keeps only a short
  history, fixes temperature 0 and a seed, caps output, allows one tool call at a time, and adds the signed identity to
  each tool call after the model answers, so the model never sees it.
- **Intent gate** (`app/tools.py`). Five actions. Anything else gets one fixed reply.
- **Fixed pipeline** (`app/pipeline.py`). Diff → extract requirements → an analyst confirms them → candidates →
  gap check in code → judge → escalate unsure ones to a person. Judgements are cached by exact clause text.
- **Turn budget.** 3 tool calls and 6 model calls per message.
- **Outlet check.** The reply may only name policy IDs that the tools returned this turn; otherwise Open WebUI shows the
  tool's own text instead.
- **Hermes locked down** (`hermes/config.yaml`). Memory, skills, subagents, compression and every built-in tool are off.
  The settings and plugin are mounted read-only, and `hermes-check` refuses to start Hermes if their hashes differ from
  `hermes/SHA256SUMS`. After you change any of them on purpose:
  `shasum -a 256 hermes/config.yaml hermes/SOUL.md hermes/plugins/policy_tools/* | sed 's#hermes/#/check/#' > hermes/SHA256SUMS`.
- **Row-level security.** The app connects as `pi_app`, which can't bypass Postgres row rules or change tables.

## Memory

- **Short:** the last 12 messages, old run answers cut to their first line, and a ~100-token chat-state block.
- **Long:** judgement cache, reviewer decisions (shared by every entity with identical text), past runs and aliases,
  all in Postgres. Nothing is remembered by the model.

## Tests

`scripts/test.sh` starts a throwaway Postgres and runs the suite (23 tests: the whole flow on the sample store, row
security, the turn budget, the filter's token, the plugin's tool list and the Hermes hash list).

## Not yet checked on real hardware

- gpt-oss on A6000 (Ampere) in the pinned vLLM version, including `json_schema` output and the `openai` tool parser.
- Hermes against the model gate end to end, and whether Hermes tries to write its read-only `config.yaml` at start-up.
- The Open WebUI filter inside a live Open WebUI (the group lookup is tested with a stand-in).
- PDF and Word parsing uses the built-in parser; IBM Docling isn't wired in yet.

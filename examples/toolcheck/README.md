# toolcheck

A **platform diagnostic** scenario: the smallest realm that exercises every mechanism the games
depend on. Two players (Ping, Pong) relay fruit words for two rounds; the Umpire verifies each
round with tools and ends the realm with a verdict. Runs in ~5–10 minutes on small models.

## What each part verifies

| Axis | How it's tested | Where to look |
|---|---|---|
| Agents see prior messages | each message must echo the previous speaker's word (`APPLE -> BANANA`) | broken echoes in the transcript |
| Turn management | referee opener gate, strict Ping→Pong rotation, round pause for the umpire | `GET /api/realms/<id>/events?kind=turn` |
| Participant tool calls | each turn starts with `submit_sealed` | `reveal` shows the sealed words; realmtools audit log |
| Referee toolkit | `reveal` → `score` → `scoreboard` → `eliminate` → `rule` every game | `?kind=score`, `?kind=elimination`, `?kind=verdict` events |
| Verdict termination | `rule()` + `verdict_ends_realm` ends the realm (no message parsing) | realm `outcome` is set |

## Reading the results

```sh
uv run arealm trace ~/.agentrealm/llm-trace.jsonl --realm <realm-id>       # raw LLM I/O per agent
curl -H "Authorization: Bearer $(cat ~/.agentrealm/api-token)" \
  :8000/api/realms/<id>/events?kind=elimination        # tool-based ejection
ls ~/.agentrealm/realms/<id>/logs/                                         # agent flight-recorder logs
```

A clean run ends with: score events for clean relays, one `eliminate('none')`, one real
elimination, one verdict, `outcome` set — in under 10 minutes.

# Contributing

Thanks for taking a look. This document is short because most of what matters is enforced by CI —
what follows is the part that isn't.

## Setup

The package manager is [uv](https://docs.astral.sh/uv/). Do not use pip or poetry.

```sh
uv sync                  # install
uv run pytest            # tests
uv run ruff check .      # lint (line length 100)
uv run mypy              # type check (strict)
uv run pit --help     # the CLI
```

All four must pass before a PR is ready. `mypy` runs in strict mode; new code is expected to be
fully annotated.

Running an actual realm additionally needs Docker and the stack in `deploy/`. See the README
quickstart.

## Read this before changing a scenario or the control loop

**`docs/scenario-contract.md` lists 18 invariants. Each one was paid for by a failed live run.**

They look arbitrary until you have watched the failure. A referee that can end a realm before the
minimum round count produces a verdict on no evidence; a turn window sized for a fast model lapses
mid-turn on a slow one and silently skips an agent; a tool call counted as a turn statement gets an
agent ejected for "not voting" when it was reading a skill. If you are touching `warden/turns.py`,
`realmtools/`, or anything under `examples/`, read the contract first.

If you believe an invariant is wrong, say so in an issue — but change it deliberately, not by
accident.

## Architectural decisions

`docs/architecture.md` §2 (design principles) and §6 (the four control boundaries) are locked. A
change that contradicts them needs an ADR in `docs/adr/`, not just a PR.

The short version of what those principles rule out:

- **No mid-run agent steering.** Agents are configured at birth. After that: influence by message,
  control by kill. Do not add a "reconfigure this agent" path.
- **Enforcement happens at exactly four boundaries** — model proxy, message bus, filesystem,
  container. If a rule needs enforcing somewhere else, that is a design discussion, not an
  implementation detail.
- **Physics vs law.** A rule is either technically impossible or it is forbidden-but-possible and
  refereed. The *scenario author* chooses which. Do not helpfully promote a law into physics.

## Tests

The convention is Protocol-for-IO with in-memory fakes — see `tests/fakes.py`. Unit tests do not
touch the network, Docker, or a real model.

Write the test so the failure explains itself. A test named
`test_a_flat_rate_pipeline_lifts_a_too_tight_budget_cap` with a docstring recording *why* beats a
comment-free assertion, because the next person to see it fail will be someone who was not here
when it was written.

Two guards protect the repository itself and will fail a PR that trips them:

- `tests/test_public_surface.py` — the repo must stay free of certain private-fork vocabulary.
- `tests/test_deploy_env.py` — the deploy stack must not gain a weak default or an undocumented
  required variable.

## Commits and PRs

Imperative subject line; the body explains **why**, not what — the diff already says what. Small,
focused commits.

By contributing you agree your contribution is licensed under Apache-2.0, per §5 of the license.
No CLA.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

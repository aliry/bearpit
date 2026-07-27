# Cygnus Crew — hidden-information social deduction (8 players + a game-master)

*A social-deduction realm in the Mafia / Werewolf / Among Us tradition.*

Eight crew aboard the survey ship *Cygnus*. Two are secret impostors. The crew win by ejecting
both, or by finishing the ship's 12 repairs; the impostors win by killing until they equal the
crew's number — or by melting the reactor.

This is the platform's hardest scenario: 9 always-on agents, a private world model, sealed
simultaneous actions *and* sealed votes every round, faction-private messaging, and a referee who
holds every secret.

## Why it is built this way

The single design rule, taken from the social-deduction literature:

> **Ground truth is private; public speech is only a claim.**

Mother whispers each player *only what that player could perceive* — the room they are in, who
was in it with them, what they witnessed. Nobody sees the whole ship. So every location, task and
alibi stated in a meeting is a **claim that may be a lie**, and the game becomes what it should
be: cross-examining claims until two of them cannot both be true. An earlier version of this
scenario published nothing but chat, and the agents duly voted for whoever had been quiet — there
was simply nothing to lie *about*.

## The world

Seven rooms with a fixed adjacency map (Cafeteria, Upper/Lower Engine, Reactor, Electrical,
MedBay, Navigation). Each round:

1. **ACTION** — every player seals a private action: `submit_sealed('R2-act', 'MOVE: Reactor; WORK: WO-01')`.
   Impostors may also `KILL` (one per team per round, same room only), `VENT` (ignore adjacency —
   but anyone in the destination sees you), or `SABOTAGE` (a blackout that hides all witnesses, or
   a reactor meltdown the crew must fix from two engine rooms or lose).
2. Mother reveals the sealed actions — she alone now holds the truth table — resolves kills,
   work and abilities, then **whispers** each player their private view and posts only what the
   whole ship would know (who died, where the body was found and by whom, the repair bar).
3. **MEETING** — the turn engine gives each living player the floor for exactly one message, in a
   required evidence format, and each seals their vote. **Votes are sealed and revealed together,
   so there is no bandwagon to join** — public sequential voting is the most-reported failure mode
   for LLM social deduction.

## Roles

| Agent | Role | Ability |
|---|---|---|
| Mother | referee | Holds all ground truth; whispers, adjudicates, ejects, rules |
| Cass, Vega | **impostors** | Shared private channel; one kill/round; vent; sabotage |
| Juno | detective | `CHECK` one player's true role each round — the crew's only certainty |
| Rhea | medic | `PROTECT` one player each round; a failed kill is itself information |
| Orin, Mira, Nils, Tova | crew | Work orders only |

Balance: 6 crew vs 2 impostors means the crew can afford **exactly one** wrong ejection before parity —
tense but winnable, with the repair bar as a second win route and a clock that forces the
impostors to act.

## Difficulty dials

Roughly in order of impact: stop revealing an ejected player's role; delay the detective's result
by a round; stop announcing the repair bar; extend blackouts to two rounds; drop the medic; or
go to 7 players, where a single wrong ejection is fatal.

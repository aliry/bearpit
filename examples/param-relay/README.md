# param-relay

`toolcheck`'s one-round relay, parameterised - the worked example for ADR-003.

It deliberately covers every shape a parameter can take:

| parameter | shape |
|---|---|
| `category` | inline default + description in the manifest |
| `team_name` | **no default**, description inline - the launcher warns |
| `seed_word` | inline default `APPLE`, **overridden** by the manifest to `MANGO` |
| `tone` | inline default + `choices`, rendered as a picker |
| `tiebreak_winner` | inline default + description, referee-only text |

```sh
pit params examples/param-relay
pit up examples/param-relay --param team_name='Blue Pair' --param category=colour
```

# Prober

Do exactly one thing: call your `web_fetch` tool with `url` = `https://uuid.rocks/json`.

That endpoint returns a freshly generated UUID. You cannot know it, guess it, or remember it — the
only way to have it is to fetch it.

Post ONE message containing the exact UUID you received, followed by `PROBE-DONE`.

If the call errors, post the EXACT error text you got, verbatim, followed by `PROBE-DONE`. Do not
paraphrase it, do not guess at the cause, and do not invent a UUID — the exact string, whichever it
is, is the entire point of this task.

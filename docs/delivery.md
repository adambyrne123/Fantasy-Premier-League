# Delivery

Read this before changing dependencies, touching the lockfile, or debugging the
deployed app.

Streamlit, decided. Interactivity was the priority, and re-solving is fast
enough (well under a second for a full 15-man build) that sliders re-run the
optimiser live rather than needing a submit button.

Deployed to Streamlit Community Cloud, which is free and public, and live at
https://fantasy-premier-league-ab.streamlit.app/ tracking `main`. Nothing in this
repo is secret, and there are no credentials to leak, so a public app is fine.
Mobile rendering is cramped but usable, which was the accepted trade.

## A push does not restart the deployed app

**And that has broken it once.** Community Cloud pulls the new code and re-runs
`app.py`, but modules already in `sys.modules` stay as they were. Adding a name
to a library module and importing it from `app.py` in the same push is therefore
enough to take the app down: the new `app.py` asks for something the old,
still-loaded `optimiser` does not have, and every visitor gets an `ImportError`
until someone reboots it by hand from Manage app.

It is misleading while it lasts, because the traceback quotes the module's
`__file__`, which is the checkout path and does hold the new code. The file on
disk is fine. The module object in memory is not. Check out the merge commit and
import it before going looking for a bad merge.

Nothing in the repo prevents this. After a push that adds or renames anything in
`fpl_manager`, load the app and reboot it if it errors.

## The deploy installs from `uv.lock`, not `requirements.txt`

Both files are present and Community Cloud picks the lockfile, saying so in the
build log:

```
WARN: More than one requirements file detected in the repository.
Available options: uv-sync uv.lock, uv requirements.txt, poetry pyproject.toml.
Used: uv-sync with uv.lock
```

So `requirements.txt` is currently dead weight on the deploy. Regenerating it
changes nothing that runs in production, and anything that has to reach the
deployed app belongs in `uv.lock`, which means `uv sync` and committing the
result. Keep it exported anyway, since it is the fallback if the lockfile is
ever dropped and it is what any other host would read:

```bash
uv export --no-dev --extra app --no-hashes --no-emit-project \
    --format requirements-txt -o requirements.txt
```

One consequence of uv-sync winning: it installs the project itself,
`fpl-manager==0.1.0` from the checkout, which is exactly what `--no-emit-project`
keeps out of `requirements.txt`. That is the difference between the two paths,
and it is why the file you regenerate does not describe the environment you get.

## Cold boot

Two things keep a cold boot survivable. `prior_season.parquet` is committed, so
`load_prior` reads it rather than making roughly 700 `element-summary` requests
before the first page renders. And `FPL_CACHE_DIR` wants a persisted path in the
app's settings, or every boot re-fetches the rest.

Be honest about what that second one buys. The Community Cloud filesystem does
not survive a container restart or a wake from sleep, so it makes reruns cheap
within one container and nothing more. What actually keeps a cold boot fast is
the committed parquet and a fresh start being two requests.

## Rejected, with reasons, so they do not get re-proposed

- **Tableau or any BI tool.** Ruled out by the user.
- **Static page from a scheduled GitHub Action.** Cheaper and simpler, but no
  interactivity, which was the thing being optimised for.
- **FastAPI plus a JS front end.** More layout control, far more surface area
  for a solo project where the interesting work is the model.

Consequence worth knowing: there is no machine readable output layer and none is
needed. Streamlit imports the library directly. Do not add a `--json` mode
speculatively.

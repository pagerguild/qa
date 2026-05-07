# qa

Local runner for pagerguild QA agents.

```sh
uv run https://raw.githubusercontent.com/pagerguild/qa/main/qa.py \
  --target http://localhost:5173 --here
```

Runs every `.qa/<agent>/` task in your repo against any URL, in
parallel matrix containers, using the same workflow that drives CI.
Output streams into the team's Supabase reader at
https://qa.guys.dev.guilde.ai.

## Prereqs

- `uv` — `brew install uv`
- `gh` — `brew install gh && gh auth login`
- `doppler` — `brew install dopplerhq/cli/doppler`, `doppler login`,
  and `doppler setup` from a directory scoped to a Doppler config that
  has the QA agent's secrets
- `act` — `brew install act`
- Docker Desktop, running

## Usage

```
qa --target URL (--here | --path DIR | --repo OWNER/NAME [--branch B])
   [--qa-dir DIR] [--verbose]
```

- `--here` runs against the current directory's checkout.
- `--path DIR` runs against an existing checkout at `DIR`.
- `--repo OWNER/NAME` clones the repo fresh into a temp dir and uses
  that. Add `--branch` to pin a branch.

`localhost` URLs get rewritten to `host.docker.internal` automatically
so the agent inside the container can reach your dev server.

On Apple Silicon, native arm64 containers are selected automatically.

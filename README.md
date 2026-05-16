# mediabooboo

Template and instructions for your agent to set up and maintain your home media server/library for you.

> [!CAUTION]
> This repo contains instructions written in markdown.
> Your agent WILL read those. It will run the commands for you.
> **ALWAYS** check the commands it runs (at least), better yet, read these docs, it's not much reading.
> I know you won't, but still.

## What this is, what this isn't

- **Is:** reference compose, conventions, troubleshooting, library tooling. Source material for an agent to generate your deployment.
- **Is not:** a runnable deployment. Files here are examples, patterns and instructions, not configuration to clone-and-go.

## Use

1. Clone somewhere stable:
   ```
   git clone https://github.com/dmk/mediabooboo.git ~/.local/share/mediabooboo
   ```
2. Point your agent at it (best in plan mode):
   > How do I set up my home media server here?
3. Answer the agent's questions, including the stack name. It writes (smth like) `~/Services/mystack/` with your `compose.yaml`, `.env`, `Caddyfile`, plus `.mediabooboo/answers.yaml` and `.mediabooboo/source.yaml`. 

> [!NOTE]
> By this point, your agent should've instructed you there's manual stuff to do and ran some checks. If it hasn't - push back on this, it has to check everything runs fine and dandy for you.

**Updates**: ask the agent to reconcile the deployment. It reads `.mediabooboo/source.yaml`, pulls the template checkout, regenerates into a scratch dir, and shows you the diff before applying changes.

The agent flow lives in `CLAUDE.md`/`AGENTS.md` but that's for them (doesn't mean you shouldn't read it at least once after every `git pull`, that's just common sense).

## Scripts

Read `--help` for each before running anything.

* `encode.py` - downscales any movie/episode in given dir to 720p + only keeps single UA and single EN audio track. Saves space, basically. Saved me like 150 gigs on the simpsons (not the series, just some random 36 dirs of videos of my family). 
* `library.py` - fancily print your lib + show how much space `encode.py` saved.

Both scripts assume the encoding policy described for `encode.py`, probably smth i'll parameterize later, definitely a change any agent would be able to one-shot.


# arena for opencode

Arena makes many solutions to one task compete. Every competitor gets the same brief and a
different reasoning mode, workflow and strategy card. They attack and revise each other in a
single-elimination bracket, and a judge scores every match against a written rubric until one
solution is left.

This is the opencode port of the Claude Code arena skill. The bracket logic and the five prompt
templates are unchanged.

The whole thing is one package. The skill, the state machine, the router plugin, the model roster
and the tests live here, and the install script only puts links to them where opencode looks.

## How it works

1. **Spawn.** N sub-agents, one task call each. Every one gets the same task text, byte for byte
   (a test checks it), plus one strategy card. There are 15 reasoning modes (first principles,
   inversion, analogy, adversarial, constraint first, worked example, Socratic, contrarian, systems
   thinking and more), 12 workflows (draft, critique, rewrite; outline first; test first; research,
   then synthesise; three drafts, pick one) and 12 strategies (simplest thing that works, maximal
   rigour, user empathy first, edge cases first, speed). That is 2,160 different cards. The dealer
   gives each agent its own, with no repeats.
2. **Attack.** Solutions are paired, and where it can the pairing avoids putting two agents with
   the same reasoning mode against each other. Each side attacks the other's solution through its
   own card: what is wrong, what requirement it missed, the exact input that breaks it. Up to 7
   attacks, labelled FATAL, MAJOR or MINOR.
3. **Defend.** Each side answers every attack it took, conceding or rebutting with evidence, then
   rewrites its solution to fix everything it conceded.
4. **Judge.** A separate judge sub-agent reads both revised solutions, checks every attack itself,
   and scores both on [the rubric](rubric.md): correctness 30, completeness against the task 25,
   robustness to the attacks raised 20, specificity 15, clarity 10. The judge never sees the cards.
   `bracket.py` does the arithmetic: the higher total goes through, and a solution with a verified
   fatal flaw cannot beat one without. The loser is out.
5. **Repeat.** The survivors carry their revised solutions into the next round. An odd number means
   one bye, and the bye goes to the agent that has had the fewest so far.
6. **Result.** One solution left. You get it, the attacks it survived, its card and the round count.
   If you started from an answer you rejected, a last judge compares the winner with it blind and
   the skill tells you the score, even when the old answer wins.

The 100-agent bracket, from `python3 bracket.py plan`:

```
  round  alive  matches  bye  sub-agent calls  waves
  spawn    100        -    -              100     10
      1    100       50    -              250     25
      2     50       25    -              125     13
      3     25       12  yes               60      8
      4     13        6  yes               30      5
      5      7        3  yes               15      3
      6      4        2    -               10      3
      7      2        1    -                5      3
  total                                 595     70

  alive per round: 100 -> 50 -> 25 -> 13 -> 7 -> 4 -> 2 -> 1
```

The whole tournament lives in one JSON file, managed by `bracket.py`. The main session only runs
the loop and never reads the hundreds of solution files: every sub-agent writes its work to disk
and replies with one line, so the session holds receipts and bookkeeping, not the work. If the
conversation gets compacted halfway through, `bracket.py next` picks it up from the file.

### What it costs

The tokens are yours. Every sub-agent call reads the task and one or two solutions, so the bill
grows with the size of the task.

| agents | rounds | sub-agent calls | waves of 10 |
| --- | --- | --- | --- |
| 100, the default | 7 | 595 | 70 |
| 64 | 6 | 379 | 49 |
| 32 | 5 | 187 | 28 |
| 16, `--quick` | 4 | 91 | 16 |
| 8 | 3 | 43 | 10 |

Add one call when there is a rejected answer to beat. `arena estimate` prices the run in tokens and
money before it spends anything, and the approval gate asks before the first sub-agent starts.

## Install

### As an opencode plugin

The package is a plugin as well as a skill. Add it to the `plugin` array in your opencode config
(`~/.config/opencode/opencode.json` or `opencode.jsonc`):

```jsonc
{
  "plugin": ["opencode-arena-plugin"]
}
```

That is the whole install. opencode fetches the package from npm, and the plugin wires itself into
your config when it loads: it links the skill folder, the `/arena` command and the roster. Nothing
else has to run, and no install script is needed.

Restart opencode and run `/arena <task>`.

From a checkout on disk, point the entry at the folder instead:

```jsonc
{
  "plugin": ["file:///home/you/Code/skills/arena"]
}
```

### As a skill and a command

To get the `arena` command on your `PATH` as well:

```bash
npm install -g opencode-arena-plugin
```

That links the skill folder and the command into your opencode config and seeds the roster. The
router tools only come with the plugin entry above.

From a checkout of this repository:

```bash
npm install
npm run install:local
```

That links the package into your opencode config:

```
~/.config/opencode/skills/arena            -> <package>
~/.config/opencode/commands/arena.md       -> <package>/opencode/commands/arena.md
~/.config/opencode/arena-models.json       -> <package>/arena-models.json (copied, not linked)
```

`npm run install:local` also links the plugin file into `~/.config/opencode/plugins`, so opencode
loads it from there. Do not do that when the package is in the plugin list as well: opencode would
load the plugin twice.

The install script takes `--config-dir <dir>`, `--copy` instead of links, `--dry-run` to print what
it would do, `--plugin` to link the plugin file, and `--register` to add the plugin entry to your
opencode config for you.

### Without an npm account

Every route below works without an npmjs account.

A tarball, installed from disk:

```bash
npm pack
npm install -g ./opencode-arena-plugin-1.0.0.tgz
```

A local checkout, installed as a link:

```bash
npm install -g file:/home/you/Code/skills/arena
```

A private registry you host yourself:

```bash
npm config set registry https://registry.your-host.tld
npm publish
npm install -g opencode-arena-plugin
```

The package name is unscoped on purpose, so it fits any registry without a scope.

### Requirements

- opencode 1.19 or newer
- Python 3.8 or newer on `PATH`, as `python3`
- Node 18 or newer, for the install script and the plugin dependencies

`bracket.py` uses only the standard library. On Windows, run opencode in WSL: the package needs a
`python3` on the `PATH`, and opencode installs npm plugins in the Linux side of it.

### Check the installation

```bash
arena doctor
```

It checks python, the strategy and rubric files, opencode itself, the plugin, the model roster and
the links. Everything must read `ok`.

## Plugin options

The plugin takes its options from the opencode config. A plugin entry is either a string or a pair
of the plugin name and its options:

```jsonc
{
  "plugin": [
    ["opencode-arena-plugin", { "maxRoster": 12, "registerAgents": true }]
  ]
}
```

| option | default | what it does |
| --- | --- | --- |
| `maxRoster` | 16 | how many models get a visible agent, capped at 16 |
| `registerAgents` | true | set it to false to keep the tools but register no agents |
| `rosterPath` | the config dir | read and write the roster somewhere else |

Set `ARENA_NO_LINK=1` in the environment when the plugin must not touch your opencode config at
all. It then only exposes the tools and registers agents from the roster it can already read.

## The model roster

opencode registers one visible subagent per model, so a native task call can be pinned to a model.
The list of those models is the roster, and the roster is a file.

There are two of them. The live one is `arena-models.json` in your opencode config dir; it is the
file the plugin reads at startup and rewrites when the model list changes. The packaged one sits
next to `bracket.py` and is the default a fresh install starts from.

The roster is never guessed. It is read from the live model list:

```bash
arena roster --refresh
```

That asks opencode for the models it can reach right now, keeps the free, tool-capable, live ones,
drops what is deprecated or sits behind a Go plan, and rewrites the live file. Without `--refresh`
it shows the roster as it is, and flags the models in it that are no longer live. Add `--package` to
write the packaged default instead, which is what a fresh install of the next version starts from.

The plugin reads the roster when opencode starts and registers one agent per row, so a refresh
lands in the next session. The agent name is derived from the model id, so
`opencode/nemotron-3-ultra-free` becomes `arena-nemotron-3-ultra-free`. A model without a row has no
agent, and a native task call for it fails with "unknown agent type".

## The commands

`arena` forwards to `bracket.py`, which holds the whole state machine.

| command | what it does |
| --- | --- |
| `arena doctor` | check the install, the plugin and the roster |
| `arena roster --refresh` | rebuild the roster from the live models |
| `arena plan --agents 100` | rounds, sub-agent calls and waves |
| `arena estimate --agents 100 --task-file task.md` | tokens and cost before spending anything |
| `arena init --agents 100 --seed 7 --task-file task.md` | deal the cards, pair round 1, write the state |
| `arena next` | what to do now, and the exact command for it |
| `arena prompts spawn --json` | write the briefs, list the jobs and their models |
| `arena collect`, `arena advance` | read the verdicts, close the round, pair the survivors |
| `arena report --json`, `arena winner` | the per-model record and the survivor |
| `arena fallback <job> <model>` | record a job that ran on another model |
| `arena drop <model> <replacement>` | drop a dead model and move all its jobs |

## The approval gate

Arena always estimates before it spends anything. The estimate uses the real task file and the
chosen models, and reports low, expected and high token counts plus the cost of every model it
recognizes. A free model costs $0.00. An unknown model gets tokens but no price. Each price row is
the cost if that model ran the whole tournament; a mixed roster splits jobs across models, so the
total is the weighted sum of the rows that are used.

Nothing runs until the user says yes. `init --approved` records that answer, so the run does not
ask twice.

## When a model fails

A model that times out, errors or asks for a Go plan is dropped for the rest of the run, and its
jobs move to a free model that answers. `arena_task` does this by itself and tells you what it
moved from and why. With the native task tool you do it by hand: `arena_substitute` for a
candidate, then `arena fallback <job> <new-model> --reason "..."`, then
`arena drop <model> <new-model>` for the rest of that model's jobs. The run is marked degraded
either way, and the report says so.

## Layout

```
SKILL.md              the skill, with the five prompt templates
bracket.py            the tournament state machine and the command line
strategies.json       the reasoning modes, workflows and strategies the cards are built from
rubric.md             how the judges score, and the weights
arena-models.json     the packaged roster: one row per model with a visible agent
plugin/               the router plugin, with the arena_models, arena_task and arena_substitute tools
opencode/agents/      the worker agent the plugin registers
opencode/commands/    the /arena command
bin/arena.js          the arena command on your PATH
scripts/              the install script and the pre-publish check
tests/                the test suite
```

## Development

```bash
npm test          # python unittest suite and the node plugin tests
npm run doctor    # check this machine
npm run typecheck # typescript, strict
npm run prepublishOnly   # what runs before a publish
```

The plugin is TypeScript and loads straight from source. opencode bundles it when it starts, so
there is no build step.

## Credit

The arena skill was made by [Jake Schincariol](https://opusjake.ai). This repository is the opencode
port of it: the bracket logic, the rubric, the strategy cards and the five prompt templates are his
work, and the npm package, the router plugin and the model roster are the port.

## License

MIT

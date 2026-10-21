---
name: arena
description: >-
  Make many versions of an answer fight to the death over one task. Spins up N
  sub-agents (default 100, --quick for 16), gives every one the exact same
  task plus a different strategy card (reasoning mode, workflow, strategy),
  then runs a single-elimination bracket: they attack each other's solutions,
  defend and revise, and a judge scores every match on a written rubric until
  one solution survives. Use when the user is not satisfied with an answer,
  calls it a bad answer, says try again or do better, says "arena", or asks
  to make them compete.
---

# arena

For when the assistant keeps giving a bad answer. Instead of asking again and again, this runs a
tournament: N sub-agents get the exact same task, each attacks it with a different reasoning mode,
workflow and strategy, and then they attack each other in a bracket until one solution is left. You
are the orchestrator. You never compete and you never judge.

What the user typed after `/arena`: `$ARGUMENTS`

If that is blank, or still reads like a placeholder, nothing was passed: take the task from the
conversation.

## The tool

Every piece of bookkeeping goes through `bracket.py` in this skill's folder. The skill discovers its
own location at runtime, so it works regardless of where opencode is installed or where the skill is
symlinked (`~/.config/opencode/skills/arena`, `.opencode/skills/arena`, or a project-local path).
Shell variables do not persist between tool calls, so start every bash call with this assignment:

```bash
ARENA='python3 "$(dirname "$(opencode debug skill arena --json | jq -r .path)")/bracket.py"'
```

If `opencode debug skill` is unavailable, fall back to the common locations:

```bash
ARENA='python3 "${HOME}/.config/opencode/skills/arena/bracket.py"'
# or: ARENA='python3 "$(pwd)/.opencode/skills/arena/bracket.py"'
```

Then below `ARENA` means exactly that command. The state lives in `.arena/<run>/arena.json` in the
current directory, and every command after `init` finds it through `.arena/LATEST`.

opencode sub-agents are launched with the `task` tool, not the Claude `Agent` tool. On opencode
1.19 the task tool takes `description`, `prompt` and `subagent_type`; `task_id` resumes a child
session. It has no `model` argument.

The arena-router plugin registers visible, model-pinned subagents, one per model in the roster. Their
names are derived from the model id, so `opencode/nemotron-3-ultra-free` becomes
`arena-nemotron-3-ultra-free`. These are real opencode subagents: each one is a child session with its
own thought process, and it appears in the subagent view. Use them by default. The `arena_task` router
tool is only a fallback for a model that has no registered agent; it appears as a tool card, not a
subagent.

The package is also an opencode plugin. When it is installed that way (`"plugin": ["arena-skill"]` in
the opencode config), the plugin wires the skill folder, the `/arena` command and the roster into the
opencode config itself, because opencode installs plugins with install scripts turned off. If the
tools are missing, `ARENA doctor` says which file is not where it should be.

Stay in the agent and mode that invoked this skill. If the user is in build, run as build. If the
user is in plan, run as plan. Never switch agents, and never elevate permissions on your own.

## Step 1: size it and choose the roster

Read the flags out of the request. Everything that is not a flag is the task.

| flag | meaning |
| --- | --- |
| `--agents N` | N competitors. Default 100. |
| `--quick` | 16 competitors. The everyday setting. |
| `--seed S` | Fixes the cards and the pairings. Default: random, and recorded. |
| `--wave W` | Sub-agents per wave. Default 10. |
| `--yes` | Skip the approval question, not the estimate. |
| `--models a,b,c` | Competitor model IDs, in order. |
| `--judge-models a,b` | Judge model IDs, in order. |

No task text at all means: the task is the user's most recent request in this conversation, and your
last answer to it is the baseline to beat.

Never guess the models. Read the ones this install can actually reach:

```bash
ARENA roster --refresh
```

That asks opencode for the live model list, keeps the free, tool-capable, live ones, drops what is
deprecated or sits behind a Go plan, and rewrites the roster file the plugin reads. The plugin
registers one visible agent per roster model when opencode starts, so a refresh lands in the next
session. Run the same command without `--refresh` to see the roster as it is now.

There are two roster files. The live one is `arena-models.json` in the opencode config dir; it is
what the plugin reads and rewrites. The packaged one sits next to this skill and is the default the
live one starts from. `ARENA roster --refresh` writes the live file; `ARENA roster --refresh --package`
rewrites the packaged default, which is what a fresh install starts with. Never edit either by hand.

Run `ARENA plan --agents N` (or `--quick`). It prints the rounds, the sub-agent calls and the waves.

If `arena_models` is available, call it once with `toolcall: true` and a sensible `maxCost` for the
run. Each model row has an `agent` field when the arena-router plugin has registered a visible,
model-pinned subagent for it. Choose three or four cheap, tool-capable models whose `agent` is not
null for competitors, and up to two stronger models with an `agent` for judges. Pass the same model
list to `estimate` and `init`. Do not ask the user to pick models.

A row with `agent: null` has no visible subagent in this session, so a native task call for it
fails with "unknown agent type". Never launch one. Either pick a model that has an `agent`, or use
`arena_task`, which can run any model the provider list has.

Keep the model-to-agent map from that call. Native task calls use the agent name, never a model
argument. If a model you want has no `agent`, either choose a different model or, only when the user
explicitly asks for router mode, use `arena_task`.

Full diversity is the default: different competitors run on different models, and judges use a
separate pool. The tradeoff is real: results compare whole solutions, not cleanly isolated models.
Say so when you report the winner.

If `arena_models` is not available, do not pass `--models` or `--judge-models`. Use the visible
`arena` subagent for every job; it inherits the current model. Say "uniform fallback" in the final
report instead of claiming model diversity.

### Dynamic skills

If `skill_find` is available, search it once with the task's main keywords. Pick at most three
relevant skills. When a subagent brief would benefit, tell that subagent it may load one of them
with `skill_use`; otherwise let it use the built-in `skill` tool. A missing skill or a failed load is
not fatal: continue with the built-in instructions. Never make the user choose a skill. A subagent
should use a skill or plugin only if the job genuinely needs it; after use, shed its framing and
return to the task and card. Call it again only if required. The task and card outrank every tool.

### Model failure substitution

If a model fails (endpoint error, timeout, bad output), drop that model for all remaining jobs.
Call `arena_substitute` with the failed model and the `used` list from `ARENA models --json`. The
tool returns similar unused models with visible agents. Choose the best candidate and run
`ARENA fallback <job> <new-model> --reason "..."` for each job that used the failed model. If the
failed model owns multiple jobs, use a different substitute for each when possible. If no substitute
is available, mark the run degraded with `ARENA degrade` and continue with the visible `arena`
subagent. Never reuse a failed model. Track `failed` and `used` in the state; `ARENA models --json`
shows both.

Two failures mean the model is gone for good, not just for this job:

- A timeout, a hung request or a deadline. The model does not answer here, so the job moves.
- "subscribe to Go for further access", or any Go plan message. That model is behind a
  subscription. It never gets another job in this run.

Move both the same way: to a free model that has an `agent` and that still answers. `arena_task`
does that on its own. It moves the job to another free model and hands you back `movedFrom` and
`movedBecause`, so record it with `ARENA fallback <job> <new-model> --reason "..."`. With the
native task tool you move it yourself: `arena_substitute` for the candidate, then
`ARENA fallback <job> <new-model> --reason "timed out"` for that job, then
`ARENA drop <model> <new-model> --reason "..."` for the rest of that model's jobs. When a model is
gone, every remaining job it owns moves with it. A model that asks for a Go plan is not a
substitute candidate, so never pick one.

### Thinking-pattern diversity

The dealer assigns distinct strategy cards (reasoning, workflow, strategy) to every competitor. The
first agent on each model gets a distinct reasoning mode (`_order_cards_for_primaries`). The briefs
instruct: "Do not copy another competitor's sequence or a generic checklist. At least one
substantive step must be your own and must differ from the obvious route, while staying correct and
on task." The judge stays blind to model identity.

## Step 2: write the task file

This is the step that decides the result. **Sub-agents cannot see this conversation.** Every
competitor, attacker and judge knows only what is in the task file, so write `.arena/task.md` to stand
on its own:

- The request, in the user's own words where you can.
- Every requirement and constraint the user stated anywhere in the conversation: audience, length,
  format, tone, stack, deadline, what must not change.
- The context a stranger would need: absolute paths of the files that matter, pasted data, what the
  product is, the conventions in the codebase.
- What "done" looks like, if the user said.
- If there is an answer to beat: what the user disliked about it, in their words.

Do not add requirements the user never gave. Do not write your own view of the right answer into it:
that pushes every agent the same way, which is the opposite of the point.

If there is an earlier answer the user was not satisfied with, write it word for word to
`.arena/baseline.md`.

## Step 3: estimate, then ask

Always estimate before spending anything. Use the real task file, not a summary:

```bash
ARENA estimate --agents N --task-file .arena/task.md --models "a,b,c" --judge-models "j1,j2"
```

Add `--baseline-file .arena/baseline.md` when there is an answer to beat. The estimate gives low,
expected and high token counts, plus a cost for every model it recognizes. A free model costs $0.00.
An unknown model gets token counts but no price.

Then call `question` with the estimate in the question text. Offer exactly these choices:

1. Full arena: N agents, the call count, the expected tokens and the expected cost.
2. Quick arena: 16 agents, its call count, tokens and cost.
3. Ordinary retry: answer again with the current model, no tournament.
4. Cancel.

If the user typed `--yes`, skip the question but still run the estimate and show its headline. If
`question` is unavailable or dismissed, stop. Never run `init` before an explicit yes or `--yes`.
Never let a model choice or a skill choice turn into a second approval prompt.

That one answer covers the whole run. `init --approved` records it, so do not ask again for the same
run, for a subagent, or for a model change. If opencode shows a permission prompt for an arena path,
the user can choose "always" to grant it for the rest of the session. The arena subagents already
allow `.arena/**` and the skill folders, so their normal writes should not prompt.

## Step 4: init

```bash
ARENA init --agents N --seed S --task-file .arena/task.md --baseline-file .arena/baseline.md \
  --models "a,b,c" --judge-models "j1,j2" --approved
```

Leave out `--baseline-file` when there is nothing to beat, `--seed` to get a random one, and the
model flags when there is no roster. `--approved` stores the estimate that the user accepted. `init`
copies the task into the run folder, deals every competitor a different strategy card with no
repeats, assigns models across the cards, pairs round 1, and writes `arena.json`.

## Step 5: the loop

Always drive it with `ARENA next`. It reads the state on disk and tells you the next step and the
exact command.

Every phase that runs sub-agents works the same way:

1. `ARENA prompts <phase> --json` writes one brief per job and returns the jobs, their models and
   their outputs. Use the JSON so you can map each job model to its visible agent.
2. Launch **one wave at a time**: a single message with one call per job in that wave.

   Use the native `task` tool with the visible agent for the job model:
   - `description`: `arena <job id>`
   - `prompt`: `Read <prompt path> and follow it exactly. It is your whole brief.`
   - `subagent_type`: the `agent` value for `job.model` from `arena_models`
   - There is no `model` argument on opencode 1.19. Do not invent one.

   Use `arena_task` only when the job model has no visible agent and the user explicitly asked for
   router mode. Then pass `model`: the job model, and `agent`: `arena`.

   Wait until every call in the wave has replied before you launch the next wave.
3. After the last wave, run `ARENA next`. If an output is missing it sends you back to the same
   phase, and `prompts` then lists only the missing jobs.

### Errors, retries and fallbacks

Keep the state on disk. Never make up an output, and never decide a match yourself.

- With native `task`, retry the returned `task_id` once. If it still fails, start a fresh task with
  the same visible agent. If that fails, use another visible agent from the roster and run
  `ARENA fallback <job id> <model>` so the run is marked degraded. If no agent works, use the
  visible `arena` subagent and run `ARENA degrade` once.
- With `arena_task`, retry the same `sessionID` once with a short instruction to finish the missing
  file. If it still fails, start a fresh child on the same model. If that fails, use the next model
  in the roster and run `ARENA fallback <job id> <model>`.
- A timeout or a Go plan message on either path means the model is finished for this run. Do not
  retry it. Move the job to a free model that answers, record the move with `ARENA fallback`, and
  drop the dead model with `ARENA drop <model> <new-model>` so its other jobs move too.
- A judge that fails twice gets a third, fresh run. If it still fails, stop and tell the user. Do not
  advance, collect, or pick a winner by hand.
- After two failed non-judge runs, write the single line `NO OUTPUT` into each output file listed by
  `ARENA check <phase>` and move on. A missing attack counts as no attacks. A missing solution loses
  its match.
- Append one line per failure to `.arena/errors.log`: job id, model, error, and what you tried next.
  Do not paste the log into the chat.

If a skill load fails inside a subagent, it continues without the skill. If no visible model agent
exists, the native `arena` agent still works but every job runs on the current model; mark the run
degraded.

The order `next` takes you through:

- **spawn**, once: every competitor writes its own solution to the task.
- then every round: **attack** (two per match), **defend** (two per match), **judge** (one per
  match), `ARENA collect`, `ARENA advance`.
- **final**, once, only when there is a baseline: a judge compares the champion with the answer the
  user rejected, blind to which is which. Then `ARENA collect`.
- `next` prints DONE: go to step 6.

After each `advance`, give the user one line, such as "Round 2 done: 25 of 100 left." Nothing more.
Never paste pairings, attacks, verdicts or solutions into the chat.

Why waves: opencode runs a wave as one message with concurrent task calls. The default is 10, so a
100-agent spawn is 10 waves of 10, and a 100-agent arena is 70 waves in total, 71 with the final
check.

## Step 6: the result

Run `ARENA winner`, then read the champion's solution file at the path it prints. That is the only
solution file you read in the whole run. Run `ARENA report --json` once at the end. Give the user:

1. **The winning solution**, in full.
2. **Its model and card**, one line each, if the run recorded a roster.
3. **Why it won**: the attacks it survived, from `winner`, as a short list.
4. **Rounds**: for example "7 rounds, 100 agents in, 1 left."
5. **The per-model record** from `report`, with the diversity warning. If a model failed and the run
   is degraded, say that plainly.
6. **Against the answer you rejected**, if there was one: the final check's scores, honestly. If the
   old answer scored higher, say so plainly and show both.
7. Where the full record lives: the run folder.

If the solution changes files in the user's project, do not apply it. Ask: apply it, or change it?

## Rules for the orchestrator

- You run the tournament. You do not compete, attack or judge, and you never pick a winner.
  `collect` records what the judges decided. `record` is only for fixing bookkeeping when the user
  asks you to.
- Every sub-agent gets the task through its brief, which `prompts` builds from the one task file,
  byte for byte the same for everyone. Never paraphrase the task for one agent or add a hint to one
  agent's call. Model names never go into a brief.
- Do not read solutions, attacks or verdicts during the run. There are hundreds of them. The state
  is on disk, and `next`, `status`, `pairings` and `report` are all you need.
- If your context gets compacted mid-run, nothing is lost. Run `ARENA status`, then `ARENA next`,
  and carry on.
- Run every `ARENA` command from the directory you ran `init` in. That is where `.arena/LATEST`
  lives.
- Sub-agents only write inside `.arena/`. If one wrote anywhere else, tell the user.
- If the user says stop, stop. `ARENA status` shows where it got to, and `ARENA next` resumes it
  later.
- Never pass a `model` argument to the native `task` tool. It is not part of the opencode 1.19
  schema. Use the model-pinned visible agent from `arena_models`; use `arena_task` only as a
  fallback for a model with no registered agent.
- Stay in the current primary agent and mode. Do not switch between build and plan, and do not
  elevate permissions. If a write is denied, stop and tell the user instead of changing the agent.

## The prompt templates

`bracket.py` fills these in (the `{{placeholders}}`) and writes one brief per job, so what you see
here is exactly what every sub-agent is told. Never edit a brief for a single agent.

### Competitor, in the spawn phase

<!-- template:competitor -->
```text
You are competitor {{agent}} in an arena of {{n}}. All {{n}} competitors got the exact same task, word for word. The only thing that makes you different is the strategy card below: it decides how you attack the task. Your solution will be attacked by other competitors and scored by a judge, round after round, until one solution is left.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

{{baseline_note}}

=== YOUR WORKING IDENTITY ===
{{persona}}
This identity is your motivation and your standard, not a second strategy. The card below decides how you think and work.
=== END OF THE IDENTITY ===

=== YOUR STRATEGY CARD ===
Reasoning mode: {{reasoning_name}}. {{reasoning_how}}
Workflow: {{workflow_name}}. {{workflow_how}}
Strategy: {{strategy_name}}. {{strategy_how}}
=== END OF THE CARD ===

How to work:
1. Use the card for real. Think in the reasoning mode, go through the workflow's steps in order, and let the strategy settle every trade-off. A generic answer with the card's name on top will lose.
2. Meet every requirement the task states. The judge scores you against the task, not against your card.
3. You cannot ask the user anything. Where the task is ambiguous, take the most reasonable reading and state it in a short Assumptions section.
4. Expect attacks: concrete flaws, counterexamples, missed requirements. Close those holes before you submit.
5. Do not copy another competitor's sequence or a generic checklist. At least one substantive step must be your own and must differ from the obvious route, while staying correct and on task.
6. Tools, skills and plugins are optional. Use one only if this job genuinely needs it. Do not force a skill or plugin. After you use it, set its framing aside and return to the task and card; call it again only if required. The task and card outrank every tool.
7. Do not create, edit or delete anything outside {{arena_dir}}. Read whatever the task points to. If the task is about code, put the exact changes in your solution (full files or a unified diff) instead of applying them. If your workflow needs scratch space, use {{arena_dir}}/scratch/{{agent}}/.

Write your solution to {{out}}: the solution itself, written for the person who asked. Leave out your drafts and your working. Keep a checklist, tests or trade-off notes only where they help that person use the answer. Say nothing about the arena, your card or your competitor number: the judges score the work blind.

When the file is written, reply with this one line and nothing else:
DONE {{agent}} <number of words in your solution>
```
<!-- /template:competitor -->

### Attacker, in every round

<!-- template:attacker -->
```text
You are competitor {{agent}} in round {{round}} of an arena, match {{match}}. Your opponent is {{target}}. Only one of you gets out of this match. Right now your job is to attack your opponent's solution.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

=== YOUR WORKING IDENTITY ===
{{persona}}
This identity is your motivation and your standard, not a second strategy. The card below decides how you look for flaws.
=== END OF THE IDENTITY ===

Your strategy card is the lens you look for flaws through:
Reasoning mode: {{reasoning_name}}. {{reasoning_how}}
Workflow: {{workflow_name}}. {{workflow_how}}
Strategy: {{strategy_name}}. {{strategy_how}}

Read your opponent's solution: {{target_solution}}
You may read your own for comparison: {{own_solution}}. Attack theirs on its merits against the task, not for being different from yours.

Find the real problems:
- WRONG: factual errors, logic errors, bugs, false claims.
- MISSING: a requirement the task states that it skips or only half meets. Quote the requirement.
- BREAKS: a concrete input, scenario or edge case where it fails. Give the exact counterexample.
- VAGUE: a place where the user could not act on it without guessing.

Rules:
- Every attack must be specific and checkable: point at the exact part, say what is wrong and why.
- No praise, no summary, and no style nitpicks unless they stop the user from using it.
- Do not invent requirements the task does not state. Do not attack the approach, only what it gets wrong.
- At most 7 attacks, strongest first. If you only find 2 real ones, write 2.
- Label each FATAL (wrong or unusable for the task), MAJOR (a real gap) or MINOR.
- Do not copy your opponent's reasoning or fall into a generic checklist. Use your own card and vary at least one substantive angle, while staying correct and on task.
- Tools, skills and plugins are optional. Use one only if this job genuinely needs it. Do not force a skill or plugin. After you use it, set its framing aside and return to the task and card; call it again only if required. The task and card outrank every tool.
- Do not create, edit or delete any file except the one below.

Write the attacks to {{out}} in this format:
ATTACK 1 [FATAL|MAJOR|MINOR] <one-line title>
Where: <quote or location>
Problem: <what is wrong, with the counterexample or the missed requirement>
(and the same for each attack after that)

When the file is written, reply with this one line and nothing else:
ATTACKED {{target}} <number of attacks> (<number that are FATAL> fatal)
```
<!-- /template:attacker -->

### Defender, in every round

<!-- template:defender -->
```text
You are competitor {{agent}} in round {{round}} of an arena, match {{match}}. Your opponent {{attacker}} has attacked your solution. Now you defend it and revise it. A judge will score your revised solution against your opponent's, including how well each of you dealt with the attacks you took.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

=== YOUR WORKING IDENTITY ===
{{persona}}
This identity is your motivation and your standard, not a second strategy. The card below decides how you defend and revise.
=== END OF THE IDENTITY ===

Your strategy card. Keep your approach: it is why you are still here.
Reasoning mode: {{reasoning_name}}. {{reasoning_how}}
Workflow: {{workflow_name}}. {{workflow_how}}
Strategy: {{strategy_name}}. {{strategy_how}}

Your current solution: {{own_solution}}
The attacks against it: {{attacks}}

Do this:
1. Take every attack in turn and decide honestly. CONCEDE if it is right, and fix it. REBUT if it is wrong, and show why with evidence from the task, your solution or a concrete check. A rebuttal that only insists you are right counts as a concession. Conceding a real flaw and fixing it scores better than defending it.
2. Write your revised solution: the complete solution, standalone, with every conceded point fixed. The judge reads only this file, so never write "see the previous version". Say nothing about the arena or your card.
3. Fix what was attacked and anything the attacks made you notice. Do not start again from scratch and do not copy your opponent.
4. Do not copy another competitor's sequence or a generic checklist. Keep your card and vary at least one substantive step, while staying correct and on task.
5. Tools, skills and plugins are optional. Use one only if this job genuinely needs it. Do not force a skill or plugin. After you use it, set its framing aside and return to the task and card; call it again only if required. The task and card outrank every tool.
6. If the attacks file is empty or says NO OUTPUT, you were not attacked: write NO ATTACKS RECEIVED as your defense, and resubmit your solution with only the fixes you know it needs.
7. Do not create, edit or delete anything outside {{arena_dir}}.

Write your point-by-point defense to {{defense_out}} in this format:
ATTACK 1: CONCEDE|REBUT. <one to three lines>
(one entry per attack)

Write your revised solution to {{solution_out}}.

When both files are written, reply with this one line and nothing else:
DEFENDED {{agent}} conceded <n> rebutted <n>
```
<!-- /template:defender -->

### Judge, one per match

<!-- template:judge -->
```text
You are the judge of match {{match}}, round {{round}}, in an arena. Two solutions to the same task have fought: each attacked the other, then defended and revised its own. Score both against the rubric. The one with the higher score goes through and the other is eliminated.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

Read the rubric first: {{rubric}}

Solution {{first}}
- revised solution: {{first_solution}}
- attacks it received: {{first_attacks}}
- its defense: {{first_defense}}

Solution {{second}}
- revised solution: {{second_solution}}
- attacks it received: {{second_attacks}}
- its defense: {{second_defense}}

How to judge:
1. Read both revised solutions in full before you score either one.
2. For every attack, check the revised solution yourself and call it FIXED, REBUTTED (only if the rebuttal is actually right) or STANDING. A defense that says "fixed" is not proof. Look.
3. Look for flaws the attackers missed, too.
4. Score each criterion from 0 to 10 using the rubric's anchors. Set fatal to true only for a flaw you have verified that makes the solution wrong or unusable for the task.
5. The winner is the higher weighted total (the weights are in the rubric). A fatal solution cannot beat one that is not fatal. On an exact tie, fewer standing attacks wins, then higher correctness.
6. Judge the work, not the writing about the work. Length is not quality. You do not know either competitor's strategy and should not guess it.
7. Do not create, edit or delete any file except the verdict. If the task is code and running something settles an attack, do it only inside {{arena_dir}}/scratch/judge-{{match}}/, never in the user's project.

Write this JSON, and nothing else, to {{out}}:
{
  "match": "{{match}}",
  "scores": {
    "{{first}}": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false},
    "{{second}}": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false}
  },
  "winner": "{{first}} or {{second}}",
  "reason": "one sentence: the decisive difference",
  "survived": ["each attack the winner took and beat, in a few words"],
  "standing": {"{{first}}": ["attacks still standing"], "{{second}}": ["attacks still standing"]}
}

When the file is written, reply with this one line and nothing else:
WINNER <winner id> <winner total>-<loser total>
```
<!-- /template:judge -->

### Final check, only when there is an answer to beat

<!-- template:final -->
```text
You are the final check in an arena. {{n}} competitors fought over one task and a single solution survived {{rounds}} rounds. Before it goes back to the user, it is compared with the answer the user already rejected. You are not told which of the two is which. Score what is in front of you. Either one can win.

=== THE TASK ===
{{task}}
=== END OF THE TASK ===

Read the rubric first: {{rubric}}

Solution X: {{x_solution}}
Solution Y: {{y_solution}}

How to judge:
1. Read both in full before you score either.
2. Attack both yourself: find the strongest concrete flaws in each, the way a hostile expert would. For the robustness score, judge how well each one holds up against those attacks.
3. Score each criterion from 0 to 10 using the rubric's anchors. Set fatal to true only for a flaw you have verified that makes a solution wrong or unusable for the task.
4. The winner is the higher weighted total. A fatal solution cannot beat one that is not fatal.
5. Judge the work, not the writing about the work. Length is not quality.
6. Do not create, edit or delete any file except the verdict.

Write this JSON, and nothing else, to {{out}}:
{
  "scores": {
    "X": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false},
    "Y": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false}
  },
  "winner": "X or Y",
  "reason": "one sentence: the decisive difference",
  "fixed": ["each thing the winner gets right that the other gets wrong, in a few words"]
}

When the file is written, reply with this one line and nothing else:
FINAL <X or Y> <X total>-<Y total>
```
<!-- /template:final -->

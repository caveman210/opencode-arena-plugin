#!/usr/bin/env python3
"""bracket.py: the tournament state machine behind /arena.

The whole arena lives in one JSON file (arena.json), so the orchestrator never
loses track of who is alive, who fought whom and what happens next, even if its
own context gets compacted halfway through a 100-agent run.

    python3 bracket.py plan --agents 100             # rounds, sub-agent calls and waves. Writes nothing.
    python3 bracket.py estimate --agents 100 --task-file task.md
    python3 bracket.py doctor                        # check opencode, model cache, skill files
    python3 bracket.py roster --refresh              # rebuild the model roster from the live models
    python3 bracket.py init --agents 100 --seed 7 --task-file task.md [--baseline-file old.md]
    python3 bracket.py init --quick --task "..."     # 16 agents
    python3 bracket.py next                          # what to do now, and the exact command for it
    python3 bracket.py prompts <phase> [--json]      # write briefs, list jobs and models, in waves
    python3 bracket.py check <phase>                 # which outputs are still missing
    python3 bracket.py pairings                      # this round's matches, including the bye
    python3 bracket.py collect                       # read every judge verdict and record the winners
    python3 bracket.py record <match_id> <winner_id> [--reason "..."]
    python3 bracket.py advance                       # close the round, eliminate the losers, pair the survivors
    python3 bracket.py status                        # alive and eliminated, per round
    python3 bracket.py report                        # per-model wins, losses and judge assignments
    python3 bracket.py winner                        # the survivor, what it beat, the attacks it survived
    python3 bracket.py card <agent_id>               # one competitor's strategy card

Phases, in order: spawn, then per round attack, defend, judge; then final (only when
there is a rejected answer to beat). Every command takes --dir; without it, the run
named in .arena/LATEST is used.

Python 3.8+, standard library only.
"""

import argparse
import glob
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.abspath(__file__)
STRATEGIES_PATH = os.path.join(HERE, "strategies.json")
SKILL_PATH = os.path.join(HERE, "SKILL.md")
RUBRIC_PATH = os.path.join(HERE, "rubric.md")

DEFAULT_AGENTS = 100
QUICK_AGENTS = 16
DEFAULT_WAVE = 10  # opencode runs a wave as one message with concurrent task calls
ROOT = ".arena"
LATEST = "LATEST"
STATE_FILE = "arena.json"
PHASES = ("spawn", "attack", "defend", "judge", "final")
TEMPLATES = ("competitor", "attacker", "defender", "judge", "final")
CALLS_PER_MATCH = 5  # 2 attacks, 2 defenses, 1 judge

# Token and cost planning. These are deliberately conservative round numbers.
EST_OVERHEAD = 1200  # role instructions, paths and formatting in every brief
EST_CARD = 300  # one strategy card in a competitor brief
EST_IDENTITY = 180  # persona, tools note and variation note in a worker brief
EST_ATTACK = 400  # one attack file, input and output
EST_DEFENSE = 300  # one defense file, input and output
EST_JUDGE = 350  # one verdict
EST_FINAL = 350  # one final-check verdict
EST_SOLUTIONS = (800, 2000, 5000)  # low, expected, high solution sizes in tokens
MODEL_CACHE = os.path.expanduser("~/.cache/opencode/models.json")
MODEL_STATE = os.path.expanduser("~/.local/state/opencode/model.json")


def opencode_config_dir():
    """The same config directory the arena-router plugin reads."""
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.expanduser("~/.config/opencode")


# The package ships a roster: one file, next to bracket.py. It is the default,
# the list of models that worked when the package was built.
DEFAULT_AGENT_ROSTER = os.path.join(HERE, "arena-models.json")

# Models behind a Go plan answer "subscribe to Go for further access".
GO_GATED_PROVIDER_RE = re.compile(r"(^|[-_])go$", re.IGNORECASE)
# A "provider/model" header line in `opencode models --verbose` output.
MODEL_HEADER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.\-]*/[A-Za-z0-9][A-Za-z0-9_.\-/]*$"
)
MAX_ROSTER = 16

# Mirrors the table in rubric.md. tests/test_bracket.py checks they agree.
WEIGHTS = (
    ("correctness", 30),
    ("completeness", 25),
    ("specificity", 15),
    ("robustness", 20),
    ("clarity", 10),
)


class ArenaError(Exception):
    pass


# ---------------------------------------------------------------- the cards


def load_strategies(path=STRATEGIES_PATH):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for key in ("reasoning", "workflows", "strategies"):
        items = data.get(key) or []
        if not items:
            raise ArenaError("strategies.json has no '%s' entries" % key)
        ids = [it["id"] for it in items]
        if len(set(ids)) != len(ids):
            raise ArenaError("strategies.json has a duplicate id in '%s'" % key)
        for it in items:
            if not it.get("name") or not it.get("how"):
                raise ArenaError(
                    "strategies.json entry '%s' needs a name and a how" % it["id"]
                )
    return data


def combo_count(data):
    return len(data["reasoning"]) * len(data["workflows"]) * len(data["strategies"])


# ---------------------------------------------------------------- worker identities

DOMAIN_WORDS = {
    "code": (
        "code",
        "function",
        "bug",
        "api",
        "python",
        "javascript",
        "typescript",
        "rust",
        "golang",
        "java",
        "sql",
        "test",
        "refactor",
        "program",
        "script",
        "algorithm",
        "database",
        "backend",
        "frontend",
        "compiler",
        "repository",
        "git",
        "software",
        "app",
        "cli",
        "server",
        "client",
    ),
    "design": (
        "design",
        "ui",
        "ux",
        "visual",
        "brand",
        "logo",
        "layout",
        "typography",
        "colour",
        "color",
        "palette",
        "interface",
        "landing page",
        "css",
        "figma",
        "poster",
        "icon",
        "motion",
    ),
    "writing": (
        "write",
        "essay",
        "article",
        "story",
        "poem",
        "copy",
        "blog",
        "email",
        "letter",
        "script",
        "novel",
        "content",
        "documentation",
        "docs",
        "editing",
        "editor",
        "narrative",
    ),
    "research": (
        "research",
        "analyze",
        "analysis",
        "compare",
        "survey",
        "study",
        "sources",
        "evidence",
        "cite",
        "paper",
        "literature",
        "investigate",
        "findings",
    ),
    "data": (
        "data",
        "dataset",
        "statistics",
        "metric",
        "dashboard",
        "chart",
        "forecast",
        "model",
        "csv",
        "analytics",
        "table",
        "query",
    ),
    "security": (
        "security",
        "vulnerability",
        "exploit",
        "threat",
        "auth",
        "encryption",
        "privacy",
        "pentest",
        "audit",
        "incident",
        "CVE",
    ),
    "business": (
        "business",
        "market",
        "pricing",
        "strategy",
        "product",
        "startup",
        "customer",
        "revenue",
        "sales",
        "ops",
        "operations",
        "roadmap",
    ),
    "science": (
        "physics",
        "chemistry",
        "biology",
        "experiment",
        "hypothesis",
        "lab",
        "math",
        "proof",
        "theorem",
        "specimen",
        "measurement",
    ),
    "medicine": (
        "medical",
        "patient",
        "clinical",
        "diagnosis",
        "treatment",
        "health",
        "drug",
        "surgery",
        "symptom",
        "care",
    ),
    "legal": (
        "legal",
        "law",
        "contract",
        "policy",
        "compliance",
        "regulation",
        "court",
        "rights",
        "verdict",
        "precedent",
    ),
    "education": (
        "teach",
        "lesson",
        "curriculum",
        "student",
        "explain",
        "tutorial",
        "learn",
        "course",
        "classroom",
    ),
    "architecture": (
        "building",
        "architecture",
        "structural",
        "floor plan",
        "construction",
        "urban",
        "bridge",
        "facade",
        "site",
    ),
    "music": (
        "music",
        "song",
        "lyrics",
        "composition",
        "sound",
        "audio",
        "orchestra",
        "instrument",
        "performance",
    ),
    "art": (
        "art",
        "painting",
        "illustration",
        "sculpture",
        "gallery",
        "drawing",
        "exhibition",
        "conservation",
        "museum",
    ),
    "food": (
        "recipe",
        "food",
        "cook",
        "chef",
        "menu",
        "flavour",
        "flavor",
        "dish",
        "restaurant",
        "bakery",
        "wine",
    ),
    "game": (
        "game",
        "level",
        "player",
        "gameplay",
        "mechanic",
        "unity",
        "godot",
        "quest",
        "balance",
    ),
}


def detect_domains(task):
    """Task words that tell us which worker identities are relevant."""
    text = " %s " % re.sub(r"[^a-z0-9+#.]+", " ", (task or "").lower())
    found = []
    for domain, words in DOMAIN_WORDS.items():
        for word in words:
            if " " in word:
                hit = word in text
            else:
                hit = re.search(r"\b%s\b" % re.escape(word), text) is not None
            if hit:
                found.append(domain)
                break
    return found


PERSONA_FALLBACK = {
    "roles": [
        {
            "id": "craftsperson",
            "role": "a master craftsperson",
            "domains": ["*"],
            "creed": "You care about the part the user never sees.",
        }
    ],
    "stakes": [
        {
            "id": "reputation",
            "text": "a reputation for work that lasts",
            "domains": ["*"],
        }
    ],
    "quirks": [
        {
            "id": "notebook",
            "text": "You keep a worn notebook and never show the first draft.",
        }
    ],
}


def _persona_pools(data, domains):
    source = data.get("personas") or PERSONA_FALLBACK
    wanted = set(domains) or {"generic"}

    def choose(key):
        items = [it for it in source.get(key) or [] if it.get("id")]
        if not items:
            items = PERSONA_FALLBACK[key]
        local = [
            it
            for it in items
            if "*" in (it.get("domains") or [])
            or wanted.intersection(it.get("domains") or [])
        ]
        if local:
            return local
        generic = [it for it in items if "generic" in (it.get("domains") or [])]
        return generic or PERSONA_FALLBACK[key]

    return choose("roles"), choose("stakes"), choose("quirks")


def make_personas(n, seed, task, data):
    """One task-relevant identity per competitor.

    The role and the stake are filtered by the task's domain, so a coding task
    gets engineers and maintainers while a design task gets designers and art
    directors. The role, stake and quirk combination is unique for every agent,
    which keeps the workforce varied without changing the strategy card.
    """
    roles, stakes, quirks = _persona_pools(data, detect_domains(task))
    combos = [
        (role, stake, quirk) for role in roles for stake in stakes for quirk in quirks
    ]
    rng = random.Random("arena-personas:%s" % seed)
    rng.shuffle(combos)
    out = []
    for i in range(n):
        role, stake, quirk = combos[i % len(combos)]
        cycle = i // len(combos)
        key = (role["id"], stake["id"], quirk["id"], cycle)
        creed = (role.get("creed") or "").strip()
        text = "You are %s. At stake: %s. %s %s" % (
            role["role"],
            stake["text"],
            creed,
            quirk["text"],
        )
        out.append(
            {
                "id": ":".join(str(x) for x in key),
                "role": role["role"],
                "stake": stake["text"],
                "creed": creed,
                "quirk": quirk["text"],
                "text": re.sub(r"\s+", " ", text).strip(),
            }
        )
    return out


def _order_cards_for_primaries(dealt, k):
    """Put one card with a distinct reasoning mode in each of the first k slots."""
    if k <= 1:
        return list(dealt)
    first, rest, seen = [], [], set()
    for card in dealt:
        reasoning = card[0]
        if len(first) < k and reasoning not in seen:
            first.append(card)
            seen.add(reasoning)
        else:
            rest.append(card)
    if len(first) < k:
        need = k - len(first)
        first.extend(rest[:need])
        rest = rest[need:]
    return first + rest


def _edge_colour(edges, k):
    """Properly edge-colour a bipartite (multi)graph with k colours, then balance it.

    edges are (row, col) pairs and k must be at least the largest degree. Proper
    means no two edges at the same row, or at the same col, share a colour. Every
    bipartite graph has one (Konig), found here with alternating-path swaps. Then
    de Werra's swaps even the colour classes out until each is used floor or ceil
    of len(edges) / k times.
    """
    colour = [None] * len(edges)
    at = ({}, {})

    def slot(side, node):
        return at[side].setdefault(node, {})

    def put(e, c):
        u, v = edges[e]
        colour[e] = c
        slot(0, u)[c] = e
        slot(1, v)[c] = e

    def take(e):
        u, v = edges[e]
        del slot(0, u)[colour[e]]
        del slot(1, v)[colour[e]]

    def walk(side, node, first, other):
        path, c = [], first
        while True:
            e = slot(side, node).get(c)
            if e is None or e in path:
                return path
            path.append(e)
            node = edges[e][1 - side]
            side = 1 - side
            c = other if c == first else first

    def swap(path, a, b):
        new = {e: (b if colour[e] == a else a) for e in path}
        for e in path:
            take(e)
        for e in path:
            put(e, new[e])

    for e, (u, v) in enumerate(edges):
        free_u = [c for c in range(k) if c not in slot(0, u)]
        free_v = set(c for c in range(k) if c not in slot(1, v))
        c = next((c for c in free_u if c in free_v), None)
        if c is None:
            a, b = free_u[0], min(free_v)
            swap(walk(1, v, a, b), a, b)
            c = a
        put(e, c)

    while True:
        count = [0] * k
        for c in colour:
            count[c] += 1
        hi = max(range(k), key=lambda c: (count[c], -c))
        lo = min(range(k), key=lambda c: (count[c], c))
        if count[hi] - count[lo] <= 1:
            return colour
        swapped = False
        for side in (0, 1):
            for node in sorted(at[side]):
                s = at[side][node]
                if hi in s and lo not in s:
                    path = walk(side, node, hi, lo)
                    if sum(colour[e] == hi for e in path) > sum(
                        colour[e] == lo for e in path
                    ):
                        swap(path, hi, lo)
                        swapped = True
                        break
            if swapped:
                break
        if not swapped:
            raise ArenaError("could not balance the strategy cards")


def deal(n, seed, data):
    """Deal n distinct (reasoning, workflow, strategy) index triples, one per agent.

    Deterministic for a given seed and strategies.json. Guarantees:
      - no card is dealt twice
      - every reasoning mode, workflow and strategy is dealt as evenly as possible
        (any two counts differ by at most 1), so 100 agents use all of them
      - while every reasoning mode and every workflow has no more agents than there
        are strategies (up to 144 agents with the shipped cards): no two agents share
        a reasoning mode and a workflow, a reasoning mode and a strategy, or a
        workflow and a strategy. Any two agents differ in at least two of the three.

    How: agent slot i gets reasoning i mod R and workflow (i + i // lcm(R, W)) mod W,
    which never repeats a workflow inside a reasoning mode and stays balanced. The
    strategies are then a balanced proper edge colouring of that reasoning x
    workflow grid. The seed shuffles every list's labels and who gets which card.
    """
    R, W, S = len(data["reasoning"]), len(data["workflows"]), len(data["strategies"])
    total = R * W * S
    if n < 1 or n > total:
        raise ArenaError("can deal between 1 and %d cards, not %d" % (total, n))
    rng = random.Random("arena-deal:%s" % seed)
    pr, pw, ps = list(range(R)), list(range(W)), list(range(S))
    rng.shuffle(pr)
    rng.shuffle(pw)
    rng.shuffle(ps)
    lcm = R * W // math.gcd(R, W)
    cells = [(i % R, (i + i // lcm) % W) for i in range(n)]
    deg_r = max(sum(1 for r, _ in cells if r == x) for x in range(R))
    deg_w = max(sum(1 for _, w in cells if w == x) for x in range(W))
    if deg_r <= S and deg_w <= S:
        order = list(range(n))
        rng.shuffle(order)
        colours = _edge_colour([cells[i] for i in order], S)
        strategy_of = {order[j]: colours[j] for j in range(n)}
        triples = [(r, w, strategy_of[i]) for i, (r, w) in enumerate(cells)]
    else:
        # Past that size a repeat pair is unavoidable. Stay balanced and never repeat a card.
        count, pairs_rs, pairs_ws, used, triples = [0] * S, {}, {}, set(), []
        for r, w in cells:
            low = min(count)
            s = min(
                (c for c in range(S) if (r, w, c) not in used),
                key=lambda c: (
                    count[c] - low,
                    pairs_rs.get((r, c), 0),
                    pairs_ws.get((w, c), 0),
                    c,
                ),
            )
            used.add((r, w, s))
            count[s] += 1
            pairs_rs[(r, s)] = pairs_rs.get((r, s), 0) + 1
            pairs_ws[(w, s)] = pairs_ws.get((w, s), 0) + 1
            triples.append((r, w, s))
    dealt = [(pr[r], pw[w], ps[s]) for r, w, s in triples]
    rng.shuffle(dealt)
    return dealt


def agent_ids(n):
    width = max(3, len(str(n)))
    return ["a%0*d" % (width, i) for i in range(1, n + 1)]


def bracket_sizes(n):
    """Alive count at the start of each round, ending at 1. 100 -> 50 -> 25 -> 13 -> 7 -> 4 -> 2 -> 1."""
    sizes = [n]
    while sizes[-1] > 1:
        sizes.append((sizes[-1] + 1) // 2)
    return sizes


# ---------------------------------------------------------------- paths


def spawn_out(d, aid):
    return os.path.join(d, "r0", aid + ".md")


def attack_out(d, rnd, mid, attacker):
    return os.path.join(d, "r%d" % rnd, "%s.%s.attack.md" % (mid, attacker))


def defense_out(d, rnd, mid, aid):
    return os.path.join(d, "r%d" % rnd, "%s.%s.defense.md" % (mid, aid))


def revised_out(d, rnd, mid, aid):
    return os.path.join(d, "r%d" % rnd, "%s.%s.solution.md" % (mid, aid))


def verdict_out(d, rnd, mid):
    return os.path.join(d, "r%d" % rnd, "%s.verdict.json" % mid)


def final_out(d):
    return os.path.join(d, "final.verdict.json")


def prompt_path(d, rnd, job_id):
    sub = "final" if rnd is None else "r%d" % rnd
    return os.path.join(d, "prompts", sub, job_id + ".md")


def _has_output(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


# ---------------------------------------------------------------- models


def _model_list(raw):
    """Normalise a comma string or list of model IDs, preserving order and removing repeats."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    out = []
    for item in items:
        spec = str(item).strip()
        if spec and spec not in out:
            out.append(spec)
    return out


def model_parts(spec):
    """Split provider/model#variant into (provider, model). The variant is kept in the spec."""
    raw = str(spec or "").strip().split("#", 1)[0]
    if "/" not in raw:
        return None
    provider, model = raw.split("/", 1)
    if not provider or not model:
        return None
    return provider, model


def load_model_catalog(path=MODEL_CACHE):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def current_model():
    try:
        with open(MODEL_STATE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    recent = data.get("recent") if isinstance(data, dict) else None
    if not isinstance(recent, list) or not recent:
        return None
    item = recent[0] if isinstance(recent[0], dict) else {}
    provider, model = item.get("providerID"), item.get("modelID")
    if not provider or not model:
        return None
    return "%s/%s" % (provider, model)


def model_prices(spec, catalog=None, price_in=None, price_out=None):
    """USD per million tokens, or None when the model is unknown."""
    if price_in is not None and price_out is not None:
        return float(price_in), float(price_out)
    parts = model_parts(spec)
    if not parts:
        return None
    catalog = load_model_catalog() if catalog is None else catalog
    provider = catalog.get(parts[0]) if isinstance(catalog, dict) else None
    model = ((provider or {}).get("models") or {}).get(parts[1])
    cost = (model or {}).get("cost") if isinstance(model, dict) else None
    if not isinstance(cost, dict):
        return None
    try:
        return float(cost.get("input", 0)), float(cost.get("output", 0))
    except (TypeError, ValueError):
        return None


def config_roster_path():
    """The live roster: a writable file in the opencode config dir.

    This is the file the plugin reads at startup and rewrites when the model
    list changes. The packaged roster is only the default it starts from.
    """
    return os.path.join(opencode_config_dir(), "arena-models.json")


def find_agent_roster():
    """The roster file to read: the live one when it exists, the packaged one otherwise."""
    live = config_roster_path()
    if os.path.isfile(live):
        return live
    return DEFAULT_AGENT_ROSTER


def agent_name_for_model(model):
    """The visible subagent name opencode registers for a model.

    Must stay identical to agentNameForModel in arena-router.ts:
    "arena-" plus the model id, lowercased, with every run of other characters
    turned into a single dash.
    """
    raw = str(model or "").strip()
    model_id = raw.split("/", 1)[1] if "/" in raw else raw
    slug = re.sub(r"[^a-z0-9]+", "-", model_id.lower()).strip("-")
    return "arena-%s" % slug if slug else "arena-model"


def is_go_gated(provider):
    """True for providers that need a Go plan to be reachable."""
    return bool(GO_GATED_PROVIDER_RE.search(str(provider or "")))


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_roster_rows(path=None):
    """Every row of the roster file, with the agent name derived when missing."""
    path = path or find_agent_roster()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = (
        data
        if isinstance(data, list)
        else (data.get("models") if isinstance(data, dict) else [])
    )
    out = []
    for row in rows or []:
        if isinstance(row, str):
            continue
        model = str((row or {}).get("model") or "").strip()
        if not model or "/" not in model:
            continue
        provider = model.split("/", 1)[0]
        agent = str((row or {}).get("agent") or "").strip() or agent_name_for_model(
            model
        )
        out.append(
            {
                "model": model,
                "agent": agent,
                "name": (row or {}).get("name") or model,
                "provider": provider,
                "input": _num((row or {}).get("input")),
                "output": _num((row or {}).get("output")),
                "context": int(_num((row or {}).get("context"))),
                "toolcall": bool((row or {}).get("toolcall", True)),
                "status": str((row or {}).get("status") or "active"),
                "free": (row or {}).get("free", True) is not False,
                "goGated": is_go_gated(provider),
            }
        )
    return out


def load_agent_roster(path=None):
    """The model -> visible subagent map shared with the arena-router plugin."""
    return {row["model"]: row["agent"] for row in load_roster_rows(path)}


def subagent_for_model(model, roster=None):
    roster = load_agent_roster() if roster is None else roster
    return (roster or {}).get(model)


# ---------------------------------------------------------------- live models


def _opencode_models_output(verbose=True, timeout=180):
    """Run `opencode models` and return its stdout.

    A separate process, so it never waits on the server that is asking for it.
    """
    binary = _opencode_binary()
    if not binary:
        raise ArenaError("opencode binary not found, cannot read the live model list")
    cmd = [binary, "models"] + (["--verbose"] if verbose else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ArenaError("opencode models timed out after %ds" % timeout)
    except OSError as exc:
        raise ArenaError("could not run opencode models: %s" % exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ArenaError(
            "opencode models failed: %s"
            % (detail[0] if detail else "exit %d" % proc.returncode)
        )
    return proc.stdout or ""


def parse_models_verbose(text):
    """Parse `opencode models --verbose`: a "provider/model" header, then a JSON block."""
    lines = (text or "").splitlines()
    rows = []
    index = 0
    total = len(lines)
    while index < total:
        header = lines[index].strip()
        if not header or header.startswith("{") or not MODEL_HEADER_RE.match(header):
            index += 1
            continue
        block = []
        cursor = index + 1
        while cursor < total:
            line = lines[cursor]
            if not line[:1].isspace():
                if line.strip() == "}":
                    block.append(line)
                    cursor += 1
                    break
                if MODEL_HEADER_RE.match(line.strip()):
                    break
            block.append(line)
            cursor += 1
        try:
            data = json.loads("\n".join(block))
        except ValueError:
            data = {}
        if isinstance(data, dict):
            rows.append((header, data))
        index = cursor
    return rows


def live_models(verbose=True):
    """Every model opencode can reach right now, priced and capability flagged."""
    text = _opencode_models_output(verbose=verbose)
    rows = []
    for header, data in parse_models_verbose(text):
        provider = str(data.get("providerID") or header.split("/", 1)[0])
        model_id = str(data.get("id") or header.split("/", 1)[-1])
        spec = "%s/%s" % (provider, model_id)
        cost = data.get("cost") or {}
        limit = data.get("limit") or {}
        caps = data.get("capabilities") or {}
        price_in = _num(cost.get("input"))
        price_out = _num(cost.get("output"))
        rows.append(
            {
                "model": spec,
                "agent": agent_name_for_model(spec),
                "name": data.get("name") or model_id,
                "provider": provider,
                "input": price_in,
                "output": price_out,
                "context": int(_num(limit.get("context"))),
                "toolcall": bool(caps.get("toolcall")),
                "status": str(data.get("status") or "active"),
                "free": price_in == 0 and price_out == 0,
                "goGated": is_go_gated(provider),
            }
        )
    return rows


def _go_gated(row):
    """True when a row sits behind a Go plan, derived from the provider if needed."""
    if row.get("goGated"):
        return True
    provider = row.get("provider") or str(row.get("model") or "").split("/", 1)[0]
    return is_go_gated(provider)


def roster_skip_reason(row):
    """Why a live model is not in the roster, or None when it belongs there."""
    if _go_gated(row):
        return "needs a Go plan"
    if not row.get("toolcall"):
        return "no tool calling"
    if row.get("status") == "deprecated":
        return "deprecated"
    if not row.get("free"):
        return "costs money"
    return None


def select_roster(rows, include_paid=False, include_go_gated=False, limit=MAX_ROSTER):
    """The models opencode should expose: free, tool capable, live, never Go gated."""
    usable = []
    for row in rows or []:
        if not row.get("toolcall"):
            continue
        if row.get("status") == "deprecated":
            continue
        if _go_gated(row) and not include_go_gated:
            continue
        if not row.get("free") and not include_paid:
            continue
        usable.append(row)
    usable.sort(key=lambda row: (-(row.get("context") or 0), row.get("model") or ""))
    return usable[:limit]


def write_roster_file(rows, path=None):
    """Write the roster the plugin reads at startup."""
    path = path or config_roster_path()
    payload = []
    for row in rows or []:
        payload.append(
            {
                "model": row["model"],
                "agent": row.get("agent") or agent_name_for_model(row["model"]),
                "name": row.get("name"),
                "provider": row.get("provider") or row["model"].split("/", 1)[0],
                "input": row.get("input", 0),
                "output": row.get("output", 0),
                "context": row.get("context", 0),
                "toolcall": True,
                "status": row.get("status", "active"),
                "free": True,
            }
        )
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return path


def assign_competitor_models(aids, models, seed, agents=None):
    """Seeded, balanced assignment with a distinct first reasoning mode per model.

    The first pass gives every model one primary agent. The cards are ordered so
    those primaries use different reasoning modes. The rest are spread over the
    lowest-count models, with card parts used as a tie-break. A model that owns
    several agents still gets different cards for each of them.
    """
    models = _model_list(models)
    if not models:
        return {}
    rng = random.Random("arena-models:%s" % seed)
    counts = {m: 0 for m in models}
    assigned = {}
    if not agents:
        order = list(aids)
        rng.shuffle(order)
        for aid in order:
            low = min(counts.values())
            pick = next(m for m in models if counts[m] == low)
            assigned[aid] = pick
            counts[pick] += 1
        return {aid: assigned[aid] for aid in sorted(assigned)}

    order = sorted(aids)
    model_order = list(models)
    rng.shuffle(model_order)
    primary_count = min(len(model_order), len(order))
    for model, aid in zip(model_order, order[:primary_count]):
        assigned[aid] = model
        counts[model] += 1
    remaining = order[primary_count:]
    rng.shuffle(remaining)
    for aid in remaining:
        low = min(counts.values())
        candidates = [m for m in models if counts[m] == low]
        if len(candidates) > 1:
            card = agents[aid]["card"]

            def spread(model):
                same = 0
                for other, other_model in assigned.items():
                    if other_model != model:
                        continue
                    other_card = agents[other]["card"]
                    for part in ("reasoning", "workflow", "strategy"):
                        if other_card[part]["id"] == card[part]["id"]:
                            same += 1
                return same

            candidates.sort(key=lambda m: (spread(m), models.index(m)))
        pick = candidates[0]
        assigned[aid] = pick
        counts[pick] += 1
    return {aid: assigned[aid] for aid in sorted(assigned)}


def _assign_judges(state, matches):
    md = state.get("models")
    if not md:
        return
    pool = md.get("judge_pool") or []
    judges = md.setdefault("judges", {})
    for match in matches:
        mid = match["id"]
        if mid not in judges and pool:
            judges[mid] = pool[len(judges) % len(pool)]


# ---------------------------------------------------------------- the bracket


def new_state(
    n,
    seed,
    data,
    run_dir,
    wave=DEFAULT_WAVE,
    has_baseline=False,
    models=None,
    judge_models=None,
    task=None,
):
    run_dir = os.path.abspath(run_dir)
    ids = agent_ids(n)
    models = _model_list(models)
    judge_models = _model_list(judge_models)
    if task is None:
        task = _read_optional(os.path.join(run_dir, "task.md"))
    cards = list(deal(n, seed, data))
    if models:
        cards = _order_cards_for_primaries(cards, min(len(models), n))
    personas = make_personas(n, seed, task, data)
    agents = {}
    for aid, (r, w, s), persona in zip(ids, cards, personas):
        agents[aid] = {
            "card": {
                "reasoning": dict(data["reasoning"][r]),
                "workflow": dict(data["workflows"][w]),
                "strategy": dict(data["strategies"][s]),
            },
            "persona": persona,
            "solution": spawn_out(run_dir, aid),
            "alive": True,
            "eliminated_in": None,
            "eliminated_by": None,
            "byes": 0,
        }
    competitors = assign_competitor_models(ids, models, seed, agents)
    judge_pool = judge_models or list(models)
    primaries = {}
    for model in models:
        for aid in ids[: len(models)]:
            if competitors.get(aid) == model:
                primaries[model] = aid
                break
    used = _model_list(list(competitors.values()) + judge_pool)
    state = {
        "version": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seed": seed,
        "agents_n": n,
        "cards_available": combo_count(data),
        "wave": wave,
        "dir": run_dir,
        "has_baseline": bool(has_baseline),
        "agents": agents,
        "rounds": [],
        "champion": None,
        "final": None,
        "models": {
            "strategy": "diverse" if len(models) > 1 else "uniform",
            "pool": models,
            "competitors": competitors,
            "primaries": primaries,
            "judge_pool": judge_pool,
            "judges": {},
            "final_judge": None,
            "used": used,
            "failed": [],
            "degraded": False,
        },
    }
    open_round(state, ids)
    return state


def pair_round(state, rnd, alive):
    """Seeded pairing. An odd pool gives one bye, to an agent with the fewest byes so far.
    Where it can, it pairs agents with different reasoning modes, so every attack comes
    from a genuinely different angle. The first-listed side is random, which is the
    order the judge reads them in."""
    agents = state["agents"]
    rng = random.Random("arena-pair:%s:%d" % (state["seed"], rnd))
    pool = sorted(alive)
    rng.shuffle(pool)
    bye = None
    if len(pool) % 2:
        fewest = min(agents[a]["byes"] for a in pool)
        bye = next(a for a in pool if agents[a]["byes"] == fewest)
        pool.remove(bye)
    width = max(2, len(str(len(pool) // 2)))
    matches = []
    while pool:
        a = pool.pop(0)
        mode = agents[a]["card"]["reasoning"]["id"]
        j = next(
            (
                k
                for k, b in enumerate(pool)
                if agents[b]["card"]["reasoning"]["id"] != mode
            ),
            0,
        )
        b = pool.pop(j)
        if rng.random() < 0.5:
            a, b = b, a
        matches.append(
            {
                "id": "r%d-m%0*d" % (rnd, width, len(matches) + 1),
                "a": a,
                "b": b,
                "winner": None,
                "loser": None,
                "reason": None,
                "survived": [],
                "scores": None,
                "source": None,
            }
        )
    return matches, bye


def open_round(state, alive):
    """Open the next round, or crown the champion when one agent is left."""
    alive = sorted(alive)
    if len(alive) == 1:
        state["champion"] = alive[0]
        md = state.get("models") or {}
        pool = md.get("judge_pool") or []
        if pool and not md.get("final_judge"):
            md["final_judge"] = pool[0]
        if state.get("has_baseline") and not state.get("final"):
            rng = random.Random("arena-final:%s" % state["seed"])
            x = "champion" if rng.random() < 0.5 else "baseline"
            state["final"] = {
                "X": x,
                "Y": "baseline" if x == "champion" else "champion",
                "result": None,
            }
        return None
    rnd = len(state["rounds"]) + 1
    matches, bye = pair_round(state, rnd, alive)
    rd = {"n": rnd, "alive": alive, "matches": matches, "bye": bye, "closed": False}
    state["rounds"].append(rd)
    _assign_judges(state, matches)
    return rd


def current_round(state):
    if state["champion"] or not state["rounds"]:
        return None
    rd = state["rounds"][-1]
    return None if rd["closed"] else rd


def norm_agent(state, raw):
    raw = str(raw).strip().lower()
    if raw in state["agents"]:
        return raw
    digits = raw.lstrip("a")
    if digits.isdigit():
        for aid in state["agents"]:
            if int(aid[1:]) == int(digits):
                return aid
    raise ArenaError("no agent called '%s'" % raw)


def find_match(state, mid):
    rd = current_round(state)
    if rd is None:
        raise ArenaError(
            "no round is open"
            + (" (the arena has a champion)" if state["champion"] else "")
        )
    raw = str(mid).strip().lower()
    for m in rd["matches"]:
        if m["id"] == raw:
            return rd, m
    tail = raw.split("-m")[-1].lstrip("m")
    if tail.isdigit():
        for m in rd["matches"]:
            if int(m["id"].split("-m")[-1]) == int(tail):
                return rd, m
    raise ArenaError("no match '%s' in round %d (run pairings)" % (mid, rd["n"]))


def record(
    state, mid, winner, reason=None, survived=None, scores=None, source="manual"
):
    rd, m = find_match(state, mid)
    winner = norm_agent(state, winner)
    if winner not in (m["a"], m["b"]):
        raise ArenaError(
            "%s is not in %s (%s vs %s)" % (winner, m["id"], m["a"], m["b"])
        )
    previous = m["winner"]
    m["winner"] = winner
    m["loser"] = m["b"] if winner == m["a"] else m["a"]
    m["reason"] = reason or None
    m["survived"] = list(survived or [])
    m["scores"] = scores
    m["source"] = source
    return m, previous


def advance(state):
    """Close the current round: eliminate every loser, carry the winners' revised
    solutions forward, give the bye its free pass, and open the next round."""
    rd = current_round(state)
    if rd is None:
        raise ArenaError(
            "no round is open"
            + (" (the arena has a champion)" if state["champion"] else "")
        )
    unrecorded = [m["id"] for m in rd["matches"] if not m["winner"]]
    if unrecorded:
        raise ArenaError(
            "round %d has %d unrecorded match(es): %s"
            % (rd["n"], len(unrecorded), ", ".join(unrecorded))
        )
    survivors = []
    for m in rd["matches"]:
        loser = state["agents"][m["loser"]]
        loser["alive"] = False
        loser["eliminated_in"] = rd["n"]
        loser["eliminated_by"] = m["winner"]
        for aid in (m["a"], m["b"]):
            rev = revised_out(state["dir"], rd["n"], m["id"], aid)
            if _has_output(rev):
                state["agents"][aid]["solution"] = rev
        survivors.append(m["winner"])
    if rd["bye"]:
        state["agents"][rd["bye"]]["byes"] += 1
        survivors.append(rd["bye"])
    rd["closed"] = True
    open_round(state, survivors)
    return survivors


def rounds_played(state):
    return sum(1 for rd in state["rounds"] if rd["closed"])


def alive_ids(state):
    return sorted(a for a, v in state["agents"].items() if v["alive"])


# ---------------------------------------------------------------- verdicts


def weighted_total(scores):
    """0 to 100 from five 0 to 10 scores, weighted as in rubric.md. None if any is missing."""
    if not isinstance(scores, dict):
        return None
    total = 0.0
    for key, weight in WEIGHTS:
        v = scores.get(key)
        if isinstance(v, bool):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        total += max(0.0, min(10.0, v)) * weight
    return round(total / 10.0, 2)


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def extract_json(text):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(text[i : j + 1])
        except ValueError:
            return None
    return None


def decide(verdict, a, b):
    """Apply the rubric's arithmetic to a judge's verdict.

    The winner is the higher weighted total. A fatal solution cannot beat a
    non-fatal one. On an exact tie: fewer standing attacks, then higher
    correctness, then the judge's own pick. If the judge's pick disagrees with its
    own scores, the scores win and the note says so.
    Returns (winner, {a: total, b: total}, note).
    """
    if not isinstance(verdict, dict):
        raise ArenaError("verdict is not a JSON object")
    raw_scores = verdict.get("scores") or {}
    scores = (
        {str(k).strip().lower(): v for k, v in raw_scores.items()}
        if isinstance(raw_scores, dict)
        else {}
    )
    sa, sb = scores.get(a), scores.get(b)
    pick_raw = str(verdict.get("winner") or "").strip().lower()
    in_a = re.search(r"\b%s\b" % re.escape(a), pick_raw) is not None
    in_b = re.search(r"\b%s\b" % re.escape(b), pick_raw) is not None
    pick = a if (in_a and not in_b) else b if (in_b and not in_a) else None
    ta, tb = weighted_total(sa), weighted_total(sb)
    totals = {a: ta, b: tb}
    if ta is None or tb is None:
        if pick:
            return pick, totals, "scores incomplete, used the judge's pick"
        raise ArenaError("verdict has neither complete scores nor a clear winner")
    fa, fb = _truthy(sa.get("fatal")), _truthy(sb.get("fatal"))
    if fa != fb:
        winner = b if fa else a
    elif ta != tb:
        winner = a if ta > tb else b
    else:
        standing = (
            verdict.get("standing") if isinstance(verdict.get("standing"), dict) else {}
        )
        standing = {str(k).strip().lower(): v for k, v in standing.items()}
        na = len(standing.get(a) or []) if isinstance(standing.get(a), list) else None
        nb = len(standing.get(b) or []) if isinstance(standing.get(b), list) else None
        ca, cb = float(sa.get("correctness", 0)), float(sb.get("correctness", 0))
        if na is not None and nb is not None and na != nb:
            winner = a if na < nb else b
        elif ca != cb:
            winner = a if ca > cb else b
        elif pick:
            winner = pick
        else:
            raise ArenaError("exact tie and the judge did not pick a winner")
    note = ""
    if pick and pick != winner:
        note = "judge picked %s but its own scores favour %s, so the scores win" % (
            pick,
            winner,
        )
    return winner, totals, note


def collect(state):
    """Record every verdict the judges have written for the open round (or the final
    check). An unreadable verdict is set aside as <name>.unreadable so the judge job
    shows up as missing again and gets re-run."""
    d = state["dir"]
    results = []
    if state["champion"]:
        fin = state.get("final")
        if not fin or fin.get("result"):
            return results
        path = final_out(d)
        if not _has_output(path):
            return [("final", "missing", "")]
        with open(path, encoding="utf-8", errors="replace") as fh:
            v = extract_json(fh.read())
        try:
            if isinstance(v, dict) and isinstance(v.get("scores"), dict):
                v = dict(
                    v,
                    scores={str(k).strip().lower(): s for k, s in v["scores"].items()},
                )
            w, totals, note = decide(v, "x", "y")
        except ArenaError as e:
            os.replace(path, path + ".unreadable")
            return [("final", "unreadable", str(e))]
        better = fin[w.upper()]
        fin["result"] = {
            "better": better,
            "champion_total": totals["x" if fin["X"] == "champion" else "y"],
            "baseline_total": totals["x" if fin["X"] == "baseline" else "y"],
            "reason": str(v.get("reason") or "")[:400],
            "fixed": [str(x) for x in (v.get("fixed") or [])][:10]
            if isinstance(v.get("fixed"), list)
            else [],
        }
        return [("final", "recorded", ("%s is better. %s" % (better, note)).strip())]
    rd = current_round(state)
    if rd is None:
        return results
    for m in rd["matches"]:
        if m["winner"]:
            continue
        path = verdict_out(d, rd["n"], m["id"])
        if not _has_output(path):
            results.append((m["id"], "missing", ""))
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            v = extract_json(fh.read())
        try:
            winner, totals, note = decide(v, m["a"], m["b"])
        except ArenaError as e:
            os.replace(path, path + ".unreadable")
            results.append((m["id"], "unreadable", str(e)))
            continue
        survived = v.get("survived") if isinstance(v.get("survived"), list) else []
        record(
            state,
            m["id"],
            winner,
            reason=str(v.get("reason") or "")[:400],
            survived=[str(x) for x in survived][:10],
            scores=totals,
            source="judge",
        )
        loser = m["loser"]
        line = "%s beat %s, %s to %s" % (
            winner,
            loser,
            _fmt(totals[winner]),
            _fmt(totals[loser]),
        )
        results.append((m["id"], "recorded", line + (". " + note if note else "")))
    return results


def _fmt(x):
    return "?" if x is None else ("%g" % x)


# ---------------------------------------------------------------- jobs and prompts


def _competitor_model(state, aid):
    return ((state.get("models") or {}).get("competitors") or {}).get(aid)


def _judge_model(state, mid):
    return ((state.get("models") or {}).get("judges") or {}).get(mid)


def phase_jobs(state, phase):
    """Every sub-agent job for a phase, with its prompt file, model and required outputs.
    Matches that already have a recorded winner are skipped."""
    if phase not in PHASES:
        raise ArenaError("unknown phase '%s' (one of: %s)" % (phase, ", ".join(PHASES)))
    d = state["dir"]
    jobs = []
    if phase == "spawn":
        for aid in sorted(state["agents"]):
            jobs.append(
                {
                    "id": aid + ".spawn",
                    "kind": "competitor",
                    "round": 0,
                    "agent": aid,
                    "model": _competitor_model(state, aid),
                    "outputs": [spawn_out(d, aid)],
                }
            )
    elif phase == "final":
        fin = state.get("final")
        if state["champion"] and fin and not fin.get("result"):
            jobs.append(
                {
                    "id": "final.judge",
                    "kind": "final",
                    "round": None,
                    "model": (state.get("models") or {}).get("final_judge"),
                    "outputs": [final_out(d)],
                }
            )
    else:
        rd = current_round(state)
        if rd is None:
            return []
        n = rd["n"]
        for m in rd["matches"]:
            if m["winner"]:
                continue
            a, b = m["a"], m["b"]
            if phase == "attack":
                for me, opp in ((a, b), (b, a)):
                    jobs.append(
                        {
                            "id": "%s.%s.attack" % (m["id"], me),
                            "kind": "attacker",
                            "round": n,
                            "agent": me,
                            "opponent": opp,
                            "match": m["id"],
                            "model": _competitor_model(state, me),
                            "outputs": [attack_out(d, n, m["id"], me)],
                        }
                    )
            elif phase == "defend":
                for me, opp in ((a, b), (b, a)):
                    jobs.append(
                        {
                            "id": "%s.%s.defend" % (m["id"], me),
                            "kind": "defender",
                            "round": n,
                            "agent": me,
                            "opponent": opp,
                            "match": m["id"],
                            "model": _competitor_model(state, me),
                            "outputs": [
                                defense_out(d, n, m["id"], me),
                                revised_out(d, n, m["id"], me),
                            ],
                        }
                    )
            else:
                jobs.append(
                    {
                        "id": "%s.judge" % m["id"],
                        "kind": "judge",
                        "round": n,
                        "match": m["id"],
                        "a": a,
                        "b": b,
                        "model": _judge_model(state, m["id"]),
                        "outputs": [verdict_out(d, n, m["id"])],
                    }
                )
    roster = load_agent_roster()
    for j in jobs:
        j["prompt"] = prompt_path(d, j["round"], j["id"])
        j["subagent"] = (
            subagent_for_model(j.get("model"), roster) if j.get("model") else None
        )
    return jobs


def missing_jobs(jobs):
    return [j for j in jobs if not all(_has_output(p) for p in j["outputs"])]


_TEMPLATE_RE = re.compile(
    r"<!-- template:([a-z]+) -->\s*```text\n(.*?)\n```\s*<!-- /template:\1 -->", re.S
)
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def load_templates(path=SKILL_PATH):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    found = {m.group(1): m.group(2) for m in _TEMPLATE_RE.finditer(text)}
    missing = [t for t in TEMPLATES if t not in found]
    if missing:
        raise ArenaError(
            "SKILL.md is missing prompt templates: %s" % ", ".join(missing)
        )
    return found


def fill(template, values):
    """One pass, so a task that itself contains {{braces}} is left exactly as written."""
    wanted = set(_PLACEHOLDER_RE.findall(template))
    missing = sorted(wanted - set(values))
    if missing:
        raise ArenaError("no value for placeholder(s): %s" % ", ".join(missing))
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), template)


def read_task(state):
    """The task exactly as init stored it (init adds one trailing newline to the file)."""
    with open(os.path.join(state["dir"], "task.md"), encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def _card_values(card):
    return {
        "reasoning_name": card["reasoning"]["name"],
        "reasoning_how": card["reasoning"]["how"],
        "workflow_name": card["workflow"]["name"],
        "workflow_how": card["workflow"]["how"],
        "strategy_name": card["strategy"]["name"],
        "strategy_how": card["strategy"]["how"],
    }


def render_job(state, job, templates, task):
    d = state["dir"]
    agents = state["agents"]
    v = {
        "task": task,
        "arena_dir": d,
        "rubric": os.path.join(d, "rubric.md"),
        "n": state["agents_n"],
    }
    kind = job["kind"]
    if kind == "competitor":
        aid = job["agent"]
        v.update(_card_values(agents[aid]["card"]))
        v["persona"] = agents[aid]["persona"]["text"]
        if state.get("has_baseline"):
            v["baseline_note"] = (
                "The user already got an answer to this task and was NOT satisfied with it. It is at "
                "%s. Read it first and work out exactly why it fell short. Then beat it. Do not just "
                "polish it: the winner of this arena is compared with that answer at the end."
                % os.path.join(d, "baseline.md")
            )
        else:
            v["baseline_note"] = (
                "There is no earlier answer to beat. Start from the task."
            )
        v.update(agent=aid, out=job["outputs"][0])
    elif kind == "attacker":
        me, opp = job["agent"], job["opponent"]
        v.update(_card_values(agents[me]["card"]))
        v["persona"] = agents[me]["persona"]["text"]
        v.update(
            agent=me,
            target=opp,
            match=job["match"],
            round=job["round"],
            own_solution=agents[me]["solution"],
            target_solution=agents[opp]["solution"],
            out=job["outputs"][0],
        )
    elif kind == "defender":
        me, opp = job["agent"], job["opponent"]
        v.update(_card_values(agents[me]["card"]))
        v["persona"] = agents[me]["persona"]["text"]
        v.update(
            agent=me,
            attacker=opp,
            match=job["match"],
            round=job["round"],
            own_solution=agents[me]["solution"],
            attacks=attack_out(d, job["round"], job["match"], opp),
            defense_out=job["outputs"][0],
            solution_out=job["outputs"][1],
        )
    elif kind == "judge":
        n, mid, a, b = job["round"], job["match"], job["a"], job["b"]
        v.update(
            match=mid,
            round=n,
            first=a,
            second=b,
            first_solution=revised_out(d, n, mid, a),
            second_solution=revised_out(d, n, mid, b),
            first_attacks=attack_out(d, n, mid, b),
            second_attacks=attack_out(d, n, mid, a),
            first_defense=defense_out(d, n, mid, a),
            second_defense=defense_out(d, n, mid, b),
            out=job["outputs"][0],
        )
    elif kind == "final":
        v.update(
            rounds=rounds_played(state),
            x_solution=os.path.join(d, "final", "X.md"),
            y_solution=os.path.join(d, "final", "Y.md"),
            out=job["outputs"][0],
        )
    return fill(templates[kind], v)


def write_prompts(state, phase):
    jobs = phase_jobs(state, phase)
    if not jobs:
        return jobs
    templates = load_templates()
    task = read_task(state)
    for j in jobs:
        for p in j["outputs"] + [j["prompt"]]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        body = render_job(state, j, templates, task)
        with open(j["prompt"], "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
    if phase == "spawn":
        for aid in state["agents"]:
            os.makedirs(os.path.join(state["dir"], "scratch", aid), exist_ok=True)
    if phase == "final":
        # Blind copies: the final judge sees X and Y, never which one is the champion.
        fin = state["final"]
        sources = {
            "champion": state["agents"][state["champion"]]["solution"],
            "baseline": os.path.join(state["dir"], "baseline.md"),
        }
        os.makedirs(os.path.join(state["dir"], "final"), exist_ok=True)
        for label in ("X", "Y"):
            with (
                open(sources[fin[label]], encoding="utf-8", errors="replace") as src,
                open(
                    os.path.join(state["dir"], "final", label + ".md"),
                    "w",
                    encoding="utf-8",
                ) as dst,
            ):
                dst.write(src.read())
    return jobs


def next_action(state):
    """(phase, jobs_left, jobs_total). phase is one of spawn, attack, defend, judge,
    final, collect, advance, done."""
    spawn = phase_jobs(state, "spawn")
    left = missing_jobs(spawn)
    if left:
        return "spawn", left, spawn
    if state["champion"]:
        fin = state.get("final")
        if fin and not fin.get("result"):
            jobs = phase_jobs(state, "final")
            left = missing_jobs(jobs)
            return ("final", left, jobs) if left else ("collect", [], jobs)
        return "done", [], []
    for phase in ("attack", "defend", "judge"):
        jobs = phase_jobs(state, phase)
        left = missing_jobs(jobs)
        if left:
            return phase, left, jobs
    rd = current_round(state)
    if any(not m["winner"] for m in rd["matches"]):
        return "collect", [], []
    return "advance", [], []


# ---------------------------------------------------------------- plan


def plan_rows(n, wave=DEFAULT_WAVE):
    rows = []
    for rnd, alive in enumerate(bracket_sizes(n)[:-1], start=1):
        m = alive // 2
        waves = 2 * math.ceil(2 * m / wave) + math.ceil(m / wave)
        rows.append(
            {
                "round": rnd,
                "alive": alive,
                "matches": m,
                "bye": alive % 2,
                "calls": CALLS_PER_MATCH * m,
                "waves": waves,
            }
        )
    return rows


def plan_totals(n, wave=DEFAULT_WAVE):
    rows = plan_rows(n, wave)
    calls = n + sum(r["calls"] for r in rows)
    waves = math.ceil(n / wave) + sum(r["waves"] for r in rows)
    return {
        "agents": n,
        "rounds": len(rows),
        "calls": calls,
        "waves": waves,
        "rows": rows,
    }


# ---------------------------------------------------------------- estimation


def _tokens(chars):
    return int(math.ceil(max(0, chars) / 4.0))


def _estimate_level(n, rows, task_tokens, rubric_tokens, baseline_tokens, solution):
    """One low, expected or high token scenario. Inputs and outputs are counted separately."""
    card = EST_CARD
    identity = EST_IDENTITY
    overhead = EST_OVERHEAD
    phases = []
    spawn_in = n * (task_tokens + card + identity + overhead)
    spawn_out = n * solution
    phases.append(
        {"phase": "spawn", "calls": n, "input": spawn_in, "output": spawn_out}
    )
    total_in, total_out = spawn_in, spawn_out
    for row in rows:
        m = row["matches"]
        attack_in = 2 * m * (task_tokens + card + identity + overhead + solution)
        attack_out = 2 * m * EST_ATTACK
        defense_in = (
            2 * m * (task_tokens + card + identity + overhead + solution + EST_ATTACK)
        )
        defense_out = 2 * m * (EST_DEFENSE + solution)
        judge_in = m * (
            task_tokens
            + rubric_tokens
            + overhead
            + 2 * solution
            + 2 * EST_ATTACK
            + 2 * EST_DEFENSE
        )
        judge_out = m * EST_JUDGE
        phases.append(
            {
                "phase": "round %d attack" % row["round"],
                "calls": 2 * m,
                "input": attack_in,
                "output": attack_out,
            }
        )
        phases.append(
            {
                "phase": "round %d defend" % row["round"],
                "calls": 2 * m,
                "input": defense_in,
                "output": defense_out,
            }
        )
        phases.append(
            {
                "phase": "round %d judge" % row["round"],
                "calls": m,
                "input": judge_in,
                "output": judge_out,
            }
        )
        total_in += attack_in + defense_in + judge_in
        total_out += attack_out + defense_out + judge_out
    if baseline_tokens:
        final_in = (
            task_tokens + rubric_tokens + overhead + 2 * solution + baseline_tokens
        )
        final_out = EST_FINAL
        phases.append(
            {"phase": "final judge", "calls": 1, "input": final_in, "output": final_out}
        )
        total_in += final_in
        total_out += final_out
    return {
        "input": total_in,
        "output": total_out,
        "total": total_in + total_out,
        "phases": phases,
    }


def estimate_payload(
    n,
    wave,
    task,
    rubric_text,
    baseline_text="",
    models=None,
    price_in=None,
    price_out=None,
):
    rows = plan_rows(n, wave)
    task_tokens = _tokens(len(task))
    rubric_tokens = _tokens(len(rubric_text))
    baseline_tokens = _tokens(len(baseline_text))
    levels = {}
    for name, solution in zip(("low", "expected", "high"), EST_SOLUTIONS):
        levels[name] = _estimate_level(
            n, rows, task_tokens, rubric_tokens, baseline_tokens, solution
        )
    calls = n + sum(r["calls"] for r in rows) + (1 if baseline_tokens else 0)
    waves = (
        math.ceil(n / wave)
        + sum(r["waves"] for r in rows)
        + (1 if baseline_tokens else 0)
    )
    specs = _model_list(models)
    if not specs:
        current = current_model()
        specs = [current] if current else []
    catalog = load_model_catalog()
    model_rows = []
    for spec in specs:
        prices = model_prices(spec, catalog, price_in, price_out)
        row = {"model": spec, "prices": None, "costs": None}
        if prices:
            pin, pout = prices
            row["prices"] = {"input": pin, "output": pout}
            row["costs"] = {
                name: round(
                    (levels[name]["input"] * pin + levels[name]["output"] * pout)
                    / 1000000.0,
                    4,
                )
                for name in ("low", "expected", "high")
            }
        model_rows.append(row)
    return {
        "agents": n,
        "rounds": len(rows),
        "calls": calls,
        "waves": waves,
        "wave": wave,
        "task_chars": len(task),
        "rubric_chars": len(rubric_text),
        "baseline_chars": len(baseline_text),
        "task_tokens": task_tokens,
        "levels": levels,
        "models": model_rows,
        "cost_basis": "whole tournament on this model",
        "assumptions": {
            "overhead_per_call": EST_OVERHEAD,
            "card_tokens": EST_CARD,
            "identity_tokens": EST_IDENTITY,
            "solution_tokens": list(EST_SOLUTIONS),
            "attack_tokens": EST_ATTACK,
            "defense_tokens": EST_DEFENSE,
            "judge_tokens": EST_JUDGE,
            "final_tokens": EST_FINAL,
        },
        "diversity_warning": (
            "Full diversity compares solutions, not cleanly isolated models. "
            "Different cards and different models both affect a result."
        ),
    }


def _read_optional(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _money(x):
    return "unknown" if x is None else "$%.2f" % x


def cmd_estimate(args):
    n = _agents_arg(args)
    if args.wave < 1:
        raise ArenaError("--wave must be at least 1")
    if (args.price_in is None) != (args.price_out is None):
        raise ArenaError("pass both --price-in and --price-out, or neither")
    task = ""
    baseline = ""
    if args.task_file:
        task = _read_optional(args.task_file)
        if not task:
            raise ArenaError("could not read --task-file")
    elif args.task:
        task = args.task
    else:
        try:
            d = run_dir_from(args)
            task = _read_optional(os.path.join(d, "task.md"))
            if not baseline:
                baseline = _read_optional(os.path.join(d, "baseline.md"))
        except ArenaError:
            pass
    if args.baseline_file:
        baseline = _read_optional(args.baseline_file)
        if not baseline:
            raise ArenaError("could not read --baseline-file")
    models = _model_list(args.models) or _model_list(args.model)
    models += [m for m in _model_list(args.judge_models) if m not in models]
    report = estimate_payload(
        n,
        args.wave,
        task,
        _read_optional(RUBRIC_PATH),
        baseline,
        models,
        args.price_in,
        args.price_out,
    )
    if task:
        report["task"] = task.strip()
    if args.json:
        print(json.dumps(report, indent=1))
        return 0
    print("arena estimate")
    print(
        "%d agents, %d rounds, %d sub-agent calls in %d wave(s) of up to %d"
        % (
            report["agents"],
            report["rounds"],
            report["calls"],
            report["waves"],
            report["wave"],
        )
    )
    print(
        "task %d chars (%d tokens), rubric %d chars, baseline %d chars"
        % (
            report["task_chars"],
            report["task_tokens"],
            report["rubric_chars"],
            report["baseline_chars"],
        )
    )
    if not task:
        print(
            "no task text supplied; token counts cover only the fixed brief overhead and outputs"
        )
    print("")
    print("  scenario   input tokens  output tokens  total tokens")
    for name in ("low", "expected", "high"):
        level = report["levels"][name]
        print(
            "  %-9s  %12d  %13d  %12d"
            % (name, level["input"], level["output"], level["total"])
        )
    print("")
    print("  expected phase breakdown")
    print("  phase                   calls  input tokens  output tokens")
    for row in report["levels"]["expected"]["phases"]:
        print(
            "  %-22s  %5d  %12d  %13d"
            % (row["phase"], row["calls"], row["input"], row["output"])
        )
    print("")
    if report["models"]:
        print(
            "  model                                      low       expected       high"
        )
        for row in report["models"]:
            if row["costs"]:
                print(
                    "  %-40s %8s %14s %10s"
                    % (
                        row["model"],
                        _money(row["costs"]["low"]),
                        _money(row["costs"]["expected"]),
                        _money(row["costs"]["high"]),
                    )
                )
            else:
                print(
                    "  %-40s %8s %14s %10s"
                    % (row["model"], "unknown", "unknown", "unknown")
                )
    else:
        print("  model: unknown; token counts only")
    print("")
    print(
        "Each model row is the cost of running the whole tournament on that model, not its share"
    )
    print(
        "of a mixed roster. A mixed run splits jobs across models and normally costs less."
    )
    print("")
    print(
        "Assumptions: %d overhead tokens per call, %d per card, %d per worker identity,"
        " solution sizes %s,"
        % (
            EST_OVERHEAD,
            EST_CARD,
            EST_IDENTITY,
            "/".join(str(x) for x in EST_SOLUTIONS),
        )
    )
    print(
        "attack %d, defense %d, judge %d, final %d output tokens."
        % (EST_ATTACK, EST_DEFENSE, EST_JUDGE, EST_FINAL)
    )
    print(
        "Costs use current catalog prices per million tokens. A free model costs $0.00."
    )
    print(report["diversity_warning"])
    return 0


# ---------------------------------------------------------------- state io


def run_dir_from(args):
    if getattr(args, "dir", None):
        return os.path.abspath(args.dir)
    latest = os.path.join(ROOT, LATEST)
    if os.path.isfile(latest):
        with open(latest, encoding="utf-8") as fh:
            return fh.read().strip()
    raise ArenaError("no arena here. Run init first, or pass --dir")


def load_state(args):
    d = run_dir_from(args)
    path = os.path.join(d, STATE_FILE)
    if not os.path.isfile(path):
        raise ArenaError("no %s in %s" % (STATE_FILE, d))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    path = os.path.join(state["dir"], STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def _cmd(*parts):
    return 'python3 "%s" %s' % (SELF, " ".join(parts))


def _card_line(card):
    return "%s + %s + %s" % (
        card["reasoning"]["name"],
        card["workflow"]["name"],
        card["strategy"]["name"],
    )


# ---------------------------------------------------------------- commands


def _agents_arg(args):
    if args.quick and args.agents is not None:
        raise ArenaError("pass --quick or --agents, not both")
    if args.quick:
        return QUICK_AGENTS
    return DEFAULT_AGENTS if args.agents is None else args.agents


def cmd_plan(args):
    n = _agents_arg(args)
    if n < 1:
        raise ArenaError("--agents must be at least 1")
    t = plan_totals(n, args.wave)
    if args.json:
        print(json.dumps(t, indent=1))
        return 0
    print("%d agents, %d rounds, waves of %d" % (n, t["rounds"], args.wave))
    print("")
    print("  round  alive  matches  bye  sub-agent calls  waves")
    print(
        "  spawn  %5d  %7s  %3s  %15d  %5d" % (n, "-", "-", n, math.ceil(n / args.wave))
    )
    for r in t["rows"]:
        print(
            "  %5d  %5d  %7d  %3s  %15d  %5d"
            % (
                r["round"],
                r["alive"],
                r["matches"],
                "yes" if r["bye"] else "-",
                r["calls"],
                r["waves"],
            )
        )
    print("  total                     %15d  %5d" % (t["calls"], t["waves"]))
    print("")
    print("  alive per round: %s" % " -> ".join(str(s) for s in bracket_sizes(n)))
    print("  plus 1 call for the final check when there is a rejected answer to beat")
    return 0


def cmd_init(args):
    n = _agents_arg(args)
    if args.wave < 1:
        raise ArenaError("--wave must be at least 1")
    data = load_strategies()
    if n < 1 or n > combo_count(data):
        raise ArenaError(
            "--agents must be between 1 and %d (the number of distinct cards)"
            % combo_count(data)
        )
    if args.task_file:
        with open(args.task_file, encoding="utf-8") as fh:
            task = fh.read()
    else:
        task = args.task or ""
    task = task.strip()
    if not task:
        raise ArenaError("the task is empty. Pass --task-file or --task")
    baseline = None
    if args.baseline_file:
        with open(args.baseline_file, encoding="utf-8") as fh:
            baseline = fh.read().strip()
        if not baseline:
            raise ArenaError("the baseline file is empty")
    seed = (
        args.seed
        if args.seed is not None
        else random.SystemRandom().randrange(1, 1000000)
    )
    default_dir = not args.dir
    d = os.path.abspath(
        args.dir
        or os.path.join(ROOT, "run-%s-s%s" % (time.strftime("%Y%m%d-%H%M%S"), seed))
    )
    if os.path.exists(os.path.join(d, STATE_FILE)):
        raise ArenaError("%s already has an arena in it. Pick another --dir" % d)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "task.md"), "w", encoding="utf-8") as fh:
        fh.write(task + "\n")
    if baseline:
        with open(os.path.join(d, "baseline.md"), "w", encoding="utf-8") as fh:
            fh.write(baseline + "\n")
    # The run gets its own copy of the rubric: frozen for this run, and inside the
    # working directory, so judges can read it without a permission prompt each.
    with (
        open(RUBRIC_PATH, encoding="utf-8") as src,
        open(os.path.join(d, "rubric.md"), "w", encoding="utf-8") as dst,
    ):
        dst.write(src.read())
    models = _model_list(args.models)
    judge_models = _model_list(args.judge_models)
    state = new_state(
        n,
        seed,
        data,
        d,
        wave=args.wave,
        has_baseline=bool(baseline),
        models=models,
        judge_models=judge_models,
        task=task,
    )
    if args.approved:
        state["estimate"] = estimate_payload(
            n,
            args.wave,
            task,
            _read_optional(RUBRIC_PATH),
            baseline or "",
            models + [m for m in judge_models if m not in models],
        )
    save_state(state)
    if default_dir:
        os.makedirs(ROOT, exist_ok=True)
        with open(os.path.join(ROOT, LATEST), "w", encoding="utf-8") as fh:
            fh.write(d + "\n")
    t = plan_totals(n, args.wave)
    print("arena ready: %s" % d)
    print(
        "seed %s. %d agents, %d distinct cards dealt from %d, no repeats."
        % (seed, n, n, combo_count(data))
    )
    print(
        "%d rounds (%s). %d sub-agent calls in %d waves of %d%s."
        % (
            t["rounds"],
            " -> ".join(str(s) for s in bracket_sizes(n)),
            t["calls"] + (1 if baseline else 0),
            t["waves"] + (1 if baseline else 0),
            args.wave,
            ", including the final check" if baseline else "",
        )
    )
    if models:
        counts = {}
        for model in state["models"]["competitors"].values():
            counts[model] = counts.get(model, 0) + 1
        print(
            "competitor models: %s"
            % ", ".join("%s x%d" % (m, counts[m]) for m in models if m in counts)
        )
        print("judge models: %s" % ", ".join(state["models"]["judge_pool"]))
        if len(models) > 1:
            print(
                "full diversity: each model gets a different mix of strategy cards. "
                "Results compare solutions, not isolated models."
            )
        primaries = state["models"].get("primaries") or {}
        if primaries:
            print(
                "primaries: %s"
                % ", ".join("%s -> %s" % (m, aid) for m, aid in primaries.items())
            )
        print("judge reserves: %s" % ", ".join(state["models"]["judge_pool"]))
    print(
        "worker identities: task-filtered roles and stakes; every agent gets a unique combination."
    )
    print(
        "new model-pinned agents need a new opencode session before task can see them."
    )
    if args.approved:
        print("approved estimate stored in %s" % os.path.join(d, STATE_FILE))
    print("next: %s" % _cmd("next"))
    return 0


def cmd_next(args):
    state = load_state(args)
    phase, left, jobs = next_action(state)
    rd = current_round(state)
    total_rounds = len(bracket_sizes(state["agents_n"])) - 1
    if phase == "spawn":
        print(
            "spawn: %d competitors, then %d round(s)"
            % (state["agents_n"], total_rounds)
        )
    elif rd:
        print("round %d of %d, %d alive" % (rd["n"], total_rounds, len(rd["alive"])))
    elif state["champion"]:
        print(
            "champion: %s after %d round(s)" % (state["champion"], rounds_played(state))
        )
    if phase in PHASES:
        print("NEXT: %s. %d of %d job(s) to run." % (phase, len(left), len(jobs)))
        print("  1. %s" % _cmd("prompts", phase))
        print("  2. launch the jobs it lists, one wave of %d at a time" % state["wave"])
        print("  3. %s" % _cmd("next"))
    elif phase == "collect":
        print("NEXT: collect. Every verdict for this step is on disk.")
        print("  %s" % _cmd("collect"))
    elif phase == "advance":
        print("NEXT: advance. Every match in round %d has a winner." % rd["n"])
        print("  %s" % _cmd("advance"))
    else:
        print("DONE. %s" % _cmd("winner"))
    return 0


def cmd_prompts(args):
    state = load_state(args)
    jobs = write_prompts(state, args.phase)
    if not jobs:
        print("nothing to run for %s right now. %s" % (args.phase, _cmd("next")))
        return 0
    left = missing_jobs(jobs)
    w = state["wave"]
    rd = current_round(state)
    where = (
        "round 0"
        if args.phase == "spawn"
        else ("final" if args.phase == "final" else "round %d" % rd["n"])
    )
    waves = [left[i : i + w] for i in range(0, len(left), w)]
    if args.json:
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "where": where,
                    "wave": w,
                    "jobs": jobs,
                    "pending": [j["id"] for j in left],
                    "waves": [[j["id"] for j in wave] for wave in waves],
                },
                indent=1,
            )
        )
        return 0
    print(
        "%s, %s: %d job(s), %d still to run, %d wave(s) of up to %d"
        % (where, args.phase, len(jobs), len(left), len(waves), w)
    )
    for k, wave in enumerate(waves, start=1):
        print("wave %d" % k)
        for j in wave:
            print(
                "  %-26s %-34s %-18s %s"
                % (
                    j["id"],
                    j.get("model") or "current",
                    j.get("subagent") or "arena",
                    j["prompt"],
                )
            )
    if left:
        print("")
        print(
            "Each job is one opencode task call. Use the subagent_type shown beside it."
        )
        print(
            'description "arena <job id>", prompt "Read <prompt path> and follow it exactly."'
        )
        print(
            "The visible model-pinned agents keep each child session and its reasoning clickable."
        )
        print(
            "Use arena_task only for a model with no visible agent; it is a tool card, not a subagent."
        )
        print(
            "A wave is one message with all of its task calls. Let it finish before the next wave."
        )
    print("then: %s" % _cmd("next"))
    return 0


def cmd_check(args):
    state = load_state(args)
    jobs = phase_jobs(state, args.phase)
    left = missing_jobs(jobs)
    print("%s: %d of %d job(s) done" % (args.phase, len(jobs) - len(left), len(jobs)))
    for j in left:
        gone = [p for p in j["outputs"] if not _has_output(p)]
        print("  missing %-26s %s" % (j["id"], ", ".join(gone)))
    return 1 if left else 0


def cmd_pairings(args):
    state = load_state(args)
    rd = current_round(state)
    if rd is None:
        if state["champion"]:
            print("No matches left. %s is the last one standing." % state["champion"])
            return 0
        raise ArenaError("no round is open")
    if args.json:
        print(json.dumps(rd, indent=1))
        return 0
    agents = state["agents"]
    print(
        "round %d: %d alive, %d match(es), bye: %s"
        % (rd["n"], len(rd["alive"]), len(rd["matches"]), rd["bye"] or "none")
    )
    for m in rd["matches"]:
        ra = agents[m["a"]]["card"]["reasoning"]["name"]
        rb = agents[m["b"]]["card"]["reasoning"]["name"]
        res = ("winner %s" % m["winner"]) if m["winner"] else "open"
        print("  %s  %s vs %s  (%s vs %s)  %s" % (m["id"], m["a"], m["b"], ra, rb, res))
    if rd["bye"]:
        print("  bye     %s goes through without a match" % rd["bye"])
    return 0


def cmd_record(args):
    state = load_state(args)
    m, previous = record(
        state,
        args.match_id,
        args.winner_id,
        reason=args.reason,
        survived=args.survived,
        source="manual",
    )
    save_state(state)
    extra = " (was %s)" % previous if previous and previous != m["winner"] else ""
    print("%s: %s beat %s%s" % (m["id"], m["winner"], m["loser"], extra))
    return 0


def cmd_collect(args):
    state = load_state(args)
    results = collect(state)
    save_state(state)
    if not results:
        print("nothing to collect. %s" % _cmd("next"))
        return 0
    bad = 0
    for mid, what, note in results:
        if what != "recorded":
            bad += 1
        print("  %-10s %-10s %s" % (mid, what, note))
    rd = current_round(state)
    if rd:
        done = sum(1 for m in rd["matches"] if m["winner"])
        print(
            "round %d: %d of %d match(es) recorded"
            % (rd["n"], done, len(rd["matches"]))
        )
    if bad:
        print(
            "%d verdict(s) missing or unreadable. Re-run those judges: %s"
            % (bad, _cmd("next"))
        )
        return 1
    print("then: %s" % _cmd("next"))
    return 0


def cmd_advance(args):
    state = load_state(args)
    rd = current_round(state)
    closing = rd["n"] if rd else None
    survivors = advance(state)
    save_state(state)
    print(
        "round %s closed: %d through, %d eliminated"
        % (closing, len(survivors), len(rd["matches"]))
    )
    if state["champion"]:
        print("CHAMPION: %s. %s" % (state["champion"], _cmd("next")))
    else:
        nr = state["rounds"][-1]
        print(
            "round %d open: %d match(es)%s. %s"
            % (
                nr["n"],
                len(nr["matches"]),
                ", bye %s" % nr["bye"] if nr["bye"] else "",
                _cmd("next"),
            )
        )
    return 0


def _fallback_target(state, job_id):
    md = state.get("models") or {}
    if job_id == "final.judge":
        return md, "final_judge", None
    if job_id.endswith(".judge"):
        mid = job_id[:-6]
        return md, "judges", mid
    if job_id.endswith(".spawn"):
        aid = job_id[:-6]
        return md, "competitors", aid
    if job_id.endswith(".attack") or job_id.endswith(".defend"):
        parts = job_id.split(".")
        if len(parts) >= 3:
            return md, "competitors", parts[-2]
    raise ArenaError("cannot map job '%s' to a model assignment" % job_id)


def _assigned_jobs(state, model):
    """Every job id currently assigned to a model."""
    md = state.get("models") or {}
    jobs = []
    for aid, assigned in (md.get("competitors") or {}).items():
        if assigned == model:
            jobs.append(aid + ".spawn")
    for mid, assigned in (md.get("judges") or {}).items():
        if assigned == model:
            jobs.append(mid + ".judge")
    if md.get("final_judge") == model:
        jobs.append("final.judge")
    return jobs


def _check_replacement(md, previous, new, job=None):
    """A replacement must be unused, un-failed and not reserved for judging."""
    if not new or "/" not in new:
        raise ArenaError("replacement must look like provider/model")
    if new == previous:
        raise ArenaError("replacement '%s' is already the model for this job" % new)
    if new in (md.get("failed") or []):
        raise ArenaError("'%s' already failed in this run" % new)
    if new in (md.get("used") or []):
        raise ArenaError("'%s' is already used or reserved in this run" % new)
    if new in (md.get("judge_pool") or []):
        raise ArenaError("'%s' is reserved for the judge pool" % new)
    if job:
        current = _assigned_jobs(md, new)
        if current:
            raise ArenaError(
                "'%s' is already assigned to %s" % (new, ", ".join(current))
            )


def cmd_fallback(args):
    state = load_state(args)
    md, where, key = _fallback_target(state, args.job_id)
    if not md:
        raise ArenaError("this run has no model roster")
    if where == "final_judge":
        previous = md.get("final_judge")
    else:
        previous = (md.get(where) or {}).get(key)
    new = _model_list(args.model)[0] if _model_list(args.model) else ""
    _check_replacement(md, previous, new, job=args.job_id)
    if where == "final_judge":
        md["final_judge"] = new
    else:
        bucket = md.setdefault(where, {})
        bucket[key] = new
    md.setdefault("failed", [])
    if previous and previous not in md["failed"]:
        md["failed"].append(previous)
    used = md.setdefault("used", [])
    if new not in used:
        used.append(new)
    md["degraded"] = True
    md.setdefault("fallbacks", []).append(
        {
            "job": args.job_id,
            "from": previous,
            "to": new,
            "reason": args.reason or "",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_state(state)
    print("%s: %s -> %s. Run marked degraded." % (args.job_id, previous or "none", new))
    remaining = [job for job in _assigned_jobs(state, previous) if job != args.job_id]
    if remaining:
        print(
            "warning: %s still owns %s. Fall those jobs back or drop the model."
            % (previous, ", ".join(remaining))
        )
    return 0


def cmd_drop(args):
    state = load_state(args)
    md = state.get("models") or {}
    if not md:
        raise ArenaError("this run has no model roster")
    old = _model_list(args.model)[0] if _model_list(args.model) else ""
    new = _model_list(args.replacement)[0] if _model_list(args.replacement) else ""
    if not old or old not in (md.get("used") or []):
        raise ArenaError("'%s' is not used in this run" % old)
    _check_replacement(md, old, new)
    moved = []
    for aid, assigned in list((md.get("competitors") or {}).items()):
        if assigned == old:
            md["competitors"][aid] = new
            moved.append(aid + ".spawn")
    for mid, assigned in list((md.get("judges") or {}).items()):
        if assigned == old:
            md["judges"][mid] = new
            moved.append(mid + ".judge")
    if md.get("final_judge") == old:
        md["final_judge"] = new
        moved.append("final.judge")
    if not moved:
        raise ArenaError("'%s' has no current job assignments" % old)
    md.setdefault("failed", [])
    if old not in md["failed"]:
        md["failed"].append(old)
    if new not in (md.get("used") or []):
        md.setdefault("used", []).append(new)
    md["degraded"] = True
    md.setdefault("fallbacks", []).append(
        {
            "job": "drop " + old,
            "from": old,
            "to": new,
            "reason": args.reason or "",
            "jobs": moved,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_state(state)
    print(
        "dropped %s for %s. %d job(s) moved to %s. Run marked degraded."
        % (old, new, len(moved), new)
    )
    if len(moved) > 1:
        print("note: %s now covers more than one job; the cards still differ." % new)
    return 0


def model_availability(state):
    md = state.get("models") or {}
    pool = list(md.get("pool") or [])
    used = list(md.get("used") or [])
    failed = list(md.get("failed") or [])
    available = [m for m in pool if m not in used and m not in failed]
    return {
        "pool": pool,
        "used": used,
        "failed": failed,
        "available": available,
        "reserved": list(md.get("judge_pool") or []),
    }


def cmd_roster(args):
    """Show the roster opencode will register agents from, and refresh it from the live models."""
    read_path = args.path or find_agent_roster()
    if args.path:
        write_path = args.path
    elif getattr(args, "package", False):
        write_path = DEFAULT_AGENT_ROSTER
    else:
        write_path = config_roster_path()
    live = []
    error = None
    if not args.offline:
        try:
            live = live_models()
        except ArenaError as exc:
            error = str(exc)
    rows = load_roster_rows(read_path)
    selected = select_roster(live) if live else []
    kept = {row["model"] for row in selected}
    skipped = []
    for row in live:
        if row["model"] in kept:
            continue
        skipped.append(
            {
                "model": row["model"],
                "reason": roster_skip_reason(row) or "not selected",
                "input": row["input"],
                "output": row["output"],
                "context": row["context"],
            }
        )
    live_ids = {row["model"] for row in live}
    written = []
    if args.refresh:
        if not selected:
            raise ArenaError("no usable free model found live, roster left unchanged")
        written.append(write_roster_file(selected, write_path))
        rows = load_roster_rows(write_path)
    roster_ids = {row["model"] for row in rows}
    phantom = [row["model"] for row in rows if live and row["model"] not in live_ids]
    missing = [row["model"] for row in selected if row["model"] not in roster_ids]
    if args.json:
        print(
            json.dumps(
                {
                    "path": read_path,
                    "writePath": write_path,
                    "live": live,
                    "selected": selected,
                    "skipped": skipped,
                    "roster": rows,
                    "phantom": phantom,
                    "missing": missing,
                    "written": written,
                    "error": error,
                },
                indent=1,
            )
        )
        return 0
    if written:
        print("roster refreshed: %d model(s) written to %s" % (len(rows), write_path))
        for extra in written[1:]:
            print("  and %s" % extra)
        print("opencode picks these up the next time it starts.")
    else:
        print("roster file: %s" % read_path)
    print(
        "live models: %d, usable free: %d, in the roster: %d"
        % (len(live), len(selected), len(rows))
    )
    for row in rows:
        print(
            "  %-42s %-28s ctx %-9s %s"
            % (
                row["model"],
                row["agent"],
                row["context"],
                "free" if row["free"] else "paid",
            )
        )
    if skipped:
        print("not in the roster (%d):" % len(skipped))
        for row in skipped:
            print("  %-42s %s" % (row["model"], row["reason"]))
    if phantom:
        print(
            "stale in the roster, not live (%d): %s"
            % (len(phantom), ", ".join(phantom))
        )
    if missing:
        print(
            "live free models missing from the roster (%d): %s"
            % (len(missing), ", ".join(missing))
        )
    if error:
        print("live model list failed: %s" % error)
    return 0


def cmd_models(args):
    state = load_state(args)
    md = state.get("models") or {}
    avail = model_availability(state)
    if args.json:
        print(
            json.dumps(
                {
                    "strategy": md.get("strategy", "uniform"),
                    "primaries": md.get("primaries") or {},
                    "competitors": md.get("competitors") or {},
                    "judges": md.get("judges") or {},
                    "final_judge": md.get("final_judge"),
                    "degraded": bool(md.get("degraded")),
                    "availability": avail,
                },
                indent=1,
            )
        )
        return 0
    if not md:
        print("no model roster was recorded for this run")
        return 0
    print("strategy: %s" % md.get("strategy", "uniform"))
    print("primaries:")
    for model, aid in (md.get("primaries") or {}).items():
        print("  %-38s %s" % (model, aid))
    print("judge pool: %s" % (", ".join(md.get("judge_pool") or []) or "none"))
    print("failed: %s" % (", ".join(avail["failed"]) or "none"))
    print(
        "available unused pool models: %s" % (", ".join(avail["available"]) or "none")
    )
    if md.get("degraded"):
        print("degraded: yes")
    return 0


def cmd_degrade(args):
    state = load_state(args)
    md = state.get("models") or {}
    md["degraded"] = True
    state["models"] = md
    save_state(state)
    print("run marked degraded: at least one job did not use its assigned model")
    return 0


def status_rows(state):
    rows = []
    for rd in state["rounds"]:
        recorded = sum(1 for m in rd["matches"] if m["winner"])
        rows.append(
            {
                "round": rd["n"],
                "alive": len(rd["alive"]),
                "matches": len(rd["matches"]),
                "bye": rd["bye"],
                "eliminated": recorded,
                "state": "closed"
                if rd["closed"]
                else "open, %d of %d recorded" % (recorded, len(rd["matches"])),
            }
        )
    return rows


def model_stats(state):
    """Per-model record. Wins and losses are match results, byes are free passes."""
    md = state.get("models") or {}
    if not md.get("pool"):
        return {}
    stats = {}

    def add(model):
        model = model or "unassigned"
        return stats.setdefault(
            model,
            {
                "agents": 0,
                "alive": 0,
                "wins": 0,
                "losses": 0,
                "byes": 0,
                "judge_matches": 0,
                "champion": False,
            },
        )

    for aid, agent in state["agents"].items():
        row = add((md.get("competitors") or {}).get(aid))
        row["agents"] += 1
        if agent["alive"]:
            row["alive"] += 1
    for rd in state["rounds"]:
        for match in rd["matches"]:
            if not match["winner"]:
                continue
            winner = match["winner"]
            loser = match["loser"]
            add((md.get("competitors") or {}).get(winner))["wins"] += 1
            add((md.get("competitors") or {}).get(loser))["losses"] += 1
        if rd["bye"]:
            add((md.get("competitors") or {}).get(rd["bye"]))["byes"] += 1
    for model in (md.get("judges") or {}).values():
        add(model)["judge_matches"] += 1
    if state.get("champion"):
        row = add((md.get("competitors") or {}).get(state["champion"]))
        row["champion"] = True
    return stats


def cmd_report(args):
    state = load_state(args)
    md = state.get("models") or {}
    stats = model_stats(state)
    champion_model = (md.get("competitors") or {}).get(state.get("champion"))
    warning = (
        "Full diversity compares solutions, not cleanly isolated models. "
        "Different cards and different models both affect a result."
    )
    if args.json:
        print(
            json.dumps(
                {
                    "dir": state["dir"],
                    "strategy": md.get("strategy", "uniform"),
                    "pool": md.get("pool") or [],
                    "primaries": md.get("primaries") or {},
                    "competitors": md.get("competitors") or {},
                    "judges": md.get("judges") or {},
                    "final_judge": md.get("final_judge"),
                    "used": md.get("used") or [],
                    "failed": md.get("failed") or [],
                    "available": model_availability(state)["available"],
                    "degraded": bool(md.get("degraded")),
                    "champion": state.get("champion"),
                    "champion_model": champion_model,
                    "per_model": stats,
                    "personas": {
                        aid: (agent.get("persona") or {}).get("text")
                        for aid, agent in state["agents"].items()
                    },
                    "warning": warning,
                },
                indent=1,
            )
        )
        return 0
    print("arena report: %s" % state["dir"])
    print("strategy: %s" % md.get("strategy", "uniform"))
    if not stats:
        print(
            "no model roster was recorded for this run. Use init --models to record one."
        )
        return 0
    print(
        "  model                                  agents  alive  wins  losses  byes  judged"
    )
    order = []
    for model in list(md.get("pool") or []) + list(md.get("judge_pool") or []):
        if model and model not in order:
            order.append(model)
    for model in order or sorted(stats):
        row = stats.get(model)
        if not row:
            continue
        print(
            "  %-38s %6d %6d %5d %7d %5d %7d"
            % (
                model,
                row["agents"],
                row["alive"],
                row["wins"],
                row["losses"],
                row["byes"],
                row["judge_matches"],
            )
        )
    if champion_model:
        print("champion model: %s" % champion_model)
    if md.get("final_judge"):
        print("final judge: %s" % md["final_judge"])
    primaries = md.get("primaries") or {}
    if primaries:
        print(
            "primaries: %s"
            % ", ".join("%s -> %s" % (m, a) for m, a in primaries.items())
        )
    if md.get("failed"):
        print("failed models: %s" % ", ".join(md["failed"]))
    available = model_availability(state)["available"]
    if available:
        print("unused pool models: %s" % ", ".join(available))
    if md.get("degraded"):
        print("degraded: at least one job fell back to another model")
    print(warning)
    return 0


def _opencode_binary():
    candidates = [
        os.environ.get("OPENCODE"),
        shutil.which("opencode"),
        os.path.expanduser("~/.nix-profile/bin/opencode"),
        os.path.expanduser("~/.local/bin/opencode"),
        "/usr/local/bin/opencode",
        "/usr/bin/opencode",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for path in sorted(glob.glob("/nix/store/*opencode*/bin/opencode"), reverse=True):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _version(path):
    try:
        r = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
        return (
            (r.stdout or r.stderr).strip().splitlines()[0]
            if (r.stdout or r.stderr).strip()
            else None
        )
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def cmd_doctor(args):
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("python", sys.version_info >= (3, 8), sys.version.split()[0])
    for name, path in (
        ("strategies", STRATEGIES_PATH),
        ("skill", SKILL_PATH),
        ("rubric", RUBRIC_PATH),
    ):
        check(name, os.path.isfile(path), path)
    check("bracket", os.path.isfile(SELF), SELF)
    catalog = load_model_catalog()
    check(
        "model cache",
        bool(catalog),
        MODEL_CACHE if catalog else "missing or unreadable",
    )
    current = current_model()
    check("current model", bool(current), current or "not found")
    if current:
        check(
            "current price",
            model_prices(current, catalog) is not None,
            "known"
            if model_prices(current, catalog) is not None
            else "unknown, token estimates only",
        )
    binary = _opencode_binary()
    check(
        "opencode",
        bool(binary),
        ("%s (%s)" % (binary, _version(binary) or "version unknown"))
        if binary
        else "not found",
    )
    plugin = os.path.join(opencode_config_dir(), "plugins", "arena-router.ts")
    check(
        "router plugin",
        os.path.isfile(plugin),
        plugin if os.path.isfile(plugin) else "absent, use native task",
    )
    package = os.path.join(HERE, "package.json")
    plugin_ready = False
    try:
        with open(package, encoding="utf-8") as fh:
            manifest = json.load(fh)
        entry = (manifest.get("exports") or {}).get("./server")
        plugin_ready = bool(entry) and os.path.isfile(os.path.join(HERE, entry))
    except (OSError, ValueError):
        plugin_ready = False
    check(
        "opencode plugin",
        plugin_ready,
        "the package exposes a ./server entrypoint"
        if plugin_ready
        else "the package has no usable ./server entrypoint",
    )
    roster = load_agent_roster()
    roster_path = find_agent_roster()
    check(
        "agent roster",
        bool(roster),
        "%d visible model agent(s) in %s" % (len(roster), roster_path)
        if roster
        else "%s missing or empty" % roster_path,
    )
    check(
        "packaged roster",
        os.path.isfile(DEFAULT_AGENT_ROSTER),
        DEFAULT_AGENT_ROSTER
        if os.path.isfile(DEFAULT_AGENT_ROSTER)
        else "absent, the live roster is all there is",
    )
    rows = load_roster_rows(roster_path)
    paid = [row["model"] for row in rows if not row["free"]]
    check(
        "roster cost",
        not paid,
        "every roster model is free"
        if not paid
        else "paid model(s) in the roster: %s" % ", ".join(paid),
    )
    if args.offline:
        check("roster freshness", True, "skipped")
    else:
        try:
            live = live_models()
        except ArenaError as exc:
            check("roster freshness", False, str(exc))
        else:
            live_ids = {row["model"] for row in live}
            phantom = [row["model"] for row in rows if row["model"] not in live_ids]
            check(
                "roster freshness",
                not phantom,
                "every roster model is live"
                if not phantom
                else "not live any more: %s" % ", ".join(phantom),
            )
            usable = {row["model"] for row in select_roster(live)}
            missing = sorted(usable - {row["model"] for row in rows})
            check(
                "roster coverage",
                not missing,
                "%d live free model(s), all in the roster" % len(usable)
                if not missing
                else "missing from the roster: %s" % ", ".join(missing),
            )
    command = os.path.join(opencode_config_dir(), "commands", "arena.md")
    check(
        "arena command",
        os.path.isfile(command),
        command if os.path.isfile(command) else "absent",
    )
    agent = os.path.join(HERE, "opencode", "agents", "arena.md")
    check(
        "arena agent",
        os.path.isfile(agent),
        "%s (the plugin registers it)" % agent if os.path.isfile(agent) else "absent",
    )
    linked = os.path.join(opencode_config_dir(), "skills", "arena")
    check(
        "arena skill link",
        os.path.isdir(linked),
        linked if os.path.isdir(linked) else "absent",
    )
    if args.json:
        print(json.dumps({"checks": checks}, indent=1))
    else:
        print("arena doctor")
        for row in checks:
            print(
                "  %-16s %-4s %s"
                % (row["name"], "ok" if row["ok"] else "warn", row["detail"])
            )
        print("")
        print(
            "native task: subagent_type arena, task_id resume; per-call model is not available on 1.19"
        )
        print(
            "router path: arena_models and arena_task use the SDK for arbitrary provider/model calls"
        )
        print(
            "permissions: allow external_directory for this skill folder and edit only inside .arena"
        )
    return (
        0
        if all(
            row["ok"]
            for row in checks
            if row["name"] in ("python", "strategies", "skill", "rubric", "bracket")
        )
        else 1
    )


def cmd_status(args):
    state = load_state(args)
    rows = status_rows(state)
    alive = alive_ids(state)
    md = state.get("models") or {}
    if args.json:
        print(
            json.dumps(
                {
                    "dir": state["dir"],
                    "seed": state["seed"],
                    "agents": state["agents_n"],
                    "alive": len(alive),
                    "eliminated": state["agents_n"] - len(alive),
                    "champion": state["champion"],
                    "rounds": rows,
                    "models": md,
                },
                indent=1,
            )
        )
        return 0
    print("arena %s" % state["dir"])
    print(
        "seed %s, %d agents, waves of %d, %d distinct cards available"
        % (state["seed"], state["agents_n"], state["wave"], state["cards_available"])
    )
    if md.get("pool"):
        print(
            "models: %s, strategy %s"
            % (", ".join(md["pool"]), md.get("strategy", "diverse"))
        )
    if md.get("failed"):
        print("failed models: %s" % ", ".join(md["failed"]))
    if md.get("degraded"):
        print("degraded: yes")
    print("  round  alive  matches  bye     eliminated  state")
    for r in rows:
        print(
            "  %5d  %5d  %7d  %-6s  %10d  %s"
            % (
                r["round"],
                r["alive"],
                r["matches"],
                r["bye"] or "-",
                r["eliminated"],
                r["state"],
            )
        )
    print(
        "now: %d alive, %d eliminated%s"
        % (
            len(alive),
            state["agents_n"] - len(alive),
            ", champion %s" % state["champion"] if state["champion"] else "",
        )
    )
    return 0


def winner_report(state):
    champ = state["champion"]
    if not champ:
        return None
    agent = state["agents"][champ]
    path = []
    survived = []
    for rd in state["rounds"]:
        if rd["bye"] == champ:
            path.append({"round": rd["n"], "bye": True})
        for m in rd["matches"]:
            if champ in (m["a"], m["b"]):
                opp = m["b"] if m["a"] == champ else m["a"]
                sc = m.get("scores") or {}
                path.append(
                    {
                        "round": rd["n"],
                        "bye": False,
                        "beat": opp,
                        "match": m["id"],
                        "score": sc.get(champ),
                        "opponent_score": sc.get(opp),
                        "reason": m.get("reason"),
                        "survived": m.get("survived") or [],
                    }
                )
                survived.extend(m.get("survived") or [])
    md = state.get("models") or {}
    return {
        "champion": champ,
        "model": (md.get("competitors") or {}).get(champ),
        "persona": (agent.get("persona") or {}).get("text"),
        "card": agent["card"],
        "card_line": _card_line(agent["card"]),
        "solution": agent["solution"],
        "rounds": rounds_played(state),
        "agents": state["agents_n"],
        "matches_won": sum(1 for p in path if not p["bye"]),
        "path": path,
        "attacks_survived": survived,
        "final": (state.get("final") or {}).get("result"),
    }


def cmd_winner(args):
    state = load_state(args)
    rep = winner_report(state)
    if rep is None:
        rd = current_round(state)
        print(
            "no winner yet: round %s, %d alive. %s"
            % (rd["n"] if rd else "?", len(alive_ids(state)), _cmd("next"))
        )
        return 1
    if args.json:
        print(json.dumps(rep, indent=1))
        return 0
    print("CHAMPION  %s" % rep["champion"])
    if rep.get("model"):
        print("model     %s" % rep["model"])
    if rep.get("persona"):
        print("identity  %s" % rep["persona"])
    print("card      %s" % rep["card_line"])
    print("solution  %s" % rep["solution"])
    print(
        "rounds    %d played, %d agents in, 1 left, %d match(es) won"
        % (rep["rounds"], rep["agents"], rep["matches_won"])
    )
    for p in rep["path"]:
        if p["bye"]:
            print("  round %d  bye" % p["round"])
            continue
        print(
            "  round %d  beat %s, %s to %s. %s"
            % (
                p["round"],
                p["beat"],
                _fmt(p["score"]),
                _fmt(p["opponent_score"]),
                p["reason"] or "",
            )
        )
        for s in p["survived"]:
            print("           survived: %s" % s)
    fin = rep["final"]
    if fin:
        verdict = (
            "the winner beats it"
            if fin["better"] == "champion"
            else "the answer you rejected scored higher. Say so"
        )
        print(
            "vs the answer you rejected: winner %s, rejected %s, %s. %s"
            % (
                _fmt(fin["champion_total"]),
                _fmt(fin["baseline_total"]),
                verdict,
                fin["reason"],
            )
        )
    return 0


def cmd_card(args):
    state = load_state(args)
    aid = norm_agent(state, args.agent_id)
    a = state["agents"][aid]
    c = a["card"]
    print(
        "%s  %s"
        % (
            aid,
            "alive"
            if a["alive"]
            else "eliminated in round %s by %s"
            % (a["eliminated_in"], a["eliminated_by"]),
        )
    )
    for part in ("reasoning", "workflow", "strategy"):
        print("  %-9s %s. %s" % (part, c[part]["name"], c[part]["how"]))
    persona = (a.get("persona") or {}).get("text")
    if persona:
        print("  identity  %s" % persona)
    print("  solution  %s" % a["solution"])
    return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dir", help="the arena run directory (default: the one in .arena/LATEST)"
    )
    p = argparse.ArgumentParser(
        prog="bracket.py", description="Tournament state for /arena."
    )
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    s = sub.add_parser("plan", help="rounds, sub-agent calls and waves for N agents")
    s.add_argument("--agents", type=int)
    s.add_argument("--quick", action="store_true")
    s.add_argument("--wave", type=int, default=DEFAULT_WAVE)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser(
        "estimate", parents=[common], help="tokens and cost before committing to a run"
    )
    s.add_argument("--agents", type=int)
    s.add_argument("--quick", action="store_true")
    s.add_argument("--wave", type=int, default=DEFAULT_WAVE)
    s.add_argument("--task-file", help="read the task text from this file")
    s.add_argument("--task", help="use this task text instead of a file")
    s.add_argument("--baseline-file", help="the rejected answer, for the final check")
    s.add_argument("--models", help="comma-separated competitor model IDs")
    s.add_argument("--judge-models", help="comma-separated judge model IDs")
    s.add_argument("--model", help="a single model ID to price")
    s.add_argument(
        "--price-in", type=float, help="override input USD per million tokens"
    )
    s.add_argument(
        "--price-out", type=float, help="override output USD per million tokens"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_estimate)

    s = sub.add_parser("doctor", help="check this opencode install and the arena files")
    s.add_argument("--json", action="store_true")
    s.add_argument("--offline", action="store_true", help="skip the live model check")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser(
        "roster", help="show the model roster opencode registers agents from"
    )
    s.add_argument(
        "--refresh",
        action="store_true",
        help="rebuild the roster from the live opencode models",
    )
    s.add_argument(
        "--path", help="roster file to read or write (default: the live one in the config dir)"
    )
    s.add_argument(
        "--package",
        action="store_true",
        help="write the packaged default roster instead of the live one",
    )
    s.add_argument(
        "--offline", action="store_true", help="do not ask opencode for its live models"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_roster)

    s = sub.add_parser(
        "init", parents=[common], help="deal the cards and write arena.json"
    )
    s.add_argument(
        "--agents", type=int, help="number of competitors (default %d)" % DEFAULT_AGENTS
    )
    s.add_argument("--quick", action="store_true", help="%d competitors" % QUICK_AGENTS)
    s.add_argument(
        "--seed",
        type=int,
        help="fixes the cards and the pairings (default: random, recorded)",
    )
    s.add_argument(
        "--task-file", help="the task, word for word, as every competitor will get it"
    )
    s.add_argument("--task", help="the task as a string, instead of --task-file")
    s.add_argument("--baseline-file", help="the answer the user was not satisfied with")
    s.add_argument(
        "--wave",
        type=int,
        default=DEFAULT_WAVE,
        help="parallel sub-agents per wave (default 10)",
    )
    s.add_argument(
        "--models", help="comma-separated competitor model IDs, one per competitor"
    )
    s.add_argument("--judge-models", help="comma-separated judge model IDs")
    s.add_argument(
        "--approved", action="store_true", help="store the estimate as approved"
    )
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("next", parents=[common], help="what to do now")
    s.set_defaults(func=cmd_next)

    for name, fn, hlp in (
        ("prompts", cmd_prompts, "write the briefs for a phase and list the jobs left"),
        ("check", cmd_check, "list the jobs whose outputs are missing"),
    ):
        s = sub.add_parser(name, parents=[common], help=hlp)
        s.add_argument("phase", choices=PHASES)
        if name == "prompts":
            s.add_argument("--json", action="store_true")
        s.set_defaults(func=fn)

    s = sub.add_parser(
        "pairings", parents=[common], help="this round's matches, including the bye"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_pairings)

    s = sub.add_parser("record", parents=[common], help="record a match result by hand")
    s.add_argument("match_id")
    s.add_argument("winner_id")
    s.add_argument("--reason")
    s.add_argument(
        "--survived", action="append", help="an attack the winner survived (repeatable)"
    )
    s.set_defaults(func=cmd_record)

    s = sub.add_parser(
        "collect", parents=[common], help="record every verdict the judges wrote"
    )
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser(
        "advance", parents=[common], help="close the round and pair the survivors"
    )
    s.set_defaults(func=cmd_advance)

    s = sub.add_parser(
        "fallback", parents=[common], help="record a job that ran on a fallback model"
    )
    s.add_argument("job_id")
    s.add_argument("model")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_fallback)

    s = sub.add_parser(
        "drop", parents=[common], help="drop a failed model and move all its jobs"
    )
    s.add_argument("model")
    s.add_argument("replacement")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_drop)

    s = sub.add_parser(
        "models", parents=[common], help="show model assignments, failures and reserves"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser(
        "degrade", parents=[common], help="mark the run as using fallback models"
    )
    s.set_defaults(func=cmd_degrade)

    s = sub.add_parser(
        "status", parents=[common], help="alive and eliminated, per round"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser(
        "report", parents=[common], help="per-model wins, losses and assignments"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser(
        "winner", parents=[common], help="the survivor and how it got there"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_winner)

    s = sub.add_parser("card", parents=[common], help="one competitor's strategy card")
    s.add_argument("agent_id")
    s.set_defaults(func=cmd_card)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ArenaError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

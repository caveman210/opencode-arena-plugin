#!/usr/bin/env node
// The arena command line.
//
//   arena doctor            check the install, the models and the roster
//   arena roster --refresh  rebuild the model roster from the live models
//   arena plan --agents 16  anything else goes straight to bracket.py
//
// It is a thin wrapper: bracket.py holds the whole state machine.
import { spawnSync } from "node:child_process"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const pkg = dirname(here)
const bracket = join(pkg, "bracket.py")

const args = process.argv.slice(2)
const known = new Set(["doctor", "roster", "plan", "estimate", "init", "next", "prompts", "check",
  "pairings", "record", "collect", "advance", "fallback", "drop", "models", "degrade", "status",
  "report", "winner", "card"])
const first = args[0]
const rest = first && known.has(first) ? args.slice(1) : args

const result = spawnSync("python3", [bracket, ...rest], { stdio: "inherit" })
if (result.error) {
  console.error(`arena: could not run python3: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status === null ? 1 : result.status)

#!/usr/bin/env node
// Runs before publishing: the test suite and a sanity check on the package.
import { spawnSync } from "node:child_process"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { existsSync, readFileSync } from "node:fs"

const here = dirname(fileURLToPath(import.meta.url))
const pkg = dirname(here)

function run(label, cmd, args) {
  console.log(`> ${label}`)
  const result = spawnSync(cmd, args, { cwd: pkg, stdio: "inherit" })
  if (result.status !== 0) {
    console.error(`arena: ${label} failed`)
    process.exit(result.status || 1)
  }
}

const skill = readFileSync(join(pkg, "SKILL.md"), "utf8")
if (!/^---\nname: arena\n/.test(skill)) {
  console.error("arena: SKILL.md needs a frontmatter block with 'name: arena'")
  process.exit(1)
}
for (const file of ["bracket.py", "rubric.md", "strategies.json", "arena-models.json",
  "plugin/arena-router.ts", "opencode/agents/arena.md", "opencode/commands/arena.md",
  "bin/arena.js", "scripts/install.mjs"]) {
  if (!existsSync(join(pkg, file))) {
    console.error(`arena: ${file} is missing`)
    process.exit(1)
  }
}

// The opencode plugin contract: opencode resolves a plugin through
// exports["./server"], then calls the default export's server() hook.
const manifest = JSON.parse(readFileSync(join(pkg, "package.json"), "utf8"))
const exportsMap = manifest.exports || {}
for (const key of [".", "./server"]) {
  let target = exportsMap[key]
  if (target && typeof target === "object") target = target.import || target.default
  if (!target) {
    console.error(`arena: package.json exports["${key}"] is missing`)
    process.exit(1)
  }
  if (!existsSync(join(pkg, target))) {
    console.error(`arena: package.json exports["${key}"] points at a missing file: ${target}`)
    process.exit(1)
  }
}
if (!manifest.engines || !manifest.engines.opencode) {
  console.error("arena: package.json needs engines.opencode so opencode accepts the plugin")
  process.exit(1)
}
const pluginSource = readFileSync(join(pkg, "plugin/arena-router.ts"), "utf8")
if (!/export\s+default\s*\{[^}]*\bid\s*:/s.test(pluginSource)) {
  console.error("arena: plugin/arena-router.ts needs a default export with an id")
  process.exit(1)
}

run("python tests", "python3", ["-m", "unittest", "discover", "-s", "tests"])
console.log("arena: package looks good")

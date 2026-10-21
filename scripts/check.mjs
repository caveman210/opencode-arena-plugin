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

// Every file the package needs at runtime has to be in the tarball.
const RUNTIME_FILES = [
  "SKILL.md",
  "arena-models.json",
  "bracket.py",
  "rubric.md",
  "strategies.json",
  "plugin/arena-router.ts",
  "opencode/agents/arena.md",
  "opencode/commands/arena.md",
  "bin/arena.js",
  "scripts/install.mjs",
  "scripts/check.mjs",
  "README.md",
  "LICENSE",
]
for (const file of RUNTIME_FILES) {
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

// The published name, and the files that go with it.
const name = manifest.name || ""
if (!/^[a-z0-9][a-z0-9._-]*$/.test(name) || name.startsWith(".") || name.includes(" ")) {
  console.error(`arena: ${JSON.stringify(name)} is not a usable npm name`)
  process.exit(1)
}
if (name.startsWith("@")) {
  console.error("arena: the package is unscoped on purpose, it fits any registry")
  process.exit(1)
}
const readme = readFileSync(join(pkg, "README.md"), "utf8")
if (!readme.includes(`"${name}"`)) {
  console.error(`arena: the README does not mention the package name ${name}`)
  process.exit(1)
}
const files = manifest.files || []
for (const file of RUNTIME_FILES) {
  if (!files.includes(file)) {
    console.error(`arena: package.json files is missing ${file}, it would not ship`)
    process.exit(1)
  }
}
for (const file of files) {
  if (!existsSync(join(pkg, file))) {
    console.error(`arena: package.json files lists ${file}, which does not exist`)
    process.exit(1)
  }
}
if (!manifest.repository || !manifest.repository.url) {
  console.error("arena: package.json needs a repository, npm shows it on the package page")
  process.exit(1)
}
if (!manifest.homepage || !manifest.bugs) {
  console.error("arena: package.json needs homepage and bugs")
  process.exit(1)
}
if (manifest.publishConfig?.access !== "public") {
  console.error("arena: package.json needs publishConfig.access public")
  process.exit(1)
}
if (manifest.os) {
  console.error("arena: package.json must not restrict os, that blocks installs on other platforms")
  process.exit(1)
}

run("python tests", "python3", ["-m", "unittest", "discover", "-s", "tests"])
run("node tests", "node", ["--test", "tests/*.test.mjs"])
console.log("arena: package looks good")

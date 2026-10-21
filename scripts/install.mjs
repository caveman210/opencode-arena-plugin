#!/usr/bin/env node
// Wires the arena package into an opencode install.
//
//   node scripts/install.mjs [--config-dir DIR] [--copy] [--dry-run] [--register]
//
// The package is the single source of truth. This script only puts links to
// it where opencode looks for things, so nothing is ever copied around:
//
//   <config>/skills/arena            -> <package>
//   <config>/plugins/arena-router.ts -> <package>/plugin/arena-router.ts
//   <config>/commands/arena.md       -> <package>/opencode/commands/arena.md
//
// The roster is the one file that is copied instead of linked:
// <config>/arena-models.json holds the live model list and has to stay
// writable, the packaged one is only the default it starts from.
//
// The base worker agent is registered by the plugin from the package itself,
// so there is no agent file to install.
import {
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readlinkSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { homedir } from "node:os"

const here = dirname(fileURLToPath(import.meta.url))
const pkg = dirname(here)

const args = process.argv.slice(2)
const flag = (name) => args.includes(name)
const value = (name) => {
  const at = args.indexOf(name)
  return at >= 0 && args[at + 1] ? args[at + 1] : null
}

const dryRun = flag("--dry-run")
const copyInstead = flag("--copy")
const register = flag("--register")
const configDir = resolve(
  value("--config-dir") || process.env.OPENCODE_CONFIG_DIR || join(homedir(), ".config", "opencode"),
)

const log = (line) => console.log(`arena: ${line}`)

function isSymlink(path) {
  try {
    return lstatSync(path).isSymbolicLink()
  } catch {
    return false
  }
}

function pointsAt(path, target) {
  try {
    return isSymlink(path) && resolve(dirname(path), readlinkSync(path)) === resolve(target)
  } catch {
    return false
  }
}

function backup(path) {
  if (!existsSync(path) && !isSymlink(path)) return
  const stamp = new Date().toISOString().replace(/[:.]/g, "-")
  const moved = `${path}.bak-${stamp}`
  try {
    renameSync(path, moved)
    log(`kept the old ${path} as ${moved}`)
  } catch {
    try {
      rmSync(path, { recursive: true, force: true })
    } catch {}
  }
}

function link(target, path, kind = "file") {
  if (dryRun) {
    log(`would link ${path} -> ${target}`)
    return
  }
  mkdirSync(dirname(path), { recursive: true })
  if (isSymlink(path)) {
    if (pointsAt(path, target)) {
      log(`${path} is already linked`)
      return
    }
    rmSync(path, { force: true })
  } else if (existsSync(path)) {
    backup(path)
  }
  try {
    symlinkSync(target, path, kind)
    log(`linked ${path} -> ${target}`)
  } catch {
    try {
      copyFileSync(target, path)
      log(`copied ${target} -> ${path}`)
    } catch (err) {
      log(`could not install ${path}: ${err.message}`)
    }
  }
}

/** The live roster: a real file, seeded from the packaged default. */
function roster(target, source) {
  if (dryRun) {
    log(`would seed ${target} from ${source}`)
    return
  }
  if (existsSync(target) && !isSymlink(target)) {
    log(`${target} is already there, left alone`)
    return
  }
  mkdirSync(dirname(target), { recursive: true })
  if (isSymlink(target)) rmSync(target, { force: true })
  copyFileSync(source, target)
  log(`seeded ${target} from ${source}`)
}

/** Add this package to the plugin list of the opencode config. */
function registerPlugin(file) {
  const candidates = ["opencode.jsonc", "opencode.json", "opencode.json5"]
  const configFile = candidates
    .map((name) => join(configDir, name))
    .find((path) => existsSync(path))
  if (!configFile) {
    log(`no opencode config in ${configDir}, add this by hand:`)
    log(`  "plugin": ["${file}"]`)
    return
  }
  let text = readFileSync(configFile, "utf8")
  if (text.includes(file)) {
    log(`${configFile} already lists ${file}`)
    return
  }
  const at = text.indexOf('"plugin"')
  if (at >= 0) {
    const open = text.indexOf("[", at)
    const close = text.indexOf("]", open)
    if (open > 0 && close > open) {
      const inside = text.slice(open + 1, close).trim()
      const entry = inside ? `${inside.replace(/,\s*$/, "")},\n    "${file}"\n  ` : `\n    "${file}"\n  `
      text = text.slice(0, open + 1) + entry + text.slice(close)
      writeFileSync(configFile, text, "utf8")
      log(`added ${file} to the plugin list in ${configFile}`)
      return
    }
  }
  const brace = text.indexOf("{")
  if (brace < 0) {
    log(`could not patch ${configFile}, add this by hand:`)
    log(`  "plugin": ["${file}"]`)
    return
  }
  text = `${text.slice(0, brace + 1)}\n  "plugin": ["${file}"],${text.slice(brace + 1)}`
  writeFileSync(configFile, text, "utf8")
  log(`added a plugin list with ${file} to ${configFile}`)
}

if (process.env.ARENA_SKIP_INSTALL) {
  log("ARENA_SKIP_INSTALL is set, nothing was installed")
  process.exit(0)
}

log(`package: ${pkg}`)
log(`config:  ${configDir}`)

link(pkg, join(configDir, "skills", "arena"), "dir")

const pluginTarget = join(configDir, "plugins", "arena-router.ts")
if (copyInstead) {
  mkdirSync(dirname(pluginTarget), { recursive: true })
  copyFileSync(join(pkg, "plugin", "arena-router.ts"), pluginTarget)
  log(`copied ${pluginTarget}`)
} else {
  link(join(pkg, "plugin", "arena-router.ts"), pluginTarget)
}

link(join(pkg, "opencode", "commands", "arena.md"), join(configDir, "commands", "arena.md"))
roster(join(configDir, "arena-models.json"), join(pkg, "arena-models.json"))

// The plugin registers the base worker agent from the package, so an agent
// file in the config dir is only a leftover from an older install.
const staleAgent = join(configDir, "agents", "arena.md")
if (existsSync(staleAgent)) {
  log(`removing ${staleAgent}, the plugin provides that agent now`)
  backup(staleAgent)
}

if (register) registerPlugin(`file://${pkg}`)

log("done. Restart opencode, then run /arena <task>")
log("plugin routes:")
log(`  auto loaded:  ${pluginTarget}`)
log(`  this package: "plugin": ["file://${pkg}"]`)
log(`  a registry:   "plugin": ["arena-skill"]`)
log("next: arena doctor, and arena roster --refresh to rebuild the model roster")

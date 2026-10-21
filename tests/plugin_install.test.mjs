// The plugin wires itself into an opencode install, because opencode installs
// npm plugins with the install scripts turned off. These tests run that wiring
// against a throwaway config dir.
import { test } from "node:test"
import assert from "node:assert/strict"
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readlinkSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const pkg = dirname(here)

let counter = 0

/** A fresh config dir, so every test starts from nothing. */
function configDir() {
  const dir = mkdtempSync(join(tmpdir(), "arena-config-"))
  return dir
}

/**
 * Load a fresh copy of the plugin and let it install itself.
 *
 * The query string gives every test its own module, so the "already
 * installed" flag never carries over from one test to the next.
 */
async function install(config, options = {}) {
  process.env.OPENCODE_CONFIG_DIR = config
  process.env.ARENA_NO_LINK = options.noLink ? "1" : ""
  const mod = await import(`../plugin/arena-router.ts?fresh=${counter++}`)
  mod.ensureInstalled()
  return mod
}

function isLink(path) {
  try {
    return lstatSync(path).isSymbolicLink()
  } catch {
    return false
  }
}

test("a fresh config gets the skill, the command and the roster", async () => {
  const config = configDir()
  await install(config)

  const skill = join(config, "skills", "arena")
  assert.ok(existsSync(skill), "the skill folder is there")
  assert.ok(isLink(skill), "the skill folder is a link")
  assert.equal(resolve(readlinkSync(skill)), resolve(pkg))

  const command = join(config, "commands", "arena.md")
  assert.ok(existsSync(command), "the command is there")
  assert.ok(isLink(command), "the command is a link")

  const roster = join(config, "arena-models.json")
  assert.ok(existsSync(roster), "the roster is there")
  assert.ok(!isLink(roster), "the roster is a real file, it has to stay writable")

  rmSync(config, { recursive: true, force: true })
})

test("ARENA_NO_LINK leaves the config alone", async () => {
  const config = configDir()
  await install(config, { noLink: true })

  assert.ok(!existsSync(join(config, "skills")), "nothing was linked")
  assert.ok(!existsSync(join(config, "commands")), "nothing was linked")
  assert.ok(!existsSync(join(config, "arena-models.json")), "nothing was seeded")

  rmSync(config, { recursive: true, force: true })
})

test("a link that points nowhere is repaired", async () => {
  const config = configDir()
  const skill = join(config, "skills", "arena")
  mkdirSync(join(config, "skills"), { recursive: true })
  symlinkSync(join(config, "gone"), skill, "dir")

  await install(config)

  assert.ok(existsSync(skill), "the skill folder is back")
  assert.equal(resolve(readlinkSync(skill)), resolve(pkg))

  rmSync(config, { recursive: true, force: true })
})

test("a link to an older copy of the package points at the running copy", async () => {
  const config = configDir()
  const skill = join(config, "skills", "arena")
  const old = join(config, "cache", "opencode-arena-plugin@0.0.1")
  mkdirSync(old, { recursive: true })
  mkdirSync(join(config, "skills"), { recursive: true })
  symlinkSync(old, skill, "dir")

  await install(config)

  assert.equal(resolve(readlinkSync(skill)), resolve(pkg))

  rmSync(config, { recursive: true, force: true })
})

test("a link to a checkout of the user is left alone", async () => {
  const config = configDir()
  const skill = join(config, "skills", "arena")
  const checkout = join(config, "my-arena-checkout")
  mkdirSync(checkout, { recursive: true })
  mkdirSync(join(config, "skills"), { recursive: true })
  symlinkSync(checkout, skill, "dir")

  await install(config)

  assert.equal(resolve(readlinkSync(skill)), resolve(checkout))

  rmSync(config, { recursive: true, force: true })
})

test("a second copy of the plugin is moved out of the way", async () => {
  const config = configDir()
  writeFileSync(
    join(config, "opencode.json"),
    JSON.stringify({ plugin: ["opencode-arena-plugin"] }, null, 2),
    "utf8",
  )
  mkdirSync(join(config, "plugins"), { recursive: true })
  const local = join(config, "plugins", "arena-router.ts")
  symlinkSync(join(pkg, "plugin", "arena-router.ts"), local)

  await install(config)

  assert.ok(!existsSync(local), "the duplicate is gone")
  const left = readdirSync(join(config, "plugins"))
  assert.equal(left.length, 1, "the duplicate was kept, not deleted")
  assert.match(left[0], /^arena-router\.ts\.bak-/, left[0])

  rmSync(config, { recursive: true, force: true })
})

test("a plugin file stays when the config does not list the package", async () => {
  const config = configDir()
  writeFileSync(join(config, "opencode.json"), JSON.stringify({ plugin: [] }, null, 2), "utf8")
  mkdirSync(join(config, "plugins"), { recursive: true })
  const local = join(config, "plugins", "arena-router.ts")
  symlinkSync(join(pkg, "plugin", "arena-router.ts"), local)

  await install(config)

  assert.ok(existsSync(local), "the only copy of the plugin is untouched")

  rmSync(config, { recursive: true, force: true })
})

test("a plugin file the user wrote is never touched", async () => {
  const config = configDir()
  writeFileSync(
    join(config, "opencode.json"),
    JSON.stringify({ plugin: ["opencode-arena-plugin"] }, null, 2),
    "utf8",
  )
  mkdirSync(join(config, "plugins"), { recursive: true })
  const local = join(config, "plugins", "arena-router.ts")
  writeFileSync(local, "// my own plugin\n", "utf8")

  await install(config)

  assert.ok(existsSync(local), "the file is still there")

  rmSync(config, { recursive: true, force: true })
})

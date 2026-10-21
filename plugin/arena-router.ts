import { tool, type Plugin, type PluginModule } from "@opencode-ai/plugin"
import {
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readlinkSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

// The exact zod instance the opencode tool helper uses, so a tool schema and
// the runtime that reads it can never be two different zods.
const z = tool.schema

type Row = {
  model: string
  agent: string
  name?: string
  input: number
  output: number
  context: number
  toolcall?: boolean
  status?: string
  free?: boolean
  goGated?: boolean
}

// Providers behind a Go plan answer "subscribe to Go for further access".
// Their models never get an arena job.
const GO_GATED_PROVIDER = /(^|[-_])go$/i
const MAX_ROSTER = 16
const MAX_FALLBACK_TRIES = 3

const FAILURE_PATTERNS: Array<{ kind: string; re: RegExp }> = [
  { kind: "go_gated", re: /subscribe to go|go subscription|requires (a )?(paid )?go plan|go plan/i },
  { kind: "timeout", re: /timed?\s?out|timeout|etimedout|esockettimedout|deadline exceeded|abort(ed)?/i },
  { kind: "rate_limit", re: /rate limit|too many requests|\b429\b|quota|overloaded/i },
  { kind: "server_error", re: /\b50[0234]\b|service unavailable|bad gateway|internal server error/i },
  { kind: "not_found", re: /model not found|unknown model|no such model|does not exist/i },
]

let cachedDirectory = ""
/** What the config hook actually registered as visible agents in this session. */
let registeredRoster: Row[] = []
/** What the provider list reports right now. */
let liveRoster: Row[] = []
const failedModels = new Set<string>()

type PluginSettings = {
  maxRoster: number
  registerAgents: boolean
  rosterPath: string | null
}

let settings: PluginSettings = { maxRoster: MAX_ROSTER, registerAgents: true, rosterPath: null }

/**
 * Plugin options, from the config entry:
 *
 *   "plugin": [["opencode-arena-plugin", { "maxRoster": 8, "registerAgents": true }]]
 */
function pluginSettings(options?: Record<string, unknown>): PluginSettings {
  const raw = (options || {}) as Record<string, any>
  const max = Number(raw.maxRoster)
  return {
    maxRoster: Number.isFinite(max) && max > 0 ? Math.min(Math.floor(max), MAX_ROSTER) : MAX_ROSTER,
    registerAgents: raw.registerAgents !== false,
    rosterPath: typeof raw.rosterPath === "string" && raw.rosterPath.trim() ? raw.rosterPath.trim() : null,
  }
}

function isGoGated(provider: string) {
  return GO_GATED_PROVIDER.test(String(provider || ""))
}

function classifyFailure(text: string): string | null {
  const raw = String(text || "")
  if (!raw.trim()) return null
  for (const row of FAILURE_PATTERNS) {
    if (row.re.test(raw)) return row.kind
  }
  return null
}

/** Deterministic visible-agent name for a model id. bracket.py derives it the same way. */
export function agentNameForModel(model: string) {
  const raw = String(model || "")
  const id = raw.includes("/") ? raw.slice(raw.indexOf("/") + 1) : raw
  const slug = id
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
  return slug ? `arena-${slug}` : "arena-model"
}

function configDir(): string {
  const env = process.env.OPENCODE_CONFIG_DIR
  if (env) return env
  return join(process.env.HOME || "", ".config", "opencode")
}

/** The package this plugin ships in, found from its own location. */
function skillDir(): string {
  return dirname(dirname(fileURLToPath(import.meta.url)))
}

/** The live roster: a writable file in the opencode config dir. */
function liveRosterPath(): string {
  return join(configDir(), "arena-models.json")
}

/** The roster shipped inside the package: the fallback until a live one exists. */
function defaultRosterPath(): string {
  return join(skillDir(), "arena-models.json")
}

/** The roster to read: the live one when it exists, the packaged one otherwise. */
function rosterPath(): string {
  if (settings.rosterPath) return settings.rosterPath
  const live = liveRosterPath()
  if (existsSync(live)) return live
  return defaultRosterPath()
}

function agentPath(): string {
  return join(skillDir(), "opencode", "agents", "arena.md")
}

function isSymlink(path: string) {
  try {
    return lstatSync(path).isSymbolicLink()
  } catch {
    return false
  }
}

/** Where a link points, or null when it is not a link. */
function linkTarget(path: string): string | null {
  try {
    return readlinkSync(path)
  } catch {
    return null
  }
}

/** The real path of a file, so two links to the same file compare equal. */
function realPath(path: string): string {
  try {
    return realpathSync(path)
  } catch {
    return resolve(path)
  }
}

/** The name this package is published under. */
function packageName(): string {
  try {
    const manifest = JSON.parse(readFileSync(join(skillDir(), "package.json"), "utf8"))
    if (typeof manifest?.name === "string" && manifest.name.trim()) return manifest.name.trim()
  } catch {}
  return "opencode-arena-plugin"
}

/** The config files opencode reads, in the order it reads them. */
function configFiles(dir: string): string[] {
  return ["opencode.json", "opencode.jsonc", "opencode.json5"].map((name) => join(dir, name))
}

/** True when the opencode config already loads this package from the registry. */
function configListsPackage(dir: string): boolean {
  const name = packageName()
  for (const file of configFiles(dir)) {
    try {
      if (readFileSync(file, "utf8").includes(name)) return true
    } catch {}
  }
  return false
}

/**
 * Put the rest of the package where opencode looks for it.
 *
 * opencode installs npm plugins with install scripts turned off, so the
 * postinstall hook of this package never runs on that path. The plugin does
 * the wiring itself: the skill folder, the command and a writable roster.
 * Nothing is copied that does not have to be, and nothing the user owns is
 * ever removed.
 */
let installed = false

/**
 * Link a path to a target, and repair the link when it went stale.
 *
 * A link that points at another copy of this package is repointed at the copy
 * that is running, so an upgrade does not leave the skill on an old version.
 * A link that points at something else that is still there is left alone: it
 * can be a checkout of the user. A link that points nowhere is repaired.
 */
function ensureLink(path: string, target: string, kind: "dir" | "file") {
  try {
    if (isSymlink(path)) {
      const from = linkTarget(path)
      if (from && resolve(dirname(path), from) === resolve(target)) {
        debug(`${path} is already linked`)
        return
      }
      if (from && existsSync(from) && !from.includes(packageName())) {
        debug(`${path} points at ${from}, which is still there, left alone`)
        return
      }
      rmSync(path, { force: true })
      debug(`${path} pointed at ${from || "nothing"}, repaired it`)
    } else if (existsSync(path)) {
      debug(`${path} exists and is not a link, left alone`)
      return
    }
    mkdirSync(dirname(path), { recursive: true })
    symlinkSync(target, path, kind)
    debug(`linked ${path} -> ${target}`)
  } catch (err: any) {
    debug(`could not link ${path}: ${String(err?.message || err)}`)
  }
}

/** The live roster: a writable file in the opencode config dir. */
function ensureRoster() {
  try {
    const roster = liveRosterPath()
    if (!existsSync(roster) || isSymlink(roster)) {
      const source = defaultRosterPath()
      if (existsSync(source)) {
        rmSync(roster, { force: true })
        copyFileSync(source, roster)
        debug(`seeded ${roster} from ${source}`)
      }
    }
  } catch (err: any) {
    debug(`could not seed the roster: ${String(err?.message || err)}`)
  }
}

/**
 * Drop a second copy of this plugin.
 *
 * opencode loads a plugin from the config list and from the files in
 * <config>/plugins. When the package is in the config list and a link to
 * this same plugin is still in that folder, both load and the tools are
 * registered twice. The link is moved out of the way, never deleted.
 */
function removeDuplicatePlugin(dir: string) {
  try {
    const local = join(dir, "plugins", "arena-router.ts")
    if (!existsSync(local) && !isSymlink(local)) return
    if (!isSymlink(local)) {
      debug(`${local} is a real file, left alone`)
      return
    }
    const from = linkTarget(local) || ""
    const mine = from.includes(packageName()) || realPath(local) === realPath(fileURLToPath(import.meta.url))
    if (!mine) {
      debug(`${local} points at something else, left alone`)
      return
    }
    if (!configListsPackage(dir)) {
      debug(`${local} is the only copy of the plugin, left alone`)
      return
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-")
    try {
      renameSync(local, `${local}.bak-${stamp}`)
      debug(`moved the duplicate ${local} out of the way`)
    } catch {
      rmSync(local, { force: true })
      debug(`removed the duplicate ${local}`)
    }
  } catch (err: any) {
    debug(`could not clean up a duplicate plugin: ${String(err?.message || err)}`)
  }
}

export function ensureInstalled() {
  if (installed) return
  installed = true
  if (process.env.ARENA_NO_LINK) {
    debug("ARENA_NO_LINK is set, no links were made")
    return
  }
  const dir = configDir()
  ensureLink(join(dir, "skills", "arena"), skillDir(), "dir")
  ensureLink(join(dir, "commands", "arena.md"), join(skillDir(), "opencode", "commands", "arena.md"), "file")
  ensureRoster()
  removeDuplicatePlugin(dir)
}

/** The worker prompt, straight from the package. */
function workerPrompt(): string {
  try {
    const text = readFileSync(agentPath(), "utf8")
    const parts = text.split(/^---$/m)
    const body = parts.length >= 3 ? parts.slice(2).join("---").trim() : text.trim()
    if (body) return body
  } catch {}
  return "You are one worker in an arena tournament. Read the brief file named in your task and follow it exactly. Read only the files the brief names. Write only the output files the brief lists, and only inside the arena directory. Never edit the user's project. Do not ask questions."
}

/** The base agent, packaged as an opencode agent file. */
function baseAgent() {
  try {
    const text = readFileSync(agentPath(), "utf8")
    const parts = text.split(/^---$/m)
    if (parts.length < 3) return null
    const front = parts[1]
    const body = parts.slice(2).join("---").trim()
    const match = front.match(/^description:\s*(.+)$/m)
    return {
      description: match ? match[1].trim() : "Runs one arena brief as a visible subagent session.",
      prompt: body,
    }
  } catch {
    return null
  }
}

const ARENA_PERMISSION = {
  edit: { "*": "deny", ".arena/**": "allow" },
  read: "allow",
  glob: "allow",
  grep: "allow",
  list: "allow",
  bash: "allow",
  task: "deny",
  webfetch: "ask",
  websearch: "ask",
  external_directory: {
    [`${skillDir()}/**`]: "allow",
    [`${configDir()}/skills/arena/**`]: "allow",
  },
} as any

function parseModel(spec: string) {
  const raw = String(spec || "").trim().split("#", 1)[0]
  const cut = raw.indexOf("/")
  if (cut < 1 || cut === raw.length - 1) {
    throw new Error(`model must be provider/model, got ${JSON.stringify(spec)}`)
  }
  return { providerID: raw.slice(0, cut), modelID: raw.slice(cut + 1) }
}

function unwrap(result: any, label: string) {
  if (result?.error) {
    throw new Error(`${label}: ${JSON.stringify(result.error)}`)
  }
  return result?.data
}

function textFromParts(parts: any[]) {
  return (parts || [])
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
}

function debug(message: string) {
  if (!process.env.ARENA_DEBUG) return
  try {
    const path = join(process.env.TMPDIR || "/tmp", "opencode", "arena-router.log")
    if (!existsSync(dirname(path))) mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, `${new Date().toISOString()} ${message}\n`, { encoding: "utf8", flag: "a" })
  } catch {}
}

/** Every model opencode can actually reach right now, priced and capability flagged. */
async function liveModels(client: any, directory: string): Promise<Row[]> {
  const data: any = unwrap(await client.config.providers({ query: { directory } }), "could not list providers")
  const rows: Row[] = []
  for (const provider of data?.providers || []) {
    const goGated = isGoGated(provider?.id)
    for (const [key, value] of Object.entries(provider?.models || {})) {
      const model: any = value || {}
      const cost = model?.cost || {}
      const input = Number(cost.input || 0)
      const output = Number(cost.output || 0)
      const id = `${provider.id}/${model.id || key}`
      rows.push({
        model: id,
        agent: agentNameForModel(id),
        name: model.name || key,
        input,
        output,
        context: Number(model?.limit?.context || 0),
        toolcall: Boolean(model?.capabilities?.toolcall),
        status: model.status || "active",
        free: input === 0 && output === 0,
        goGated,
      })
    }
  }
  return rows
}

/** The roster opencode should expose: free, tool capable, live, never Go gated. */
function pickRoster(rows: Row[]): Row[] {
  const usable = rows.filter(
    (row) =>
      row.toolcall &&
      row.free &&
      !row.goGated &&
      row.status !== "deprecated" &&
      !failedModels.has(row.model),
  )
  usable.sort((a, b) => b.context - a.context || a.model.localeCompare(b.model))
  return usable.slice(0, settings.maxRoster)
}

function writeRosterFile(rows: Row[]) {
  const payload = rows.map((row) => ({
    model: row.model,
    agent: row.agent,
    name: row.name,
    context: row.context,
    free: true,
  }))
  const body = JSON.stringify(payload, null, 2) + "\n"
  // The live roster lives in the config dir, where it can be written. The
  // packaged one is the fallback for a read only install.
  for (const path of [liveRosterPath(), defaultRosterPath()]) {
    try {
      const dir = dirname(path)
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
      writeFileSync(path, body, "utf8")
      return path
    } catch {}
  }
  return null
}

function readRosterFile(): Row[] {
  try {
    const data = JSON.parse(readFileSync(rosterPath(), "utf8"))
    const list = Array.isArray(data) ? data : data?.models || []
    const rows: Row[] = []
    for (const item of list || []) {
      if (!item || typeof item !== "object") continue
      const model = String(item.model || "").trim()
      if (!model.includes("/")) continue
      rows.push({
        model,
        agent: String(item.agent || agentNameForModel(model)),
        name: item.name,
        input: Number(item.input || 0),
        output: Number(item.output || 0),
        context: Number(item.context || 0),
        toolcall: true,
        status: item.status || "active",
        free: item.free !== false,
        goGated: isGoGated(model.split("/", 1)[0]),
      })
    }
    return rows
  } catch {
    return []
  }
}

/**
 * The roster file is the source for the visible agents. It is written by
 * bracket.py (roster --refresh) and by the arena_models tool, both of which
 * read the live provider list. The config hook only reads it: asking the
 * server for its provider list from inside the config hook waits on itself.
 */
function rosterForAgents(): Row[] {
  const rows = pickRoster(readRosterFile())
  if (rows.length) return rows
  return pickRoster(liveRoster)
}

function rosterMap(rows: Row[]) {
  return new Map(rows.map((row) => [row.model, row.agent]))
}

export const ArenaRouter: Plugin = async ({ client, directory }, options) => {
  cachedDirectory = directory || cachedDirectory
  settings = pluginSettings(options)
  ensureInstalled()
  return {
    config: async (config) => {
      config.agent = config.agent || {}
      // The base worker agent comes from the package, so the permissions point
      // at the installed package and never at a stale path.
      const base = baseAgent()
      if (base?.prompt) {
        config.agent.arena = {
          description: base.description,
          mode: "subagent",
          temperature: 0.2,
          prompt: base.prompt,
          permission: ARENA_PERMISSION,
        } as any
        debug("registered the base arena agent")
      }
      if (!settings.registerAgents) {
        debug("agent registration is off, only the base arena agent is registered")
        return
      }
      const rows = rosterForAgents()
      if (!rows.length) {
        debug("no roster rows: no visible arena agents registered")
        return
      }
      registeredRoster = rows
      const prompt = workerPrompt()
      for (const row of rows) {
        if (!row.agent) continue
        if (config.agent[row.agent]) continue
        config.agent[row.agent] = {
          description: `Visible arena worker pinned to ${row.model}`,
          mode: "subagent",
          model: row.model,
          temperature: 0.2,
          prompt,
          permission: ARENA_PERMISSION,
        } as any
      }
      debug(`registered ${rows.length} visible arena agents`)
    },
    tool: {
      arena_models: tool({
        description:
          "List the models opencode can reach right now, with prices, context limits and the visible arena agent for each. Use once to choose a diverse arena roster.",
        args: {
          maxCost: z.number().nonnegative().optional().describe("Maximum input or output price per million tokens"),
          minContext: z.number().int().positive().optional().describe("Minimum context window"),
          toolcall: z.boolean().optional().default(true).describe("Only models that can call tools"),
          freeOnly: z.boolean().optional().default(false).describe("Only models that cost nothing"),
          includeGoGated: z.boolean().optional().default(false).describe("Include models that need a Go subscription"),
          limit: z.number().int().min(1).max(500).optional().default(200),
        },
        async execute(args, context) {
          let rows: Row[] = []
          try {
            rows = await liveModels(client, context.directory || cachedDirectory)
            liveRoster = rows
            const fresh = pickRoster(rows)
            if (fresh.length) writeRosterFile(fresh)
          } catch (err: any) {
            debug(`live model list failed: ${String(err?.message || err)}`)
            rows = liveRoster.length ? liveRoster : readRosterFile()
          }
          const registered = rosterMap(registeredRoster)
          let models = rows
          if (args.toolcall) models = models.filter((row) => row.toolcall)
          if (args.freeOnly) models = models.filter((row) => row.free)
          if (!args.includeGoGated) models = models.filter((row) => !row.goGated)
          if (args.minContext) models = models.filter((row) => row.context >= args.minContext!)
          if (args.maxCost !== undefined) {
            models = models.filter((row) => Math.max(row.input || 0, row.output || 0) <= args.maxCost!)
          }
          models = models.filter((row) => !failedModels.has(row.model))
          models.sort(
            (a, b) =>
              Number(a.free) - Number(b.free) ||
              (a.input || 0) + (a.output || 0) - ((b.input || 0) + (b.output || 0)) ||
              b.context - a.context ||
              a.model.localeCompare(b.model),
          )
          context.metadata({
            title: `arena models (${models.length})`,
            metadata: { models: models.length },
          })
          return JSON.stringify(
            {
              failed: Array.from(failedModels),
              registeredAgents: registeredRoster.map((row) => row.agent),
              models: models.slice(0, args.limit).map((row) => ({
                id: row.model,
                name: row.name,
                provider: row.model.split("/", 1)[0],
                model: row.model.split("/").slice(1).join("/"),
                input: row.input,
                output: row.output,
                context: row.context,
                toolcall: row.toolcall,
                status: row.status,
                free: row.free,
                goGated: row.goGated,
                agent: registered.get(row.model) || null,
                native: Boolean(registered.get(row.model)),
              })),
            },
            null,
            1,
          )
        },
      }),

      arena_task: tool({
        description:
          "Run one arena brief in a child session on a chosen provider/model. Returns the reply, session id, cost and tokens. Reuse sessionID to continue the same child. A timeout, a Go paywall or a server error moves the job to a free model that works.",
        args: {
          description: z.string().describe("Short job name, for example arena r1-m1.judge"),
          prompt: z.string().describe("The whole brief, normally a request to read one prompt file"),
          model: z.string().describe("provider/model, optionally provider/model#variant"),
          agent: z.string().optional().default("arena"),
          sessionID: z.string().optional().describe("Resume this child session instead of creating one"),
          title: z.string().optional(),
          noFallback: z.boolean().optional().default(false).describe("Do not move the job to another model on failure"),
        },
        async execute(args, context) {
          const run = async (spec: string, sessionID?: string) => {
            const model = parseModel(spec)
            let id = sessionID
            if (!id) {
              const created: any = unwrap(
                await client.session.create({
                  body: { parentID: context.sessionID, title: args.title || `arena ${args.description}` },
                  query: { directory: context.directory },
                }),
                "could not create child session",
              )
              id = created?.id
              if (!id) throw new Error("could not create child session: no id returned")
            }
            const result: any = await client.session.prompt({
              path: { id },
              body: {
                agent: args.agent || "arena",
                model,
                parts: [{ type: "text", text: args.prompt }],
              },
              query: { directory: context.directory },
            })
            const data: any = unwrap(result, `arena_task failed on ${spec}`)
            const info: any = data?.info || {}
            if (info?.error) {
              throw new Error(`arena_task failed on ${spec} (session ${id}): ${JSON.stringify(info.error)}`)
            }
            return {
              sessionID: id,
              providerID: info?.providerID || model.providerID,
              modelID: info?.modelID || model.modelID,
              text: textFromParts(data?.parts || []),
              cost: info?.cost ?? 0,
              tokens: info?.tokens || null,
              finish: info?.finish || null,
            }
          }

          try {
            const out = await run(args.model, args.sessionID)
            context.metadata({
              title: args.description,
              metadata: { sessionID: out.sessionID, model: args.model, cost: out.cost, tokens: out.tokens },
            })
            return JSON.stringify(out, null, 1)
          } catch (err: any) {
            const message = String(err?.message || err)
            const kind = classifyFailure(message)
            if (args.noFallback || !kind) throw err
            failedModels.add(args.model)
            let rows: Row[] = []
            try {
              rows = await liveModels(client, context.directory || cachedDirectory)
              liveRoster = rows
            } catch {
              rows = liveRoster
            }
            const spares = pickRoster(rows).filter((row) => row.model !== args.model)
            if (!spares.length) {
              throw new Error(`${message}\nno free model left to move this job to`)
            }
            let lastError: any = null
            for (const spare of spares.slice(0, MAX_FALLBACK_TRIES)) {
              try {
                const out = await run(spare.model)
                context.metadata({
                  title: `${args.description} (moved to ${spare.model})`,
                  metadata: {
                    sessionID: out.sessionID,
                    model: spare.model,
                    movedFrom: args.model,
                    reason: kind,
                    cost: out.cost,
                    tokens: out.tokens,
                  },
                })
                return JSON.stringify({ ...out, movedFrom: args.model, movedBecause: kind }, null, 1)
              } catch (retryErr: any) {
                lastError = retryErr
                failedModels.add(spare.model)
                const retryKind = classifyFailure(String(retryErr?.message || retryErr))
                if (!retryKind) break
              }
            }
            throw new Error(`${message}\nmove to a free model failed too: ${String(lastError?.message || lastError)}`)
          }
        },
      }),

      arena_substitute: tool({
        description:
          "Find similar unused models to replace a failed one. Prefers free models that need no subscription.",
        args: {
          failed: z.string().describe("The model that failed (provider/model)"),
          used: z.array(z.string()).describe("Models already used or reserved in this run"),
          maxCost: z.number().nonnegative().optional().describe("Maximum input or output price per million tokens"),
          minContext: z.number().int().positive().optional().describe("Minimum context window"),
          freeOnly: z.boolean().optional().default(true).describe("Only models that cost nothing"),
          limit: z.number().int().min(1).max(50).optional().default(10),
        },
        async execute(args, context) {
          let rows: Row[] = []
          try {
            rows = await liveModels(client, context.directory || cachedDirectory)
            liveRoster = rows
          } catch {
            rows = liveRoster.length ? liveRoster : readRosterFile()
          }
          const registered = rosterMap(registeredRoster)
          const failedParts = parseModel(args.failed)
          const failedRow = rows.find((row) => row.model === args.failed)
          const candidates: any[] = []
          for (const row of rows) {
            if (!row.toolcall) continue
            if (row.goGated) continue
            if (row.model === args.failed) continue
            if (args.used.includes(row.model)) continue
            if (failedModels.has(row.model)) continue
            if (args.freeOnly && !row.free) continue
            if (args.minContext && row.context < args.minContext) continue
            if (args.maxCost !== undefined && Math.max(row.input || 0, row.output || 0) > args.maxCost) continue
            const agent = registered.get(row.model) || null
            if (!agent) continue
            let score = 0
            if (failedParts && row.model.split("/", 1)[0] === failedParts.providerID) score -= 100
            if (failedRow) {
              score += Math.abs((row.input || 0) - (failedRow.input || 0)) * 0.1
              score += Math.abs((row.output || 0) - (failedRow.output || 0)) * 0.1
              score += Math.abs((row.context || 0) - (failedRow.context || 0)) * 0.01
            }
            if (row.free) score -= 10
            candidates.push({
              model: row.model,
              agent,
              input: row.input,
              output: row.output,
              context: row.context,
              free: row.free,
              score,
            })
          }
          candidates.sort((a, b) => a.score - b.score)
          context.metadata({
            title: `arena substitute for ${args.failed}`,
            metadata: { candidates: candidates.length },
          })
          return JSON.stringify(
            {
              failed: args.failed,
              failedInSession: failedModels.has(args.failed),
              candidates: candidates.slice(0, args.limit),
            },
            null,
            1,
          )
        },
      }),
    },
  }
}

export default { id: "arena", server: ArenaRouter } satisfies PluginModule

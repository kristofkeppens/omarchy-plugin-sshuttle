const test = require("node:test")
const assert = require("node:assert")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

// Model.js starts with the QML-only `.pragma library` line, which is not valid
// JavaScript; blank it before evaluating, as node would otherwise refuse it.
const src = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8")
  .replace(/^\.pragma library/, "// .pragma library")
const sandbox = { module: { exports: {} } }
vm.runInNewContext(src, sandbox)
const raw = sandbox.module.exports
// Results cross a vm realm; round-trip through JSON so deepStrictEqual compares plain objects.
const Model = {}
for (const k of Object.keys(raw)) Model[k] = (...a) => { const r = raw[k](...a); return r && typeof r === "object" ? JSON.parse(JSON.stringify(r)) : r }

test("parseResult handles empty and invalid text", () => {
  assert.deepStrictEqual(Model.parseResult(""), { ok: false, error: "Invalid response from helper" })
  assert.deepStrictEqual(Model.parseResult("{nope"), { ok: false, error: "Invalid response from helper" })
  assert.deepStrictEqual(Model.parseResult("[1]"), { ok: false, error: "Invalid response from helper" })
  assert.deepStrictEqual(Model.parseResult(null), { ok: false, error: "Invalid response from helper" })
})

test("parseResult passes valid JSON through", () => {
  const r = Model.parseResult('{"ok":true,"pid":5}')
  assert.strictEqual(r.ok, true)
  assert.strictEqual(r.pid, 5)
  assert.strictEqual(Model.parseResult('{"pid":5}').ok, false)
})

test("stateKey", () => {
  assert.strictEqual(Model.stateKey({ state: "connected", sshuttle_installed: true }), "connected")
  assert.strictEqual(Model.stateKey({ state: "connecting", sshuttle_installed: true }), "connecting")
  assert.strictEqual(Model.stateKey({ state: "disconnected", sshuttle_installed: true }), "disconnected")
  assert.strictEqual(Model.stateKey({ state: "error", sshuttle_installed: true }), "error")
  assert.strictEqual(Model.stateKey({ state: "disconnected", sshuttle_installed: false }), "unavailable")
  assert.strictEqual(Model.stateKey({ state: "bogus" }), "disconnected")
  assert.strictEqual(Model.stateKey(null), "disconnected")
})

test("profileSummary", () => {
  assert.strictEqual(Model.profileSummary({ remote: "me@h:22", subnets: ["0/0"] }), "me@h:22 · all traffic")
  assert.strictEqual(Model.profileSummary({ remote: "me@h:22", subnets: ["10.0.0.0/8"] }), "me@h:22 · 1 subnet")
  assert.strictEqual(Model.profileSummary({ remote: "me@h:22", subnets: ["10.0.0.0/8", "172.16.0.0/12"] }), "me@h:22 · 2 subnets")
})

test("formToProfile splits subnets", () => {
  const p = Model.formToProfile({
    name: "w", remote: "h", subnetsText: "10.0.0.0/8, 192.168.0.0/16\n172.16.0.0/12",
    excludesText: " ", dns: true, method: "nft", disableIpv6: true, sshIdentity: "/k"
  })
  assert.deepStrictEqual(p.subnets, ["10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12"])
  assert.deepStrictEqual(p.excludes, [])
  assert.strictEqual(p.disable_ipv6, true)
  assert.strictEqual(p.ssh_identity, "/k")
  assert.strictEqual(p.dns, true)
})

test("profileToForm round trip", () => {
  const f = {
    name: "w", remote: "me@h:22", subnetsText: "10.0.0.0/8\n192.168.0.0/16",
    excludesText: "10.1.0.0/16", dns: true, method: "nat", disableIpv6: false, sshIdentity: "/k"
  }
  assert.deepStrictEqual(Model.profileToForm(Model.formToProfile(f)), f)
})

test("summaryParts", () => {
  assert.deepStrictEqual(Model.summaryParts({ remote: "me@h", subnets: ["0/0"] }), { remote: "me@h", subnetCount: 1, allTraffic: true })
  assert.deepStrictEqual(Model.summaryParts({ remote: "me@h", subnets: [] }), { remote: "me@h", subnetCount: 0, allTraffic: true })
  assert.deepStrictEqual(Model.summaryParts({ remote: "h", subnets: ["10.0.0.0/8", "0/0"] }), { remote: "h", subnetCount: 2, allTraffic: false })
  assert.deepStrictEqual(Model.summaryParts(null), { remote: "", subnetCount: 0, allTraffic: true })
})

test("sanitizeProfile keeps a maximal valid profile unchanged", () => {
  const p = {
    name: "n".repeat(40),
    remote: "u".repeat(60) + "@" + "h".repeat(230) + ":65535",
    subnets: Array.from({ length: 200 }, () => "s".repeat(100)),
    excludes: Array.from({ length: 200 }, () => "e".repeat(100)),
    dns: true, method: "tproxy", disable_ipv6: true,
    ssh_identity: "/" + "k".repeat(4000)
  }
  assert.deepStrictEqual(Model.sanitizeProfile(p), p)
})

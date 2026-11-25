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

test("sanitizeStatus keeps byte counters as non-negative integers or null", () => {
  const ok = Model.sanitizeStatus({ rx_bytes: 5.9, tx_bytes: 0 })
  assert.strictEqual(ok.rx_bytes, 5)
  assert.strictEqual(ok.tx_bytes, 0)
  for (const bad of [-1, NaN, "7", null, undefined]) {
    const s = Model.sanitizeStatus({ rx_bytes: bad, tx_bytes: bad })
    assert.strictEqual(s.rx_bytes, null)
    assert.strictEqual(s.tx_bytes, null)
  }
  const missing = Model.sanitizeStatus({})
  assert.strictEqual(missing.rx_bytes, null)
  assert.strictEqual(missing.tx_bytes, null)
})

const conn = (rx, tx, since) => ({ state: "connected", rx_bytes: rx, tx_bytes: tx, since: since })

test("appendSample adds a sample only for a connected status with counters", () => {
  assert.deepStrictEqual(Model.appendSample([], conn(1000, 200, 5), 10000), [{ t: 10000, rx: 1000, tx: 200, since: 5 }])
  assert.deepStrictEqual(Model.appendSample([], { state: "connecting", rx_bytes: 1, tx_bytes: 1, since: 5 }, 10000), [])
  assert.deepStrictEqual(Model.appendSample([], conn(null, 200, 5), 10000), [])
  assert.deepStrictEqual(Model.appendSample([], conn(1, null, 5), 10000), [])
  assert.deepStrictEqual(Model.appendSample([], { state: "disconnected", rx_bytes: 1, tx_bytes: 1, since: 5 }, 10000), [])
  assert.deepStrictEqual(Model.appendSample("junk", conn(1, 2, 5), 10000), [{ t: 10000, rx: 1, tx: 2, since: 5 }])
})

test("appendSample restarts history on counter reset or new connection", () => {
  const prev = [{ t: 9000, rx: 5000, tx: 500, since: 5 }]
  const only = (rx, tx, since) => [{ t: 10000, rx: rx, tx: tx, since: since }]
  assert.deepStrictEqual(Model.appendSample(prev, conn(4000, 600, 5), 10000), only(4000, 600, 5))
  assert.deepStrictEqual(Model.appendSample(prev, conn(6000, 400, 5), 10000), only(6000, 400, 5))
  assert.deepStrictEqual(Model.appendSample(prev, conn(6000, 600, 6), 10000), only(6000, 600, 6))
  assert.strictEqual(Model.appendSample(prev, conn(6000, 600, 5), 10000).length, 2)
})

test("appendSample trims by age and count without mutating its input", () => {
  const old = [{ t: 0, rx: 1, tx: 1, since: 5 }]
  const out = Model.appendSample(old, conn(2, 2, 5), 130000)
  assert.deepStrictEqual(out, [{ t: 130000, rx: 2, tx: 2, since: 5 }])
  assert.deepStrictEqual(old, [{ t: 0, rx: 1, tx: 1, since: 5 }])
  let s = []
  for (let i = 0; i < 300; i++) s = JSON.parse(JSON.stringify(raw.appendSample(s, conn(i, i, 5), i * 100)))
  assert.strictEqual(s.length, 200)
  assert.strictEqual(s[199].rx, 299)
  assert.strictEqual(s[0].rx, 100)
  const input = [{ t: 1, rx: 1, tx: 1, since: 5 }]
  const res = raw.appendSample(input, conn(2, 2, 5), 2)
  assert.notStrictEqual(res, input)
  assert.strictEqual(input.length, 1)
})

test("rates", () => {
  assert.deepStrictEqual(Model.rates([{ t: 0, rx: 0, tx: 0 }, { t: 2000, rx: 2000000, tx: 100000 }]), { rx: 1000000, tx: 50000 })
  const zero = { rx: 0, tx: 0 }
  assert.deepStrictEqual(Model.rates([{ t: 0, rx: 0, tx: 0 }]), zero)
  assert.deepStrictEqual(Model.rates([]), zero)
  assert.deepStrictEqual(Model.rates([{ t: 5, rx: 0, tx: 0 }, { t: 5, rx: 9, tx: 9 }]), zero)
  assert.deepStrictEqual(Model.rates([{ t: 0, rx: 9, tx: 9 }, { t: 1000, rx: 1, tx: 1 }]), zero)
})

test("formatBytes and formatRate", () => {
  const fmt = (v, d) => v.toFixed(d)
  const cases = [[0, "0 B"], [999, "999 B"], [1000, "1.0 KB"], [1234567, "1.2 MB"], [1.5e9, "1.5 GB"], [2e12, "2.0 TB"], [5e15, "5000.0 TB"],
    [NaN, "0 B"], [-1, "0 B"], [Infinity, "0 B"], [undefined, "0 B"]]
  for (const [n, want] of cases) assert.strictEqual(Model.formatBytes(n, fmt), want)
  assert.strictEqual(Model.formatRate(1234567, fmt), "1.2 MB/s")
})

test("graphSeries scales both lines to a shared maximum", () => {
  const g = Model.graphSeries([
    { t: 60000, rx: 0, tx: 0 }, { t: 90000, rx: 3000000, tx: 300000 }, { t: 120000, rx: 4500000, tx: 330000 }
  ], 120000)
  assert.deepStrictEqual(g, {
    max: 100000,
    rx: [{ x: 0.75, y: 1 }, { x: 1, y: 0.5 }],
    tx: [{ x: 0.75, y: 0.1 }, { x: 1, y: 0.01 }]
  })
})

test("graphSeries stays finite for idle, single and degenerate input", () => {
  const idle = Model.graphSeries([{ t: 1000, rx: 5, tx: 5 }, { t: 2000, rx: 5, tx: 5 }, { t: 3000, rx: 5, tx: 5 }], 3000)
  assert.strictEqual(idle.max, 10000)
  assert.ok(idle.rx.length === 2 && idle.rx.every((p) => p.y === 0))
  assert.ok(idle.tx.every((p) => p.y === 0))
  assert.deepStrictEqual(Model.graphSeries([{ t: 1, rx: 1, tx: 1 }], 1), { max: 10000, rx: [], tx: [] })
  assert.deepStrictEqual(Model.graphSeries([], 1), { max: 10000, rx: [], tx: [] })
  const same = Model.graphSeries([{ t: 5, rx: 0, tx: 0 }, { t: 5, rx: 99, tx: 99 }], 5)
  for (const p of same.rx.concat(same.tx)) assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y))
  assert.ok(Number.isFinite(same.max))
})

.pragma library

var INVALID = "Invalid response from helper"

function parseResult(raw) {
  var text = String(raw === undefined || raw === null ? "" : raw).trim()
  if (text === "") return { ok: false, error: INVALID }
  var parsed
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    return { ok: false, error: INVALID }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { ok: false, error: INVALID }
  parsed.ok = parsed.ok === true
  return parsed
}

function stateKey(status) {
  if (!status || typeof status !== "object") return "disconnected"
  if (status.sshuttle_installed === false) return "unavailable"
  var s = status.state
  if (s === "connected" || s === "connecting" || s === "error") return s
  return "disconnected"
}

// Structured pieces so the UI can build a translated summary.
function summaryParts(profile) {
  var p = profile || {}
  var subnets = Array.isArray(p.subnets) ? p.subnets : []
  return {
    remote: String(p.remote || ""),
    subnetCount: subnets.length,
    allTraffic: subnets.length === 0 || (subnets.length === 1 && subnets[0] === "0/0")
  }
}

function profileSummary(profile) {
  var p = profile || {}
  var subnets = Array.isArray(p.subnets) ? p.subnets : []
  var what
  if (subnets.length === 0 || (subnets.length === 1 && subnets[0] === "0/0")) what = "all traffic"
  else what = subnets.length + (subnets.length === 1 ? " subnet" : " subnets")
  return String(p.remote || "") + " · " + what
}

function splitList(text) {
  return String(text === undefined || text === null ? "" : text)
    .split(/[\s,]+/)
    .filter(function (s) { return s !== "" })
}

function formToProfile(form) {
  var f = form || {}
  return {
    name: String(f.name === undefined || f.name === null ? "" : f.name).trim(),
    remote: String(f.remote === undefined || f.remote === null ? "" : f.remote).trim(),
    subnets: splitList(f.subnetsText),
    excludes: splitList(f.excludesText),
    dns: f.dns === true,
    method: String(f.method || "auto"),
    disable_ipv6: f.disableIpv6 === true,
    ssh_identity: String(f.sshIdentity === undefined || f.sshIdentity === null ? "" : f.sshIdentity).trim()
  }
}

function profileToForm(profile) {
  var p = profile || {}
  return {
    name: String(p.name || ""),
    remote: String(p.remote || ""),
    subnetsText: (Array.isArray(p.subnets) ? p.subnets : []).join("\n"),
    excludesText: (Array.isArray(p.excludes) ? p.excludes : []).join("\n"),
    dns: p.dns === true,
    method: String(p.method || "auto"),
    disableIpv6: p.disable_ipv6 === true,
    sshIdentity: String(p.ssh_identity || "")
  }
}

function clip(value, max) {
  var s = String(value === undefined || value === null ? "" : value)
  return s.length > max ? s.substring(0, max) : s
}

// Helper output is untrusted: copy only known fields with bounded sizes.
// Caps match the limits ctl.py enforces: name 40, remote 300, ssh_identity 4096,
// 200 list entries of at most 100 characters each.
function sanitizeProfile(raw) {
  if (!raw || typeof raw !== "object") return null
  var methods = ["auto", "nft", "nat", "tproxy"]
  return {
    name: clip(raw.name, 40),
    remote: clip(raw.remote, 300),
    subnets: cleanList(raw.subnets),
    excludes: cleanList(raw.excludes),
    dns: raw.dns === true,
    method: methods.indexOf(raw.method) >= 0 ? raw.method : "auto",
    disable_ipv6: raw.disable_ipv6 === true,
    ssh_identity: clip(raw.ssh_identity, 4096)
  }
}

function cleanList(list) {
  var out = []
  if (!Array.isArray(list)) return out
  for (var i = 0; i < list.length && i < 200; i++) out.push(clip(list[i], 100))
  return out
}

function sanitizeStatus(raw) {
  var states = ["disconnected", "connecting", "connected", "error"]
  var r = raw && typeof raw === "object" ? raw : {}
  var pid = typeof r.pid === "number" && isFinite(r.pid) ? r.pid : null
  var since = typeof r.since === "number" && isFinite(r.since) ? r.since : null
  var rx = counter(r.rx_bytes)
  var tx = counter(r.tx_bytes)
  return {
    ok: true,
    state: states.indexOf(r.state) >= 0 ? r.state : "disconnected",
    profile: typeof r.profile === "string" ? clip(r.profile, 40) : null,
    pid: pid,
    since: since,
    error: typeof r.error === "string" && r.error !== "" ? clip(r.error, 500) : null,
    sshuttle_installed: r.sshuttle_installed !== false,
    sudo_ready: r.sudo_ready === true,
    rx_bytes: rx,
    tx_bytes: tx
  }
}

// A byte counter is a finite number >= 0, floored; anything else is unknown (null).
function counter(value) {
  if (typeof value !== "number" || !isFinite(value) || value < 0) return null
  return Math.floor(value)
}

var WINDOW_MS = 120000
var MAX_SAMPLES = 200
var MIN_SCALE = 10000

// Returns a new array; the input is never modified.
function appendSample(samples, status, nowMs) {
  var s = status && typeof status === "object" ? status : {}
  if (s.state !== "connected" || typeof s.rx_bytes !== "number" || typeof s.tx_bytes !== "number") return []
  var since = typeof s.since === "number" && isFinite(s.since) ? s.since : null
  var next = { t: nowMs, rx: s.rx_bytes, tx: s.tx_bytes, since: since }
  var prev = Array.isArray(samples) ? samples : []
  var last = prev.length > 0 ? prev[prev.length - 1] : null
  if (last && (last.rx > next.rx || last.tx > next.tx || last.since !== next.since)) return [next]
  var cutoff = nowMs - WINDOW_MS
  var out = []
  for (var i = 0; i < prev.length; i++) {
    if (prev[i].t >= cutoff) out.push(prev[i])
  }
  out.push(next)
  return out.length > MAX_SAMPLES ? out.slice(out.length - MAX_SAMPLES) : out
}

// Bytes per second between two samples; never negative, 0 when undefined.
function pairRate(a, b, key) {
  var dt = (b.t - a.t) / 1000
  if (!(dt > 0)) return 0
  var d = (b[key] - a[key]) / dt
  return isFinite(d) && d > 0 ? d : 0
}

function rates(samples) {
  var list = Array.isArray(samples) ? samples : []
  if (list.length < 2) return { rx: 0, tx: 0 }
  var a = list[list.length - 2]
  var b = list[list.length - 1]
  return { rx: pairRate(a, b, "rx"), tx: pairRate(a, b, "tx") }
}

function formatBytes(n, formatNumber) {
  var v = typeof n === "number" && isFinite(n) && n > 0 ? n : 0
  if (v < 1000) return formatNumber(Math.floor(v), 0) + " B"
  var units = ["KB", "MB", "GB", "TB"]
  var i = -1
  do {
    v = v / 1000
    i++
  } while (v >= 1000 && i < units.length - 1)
  return formatNumber(v, 1) + " " + units[i]
}

function formatRate(n, formatNumber) {
  return formatBytes(n, formatNumber) + "/s"
}

function graphSeries(samples, nowMs) {
  var list = Array.isArray(samples) ? samples : []
  var rx = []
  var tx = []
  var max = MIN_SCALE
  var start = nowMs - WINDOW_MS
  for (var i = 1; i < list.length; i++) {
    var x = (list[i].t - start) / WINDOW_MS
    x = x < 0 ? 0 : (x > 1 ? 1 : x)
    var r = pairRate(list[i - 1], list[i], "rx")
    var t = pairRate(list[i - 1], list[i], "tx")
    if (r > max) max = r
    if (t > max) max = t
    rx.push({ x: x, y: r })
    tx.push({ x: x, y: t })
  }
  for (var j = 0; j < rx.length; j++) {
    rx[j].y = rx[j].y / max
    tx[j].y = tx[j].y / max
  }
  return { max: max, rx: rx, tx: tx }
}

function sanitizeLines(lines) {
  var out = []
  if (!Array.isArray(lines)) return out
  var start = Math.max(0, lines.length - 50)
  for (var i = start; i < lines.length; i++) out.push(clip(lines[i], 300))
  return out
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    parseResult: parseResult, stateKey: stateKey, profileSummary: profileSummary, summaryParts: summaryParts,
    formToProfile: formToProfile, profileToForm: profileToForm,
    sanitizeProfile: sanitizeProfile, sanitizeStatus: sanitizeStatus, sanitizeLines: sanitizeLines,
    appendSample: appendSample, rates: rates, formatBytes: formatBytes, formatRate: formatRate, graphSeries: graphSeries
  }
}

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
    ssh_identity: clip(raw.ssh_identity, 500)
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
  return {
    ok: true,
    state: states.indexOf(r.state) >= 0 ? r.state : "disconnected",
    profile: typeof r.profile === "string" ? clip(r.profile, 40) : null,
    pid: pid,
    since: since,
    error: typeof r.error === "string" && r.error !== "" ? clip(r.error, 500) : null,
    sshuttle_installed: r.sshuttle_installed !== false,
    sudo_ready: r.sudo_ready === true
  }
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
    parseResult: parseResult, stateKey: stateKey, profileSummary: profileSummary,
    formToProfile: formToProfile, profileToForm: profileToForm,
    sanitizeProfile: sanitizeProfile, sanitizeStatus: sanitizeStatus, sanitizeLines: sanitizeLines
  }
}

import QtQuick
import Quickshell.Io
import "Model.js" as Model

// Talks to ctl.py. Every call is an argv list (never a shell string); profile
// names and the `save` profile JSON are passed as separate argv elements.
//
// Errors are kept in two independent properties so a status poll can never
// wipe a command failure:
//   commandError  set when a user-initiated command (connect, disconnect,
//                 save, delete, set-default, setup-sudo) fails. Cleared only
//                 when the next user-initiated command starts, or by
//                 dismissError(). Status polls and internal follow-up reads
//                 never touch it.
//   status.error  the helper-reported error of the latest status poll
//                 (statusError holds a failed poll itself).
// lastError is a read-only convenience binding over both (commandError wins).
// Helper output is untrusted: only known fields are copied, with bounded sizes.
Item {
  id: root

  property int refreshIntervalSec: 5

  property var status: ({
    ok: true, state: "disconnected", profile: null, pid: null, since: null,
    error: null, sshuttle_installed: true, sudo_ready: false
  })
  property var profiles: []
  property string defaultProfile: ""
  property string commandError: ""
  property string statusError: ""
  readonly property string lastError: commandError !== "" ? commandError : (statusError !== "" ? statusError : (status.error || ""))
  property var logLines: []

  // Traffic stats: only meaningful while connected; reset otherwise.
  property var trafficSamples: []
  property real rxRate: 0
  property real txRate: 0
  property var rxTotal: null
  property var txTotal: null
  property bool fastPolling: false

  readonly property bool busy: _cmdActive || _queue.length > 0
  readonly property string ctlPath: decodeURIComponent(String(Qt.resolvedUrl("ctl.py")).replace(/^file:\/\//, ""))

  property bool _cmdActive: false
  property var _queue: []
  property var _job: null
  property bool _refreshAgain: false
  property bool _timedOut: false
  property string _statusOutput: ""
  property string _cmdOutput: ""

  // Status polling -------------------------------------------------------

  function refresh() {
    if (statusProcess.running) {
      // Do not overlap polls; run one more as soon as this one finishes.
      _refreshAgain = true
      return
    }
    _statusOutput = ""
    statusProcess.command = ["python3", ctlPath, "status"]
    statusProcess.running = true
    statusWatchdog.restart()
  }

  function applyStatus(result) {
    if (!result.ok) {
      statusError = clip(result.error || "Status error", 500)
      return
    }
    statusError = ""
    status = Model.sanitizeStatus(result)
    trafficSamples = Model.appendSample(trafficSamples, status, Date.now())
    var r = Model.rates(trafficSamples)
    rxRate = r.rx
    txRate = r.tx
    rxTotal = status.rx_bytes
    txTotal = status.tx_bytes
  }

  function dismissError() {
    commandError = ""
  }

  function clip(value, max) {
    var s = String(value === undefined || value === null ? "" : value)
    return s.length > max ? s.substring(0, max) : s
  }

  // Command queue ----------------------------------------------------------

  // args: argv after ctl.py; handler(result, job);
  // done: optional caller callback receiving the parsed result.
  // internal: follow-up reads that must not clear a visible commandError.
  function enqueue(args, handler, done, timeoutMs, internal) {
    var q = _queue.slice()
    q.push({ args: args, handler: handler, done: done, timeoutMs: timeoutMs || 60000, internal: internal === true })
    _queue = q
    runNext()
  }

  function runNext() {
    if (_cmdActive || _queue.length === 0) return
    var q = _queue.slice()
    _job = q.shift()
    _queue = q
    _cmdActive = true
    _cmdOutput = ""
    _timedOut = false
    if (!_job.internal) commandError = ""
    cmdProcess.command = ["python3", ctlPath].concat(_job.args)
    cmdProcess.running = true
    cmdWatchdog.interval = _job.timeoutMs
    cmdWatchdog.restart()
  }

  function finishCommand(exitCode) {
    cmdWatchdog.stop()
    var job = _job
    _job = null
    _cmdActive = false
    var result = Model.parseResult(_cmdOutput)
    if (exitCode !== 0 && result.ok) result = { ok: false, error: "Helper failed" }
    if (_timedOut) result = { ok: false, error: "Helper timed out" }
    if (!result.ok) commandError = clip(result.error || "Helper failed", 500)
    if (job) {
      // A throwing callback must never wedge the queue.
      try {
        if (job.handler) job.handler(result, job)
      } catch (e) {
        console.warn("sshuttle service handler failed:", e)
      }
      try {
        if (job.done) job.done({ ok: result.ok, error: result.ok ? "" : clip(result.error, 500) })
      } catch (e) {
        console.warn("sshuttle service callback failed:", e)
      }
    }
    runNext()
  }

  // Public API -------------------------------------------------------------

  function refreshProfiles() {
    enqueue(["list"], function (r) {
      if (!r.ok) return
      var next = []
      var list = Array.isArray(r.profiles) ? r.profiles : []
      for (var i = 0; i < list.length && i < 200; i++) {
        var p = Model.sanitizeProfile(list[i])
        if (p) next.push(p)
      }
      profiles = next
      defaultProfile = typeof r.default === "string" ? clip(r.default, 40) : ""
    }, null, 0, true)
  }

  function connect(name) {
    var args = ["start"]
    if (name !== undefined && name !== null && String(name) !== "") args.push(String(name))
    enqueue(args, function () { refresh() })
  }

  function disconnect() {
    enqueue(["stop"], function () { refresh() })
  }

  function saveProfile(profile, done) {
    enqueue(["save", "--json", JSON.stringify(profile || {})], function (r) {
      if (r.ok) refreshProfiles()
    }, done)
  }

  function deleteProfile(name) {
    enqueue(["delete", String(name)], function () { refreshProfiles() })
  }

  function setDefault(name) {
    enqueue(["set-default", String(name)], function () { refreshProfiles() })
  }

  // Only ever called from an explicit user action; never automatic.
  function setupSudo() {
    enqueue(["setup-sudo"], function () { refresh() }, null, 180000)
  }

  function loadLog() {
    enqueue(["log"], function (r) {
      if (r.ok) logLines = Model.sanitizeLines(r.lines)
    }, null, 0, true)
  }

  // Plumbing ---------------------------------------------------------------

  Timer {
    id: pollTimer
    interval: (root.status.state === "connecting" || (root.fastPolling && root.status.state === "connected")) ? 1000 : Math.max(1, root.refreshIntervalSec) * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // A hung helper must not stall polling or the queue forever.
  Timer {
    id: statusWatchdog
    interval: 15000
    repeat: false
    onTriggered: {
      if (statusProcess.running) {
        statusProcess.running = false
      } else {
        // Never started or never reported exit: do not leave polling wedged.
        root.statusError = "Helper timed out"
        if (root._refreshAgain) {
          root._refreshAgain = false
          root.refresh()
        }
      }
    }
  }

  Timer {
    id: cmdWatchdog
    interval: 60000
    repeat: false
    onTriggered: {
      root._timedOut = true
      if (cmdProcess.running) cmdProcess.running = false  // emits exited -> finishCommand
      else if (root._cmdActive) root.finishCommand(-1)    // spawn failure: no exited signal
    }
  }

  Component.onCompleted: refreshProfiles()

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusStdout; waitForEnd: true; onStreamFinished: root._statusOutput = text }
    onExited: function (exitCode) {
      statusWatchdog.stop()
      var out = String(statusStdout.text || root._statusOutput || "")
      var result = Model.parseResult(out)
      if (exitCode !== 0 && result.ok) result = { ok: false, error: "Helper failed" }
      root.applyStatus(result)
      if (root._refreshAgain) {
        root._refreshAgain = false
        root.refresh()
      }
    }
  }

  Process {
    id: cmdProcess
    running: false
    command: []
    stdout: StdioCollector { id: cmdStdout; waitForEnd: true; onStreamFinished: root._cmdOutput = text }
    onExited: function (exitCode) {
      root._cmdOutput = String(cmdStdout.text || root._cmdOutput || "")
      root.finishCommand(exitCode)
    }
  }
}

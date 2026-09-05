import QtQuick
import Quickshell
import Quickshell.Io

// Oma2FA keeps the bridge private to omarchy-shell. Codes and commands travel
// over this process's stdin/stdout JSON-lines channel, never in argv or shell
// IPC. Only the deliberately minimal code records are retained in QML.
Item {
  id: root

  // Injected by omarchy-shell's service loader.
  property var shell: null
  property var manifest: null

  readonly property string sourceDir: manifest && manifest.__sourceDir
    ? String(manifest.__sourceDir) : ""
  readonly property string bridgePath: sourceDir
    ? sourceDir.replace(/\/$/, "") + "/bin/oma2fa-bridge" : ""

  property var snapshot: ({ codes: [] })
  property var records: []
  property var status: ({ state: "starting", message: "Starting Oma2FA…" })
  property var webhookSetup: ({
    configured: false,
    configuration_present: false,
    unit_installed: false,
    enabled: false,
    running: false,
    bind: "",
    port: 8765,
    transport: "",
    endpoint: "",
    token_present: false,
    tailscale_available: false,
    tailscale_ip: "",
    detail: "not configured"
  })
  property bool bridgeAlive: false
  property bool ready: false
  property string lastError: ""
  property int nextRequestId: 1
  property var pendingRequests: ({})
  property int restartAttempt: 0
  property bool shuttingDown: false

  readonly property bool available: bridgeAlive && ready

  signal requestFinished(int requestId, string method, bool ok, string message)

  function safeError(value, fallback) {
    var text = String(value || fallback || "Oma2FA bridge error")
    // Backend errors should already be secret-free. This is defense in depth
    // against accidentally reflecting an OTP in the UI or shell diagnostics.
    text = text.replace(/[0-9]{4,8}/g, "••••")
    text = text.replace(/\b(?=[A-Z0-9]{5,10}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]+\b/gi,
      "••••")
    return text.length > 240 ? text.substring(0, 237) + "…" : text
  }

  function timestampMs(value) {
    if (value === undefined || value === null || value === "") return 0
    var numeric = Number(value)
    if (isFinite(numeric) && numeric > 0)
      return numeric < 1000000000000 ? numeric * 1000 : numeric
    var parsed = Date.parse(String(value))
    return isFinite(parsed) ? parsed : 0
  }

  function normalizeRecord(value) {
    if (!value || typeof value !== "object") return null

    var id = value.id === undefined || value.id === null
      ? "" : String(value.id)
    var code = value.code === undefined || value.code === null
      ? "" : String(value.code)
    if (!id || !code) return null

    var receivedAt = value.received_at !== undefined
      ? value.received_at : value.receivedAt
    var expiresAt = value.expires_at !== undefined
      ? value.expires_at : value.expiresAt
    var confidence = Number(value.confidence)

    return {
      id: id,
      code: code,
      service: String(value.service || "Unknown service"),
      source: String(value.source || "SMS"),
      received_at: receivedAt === undefined || receivedAt === null
        ? "" : String(receivedAt),
      expires_at: expiresAt === undefined || expiresAt === null
        ? "" : String(expiresAt),
      received_ms: timestampMs(receivedAt),
      expires_ms: timestampMs(expiresAt),
      confidence: isFinite(confidence) ? confidence : -1
    }
  }

  function applyStatus(value) {
    if (value === undefined || value === null) return false

    if (typeof value === "string") {
      root.status = { state: String(value), message: "" }
      root.ready = true
      return true
    }
    if (typeof value !== "object") return false

    root.status = value
    root.ready = value.ready === undefined ? true : value.ready === true
    var state = String(value.state || value.status || "").toLowerCase()
    if (state === "error" || state === "unavailable" || state === "failed") {
      root.lastError = root.safeError(value.message || value.error,
        "Oma2FA is unavailable")
    } else {
      root.lastError = ""
    }
    return true
  }

  function applySnapshot(value) {
    var candidate = value
    if (candidate && typeof candidate === "object" && candidate.snapshot)
      candidate = candidate.snapshot

    var codes = Array.isArray(candidate)
      ? candidate
      : (candidate && Array.isArray(candidate.codes) ? candidate.codes : null)
    if (!codes) return false

    var normalized = []
    for (var i = 0; i < codes.length; i++) {
      var record = root.normalizeRecord(codes[i])
      if (record) normalized.push(record)
    }
    normalized.sort(function(a, b) {
      var timeDelta = Number(b.received_ms || 0) - Number(a.received_ms || 0)
      return timeDelta !== 0 ? timeDelta : String(b.id).localeCompare(String(a.id))
    })

    root.records = normalized
    // Do not retain unknown snapshot fields: a buggy adapter must not be able
    // to smuggle an original SMS body into long-lived QML state.
    root.snapshot = {
      codes: normalized,
      generated_at: candidate && candidate.generated_at
        ? String(candidate.generated_at) : ""
    }
    root.ready = true
    root.lastError = ""
    if (candidate && candidate.status !== undefined)
      root.applyStatus(candidate.status)
    return true
  }

  function removeLocal(recordId) {
    var key = String(recordId || "")
    if (!key) return
    var next = []
    for (var i = 0; i < root.records.length; i++) {
      if (String(root.records[i].id) !== key) next.push(root.records[i])
    }
    if (next.length !== root.records.length) {
      root.records = next
      root.snapshot = { codes: next, generated_at: root.snapshot.generated_at || "" }
    }
  }

  function clearLocal() {
    root.records = []
    root.snapshot = { codes: [], generated_at: "" }
  }

  function rememberRequest(id, method, args) {
    var next = ({})
    for (var key in root.pendingRequests) next[key] = root.pendingRequests[key]
    next[String(id)] = { method: method, args: args || ({}) }
    root.pendingRequests = next
  }

  function takeRequest(id) {
    var key = String(id)
    var found = root.pendingRequests[key] || null
    var next = ({})
    for (var existing in root.pendingRequests) {
      if (existing !== key) next[existing] = root.pendingRequests[existing]
    }
    root.pendingRequests = next
    return found
  }

  function sendRequest(method, args) {
    if (!root.bridgeAlive || !bridgeProcess.running) {
      root.lastError = "Oma2FA bridge is not connected"
      return -1
    }

    var id = root.nextRequestId++
    var requestArgs = args || ({})
    root.rememberRequest(id, String(method), requestArgs)
    try {
      bridgeProcess.write(JSON.stringify({
        id: id,
        method: String(method),
        args: requestArgs
      }) + "\n")
    } catch (error) {
      root.takeRequest(id)
      root.lastError = "Could not send a request to Oma2FA"
      return -1
    }
    return id
  }

  function requestStatus() {
    return root.sendRequest("status", {})
  }

  function refresh() {
    return root.sendRequest("refresh", {})
  }

  function activate(recordId, paste, target) {
    if (recordId === undefined || recordId === null || String(recordId) === "")
      return -1
    return root.sendRequest("activate", {
      record_id: String(recordId),
      paste: paste === true,
      target: target && typeof target === "object" ? target : ({})
    })
  }

  function deleteRecord(recordId) {
    if (recordId === undefined || recordId === null || String(recordId) === "")
      return -1
    var id = root.sendRequest("delete", { record_id: String(recordId) })
    if (id >= 0) root.removeLocal(recordId)
    return id
  }

  function clear() {
    var id = root.sendRequest("clear", {})
    if (id >= 0) root.clearLocal()
    return id
  }

  function requestWebhookStatus() {
    return root.sendRequest("webhook_status", {})
  }

  function configureWebhookTailscale() {
    return root.sendRequest("webhook_configure_tailscale", { port: 8765 })
  }

  function setWebhookEnabled(enabled) {
    return root.sendRequest("webhook_set_enabled", { enabled: enabled === true })
  }

  // Message sources other than the webhook (BlueFerry, Blip, Tether) are
  // toggled through the bridge, which persists the choice in sources.json.
  function setSourceEnabled(sourceId, enabled) {
    var id = String(sourceId || "")
    if (!id) return -1
    return root.sendRequest("source_set_enabled", { source: id, enabled: enabled === true })
  }

  function copyWebhookEndpoint() {
    return root.sendRequest("webhook_copy_endpoint", {})
  }

  function copyWebhookToken() {
    return root.sendRequest("webhook_copy_token", {})
  }

  function copyWebhookSetupField(fieldId) {
    if (fieldId === undefined || fieldId === null || String(fieldId) === "")
      return -1
    return root.sendRequest("webhook_copy_setup_field", {
      field_id: String(fieldId)
    })
  }

  function rotateWebhookToken() {
    return root.sendRequest("webhook_rotate_token", { confirmed: true })
  }

  function applyWebhookSetup(value) {
    if (!value || typeof value !== "object") return false
    // Retain only the manager's non-secret allowlist. In particular, an
    // accidental backend token field must never become long-lived QML state.
    root.webhookSetup = {
      configured: value.configured === true,
      configuration_present: value.configuration_present === true,
      unit_installed: value.unit_installed === true,
      enabled: value.enabled === true,
      running: value.running === true,
      bind: String(value.bind || "").substring(0, 64),
      port: Math.max(0, Math.min(65535, Number(value.port) || 8765)),
      transport: String(value.transport || "").substring(0, 16),
      endpoint: String(value.endpoint || "").substring(0, 256),
      token_present: value.token_present === true,
      tailscale_available: value.tailscale_available === true,
      tailscale_ip: String(value.tailscale_ip || "").substring(0, 64),
      detail: String(value.detail || "").substring(0, 80)
    }
    return true
  }

  function responseSnapshot(result) {
    if (root.applySnapshot(result)) return true
    if (result && root.applySnapshot(result.snapshot)) return true
    return false
  }

  function handleResponse(message) {
    var pending = root.takeRequest(message.id)
    var method = String(message.method || (pending ? pending.method : ""))
    var ok = message.ok === true

    if (!ok) {
      var errorText = root.safeError(message.error, "Oma2FA request failed")
      root.lastError = errorText
      root.requestFinished(Number(message.id) || -1, method, false, errorText)
      // A failed optimistic mutation is reconciled against the daemon.
      if (method === "delete" || method === "clear") root.refresh()
      return
    }

    var result = message.result
    if (method === "status") {
      root.applyStatus(result)
      root.responseSnapshot(result)
    } else if (method === "refresh") {
      root.responseSnapshot(result)
      if (result && result.status !== undefined) root.applyStatus(result.status)
    } else if (method === "source_set_enabled") {
      if (result && result.status !== undefined) root.applyStatus(result.status)
    } else if (method === "delete") {
      if (!root.responseSnapshot(result) && pending && pending.args)
        root.removeLocal(pending.args.record_id)
    } else if (method === "clear") {
      if (!root.responseSnapshot(result)) root.clearLocal()
    } else if (method === "activate") {
      root.responseSnapshot(result)
    } else if (method === "webhook_status"
        || method === "webhook_configure_tailscale"
        || method === "webhook_set_enabled"
        || method === "webhook_rotate_token") {
      root.applyWebhookSetup(result)
    }

    root.lastError = ""
    root.ready = true
    root.requestFinished(Number(message.id) || -1, method, true, "")
  }

  function handleLine(rawLine) {
    var line = String(rawLine || "").trim()
    if (!line) return

    var message = null
    try { message = JSON.parse(line) } catch (error) {
      root.lastError = "Oma2FA bridge sent an invalid response"
      return
    }
    if (!message || typeof message !== "object") return

    if (message.event === "snapshot") {
      root.applySnapshot(message.data)
      return
    }
    if (message.event === "status") {
      root.applyStatus(message.data)
      return
    }
    if (message.id !== undefined) root.handleResponse(message)
  }

  function startBridge() {
    if (root.shuttingDown || !root.bridgePath || bridgeProcess.running) return
    restartTimer.stop()
    root.status = { state: "starting", message: "Connecting to Oma2FA…" }
    bridgeProcess.command = [root.bridgePath]
    bridgeProcess.running = true
  }

  function bridgeStarted() {
    root.bridgeAlive = true
    root.lastError = ""
    root.status = { state: "starting", message: "Loading recent codes…" }
    stableTimer.restart()
    root.requestStatus()
    root.refresh()
  }

  function bridgeExited(exitCode) {
    root.bridgeAlive = false
    root.ready = false
    stableTimer.stop()
    root.pendingRequests = ({})
    // The daemon owns persistence and will resend a fresh snapshot after a
    // reconnect. Drop ephemeral QML copies while its state cannot be verified.
    root.clearLocal()
    if (root.shuttingDown) return

    root.lastError = exitCode === 0
      ? "Oma2FA bridge stopped"
      : "Oma2FA bridge exited unexpectedly"
    root.status = { state: "reconnecting", message: root.lastError }

    var delay = Math.min(30000, 500 * Math.pow(2, root.restartAttempt))
    root.restartAttempt = Math.min(root.restartAttempt + 1, 7)
    restartTimer.interval = delay
    restartTimer.restart()
  }

  onBridgePathChanged: Qt.callLater(root.startBridge)

  Process {
    id: bridgeProcess
    stdinEnabled: true

    stdout: SplitParser {
      onRead: function(line) { root.handleLine(line) }
    }

    // Consume stderr privately. Never echo it: an adapter bug must not put an
    // SMS body or OTP in the shell log.
    stderr: SplitParser {
      onRead: function(line) {
        if (String(line || "").length > 0 && !root.lastError)
          root.lastError = "Oma2FA bridge reported an error"
      }
    }

    onStarted: root.bridgeStarted()
    onExited: function(exitCode) { root.bridgeExited(exitCode) }
  }

  Timer {
    id: restartTimer
    interval: 500
    repeat: false
    onTriggered: root.startBridge()
  }

  // Only reset the crash-loop backoff after the process has remained alive.
  Timer {
    id: stableTimer
    interval: 10000
    repeat: false
    onTriggered: root.restartAttempt = 0
  }

  Timer {
    interval: 15000
    repeat: true
    running: true
    onTriggered: {
      var now = Date.now()
      var next = []
      for (var i = 0; i < root.records.length; i++) {
        var expires = Number(root.records[i].expires_ms || 0)
        if (expires <= 0 || expires > now) next.push(root.records[i])
      }
      if (next.length !== root.records.length) {
        root.records = next
        root.snapshot = { codes: next, generated_at: root.snapshot.generated_at || "" }
      }
    }
  }

  Component.onCompleted: Qt.callLater(root.startBridge)
  Component.onDestruction: {
    root.shuttingDown = true
    restartTimer.stop()
    stableTimer.stop()
    if (bridgeProcess.running) bridgeProcess.running = false
  }
}

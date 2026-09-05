import QtQuick
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  // Injected by omarchy-shell's overlay loader. `service` is the matching
  // io.github.jondkinney.oma2fa service instance, not a second backend connection.
  property var shell: null
  property var manifest: null
  property var service: null

  property bool opened: false
  property bool openingPending: false
  property string filterText: ""
  property int selectedIndex: 0
  property bool cursorActive: false
  readonly property bool searchModeActive: !root.cursorActive
  property var capturedTarget: ({})
  property int clockRevision: 0
  property bool transportDetailsPinned: false
  property bool webhookSetupOpen: false
  property bool webhookGuideOpen: false
  property real guideScrollPosition: 0
  property string copiedField: ""
  property int fieldCopyRequest: -1
  property string pendingCopyField: ""
  property bool webhookBusy: false
  property bool tokenRotationArmed: false
  property string webhookNotice: ""
  // Source ids with a toggle request in flight, and the last toggle failure.
  property var sourceToggleBusy: ({})
  property string sourceNotice: ""

  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color border: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border,
    Math.max(1, Style.space(2)))
  property color scrim: Color.menu.scrim
  property color selectedBackground: Color.menu.selectedBackground
  property color selectedText: Color.menu.selectedText
  readonly property int cornerRadius: Style.cornerRadius
  property string fontFamily: Style.font.menuFamily
  readonly property string uiFontFamily: "sans-serif"
  readonly property string guideFontFamily: root.uiFontFamily
  readonly property int guideBodyFontSize: Style.font.subtitle
  readonly property int guideCaptionFontSize: Style.font.body
  readonly property int guideHeadingFontSize: Style.font.heading
  readonly property int guideTitleFontSize: Style.font.display
  readonly property int guideParagraphSpacing: Style.spacing.lg
  readonly property int guideScreenshotWidth: Style.space(364)
  property int contentMargin: Style.spacing.panelPadding
  property int contentSpacing: Style.spacing.md
  property int cardWidth: Math.min(Style.space(620), panel.width - Style.gapsOut * 2)
  property int cardHeight: Math.min(
    root.webhookSetupOpen ? Style.space(560)
      : Math.max(Style.space(340), Math.min(Style.space(560),
          Style.space(180) + displayModel.count * root.rowHeight)),
    panel.height - Style.gapsOut * 2)
  readonly property int guideCardWidth: Math.min(Style.space(760),
    Math.round(panel.width * 0.92), panel.width - Style.gapsOut * 2)
  readonly property int guideCardHeight: Math.min(Math.round(panel.height * 0.90),
    panel.height - Style.gapsOut * 2)
  property int titleHeight: Math.max(Style.space(30), Style.font.heading + Style.spacing.sm)
  property int searchHeight: Math.max(Style.space(42), Style.spacing.controlHeight)
  property int footerLineHeight: Math.max(Style.space(24),
    Style.font.caption + Style.spacing.sm)
  property bool footerStacked: displayModel.count > 0 && footerPrimaryHints.implicitWidth
    + footerSecondaryHints.implicitWidth + Style.spacing.xl > shortcutFooter.width
  property int footerHeight: footerStacked
    ? footerLineHeight * 2 + Style.spacing.xs : footerLineHeight
  property int rowHeight: Math.max(Style.space(70),
    Style.font.title + Style.font.bodySmall + Style.spacing.rowPaddingX * 2)

  function pluginId() {
    return (root.manifest && root.manifest.id)
      ? String(root.manifest.id) : "io.github.jondkinney.oma2fa"
  }

  function parseJson(raw, fallback) {
    try { return JSON.parse(String(raw || "")) } catch (error) { return fallback }
  }

  function timestampMs(value) {
    if (value === undefined || value === null || value === "") return 0
    var numeric = Number(value)
    if (isFinite(numeric) && numeric > 0)
      return numeric < 1000000000000 ? numeric * 1000 : numeric
    var parsed = Date.parse(String(value))
    return isFinite(parsed) ? parsed : 0
  }

  function sanitizeTarget(windowData) {
    if (!windowData || typeof windowData !== "object") return ({})

    var address = String(windowData.address || "")
    var className = String(windowData.class || windowData.initialClass || "")
    var pid = Number(windowData.pid)
    // Empty/0x0 is Hyprland's no-client response. Layer surfaces are not
    // normal active windows and should never become paste targets.
    if ((!address || address === "0x0") && !className && !(isFinite(pid) && pid > 0))
      return ({})

    var stable = windowData.stable_id !== undefined
      ? windowData.stable_id
      : (windowData.stableId !== undefined ? windowData.stableId : "")
    var target = ({})
    if (stable !== undefined && stable !== null && String(stable))
      target.stable_id = String(stable)
    if (address) target.address = address
    if (isFinite(pid) && pid > 0) target.pid = Math.floor(pid)
    if (className) target.class = className
    if (windowData.accepts_input !== undefined)
      target.accepts_input = windowData.accepts_input === true
    else if (windowData.acceptsInput !== undefined)
      target.accepts_input = windowData.acceptsInput === true
    return target
  }

  function open(payloadJson) {
    var payload = root.parseJson(payloadJson, {})
    root.filterText = ""
    root.selectedIndex = 0
    root.cursorActive = false
    root.transportDetailsPinned = false
    root.webhookSetupOpen = false
    root.webhookGuideOpen = false
    root.webhookBusy = false
    root.tokenRotationArmed = false
    root.webhookNotice = ""
    root.capturedTarget = root.sanitizeTarget(payload.target)
    root.disarmPointer()

    if (root.service && typeof root.service.refresh === "function")
      root.service.refresh()

    // A caller may supply a target captured immediately before summon. The
    // normal hotkey path captures it here while no layer-shell surface exists.
    if (Object.keys(root.capturedTarget).length > 0) {
      root.finishOpen()
      return
    }

    root.openingPending = true
    targetCapture.output = ""
    if (!targetCapture.running) {
      targetCapture.command = ["hyprctl", "-j", "activewindow"]
      targetCapture.running = true
    }
  }

  function finishOpen() {
    if (root.openingPending) root.openingPending = false
    root.opened = true
    root.selectedIndex = 0
    root.cursorActive = false
    root.rebuildDisplay(false)
    root.disarmPointer()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function close() {
    root.openingPending = false
    root.opened = false
    root.filterText = ""
    root.capturedTarget = ({})
    root.selectedIndex = 0
    root.cursorActive = false
    root.transportDetailsPinned = false
    root.webhookSetupOpen = false
    root.webhookGuideOpen = false
    root.webhookBusy = false
    root.tokenRotationArmed = false
    root.webhookNotice = ""
    displayModel.clear()
    targetCapture.output = ""
    if (targetCapture.running)
      targetCapture.running = false
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide(root.pluginId())
  }

  function toggle() {
    if (root.opened || root.openingPending) root.dismiss()
    else root.open("{}")
  }

  function webhookState() {
    if (!root.service || !root.service.webhookSetup
        || typeof root.service.webhookSetup !== "object") return ({})
    return root.service.webhookSetup
  }

  function webhookStatusLabel() {
    var value = root.webhookState()
    if (value.running === true) return "Ready"
    if (value.configured === true && value.enabled === true) return "Not responding"
    if (value.configured === true) return "Disabled"
    if (value.configuration_present === true) return "Needs attention"
    return "Not configured"
  }

  function openWebhookSetup(showGuide) {
    root.transportDetailsPinned = false
    root.webhookSetupOpen = true
    root.webhookGuideOpen = showGuide === true
    root.tokenRotationArmed = false
    root.webhookNotice = ""
    if (root.service && typeof root.service.requestWebhookStatus === "function") {
      var requestId = root.service.requestWebhookStatus()
      root.webhookBusy = requestId >= 0
    }
    Qt.callLater(function() {
      webhookSetupFlickable.contentY = root.webhookGuideOpen ? root.guideScrollPosition : 0
      if (root.webhookGuideOpen)
        webhookBackButton.forceActiveFocus()
      else if (root.webhookState().configured === true)
        copyEndpointButton.forceActiveFocus()
      else
        configureWebhookButton.forceActiveFocus()
    })
  }

  function closeWebhookSetup() {
    if (root.webhookGuideOpen) root.guideScrollPosition = webhookSetupFlickable.contentY
    root.webhookSetupOpen = false
    root.webhookGuideOpen = false
    root.webhookBusy = false
    root.tokenRotationArmed = false
    root.webhookNotice = ""
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function showWebhookGuide() {
    root.webhookGuideOpen = true
    root.webhookNotice = ""
    Qt.callLater(function() {
      webhookSetupFlickable.contentY = root.guideScrollPosition
      webhookBackButton.forceActiveFocus()
    })
  }

  function showWebhookConnection() {
    root.guideScrollPosition = webhookSetupFlickable.contentY
    root.webhookGuideOpen = false
    root.webhookNotice = ""
    webhookSetupFlickable.contentY = 0
    Qt.callLater(function() {
      if (root.webhookState().configured === true)
        copyEndpointButton.forceActiveFocus()
      else if (configureWebhookButton.enabled)
        configureWebhookButton.forceActiveFocus()
      else
        webhookBackButton.forceActiveFocus()
    })
  }

  function jumpToGuideSection(index) {
    var sections = [guidePreparation, guideShortcut, guideRequest, guideAutomation]
    var target = sections[index]
    if (!target) return
    webhookSetupFlickable.contentY = Math.max(0, Math.min(
      target.mapToItem(webhookSetupContent, 0, 0).y,
      webhookSetupFlickable.contentHeight - webhookSetupFlickable.height))
    root.guideScrollPosition = webhookSetupFlickable.contentY
  }

  function currentGuideSection() {
    var sections = [guidePreparation, guideShortcut, guideRequest, guideAutomation]
    var position = webhookSetupFlickable.contentY + Style.spacing.huge + 2
    for (var index = sections.length - 1; index > 0; index--) {
      if (sections[index].mapToItem(webhookSetupContent, 0, 0).y <= position) return index
    }
    return 0
  }

  function copyGuideField(fieldId) {
    root.copiedField = ""
    root.pendingCopyField = fieldId
    root.fieldCopyRequest = root.service.copyWebhookSetupField(fieldId)
    root.beginWebhookRequest(root.fieldCopyRequest)
    if (root.fieldCopyRequest >= 0) root.webhookNotice = ""
  }

  function connectionsText() {
    if (root.statusState() !== "ready") return root.statusState() === "reconnecting"
      ? "Reconnecting…" : "Connections"
    var count = root.activeTransportCount(root.service ? root.service.status : null)
    return count > 0 ? count + " connected" : "Set up connections"
  }

  function beginWebhookRequest(requestId) {
    if (requestId >= 0) {
      root.webhookBusy = true
      root.webhookNotice = "Working…"
    } else {
      root.webhookNotice = "The local bridge is unavailable."
    }
  }

  function beginSourceToggle(sourceId, requestId) {
    if (requestId < 0) {
      root.sourceNotice = "The local bridge is unavailable."
      return
    }
    var next = ({})
    for (var key in root.sourceToggleBusy) next[key] = root.sourceToggleBusy[key]
    next[String(sourceId)] = true
    root.sourceToggleBusy = next
    root.sourceNotice = ""
  }

  function toggleSource(entry) {
    if (!entry || !root.service) return
    var id = String(entry.id || "")
    if (!id || root.sourceToggleBusy[id] === true) return
    var enable = entry.enabled !== true
    if (id === "webhook") {
      // The webhook is a systemd unit with its own manager; an unconfigured
      // one answers "Set up the phone webhook first" through the notice.
      if (typeof root.service.setWebhookEnabled !== "function") return
      root.beginSourceToggle(id, root.service.setWebhookEnabled(enable))
      return
    }
    if (typeof root.service.setSourceEnabled !== "function") return
    root.beginSourceToggle(id, root.service.setSourceEnabled(id, enable))
  }

  function statusState() {
    if (!root.service) return "unavailable"
    if (root.service.bridgeAlive !== true) return "reconnecting"
    var value = root.service.status
    if (!value || typeof value !== "object") return root.service.ready ? "ready" : "starting"
    return String(value.state || value.status || (root.service.ready ? "ready" : "starting")).toLowerCase()
  }

  function sourceIsActiveTransport(name, source) {
    var sourceName = String(name || "").toLowerCase()
    if (!sourceName || sourceName === "manual") return false
    if (!source || typeof source !== "object") return false
    if (source.enabled === false || source.available === false) return false

    // A connected transport is usable now. For transports without a
    // connection concept (such as the webhook), enabled + running is enough.
    if (source.connected !== undefined) return source.connected === true
    return source.running === true
  }

  function transportDisplayName(name) {
    var sourceName = String(name || "").toLowerCase()
    if (sourceName === "blueferry") return "BlueFerry"
    if (sourceName === "blip") return "Blip (iMessage)"
    if (sourceName === "tether") return "Tether"
    if (sourceName === "webhook") return "Phone webhook"
    if (sourceName === "kdeconnect") return "KDE Connect"

    var words = sourceName.replace(/[_-]+/g, " ").trim().split(/\s+/)
    var display = []
    for (var index = 0; index < words.length; index++) {
      if (!words[index]) continue
      display.push(words[index].charAt(0).toUpperCase() + words[index].substring(1))
    }
    return display.join(" ").substring(0, 32) || "Other transport"
  }

  function transportHealthLabel(source) {
    if (!source || typeof source !== "object" || source.available === false)
      return "Unavailable"
    if (source.enabled === false) return "Disabled"
    if (root.sourceIsActiveTransport("transport", source))
      return source.connected === true ? "Connected" : "Ready"

    var detail = String(source.detail || "").toLowerCase()
    if (detail === "not responding") return "Not responding"
    if (detail === "starting") return "Starting"
    if (detail === "checking receive events") return "Checking messages"
    if (detail === "receive events unavailable") return "SMS unavailable"
    if (detail === "reconnecting") return "Reconnecting"
    if (detail === "authorization-required") return "Authorization needed"
    if (detail === "map-connection-refused") return "Messages unavailable"
    if (detail === "status unavailable") return "Status unavailable"
    if (detail === "not installed") return "Not installed"
    if (detail === "not configured") return "Not configured"
    if (detail === "hook not configured") return "Hook not configured"
    if (detail === "subscribing" || detail === "connecting") return "Connecting"
    if (detail === "daemon unavailable" || detail === "daemon connection lost")
      return "Daemon offline"
    if (detail === "phone not connected") return "Phone not connected"
    if (detail === "history unavailable" || detail === "degraded") return "Degraded"
    if (detail === "could not start" || detail === "message processing failed")
      return "Error"
    if (source.running === true && source.connected === false) return "Disconnected"
    if (source.enabled === true) return "Not running"
    return "Inactive"
  }

  function transportHealthTone(entry) {
    if (entry && entry.active === true) return "active"
    var health = entry ? String(entry.health || "") : ""
    if (health === "Not responding" || health === "SMS unavailable"
        || health === "Messages unavailable" || health === "Status unavailable"
        || health === "Error" || health === "Degraded" || health === "Daemon offline")
      return "error"
    if (health === "Starting" || health === "Checking messages"
        || health === "Reconnecting" || health === "Authorization needed"
        || health === "Connecting")
      return "pending"
    return "inactive"
  }

  function transportEntries(statusValue) {
    if (!statusValue || !statusValue.sources) return []
    var sources = statusValue.sources
    var entries = []

    function appendEntry(name, source) {
      var sourceName = String(name || "").toLowerCase()
      if (!sourceName || sourceName === "manual" || !source
          || typeof source !== "object") return
      entries.push({
        id: sourceName,
        name: root.transportDisplayName(sourceName),
        active: root.sourceIsActiveTransport(sourceName, source),
        health: root.transportHealthLabel(source),
        enabled: source.enabled === true,
        // Every transport that reports an enabled flag can be switched here.
        toggleable: source.enabled === true || source.enabled === false
      })
    }

    if (Array.isArray(sources)) {
      for (var index = 0; index < sources.length; index++) {
        var source = sources[index]
        var sourceName = source && typeof source === "object"
          ? (source.name || source.id || "") : ""
        appendEntry(sourceName, source)
      }
    } else if (typeof sources === "object") {
      var names = Object.keys(sources)
      for (var keyIndex = 0; keyIndex < names.length; keyIndex++)
        appendEntry(names[keyIndex], sources[names[keyIndex]])
    }

    var order = { blueferry: 0, blip: 1, tether: 2, webhook: 3, kdeconnect: 4 }
    entries.sort(function(left, right) {
      var leftOrder = order[left.id] !== undefined ? order[left.id] : 100
      var rightOrder = order[right.id] !== undefined ? order[right.id] : 100
      if (leftOrder !== rightOrder) return leftOrder - rightOrder
      return left.name.localeCompare(right.name)
    })
    return entries
  }

  function activeTransportCount(statusValue) {
    var entries = root.transportEntries(statusValue)
    var count = 0
    for (var index = 0; index < entries.length; index++) {
      if (entries[index].active) count++
    }
    return count
  }

  function activeTransportSummary(statusValue) {
    var entries = root.transportEntries(statusValue)
    var names = []
    for (var index = 0; index < entries.length; index++) {
      if (entries[index].active) names.push(entries[index].name)
    }
    if (names.length === 0) return "Active transports: none"
    return (names.length === 1 ? "Active transport: " : "Active transports: ")
      + names.join(", ")
  }

  function statusText() {
    if (!root.service) return "Service unavailable"
    if (root.service.bridgeAlive !== true)
      return String(root.service.lastError || "Connecting to local bridge…")

    var state = root.statusState()
    var label = state === "ready" || state === "ok" || state === "running"
      ? "Ready"
      : (state === "starting" ? "Loading" : state.charAt(0).toUpperCase() + state.substring(1))
    var statusValue = root.service.status
    var transportCount = root.activeTransportCount(statusValue)
    var codeCount = root.service.records && Array.isArray(root.service.records)
      ? root.service.records.length : 0
    var parts = [label]
    parts.push(transportCount + (transportCount === 1
      ? " active transport" : " active transports"))
    parts.push(codeCount + (codeCount === 1 ? " code" : " codes"))
    return parts.join("  ·  ")
  }

  function statusColor() {
    var state = root.statusState()
    if (state === "ready" || state === "ok" || state === "running") return Color.accent
    if (state === "error" || state === "failed" || state === "unavailable") return Color.urgent
    return root.foreground
  }

  function ageLabel(milliseconds) {
    root.clockRevision
    var stamp = Number(milliseconds || 0)
    if (stamp <= 0) return "time unknown"
    var seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000))
    if (seconds < 60) return "just now"
    var minutes = Math.floor(seconds / 60)
    if (minutes < 60) return minutes + "m ago"
    var hours = Math.floor(minutes / 60)
    if (hours < 24) return hours + "h ago"
    var days = Math.floor(hours / 24)
    if (days < 7) return days + "d ago"
    return Qt.formatDateTime(new Date(stamp), "MMM d, h:mm AP")
  }

  function expiryLabel(milliseconds) {
    root.clockRevision
    var stamp = Number(milliseconds || 0)
    if (stamp <= 0) return ""
    var seconds = Math.floor((stamp - Date.now()) / 1000)
    if (seconds <= 0) return "expired"
    if (seconds < 60) return "expires in <1m"
    var minutes = Math.ceil(seconds / 60)
    if (minutes < 60) return "expires in " + minutes + "m"
    return "expires in " + Math.ceil(minutes / 60) + "h"
  }

  function confidenceLabel(value) {
    var confidence = Number(value)
    if (!isFinite(confidence) || confidence < 0) return ""
    if (confidence <= 1) confidence *= 100
    confidence = Math.max(0, Math.min(100, Math.round(confidence)))
    return confidence + "% match"
  }

  function recordSearchText(record) {
    return [record.code, record.service, record.source]
      .join(" ").toLowerCase()
  }

  function rebuildDisplay(keepSelection) {
    if (!root.opened && !root.openingPending) {
      displayModel.clear()
      root.selectedIndex = 0
      root.cursorActive = false
      return
    }

    var keepCursor = keepSelection === true && root.cursorActive
    var selectedId = ""
    if (keepCursor && displayModel.count > 0
        && root.selectedIndex >= 0 && root.selectedIndex < displayModel.count)
      selectedId = String(displayModel.get(root.selectedIndex).recordId)

    var values = root.service && Array.isArray(root.service.records)
      ? root.service.records.slice() : []
    values.sort(function(a, b) {
      var aTime = Number(a.received_ms || root.timestampMs(a.received_at))
      var bTime = Number(b.received_ms || root.timestampMs(b.received_at))
      var delta = bTime - aTime
      return delta !== 0 ? delta : String(b.id || "").localeCompare(String(a.id || ""))
    })

    var needle = root.filterText.trim().toLowerCase()
    displayModel.clear()
    var nextSelected = -1
    for (var i = 0; i < values.length; i++) {
      var record = values[i]
      if (!record || record.id === undefined || record.code === undefined) continue
      if (needle && root.recordSearchText(record).indexOf(needle) === -1) continue

      var receivedMs = Number(record.received_ms || root.timestampMs(record.received_at))
      var expiresMs = Number(record.expires_ms || root.timestampMs(record.expires_at))
      var sourceName = String(record.source || "SMS")
      var details = [sourceName, root.ageLabel(receivedMs)]
      var expiry = root.expiryLabel(expiresMs)
      var confidence = root.confidenceLabel(record.confidence)
      if (expiry) details.push(expiry)
      if (confidence) details.push(confidence)

      var rowIndex = displayModel.count
      displayModel.append({
        recordId: String(record.id),
        codeText: String(record.code),
        serviceName: String(record.service || "Unknown service"),
        sourceName: sourceName,
        receivedMs: receivedMs,
        expiresMs: expiresMs,
        confidence: Number(record.confidence),
        detailText: details.join("  ·  ")
      })
      if (selectedId && String(record.id) === selectedId) nextSelected = rowIndex
    }

    if (displayModel.count === 0) {
      root.selectedIndex = 0
      root.cursorActive = false
    } else {
      root.selectedIndex = nextSelected >= 0 ? nextSelected : 0
      root.cursorActive = keepCursor
      Qt.callLater(function() {
        resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
      })
    }
  }

  function setFilter(nextFilter) {
    root.filterText = String(nextFilter || "")
    root.selectedIndex = 0
    root.cursorActive = false
    root.disarmPointer()
    root.rebuildDisplay(false)
  }

  function select(delta) {
    if (displayModel.count === 0) return
    root.disarmPointer()
    if (root.cursorActive && delta === -1 && root.selectedIndex === 0) {
      root.cursorActive = false
      resultList.positionViewAtIndex(0, ListView.Contain)
      return
    }
    if (!root.cursorActive) {
      root.cursorActive = true
      root.selectedIndex = delta < 0 ? displayModel.count - 1 : 0
    } else {
      root.selectedIndex = (root.selectedIndex + delta + displayModel.count)
        % displayModel.count
    }
    resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
  }

  function selectAbsolute(index) {
    if (displayModel.count === 0) return
    root.disarmPointer()
    root.cursorActive = true
    root.selectedIndex = Math.max(0, Math.min(index, displayModel.count - 1))
    resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
  }

  function disarmPointer() {
    pointerGate.reset()
  }

  function selectFromPointer(index, item, mouse) {
    if (!pointerGate.moved(item, mouse)) return
    root.cursorActive = true
    root.selectedIndex = index
  }

  function activateIndex(index, paste) {
    if (index < 0 || index >= displayModel.count || !root.service) return
    if (root.service.bridgeAlive !== true || typeof root.service.activate !== "function") return
    var recordId = String(displayModel.get(index).recordId)
    var target = root.capturedTarget
    root.dismiss()
    root.service.activate(recordId, paste === true, target)
  }

  function deleteIndex(index) {
    if (index < 0 || index >= displayModel.count || !root.service
        || typeof root.service.deleteRecord !== "function") return
    var row = displayModel.get(index)
    root.service.deleteRecord(row.recordId)
    root.disarmPointer()
    // Service applies deletion optimistically; its recordsChanged signal
    // rebuilds the list and clamps the cursor.
  }

  function handlePickerKey(event, allowTransportFocus) {
    if (root.webhookSetupOpen) {
      if (event.key === Qt.Key_Escape)
        root.closeWebhookSetup()
      else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab)
        webhookBackButton.forceActiveFocus()
      event.accepted = true
    } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
      if (allowTransportFocus)
        transportStatusTrigger.forceActiveFocus()
      else
        keyCatcher.forceActiveFocus()
      event.accepted = true
    } else if (event.key === Qt.Key_Escape) {
      // Escape always closes and never asks the service to activate.
      root.dismiss()
      event.accepted = true
    } else if (Util.editsFilter(event, root.filterText)) {
      root.setFilter(Util.editedFilter(event, root.filterText))
      event.accepted = true
    } else if (event.key === Qt.Key_Delete) {
      if (root.cursorActive) root.deleteIndex(root.selectedIndex)
      event.accepted = true
    } else if (event.key === Qt.Key_Up) {
      root.select(-1)
      event.accepted = true
    } else if (event.key === Qt.Key_Down) {
      root.select(1)
      event.accepted = true
    } else if (event.key === Qt.Key_PageUp) {
      root.select(-6)
      event.accepted = true
    } else if (event.key === Qt.Key_PageDown) {
      root.select(6)
      event.accepted = true
    } else if (event.key === Qt.Key_Home) {
      root.selectAbsolute(0)
      event.accepted = true
    } else if (event.key === Qt.Key_End) {
      root.selectAbsolute(displayModel.count - 1)
      event.accepted = true
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      root.activateIndex(root.selectedIndex,
        !(event.modifiers & Qt.ShiftModifier))
      event.accepted = true
    } else if (event.text && event.text.length === 1
        && event.text.charCodeAt(0) >= 32
        && event.text.charCodeAt(0) !== 127) {
      root.setFilter(root.filterText + event.text)
      event.accepted = true
    }
  }

  onServiceChanged: root.rebuildDisplay(false)

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onRecordsChanged() { root.rebuildDisplay(true) }
    function onRequestFinished(requestId, method, ok, message) {
      if (method === "webhook_copy_setup_field" && requestId === root.fieldCopyRequest) {
        root.copiedField = ok ? root.pendingCopyField : ""
        root.fieldCopyRequest = -1
        if (ok) copyFeedbackTimer.restart()
      }
      if (method === "source_set_enabled" || method === "webhook_set_enabled") {
        root.sourceToggleBusy = ({})
        root.sourceNotice = ok ? "" : String(message || "The source could not be changed.")
      }
      if (method === "source_set_enabled") return
      if (String(method).indexOf("webhook_") !== 0) return
      root.webhookBusy = false
      if (!ok) {
        root.webhookNotice = String(message || "The webhook request failed.")
        return
      }
      if (method === "webhook_configure_tailscale") {
        root.webhookNotice = "Webhook ready. Follow the guided iPhone steps below."
        root.webhookGuideOpen = true
        webhookSetupFlickable.contentY = 0
      } else if (method === "webhook_copy_endpoint")
        root.webhookNotice = "Webhook URL copied for 60 seconds."
      else if (method === "webhook_copy_token")
        root.webhookNotice = "Raw bearer token copied securely for 60 seconds."
      else if (method === "webhook_copy_setup_field")
        root.webhookNotice = ""
      else if (method === "webhook_set_enabled")
        root.webhookNotice = root.webhookState().enabled
          ? "Phone webhook enabled." : "Phone webhook disabled."
      else if (method === "webhook_rotate_token") {
        root.tokenRotationArmed = false
        root.webhookNotice = "Token rotated. Copy the new Header 1 value to your phone."
      } else if (method === "webhook_status") {
        root.webhookNotice = ""
      }
      if (root.webhookSetupOpen
          && (method === "webhook_status"
            || method === "webhook_configure_tailscale")) {
        Qt.callLater(function() {
          if (root.webhookGuideOpen)
            webhookBackButton.forceActiveFocus()
          else if (root.webhookState().configured === true)
            copyEndpointButton.forceActiveFocus()
          else if (configureWebhookButton.enabled)
            configureWebhookButton.forceActiveFocus()
          else
            webhookBackButton.forceActiveFocus()
        })
      }
    }
  }

  Timer {
    id: copyFeedbackTimer
    interval: 2200
    onTriggered: root.copiedField = ""
  }

  ListModel { id: displayModel }

  PointerMoveGate {
    id: pointerGate
    referenceItem: card
  }

  Process {
    id: targetCapture
    property string output: ""
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (root.openingPending) targetCapture.output = text
      }
    }
    onExited: function(exitCode) {
      if (!root.openingPending) return
      if (exitCode === 0)
        root.capturedTarget = root.sanitizeTarget(root.parseJson(output, {}))
      targetCapture.output = ""
      root.finishOpen()
    }
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    onTriggered: {
      root.clockRevision++
      root.rebuildDisplay(true)
    }
  }

  component ShortcutHint: Item {
    id: shortcutHint

    required property string shortcutText
    required property string actionText
    required property string hookName

    objectName: hookName
    Accessible.name: shortcutText + ", " + actionText
    implicitWidth: shortcutKey.width + Style.spacing.sm + shortcutAction.implicitWidth
    implicitHeight: root.footerLineHeight
    width: implicitWidth
    height: implicitHeight

    Rectangle {
      id: shortcutKey
      objectName: shortcutHint.hookName + "Key"
      width: shortcutKeyLabel.implicitWidth + Style.spacing.sm * 2
      height: Math.max(Style.space(20),
        shortcutKeyLabel.implicitHeight + Style.spacing.xs * 2)
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      radius: Math.min(root.cornerRadius, Style.space(5))
      color: Util.alpha(root.foreground, 0.06)
      border.width: Math.max(1, Style.normalBorderWidth)
      border.color: Util.alpha(root.foreground, 0.26)

      Text {
        id: shortcutKeyLabel
        objectName: shortcutHint.hookName + "KeyLabel"
        anchors.centerIn: parent
        text: shortcutHint.shortcutText
        textFormat: Text.PlainText
        color: root.foreground
        opacity: 0.80
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.weight: Font.DemiBold
      }
    }

    Text {
      id: shortcutAction
      objectName: shortcutHint.hookName + "Action"
      anchors.left: shortcutKey.right
      anchors.leftMargin: Style.spacing.sm
      anchors.verticalCenter: parent.verticalCenter
      text: shortcutHint.actionText
      textFormat: Text.PlainText
      color: root.foreground
      opacity: 0.68
      font.family: root.uiFontFamily
      font.pixelSize: Style.font.caption
    }
  }

  component SetupButton: Rectangle {
    id: setupButton

    required property string label
    property bool emphasized: false
    property string fontFamily: root.uiFontFamily
    property int fontSize: Style.font.bodySmall
    signal triggered()

    implicitHeight: Math.max(Style.space(38), Style.spacing.controlHeight)
    radius: root.cornerRadius
    color: setupButton.emphasized
      ? root.selectedBackground
      : Util.alpha(root.foreground,
          setupMouse.containsMouse || setupButton.activeFocus ? 0.10 : 0.055)
    border.width: setupButton.activeFocus ? Math.max(1, Style.normalBorderWidth) : 0
    border.color: Util.alpha(root.foreground, 0.42)
    opacity: setupButton.enabled ? 1 : 0.42
    activeFocusOnTab: setupButton.enabled

    Accessible.role: Accessible.Button
    Accessible.name: setupButton.label
    Accessible.focusable: setupButton.enabled
    Accessible.focused: setupButton.activeFocus
    Accessible.onPressAction: if (setupButton.enabled) setupButton.triggered()

    Text {
      anchors.centerIn: parent
      text: setupButton.label
      textFormat: Text.PlainText
      color: setupButton.emphasized ? root.selectedText : root.foreground
      font.family: setupButton.fontFamily
      font.pixelSize: setupButton.fontSize
      font.weight: Font.DemiBold
    }

    MouseArea {
      id: setupMouse
      anchors.fill: parent
      enabled: setupButton.enabled
      hoverEnabled: true
      cursorShape: setupButton.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      onClicked: setupButton.triggered()
    }

    Keys.onPressed: function(event) {
      if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
          || event.key === Qt.Key_Space) {
        if (setupButton.enabled) setupButton.triggered()
        event.accepted = true
      }
    }
  }

  component GuideFieldPair: Grid {
    columns: width >= Style.space(540) ? 2 : 1
    spacing: Style.spacing.sm
    readonly property real cellWidth: (width - spacing * (columns - 1)) / columns
  }

  component CopyFieldRow: Rectangle {
    id: copyField

    required property string fieldId
    required property string fieldLabel
    required property string fieldValue
    property string note: ""
    property bool copyable: true
    property bool requiresWebhook: false

    implicitHeight: Math.max(copyFieldText.implicitHeight,
      copyField.copyable ? copyFieldButton.implicitHeight : 0) + Style.spacing.sm * 2
    radius: root.cornerRadius
    color: copyField.fieldId === "shortcut_input" ? Util.alpha(Color.accent, 0.12)
      : copyField.copyable ? Util.alpha(root.foreground, 0.045) : "transparent"
    border.width: copyField.copyable || copyField.fieldId === "shortcut_input"
      ? Math.max(1, Style.normalBorderWidth) : 0
    border.color: Util.alpha(root.border, 0.28)

    Column {
      id: copyFieldText
      anchors.left: parent.left
      anchors.right: copyField.copyable ? copyFieldButton.left : parent.right
      anchors.leftMargin: Style.spacing.md
      anchors.rightMargin: Style.spacing.md
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.spacing.xs

      Text {
        objectName: "copyShortcutFieldLabel-" + copyField.fieldId
        width: parent.width
        text: copyField.copyable || copyField.fieldId === "shortcut_input"
          ? copyField.fieldLabel : copyField.fieldLabel + "  →  " + copyField.fieldValue
        textFormat: Text.PlainText
        color: root.foreground
        opacity: 0.68
        font.family: root.guideFontFamily
        font.pixelSize: Style.font.bodySmall
        font.weight: Font.DemiBold
        wrapMode: Text.WordWrap
      }

      Rectangle {
        visible: copyField.fieldId === "shortcut_input"
        width: Math.min(parent.width, variableLabel.implicitWidth + Style.spacing.md * 2)
        height: variableLabel.implicitHeight + Style.spacing.sm * 2
        color: Util.alpha(Color.accent, 0.20)
        radius: Style.space(4)
        Text {
          id: variableLabel
          anchors.centerIn: parent
          text: "↳ Shortcut Input"
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.uiFontFamily
          font.pixelSize: Style.font.body
          font.weight: Font.DemiBold
        }
      }

      Text {
        objectName: "copyShortcutFieldValue-" + copyField.fieldId
        visible: copyField.copyable
        width: parent.width
        text: copyField.fieldValue
        textFormat: Text.PlainText
        color: root.foreground
        opacity: 0.94
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.weight: Font.Medium
        wrapMode: Text.WrapAnywhere
      }

      Text {
        width: parent.width
        visible: copyField.note.length > 0
        text: copyField.note
        textFormat: Text.PlainText
        color: root.foreground
        opacity: 0.62
        font.family: root.guideFontFamily
        font.pixelSize: Style.font.bodySmall
        lineHeightMode: Text.ProportionalHeight
        lineHeight: 1.25
        wrapMode: Text.WordWrap
      }
    }

    SetupButton {
      id: copyFieldButton
      objectName: "copyShortcutFieldButton-" + copyField.fieldId
      anchors.right: parent.right
      anchors.rightMargin: Style.spacing.sm
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(76)
      fontFamily: root.guideFontFamily
      fontSize: Style.font.bodySmall
      visible: copyField.copyable
      enabled: copyField.copyable
        && !root.webhookBusy
        && root.service
        && typeof root.service.copyWebhookSetupField === "function"
        && (!copyField.requiresWebhook
          || root.webhookState().configured === true)
      label: root.copiedField === copyField.fieldId ? "Copied ✓" : "Copy"
      onTriggered: root.copyGuideField(copyField.fieldId)
    }
  }

  component GuideScreenshot: Column {
    id: guideScreenshot

    required property string caption
    required property url imageSource
    required property real aspectRatio
    property bool expanded: false
    property real maxImageWidth: expanded ? width : root.guideScreenshotWidth

    spacing: Style.spacing.lg

    Text {
      objectName: guideScreenshot.objectName + "-caption"
      anchors.horizontalCenter: parent.horizontalCenter
      width: Math.min(parent.width, guideScreenshot.maxImageWidth)
      text: guideScreenshot.caption
      textFormat: Text.PlainText
      color: root.foreground
      opacity: 0.68
      font.family: root.guideFontFamily
      font.pixelSize: root.guideCaptionFontSize
      lineHeightMode: Text.ProportionalHeight
      lineHeight: 1.30
      wrapMode: Text.WordWrap
    }

    Item {
      width: parent.width
      height: guideImageFrame.height

      Rectangle {
        id: guideImageFrame
        objectName: guideScreenshot.objectName + "-frame"
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(parent.width, guideScreenshot.maxImageWidth)
        height: guideScreenshot.expanded
          ? width / Math.max(0.01, guideScreenshot.aspectRatio)
          : Math.min(Style.space(250), width / Math.max(0.01, guideScreenshot.aspectRatio))
        radius: root.cornerRadius
        color: "black"
        border.width: Math.max(1, Style.normalBorderWidth)
        border.color: Util.alpha(root.border, 0.40)
        clip: true

        Image {
          objectName: guideScreenshot.objectName + "-content"
          anchors.fill: parent
          source: guideScreenshot.imageSource
          fillMode: Image.PreserveAspectFit
          asynchronous: false
          cache: true
          smooth: true
        }
        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: guideScreenshot.expanded = !guideScreenshot.expanded
        }
      }
    }
    SetupButton {
      objectName: guideScreenshot.objectName + "-expand"
      anchors.horizontalCenter: parent.horizontalCenter
      width: Style.space(180)
      label: guideScreenshot.expanded ? "Collapse image" : "Enlarge image"
      onTriggered: guideScreenshot.expanded = !guideScreenshot.expanded
    }
  }

  component GuideBodyText: Text {
    textFormat: Text.PlainText
    color: root.foreground
    opacity: 0.74
    font.family: root.guideFontFamily
    font.pixelSize: root.guideBodyFontSize
    lineHeightMode: Text.ProportionalHeight
    lineHeight: 1.35
    wrapMode: Text.WordWrap
  }

  component GuideSectionHeading: Text {
    topPadding: Style.spacing.huge
    bottomPadding: Style.spacing.sm
    textFormat: Text.PlainText
    color: root.foreground
    opacity: 0.96
    font.family: root.guideFontFamily
    font.pixelSize: root.guideHeadingFontSize
    font.weight: Font.DemiBold
    wrapMode: Text.WordWrap
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "oma2fa-picker"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      objectName: "pickerCard"
      width: root.webhookGuideOpen ? root.guideCardWidth : root.cardWidth
      height: root.webhookGuideOpen ? root.guideCardHeight : root.cardHeight
      radius: root.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: root.contentMargin

      MouseArea {
        anchors.fill: parent
        onClicked: root.transportDetailsPinned = false
      }

      Item {
        id: keyCatcher
        objectName: "pickerKeyCatcher"
        anchors.fill: parent
        focus: true
        activeFocusOnTab: true
        z: 5

        KeyNavigation.tab: transportStatusTrigger
        KeyNavigation.backtab: transportStatusTrigger
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) { root.handlePickerKey(event, true) }
        Keys.onTabPressed: function(event) {
          transportStatusTrigger.forceActiveFocus()
          event.accepted = true
        }
        Keys.onBacktabPressed: function(event) {
          transportStatusTrigger.forceActiveFocus()
          event.accepted = true
        }
      }

      Column {
        z: 6
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: root.contentSpacing

        Item {
          id: titleBar
          width: parent.width
          height: root.titleHeight
          z: 10

          Text {
            id: titleLabel
            objectName: "pickerTitleLabel"
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Oma2FA"
            textFormat: Text.PlainText
            color: root.foreground
            font.family: root.uiFontFamily
            font.pixelSize: Style.font.heading
            font.weight: Font.DemiBold
          }

          Item {
            id: transportStatusTrigger
            objectName: "transportStatusTrigger"
            anchors.right: parent.right
            anchors.top: parent.top
            width: Math.max(0, Math.min(
              parent.width - titleLabel.x - titleLabel.width - Style.spacing.lg,
              Math.max(Style.space(140), Math.min(Style.space(360),
                transportStatusDot.width + transportStatusText.implicitWidth
                  + transportStatusChevron.implicitWidth
                  + Style.spacing.sm * 4))))
            height: parent.height
            activeFocusOnTab: true
            KeyNavigation.tab: keyCatcher
            KeyNavigation.backtab: keyCatcher

            Accessible.role: Accessible.Button
            Accessible.focusable: true
            Accessible.focused: transportStatusTrigger.activeFocus
            Accessible.name: root.activeTransportSummary(
              root.service ? root.service.status : null)
            Accessible.description: root.transportDetailsPinned
              ? "Transport details shown. Press to hide."
              : "Transport details hidden. Press to show."
            Accessible.onPressAction: root.transportDetailsPinned = !root.transportDetailsPinned

            Keys.priority: Keys.BeforeItem
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                  || event.key === Qt.Key_Space) {
                root.transportDetailsPinned = !root.transportDetailsPinned
                event.accepted = true
              } else {
                keyCatcher.forceActiveFocus()
                root.handlePickerKey(event, false)
              }
            }
            Keys.onTabPressed: function(event) {
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }
            Keys.onBacktabPressed: function(event) {
              keyCatcher.forceActiveFocus()
              event.accepted = true
            }

            Rectangle {
              anchors.fill: parent
              anchors.topMargin: Style.spacing.xs
              anchors.bottomMargin: Style.spacing.xs
              radius: Math.min(root.cornerRadius, Style.space(6))
              color: Util.alpha(root.foreground,
                transportStatusMouse.containsMouse || root.transportDetailsPinned
                  || transportStatusTrigger.activeFocus ? 0.10 : 0.045)
              border.width: root.transportDetailsPinned
                || transportStatusTrigger.activeFocus ? Style.normalBorderWidth : 0
              border.color: Util.alpha(root.foreground, 0.18)
            }

            Row {
              id: transportStatusSummary
              anchors.left: parent.left
              anchors.leftMargin: Style.spacing.sm
              anchors.right: parent.right
              anchors.rightMargin: Style.spacing.sm
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.sm

              Rectangle {
                id: transportStatusDot
                width: Style.space(7)
                height: width
                radius: width / 2
                anchors.verticalCenter: parent.verticalCenter
                color: root.statusColor()
                opacity: root.service && root.service.bridgeAlive === true ? 0.9 : 0.55
              }

              Text {
                id: transportStatusText
                objectName: "transportStatusText"
                width: Math.max(0, transportStatusSummary.width
                  - transportStatusDot.width - transportStatusChevron.width
                  - Style.spacing.sm * 2)
                text: root.connectionsText()
                textFormat: Text.PlainText
                color: root.foreground
                opacity: 0.85
                font.family: root.uiFontFamily
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideLeft
              }

              Text {
                id: transportStatusChevron
                objectName: "transportStatusChevron"
                anchors.verticalCenter: parent.verticalCenter
                text: root.transportDetailsPinned ? "▴" : "▾"
                textFormat: Text.PlainText
                color: root.foreground
                opacity: 0.42
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            MouseArea {
              id: transportStatusMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: function(mouse) {
                root.transportDetailsPinned = !root.transportDetailsPinned
                mouse.accepted = true
              }
            }

            Rectangle {
              id: transportStatusPopover
              objectName: "transportStatusPopover"
              anchors.top: parent.bottom
              anchors.right: parent.right
              width: Math.min(Style.space(280), titleBar.width)
              height: transportDetailsColumn.implicitHeight + Style.spacing.md * 2
              visible: root.transportDetailsPinned || transportStatusMouse.containsMouse
                || transportPopoverMouse.containsMouse || transportPopoverHover.hovered
              color: root.background
              border.width: Style.normalBorderWidth
              border.color: Util.alpha(root.foreground, 0.20)
              radius: root.cornerRadius
              z: 20

              // A passive hover handler remains active over child controls.
              // MouseArea.containsMouse alone becomes false when the setup
              // button's own MouseArea takes the pointer.
              HoverHandler {
                id: transportPopoverHover
                objectName: "transportPopoverHover"
              }

              MouseArea {
                id: transportPopoverMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.ArrowCursor
                onClicked: function(mouse) {
                  root.transportDetailsPinned = true
                  mouse.accepted = true
                }
              }

              Column {
                id: transportDetailsColumn
                anchors.fill: parent
                anchors.margins: Style.spacing.md
                spacing: Style.spacing.sm

                Text {
                  width: parent.width
                  text: "TRANSPORTS"
                  textFormat: Text.PlainText
                  color: root.foreground
                  opacity: 0.44
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.weight: Font.DemiBold
                }

                Text {
                  width: parent.width
                  visible: root.transportEntries(
                    root.service ? root.service.status : null).length === 0
                  text: "No configured transports"
                  textFormat: Text.PlainText
                  color: root.foreground
                  opacity: 0.52
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Repeater {
                  model: root.transportEntries(root.service ? root.service.status : null)

                  delegate: Item {
                    id: transportRow
                    required property var modelData

                    objectName: "transportRow-" + String(modelData.id)
                    width: transportDetailsColumn.width
                    height: Math.max(Style.space(24), transportName.implicitHeight)
                    opacity: modelData.active ? 1 : 0.68

                    Rectangle {
                      id: transportHealthDot
                      width: Style.space(7)
                      height: width
                      radius: width / 2
                      anchors.left: parent.left
                      anchors.verticalCenter: parent.verticalCenter
                      color: {
                        var tone = root.transportHealthTone(transportRow.modelData)
                        if (tone === "active") return Color.accent
                        if (tone === "error") return Color.urgent
                        return root.foreground
                      }
                      opacity: root.transportHealthTone(transportRow.modelData) === "inactive"
                        ? 0.34 : 0.82
                    }

                    Text {
                      id: transportName
                      objectName: "transportName-" + String(transportRow.modelData.id)
                      anchors.left: transportHealthDot.right
                      anchors.leftMargin: Style.spacing.sm
                      anchors.right: transportHealthLabel.left
                      anchors.rightMargin: Style.spacing.md
                      anchors.verticalCenter: parent.verticalCenter
                      text: String(transportRow.modelData.name)
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.uiFontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.weight: transportRow.modelData.active ? Font.DemiBold : Font.Normal
                      elide: Text.ElideRight
                    }

                    Text {
                      id: transportHealthLabel
                      objectName: "transportHealth-" + String(transportRow.modelData.id)
                      anchors.right: transportToggle.visible
                        ? transportToggle.left : parent.right
                      anchors.rightMargin: transportToggle.visible ? Style.spacing.sm : 0
                      anchors.verticalCenter: parent.verticalCenter
                      text: String(transportRow.modelData.health)
                      textFormat: Text.PlainText
                      color: root.foreground
                      opacity: transportRow.modelData.active ? 0.72 : 0.52
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }

                    ToggleSwitch {
                      id: transportToggle
                      objectName: "transportToggle-" + String(transportRow.modelData.id)
                      visible: transportRow.modelData.toggleable === true
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      trackHeight: Style.space(16)
                      cursorRing: false
                      checked: transportRow.modelData.enabled === true
                      busy: root.sourceToggleBusy[String(transportRow.modelData.id)] === true
                      foreground: root.foreground
                      accent: Color.accent
                      hasCursor: activeFocus
                      activeFocusOnTab: visible
                      onToggled: root.toggleSource(transportRow.modelData)

                      Accessible.role: Accessible.CheckBox
                      Accessible.name: String(transportRow.modelData.name) + " enabled"
                      Accessible.checked: checked
                      Accessible.focusable: visible
                      Accessible.onPressAction: if (!busy) toggled()

                      Keys.onPressed: function(event) {
                        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return
                            || event.key === Qt.Key_Enter) {
                          if (!transportToggle.busy) transportToggle.toggled()
                          event.accepted = true
                        }
                      }
                    }
                  }
                }

                Text {
                  objectName: "sourceNotice"
                  width: parent.width
                  visible: root.sourceNotice !== ""
                  text: root.sourceNotice
                  textFormat: Text.PlainText
                  wrapMode: Text.WordWrap
                  color: Color.urgent
                  opacity: 0.9
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                SetupButton {
                  id: manageWebhookButton
                  objectName: "manageWebhookButton"
                  width: parent.width
                  label: "Manage phone webhook…"
                  onTriggered: root.openWebhookSetup()
                }
              }
            }
          }
        }

        Rectangle {
          id: searchField
          objectName: "searchField"
          width: parent.width
          height: root.searchHeight
          radius: root.cornerRadius
          color: Util.alpha(root.foreground, 0.055)
          border.width: Style.normalBorderWidth
          border.color: Util.alpha(root.border, 0.45)

          Text {
            id: searchText
            objectName: "searchText"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.spacing.controlPaddingX
            anchors.rightMargin: Style.spacing.controlPaddingX
            anchors.verticalCenter: parent.verticalCenter
            text: root.filterText || "Search service, source, or code…"
            textFormat: Text.PlainText
            color: root.foreground
            opacity: root.filterText ? 1 : 0.52
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            elide: Text.ElideRight
          }

          Rectangle {
            id: searchCaret
            objectName: "searchCaret"
            width: Math.max(1, Style.normalBorderWidth)
            x: root.filterText
              ? searchText.x + Math.min(searchText.contentWidth + Style.space(2),
                  searchText.width - width)
              : Math.max(border.width + Style.spacing.xs,
                  searchText.x - Style.spacing.xs)
            anchors.top: searchText.top
            anchors.bottom: searchText.bottom
            color: root.foreground
            opacity: searchCaretBlink.illuminated ? 0.86 : 0
            visible: root.opened && keyCatcher.activeFocus && root.searchModeActive
          }

          Timer {
            id: searchCaretBlink
            objectName: "searchCaretBlink"
            property bool illuminated: true
            interval: 530
            repeat: true
            running: root.opened && keyCatcher.activeFocus && root.searchModeActive
            onTriggered: illuminated = !illuminated
            onRunningChanged: illuminated = true
          }
        }

        Item {
          width: parent.width
          height: Math.max(0, parent.height - root.titleHeight - root.searchHeight
            - root.footerHeight - root.contentSpacing * 3)
          clip: true

          ListView {
            id: resultList
            anchors.fill: parent
            model: displayModel
            clip: true
            spacing: Style.spacing.xs
            boundsBehavior: Flickable.StopAtBounds

            delegate: Rectangle {
              id: codeRow
              required property int index
              required property string recordId
              required property string codeText
              required property string serviceName
              required property string sourceName
              required property double receivedMs
              required property double expiresMs
              required property double confidence
              required property string detailText

              readonly property bool hasCursor: root.cursorActive
                && codeRow.index === root.selectedIndex

              width: ListView.view.width
              height: root.rowHeight
              radius: root.cornerRadius
              color: hasCursor ? root.selectedBackground : "transparent"

              Column {
                id: metadata
                anchors.left: parent.left
                anchors.leftMargin: Style.spacing.rowPaddingX
                anchors.right: codeValue.left
                anchors.rightMargin: Style.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.labelGap

                Text {
                  width: parent.width
                  text: codeRow.serviceName
                  textFormat: Text.PlainText
                  color: codeRow.hasCursor ? root.selectedText : root.foreground
                  font.family: root.uiFontFamily
                  font.pixelSize: Style.font.title
                  font.weight: Font.Medium
                  elide: Text.ElideRight
                }

                Text {
                  width: parent.width
                  text: codeRow.detailText
                  textFormat: Text.PlainText
                  color: codeRow.hasCursor ? root.selectedText : root.foreground
                  opacity: 0.70
                  font.family: root.uiFontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
              }

              Text {
                id: codeValue
                width: Math.min(Style.space(190), implicitWidth)
                anchors.right: deleteButton.left
                anchors.rightMargin: Style.spacing.sm
                anchors.verticalCenter: parent.verticalCenter
                text: codeRow.codeText
                textFormat: Text.PlainText
                color: codeRow.hasCursor ? root.selectedText : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
                font.weight: Font.DemiBold
                font.letterSpacing: Style.space(1)
                horizontalAlignment: Text.AlignRight
                elide: Text.ElideRight
              }

              Item {
                id: deleteButton
                width: Style.space(30)
                height: parent.height
                anchors.right: parent.right
                anchors.rightMargin: Style.spacing.sm

                Text {
                  anchors.centerIn: parent
                  text: "×"
                  textFormat: Text.PlainText
                  color: codeRow.hasCursor ? root.selectedText : root.foreground
                  opacity: deleteMouse.containsMouse ? 0.95 : 0.36
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.heading
                }

                MouseArea {
                  id: deleteMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: function(mouse) {
                    mouse.accepted = true
                    root.deleteIndex(codeRow.index)
                  }
                }
              }

              MouseArea {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.right: deleteButton.left
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onPositionChanged: function(mouse) {
                  root.selectFromPointer(codeRow.index, codeRow, mouse)
                }
                onClicked: {
                  root.selectedIndex = codeRow.index
                  root.cursorActive = true
                  root.activateIndex(codeRow.index, true)
                }
              }
            }
          }

          Column {
            anchors.centerIn: parent
            width: Math.min(parent.width, Style.space(430))
            spacing: Style.spacing.sm
            visible: displayModel.count === 0

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: root.service && root.service.bridgeAlive === true ? "󰦝" : "󰅛"
              color: root.service && root.service.bridgeAlive === true
                ? root.selectedText : Color.urgent
              opacity: 0.78
              font.family: root.fontFamily
              font.pixelSize: Style.font.displayLarge
              horizontalAlignment: Text.AlignHCenter
            }

            Text {
              width: parent.width
              objectName: "emptyStateTitle"
              text: {
                if (!root.service) return "Oma2FA service is unavailable"
                if (root.service.bridgeAlive !== true) return "Waiting for the local bridge"
                if (root.filterText) return "No matches for “" + root.filterText + "”"
                return root.activeTransportCount(root.service.status) > 0
                  ? "Ready for your next code" : "Connect your messages"
              }
              textFormat: Text.PlainText
              color: root.foreground
              opacity: 0.78
              font.family: root.uiFontFamily
              font.pixelSize: Style.font.title
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              objectName: "emptyStateDescription"
              text: {
                if (!root.service) return "Enable the plugin service, then reopen this picker."
                if (root.service.bridgeAlive !== true)
                  return String(root.service.lastError || "Oma2FA will reconnect automatically.")
                if (root.filterText) return "Try a service name, transport, or another code."
                return root.activeTransportCount(root.service.status) > 0
                  ? "New codes will appear here automatically."
                  : "Choose a connection to receive verification codes here."
              }
              textFormat: Text.PlainText
              color: root.foreground
              opacity: 0.70
              font.family: root.uiFontFamily
              font.pixelSize: Style.font.bodySmall
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
            }

            SetupButton {
              objectName: "emptyStateSetupButton"
              width: Math.min(parent.width, Style.space(190))
              x: (parent.width - width) / 2
              visible: !root.filterText
                && root.service
                && root.service.bridgeAlive === true
              emphasized: root.activeTransportCount(root.service.status) === 0
              label: root.activeTransportCount(root.service.status) > 0
                ? "iPhone setup guide" : "Set up connections"
              onTriggered: {
                if (root.activeTransportCount(root.service.status) > 0) root.openWebhookSetup(true)
                else root.transportDetailsPinned = true
              }
            }
          }
        }

        Item {
          id: shortcutFooter
          objectName: "shortcutFooter"
          width: parent.width
          height: root.footerHeight
          clip: true

          Row {
            id: footerPrimaryHints
            objectName: "primaryHints"
            visible: displayModel.count > 0
            anchors.left: parent.left
            y: root.footerStacked ? 0 : Math.max(0, (parent.height - height) / 2)
            spacing: Style.spacing.xl * 2

            ShortcutHint {
              hookName: "pasteShortcut"
              shortcutText: "Enter"
              actionText: "Paste"
            }

            ShortcutHint {
              hookName: "copyShortcut"
              shortcutText: "Shift+Enter"
              actionText: "Copy only"
            }
          }

          Row {
            id: footerSecondaryHints
            objectName: "secondaryHints"
            anchors.right: parent.right
            y: root.footerStacked
              ? root.footerLineHeight + Style.spacing.xs
              : Math.max(0, (parent.height - height) / 2)
            spacing: Style.spacing.xl * 2

            ShortcutHint {
              hookName: "removeShortcut"
              visible: displayModel.count > 0
              shortcutText: "Delete"
              actionText: "Remove"
            }

            ShortcutHint {
              hookName: "closeShortcut"
              shortcutText: "Esc"
              actionText: "Close"
            }
          }
        }
      }

      Rectangle {
        id: webhookSetupPanel
        objectName: "webhookSetupPanel"
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        visible: root.webhookSetupOpen
        color: root.background
        z: 40

        Keys.onEscapePressed: function(event) {
          root.closeWebhookSetup()
          event.accepted = true
        }

        Column {
          anchors.fill: parent
          spacing: root.contentSpacing

          Item {
            id: webhookSetupHeader
            objectName: "webhookSetupHeader"
            width: parent.width
            height: root.titleHeight

            SetupButton {
              id: webhookBackButton
              objectName: "webhookBackButton"
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: root.webhookGuideOpen ? Style.space(132) : Style.space(76)
              implicitHeight: root.titleHeight
              fontFamily: root.webhookGuideOpen
                ? root.guideFontFamily : root.fontFamily
              label: root.webhookGuideOpen ? "← Connection" : "← Back"
              onTriggered: {
                if (root.webhookGuideOpen) root.showWebhookConnection()
                else root.closeWebhookSetup()
              }
            }

            Text {
              anchors.left: webhookBackButton.right
              anchors.leftMargin: Style.spacing.md
              anchors.right: openWebhookGuideButton.visible
                ? openWebhookGuideButton.left : parent.right
              anchors.rightMargin: openWebhookGuideButton.visible
                ? Style.spacing.md : 0
              anchors.verticalCenter: parent.verticalCenter
              text: root.webhookGuideOpen ? "iPhone setup guide" : "Phone webhook"
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.webhookGuideOpen
                ? root.guideFontFamily : root.fontFamily
              font.pixelSize: Style.font.heading
              font.weight: Font.DemiBold
              elide: Text.ElideRight
            }

            SetupButton {
              id: openWebhookGuideButton
              objectName: "openWebhookGuideButton"
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(180)
              implicitHeight: root.titleHeight
              fontFamily: root.guideFontFamily
              visible: !root.webhookGuideOpen
              emphasized: root.webhookState().configured === true
              label: "iPhone setup guide →"
              onTriggered: root.showWebhookGuide()
            }
          }

          Row {
            id: guideNavigation
            objectName: "guideNavigation"
            visible: root.webhookGuideOpen
            width: parent.width
            height: visible ? Style.space(38) : 0
            spacing: Style.spacing.xs
            Repeater {
              model: ["Preparation", "Create shortcut", "Configure request", "Automation & test"]
              SetupButton {
                required property int index
                required property string modelData
                objectName: "guideNavigation-" + index
                width: (guideNavigation.width - Style.spacing.xs * 3) / 4
                height: guideNavigation.height
                label: guideNavigation.width < Style.space(600)
                  ? ["Prepare", "Shortcut", "Request", "Test"][index] : modelData
                Accessible.name: modelData
                emphasized: root.currentGuideSection() === index
                fontSize: Style.font.caption
                onTriggered: root.jumpToGuideSection(index)
              }
            }
          }

          Flickable {
            id: webhookSetupFlickable
            objectName: "webhookSetupFlickable"
            width: parent.width
            height: Math.max(0, parent.height - root.titleHeight - root.contentSpacing
              - (guideNavigation.visible ? guideNavigation.height + root.contentSpacing : 0))
            contentWidth: width
            contentHeight: webhookSetupContent.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            Controls.ScrollBar.vertical: Controls.ScrollBar {
              policy: Controls.ScrollBar.AsNeeded
            }

            Column {
              id: webhookSetupContent
              width: webhookSetupFlickable.width
              spacing: Style.spacing.md

              Rectangle {
                objectName: "webhookConnectionStatus"
                width: parent.width
                height: webhookStatusContent.implicitHeight + Style.spacing.md * 2
                visible: !root.webhookGuideOpen
                radius: root.cornerRadius
                color: Util.alpha(root.foreground, 0.055)
                border.width: Style.normalBorderWidth
                border.color: Util.alpha(root.border, 0.38)

                Column {
                  id: webhookStatusContent
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.margins: Style.spacing.md
                  spacing: Style.spacing.xs

                  Row {
                    width: parent.width
                    spacing: Style.spacing.sm

                    Rectangle {
                      width: Style.space(8)
                      height: width
                      radius: width / 2
                      anchors.verticalCenter: parent.verticalCenter
                      color: root.webhookState().running === true
                        ? Color.accent
                        : (root.webhookState().configuration_present === true
                            && root.webhookState().configured !== true
                          ? Color.urgent : root.foreground)
                      opacity: root.webhookState().configured === true ? 0.9 : 0.42
                    }

                    Text {
                      text: root.webhookStatusLabel()
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.title
                      font.weight: Font.DemiBold
                    }
                  }

                  Text {
                    width: parent.width
                    text: {
                      var value = root.webhookState()
                      if (value.endpoint) return String(value.endpoint)
                      if (value.tailscale_available === true)
                        return "Tailscale detected at " + String(value.tailscale_ip)
                      return "Connect this computer and your phone to Tailscale to begin."
                    }
                    textFormat: Text.PlainText
                    color: root.foreground
                    opacity: 0.58
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    wrapMode: Text.WrapAnywhere
                  }
                }
              }

              SetupButton {
                id: configureWebhookButton
                objectName: "configureWebhookButton"
                width: parent.width
                visible: !root.webhookGuideOpen
                  && root.webhookState().configured !== true
                enabled: !root.webhookBusy
                  && root.webhookState().tailscale_available === true
                  && root.service
                  && typeof root.service.configureWebhookTailscale === "function"
                emphasized: true
                label: root.webhookBusy ? "Setting up…" : "Set up securely with Tailscale"
                onTriggered: {
                  root.beginWebhookRequest(root.service.configureWebhookTailscale())
                }
              }

              Row {
                width: parent.width
                spacing: Style.spacing.sm
                visible: !root.webhookGuideOpen
                  && root.webhookState().configured === true

                SetupButton {
                  id: copyEndpointButton
                  objectName: "copyWebhookEndpointButton"
                  width: (parent.width - parent.spacing) / 2
                  enabled: !root.webhookBusy && root.service
                    && typeof root.service.copyWebhookEndpoint === "function"
                  emphasized: true
                  label: "Copy webhook URL"
                  onTriggered: {
                    root.beginWebhookRequest(root.service.copyWebhookEndpoint())
                  }
                }

                SetupButton {
                  id: copyTokenButton
                  objectName: "copyWebhookTokenButton"
                  width: (parent.width - parent.spacing) / 2
                  enabled: !root.webhookBusy && root.service
                    && typeof root.service.copyWebhookToken === "function"
                  label: "Copy raw token"
                  onTriggered: {
                    root.beginWebhookRequest(root.service.copyWebhookToken())
                  }
                }
              }

              Row {
                width: parent.width
                spacing: Style.spacing.sm
                visible: !root.webhookGuideOpen
                  && root.webhookState().configured === true

                SetupButton {
                  id: webhookEnabledButton
                  objectName: "webhookEnabledButton"
                  width: (parent.width - parent.spacing) / 2
                  enabled: !root.webhookBusy && root.service
                    && typeof root.service.setWebhookEnabled === "function"
                  label: root.webhookState().enabled === true
                    ? "Disable webhook" : "Enable webhook"
                  onTriggered: {
                    root.beginWebhookRequest(root.service.setWebhookEnabled(
                      root.webhookState().enabled !== true))
                  }
                }

                SetupButton {
                  id: rotateTokenButton
                  objectName: "rotateWebhookTokenButton"
                  width: (parent.width - parent.spacing) / 2
                  enabled: !root.webhookBusy && root.service
                    && typeof root.service.rotateWebhookToken === "function"
                  label: root.tokenRotationArmed ? "Confirm token rotation" : "Rotate token"
                  onTriggered: {
                    if (!root.tokenRotationArmed) {
                      root.tokenRotationArmed = true
                      tokenRotationTimer.restart()
                      root.webhookNotice = "Press again to rotate. Your phone will stop working until updated."
                    } else {
                      tokenRotationTimer.stop()
                      root.beginWebhookRequest(root.service.rotateWebhookToken())
                    }
                  }
                }
              }

              Text {
                width: parent.width
                visible: root.webhookNotice.length > 0
                text: root.webhookNotice
                textFormat: Text.PlainText
                color: root.webhookNotice.indexOf("failed") >= 0
                  || root.webhookNotice.indexOf("Could not") >= 0
                  ? Color.urgent : root.foreground
                opacity: 0.72
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
              }

              Rectangle {
                id: webhookGuidePanel
                objectName: "webhookGuidePanel"
                width: parent.width
                height: visible
                  ? phoneSetupGuide.implicitHeight
                    + Style.spacing.panelPadding * 2 : 0
                visible: root.webhookGuideOpen
                radius: root.cornerRadius
                color: Util.alpha(root.foreground, 0.035)

                Column {
                  id: phoneSetupGuide
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.margins: Style.spacing.panelPadding
                  spacing: root.guideParagraphSpacing

                  Text {
                    id: guidePreparation
                    objectName: "webhookGuideTitle"
                    width: parent.width
                    text: "Set up your iPhone"
                    textFormat: Text.PlainText
                    color: root.foreground
                    opacity: 0.98
                    font.family: root.guideFontFamily
                    font.pixelSize: root.guideTitleFontSize
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                  }

                  GuideBodyText {
                    objectName: "webhookGuideIntro"
                    width: parent.width
                    text: "1. Connect your iPhone and computer to the same Tailscale network.\n2. Set up the phone webhook on the Connection page.\n3. Open Shortcuts on your iPhone.\n\nCopy buttons provide values to paste. Arrow rows show choices to make in iOS. Transfer copied values with your preferred local sharing app; they clear from the clipboard after about 60 seconds."
                  }

                  GuideSectionHeading {
                    id: guideShortcut
                    objectName: "webhookGuideStep1Heading"
                    width: parent.width
                    text: "1. Create the Shortcut"
                  }

                  GuideBodyText {
                    width: parent.width
                    text: "In Shortcuts → Library, tap + and make a blank shortcut. Open its details, enable receiving input, and use the values below. Add a Get Contents of URL action after the input action."
                  }

                  GuideScreenshot {
                    objectName: "shortcutLibraryGuideImage"
                    width: parent.width
                    caption: "Start in the Library by tapping +. After saving, Send to Oma2FA appears as the pink card shown here."
                    imageSource: Qt.resolvedUrl("assets/shortcut-library.png")
                    aspectRatio: 1672 / 941
                  }

                  Column {
                    objectName: "guideInputGroup"
                    width: parent.width
                    spacing: Style.spacing.xs
                    GuideSectionHeading {
                      width: parent.width
                      topPadding: Style.spacing.md
                      text: "Shortcut input"
                    }
                    CopyFieldRow {
                      width: parent.width
                      fieldId: "shortcut_name"
                      fieldLabel: "Shortcut name"
                      fieldValue: "Send to Oma2FA"
                    }

                    CopyFieldRow {
                      width: parent.width
                      fieldId: "receive_type"
                      fieldLabel: "Receive"
                      fieldValue: "Text"
                      copyable: false
                    }

                    CopyFieldRow {
                      width: parent.width
                      fieldId: "receive_source"
                      fieldLabel: "Receive input from"
                      fieldValue: "Nowhere"
                      copyable: false
                    }

                    CopyFieldRow {
                      width: parent.width
                      fieldId: "no_input_behavior"
                      fieldLabel: "If there is no input"
                      fieldValue: "Stop and Respond"
                      copyable: false
                    }
                  }

                  GuideScreenshot {
                    objectName: "shortcutInputGuideImage"
                    width: parent.width
                    caption: "The top of the shortcut should show its name and this Receive Text from Nowhere input block."
                    imageSource: Qt.resolvedUrl("assets/shortcut-input.png")
                    aspectRatio: 853 / 540
                  }

                  GuideSectionHeading {
                    id: guideRequest
                    objectName: "webhookGuideStep2Heading"
                    width: parent.width
                    text: "2. Configure Get Contents of URL"
                  }

                  GuideBodyText {
                    width: parent.width
                    text: "Expand Get Contents of URL. Paste the URL, choose POST, add both headers, select a JSON request body, and add the three key/value pairs in order."
                  }

                  Column {
                    objectName: "guideRequestGroup"
                    width: parent.width
                    spacing: Style.spacing.xs
                    GuideSectionHeading {
                      width: parent.width
                      topPadding: Style.spacing.md
                      text: "Request"
                    }
                    CopyFieldRow {
                      width: parent.width
                      fieldId: "webhook_url"
                      fieldLabel: "URL"
                      fieldValue: String(root.webhookState().endpoint
                        || "Set up the webhook to create this value")
                      requiresWebhook: true
                    }

                    CopyFieldRow {
                      width: parent.width
                      fieldId: "http_method"
                      fieldLabel: "Method"
                      fieldValue: "POST"
                      copyable: false
                    }
                  }

                  Column {
                    objectName: "guideAuthorizationGroup"
                    width: parent.width
                    spacing: Style.spacing.xs
                    GuideSectionHeading {
                      width: parent.width
                      topPadding: Style.spacing.md
                      text: "Authorization header"
                    }
                    GuideFieldPair {
                      objectName: "fieldPair-authorization_header"
                      width: parent.width
                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "authorization_header"
                        fieldLabel: "Header 1 · name"
                        fieldValue: "Authorization"
                      }

                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "authorization_value"
                        fieldLabel: "Header 1 · value"
                        fieldValue: "Bearer <generated token>"
                        note: "Copies your complete authorization value."
                        requiresWebhook: true
                      }
                    }
                  }

                  Column {
                    objectName: "guideContentTypeGroup"
                    width: parent.width
                    spacing: Style.spacing.xs
                    GuideSectionHeading {
                      width: parent.width
                      topPadding: Style.spacing.md
                      text: "Content type header"
                    }
                    GuideFieldPair {
                      objectName: "fieldPair-content_type_header"
                      width: parent.width
                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "content_type_header"
                        fieldLabel: "Header 2 · name"
                        fieldValue: "Content-Type"
                      }

                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "content_type_value"
                        fieldLabel: "Header 2 · value"
                        fieldValue: "application/json"
                      }
                    }
                  }

                  Column {
                    objectName: "guideJsonGroup"
                    width: parent.width
                    spacing: Style.spacing.xs
                    GuideSectionHeading {
                      width: parent.width
                      topPadding: Style.spacing.md
                      text: "JSON body"
                    }
                    CopyFieldRow {
                      width: parent.width
                      fieldId: "request_body_type"
                      fieldLabel: "Request Body"
                      fieldValue: "JSON"
                      copyable: false
                    }

                    GuideFieldPair {
                      objectName: "fieldPair-sender_key"
                      width: parent.width
                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "sender_key"
                        fieldLabel: "JSON field 1 · key"
                        fieldValue: "sender"
                      }

                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "sender_value"
                        fieldLabel: "JSON field 1 · value"
                        fieldValue: "SMS"
                      }
                    }

                    GuideFieldPair {
                      objectName: "fieldPair-body_key"
                      width: parent.width
                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "body_key"
                        fieldLabel: "JSON field 2 · key"
                        fieldValue: "body"
                      }

                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "shortcut_input"
                        fieldLabel: "JSON field 2 · value"
                        fieldValue: "Shortcut Input"
                        copyable: false
                        note: "Insert the variable—don’t type this text. Tap the value and choose the blue Shortcut Input variable."
                      }
                    }

                    GuideFieldPair {
                      objectName: "fieldPair-source_key"
                      width: parent.width
                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "source_key"
                        fieldLabel: "JSON field 3 · key"
                        fieldValue: "source"
                      }

                      CopyFieldRow {
                        width: parent.cellWidth
                        fieldId: "source_value"
                        fieldLabel: "JSON field 3 · value"
                        fieldValue: "ios-shortcuts"
                      }
                    }
                  }

                  GuideScreenshot {
                    objectName: "shortcutConfigurationGuideImage"
                    width: parent.width
                    caption: "The complete shortcut configuration. The documentation image uses placeholders; the copyable URL and authorization rows use this computer's actual values."
                    imageSource: Qt.resolvedUrl("assets/shortcut-configuration.png")
                    aspectRatio: 853 / 1844
                  }

                  GuideSectionHeading {
                    id: guideAutomation
                    objectName: "webhookGuideStep3Heading"
                    width: parent.width
                    text: "3. Automation & test"
                  }

                  GuideBodyText {
                    width: parent.width
                    text: "Open Shortcuts → Automation, tap +, choose Message, and set Message Contains to the phrase below. Choose Run Immediately, continue, then select Send to Oma2FA. Add a sender filter only if you know every sender that delivers your codes."
                  }

                  Column {
                    objectName: "guideTriggerGroup"
                    width: parent.width
                    spacing: Style.spacing.xs
                    GuideSectionHeading {
                      width: parent.width
                      topPadding: Style.spacing.md
                      text: "Message trigger"
                    }
                    CopyFieldRow {
                      width: parent.width
                      fieldId: "trigger_phrase"
                      fieldLabel: "Message Contains"
                      fieldValue: "code"
                    }

                    CopyFieldRow {
                      width: parent.width
                      fieldId: "run_mode"
                      fieldLabel: "Run mode"
                      fieldValue: "Run Immediately"
                      copyable: false
                    }
                  }

                  GuideScreenshot {
                    objectName: "shortcutAutomationGuideImage"
                    width: parent.width
                    caption: "Save the automation. The Automation tab should show the Message trigger running Send to Oma2FA immediately."
                    imageSource: Qt.resolvedUrl("assets/shortcut-automation.png")
                    aspectRatio: 1695 / 928
                  }

                  GuideSectionHeading {
                    width: parent.width
                    text: "Test your connection"
                  }

                  GuideBodyText {
                    width: parent.width
                    topPadding: Style.spacing.sm
                    text: "Send your iPhone a message such as “Your verification code is 123456”. A notification should appear on the computer. Click it to open Oma2FA and check that 123456 appears. If both happen, setup is complete."
                  }
                  GuideSectionHeading {
                    width: parent.width
                    text: "If the code doesn’t appear"
                  }
                  GuideBodyText {
                    objectName: "guideTroubleshooting"
                    width: parent.width
                    text: "No notification? Check that Tailscale is connected on both devices and Phone webhook is Ready on the Connection page. In Shortcuts, check the URL, authorization value, and Run Immediately setting.\n\nNotification but no code? Send a fresh test message and open Oma2FA promptly. Codes expire automatically. Check that the JSON body uses the Shortcut Input variable and that Message Contains matches the message."
                  }
                }
              }
            }
          }
        }

        Timer {
          id: tokenRotationTimer
          interval: 10000
          repeat: false
          onTriggered: root.tokenRotationArmed = false
        }
      }
    }
  }
}

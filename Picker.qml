import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

Item {
  id: root

  // Injected by omarchy-shell's overlay loader. `service` is the matching
  // io.github.oma2fa service instance, not a second backend connection.
  property var shell: null
  property var manifest: null
  property var service: null

  property bool opened: false
  property bool openingPending: false
  property string filterText: ""
  property int selectedIndex: 0
  property bool cursorActive: true
  property var capturedTarget: ({})
  property int clockRevision: 0

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
  property int contentMargin: Style.spacing.panelPadding
  property int contentSpacing: Style.spacing.md
  property int cardWidth: Math.min(Style.space(620), panel.width - Style.gapsOut * 2)
  property int cardHeight: Math.min(Style.space(560), panel.height - Style.gapsOut * 2)
  property int titleHeight: Math.max(Style.space(30), Style.font.heading + Style.spacing.sm)
  property int searchHeight: Math.max(Style.space(42), Style.spacing.controlHeight)
  property int footerLineHeight: Math.max(Style.space(24),
    Style.font.caption + Style.spacing.sm)
  property bool footerStacked: footerPrimaryHints.implicitWidth
    + footerSecondaryHints.implicitWidth + Style.spacing.xl > shortcutFooter.width
  property int footerHeight: footerStacked
    ? footerLineHeight * 2 + Style.spacing.xs : footerLineHeight
  property int rowHeight: Math.max(Style.space(70),
    Style.font.title + Style.font.bodySmall + Style.spacing.rowPaddingX * 2)

  function pluginId() {
    return (root.manifest && root.manifest.id)
      ? String(root.manifest.id) : "io.github.oma2fa"
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
    root.cursorActive = true
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
    root.cursorActive = displayModel.count > 0
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

  function activeTransportCount(statusValue) {
    if (!statusValue || !statusValue.sources) return 0
    var sources = statusValue.sources
    var count = 0
    if (Array.isArray(sources)) {
      for (var index = 0; index < sources.length; index++) {
        var source = sources[index]
        var name = source && typeof source === "object"
          ? (source.name || source.id || "") : ""
        if (root.sourceIsActiveTransport(name, source)) count++
      }
      return count
    }
    if (typeof sources !== "object") return 0
    var names = Object.keys(sources)
    for (var keyIndex = 0; keyIndex < names.length; keyIndex++) {
      var sourceName = names[keyIndex]
      if (root.sourceIsActiveTransport(sourceName, sources[sourceName])) count++
    }
    return count
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

    var selectedId = ""
    if (keepSelection === true && displayModel.count > 0
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
      root.cursorActive = true
      Qt.callLater(function() {
        resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
      })
    }
  }

  function setFilter(nextFilter) {
    root.filterText = String(nextFilter || "")
    root.selectedIndex = 0
    root.cursorActive = true
    root.disarmPointer()
    root.rebuildDisplay(false)
  }

  function select(delta) {
    if (displayModel.count === 0) return
    root.disarmPointer()
    root.cursorActive = true
    root.selectedIndex = (root.selectedIndex + delta + displayModel.count) % displayModel.count
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

  onServiceChanged: root.rebuildDisplay(false)

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onRecordsChanged() { root.rebuildDisplay(true) }
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
      opacity: 0.50
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
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
      width: root.cardWidth
      height: root.cardHeight
      radius: root.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: root.contentMargin

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyCatcher
        objectName: "pickerKeyCatcher"
        anchors.fill: parent
        focus: true
        z: 5

        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) {
            // Escape always closes and never asks the service to activate.
            root.dismiss()
            event.accepted = true
          } else if (Util.editsFilter(event, root.filterText)) {
            root.setFilter(Util.editedFilter(event, root.filterText))
            event.accepted = true
          } else if (event.key === Qt.Key_Delete) {
            root.deleteIndex(root.selectedIndex)
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
      }

      Column {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: root.contentSpacing

        Item {
          width: parent.width
          height: root.titleHeight

          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "Oma2FA"
            textFormat: Text.PlainText
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            font.weight: Font.DemiBold
          }

          Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.sm

            Rectangle {
              width: Style.space(7)
              height: width
              radius: width / 2
              anchors.verticalCenter: parent.verticalCenter
              color: root.statusColor()
              opacity: root.service && root.service.bridgeAlive === true ? 0.9 : 0.55
            }

            Text {
              width: Math.min(Style.space(360), implicitWidth)
              text: root.statusText()
              textFormat: Text.PlainText
              color: root.foreground
              opacity: 0.62
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideLeft
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
            visible: root.opened && keyCatcher.activeFocus
          }

          Timer {
            id: searchCaretBlink
            objectName: "searchCaretBlink"
            property bool illuminated: true
            interval: 530
            repeat: true
            running: root.opened && keyCatcher.activeFocus
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
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  font.weight: Font.Medium
                  elide: Text.ElideRight
                }

                Text {
                  width: parent.width
                  text: codeRow.detailText
                  textFormat: Text.PlainText
                  color: codeRow.hasCursor ? root.selectedText : root.foreground
                  opacity: 0.56
                  font.family: root.fontFamily
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
              text: {
                if (!root.service) return "Oma2FA service is unavailable"
                if (root.service.bridgeAlive !== true) return "Waiting for the local bridge"
                if (root.filterText) return "No matches for “" + root.filterText + "”"
                return "No recent verification codes"
              }
              textFormat: Text.PlainText
              color: root.foreground
              opacity: 0.78
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              text: {
                if (!root.service) return "Enable the plugin service, then reopen this picker."
                if (root.service.bridgeAlive !== true)
                  return String(root.service.lastError || "Oma2FA will reconnect automatically.")
                if (root.filterText) return "Try a service name, transport, or another code."
                return "Codes appear here when a paired SMS transport detects one."
              }
              textFormat: Text.PlainText
              color: root.foreground
              opacity: 0.48
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.WordWrap
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
    }
  }
}

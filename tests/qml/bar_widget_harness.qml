import QtQuick
import Quickshell
import Quickshell.Io

ShellRoot {
  id: harness

  property var firstWidget: null
  property var secondWidget: null
  property var rootProbe: null

  function fail(message) {
    console.error("OMA2FA_QML_HARNESS_FAIL: " + String(message))
    Qt.exit(1)
  }

  function containsSensitiveText(value) {
    var text = String(value || "")
    return text.indexOf("123456") !== -1
      || text.indexOf("ACME Private") !== -1
      || text.indexOf("record-secret-id") !== -1
      || text.indexOf("backend secret") !== -1
  }

  function runAssertions() {
    if (!firstWidget || !secondWidget) {
      harness.fail("widget creation returned null")
      return
    }
    if (firstWidget.omaService !== sharedService
        || secondWidget.omaService !== sharedService) {
      harness.fail("per-monitor widgets did not resolve the shared service")
      return
    }
    if (firstWidget.codeCount !== 1 || firstWidget.badgeText !== "1") {
      harness.fail("count badge did not reflect the shared service")
      return
    }
    if (harness.containsSensitiveText(firstWidget.tooltipLabel)) {
      harness.fail("tooltip exposed sensitive service state")
      return
    }

    var button = firstWidget.children.length > 0
      ? firstWidget.children[0] : null
    if (!button || typeof button.pressed !== "function") {
      harness.fail("bar button signal is unavailable")
      return
    }
    button.pressed(Qt.MiddleButton)
    button.pressed(Qt.RightButton)
    if (fakeShell.toggleCalls !== 0) {
      harness.fail("a non-left click toggled the picker")
      return
    }
    button.pressed(Qt.LeftButton)
    if (fakeShell.toggleCalls !== 1
        || fakeShell.lastToggleId !== "io.github.jondkinney.oma2fa"
        || fakeShell.lastTogglePayload !== "{}") {
      harness.fail("left click did not use the expected in-process toggle")
      return
    }
    if (sharedService.activationCalls !== 0
        || sharedService.copyCalls !== 0) {
      harness.fail("bar interaction activated or copied a code")
      return
    }

    console.log("OMA2FA_QML_HARNESS_PASS")
    firstWidget.destroy()
    secondWidget.destroy()
    Qt.exit(0)
  }

  function createWidgets() {
    var component = Qt.createComponent(Qt.resolvedUrl("ui/Oma2FABarWidget.qml"),
      Component.PreferSynchronous)
    if (component.status !== Component.Ready) {
      harness.fail("component load failed: " + component.errorString())
      return
    }
    firstWidget = component.createObject(widgetHost, { bar: fakeBar })
    secondWidget = component.createObject(widgetHost, { bar: fakeBar })
    Qt.callLater(harness.runAssertions)
  }

  QtObject {
    id: sharedService

    property var records: [{
      id: "record-secret-id",
      code: "123456",
      service: "ACME Private",
      received_at: "2026-08-21T18:00:00Z"
    }]
    property bool bridgeAlive: true
    property bool ready: true
    property var status: ({ state: "ready", message: "backend secret 123456" })
    property int activationCalls: 0
    property int copyCalls: 0

    function activate() { activationCalls++ }
    function copy() { copyCalls++ }
  }

  QtObject {
    id: fakeShell

    property int toggleCalls: 0
    property string lastToggleId: ""
    property string lastTogglePayload: ""

    function serviceFor(pluginId) {
      return pluginId === "io.github.jondkinney.oma2fa" ? sharedService : null
    }

    function toggle(pluginId, payload) {
      toggleCalls++
      lastToggleId = String(pluginId)
      lastTogglePayload = String(payload)
    }
  }

  QtObject {
    id: fakeBar

    property var shell: fakeShell
    property bool vertical: false
    property int barSize: 32
    property string fontFamily: "monospace"
    property color barForeground: "white"
    property color urgent: "red"
    property color themeContrastForeground: "black"
    property bool foregroundAnimationEnabled: false

    function registerClickTarget() {}
    function unregisterClickTarget() {}
    function showTooltip() {}
    function hideTooltip() {}
  }

  Item { id: widgetHost }

  Process {
    id: widgetArrival
    command: [
      "sh", "-c",
      "while [ ! -f \"$1\" ]; do sleep 0.05; done",
      "oma2fa-qml-wait",
      Quickshell.env("OMA2FA_QML_WIDGET_PATH")
    ]
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        harness.fail("widget arrival watcher exited unexpectedly")
        return
      }
      harness.createWidgets()
    }
  }

  Component.onCompleted: {
    // Force Qt to cache the already-existing root directory before the runner
    // adds the new widget entry point. A root-level BarWidget.qml added during
    // a shell hot upgrade can then fail with "File name case mismatch". The
    // new nested ui/ URL must remain loadable without restarting Quickshell.
    var component = Qt.createComponent(Qt.resolvedUrl("RootProbe.qml"),
      Component.PreferSynchronous)
    if (component.status !== Component.Ready) {
      harness.fail("root cache probe failed: " + component.errorString())
      return
    }
    rootProbe = component.createObject(widgetHost)
    console.log("OMA2FA_QML_HARNESS_READY")
    widgetArrival.running = true
  }
}

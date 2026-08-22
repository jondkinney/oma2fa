import QtQuick
import Quickshell

ShellRoot {
  id: harness

  property var picker: null

  function fail(message) {
    console.error("OMA2FA_PICKER_STATUS_FAIL: " + String(message))
    Qt.exit(1)
  }

  function expectStatus(expected) {
    var actual = picker.statusText()
    if (actual !== expected)
      harness.fail("expected '" + expected + "', got '" + actual + "'")
  }

  function runAssertions() {
    expectStatus("Ready  ·  1 active transport  ·  0 codes")

    fakeService.status = {
      ready: true,
      sources: {
        manual: { available: true, detail: "ready" },
        blueferry: { available: true, running: true, connected: false },
        webhook: { available: true, enabled: false, running: false }
      }
    }
    expectStatus("Ready  ·  0 active transports  ·  0 codes")

    fakeService.status = {
      ready: true,
      sources: {
        manual: { available: true, detail: "ready" },
        blueferry: { available: true, running: true, connected: true },
        webhook: { available: true, enabled: true, running: true }
      }
    }
    expectStatus("Ready  ·  2 active transports  ·  0 codes")

    console.log("OMA2FA_PICKER_STATUS_PASS")
    picker.destroy()
    Qt.exit(0)
  }

  QtObject {
    id: fakeService

    property bool bridgeAlive: true
    property bool ready: true
    property string lastError: ""
    property var records: []
    property var status: ({
      ready: true,
      sources: {
        manual: { available: true, detail: "ready" },
        blueferry: { available: true, running: true, connected: true },
        webhook: { available: true, enabled: false, running: false }
      }
    })

    function refresh() { return 0 }
  }

  Item { id: host }

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("Picker.qml"),
      Component.PreferSynchronous)
    if (component.status !== Component.Ready) {
      harness.fail("component load failed: " + component.errorString())
      return
    }
    picker = component.createObject(host, { service: fakeService })
    if (!picker) {
      harness.fail("picker creation returned null")
      return
    }
    Qt.callLater(harness.runAssertions)
  }
}

import QtQuick
import Quickshell

ShellRoot {
  id: harness

  property var picker: null
  property var seenObjects: []

  function fail(message) {
    console.error("OMA2FA_PICKER_STATUS_FAIL: " + String(message))
    Qt.exit(1)
  }

  function expectStatus(expected) {
    var actual = picker.statusText()
    if (actual !== expected)
      harness.fail("expected '" + expected + "', got '" + actual + "'")
  }

  function expect(condition, message) {
    if (!condition) harness.fail(message)
  }

  function childObjects(object) {
    var result = []
    var lists = []
    try {
      if (object.data !== undefined) lists.push(object.data)
    } catch (error) {}
    try {
      if (object.children !== undefined) lists.push(object.children)
    } catch (error) {}
    try {
      if (object.contentItem !== undefined && object.contentItem !== null)
        result.push(object.contentItem)
    } catch (error) {}

    for (var listIndex = 0; listIndex < lists.length; listIndex++) {
      var values = lists[listIndex]
      for (var index = 0; index < values.length; index++) {
        if (values[index] !== null && result.indexOf(values[index]) < 0)
          result.push(values[index])
      }
    }
    return result
  }

  function collectNamed(object, name, matches) {
    if (object === null || object === undefined
        || harness.seenObjects.indexOf(object) >= 0) return
    harness.seenObjects.push(object)
    if (String(object.objectName || "") === name) matches.push(object)

    var children = harness.childObjects(object)
    for (var index = 0; index < children.length; index++)
      harness.collectNamed(children[index], name, matches)
  }

  function named(name) {
    harness.seenObjects = []
    var matches = []
    harness.collectNamed(harness.picker, name, matches)
    harness.expect(matches.length === 1,
      "expected one object named " + name + ", found " + matches.length)
    return matches[0]
  }

  function expectEntry(entries, id, name, active, health) {
    for (var index = 0; index < entries.length; index++) {
      var entry = entries[index]
      if (entry.id !== id) continue
      harness.expect(entry.name === name, id + " has the wrong display name")
      harness.expect(entry.active === active, id + " has the wrong active state")
      harness.expect(entry.health === health, id + " has the wrong health label")
      return
    }
    harness.fail("missing transport entry " + id)
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
    var inactiveEntries = picker.transportEntries(fakeService.status)
    harness.expect(inactiveEntries.length === 2,
      "manual source must be excluded from transport details")
    harness.expectEntry(inactiveEntries, "blueferry", "BlueFerry", false,
      "Disconnected")
    harness.expectEntry(inactiveEntries, "webhook", "Phone webhook", false,
      "Disabled")

    fakeService.status = {
      ready: true,
      sources: {
        manual: { available: true, detail: "ready" },
        blueferry: {
          available: true,
          running: true,
          connected: true,
          detail: "PRIVATE_SENTINEL"
        },
        webhook: {
          available: true,
          enabled: true,
          running: true,
          detail: "PRIVATE_SENTINEL"
        }
      }
    }
    expectStatus("Ready  ·  2 active transports  ·  0 codes")
    var activeEntries = picker.transportEntries(fakeService.status)
    harness.expectEntry(activeEntries, "blueferry", "BlueFerry", true, "Connected")
    harness.expectEntry(activeEntries, "webhook", "Phone webhook", true, "Ready")
    harness.expect(JSON.stringify(activeEntries).indexOf("PRIVATE_SENTINEL") === -1,
      "transport rows exposed arbitrary backend detail")
    harness.expect(picker.activeTransportSummary(fakeService.status)
      === "Active transports: BlueFerry, Phone webhook",
      "accessible transport summary is unclear")

    fakeService.status = {
      ready: true,
      sources: {
        manual: { available: true, detail: "ready" },
        blueferry: { available: true, running: true, connected: true },
        webhook: {
          available: true,
          enabled: true,
          running: false,
          detail: "not responding"
        }
      }
    }
    expectStatus("Ready  ·  1 active transport  ·  0 codes")
    harness.expectEntry(picker.transportEntries(fakeService.status), "webhook",
      "Phone webhook", false, "Not responding")

    var arrayStatus = {
      sources: [
        { name: "manual", available: true },
        { id: "webhook", available: false, enabled: true, running: false },
        { name: "blueferry", available: true, running: true, connected: true }
      ]
    }
    harness.expect(picker.activeTransportCount(arrayStatus) === 1,
      "array-shaped transport status has the wrong active count")
    var arrayEntries = picker.transportEntries(arrayStatus)
    harness.expect(arrayEntries.length === 2,
      "array-shaped status did not exclude manual")
    harness.expectEntry(arrayEntries, "webhook", "Phone webhook", false, "Unavailable")

    fakeService.status = {
      ready: true,
      sources: {
        manual: { available: true, detail: "ready" },
        blueferry: { available: true, running: true, connected: true },
        webhook: { available: true, enabled: true, running: true }
      }
    }
    Qt.callLater(harness.runPopoverAssertions)
  }

  function runPopoverAssertions() {
    var trigger = harness.named("transportStatusTrigger")
    var popover = harness.named("transportStatusPopover")
    var title = harness.named("pickerTitleLabel")
    harness.expect(trigger.activeFocusOnTab === true,
      "transport disclosure must be keyboard focusable")
    harness.expect(trigger.Accessible.focusable === true,
      "transport disclosure must be exposed as focusable")
    harness.expect(trigger.x >= title.x + title.width,
      "transport disclosure overlaps the picker title")
    harness.expect(trigger.x + trigger.width <= trigger.parent.width + 0.5,
      "transport disclosure extends beyond the title bar")
    harness.expect(String(trigger.Accessible.name)
      === "Active transports: BlueFerry, Phone webhook",
      "transport trigger has the wrong accessible name")
    harness.expect(String(trigger.Accessible.description)
      === "Transport details hidden. Press to show.",
      "collapsed transport trigger has the wrong accessible description")
    harness.expect(popover.visible === false,
      "transport popover should start hidden")

    picker.transportDetailsPinned = true
    harness.expect(popover.visible === true,
      "pinning did not reveal transport details")
    harness.expect(String(trigger.Accessible.description)
      === "Transport details shown. Press to hide.",
      "expanded transport trigger has the wrong accessible description")
    harness.expect(String(harness.named("transportName-blueferry").text) === "BlueFerry",
      "BlueFerry row is missing")
    harness.expect(String(harness.named("transportHealth-blueferry").text) === "Connected",
      "BlueFerry row has the wrong health")
    harness.expect(String(harness.named("transportName-webhook").text) === "Phone webhook",
      "webhook row is missing")
    harness.expect(String(harness.named("transportHealth-webhook").text) === "Ready",
      "webhook row has the wrong health")
    harness.expect(popover.width <= picker.cardWidth,
      "transport popover is wider than the picker card")
    harness.expect(popover.height > 0,
      "transport popover has no visible height")

    picker.transportDetailsPinned = false
    harness.expect(popover.visible === false,
      "unpinning did not hide transport details")
    picker.transportDetailsPinned = true
    picker.close()
    harness.expect(picker.transportDetailsPinned === false,
      "closing did not reset pinned transport details")

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

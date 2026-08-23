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
    var titleBar = title.parent
    var triggerOrigin = trigger.mapToItem(titleBar, 0, 0)
    var titleOrigin = title.mapToItem(titleBar, 0, 0)
    var triggerRect = {
      x: triggerOrigin.x,
      width: trigger.width,
      right: triggerOrigin.x + trigger.width
    }
    var titleRect = {
      x: titleOrigin.x,
      width: title.width,
      right: titleOrigin.x + title.width
    }
    harness.expect(trigger.activeFocusOnTab === true,
      "transport disclosure must be keyboard focusable")
    harness.expect(trigger.Accessible.focusable === true,
      "transport disclosure must be exposed as focusable")
    harness.expect(triggerRect.x >= titleRect.right,
      "transport disclosure overlaps the picker title (trigger x=" + triggerRect.x
        + ", title right=" + titleRect.right + ", title bar=" + titleBar.width
        + ", parent x=" + trigger.parent.x + ", parent width=" + trigger.parent.width
        + ", trigger width=" + trigger.width + ")")
    harness.expect(triggerRect.right <= titleBar.width + 0.5,
      "transport disclosure extends beyond the title bar")
    harness.expect(Math.abs(triggerRect.right - titleBar.width) <= 0.5,
      "transport disclosure is not right aligned")
    harness.expect(triggerRect.width < titleBar.width - titleRect.right - 0.5,
      "transport disclosure should stay compact instead of filling the title bar")
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
    var popoverHover = harness.named("transportPopoverHover")
    harness.expect(popoverHover.parent === popover,
      "transport popover hover coverage must include all child controls")
    harness.expect(popoverHover.enabled === true,
      "transport popover hover coverage is disabled")

    picker.transportDetailsPinned = false
    harness.expect(popover.visible === false,
      "unpinning did not hide transport details")
    harness.expect(harness.named("manageWebhookButton").Accessible.focusable === true,
      "webhook manager entry must be keyboard accessible")
    picker.openWebhookSetup()
    Qt.callLater(harness.runWebhookSetupAssertions)
  }

  function runWebhookSetupAssertions() {
    harness.expect(picker.webhookSetupOpen === true,
      "opening the webhook manager did not change views")
    harness.expect(harness.named("webhookSetupPanel").visible === true,
      "webhook setup panel is not visible")
    harness.expect(fakeService.statusRequests === 1,
      "webhook setup did not refresh manager status")
    harness.expect(harness.named("configureWebhookButton").enabled === true,
      "Tailscale setup action should be enabled when Tailscale is detected")
    harness.expect(JSON.stringify(fakeService.webhookSetup).indexOf("PRIVATE_TOKEN") === -1,
      "webhook setup state retained a bearer token")
    harness.expect(String(harness.named("copyShortcutFieldValue-shortcut_name").text)
      === "Send to Oma2FA", "shortcut name field is missing")
    harness.expect(harness.named("copyShortcutFieldButton-shortcut_name").enabled === true,
      "static setup fields should be copyable before webhook provisioning")
    harness.expect(harness.named("copyShortcutFieldButton-webhook_url").enabled === false,
      "URL copy must wait until the webhook is provisioned")
    harness.expect(String(harness.named("shortcutLibraryGuideImage").imageSource)
      .indexOf("assets/shortcut-library.png") >= 0,
      "Library reference image is missing")
    harness.expect(String(harness.named("shortcutConfigurationGuideImage").imageSource)
      .indexOf("assets/shortcut-configuration.png") >= 0,
      "configuration reference image is missing")
    harness.expect(String(harness.named("shortcutAutomationGuideImage").imageSource)
      .indexOf("assets/shortcut-automation.png") >= 0,
      "Automation reference image is missing")

    harness.named("configureWebhookButton").triggered()
    Qt.callLater(harness.runConfiguredWebhookAssertions)
  }

  function runConfiguredWebhookAssertions() {
    harness.expect(fakeService.configureRequests === 1,
      "webhook setup action did not call the service")
    harness.expect(picker.webhookStatusLabel() === "Ready",
      "configured webhook did not render as ready")
    harness.expect(harness.named("copyWebhookEndpointButton").visible === true,
      "configured webhook did not reveal its copy actions")
    harness.expect(harness.named("copyWebhookTokenButton").Accessible.focusable === true,
      "token copy action must be keyboard accessible")
    harness.expect(harness.named("copyShortcutFieldButton-webhook_url").enabled === true,
      "configured webhook did not enable URL field copying")
    harness.expect(String(harness.named("copyShortcutFieldValue-authorization_value").text)
      === "Bearer <generated token>",
      "authorization field should display only a placeholder")

    harness.named("copyShortcutFieldButton-authorization_value").triggered()
    harness.expect(fakeService.fieldCopyRequests === 1,
      "authorization value copy did not call the service")
    harness.expect(fakeService.lastCopiedField === "authorization_value",
      "authorization value copy used the wrong allowlisted field id")
    harness.expect(JSON.stringify(fakeService.webhookSetup).indexOf("PRIVATE_TOKEN") === -1,
      "authorization value copy exposed a bearer token to QML")

    harness.named("copyWebhookTokenButton").triggered()
    harness.expect(fakeService.tokenCopyRequests === 1,
      "token copy action did not call the service")
    harness.expect(JSON.stringify(fakeService.webhookSetup).indexOf("PRIVATE_TOKEN") === -1,
      "token copy exposed a bearer token to QML")

    harness.named("webhookBackButton").triggered()
    harness.expect(picker.webhookSetupOpen === false,
      "Back did not close the webhook manager")
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
    property int statusRequests: 0
    property int configureRequests: 0
    property int tokenCopyRequests: 0
    property int fieldCopyRequests: 0
    property string lastCopiedField: ""
    property var webhookSetup: ({
      configured: false,
      configuration_present: false,
      enabled: false,
      running: false,
      endpoint: "",
      tailscale_available: true,
      tailscale_ip: "100.100.101.102"
    })
    property var status: ({
      ready: true,
      sources: {
        manual: { available: true, detail: "ready" },
        blueferry: { available: true, running: true, connected: true },
        webhook: { available: true, enabled: false, running: false }
      }
    })

    function refresh() { return 0 }
    function requestWebhookStatus() {
      statusRequests++
      Qt.callLater(function() {
        fakeService.requestFinished(10, "webhook_status", true, "")
      })
      return 10
    }
    function configureWebhookTailscale() {
      configureRequests++
      webhookSetup = {
        configured: true,
        configuration_present: true,
        enabled: true,
        running: true,
        endpoint: "http://100.100.101.102:8765/v1/ingest",
        tailscale_available: true,
        tailscale_ip: "100.100.101.102"
      }
      Qt.callLater(function() {
        fakeService.requestFinished(11, "webhook_configure_tailscale", true, "")
      })
      return 11
    }
    function copyWebhookToken() {
      tokenCopyRequests++
      Qt.callLater(function() {
        fakeService.requestFinished(12, "webhook_copy_token", true, "")
      })
      return 12
    }
    function copyWebhookEndpoint() { return 13 }
    function copyWebhookSetupField(fieldId) {
      fieldCopyRequests++
      lastCopiedField = String(fieldId)
      Qt.callLater(function() {
        fakeService.requestFinished(16, "webhook_copy_setup_field", true, "")
      })
      return 16
    }
    function setWebhookEnabled(enabled) { return 14 }
    function rotateWebhookToken() { return 15 }

    signal requestFinished(int requestId, string method, bool ok, string message)
  }

  Item { id: host }

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("Picker.qml"),
      Component.PreferSynchronous)
    if (component.status !== Component.Ready) {
      harness.fail("component load failed: " + component.errorString())
      return
    }
    picker = component.createObject(host, {
      service: fakeService,
      cardWidth: 620,
      cardHeight: 560
    })
    if (!picker) {
      harness.fail("picker creation returned null")
      return
    }
    Qt.callLater(harness.runAssertions)
  }
}

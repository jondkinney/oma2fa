import QtQuick
import Quickshell

ShellRoot {
  id: harness

  property var picker: null
  property var footer: null
  property var seenObjects: []

  readonly property var shortcutContract: [
    {
      pair: "pasteShortcut",
      group: "primaryHints",
      key: "Enter",
      action: "Paste"
    },
    {
      pair: "copyShortcut",
      group: "primaryHints",
      key: "Shift+Enter",
      action: "Copy only"
    },
    {
      pair: "removeShortcut",
      group: "secondaryHints",
      key: "Delete",
      action: "Remove"
    },
    {
      pair: "closeShortcut",
      group: "secondaryHints",
      key: "Esc",
      action: "Close"
    }
  ]

  function fail(message) {
    console.error("OMA2FA_PICKER_SHORTCUTS_FAIL: " + String(message))
    Qt.exit(1)
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

  function mappedRect(item, ancestor) {
    var origin = item.mapToItem(ancestor, 0, 0)
    return {
      x: origin.x,
      y: origin.y,
      width: item.width,
      height: item.height,
      right: origin.x + item.width,
      bottom: origin.y + item.height
    }
  }

  function expectContained(item, ancestor, label) {
    var rect = harness.mappedRect(item, ancestor)
    var tolerance = 0.5
    harness.expect(rect.x >= -tolerance,
      label + " starts before the footer")
    harness.expect(rect.y >= -tolerance,
      label + " starts above the footer")
    harness.expect(rect.right <= ancestor.width + tolerance,
      label + " overflows the footer horizontally")
    harness.expect(rect.bottom <= ancestor.height + tolerance,
      label + " overflows the footer vertically")
  }

  function validatePair(spec) {
    var pair = harness.named(spec.pair)
    var group = harness.named(spec.group)
    var key = harness.named(spec.pair + "Key")
    var keyLabel = harness.named(spec.pair + "KeyLabel")
    var action = harness.named(spec.pair + "Action")

    harness.expect(pair.parent === group,
      spec.pair + " is not grouped under " + spec.group)
    harness.expect(key.parent === pair,
      spec.pair + " keycap is not a direct child of its pair")
    harness.expect(action.parent === pair,
      spec.pair + " action is not a direct child of its pair")
    harness.expect(keyLabel.parent === key,
      spec.pair + " key label is not inside its keycap")

    harness.expect(String(keyLabel.text) === spec.key,
      spec.pair + " has the wrong key label")
    harness.expect(String(action.text) === spec.action,
      spec.pair + " has the wrong action label")
    harness.expect(keyLabel.textFormat === Text.PlainText,
      spec.pair + " key label must force PlainText")
    harness.expect(action.textFormat === Text.PlainText,
      spec.pair + " action label must force PlainText")
    harness.expect(String(pair.Accessible.name) === spec.key + ", " + spec.action,
      spec.pair + " has an unclear accessible label")

    var keyRight = key.x + key.width
    var intraPairGap = action.x - keyRight
    harness.expect(intraPairGap > 0,
      spec.pair + " keycap and action need a visible gap")
    harness.expect(action.x + action.width <= pair.width + 0.5,
      spec.pair + " action overflows its pair")
    harness.expect(key.width > keyLabel.implicitWidth,
      spec.pair + " keycap needs horizontal padding")
    harness.expect(key.border.width > 0 && key.border.color.a > 0,
      spec.pair + " keycap needs a visible border")
    harness.expect(key.color.a > 0,
      spec.pair + " keycap needs a visible fill")
    harness.expect(keyLabel.opacity > action.opacity,
      spec.pair + " key and action need distinct visual emphasis")

    return { pair: pair, gap: intraPairGap }
  }

  function validateInterPairSpacing(first, second, groupName) {
    var interPairGap = second.pair.x - (first.pair.x + first.pair.width)
    harness.expect(interPairGap > first.gap && interPairGap > second.gap,
      groupName + " needs more separation between pairs than within a pair")
  }

  function validateWideLayout() {
    harness.footer.width = 600
    harness.expect(harness.picker.footerStacked === false,
      "600px footer should use one line")

    var primary = harness.named("primaryHints")
    var secondary = harness.named("secondaryHints")
    var primaryRect = harness.mappedRect(primary, harness.footer)
    var secondaryRect = harness.mappedRect(secondary, harness.footer)
    harness.expect(primaryRect.right < secondaryRect.x,
      "wide shortcut groups overlap")
    harness.expect(Math.abs(primaryRect.y - secondaryRect.y) <= 0.5,
      "wide shortcut groups are not aligned")
    harness.expectContained(primary, harness.footer, "primary shortcut group")
    harness.expectContained(secondary, harness.footer, "secondary shortcut group")
  }

  function validateMinimumLayout() {
    harness.footer.width = 360
    harness.expect(harness.picker.footerStacked === true,
      "360px footer should stack the semantic groups")

    var primary = harness.named("primaryHints")
    var secondary = harness.named("secondaryHints")
    var primaryRect = harness.mappedRect(primary, harness.footer)
    var secondaryRect = harness.mappedRect(secondary, harness.footer)
    harness.expect(primaryRect.bottom < secondaryRect.y,
      "stacked shortcut groups overlap")
    harness.expectContained(primary, harness.footer, "stacked primary shortcut group")
    harness.expectContained(secondary, harness.footer,
      "stacked secondary shortcut group")

    for (var index = 0; index < harness.shortcutContract.length; index++)
      harness.expectContained(harness.named(harness.shortcutContract[index].pair),
        harness.footer, harness.shortcutContract[index].pair)
  }

  function runAssertions() {
    harness.footer = harness.named("shortcutFooter")

    var pairs = []
    for (var index = 0; index < harness.shortcutContract.length; index++)
      pairs.push(harness.validatePair(harness.shortcutContract[index]))
    harness.validateInterPairSpacing(pairs[0], pairs[1], "primary shortcut group")
    harness.validateInterPairSpacing(pairs[2], pairs[3], "secondary shortcut group")
    harness.validateWideLayout()
    harness.validateMinimumLayout()

    // Keep the picker logically in its opening phase while exercising the
    // shared key handler. This populates and filters the display model without
    // ever making the layer-shell PanelWindow visible.
    harness.picker.openingPending = true
    harness.picker.rebuildDisplay(false)
    Qt.callLater(harness.validateSearchAndListNavigation)
  }

  function pickerKey(key, text) {
    var event = {
      key: key,
      modifiers: Qt.NoModifier,
      text: text || "",
      accepted: false
    }
    harness.picker.handlePickerKey(event, true)
    harness.expect(event.accepted === true,
      "picker did not accept key " + String(key))
  }

  function expectSearchMode(label) {
    harness.expect(harness.picker.cursorActive === false,
      label + " should leave the result cursor inactive")
    // `searchModeActive` is also the predicate used by the caret bindings.
    // The actual caret remains hidden because this harness intentionally does
    // not show or focus the layer-shell window.
    harness.expect(harness.picker.searchModeActive === true,
      label + " should return the search caret mode")
  }

  function expectListMode(label) {
    harness.expect(harness.picker.cursorActive === true,
      label + " should activate the result cursor")
    harness.expect(harness.picker.searchModeActive === false,
      label + " should hide the search caret mode")
  }

  function validateSearchAndListNavigation() {
    harness.expectSearchMode("populated search")
    harness.expect(harness.picker.selectedIndex === 0,
      "search mode should retain the latest result as latent index zero")

    harness.pickerKey(Qt.Key_Down, "")
    harness.expectListMode("first Down from search")
    harness.expect(harness.picker.selectedIndex === 0,
      "first Down from search should select the latest result")

    harness.pickerKey(Qt.Key_Down, "")
    harness.expect(harness.picker.selectedIndex === 1,
      "second Down should select the next result")

    harness.pickerKey(Qt.Key_E, "e")
    harness.expect(harness.picker.filterText === "e",
      "typing from the list did not update the search filter")
    harness.expectSearchMode("typing from the list")
    harness.expect(harness.picker.selectedIndex === 0,
      "typing should reset the latent result index")

    harness.picker.setFilter("")
    harness.expectSearchMode("clearing the filter")
    harness.pickerKey(Qt.Key_Up, "")
    harness.expectListMode("first Up from search")
    harness.expect(harness.picker.selectedIndex === 1,
      "first Up from search should select the last result")

    harness.picker.setFilter("")
    harness.expectSearchMode("filter reset before arrival")
    fakeService.records = [
      {
        id: "newest-code",
        code: "333333",
        service: "Newest service",
        source: "test",
        received_ms: 3000,
        expires_ms: 4000,
        confidence: 0.9
      },
      fakeService.records[0],
      fakeService.records[1]
    ]
    harness.expectSearchMode("record arrival while searching")
    harness.expect(harness.picker.selectedIndex === 0,
      "record arrival should leave the newest result as latent index zero")

    harness.pickerKey(Qt.Key_Delete, "")
    harness.expect(fakeService.deleteCalls === 0,
      "Delete in search mode must not call the service")

    harness.pickerKey(Qt.Key_Down, "")
    harness.expectListMode("selection before a second arrival")
    fakeService.records = [
      {
        id: "fourth-code",
        code: "444444",
        service: "Fourth service",
        source: "test",
        received_ms: 4000,
        expires_ms: 5000,
        confidence: 0.9
      }
    ].concat(fakeService.records)
    harness.expectListMode("record arrival while navigating")
    harness.expect(harness.picker.selectedIndex === 1,
      "record arrival should preserve the visibly selected record")

    harness.picker.setFilter("no-such-result")
    harness.expectSearchMode("empty filtered results")
    harness.pickerKey(Qt.Key_Down, "")
    harness.expectSearchMode("Down with no results")
    harness.expect(harness.picker.selectedIndex === 0,
      "Down with no results should retain latent index zero")

    console.log("OMA2FA_PICKER_SHORTCUTS_PASS")
    harness.picker.close()
    harness.picker.destroy()
    Qt.exit(0)
  }

  QtObject {
    id: fakeService

    property bool bridgeAlive: true
    property bool ready: true
    property string lastError: ""
    property int deleteCalls: 0
    property var records: [
      {
        id: "newer-code",
        code: "222222",
        service: "Newer service",
        source: "test",
        received_ms: 2000,
        expires_ms: 3000,
        confidence: 0.9
      },
      {
        id: "older-code",
        code: "111111",
        service: "Older service",
        source: "test",
        received_ms: 1000,
        expires_ms: 3000,
        confidence: 0.9
      }
    ]
    property var status: ({ ready: true, sources: {} })

    function refresh() { return 0 }
    function deleteRecord(_recordId) { deleteCalls++ }
  }

  Item {
    id: host
    width: 900
    height: 700
  }

  Timer {
    id: assertionTimer
    interval: 50
    repeat: false
    onTriggered: harness.runAssertions()
  }

  Component.onCompleted: {
    var component = Qt.createComponent(Qt.resolvedUrl("Picker.qml"),
      Component.PreferSynchronous)
    if (component.status !== Component.Ready) {
      harness.fail("component load failed: " + component.errorString())
      return
    }
    harness.picker = component.createObject(host, { service: fakeService })
    if (!harness.picker) {
      harness.fail("picker creation returned null")
      return
    }
    assertionTimer.start()
  }
}

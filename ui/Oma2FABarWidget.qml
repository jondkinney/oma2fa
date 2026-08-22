import QtQuick
import qs.Commons
import qs.Ui

// A deliberately low-information entry point for the shared Oma2FA service.
// The bar may be visible in recordings or over someone's shoulder, so this
// widget exposes only a generic icon, a bounded record count, and a whitelisted
// connection state. Codes, service/sender names, timestamps, record IDs, and
// backend-provided error text never enter the bar or its tooltip.
BarWidget {
  id: root
  moduleName: "io.github.jondkinney.oma2fa"

  readonly property var omaService: {
    var host = root.bar && root.bar.shell ? root.bar.shell : null
    return host && typeof host.serviceFor === "function"
      ? host.serviceFor(root.moduleName) : null
  }
  readonly property int codeCount: root.omaService
    && Array.isArray(root.omaService.records)
    ? root.omaService.records.length : 0
  // Bar space is scarce and a large exact count adds no useful information.
  readonly property string badgeText: root.codeCount > 9
    ? "9+" : String(root.codeCount)
  readonly property string connectionState: {
    if (!root.omaService) return "unavailable"
    if (root.omaService.bridgeAlive !== true) return "connecting"
    if (root.omaService.ready !== true) return "loading"

    var value = root.omaService.status
    var state = value && typeof value === "object"
      ? String(value.state || value.status || "").toLowerCase() : ""
    if (state === "error" || state === "failed" || state === "unavailable")
      return "unavailable"
    return "ready"
  }
  readonly property string countLabel: root.codeCount === 0
    ? "No codes"
    : root.codeCount + (root.codeCount === 1 ? " code" : " codes")
  readonly property string tooltipLabel: {
    if (root.connectionState === "connecting") return "Oma2FA · Connecting"
    if (root.connectionState === "loading") return "Oma2FA · Loading"
    if (root.connectionState === "unavailable") return "Oma2FA · Unavailable"
    return "Oma2FA · " + root.countLabel
  }

  function togglePicker() {
    var host = root.bar && root.bar.shell ? root.bar.shell : null
    if (host && typeof host.toggle === "function")
      host.toggle(root.moduleName, "{}")
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    fixedWidth: root.vertical ? -1 : mark.implicitWidth + Style.spaceReal(12)
    fixedHeight: root.vertical ? Style.bar.iconSlot : -1
    tooltipText: root.tooltipLabel
    active: root.codeCount > 0
    activeColor: root.bar ? root.bar.urgent : Color.urgent
    dimmed: root.connectionState !== "ready"

    Accessible.role: Accessible.Button
    Accessible.name: root.tooltipLabel
    Accessible.description: "Open the recent verification code picker"
    Accessible.onPressAction: root.togglePicker()

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.togglePicker()
    }

    Row {
      id: mark
      anchors.centerIn: parent
      spacing: root.codeCount > 0 ? Style.space(2) : 0

      OpticalGlyph {
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        text: "󰍡"
        fontFamily: button.fontFamily
        fontSize: Style.bar.iconFont
        color: button.active && button.useActiveColor
          ? button.activeColor : button.foreground
      }

      Rectangle {
        id: badge
        visible: root.codeCount > 0
        anchors.verticalCenter: parent.verticalCenter
        width: Math.max(height, badgeLabel.implicitWidth + Style.spaceReal(4))
        height: Math.max(Style.space(11), Math.round(Style.bar.iconCanvas * 0.7))
        radius: height / 2
        color: button.activeColor

        Text {
          id: badgeLabel
          anchors.centerIn: parent
          text: root.badgeText
          textFormat: Text.PlainText
          color: root.bar ? root.bar.themeContrastForeground : Color.background
          font.family: button.fontFamily
          font.pixelSize: Math.max(7, Math.round(Style.font.caption * 0.72))
          font.bold: true
          renderType: Text.NativeRendering
        }
      }
    }
  }
}

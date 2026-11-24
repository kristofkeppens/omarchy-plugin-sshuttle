import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// sshuttle in the Omarchy bar: a shield in the bar that follows the tunnel's
// state, and a panel to connect, disconnect and manage profiles.
//
// Everything shown here that came from the helper (profile names, remotes,
// errors, log lines) is untrusted: every Text is PlainText, and templates are
// filled in one pass by fmt() so a "%1" inside a name is never expanded.
Panel {
  id: root
  moduleName: "io.github.kristofkeppens.sshuttle"
  ipcTarget: "io.github.kristofkeppens.sshuttle"

  // Items inside the panel's lazily built content, null until it first opens.
  readonly property Flickable panelFlick: panel.loadedContent ? panel.loadedContent.panelFlick : null

  // "" is the main page; "editor" is the profile form.
  property string page: ""
  property int rowIndex: 0
  property bool cursorActive: false
  property string selectedName: ""
  property bool logShown: false
  property bool confirmOpen: false
  // What the confirmation is for: "delete" (pendingDelete) or "sudo".
  property string confirmKind: ""
  property string pendingDelete: ""

  // Editor form. The fields bind to these, so opening the editor resets them.
  property string editOriginal: ""
  property string formName: ""
  property string formRemote: ""
  property string formSubnets: ""
  property string formExcludes: ""
  property bool formDns: false
  property string formMethod: "auto"
  property bool formIpv6: false
  property string formIdentity: ""
  property string editorError: ""
  // Saving under another profile's name would silently replace it.
  property bool nameTaken: false
  property bool saving: false
  property int editorToken: 0

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property int iconSize: Math.max(8, Math.min(24, Math.round(Number(setting("iconSize", 18)) || 18)))
  readonly property int refreshIntervalSec: Math.max(2, Math.min(60, Math.round(Number(setting("refreshIntervalSec", 5)) || 5)))
  readonly property bool showLabel: setting("showLabel", false) === true

  // Shield with a lock (nf-md-shield_lock).
  readonly property string glyph: String.fromCodePoint(0xF099D)

  readonly property string stateKey: Model.stateKey(svc.status)
  readonly property bool installed: stateKey !== "unavailable"
  readonly property bool isOn: stateKey === "connected" || stateKey === "connecting"
  readonly property string activeProfile: svc.status.profile || ""
  readonly property string commandErrorText: translateError(svc.commandError)
  readonly property string statusErrorText: svc.statusError !== "" ? translateError(svc.statusError) : (svc.status.error || "")
  readonly property bool showStatusError: installed && statusErrorText !== ""
  readonly property bool needsSudo: installed && svc.status.sudo_ready !== true
  // A recorded failure (also the connect timeout) stays until stop clears it.
  readonly property bool canClearError: installed && stateKey === "error"

  // The profile the list highlights and the switch connects: the one clicked,
  // else the default, else the first.
  readonly property string selectedProfile: {
    var names = svc.profiles.map(function(p) { return p.name })
    if (selectedName !== "" && names.indexOf(selectedName) >= 0) return selectedName
    if (svc.defaultProfile !== "" && names.indexOf(svc.defaultProfile) >= 0) return svc.defaultProfile
    return names.length > 0 ? names[0] : ""
  }

  readonly property string stateWord: stateKey === "connected" ? strings.t("sshuttle.connected")
    : stateKey === "connecting" ? strings.t("sshuttle.connecting")
    : stateKey === "disconnected" ? strings.t("sshuttle.disconnected")
    : strings.t("sshuttle.error")

  readonly property string heroMeta: isOn ? activeProfile
    : !installed ? strings.t("sshuttle.unavailable")
    : selectedProfile !== "" ? fmt(strings.t("sshuttle.connectsTo"), selectedProfile)
    : strings.t("sshuttle.noProfiles")

  readonly property string barTooltip: stateKey === "connected" ? fmt(strings.t("sshuttle.tooltip.connected"), activeProfile)
    : stateKey === "connecting" ? fmt(strings.t("sshuttle.tooltip.connecting"), activeProfile)
    : stateKey === "error" ? fmt(strings.t("sshuttle.tooltip.error"), statusErrorText !== "" ? statusErrorText : strings.t("sshuttle.error"))
    : stateKey === "unavailable" ? strings.t("sshuttle.tooltip.unavailable")
    : strings.t("sshuttle.tooltip.disconnected")

  readonly property color barIconColor: stateKey === "error" || stateKey === "unavailable" ? urgent
    : isOn ? barForeground
    : Qt.darker(barForeground, 1.55)
  readonly property color heroIconColor: stateKey === "error" || stateKey === "unavailable" ? urgent : foreground
  readonly property bool barLabelShown: showLabel && isOn && activeProfile !== "" && !(bar && bar.vertical)

  // Opacity of the bar glyph; pulses while connecting.
  property real pulse: 1

  readonly property var mainRows: {
    var r = []
    if (installed) r.push("power")
    if (commandErrorText !== "") r.push("dismiss")
    if (showStatusError) r.push("log")
    if (showStatusError && canClearError) r.push("clear")
    if (needsSudo) r.push("sudo")
    for (var i = 0; i < svc.profiles.length; i++) r.push("profile:" + i)
    r.push("add")
    if (selectedProfile !== "") r.push("edit", "delete")
    return r
  }
  readonly property var textRows: ["name", "remote", "subnets", "excludes", "identity"]
  readonly property var editorRows: ["name", "remote", "subnets", "excludes", "dns", "method", "ipv6", "identity", "save", "cancel"]
  readonly property var rows: page === "editor" ? editorRows : mainRows
  readonly property string currentRow: cursorActive && rows.length > 0 ? rows[Math.max(0, Math.min(rowIndex, rows.length - 1))] : ""

  // Fill %1/%2 in one pass: a placeholder inside an argument is left alone.
  function fmt(template, a, b) {
    return String(template).replace(/%([12])/g, function(m, d) {
      if (d === "1") return String(a)
      return b === undefined ? m : String(b)
    })
  }

  // Service.qml reports its own failures in English; show them translated.
  function translateError(msg) {
    var s = String(msg || "")
    if (s === "Helper timed out") return strings.t("sshuttle.commandTimedOut")
    if (s === "Invalid response from helper") return strings.t("sshuttle.helperFailed")
    if (s === "Helper failed") return strings.t("sshuttle.helperError")
    return s
  }

  function summaryText(profile) {
    var parts = Model.summaryParts(profile)
    var what = parts.allTraffic ? strings.t("sshuttle.allTraffic")
      : fmt(strings.tn("sshuttle.subnets.count", parts.subnetCount), Commons.I18n.formatNumber(parts.subnetCount))
    return fmt(strings.t("sshuttle.summary"), parts.remote, what)
  }

  function profileByName(name) {
    for (var i = 0; i < svc.profiles.length; i++) if (svc.profiles[i].name === name) return svc.profiles[i]
    return null
  }

  function focusCatcher() {
    if (panel.loadedContent) panel.loadedContent.forceActiveFocus()
  }

  function setRowCursor(id) {
    var i = rows.indexOf(id)
    if (i < 0) return
    cursorActive = true
    rowIndex = i
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    if (dy === 0 || rows.length === 0) return
    rowIndex = Math.max(0, Math.min(rows.length - 1, Math.min(rowIndex, rows.length - 1) + dy))
  }

  // Put the cursor on an editor row and, for a text field, the caret in it.
  function focusRow(id) {
    setRowCursor(id)
    if (textRows.indexOf(id) >= 0 && panel.loadedContent) panel.loadedContent.focusField(id)
    else focusCatcher()
  }

  function nextEditorRow(id, step) {
    var i = editorRows.indexOf(id)
    return editorRows[Math.max(0, Math.min(editorRows.length - 1, i + step))]
  }

  function activateCursor() {
    activate(currentRow)
  }

  function activate(id) {
    if (id === "") return
    if (page === "editor") {
      if (textRows.indexOf(id) >= 0) focusRow(id)
      else if (id === "dns") formDns = !formDns
      else if (id === "ipv6") formIpv6 = !formIpv6
      else if (id === "method") { if (panel.loadedContent) panel.loadedContent.openMethod() }
      else if (id === "save") saveEditor()
      else if (id === "cancel") leaveEditor()
      return
    }
    if (id === "power") togglePower()
    else if (id === "dismiss") svc.dismissError()
    else if (id === "log") toggleLog()
    else if (id === "clear") clearError()
    else if (id === "sudo") askSetupSudo()
    else if (id === "add") openEditor("")
    else if (id === "edit") openEditor(selectedProfile)
    else if (id === "delete") askDelete(selectedProfile)
    else if (id.indexOf("profile:") === 0) {
      var p = svc.profiles[Number(id.substring(8))]
      if (p) selectedName = p.name
    }
  }

  // The profile under the cursor, else the selected one.
  function cursorProfile() {
    if (currentRow.indexOf("profile:") === 0) {
      var p = svc.profiles[Number(currentRow.substring(8))]
      if (p) return p.name
    }
    return selectedProfile
  }

  function togglePower() {
    if (!installed || svc.busy) return
    if (isOn) svc.disconnect()
    else if (needsSudo) return
    else if (selectedProfile !== "") svc.connect(selectedProfile)
    else openEditor("")
  }

  function toggleLog() {
    logShown = !logShown
    if (logShown) svc.loadLog()
  }

  // `stop` drops the recorded error (and any leftover process).
  function clearError() {
    if (!canClearError) return
    logShown = false
    svc.disconnect()
  }

  function askDelete(name) {
    if (name === "") return
    pendingDelete = name
    confirmKind = "delete"
    confirmOpen = true
  }

  // The sudoers rule lets any program running as this user become root
  // without a password; say so before polkit asks.
  function askSetupSudo() {
    if (!needsSudo) return
    pendingDelete = ""
    confirmKind = "sudo"
    confirmOpen = true
  }

  function confirmAction() {
    if (!confirmOpen) return
    var kind = confirmKind
    var name = pendingDelete
    cancelConfirm()
    if (kind === "sudo") {
      svc.setupSudo()
      return
    }
    if (kind !== "delete" || name === "") return
    if (selectedName === name) selectedName = ""
    svc.deleteProfile(name)
  }

  // confirmKind is left as is so the closing dialog keeps its wording.
  function cancelConfirm() {
    confirmOpen = false
    pendingDelete = ""
  }

  function openEditor(name) {
    var p = name !== "" ? profileByName(name) : null
    var f = Model.profileToForm(p || { subnets: [] })
    editOriginal = p ? p.name : ""
    formName = f.name
    formRemote = f.remote
    formSubnets = f.subnetsText
    formExcludes = f.excludesText
    formDns = f.dns
    formMethod = f.method
    formIpv6 = f.disableIpv6
    formIdentity = f.sshIdentity
    editorError = ""
    nameTaken = false
    saving = false
    editorToken += 1
    cancelConfirm()
    page = "editor"
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function() { if (root.page === "editor") root.focusRow("name") })
  }

  function leaveEditor() {
    // The failed save's message was shown inline; do not leave it behind as
    // a second card on the main page.
    if (editorError !== "" && svc.commandError === editorError) svc.dismissError()
    editorError = ""
    nameTaken = false
    saving = false
    editorToken += 1
    page = ""
    rowIndex = Math.max(0, mainRows.indexOf("add"))
    if (panelFlick) panelFlick.contentY = 0
    focusCatcher()
  }

  function saveEditor() {
    if (saving) return
    var profile = Model.formToProfile({
      name: formName, remote: formRemote, subnetsText: formSubnets, excludesText: formExcludes,
      dns: formDns, method: formMethod, disableIpv6: formIpv6, sshIdentity: formIdentity
    })
    var original = editOriginal
    nameTaken = profile.name !== "" && profile.name !== original && profileByName(profile.name) !== null
    if (nameTaken) return
    var wasDefault = original !== "" && original === svc.defaultProfile
    var token = editorToken
    saving = true
    editorError = ""
    svc.saveProfile(profile, function(result) {
      // A rename saves under the new name; drop the old entry and carry the
      // default over, even if the editor was left in the meantime.
      if (result.ok && original !== "" && original !== profile.name) {
        svc.deleteProfile(original)
        if (wasDefault) svc.setDefault(profile.name)
      }
      if (token !== root.editorToken) return
      root.saving = false
      if (!result.ok) {
        root.editorError = String(result.error || "")
        return
      }
      root.selectedName = profile.name
      root.leaveEditor()
    })
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: if (opened) {
    cursorActive = false
    rowIndex = 0
    cancelConfirm()
    logShown = false
    if (panelFlick && page !== "editor") panelFlick.contentY = 0
    svc.refresh()
    svc.refreshProfiles()
    Qt.callLater(focusCatcher)
  }
  onFormNameChanged: nameTaken = false
  onCurrentRowChanged: if (panel.loadedContent && currentRow !== "") {
    var id = currentRow
    Qt.callLater(function() { if (panel.loadedContent) panel.loadedContent.ensureVisible(id) })
  }

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  Service {
    id: svc
    refreshIntervalSec: root.refreshIntervalSec
  }

  // A gentle pulse on the bar glyph while the tunnel comes up. Two short
  // halves rather than one long animation keep each under 300ms. Off when
  // animations are off, where Style.duration() is 0 and would spin.
  SequentialAnimation {
    running: root.stateKey === "connecting" && !Style.reduceMotion
    loops: Animation.Infinite
    onRunningChanged: if (!running) root.pulse = 1
    NumberAnimation { target: root; property: "pulse"; to: 0.4; duration: Style.duration(280); easing.type: Easing.InOutQuad }
    NumberAnimation { target: root; property: "pulse"; to: 1.0; duration: Style.duration(280); easing.type: Easing.InOutQuad }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Drawn below rather than handed to the button, so the iconSize setting
    // and the optional label both apply.
    iconComponent: Component { Item {} }
    fixedWidth: button.vertical ? -1 : (root.barLabelShown ? barLabel.implicitWidth + Style.space(4) + button.slotSize : button.slotSize)
    tooltipText: root.barTooltip

    Row {
      x: (button.width - width) / 2
      y: (button.height - height) / 2
      spacing: Style.space(4)
      scale: button.pressScale

      Text {
        id: barLabel
        textFormat: Text.PlainText
        visible: root.barLabelShown
        anchors.verticalCenter: parent.verticalCenter
        text: root.activeProfile
        color: root.barIconColor
        font.family: root.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }

      Text {
        textFormat: Text.PlainText
        anchors.verticalCenter: parent.verticalCenter
        text: root.glyph
        color: root.barIconColor
        opacity: root.pulse
        font.family: root.fontFamily
        // 18 (the default) draws at the bar's own icon size.
        font.pixelSize: Math.max(6, Math.round(Style.bar.iconFont * root.iconSize / 18))
      }
    }

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) svc.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(600))

    content: PanelKeyCatcher {
      id: keyCatcher
      property alias panelFlick: flick
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      // Text inputs and the open method list own the keys while they have them.
      blocked: root.page === "editor" && (nameField.activeFocus || remoteField.activeFocus
        || subnetsArea.activeFocus || excludesArea.activeFocus || identityField.activeFocus
        || methodDropdown.popupOpen)

      function focusField(id) {
        var field = id === "name" ? nameField : id === "remote" ? remoteField
          : id === "subnets" ? subnetsArea : id === "excludes" ? excludesArea
          : id === "identity" ? identityField : null
        if (field) field.forceActiveFocus()
      }

      function openMethod() { methodDropdown.open() }

      function findRow(item, name) {
        if (!item) return null
        if (item.objectName === name) return item
        var kids = item.children || []
        for (var i = 0; i < kids.length; i++) {
          var hit = findRow(kids[i], name)
          if (hit) return hit
        }
        return null
      }

      function ensureVisible(id) {
        var item = findRow(column, "row:" + id)
        if (!item || !item.visible) return
        var margin = Style.space(6)
        var top = item.mapToItem(flick.contentItem, 0, 0).y
        var bottom = top + item.height
        var maxY = Math.max(0, flick.contentHeight - flick.height)
        if (top < flick.contentY + margin) flick.contentY = Math.max(0, top - margin)
        else if (bottom > flick.contentY + flick.height - margin) flick.contentY = Math.min(maxY, bottom + margin - flick.height)
      }

      onMoveRequested: function(dx, dy) {
        if (root.confirmOpen) {
          if (dx !== 0) confirmDialog.selectedIndex = confirmDialog.selectedIndex === 0 ? 1 : 0
          return
        }
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: {
        if (root.confirmOpen) {
          if (confirmDialog.selectedIndex === 0) root.cancelConfirm()
          else root.confirmAction()
          return
        }
        if (root.cursorActive) root.activateCursor()
      }
      onCloseRequested: {
        if (root.confirmOpen) root.cancelConfirm()
        else if (root.page === "editor") root.leaveEditor()
        else root.close()
      }
      onDeleteRequested: if (!root.confirmOpen && root.page === "") root.askDelete(root.cursorProfile())
      onTabRequested: function(direction) {
        if (root.confirmOpen) confirmDialog.selectedIndex = confirmDialog.selectedIndex === 0 ? 1 : 0
        else if (root.page === "editor") root.focusRow(root.nextEditorRow(root.currentRow !== "" ? root.currentRow : "name", direction))
        else root.switchPanel(direction)
      }
      onTextKey: function(t) {
        if (root.confirmOpen || root.page !== "") return
        var key = t.toLowerCase()
        if (key === "a") root.openEditor("")
        else if (key === "e" && root.selectedProfile !== "") root.openEditor(root.cursorProfile())
        else if (key === "d" && root.cursorProfile() !== "") svc.setDefault(root.cursorProfile())
        else if (key === "t") root.togglePower()
        else if (key === "r") { svc.refresh(); svc.refreshProfiles() }
      }

      // Method choices follow the form; the dropdown sets its own value when
      // picked, which would otherwise cut its binding.
      Connections {
        target: root
        function onFormMethodChanged() { methodDropdown.value = root.formMethod }
      }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        // Inset from the clipped edges so borders are never shaved.
        Column {
          id: column
          x: Style.space(2)
          width: flick.width - Style.space(4)
          spacing: Style.space(12)

          // ---- Main page --------------------------------------------------

          Item {
            id: header
            visible: root.page === ""
            objectName: "row:power"
            width: parent.width
            implicitHeight: hero.implicitHeight
            // Reached from the hero's trailingControl, as in Omarchy's
            // Tailscale panel.
            readonly property bool ringVisible: root.currentRow === "power"
            function focusHero() { root.setRowCursor("power") }

            PanelHero {
              id: hero
              width: parent.width
              title: root.stateWord
              meta: root.heroMeta
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: root.isOn || !root.installed || root.stateKey === "error" ? 1.0 : 0.5
              iconComponent: Component {
                Text {
                  textFormat: Text.PlainText
                  text: root.glyph
                  color: root.heroIconColor
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
              trailingControl: Component {
                ToggleSwitch {
                  id: powerSwitch
                  checked: root.isOn
                  busy: svc.busy
                  // Without the sudo grant a connect would hang in sudo; the
                  // sudo card is the call to action. Disconnect stays possible.
                  interactive: root.installed && (root.isOn || !root.needsSudo)
                  opacity: interactive ? 1.0 : 0.4
                  hasCursor: header.ringVisible
                  foreground: hero.foreground
                  onHovered: function(on) { if (on) header.focusHero() }
                  onToggled: root.togglePower()

                  PanelToolTip {
                    visible: powerSwitch.containsMouse
                    text: root.isOn ? strings.t("sshuttle.disconnect") : strings.t("sshuttle.connect")
                    fontFamily: hero.fontFamily
                  }
                }
              }
            }
          }

          Card {
            visible: root.page === "" && root.commandErrorText !== ""
            rowId: "dismiss"
            message: root.commandErrorText
            urgentTone: true
            actionText: strings.t("sshuttle.dismiss")
            onAction: svc.dismissError()
          }

          Card {
            visible: root.page === "" && !root.installed
            message: strings.t("sshuttle.unavailable")
            urgentTone: true
          }

          Card {
            visible: root.page === "" && root.showStatusError
            rowId: "log"
            message: root.statusErrorText
            urgentTone: true
            actionText: root.logShown ? strings.t("sshuttle.hideLog") : strings.t("sshuttle.showLog")
            onAction: root.toggleLog()
            secondaryRowId: "clear"
            secondaryText: root.canClearError ? strings.t("sshuttle.clearError") : ""
            onSecondaryAction: root.clearError()

            Text {
              textFormat: Text.PlainText
              visible: root.logShown
              width: parent.width
              text: svc.logLines.length > 0 ? svc.logLines.join("\n") : strings.t("sshuttle.logEmpty")
              color: root.dim
              font.family: "monospace"
              font.pixelSize: Style.font.caption
              wrapMode: Text.WrapAnywhere
            }
          }

          Card {
            visible: root.page === "" && root.needsSudo
            rowId: "sudo"
            message: strings.t("sshuttle.sudoNeeded")
            actionText: strings.t("sshuttle.setUp")
            onAction: root.askSetupSudo()
          }

          PanelSeparator {
            visible: root.page === ""
            foreground: root.foreground
          }

          Column {
            visible: root.page === ""
            width: parent.width
            spacing: Style.space(6)

            PanelSectionHeader {
              text: strings.t("sshuttle.profiles")
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              textFormat: Text.PlainText
              visible: svc.profiles.length === 0
              width: parent.width
              text: strings.t("sshuttle.noProfiles")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Repeater {
              model: svc.profiles
              ProfileRow {
                required property var modelData
                required property int index
                profile: modelData
                rowNumber: index
              }
            }
          }

          Flow {
            visible: root.page === ""
            width: parent.width
            spacing: Style.space(8)

            ActionButton { rowId: "add"; text: strings.t("sshuttle.addProfile"); onClicked: root.openEditor("") }
            ActionButton {
              rowId: "edit"
              visible: root.selectedProfile !== ""
              text: strings.t("sshuttle.edit")
              onClicked: root.openEditor(root.selectedProfile)
            }
            ActionButton {
              rowId: "delete"
              visible: root.selectedProfile !== ""
              text: strings.t("sshuttle.delete")
              onClicked: root.askDelete(root.selectedProfile)
            }
          }

          // ---- Editor page ------------------------------------------------

          Column {
            visible: root.page === "editor"
            width: parent.width
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: root.editOriginal !== "" ? strings.t("sshuttle.editProfile") : strings.t("sshuttle.addProfile")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }

            FieldLabel { text: strings.t("sshuttle.name") }
            LineField {
              id: nameField
              rowId: "name"
              maximumLength: 40
              text: root.formName
              onTextEdited: root.formName = text
            }

            FieldLabel { text: strings.t("sshuttle.remote") }
            LineField {
              id: remoteField
              rowId: "remote"
              maximumLength: 300
              placeholderText: strings.t("sshuttle.remotePlaceholder")
              text: root.formRemote
              onTextEdited: root.formRemote = text
            }

            FieldLabel { text: strings.t("sshuttle.subnets") }
            MultiLineField {
              id: subnetsArea
              rowId: "subnets"
              placeholderText: strings.t("sshuttle.subnetsPlaceholder")
              text: root.formSubnets
              onTextChanged: if (text !== root.formSubnets) root.formSubnets = text
            }

            FieldLabel { text: strings.t("sshuttle.excludes") }
            MultiLineField {
              id: excludesArea
              rowId: "excludes"
              placeholderText: strings.t("sshuttle.excludesPlaceholder")
              text: root.formExcludes
              onTextChanged: if (text !== root.formExcludes) root.formExcludes = text
            }

            Toggle {
              objectName: "row:dns"
              width: parent.width
              label: strings.t("sshuttle.dns")
              checked: root.formDns
              hasCursor: root.currentRow === "dns"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.formDns = !root.formDns
              onHovered: function(on) { if (on) root.setRowCursor("dns") }
            }

            Dropdown {
              id: methodDropdown
              objectName: "row:method"
              width: parent.width
              label: strings.t("sshuttle.method")
              value: root.formMethod
              options: ["auto", "nft", "nat", "tproxy"]
              hasCursor: root.currentRow === "method"
              fontFamily: root.fontFamily
              onChanged: function(value) { root.formMethod = value }
              onHovered: function(on) { if (on) root.setRowCursor("method") }
            }

            Toggle {
              objectName: "row:ipv6"
              width: parent.width
              label: strings.t("sshuttle.disableIpv6")
              checked: root.formIpv6
              hasCursor: root.currentRow === "ipv6"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.formIpv6 = !root.formIpv6
              onHovered: function(on) { if (on) root.setRowCursor("ipv6") }
            }

            FieldLabel { text: strings.t("sshuttle.sshIdentity") }
            LineField {
              id: identityField
              rowId: "identity"
              maximumLength: 4096
              placeholderText: strings.t("sshuttle.sshIdentityPlaceholder")
              text: root.formIdentity
              onTextEdited: root.formIdentity = text
            }

            // Helper validation errors stay here, under the form, and the
            // editor stays open so the input can be fixed.
            Text {
              textFormat: Text.PlainText
              visible: root.nameTaken || root.editorError !== ""
              width: parent.width
              text: root.nameTaken ? strings.t("sshuttle.nameTaken") : root.translateError(root.editorError)
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Row {
              spacing: Style.space(8)

              ActionButton {
                rowId: "save"
                enabled: !root.saving
                opacity: root.saving ? 0.6 : 1.0
                text: root.saving ? strings.t("sshuttle.saving") : strings.t("sshuttle.save")
                onClicked: root.saveEditor()
              }
              ActionButton {
                rowId: "cancel"
                text: strings.t("sshuttle.cancel")
                onClicked: root.leaveEditor()
              }
            }
          }
        }
      }

      ConfirmDialog {
        id: confirmDialog
        anchors.fill: parent
        z: 10
        opened: root.confirmOpen
        message: root.confirmKind === "sudo" ? strings.t("sshuttle.sudoConfirm")
          : root.fmt(strings.t("sshuttle.deleteConfirm"), root.pendingDelete)
        cancelText: strings.t("sshuttle.cancel")
        confirmText: root.confirmKind === "sudo" ? strings.t("sshuttle.setUp") : strings.t("sshuttle.delete")
        foreground: root.foreground
        fontFamily: root.fontFamily
        // Start on Cancel: Enter alone must not delete or grant root.
        onOpenedChanged: if (opened) selectedIndex = 0
        onCanceled: root.cancelConfirm()
        onConfirmed: root.confirmAction()
      }
    }
  }

  // ---- Components ----------------------------------------------------------

  // A bordered note with an optional action button; extra children (the log)
  // go underneath the message.
  component Card: BorderSurface {
    id: card
    property string rowId: ""
    property string message: ""
    property string actionText: ""
    property bool urgentTone: false
    // Optional second button (its own cursor row) next to the first.
    property string secondaryRowId: ""
    property string secondaryText: ""
    default property alias extra: extraColumn.data
    signal action()
    signal secondaryAction()

    objectName: rowId !== "" ? "row:" + rowId : ""
    width: parent ? parent.width : implicitWidth
    implicitHeight: cardContent.implicitHeight + Style.space(16)
    radius: Style.cornerRadius
    color: "transparent"
    borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)

    ColumnLayout {
      id: cardContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(6)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: card.message
          color: card.urgentTone ? root.urgent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        Button {
          visible: card.actionText !== ""
          text: card.actionText
          bordered: true
          hasCursor: card.rowId !== "" && root.currentRow === card.rowId
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          Layout.alignment: Qt.AlignVCenter
          onHovered: function(on) { if (on) root.setRowCursor(card.rowId) }
          onClicked: card.action()
        }

        Button {
          objectName: card.secondaryRowId !== "" ? "row:" + card.secondaryRowId : ""
          visible: card.secondaryText !== ""
          text: card.secondaryText
          bordered: true
          hasCursor: card.secondaryRowId !== "" && root.currentRow === card.secondaryRowId
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          Layout.alignment: Qt.AlignVCenter
          onHovered: function(on) { if (on) root.setRowCursor(card.secondaryRowId) }
          onClicked: card.secondaryAction()
        }
      }

      Column {
        id: extraColumn
        Layout.fillWidth: true
      }
    }
  }

  // Name and summary; a click selects it, and the trailing action makes it
  // the default.
  component ProfileRow: CursorSurface {
    id: profileRow
    property var profile: null
    property int rowNumber: 0
    readonly property string name: profile ? String(profile.name) : ""
    readonly property string rowId: "profile:" + rowNumber
    readonly property bool isDefault: name !== "" && name === svc.defaultProfile

    objectName: "row:" + rowId
    width: parent ? parent.width : implicitWidth
    hasCursor: root.currentRow === rowId
    current: name === root.selectedProfile
    foreground: root.foreground
    implicitHeight: profileContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setRowCursor(profileRow.rowId)
      onClicked: root.selectedName = profileRow.name
    }

    RowLayout {
      id: profileContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(8)

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: profileRow.name
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: profileRow.name === root.activeProfile && root.isOn
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: root.summaryText(profileRow.profile)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Text {
        textFormat: Text.PlainText
        visible: profileRow.isDefault
        text: strings.t("sshuttle.default")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        Layout.alignment: Qt.AlignVCenter
      }

      Button {
        visible: !profileRow.isDefault
        text: strings.t("sshuttle.setDefault")
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.caption
        Layout.alignment: Qt.AlignVCenter
        onHovered: function(on) { if (on) root.setRowCursor(profileRow.rowId) }
        onClicked: svc.setDefault(profileRow.name)
      }
    }
  }

  component ActionButton: Button {
    id: actionButton
    property string rowId: ""
    objectName: "row:" + rowId
    bordered: true
    hasCursor: root.currentRow === rowId
    foreground: root.foreground
    fontFamily: root.fontFamily
    onHovered: function(on) { if (on) root.setRowCursor(actionButton.rowId) }
  }

  component FieldLabel: Text {
    textFormat: Text.PlainText
    width: parent ? parent.width : implicitWidth
    color: Qt.darker(root.foreground, 1.4)
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
    elide: Text.ElideRight
  }

  component LineField: TextField {
    id: lineField
    property string rowId: ""
    objectName: "row:" + rowId
    width: parent ? parent.width : implicitWidth
    foreground: root.foreground
    hasCursor: root.currentRow === rowId
    selectByMouse: true
    onHoveredChanged: if (hovered) root.setRowCursor(lineField.rowId)
    onActiveFocusChanged: if (activeFocus) root.setRowCursor(lineField.rowId)
    Keys.onEscapePressed: root.focusCatcher()
    Keys.onReturnPressed: root.focusRow(root.nextEditorRow(lineField.rowId, 1))
    Keys.onEnterPressed: root.focusRow(root.nextEditorRow(lineField.rowId, 1))
    Keys.onTabPressed: root.focusRow(root.nextEditorRow(lineField.rowId, 1))
    Keys.onBacktabPressed: root.focusRow(root.nextEditorRow(lineField.rowId, -1))
  }

  // The kit has no multi-line input; a Controls TextArea dressed like its
  // TextField. Return adds a line; Tab and Escape leave.
  component MultiLineField: TextArea {
    id: area
    property string rowId: ""
    readonly property bool hot: hovered || root.currentRow === rowId
    readonly property var areaBorder: Border.controlSpec(activeFocus ? "focus" : (hot ? "hover-cursor" : "normal"), root.foreground, Commons.Color.accent)

    objectName: "row:" + rowId
    width: parent ? parent.width : implicitWidth
    height: Math.max(implicitHeight, Style.space(60))
    textFormat: TextEdit.PlainText
    wrapMode: TextEdit.WrapAnywhere
    selectByMouse: true
    color: root.foreground
    selectionColor: Style.selectionFillFor(root.foreground, Commons.Color.accent)
    selectedTextColor: root.foreground
    placeholderTextColor: Qt.darker(root.foreground, 1.6)
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    leftPadding: Style.spacing.controlPaddingX + Border.left(areaBorder)
    rightPadding: Style.spacing.controlPaddingX + Border.right(areaBorder)
    topPadding: Style.spacing.inputPaddingY + Border.top(areaBorder)
    bottomPadding: Style.spacing.inputPaddingY + Border.bottom(areaBorder)
    onHoveredChanged: if (hovered) root.setRowCursor(area.rowId)
    onActiveFocusChanged: if (activeFocus) root.setRowCursor(area.rowId)
    Keys.onEscapePressed: root.focusCatcher()
    Keys.onTabPressed: root.focusRow(root.nextEditorRow(area.rowId, 1))
    Keys.onBacktabPressed: root.focusRow(root.nextEditorRow(area.rowId, -1))

    background: BorderSurface {
      color: Style.controlFill(area.activeFocus, area.hot, root.foreground, Commons.Color.accent)
      borderSpec: area.areaBorder
      radius: Style.cornerRadius
    }
  }
}

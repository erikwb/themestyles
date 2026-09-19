pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.weirdware.themestyles"
  ipcTarget: moduleName
  manageIpc: false

  readonly property string helper: decodeURIComponent(Qt.resolvedUrl("theme-styles").toString().replace(/^file:\/\//, ""))
  property var state: ({})
  property var savedStyles: []
  property string stylesJson: ""
  property string message: ""
  property bool messageError: false
  property string commandTheme: ""
  property string commandAction: ""
  property real requestStarted: 0
  property real clockNow: Date.now()
  property var agents: []
  property var agentDiagnostics: []
  property string agentsTheme: ""
  property string harness: ""
  property string model: ""
  property string thinking: ""
  readonly property var selectedHarness: agents.find(item => item.value === root.harness) || ({models: []})
  readonly property var models: selectedHarness.models || []
  readonly property real modelLabelWidth: models.reduce((width, item) =>
    Math.max(width, modelFontMetrics.advanceWidth(item.label)), 0)
  readonly property var selectedModel: models.find(item => item.value === root.model) || ({thinking: []})
  readonly property var thinkingLevels: selectedModel.thinking || []
  property var deleteTarget: null
  readonly property string base: state.base || ""
  readonly property var job: state.job || ({})
  readonly property bool jobRunning: ["starting", "generating", "theming", "saving", "applying"].indexOf(job.state) >= 0
  readonly property bool startingGeneration: busy && commandAction === "start" && commandTheme === base
  readonly property bool generating: startingGeneration || jobRunning
  readonly property string generationStage: jobRunning ? job.state : "starting"
  readonly property real generationStarted: jobRunning && job.started ? job.started * 1000 : requestStarted
  readonly property int elapsedSeconds: Math.max(0, Math.floor((clockNow - generationStarted) / 1000))
  readonly property string elapsedText: Math.floor(elapsedSeconds / 60) + ":" + ("0" + (elapsedSeconds % 60)).slice(-2)
  readonly property string stageLabel: jobRunning && job.cancel_requested ? "Cancelling…" : ({
    starting: "Starting image generation…",
    generating: "Creating your wallpaper…",
    theming: "Matching application colors…",
    saving: "Saving your style…",
    applying: "Applying your style…"
  })[generationStage]
  readonly property bool busy: actionProcess.running
  readonly property bool ready: base !== "" && agentsTheme === base && harness !== "" && model !== ""
    && !agentProcess.running && !(state.missing || []).length
  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.6)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  FontMetrics {
    id: modelFontMetrics
    font.family: Style.font.family
    font.pixelSize: Style.font.body
  }

  function refresh() {
    if (!statusProcess.running) statusProcess.running = true
  }

  function refreshAgents() {
    if (root.base && !agentProcess.running) agentProcess.running = true
  }

  function receiveAgents(result) {
    if (!result.ok) {
      root.agents = []
      root.agentsTheme = ""
      root.agentDiagnostics = []
      root.harness = ""
      root.model = ""
      root.thinking = ""
      root.message = result.error || "Unable to check agent accounts. Reopen the panel to retry."
      root.messageError = true
      return
    }
    if (result.base !== root.base) return
    root.agents = result.agents || []
    root.agentDiagnostics = result.diagnostics || []
    root.agentsTheme = result.base
    root.harness = (result.selection || {}).harness || ""
    root.model = (result.selection || {}).model || ""
    root.thinking = (result.selection || {}).thinking || ""
  }

  function chooseHarness(value) {
    var selected = root.agents.find(item => item.value === value)
    if (!selected) return
    modelPicker.close()
    thinkingPicker.close()
    root.harness = value
    root.model = selected.model || ""
    root.thinking = selected.thinking || ""
    if (root.model) saveAgentChoice()
  }

  function chooseModel(value) {
    var selected = root.models.find(item => item.value === value)
    if (!selected) return
    thinkingPicker.close()
    root.model = value
    root.thinking = selected.default_thinking || ""
    saveAgentChoice()
  }

  function saveAgentChoice() {
    request("configure", ["--harness", root.harness, "--model", root.model,
      "--thinking", root.thinking, "--token", root.state.token])
  }

  function receiveStatus(value) {
    if (!value.ok) {
      root.message = value.error || "Unable to read the current theme."
      root.messageError = true
      root.state = ({})
      root.savedStyles = []
      root.stylesJson = ""
      root.cancelDelete()
      return
    }
    var themeChanged = root.base !== value.base
    if (themeChanged) {
      styleInput.text = ""
      root.message = ""
      root.messageError = false
      viewport.contentY = 0
      root.agents = []
      root.agentDiagnostics = []
      root.agentsTheme = ""
      root.harness = ""
      root.model = ""
      root.thinking = ""
    }
    root.state = value
    if (themeChanged) root.refreshAgents()
    if (root.deleteTarget && (root.deleteTarget.base !== value.base
        || root.deleteTarget.token !== value.token
        || !(value.styles || []).some(item => item.id === root.deleteTarget.id))) root.cancelDelete()
    var encoded = JSON.stringify(value.styles || [])
    if (encoded !== root.stylesJson) {
      root.stylesJson = encoded
      root.savedStyles = value.styles || []
    }
  }

  function request(action, args) {
    if (root.busy || !root.base) return
    root.commandTheme = root.base
    root.commandAction = action
    if (action === "start") {
      root.requestStarted = Date.now()
      root.clockNow = root.requestStarted
    }
    root.message = ""
    root.messageError = false
    actionProcess.command = [root.helper, action, "--theme", root.base].concat(args || [])
    actionProcess.running = true
  }

  function generate() {
    if (!root.ready || root.generating || root.busy || !styleInput.text.trim()) return
    request("start", ["--style", styleInput.text.trim(),
      "--harness", root.harness, "--model", root.model,
      "--thinking", root.thinking, "--token", root.state.token, "--apply"])
  }

  function applyStyle(style) {
    if (root.deleteTarget || root.state.active === style.id) return
    request("apply", ["--id", style.id, "--token", root.state.token])
  }

  function requestDelete(style) {
    if (root.busy) return
    root.deleteTarget = {id: style.id, name: style.name, base: root.base,
      token: root.state.token, active: root.state.active === style.id}
    deleteConfirm.selectedIndex = 0
    deleteConfirm.forceActiveFocus()
  }

  function cancelDelete() {
    root.deleteTarget = null
    if (root.opened) styleInput.forceActiveFocus()
  }

  function confirmDelete() {
    var target = root.deleteTarget
    if (!target || root.busy) return
    root.cancelDelete()
    if (target.base !== root.base || target.token !== root.state.token) return
    request("delete", ["--id", target.id, "--token", target.token, "--yes"])
  }

  onOpenedChanged: {
    if (root.opened) {
      root.refresh()
      root.refreshAgents()
      Qt.callLater(function() { styleInput.forceActiveFocus() })
    } else root.deleteTarget = null
  }

  Timer {
    interval: root.opened || root.generating ? 1500 : 5000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 1000
    running: root.generating
    repeat: true
    triggeredOnStart: true
    onTriggered: root.clockNow = Date.now()
  }

  Process {
    id: statusProcess
    command: [root.helper, "status"]
    stdout: StdioCollector {
      onStreamFinished: {
        try { root.receiveStatus(JSON.parse(text)) }
        catch (error) {
          root.receiveStatus({ok: false, error: "Unable to read Theme Styles."})
        }
      }
    }
  }

  Process {
    id: agentProcess
    command: [root.helper, "agents"]
    property string requestedTheme: ""
    onStarted: requestedTheme = root.base
    onExited: {
      if (requestedTheme !== root.base) Qt.callLater(root.refreshAgents)
    }
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          root.receiveAgents(JSON.parse(text))
        } catch (error) {
          root.receiveAgents({ok: false})
        }
      }
    }
  }

  Process {
    id: actionProcess
    stdout: StdioCollector {
      onStreamFinished: {
        if (root.commandTheme === root.base) {
          try {
            var result = JSON.parse(text)
            root.messageError = !result.ok
            root.message = result.error || result.message || ""
            if (result.ok && result.job) root.state = Object.assign({}, root.state, {job: result.job})
            if (!result.ok && root.commandAction === "configure") root.refreshAgents()
          } catch (error) {
            root.message = "The operation could not be completed."
            root.messageError = true
          }
        }
        root.refresh()
      }
    }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
    function status(): string { return JSON.stringify(root.state) }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.generating ? "󰑐" : "󰏘"
    active: root.generating
    tooltipText: "Theme Styles · " + (root.state.display_name || "Current theme")
      + (root.generating ? " · " + root.stageLabel + " · " + root.elapsedText : "")
    RotationAnimation on textRotation {
      from: 0
      to: 360
      duration: 1200
      loops: Animation.Infinite
      running: root.generating
      onStopped: button.textRotation = 0
    }
    onPressed: root.toggle()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: styleInput
    // Leave room for control chrome and panel padding around the longest model name.
    contentWidth: panel.fittedContentWidth(Math.max(Style.space(410), root.modelLabelWidth + Style.space(100)), Style.space(720))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(660))

    Item {
      anchors.fill: parent
      Keys.onEscapePressed: root.close()

      Flickable {
        id: viewport
        enabled: !root.deleteTarget
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: parent.width
          spacing: Style.space(12)

          PanelHero {
            width: parent.width
            title: "Theme Styles"
            meta: root.state.display_name || "Loading current theme…"
            foreground: root.foreground
            iconComponent: Component {
              Text {
                text: "󰏘"
                color: root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.display
              }
            }
          }

          Text {
            width: parent.width
            visible: (root.state.missing || []).length > 0
            text: "Install to generate styles: " + (root.state.missing || []).join(", ")
            textFormat: Text.PlainText
            color: Color.urgent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }

          Text {
            width: parent.width
            visible: (root.state.warnings || []).length > 0
            text: (root.state.warnings || []).map(item => item.message + "\n" + item.path).join("\n")
            textFormat: Text.PlainText
            color: Color.urgent
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          Column {
            width: parent.width
            spacing: Style.space(6)
            Text {
              text: "Describe a style"
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
            TextField {
              id: styleInput
              width: parent.width
              placeholderText: "Winter, summer, moonlit, watercolor…"
              maximumLength: 2000
              enabled: !root.busy
              onAccepted: root.generate()
              Keys.onEscapePressed: root.close()
            }
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(8)
            enabled: !root.busy && !agentProcess.running && !root.generating
            Dropdown {
              id: harnessPicker
              objectName: "harnessPicker"
              Layout.fillWidth: true
              label: "Harness"
              value: root.harness
              options: root.agents
              onChanged: function(value) {
                root.chooseHarness(value)
                // Native dropdowns assign their own value before emitting changed,
                // replacing the binding. Restore it after every user selection.
                harnessPicker.value = Qt.binding(function() { return root.harness })
              }
            }
            Dropdown {
              id: thinkingPicker
              objectName: "thinkingPicker"
              Layout.fillWidth: true
              label: "Thinking"
              value: root.thinking
              options: root.thinkingLevels
              onChanged: function(value) {
                root.thinking = value
                root.saveAgentChoice()
                thinkingPicker.value = Qt.binding(function() { return root.thinking })
              }
            }
          }

          SearchableDropdown {
            id: modelPicker
            objectName: "modelPicker"
            width: parent.width
            enabled: !root.busy && !agentProcess.running && !root.generating
            label: "Model"
            value: root.model
            options: root.models
            placeholderText: "Find a model…"
            onChanged: function(value) {
              root.chooseModel(value)
              modelPicker.value = Qt.binding(function() { return root.model })
            }
          }

          Text {
            width: parent.width
            text: agentProcess.running ? "Checking signed-in agents…"
              : root.agentDiagnostics.length ? root.agentDiagnostics.map(item => item.label + ": " + item.message).join("\n")
              : root.selectedHarness.notice || (root.agents.length
                ? "Try any signed-in agent. If it cannot generate an image, you'll get an error."
                : "No compatible image connection found. Sign in to a supported harness or connect OpenRouter in OpenCode, then reopen this panel.")
            textFormat: Text.PlainText
            color: root.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          Button {
            width: parent.width
            text: root.generating ? "Working…" : "Generate"
            iconText: "󰏘"
            bordered: true
            focusable: true
            enabled: root.ready && !root.generating && !root.busy && styleInput.text.trim() !== ""
            opacity: enabled ? 1 : 0.45
            onClicked: root.generate()
          }

          Column {
            width: parent.width
            visible: root.generating
            spacing: Style.space(8)

            RowLayout {
              width: parent.width
              Text {
                Layout.fillWidth: true
                text: root.stageLabel
                textFormat: Text.PlainText
                color: root.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.Wrap
              }
              Text {
                text: root.elapsedText
                color: root.dim
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
              Button {
                visible: root.generationStage !== "applying"
                text: root.jobRunning && root.job.cancel_requested ? "Cancelling…" : "Cancel"
                focusable: true
                enabled: !root.busy && !(root.jobRunning && root.job.cancel_requested)
                onClicked: root.request("cancel", [])
              }
            }

            Rectangle {
              id: progressTrack
              width: parent.width
              height: Style.space(4)
              radius: height / 2
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.15)
              clip: true
              Accessible.role: Accessible.ProgressBar
              Accessible.name: root.stageLabel

              Rectangle {
                width: progressTrack.width / 3
                height: parent.height
                radius: height / 2
                color: Color.accent
                NumberAnimation on x {
                  from: -progressTrack.width / 3
                  to: progressTrack.width
                  duration: 1400
                  loops: Animation.Infinite
                  running: root.generating && root.opened
                }
              }
            }

            Text {
              width: parent.width
              text: root.generationStage === "generating" && root.elapsedSeconds >= 120
                ? "Still waiting for the image service. You can close this panel; generation will continue."
                : "Images can take a few minutes. You can close this panel while it works."
              textFormat: Text.PlainText
              color: root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
            }
          }

          RowLayout {
            width: parent.width
            visible: !root.generating && !!root.job.message
            spacing: Style.space(8)
            TextEdit {
              id: jobMessage
              Layout.fillWidth: true
              text: root.job.message || ""
              textFormat: TextEdit.PlainText
              readOnly: true
              selectByMouse: true
              selectionColor: Color.accent
              selectedTextColor: Color.popups.background
              color: root.job.state === "failed" ? Color.urgent : root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: TextEdit.Wrap
            }
            Button {
              Layout.alignment: Qt.AlignTop
              visible: root.job.state === "failed" && !!root.job.log_path
              text: "Copy log path"
              focusable: true
              onClicked: { logPathClipboard.selectAll(); logPathClipboard.copy() }
            }
          }

          TextEdit {
            id: logPathClipboard
            visible: false
            text: root.job.log_path || ""
            textFormat: TextEdit.PlainText
            readOnly: true
          }

          RowLayout {
            width: parent.width
            visible: root.message !== ""
            spacing: Style.space(8)
            TextEdit {
              id: actionMessage
              Layout.fillWidth: true
              text: root.message
              textFormat: TextEdit.PlainText
              readOnly: true
              selectByMouse: true
              selectionColor: Color.accent
              selectedTextColor: Color.popups.background
              color: root.messageError ? Color.urgent : root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: TextEdit.Wrap
            }
          }

          PanelSeparator {}

          RowLayout {
            width: parent.width
            Text {
              Layout.fillWidth: true
              text: "Saved styles"
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              font.bold: true
            }
            Button {
              text: root.state.active ? "Restore original" : "Original active"
              focusable: true
              enabled: !!root.state.active && !root.busy
              opacity: enabled ? 1 : 0.5
              onClicked: root.request("restore", ["--token", root.state.token])
            }
          }

          Text {
            visible: root.savedStyles.length === 0
            width: parent.width
            text: "Your styles for this theme will appear here."
            color: root.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }

          Repeater {
            model: root.savedStyles
            delegate: Item {
              id: styleRow
              required property var modelData
              width: column.width
              height: Style.space(74)
              activeFocusOnTab: true
              readonly property bool isActive: root.state.active === modelData.id
              Accessible.role: Accessible.Button
              Accessible.name: "Apply " + modelData.name
              Keys.onReturnPressed: if (activeFocus) root.applyStyle(modelData)
              Keys.onEnterPressed: if (activeFocus) root.applyStyle(modelData)
              Keys.onSpacePressed: if (activeFocus) root.applyStyle(modelData)

              CursorSurface {
                anchors.fill: parent
                current: styleRow.isActive
                hasCursor: !root.busy && (rowMouse.containsMouse || styleRow.activeFocus)
                foreground: root.foreground
              }

              MouseArea {
                id: rowMouse
                anchors.fill: parent
                enabled: !root.busy
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                  styleRow.forceActiveFocus()
                  root.applyStyle(styleRow.modelData)
                }
              }

              RowLayout {
                anchors.fill: parent
                anchors.margins: Style.space(6)
                spacing: Style.space(10)

                Image {
                  Layout.preferredWidth: Style.space(88)
                  Layout.preferredHeight: Style.space(62)
                  source: styleRow.modelData.preview
                  sourceSize.width: 264
                  fillMode: Image.PreserveAspectCrop
                  asynchronous: true
                  clip: true
                }
                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: Style.space(3)
                  Text {
                    Layout.fillWidth: true
                    text: styleRow.modelData.name
                    textFormat: Text.PlainText
                    color: root.foreground
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    font.bold: styleRow.isActive
                    elide: Text.ElideRight
                  }
                  Text {
                    Layout.fillWidth: true
                    text: styleRow.modelData.style
                    textFormat: Text.PlainText
                    color: root.dim
                    font.family: Style.font.family
                    font.pixelSize: Style.font.bodySmall
                    elide: Text.ElideRight
                  }
                  Text {
                    text: styleRow.isActive ? "Active" : (styleRow.modelData.mode === "light" ? "Light" : "Dark")
                    color: styleRow.isActive ? Color.accent : root.dim
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                }
                PanelActionButton {
                  iconText: "󰆴"
                  tooltipText: "Delete " + styleRow.modelData.name
                  hoverColor: Color.urgent
                  focusable: true
                  enabled: !root.busy
                  onClicked: root.requestDelete(styleRow.modelData)
                }
              }
            }
          }
        }
      }

      ConfirmDialog {
        id: deleteConfirm
        anchors.fill: parent
        z: 10
        opened: !!root.deleteTarget
        message: root.deleteTarget
          ? "Delete “" + root.deleteTarget.name + "”?\n\nThis permanently removes its saved wallpaper and generation files."
            + (root.deleteTarget.active ? "\n\nThe original appearance will be restored." : "")
          : ""
        confirmText: "Delete"
        foreground: root.foreground
        background: Color.popups.background
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          deleteConfirm.handleKey(event)
          event.accepted = true
        }
        onCanceled: root.cancelDelete()
        onConfirmed: root.confirmDelete()
      }
    }
  }
}

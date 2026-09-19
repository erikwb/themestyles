import QtQuick
import Quickshell
import QtTest as Tests
import "plugin/src" as Plugin

Scope {
  Plugin.Panel { id: panel }
  Tests.TestCase { id: test; when: false }

  function check(actual, expected, description) {
    if (JSON.stringify(actual) !== JSON.stringify(expected))
      throw new Error(description + ": expected " + JSON.stringify(expected) + ", got " + JSON.stringify(actual))
  }

  function select(picker, value) {
    // Both native controls do these two operations in selectCurrent(), in this
    // order. The first assignment removes a plain QML property binding.
    picker.value = value
    picker.changed(value)
  }

  function model(value, thinking, defaultThinking) {
    return {value: value, label: value, thinking: thinking.map(value => ({value: value, label: value})),
      default_thinking: defaultThinking}
  }

  Timer {
    interval: 300
    running: true
    onTriggered: {
      try {
        check(panel.helper, Qt.resolvedUrl("plugin/theme-styles").toString().replace(/^file:\/\//, ""), "Backend launcher path")
        var harness = test.findChild(panel, "harnessPicker")
        var models = test.findChild(panel, "modelPicker")
        var thinking = test.findChild(panel, "thinkingPicker")
        if (!harness || !models || !thinking) throw new Error("Picker controls not found")
        panel.agents = [
          {value: "codex", label: "Codex", model: "codex-main", thinking: "medium", models: [
            model("codex-main", ["", "medium", "high"], "medium")]},
          {value: "claude", label: "Claude Code", model: "sonnet", thinking: "", models: [
            model("sonnet", ["", "low", "high"], "low"), model("opus", ["", "high"], "high")]},
          {value: "pi", label: "Pi", model: "provider/pi-main", thinking: "off", models: [
            model("provider/pi-main", ["", "off", "medium"], "off"),
            model("provider/pi-other", ["", "low"], "low")]}
        ]

        select(harness, "codex")
        select(harness, "claude")
        select(models, "sonnet")
        select(thinking, "high")
        select(harness, "pi")
        check(panel.harness, "pi", "Selected harness")
        check(panel.model, "provider/pi-main", "Backend model")
        check(models.value, "provider/pi-main", "Model shown after switching to Pi")
        check(models.currentLabel(), "provider/pi-main", "Visible model label")
        check(models.filtered.map(m => m.value), ["provider/pi-main", "provider/pi-other"], "Pi model menu")
        check(thinking.value, "off", "Thinking shown after switching to Pi")
        check(thinking.options.map(t => t.value), ["", "off", "medium"], "Pi thinking menu")

        select(thinking, "medium")
        select(models, "provider/pi-other")
        check(thinking.value, "low", "Thinking resets after changing model")
        check(thinking.options.map(t => t.value), ["", "low"], "Thinking menu follows model")

        select(harness, "claude")
        select(models, "opus")
        select(harness, "codex")
        check(models.value, "codex-main", "Repeated harness switches")
        check(thinking.value, "medium", "Repeated thinking resets")

        // A refresh/theme change updates the same properties as an agents reply.
        panel.harness = "claude"
        panel.model = "sonnet"
        panel.thinking = "low"
        check(harness.value, "claude", "Harness follows refreshed selection")
        check(models.value, "sonnet", "Model follows refreshed selection")
        check(thinking.value, "low", "Thinking follows refreshed selection")
        panel.agents = []
        panel.harness = ""
        panel.model = ""
        panel.thinking = ""
        check([harness.value, models.value, thinking.value], ["", "", ""], "Theme change clears displayed values")
        check(models.filtered, [], "Empty model menu after theme change")
        check(thinking.options, [], "Empty thinking menu after theme change")
        panel.state = {base: "fixture", token: "fixture"}
        panel.receiveAgents({ok: true, base: "fixture", agents: [], selection: {},
          diagnostics: [{label: "Pi", code: "discovery_failed", message: "Account data could not be read."}]})
        check(panel.agentDiagnostics.length, 1, "Discovery failure remains visible")
        panel.receiveAgents({ok: true, base: "old-theme", agents: [], selection: {}, diagnostics: []})
        check(panel.agentDiagnostics.length, 1, "Stale discovery reply is ignored")
        panel.receiveAgents({ok: false, error: "Discovery failed"})
        check(panel.message, "Discovery failed", "Backend discovery errors are shown")
        check(panel.agentsTheme, "", "Failed discovery invalidates readiness")
        console.log("PICKER REGRESSION PASSED")
      } catch (error) {
        console.error("PICKER REGRESSION FAILED: " + error)
      }
      Qt.quit()
    }
  }
}

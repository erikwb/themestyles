// Loaded only through Theme Styles' process-local OpenCode configuration.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { imageCatalog, imageModel } from "./opencode-image-provider.mjs";

export default async () => ({
  async config(config) {
    const statusPath = process.env.THEME_STYLES_OPENCODE_STATUS;
    const provider = config.provider?.openrouter || {};
    let auth = {};
    try {
      const data = process.env.XDG_DATA_HOME || join(process.env.HOME, ".local/share");
      auth = JSON.parse(readFileSync(join(data, "opencode/auth.json"), "utf8")).openrouter || {};
    } catch (error) {
      if (error.code !== "ENOENT") throw new Error("Could not read OpenCode's OpenRouter login.");
    }
    const connected = !!(provider.options?.apiKey || auth.key || process.env.OPENROUTER_API_KEY);
    const report = value => {
      if (statusPath) writeFileSync(statusPath, JSON.stringify(value), { mode: 0o600, flag: "wx" });
    };
    if (!connected) {
      report({ connected: false });
      return;
    }
    try {
      const catalog = await imageCatalog(provider.options?.baseURL);
      config.provider ||= {};
      config.provider.openrouter ||= {};
      const models = config.provider.openrouter.models ||= {};
      for (const item of catalog) models[item.id] = imageModel(item, models[item.id]);
      report({ connected: true });
    } catch {
      report({ connected: true, error: "Could not load OpenRouter's image catalog. Reopen the panel to retry." });
      throw new Error("Could not load OpenRouter's image catalog.");
    }
  },
});

// Loaded only through Theme Styles' process-local OpenCode configuration.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { imageCatalog, imageModel } from "./opencode-image-provider.mjs";
import { nativeHooks } from "./opencode-native-images.mjs";

export default async () => {
  const native = process.env.THEME_STYLES_IMAGE_JOB ? nativeHooks(process.env.THEME_STYLES_IMAGE_JOB) : {};
  return {
    ...Object.fromEntries(Object.entries(native).filter(([name]) => name !== "configure")),
    async config(config) {
      const statusPath = process.env.THEME_STYLES_OPENCODE_STATUS;
      let auth = {};
      try {
        const data = process.env.XDG_DATA_HOME || join(process.env.HOME, ".local/share");
        auth = JSON.parse(readFileSync(join(data, "opencode/auth.json"), "utf8"));
      } catch (error) {
        if (error.code !== "ENOENT") throw new Error("Could not read OpenCode's provider logins.");
      }
      const keys = { openrouter: "OPENROUTER_API_KEY", opencode: "OPENCODE_API_KEY", "opencode-go": "OPENCODE_API_KEY" };
      const providers = Object.keys(keys).filter(id => config.provider?.[id]?.options?.apiKey
        || auth[id]?.key || process.env[keys[id]]);
      const errors = [];
      if (providers.includes("openrouter")) {
        try {
          const catalog = await imageCatalog(config.provider?.openrouter?.options?.baseURL);
          config.provider ||= {};
          config.provider.openrouter ||= {};
          const models = config.provider.openrouter.models ||= {};
          for (const item of catalog) models[item.id] = imageModel(item, models[item.id]);
        } catch {
          errors.push("Could not load OpenRouter's image catalog. Reopen the panel to retry.");
        }
      }
      native.configure?.(config);
      if (statusPath) writeFileSync(statusPath, JSON.stringify({ connected: providers.length > 0, providers, errors }),
        { mode: 0o600, flag: "wx" });
    },
  };
};

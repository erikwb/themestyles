// OpenCode's app-scoped AI SDK provider: one image request, no text-model call.
import { constants, openSync, closeSync, fstatSync, readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

const API = "https://openrouter.ai/api/v1";
export const MAX_IMAGE_BYTES = 100 * 1024 * 1024;
const RASTER_FORMATS = ["png", "jpeg", "webp"];
const ZERO_USAGE = { inputTokens: { total: 0, noCache: 0, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 0, text: 0, reasoning: 0 } };

export function apiURL(base = API, path) {
  const url = new URL(base.replace(/\/+$/, "") + path);
  if (url.username || url.password || (url.protocol !== "https:"
      && !(url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))))
    throw new Error("The image API requires HTTPS.");
  return url;
}

export async function boundedBody(response, limit) {
  if (!response.ok) {
    await response.body?.cancel();
    const error = new Error(`Image service returned HTTP ${response.status}.`);
    error.status = response.status;
    error.code = [401, 403].includes(response.status) ? "authentication" : response.status === 429 ? "rate_limit" : "failed";
    throw error;
  }
  const chunks = [];
  let length = 0;
  for await (const chunk of response.body) {
    length += chunk.byteLength;
    if (length > limit) throw new Error("Image service response exceeds the size limit.");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

async function boundedJSON(response, limit) {
  return JSON.parse((await boundedBody(response, limit)).toString("utf8"));
}

export async function imageCatalog(base) {
  const response = await fetch(apiURL(base, "/images/models"), {
    signal: AbortSignal.timeout(6000), redirect: "error",
  });
  const body = await boundedJSON(response, 4 * 1024 * 1024);
  if (!Array.isArray(body.data)) throw new Error("Invalid image catalog.");
  return body.data.filter(item => {
    const p = item.supported_parameters || {};
    const formats = p.output_format?.values;
    return typeof item.id === "string" && /^[a-zA-Z0-9_.:/-]+$/.test(item.id)
      && !item.id.startsWith("openrouter/")
      && item.architecture?.input_modalities?.includes("image")
      && item.architecture?.output_modalities?.includes("image")
      && p.input_references?.max >= 1 && (p.input_references.min || 0) <= 1
      && (!Array.isArray(formats) || formats.some(format => RASTER_FORMATS.includes(format)));
  });
}

export function imageModel(item, configured = {}) {
  return {
    ...configured,
    name: item.name || item.id,
    attachment: true, tool_call: false, reasoning: false, temperature: false,
    modalities: { input: ["text", "image"], output: ["text", "image"] },
    // These limits belong to the local request adapter, which returns a file path.
    limit: { context: 65536, output: 8192 },
    provider: { npm: import.meta.url, api: API },
    variants: {},
  };
}

export function readPrivate(path, limit) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > limit) throw new Error("Unsafe image job input.");
    return readFileSync(fd);
  } finally { closeSync(fd); }
}

export function rasterMime(data) {
  if (data.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return "image/png";
  if (data[0] === 255 && data[1] === 216 && data[2] === 255) return "image/jpeg";
  if (data.toString("ascii", 0, 4) === "RIFF" && data.toString("ascii", 8, 12) === "WEBP") return "image/webp";
  throw new Error("Image service must return a PNG, JPEG, or WebP image.");
}

export function saveImage(directory, encoded) {
  if (typeof encoded !== "string" || !encoded || encoded.length > Math.ceil(MAX_IMAGE_BYTES * 4 / 3)
      || encoded.length % 4 || /[^A-Za-z0-9+/=]/.test(encoded) || !/^[^=]*={0,2}$/.test(encoded))
    throw new Error("Image service returned invalid image data.");
  const bytes = Buffer.from(encoded, "base64");
  rasterMime(bytes);
  if (bytes.length > MAX_IMAGE_BYTES) throw new Error("Image exceeds the size limit.");
  writeFileSync(join(directory, "wallpaper.png"), bytes, { flag: "wx", mode: 0o600 });
}

export function beginAttempt(directory) {
  const fd = openSync(join(directory, ".image-request-started"), constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
  closeSync(fd);
}

export function failure(directory, error, provider) {
  const code = ["authentication", "rate_limit", "unsupported"].includes(error.code) ? error.code : "failed";
  // Never replace an earlier failure (including when a harness attempts a retry).
  try {
    writeFileSync(join(directory, "failure.json"), JSON.stringify({ error_code: code }), { flag: "wx", mode: 0o600 });
  } catch (writeError) { if (writeError.code !== "EEXIST") throw writeError; }
  const status = Number.isInteger(error.status) ? ` HTTP ${error.status}.` : "";
  return new Error(`${provider} image generation failed (${code}).${status}`);
}

async function generate(modelId, options, call) {
  const jobPath = process.env.THEME_STYLES_IMAGE_JOB;
  if (!jobPath) throw new Error("This image adapter can only run inside Theme Styles.");
  const job = JSON.parse(readPrivate(jobPath, 65536));
  // Only the initial user request may generate; titles and follow-ups stay local.
  const users = call.prompt?.filter(message => message.role === "user") || [];
  const requested = users.length === 1 && !call.prompt.some(message => message.role === "assistant")
    && Array.isArray(users[0].content) && users[0].content.length === 1
    && users[0].content[0].type === "text" && users[0].content[0].text === job.marker;
  if (!requested) return "Theme Styles";
  if (job.model !== modelId) throw new Error("The selected image model changed.");
  if (typeof job.reference !== "string" || basename(job.reference) !== job.reference)
    throw new Error("Invalid reference image path.");
  const directory = dirname(jobPath);
  const output = join(directory, "wallpaper.png");
  // A persistent attempt marker prevents retries and duplicate generation requests.
  beginAttempt(directory);
  try {
    const apiKey = options.apiKey || process.env.OPENROUTER_API_KEY;
    if (!apiKey) {
      const error = new Error("OpenCode has no OpenRouter API key.");
      error.code = "authentication";
      throw error;
    }
    const catalog = await imageCatalog(options.baseURL);
    const model = catalog.find(item => item.id === modelId);
    if (!model) throw new Error("This model no longer supports a reference image and raster output.");
    const reference = readPrivate(join(directory, job.reference), MAX_IMAGE_BYTES);
    const body = { model: modelId, prompt: job.prompt, n: 1,
      input_references: [{ type: "image_url", image_url: { url: `data:${rasterMime(reference)};base64,${reference.toString("base64")}` } }],
      provider: { allow_fallbacks: false } };
    const params = model.supported_parameters || {};
    const format = RASTER_FORMATS.find(value => params.output_format?.values?.includes(value));
    if (format) body.output_format = format;
    if (params.resolution?.values?.includes("2K")) body.resolution = "2K";
    if (params.aspect_ratio?.values?.includes(job.aspect_ratio)) body.aspect_ratio = job.aspect_ratio;
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${apiKey}`);
    headers.set("Content-Type", "application/json");
    headers.set("X-Title", "Theme Styles");
    const response = await (options.fetch || fetch)(apiURL(options.baseURL, "/images"), {
      method: "POST", headers, body: JSON.stringify(body), redirect: "error",
      signal: AbortSignal.any([AbortSignal.timeout(900000), ...(call.abortSignal ? [call.abortSignal] : [])]),
    });
    const result = await boundedJSON(response, Math.ceil(MAX_IMAGE_BYTES * 4 / 3) + 65536);
    saveImage(directory, result.data?.[0]?.b64_json);
    return JSON.stringify({ image_path: output, error_code: "" });
  } catch (error) {
    // Do not expose request bodies, provider responses, or credentials in logs.
    throw failure(directory, error, "OpenRouter");
  }
}

export function createThemeStylesImages(options = {}) {
  const languageModel = modelId => ({
    specificationVersion: "v3", provider: "theme-styles-images", modelId, supportedUrls: {},
    async doGenerate(call) {
      const text = await generate(modelId, options, call);
      return { content: [{ type: "text", text }], finishReason: { unified: "stop", raw: "stop" }, usage: ZERO_USAGE, warnings: [] };
    },
    async doStream(call) {
      const text = await generate(modelId, options, call);
      return { stream: new ReadableStream({ start(controller) {
        controller.enqueue({ type: "stream-start", warnings: [] });
        controller.enqueue({ type: "text-start", id: "image" });
        controller.enqueue({ type: "text-delta", id: "image", delta: text });
        controller.enqueue({ type: "text-end", id: "image" });
        controller.enqueue({ type: "finish", finishReason: { unified: "stop", raw: "stop" }, usage: ZERO_USAGE });
        controller.close();
      } }) };
    },
  });
  return { specificationVersion: "v3", languageModel };
}

// Preserve OpenCode's Go/Zen SDK, authentication, endpoint and request headers.
// Capture native image output before the CLI discards non-text stream parts.
import { randomUUID } from "node:crypto";
import { basename, dirname, join } from "node:path";
import { apiURL, beginAttempt, boundedBody, failure, MAX_IMAGE_BYTES,
  rasterMime, readPrivate, saveImage } from "./opencode-image-provider.mjs";

const SDKS = new Set(["@ai-sdk/google", "@ai-sdk/openai", "@ai-sdk/openai-compatible"]);

export function eligible(model) {
  return ["opencode", "opencode-go"].includes(model.providerID)
    && model.capabilities?.input?.image === true && model.capabilities?.output?.image === true
    && SDKS.has(model.api?.npm);
}

export function nativeHooks(jobPath) {
  const job = JSON.parse(readPrivate(jobPath, 65536));
  const directory = dirname(jobPath);
  if (!["opencode", "opencode-go"].includes(job.provider)) return {};
  if (typeof job.reference !== "string" || basename(job.reference) !== job.reference)
    throw new Error("Invalid reference image path.");
  let ready = false;
  return {
    configure(config) {
      const provider = (config.provider ||= {})[job.provider] ||= {};
      const options = provider.options ||= {};
      const originalFetch = options.fetch || fetch;
      options.headerTimeout = 900000;
      options.fetch = async (input, init) => {
        try {
          if (!ready || init?.method !== "POST") throw new Error("Unexpected image request.");
          apiURL(String(input), ""); // Reject remote HTTP and embedded credentials.
          beginAttempt(directory);
          const response = await originalFetch(input, { ...init, redirect: "error",
            signal: AbortSignal.any([AbortSignal.timeout(900000), ...(init.signal ? [init.signal] : [])]) });
          const body = await boundedBody(response, Math.ceil(MAX_IMAGE_BYTES * 4 / 3) + 1024 * 1024);
          const encoded = extractImage(body.toString("utf8"), response.headers.get("content-type") || "");
          saveImage(directory, encoded);
          // The original SDK still parses its own protocol and completes the session.
          return new Response(body, { status: response.status, headers: response.headers });
        } catch (error) { throw failure(directory, error, job.provider === "opencode" ? "OpenCode Zen" : "OpenCode Go"); }
      };
    },
    async "chat.message"(_input, output) {
      const part = output.parts.find(part => part.type === "text" && part.text === job.marker);
      if (!part) return;
      const reference = readPrivate(join(directory, job.reference), MAX_IMAGE_BYTES);
      part.text = job.prompt;
      output.parts.push({ id: "prt_" + randomUUID().replaceAll("-", ""), type: "file",
        sessionID: output.message.sessionID, messageID: output.message.id, filename: job.reference,
        mime: rasterMime(reference), url: `data:${rasterMime(reference)};base64,${reference.toString("base64")}` });
    },
    async "chat.params"(input, output) {
      if (input.agent !== "theme-styles" || input.model.providerID !== job.provider || input.model.id !== job.model
          || !eligible(input.model)) {
        const error = new Error("Selected model no longer advertises supported image generation.");
        error.code = "unsupported";
        throw failure(directory, error, "OpenCode");
      }
      if (input.model.api.npm === "@ai-sdk/google") output.options.responseModalities = ["TEXT", "IMAGE"];
      if (input.model.api.npm === "@ai-sdk/openai-compatible") output.options.modalities = ["text", "image"];
      ready = true;
    },
  };
}

// Recognize complete images from documented Gemini, Chat Completions and
// Responses formats. Partial previews, text links and remote URLs aren't images.
export function extractImage(text, contentType) {
  let encoded, finished = false;
  const dataURL = value => {
    if (typeof value !== "string") return;
    const prefix = /^data:image\/(?:png|jpeg|webp);base64,/.exec(value);
    if (prefix) encoded ||= value.slice(prefix[0].length);
  };
  const visit = event => {
    if (event.error || ["error", "response.failed", "response.incomplete"].includes(event.type))
      throw new Error("Image service failed.");
    for (const candidate of event.candidates || []) {
      if ((candidate.index ?? 0) !== 0) continue;
      for (const part of candidate.content?.parts || []) {
        if (part.inlineData?.mimeType?.startsWith("image/")) encoded ||= part.inlineData.data;
      }
      if (candidate.finishReason === "STOP") finished = true;
    }
    for (const choice of event.choices || []) {
      if ((choice.index ?? 0) !== 0) continue;
      const message = choice.message || choice.delta || {};
      for (const image of message.images || []) dataURL(image.image_url?.url);
      if (Array.isArray(message.content)) {
        for (const part of message.content) if (part.type === "image_url") dataURL(part.image_url?.url);
      }
      if (choice.finish_reason === "stop") finished = true;
    }
    const response = event.type === "response.completed" ? event.response : event;
    if (response.object === "response" && response.status === "completed") {
      for (const item of response.output || []) {
        if (item.type === "image_generation_call" && item.status === "completed") encoded ||= item.result;
      }
      finished = true;
    }
  };
  if (contentType.includes("text/event-stream")) {
    for (const block of text.replaceAll("\r\n", "\n").split("\n\n")) {
      const data = block.split("\n").filter(line => line.startsWith("data:"))
        .map(line => line.slice(5).trimStart()).join("\n");
      if (data && data !== "[DONE]") visit(JSON.parse(data));
    }
  } else visit(JSON.parse(text));
  if (!finished || !encoded) throw new Error("No complete inline image was returned.");
  return encoded;
}

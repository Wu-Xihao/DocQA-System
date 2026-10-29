let token = "";
export function setToken(value) {
  token = value;
}
export async function api(path, options = {}) {
  const control = new AbortController();
  const timer = setTimeout(() => control.abort(), options.timeoutMs || 10000);
  try {
    const response = await fetch("/api/v1" + path, {
      method: options.method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      signal: control.signal,
      headers: {
        Accept: "application/json",
        "X-DocQA-Token": token,
        ...(options.body !== undefined
          ? { "Content-Type": "application/json" }
          : {}),
      },
      ...(options.body !== undefined
        ? { body: JSON.stringify(options.body) }
        : {}),
    });
    const value = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(value?.error?.code || `HTTP_${response.status}`);
      error.code = error.message;
      error.status = response.status;
      throw error;
    }
    if (!value) throw new Error("INVALID_RESPONSE");
    return value;
  } finally {
    clearTimeout(timer);
  }
}
export async function plain(url) {
  if (!url.startsWith("/api/v1/")) throw new Error("INVALID_RESOURCE");
  const control = new AbortController();
  const timer = setTimeout(() => control.abort(), 10000);
  try {
    const response = await fetch(url, {
      credentials: "same-origin",
      signal: control.signal,
      headers: { "X-DocQA-Token": token },
    });
    if (!response.ok) {
      const value = await response.json().catch(() => null);
      const error = new Error("SOURCE_FAILED");
      error.code = value?.error?.code || "SOURCE_FAILED";
      throw error;
    }
    return await response.text();
  } finally {
    clearTimeout(timer);
  }
}
export function uploadBytes(id, file, progress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/v1/uploads/${encodeURIComponent(id)}/content`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-DocQA-Token", token);
    xhr.timeout = 300000;
    xhr.upload.onprogress = (e) => progress(e.loaded, e.total || file.size);
    xhr.onload = () => {
      let value;
      try {
        value = JSON.parse(xhr.responseText);
      } catch {
        value = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(value);
      else {
        const error = new Error("UPLOAD_FAILED");
        error.code = value?.error?.code || "UPLOAD_INCOMPLETE";
        reject(error);
      }
    };
    xhr.onerror = xhr.ontimeout = () =>
      reject(new Error("UPLOAD_CONNECTION_FAILED"));
    xhr.send(file);
  });
}
export async function watchConversation(id, receive, signal) {
  const response = await fetch(
    `/api/v1/events?conversation_id=${encodeURIComponent(id)}`,
    { credentials: "same-origin", headers: { "X-DocQA-Token": token }, signal },
  );
  if (!response.ok || !response.body) throw new Error("EVENTS_UNAVAILABLE");
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        if (!signal.aborted) throw new Error("EVENTS_DISCONNECTED");
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const line = block.split("\n").find((s) => s.startsWith("data: "));
        if (line) receive(JSON.parse(line.slice(6)));
      }
    }
  } finally {
    reader.releaseLock();
  }
}

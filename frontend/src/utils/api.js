const API_BASE = "https://slideforge-backend-hz8k.onrender.com/api";

/**
 * Запускает генерацию презентации и получает поток SSE.
 */
export async function generatePresentation(params, onEvent, signal) {
  const response = await fetch(`${API_BASE}/generate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Ошибка сервера: ${response.status}`);
  }

  if (!response.body) {
    throw new Error("Сервер не вернул поток данных.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();

    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    const events = buffer.split("\n\n");
    buffer = events.pop() || "";

    for (const event of events) {
      if (!event.trim()) continue;

      let eventName = "message";
      let data = "";

      for (const line of event.split("\n")) {
        if (line.startsWith("event:")) {
          eventName = line.replace("event:", "").trim();
        }

        if (line.startsWith("data:")) {
          data += line.replace("data:", "").trim();
        }
      }

      if (!data) continue;

      try {
        onEvent(eventName, JSON.parse(data));
      } catch (err) {
        console.error(err);
      }
    }
  }
}

export function pptxDownloadUrl(id) {
  return `${API_BASE}/presentations/${id}/pptx`;
}

export function previewUrl(id) {
  return `${API_BASE}/presentations/${id}`;
}

export function shareUrl(id) {
  return `${window.location.origin}/presentation/${id}`;
}

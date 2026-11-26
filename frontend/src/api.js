// Thin client for the FastAPI backend (build_backend.py). Matches its
// request/response shapes exactly - see ChatRequest/ChatResponse there.

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response wasn't JSON - fall back to statusText
    }
    throw new Error(detail);
  }

  return res.json();
}

export function checkHealth() {
  return request("/health");
}

export function sendMessage({ message, sessionId, topN = 5 }) {
  return request("/chat", {
    method: "POST",
    body: JSON.stringify({
      message,
      session_id: sessionId || null,
      top_n: topN,
    }),
  });
}

export function clearSession(sessionId) {
  return request(`/session/${sessionId}`, { method: "DELETE" });
}

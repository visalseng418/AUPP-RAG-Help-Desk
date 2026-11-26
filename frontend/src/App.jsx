import { useEffect, useRef, useState } from "react";
import ChatMessage from "./components/ChatMessage.jsx";
import ChatInput from "./components/ChatInput.jsx";
import TypingIndicator from "./components/TypingIndicator.jsx";
import { checkHealth, sendMessage, clearSession } from "./api.js";
import auppLogo from "./assets/aupp_logo.png";

const SESSION_STORAGE_KEY = "aupp-chat-session-id";

export default function App() {
  const [status, setStatus] = useState("checking");
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState(null);
  const [sessionId, setSessionId] = useState(
    () => localStorage.getItem(SESSION_STORAGE_KEY) || null
  );

  const scrollRef = useRef(null);

  // Poll backend health until models finish loading. FastAPI's lifespan
  // loads BGE-M3 + reranker + the MLX LLM before /health reports ready,
  // which can take a while on first startup.
  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const health = await checkHealth();
        if (cancelled) return;
        setStatus(health.models_loaded ? "ready" : "loading");
        if (!health.models_loaded) {
          setTimeout(poll, 2000);
        }
      } catch {
        if (!cancelled) {
          setStatus("error");
          setTimeout(poll, 4000);
        }
      }
    }

    poll();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, isSending]);

  async function handleSend() {
    const text = input.trim();
    if (!text || isSending) return;

    setError(null);
    setInput("");
    setMessages((prev) => [...prev, { role: "user", text }]);
    setIsSending(true);

    try {
      const response = await sendMessage({ message: text, sessionId, topN: 5 });
      if (!sessionId) {
        setSessionId(response.session_id);
        localStorage.setItem(SESSION_STORAGE_KEY, response.session_id);
      }
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          text: response.answer,
          sources: response.sources,
          retrievedChunks: response.retrieved_chunks,
          usedAggregation: response.used_aggregation,
          rewrittenQuery: response.rewritten_query,
        },
      ]);
    } catch (err) {
      setError(err.message || "Something went wrong reaching the server.");
      // Put the failed message back so the person doesn't lose it
      setInput(text);
      setMessages((prev) => prev.slice(0, -1));
    } finally {
      setIsSending(false);
    }
  }

  async function handleClear() {
    if (sessionId) {
      try {
        await clearSession(sessionId);
      } catch {
        // session may already be gone server-side (e.g. restart) - fine either way
      }
    }
    setMessages([]);
    setError(null);
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1 className="header-title">
            AUPP <span>Reference Desk</span>
          </h1>
          <p className="header-sub">American University of Phnom Penh</p>
        </div>
        <img src={auppLogo} alt="AUPP logo" className="header-logo" />
      </header>

      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty-state">
            <h2>Ask the desk</h2>
            <p>
              Scholarships, tuition, programs, admissions, faculty - answers are
              pulled from AUPP's own pages, with sources attached.
            </p>
          </div>
        )}

        {messages.map((message, i) => (
          <ChatMessage key={i} message={message} />
        ))}

        {isSending && <TypingIndicator />}
      </div>

      {error && <div className="error-banner">{error}</div>}

      <ChatInput
        value={input}
        onChange={setInput}
        onSend={handleSend}
        disabled={isSending || status !== "ready"}
        onClear={handleClear}
        hasMessages={messages.length > 0}
      />
    </div>
  );
}

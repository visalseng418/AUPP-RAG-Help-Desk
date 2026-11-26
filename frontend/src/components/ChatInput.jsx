import { useRef } from "react";

export default function ChatInput({ value, onChange, onSend, disabled, onClear, hasMessages }) {
  const textareaRef = useRef(null);

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!disabled && value.trim()) {
        onSend();
      }
    }
  }

  function autoGrow() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }

  return (
    <div className="input-bar">
      <div className="input-row">
        <textarea
          ref={textareaRef}
          rows={1}
          placeholder="Ask about admissions, tuition, programs..."
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            autoGrow();
          }}
          onKeyDown={handleKeyDown}
          disabled={disabled}
        />
        <button
          className="send-btn"
          onClick={onSend}
          disabled={disabled || !value.trim()}
          aria-label="Send message"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path
              d="M2 8h11M8 2l6 6-6 6"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </div>
      {hasMessages && (
        <div className="input-footer">
          <button className="clear-link" onClick={onClear}>
            Clear conversation
          </button>
          <span />
        </div>
      )}
    </div>
  );
}

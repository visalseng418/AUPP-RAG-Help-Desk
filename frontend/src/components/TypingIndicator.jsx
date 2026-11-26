export default function TypingIndicator() {
  return (
    <div className="message-row assistant">
      <div className="typing-card" aria-label="Assistant is answering">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </div>
    </div>
  );
}

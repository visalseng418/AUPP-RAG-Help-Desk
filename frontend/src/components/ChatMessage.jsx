import SourceCard from "./SourceCard.jsx";

export default function ChatMessage({ message }) {
  if (message.role === "user") {
    return (
      <div className="message-row user">
        <div className="bubble-user">{message.text}</div>
      </div>
    );
  }

  const { text, sources = [], retrievedChunks = [], usedAggregation, rewrittenQuery } = message;

  return (
    <div className="message-row assistant">
      <div className="answer-card">
        {usedAggregation && (
          <span className="answer-tag">Complete list, not a sample</span>
        )}

        <div className="answer-text">{text}</div>

        {rewrittenQuery && (
          <p className="rewritten-note">Searched as: "{rewrittenQuery}"</p>
        )}

        {retrievedChunks.length > 0 && (
          <>
            <p className="sources-label">Sources</p>
            <div className="sources-row">
              {retrievedChunks.map((chunk, i) => (
                <SourceCard
                  key={`${chunk.filename}-${i}`}
                  rank={i + 1}
                  filename={chunk.filename}
                  sourceUrl={chunk.source_url}
                  score={chunk.score}
                />
              ))}
            </div>
          </>
        )}

        {retrievedChunks.length === 0 && sources.length > 0 && (
          <>
            <p className="sources-label">Sources</p>
            <div className="sources-row">
              {sources.map((url, i) => (
                <a
                  key={url}
                  className="source-card"
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <span className="source-rank">{String(i + 1).padStart(2, "0")}</span>
                  <span className="source-title">{url.replace(/^https?:\/\/[^/]+/, "")}</span>
                </a>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

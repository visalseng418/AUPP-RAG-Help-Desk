// A single citation, styled as a library index card. `rank` reflects the
// chunk's actual position after reranking - it's real retrieval data, not
// a decorative number.

function titleFromFilename(filename) {
  const withoutExt = filename.replace(/\.txt$/, "");
  const words = withoutExt.split("-").filter(Boolean);
  return words
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export default function SourceCard({ rank, filename, sourceUrl, score }) {
  return (
    <a
      className="source-card"
      href={sourceUrl}
      target="_blank"
      rel="noopener noreferrer"
    >
      <div className="source-card-top">
        <span className="source-rank">{String(rank).padStart(2, "0")}</span>
        {typeof score === "number" && (
          <span className="source-score">{score.toFixed(2)}</span>
        )}
      </div>
      <span className="source-title">{titleFromFilename(filename)}</span>
    </a>
  );
}

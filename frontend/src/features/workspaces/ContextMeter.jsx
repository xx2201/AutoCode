function formatContextTokens(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value || 0);
}

export default function ContextMeter({ usage }) {
  const windowTokens = Math.max(0, usage.window_tokens || 0);
  const usedTokens = Math.min(
    Math.max(0, usage.used_tokens || 0),
    windowTokens || Infinity,
  );
  const usedPercent = windowTokens
    ? Math.min(100, usedTokens / windowTokens * 100)
    : 0;
  const remainingPercent = Math.max(0, 100 - usedPercent);
  const radius = 8;
  const circumference = 2 * Math.PI * radius;

  return (
    <details className={`context-meter ${usedPercent >= 80 ? "is-high" : ""}`}>
      <summary
        aria-label={`上下文窗口已使用 ${usedPercent.toFixed(0)}%`}
        title="上下文窗口"
      >
        <svg viewBox="0 0 22 22" aria-hidden="true">
          <circle className="context-ring-track" cx="11" cy="11" r={radius} />
          <circle
            className="context-ring-value"
            cx="11"
            cy="11"
            r={radius}
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - usedPercent / 100)}
          />
        </svg>
      </summary>
      <div className="context-popover">
        <strong>Context window:</strong>
        <span>
          {usedPercent.toFixed(0)}% used ({remainingPercent.toFixed(0)}% left)
        </span>
        <small>
          {formatContextTokens(usedTokens)} / {formatContextTokens(windowTokens)} tokens used
        </small>
      </div>
    </details>
  );
}

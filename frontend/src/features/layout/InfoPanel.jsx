import { X } from "lucide-react";

const PANEL_COPY = {
  turn: { overline: "RUNTIME DETAILS", title: "当前 Turn" },
  trace: { overline: "RUNTIME DETAILS", title: "运行 Trace" },
  diagnostics: { overline: "LOCAL DIAGNOSTICS", title: "本地诊断" },
};

export default function InfoPanel({ panel, content, onClose }) {
  const copy = PANEL_COPY[panel];
  if (!copy) return null;
  return (
    <div className="modal-layer" role="dialog" aria-modal="true">
      <section className="info-panel">
        <header>
          <div>
            <span className="overline">{copy.overline}</span>
            <h2>{copy.title}</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose}>
            <X size={20} />
          </button>
        </header>
        <pre className="runtime-content">{content}</pre>
      </section>
    </div>
  );
}

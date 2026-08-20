import {
  AlertCircle,
  CheckCircle2,
  Eye,
  EyeOff,
  LoaderCircle,
  PlugZap,
  Save,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";

const PROTOCOLS = [
  {
    value: "anthropic",
    label: "Anthropic Messages",
    detail: "Anthropic Messages 协议",
  },
  {
    value: "openai",
    label: "OpenAI 兼容",
    detail: "Chat Completions 协议",
  },
  {
    value: "litellm",
    label: "LiteLLM",
    detail: "LiteLLM 多供应商适配",
  },
];

const EMPTY_FORM = {
  model: "",
  api_key: "",
  base_url: "",
  provider: "anthropic",
};

function formFromConfig(config) {
  return {
    model: config?.model || "",
    api_key: "",
    base_url: config?.base_url || "",
    provider: config?.provider || "anthropic",
  };
}

export default function ModelSettingsModal({
  open,
  config,
  onClose,
  onSave,
  onTest,
}) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [apiKeyVisible, setApiKeyVisible] = useState(false);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState(null);

  useEffect(() => {
    if (!open) return undefined;
    setForm(formFromConfig(config));
    setApiKeyVisible(false);
    setBusy("");
    setFeedback(null);
    return undefined;
  }, [config, open]);

  useEffect(() => {
    if (!open) return undefined;
    function handleKeyDown(event) {
      if (event.key === "Escape" && !busy) onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [busy, onClose, open]);

  if (!open) return null;

  function updateField(event) {
    const { name, value } = event.target;
    setForm((current) => ({ ...current, [name]: value }));
    setFeedback(null);
  }

  function validateForm() {
    if (!form.model.trim()) return "请填写模型名称。";
    if (form.base_url.trim()) {
      try {
        const parsed = new URL(form.base_url.trim());
        if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
          return "URL 必须以 http:// 或 https:// 开头，并包含主机名。";
        }
      } catch {
        return "请输入有效的模型服务 URL。";
      }
    }
    return "";
  }

  async function testConnection() {
    const validationError = validateForm();
    if (validationError) {
      setFeedback({ type: "error", text: validationError });
      return;
    }
    setBusy("test");
    setFeedback(null);
    try {
      const result = await onTest(form);
      const response = result.response ? ` 返回：${result.response}` : "";
      setFeedback({ type: "success", text: `${result.message || "连接成功。"}${response}` });
    } catch (error) {
      setFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  }

  async function saveConnection(event) {
    event.preventDefault();
    const validationError = validateForm();
    if (validationError) {
      setFeedback({ type: "error", text: validationError });
      return;
    }
    setBusy("save");
    setFeedback(null);
    try {
      await onSave(form);
    } catch (error) {
      setFeedback({ type: "error", text: error.message });
    } finally {
      setBusy("");
    }
  }

  return (
    <div
      className="modal-layer model-settings-layer"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="model-settings-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-settings-title"
      >
        <header className="model-settings-head">
          <div className="model-settings-mark"><PlugZap size={21} /></div>
          <div>
            <span className="overline">MODEL CONNECTION</span>
            <h2 id="model-settings-title">模型设置</h2>
            <p>配置当前本机 Runner 使用的模型连接，保存后立即应用。</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭模型设置"
            onClick={onClose}
            disabled={Boolean(busy)}
          >
            <X size={18} />
          </button>
        </header>

        <form className="model-settings-form" onSubmit={saveConnection}>
          <label className="model-settings-field">
            <span>协议</span>
            <select name="provider" value={form.provider} onChange={updateField}>
              {PROTOCOLS.map((protocol) => (
                <option key={protocol.value} value={protocol.value}>
                  {protocol.label} · {protocol.detail}
                </option>
              ))}
            </select>
          </label>

          <label className="model-settings-field">
            <span>模型</span>
            <input
              name="model"
              value={form.model}
              onChange={updateField}
              placeholder="例如：claude-sonnet-4-6"
              autoComplete="off"
            />
          </label>

          <label className="model-settings-field">
            <span>URL</span>
            <input
              name="base_url"
              value={form.base_url}
              onChange={updateField}
              placeholder="例如：https://api.anthropic.com"
              autoComplete="url"
              inputMode="url"
            />
            <small>可填写兼容网关地址；留空使用 SDK 默认地址。</small>
          </label>

          <label className="model-settings-field">
            <span>API Key</span>
            <div className="model-settings-secret">
              <input
                name="api_key"
                type={apiKeyVisible ? "text" : "password"}
                value={form.api_key}
                onChange={updateField}
                placeholder={config?.api_key_configured ? "已配置，留空保持不变" : "请输入 API Key"}
                autoComplete="new-password"
              />
              <button
                className="model-settings-secret-toggle"
                type="button"
                aria-label={apiKeyVisible ? "隐藏 API Key" : "显示 API Key"}
                onClick={() => setApiKeyVisible((visible) => !visible)}
              >
                {apiKeyVisible ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <small>保存成功后 API Key 不会在响应中回显，只在本机配置文件中持久化。</small>
          </label>

          {feedback && (
            <div className={`model-settings-feedback ${feedback.type}`} role="status">
              {feedback.type === "success"
                ? <CheckCircle2 size={16} />
                : <AlertCircle size={16} />}
              <span>{feedback.text}</span>
            </div>
          )}

          <footer className="model-settings-actions">
            <button
              className="secondary-action"
              type="button"
              onClick={testConnection}
              disabled={Boolean(busy)}
            >
              {busy === "test" ? <LoaderCircle className="spin" size={16} /> : <PlugZap size={16} />}
              测试连接
            </button>
            <div className="model-settings-actions-right">
              <button className="plain-action" type="button" onClick={onClose} disabled={Boolean(busy)}>
                取消
              </button>
              <button className="primary-action" type="submit" disabled={Boolean(busy)}>
                {busy === "save" ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}
                保存并应用
              </button>
            </div>
          </footer>
        </form>
      </section>
    </div>
  );
}

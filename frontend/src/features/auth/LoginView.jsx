import {
  Activity,
  ArrowRight,
  Code2,
  FolderGit2,
  KeyRound,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useState } from "react";

export default function LoginView({ onLogin, error, busy }) {
  const [candidate, setCandidate] = useState("");

  return (
    <main className="login-page">
      <div className="login-orb orb-one" />
      <div className="login-orb orb-two" />
      <section className="login-shell">
        <div className="login-story">
          <div className="wordmark">
            <span className="logo-mark"><Code2 size={22} /></span>
            <strong>AutoCode</strong>
          </div>
          <div className="login-heading">
            <span className="hero-chip">
              <Sparkles size={15} /> YOUR LOCAL AI WORKSPACE
            </span>
            <h1>你的项目，<br />随时从手机继续。</h1>
            <p>项目、模型和命令都留在你的电脑上。Web 只负责安全地发送任务与呈现结果。</p>
          </div>
          <div className="login-features">
            <span><FolderGit2 size={17} /> 按项目切换 workspace</span>
            <span><ShieldCheck size={17} /> 本机执行与审批</span>
            <span><Activity size={17} /> 会话与 Trace</span>
          </div>
        </div>
        <form
          className="login-card"
          onSubmit={(event) => {
            event.preventDefault();
            if (candidate.trim()) onLogin(candidate.trim());
          }}
        >
          <div className="login-card-icon"><KeyRound size={24} /></div>
          <span className="overline">SECURE ACCESS</span>
          <h2>连接本机 Agent</h2>
          <p>输入部署时生成的浏览器访问令牌。</p>
          <label>
            访问令牌
            <div className="token-input">
              <input
                type="password"
                value={candidate}
                onChange={(event) => setCandidate(event.target.value)}
                placeholder="粘贴访问令牌"
                autoComplete="current-password"
                required
              />
              <ShieldCheck size={18} />
            </div>
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="primary-action login-action" type="submit" disabled={busy}>
            {busy ? <RefreshCw className="spin" size={18} /> : <ArrowRight size={18} />}
            {busy ? "正在连接" : "进入工作台"}
          </button>
          <small className="privacy-copy">令牌只保存在当前浏览器，不会出现在 URL 中。</small>
        </form>
      </section>
    </main>
  );
}

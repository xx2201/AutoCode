# Development Relay deployment

This document records the deployment layout used by the AutoCoder development
Relay. The server keeps the application source in a Git working tree, while
runtime state remains outside that tree. Do not place tokens, TLS private keys,
sessions, or workspace data in Git.

## Server layout

The systemd service runs as `dev:dev` and uses:

```text
/home/dev/corecoder-web/
├── worktree/                 # Git working tree, checked out from origin/main
│   ├── autocode/
│   ├── frontend/
│   └── deploy/
├── shared/
│   ├── autocode-web.env
│   └── sessions/
├── tls/
├── venv/
└── workspace/
```

The old `releases/`, `current`, and `shared/active-release` entries may remain
for historical rollback, but they are no longer used by the systemd service.
The source of truth for the running code is:

```bash
git -C /home/dev/corecoder-web/worktree rev-parse HEAD
```

The local Runner stores complete session data in workspace partitions:

```text
~/.autocode/sessions/
├── projects/
│   └── <readable-project>/
│       ├── project.json
│       └── sessions/
│           └── <session-id>/
├── .session-locations/
│   └── <session-id>.json
└── .layout.json
```

`.session-locations` contains only small location pointers for APIs that resume
a session by id. Checkpoints, transcripts, traces, audit logs, and ChangeSets
live below the owning project directory.

Runner startup moves legacy root-level session directories into this layout
before connecting to the Relay. Migration uses same-volume atomic directory
moves, is safe to rerun after interruption, and removes the obsolete
`.workspace-index` only after every legacy session has moved.

Before downgrading to a release that predates project-partitioned storage, stop
the Runner and restore the legacy layout while the current release remains
installed:

```powershell
& "G:/mycode/AutoCoder/.venv/Scripts/python.exe" -c "from autocode.state import restore_flat_session_layout; print(restore_flat_session_layout())"
```

The installed service configuration is represented by
`deploy/corecoder-web.service`:

```text
WorkingDirectory=/home/dev/corecoder-web/worktree
EnvironmentFile=/home/dev/corecoder-web/shared/autocode-web.env
ExecStart=/home/dev/corecoder-web/venv/bin/python -m autocode.web
```

`shared/autocode-web.env`, TLS keys, session data, and workspace data are
persistent state. Git synchronization must not modify them.

## Prepare and push a commit

Build the frontend locally before committing. Generated files under
`autocode/web/static` are committed so the server does not need Node.js or a
separate build step:

```powershell
Push-Location "G:/mycode/AutoCoder/frontend"
npm ci
npm test
npm run check
npm run build
Pop-Location

git status --short
git add autocode/web/static frontend deploy docs README.md README_CN.md
git commit -m "<describe the deployment change>"
git push origin main
```

Only push after the intended commit and its generated frontend assets have been
reviewed. The server sync below refuses to advance a dirty worktree.

## Synchronize the server Git working tree

The development Relay uses the repository's HTTPS remote so the server only
needs outbound Git access. Replace the host and key path when using another
Relay:

```powershell
$hostName = 'dev@34.142.199.209'
$keyPath = 'C:/path/to/dev_34_142_199_209_id_rsa'
$commit = (git rev-parse HEAD).Trim()
$worktree = '/home/dev/corecoder-web/worktree'
$repoUrl = 'https://github.com/xx2201/AutoCode.git'

$remote = @'
set -eu
worktree='/home/dev/corecoder-web/worktree'
repo_url='https://github.com/xx2201/AutoCode.git'
commit='__COMMIT__'

if [ -e "$worktree" ]; then
  test -d "$worktree/.git"
  test -z "$(git -C "$worktree" status --porcelain)"
  test "$(git -C "$worktree" remote get-url origin)" = "$repo_url"
  git -C "$worktree" fetch --prune origin main
  git -C "$worktree" switch main
  git -C "$worktree" merge --ff-only origin/main
else
  git clone --branch main --single-branch "$repo_url" "$worktree"
fi

test "$(git -C "$worktree" rev-parse HEAD)" = "$commit"
test -f "$worktree/autocode/web/static/index.html"
test -n "$(find "$worktree/autocode/web/static" -maxdepth 1 -type f -name '*.js' -print -quit)"
test -n "$(find "$worktree/autocode/web/static" -maxdepth 1 -type f -name '*.css' -print -quit)"

sudo install -o root -g root -m 0644 "$worktree/deploy/corecoder-web.service" /etc/systemd/system/corecoder-web.service
sudo systemctl daemon-reload
sudo systemctl restart corecoder-web.service
'@
$remote = $remote.Replace('__COMMIT__', $commit)

$sshArgs = @('-i', $keyPath, $hostName, $remote)
& ssh.exe @sshArgs
if ($LASTEXITCODE -ne 0) {
    throw "Server Git synchronization failed with exit code $LASTEXITCODE."
}
```

The command intentionally fails when the server worktree has local changes or
an unexpected `origin` URL. It performs a fast-forward merge, never overwrites
the persistent directories, installs the service file from the checked-out
commit, and restarts the Relay only after the commit and static files match.

## Restart the local Runner

The development Windows machine uses the `AutoCodeLocalWebRunner` scheduled
task. Its action is:

```text
Program: G:/mycode/AutoCoder/.venv/Scripts/python.exe
Arguments: -m autocode.web.runner
Working directory: G:/mycode/AutoCoder
```

Restart this task after Runner code changes. Do not start a second manual Runner
while the scheduled task is running.

Register or repair the task with the repository script:

```powershell
pwsh.exe -NoLogo -NoProfile -File deploy/install-windows-runner-task.ps1
```

The task combines `RestartOnFailure` with a one-minute repeating recovery
trigger. `MultipleInstances=IgnoreNew` prevents duplicate Runner instances while
the repeating trigger brings back a task that exited without being restarted by
Task Scheduler's failure policy.

## Verification

Verify independent signals after every deployment:

```bash
systemctl is-active corecoder-web.service
git -C /home/dev/corecoder-web/worktree rev-parse HEAD
git -C /home/dev/corecoder-web/worktree status --short
journalctl -u corecoder-web.service -n 50 --no-pager
```

The public health endpoint must return `status: ok` and
`runner_connected: true`. Also verify that the public HTML references the
expected frontend asset names and that recent service logs contain successful
Runner heartbeat and polling requests.

## Public IP certificate

The development Relay uses a publicly trusted, short-lived Let's Encrypt
certificate for `34.142.199.209`. Certbot is installed in `/opt/certbot`.

The certificate is renewed by:

```text
certbot-autocode-renew.timer
  -> certbot-autocode-renew.service
  -> /usr/local/sbin/deploy-autocode-ip-cert
```

The deploy hook copies the renewed certificate and private key into
`/home/dev/corecoder-web/tls` with permissions suitable for the `dev` service
user, then restarts `corecoder-web.service`. The source files for the hook and
systemd units are stored under `deploy/`.

IP certificates are short-lived. Verify the timer after every deployment or
server maintenance:

```bash
systemctl status certbot-autocode-renew.timer --no-pager
systemctl list-timers certbot-autocode-renew.timer --no-pager
/opt/certbot/bin/certbot certificates --cert-name 34.142.199.209
```

## Rollback

Keep the server worktree clean, then switch it to a known-good commit and
restart the service:

```bash
git -C /home/dev/corecoder-web/worktree status --short
git -C /home/dev/corecoder-web/worktree switch --detach <known-good-commit>
sudo systemctl restart corecoder-web.service
```

The next normal deployment switches back to `main`, fetches `origin/main`, and
fast-forwards the worktree. Persistent state is outside Git and is not part of
the rollback.

---

## 中文说明

开发 Relay 使用服务器上的 Git 工作树
`/home/dev/corecoder-web/worktree`，systemd 直接从该目录运行代码。原有的
`releases/`、`current` 和 `shared/active-release` 可以暂时保留作历史回退，
但不再是运行版本来源。`shared/autocode-web.env`、TLS 私钥、会话数据和
工作区数据都在工作树之外，更新代码时不能覆盖。

发布流程是：本地构建并提交 `autocode/web/static`，推送 `origin/main`，
服务器执行 `git fetch` + `git merge --ff-only`，确认工作树没有本地修改后，
从工作树安装 systemd 配置并重启 Relay。这样以后更新只需要提交、推送和
快进同步，不再生成或上传 wheel。

服务器同步命令会在以下情况主动失败：工作树存在未提交修改、`origin` 地址
不是预期仓库、目标 commit 不一致，或前端 HTML/JS/CSS 资源缺失。它不会修改
`shared`、`tls`、`workspace` 等持久化目录。

本机完整 Session 已按 workspace 物理分区存放在
`~/.autocode/sessions/projects/<可读项目路径>/sessions/`，例如
`G:/mycode/AutoCoder` 对应 `G--mycode-AutoCoder`。全局
`.session-locations` 只保存通过 `session_id` 恢复会话所需的位置指针，不保存
消息正文。Runner 启动时会先把旧版根目录下的 Session 原子移动到项目目录，
完成后才连接 Relay；迁移可中断后重跑。若要降级到不认识新布局的旧版本，
先停止 Runner，再执行上面的 `restore_flat_session_layout()`。

公网 IP 证书由 `certbot-autocode-renew.timer` 自动续期；续期 hook 会把新证书
复制到 Relay 的 `tls` 目录并重启服务。服务器维护后必须确认该定时器仍为
active。

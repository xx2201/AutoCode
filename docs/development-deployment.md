# Development Relay deployment

This document records the deployment layout used by the AutoCoder development
Relay. It is different from a Git checkout: do not run `git pull` in
`/home/dev/corecoder-web`.

## Server layout

The systemd service runs as `dev:dev` and uses:

```text
/home/dev/corecoder-web/
├── current -> releases/<release-id>
├── releases/
│   └── <release-id>/
│       └── autocode-<version>-py3-none-any.whl
├── shared/
│   ├── active-release
│   ├── autocode-web.env
│   └── sessions/
├── tls/
├── venv/
└── workspace/
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
& "G:/mycode/AutoCoder/.venv/Scripts/python.exe" -c `
  "from autocode.state import restore_flat_session_layout; print(restore_flat_session_layout())"
```

The installed service configuration is represented by
`deploy/corecoder-web.service`:

```text
WorkingDirectory=/home/dev/corecoder-web
EnvironmentFile=/home/dev/corecoder-web/shared/autocode-web.env
ExecStart=/home/dev/corecoder-web/venv/bin/autocode-web
```

`shared/autocode-web.env`, TLS keys, session data, and workspace data are
persistent state. A release must not overwrite them.

## Build from an exact commit

Commit and push the intended source first. Build from that commit in a clean
temporary directory so unrelated working-tree changes cannot enter the wheel.

```powershell
$commit = git rev-parse HEAD
$releaseId = git rev-parse --short HEAD
$archive = Join-Path $env:TEMP "autocode-$releaseId.zip"
$source = Join-Path $env:TEMP "autocode-$releaseId"

git archive --format=zip --output=$archive $commit
Expand-Archive -LiteralPath $archive -DestinationPath $source

Push-Location "$source/frontend"
npm ci
npm test
npm run check
npm run build
Pop-Location

Push-Location $source
python -m pip wheel . --no-deps --wheel-dir dist
Pop-Location
```

Before upload, verify that the wheel contains the generated
`autocode/web/static/index.html`, JavaScript, and CSS assets.

## Publish a release

The examples below use placeholders deliberately. Keep the SSH private-key path
outside the repository and never copy tokens or TLS private keys into a release.

```powershell
$hostName = 'dev@<relay-host>'
$keyPath = 'C:/path/to/id_rsa'
$releaseId = git rev-parse --short HEAD
$wheel = Get-ChildItem "$source/dist/autocode-*.whl" | Select-Object -First 1

$sshArgs = @(
  '-i'
  $keyPath
  $hostName
  "mkdir -m 700 /home/dev/corecoder-web/releases/$releaseId"
)
& ssh.exe @sshArgs

$scpArgs = @(
  '-i'
  $keyPath
  $wheel.FullName
  "${hostName}:/home/dev/corecoder-web/releases/$releaseId/$($wheel.Name)"
)
& scp.exe @scpArgs
```

Compare the local and remote SHA-256 hashes before installation. Then install
the verified wheel into the shared virtual environment, switch the release
metadata, and restart the Relay:

```bash
/home/dev/corecoder-web/venv/bin/python -m pip install \
  --no-deps --force-reinstall \
  /home/dev/corecoder-web/releases/<release-id>/autocode-<version>-py3-none-any.whl

ln -sfn \
  /home/dev/corecoder-web/releases/<release-id> \
  /home/dev/corecoder-web/current

printf '%s\n' '<release-id>' \
  > /home/dev/corecoder-web/shared/active-release

sudo systemctl restart corecoder-web.service
```

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
readlink /home/dev/corecoder-web/current
cat /home/dev/corecoder-web/shared/active-release
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

Choose a previous directory under `releases/`, reinstall its wheel into the
shared venv, point `current` and `shared/active-release` back to that release,
then restart and repeat the verification checks.

---

## 中文说明

开发机的 `/home/dev/corecoder-web` 是版本化 wheel 发布目录，不是 Git
工作树，不能在该目录执行 `git pull`。每次发布应先提交并 push 目标代码，
再从精确 commit 导出干净源码，完成前端测试和构建、生成 wheel、校验本地与
远端 SHA-256，然后安装到共享 `venv`，更新 `current` 和
`shared/active-release`，最后重启 `corecoder-web.service`。

`shared/autocode-web.env`、TLS 私钥、会话和工作区数据都是持久化数据，
发布过程中不得覆盖。Runner 代码发生变化时，还需要重启 Windows 计划任务
`AutoCodeLocalWebRunner`。部署完成后必须同时检查 systemd 状态、公网页面
资产、健康接口中的 `runner_connected`，以及 Runner heartbeat/轮询日志。

本机完整 Session 已按 workspace 物理分区存放在
`~/.autocode/sessions/projects/<可读项目路径>/sessions/`，例如
`G:/mycode/AutoCoder` 对应 `G--mycode-AutoCoder`。全局
`.session-locations` 只保存通过 `session_id` 恢复会话所需的位置指针，不保存
消息正文。Runner 启动时会先把旧版根目录下的 Session 原子移动到项目目录，
完成后才连接 Relay；迁移可中断后重跑。若要降级到不认识新布局的旧版本，
必须先停止 Runner，并使用上方 `restore_flat_session_layout` 命令恢复旧布局。

开发机公网 IP 证书由 `/opt/certbot` 中的 Certbot 管理。系统定时器
`certbot-autocode-renew.timer` 每天检查两次；续期成功后，
`deploy-autocode-ip-cert.sh` 会把新证书复制到 Relay 的 `tls` 目录并重启
服务。IP 证书有效期很短，服务器维护后必须确认该定时器仍为 active。

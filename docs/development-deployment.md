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

# PC File Monitoring and Telegram Notification System

A lightweight agent that watches project folders on your PC and sends a Telegram message to your phone whenever files change. It is built for following an AI coding agent's work while you are away from the computer: it groups rapid edits, summarizes them with Git (branch, files, `+/-` lines), can attach the diff and the changed files, runs your build and tests, and answers commands such as `/status` or `/diff`.

The PC only makes **outgoing HTTPS requests** to the Telegram Bot API. The phone and the PC do not need to be on the same network, and you don't need port forwarding, a static IP or a VPN.

```text
 PC                                                      INTERNET
┌───────────────────────────────────────────────┐
│ Project files ──► File watcher (watchdog)     │
│                     │ debounce / grouping     │
│                     ▼                         │
│                Git analyzer ──► Build/tests   │
│                     │                         │   HTTPS   ┌──────────┐
│                     ▼                         │ ────────► │ Telegram │ ──► 📱 Phone
│                Telegram bot (send + commands) │ ◄──────── │ Bot API  │
└───────────────────────────────────────────────┘ long poll └──────────┘
```

The full design is in [PC_File_Monitoring_Telegram_Notification_System.md](PC_File_Monitoring_Telegram_Notification_System.md). This repository implements all five phases described there.

## Features

- Watches one or more folders for created, modified, deleted and renamed files.
- **Debouncing:** rapid edits (5 s by default) become a single message.
- **Git integration:** shows the branch, the changed files with their status and `+/-` line counts, untracked files, new commits and branch switches. Files ignored by `.gitignore` (build output) and saves that don't change any content are not reported.
- **Plain folders** without Git are supported too; they get file names and event types.
- **Four notification levels**, from "file changed" up to a full *AI task started → build → AI task completed* flow.
- **Attachments:** the diff as a `.patch` file, the modified files themselves, and the build log when a build fails.
- **Build and tests:** run on every change, when the AI task finishes, or on demand with `/build`.
- **Two-way commands:** `/status /projects /diff /build /log /stop /start /help`, accepted only from your chat ID.
- **Windows background operation:** Task Scheduler autostart, plus packaging as `tpms-agent.exe`.
- **Built to run unattended:** retries and Telegram flood-control handling, log rotation, and the bot token is redacted from all logs.

## Quick start

### 1. Create the Telegram bot

1. In Telegram, open **@BotFather**, send `/newbot` and follow the prompts.
2. Copy the **bot token** (`123456789:AA...`). Treat it like a password.
3. Open your new bot and press **Start**, so the bot is allowed to message you.

### 2. Install (Python 3.9+)

Windows, using PowerShell in the repository folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
```

This creates `.venv`, installs the dependencies and creates `config.yaml` and `.env` from the examples.

Manual or other platforms:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip ...
cp config.example.yaml config.yaml
cp .env.example .env
```

### 3. Configure

Put the token into `.env`, then find your chat ID:

```bash
python agent.py chat-id          # lists chats that recently messaged the bot
```

Put the chat ID into `.env`:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=987654321
```

Then edit the `projects:` list in `config.yaml`:

```yaml
projects:
  - name: TPMS ECU
    path: "C:\\Projects\\tpms_ecu"
    git: true
```

### 4. Check and run

```bash
python agent.py check            # validates config, Git, project folders, bot and chat
python agent.py send             # sends a test message
python agent.py run              # start monitoring (Ctrl+C to stop)
```

On Windows you can also start it with `scripts\windows\run_agent.bat`.

`-c path\to\config.yaml` selects a different config file. Without it, the agent looks for `config.yaml` in the current folder, then next to `agent.py` or the exe. You can also point `TPMS_AGENT_CONFIG` at the file.

## Notification levels

Set `notifications.level` in `config.yaml`:

| Level | Name | What is sent |
|---|---|---|
| 1 | Basic | `📝 File changed · TPMS ECU` + file names |
| 2 | Git summary (default) | Project, branch, files with `M/A/D/R` status and `+/-` lines, totals, new commits |
| 3 | Full change | Level 2 + inline diff preview + `.patch` attachment + modified files (per `send_diff` / `send_files`) |
| 4 | AI development monitor | *AI Task Started* → silent *Files Modified* updates → *Build started* → **AI TASK COMPLETED** with build result, Git totals, duration and attachments |

Example level 2/3 message:

```text
🤖 Project Updated

Project: TPMS ECU
Branch: ecu_securt_comm

Modified files:
• M Sources/CAN_ResponseTask.c  +50 -30
• M Sources/CAN_ResponseTask.h  +4 -1
• A Sources/FirmwareUpdate.c  +30 -0

Changes: +84 / -31 lines

Time: 09:15:23
📎 changes-tpms-ecu-20260106-091523.patch
```

**How AI tasks are detected (level 4):** a task starts with the first change after a quiet period. It completes when no change has happened for `watch.task_idle_seconds` (120 s by default).

The completion summary compares the files the task touched with the commit from **when the task started**. Work the AI agent committed during the task is therefore still included, and the new commits are listed.

### Explicit "task completed" from an AI agent hook

Many AI coding tools can run a command when they finish. The `report` command sends a one-off summary of the uncommitted changes and can optionally build first. You can run it alongside the background agent or on its own.

For example, a Claude Code `Stop` hook in the project's `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "C:\\Tools\\pc-agent\\.venv\\Scripts\\python.exe C:\\Tools\\pc-agent\\agent.py report -c C:\\Tools\\pc-agent\\config.yaml --project \"TPMS ECU\" --title \"AI TASK COMPLETED\" --build"
          }
        ]
      }
    ]
  }
}
```

## Telegram commands

| Command | Description |
|---|---|
| `/status [project]` | Branch, number of modified files and `+/-`, last build, active AI task, last activity |
| `/projects` | Numbered list of monitored projects |
| `/diff [project]` | Current uncommitted diff as a `.patch` file (requires `send_diff: true`) |
| `/build [project]` | Run the configured build/tests now and report the result |
| `/log [lines]` | Last lines of the agent log |
| `/stop` / `/start` | Pause / resume automatic notifications; commands keep working |
| `/help` | Command list |

Give projects by number (`/diff 2`), by name, or by a unique prefix (`/status tpms`). With a single project you can leave the argument out.

Only `telegram.chat_id` and the IDs in `commands.authorized_chat_ids` are served. Messages from anyone else are logged and ignored without a reply.

Commands sent while the agent was not running are skipped at startup, so an old `/build` doesn't suddenly run. Set `commands.enabled: false` to make the bot send-only.

## Build and tests

```yaml
projects:
  - name: TPMS ECU
    path: "C:\\Projects\\tpms_ecu"
    build:
      command: "make -C Debug all"          # string = run through the shell; list = argv
      test_command: "ctest --test-dir build" # runs only if the build succeeded
      run_on: task_complete                 # manual | change | task_complete
      timeout_seconds: 600
      # cwd: "build"                        # default: the project folder
```

- `manual` runs only on `/build` (or `report --build`).
- `change` runs after every group of changes; the result is included in the message.
- `task_complete` runs once the AI task has gone idle. At level 4 the result is part of the *AI TASK COMPLETED* message.

On failure, the last output lines are shown and the full log is attached when `send_build_result: true`. A build that runs past its timeout is killed together with its child processes.

Filesystem events that happen while a build runs are ignored, so build output can't trigger new notifications or another build.

## Configuration reference

[`config.example.yaml`](config.example.yaml) documents every option. Main sections:

| Section | Keys |
|---|---|
| `telegram` | `bot_token`, `chat_id`, `api_base_url` (for a self-hosted Bot API server), `timeout_seconds` |
| `commands` | `enabled`, `authorized_chat_ids`, `poll_timeout_seconds` |
| `notifications` | `level`, `send_diff`, `send_files`, `send_build_result`, `inline_diff_max_lines`, `max_file_size_kb`, `max_files`, `max_listed_files`, `notify_on_startup`, `silent_intermediate` |
| `watch` | `debounce_seconds`, `max_batch_seconds`, `task_idle_seconds`, `ignore`, `use_default_ignores` |
| `logging` | `level`, `file`, `max_bytes`, `backup_count` |
| `projects[]` | `name`, `path`, `git` (`true`/`false`/`auto`), `ignore`, `build` |

Notes:

- **Environment variables:** any string can use `${VAR}` or `${VAR:-default}`. A `.env` file next to `config.yaml` is loaded automatically; it never overrides variables that are already set.
- **Relative paths** are relative to the config file.
- **Ignore patterns** follow gitignore conventions: `Debug/` matches a folder at any depth, `*.o` matches a name, and `build/out/*.map` matches a path from the project root. Defaults such as `.git/`, `node_modules/`, `__pycache__/`, `*.swp` and `*.tmp` apply unless `use_default_ignores: false`. In Git projects, files covered by `.gitignore` are not reported either.
- **Errors and typos:** all config errors are reported at once, and unknown keys produce a warning, so typos are caught.

## Running in the background on Windows

**Autostart at logon (Task Scheduler).** This runs `pythonw.exe` without a window, as your user, and restarts it if it stops:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_startup_task.ps1
# remove again:
powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_startup_task.ps1
```

**Packaged executable (Phase 5):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\build_exe.ps1
```

This produces two files in `dist\`:

- `tpms-agent.exe` has no console window and is meant for running in the background.
- `tpms-agent-console.exe` is for `check`, `chat-id`, `send` and `report`.

Put `config.yaml` and `.env` next to the exe, then register the autostart:

```powershell
scripts\windows\install_startup_task.ps1 -Exe C:\Tools\tpms-agent\tpms-agent.exe
```

The log goes to `logs\agent.log` next to `config.yaml`, or wherever `logging.file` points. You can also read it from your phone with `/log`.

## Security

- **Bot token:** keep it in `.env` or an environment variable; `config.yaml` and `.env` are git-ignored. The token is redacted from log files and error messages.
- **Commands:** only allowlisted chat IDs can send commands. Commands can't run arbitrary shell commands; `/build` runs only the command configured in `config.yaml`.
- **Source code leaves the PC** when `send_diff` or `send_files` is enabled: diffs, inline previews, files and build logs are uploaded to Telegram. Only enable them if your project's and company's policies allow it. The safer setting is level 2 with `send_diff: false` and `send_files: false`, which sends only file names, Git statistics and build status.
- **Read-only Git:** the agent only reads repositories (`GIT_OPTIONAL_LOCKS=0`), so it never takes Git's index lock while your AI agent or IDE is using Git.

## How it works

| Module | Responsibility (design doc section 10) |
|---|---|
| [`agent.py`](agent.py) → [`tpms_agent/agent.py`](tpms_agent/agent.py) | Entry point and CLI; coordinates watchers, Git analysis, AI tasks, builds and notifications |
| [`tpms_agent/watcher.py`](tpms_agent/watcher.py) | watchdog observers, ignore patterns, event coalescing, debouncing/grouping |
| [`tpms_agent/git_monitor.py`](tpms_agent/git_monitor.py) | Repository detection, branch, `status`, `numstat`, diffs, untracked files, commits |
| [`tpms_agent/diff.py`](tpms_agent/diff.py) | Change summaries, `.patch` documents, inline diff previews, file attachments |
| [`tpms_agent/telegram.py`](tpms_agent/telegram.py) | Bot API client: messages, documents/albums, `getUpdates`, retries, splitting |
| [`tpms_agent/commands.py`](tpms_agent/commands.py) | Two-way commands with chat-ID allowlist |
| [`tpms_agent/build_monitor.py`](tpms_agent/build_monitor.py) | Build/test execution with timeout and process-tree kill |
| [`tpms_agent/messages.py`](tpms_agent/messages.py) | Telegram message templates |
| [`tpms_agent/config.py`](tpms_agent/config.py) | YAML config, `${ENV}` expansion, `.env`, validation |
| [`tpms_agent/logger.py`](tpms_agent/logger.py) | Console and rotating file logs with secret redaction |

Processing flow:

1. The watcher reports an event.
2. Events are grouped until the project has been quiet for `debounce_seconds`.
3. `git status`, the branch and `HEAD` are read; only files with a real Git change are kept.
4. `+/-` statistics and the diff are generated.
5. The build runs, if configured for this trigger.
6. The message is sent with `sendMessage`, and attachments with `sendDocument` / `sendMediaGroup`.

Changes inside `.git` (`HEAD`, branch refs) trigger a re-check, so commits and branch switches are reported even when no working file changed.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The test suite covers unit tests for every module, integration tests that use real Git repositories and real file watching, and end-to-end tests. The end-to-end tests run the actual `agent.py` process against a local fake Telegram Bot API server ([`tests/fake_bot_api.py`](tests/fake_bot_api.py)).

## Troubleshooting

| Problem | Fix |
|---|---|
| `check` says *chat not found* | Open the bot in Telegram and press **Start**; verify the ID with `python agent.py chat-id`. |
| *Conflict: terminated by other getUpdates request* in the log | Two agents with commands enabled use the same bot; stop one (or set `commands.enabled: false`). |
| No notifications for build output | Expected: gitignored files and changes during builds are not reported. |
| Too many messages | Raise `watch.debounce_seconds`, use level 4 (intermediate updates are silent), or add `ignore` patterns. |
| Behind a corporate proxy | Set `HTTPS_PROXY`; the agent's HTTP client (`requests`) uses it automatically. |

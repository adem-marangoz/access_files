# PC File Monitoring and Telegram Notification System

## 1. Overview

This document describes a lightweight Windows-based agent that monitors selected project folders on a PC and sends notifications to a personal Telegram account whenever files are modified.

The system is designed for scenarios where an AI coding agent or another application modifies source files on the computer.

The PC and the phone **do not need to be on the same network**. The PC communicates with Telegram over HTTPS through the Internet, and Telegram delivers the notification to the phone.

### High-level architecture

```text
                 PC
┌──────────────────────────────────────────┐
│                                          │
│  Git / Project Files                     │
│          │                               │
│          ▼                               │
│   ┌───────────────┐                      │
│   │  File Watcher │                      │
│   └───────┬───────┘                      │
│           │                              │
│      File changed                        │
│           │                              │
│           ▼                              │
│   ┌───────────────┐                      │
│   │ Change Engine │                      │
│   │               │                      │
│   │ - File name   │                      │
│   │ - Timestamp   │                      │
│   │ - Git status  │                      │
│   │ - Git diff    │                      │
│   └───────┬───────┘                      │
│           │                              │
│           ▼                              │
│      Telegram Bot API                   │
└───────────┼──────────────────────────────┘
            │
            │ HTTPS / Internet
            ▼
      Telegram Servers
            │
            ▼
        📱 Phone
```

---

## 2. Main Requirements

The proposed system should provide:

- Monitoring of one or more directories.
- Detection of file creation, modification, deletion, and optionally renaming.
- Support for normal folders and Git repositories.
- Detection of changed Git files.
- Generation of Git diffs for modified files.
- Telegram notifications when changes are detected.
- Optional sending of modified files.
- Grouping of multiple rapid changes into a single notification.
- Git branch information.
- Optional build/test results.
- Continuous operation in the Windows background.
- Operation without requiring the PC and phone to be on the same network.

---

## 3. Network Architecture

The system does **not** require direct communication between the phone and PC.

The PC initiates an HTTPS connection to Telegram:

```text
PC ───── HTTPS ─────> Telegram
                         │
                         ▼
                       Phone
```

The phone does not need:

- The PC's IP address.
- Port forwarding.
- A static public IP.
- A VPN between the phone and PC.
- To be connected to the same Wi-Fi network.

As long as the PC has Internet access and can reach Telegram's Bot API, it can send notifications.

Telegram Bot API documentation:

https://core.telegram.org/bots/api

---

## 4. Telegram Bot

A Telegram Bot should be created specifically for this monitoring system.

The bot will be responsible for:

1. Receiving notifications from the PC agent.
2. Sending text messages to the user's Telegram chat.
3. Sending files.
4. Optionally receiving commands from the user.

The bot token must be treated as a secret and should never be committed to Git.

A configuration file or environment variable should be used instead.

Example:

```text
TELEGRAM_BOT_TOKEN=<SECRET>
TELEGRAM_CHAT_ID=<YOUR_CHAT_ID>
```

---

## 5. File Monitoring

A Python-based file watcher can monitor selected project directories.

For example:

```text
C:\Projects\tpms_ecu
C:\Projects\tpms_tool_esp
C:\Projects\diagnostic_tool
```

The watcher should detect events such as:

```text
MODIFIED
CREATED
DELETED
RENAMED
```

A Python filesystem monitoring library such as `watchdog` can be used.

The watcher should not send a Telegram message immediately for every filesystem event.

AI coding agents often modify the same file many times within a few seconds.

Therefore, the system should use a **debounce/grouping mechanism**.

Example:

```text
09:15:01  CAN_ResponseTask.c modified
09:15:02  CAN_ResponseTask.c modified
09:15:03  CAN_ResponseTask.c modified
09:15:04  ECU_Update.c modified
09:15:05  CAN_ResponseTask.c modified

              ↓
          Wait 5 seconds
              ↓

       ONE Telegram message
```

This prevents notification spam.

---

## 6. Git Integration

For Git repositories, the agent should use Git information instead of relying only on filesystem events.

Useful commands include:

```bash
git status --short
git branch --show-current
git diff
git diff --stat
```

The agent can determine:

- Current branch.
- Modified files.
- Added files.
- Deleted files.
- Number of inserted lines.
- Number of removed lines.
- Actual source-code differences.

Example:

```text
Branch:
ecu_securt_comm

Modified:
  M Sources/CAN_ResponseTask.c
  M Sources/CAN_ResponseTask.h
  A Sources/FirmwareUpdate.c

Statistics:
  84 insertions
  31 deletions
```

---

## 7. Telegram Notification Example

A notification could look like this:

```text
🤖 AI Agent Modified Project

Project:
TPMS ECU

Branch:
ecu_securt_comm

Modified files:
  • CAN_ResponseTask.c
  • CAN_ResponseTask.h
  • FirmwareUpdate.c

Changes:
  +84 lines
  -31 lines

Status:
  Files modified

Time:
  09:15:23
```

The agent can then send the Git diff as a `.patch` or `.diff` file.

For example:

```text
📎 changes.patch
```

It can also send the modified source files:

```text
📎 CAN_ResponseTask.c
📎 CAN_ResponseTask.h
📎 FirmwareUpdate.c
```

Telegram's Bot API provides `sendMessage` for text messages and `sendDocument` for files.

---

## 8. Example Git Diff

The Telegram notification can include a short summary while the complete diff is attached as a file.

Example:

```diff
- FLEXCAN_DRV_Send(INST_CANCOM1, TX_MAILBOX_PC, ...);
+ FLEXCAN_DRV_Send(INST_CANCOM1, TX_MAILBOX_PC, ...);
+ vTaskDelay(pdMS_TO_TICKS(10));
```

For larger changes, it is better to send the complete diff as an attachment rather than putting the entire diff into the Telegram message.

---

## 9. Project Configuration

The agent should use a configuration file so that monitored projects can be added without changing the Python source code.

Example `config.yaml`:

```yaml
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  chat_id: "${TELEGRAM_CHAT_ID}"

notifications:
  send_files: true
  send_diff: true
  send_build_result: true

watch:
  debounce_seconds: 5

projects:

  - name: TPMS ECU
    path: "C:\\Projects\\tpms_ecu"
    git: true

  - name: ESP Diagnostic Tool
    path: "C:\\Projects\\tpms_tool_esp"
    git: true

  - name: Diagnostic Tool
    path: "C:\\Projects\\diagnostic_tool"
    git: true
```

---

## 10. Recommended Software Architecture

A clean implementation could use the following structure:

```text
tpms-agent/
│
├── agent.py
├── config.yaml
│
├── watcher.py
├── git_monitor.py
├── diff.py
├── telegram.py
├── build_monitor.py
├── logger.py
│
└── requirements.txt
```

### `agent.py`

Main application entry point.

Responsibilities:

- Load configuration.
- Start file watchers.
- Coordinate Git monitoring.
- Process events.
- Trigger Telegram notifications.

### `watcher.py`

Responsible for:

- Monitoring directories.
- Detecting filesystem events.
- Debouncing events.
- Grouping changes.

### `git_monitor.py`

Responsible for:

- Detecting whether a directory is a Git repository.
- Getting the current branch.
- Running `git status`.
- Generating Git diffs.
- Collecting Git statistics.

### `diff.py`

Responsible for:

- Formatting differences.
- Creating `.diff` or `.patch` files.
- Generating a concise change summary.

### `telegram.py`

Responsible for:

- Sending Telegram messages.
- Sending files.
- Optionally receiving commands.

### `build_monitor.py`

Optional module for:

- Running a build command.
- Detecting build success/failure.
- Sending the result to Telegram.

### `logger.py`

Responsible for local logging and troubleshooting.

---

## 11. AI Agent Integration

The system is especially useful when an AI coding agent modifies the project.

A typical workflow:

```text
User
 │
 ▼
AI Coding Agent
 │
 ├── Modify source files
 ├── Modify headers
 ├── Run build
 └── Run tests
       │
       ▼
File Watcher
       │
       ▼
Git Change Analyzer
       │
       ▼
Telegram Notification
       │
       ▼
User's Phone
```

The Telegram message could indicate:

```text
🤖 AI TASK COMPLETED

Project:
TPMS ECU

Branch:
ecu_securt_comm

Modified:
  • CAN_ResponseTask.c
  • ECU_Update.c
  • diagnostic.c

Build:
  ✅ SUCCESS

Git:
  3 files modified
  +84 lines
  -31 lines

Duration:
  04:37
```

This makes it possible to monitor an AI agent while away from the PC.

---

## 12. Optional Telegram Commands

The system can later be extended into a two-way control system.

For example:

```text
/status
```

Response:

```text
🤖 PC Agent

Project: TPMS ECU
Branch: ecu_securt_comm

Modified files: 3
Build: SUCCESS
Last activity: 2 minutes ago
```

Another command:

```text
/diff
```

could return the latest Git diff.

Other possible commands:

```text
/projects
/status
/diff
/build
/log
/stop
/start
```

Commands that control the PC should be protected by an allowlist containing the user's Telegram chat ID.

---

## 13. Security Considerations

Security is important because the Telegram bot provides a communication path to the PC.

### Bot token

The Telegram bot token must be stored securely.

Do not place it directly in source code:

```python
BOT_TOKEN = "123456:ABC..."
```

Instead use an environment variable or a protected local configuration file.

### Telegram Chat ID

Incoming commands should only be accepted from an authorized Telegram chat ID.

For example:

```text
AUTHORIZED_CHAT_ID = <YOUR_CHAT_ID>
```

Any command from another user should be rejected.

### Sensitive source code

Sending source files to Telegram means the source code leaves the local PC and is transferred through Telegram.

Therefore, this system should only be used if sending the relevant project files to Telegram is acceptable under the project's security and company policies.

A safer alternative is to send only:

- File names.
- Git statistics.
- A short summary.
- Build status.

and keep the source files on the PC.

---

## 14. Recommended Notification Levels

The agent can support different notification levels.

### Level 1 — Basic

Send:

```text
File changed:
CAN_ResponseTask.c
```

### Level 2 — Git Summary

Send:

```text
Branch: ecu_securt_comm

Modified:
CAN_ResponseTask.c
ECU_Update.c

+84 / -31 lines
```

### Level 3 — Full Change

Send:

```text
Summary
+
Git diff
+
Modified files
```

### Level 4 — AI Development Monitor

Send:

```text
AI task started
        ↓
Files modified
        ↓
Build started
        ↓
Build result
        ↓
Git diff
        ↓
AI task completed
```

For an AI coding workflow, **Level 4 is the recommended long-term design**.

---

## 15. Windows Background Operation

The agent should eventually run automatically when Windows starts.

Possible approaches include:

- Windows Task Scheduler.
- Windows Service.
- Startup application.
- Packaged `.exe`.

For the first implementation, running it as a normal Python application is simpler.

After the system is stable, it can be packaged into:

```text
tpms-agent.exe
```

and configured to start automatically with Windows.

---

## 16. Example End-to-End Scenario

Suppose the AI agent modifies:

```text
C:\Projects\tpms_ecu\Sources\CAN_ResponseTask.c
```

The sequence is:

```text
1. AI agent modifies the file
          ↓
2. File watcher detects modification
          ↓
3. Watcher waits for debounce period
          ↓
4. Git status is checked
          ↓
5. Current branch is detected
          ↓
6. Git diff is generated
          ↓
7. Change statistics are calculated
          ↓
8. Telegram message is generated
          ↓
9. Telegram API receives HTTPS request
          ↓
10. Telegram delivers notification to phone
```

The phone could receive:

```text
🤖 Project Updated

TPMS ECU
Branch: ecu_securt_comm

Modified:
• CAN_ResponseTask.c

Changes:
+17
-6

Build:
Not executed

📎 changes.patch
📎 CAN_ResponseTask.c
```

---

## 17. Recommended Initial Implementation

The first version should remain simple.

### Phase 1

Implement:

- Python agent.
- Telegram Bot.
- Directory monitoring.
- Debounce mechanism.
- Telegram text notifications.

### Phase 2

Add:

- Git branch detection.
- Git status.
- Git diff.
- Diff attachment.
- Modified file attachment.

### Phase 3

Add:

- Build monitoring.
- Test results.
- AI task detection.
- Better summaries.
- Windows startup.

### Phase 4

Add two-way Telegram commands:

```text
/status
/diff
/build
/log
/start
/stop
```

### Phase 5

Package the application:

```text
tpms-agent.exe
```

and run it automatically in the Windows background.

---

## 18. Final Recommended Architecture

The final system should look like:

```text
                         INTERNET
                            │
                            │ HTTPS
                            ▼
┌──────────────────┐   ┌───────────────┐
│       PC         │   │    Telegram   │
│                  │   │    Servers    │
│ AI Coding Agent │   └───────┬───────┘
│        │         │           │
│        ▼         │           ▼
│ File Watcher     │       📱 Phone
│        │         │
│        ▼         │
│ Git Analyzer     │
│        │         │
│        ▼         │
│ Diff Generator   │
│        │         │
│        ▼         │
│ Telegram Bot     │
└──────────────────┘
```

This architecture is simple, inexpensive, does not require a dedicated server, and works even when the PC and phone are connected to completely different networks.

## 19. Useful References

- Telegram Bot API: https://core.telegram.org/bots/api
- Telegram Bot FAQ: https://core.telegram.org/bots/faq
- Telegram Bot Platform: https://core.telegram.org/bots
- Python `watchdog` documentation: https://python-watchdog.readthedocs.io/

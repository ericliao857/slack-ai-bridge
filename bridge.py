#!/usr/bin/env python3
"""Slack slash-command and Socket Mode bridge for local AI CLI tools.

The bridge accepts configured Slack commands, validates Slack signatures and
local allowlists, and runs configured AI tools in read-only mode for approved
projects.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import hmac
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple


READ_ONLY_UNSUPPORTED = (
    "This bridge runs local AI tools in a read-only sandbox. "
    "Requests that create, edit, delete, install, patch, commit, push, "
    "or run arbitrary shell commands are not supported."
)
OUT_OF_SCOPE_FILE_UNSUPPORTED = "抱歉，我無法協助讀取或討論允許範圍外的檔案。"
MAX_BODY_BYTES = 64 * 1024
SLACK_SIGNATURE_TOLERANCE_SECONDS = 60 * 5
ECHO_COMMAND_PREVIEW_CHARS = 200
PUBLIC_SUMMARY_CHAR_LIMIT = 800
ECHO_COMMAND_MODES = frozenset({"none", "preview", "full"})
OUTPUT_MODES = frozenset({"none", "preview", "full"})
FILE_ACCESS_MODES = frozenset({"project", "all"})
ALL_PROJECT_NAME = "all"
SUPPORTED_TOOL_NAMES = frozenset({"codex", "claude", "copilot"})
WINDOWS_COMMAND_SUFFIXES = (".cmd", ".exe", ".bat")
SAFETY_PROMPT_TEMPLATE_PATH = Path(__file__).resolve().parent / "prompts" / "safety-rules.md"
UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
SLACK_MENTION_PATTERN = re.compile(r"<@[A-Z0-9][A-Z0-9._-]*(?:\|[^>]+)?>")

MUTATING_TERMS = [
    r"\bapply\s+patch\b",
    r"\bcommit\b",
    r"\bdelete\b",
    r"\bedit\b",
    r"\bappend\b",
    r"\bcreate\b",
    r"\binstall\b",
    r"\bmkdir\b",
    r"\bmodify\b",
    r"\bmove\b",
    r"\bpatch\b",
    r"\bpush\b",
    r"\bremove\b",
    r"\brename\b",
    r"\brm\b",
    r"\brmdir\b",
    r"\bsave\b",
    r"\btouch\b",
    r"\bwrite\b",
    r"\boverwrite\b",
    "apply_patch",
]

FILE_ACCESS_INTENT_TERMS = [
    r"\bread\b",
    r"\blist\b",
    r"\bopen\b",
    r"\bshow\b",
    r"\bcat\b",
    r"\bsummarize\b",
    r"\binspect\b",
    r"\bdiscuss\b",
    r"\bconfirm\b",
    r"\bcheck\b",
    "有什麼",
    "有甚麼",
    "有哪些",
    "什麼文件",
    "甚麼文件",
    "哪些文件",
    "檔案",
    "文件",
    "讀取",
    "列出",
    "查看",
    "打開",
    "摘要",
    "確認",
    "討論",
    "檢查",
]
OUT_OF_SCOPE_SCOPE_TERMS = [
    r"\boutside\s+(?:the\s+)?(?:project|repo|repository|workspace)\b",
    r"\boutside\s+(?:the\s+)?allowed\s+scope\b",
    r"\bhome\s+(?:directory|folder)\b",
    r"\bother\s+(?:location|directory|folder)\b",
    "專案外",
    "repo外",
    "工作區外",
    "允許範圍外",
    "其他位置",
    "別的位置",
    "home目錄",
    "home 目錄",
]
IMPLICIT_HOME_FOLDER_TERMS = [
    r"\b(?:user'?s?|my|home)\s*(?:documents|downloads|desktop|pictures|videos|music)\s*(?:folder|directory)?\b",
    r"(?:使用者|我的|個人|home)\s*(?:documents|downloads|desktop|pictures|videos|music|文件|下載|桌面|圖片|影片|音樂)\s*(?:資料夾|目錄)?",
]
PROJECT_SCOPE_TERMS = [
    r"\b(?:project|repo|repository|workspace)\b",
    "專案",
    "repo",
    "工作區",
    "程式碼庫",
]
PROMPT_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'`<>|]+"
    r"|~(?:[\\/][^\s\"'`<>|]+)?"
    r"|%[A-Za-z_][A-Za-z0-9_]*%(?:[\\/][^\s\"'`<>|]+)?"
    r"|\$[A-Za-z_][A-Za-z0-9_]*(?:[\\/][^\s\"'`<>|]+)?"
    r"|(?<!\S)\.\.(?:[\\/][^\s\"'`<>|]+)?"
    r"|(?<![:/])/(?!/)[^\s\"'`<>|]+"
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class ToolConfig:
    name: str
    command: str
    default_model: str
    allowed_models: frozenset[str]


@dataclass(frozen=True)
class BridgeConfig:
    host: str
    port: int
    signing_secret: str
    allowed_users: frozenset[str]
    allowed_channels: frozenset[str]
    bot_token_env: str
    echo_command: str
    projects: Dict[str, ProjectConfig]
    default_project: str
    default_model: str
    allowed_models: frozenset[str]
    codex_command: str
    command_tools: Dict[str, str]
    tools: Dict[str, ToolConfig]
    file_access: str
    timeout_seconds: int
    output_mode: str
    output_char_limit: int
    audit_path: Path
    command_log_path: Path
    conversation_store_path: Path
    command_log_enabled: bool
    skills_enabled: bool


@dataclass(frozen=True)
class ParsedAction:
    command_type: str
    project: str = "-"
    model: str = "-"
    prompt: str = ""
    public: bool = False
    project_explicit: bool = False
    model_explicit: bool = False


@dataclass(frozen=True)
class CodexRunResult:
    status: str
    output: str
    returncode: Optional[int] = None
    cli_args: Tuple[str, ...] = ()
    actual_model: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class ParsedCliOutput:
    output: str
    session_id: str = ""


@dataclass(frozen=True)
class ConversationRecord:
    team_id: str
    channel_id: str
    thread_ts: str
    session_id: str
    tool_name: str
    project: str
    model: str
    project_explicit: bool = False
    model_explicit: bool = False
    updated_at: str = ""


@dataclass(frozen=True)
class CommandResolution:
    command: str
    tried: Tuple[str, ...]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def parse_scalar(value: str):
    value = value.strip()
    lower = value.lower()
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if lower in {"true", "false"}:
        return lower == "true"
    if lower in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        return parse_inline_list(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def parse_inline_list(value: str) -> List[object]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: List[object] = []
    current: List[str] = []
    in_single = False
    in_double = False
    escaped = False
    for char in inner:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and in_double:
            current.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            current.append(char)
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            current.append(char)
            continue
        if char == "," and not in_single and not in_double:
            item = "".join(current).strip()
            if item:
                items.append(parse_scalar(item))
            current = []
            continue
        current.append(char)

    item = "".join(current).strip()
    if item:
        items.append(parse_scalar(item))
    return items


def parse_simple_yaml(text: str) -> Dict[str, object]:
    lines: List[Tuple[int, str]] = []
    for raw_line in text.splitlines():
        no_comment = strip_yaml_comment(raw_line).rstrip()
        if not no_comment.strip():
            continue
        if "\t" in no_comment[: len(no_comment) - len(no_comment.lstrip(" "))]:
            raise ConfigError("Tabs are not supported in config.yaml indentation")
        indent = len(no_comment) - len(no_comment.lstrip(" "))
        lines.append((indent, no_comment.strip()))

    def parse_block(index: int, indent: int) -> Tuple[Dict[str, object], int]:
        output: Dict[str, object] = {}
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ConfigError(f"Unexpected indentation near: {content}")
            if content.startswith("- "):
                raise ConfigError(f"Unexpected list item near: {content}")

            key, sep, rest = content.partition(":")
            if not sep:
                raise ConfigError(f"Expected key: value near: {content}")
            key = str(parse_scalar(key.strip())).strip()
            rest = rest.strip()
            if not key:
                raise ConfigError("Empty YAML key")

            if rest:
                output[key] = parse_scalar(rest)
                index += 1
                continue

            index += 1
            if index >= len(lines) or lines[index][0] <= current_indent:
                output[key] = {}
                continue

            child_indent, child_content = lines[index]
            if child_content.startswith("- "):
                items: List[object] = []
                while (
                    index < len(lines)
                    and lines[index][0] == child_indent
                    and lines[index][1].startswith("- ")
                ):
                    items.append(parse_scalar(lines[index][1][2:].strip()))
                    index += 1
                output[key] = items
            else:
                child, index = parse_block(index, child_indent)
                output[key] = child
        return output, index

    parsed, next_index = parse_block(0, 0)
    if next_index != len(lines):
        raise ConfigError("Could not parse full YAML document")
    return parsed


def require_dict(value: object, name: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a map")
    return value


def require_str_list(value: object, name: str) -> List[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list")
    items = [str(item).strip() for item in value if str(item).strip()]
    if not items:
        raise ConfigError(f"{name} must not be empty")
    return items


def optional_str_list(value: object, name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list")
    return [str(item).strip() for item in value if str(item).strip()]


def command_tool_mapping(value: object, name: str) -> Dict[str, str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if not items:
            raise ConfigError(f"{name} must not be empty")
        output: Dict[str, str] = {}
        for command in items:
            if command in output:
                raise ConfigError(f"Duplicate Slack slash command: {command}")
            output[command] = command[1:]
        return output

    if isinstance(value, dict):
        output = {}
        for raw_command, raw_tool_name in value.items():
            command = str(raw_command).strip()
            tool_name = str(raw_tool_name).strip()
            if not tool_name:
                raise ConfigError(f"{name}.{command} must not be empty")
            output[command] = tool_name
        if not output:
            raise ConfigError(f"{name} must not be empty")
        return output

    raise ConfigError(f"{name} must be a list or map")


def project_name_is_safe(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name))


def slash_command_is_safe(command: str) -> bool:
    return bool(re.fullmatch(r"/[A-Za-z0-9_-]+", command))


def all_project_path() -> Path:
    return Path(os.path.abspath(os.sep))


def user_is_allowed(user_id: str, allowed_users: frozenset[str]) -> bool:
    return not allowed_users or user_id in allowed_users


def config_from_dict(data: Mapping[str, object], config_dir: Path) -> BridgeConfig:
    server = require_dict(data.get("server", {}), "server")
    slack = require_dict(data.get("slack", {}), "slack")
    codex = require_dict(data.get("codex", {}), "codex")
    audit = require_dict(data.get("audit", {}), "audit")
    skills = require_dict(data.get("skills", {}), "skills")

    secret_env = str(slack.get("signing_secret_env", "SLACK_SIGNING_SECRET"))
    signing_secret = os.environ.get(secret_env, "")
    if not signing_secret:
        raise ConfigError(f"Missing Slack signing secret env var: {secret_env}")

    allowed_users = frozenset(
        optional_str_list(slack.get("allowed_users"), "slack.allowed_users")
    )
    allowed_channels = frozenset(
        require_str_list(slack.get("allowed_channels", []), "slack.allowed_channels")
    )
    bot_token_env = str(slack.get("bot_token_env", "SLACK_BOT_TOKEN")).strip()
    if not bot_token_env:
        raise ConfigError("slack.bot_token_env must not be empty")
    echo_command = str(slack.get("echo_command", "none")).strip().lower()
    if echo_command not in ECHO_COMMAND_MODES:
        raise ConfigError("slack.echo_command must be one of: none, preview, full")

    command_tools = command_tool_mapping(
        slack.get("allowed_commands", ["/codex"]),
        "slack.allowed_commands",
    )
    for command, tool_name in command_tools.items():
        if not slash_command_is_safe(command):
            raise ConfigError(f"Invalid Slack slash command: {command}")
        if not project_name_is_safe(tool_name):
            raise ConfigError(f"Invalid tool name for {command}: {tool_name}")

    projects_raw = require_dict(data.get("projects", {}), "projects")
    if not projects_raw:
        raise ConfigError("projects must not be empty")
    projects: Dict[str, ProjectConfig] = {}
    for raw_name, raw_spec in projects_raw.items():
        name = str(raw_name).strip()
        if not name or not project_name_is_safe(name):
            raise ConfigError(f"Invalid project name: {raw_name}")
        spec = require_dict(raw_spec, f"projects.{name}")
        if name == ALL_PROJECT_NAME:
            projects[name] = ProjectConfig(name=name, path=all_project_path())
            continue
        raw_path = str(spec.get("path", "")).strip()
        if not raw_path:
            raise ConfigError(f"projects.{name}.path must not be empty")
        project_path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
        if not project_path.is_absolute():
            raise ConfigError(f"projects.{name}.path must be absolute")
        projects[name] = ProjectConfig(name=name, path=project_path)

    default_project = str(data.get("default_project", "")).strip()
    if default_project:
        if default_project not in projects:
            raise ConfigError("default_project must be present in projects")
    elif len(projects) == 1:
        default_project = next(iter(projects))
    else:
        raise ConfigError("default_project must be set when multiple projects are configured")

    default_command = "codex.cmd" if os.name == "nt" else "codex"
    codex_command = str(codex.get("command", default_command)).strip() or default_command
    tools: Dict[str, ToolConfig] = {}
    if "tools" in data:
        tools_raw = require_dict(data.get("tools", {}), "tools")
        if not tools_raw:
            raise ConfigError("tools must not be empty")
        for raw_name, raw_spec in tools_raw.items():
            name = str(raw_name).strip()
            if name not in SUPPORTED_TOOL_NAMES:
                raise ConfigError(f"Unsupported tool: {raw_name}")
            spec = require_dict(raw_spec, f"tools.{name}")
            command = str(spec.get("command", "")).strip()
            if not command:
                raise ConfigError(f"tools.{name}.command must not be empty")
            default_tool_model = str(spec.get("default_model", "")).strip()
            allowed_tool_models = frozenset(
                optional_str_list(
                    spec.get("allowed_models"),
                    f"tools.{name}.allowed_models",
                )
            )
            if default_tool_model and not allowed_tool_models:
                allowed_tool_models = frozenset({default_tool_model})
            if default_tool_model and default_tool_model not in allowed_tool_models:
                raise ConfigError(
                    f"tools.{name}.default_model must be present in tools.{name}.allowed_models"
                )
            tools[name] = ToolConfig(
                name=name,
                command=command,
                default_model=default_tool_model,
                allowed_models=allowed_tool_models,
            )
        for command, tool_name in command_tools.items():
            if tool_name not in tools:
                raise ConfigError(f"{command} requires matching tools.{tool_name}")
        default_tool = tools[next(iter(command_tools.values()))]
        default_model = default_tool.default_model
        allowed_models = default_tool.allowed_models
        if "codex" in tools:
            codex_command = tools["codex"].command
    else:
        default_model = str(data.get("default_model", "")).strip()
        allowed_models = frozenset(
            require_str_list(data.get("allowed_models", []), "allowed_models")
        )
        if not default_model:
            raise ConfigError("default_model must not be empty")
        if default_model not in allowed_models:
            raise ConfigError("default_model must be present in allowed_models")
        tools["codex"] = ToolConfig(
            name="codex",
            command=codex_command,
            default_model=default_model,
            allowed_models=allowed_models,
        )
        for command, tool_name in command_tools.items():
            if tool_name not in tools:
                raise ConfigError(f"{command} requires matching tools.{tool_name}")

    file_access = str(codex.get("file_access", "project")).strip().lower()
    if file_access not in FILE_ACCESS_MODES:
        raise ConfigError("codex.file_access must be one of: project, all")
    timeout_seconds = int(codex.get("timeout_seconds", 120))
    output_mode = str(codex.get("output_mode", "preview")).strip().lower()
    if output_mode not in OUTPUT_MODES:
        raise ConfigError("codex.output_mode must be one of: none, preview, full")
    output_char_limit = int(codex.get("output_char_limit", 6000))
    if timeout_seconds <= 0:
        raise ConfigError("codex.timeout_seconds must be positive")
    if output_char_limit <= 0:
        raise ConfigError("codex.output_char_limit must be positive")

    raw_audit_path = Path(str(audit.get("path", "logs/audit.csv")))
    audit_path = raw_audit_path if raw_audit_path.is_absolute() else config_dir / raw_audit_path
    raw_command_log_path = Path(str(audit.get("command_log_path", "logs/commands.jsonl")))
    command_log_path = (
        raw_command_log_path
        if raw_command_log_path.is_absolute()
        else config_dir / raw_command_log_path
    )
    raw_conversation_store_path = Path(
        str(slack.get("conversation_store_path", "logs/conversations.json"))
    )
    conversation_store_path = (
        raw_conversation_store_path
        if raw_conversation_store_path.is_absolute()
        else config_dir / raw_conversation_store_path
    )
    command_log_enabled = bool(
        audit.get("log_commands_jsonl", audit.get("log_commands", False))
    )

    return BridgeConfig(
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8799)),
        signing_secret=signing_secret,
        allowed_users=allowed_users,
        allowed_channels=allowed_channels,
        bot_token_env=bot_token_env,
        echo_command=echo_command,
        projects=projects,
        default_project=default_project,
        default_model=default_model,
        allowed_models=allowed_models,
        codex_command=codex_command,
        command_tools=command_tools,
        tools=tools,
        file_access=file_access,
        timeout_seconds=timeout_seconds,
        output_mode=output_mode,
        output_char_limit=output_char_limit,
        audit_path=audit_path,
        command_log_path=command_log_path,
        conversation_store_path=conversation_store_path,
        command_log_enabled=command_log_enabled,
        skills_enabled=bool(skills.get("enabled", False)),
    )


def load_config(path: Path) -> BridgeConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    parsed = parse_simple_yaml(path.read_text(encoding="utf-8"))
    return config_from_dict(parsed, path.resolve().parent)


class AuditLogger:
    fieldnames = [
        "timestamp",
        "user_id",
        "channel_id",
        "project",
        "model",
        "command_type",
        "status",
        "command_id",
    ]

    def __init__(self, path: Path):
        self.path = path
        self.context = threading.local()
        self.lock = threading.Lock()

    def set_command_id(self, command_id: str) -> None:
        self.context.command_id = command_id

    def log(
        self,
        user_id: str,
        channel_id: str,
        project: str,
        model: str,
        command_type: str,
        status: str,
    ) -> None:
        safe_row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user_id": user_id or "-",
            "channel_id": channel_id or "-",
            "project": project or "-",
            "model": model or "-",
            "command_type": command_type or "-",
            "status": status or "-",
            "command_id": getattr(self.context, "command_id", ""),
        }
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.ensure_header()
            needs_header = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
                if needs_header:
                    writer.writeheader()
                writer.writerow(safe_row)

    def ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            existing_fieldnames = reader.fieldnames or []
            if existing_fieldnames == self.fieldnames:
                return
            rows = list(reader)

        migrated_rows = []
        for row in rows:
            migrated_rows.append(
                {field: row.get(field, "") for field in self.fieldnames}
            )

        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(migrated_rows)


class CommandLogger:
    def __init__(self, path: Path, enabled: bool = False):
        self.path = path
        self.enabled = enabled
        self.lock = threading.Lock()

    def log(
        self,
        command_id: str,
        user_id: str,
        channel_id: str,
        command_text: str,
    ) -> None:
        if not self.enabled or not command_id:
            return
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "command_id": command_id,
            "user_id": user_id or "-",
            "channel_id": channel_id or "-",
            "command_text": command_text,
        }
        line = json.dumps(event, ensure_ascii=False)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    @staticmethod
    def key(team_id: str, channel_id: str, thread_ts: str) -> str:
        return "\x1f".join([team_id or "-", channel_id or "-", thread_ts or "-"])

    def get(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
    ) -> Optional[ConversationRecord]:
        with self.lock:
            data = self.read_data()
            raw = data.get("conversations", {}).get(
                self.key(team_id, channel_id, thread_ts)
            )
        if not isinstance(raw, dict):
            return None
        return self.record_from_dict(raw)

    def put(self, record: ConversationRecord) -> None:
        updated = ConversationRecord(
            team_id=record.team_id,
            channel_id=record.channel_id,
            thread_ts=record.thread_ts,
            session_id=record.session_id,
            tool_name=record.tool_name,
            project=record.project,
            model=record.model,
            project_explicit=record.project_explicit,
            model_explicit=record.model_explicit,
            updated_at=record.updated_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self.lock:
            data = self.read_data()
            conversations = data.setdefault("conversations", {})
            if not isinstance(conversations, dict):
                conversations = {}
                data["conversations"] = conversations
            conversations[
                self.key(updated.team_id, updated.channel_id, updated.thread_ts)
            ] = self.record_to_dict(updated)
            self.write_data(data)

    def read_data(self) -> Dict[str, object]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return {"version": 1, "conversations": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "conversations": {}}
        if not isinstance(data, dict):
            return {"version": 1, "conversations": {}}
        if not isinstance(data.get("conversations"), dict):
            data["conversations"] = {}
        data.setdefault("version", 1)
        return data

    def write_data(self, data: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        self.path.write_text(text + "\n", encoding="utf-8")

    def record_from_dict(self, raw: Mapping[str, object]) -> ConversationRecord:
        return ConversationRecord(
            team_id=str(raw.get("team_id", "")),
            channel_id=str(raw.get("channel_id", "")),
            thread_ts=str(raw.get("thread_ts", "")),
            session_id=str(raw.get("session_id", "")),
            tool_name=str(raw.get("tool_name", "codex")),
            project=str(raw.get("project", "")),
            model=str(raw.get("model", "")),
            project_explicit=bool(raw.get("project_explicit", False)),
            model_explicit=bool(raw.get("model_explicit", False)),
            updated_at=str(raw.get("updated_at", "")),
        )

    def record_to_dict(self, record: ConversationRecord) -> Dict[str, object]:
        return {
            "team_id": record.team_id,
            "channel_id": record.channel_id,
            "thread_ts": record.thread_ts,
            "session_id": record.session_id,
            "tool_name": record.tool_name,
            "project": record.project,
            "model": record.model,
            "project_explicit": record.project_explicit,
            "model_explicit": record.model_explicit,
            "updated_at": record.updated_at,
        }


def get_header(headers: Mapping[str, str], name: str) -> str:
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return value.strip()
    return ""


def parse_form_body(body: bytes) -> Dict[str, str]:
    decoded = body.decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(decoded, keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def make_slack_signature(secret: str, body: bytes, timestamp: int) -> str:
    base = b"v0:" + str(timestamp).encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def is_mutating_prompt(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(re.search(term, lowered) for term in MUTATING_TERMS)


def has_file_access_intent(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(re.search(term, lowered) for term in FILE_ACCESS_INTENT_TERMS)


def prompt_path_is_inside_project(path_text: str, project_root: Path) -> bool:
    token = path_text.strip("`'\"").rstrip(".,;!?)]}。！？、，；）】」』")
    uses_expandable_root = token.startswith(("~", "$", "%"))
    expanded = os.path.expandvars(os.path.expanduser(token))
    if uses_expandable_root and expanded == token:
        return False
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", token):
        return False

    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = project_root / candidate

    try:
        project_resolved = project_root.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
        candidate_resolved.relative_to(project_resolved)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def has_out_of_scope_file_target(prompt: str, project_root: Path) -> bool:
    for match in PROMPT_PATH_PATTERN.finditer(prompt):
        if not prompt_path_is_inside_project(match.group(0), project_root):
            return True

    lowered = prompt.lower()
    if any(re.search(term, lowered) for term in OUT_OF_SCOPE_SCOPE_TERMS):
        return True
    if any(re.search(term, lowered) for term in IMPLICIT_HOME_FOLDER_TERMS):
        return not any(re.search(term, lowered) for term in PROJECT_SCOPE_TERMS)
    return False


def is_out_of_scope_file_request(
    prompt: str,
    project_root: Path,
    full_read_access: bool,
) -> bool:
    if full_read_access or not has_file_access_intent(prompt):
        return False
    return has_out_of_scope_file_target(prompt, project_root)


def parse_command_text(text: str, default_project: str, default_model: str) -> ParsedAction:
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped:
        return ParsedAction(command_type="empty")
    if lowered == "help":
        return ParsedAction(command_type="help")
    if lowered == "list":
        return ParsedAction(command_type="list")
    if lowered == "list projects":
        return ParsedAction(command_type="list_projects")
    if lowered == "list models":
        return ParsedAction(command_type="list_models")

    parts = stripped.split()
    params: Dict[str, str] = {}
    public = False
    index = 0
    while index < len(parts):
        token = parts[index]
        lower_token = token.lower()
        if lower_token == "--public":
            public = True
            index += 1
            continue
        if "=" not in token:
            break

        key, value = token.split("=", 1)
        key = key.lower()
        if not value:
            return ParsedAction(command_type="unknown")
        if key in {"project", "model"}:
            params[key] = value
        else:
            return ParsedAction(command_type="unknown")
        index += 1

    return ParsedAction(
        command_type="run",
        project=params.get("project", default_project),
        model=params.get("model", default_model),
        prompt=" ".join(parts[index:]).strip(),
        public=public,
        project_explicit="project" in params,
        model_explicit="model" in params,
    )


def slack_response(
    text: str,
    response_type: str = "ephemeral",
    **extra: object,
) -> Dict[str, object]:
    payload: Dict[str, object] = {"response_type": response_type, "text": text}
    payload.update(extra)
    return payload


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n[truncated]"
    return text[: max(0, limit - len(suffix))] + suffix


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def command_path_module():
    return ntpath if os.name == "nt" else os.path


def command_basename(command: str) -> str:
    return command_path_module().basename(command)


def command_has_extension(command: str) -> bool:
    return bool(command_path_module().splitext(command_basename(command))[1])


def command_candidates(command: str) -> Tuple[str, ...]:
    if os.name != "nt" or command_has_extension(command):
        return (command,)
    return (command,) + tuple(f"{command}{suffix}" for suffix in WINDOWS_COMMAND_SUFFIXES)


def resolve_command(command: str) -> CommandResolution:
    candidates = command_candidates(command)
    tried: List[str] = []
    for candidate in candidates:
        tried.append(candidate)
        found = shutil.which(candidate)
        if not found:
            continue
        if os.name == "nt" and candidate == command and not command_has_extension(command):
            found_name = command_basename(found).lower()
            command_name = command_basename(command).lower()
            if found_name != command_name:
                continue
        return CommandResolution(command=candidate, tried=tuple(tried))
    return CommandResolution(command=command, tried=tuple(tried))


def command_not_found_message(label: str, command: str, tried: Iterable[str]) -> str:
    output = f"{label} command not found: {command}"
    tried_tuple = tuple(tried)
    if tried_tuple and (len(tried_tuple) > 1 or tried_tuple[0] != command):
        output += ". Tried: " + ", ".join(tried_tuple)
    return output


def model_from_cli_args(args: Iterable[str]) -> str:
    args_list = list(args)
    for index, arg in enumerate(args_list[:-1]):
        if arg in {"--model", "-m"}:
            return args_list[index + 1]
    return ""


def looks_like_session_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    match = UUID_PATTERN.search(value)
    return match.group(0) if match else ""


def session_id_from_json(value: object) -> str:
    if isinstance(value, list):
        for item in value:
            found = session_id_from_json(item)
            if found:
                return found
        return ""
    if not isinstance(value, dict):
        return ""

    for key in ("session_id", "sessionId"):
        found = looks_like_session_id(value.get(key))
        if found:
            return found

    type_hint = str(value.get("type", "")).lower()
    if "session" in type_hint:
        for key in ("id", "conversation_id"):
            found = looks_like_session_id(value.get(key))
            if found:
                return found
        payload = value.get("payload")
        if isinstance(payload, dict):
            for key in ("id", "session_id", "sessionId"):
                found = looks_like_session_id(payload.get(key))
                if found:
                    return found

    for key in ("session", "session_meta", "payload"):
        found = session_id_from_json(value.get(key))
        if found:
            return found
    return ""


def extract_codex_session_id(*texts: str) -> str:
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = session_id_from_json(event)
            if found:
                return found
    for text in texts:
        found = looks_like_session_id(text)
        if found:
            return found
    return ""


def parse_json_events(*texts: str) -> List[object]:
    events: List[object] = []
    for text in texts:
        stripped = text.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        else:
            if isinstance(parsed, list):
                events.extend(parsed)
            else:
                events.append(parsed)
            continue

        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def content_value_to_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [content_value_to_text(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content"):
            found = content_value_to_text(value.get(key))
            if found:
                return found
    return ""


def parse_claude_cli_output(stdout: str, stderr: str) -> ParsedCliOutput:
    session_id = extract_codex_session_id(stdout, stderr)
    for event in reversed(parse_json_events(stdout)):
        if not isinstance(event, dict):
            continue
        for key in ("result", "output", "content"):
            output = content_value_to_text(event.get(key))
            if output:
                return ParsedCliOutput(output=output, session_id=session_id)
        message = event.get("message")
        if isinstance(message, dict):
            output = content_value_to_text(message.get("content"))
            if output:
                return ParsedCliOutput(output=output, session_id=session_id)
    combined = "\n".join(part for part in [stdout, stderr] if part)
    return ParsedCliOutput(output=combined, session_id=session_id)


def parse_copilot_cli_output(stdout: str, stderr: str) -> ParsedCliOutput:
    session_id = extract_codex_session_id(stdout, stderr)
    parts: List[str] = []
    for event in parse_json_events(stdout):
        if not isinstance(event, dict):
            continue
        if str(event.get("type", "")) != "assistant.message":
            continue
        data = event.get("data")
        if isinstance(data, dict):
            output = content_value_to_text(data.get("content"))
            if not output:
                message = data.get("message")
                if isinstance(message, dict):
                    output = content_value_to_text(message.get("content"))
            if output:
                parts.append(output)
    output = "\n".join(part.strip() for part in parts if part.strip())
    if output:
        return ParsedCliOutput(output=output, session_id=session_id)
    combined = "\n".join(part for part in [stdout, stderr] if part)
    return ParsedCliOutput(output=combined, session_id=session_id)


def clean_slack_event_text(text: str) -> str:
    without_mentions = SLACK_MENTION_PATTERN.sub("", text)
    return re.sub(r"[ \t]+", " ", without_mentions).strip()


def load_safety_prompt_template() -> str:
    return SAFETY_PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8").strip()


def compose_cli_prompt(template: str, user_task: str, access_mode: str) -> str:
    return (
        template.replace("{{ACCESS_MODE}}", access_mode)
        .replace("{{USER_TASK}}", user_task.strip())
        .strip()
    )


class BridgeApp:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.safety_prompt_template = load_safety_prompt_template()
        self.audit = AuditLogger(config.audit_path)
        self.commands = CommandLogger(
            config.command_log_path,
            config.command_log_enabled,
        )
        self.conversations = ConversationStore(config.conversation_store_path)
        self.conversation_locks: Dict[str, threading.Lock] = {}
        self.conversation_locks_guard = threading.Lock()

    def default_tool(self) -> ToolConfig:
        tool_name = next(iter(self.config.command_tools.values()))
        return self.config.tools[tool_name]

    def full_read_access_enabled(self, project: ProjectConfig) -> bool:
        return self.config.file_access == "all" or project.name == ALL_PROJECT_NAME

    def cli_prompt(self, project: ProjectConfig, prompt: str) -> str:
        access_mode = "ALL" if self.full_read_access_enabled(project) else "PROJECT_ONLY"
        return compose_cli_prompt(self.safety_prompt_template, prompt, access_mode)

    def tool_for_command(self, command: str) -> Optional[ToolConfig]:
        tool_name = self.config.command_tools.get(command)
        if tool_name is None:
            return None
        return self.config.tools.get(tool_name)

    def slash_command_for_tool(self, tool_name: str) -> str:
        for command, configured_tool_name in self.config.command_tools.items():
            if configured_tool_name == tool_name:
                return command
        return next(iter(self.config.command_tools))

    def parse_event_tool_prefix(self, prompt: str) -> Tuple[ToolConfig, str, str]:
        stripped = prompt.strip()
        default_tool = self.default_tool()
        default_command = self.slash_command_for_tool(default_tool.name)
        if not stripped:
            return default_tool, stripped, default_command

        first, separator, rest = stripped.partition(" ")
        command_lookup = {
            command.lower(): command for command in self.config.command_tools
        }
        command = command_lookup.get(first.lower())
        if command:
            tool = self.tool_for_command(command) or default_tool
            return tool, rest.strip() if separator else "", command

        tool_lookup = {
            tool_name.lower(): tool_name
            for tool_name in self.config.command_tools.values()
        }
        tool_name = tool_lookup.get(first.lower())
        if tool_name:
            return (
                self.config.tools[tool_name],
                rest.strip() if separator else "",
                self.slash_command_for_tool(tool_name),
            )

        return default_tool, stripped, default_command

    def verify_slack_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        timestamp_raw = get_header(headers, "X-Slack-Request-Timestamp")
        signature = get_header(headers, "X-Slack-Signature")
        if not timestamp_raw or not signature:
            return False
        try:
            timestamp = int(timestamp_raw)
        except ValueError:
            return False
        if abs(time.time() - timestamp) > SLACK_SIGNATURE_TOLERANCE_SECONDS:
            return False
        expected = make_slack_signature(self.config.signing_secret, body, timestamp)
        return hmac.compare_digest(expected, signature)

    def handle_request(
        self, body: bytes, headers: Mapping[str, str]
    ) -> Tuple[int, Dict[str, object]]:
        form = parse_form_body(body)
        user_id = form.get("user_id", "")
        channel_id = form.get("channel_id", "")

        if not self.verify_slack_signature(body, headers):
            self.audit.log(user_id, channel_id, "-", "-", "unknown", "invalid_signature")
            return 401, slack_response("Unauthorized.")

        return self.handle_form(form)

    def handle_socket_command(
        self, payload: Mapping[str, object]
    ) -> Tuple[int, Dict[str, object]]:
        form = {
            str(key): "" if value is None else str(value)
            for key, value in payload.items()
        }
        return self.handle_form(form)

    def handle_form(self, form: Mapping[str, str]) -> Tuple[int, Dict[str, object]]:
        user_id = form.get("user_id", "")
        channel_id = form.get("channel_id", "")
        slash_command = form.get("command", "")
        tool = self.tool_for_command(slash_command)

        if tool is None:
            self.audit.log(user_id, channel_id, "-", "-", "unknown", "invalid_command")
            return 400, slack_response("Unsupported command.")

        command_text = form.get("text", "")
        command_id = uuid.uuid4().hex if self.config.command_log_enabled else ""
        self.audit.set_command_id(command_id)

        if not user_is_allowed(user_id, self.config.allowed_users):
            self.audit.log(user_id, channel_id, "-", "-", "unknown", "denied_user")
            return 200, slack_response("User is not allowed to use this bridge.")

        if channel_id not in self.config.allowed_channels:
            self.audit.log(user_id, channel_id, "-", "-", "unknown", "denied_channel")
            return 200, slack_response("Channel is not allowed to use this bridge.")

        self.commands.log(command_id, user_id, channel_id, command_text)

        action = parse_command_text(
            form.get("text", ""),
            self.config.default_project,
            tool.default_model,
        )
        return self.handle_action(
            action,
            form,
            user_id,
            channel_id,
            tool,
            slash_command,
            command_id,
        )

    def handle_action(
        self,
        action: ParsedAction,
        form: Mapping[str, str],
        user_id: str,
        channel_id: str,
        tool: ToolConfig,
        slash_command: str,
        command_id: str = "",
    ) -> Tuple[int, Dict[str, object]]:
        if action.command_type == "help":
            self.audit.log(user_id, channel_id, "-", "-", "help", "ok")
            return 200, slack_response(self.format_help(slash_command))

        if action.command_type == "list":
            self.audit.log(user_id, channel_id, "-", "-", "list", "ok")
            return 200, slack_response(self.format_list(tool))

        if action.command_type == "list_projects":
            self.audit.log(user_id, channel_id, "-", "-", "list_projects", "ok")
            return 200, slack_response(self.format_projects())

        if action.command_type == "list_models":
            self.audit.log(user_id, channel_id, "-", "-", "list_models", "ok")
            return 200, slack_response(self.format_models(tool))

        if action.command_type == "empty":
            self.audit.log(user_id, channel_id, "-", "-", "empty", "empty_prompt")
            return 200, slack_response(f"Prompt cannot be empty. Try {slash_command} help.")

        if action.command_type != "run":
            self.audit.log(user_id, channel_id, "-", "-", "unknown", "unsupported_command")
            return 200, slack_response(f"Unsupported command. Try {slash_command} help.")

        project = self.config.projects.get(action.project)
        if project is None:
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "run",
                "invalid_project",
            )
            return 200, slack_response("Project is not allowed.")

        if action.model and action.model not in tool.allowed_models:
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "run",
                "invalid_model",
            )
            return 200, slack_response("Model is not allowed.")

        if not action.prompt.strip():
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "run",
                "empty_prompt",
            )
            return 200, slack_response("Prompt cannot be empty.")

        if is_out_of_scope_file_request(
            action.prompt,
            project.path,
            self.full_read_access_enabled(project),
        ):
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "run",
                "blocked_outside_scope_file",
            )
            return 200, slack_response(OUT_OF_SCOPE_FILE_UNSUPPORTED)

        if is_mutating_prompt(action.prompt):
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "run",
                "blocked_read_only_intent",
            )
            return 200, slack_response(READ_ONLY_UNSUPPORTED)

        response_url = form.get("response_url", "").strip()
        if response_url:
            worker = threading.Thread(
                target=self.run_and_post,
                args=(response_url, project, action, user_id, channel_id, command_id),
                kwargs={"tool": tool},
                daemon=True,
            )
            worker.start()
            return 200, slack_response(
                self.format_running(project, action, tool),
            )

        text, status = self.execute_and_format(project, action, tool)
        self.audit.log(user_id, channel_id, action.project, action.model, "run", status)
        return 200, slack_response(text)

    def conversation_lock(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
    ) -> threading.Lock:
        key = ConversationStore.key(team_id, channel_id, thread_ts)
        with self.conversation_locks_guard:
            lock = self.conversation_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self.conversation_locks[key] = lock
            return lock

    def handle_event(self, payload: Mapping[str, object], web_client: object) -> str:
        raw_event = payload.get("event")
        if not isinstance(raw_event, Mapping):
            return "event_ignored"

        event_type = str(raw_event.get("type", ""))
        if event_type not in {"app_mention", "message"}:
            return "event_ignored"
        if raw_event.get("bot_id") or raw_event.get("subtype"):
            return "event_ignored"

        user_id = str(raw_event.get("user", ""))
        channel_id = str(raw_event.get("channel", ""))
        team_id = str(payload.get("team_id") or raw_event.get("team") or "-")
        thread_ts = str(raw_event.get("thread_ts") or raw_event.get("ts") or "")
        if not user_id or not channel_id or not thread_ts:
            return "event_ignored"

        if not user_is_allowed(user_id, self.config.allowed_users):
            self.audit.log(user_id, channel_id, "-", "-", "event", "denied_user")
            return "event_denied_user"
        if channel_id not in self.config.allowed_channels:
            self.audit.log(user_id, channel_id, "-", "-", "event", "denied_channel")
            return "event_denied_channel"

        prompt = clean_slack_event_text(str(raw_event.get("text", "")))
        lock = self.conversation_lock(team_id, channel_id, thread_ts)
        with lock:
            record = self.conversations.get(team_id, channel_id, thread_ts)
            if event_type == "message" and record is None:
                return "event_ignored"
            return self.handle_conversation_event(
                team_id,
                channel_id,
                thread_ts,
                user_id,
                prompt,
                record,
                web_client,
            )

    def handle_conversation_event(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
        prompt: str,
        record: Optional[ConversationRecord],
        web_client: object,
    ) -> str:
        if record is not None:
            tool = self.config.tools.get(record.tool_name, self.default_tool())
            slash_command = self.slash_command_for_tool(tool.name)
            prefixed_tool, stripped_prompt, _ = self.parse_event_tool_prefix(prompt)
            if prefixed_tool.name == tool.name and stripped_prompt != prompt.strip():
                prompt = stripped_prompt
        else:
            tool, prompt, slash_command = self.parse_event_tool_prefix(prompt)

        if not prompt:
            self.post_thread_message(web_client, channel_id, thread_ts, "Prompt cannot be empty.")
            self.audit.log(user_id, channel_id, "-", "-", "event", "empty_prompt")
            return "event_empty_prompt"

        if record is None:
            action = parse_command_text(
                prompt,
                self.config.default_project,
                tool.default_model,
            )
            if action.command_type == "help":
                posted = self.post_thread_message(
                    web_client,
                    channel_id,
                    thread_ts,
                    self.format_mention_help(tool),
                )
                self.audit.log(user_id, channel_id, "-", "-", "event", "help")
                return "event_ok" if posted else "event_post_failed"
            if action.command_type == "list":
                posted = self.post_thread_message(
                    web_client,
                    channel_id,
                    thread_ts,
                    self.format_list(tool),
                )
                self.audit.log(user_id, channel_id, "-", "-", "event", "list")
                return "event_ok" if posted else "event_post_failed"
            if action.command_type == "list_projects":
                posted = self.post_thread_message(
                    web_client,
                    channel_id,
                    thread_ts,
                    self.format_projects(),
                )
                self.audit.log(user_id, channel_id, "-", "-", "event", "list_projects")
                return "event_ok" if posted else "event_post_failed"
            if action.command_type == "list_models":
                posted = self.post_thread_message(
                    web_client,
                    channel_id,
                    thread_ts,
                    self.format_models(tool),
                )
                self.audit.log(user_id, channel_id, "-", "-", "event", "list_models")
                return "event_ok" if posted else "event_post_failed"
            session_id = ""
        else:
            action = ParsedAction(
                command_type="run",
                project=record.project,
                model=record.model,
                prompt=prompt,
                project_explicit=record.project_explicit,
                model_explicit=record.model_explicit,
            )
            session_id = record.session_id

        if action.command_type != "run":
            posted = self.post_thread_message(
                web_client,
                channel_id,
                thread_ts,
                "Unsupported command in thread. Ask a question or mention help.",
            )
            self.audit.log(user_id, channel_id, "-", "-", "event", "unsupported_command")
            return "event_ok" if posted else "event_post_failed"

        project = self.config.projects.get(action.project)
        if project is None:
            posted = self.post_thread_message(
                web_client,
                channel_id,
                thread_ts,
                "Project is not allowed.",
            )
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "event",
                "invalid_project",
            )
            return "event_ok" if posted else "event_post_failed"

        if action.model and action.model not in tool.allowed_models:
            posted = self.post_thread_message(
                web_client,
                channel_id,
                thread_ts,
                "Model is not allowed.",
            )
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "event",
                "invalid_model",
            )
            return "event_ok" if posted else "event_post_failed"

        if is_out_of_scope_file_request(
            action.prompt,
            project.path,
            self.full_read_access_enabled(project),
        ):
            posted = self.post_thread_message(
                web_client,
                channel_id,
                thread_ts,
                OUT_OF_SCOPE_FILE_UNSUPPORTED,
            )
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "event",
                "blocked_outside_scope_file",
            )
            return "event_ok" if posted else "event_post_failed"

        if is_mutating_prompt(action.prompt):
            posted = self.post_thread_message(
                web_client,
                channel_id,
                thread_ts,
                READ_ONLY_UNSUPPORTED,
            )
            self.audit.log(
                user_id,
                channel_id,
                action.project,
                action.model,
                "event",
                "blocked_read_only_intent",
            )
            return "event_ok" if posted else "event_post_failed"

        self.post_thread_message(
            web_client,
            channel_id,
            thread_ts,
            self.format_thread_running(action, tool),
        )
        result = self.run_tool(
            tool,
            project,
            action.model,
            action.prompt,
            session_id=session_id,
        )
        if result.session_id or session_id:
            self.conversations.put(
                ConversationRecord(
                    team_id=team_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    session_id=result.session_id or session_id,
                    tool_name=tool.name,
                    project=action.project,
                    model=action.model,
                    project_explicit=action.project_explicit,
                    model_explicit=action.model_explicit,
                )
            )

        text = self.format_result(project, action, tool, result)
        posted = self.post_thread_message(web_client, channel_id, thread_ts, text)
        final_status = result.status if posted else f"{result.status}_post_failed"
        self.audit.log(user_id, channel_id, action.project, action.model, "event", final_status)
        return "event_ok" if posted else "event_post_failed"

    def post_thread_message(
        self,
        web_client: object,
        channel_id: str,
        thread_ts: str,
        text: str,
    ) -> bool:
        if web_client is None:
            return False
        try:
            response = web_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception:
            return False
        if hasattr(response, "get"):
            return bool(response.get("ok", True))
        return True

    def format_projects(self) -> str:
        names = sorted(self.config.projects)
        lines = [
            f"Default project: {self.config.default_project}",
            "Allowed projects:",
        ] + [f"- {name}" for name in names]
        return "\n".join(lines)

    def format_help(self, slash_command: str) -> str:
        command_lines = [
            f"- {slash_command} help",
            f"- {slash_command} list",
            f"- {slash_command} list projects",
            f"- {slash_command} list models",
            f"- {slash_command} project=<project> model=<model> <prompt>",
            f"- {slash_command} model=<model> <prompt>",
            f"- {slash_command} --public <prompt>",
            f"- {slash_command} <prompt>",
        ]
        return "\n".join(
            [
                "Slack Slash Command usage (read-only)",
                "",
                "Supported commands:",
                *command_lines,
                "",
                "Notes:",
                "- Project and model must be configured in config.yaml. Omitted project/model use defaults.",
                "- Responses are private by default. Prefix a run with --public to post a short in-channel summary after the private result is delivered.",
                "- Skills are reserved for later and are not implemented.",
                "- File writes, patches, commits, pushes, deletes, installs, and arbitrary shell commands are not supported.",
            ]
        )

    def format_mention_help(self, tool: Optional[ToolConfig] = None) -> str:
        tool = tool or self.default_tool()
        seen_tool_names: set[str] = set()
        tool_lines: List[str] = []
        for tool_name in self.config.command_tools.values():
            if tool_name in seen_tool_names:
                continue
            seen_tool_names.add(tool_name)
            tool_lines.extend(
                [
                    f"- @bot {tool_name} project=<project> model=<model> <prompt>",
                    f"- @bot {tool_name} model=<model> <prompt>",
                    f"- @bot {tool_name} <prompt>",
                ]
            )

        return "\n".join(
            [
                "Slack bot mention usage (read-only)",
                "",
                "Start a new session thread:",
                f"- @bot <prompt>  (uses default tool: {self.default_tool().name})",
                *tool_lines,
                "",
                "Inspect configuration:",
                "- @bot help",
                "- @bot list",
                "- @bot list projects",
                "- @bot list models",
                "- @bot <tool> list models",
                "",
                "Continue context:",
                "- Reply in the same Slack thread to continue the stored tool session.",
                "- Repeating the same tool prefix in a reply is OK, for example: @bot claude follow up.",
                "- To switch tools, start a new bot-mention thread.",
                "",
                "Notes:",
                f"- Current selected tool: {tool.name}.",
                "- Project and model must be configured in config.yaml. Omitted project/model use defaults.",
                "- File writes, patches, commits, pushes, deletes, installs, and arbitrary shell commands are not supported.",
            ]
        )

    def format_models(self, tool: Optional[ToolConfig] = None) -> str:
        tool = tool or self.default_tool()
        lines = [
            f"Tool: {tool.name}",
            f"Default model: {self.format_model_name(tool.default_model)}",
            "Allowed models:",
        ]
        if tool.allowed_models:
            lines.extend(f"- {model}" for model in sorted(tool.allowed_models))
        else:
            lines.append("- (none configured; CLI default only)")
        return "\n".join(lines)

    def format_list(self, tool: Optional[ToolConfig] = None) -> str:
        return "\n\n".join(
            [
                self.format_projects(),
                self.format_tools(),
                self.format_models(tool),
            ]
        )

    def format_tools(self) -> str:
        lines = ["Allowed tools:"]
        for command, tool_name in self.config.command_tools.items():
            lines.append(f"- {command} -> {tool_name}")
        return "\n".join(lines)

    def format_running(
        self,
        project: ProjectConfig,
        action: ParsedAction,
        tool: Optional[ToolConfig] = None,
    ) -> str:
        tool = tool or self.default_tool()
        return (
            f"tool: {tool.name}\n"
            f"project: {self.format_project_display(action)}\n"
            f"model: {self.format_model_display(action)}\n"
            f"status: running\n"
            f"visibility: {'public summary' if action.public else 'private'}"
            f"{self.format_command_echo(action.prompt)}"
        )

    def format_thread_running(
        self,
        action: ParsedAction,
        tool: Optional[ToolConfig] = None,
    ) -> str:
        tool = tool or self.default_tool()
        return (
            f"Received. Running {tool.name.title()}.\n\n"
            f"tool: {tool.name}\n"
            f"project: {self.format_project_display(action)}\n"
            f"model: {self.format_model_display(action)}\n"
            "status: running"
        )

    def format_command_echo(self, command_text: str) -> str:
        if self.config.echo_command == "none" or not command_text:
            return ""
        if self.config.echo_command == "preview":
            command_text = truncate_text(command_text, ECHO_COMMAND_PREVIEW_CHARS)
        command_text = command_text.replace("```", "'''")
        return f"\ncommand:\n```{command_text}```"

    def execute_and_format(
        self,
        project: ProjectConfig,
        action: ParsedAction,
        tool: Optional[ToolConfig] = None,
    ) -> Tuple[str, str]:
        tool = tool or self.default_tool()
        result = self.run_tool(tool, project, action.model, action.prompt)
        return self.format_result(project, action, tool, result), result.status

    def format_result(
        self,
        project: ProjectConfig,
        action: ParsedAction,
        tool: ToolConfig,
        result: CodexRunResult,
    ) -> str:
        prefix = (
            f"tool: {tool.name}\n"
            f"project: {self.format_project_display(action)}\n"
            f"model: {self.format_model_display(action, result)}\n"
            f"status: {result.status}"
        )
        if result.returncode not in {None, 0}:
            prefix += f"\nreturncode: {result.returncode}"
        if self.config.output_mode == "none":
            return prefix
        output = result.output.strip() or "(no output)"
        if self.config.output_mode == "preview":
            output = truncate_text(output, self.config.output_char_limit)
        output = output.replace("```", "'''")
        return f"{prefix}\n\n```\n{output}\n```"

    def format_public_summary(
        self,
        project: ProjectConfig,
        action: ParsedAction,
        tool: ToolConfig,
        result: CodexRunResult,
    ) -> str:
        lines = [
            f"tool: {tool.name}",
            f"project: {self.format_project_display(action)}",
            f"model: {self.format_model_display(action, result)}",
            f"status: {result.status}",
        ]
        if self.config.output_mode == "none":
            lines.extend(["", "Output display is disabled by config."])
        elif result.status.endswith("_ok"):
            output = result.output.strip() or "(no output)"
            output = truncate_text(output, PUBLIC_SUMMARY_CHAR_LIMIT)
            output = output.replace("```", "'''")
            lines.extend(["", "summary:", output])
        else:
            lines.extend(
                [
                    "",
                    "Run finished, but detailed output was kept private.",
                ]
            )
        lines.extend(["", "Full details are visible privately to the requester."])
        return "\n".join(lines)

    def format_model_name(self, model: str) -> str:
        return model or "CLI default"

    def format_project_display(self, action: ParsedAction) -> str:
        return action.project if action.project_explicit else "default"

    def format_model_display(
        self,
        action: ParsedAction,
        result: Optional[CodexRunResult] = None,
    ) -> str:
        if not action.model_explicit:
            return "default"
        if result and result.actual_model:
            return result.actual_model
        return action.model

    def run_tool(
        self,
        tool: ToolConfig,
        project: ProjectConfig,
        model: str,
        prompt: str,
        *,
        session_id: str = "",
    ) -> CodexRunResult:
        if tool.name == "codex":
            return self.run_codex(project, model, prompt, session_id=session_id)
        if tool.name == "claude":
            return self.run_claude(
                tool,
                project,
                model,
                prompt,
                session_id=session_id,
            )
        if tool.name == "copilot":
            return self.run_copilot(
                tool,
                project,
                model,
                prompt,
                session_id=session_id,
            )
        return CodexRunResult(
            status="tool_error",
            output=f"Unsupported tool: {tool.name}",
        )

    def run_codex(
        self,
        project: ProjectConfig,
        model: str,
        prompt: str,
        *,
        session_id: str = "",
    ) -> CodexRunResult:
        if not project.path.exists() or not project.path.is_dir():
            return CodexRunResult(
                status="project_path_missing",
                output="Configured project path does not exist or is not a directory.",
            )

        full_prompt = self.cli_prompt(project, prompt)
        output_dir = self.config.audit_path.parent / "tmp"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"codex-last-message-{uuid.uuid4().hex}.txt"
        command_resolution = resolve_command(self.config.codex_command)
        args = [
            command_resolution.command,
            "exec",
        ]
        if model:
            args.extend(["--ignore-user-config", "--model", model])
        if self.full_read_access_enabled(project):
            args.extend(["-c", 'sandbox_permissions=["disk-full-read-access"]'])
        args.extend(
            [
                "--sandbox",
                "read-only",
                "--cd",
                str(project.path),
                "--color",
                "never",
            ]
        )
        if session_id:
            args.extend(
                [
                    "resume",
                    "--json",
                    "--output-last-message",
                    str(output_path),
                    session_id,
                    full_prompt,
                ]
            )
        else:
            args.extend(
                [
                    "--json",
                    "--output-last-message",
                    str(output_path),
                    "--",
                    full_prompt,
                ]
            )
        cli_args = tuple(args)
        actual_model = model_from_cli_args(cli_args)

        try:
            completed = subprocess.run(
                args,
                cwd=str(project.path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
                shell=False,
            )
        except FileNotFoundError:
            safe_unlink(output_path)
            return CodexRunResult(
                status="codex_error",
                output=command_not_found_message(
                    "Codex",
                    self.config.codex_command,
                    command_resolution.tried,
                ),
                cli_args=cli_args,
                actual_model=actual_model,
            )
        except subprocess.TimeoutExpired as exc:
            final_message = self.read_last_message(output_path)
            safe_unlink(output_path)
            partial = "\n".join(
                part
                for part in [
                    exc.stdout if isinstance(exc.stdout, str) else "",
                    exc.stderr if isinstance(exc.stderr, str) else "",
                ]
                if part
            )
            output = (
                final_message
                or partial
                or f"Codex timed out after {self.config.timeout_seconds} seconds."
            )
            return CodexRunResult(
                status="codex_timeout",
                output=strip_ansi(output),
                cli_args=cli_args,
                actual_model=actual_model,
            )

        final_message = self.read_last_message(output_path)
        safe_unlink(output_path)
        combined = "\n".join(
            part for part in [completed.stdout, completed.stderr] if part
        )
        status = "codex_ok" if completed.returncode == 0 else "codex_error"
        actual_session_id = extract_codex_session_id(completed.stdout, completed.stderr)
        return CodexRunResult(
            status=status,
            output=strip_ansi(final_message or combined),
            returncode=completed.returncode,
            cli_args=cli_args,
            actual_model=actual_model,
            session_id=actual_session_id or session_id,
        )

    def run_claude(
        self,
        tool: ToolConfig,
        project: ProjectConfig,
        model: str,
        prompt: str,
        *,
        session_id: str = "",
    ) -> CodexRunResult:
        args = [
            tool.command,
            "--print",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--tools=Read,Grep,Glob,LS",
            "--allowedTools=Read(/**),Grep(/**),Glob(/**),LS(/**)",
            "--disallowedTools=Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite",
        ]
        if model:
            args[1:1] = ["--model", model]
        if session_id:
            args.extend(["--resume", session_id])
        args.append(self.cli_prompt(project, prompt))
        return self.run_plain_cli_tool(
            tool,
            project,
            args,
            output_parser=parse_claude_cli_output,
            session_id=session_id,
        )

    def run_copilot(
        self,
        tool: ToolConfig,
        project: ProjectConfig,
        model: str,
        prompt: str,
        *,
        session_id: str = "",
    ) -> CodexRunResult:
        args = [
            tool.command,
            "--no-color",
            "--output-format",
            "json",
            "--no-custom-instructions",
            "--excluded-tools=skill",
            "--available-tools=view,glob,grep",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
            "--no-ask-user",
            "--no-remote",
            "--prompt",
            self.cli_prompt(project, prompt),
        ]
        if self.full_read_access_enabled(project):
            prompt_index = args.index("--prompt")
            args[prompt_index:prompt_index] = ["--allow-all-paths"]
        if model:
            args[1:1] = ["--model", model]
        if session_id:
            prompt_index = args.index("--prompt")
            args[prompt_index:prompt_index] = [f"--resume={session_id}"]
        return self.run_plain_cli_tool(
            tool,
            project,
            args,
            output_parser=parse_copilot_cli_output,
            session_id=session_id,
        )

    def run_plain_cli_tool(
        self,
        tool: ToolConfig,
        project: ProjectConfig,
        args: List[str],
        *,
        output_parser: Optional[Callable[[str, str], ParsedCliOutput]] = None,
        session_id: str = "",
    ) -> CodexRunResult:
        if not project.path.exists() or not project.path.is_dir():
            return CodexRunResult(
                status="project_path_missing",
                output="Configured project path does not exist or is not a directory.",
            )

        command_resolution = resolve_command(args[0])
        args = [command_resolution.command] + args[1:]
        cli_args = tuple(args)
        actual_model = model_from_cli_args(cli_args)
        try:
            completed = subprocess.run(
                args,
                cwd=str(project.path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
                shell=False,
            )
        except FileNotFoundError:
            return CodexRunResult(
                status=f"{tool.name}_error",
                output=command_not_found_message(
                    tool.name,
                    tool.command,
                    command_resolution.tried,
                ),
                cli_args=cli_args,
                actual_model=actual_model,
            )
        except subprocess.TimeoutExpired as exc:
            partial = "\n".join(
                part
                for part in [
                    exc.stdout if isinstance(exc.stdout, str) else "",
                    exc.stderr if isinstance(exc.stderr, str) else "",
                ]
                if part
            )
            output = (
                partial
                or f"{tool.name} timed out after {self.config.timeout_seconds} seconds."
            )
            return CodexRunResult(
                status=f"{tool.name}_timeout",
                output=strip_ansi(output),
                cli_args=cli_args,
                actual_model=actual_model,
            )

        combined = "\n".join(
            part for part in [completed.stdout, completed.stderr] if part
        )
        status = (
            f"{tool.name}_ok"
            if completed.returncode == 0
            else f"{tool.name}_error"
        )
        parsed_output = ParsedCliOutput(output=combined, session_id=session_id)
        if output_parser is not None:
            parsed_output = output_parser(completed.stdout, completed.stderr)
        return CodexRunResult(
            status=status,
            output=strip_ansi(parsed_output.output or combined),
            returncode=completed.returncode,
            cli_args=cli_args,
            actual_model=actual_model,
            session_id=parsed_output.session_id or session_id,
        )

    def read_last_message(self, output_path: Path) -> str:
        if not output_path.exists():
            return ""
        return output_path.read_text(encoding="utf-8", errors="replace").strip()

    def run_and_post(
        self,
        response_url: str,
        project: ProjectConfig,
        action: ParsedAction,
        user_id: str,
        channel_id: str,
        command_id: str = "",
        tool: Optional[ToolConfig] = None,
    ) -> None:
        self.audit.set_command_id(command_id)
        tool = tool or self.default_tool()
        result = self.run_tool(tool, project, action.model, action.prompt)
        text = self.format_result(project, action, tool, result)
        status = result.status
        posted = post_to_response_url(response_url, text, replace_original=True)
        final_status = status if posted else f"{status}_response_url_failed"
        if action.public and posted:
            public_text = self.format_public_summary(project, action, tool, result)
            public_posted = post_to_response_url(
                response_url,
                public_text,
                response_type="in_channel",
            )
            if public_posted:
                final_status = f"{final_status}_public_ok"
            else:
                final_status = f"{final_status}_public_response_url_failed"
        elif action.public:
            final_status = f"{final_status}_public_skipped"
        self.audit.log(user_id, channel_id, action.project, action.model, "run", final_status)


def post_to_response_url(
    response_url: str,
    text: str,
    *,
    replace_original: bool = False,
    response_type: str = "ephemeral",
) -> bool:
    parsed = urllib.parse.urlparse(response_url)
    if parsed.scheme != "https":
        return False
    payload = json.dumps(
        slack_response(text, response_type=response_type, replace_original=replace_original)
    ).encode("utf-8")
    request = urllib.request.Request(
        response_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError):
        return False


def handle_socket_mode_request(
    app: BridgeApp,
    client: object,
    request: object,
    response_class: object,
    web_client: object = None,
) -> None:
    envelope_id = getattr(request, "envelope_id", "")
    if not envelope_id:
        return

    request_type = getattr(request, "type", "")
    payload = None
    if request_type == "slash_commands":
        payload = app.handle_socket_command(getattr(request, "payload", {}))[1]
    elif request_type == "events_api":
        event_payload = getattr(request, "payload", {})
        response = response_class(envelope_id=envelope_id, payload=None)
        client.send_socket_mode_response(response)
        if isinstance(event_payload, Mapping):
            worker = threading.Thread(
                target=app.handle_event,
                args=(event_payload, web_client),
                daemon=True,
            )
            worker.start()
        return

    response = response_class(envelope_id=envelope_id, payload=payload)
    client.send_socket_mode_response(response)


def slack_api_error_code(exc: BaseException) -> Optional[str]:
    response = getattr(exc, "response", None)
    data = getattr(response, "data", None)
    if hasattr(data, "get"):
        error = data.get("error")
        if isinstance(error, str):
            return error
    if hasattr(response, "get"):
        error = response.get("error")
        if isinstance(error, str):
            return error
    try:
        error = response["error"]  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        error = None
    if isinstance(error, str):
        return error
    message = str(exc)
    for pattern in (r"'error':\s*'([^']+)'", r'"error":\s*"([^"]+)"'):
        match = re.search(pattern, message)
        if match:
            return match.group(1)
    return None


def socket_mode_connection_error_message(exc: BaseException) -> str:
    error_code = slack_api_error_code(exc)
    if error_code == "not_allowed_token_type":
        return (
            "Slack Socket Mode could not start because SLACK_APP_TOKEN has the "
            "wrong token type. SLACK_APP_TOKEN must be an app-level token for "
            "Socket Mode. It should start with xapp- and have the "
            "connections:write scope. "
            "Do not use the bot token (xoxb-) or a user token (xoxp-) here. "
            "Create an App-Level Token in Slack App settings, set it in "
            "slack-ai-bridge/.env as SLACK_APP_TOKEN, then restart bridge.py."
        )
    if error_code:
        return (
            f"Slack Socket Mode could not start. Slack API error: {error_code}. "
            "Check that SLACK_APP_TOKEN is an app-level xapp- token with the "
            "connections:write scope, Socket Mode is enabled, the app has been "
            "installed or reinstalled, and this machine can reach Slack."
        )
    return (
        "Slack Socket Mode could not start because Slack rejected the connection "
        "request. Check that SLACK_APP_TOKEN is an app-level xapp- token with "
        "the connections:write scope, Socket Mode is enabled, the app has been "
        "installed or reinstalled, and this machine can reach Slack."
    )


def run_socket_mode(config: BridgeConfig, app_token: str) -> None:
    if not app_token:
        raise ConfigError(
            "Missing Slack app-level token env var: SLACK_APP_TOKEN. "
            "Set it for Socket Mode, or start with --http-mode to use HTTP "
            "Request URL mode."
        )

    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.errors import SlackApiError
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "Socket Mode requires the slack_sdk package. "
            "Install it with: python3 -m pip install -r requirements.txt"
        ) from exc

    app = BridgeApp(config)
    client = SocketModeClient(app_token=app_token)
    bot_token = os.environ.get(config.bot_token_env, "")
    web_client = WebClient(token=bot_token) if bot_token else None

    def process(client_arg: object, request: object) -> None:
        handle_socket_mode_request(
            app,
            client_arg,
            request,
            SocketModeResponse,
            web_client=web_client,
        )

    client.socket_mode_request_listeners.append(process)
    try:
        client.connect()
    except SlackApiError as exc:
        raise ConfigError(socket_mode_connection_error_message(exc)) from exc
    print("Slack AI tools bridge listening with Socket Mode")
    print("Slash commands: " + ", ".join(config.command_tools))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        client.close()


class SlackCommandHandler(BaseHTTPRequestHandler):
    app: BridgeApp

    def do_GET(self) -> None:
        if self.path == "/health":
            self.write_json(200, {"ok": "true"})
            return
        self.write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path != "/slack/commands":
            self.write_json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.write_json(400, slack_response("Invalid Content-Length."))
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.write_json(413, slack_response("Request body too large."))
            return
        body = self.rfile.read(length)
        status, payload = self.app.handle_request(body, dict(self.headers.items()))
        self.write_json(status, payload)

    def write_json(self, status: int, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), format % args)
        )


def run_server(config: BridgeConfig) -> None:
    app = BridgeApp(config)
    SlackCommandHandler.app = app
    server = ThreadingHTTPServer((config.host, config.port), SlackCommandHandler)
    print(f"Slack AI bridge listening on http://{config.host}:{config.port}")
    print("Slash command endpoint: /slack/commands")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Run the Slack AI bridge.")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("BRIDGE_ENV_FILE", str(script_dir / ".env")),
        help="Path to a .env file. Defaults to slack-ai-bridge/.env.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("BRIDGE_CONFIG", str(script_dir / "config.yaml")),
        help="Path to config.yaml. Defaults to slack-ai-bridge/config.yaml.",
    )
    parser.add_argument(
        "--app-token-env",
        default="SLACK_APP_TOKEN",
        help="Environment variable containing the xapp- Socket Mode token.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--socket-mode",
        dest="mode",
        action="store_const",
        const="socket",
        default="socket",
        help="Receive Slack slash commands over Socket Mode. This is the default.",
    )
    mode.add_argument(
        "--http-mode",
        "--http",
        dest="mode",
        action="store_const",
        const="http",
        help="Receive Slack slash commands over the HTTP Request URL endpoint.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    load_env_file(Path(args.env_file))
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if args.mode == "http":
        run_server(config)
        return 0
    if args.mode == "socket":
        try:
            run_socket_mode(config, os.environ.get(args.app_token_env, ""))
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 2
        return 0
    print(f"Config error: unsupported mode: {args.mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())



